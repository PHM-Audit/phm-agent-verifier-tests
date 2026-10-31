# langgraph wrap_tool_call PoC - deterministic physics guard

Validator hook on ToolNode (langgraph 1.2.19), per the pointer in langgraph-ai/langgraph#9428.

- CASE1 "speed 500000 km/s" -> PHM_VETO speed_exceeds_light (tool never executes)
- CASE2 "cruise 900 km/h..." -> executes normally
- 3 consecutive runs: byte-identical verdicts (see repeat3_consistency.txt)

Validator wheel: github.com/PHM-Audit/phm-lite-check (deterministic, zero network, no LLM in judging path)
