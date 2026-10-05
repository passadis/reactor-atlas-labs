"""Regression kit: stale_knowledge.

See scripts/regression_kit.py for what it changes and what should notice.

    uv run python -m scripts.inject_stale_knowledge --apply | --revert | --status
"""

import sys

from scripts.regression_kit import cli

if __name__ == "__main__":
    sys.exit(cli("stale_knowledge"))
