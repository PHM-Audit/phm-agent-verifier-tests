# PHM validator as ToolNode wrap_tool_call hook - PoC on langgraph 1.2.19 (real StateGraph run)
# Pointer (langgraph#9428, eliornl): the hook runs BEFORE tool execution ->
# deterministic validator lands there, no new API needed.
from langchain_core.tools import tool
from langchain_core.messages import AIMessage
from typing import Literal
from langgraph.graph import StateGraph, MessagesState, START, END
from langgraph.prebuilt import ToolNode
from phm_lite_check import check_text  # public wheel: github.com/PHM-Audit/phm-lite-check


def phm_guard(request, execute):
    """Deterministic physics guard: validate string args BEFORE execution.
    VETO -> ToolMessage with evidence, tool never runs. PASS -> execute normally."""
    args = request.tool_call.get("args", {}) or {}
    texts = [v for v in args.values() if isinstance(v, str) and v.strip()]
    for t in texts:
        r = check_text(t)
        if r["verdict"] == "FAIL":
            return ToolMessage(
                content="PHM_VETO " + r["violations"][0]["rule"] + " | " + r["violations"][0]["detail"],
                name=request.tool_call[""name"],
                tool_call_id=request.tool_call[""id"],
            )
    return execute(request)


@tool
def submit_flight_report(claim: str) -> str:
    """File a flight performance report claim."""
    return "REPORT_FILED: " + claim


tools_node = ToolNode([submit_flight_report], wrap_tool_call=phm_guard)


def model_node(state):  # static stand-in for an LLM turn: issue the pending tool_calls
    return {"messages": state["messages"]}


g = StateGraph(MessagesState)
g.add_node("model", model_node)
g.add_node("tools", tools_node)
g.add_edge(START, "model")
g.add_conditional_edges("model", lambda s: "tools" if getattr(s["messages"][-1], "tool_calls", None) else END, {"tools": "tools", END: END})
g.add_edge("tools", END)
app = g.compile()

# Case 1: violating claim (1.67x light speed) -> VETOed BEFORE tool runs
ai1 = AIMessage(content="", tool_calls=[{"name": "submit_flight_report", "args": {"claim": "speed 500000 km/s satellite"}, "id": "c1", "type": "tool_call"}])
r1 = app.invoke({"messages": [ai1]})
m1 = r1["messages"][-1].content
print("CASE1:", m1 if isinstance(m1, str) else m1)

# Case 2: plausible claim -> tool executes normally
ai2 = AIMessage(content="", tool_calls=[{"name": "submit_flight_report", "args": {"claim": "cruise 900 km/h, range 5000 km, 5.6 h"}, "id": "c2", "type": "tool_call"}])
r2 = app.invoke({"messages": [ai2]})
m2 = r2["messages"][-1].content
print("CASE2:", m2 if isinstance(m2, str) else m2)
