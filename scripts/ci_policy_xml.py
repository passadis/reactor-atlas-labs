"""CI: every APIM policy file is well-formed APIM policy XML. No Azure, no ATLAS imports.

    python scripts/ci_policy_xml.py

APIM policies are "raw XML": C# policy expressions - `@(...)` and `@{ ... }` - may contain `"`,
`<` and `&&` that strict XML rejects, which is why they deploy with `format: 'rawxml'`
(infra/modules/apim-gateway.bicep). So each expression is replaced by a placeholder first,
matching brackets and skipping C# string and char literals, and what remains must parse.
"""

from __future__ import annotations

import sys
import xml.etree.ElementTree as ET
from pathlib import Path

OPEN = {"(": ")", "{": "}"}


def _skip_literal(text: str, i: int) -> int:
    """Index just past the C# string or char literal starting at text[i]."""
    quote = text[i]
    verbatim = quote == '"' and i > 0 and text[i - 1] == "@"
    i += 1
    while i < len(text):
        c = text[i]
        if verbatim and c == '"' and i + 1 < len(text) and text[i + 1] == '"':
            i += 2
            continue
        if not verbatim and c == "\\":
            i += 2
            continue
        if c == quote:
            return i + 1
        i += 1
    raise ValueError("unterminated literal in a policy expression")


def neutralise(text: str) -> str:
    """Replace every policy expression with a placeholder token."""
    out, i = [], 0
    while i < len(text):
        if text[i] == "@" and i + 1 < len(text) and text[i + 1] in OPEN:
            opener, closer = text[i + 1], OPEN[text[i + 1]]
            depth, j = 1, i + 2
            while depth and j < len(text):
                c = text[j]
                if c in "\"'":
                    j = _skip_literal(text, j)
                    continue
                if c == opener:
                    depth += 1
                elif c == closer:
                    depth -= 1
                j += 1
            if depth:
                raise ValueError(f"unbalanced policy expression at offset {i}")
            out.append("EXPR")
            i = j
        else:
            out.append(text[i])
            i += 1
    return "".join(out)


def main() -> int:
    bad = []
    files = sorted(Path("apim/policies").rglob("*.xml"))
    for path in files:
        try:
            ET.fromstring(neutralise(path.read_text(encoding="utf-8")))  # noqa: S314
        except (ET.ParseError, ValueError) as exc:
            bad.append(f"{path}: {exc}")
    print("\n".join(bad) or f"{len(files)} policy files are well-formed APIM policy XML")
    return 1 if bad else 0


if __name__ == "__main__":
    sys.exit(main())
