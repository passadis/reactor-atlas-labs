"""Golden-fixture tests. Offline: they need only the committed seed data.

Episode 4's regression tests reference these fixtures. If one fails, either the seed changed or the
graph code changed; re-freeze only after deciding the new answer is correct.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest
from atlas_common.graph import blast_radius
from atlas_common.models import Service

FIXTURES = Path("tests/fixtures")


def _services() -> list[Service]:
    return [
        Service.model_validate(s) for s in json.loads(Path("data/seed/services.json").read_text())
    ]


@pytest.mark.parametrize(
    ("service_id", "fixture"),
    [
        ("svc-checkout-api", "blast_radius_checkout.json"),
        ("svc-payments", "blast_radius_payments.json"),
    ],
)
def test_blast_radius_matches_frozen_fixture(service_id: str, fixture: str) -> None:
    expected = json.loads((FIXTURES / fixture).read_text())
    actual = blast_radius(service_id, _services(), depth=3).model_dump(mode="json")
    assert actual == expected


def test_canonical_incident_is_internally_consistent() -> None:
    fx = json.loads((FIXTURES / "canonical_incident.json").read_text())
    changes = [
        json.loads(line) for line in Path("data/seed/changes.jsonl").read_text().splitlines()
    ]
    canonical = next(c for c in changes if c["change_id"] in fx["expected"]["suspected_change_ids"])
    assert canonical["service_id"] == fx["expected"]["suspected_cause_service"]
    assert canonical["risk"] == "high"
    assert canonical["rollback_available"] is True


def test_payments_blast_radius_reaches_checkout() -> None:
    """The canonical narrative in one assertion: payments breaking affects checkout."""
    fx = json.loads((FIXTURES / "blast_radius_payments.json").read_text())
    assert "svc-checkout-api" in fx["affected_service_ids"]


def test_knowledge_fixture_pins_rb0042_first() -> None:
    path = FIXTURES / "knowledge_top3.json"
    if not path.exists():
        pytest.skip("knowledge fixture requires the live index; run scripts/freeze_fixtures.py")
    fx = json.loads(path.read_text())
    assert fx["top3"][0] == fx["must_rank_first"] == "RB-0042"
