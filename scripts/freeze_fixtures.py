"""Freeze the golden fixtures that Episode 4's regression tests compare against.

    uv run python scripts/freeze_fixtures.py            # write tests/fixtures/*.json
    uv run python scripts/freeze_fixtures.py --check    # exit 1 if a fixture would change

Fixtures are generated, never hand-edited, so re-freezing is a deliberate, reviewable act: if the
search reranker drifts and RB-0042 moves, `--check` fails and someone decides consciously whether
the new ranking is acceptable, rather than a test quietly being updated to match.

Blast-radius fixtures come from the committed seed alone (no Azure). The knowledge fixture needs the
live index, because what it freezes is what retrieval actually returns.
"""

from __future__ import annotations

import argparse
import asyncio
import json
import sys
from pathlib import Path
from typing import Any

from atlas_common.config import Settings
from atlas_common.graph import blast_radius
from atlas_common.models import Service
from atlas_common.repositories import KnowledgeSearch

FIXTURES = Path("tests/fixtures")
SEED = Path("data/seed")

CANONICAL_PROMPT = (
    "Checkout is timing out for about 20% of EU customers since 14:05, "
    "started right after the payments deploy."
)
CANONICAL_QUERY = "checkout latency after payments deploy"


def _services() -> list[Service]:
    return [Service.model_validate(s) for s in json.loads((SEED / "services.json").read_text())]


def canonical_incident() -> dict[str, Any]:
    manifest = json.loads((SEED / "manifest.json").read_text())
    return {
        "prompt": CANONICAL_PROMPT,
        "incident_at": manifest["canonical"]["incident_at"],
        # What a correct triage must conclude. ARCHITECTURE.md, "Data flow for one incident".
        "expected": {
            "primary_service": "svc-checkout-api",
            "suspected_cause_service": manifest["canonical"]["change_service"],
            "suspected_change_ids": [manifest["canonical"]["change_id"]],
            "severity": "Sev2",
            "category": "latency",
            "must_cite": [manifest["canonical"]["runbook_id"]],
        },
    }


def blast(service_id: str) -> dict[str, Any]:
    return blast_radius(service_id, _services(), depth=3).model_dump(mode="json")


async def knowledge_top3() -> dict[str, Any]:
    async with KnowledgeSearch(Settings.from_env()) as ks:
        scoped = await ks.search(CANONICAL_QUERY, doc_types=["runbook"], top=3)
    return {
        "query": CANONICAL_QUERY,
        "scope": "doc_type eq 'runbook'",
        "top3": scoped.citations,
        # The hard requirement. The other two may legitimately drift; this one may not.
        "must_rank_first": "RB-0042",
    }


def build(include_live: bool) -> dict[str, dict[str, Any]]:
    fixtures = {
        "canonical_incident.json": canonical_incident(),
        "blast_radius_checkout.json": blast("svc-checkout-api"),
        "blast_radius_payments.json": blast("svc-payments"),
    }
    if include_live:
        fixtures["knowledge_top3.json"] = asyncio.run(knowledge_top3())
    return fixtures


def _dump(payload: dict[str, Any]) -> str:
    return json.dumps(payload, indent=2, sort_keys=True) + "\n"


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--check", action="store_true", help="fail if any fixture would change")
    parser.add_argument("--offline", action="store_true", help="skip fixtures needing Azure")
    args = parser.parse_args(argv)

    fixtures = build(include_live=not args.offline)
    drift: list[str] = []
    for name, payload in fixtures.items():
        path = FIXTURES / name
        text = _dump(payload)
        if args.check:
            if not path.exists() or path.read_text(encoding="utf-8") != text:
                drift.append(name)
            continue
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(text, encoding="utf-8", newline="\n")
        print(f"  froze {path}")

    if args.check:
        if drift:
            print(f"fixture drift: {drift}. Re-freeze deliberately if the change is intended.")
            return 1
        print(f"all {len(fixtures)} fixtures match")
    return 0


if __name__ == "__main__":
    sys.exit(main())
