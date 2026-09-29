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

    # -- approvals ------------------------------------------------------------------------
    #
    # A second document per case, in the same container, with `kind: "approval"`. The checkpoint
    # is the source of truth for *resuming*; this row is the source of truth for *who is being
    # asked* - the approval console reads it, and Phase 10's Logic App will read the same row.

    async def save_approval(self, case: IncidentCase, *, request_id: str) -> None:
        decision = case.policy
        plan = case.plan
        doc: dict[str, Any] = {
            "id": f"approval:{case.case_id}", "case_id": case.case_id, "kind": "approval",
            "status": "pending", "request_id": request_id,
            "plan_id": plan.plan_id if plan else "",
            "risk": plan.risk if plan else "high",
            "blast_radius": case.impact.blast_radius if case.impact else 0,
            "reasons": list(decision.reasons) if decision else [],
            "required_roles": list(decision.required_approver_roles) if decision else [],
            "expires_at": decision.expires_at.isoformat()
            if decision and decision.expires_at else None,
            "mode": case.mode, "report": case.report,
            "created_at": _now(), "updated_at": _now(),
        }
        await self._write(doc)

    async def record_refusal(self, case_id: str, attempt: dict[str, Any]) -> None:
        """Audit an approval that was refused, WITHOUT marking the case answered.

        A refusal is not an answer. Writing it as one would leave the row in `answered`, and the
        startup sweep would later pick it up and try to resume the case with an approval that was
        rejected for being out of role or out of date. The case stays `pending` and askable.
        """
        doc = await self.get_approval(case_id)
        if doc is None:
            return
        attempts = list(doc.get("refused_attempts") or [])
        attempts.append({**attempt, "at": _now()})
        doc.update({"refused_attempts": attempts[-20:], "updated_at": _now()})
        await self._write(doc)

    async def record_answer(self, case_id: str, answer: dict[str, Any]) -> None:
        """Store a human's answer even if no replica is running the case.

        This is what makes approving during the kill window safe: the answer lands here, and the
        startup sweep picks it up when the orchestrator comes back.
        """
        doc = await self.get_approval(case_id)
        if doc is None:
            log.warning("answer recorded for %s with no pending approval row", case_id)
            doc = {"id": f"approval:{case_id}", "case_id": case_id, "kind": "approval",
                   "created_at": _now()}
        doc.update({"status": "answered", "answer": answer, "updated_at": _now()})
        await self._write(doc)

    async def close_approval(self, case: IncidentCase) -> None:
        """Mark the approval row resolved so the sweep does not pick it up again."""
        doc = await self.get_approval(case.case_id)
        if doc is None:
            return
        doc.update({"status": "resolved", "case_status": case.status, "updated_at": _now()})
        await self._write(doc)

    async def get_approval(self, case_id: str) -> dict[str, Any] | None:
        try:
            rows = await self._store.query(
                CASES, "SELECT * FROM c WHERE c.id = @id AND c.kind = 'approval'",
                [{"name": "@id", "value": f"approval:{case_id}"}], partition_key=case_id,
            )
        except Exception:
            log.exception("approval read failed for %s", case_id)
            return None
        return {k: v for k, v in rows[0].items() if not k.startswith("_")} if rows else None

    async def list_pending_approvals(self, limit: int = 50) -> list[dict[str, Any]]:
        try:
            rows = await self._store.query(
                CASES,
                "SELECT TOP @n * FROM c WHERE c.kind = 'approval' AND c.status = 'pending' "
                "ORDER BY c.created_at DESC",
                [{"name": "@n", "value": limit}],
            )
        except Exception:
            log.exception("pending approval list failed")
            return []
        return [{k: v for k, v in row.items() if not k.startswith("_")} for row in rows]

    async def answered_while_away(self, limit: int = 20) -> list[dict[str, Any]]:
        """Approvals that were answered but never resumed. The startup sweep's input."""
        try:
            rows = await self._store.query(
                CASES,
                "SELECT TOP @n * FROM c WHERE c.kind = 'approval' AND c.status = 'answered' "
                "ORDER BY c.updated_at",
                [{"name": "@n", "value": limit}],
            )
        except Exception:
            log.exception("sweep query failed")
            return []
        return [{k: v for k, v in row.items() if not k.startswith("_")} for row in rows]

    async def _write(self, doc: dict[str, Any]) -> None:
        try:
            await self._store.upsert(CASES, doc)
        except Exception:
            # A demo that dies because a case could not be written is worse than one that forgets.
            log.exception("case write failed for %s", doc.get("id"))


__all__ = ["CASES", "CaseRepository"]
