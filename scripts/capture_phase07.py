"""Capture the Episode 2 approval screenshots, and prove the gate panel works in a browser.

    uv run --group ui python -m scripts.capture_phase07

Drives the locally installed Microsoft Edge headless against the deployed app with a real Entra
token. It is also the only check that runs the approval panel's JavaScript for real: a bad
selector or a wrong event name fails here rather than on stage.
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
GATE_TIMEOUT_MS = 180_000


def shot(page: Page, name: str) -> None:
    path = OUT / f"phase07-{name}.png"
    page.screenshot(path=str(path), full_page=False)
    print(f"  {path}")


def main() -> int:
    load_local_env()
    url = os.environ["ATLAS_WEB_URL"].rstrip("/")
    OUT.mkdir(parents=True, exist_ok=True)
    token = cli_token()
    failures: list[str] = []
    with sync_playwright() as pw:
        browser = pw.chromium.launch(channel="msedge", headless=True)
        context = browser.new_context(
            viewport=VIEWPORT,  # type: ignore[arg-type]
            extra_http_headers={"Authorization": f"Bearer {token}"},
            color_scheme="dark",
        )
        page = context.new_page()
        # A 403 on /decision is the demo, not a fault: the signed-in persona may not be allowed
        # to approve this particular plan. The browser logs it as a console error regardless, so
        # only unexpected failures count.
        def note_console(msg: object) -> None:
            text = str(getattr(msg, "text", ""))
            if getattr(msg, "type", "") != "error":
                return
            if "403" in text and "decision" in text:
                return
            if "403" in text and "Failed to load resource" in text:
                return
            failures.append(f"console error: {text}")

        page.on("pageerror", lambda exc: failures.append(f"page error: {exc}"))
        page.on("console", note_console)
        page.on("response", lambda res: failures.append(f"HTTP {res.status} {res.url}")
                if res.status >= 500 else None)

        page.goto(f"{url}/?scope=ep2")
        page.wait_for_selector("#persona-name:not(:has-text('...'))")
        page.wait_for_function("() => document.querySelectorAll('.wf-node').length >= 5")

        page.fill("#message", CANONICAL)
        page.click("#run-workflow")
        page.wait_for_selector("#wf-approval:not([hidden])", timeout=GATE_TIMEOUT_MS)
        shot(page, "01-approval-gate")
        page.locator(".activity").screenshot(path=str(OUT / "phase07-02-approval-panel.png"))
        print(f"  {OUT / 'phase07-02-approval-panel.png'}")

        reasons = page.evaluate(
            "() => [...document.querySelectorAll('#wf-approval-reasons li')]"
            ".map((n) => n.textContent)")
        if not reasons:
            failures.append("the approval panel showed no policy reasons")
        print(f"  policy reasons shown: {len(reasons)}")
        for reason in reasons:
            print(f"    - {reason}")

        # A comment is mandatory: approving without one must be refused by the page itself.
        page.click("#wf-approve")
        note = page.text_content("#wf-approval-note") or ""
        if "comment is required" not in note:
            failures.append(f"an empty comment was not refused: {note!r}")
        if page.is_hidden("#wf-approval"):
            failures.append("the panel closed on an invalid approval")

        # Now decide for real. The signed-in persona may well be refused - that is the demo,
        # so either a refusal note or a resume is a pass.
        page.fill("#wf-comment", "Verification: exercising the approval path from the console.")
        # Clear the "comment is required" note first, or the wait below returns on the old text.
        page.evaluate("() => { document.querySelector('#wf-approval-note').textContent = ''; }")
        page.click("#wf-approve")
        page.wait_for_function(
            "() => document.querySelector('#wf-approval-note').textContent.trim().length > 0",
            timeout=GATE_TIMEOUT_MS)
        outcome = page.text_content("#wf-approval-note") or ""
        print(f"  outcome: {outcome.strip()[:120]}")
        shot(page, "03-decision")
        browser.close()

    for failure in failures:
        print(f"FAIL  {failure}")
    print("Episode 2 approval panel: " + ("FAILED" if failures else "clean"))
    return 1 if failures else 0


if __name__ == "__main__":
    for stream in (sys.stdout, sys.stderr):
        getattr(stream, "reconfigure", lambda **_: None)(encoding="utf-8")
    sys.exit(main())
