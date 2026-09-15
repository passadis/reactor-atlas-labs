"""Runbook front matter and the search-index document shape."""

from datetime import date

from pydantic import BaseModel, ConfigDict, Field

from .types import DocType, Risk, Severity


class RunbookFrontMatter(BaseModel):
    """YAML front matter on every generated runbook."""

    model_config = ConfigDict(extra="forbid")

    runbook_id: str
    title: str
    services: list[str]
    severity_applicability: list[Severity]
    last_reviewed: date
    owner_team: str
    risk: Risk
    requires_approval: bool
    steps_are_destructive: bool
    # Marks a runbook carrying a deliberate indirect prompt-injection payload. Tests and the
    # Episode 4 XPIA evals find the canaries by this flag. Generators must never place a canary
    # outside data/seed/runbooks/. DATA-MODEL.md section 3.
    injection_canary: bool = False


class KnowledgeDocument(BaseModel):
    """One chunk as it lands in the `idx-atlas-knowledge` search index.

    `content_vector` is 3072 floats from text-embedding-3-large. The dimension is not negotiable:
    set it wrong in the index definition and the whole index is rebuilt after seeding.
    CLAUDE.md section 7.11.
    """

    model_config = ConfigDict(extra="forbid")

    id: str
    doc_type: DocType
    doc_id: str
    title: str
    chunk: str
    services: list[str]
    risk: Risk | None = None
    last_reviewed: str | None = None
    owner_team: str | None = None
    source_path: str
    injection_canary: bool = False
    content_vector: list[float] = Field(default_factory=list)
