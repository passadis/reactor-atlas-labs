"""ATLAS domain models.

The single source of truth. Generators, repositories, agent tools, the ops API and the evaluation
harness all import from here, so a schema change is one edit and a type error everywhere it matters.
"""

from .catalogue import SLO, BlastRadius, Service
from .incidents import Change, Incident, TelemetryWindow
from .knowledge import KnowledgeDocument, KnowledgeHit, KnowledgeResults, RunbookFrontMatter
from .people import Entitlement, Person
from .types import (
    Category,
    ChangeStatus,
    DocType,
    Environment,
    PersonRole,
    Risk,
    Severity,
    Tier,
)

__all__ = [
    "SLO",
    "BlastRadius",
    "Category",
    "Change",
    "ChangeStatus",
    "DocType",
    "Entitlement",
    "Environment",
    "Incident",
    "KnowledgeDocument",
    "KnowledgeHit",
    "KnowledgeResults",
    "Person",
    "PersonRole",
    "Risk",
    "RunbookFrontMatter",
    "Service",
    "Severity",
    "TelemetryWindow",
    "Tier",
]
