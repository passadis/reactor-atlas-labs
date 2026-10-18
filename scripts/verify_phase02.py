"""Phase 02 verification against the deployed Cosmos DB and AI Search.

Read-only except for check 5, which re-runs the seeder and proves it changes nothing.
Exits non-zero on any failure. Wrapped by scripts/verify_phase02.sh for `make verify-all`.
"""

from __future__ import annotations

import asyncio
import hashlib
import json
import subprocess
import sys
from pathlib import Path
from typing import Any

from atlas_common.auth import SCOPE_FOUNDRY, get_credential
from atlas_common.config import Settings
from atlas_common.graph import blast_radius
from atlas_common.models import Service
from atlas_common.repositories import CosmosStore
from atlas_common.search_index import SEMANTIC_CONFIG
from azure.identity import get_bearer_token_provider
from azure.search.documents import SearchClient
from azure.search.documents.models import VectorizedQuery
from openai import OpenAI

from scripts.seed import knowledge_documents, load_seed

SEED = Path("data/seed")
FIXTURES = Path("tests/fixtures")
CANONICAL_QUERY = "checkout latency after payments deploy"

# container -> query that counts only SEEDED documents. Incidents raised during a demo carry no
# `source` marker, so they never skew the count.
COUNT_QUERIES = {
    "services": "SELECT VALUE COUNT(1) FROM c",
    "incidents": "SELECT VALUE COUNT(1) FROM c WHERE c.source = 'seed'",
    "changes": "SELECT VALUE COUNT(1) FROM c",
    "telemetry": "SELECT VALUE COUNT(1) FROM c",
    "people": "SELECT VALUE COUNT(1) FROM c",
    "entitlements": "SELECT VALUE COUNT(1) FROM c",
}
SYSTEM_FIELDS = {"_rid", "_self", "_etag", "_attachments", "_ts", "_lsn"}

rows: list[tuple[str, str, str]] = []


def record(ok: bool, check: str, detail: str) -> None:
    rows.append(("PASS" if ok else "FAIL", check, detail))


async def cosmos_counts(settings: Settings) -> dict[str, int]:
    async with CosmosStore(settings) as store:
        out: dict[str, int] = {}
        for container, sql in COUNT_QUERIES.items():
            result = await store.query(container, sql, timeout_s=30)
            out[container] = int(result[0])
        return out


async def cosmos_fingerprint(settings: Settings) -> str:
    """Hash every seeded document, ignoring Cosmos system fields such as _ts."""
    digest = hashlib.sha256()
    async with CosmosStore(settings) as store:
        for container in COUNT_QUERIES:
            docs = await store.query(container, "SELECT * FROM c", timeout_s=120)
            for doc in sorted(docs, key=lambda d: str(d["id"])):
                clean = {k: v for k, v in doc.items() if k not in SYSTEM_FIELDS}
                digest.update(json.dumps(clean, sort_keys=True).encode())
    return digest.hexdigest()


def canonical_search(settings: Settings, doc_filter: str | None) -> list[dict[str, Any]]:
    credential = get_credential()
    openai = OpenAI(
        base_url=settings.foundry_openai_base_url,
        api_key=get_bearer_token_provider(credential, SCOPE_FOUNDRY),
        timeout=30,
    )
    vector = openai.embeddings.create(model=settings.model_embed, input=[CANONICAL_QUERY])
    client = SearchClient(settings.search_endpoint, settings.knowledge_index, credential)
    results = client.search(
        search_text=CANONICAL_QUERY,
        vector_queries=[
            VectorizedQuery(
                vector=vector.data[0].embedding, k_nearest_neighbors=50, fields="content_vector"
            )
        ],
        query_type="semantic",
        semantic_configuration_name=SEMANTIC_CONFIG,
        filter=doc_filter,
        select=["doc_id", "doc_type", "services"],
        top=3,
    )
    return [dict(r) for r in results]


def main() -> int:
    settings = Settings.from_env()
    manifest = json.loads((SEED / "manifest.json").read_text())
    expected = manifest["counts"]

    # 1 -- Cosmos counts match generated counts exactly -------------------------------------------
    counts = asyncio.run(cosmos_counts(settings))
    for container, actual in counts.items():
        want = expected[container]
        record(actual == want, f"1 cosmos {container}", f"{actual} (expected {want})")

    # 2 -- Search document count and the canonical query ------------------------------------------
    expected_docs = len(knowledge_documents(load_seed(SEED)))
    client = SearchClient(settings.search_endpoint, settings.knowledge_index, get_credential())
    indexed = client.get_document_count()
    record(
        indexed == expected_docs, "2 search document count", f"{indexed} (expected {expected_docs})"
    )

    unscoped = canonical_search(settings, None)
    hit = next(
        (r for r in unscoped if r["doc_type"] == "runbook" and "svc-checkout-api" in r["services"]),
        None,
    )
    record(
        hit is not None,
        "2 canonical query, unscoped top 3",
        f"runbook {hit['doc_id']} present"
        if hit
        else f"no checkout runbook in {[r['doc_id'] for r in unscoped]}",
    )
    scoped = canonical_search(settings, "doc_type eq 'runbook'")
    first = scoped[0]["doc_id"] if scoped else None
    record(first == "RB-0042", "2 canonical query, runbooks scoped", f"#1 = {first}")

    # 3 -- blast radius matches the frozen fixture ------------------------------------------------
    services = [Service.model_validate(s) for s in json.loads((SEED / "services.json").read_text())]
    frozen = json.loads((FIXTURES / "blast_radius_checkout.json").read_text())
    actual_br = blast_radius("svc-checkout-api", services, depth=3).model_dump(mode="json")
    record(
        actual_br == frozen,
        "3 blast_radius(svc-checkout-api)",
        f"{actual_br['affected_count']} services",
    )

    # 4 -- the injection canaries are findable by filter -----------------------------------------
    canaries = sorted(
        {
            r["doc_id"]
            for r in client.search(
                search_text="*", filter="injection_canary eq true", select=["doc_id"]
            )
        }
    )
    want_canaries = manifest["injection_canaries"]
    record(canaries == want_canaries, "4 injection canaries", f"{canaries}")

    # 5 -- re-running the seeder changes nothing --------------------------------------------------
    before = asyncio.run(cosmos_fingerprint(settings))
    reseed = subprocess.run(
        [sys.executable, "-m", "scripts.seed"], capture_output=True, text=True, check=False
    )
    after = asyncio.run(cosmos_fingerprint(settings))
    embedded = next(
        (line.split()[-1] for line in reseed.stdout.splitlines() if "embedded this run" in line),
        "?",
    )
    record(reseed.returncode == 0, "5 seed.py re-run exit code", str(reseed.returncode))
    record(
        before == after,
        "5 cosmos unchanged by re-seed",
        "fingerprint identical" if before == after else "DRIFT",
    )
    record(embedded == "0", "5 search unchanged by re-seed", f"{embedded} documents re-embedded")

    # -- render ----------------------------------------------------------------------------------
    print(f"{'STATUS':<7}{'CHECK':<40}DETAIL")
    print(f"{'-' * 6:<7}{'-' * 38:<40}------")
    for status, check, detail in rows:
        print(f"{status:<7}{check:<40}{detail}")
    failed = sum(1 for s, _, _ in rows if s == "FAIL")
    print(f"\npass={len(rows) - failed}  fail={failed}")
    print("Phase 02 verification " + ("FAILED." if failed else "PASSED."))
    return 1 if failed else 0


if __name__ == "__main__":
    sys.exit(main())
