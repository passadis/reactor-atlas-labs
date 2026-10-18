"""Read one Key Vault secret by URI, with the workload's own identity.

The APIM subscription key is the only secret ATLAS holds (CLAUDE.md section 1). It lives in Key
Vault and every workload is given its URI, never its value: `ATLAS_APIM_KEY_SECRET_URI`. This reads
it once at first use over the Key Vault REST API - one GET, so no extra SDK dependency - and the
caller caches it for the life of the process.
"""

from __future__ import annotations

import logging
from typing import Any

import httpx

log = logging.getLogger("atlas.secrets")

VAULT_SCOPE = "https://vault.azure.net/.default"
API_VERSION = "7.4"


class SecretUnavailableError(RuntimeError):
    """The secret could not be read. The message never contains the secret."""


async def read_secret(uri: str, credential: Any, *, timeout_s: float = 10.0) -> str:
    """The current value of the secret at `uri` (https://<vault>.vault.azure.net/secrets/<name>)."""
    if not uri.startswith("https://") or "/secrets/" not in uri:
        raise SecretUnavailableError("not a Key Vault secret URI")
    token = await credential.get_token(VAULT_SCOPE)
    async with httpx.AsyncClient(timeout=timeout_s) as http:
        response = await http.get(uri.rstrip("/"), params={"api-version": API_VERSION},
                                  headers={"Authorization": f"Bearer {token.token}"})
    if response.status_code != 200:
        # 403 here is a missing Key Vault Secrets User assignment, not a bad secret.
        raise SecretUnavailableError(
            f"Key Vault answered {response.status_code} for {uri.rsplit('/', 1)[-1]}")
    value = response.json().get("value")
    if not isinstance(value, str) or not value:
        raise SecretUnavailableError("the secret has no value")
    log.info("read secret %s", uri.rsplit("/", 1)[-1])
    return value


__all__ = ["SecretUnavailableError", "read_secret"]
