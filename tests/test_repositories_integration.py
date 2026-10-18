"""Repository integration tests against the DEPLOYED Cosmos DB and AI Search.

CLAUDE.md section 4: phase acceptance is demonstrated against deployed Azure resources, not stubs.
These run whenever `.env.local` points at a provisioned environment and skip cleanly otherwise, so
an offline CI job stays green without pretending to have tested Azure.
"""

from __future__ import annotations

import dataclasses
import os
from datetime import UTC, datetime, timedelta

import pytest
from atlas_common.config import Settings, load_local_env
from atlas_common.repositories import (
    ChangeRepository,
    CosmosStore,
    EntitlementRepository,
    KnowledgeSearch,
    RepositoryUnavailableError,
    ServiceRepository,
    TelemetryRepository,
)

load_local_env()
pytestmark = [
    pytest.mark.integration,
    pytest.mark.skipif(
        not os.environ.get("ATLAS_COSMOS_ENDPOINT"),
        reason="no provisioned environment (.env.local missing)",
    ),
]

INCIDENT_AT = datetime(2026, 9, 1, 14, 5, tzinfo=UTC)


async def test_service_lookup_and_not_found() -> None:
    async with CosmosStore(Settings.from_env()) as store:
        repo = ServiceRepository(store)
        svc = await repo.get("svc-checkout-api")
        assert svc is not None and svc.tier == 1
        assert await repo.get("svc-does-not-exist") is None


async def test_canonical_change_is_the_only_recent_payments_change() -> None:
    """The thread the agent must pull, found by code rather than guessed by a model."""
    async with CosmosStore(Settings.from_env()) as store:
        changes = await ChangeRepository(store).recent("svc-payments", INCIDENT_AT, 240)
    assert [c.change_id for c in changes] == ["CHG-2026-0873"]
    assert changes[0].risk == "high" and changes[0].rollback_available


async def test_checkout_telemetry_shows_the_incident() -> None:
    async with CosmosStore(Settings.from_env()) as store:
        windows = await TelemetryRepository(store).windows(
            "svc-checkout-api", INCIDENT_AT + timedelta(minutes=30), 60
        )
    assert any(w.anomaly for w in windows)
    assert any(not w.anomaly for w in windows), "the before-picture must be visible too"


async def test_readonly_persona_is_powerless() -> None:
    async with CosmosStore(Settings.from_env()) as store:
        ent = await EntitlementRepository(store).for_role("readonly")
    assert ent is not None
    assert ent.may_execute is False
    assert ent.may_approve_tiers == [] and ent.max_blast_radius == 0


async def test_scoped_search_ranks_rb0042_first() -> None:
    async with KnowledgeSearch(Settings.from_env()) as ks:
        results = await ks.search(
            "checkout latency after payments deploy", doc_types=["runbook"], top=3
        )
    assert results.citations[0] == "RB-0042"


async def test_dead_backend_is_a_typed_error_not_a_hang() -> None:
    """CLAUDE.md section 6: a dead backend must degrade visibly, never hang or leak a trace."""
    bad = dataclasses.replace(
        Settings.from_env(),
        cosmos_endpoint="https://cosmos-atlas-doesnotexist.documents.azure.com:443/",
    )
    with pytest.raises(RepositoryUnavailableError) as info:
        async with CosmosStore(bad) as store:
            await store.query("services", "SELECT * FROM c", timeout_s=5)
    assert info.value.backend == "Cosmos services"
