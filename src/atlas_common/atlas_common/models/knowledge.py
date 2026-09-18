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
    # sha256 of the embedded text. Lets a re-seed skip re-embedding unchanged documents, which is
    # what makes scripts/seed.py idempotent and cheap to re-run.
    content_hash: str = ""
    content_vector: list[float] = Field(default_factory=list)


class KnowledgeHit(BaseModel):
    """One retrieved document, as a tool returns it to an agent.

    `doc_id` is what the agent cites. A factual claim without one is ungrounded, and the
    instructions tell the agent to say "I have no grounded source for that" instead.
    """

    model_config = ConfigDict(extra="forbid")

    doc_id: str
    doc_type: DocType
    title: str
    snippet: str
    services: list[str]
    last_reviewed: str | None
    score: float
    reranker_score: float | None = None
    injection_canary: bool = False


class KnowledgeResults(BaseModel):
    """What `search_knowledge` returns. Empty `hits` is a valid, typed answer - never an error."""

    model_config = ConfigDict(extra="forbid")

    query: str
    hits: list[KnowledgeHit]

    @property
    def citations(self) -> list[str]:
        """Distinct doc ids in rank order."""
        seen: list[str] = []
        for hit in self.hits:
            if hit.doc_id not in seen:
                seen.append(hit.doc_id)
        return seen
