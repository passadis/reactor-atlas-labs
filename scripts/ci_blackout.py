"""Refuse to touch the system within 6 hours of a scheduled broadcast (CLAUDE.md 9.5).

    python scripts/ci_blackout.py docs/episodes.yaml     # exit 1 inside a blackout window

`starts` is either a UTC timestamp ("2026-10-20T16:00:00Z": blocked 6 hours either side) or a
date with no time yet ("2026-10-06": the whole day in the presenter's timezone is blocked, plus
the 6 hours either side). A date is the conservative form for an episode whose hour is not fixed.

Standard library only: runs before anything is installed.
"""

from __future__ import annotations

import re
import sys
from datetime import UTC, date, datetime, timedelta, timezone
from pathlib import Path

WINDOW = timedelta(hours=6)
PRESENTER_TZ = timezone(timedelta(hours=3))  # the presenter's clock (UTC+3)


def window(value: str) -> tuple[datetime, datetime]:
    """The blocked interval for one `starts` value."""
    if re.fullmatch(r"\d{4}-\d{2}-\d{2}", value):
        day = datetime.combine(date.fromisoformat(value), datetime.min.time(), PRESENTER_TZ)
        return day - WINDOW, day + timedelta(days=1) + WINDOW
    start = datetime.fromisoformat(value.replace("Z", "+00:00"))
    return start - WINDOW, start + WINDOW


def main() -> int:
    text = Path(sys.argv[1] if len(sys.argv) > 1 else "docs/episodes.yaml").read_text("utf-8")
    now = datetime.now(UTC)
    for value in re.findall(r'starts:\s*"([^"]+)"', text):
        begin, end = window(value)
        if begin <= now < end:
            print(f"blackout: episode {value} blocks {begin.isoformat()} to {end.isoformat()}; "
                  "not touching the system")
            return 1
    print("no scheduled episode within 6 hours")
    return 0


if __name__ == "__main__":
    sys.exit(main())
