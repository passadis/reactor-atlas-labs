"""The second approval mechanism: the framework pausing a single tool call.

ATLAS has two, and Episode 2 shows both because they answer different questions.

| | Workflow request-info (`approval.py`) | Tool approval (here) |
|---|---|---|
| What pauses | the whole **case** | one **agent turn** |
| Where the pause lives | a Cosmos checkpoint | the agent's session |
| Survives a process restart | yes, that is the point | only as far as the session store does |
| Granularity | the plan, as a whole | one call, with its exact arguments |
| Who is asked | a role named by the policy gate | whoever is holding the conversation |
| Fits when | a human signs off before anything runs | an agent reaches for something dangerous |

The workflow gate is the one the demo leans on, because an incident approval may sit for half an
hour and must survive a deploy. Tool approval is the right shape for the other case: the agent
decided, on its own, to call something destructive, and you want to see the **arguments** before it
goes ahead - which a plan-level approval cannot show you.

`execute_remediation` here is a placeholder that records intent and executes nothing. Phase 10
replaces its body with a Service Bus dispatch keyed on `plan_id`; the approval wrapper around it
does not change.
"""

# No `from __future__ import annotations`: see approval.py for why the framework's decorators
# cannot read string annotations.

import logging
from typing import Any

from agent_framework import Agent, AgentSession, Content, Message, tool
from atlas_common.models import Entitlement
from pydantic import BaseModel, ConfigDict

from .models import IncidentCase, PolicyDecision
from .policy import approval_is_valid

log = logging.getLogger("atlas.orchestrator")

EXECUTOR_INSTRUCTIONS = """\
You carry out remediation steps that a human has already approved, one at a time, in order.

Call `execute_remediation` once per step, with the plan id and the step number. Never invent a
step number, never skip a step, and never call the tool for a plan you were not given.

You cannot approve anything yourself. The tool will pause and ask a human before it runs, every
time, and that is expected - report what it tells you.
"""


class DispatchRecord(BaseModel):
    """What `execute_remediation` was asked to do. Phase 10 turns this into a queued command."""

    model_config = ConfigDict(extra="forbid")

    plan_id: str
    step: int
    approved_by: str = ""
    dispatched: bool = False
    detail: str = ""


class RemediationDispatcher:
    """Holds the tool, the agent, and the record of what was asked for.

    Nothing is executed in Phase 07. `dispatched` records that the approval reached the tool,
    which is what the demo is about; Phase 10 makes the call real.
    """

    def __init__(self, case: IncidentCase) -> None:
        self.case = case
        self.records: list[DispatchRecord] = []

    def build_tool(self) -> Any:
        @tool(
            name="execute_remediation",
            description=(
                "Execute one approved remediation step against a service. Destructive: this "
                "changes production. Takes the plan id and the step number."
            ),
            # The framework refuses to run this without a human saying yes to THIS call, with
            # THESE arguments. Declared on the tool, so no caller can forget to check.
            approval_mode="always_require",
        )
        async def execute_remediation(plan_id: str, step: int) -> str:
            plan = self.case.plan
            if plan is None or plan.plan_id != plan_id:
                return (f"refused: this case holds plan "
                        f"{plan.plan_id if plan else 'none'}, not {plan_id}")
            if not any(s.order == step for s in plan.steps):
                return f"refused: plan {plan_id} has no step {step}"
            record = DispatchRecord(plan_id=plan_id, step=step, dispatched=True,
                                    detail="recorded; Phase 10 dispatches it to Service Bus")
            self.records.append(record)
            log.info("execute_remediation approved and recorded: %s step %s", plan_id, step)
            return f"step {step} of plan {plan_id} accepted for dispatch"

        return execute_remediation

    def build_agent(self, client: Any) -> Agent:
        return Agent(client=client, name="remediation_executor",
                     description="Carries out approved remediation steps, one call at a time.",
                     instructions=EXECUTOR_INSTRUCTIONS, tools=[self.build_tool()])


class PendingToolCall(BaseModel):
    """One tool call the framework is holding, with the arguments a human gets to see."""

    model_config = ConfigDict(extra="forbid")

    approval_id: str
    tool_name: str
    arguments: str = ""
    case_id: str = ""


def pending_tool_calls(response: Any, case_id: str = "") -> list[PendingToolCall]:
    """Extract the approval requests from an agent response. Empty means nothing was asked."""
    pending: list[PendingToolCall] = []
    for message in getattr(response, "messages", []) or []:
        for content in message.contents:
            if content.type != "function_approval_request":
                continue
            call = getattr(content, "function_call", None)
            pending.append(PendingToolCall(
                approval_id=str(content.id),
                tool_name=str(getattr(call, "name", "") or ""),
                arguments=str(getattr(call, "arguments", "") or ""),
                case_id=case_id,
            ))
    return pending


def approval_message(response: Any, *, approved: bool) -> Message:
    """Build the reply that releases (or refuses) every held call in a response.

    The response content carries the original `function_call` back, so the framework matches the
    answer to the exact call it paused - not merely to the tool by name.
    """
    contents: list[Content] = []
    for message in getattr(response, "messages", []) or []:
        for content in message.contents:
            if content.type != "function_approval_request":
                continue
            call: Content | None = getattr(content, "function_call", None)
            if call is None:
                continue
            contents.append(Content.from_function_approval_response(
                approved=approved, id=content.id, function_call=call))
    return Message(role="user", contents=contents)


class ToolApprovalGate:
    """Runs the executor agent and applies the same policy rules to each held tool call.

    The framework guarantees a human is asked. It does not know *which* human is allowed to say
    yes - that is still `policy.approval_is_valid`, exactly as in the workflow gate. The two
    mechanisms differ in where they pause, not in who is permitted to release them.
    """

    def __init__(self, case: IncidentCase, entitlements: dict[str, Entitlement]) -> None:
        self.case = case
        self.entitlements = entitlements
        self.dispatcher = RemediationDispatcher(case)

    async def start(self, client: Any, instruction: str,
                    session: AgentSession | None = None) -> tuple[Any, AgentSession, list[
                        PendingToolCall]]:
        agent = self.dispatcher.build_agent(client)
        live = session or agent.create_session()
        response = await agent.run(instruction, session=live)
        self._agent = agent
        return response, live, pending_tool_calls(response, self.case.case_id)

    async def release(self, response: Any, session: AgentSession, *,
                      approver_role: str, approved: bool = True) -> tuple[Any, str]:
        """Answer the held call, after checking the approver is allowed to."""
        decision = self.case.policy or PolicyDecision()
        ok, reason, _code = approval_is_valid(
            decision, approver_role=approver_role,
            approver_entitlement=self.entitlements.get(approver_role),
            current_plan_id=self.case.plan.plan_id if self.case.plan else "",
        )
        if not ok:
            # Refuse the call rather than the request: the agent is told no, and carries on.
            refusal = await self._agent.run(
                approval_message(response, approved=False), session=session)
            return refusal, f"refused: {reason}"
        result = await self._agent.run(
            approval_message(response, approved=approved), session=session)
        return result, ("released" if approved else "rejected by the approver")


__all__ = [
    "EXECUTOR_INSTRUCTIONS",
    "DispatchRecord",
    "PendingToolCall",
    "RemediationDispatcher",
    "ToolApprovalGate",
    "approval_message",
    "pending_tool_calls",
]
