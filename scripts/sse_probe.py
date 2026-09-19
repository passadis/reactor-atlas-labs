"""Drive the ATLAS web app the way a browser does, and measure it.

    uv run python -m scripts.sse_probe                         # deployed app, canonical incident
    uv run python -m scripts.sse_probe --url http://127.0.0.1:8000 --no-auth
    uv run python -m scripts.sse_probe --follow-up "Is the service that caused this under a freeze?"
    uv run python -m scripts.sse_probe --cancel-after 3        # disconnect mid-stream
    uv run python -m scripts.sse_probe --mode prompt --message "Which runbook ...?"

Against the deployed app it authenticates as the signed-in Azure CLI user with a real Entra token
for `api://atlas-web/user_impersonation` (the CLI is pre-authorized for that scope), so it goes
through Container Apps built-in auth exactly as a person would. No bypass.

Prints one JSON report: time to first event, tool lifecycle events, the structured result, the
follow-up answer. Exit 1 if the stream failed.
"""

from __future__ import annotations

import argparse
import json
import os
import shutil
import subprocess
import sys
import time
from typing import Any

import httpx

CANONICAL = (
    "Checkout is timing out for about 20% of EU customers since 14:05, "
    "started right after the payments deploy."
)
SCOPE = "api://atlas-web/user_impersonation"


def cli_token() -> str:
    az = shutil.which("az") or "az"
    out = subprocess.run(
        [az, "account", "get-access-token", "--scope", SCOPE,
         "--query", "accessToken", "-o", "tsv"],
        capture_output=True, text=True, timeout=60, check=True,
    )
    return out.stdout.strip()


def run_turn(client: httpx.Client, message: str, mode: str, session_id: str | None,
             cancel_after: float | None) -> dict[str, Any]:
    started = time.perf_counter()
    r = client.post("/api/chat", json={"message": message, "mode": mode, "session_id": session_id})
    r.raise_for_status()
    turn = r.json()
    report: dict[str, Any] = {"turn_id": turn["turn_id"], "session_id": turn["session_id"],
                              "mode": mode, "events": {}, "tools": [], "text": ""}
    first: float | None = None
    event = "message"
    with client.stream("GET", f"/api/chat/{turn['turn_id']}/stream", timeout=200) as stream:
        for line in stream.iter_lines():
            now = time.perf_counter() - started
            if cancel_after is not None and now > cancel_after:
                # What the page does when the tab closes: drop the stream and send the cancel
                # beacon (ingress does not tell the app that the client went away).
                report["cancelled_at_s"] = round(now, 1)
                client.post(f"/api/chat/{turn['turn_id']}/cancel")
                break
            if line.startswith("event:"):
                event = line[6:].strip()
                continue
            if not line.startswith("data:"):
                continue
            data = json.loads(line[5:].strip() or "{}")
            report["events"][event] = report["events"].get(event, 0) + 1
            if event in ("token", "tool_start") and first is None:
                first = now
            if event == "token":
                report["text"] += data.get("text", "")
            elif event == "tool_end":
                report["tools"].append(f"{data['name']} {data['ms']}ms {data['outcome']}")
            elif event == "structured":
                report["structured"] = data
            elif event == "meta" and data.get("traceparent"):
                report["traceparent"] = data["traceparent"]
            elif event == "error":
                report.setdefault("errors", []).append(data)
            elif event == "done":
                report["done"] = data
                break
    report["first_event_s"] = round(first, 2) if first is not None else None
    report["elapsed_s"] = round(time.perf_counter() - started, 1)
    return report


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--url", default=os.environ.get("ATLAS_WEB_URL", ""))
    parser.add_argument("--no-auth", action="store_true", help="local run with the auth bypass")
    parser.add_argument("--mode", default="app", choices=["app", "model", "prompt", "hosted"])
    parser.add_argument("--message", default=CANONICAL)
    parser.add_argument("--follow-up", default=None)
    parser.add_argument("--cancel-after", type=float, default=None)
    args = parser.parse_args(argv)
    if not args.url:
        from atlas_common.config import load_local_env

        load_local_env()
        args.url = os.environ.get("ATLAS_WEB_URL", "")
    headers = {} if args.no_auth else {"Authorization": f"Bearer {cli_token()}"}

    with httpx.Client(base_url=args.url, headers=headers, timeout=60) as client:
        first = run_turn(client, args.message, args.mode, None, args.cancel_after)
        out: dict[str, Any] = {"url": args.url, "first": first}
        if args.follow_up:
            out["follow_up"] = run_turn(client, args.follow_up, args.mode, first["session_id"],
                                        None)
    print(json.dumps(out, indent=2, default=str))
    ok = "done" in first or args.cancel_after is not None
    return 0 if ok else 1


if __name__ == "__main__":
    for s in (sys.stdout, sys.stderr):
        getattr(s, "reconfigure", lambda **_: None)(encoding="utf-8")
    sys.exit(main())
