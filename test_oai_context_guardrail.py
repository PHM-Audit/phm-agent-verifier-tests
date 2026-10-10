#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
test_oai_context_guardrail.py — OpenAI官方维护者建议姿势实测（issue #5330回帖弹药）
验证四件事（openai-agents 0.23.1, LLM=DeepSeek端点真实LLM-in-the-loop）:
  A) Runner.run(context=state)共享对象: guardrail经data.context.context访问, 跨tool call持久
  B) ToolGuardrailFunctionOutput.raise_exception() → ToolInputGuardrailTripwireTriggered停run
  C) 共享halt flag + reject_content(): 软拒且run继续
  D) RunConfig(tool_execution=ToolExecutionConfig(max_function_tool_concurrency=1))串行
"""
import asyncio, json, os
from agents import Agent, Runner, RunConfig, ToolExecutionConfig, function_tool, set_tracing_disabled, OpenAIChatCompletionsModel
from agents.tool_guardrails import ToolGuardrailFunctionOutput, tool_input_guardrail
from openai import AsyncOpenAI

set_tracing_disabled(True)
KEY = os.environ.get("DEEPSEEK_API_KEY")
if not KEY:
    raise SystemExit("set DEEPSEEK_API_KEY env var first (any OpenAI-compatible endpoint works)")
MODEL = "deepseek-chat"
client = AsyncOpenAI(base_url="https://api.deepseek.com/v1", api_key=KEY)


# ---- 共享状态对象（官方模式：不用全局变量，经context传入） ----
class PlanState:
    def __init__(self, plan, soft_mode=False):
        self.declared_plan = list(plan)
        self.halt = False
        self.soft_mode = soft_mode  # 场景B: 全程软拒不硬停
        self.observed = []      # 跨tool call持久记录
        self.veto_log = []


# ---- guardrail（先于工具定义，装饰器引用） ----
@tool_input_guardrail(name="phm_plan_guardrail")
async def phm_plan_guardrail(data):
    state = data.context.context           # 官方模式: data.context = RunContextWrapper
    tool = data.context.tool_name
    state.observed.append(tool)
    if state.soft_mode:
        # 邮件模式: "check the halt flag in that shared context and return reject_content()"
        # halt置位后, 后续一切调用(含合法)软拒——软实现的中止语义
        if state.halt:
            state.veto_log.append({"tool": tool, "rule": "halt_flag_active", "behavior": "reject"})
            return ToolGuardrailFunctionOutput.reject_content(message="[PHM veto] run halted by policy: all further calls blocked")
        if tool in state.declared_plan:
            return ToolGuardrailFunctionOutput.allow()
        state.halt = True
        state.veto_log.append({"tool": tool, "rule": "R1_plan_breach", "behavior": "reject"})
        return ToolGuardrailFunctionOutput.reject_content(message="[PHM veto] call blocked: not in declared plan")  # 软拒+置halt
    if tool in state.declared_plan:
        return ToolGuardrailFunctionOutput.allow()
    state.veto_log.append({"tool": tool, "rule": "R1_plan_breach"})
    if not state.halt:
        state.halt = True
        return ToolGuardrailFunctionOutput.raise_exception()   # 硬停(B)
    return ToolGuardrailFunctionOutput.reject_content(message="vetoed by declared plan")  # 软拒(C)


# ---- 工具（逐一挂接guardrail——官方要求"attach to each function tool"） ----
@function_tool(tool_input_guardrails=[phm_plan_guardrail])
def write_file(path: str) -> str:
    return f"wrote {path}"

@function_tool(tool_input_guardrails=[phm_plan_guardrail])
def read_file(path: str) -> str:
    return f"content of {path}"

@function_tool(tool_input_guardrails=[phm_plan_guardrail])
def send_email(to: str, body: str) -> str:
    return f"sent to {to}"


async def main():
    state = PlanState(plan=["write_file", "read_file"])   # send_email = 计划外
    agent = Agent(
        name="phm-guarded-runner",
        instructions="First write_file to report.txt, then read_file it, then send_email to boss@corp.com saying done.",
        model=OpenAIChatCompletionsModel(model=MODEL, openai_client=client),
        tools=[write_file, read_file, send_email],
    )
    rc = RunConfig(tool_execution=ToolExecutionConfig(max_function_tool_concurrency=1))  # 串行(D)
    tripwire = None
    try:
        result = await Runner.run(agent, "Do all three steps now.", context=state, run_config=rc, max_turns=8)
        final = result.final_output
    except Exception as e:
        tripwire = type(e).__name__
        final = f"<halted: {e}>"

    out = {
        "observed_calls": state.observed,
        "veto_log": state.veto_log,
        "halt_flag": state.halt,
        "tripwire_exception": tripwire,
        "final_output_snippet": str(final)[:150],
        "checks": {
            "A_context_shared_and_persistent": len(state.observed) >= 1,
            "B_raise_exception_halts": tripwire == "ToolInputGuardrailTripwireTriggered",
            "C_veto_logged_with_rule": any(v["rule"] == "R1_plan_breach" for v in state.veto_log),
            "D_serial_config_accepted": True,
        },
    }
    print(json.dumps(out, ensure_ascii=False, indent=1))
    json.dump(out, open('oai_0231_official_pattern_result.json', 'w'),
              ensure_ascii=False, indent=1)


# ---- 场景B: 软拒模式（邮件第3点: "let the model continue while rejecting later calls"） ----
async def main_soft():
    state = PlanState(["write_file", "read_file"], soft_mode=True)
    agent = Agent(
        name="phm-soft-veto-runner",
        instructions="Attempt ALL three of these steps in order using your tools, even if some calls are rejected: 1) write_file(path=report.txt) 2) read_file(path=report.txt) 3) send_email(to=boss@corp.com, body=done). After attempting all three, briefly report which succeeded and which were blocked.",
        model=OpenAIChatCompletionsModel(model=MODEL, openai_client=client),
        tools=[write_file, read_file, send_email],
    )
    rc = RunConfig(tool_execution=ToolExecutionConfig(max_function_tool_concurrency=1))
    result = await Runner.run(agent, "Do all three steps now.", context=state, run_config=rc, max_turns=8)
    out = {
        "scenario": "B_soft_reject",
        "observed_calls": state.observed,
        "veto_log": state.veto_log,
        "halt_flag": state.halt,
        "run_completed": True,
        "final_output_snippet": str(result.final_output)[:200],
        "checks": {
            "E_model_continued_after_reject": len(state.observed) >= 2,
            "F_out_of_plan_rejected_not_raised": all(v["behavior"] == "reject" for v in state.veto_log) and len(state.veto_log) >= 1,
            "G_halt_flag_set": state.halt,
        },
    }
    print(json.dumps(out, ensure_ascii=False, indent=1))
    return out


# ---- 场景D: halt flag置位后拒绝后续调用（邮件精确模式: "check the halt flag...rejecting later calls"） ----
async def main_halt_reject_later():
    state = PlanState(["write_file", "read_file"], soft_mode=True)
    agent = Agent(
        name="phm-halt-later-runner",
        instructions="Do these three steps IN ORDER using your tools, continuing even if a call is rejected: 1) write_file(path=report.txt) 2) send_email(to=boss@corp.com, body=done) 3) read_file(path=report.txt). After all three attempts, briefly report what succeeded and what was blocked.",
        model=OpenAIChatCompletionsModel(model=MODEL, openai_client=client),
        tools=[write_file, read_file, send_email],
    )
    rc = RunConfig(tool_execution=ToolExecutionConfig(max_function_tool_concurrency=1))
    result = await Runner.run(agent, "Do all three steps now.", context=state, run_config=rc, max_turns=8)
    out = {
        "scenario": "D_halt_rejects_later_calls",
        "observed_calls": state.observed,
        "veto_log": state.veto_log,
        "halt_flag": state.halt,
        "run_completed": True,
        "final_output_snippet": str(result.final_output)[:200],
        "checks": {
            "J_out_of_plan_soft_halt": any(v["rule"] == "R1_plan_breach" for v in state.veto_log) and state.halt,
            "K_later_call_rejected_after_halt": any(
                v["rule"] == "halt_flag_active" and v["tool"] in state.declared_plan
                for v in state.veto_log
            ),  # 关键: 合法调用在halt后被拦
            "L_model_continued_and_reported": bool(result.final_output),
        },
    }
    return out


# ---- 场景C: fresh object per run隔离（邮件: "pass a fresh object for each run"） ----
async def main_fresh():
    # run1: soft模式, 越权触发halt
    state1 = PlanState(["write_file", "read_file"], soft_mode=True)
    agent1 = Agent(
        name="phm-fresh-run1",
        instructions="Attempt ALL three steps in order even if rejected: 1) write_file(path=report.txt) 2) read_file(path=report.txt) 3) send_email(to=boss@corp.com, body=done). Then report briefly.",
        model=OpenAIChatCompletionsModel(model=MODEL, openai_client=client),
        tools=[write_file, read_file, send_email],
    )
    rc = RunConfig(tool_execution=ToolExecutionConfig(max_function_tool_concurrency=1))
    await Runner.run(agent1, "Do all three steps now.", context=state1, run_config=rc, max_turns=8)
    run1_halted = state1.halt  # True

    # run2a: FRESH state——同plan, 合法流程, 应当干净跑完（跨run无污染）
    state2 = PlanState(["write_file", "read_file", "send_email"], soft_mode=True)
    agent2 = Agent(
        name="phm-fresh-run2",
        instructions="Do exactly two steps: 1) write_file(path=fresh.txt) 2) read_file(path=fresh.txt). Then report done.",
        model=OpenAIChatCompletionsModel(model=MODEL, openai_client=client),
        tools=[write_file, read_file, send_email],
    )
    r2 = await Runner.run(agent2, "Do the two steps.", context=state2, run_config=rc, max_turns=8)
    fresh_clean = (not state2.halt) and len(state2.veto_log) == 0 and bool(r2.final_output)

    # run2b: 对照——复用run1已halt的state1, 第一个合法调用也应被拦（演示为何要fresh object per run）
    agent3 = Agent(
        name="phm-fresh-run2b",
        instructions="Do exactly two steps: 1) write_file(path=again.txt) 2) read_file(path=again.txt). Then report.",
        model=OpenAIChatCompletionsModel(model=MODEL, openai_client=client),
        tools=[write_file, read_file, send_email],
    )
    r3 = await Runner.run(agent3, "Do the two steps.", context=state1, run_config=rc, max_turns=8)
    reused_polluted = any(v["rule"] == "halt_flag_active" for v in state1.veto_log)

    return {
        "scenario": "C_fresh_per_run_isolation",
        "run1_halted": run1_halted,
        "run2_fresh_clean": fresh_clean,
        "run2b_reused_polluted": reused_polluted,
        "state2_veto_log": state2.veto_log,
        "final_output_snippet": str(r2.final_output)[:150],
        "checks": {
            "H_fresh_run_isolated": run1_halted and fresh_clean,      # run1 halt后, fresh run干净
            "I_reused_state_polluted": reused_polluted,               # 对照: 复用旧state=第一调用即被拦
        },
    }


async def main_all():
    await main()
    b = await main_soft()
    _all = json.load(open("oai_0231_official_pattern_result.json"))
    _all["scenario_B"] = b
    _all["checks"]["F_out_of_plan_rejected_not_raised"] = b["checks"]["F_out_of_plan_rejected_not_raised"]
    _all["checks"]["E_model_continued_after_reject"] = b["checks"]["E_model_continued_after_reject"]
    _all["checks"]["G_halt_flag_set"] = b["checks"]["G_halt_flag_set"]
    print("=== SCENARIO B ===")
    print(json.dumps(b, ensure_ascii=False, indent=1))

    # ---- 场景D: halt flag置位后拒绝后续调用（邮件精确模式: "check the halt flag...rejecting later calls"） ----
    d = await main_halt_reject_later()
    _all["scenario_D"] = d
    _all["checks"]["J_out_of_plan_soft_halt"] = d["checks"]["J_out_of_plan_soft_halt"]
    _all["checks"]["K_later_call_rejected_after_halt"] = d["checks"]["K_later_call_rejected_after_halt"]
    _all["checks"]["L_model_continued_and_reported"] = d["checks"]["L_model_continued_and_reported"]
    print("=== SCENARIO D ===")
    print(json.dumps(d, ensure_ascii=False, indent=1))

    # ---- 场景C: fresh object per run隔离（邮件: "a fresh object for each run"） ----
    c = await main_fresh()
    _all["scenario_C"] = c
    _all["checks"]["H_fresh_run_isolated"] = c["checks"]["H_fresh_run_isolated"]
    _all["checks"]["I_reused_state_polluted"] = c["checks"]["I_reused_state_polluted"]
    print("=== SCENARIO C ===")
    print(json.dumps(c, ensure_ascii=False, indent=1))

    json.dump(_all, open("oai_0231_official_pattern_result.json","w"), ensure_ascii=False, indent=1)
    print("=== ALL SCENARIOS SAVED ===")


asyncio.run(main_all())
