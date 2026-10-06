"""Capture the Episode 1 deck's screenshots from the DEPLOYED web app (light theme, 16:9).

    uv run --group ui python -m scripts.capture_slides_ep1     # writes docs/assets/ep1-slide-*.png

Same approach as scripts/capture_screenshots.py: headless Edge, a real Entra token for the signed-in
Azure CLI user, the public URL, no bypass. Writes application data only (two chat sessions).

  ep1-slide12-this-app-triage.png   This app shape: the activity panel and the Sev2 card
  ep1-slide13-contract.png          the same turn with "Show the contract" ticked
  ep1-slide14-hosted-agent.png      Hosted agent shape: same report, same card
  ep1-slide14-this-app.png          This app shape, framed like the hosted one
"""

from __future__ import annotations

import os
import sys
from pathlib import Path

from atlas_common.config import load_local_env
from playwright.sync_api import Page, sync_playwright

from scripts.sse_probe import CANONICAL, cli_token

OUT = Path("docs/assets")
VIEWPORT = {"width": 1600, "height": 900}


def shot(page: Page, name: str) -> None:
    path = OUT / f"ep1-{name}.png"
    page.screenshot(path=str(path), full_page=False)
    print(f"  {path}", flush=True)


def ask(page: Page, text: str, timeout_ms: int = 150_000) -> None:
    page.fill("#message", text)
    page.click("#send")
    page.wait_for_selector(".triage", timeout=timeout_ms)
    page.wait_for_function("() => !document.querySelector('#send').disabled", timeout=timeout_ms)


def main() -> int:
    load_local_env()
    url = os.environ["ATLAS_WEB_URL"].rstrip("/")
    OUT.mkdir(parents=True, exist_ok=True)
    token = cli_token()
    with sync_playwright() as pw:
        browser = pw.chromium.launch(channel="msedge", headless=True)
        context = browser.new_context(
            viewport=VIEWPORT,  # type: ignore[arg-type]
            extra_http_headers={"Authorization": f"Bearer {token}"},
            color_scheme="light",
        )
        page = context.new_page()
        page.goto(f"{url}/?scope=ep1")
        page.wait_for_selector("#persona-name:not(:has-text('...'))")

        page.click("button[data-mode='app']")
        ask(page, CANONICAL)
        shot(page, "slide12-this-app-triage")
        shot(page, "slide14-this-app")
        page.check("#contract")
        page.locator(".msg.bot").last.scroll_into_view_if_needed()
        shot(page, "slide13-contract")
        page.uncheck("#contract")

        page.click("#new-session")
        page.click("button[data-mode='hosted']")
        ask(page, CANONICAL)
        shot(page, "slide14-hosted-agent")
        browser.close()
    return 0


if __name__ == "__main__":
    for s in (sys.stdout, sys.stderr):
        getattr(s, "reconfigure", lambda **_: None)(encoding="utf-8")
    sys.exit(main())
