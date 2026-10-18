"""Capture fallback-deck screenshots of the DEPLOYED web app (Phase 04 acceptance).

    uv run python -m scripts.capture_screenshots            # writes docs/assets/phase04-*.png

Drives the locally installed Microsoft Edge headless (Playwright `channel="msedge"`) against the
public URL. Every request, including the SSE stream, carries a real Entra bearer token for the
signed-in Azure CLI user, so the pages go through built-in auth exactly as a browser session does.
No bypass: the screenshots show the real deployed app.
"""

from __future__ import annotations

import os
import sys
from pathlib import Path

from atlas_common.config import load_local_env
from playwright.sync_api import Page, sync_playwright

from scripts.sse_probe import CANONICAL, cli_token

OUT = Path("docs/assets")
VIEWPORT = {"width": 1600, "height": 950}


def shot(page: Page, name: str) -> None:
    path = OUT / f"phase04-{name}.png"
    page.screenshot(path=str(path), full_page=False)
    print(f"  {path}")


def ask(page: Page, text: str, wait_for: str, timeout_ms: int = 120_000) -> None:
    page.fill("#message", text)
    page.click("#send")
    page.wait_for_selector(wait_for, timeout=timeout_ms)
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
            color_scheme="dark",
        )
        page = context.new_page()

        page.goto(f"{url}/?scope=ep1")
        page.wait_for_selector("#persona-name:not(:has-text('...'))")
        shot(page, "01-console-empty")

        ask(page, CANONICAL, ".triage")
        shot(page, "02-triage-activity")
        page.locator(".activity").screenshot(path=str(OUT / "phase04-03-activity-panel.png"))
        print(f"  {OUT / 'phase04-03-activity-panel.png'}")

        page.check("#contract")
        page.locator(".msg.bot").last.scroll_into_view_if_needed()
        shot(page, "04-contract")
        page.uncheck("#contract")

        ask(page, "Is the service that caused this under a change freeze, and which team owns it?",
            ".msg.bot:nth-of-type(4)")
        shot(page, "05-follow-up")

        page.click("#new-session")
        page.click("button[data-mode='model']")
        ask(page, CANONICAL, ".msg.bot")
        shot(page, "06-model-call")

        page.click("#new-session")
        page.click("button[data-mode='prompt']")
        ask(page, "Which runbook covers elevated p95 latency on the Checkout API after a "
                  "deployment?", ".msg.bot")
        shot(page, "07-prompt-agent")

        page.select_option("#scope", "full")
        shot(page, "08-scope-full")

        page.goto(f"{url}/kb/RB-0042")
        page.wait_for_selector("h1")
        shot(page, "09-knowledge-RB-0042")

        page.goto(f"{url}/approvals")
        page.wait_for_selector("#list .card")
        shot(page, "10-approvals")
        browser.close()
    return 0


if __name__ == "__main__":
    for s in (sys.stdout, sys.stderr):
        getattr(s, "reconfigure", lambda **_: None)(encoding="utf-8")
    sys.exit(main())
