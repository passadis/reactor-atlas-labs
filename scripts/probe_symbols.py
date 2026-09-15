"""Verify the agent-stack symbol surface by import, never from memory.

This is the machine-checkable half of the CLAUDE.md prime directive. It fails loudly when a
package upgrade moves or removes a symbol the build depends on, so a version bump can never
silently break a later phase.

Usage:
    uv run python scripts/probe_symbols.py            # human-readable table
    uv run python scripts/probe_symbols.py --json     # machine-readable
    uv run python scripts/probe_symbols.py --check    # exit 1 if any REQUIRED symbol is missing

Keep EXPECTED in step with docs/API-NOTES.md section 3.
"""

from __future__ import annotations

import argparse
import importlib
import inspect
import json
import sys
from typing import Any

# module -> {symbol: required}
# required=True  -> the build depends on it; absence is a failure.
# required=False -> we assert its ABSENCE (a stale name we must never reintroduce).
EXPECTED: dict[str, dict[str, bool]] = {
    "agent_framework": {
        "Agent": True,
        "WorkflowBuilder": True,
        "tool": True,
        "RunContext": True,
        "Executor": True,
        "AgentExecutor": True,
        "AgentExecutorRequest": True,
        "AgentExecutorResponse": True,
        "CheckpointStorage": True,
        "InMemoryCheckpointStorage": True,
        "FileCheckpointStorage": True,
        "register_checkpoint_type": True,
        "response_handler": True,
        "WorkflowEvent": True,
        "WorkflowEventType": True,
        "WorkflowRunResult": True,
        "WorkflowViz": True,
        "ToolApprovalMiddleware": True,
        "ToolMode": True,
        "MCPStreamableHTTPTool": True,
        # Stale names. If any of these reappear, re-read the upgrade guide before using them.
        "ChatAgent": False,
        "RequestInfoExecutor": False,
        "RequestInfoEvent": False,
        "WorkflowOutputEvent": False,
        "SequentialBuilder": False,  # lives in agent_framework_orchestrations
    },
    "agent_framework.foundry": {
        "FoundryChatClient": True,
        "FoundryAgent": True,
        "FoundryChatOptions": True,
    },
    "agent_framework_foundry_hosting": {
        "FoundryToolbox": True,
        "ResponsesHostServer": True,
        "InvocationsHostServer": True,
    },
    "agent_framework_azure_cosmos": {
        "CosmosCheckpointStorage": True,
    },
    "agent_framework_orchestrations": {
        "SequentialBuilder": True,
        "ConcurrentBuilder": True,
        "HandoffBuilder": True,
        "MagenticBuilder": True,
    },
}

# Signatures whose shape later phases depend on. Substring must appear in str(signature).
SIGNATURE_CONTRACTS: list[tuple[str, str, str, str]] = [
    ("agent_framework", "tool", "approval_mode", "Phase 07 tool-level approval"),
    ("agent_framework", "WorkflowBuilder.__init__", "checkpoint_storage", "Phase 07 checkpointing"),
    ("agent_framework", "WorkflowBuilder.__init__", "start_executor", "Phase 06 graph entry point"),
    ("agent_framework", "RunContext.request_info", "response_type", "Phase 07 HITL suspend"),
]


def _resolve(module: Any, dotted: str) -> Any:
    obj = module
    for part in dotted.split("."):
        obj = getattr(obj, part)
    return obj


def probe() -> dict[str, Any]:
    result: dict[str, Any] = {
        "python": sys.version.split()[0],
        "modules": {},
        "signatures": [],
        "failures": [],
    }

    for mod_name, symbols in EXPECTED.items():
        try:
            mod = importlib.import_module(mod_name)
        except Exception as exc:
            result["modules"][mod_name] = {
                "imported": False,
                "error": f"{type(exc).__name__}: {exc}",
            }
            result["failures"].append(f"{mod_name}: import failed ({type(exc).__name__}: {exc})")
            continue

        entry: dict[str, Any] = {
            "imported": True,
            "version": getattr(mod, "__version__", None),
            "symbols": {},
        }
        for sym, required in symbols.items():
            present = hasattr(mod, sym)
            entry["symbols"][sym] = {"present": present, "required": required}
            if required and not present:
                result["failures"].append(f"{mod_name}.{sym}: REQUIRED but missing")
            if not required and present:
                result["failures"].append(
                    f"{mod_name}.{sym}: reappeared - expected absent, re-read the upgrade guide"
                )
        result["modules"][mod_name] = entry

    for mod_name, dotted, needle, why in SIGNATURE_CONTRACTS:
        try:
            mod = importlib.import_module(mod_name)
            sig = str(inspect.signature(_resolve(mod, dotted)))
        except Exception as exc:
            result["failures"].append(f"{mod_name}.{dotted}: {type(exc).__name__}: {exc}")
            continue
        ok = needle in sig
        result["signatures"].append(
            {"target": f"{mod_name}.{dotted}", "needs": needle, "ok": ok, "why": why}
        )
        if not ok:
            result["failures"].append(f"{mod_name}.{dotted}: '{needle}' gone - breaks {why}")

    return result


def render(data: dict[str, Any]) -> None:
    print(f"python {data['python']}\n")
    for mod_name, entry in data["modules"].items():
        if not entry["imported"]:
            print(f"[FAIL] {mod_name}: {entry['error']}\n")
            continue
        ver = entry["version"] or "n/a"
        print(f"[ OK ] {mod_name}  version={ver}")
        for sym, info in entry["symbols"].items():
            if info["required"]:
                mark = "YES" if info["present"] else "*** MISSING ***"
            else:
                mark = "*** REAPPEARED ***" if info["present"] else "absent (expected)"
            print(f"         {mark:<20} {sym}")
        print()

    print("signature contracts")
    for s in data["signatures"]:
        print(f"  {'OK ' if s['ok'] else 'BAD'} {s['target']} needs '{s['needs']}'  <- {s['why']}")

    print()
    if data["failures"]:
        print(f"{len(data['failures'])} FAILURE(S):")
        for f in data["failures"]:
            print(f"  - {f}")
    else:
        print("All symbol and signature contracts hold.")


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--json", action="store_true", help="machine-readable output")
    ap.add_argument("--check", action="store_true", help="exit non-zero on any failure")
    args = ap.parse_args()

    data = probe()
    if args.json:
        print(json.dumps(data, indent=2))
    else:
        render(data)

    return 1 if (args.check and data["failures"]) else 0


if __name__ == "__main__":
    raise SystemExit(main())
