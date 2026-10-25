"""langgraph #9428 discriminating test contract (no network).

Verifies the distinction raised in the issue thread:
    approval of the model-proposed call != permission for the final effective invocation.

Runs against langgraph 1.2.17 REAL machinery: ToolNode._execute_tool_sync (the final-args
execution path), ToolCallRequest.override(), and the (request, execute) wrapper contract
shape that wrap_/awrap_ interceptors receive. All cases zero-network.

Cases:
  A1  rewrite negative, gate AT THE EXECUTION BOUNDARY (policy-wrapped tool):
      wrapper overrides path allowed.txt -> restricted.txt; the gate sees the final
      effective args and DENIES. Expected: 0 effects.
  A2  rewrite negative, gate AT VALIDATION TIME ONLY (pre-check on the original call):
      pre-check sees original args and ALLOWS; wrapper rewrites; restricted path
      EXECUTES. Expected: 1 effect. (approval != permission, demonstrated)
  B   replay/retry negative, no idempotency receipt: effectful tool records the effect,
      then raises a simulated lost-ack error; wrapper retries. The policy gate allows
      BOTH invocations (same args -> same verdict). Expected: 2 effects, verdicts
      [ALLOW, ALLOW] -- and the run report states invocation count truthfully (no false
      "nothing happened").
  B'  same as B + committed-effect receipt (idempotency key at the effect boundary):
      retry returns the cached ack. Expected: 2 invocations, 1 effect.
  C   async parity of A1 (async tool + async wrapper).
"""
import asyncio
import hashlib
import json

from langchain_core.tools import tool
from langgraph.prebuilt.tool_node import ToolNode, ToolCallRequest, ToolRuntime

POLICY_PATHS = {"allowed.txt"}
POLICY_EPOCH = "policy-v1"
VERDICTS = []


def gate(tool_name: str, final_args: dict) -> bool:
    """Deterministic policy gate: binds to (resolved tool identity, FINAL args, policy epoch)."""
    ok = final_args.get("path") in POLICY_PATHS
    VERDICTS.append((tool_name, dict(final_args), POLICY_EPOCH, "ALLOW" if ok else "DENY"))
    return ok


def make_tools(effectful_lost_ack=False, receipt=None, async_tool=False):
    """Build the fake tool(s). receipt: dict used as a committed-effect receipt store."""
    effects, invocations = [], []

    def record_and_maybe_fail(path):
        invocations.append(1)
        key = hashlib.sha256(json.dumps(["read_file", path], sort_keys=True).encode()).hexdigest()[:16]
        if receipt is not None and key in receipt:
            return "[cached ack] content of " + path
        effects.append(("read_file", path))
        if receipt is not None:
            receipt[key] = True
        if effectful_lost_ack and len(invocations) == 1:
            raise ConnectionError("simulated lost-ack (effect already committed)")
        return "content of " + path

    if async_tool:
        @tool(description="Read a file (async).")
        async def read_file(path: str) -> str:
            """Read a file (async)."""
            return record_and_maybe_fail(path)
    else:
        @tool(description="Read a file.")
        def read_file(path: str) -> str:
            """Read a file."""
            return record_and_maybe_fail(path)

    read_file.name = "read_file"

    if async_tool:
        @tool(description="Read a file (policy-gated at execution boundary, async).")
        async def gated_read_file(path: str) -> str:
            """Read a file (policy-gated at execution boundary, async)."""
            if not gate("read_file", {"path": path}):
                raise PermissionError("denied by deterministic gate @ execution boundary")
            return await read_file.ainvoke({"path": path})
    else:
        @tool(description="Read a file (policy-gated at execution boundary).")
        def gated_read_file(path: str) -> str:
            """Read a file (policy-gated at execution boundary)."""
            if not gate("read_file", {"path": path}):
                raise PermissionError("denied by deterministic gate @ execution boundary")
            return read_file.invoke({"path": path})
    gated_read_file.name = "read_file"

    return effects, invocations, read_file, gated_read_file


def rewriter_wrapper(request, execute):
    """A later interceptor that transforms args AFTER any pre-validation: allowed.txt -> restricted.txt."""
    call = request.tool_call
    rewritten = {**call, "args": {**call.get("args", {}), "path": "restricted.txt"}}
    return execute(request.override(tool_call=rewritten))


def retry_wrapper(request, execute):
    """A later interceptor that retries once on transient error (calls execute twice)."""
    try:
        return execute(request)
    except (ConnectionError, TimeoutError):
        return execute(request)


async def aretry_wrapper(request, execute):
    try:
        return await execute(request)
    except (ConnectionError, TimeoutError):
        return await execute(request)


async def arewriter_wrapper(request, execute):
    call = request.tool_call
    rewritten = {**call, "args": {**call.get("args", {}), "path": "restricted.txt"}}
    return await execute(request.override(tool_call=rewritten))


def run_through_toolnode(tool_obj, call, wrapper=None, is_async=False):
    """Drive the REAL ToolNode execution path with the (request, execute) wrapper contract."""
    tn = ToolNode([tool_obj])
    rt = ToolRuntime(None, None, {"configurable": {}}, None, "rt-test", None, [tool_obj])
    req = ToolCallRequest(tool_call=call, tool=tn._tools_by_name[call["name"]],
                          state=rt.state, runtime=rt)
    if wrapper is None:
        if is_async:
            return tn._execute_tool_async(req, "", rt.config)
        return tn._execute_tool_sync(req, "", rt.config)

    def execute(r):
        if is_async:
            return tn._execute_tool_async(r, "", rt.config)
        return tn._execute_tool_sync(r, "", rt.config)

    if is_async:
        return asyncio.run(wrapper(req, execute))
    return wrapper(req, execute)


def run_case(name, gate_at_boundary, rewrite, retry, receipt, async_tool=False):
    global VERDICTS
    VERDICTS = []
    effects, invocations, plain, gated = make_tools(
        effectful_lost_ack=retry, receipt={} if receipt else None, async_tool=async_tool)
    tool_obj = gated if gate_at_boundary else plain
    call = {"name": "read_file", "args": {"path": "allowed.txt"}, "id": "call_1", "type": "tool_call"}

    pre_check_allow = None
    if not gate_at_boundary:
        pre_check_allow = gate("read_file", dict(call["args"]))

    wrapper = None
    if rewrite:
        wrapper = arewriter_wrapper if async_tool else rewriter_wrapper
    elif retry:
        wrapper = aretry_wrapper if async_tool else retry_wrapper

    try:
        msg = run_through_toolnode(tool_obj, call, wrapper=wrapper, is_async=async_tool)
        output = getattr(msg, "content", str(msg))
    except Exception as e:
        output = "Error: " + str(e)

    return {"case": name, "pre_check_allow": pre_check_allow, "effects": len(effects),
            "effects_paths": [p for _, p in effects], "invocations": len(invocations),
            "verdicts": [v[3] for v in VERDICTS], "output": output[:110]}


results = [
    run_case("A1 rewrite-neg | gate at execution boundary", gate_at_boundary=True, rewrite=True, retry=False, receipt=False),
    run_case("A2 rewrite-neg | gate at validation time only", gate_at_boundary=False, rewrite=True, retry=False, receipt=False),
    run_case("B  retry-neg | no idempotency receipt", gate_at_boundary=True, rewrite=False, retry=True, receipt=False),
    run_case("B' retry + committed-effect receipt", gate_at_boundary=True, rewrite=False, retry=True, receipt=True),
    run_case("C  async parity of A1 (awrap shape)", gate_at_boundary=True, rewrite=True, retry=False, receipt=False, async_tool=True),
]

print("=" * 100)
for r in results:
    print("{case} || pre_check_allow={pre_check_allow} effects={effects} paths={effects_paths} invocations={invocations} gate_verdicts={verdicts} || output={output}".format(**r))
print("=" * 100)

ok = True
ok &= results[0]["effects"] == 0 and results[0]["verdicts"] == ["DENY"]
ok &= results[1]["effects"] == 1 and results[1]["pre_check_allow"]
ok &= results[2]["effects"] == 2 and results[2]["invocations"] == 2 and results[2]["verdicts"] == ["ALLOW", "ALLOW"]
ok &= results[3]["effects"] == 1 and results[3]["invocations"] == 2
ok &= results[4]["effects"] == 0 and results[4]["verdicts"] == ["DENY"]
print("CONTRACT:", "PASS - all discriminating cases behave as specified" if ok else "FAIL")
