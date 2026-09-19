"""Phase 03 acceptance, live: the incident agent against the deployed model, Cosmos and Search.

The canonical incident must come back with the right shape every time: Sev2, checkout affected, the
payments change suspected, grounded in RB-0042. The follow-up must resolve "the service that caused
this" from a session that went through Cosmos, as a restarted process would.

Skips cleanly without a provisioned environment. Costs roughly 40 seconds of model time.
"""

from __future__ import annotations

import os
import uuid

import pytest
from atlas_agent.factory import IncidentAgentRuntime, answer, triage
from atlas_agent.schemas import IncidentTriage
from atlas_agent.tools import TOOL_NAMES
from atlas_common.config import Settings, load_local_env

load_local_env()
pytestmark = [
    pytest.mark.integration,
    pytest.mark.slow,
    pytest.mark.skipif(
        not os.environ.get("FOUNDRY_PROJECT_ENDPOINT"),
        reason="no provisioned environment (.env.local missing)",
    ),
]

CANONICAL = (
    "Checkout is timing out for about 20% of EU customers since 14:05, "
    "started right after the payments deploy."
)


async def test_canonical_triage_and_coreference_across_a_restart() -> None:
    session_id = f"test-{uuid.uuid4().hex[:12]}"

    async with IncidentAgentRuntime(Settings.from_env()) as runtime:
        agent, loaded = await runtime.agent("v1")
        assert loaded.source == "cosmos"
        session = agent.create_session()
        result = await triage(agent, CANONICAL, session)
        await runtime.sessions.save(session_id, session)

    triage_out = result.output
    assert isinstance(triage_out, IncidentTriage), triage_out
    assert triage_out.severity == "Sev2", triage_out.reasoning_notes
    assert "svc-checkout-api" in triage_out.affected_services
    assert "CHG-2026-0873" in triage_out.suspected_change_ids
    cited = {c.doc_id for c in triage_out.citations}
    assert "RB-0042" in cited
    assert len(cited) >= 2
    assert set(result.tool_calls) <= set(TOOL_NAMES)
    assert {"get_recent_changes", "get_telemetry", "compute_blast_radius"} <= set(result.tool_calls)

    # A fresh runtime - new clients, new credential - as after a process restart.
    async with IncidentAgentRuntime(Settings.from_env()) as runtime:
        try:
            restored = await runtime.sessions.load(session_id)
            assert restored is not None
            agent, _ = await runtime.agent("v1")
            turn = await answer(
                agent,
                "Is the service that caused this under a change freeze, and which team owns it?",
                restored,
            )
        finally:
            await runtime.sessions.expire(session_id)

    assert turn.failure is None, turn.text
    text = turn.text.lower()
    assert "team-payments" in text
    assert "freeze" in text
    assert "svc-payments" in text
