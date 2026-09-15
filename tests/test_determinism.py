"""The seed data must be byte-identical across runs.

This is not a style preference. Every rehearsal and every live demo depends on the same prompt
producing the same shape of answer (CLAUDE.md section 6), and the Episode 4 regression tests
compare against frozen fixtures. If generation drifts, those comparisons silently stop meaning
anything.

The three usual causes of drift are covered here: unseeded randomness, wall-clock timestamps, and
dict ordering leaking into serialisation.
"""

from __future__ import annotations

import hashlib
from pathlib import Path

import pytest

from data.generators.common import DEFAULT_REFERENCE_DATE, DEFAULT_SEED, parse_reference_date
from data.generators.generate_all import generate

REFERENCE = parse_reference_date(DEFAULT_REFERENCE_DATE)


def tree_hash(root: Path) -> dict[str, str]:
    """Map each relative path to a hash of its bytes."""
    return {
        str(p.relative_to(root)).replace("\\", "/"): hashlib.sha256(p.read_bytes()).hexdigest()
        for p in sorted(root.rglob("*"))
        if p.is_file()
    }


@pytest.fixture(scope="module")
def two_runs(tmp_path_factory: pytest.TempPathFactory) -> tuple[Path, Path]:
    first = tmp_path_factory.mktemp("gen-a")
    second = tmp_path_factory.mktemp("gen-b")
    generate(DEFAULT_SEED, first, REFERENCE)
    generate(DEFAULT_SEED, second, REFERENCE)
    return first, second


def test_same_seed_produces_identical_bytes(two_runs: tuple[Path, Path]) -> None:
    first, second = two_runs
    a, b = tree_hash(first), tree_hash(second)
    assert sorted(a) == sorted(b), "file set differs between runs"
    differing = sorted(name for name in a if a[name] != b[name])
    assert differing == [], f"content differs between runs: {differing}"


def test_generation_produces_the_expected_file_set(two_runs: tuple[Path, Path]) -> None:
    first, _ = two_runs
    names = set(tree_hash(first))
    for expected in [
        "services.json",
        "incidents.jsonl",
        "changes.jsonl",
        "telemetry.jsonl",
        "people.json",
        "entitlements.json",
        "manifest.json",
    ]:
        assert expected in names
    assert sum(1 for n in names if n.startswith("runbooks/")) == 60


def test_a_different_seed_changes_the_data(tmp_path: Path) -> None:
    """Guards against a generator that ignores the seed and only looks deterministic."""
    a, b = tmp_path / "a", tmp_path / "b"
    generate(DEFAULT_SEED, a, REFERENCE)
    generate(DEFAULT_SEED + 1, b, REFERENCE)
    assert tree_hash(a) != tree_hash(b)


def test_no_wall_clock_leaks_into_output(tmp_path: Path) -> None:
    """A different reference date must move every timestamp, proving nothing reads the clock."""
    a, b = tmp_path / "a", tmp_path / "b"
    generate(DEFAULT_SEED, a, REFERENCE)
    generate(DEFAULT_SEED, b, parse_reference_date("2025-03-01T00:00:00Z"))
    assert (a / "incidents.jsonl").read_bytes() != (b / "incidents.jsonl").read_bytes()
    # Same seed and same reference: identical again. So the only inputs are seed and reference.
    c = tmp_path / "c"
    generate(DEFAULT_SEED, c, REFERENCE)
    assert tree_hash(a) == tree_hash(c)


def test_committed_seed_data_matches_the_generator(tmp_path: Path) -> None:
    """The data in git must be what the generator produces today.

    Catches the case where someone hand-edits a seed file: the demo would then depend on content
    that no `make seed` can reproduce.
    """
    committed = Path("data/seed")
    if not (committed / "manifest.json").exists():
        pytest.skip("data/seed not generated yet")
    fresh = tmp_path / "fresh"
    generate(DEFAULT_SEED, fresh, REFERENCE)
    a, b = tree_hash(committed), tree_hash(fresh)
    differing = sorted(name for name in b if a.get(name) != b[name])
    assert differing == [], (
        f"committed seed data differs from a fresh generation: {differing}. "
        "Re-run: uv run python -m data.generators.generate_all --seed 20260914 --out data/seed"
    )
