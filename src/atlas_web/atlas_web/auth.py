"""Who is calling. Container Apps built-in authentication (Easy Auth) does the sign-in.

Easy Auth validates the Entra token before a request reaches this process and forwards the claims
in `X-MS-CLIENT-PRINCIPAL` (base64 JSON). This module only reads that header; it never validates a
token itself, and it trusts the header only because ingress is the sole way in and the platform
strips any client-supplied copy.

`ATLAS_DEMO_AUTH_BYPASS=true` gives a local developer a signed-in demo identity without Entra. It is
refused outright when the process runs in Azure, and logged at ERROR level on every start and every
request while it is on, so it cannot quietly ship.
"""

from __future__ import annotations

import base64
import json
import logging
import os
from dataclasses import dataclass, field
from typing import Literal

from atlas_common.auth import credential_kind
from atlas_common.entitlements import APP_ROLE_APPROVER, APP_ROLE_ENGINEER
from fastapi import HTTPException, Request

log = logging.getLogger("atlas.web.auth")

PRINCIPAL_HEADER = "x-ms-client-principal"

_EMAIL_CLAIMS = (
    "preferred_username",
    "email",
    "upn",
    "http://schemas.xmlsoap.org/ws/2005/05/identity/claims/emailaddress",
    "http://schemas.xmlsoap.org/ws/2005/05/identity/claims/upn",
)
_OID_CLAIMS = ("oid", "http://schemas.microsoft.com/identity/claims/objectidentifier")
_ROLE_CLAIMS = ("roles", "http://schemas.microsoft.com/ws/2008/06/identity/claims/role")


@dataclass(frozen=True)
class Principal:
    object_id: str
    name: str
    email: str
    app_roles: tuple[str, ...] = field(default_factory=tuple)
    source: Literal["easyauth", "bypass"] = "easyauth"


def bypass_enabled() -> bool:
    return os.environ.get("ATLAS_DEMO_AUTH_BYPASS", "").strip().lower() in ("1", "true", "yes")


def check_bypass_at_startup() -> None:
    """Refuse the bypass in Azure; shout about it anywhere else."""
    if not bypass_enabled():
        return
    if credential_kind() == "managed":
        raise RuntimeError(
            "ATLAS_DEMO_AUTH_BYPASS is set in an Azure deployment. Refusing to start: the deployed "
            "app must authenticate every request through Entra (CLAUDE.md section 6)."
        )
    log.error("ATLAS_DEMO_AUTH_BYPASS IS ON - every request is treated as a signed-in demo user")


def parse_client_principal(header_value: str) -> Principal | None:
    """Decode Easy Auth's `X-MS-CLIENT-PRINCIPAL`. None if absent or malformed."""
    try:
        payload = json.loads(base64.b64decode(header_value + "=" * (-len(header_value) % 4)))
    except (ValueError, TypeError):
        return None
    claims: dict[str, list[str]] = {}
    for claim in payload.get("claims") or []:
        claims.setdefault(str(claim.get("typ")), []).append(str(claim.get("val")))

    def first(names: tuple[str, ...]) -> str:
        for name in names:
            if claims.get(name):
                return claims[name][0]
        return ""

    roles: list[str] = []
    for name in _ROLE_CLAIMS:
        roles.extend(claims.get(name, []))
    return Principal(
        object_id=first(_OID_CLAIMS),
        name=first(("name",)) or first(_EMAIL_CLAIMS),
        email=first(_EMAIL_CLAIMS).lower(),
        app_roles=tuple(sorted(set(roles))),
    )


def current_principal(request: Request) -> Principal:
    """FastAPI dependency. 401 if Easy Auth did not authenticate the request."""
    header = request.headers.get(PRINCIPAL_HEADER)
    if header:
        principal = parse_client_principal(header)
        if principal is not None:
            return principal
    if bypass_enabled() and credential_kind() != "managed":
        log.error("ATLAS_DEMO_AUTH_BYPASS used for %s %s", request.method, request.url.path)
        return Principal(
            object_id="demo-bypass",
            name="Demo presenter (auth bypass)",
            email="demo@contoso.example",
            app_roles=(APP_ROLE_APPROVER, APP_ROLE_ENGINEER),
            source="bypass",
        )
    raise HTTPException(status_code=401, detail="Sign in required.")
