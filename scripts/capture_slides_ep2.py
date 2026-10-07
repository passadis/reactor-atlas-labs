"""Capture the Episode 2 deck's screenshots from the DEPLOYED web app (Scope: Episode 2).

    uv run --group ui python -m scripts.capture_slides_ep2     # writes docs/assets/ep2-slide-*.png

Same approach as scripts/capture_slides_ep1.py: headless Edge, a real Entra token, the public URL.
Runs the canonical case to the policy gate and leaves it parked (application data only;
`make demo-reset` clears it). Prints the case id, so `make checkpoints CASE=...` can follow.

  ep2-slide08-workflow-graph.png    the console with the graph run to the gate
  ep2-slide13-approval-panel.png    the approval panel: who may approve, and why
"""

from __future__ import annotations

import os
import sys
from pathlib import Path

from atlas_common.config import load_local_env
from playwright.sync_api import sync_playwright

from scripts.sse_probe import CANONICAL, cli_token

OUT = Path("docs/assets")


def main() -> int:
    load_local_env()
    url = os.environ["ATLAS_WEB_URL"].rstrip("/")
    OUT.mkdir(parents=True, exist_ok=True)
    with sync_playwright() as pw:
        browser = pw.chromium.launch(channel="msedge", headless=True)
        context = browser.new_context(
            viewport={"width": 1600, "height": 1000},
            extra_http_headers={"Authorization": f"Bearer {cli_token()}"},
        )
        page = context.new_page()
        page.goto(f"{url}/?scope=ep2")
        page.wait_for_selector("#persona-name:not(:has-text('...'))")
        page.fill("#message", CANONICAL)
        page.click("#run-workflow")
        page.wait_for_selector("#wf-approval:not([hidden])", timeout=180_000)
        page.wait_for_timeout(1500)
        print("case", page.inner_text("#wf-case"), flush=True)
        # The graph itself: the node list with each node's kind, state and timing, plus the case
        # metadata. (A page screenshot scrolls to the approval panel and misses it.)
        page.locator("#wf-nodes").scroll_into_view_if_needed()
        page.locator("#wf-nodes").screenshot(path=str(OUT / "ep2-slide08-workflow-graph.png"))
        page.locator("#wf-approval").screenshot(path=str(OUT / "ep2-slide13-approval-panel.png"))
        print(f"  {OUT}/ep2-slide08-workflow-graph.png\n  {OUT}/ep2-slide13-approval-panel.png")
        browser.close()
    return 0


if __name__ == "__main__":
    for s in (sys.stdout, sys.stderr):
        getattr(s, "reconfigure", lambda **_: None)(encoding="utf-8")
    sys.exit(main())
