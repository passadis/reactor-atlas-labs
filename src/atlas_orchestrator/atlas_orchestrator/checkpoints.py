"""Checkpoint storage, and the types allowed back out of one.

A checkpoint is written at the end of every superstep, so the approval suspension in the middle of
a case is durable without anyone asking for it. Two things about this module are load-bearing:

**Deserialisation is restricted.** `CosmosCheckpointStorage` uses a hybrid JSON plus pickle
encoding, and pickle from an untrusted source executes code. The default allowlist covers
primitives and `agent_framework` types only, so every ATLAS model that can appear in a checkpoint
is registered here explicitly - and nothing else is. Widening this list is a security decision.

**A failed checkpoint does not fail the run.** The framework logs
`Failed to create checkpoint at iteration N ... this does not fail the workflow run` and carries
on. A misconfigured store therefore looks exactly like a working one until the moment you try to
resume in front of an audience. `verify_phase07` counts checkpoints rather than trusting the run,
and `probe()` below exists so startup says plainly whether the store works.
"""

from __future__ import annotations

import logging
import os
from datetime import datetime
from typing import Any

from agent_framework import CheckpointStorage, FileCheckpointStorage, register_checkpoint_type
from atlas_agent.schemas import Citation
from atlas_common.auth import get_async_credential
from atlas_common.config import Settings
from pydantic import TypeAdapter

from .approval import ApprovalRequest, ApprovalResponse
from .executors import IntakeRequest, NodeProgress, Partial
from .models import (
    ApprovalRecord,
    Decision,
    Degraded,
    ImpactResult,
    IncidentCase,
    IncidentReport,
    KnowledgeResult,
    PolicyDecision,
    RemediationPlan,
    RemediationStep,
    RunbookRef,
    TriageResult,
)

log = logging.getLogger("atlas.orchestrator")

# `pydantic_core._pydantic_core.TzInfo` is not importable by name, so it is resolved from a
# validated value. Every `datetime` field with a timezone pickles one of these.
PYDANTIC_TZINFO: type[Any] = type(
    TypeAdapter(datetime).validate_python("2026-01-01T00:00:00Z").tzinfo)

CHECKPOINT_CONTAINER = "workflow-checkpoints"

# Every ATLAS type that can cross a checkpoint boundary, named once.
#
# "Every" means every one, not just the headline state models: the messages that travel on edges
# (`IntakeRequest`, `Partial`), the request and response payloads of the approval suspension, and
# every model *nested* inside those (`Citation` lives inside `KnowledgeResult`). Miss one and the
# checkpoint is still written, but reading it is refused and the checkpoint is silently skipped -
# so the run looks healthy and `list_checkpoints` just comes back short. That is how an eight-node
# run reported two checkpoints, none of them the one holding the pending approval.
CHECKPOINT_TYPES: tuple[type[Any], ...] = (
    # Workflow state
    IncidentCase,
    IncidentReport,
    TriageResult,
    KnowledgeResult,
    ImpactResult,
    RemediationPlan,
    RemediationStep,
    RunbookRef,
    PolicyDecision,
    ApprovalRecord,
    Decision,
    Degraded,
    # Nested inside the models above
    Citation,
    # Messages that travel on edges
    IntakeRequest,
    Partial,
    NodeProgress,
    # The approval suspension's payloads
    ApprovalRequest,
    ApprovalResponse,
    # Pydantic's own tzinfo implementation, which every aware `datetime` on the models above
    # carries. Not an ATLAS type and easy to miss, because the failure is a decode warning on
    # one checkpoint rather than an error anywhere near the model that caused it.
    PYDANTIC_TZINFO,
)


def register_types() -> None:
    """Allow ATLAS models through the restricted checkpoint deserialiser. Idempotent."""
    for model in CHECKPOINT_TYPES:
        register_checkpoint_type(model)


def build_storage(settings: Settings | None = None) -> CheckpointStorage | None:
    """Cosmos in Azure, a local directory for development, or nothing if checkpointing is off.

    `ATLAS_CHECKPOINT_DIR` selects file storage and is for local runs only; in Azure the
    orchestrator's user-assigned identity talks to Cosmos with no key anywhere
    (CLAUDE.md section 1).
    """
    register_types()

    local_dir = os.environ.get("ATLAS_CHECKPOINT_DIR")
    if local_dir:
        log.info("checkpointing to the local directory %s", local_dir)
        return FileCheckpointStorage(storage_path=local_dir)

    if os.environ.get("ATLAS_CHECKPOINTS", "on").lower() in ("0", "off", "false"):
        log.warning("checkpointing is DISABLED; kill/resume will not work")
        return None

    resolved = settings or Settings.from_env()
    from agent_framework_azure_cosmos import CosmosCheckpointStorage

    storage: CheckpointStorage = CosmosCheckpointStorage(
        endpoint=resolved.cosmos_endpoint,
        database_name=resolved.cosmos_database,
        container_name=CHECKPOINT_CONTAINER,
        credential=get_async_credential(),
        # Names, not classes, and the separator is a COLON: "my_app.models:MyState". A dotted
        # path is accepted without complaint and then matches nothing, so the restore fails
        # later with a deserialisation error instead of here.
        allowed_checkpoint_types=[f"{m.__module__}:{m.__qualname__}" for m in CHECKPOINT_TYPES],
    )
    log.info("checkpointing to Cosmos container %s", CHECKPOINT_CONTAINER)
    return storage


async def probe(storage: CheckpointStorage | None, workflow_name: str) -> str:
    """Can we actually read the store? Called at startup so a broken one is loud, not silent."""
    if storage is None:
        return "disabled"
    try:
        existing = await storage.list_checkpoint_ids(workflow_name=workflow_name)
    except Exception as exc:
        log.exception("checkpoint storage is not readable")
        return f"unreadable: {type(exc).__name__}: {exc}"[:200]
    return f"ok, {len(existing)} checkpoint(s) for {workflow_name}"


__all__ = [
    "CHECKPOINT_CONTAINER",
    "CHECKPOINT_TYPES",
    "build_storage",
    "probe",
    "register_types",
]
