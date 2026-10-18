"""Shared literal types.

These are Literal rather than Enum on purpose: they serialise to plain strings in Cosmos and in
agent structured outputs with no custom encoder, and they render readably in a JSON payload on a
projector.
"""

from typing import Literal

Severity = Literal["Sev1", "Sev2", "Sev3", "Sev4"]
Category = Literal[
    "latency", "availability", "data", "security", "capacity", "deployment", "config"
]
Tier = Literal[1, 2, 3, 4]
Environment = Literal["prod", "staging"]
Risk = Literal["low", "medium", "high"]
ChangeStatus = Literal["succeeded", "rolled_back", "partial"]
PersonRole = Literal["engineer", "sre", "manager", "director", "readonly"]
DocType = Literal["runbook", "incident", "postmortem"]
