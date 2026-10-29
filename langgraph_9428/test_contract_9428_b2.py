#!/usr/bin/env python3
"""
B'' negative control for test_contract_9428.py (langgraph #9428 follow-up).

Motivated by reviewer feedback (Nakagawa-master, 2026-10-29): the B' receipt
key in test_contract_9428.py derives from ["read_file", path] alone, so two
distinct authorized operations with the same tool and args share a key.
B' therefore demonstrates retry deduplication but cannot distinguish a retry
of the same logical operation from a NEW operation with identical arguments.

This extension adds the discriminating pair:

  B''  (a) op-scoped receipt key:  key = hash([tool, path, op_id])
       (b) param-only receipt key: key = hash([tool, path])   (the B' key)

Cases (in-memory, single-threaded, no network):
  B'  pos     op-1 commits once; its lost-ack retry (same op_id) returns the
              committed receipt; total effects = 1.
  B'' neg     a subsequent independent authorized op-2 with the SAME tool and
              final args but a different op_id commits once more; cumulative
              effects = 2. Under an op-scoped key this passes; under a
              param-only key op-2 reuses op-1's cached receipt (effects = 1)
              and the control FAILS the implementation.

Boundary separation (per the reviewer's guidance):
  - authorization decision stays bound to (resolved tool identity, FINAL
    args, policy epoch) -- unchanged from the base file;
  - the deduplication receipt is bound to an operation identity scoped to
    that authorized attempt (plus the actual effect target);
  - an operation ID is never carried across a changed authorization or target.

Env: python 3.12, langgraph 1.2.x (ToolNode machinery only, no agent loop).
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
    """Deterministic policy gate: binds to (resolved tool identity, FINAL args, policy epoch).
    op_id is transport metadata for deduplication and is NOT part of the authorization input."""
    ok = final_args.get("path") in POLICY_PATHS
    VERDICTS.append((tool_name, dict(final_args), POLICY_EPOCH, "ALLOW" if ok else "DENY"))
    return ok


def make_tools_op(effectful_lost_ack=False, receipt=None, op_scoped_key=True):
    """Fake side-effecting tool whose receipt key binds to (tool, path, op_id)
    when op_scoped_key=True, else to (tool, path) -- the param-only variant."""
    effects, invocations = [], []

    def record_and_maybe_fail(path: str, op_id: str = "") -> str:
        """Effect boundary: commit-once per (tool, path[, op_id])."""
        invocations.append(1)
        key_payload = ["read_file", path] + ([op_id] if op_scoped_key else [])
        key = hashlib.sha256(json.dumps(key_payload, sort_keys=True).encode()).hexdigest()[:16]
        if receipt is not None and key in receipt:
            return "[cached ack] content of " + path
        effects.append(("read_file", path))
        if receipt is not None:
            receipt[key] = True
        if effectful_lost_ack and len(invocations) == 1:
            raise ConnectionError("simulated lost-ack (effect already committed)")
        return "content of " + path

    @tool(description="Read a file (effect boundary with op-scoped receipt).")
    def read_file(path: str, op_id: str = "") -> str:
        """Read a file (effect boundary with op-scoped receipt)."""
        return record_and_maybe_fail(path, op_id)

    read_file.name = "read_file"

    @tool(description="Read a file (policy-gated at execution boundary).")
    def gated_read_file(path: str, op_id: str = "") -> str:
        """Read a file (policy-gated at execution boundary)."""
        if not gate("read_file", {"path": path}):
            raise PermissionError("denied by deterministic gate @ execution boundary")
        return read_file.invoke({"path": path, "op_id": op_id})

    gated_read_file.name = "read_file"

    return effects, invocations, read_file, gated_read_file


def retry_wrapper(request, execute):
    """A later interceptor that retries once on transient error (calls execute twice)."""
    try:
        return execute(request)
    except (ConnectionError, TimeoutError):
        return execute(request)


def run_through_toolnode(tool_obj, call):
    """Drive the REAL ToolNode execution path (sync, no wrapper -- wrappers are
    represented here by the harness directly re-invoking execute, as in the
    base file's retry shape)."""
    tn = ToolNode([tool_obj])
    rt = ToolRuntime(None, None, {"configurable": {}}, None, "rt-test", None, [tool_obj])
    req = ToolCallRequest(tool_call=call, tool=tn._tools_by_name[call["name"]],
                          state=rt.state, runtime=rt)
    return tn._execute_tool_sync(req, "", rt.config)


def run_b2(op_scoped_key: bool):
    """B' positive + B'' negative control in one flow.

    op-1: lost-ack commit + same-op retry  -> expect effects == 1
    op-2: independent op, same tool+args   -> expect cumulative effects == 2
           (param-only key fails this: op-2 gets op-1's cached ack, effects == 1)
    """
    global VERDICTS
    VERDICTS = []
    receipt = {}
    effects, invocations, plain, gated = make_tools_op(
        effectful_lost_ack=True, receipt=receipt, op_scoped_key=op_scoped_key)

    # --- op-1: first attempt commits then raises (lost ack) ---
    call_1 = {"name": "read_file", "args": {"path": "allowed.txt", "op_id": "op-1"},
              "id": "call_1", "type": "tool_call"}
    try:
        out1_first = run_through_toolnode(gated, call_1)
        out1_first = getattr(out1_first, "content", str(out1_first))
    except Exception as e:
        out1_first = "Error: " + str(e)

    # --- op-1: retry (same logical operation, same op_id) ---
    try:
        out1_retry = run_through_toolnode(gated, call_1)
        out1_retry = getattr(out1_retry, "content", str(out1_retry))
    except Exception as e:
        out1_retry = "Error: " + str(e)

    effects_after_retry = len(effects)

    # --- op-2: independent authorized operation, same tool + same final args ---
    call_2 = {"name": "read_file", "args": {"path": "allowed.txt", "op_id": "op-2"},
              "id": "call_2", "type": "tool_call"}
    try:
        out2 = run_through_toolnode(gated, call_2)
        out2 = getattr(out2, "content", str(out2))
    except Exception as e:
        out2 = "Error: " + str(e)

    total_effects = len(effects)
    op1_dedup_ok = (effects_after_retry == 1
                    and out1_first.startswith("Error:") and "lost-ack" in out1_first
                    and out1_retry.startswith("[cached ack]"))
    op2_independent = (out2 == "content of allowed.txt")  # not op-1's cached ack
    passed = op1_dedup_ok and op2_independent and total_effects == 2

    return {
        "key_mode": "op_scoped" if op_scoped_key else "param_only",
        "op1_first": out1_first[:70],
        "op1_retry": out1_retry[:70],
        "op2_out": out2[:70],
        "effects_after_retry": effects_after_retry,
        "total_effects": total_effects,
        "op1_dedup_ok": op1_dedup_ok,
        "op2_independent": op2_independent,
        "gate_verdicts": [v[3] for v in VERDICTS],
        "passed": passed,
    }


print("=" * 100)
b2_results = [run_b2(op_scoped_key=True), run_b2(op_scoped_key=False)]
for r in b2_results:
    print(("B''[{key_mode}] || op1_first={op1_first} || op1_retry={op1_retry} || op2_out={op2_out} "
           "|| effects_after_retry={effects_after_retry} total_effects={total_effects} "
           "op1_dedup_ok={op1_dedup_ok} op2_independent={op2_independent} "
           "gate_verdicts={gate_verdicts} || PASSED={passed}").format(**r))
print("=" * 100)

op_scoped = b2_results[0]
param_only = b2_results[1]
control_works = (op_scoped["passed"] is True) and (param_only["passed"] is False)
print("B'' CONTRACT:",
      "PASS - op-scoped key: op-1 retry dedupes (effects=1) AND op-2 commits independently (effects=2); "
      "param-only key fails the control (op-2 suppressed, effects=1)"
      if control_works else "FAIL - control did not discriminate as specified")
print("DETERMINISM: pure functions, no randomness, no network; fixed policy epoch "
      + POLICY_EPOCH + "; receipt store in-memory dict")
