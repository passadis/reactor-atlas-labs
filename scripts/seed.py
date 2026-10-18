"""Load the ATLAS seed data into Cosmos DB and Azure AI Search.

    uv run python scripts/seed.py                 # everything
    uv run python scripts/seed.py --skip-search   # Cosmos only
    uv run python scripts/seed.py --force-embed   # re-embed every document

Idempotent by construction:

- Every Cosmos document has a deterministic id, and writes are upserts, so a re-run overwrites in
  place and never duplicates.
- Every search document carries `content_hash`. A re-run embeds only documents whose text changed,
  and deletes index documents that no longer exist in the seed. With unchanged seed data, the
  second run embeds nothing.

Containers are NOT created here. They are provisioned by infra/modules/data.bicep, because container
creation is a control-plane operation and the operator holds only a data-plane role; a seeder that
tried would 403 (CLAUDE.md section 7.9). A missing container is reported with that instruction.

Authentication is Entra only, through `atlas_common.auth`. There are no keys to load.
"""

from __future__ import annotations

import argparse
import asyncio
import json
import os
import sys
import time
from collections.abc import Callable, Iterable, Sequence
from dataclasses import dataclass, field
from datetime import date
from pathlib import Path
from typing import Any

from atlas_agent.instructions import CONTAINER as INSTRUCTIONS_CONTAINER
from atlas_agent.instructions import instruction_documents
from atlas_common.auth import SCOPE_FOUNDRY, get_async_credential, get_credential
from atlas_common.config import EMBEDDING_DIMENSIONS, Settings
from atlas_common.knowledge import (
    content_hash,
    embedding_text,
    incident_documents,
    parse_front_matter,
    parse_list,
    postmortem_documents,
    runbook_documents,
)
from atlas_common.models import (
    Change,
    Entitlement,
    Incident,
    KnowledgeDocument,
    Person,
    RunbookFrontMatter,
    Service,
    TelemetryWindow,
)
from atlas_common.repositories import CosmosStore
from atlas_common.search_index import build_knowledge_index
from azure.core.exceptions import ResourceNotFoundError
from azure.cosmos.aio import CosmosClient
from azure.identity import get_bearer_token_provider
from azure.search.documents import SearchClient
from azure.search.documents.indexes import SearchIndexClient
from openai import OpenAI
from pydantic import BaseModel

COSMOS_CONCURRENCY = 16
EMBED_BATCH = 32
UPLOAD_BATCH = 100
# Explicit timeouts on every external call (CLAUDE.md section 6).
EMBED_TIMEOUT_S = 60.0
EMBED_MAX_RETRIES = 8


# ---------------------------------------------------------------------------- loading --


@dataclass
class SeedData:
    services: list[Service]
    incidents: list[Incident]
    changes: list[Change]
    telemetry: list[TelemetryWindow]
    people: list[Person]
    entitlements: list[Entitlement]
    runbooks: list[tuple[RunbookFrontMatter, str]]
    manifest: dict[str, Any]


def _jsonl[M: BaseModel](path: Path, model: type[M]) -> list[M]:
    return [
        model.model_validate_json(line) for line in path.read_text("utf-8").splitlines() if line
    ]


def _json[M: BaseModel](path: Path, model: type[M]) -> list[M]:
    return [model.model_validate(row) for row in json.loads(path.read_text("utf-8"))]


def _runbooks(folder: Path) -> list[tuple[RunbookFrontMatter, str]]:
    out: list[tuple[RunbookFrontMatter, str]] = []
    for path in sorted(folder.glob("*.md")):
        markdown = path.read_text("utf-8")
        fields, _ = parse_front_matter(markdown)
        out.append(
            (
                RunbookFrontMatter(
                    runbook_id=fields["runbook_id"],
                    title=fields["title"],
                    services=parse_list(fields["services"]),
                    severity_applicability=parse_list(fields["severity_applicability"]),  # type: ignore[arg-type]
                    last_reviewed=date.fromisoformat(fields["last_reviewed"]),
                    owner_team=fields["owner_team"],
                    risk=fields["risk"],  # type: ignore[arg-type]
                    requires_approval=fields["requires_approval"] == "true",
                    steps_are_destructive=fields["steps_are_destructive"] == "true",
                    injection_canary=fields.get("injection_canary") == "true",
                ),
                markdown,
            )
        )
    return out


def load_seed(seed_dir: Path) -> SeedData:
    if not (seed_dir / "manifest.json").exists():
        raise SystemExit(
            f"{seed_dir}/manifest.json not found. Generate first:\n"
            "  uv run python -m data.generators.generate_all --seed 20260914 --out data/seed"
        )
    return SeedData(
        services=_json(seed_dir / "services.json", Service),
        incidents=_jsonl(seed_dir / "incidents.jsonl", Incident),
        changes=_jsonl(seed_dir / "changes.jsonl", Change),
        telemetry=_jsonl(seed_dir / "telemetry.jsonl", TelemetryWindow),
        people=_json(seed_dir / "people.json", Person),
        entitlements=_json(seed_dir / "entitlements.json", Entitlement),
        runbooks=_runbooks(seed_dir / "runbooks"),
        manifest=json.loads((seed_dir / "manifest.json").read_text("utf-8")),
    )


# ---------------------------------------------------------------------------- cosmos --


@dataclass
class CosmosPlan:
    container: str
    rows: Sequence[BaseModel]
    document_id: Callable[[Any], str]
    extra: dict[str, Any] = field(default_factory=dict)


def cosmos_plans(data: SeedData) -> list[CosmosPlan]:
    return [
        CosmosPlan("services", data.services, lambda r: r.service_id),
        # Seeded incidents are marked so `make demo-reset` can delete only the incidents a demo
        # created and leave the 500 historical ones alone (DATA-MODEL.md, Seeding).
        CosmosPlan("incidents", data.incidents, lambda r: r.incident_id, {"source": "seed"}),
        CosmosPlan("changes", data.changes, lambda r: r.change_id),
        CosmosPlan(
            "telemetry",
            data.telemetry,
            lambda r: f"{r.service_id}_{r.window_start.strftime('%Y%m%dT%H%M')}",
        ),
        CosmosPlan("people", data.people, lambda r: r.user_id),
        CosmosPlan("entitlements", data.entitlements, lambda r: r.role),
    ]


async def seed_cosmos(settings: Settings, plans: Iterable[CosmosPlan]) -> dict[str, int]:
    """Upsert every plan concurrently. Returns rows written per container."""
    written: dict[str, int] = {}
    credential = get_async_credential()
    try:
        async with CosmosClient(settings.cosmos_endpoint, credential=credential) as client:
            database = client.get_database_client(settings.cosmos_database)
            semaphore = asyncio.Semaphore(COSMOS_CONCURRENCY)

            for plan in plans:
                container = database.get_container_client(plan.container)
                try:
                    await container.read()
                except ResourceNotFoundError:
                    raise SystemExit(
                        f"Cosmos container '{plan.container}' does not exist. Containers are "
                        "provisioned by infra/modules/data.bicep, not by this script. Run "
                        "`azd provision`."
                    ) from None

                async def upsert(
                    row: BaseModel, _c: Any = container, _p: CosmosPlan = plan
                ) -> None:
                    body = row.model_dump(mode="json")
                    body["id"] = _p.document_id(row)
                    body.update(_p.extra)
                    async with semaphore:
                        # The SDK retries 429s itself; autoscale throughput absorbs the burst.
                        await _c.upsert_item(body)

                started = time.perf_counter()
                await asyncio.gather(*(upsert(row) for row in plan.rows))
                written[plan.container] = len(plan.rows)
                print(
                    f"  cosmos {plan.container:<14} {len(plan.rows):>5} upserted "
                    f"({time.perf_counter() - started:.1f}s)"
                )
    finally:
        await credential.close()
    return written


async def seed_instructions(settings: Settings) -> int:
    """Copy the instruction versions from the package into Cosmos (CLAUDE.md section 9.1).

    Also resets the active pointer to ACTIVE_BASELINE (v2 since Phase 12). Seeding is a T-10
    operation, so it doubles as the baseline restore for the instruction-version switch.
    """
    docs = instruction_documents()
    async with CosmosStore(settings) as store:
        for doc in docs:
            await store.upsert(INSTRUCTIONS_CONTAINER, doc)
    versions = sorted(d["version"] for d in docs if not d["id"].endswith(":active"))
    print(f"  cosmos {INSTRUCTIONS_CONTAINER:<18} {len(docs):>3} upserted (versions {versions})")
    return len(docs)


# ---------------------------------------------------------------------------- search --


def knowledge_url(web_url: str, doc_id: str) -> str:
    """Where a citation points: the web app's knowledge page for the document (Phase 04)."""
    return f"{web_url.rstrip('/')}/kb/{doc_id}" if web_url else f"atlas://kb/{doc_id}"


def knowledge_documents(data: SeedData, web_url: str = "") -> list[KnowledgeDocument]:
    docs = [
        *runbook_documents(data.runbooks),
        *incident_documents(data.incidents),
        *postmortem_documents(data.incidents),
    ]
    for doc in docs:
        doc.content_hash = content_hash(embedding_text(doc))
        doc.url = knowledge_url(web_url, doc.doc_id)
    return docs


def existing_state(client: SearchClient) -> dict[str, tuple[str, str]]:
    """id -> (content_hash, url) for everything already in the index."""
    return {
        str(hit["id"]): (str(hit.get("content_hash") or ""), str(hit.get("url") or ""))
        for hit in client.search(search_text="*", select=["id", "content_hash", "url"])
    }


def embed(openai: OpenAI, model: str, texts: list[str]) -> list[list[float]]:
    vectors: list[list[float]] = []
    for start in range(0, len(texts), EMBED_BATCH):
        batch = texts[start : start + EMBED_BATCH]
        response = openai.embeddings.create(model=model, input=batch)
        for item in sorted(response.data, key=lambda d: d.index):
            if len(item.embedding) != EMBEDDING_DIMENSIONS:
                raise SystemExit(
                    f"embedding has {len(item.embedding)} dimensions, index expects "
                    f"{EMBEDDING_DIMENSIONS}. Wrong model behind '{model}'?"
                )
            vectors.append(item.embedding)
        print(f"  embedded {min(start + EMBED_BATCH, len(texts))}/{len(texts)}", end="\r")
    if texts:
        print()
    return vectors


def seed_search(settings: Settings, data: SeedData, force_embed: bool) -> dict[str, int]:
    credential = get_credential()
    index_client = SearchIndexClient(settings.search_endpoint, credential)
    index_client.create_or_update_index(
        build_knowledge_index(
            settings.knowledge_index,
            vectorizer_resource_url=settings.foundry_openai_resource_url,
            embedding_deployment=settings.model_embed,
        )
    )
    print(f"  search index '{settings.knowledge_index}' created or updated")

    search = SearchClient(settings.search_endpoint, settings.knowledge_index, credential)
    docs = knowledge_documents(data, os.environ.get("ATLAS_WEB_URL", ""))
    state = existing_state(search)
    current = {doc_id: pair[0] for doc_id, pair in state.items()}

    changed = [d for d in docs if force_embed or current.get(d.id) != d.content_hash]
    changed_ids = {d.id for d in changed}
    # Metadata-only drift (today: the citation url) is a merge, never a re-embed.
    relinked = [
        {"id": d.id, "url": d.url}
        for d in docs
        if d.id not in changed_ids and d.id in state and state[d.id][1] != d.url
    ]
    stale_ids = sorted(set(current) - {d.id for d in docs})
    print(
        f"  {len(docs)} documents: {len(changed)} to embed, "
        f"{len(docs) - len(changed)} unchanged, {len(stale_ids)} to delete"
    )

    if changed:
        openai = OpenAI(
            base_url=settings.foundry_openai_base_url,
            api_key=get_bearer_token_provider(credential, SCOPE_FOUNDRY),
            timeout=EMBED_TIMEOUT_S,
            max_retries=EMBED_MAX_RETRIES,
        )
        vectors = embed(openai, settings.model_embed, [embedding_text(d) for d in changed])
        for doc, vector in zip(changed, vectors, strict=True):
            doc.content_vector = vector
        for start in range(0, len(changed), UPLOAD_BATCH):
            batch = [d.model_dump() for d in changed[start : start + UPLOAD_BATCH]]
            results = search.merge_or_upload_documents(batch)
            failed = [r.key for r in results if not r.succeeded]
            if failed:
                raise SystemExit(f"search upload failed for {failed[:5]}")

    for start in range(0, len(relinked), UPLOAD_BATCH):
        results = search.merge_documents(relinked[start : start + UPLOAD_BATCH])
        failed = [r.key for r in results if not r.succeeded]
        if failed:
            raise SystemExit(f"search metadata merge failed for {failed[:5]}")

    if stale_ids:
        search.delete_documents([{"id": i} for i in stale_ids])

    return {
        "documents": len(docs),
        "embedded": len(changed),
        "relinked": len(relinked),
        "deleted": len(stale_ids),
    }


# ---------------------------------------------------------------------------- main --


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        description=__doc__, formatter_class=argparse.RawTextHelpFormatter
    )
    parser.add_argument("--seed-dir", type=Path, default=Path("data/seed"))
    parser.add_argument("--skip-cosmos", action="store_true")
    parser.add_argument("--skip-search", action="store_true")
    parser.add_argument("--force-embed", action="store_true", help="re-embed every document")
    args = parser.parse_args(argv)

    settings = Settings.from_env()
    data = load_seed(args.seed_dir)
    print(
        f"ATLAS seed  (seed={data.manifest['seed']}, reference={data.manifest['reference_date']})"
    )

    started = time.perf_counter()
    cosmos: dict[str, int] = {}
    search: dict[str, int] = {}
    if not args.skip_cosmos:
        cosmos = asyncio.run(seed_cosmos(settings, cosmos_plans(data)))
        cosmos[INSTRUCTIONS_CONTAINER] = asyncio.run(seed_instructions(settings))
    if not args.skip_search:
        search = seed_search(settings, data, args.force_embed)

    print("\nSUMMARY")
    print(f"  {'target':<26}{'count':>8}")
    for name, count in cosmos.items():
        print(f"  {'cosmos/' + name:<26}{count:>8}")
    if search:
        print(f"  {'search/' + settings.knowledge_index:<26}{search['documents']:>8}")
        print(f"  {'  embedded this run':<26}{search['embedded']:>8}")
        print(f"  {'  deleted this run':<26}{search['deleted']:>8}")
    print(f"\ncompleted in {time.perf_counter() - started:.1f}s")
    return 0


if __name__ == "__main__":
    sys.exit(main())
