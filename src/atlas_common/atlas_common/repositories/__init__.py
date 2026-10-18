"""Typed async repositories over Cosmos DB and Azure AI Search.

Written once here; called by the ops API (Phase 08) and the agent tools (Phase 03). Every call
has a timeout and raises only `RepositoryUnavailableError` on infrastructure failure. Not-found is
`None` or an empty list, never an exception.
"""

from .base import CosmosStore, RepositoryUnavailableError, cosmos_store, to_model, with_timeout
from .cosmos_repos import (
    ChangeRepository,
    EntitlementRepository,
    IncidentRepository,
    ServiceRepository,
    TelemetryRepository,
)
from .knowledge_search import KnowledgeSearch, build_filter

__all__ = [
    "ChangeRepository",
    "CosmosStore",
    "EntitlementRepository",
    "IncidentRepository",
    "KnowledgeSearch",
    "RepositoryUnavailableError",
    "ServiceRepository",
    "TelemetryRepository",
    "build_filter",
    "cosmos_store",
    "to_model",
    "with_timeout",
]
