"""Flip the ops API chaos flag - an application-level outage, never a stopped Function App.

    uv run python -m scripts.chaos_flag on [--seconds 120]
    uv run python -m scripts.chaos_flag off
    uv run python -m scripts.chaos_flag status

CLAUDE.md 9.3: the chaos beat is a DATA write (one document in the `control` container), not a
control-plane write. While it is on, every ops API route except /health answers a typed 503; the
gateway retries, the client's breaker opens, and the agent says "I could not reach ...". The
document always carries an expiry, so a forgotten flag turns itself off. Workers cache the flag
for up to 3 s.
"""

from __future__ import annotations

import argparse
import asyncio
import sys
from datetime import UTC, datetime, timedelta

from atlas_common.config import Settings, load_local_env
from atlas_common.repositories.base import CosmosStore

CONTROL = "control"
DOC_ID = "chaos-ops-api"


async def run(action: str, seconds: int) -> int:
    async with CosmosStore(Settings.from_env()) as store:
        if action == "status":
            rows = await store.query(CONTROL, "SELECT * FROM c WHERE c.id = @id",
                                     [{"name": "@id", "value": DOC_ID}], partition_key=DOC_ID)
            doc = rows[0] if rows else {}
            expires = doc.get("expires_at")
            live = bool(doc.get("enabled")) and bool(expires) and (
                datetime.fromisoformat(str(expires)) > datetime.now(UTC))
            print(f"ops API chaos flag: {'ON' if live else 'off'}"
                  + (f" (until {expires})" if live else ""))
            return 0
        now = datetime.now(UTC)
        await store.upsert(CONTROL, {
            "id": DOC_ID, "enabled": action == "on",
            "set_at": now.isoformat(),
            "expires_at": (now + timedelta(seconds=seconds)).isoformat(),
            "set_by": "scripts/chaos_flag.py",
        })
        print(f"ops API chaos flag {'ON for ' + str(seconds) + ' s' if action == 'on' else 'off'}"
              " (workers pick it up within 3 s)")
    return 0


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("action", choices=["on", "off", "status"])
    parser.add_argument("--seconds", type=int, default=120)
    args = parser.parse_args(argv)
    load_local_env()
    return asyncio.run(run(args.action, max(5, min(args.seconds, 900))))


if __name__ == "__main__":
    for stream in (sys.stdout, sys.stderr):
        getattr(stream, "reconfigure", lambda **_: None)(encoding="utf-8")
    sys.exit(main())
