"""Runtime settings, read from the environment.

In Azure every value arrives as a container or app setting written by Bicep. Locally they come
from `.env.local`, which `scripts/postprovision.sh` writes from the azd outputs. Nothing here is a
secret: endpoints and names only (CLAUDE.md section 1).
"""

from __future__ import annotations

import os
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path

from dotenv import load_dotenv

# The fixed logical names. Model deployments are aliases so the underlying model can be swapped
# without a code change (CLAUDE.md section 3).
KNOWLEDGE_INDEX = "idx-atlas-knowledge"
EMBEDDING_DIMENSIONS = 3072  # text-embedding-3-large. CLAUDE.md section 7.11.
MODEL_MAIN = "gpt-main"
MODEL_JUDGE = "gpt-judge"
MODEL_EMBED = "embed-main"

# The operations clock. The seeded Contoso world is anchored to 2026-09-01 and the canonical
# incident begins at 14:05 on that day, so "now" for every tool is fixed at 14:20 - fifteen minutes
# into the incident. Without this, "changes in the last four hours" would be measured from the real
# date, the canonical change would fall outside every window, and the demo would silently find
# nothing. Override with ATLAS_OPS_NOW.
DEFAULT_OPS_NOW = "2026-09-01T14:20:00Z"
INSTRUCTIONS_VERSION = "v1"


def load_local_env(path: Path | None = None) -> None:
    """Load `.env.local` if present. Existing environment variables always win."""
    candidate = path or Path(__file__).resolve().parents[3] / ".env.local"
    if candidate.exists():
        load_dotenv(candidate, override=False)


def _require(name: str) -> str:
    value = os.environ.get(name, "").strip()
    if not value:
        raise RuntimeError(
            f"{name} is not set. Locally, run `azd provision` (its postprovision hook writes "
            f".env.local); in Azure it is set by infra/main.bicep."
        )
    return value


@dataclass(frozen=True)
class Settings:
    cosmos_endpoint: str
    cosmos_database: str
    search_endpoint: str
    foundry_project_endpoint: str
    foundry_account_name: str
    model_main: str = MODEL_MAIN
    model_embed: str = MODEL_EMBED
    knowledge_index: str = KNOWLEDGE_INDEX
    ops_now: datetime = datetime(2026, 9, 1, 14, 20, tzinfo=UTC)
    instructions_version: str = INSTRUCTIONS_VERSION

    @property
    def foundry_openai_base_url(self) -> str:
        """The OpenAI v1 surface for inference and embeddings.

        Derived from the account, not the project: `/openai/v1/` lives on the account host and
        takes no api-version parameter (docs/API-NOTES.md section 8.6).
        """
        return f"https://{self.foundry_account_name}.services.ai.azure.com/openai/v1/"

    @property
    def foundry_openai_resource_url(self) -> str:
        """The account's Azure OpenAI host, the form the Search vectorizer expects."""
        return f"https://{self.foundry_account_name}.openai.azure.com"

    @classmethod
    def from_env(cls) -> Settings:
        load_local_env()
        return cls(
            cosmos_endpoint=_require("ATLAS_COSMOS_ENDPOINT"),
            cosmos_database=os.environ.get("ATLAS_COSMOS_DATABASE", "atlas"),
            search_endpoint=_require("ATLAS_SEARCH_ENDPOINT"),
            foundry_project_endpoint=_require("FOUNDRY_PROJECT_ENDPOINT"),
            foundry_account_name=_require("ATLAS_FOUNDRY_ACCOUNT_NAME"),
            ops_now=datetime.fromisoformat(
                os.environ.get("ATLAS_OPS_NOW", DEFAULT_OPS_NOW).replace("Z", "+00:00")
            ),
            instructions_version=os.environ.get("ATLAS_INSTRUCTIONS_VERSION", INSTRUCTIONS_VERSION),
        )
