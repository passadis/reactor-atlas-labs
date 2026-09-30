"""Prove the gateway's governance, in order, against the deployed system. The Episode 3 demo.

    uv run python -m scripts.prove_governance          # all eight, about 3-5 minutes
    uv run python -m scripts.prove_governance --fast   # skip waiting for Function logs (proof 6b)

Written to be read off a projector: one numbered claim per block, the evidence under it, PASS or
FAIL on the right. No secrets are ever printed - keys are fetched from Key Vault by URI and used,
never shown.

Two callers, deliberately:
- **you, the operator** - a real Entra identity with a real subscription key, and NO Atlas.Agent
  role. Proofs 1, 2, 3, 5 and 7 are run as you, because what matters there is what the gateway
  refuses.
- **an agent** - the web app's managed identity, which holds Atlas.Agent. Proofs 4, 6 and 8 need a
  caller the gateway accepts, so they go through the web app's /api/gateway/probe, which makes a
  fixed request with its own identity and reports what came back.

Writes: application data only (the chaos flag document, always switched off again). Exit code is
the number of failed proofs.
"""

from __future__ import annotations

import argparse
import asyncio
import os
import shutil
import subprocess
import sys
import time
from collections import Counter
from typing import Any

import httpx
from atlas_common.config import load_local_env
from atlas_common.secrets import read_secret
from azure.identity.aio import AzureCliCredential as AsyncAzureCliCredential

from scripts.sse_probe import cli_token

WIDTH = 78
results: list[tuple[str, bool]] = []


def banner(n: int, claim: str) -> None:
    print(f"\n[{n}] {claim}")


def line(text: str) -> None:
    print(f"      {text}")


def verdict(n: int, claim: str, ok: bool, why: str = "") -> None:
    results.append((f"{n} {claim}", ok))
    tail = "PASS" if ok else "FAIL"
    msg = f"      -> {why}" if why else "      ->"
    print(f"{msg[:WIDTH - 6]:<{WIDTH - 6}}{tail}")


def az(*args: str) -> str:
    return subprocess.run([shutil.which("az") or "az", *args], capture_output=True, text=True,
                          timeout=180, check=True).stdout.strip()


def operator_token(scope: str) -> str:
    return az("account", "get-access-token", "--scope", scope, "--query", "accessToken",
              "-o", "tsv")


def env(name: str) -> str:
    value = os.environ.get(name, "")
    if not value:
        raise SystemExit(f"{name} is not set - run `azd env refresh` or check .env.local")
    return value


class Setup:
    def __init__(self) -> None:
        self.gateway = env("ATLAS_APIM_GATEWAY_URL").rstrip("/")
        self.func_api = env("ATLAS_FUNC_API_URL").rstrip("/")
        self.web = env("ATLAS_WEB_URL").rstrip("/")
        # The operator's tokens. The gateway token carries no roles: the operator is not an agent.
        # Delegated scopes go through `--scope`; AzureCliCredential would send them as --resource.
        self.gateway_token = operator_token("api://atlas-gateway/gateway.call")
        self.ops_token = operator_token("api://atlas-ops-api/ops.call")
        self.readonly_key = asyncio.run(self._key(env("ATLAS_APIM_READONLY_KEY_SECRET_URI")))
        self.web_client = httpx.Client(base_url=self.web, timeout=120,
                                       headers={"Authorization": f"Bearer {cli_token()}"})

    @staticmethod
    async def _key(uri: str) -> str:
        async with AsyncAzureCliCredential() as cred:
            return await read_secret(uri, cred)

    def as_operator(self, *, key: bool = True, token: bool = True) -> dict[str, str]:
        headers: dict[str, str] = {}
        if key:
            headers["Ocp-Apim-Subscription-Key"] = self.readonly_key
        if token:
            headers["Authorization"] = f"Bearer {self.gateway_token}"
        return headers

    def probe(self, kind: str) -> dict[str, Any]:
        response = self.web_client.post("/api/gateway/probe", json={"kind": kind})
        response.raise_for_status()
        return dict(response.json())


# ------------------------------------------------------------------------ 1 --
def proof_direct_backend(s: Setup) -> None:
    claim = "The Function App cannot be called except through the gateway"
    banner(1, claim)
    url = f"{s.func_api}/api/services/svc-checkout-api"
    anon = httpx.get(url, timeout=60)
    line(f"no token                         HTTP {anon.status_code}")
    mine = httpx.get(url, headers={"Authorization": f"Bearer {s.ops_token}"}, timeout=60)
    line(f"valid Entra token, not the gateway HTTP {mine.status_code}")
    verdict(1, claim, anon.status_code == 401 and mine.status_code in (401, 403),
            "only APIM's managed identity is an allowed caller")


# ------------------------------------------------------------------------ 2 --
def proof_no_key(s: Setup) -> None:
    claim = "Through APIM without a subscription key is refused"
    banner(2, claim)
    r = httpx.get(f"{s.gateway}/ops/services/svc-checkout-api",
                  headers=s.as_operator(key=False), timeout=60)
    line(f"GET /ops/services/svc-checkout-api, token but no key   HTTP {r.status_code}")
    verdict(2, claim, r.status_code == 401, "no subscription, no entry")


# ------------------------------------------------------------------------ 3 --
def proof_not_an_agent(s: Setup) -> None:
    claim = "A key and a valid token are not enough: the caller must be an agent"
    banner(3, claim)
    r = httpx.get(f"{s.gateway}/ops/services/svc-checkout-api", headers=s.as_operator(),
                  timeout=60)
    body = r.json() if r.headers.get("content-type", "").startswith("application/json") else {}
    line(f"operator token (no Atlas.Agent role) + key         HTTP {r.status_code}")
    line(f"{body.get('code', '')}: {str(body.get('message', ''))[:64]}")
    verdict(3, claim, r.status_code == 403 and body.get("code") == "forbidden",
            "403, not 401: the gateway knows who you are, and says no")


# ------------------------------------------------------------------------ 4 --
def proof_valid(s: Setup) -> dict[str, Any]:
    claim = "An agent's call succeeds, with the correlation id echoed"
    banner(4, claim)
    p = s.probe("valid")
    cid = p["headers"].get("x-atlas-correlation-id", "")
    name = p["body"].get("name") if isinstance(p["body"], dict) else ""
    line(f"web app identity (Atlas.Agent) + agent key         HTTP {p['status']}  ({name})")
    line(f"x-atlas-correlation-id: {cid}")
    verdict(4, claim, p["status"] == 200 and len(cid) >= 16, f"{p['ms']} ms end to end")
    return p


# ------------------------------------------------------------------------ 5 --
def proof_rate_limit(s: Setup) -> None:
    claim = "Over 120 calls a minute: throttled with 429 and Retry-After"
    banner(5, claim)
    url = f"{s.gateway}/ops/services/svc-checkout-api"

    async def wave(http: httpx.AsyncClient, n: int) -> list[httpx.Response]:
        return list(await asyncio.gather(*(http.get(url, headers=s.as_operator())
                                           for _ in range(n))))

    async def burst() -> tuple[list[httpx.Response], int]:
        # Waves of 20, not one burst of 121: StandardV2 rate-limit counters are distributed and
        # synchronise with a small lag, so a 1-second burst can outrun them. Waves let the count
        # catch up - and the number of calls before the first 429 is printed, so nothing is hidden.
        sent: list[httpx.Response] = []
        async with httpx.AsyncClient(timeout=60) as http:
            while len(sent) < 240:
                got = await wave(http, 20)
                sent += got
                if any(r.status_code == 429 for r in got):
                    break
                await asyncio.sleep(0.5)
        first = next((i + 1 for i, r in enumerate(sent) if r.status_code == 429), 0)
        return sent, first

    t0 = time.perf_counter()
    responses, first = asyncio.run(burst())
    counts = Counter(r.status_code for r in responses)
    throttled = [r for r in responses if r.status_code == 429]
    retry_after = throttled[0].headers.get("Retry-After") if throttled else None
    line(f"{len(responses)} requests in {time.perf_counter() - t0:.1f} s   "
         + "  ".join(f"{k}: {v}" for k, v in sorted(counts.items())))
    line("(403s are expected: the limiter runs BEFORE authentication, so even a")
    line(" flood of rejected callers is counted and cut off)")
    line(f"first 429 at call {first} of this burst, Retry-After: {retry_after} s")
    verdict(5, claim, bool(throttled) and bool(retry_after),
            "cheapest check first; it protects everything behind it")


# ------------------------------------------------------------------------ 6 --
def proof_malformed(s: Setup) -> dict[str, Any]:
    claim = "A malformed body is refused by the gateway; the backend never runs"
    banner(6, claim)
    p = s.probe("malformed")
    body = p["body"] if isinstance(p["body"], dict) else {}
    line('POST /ops/tickets {"case_id": 42, "severity": "Sev9", "surprise": true}')
    line(f"HTTP {p['status']}  {body.get('code', '')}: {str(body.get('message', ''))[:58]}")
    verdict(6, claim, p["status"] == 400, "contract enforced at the gateway, from the spec")
    return p


def proof_backend_untouched(valid: dict[str, Any], malformed: dict[str, Any], wait: bool) -> None:
    claim = "Function logs: the valid call arrived, the malformed one never did"
    banner(6, claim)
    ok_cid = valid["headers"].get("x-atlas-correlation-id", "")
    bad_cid = malformed["headers"].get("x-atlas-correlation-id", "")
    if not wait:
        line("skipped (--fast); App Insights ingestion takes 1-3 minutes")
        return
    app, rg = env("ATLAS_APPINSIGHTS_NAME"), env("AZURE_RESOURCE_GROUP")
    func_name = env("ATLAS_FUNC_API_NAME")

    def seen(cid: str) -> int:
        # The Functions host logs "Executing 'Functions.<name>'" under the caller's trace id -
        # the same id the gateway echoes as x-atlas-correlation-id.
        query = (f"traces | where timestamp > ago(30m) and cloud_RoleName == '{func_name}' "
                 f"| where operation_Id == '{cid}' and message startswith 'Executing' | count")
        try:
            return int(az("monitor", "app-insights", "query", "--app", app, "-g", rg,
                          "--analytics-query", query, "--query", "tables[0].rows[0][0]",
                          "-o", "tsv") or 0)
        except subprocess.CalledProcessError:
            return 0

    # The valid call is the positive control: until IT shows up, absence proves nothing.
    deadline, arrived = time.monotonic() + 300, 0
    while time.monotonic() < deadline:
        arrived = seen(ok_cid)
        if arrived:
            break
        time.sleep(20)
    never = seen(bad_cid)
    line(f"valid call     {ok_cid}  Function executions: {arrived}")
    line(f"malformed call {bad_cid}  Function executions: {never}")
    verdict(6, claim, arrived > 0 and never == 0,
            "absence is only evidence once the control arrived")


# ------------------------------------------------------------------------ 7 --
def proof_token_limit(s: Setup) -> None:
    claim = "The model token budget is enforced: 429 and zero tokens remaining"
    banner(7, claim)
    url = f"{s.gateway}/models/responses"
    body = {"model": "gpt-main", "max_output_tokens": 400,
            "input": "List twenty facts about incident response, one sentence each."}
    last: httpx.Response | None = None
    for i in range(1, 9):
        last = httpx.post(url, json=body, headers=s.as_operator(token=False), timeout=120)
        remaining = last.headers.get("x-atlas-remaining-tokens", "?")
        line(f"call {i}: HTTP {last.status_code}   x-atlas-remaining-tokens: {remaining}")
        if last.status_code == 429:
            break
    ok = last is not None and last.status_code == 429 and \
        last.headers.get("x-atlas-remaining-tokens") == "0"
    verdict(7, claim, ok, "read-only product: 600 tokens a minute, per subscription")


# ------------------------------------------------------------------------ 8 --
def proof_retry(s: Setup) -> None:
    claim = "A failing backend is retried 3 times, then a typed envelope"
    banner(8, claim)
    flag = [sys.executable, "-m", "scripts.chaos_flag"]
    subprocess.run([*flag, "on", "--seconds", "60"], check=True, capture_output=True, text=True)
    line("chaos flag ON  (the ops API answers 503 - a data write, not a stopped app)")
    try:
        time.sleep(5)  # workers cache the flag for up to 3 s
        p = s.probe("backend_down")
    finally:
        subprocess.run([*flag, "off"], check=True, capture_output=True, text=True)
        line("chaos flag OFF")
    body = p["body"] if isinstance(p["body"], dict) else {}
    attempts = p["headers"].get("x-atlas-gateway-attempts", "?")
    line(f"HTTP {p['status']}   attempts: {attempts}   took {p['ms']} ms (backoff 1-8 s)")
    line(f"{{code: {body.get('code')}, message: {str(body.get('message'))[:40]}, "
         f"correlation_id: {str(body.get('correlation_id', ''))[:12]}...}}")
    verdict(8, claim, p["status"] == 503 and attempts == "4" and body.get("code") == "unavailable"
            and bool(body.get("correlation_id")), "1 try + 3 retries; no stack trace, ever")


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--fast", action="store_true")
    args = parser.parse_args(argv)
    load_local_env()
    print("=" * WIDTH)
    print("ATLAS gateway governance".center(WIDTH))
    print("=" * WIDTH)
    s = Setup()
    print(f"gateway   {s.gateway}")
    print("caller    you (no Atlas.Agent role)  and  the web app's identity (Atlas.Agent)")

    def attempt(n: int, fn: Any, *a: Any) -> Any:
        try:
            return fn(*a)
        except Exception as exc:
            verdict(n, fn.__name__, False, f"{type(exc).__name__}: {str(exc)[:50]}")
            return None

    attempt(1, proof_direct_backend, s)
    attempt(2, proof_no_key, s)
    attempt(3, proof_not_an_agent, s)
    valid = attempt(4, proof_valid, s)
    attempt(5, proof_rate_limit, s)
    malformed = attempt(6, proof_malformed, s)
    attempt(7, proof_token_limit, s)
    attempt(8, proof_retry, s)
    if valid and malformed:
        attempt(6, proof_backend_untouched, valid, malformed, not args.fast)

    failed = [name for name, ok in results if not ok]
    print("\n" + "=" * WIDTH)
    print(f"{len(results) - len(failed)}/{len(results)} governance proofs passed".center(WIDTH))
    print("=" * WIDTH)
    return len(failed)


if __name__ == "__main__":
    for stream in (sys.stdout, sys.stderr):
        getattr(stream, "reconfigure", lambda **_: None)(encoding="utf-8")
    sys.exit(main())
