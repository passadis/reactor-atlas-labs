"""Unit tests for chunking, front-matter parsing and search filters. No Azure."""

from __future__ import annotations

from atlas_common.knowledge import (
    CHUNK_CHARS,
    chunk_markdown,
    content_hash,
    parse_front_matter,
    parse_list,
)
from atlas_common.repositories import build_filter

RUNBOOK = """---
runbook_id: RB-0001
title: Example
services: [svc-a, svc-b]
---

# Example

## Symptoms

- Something is slow.

## Mitigation

1. Scale out.
"""


def test_front_matter_round_trip() -> None:
    fields, body = parse_front_matter(RUNBOOK)
    assert fields["runbook_id"] == "RB-0001"
    assert parse_list(fields["services"]) == ["svc-a", "svc-b"]
    assert body.lstrip().startswith("# Example")


def test_small_document_packs_into_one_chunk() -> None:
    """The regression that dropped RB-0042 out of the top five: one chunk per heading."""
    _, body = parse_front_matter(RUNBOOK)
    chunks = chunk_markdown("Example", body, "Services: svc-a, svc-b.")
    assert len(chunks) == 1


def test_every_chunk_carries_title_context_and_headings() -> None:
    _, body = parse_front_matter(RUNBOOK)
    (chunk,) = chunk_markdown("Example", body, "Services: svc-a, svc-b.")
    assert chunk.startswith("Example\nServices: svc-a, svc-b.")
    assert "## Symptoms" in chunk
    assert "## Mitigation" in chunk


def test_long_document_splits_within_budget() -> None:
    sections = "\n\n".join(f"## Section {i}\n" + ("word " * 300) for i in range(12))
    chunks = chunk_markdown("Long", sections)
    assert len(chunks) > 1
    assert all(len(c) <= CHUNK_CHARS for c in chunks)
    assert all(c.startswith("Long") for c in chunks)


def test_oversized_single_section_is_windowed_with_overlap() -> None:
    body = "## Huge\n" + ("x" * (CHUNK_CHARS * 3))
    chunks = chunk_markdown("T", body)
    assert len(chunks) >= 3
    assert all(len(c) <= CHUNK_CHARS for c in chunks)


def test_chunking_is_deterministic() -> None:
    _, body = parse_front_matter(RUNBOOK)
    assert chunk_markdown("Example", body) == chunk_markdown("Example", body)


def test_content_hash_is_stable_and_sensitive() -> None:
    assert content_hash("a", "b") == content_hash("a", "b")
    assert content_hash("a", "b") != content_hash("ab")  # separator prevents collisions


def test_filter_scopes_by_doc_type() -> None:
    assert build_filter(doc_types=["runbook"]) == "(doc_type eq 'runbook')"


def test_filter_combines_services_and_types() -> None:
    f = build_filter(services=["svc-a", "svc-b"], doc_types=["runbook", "postmortem"])
    assert f is not None
    assert "doc_type eq 'runbook' or doc_type eq 'postmortem'" in f
    assert "search.in(s, 'svc-a,svc-b', ',')" in f


def test_filter_escapes_quotes() -> None:
    """A service id is data. It must never be able to break out of the OData string."""
    f = build_filter(services=["svc-x' or 1 eq 1 or 'a"])
    assert f is not None
    assert "svc-x'' or 1 eq 1 or ''a" in f


def test_no_filter_when_unscoped() -> None:
    assert build_filter() is None
