"""Record the Episode 3 Beat 4 drill: the ops API loses its Service Bus Sender role.

    uv run python -m scripts.drill_servicebus_role     # about 10-30 minutes; NEVER in a broadcast

Timeline it records (and prints, for docs/assets/fallbacks/ep3-4-revoke-servicebus-role.txt):
1. revoke the role (scripts/chaos/revoke_servicebus_role.sh --apply);
2. probe the real submit path every 60 s until the enqueue is refused - how long a revoked role
   takes to bite a running workload is the point CLAUDE.md 9.4 makes;
3. run a real canonical case, approve it: the dispatch must fail CLOSED - case still `approved`,
   nothing executed, the operation `enqueue_failed`, not `queued`;
4. restore the role, probe until the enqueue works again;
5. retry the dispatch (POST /api/workflow/{id}/dispatch): the case completes.
Always restores the role, whatever happens. Run `make demo-reset` afterwards (the probes stage
RB-0099 plans).
"""

from __future__ import annotations

import shutil
import subprocess
import sys
import time
from datetime import UTC, datetime
from pathlib import Path

from atlas_common.config import load_local_env

from scripts.e2e_remediation import follow, to_gate, web
from scripts.sse_probe import CANONICAL

ROOT = Path(__file__).resolve().parent.parent
T0 = time.perf_counter()


def say(text: str) -> None:
    print(f"{datetime.now(UTC).strftime('%H:%M:%S')}Z  t+{time.perf_counter() - T0:6.0f}s  {text}",
          flush=True)


def role(action: str) -> str:
    out = subprocess.run([shutil.which("bash") or "bash", "scripts/chaos/revoke_servicebus_role.sh",
                          action], cwd=ROOT, capture_output=True, text=True, timeout=180,
                         check=False)
    return (out.stdout.strip().splitlines() or [out.stderr.strip()[-200:]])[-1]


def enqueue_works() -> tuple[bool, str]:
    """The real submit path (agent identity -> APIM -> ops API -> Service Bus), staged cheaply."""
    r = subprocess.run([sys.executable, "-m", "scripts.inject_failing_plan"], cwd=ROOT,
                       capture_output=True, text=True, timeout=180, check=False)
    text = r.stdout.strip().splitlines()
    if text and text[-1].startswith("OP-"):
        return True, text[-1]
    detail = next((ln for ln in text if "refused" in ln), r.stdout[-160:])
    # Only the queue refusing counts as "the revocation took effect". Any other refusal is a
    # different problem and must not be mistaken for it (it was, once: 2026-10-11).
    if "queue_unavailable" not in detail:
        raise RuntimeError(f"the probe failed for another reason: {detail[:200]}")
    return False, detail


def wait_for(want: bool, limit_s: int) -> float | None:
    start = time.perf_counter()
    while time.perf_counter() - start < limit_s:
        ok, detail = enqueue_works()
        say(f"probe: enqueue {'works' if ok else 'REFUSED'} - {detail[:110]}")
        if ok == want:
            return time.perf_counter() - start
        time.sleep(60)
    return None


def main() -> int:
    load_local_env()
    try:
        say(role("--apply"))
        status = role("--status")
        say(status)
        if not status.startswith("REVOKED"):  # it failed silently once (Git Bash path rewrite)
            say("the role was NOT revoked - stopping; nothing was changed")
            return 1
        bite = wait_for(False, 1800)
        if bite is None:
            say("the revocation never reached the running ops API within 30 minutes")
            return 1
        say(f"the revoked role took effect after {bite:.0f} s")
        client = web()
        g = to_gate(client, CANONICAL)
        say(f"canonical case {g['case_id']} parked for {g['parked'].get('required_roles')}")
        r = client.post(f"/api/workflow/{g['case_id']}/decision",
                        json={"decision": "approve", "comment": "drill: role revoked"})
        say(f"approved: HTTP {r.status_code}")
        time.sleep(30)  # the gateway retries a 5xx (1 + 3 with backoff) before the final answer
        doc = client.get(f"/api/workflow/{g['case_id']}").json()
        rm = doc.get("remediation") or {}
        say(f"FAIL CLOSED: case status {doc.get('status')}, remediation {rm.get('status')} - "
            f"{str(rm.get('detail'))[:90]}; operation id {rm.get('operation_id')}")
        say(role("--revert"))
        back = wait_for(True, 1800)
        say(f"the restored role took effect after {back:.0f} s" if back is not None else
            "the restored role did not take effect within 30 minutes")
        r = client.post(f"/api/workflow/{g['case_id']}/dispatch")
        say(f"retry dispatch: HTTP {r.status_code} {str(r.json())[:120]}")
        final = follow(client, g["case_id"], budget_s=240)["doc"]
        frm = final.get("remediation") or {}
        say(f"RECOVERED: case {final.get('status')}, remediation {frm.get('status')}, ticket "
            f"{(final.get('ticket') or {}).get('ticket_id')}")
        return 0 if final.get("status") == "completed" else 1
    finally:
        say(role("--revert"))


if __name__ == "__main__":
    for s in (sys.stdout, sys.stderr):
        getattr(s, "reconfigure", lambda **_: None)(encoding="utf-8")
    sys.exit(main())
