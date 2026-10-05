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


def test_a_date_without_a_time_blocks_the_whole_day_and_the_margins() -> None:
    begin, end = ci_blackout.window("2026-10-06")
    # 00:00 UTC+3 minus 6 h, to 24:00 UTC+3 plus 6 h: the 03:00 UTC nightly on the day is inside
    assert begin == datetime(2026, 10, 5, 15, tzinfo=UTC)
    assert end == datetime(2026, 10, 7, 3, tzinfo=UTC)
    nightly_on_the_day = datetime(2026, 10, 6, 3, tzinfo=UTC)
    nightly_next_day = datetime(2026, 10, 7, 3, tzinfo=UTC)
    assert begin <= nightly_on_the_day < end
    assert not begin <= nightly_next_day < end
