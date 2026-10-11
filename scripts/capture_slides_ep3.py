"""Capture the Episode 3 deck's screenshots from the DEPLOYED web app.

    uv run --group ui python -m scripts.capture_slides_ep3 tool         # slide 9: a tool call
    uv run --group ui python -m scripts.capture_slides_ep3 remediation  # slide 14: the worker

Headless Edge, a real Entra token, the public URL (as scripts/capture_slides_ep1.py). Writes
application data only. `remediation` runs the canonical case and approves it as the presenter.
"""

from __future__ import annotations

import os
import sys
from pathlib import Path

from atlas_common.config import load_local_env
from playwright.sync_api import sync_playwright

from scripts.sse_probe import CANONICAL, cli_token

OUT = Path("docs/assets")
TOOL_Q = "Use the lookup_service tool: who owns svc-orders and is it under a change freeze?"


def main() -> int:
    what = sys.argv[1] if len(sys.argv) > 1 else "tool"
    load_local_env()
    url = os.environ["ATLAS_WEB_URL"].rstrip("/")
    with sync_playwright() as pw:
        browser = pw.chromium.launch(channel="msedge", headless=True)
        context = browser.new_context(viewport={"width": 1600, "height": 900},
                                      extra_http_headers={"Authorization": f"Bearer {cli_token()}"})
        page = context.new_page()
        if what == "tool":
            page.goto(f"{url}/?scope=ep3")
            page.wait_for_selector("#persona-name:not(:has-text('...'))")
            page.click("button[data-mode='prompt']")
            page.fill("#message", TOOL_Q)
            page.click("#send")
            page.wait_for_timeout(1500)
            page.wait_for_function("() => !document.querySelector('#send').disabled",
                                   timeout=180_000)
            page.screenshot(path=str(OUT / "ep3-slide09-tool-call.png"))
        else:
            page.goto(f"{url}/?scope=ep3")
            page.wait_for_selector("#persona-name:not(:has-text('...'))")
            page.fill("#message", CANONICAL)
            page.click("#run-workflow")
            page.wait_for_selector("#wf-approval:not([hidden])", timeout=240_000)
            page.fill("#wf-comment", "Approved for the Episode 3 screenshot")
            page.click("#wf-approve")
            page.wait_for_timeout(45_000)  # dispatch, Service Bus, the worker's steps
            page.screenshot(path=str(OUT / "ep3-slide14-remediation.png"), full_page=True)
        print("case", page.inner_text("#wf-case") if what != "tool" else "-")
        browser.close()
    return 0


if __name__ == "__main__":
    for s in (sys.stdout, sys.stderr):
        getattr(s, "reconfigure", lambda **_: None)(encoding="utf-8")
    sys.exit(main())
