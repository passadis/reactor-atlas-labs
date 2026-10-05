"""Regression kit: bad_tool_description.

See scripts/regression_kit.py for what it changes and what should notice.

    uv run python -m scripts.inject_bad_tool_description --apply | --revert | --status
"""

import sys

from scripts.regression_kit import cli

if __name__ == "__main__":
    sys.exit(cli("bad_tool_description"))
