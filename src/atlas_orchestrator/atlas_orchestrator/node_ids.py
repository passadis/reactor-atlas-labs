"""Every executor id in one place, because a checkpoint is bound to the graph that wrote it.

This is not naming hygiene. The framework hashes the topology into
`Workflow.graph_signature_hash` and refuses a restore whose graph does not match:

    WorkflowCheckpointException: Workflow graph has changed since the checkpoint was
    created. Please rebuild the original workflow before resuming.

Verified by probe on 2026-09-21 (docs/API-NOTES.md 14.4). So a node id derived from a case id, a
request id or a uuid would produce a workflow that can never resume anything - the failure would
appear only in front of an audience, at the one moment the demo depends on it.

Rules:

- Ids are fixed strings, declared here, and never computed.
- Renaming one invalidates every checkpoint written before the rename. That is a migration, not a
  refactor; `tests/test_checkpoints.py::test_the_graph_signature_is_stable_across_builds` pins it.
"""

from __future__ import annotations

from .agents import IMPACT, KNOWLEDGE, PLANNER, TRIAGE

INTAKE = "intake"
AGGREGATE = "aggregate"
POLICY_GATE = "policy_gate"
APPROVAL = "approval"
DISPATCH = "dispatch"
REPORT = "report"

# The agent node ids are the specialist names themselves, so the decision log, the graph, the
# panel and the traces all say "triage_agent" and mean the same node.
AGENT_NODES = (TRIAGE, KNOWLEDGE, IMPACT, PLANNER)
DETERMINISTIC_NODES = (INTAKE, AGGREGATE, POLICY_GATE, APPROVAL, DISPATCH, REPORT)
ALL_NODES = (*DETERMINISTIC_NODES, *AGENT_NODES)

WORKFLOW_GRAPH = "atlas-incident-graph"
WORKFLOW_SEQUENTIAL = "atlas-incident-sequential"
WORKFLOW_HANDOFF = "atlas-incident-handoff"
# The gate, the approval and the report on their own. The handoff shape has no gated
# graph of its own - the mesh is conversational - so its typed case is fed through this.
WORKFLOW_APPROVAL_TAIL = "atlas-incident-approval"

__all__ = [
    "AGENT_NODES",
    "AGGREGATE",
    "ALL_NODES",
    "APPROVAL",
    "DETERMINISTIC_NODES",
    "DISPATCH",
    "INTAKE",
    "POLICY_GATE",
    "REPORT",
    "WORKFLOW_APPROVAL_TAIL",
    "WORKFLOW_GRAPH",
    "WORKFLOW_HANDOFF",
    "WORKFLOW_SEQUENTIAL",
]
