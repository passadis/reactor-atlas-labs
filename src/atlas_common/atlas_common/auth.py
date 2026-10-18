"""The one place ATLAS obtains an Azure credential.

CLAUDE.md section 1: no `DefaultAzureCredential` surprises. `DefaultAzureCredential` walks a chain
of eight sources and silently takes the first that answers, which on a developer laptop that also
has environment variables, a VS Code sign-in and a managed-identity endpoint can pick the wrong
identity and produce a 403 that looks like a missing role assignment.

So the choice is explicit:

- **In Azure** (Container Apps, Functions, Logic Apps, the hosted agent), use the user-assigned
  managed identity named by `AZURE_CLIENT_ID`. Every ATLAS compute resource has exactly one.
- **Locally**, use the Azure CLI sign-in (`az login`), and nothing else.

`ATLAS_CREDENTIAL=cli|managed` overrides detection when a test needs to force one path.
"""

from __future__ import annotations

import os
from functools import cache
from typing import Literal

from azure.core.credentials import TokenCredential
from azure.core.credentials_async import AsyncTokenCredential
from azure.identity import AzureCliCredential, ManagedIdentityCredential
from azure.identity.aio import AzureCliCredential as AsyncAzureCliCredential
from azure.identity.aio import ManagedIdentityCredential as AsyncManagedIdentityCredential

CredentialKind = Literal["managed", "cli"]

# Token scopes used across ATLAS, named once so a typo cannot produce a confusing 401.
SCOPE_FOUNDRY = "https://ai.azure.com/.default"
SCOPE_COGNITIVE = "https://cognitiveservices.azure.com/.default"
SCOPE_MANAGEMENT = "https://management.azure.com/.default"

# Set by the platform in every Azure host ATLAS uses. Their presence is what "running in Azure"
# means here; a laptop never has them.
_AZURE_HOST_MARKERS = (
    "IDENTITY_ENDPOINT",
    "MSI_ENDPOINT",
    "CONTAINER_APP_NAME",
    "WEBSITE_SITE_NAME",
)


def credential_kind() -> CredentialKind:
    """Decide which credential this process should use."""
    forced = os.environ.get("ATLAS_CREDENTIAL", "").strip().lower()
    if forced in ("managed", "cli"):
        return forced  # type: ignore[return-value]
    if any(os.environ.get(marker) for marker in _AZURE_HOST_MARKERS):
        return "managed"
    return "cli"


def _client_id() -> str:
    client_id = os.environ.get("AZURE_CLIENT_ID", "").strip()
    if not client_id:
        raise RuntimeError(
            "Running in Azure but AZURE_CLIENT_ID is not set. Every ATLAS compute resource has a "
            "user-assigned identity and must name it explicitly; see CLAUDE.md section 1."
        )
    return client_id


@cache
def get_credential() -> TokenCredential:
    """Synchronous credential. Cached: token caching lives inside the credential object."""
    if credential_kind() == "managed":
        return ManagedIdentityCredential(client_id=_client_id())
    return AzureCliCredential(process_timeout=30)


def get_async_credential() -> AsyncTokenCredential:
    """Async credential. Not cached - an async credential is bound to the event loop it was used
    on, and the orchestrator and tests create more than one loop. Close it when done."""
    if credential_kind() == "managed":
        return AsyncManagedIdentityCredential(client_id=_client_id())
    return AsyncAzureCliCredential(process_timeout=30)
