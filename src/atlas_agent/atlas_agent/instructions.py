"""Instruction versions, loaded per request from Cosmos DB.

CLAUDE.md section 9.1: switching v0 and v1 is used live in Episode 1 Beat 2 and Episode 4 Beat 4,
and nothing may be built or deployed during a broadcast. So the active version is DATA:

- Every version lives in the `agent-instructions` container, seeded from the files in
  `atlas_agent/instructions/` (git is the source of truth; `scripts/seed.py` copies them in).
- A pointer document `<agent>:active` names the version in force. `inject_weak_instructions.py`
  flips it to v0 with a single document write, and `restore_baseline.py` flips it back.
- A caller may also request a version explicitly, per request, which is how the web app's v0/v1
  toggle works without touching the shared pointer.

Precedence: explicit request > active pointer > `Settings.instructions_version`.

If Cosmos is unreachable the packaged file for the requested version is used and the source is
reported as "packaged", so a data-store outage degrades the agent rather than stopping it.
"""

from __future__ import annotations

import hashlib
from dataclasses import dataclass
from importlib import resources
from typing import Literal

from atlas_common.repositories import CosmosStore, RepositoryUnavailableError

AGENT_NAME = "atlas-incident-agent"
CONTAINER = "agent-instructions"
KNOWN_VERSIONS = ("v0", "v1", "v2")
ACTIVE_BASELINE = "v2"


@dataclass(frozen=True)
class LoadedInstructions:
    agent: str
    version: str
    text: str
    sha256: str
    source: Literal["cosmos", "packaged"]

    def render(self, *, now: str) -> str:
        """Fill the placeholders. `replace`, not `format`: instruction text may contain braces."""
        return self.text.replace("{now}", now)


def packaged_text(version: str, agent: str = AGENT_NAME) -> str:
    """Read an instruction version shipped inside the package."""
    name = f"{agent.replace('-', '_')}.{version}.md"
    return (resources.files("atlas_agent") / "instructions" / name).read_text(encoding="utf-8")


def _sha(text: str) -> str:
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


def instruction_documents(agent: str = AGENT_NAME) -> list[dict[str, str]]:
    """The documents `scripts/seed.py` writes: one per version, plus the active pointer."""
    docs = [
        {
            "id": f"{agent}:{version}",
            "agent": agent,
            "version": version,
            "text": packaged_text(version, agent),
            "sha256": _sha(packaged_text(version, agent)),
        }
        for version in KNOWN_VERSIONS
    ]
    # The version seed.py and demo-reset restore. v2 since 2026-10-05 (Phase 12, API-NOTES 20.7).
    docs.append({"id": f"{agent}:active", "agent": agent, "version": ACTIVE_BASELINE})
    return docs


class InstructionStore:
    """Reads instruction versions and the active pointer from Cosmos."""

    def __init__(self, store: CosmosStore, *, default_version: str, agent: str = AGENT_NAME):
        self._store = store
        self._default = default_version
        self._agent = agent

    async def active_version(self) -> str:
        try:
            rows = await self._store.query(
                CONTAINER,
                "SELECT c.version FROM c WHERE c.id = @id",
                [{"name": "@id", "value": f"{self._agent}:active"}],
                partition_key=self._agent,
            )
        except RepositoryUnavailableError:
            return self._default
        return str(rows[0]["version"]) if rows else self._default

    async def set_active(self, version: str) -> None:
        """Flip the shared pointer. The only write the v0/v1 switch ever needs."""
        if version not in KNOWN_VERSIONS:
            raise ValueError(f"unknown instruction version {version!r}; known: {KNOWN_VERSIONS}")
        await self._store.upsert(
            CONTAINER, {"id": f"{self._agent}:active", "agent": self._agent, "version": version}
        )

    async def load(self, version: str | None = None) -> LoadedInstructions:
        chosen = version or await self.active_version()
        try:
            rows = await self._store.query(
                CONTAINER,
                "SELECT c.text FROM c WHERE c.id = @id",
                [{"name": "@id", "value": f"{self._agent}:{chosen}"}],
                partition_key=self._agent,
            )
        except RepositoryUnavailableError:
            rows = []
        if rows:
            text = str(rows[0]["text"])
            return LoadedInstructions(self._agent, chosen, text, _sha(text), "cosmos")
        text = packaged_text(chosen, self._agent)
        return LoadedInstructions(self._agent, chosen, text, _sha(text), "packaged")
