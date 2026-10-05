"""Phase 13 CI helpers: the APIM policy check, the eval-required rule, the blackout window."""

from __future__ import annotations

import sys
import xml.etree.ElementTree as ET
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "scripts"))
import ci_blackout
import ci_eval_required
from ci_policy_xml import neutralise


def test_policy_expressions_with_quotes_and_operators_are_accepted() -> None:
    policy = ('<policies><inbound><set-header name="x"><value>@(context.Request.Headers'
              '.GetValueOrDefault("a", "b") ?? "c")</value></set-header>'
              '<choose><when condition="@(context.Response.StatusCode >= 500 &amp;&amp; '
              'context.Request.Method == "GET")"><return-response /></when></choose>'
              '<set-body>@{ var s = "}{\\"" ; return s; }</set-body></inbound></policies>')
    ET.fromstring(neutralise(policy))  # noqa: S314 - literal test input


@pytest.mark.parametrize("broken", [
    '<policies><inbound><value>@(context.Request</value></inbound></policies>',
    '<policies><inbound><value>@(ok())</value></inbound>',
])
def test_a_broken_policy_is_rejected(broken: str) -> None:
    with pytest.raises((ET.ParseError, ValueError)):
        ET.fromstring(neutralise(broken))  # noqa: S314 - literal test input


def test_changed_instructions_without_an_eval_fail(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(ci_eval_required, "changed", lambda _base: [
        "src/atlas_agent/atlas_agent/instructions/atlas_incident_agent.v2.md"])
    monkeypatch.setattr(sys, "argv", ["x", "origin/main"])
    assert ci_eval_required.main() == 1


def test_changed_instructions_with_an_eval_pass(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(ci_eval_required, "changed", lambda _base: [
        "src/atlas_agent/atlas_agent/instructions/atlas_incident_agent.v2.md",
        "evals/reports/scorecard-v1-v2.md"])
    monkeypatch.setattr(sys, "argv", ["x", "origin/main"])
    assert ci_eval_required.main() == 0


def test_the_blackout_window_blocks_within_six_hours(tmp_path: Path,
                                                    monkeypatch: pytest.MonkeyPatch) -> None:
    soon = (datetime.now(UTC) + timedelta(hours=5)).isoformat()
    later = (datetime.now(UTC) + timedelta(hours=9)).isoformat()
    f = tmp_path / "episodes.yaml"
    f.write_text(f'episodes:\n  - starts: "{soon}"\n')
    monkeypatch.setattr(sys, "argv", ["x", str(f)])
    assert ci_blackout.main() == 1
    f.write_text(f'episodes:\n  - starts: "{later}"\n  - starts: ""\n')
    assert ci_blackout.main() == 0
