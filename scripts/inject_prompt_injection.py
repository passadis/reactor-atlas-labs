"""Regression kit: prompt_injection.

See scripts/regression_kit.py for what it changes and what should notice.

    uv run python -m scripts.inject_prompt_injection --apply | --revert | --status
"""

import sys

from scripts.regression_kit import cli

if __name__ == "__main__":
    sys.exit(cli("prompt_injection"))
