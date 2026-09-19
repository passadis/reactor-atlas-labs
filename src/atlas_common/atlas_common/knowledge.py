"""Turning seed data into search-index documents. Pure functions: no Azure, no I/O.

Kept separate from `scripts/seed.py` so chunking can be unit tested and so the ops API, the seeder
and the evaluation harness all agree on what a document id and a chunk look like.

Chunking follows the Phase 02 spec: about 800 tokens with a 100-token overlap, and every chunk
carries its heading context so a retrieved step is never an orphan. Token counts are approximated
at four characters per token rather than by pulling in a tokenizer. The runbooks are short enough
that most sections never reach the limit, so the approximation only decides where the occasional
long section splits, and determinism matters more here than an exact count.
"""

from __future__ import annotations

import hashlib
import re
from collections.abc import Iterable

from .models import Incident, KnowledgeDocument, RunbookFrontMatter

CHARS_PER_TOKEN = 4
CHUNK_TOKENS = 800
OVERLAP_TOKENS = 100
CHUNK_CHARS = CHUNK_TOKENS * CHARS_PER_TOKEN
OVERLAP_CHARS = OVERLAP_TOKENS * CHARS_PER_TOKEN

_FRONT_MATTER = re.compile(r"\A---\n(.*?)\n---\n", re.S)


def content_hash(*parts: str) -> str:
    """Stable hash of the text a document is embedded from.

    Stored on every index document so a re-seed can skip re-embedding unchanged content. That is
    what makes `scripts/seed.py` both idempotent and cheap to re-run.
    """
    digest = hashlib.sha256()
    for part in parts:
        digest.update(part.encode("utf-8"))
        digest.update(b"\x00")
    return digest.hexdigest()


def parse_front_matter(markdown: str) -> tuple[dict[str, str], str]:
    """Split a runbook into its flat key/value front matter and its body.

    The generator writes a deliberately simple YAML subset (scalars and inline lists), so this
    avoids a YAML dependency in the runtime package.
    """
    match = _FRONT_MATTER.match(markdown)
    if not match:
        return {}, markdown
    fields: dict[str, str] = {}
    for line in match.group(1).splitlines():
        key, _, value = line.partition(":")
        fields[key.strip()] = value.strip()
    return fields, markdown[match.end() :]


def parse_list(value: str) -> list[str]:
    """Parse an inline YAML list such as `[a, b]`."""
    inner = value.strip().removeprefix("[").removesuffix("]")
    return [item.strip() for item in inner.split(",") if item.strip()]


def _sections(body: str) -> list[tuple[str, str]]:
    """Split a markdown body into (heading, text) pairs on `## ` boundaries."""
    sections: list[tuple[str, str]] = []
    heading = ""
    lines: list[str] = []
    for line in body.splitlines():
        if line.startswith("## "):
            if any(s.strip() for s in lines):
                sections.append((heading, "\n".join(lines).strip()))
            heading, lines = line[3:].strip(), []
        elif line.startswith("# "):
            continue  # the document title is carried in the chunk prefix instead
        else:
            lines.append(line)
    if any(s.strip() for s in lines):
        sections.append((heading, "\n".join(lines).strip()))
    return sections


def chunk_markdown(title: str, body: str, context: str = "") -> list[str]:
    """Pack a markdown body into chunks of up to ~800 tokens.

    Sections are packed greedily, with their `## ` heading lines kept inline, so a chunk carries its
    own heading context. Every chunk is prefixed with the document title and optional `context`
    (the services a runbook covers), so it is self-describing when retrieved on its own.

    Packing rather than splitting at every heading is deliberate. The first version emitted one
    chunk per section, turning a runbook that fits in a single chunk into five fragments, each only
    partly about the runbook's subject. Against 500 incident records whose titles read literally as
    "Checkout Api latency degradation", those fragments lost the canonical query outright, and
    RB-0042 - which the Episode 4 evals require the agent to cite - fell out of the top five.

    A single section longer than the budget is windowed with overlap.
    """
    prefix = f"{title}\n{context}".strip()
    budget = CHUNK_CHARS - len(prefix) - 2

    blocks: list[str] = []
    for heading, text in _sections(body):
        block = f"## {heading}\n{text}" if heading else text
        if len(block) <= budget:
            blocks.append(block)
            continue
        start = 0
        while start < len(block):
            blocks.append(block[start : start + budget])
            if start + budget >= len(block):
                break
            start += budget - OVERLAP_CHARS

    chunks: list[str] = []
    current: list[str] = []
    size = 0
    for block in blocks:
        if current and size + len(block) + 2 > budget:
            chunks.append(f"{prefix}\n\n" + "\n\n".join(current))
            current, size = [], 0
        current.append(block)
        size += len(block) + 2
    if current:
        chunks.append(f"{prefix}\n\n" + "\n\n".join(current))
    return chunks


def runbook_documents(
    runbooks: Iterable[tuple[RunbookFrontMatter, str]],
) -> list[KnowledgeDocument]:
    """One index document per runbook chunk."""
    docs: list[KnowledgeDocument] = []
    for fm, markdown in runbooks:
        _, body = parse_front_matter(markdown)
        # Services go into the chunk text as well as the filterable field: the filter narrows
        # retrieval, but only text is seen by the keyword and semantic rankers.
        context = f"Services: {', '.join(fm.services)}. Risk: {fm.risk}."
        # The id leads the title, as it already does for incidents and postmortems. The Foundry
        # Azure AI Search agent tool shows the model a title and content but never `doc_id`, so
        # without it a prompt agent can name a runbook but not cite it (docs/API-NOTES.md 10.3).
        title = f"{fm.runbook_id} {fm.title}"
        for n, chunk in enumerate(chunk_markdown(title, body, context), start=1):
            docs.append(
                KnowledgeDocument(
                    # Search keys allow letters, digits, underscore, dash and equals only.
                    id=f"{fm.runbook_id}-c{n:02d}",
                    doc_type="runbook",
                    doc_id=fm.runbook_id,
                    title=title,
                    chunk=chunk,
                    services=list(fm.services),
                    risk=fm.risk,
                    last_reviewed=fm.last_reviewed.isoformat(),
                    owner_team=fm.owner_team,
                    source_path=f"data/seed/runbooks/{fm.runbook_id}.md",
                    injection_canary=fm.injection_canary,
                )
            )
    return docs


def incident_documents(incidents: Iterable[Incident]) -> list[KnowledgeDocument]:
    """One document per past incident: title, description and resolution.

    This is what lets the knowledge agent answer "has this happened before" with an incident id
    rather than a vague recollection.
    """
    docs: list[KnowledgeDocument] = []
    for inc in incidents:
        body = "\n".join(
            part
            for part in (
                inc.description,
                f"Root cause: {inc.root_cause}" if inc.root_cause else "",
                f"Resolution: {inc.resolution}" if inc.resolution else "",
            )
            if part
        )
        docs.append(
            KnowledgeDocument(
                id=inc.incident_id,
                doc_type="incident",
                doc_id=inc.incident_id,
                title=f"{inc.incident_id} {inc.title}",
                chunk=f"{inc.incident_id} {inc.title} ({inc.severity}, {inc.category})\n\n{body}",
                services=list(inc.affected_services),
                last_reviewed=inc.opened_at.date().isoformat(),
                source_path="data/seed/incidents.jsonl",
            )
        )
    return docs


def postmortem_documents(incidents: Iterable[Incident]) -> list[KnowledgeDocument]:
    """Postmortem summaries for every Sev1 and Sev2 incident that has one.

    The seed carries postmortem URLs rather than postmortem documents, so these are written from
    the incident's recorded root cause and resolution. They are a separate `doc_type` so the
    knowledge agent can prefer a considered postmortem over a raw incident record.
    """
    docs: list[KnowledgeDocument] = []
    for inc in incidents:
        if not inc.postmortem_url or not inc.root_cause:
            continue
        text = (
            f"Postmortem for {inc.incident_id}: {inc.title}\n\n"
            f"Severity {inc.severity}. Detected after {inc.time_to_detect_minutes} minutes, "
            f"mitigated after {inc.time_to_mitigate_minutes} minutes.\n"
            f"Root cause: {inc.root_cause}\n"
            f"Resolution: {inc.resolution or 'not recorded'}\n"
            f"Linked changes: {', '.join(inc.linked_change_ids) or 'none'}"
        )
        docs.append(
            KnowledgeDocument(
                id=f"PM-{inc.incident_id}",
                doc_type="postmortem",
                doc_id=inc.incident_id,
                title=f"Postmortem {inc.incident_id}",
                chunk=text,
                services=list(inc.affected_services),
                last_reviewed=inc.opened_at.date().isoformat(),
                source_path=inc.postmortem_url,
            )
        )
    return docs


def embedding_text(doc: KnowledgeDocument) -> str:
    """The exact text a document is embedded from. One definition, used by seeder and verifier."""
    return f"{doc.title}\n\n{doc.chunk}"
