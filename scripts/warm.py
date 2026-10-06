"""Warm every surface the episode touches, so the first thing on stage is not a cold start.

    bash scripts/warm.sh          # or: make warm     (T-10 routine, after make demo-reset)

Health probes, one throwaway call per model deployment, one search query, one hosted-agent
turn, one gateway round trip, fresh CLI tokens, and one canonical triage through the web app in
"This app" and in "Hosted agent" mode - the web keeps that hosted sandbox warm for the next
conversation, and a new sandbox takes 28-38 s (DEMO-NOTES Phase 05). Those two turns leave two
chat sessions behind; nothing else is written. It never scales,
restarts or deploys anything (CLAUDE.md section 9) - replica floors are the scale profile's job
(`make scale PROFILE=show`, the day before).

Every probe runs in parallel with its own timeout; the table shows each one's latency, so a slow
surface is visible before you go on air. Exit 0 only when every probe answered.
"""

from __future__ import annotations

import json
import os
import shutil
import subprocess
import sys
import time
from collections.abc import Callable
from concurrent.futures import ThreadPoolExecutor

import httpx
from atlas_common.auth import SCOPE_FOUNDRY, get_credential
from atlas_common.config import KNOWLEDGE_INDEX, Settings, load_local_env
from azure.identity import get_bearer_token_provider
from openai import OpenAI

WEB_SCOPE = "api://atlas-web/user_impersonation"
TIMEOUT_S = 90


def az_token(scope: str) -> str:
    out = subprocess.run([shutil.which("az") or "az", "account", "get-access-token", "--scope",
                          scope, "--query", "accessToken", "-o", "tsv"],
                         capture_output=True, text=True, timeout=60, check=True)
    return out.stdout.strip()


def http_ok(url: str, headers: dict[str, str] | None = None) -> str:
    r = httpx.get(url, headers=headers or {}, timeout=TIMEOUT_S)
    r.raise_for_status()
    return f"HTTP {r.status_code}"


def main() -> int:
    load_local_env()
    s = Settings.from_env()
    env = os.environ
    openai = OpenAI(base_url=s.foundry_openai_base_url, timeout=TIMEOUT_S,
                    api_key=get_bearer_token_provider(get_credential(), SCOPE_FOUNDRY))
    # The SDK imports its resources lazily; two threads importing the same one at once deadlock
    # on the module lock (measured). Touch them here, single-threaded.
    _ = (openai.responses, openai.embeddings)

    def tokens() -> str:
        for scope in ("https://management.azure.com/.default", WEB_SCOPE, SCOPE_FOUNDRY):
            az_token(scope)
        return "ARM, web and Foundry tokens refreshed"

    def web_health() -> str:
        r = httpx.get(f"{env['ATLAS_WEB_URL'].rstrip('/')}/api/health", timeout=TIMEOUT_S)
        body = r.json()
        bad = [k for k, v in (body.get("checks") or {}).items() if v.get("status") != "ok"]
        if r.status_code != 200 or bad:
            raise RuntimeError(f"HTTP {r.status_code}, not ok: {bad}")
        return f"{len(body.get('checks') or {})} dependencies ok"

    def model(name: str) -> Callable[[], str]:
        def call() -> str:
            openai.responses.create(model=name, input="Reply with OK.", max_output_tokens=16,
                                    store=False)
            return "one call"
        return call

    def embed() -> str:
        openai.embeddings.create(model=s.model_embed, input="checkout latency")
        return "one embedding"

    def search() -> str:
        from azure.search.documents import SearchClient

        client = SearchClient(s.search_endpoint, KNOWLEDGE_INDEX, get_credential())
        hits = list(client.search("checkout latency after a payments deploy", top=3))
        return f"{len(hits)} hits"

    def hosted_agent() -> str:
        from azure.ai.projects import AIProjectClient

        project = AIProjectClient(endpoint=env["FOUNDRY_PROJECT_ENDPOINT"],
                                  credential=get_credential(), allow_preview=True)
        agent = project.get_openai_client(agent_name="atlas-incident-agent", timeout=TIMEOUT_S)
        agent.responses.create(input="Health check: reply with a one-line status, no tools.")
        return "one turn"

    def gateway() -> str:
        r = httpx.post(f"{env['ATLAS_WEB_URL'].rstrip('/')}/api/gateway/probe",
                       json={"kind": "backend_down"}, timeout=TIMEOUT_S,
                       headers={"Authorization": f"Bearer {az_token(WEB_SCOPE)}"})
        r.raise_for_status()
        body = r.json()
        if body.get("status") != 200:
            raise RuntimeError(f"ops API via APIM answered {body.get('status')}")
        return f"web -> APIM -> ops API {body.get('ms')} ms"

    def web_triage(mode: str) -> Callable[[], str]:
        def call() -> str:
            r = subprocess.run([sys.executable, "-m", "scripts.sse_probe", "--mode", mode],
                               capture_output=True, text=True, timeout=180, check=False,
                               env={**env, "PYTHONIOENCODING": "utf-8"})
            if r.returncode != 0:
                tail = (r.stderr.strip().splitlines() or ["stream ended without a done event"])
                raise RuntimeError(tail[-1][:160])
            first = json.loads(r.stdout)["first"]
            return f"canonical triage streamed, {len(first.get('tools') or [])} tool events"
        return call

    probes: dict[str, Callable[[], str]] = {
        "tokens": tokens,
        "web + its dependencies": web_health,
        "ops API function": lambda: http_ok(f"{env['ATLAS_FUNC_API_URL'].rstrip('/')}/api/health"),
        "ops MCP function": lambda: http_ok(f"{env['ATLAS_FUNC_MCP_URL'].rstrip('/')}/api/health"),
        f"model {s.model_main}": model(s.model_main),
        f"model {env.get('ATLAS_MODEL_JUDGE', 'gpt-judge')}": model(
            env.get("ATLAS_MODEL_JUDGE", "gpt-judge")),
        f"model {s.model_embed}": embed,
        "AI Search knowledge index": search,
        "hosted agent": hosted_agent,
        "gateway round trip": gateway,
        "web triage, This app": web_triage("app"),
        # The warm hosted sandbox lives in each web replica's memory (shapes.py), and the show
        # profile runs two. Cookie-less requests spread across replicas: four in parallel leave
        # both holding a warm sandbox (measured 2026-10-06: one alone left a 45 s cold start).
        **{f"web triage, Hosted agent #{i}": web_triage("hosted") for i in range(1, 5)},
    }

    def run(item: tuple[str, Callable[[], str]]) -> tuple[str, bool, float, str]:
        name, fn = item
        started = time.perf_counter()
        try:
            detail, ok = fn(), True
        except Exception as exc:  # every probe reports; none aborts the others
            detail, ok = f"{type(exc).__name__}: {str(exc)[:160]}", False
        return name, ok, time.perf_counter() - started, detail

    started = time.perf_counter()
    # Capped: every probe at once is about 250K tokens within seconds, enough to throttle
    # gpt-main (measured 2026-10-06). Six at a time stays well under the limit.
    with ThreadPoolExecutor(max_workers=6) as pool:
        results = list(pool.map(run, probes.items()))
    for name, ok, seconds, detail in results:
        print(f"{'WARM' if ok else 'FAIL'}  {name:<28} {seconds:5.1f} s  {detail}")
    failed = [r for r in results if not r[1]]
    print(f"warm: {len(results) - len(failed)}/{len(results)} in "
          f"{time.perf_counter() - started:.0f} s")
    report = {name: {"ok": ok, "seconds": round(sec, 2), "detail": d}
              for name, ok, sec, d in results}
    os.makedirs(".azure", exist_ok=True)
    with open(".azure/warm-last.json", "w", encoding="utf-8") as f:
        json.dump(report, f, indent=2)
    return 1 if failed else 0


if __name__ == "__main__":
    sys.exit(main())
