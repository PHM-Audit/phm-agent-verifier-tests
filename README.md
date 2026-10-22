# Agent SDK Guardrail & Ledger Verification Tests

Deterministic test scripts backing the numbers posted in [openai/openai-agents-python#5330](https://github.com/openai/openai-agents-python/issues/5330) (tool-input guardrails: context sharing, hard-stop/soft-reject, and `max_function_tool_concurrency=1` serial execution).

**Scope**: adapter-level verification only. No application engine code is included.

Also hosts the deterministic re-run backing our 2026-10-22 comment in [google/adk-python#6099](https://github.com/google/adk-python/issues/6099) (concurrent-dispatch ledger fork, Test 3 below).

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

---

**PHM evidence chain** — aggregate ledger: [phm-evidence-ledger](https://github.com/PHM-Audit/phm-evidence-ledger) (5,000,086,001 units, four layers, open metrics + SHA-256 manifest) · method parent: [agent-action-verifier](https://github.com/PHM-Audit/agent-action-verifier) (AgentDojo-validated) · [PHM-Audit profile](https://github.com/PHM-Audit)


## Test 3 — `test_adk_fork_repro.py` (google-adk 2.12.0, no LLM needed)

Independent re-run (2026-10-22) of the concurrent-dispatch ledger fixture described in google/adk-python#6099: rebuilt from the third-party comment's description alone (script not seen), Linux x86, Python 3.12.13, single run per row. All four rows match the macOS results posted in-thread:

| locked | veto A | order | ledger | fork |
|---|---|---|---|---|
| False | no | DECISION A \| DECISION B \| SIDE-EFFECT A \| SIDE-EFFECT B | (A, GENESIS) (B, GENESIS) | True |
| False | yes | DECISION A \| DECISION B \| SIDE-EFFECT B | (A, GENESIS) (B, GENESIS) | True |
| True | no | DECISION A \| DECISION B \| SIDE-EFFECT A \| SIDE-EFFECT B | (A, GENESIS) (B, A-digest) | False |
| True | yes | DECISION A \| DECISION B \| SIDE-EFFECT B | (A, GENESIS) (B, A-digest) | False |

(fork = B's entry parented at GENESIS.)

What it shows: two calls dispatched from one model response run their before-tool callbacks concurrently — in 2.12.0 source, `_execute_prepared_function_calls` creates one task per prepared call and gathers (`flows/llm_flows/tools/_batch_executor.py`), and no serial dispatch path exists for multi-call responses. A naive read-head/await/write-head ledger forks with no error anywhere; an `asyncio.Lock` around the critical section restores a single chain (same code, lock on/off, only the fork column changes); a veto on the first call does not touch the second — refusal is per-call, not per-turn.

```bash
pip install google-adk==2.12.0
python test_adk_fork_repro.py
```

Result artifact: `results/adk_fork_repro_results.json` (+ `.sha256` sidecar).
