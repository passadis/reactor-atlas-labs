"""Refuse to touch the system within 6 hours of a scheduled broadcast (CLAUDE.md 9.5).

    python scripts/ci_blackout.py docs/episodes.yaml     # exit 1 inside a blackout window

Standard library only: runs before anything is installed.
"""

from __future__ import annotations

import re
import sys
from datetime import UTC, datetime
from pathlib import Path

WINDOW_S = 6 * 3600


def main() -> int:
    text = Path(sys.argv[1] if len(sys.argv) > 1 else "docs/episodes.yaml").read_text("utf-8")
    now = datetime.now(UTC)
    for when in re.findall(r'starts:\s*"([^"]+)"', text):
        start = datetime.fromisoformat(when.replace("Z", "+00:00"))
        if abs((start - now).total_seconds()) < WINDOW_S:
            print(f"blackout: an episode starts at {start.isoformat()}; not touching the system")
            return 1
    print("no scheduled episode within 6 hours")
    return 0


if __name__ == "__main__":
    sys.exit(main())
