"""Determinism helpers shared by every generator.

Re-running the generator with the same seed must produce byte-identical files. CI asserts it, and
every rehearsal depends on it: the same demo prompt has to give the same shape of answer every
time (CLAUDE.md section 6).

Three rules make that true, and all three are enforced here rather than remembered:

1. All randomness comes from one seeded `random.Random`. Never the module-level `random`.
2. No wall clock. Every timestamp derives from an explicit reference date.
3. JSON is written with sorted keys, fixed separators and LF endings, so the bytes do not depend
   on dict insertion order or on the platform.
"""

from __future__ import annotations

import json
import random
from collections.abc import Iterable, Sequence
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

from pydantic import BaseModel

# Everything is a synthetic Contoso world. Nothing may reference a real company, person, domain or
# product, and `verify_phase02.sh` greps for violations of this.
EMAIL_DOMAIN = "contoso.example"

DEFAULT_SEED = 20260914
DEFAULT_REFERENCE_DATE = "2026-09-01T00:00:00Z"


def parse_reference_date(value: str) -> datetime:
    """Parse the --reference-date argument into an aware UTC datetime."""
    parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    return parsed.astimezone(UTC) if parsed.tzinfo else parsed.replace(tzinfo=UTC)


def iso(moment: datetime) -> str:
    """Serialise a datetime the one way ATLAS does it: UTC, seconds, trailing Z."""
    return moment.astimezone(UTC).replace(microsecond=0).isoformat().replace("+00:00", "Z")


def minutes_before(reference: datetime, minutes: float) -> datetime:
    return reference - timedelta(minutes=minutes)


def pick[T](rng: random.Random, items: Sequence[T]) -> T:
    """Choose one item. Present so no generator reaches for the global `random` by habit."""
    return items[rng.randrange(len(items))]


def weighted_pick[T](rng: random.Random, weighted: Sequence[tuple[T, float]]) -> T:
    """Choose one item by weight, using only the supplied RNG."""
    total = sum(w for _, w in weighted)
    target = rng.random() * total
    running = 0.0
    for item, weight in weighted:
        running += weight
        if target <= running:
            return item
    return weighted[-1][0]


def _default(value: Any) -> Any:
    if isinstance(value, datetime):
        return iso(value)
    raise TypeError(f"not JSON serialisable: {type(value)!r}")


def write_json(path: Path, payload: Any) -> None:
    """Write pretty JSON deterministically.

    `sort_keys` removes dependence on dict ordering; the explicit newline keeps the bytes identical
    on Windows and Linux, which matters because CI and this laptop must agree.
    """
    path.parent.mkdir(parents=True, exist_ok=True)
    text = json.dumps(payload, indent=2, sort_keys=True, ensure_ascii=False, default=_default)
    path.write_text(text + "\n", encoding="utf-8", newline="\n")


def write_jsonl(path: Path, rows: Iterable[BaseModel]) -> None:
    """Write one compact JSON object per line, keys sorted."""
    path.parent.mkdir(parents=True, exist_ok=True)
    lines = [
        json.dumps(
            row.model_dump(mode="json"),
            sort_keys=True,
            separators=(",", ":"),
            ensure_ascii=False,
            default=_default,
        )
        for row in rows
    ]
    path.write_text("\n".join(lines) + "\n", encoding="utf-8", newline="\n")


def write_text(path: Path, text: str) -> None:
    """Write a text file with LF endings regardless of platform."""
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text, encoding="utf-8", newline="\n")
