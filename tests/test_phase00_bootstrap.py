"""Phase 00 acceptance: the workspace imports and the pinned API contract holds.

These are deliberately cheap. They exist so that a dependency bump which moves a symbol
fails in CI on the next commit rather than three phases later.
"""

from __future__ import annotations

import importlib
import sys

import pytest

WORKSPACE_PACKAGES = [
    "atlas_common",
    "atlas_agent",
    "atlas_orchestrator",
    "atlas_web",
    "atlas_worker",
    "atlas_hosted_agent",
]


def test_python_is_313() -> None:
    """Hosted agents offer no python_3_12 runtime. See docs/API-NOTES.md section 4.2."""
    assert sys.version_info[:2] == (3, 13)


@pytest.mark.parametrize("pkg", WORKSPACE_PACKAGES)
def test_workspace_package_imports(pkg: str) -> None:
    mod = importlib.import_module(pkg)
    assert mod.__version__ == "0.1.0"


def test_agent_symbol_contract_holds() -> None:
    """The probe is the machine-checkable half of the CLAUDE.md prime directive."""
    sys.path.insert(0, "scripts")
    try:
        probe_symbols = importlib.import_module("probe_symbols")
    finally:
        sys.path.pop(0)

    result = probe_symbols.probe()
    assert result["failures"] == [], "\n".join(result["failures"])


def test_agent_not_chatagent() -> None:
    """`ChatAgent` is the stale surface. If this fails, re-read the upgrade guide."""
    import agent_framework as af

    assert hasattr(af, "Agent")
    assert not hasattr(af, "ChatAgent")
