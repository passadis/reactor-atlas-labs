"""Manage the Foundry hosted agent `atlas-incident-agent`: versions, pinning, rollback, identity.

    uv run python -m scripts.hosted_agent status              # versions, pinned version, identity
    uv run python -m scripts.hosted_agent pin 3               # route 100% of traffic to version 3
    uv run python -m scripts.hosted_agent rollback            # pin the previous good version
    uv run python -m scripts.hosted_agent record-identity     # agent identity -> azd env (for RBAC)
    uv run python -m scripts.hosted_agent check-guardrail 3   # rai_config resolves to a policy
    uv run python -m scripts.hosted_agent delete-version 1    # remove a version (never pinned)

Pinning is a pointer flip on the agent endpoint's version selector (`FixedRatio`, 100%), not a
build or a deploy (CLAUDE.md 9.2): Episode 4's "break the agent on stage" is `pin <bad version>`
and "recover" is `pin <good version>`, each a single control call that takes seconds.

"Previous good version" for rollback is the highest version below the pinned one that is `active`
and whose guardrail check passes. A version with a missing or placeholder RAI policy fails open
(docs/API-NOTES.md 12), so it is never a rollback target.
"""

from __future__ import annotations

import argparse
import json
import os
import shutil
import subprocess
import sys
import urllib.error
import urllib.request
from typing import Any

from atlas_common.auth import get_credential
from atlas_common.config import load_local_env
from azure.ai.projects import AIProjectClient
from azure.ai.projects.models import (
    AgentEndpointConfig,
    FixedRatioVersionSelectionRule,
    VersionSelector,
)

AGENT = os.environ.get("ATLAS_HOSTED_AGENT_NAME", "atlas-incident-agent")
ARM = "https://management.azure.com"


def project() -> AIProjectClient:
    load_local_env()
    return AIProjectClient(endpoint=os.environ["FOUNDRY_PROJECT_ENDPOINT"],
                           credential=get_credential())


def status_of(version: Any) -> str:
    """'active', 'creating', ... (the SDK returns an enum whose str() is 'AgentVersionStatus.X')."""
    raw = getattr(version, "status", "")
    return str(getattr(raw, "value", raw)).lower()


def versions(p: AIProjectClient) -> list[Any]:
    return sorted(p.agents.list_versions(AGENT), key=lambda v: int(v.version))


def pinned_version(p: AIProjectClient) -> str | None:
    details = p.agents.get(AGENT)
    endpoint = details.agent_endpoint
    selector = getattr(endpoint, "version_selector", None) if endpoint else None
    rules = getattr(selector, "version_selection_rules", None) or []
    full = [r for r in rules if int(getattr(r, "traffic_percentage", 0)) == 100]
    return str(full[0].agent_version) if full else None


def guardrail_ok(version: Any) -> tuple[bool, str]:
    definition = version.definition
    rai = getattr(definition, "rai_config", None) or (definition.get("rai_config")
                                                      if isinstance(definition, dict) else None)
    name = (rai.get("rai_policy_name") if isinstance(rai, dict)
            else getattr(rai, "rai_policy_name", None)) if rai else None
    if not name or "${" in name or not name.startswith("/subscriptions/"):
        return False, f"rai_policy_name is {name!r} - not a resolved ARM id (fails open)"
    token = get_credential().get_token(f"{ARM}/.default").token
    request = urllib.request.Request(  # noqa: S310 - fixed https ARM host
        f"{ARM}{name}?api-version=2024-10-01", headers={"Authorization": f"Bearer {token}"}
    )
    try:
        with urllib.request.urlopen(request, timeout=30):  # noqa: S310
            return True, name.rsplit("/", 1)[-1]
    except urllib.error.HTTPError as exc:
        return False, f"policy {name} does not exist ({exc.code}) - fails open"


def pin(p: AIProjectClient, version: str) -> None:
    target = next((v for v in versions(p) if str(v.version) == str(version)), None)
    if target is None:
        raise SystemExit(f"{AGENT} has no version {version}")
    if status_of(target) != "active":
        raise SystemExit(f"version {version} is {status_of(target)}, not active; refusing to pin")
    p.agents.update_details(
        AGENT,
        agent_endpoint=AgentEndpointConfig(
            version_selector=VersionSelector(version_selection_rules=[
                FixedRatioVersionSelectionRule(agent_version=str(version), traffic_percentage=100)
            ])
        ),
    )
    now = pinned_version(p)
    if now != str(version):
        raise SystemExit(f"pin did not stick: endpoint routes to {now}")
    print(f"{AGENT}: pinned to version {version}")


def cmd_status(p: AIProjectClient) -> int:
    details = p.agents.get(AGENT)
    identity = getattr(details, "instance_identity", None)
    report = {
        "agent": AGENT,
        "pinned": pinned_version(p),
        "identity_principal_id": getattr(identity, "principal_id", None),
        "versions": [
            {"version": v.version, "status": status_of(v),
             "created_at": str(getattr(v, "created_at", "")),
             "guardrail": guardrail_ok(v)[1]}
            for v in versions(p)
        ],
    }
    print(json.dumps(report, indent=2))
    return 0


def cmd_rollback(p: AIProjectClient) -> int:
    current = pinned_version(p)
    all_versions = versions(p)
    # "@latest" (the platform default) means the newest version is live.
    live = current if current and current.isdigit() else str(all_versions[-1].version)
    ceiling = int(live)
    for candidate in reversed(all_versions):
        if int(candidate.version) >= ceiling or status_of(candidate) != "active":
            continue
        ok, why = guardrail_ok(candidate)
        if ok:
            pin(p, str(candidate.version))
            print(f"rolled back from {current} to {candidate.version}")
            return 0
        print(f"skipping version {candidate.version}: {why}")
    raise SystemExit(f"no earlier good version to roll back to from {current}")


def cmd_record_identity(p: AIProjectClient) -> int:
    identity = getattr(p.agents.get(AGENT), "instance_identity", None)
    principal = getattr(identity, "principal_id", None)
    if not principal:
        raise SystemExit(f"{AGENT} has no instance identity yet (deploy it first)")
    azd = shutil.which("azd") or "azd"
    subprocess.run([azd, "env", "set", "ATLAS_HOSTED_AGENT_PRINCIPAL_ID", principal],
                   check=True, capture_output=True)
    print(f"ATLAS_HOSTED_AGENT_PRINCIPAL_ID={principal} written to the platform azd env; "
          "run `azd provision` to grant its data roles (rbac.bicep)")
    return 0


def cmd_check_guardrail(p: AIProjectClient, version: str | None) -> int:
    pinned = pinned_version(p)
    target = version or (pinned if pinned and pinned.isdigit() else str(versions(p)[-1].version))
    v = next(v for v in versions(p) if str(v.version) == target)
    ok, why = guardrail_ok(v)
    print(f"{'PASS' if ok else 'FAIL'} version {target} guardrail: {why}")
    return 0 if ok else 1


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__,
                                     formatter_class=argparse.RawDescriptionHelpFormatter)
    sub = parser.add_subparsers(dest="cmd", required=True)
    sub.add_parser("status")
    sub.add_parser("rollback")
    sub.add_parser("record-identity")
    pin_p = sub.add_parser("pin")
    pin_p.add_argument("version")
    check_p = sub.add_parser("check-guardrail")
    check_p.add_argument("version", nargs="?")
    del_p = sub.add_parser("delete-version")
    del_p.add_argument("version")
    del_p.add_argument("--force", action="store_true",
                       help="cascade-delete the version's idle sessions too")
    args = parser.parse_args(argv)

    p = project()
    if args.cmd == "status":
        return cmd_status(p)
    if args.cmd == "pin":
        pin(p, args.version)
        return 0
    if args.cmd == "rollback":
        return cmd_rollback(p)
    if args.cmd == "record-identity":
        return cmd_record_identity(p)
    if args.cmd == "check-guardrail":
        return cmd_check_guardrail(p, args.version)
    if args.cmd == "delete-version":
        if pinned_version(p) == str(args.version):
            raise SystemExit("refusing to delete the pinned version")
        p.agents.delete_version(agent_name=AGENT, agent_version=str(args.version),
                                force=args.force or None)
        print(f"{AGENT}: deleted version {args.version}")
        return 0
    return 2


if __name__ == "__main__":
    for s in (sys.stdout, sys.stderr):
        getattr(s, "reconfigure", lambda **_: None)(encoding="utf-8")
    sys.exit(main())
