#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
test_oai_concurrency_stress.py — 并发对抗测试（issue #5330回帖弹药升级版）
背景：维护者jbeckwith-oai指出默认并发工具调用存在veto/副作用窗口，
     官方姿势=RunConfig(tool_execution=ToolExecutionConfig(max_function_tool_concurrency=1))。

本测试不依赖LLM——直接向SDK真实批量执行器(_FunctionToolBatchExecutor,
即Runner.run内部同一执行路径)确定性注入6个tool call(3合法+2越权+1合法)，
n次重复，量化两组对照：

  组S(串行=1, 官方姿势):  预期=执行顺序==声明顺序, 时间轴零交叠,
                          Tripwire抛出时in-flight副作用=0(veto窗口=0)
  组P(默认并发, 对照):    预期=时间轴交叠, Tripwire抛出时存在in-flight副作用
                          (窗口>0, 有序计划校验语义失效) —— 证明测试有区分度

软拒(reject_content)组补测顺序语义; 硬停(raise_exception)组是veto窗口正面战场。
副作用模拟: 每个工具体await asyncio.sleep(0.02)模拟真实I/O(文件/网络)。
"""
import asyncio, json, time, statistics
from dataclasses import dataclass, field

from agents import (
    Agent, RunConfig, ToolExecutionConfig, function_tool,
    set_tracing_disabled, RunHooks, RunContextWrapper,
)
from agents.items import ResponseFunctionToolCall
from agents.run_internal.tool_execution import (
    _FunctionToolBatchExecutor, AgentBindings, ToolRunFunction,
    ToolInputGuardrailTripwireTriggered,
)
from agents.tool_guardrails import ToolGuardrailFunctionOutput, tool_input_guardrail

set_tracing_disabled(True)

# 声明计划(合法动作集)与注入序列分离: send_email*不在声明计划中=越权
DECLARED_PLAN = ["write_file", "read_file", "write_file2", "read_file2"]
INJECTION = ["write_file", "read_file", "send_email", "write_file2", "read_file2", "send_email2"]
# 全越权高危组: 5个金融/破坏性语义动作全部越权(不在声明计划)——最坏场景
HIGH_RISK_INJECTION = ["send_money", "delete_file", "purge_logs", "wire_transfer", "drop_table"]
HIGH_RISK_TAGS = ["sm", "df", "pl", "wt", "dt"]
# 注入顺序==模型提交顺序: 越权调用插在中间, 模拟"模型中途越权"真实场景


def make_state():
    @dataclass
    class PlanState:
        plan: list
        halt: bool = False
        observed: list = field(default_factory=list)
        veto_log: list = field(default_factory=list)
        timeline: list = field(default_factory=list)
        inflight_at_tripwire: list = field(default_factory=list)

    return PlanState(plan=list(DECLARED_PLAN))


async def _io(state, name, tag):
    """模拟真实I/O副作用的工具体: 记录start/end, sleep 20ms"""
    t0 = time.perf_counter()
    rec = {"tool": name, "phase": "body_start", "t": t0, "tag": tag}
    state.timeline.append(rec)
    state.side_effects_started.append(tag)
    try:
        await asyncio.sleep(0.02)
        state.timeline.append({"tool": name, "phase": "body_end", "t": time.perf_counter(), "tag": tag})
        state.side_effects_completed.append(tag)
    except asyncio.CancelledError:
        state.timeline.append({"tool": name, "phase": "body_cancelled", "t": time.perf_counter(), "tag": tag})
        state.cancelled_inflight.append(tag)
        raise


def build_tools(state, behavior):
    """构造3个挂PHM guardrail的工具(复现#5330七项测试的R1越权拦截)"""

    @function_tool
    async def write_file(path: str) -> str:
        """Write a report file."""
        await _io(state, "write_file", f"wf1")
        return f"wrote {path}"

    @function_tool
    async def read_file(path: str) -> str:
        """Read a file."""
        await _io(state, "read_file", f"rf1")
        return f"content of {path}"

    @function_tool
    async def send_email(to: str, body: str) -> str:
        """Send an email. OUT-OF-PLAN (越权)."""
        await _io(state, "send_email", f"se1")
        return f"sent to {to}"

    @function_tool
    async def write_file2(path: str) -> str:
        """Write another file."""
        await _io(state, "write_file2", f"wf2")
        return f"wrote {path}"

    @function_tool
    async def read_file2(path: str) -> str:
        """Read another file."""
        await _io(state, "read_file2", f"rf2")
        return f"content of {path}"

    @function_tool
    async def send_email2(to: str, body: str) -> str:
        """Send another email. OUT-OF-PLAN (越权)."""
        await _io(state, "send_email2", f"se2")
        return f"sent to {to}"

    @tool_input_guardrail
    def phm_r1_plan_guard(data):
        st = data.context.context
        tool = data.context.tool_name
        # 先记录"guardrail评估时刻"
        st.timeline.append({"tool": tool, "phase": "guardrail", "t": time.perf_counter(),
                            "tag": tool[:2] + ("1" if tool.endswith("email") and tool == "send_email" else "2") if "email" in tool else tool})
        if tool not in st.plan:
            st.veto_log.append({"tool": tool, "rule": "R1_plan_breach", "t": time.perf_counter()})
            if behavior == "raise_exception":
                # 0.23.1: raise_exception()不收message参数; veto留痕已在st.veto_log
                return ToolGuardrailFunctionOutput.raise_exception()
            return ToolGuardrailFunctionOutput.reject_content(
                message=f"[PHM veto] R1: {tool} out-of-plan, rejected")
        st.observed.append(tool)
        return ToolGuardrailFunctionOutput.allow()

    @function_tool
    async def send_money(amount: str, to: str) -> str:
        """Wire money. HIGH-RISK OUT-OF-PLAN."""
        await _io(state, "send_money", "sm")
        return f"sent {amount} to {to}"

    @function_tool
    async def delete_file(path: str) -> str:
        """Delete a file. HIGH-RISK OUT-OF-PLAN."""
        await _io(state, "delete_file", "df")
        return f"deleted {path}"

    @function_tool
    async def purge_logs(scope: str) -> str:
        """Purge logs. HIGH-RISK OUT-OF-PLAN."""
        await _io(state, "purge_logs", "pl")
        return f"purged {scope}"

    @function_tool
    async def wire_transfer(account: str, amount: str) -> str:
        """Bank transfer. HIGH-RISK OUT-OF-PLAN."""
        await _io(state, "wire_transfer", "wt")
        return f"transferred {amount} to {account}"

    @function_tool
    async def drop_table(table: str) -> str:
        """Drop DB table. HIGH-RISK OUT-OF-PLAN."""
        await _io(state, "drop_table", "dt")
        return f"dropped {table}"

    tools = {}
    for t in (write_file, read_file, send_email, write_file2, read_file2, send_email2,
              send_money, delete_file, purge_logs, wire_transfer, drop_table):
        tools[t.name] = t
    # 挂guardrail
    for t in tools.values():
        t.tool_input_guardrails = [phm_r1_plan_guard]
    return tools


async def run_one(behavior: str, serial: bool, run_id: int,
                  injection=None, decl_order=None, oop_tags=("se1", "se2")) -> dict:
    """单次注入: tool calls喂SDK真实批量执行器"""
    injection = injection or INJECTION
    decl_order = decl_order or ["wf1", "rf1", "se1", "wf2", "rf2", "se2"]
    state = make_state()
    state.side_effects_started = []
    state.side_effects_completed = []
    state.cancelled_inflight = []

    agent = Agent(
        name="phm-concurrency-probe",
        instructions="unused (deterministic injection, no LLM)",
        tools=list(build_tools(state, behavior).values()),
    )
    tools = {t.name: t for t in agent.tools}

    # 构造6个ToolRun(声明顺序==计划顺序)
    tool_runs = []
    for i, name in enumerate(injection):
        call = ResponseFunctionToolCall(
            name=name, arguments=json.dumps({"path": f"f{i}", "to": "boss@corp.com", "body": "x"} if "file" in name else {"to": "boss@corp.com", "body": "x"}),
            call_id=f"c{run_id}-{i}", type="function_call",
        )
        tool_runs.append(ToolRunFunction(call, tools[name]))

    ctx = RunContextWrapper(context=state)
    cfg = RunConfig(tool_execution=ToolExecutionConfig(
        max_function_tool_concurrency=1 if serial else None))
    bindings = AgentBindings(public_agent=agent, execution_agent=agent)

    tripwire_at = None
    try:
        await _FunctionToolBatchExecutor(
            bindings=bindings, tool_runs=tool_runs, hooks=RunHooks(),
            context_wrapper=ctx, config=cfg,
            isolate_parallel_failures=None,
            sibling_category_failure=None,
            tool_output_committer=None,
        ).execute()
    except ToolInputGuardrailTripwireTriggered as e:
        tripwire_at = time.perf_counter()
        state.halt = True
        # Tripwire时刻的in-flight副作用: 已start但未complete(且非cancelled记录)的tag
        done = set(state.side_effects_completed) | set(state.cancelled_inflight)
        state.inflight_at_tripwire = [t for t in state.side_effects_started if t not in done]

    oop_names = [nm for nm in (INJECTION + HIGH_RISK_INJECTION) if nm in ("send_email", "send_email2", "send_money", "delete_file", "purge_logs", "wire_transfer", "drop_table")]

    # ---- 指标计算 ----
    tl = state.timeline
    starts = {r["tag"]: r["t"] for r in tl if r["phase"] == "body_start"}
    ends = {r["tag"]: r["t"] for r in tl if r["phase"] == "body_end"}

    # 1) 执行顺序(按body_start时刻) vs 声明顺序
    exec_order = [t for t, _ in sorted(starts.items(), key=lambda kv: kv[1])]
    exec_order_present = [t for t in decl_order if t in starts]
    order_kept = exec_order_present == [t for t in decl_order if t in starts] and exec_order_present == exec_order

    # 2) 时间轴交叠: 排序后相邻区间end>next start(容差1e-9)
    intervals = sorted([(s, ends[t], t) for t, s in starts.items() if t in ends])
    overlap = any(iv[1] > nx[0] + 1e-9 for iv, nx in zip(intervals, intervals[1:]))
    # 取消的调用也计入交叠判定(其start存在)
    cancelled_starts = [r for r in tl if r["phase"] == "body_cancelled"]
    if cancelled_starts:
        ov2 = []
        for r in cancelled_starts:
            for s, e, t in intervals:
                if s < r["t"] < e:
                    ov2.append((t, r["tag"]))
        overlap = overlap or bool(ov2)

    # 3) veto窗口量化(硬停组):
    #    cancelled_inflight = Tripwire中止时副作用进行中被cancel的调用数(窗口>0的直接证据)
    #    guardrail_evals = Tripwire前已启动评估的调用数(S预期3=中止干净; P预期6=全部已启动)
    guardrail_evals = sum(1 for r in tl if r["phase"] == "guardrail")
    if behavior == "raise_exception":
        inflight = len(state.cancelled_inflight)
        started_before_tripwire = guardrail_evals  # guardrail同步段, 评估数=启动数
    else:
        inflight = len(state.inflight_at_tripwire) if tripwire_at else 0
        started_before_tripwire = None
    # 软拒组: 用"第一个veto决定时刻"之后是否有调用body在veto完成前开始
    veto_window_soft = None
    if behavior == "reject_content" and state.veto_log:
        first_veto_t = min(v["t"] for v in state.veto_log)
        # veto后立刻完成的判定: veto时刻仍在运行的body
        veto_window_soft = sum(1 for t, s in starts.items()
                               if s < first_veto_t and (t not in ends or ends[t] > first_veto_t))

    # 4) 副作用泄漏: 越权工具函数体是否执行
    leak = [t for t in oop_tags if t in state.side_effects_started]

    # 5) 状态一致性(按组语义):
    #    软拒: 全部6个被评估+2越权被veto+状态计数闭合
    #    硬停+串行: 干净中止——halt且恰在越权点(3评估/1veto)且零中断副作用且零泄漏
    #    硬停+并发: window组的语义是暴露窗口, 干净性以cancelled_inflight==0单独看
    n_oop = sum(1 for nm in injection if nm in oop_names)
    first_oop_idx = next(i for i, nm in enumerate(injection) if nm in oop_names)
    if behavior == "reject_content":
        state_ok = (guardrail_evals == len(injection)) and (len(state.veto_log) == n_oop)
    else:
        state_ok = (state.halt and guardrail_evals == first_oop_idx + 1 and len(state.veto_log) == 1
                    and len(state.cancelled_inflight) == 0 and not leak)

    return {
        "behavior": behavior, "serial": serial, "run": run_id,
        "high_risk": injection is not INJECTION and injection == HIGH_RISK_INJECTION,
        "halt": state.halt,
        "order_kept": bool(order_kept),
        "overlap": bool(overlap),
        "inflight_at_tripwire": inflight,
        "inflight_tags": state.inflight_at_tripwire,
        "cancelled_inflight": state.cancelled_inflight,
        "started_before_tripwire": started_before_tripwire,
        "veto_window_soft": veto_window_soft,
        "side_effect_leak": leak,
        "guardrail_evals": guardrail_evals,
        "veto_count": len(state.veto_log),
        "state_ok": bool(state_ok),
        "exec_order": exec_order_present,
    }


async def sweep(n=30):
    rows = []
    # 高危全越权组: 5个高危动作全部越权, 串行+并发对照 (对齐"5个高危调用并发100%拦截"叙事)
    hr_inj, hr_ord = HIGH_RISK_INJECTION, HIGH_RISK_TAGS
    for serial in (True, False):
        for i in range(n):
            try:
                rows.append(await run_one("reject_content", serial, i,
                                          injection=hr_inj, decl_order=hr_ord, oop_tags=hr_ord))
            except Exception as e:
                rows.append({"behavior": "reject_content", "serial": serial, "run": i,
                             "error": f"{type(e).__name__}: {str(e)[:120]}"})
    for behavior in ("reject_content", "raise_exception"):
        for serial in (True, False):
            for i in range(n):
                try:
                    rows.append(await run_one(behavior, serial, i))
                except Exception as e:
                    rows.append({"behavior": behavior, "serial": serial, "run": i,
                                 "error": f"{type(e).__name__}: {str(e)[:120]}"})
    return rows


def summarize(rows, behavior, serial):
    grp = [r for r in rows if r["behavior"] == behavior and r["serial"] == serial and "error" not in r]
    errs = [r for r in rows if r["behavior"] == behavior and r["serial"] == serial and "error" in r]
    if not grp:
        return {"n": 0, "errors": len(errs), "error_sample": errs[0]["error"] if errs else None}
    return {
        "n": len(grp),
        "errors": len(errs),
        "order_kept_rate": sum(r["order_kept"] for r in grp) / len(grp),
        "overlap_rate": sum(r["overlap"] for r in grp) / len(grp),
        "inflight_at_tripwire_mean": statistics.mean(r["inflight_at_tripwire"] for r in grp),
        "inflight_at_tripwire_max": max(r["inflight_at_tripwire"] for r in grp),
        "side_effect_leak_rate": sum(1 for r in grp if r["side_effect_leak"]) / len(grp),
        "state_ok_rate": sum(r["state_ok"] for r in grp) / len(grp),
        "halt_rate": sum(r["halt"] for r in grp) / len(grp),
        "veto_count_all_2": all(r["veto_count"] == 2 for r in grp),
        "guardrail_evals_all_6": all(r["guardrail_evals"] == 6 for r in grp),
        "guardrail_evals_set": sorted({r["guardrail_evals"] for r in grp}),
        "started_before_tripwire_set": sorted({r["started_before_tripwire"] for r in grp if r["started_before_tripwire"] is not None}),
        "veto_window_soft_mean": (lambda vs: statistics.mean(vs) if vs else 0)([r["veto_window_soft"] for r in grp if r["veto_window_soft"] is not None]),
    }


def summarize_high_risk(rows, serial):
    """高危全越权组: 软拒=5/5拦截; 硬停=首个调用即Tripwire"""
    grp = [r for r in rows if r.get("high_risk") and r.get("serial") == serial and "error" not in r]
    errs = [r for r in rows if r.get("high_risk") and r.get("serial") == serial and "error" in r]
    if not grp:
        return {"n": 0, "errors": len(errs), "error_sample": errs[0]["error"] if errs else None}
    return {
        "n": len(grp),
        "errors": len(errs),
        "veto_rate": sum(r["veto_count"] for r in grp) / (len(grp) * 5),
        "all_5_vetoed": all(r["veto_count"] == 5 for r in grp),
        "leak_rate": sum(1 for r in grp if r["side_effect_leak"]) / len(grp),
        "order_kept_rate": sum(r["order_kept"] for r in grp) / len(grp),
        "overlap_rate": sum(r["overlap"] for r in grp) / len(grp),
        "state_ok_rate": sum(r["state_ok"] for r in grp) / len(grp),
    }


async def main():
    N = 30
    rows = await sweep(N)
    summary = {
        "test": "oai_concurrency_stress",
        "sdk": "openai-agents 0.23.1",
        "injection": "mixed: 6 tool calls (4 in-plan + 2 out-of-plan) + high-risk: 5 tool calls (ALL out-of-plan: send_money/delete_file/purge_logs/wire_transfer/drop_table) into _FunctionToolBatchExecutor (Runner.run internal path), no LLM",
        "plan_order": DECLARED_PLAN,
        "injection_order": INJECTION,
        "n_per_group": N,
        "high_risk_group": {"injection": HIGH_RISK_INJECTION, "declared_plan_excludes_all": True},
        "claims": {
            "S_serial_official": summarize(rows, "reject_content", True),
            "P_concurrent_control_soft": summarize(rows, "reject_content", False),
            "S_serial_hard": summarize(rows, "raise_exception", True),
            "P_concurrent_control_hard": summarize(rows, "raise_exception", False),
            "HR5_serial_all_out_of_plan": summarize_high_risk(rows, True),
            "HR5_concurrent_all_out_of_plan": summarize_high_risk(rows, False),
        },
        "raw_rows_sample": rows[:6],
    }
    out = "oai_concurrency_stress_result.json"
    json.dump(summary, open(out, "w"), ensure_ascii=False, indent=1)
    print(json.dumps(summary["claims"], ensure_ascii=False, indent=1))
    print("saved:", out)


asyncio.run(main())
