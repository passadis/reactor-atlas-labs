"""A deliberately bad tool description. NOT wired into the agent.

Phase 13's regression kit (`scripts/inject_bad_tool_description.py`) swaps this text in for
`compute_blast_radius`'s real description, live, in the APIM MCP tool metadata - no redeploy - so
Episode 4 can show the evaluation catching a regression caused by nothing but wording.

What makes it bad, so the effect can be explained on stage:
- it never says what the tool returns;
- it says the tool is optional and suggests estimating instead, which invites the model to skip it;
- it points the model at the wrong tool for the job;
- it is vague about when to use it.

Keep this module out of the import graph: nothing in `atlas_agent` may import it.
"""

BAD_DESCRIPTIONS: dict[str, str] = {
    "compute_blast_radius": (
        "Optional helper for dependency stuff. Usually you can estimate impact from the report "
        "or from get_telemetry instead, which is faster. Only use this if you really have to."
    ),
}
