"""Case persistence for the orchestrator.

Two replicas run behind the internal ingress, so the replica that accepts `POST /cases` is often
not the one that serves the stream. Cosmos is the handoff point: the POST writes a `pending` case,
whichever replica picks up the stream runs it, and `GET /cases/{id}` reads the document rather than
any replica's memory.

This is a plain last-write-wins document, not a checkpoint. Phase 07 adds `CosmosCheckpointStorage`
and the suspend/resume that a real approval gate needs; this only has to survive a request landing
somewhere else.
"""

from __future__ import annotations

import logging
from datetime import UTC, datetime
from typing import Any

from atlas_common.repositories import CosmosStore

from .models import IncidentCase, IncidentReport

log = logging.getLogger("atlas.orchestrator")

CASES = "cases"


def _now() -> str:
    return datetime.now(UTC).isoformat()


class CaseRepository:
    """Cases in Cosmos. Every write is best-effort: losing the record must not lose the run."""

    def __init__(self, store: CosmosStore) -> None:
        self._store = store

    async def create_pending(
        self, case_id: str, report: str, mode: str, *, traceparent: str | None = None,
        raised_by: dict[str, str] | None = None,
    ) -> dict[str, Any]:
        doc: dict[str, Any] = {
            "id": case_id, "case_id": case_id, "kind": "workflow-case", "status": "pending",
            "report": report, "mode": mode, "traceparent": traceparent,
            "raised_by": raised_by or {}, "created_at": _now(), "updated_at": _now(),
        }
        await self._write(doc)
        return doc

    async def save(self, case: IncidentCase, report: IncidentReport | None = None) -> None:
        doc: dict[str, Any] = {
            "id": case.case_id, "case_id": case.case_id, "kind": "workflow-case",
            "status": case.status, "report": case.report, "mode": case.mode,
            "traceparent": case.traceparent, "raised_by": case.raised_by,
            "state": case.model_dump(mode="json"),
            "created_at": case.created_at.isoformat(), "updated_at": _now(),
        }
        if report is not None:
            doc["result"] = report.model_dump(mode="json", exclude={"case"})
        await self._write(doc)

    async def get(self, case_id: str) -> dict[str, Any] | None:
        try:
            rows = await self._store.query(
                CASES, "SELECT * FROM c WHERE c.id = @id AND c.kind = 'workflow-case'",
                [{"name": "@id", "value": case_id}], partition_key=case_id,
            )
        except Exception:
            log.exception("case read failed for %s", case_id)
            return None
        if not rows:
            return None
        return {k: v for k, v in rows[0].items() if not k.startswith("_")}

    async def _write(self, doc: dict[str, Any]) -> None:
        try:
            await self._store.upsert(CASES, doc)
        except Exception:
            # A demo that dies because a case could not be written is worse than one that forgets.
            log.exception("case write failed for %s", doc.get("id"))


__all__ = ["CASES", "CaseRepository"]
