# OpenAI Agents SDK Guardrail Verification Tests

Deterministic test scripts backing the numbers posted in [openai/openai-agents-python#5330](https://github.com/openai/openai-agents-python/issues/5330) (tool-input guardrails: context sharing, hard-stop/soft-reject, and `max_function_tool_concurrency=1` serial execution).

**Scope**: adapter-level verification only. No application engine code is included.

## Test 1 — `test_oai_context_guardrail.py` (real LLM in the loop)

Verifies the 0.23.1 patterns recommended by the SDK maintainer in #5330, including the finer points from the maintainer's reply:

- Shared state via `Runner.run(..., context=state)`, accessed as `data.context.context` inside the guardrail
- `ToolGuardrailFunctionOutput.raise_exception()` → run stops with `ToolInputGuardrailTripwireTriggered`
- Soft-reject policy: `reject_content()` → run continues, model reports which calls succeeded/blocked
- Halt-flag pattern: once the halt flag is set, later calls (even in-plan ones) are rejected via the halt-flag check while the model continues and wraps up (`halt_flag_active` rule in the veto log)
- Fresh object per run: a second run with a fresh state object runs clean after a first run halted; reusing the halted state object blocks even the first call (control case)
- `RunConfig(tool_execution=ToolExecutionConfig(max_function_tool_concurrency=1))` serial execution

Result artifact: `results/oai_0231_official_pattern_result.json` — 12 checks across 4 scenarios, all pass.

```bash
export DEEPSEEK_API_KEY=sk-...   # any OpenAI-compatible endpoint works
python test_oai_context_guardrail.py
```

## Test 2 — `test_oai_concurrency_stress.py` (no LLM needed, fully deterministic)

Injects 6 tool calls (4 in-plan, 2 out-of-plan) directly into the same batch executor `Runner.run` uses internally (`_FunctionToolBatchExecutor`), n=30 per configuration:

| Configuration | Order kept | Timeline overlap | Out-of-plan vetoed | Calls started at tripwire |
|---|---|---|---|---|
| `max_function_tool_concurrency=1` (maintainer-recommended) | 30/30 | **0/30** | 2/2 | 3/6 — remaining 3 never started |
| default concurrent (control) | 30/30 | **30/30** | 2/2 | 6/6 — all already started |

**Worst-case group — 5 high-risk calls, all out-of-plan** (`send_money`, `delete_file`, `purge_logs`, `wire_transfer`, `drop_table`): 5/5 vetoed, zero side-effect leakage, in both serial and concurrent settings (n=30 each — 60 runs). No high-risk function body ever executed; interception happens entirely at the guardrail layer.

Side-effect leak: 0 in all 180 runs. Shared-state counts consistent in all runs.

```bash
python test_oai_concurrency_stress.py   # writes oai_concurrency_stress_result.json
```

Result artifact: `results/oai_concurrency_stress_result.json`.

## Boundary

Tool **input** guardrails cover local function tools only — they do not intercept hosted tools or handoffs (consistent with the maintainer's statement in #5330). Output guardrails have not yet been evaluated.
