"""Capture Episode 2 screenshots of the DEPLOYED web app, and prove the panel works in a browser.

    uv run --extra ui python -m scripts.capture_phase06

Drives the locally installed Microsoft Edge headless against the public URL with a real Entra token,
exactly as `scripts/capture_screenshots.py` does for Episode 1. It is also the only check that runs
the Episode 2 panel's JavaScript for real: a broken selector or a bad event name fails here rather
than on stage.
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
RUN_TIMEOUT_MS = 180_000


def shot(page: Page, name: str) -> None:
    path = OUT / f"phase06-{name}.png"
    page.screenshot(path=str(path), full_page=False)
    print(f"  {path}")


def run_workflow(page: Page, mode: str) -> None:
    page.select_option("#orch-mode", mode)
    page.wait_for_function("() => document.querySelectorAll('.wf-node').length >= 5")
    page.fill("#message", CANONICAL)
    page.click("#run-workflow")
    page.wait_for_selector("#wf-decisions li", timeout=RUN_TIMEOUT_MS)
    page.wait_for_function("() => !document.querySelector('#run-workflow').disabled",
                           timeout=RUN_TIMEOUT_MS)


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
        page.on("pageerror", lambda exc: failures.append(f"page error: {exc}"))
        page.on("console", lambda msg: failures.append(f"console {msg.type}: {msg.text}")
                if msg.type == "error" else None)
        page.on("requestfailed", lambda req: failures.append(f"request failed: {req.url}"))
        page.on("response", lambda res: failures.append(f"HTTP {res.status} {res.url}")
                if res.status >= 400 else None)

        page.goto(f"{url}/?scope=ep2")
        page.wait_for_selector("#persona-name:not(:has-text('...'))")
        page.wait_for_function("() => document.querySelectorAll('.wf-node').length >= 5")
        shot(page, "01-graph-idle")

        run_workflow(page, "graph")
        shot(page, "02-graph-complete")
        page.locator(".activity").screenshot(path=str(OUT / "phase06-03-workflow-panel.png"))
        print(f"  {OUT / 'phase06-03-workflow-panel.png'}")

        # The Episode 1 view must still hide every Episode 2 panel: scope is a view filter.
        page.select_option("#scope", "ep1")
        hidden = page.evaluate(
            "() => document.querySelector('[data-scope=\"ep2\"]').hidden")
        if not hidden:
            failures.append("scope=ep1 did not hide the Episode 2 panel")
        page.select_option("#scope", "ep2")

        run_workflow(page, "sequential_only")
        shot(page, "04-sequential")

        nodes = page.evaluate(
            "() => [...document.querySelectorAll('.wf-node')]"
            ".map((n) => [n.dataset.node, n.className, n.querySelector('.wf-time').textContent])")
        done = [n for n in nodes if "done" in n[1]]
        if len(done) < 5:
            failures.append(f"only {len(done)} nodes reached 'done': {nodes}")
        browser.close()

    for failure in failures:
        print(f"FAIL  {failure}")
    print("Episode 2 panel: " + ("FAILED" if failures else "clean"))
    return 1 if failures else 0


if __name__ == "__main__":
    for stream in (sys.stdout, sys.stderr):
        getattr(stream, "reconfigure", lambda **_: None)(encoding="utf-8")
    sys.exit(main())
