"""Print the live workflow graph as Mermaid, for the Episode 2 slide that is not a slide.

Builds the workflow the same way the orchestrator does, so what prints is what runs. Needs the
provisioned environment for the clients, but makes no model call.

    make graph MODE=sequential_only
"""

from __future__ import annotations

import argparse
import asyncio
import json
import sys

from atlas_agent.factory import IncidentAgentRuntime
from atlas_common.config import Settings, load_local_env
from atlas_orchestrator.agents import SpecialistFactory
from atlas_orchestrator.graph import MODES, CaseRecorder, build_handoff_workflow, normalise_mode
from atlas_orchestrator.models import IncidentCase
from atlas_orchestrator.runner import CaseRunner


async def main(mode: str, as_json: bool) -> int:
    load_local_env()
    async with IncidentAgentRuntime(Settings.from_env()) as runtime:
        resolved = normalise_mode(mode)
        if resolved == "handoff":
            workflow, _agents = build_handoff_workflow(
                SpecialistFactory(runtime),
                CaseRecorder(IncidentCase(case_id="CASE-TOPOLOGY", report="topology only")),
            )
            from atlas_orchestrator.graph import topology

            payload = topology(workflow, resolved)
        else:
            payload = CaseRunner(runtime).graph(resolved)
    if as_json:
        print(json.dumps(payload, indent=2))
    else:
        print(payload["mermaid"])
    return 0


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--mode", default="graph", choices=list(MODES))
    parser.add_argument("--json", action="store_true", help="print the full topology, not Mermaid")
    args = parser.parse_args()
    sys.exit(asyncio.run(main(args.mode, args.json)))
