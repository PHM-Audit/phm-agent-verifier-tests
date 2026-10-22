"""adk_fork_repro.py — independent reproduction of the concurrent-dispatch
ledger-fork + per-call-veto findings (google/adk-python #6099 thread, 2026-10-22).

Origin: third-party comment (tonydzi / "Mycroft" agent) reported on google-adk
2.12.0: (1) two function calls in one model response run before-tool callbacks
concurrently, so a naive read-head/await/write-head ledger forks silently;
(2) a veto on call A does not stop sibling call B. We re-implemented the
minimal fixture from their description (script not seen) and ran it here.

Method: docs-promise vs actual behavior, same docs-promise-vs-actual-behavior caliber as our other published harnesses.
Canned BaseLlm emits ONE response with two function_calls send_money(to=A|B).
before-tool callback: record DECISION -> ledger read-head -> one await -> write-head
-> (veto rows) return dict for to=="A". Tool body appends SIDE-EFFECT.

Rows: {ledger lock off/on} x {veto A no/yes}.
Expected per third-party report:
  unlocked -> B.parent == GENESIS (fork=True);  locked -> single chain (fork=False)
  veto A=yes -> SIDE-EFFECT B still runs (per-call veto, not per-turn).
"""
from __future__ import annotations

import asyncio
import hashlib
import json
import platform
import time
from pathlib import Path

import google.adk
from google.adk.agents.llm_agent import LlmAgent
from google.adk.models.base_llm import BaseLlm
from google.adk.models.llm_response import LlmResponse
from google.adk.runners import InMemoryRunner
from google.genai import types as gtypes

HERE = Path(__file__).parent
ADK_VERSION = getattr(google.adk, "__version__", "2.12.0")

TIMELINE: list[str] = []


class TwoCaller(BaseLlm):
    """Canned model: first turn emits ONE response with two function calls;
    after function responses arrive, finalizes with text."""
    model: str = "canned-two-caller"

    async def generate_content_async(self, llm_request, stream=False):
        last = llm_request.contents[-1] if llm_request.contents else None
        got_fn_resp = last is not None and any(
            p.function_response is not None for p in (last.parts or [])
        )
        if got_fn_resp:
            yield LlmResponse(
                content=gtypes.Content(role="model", parts=[gtypes.Part(text="done")])
            )
        else:
            parts = [
                gtypes.Part(function_call=gtypes.FunctionCall(name="send_money", args={"to": "A"})),
                gtypes.Part(function_call=gtypes.FunctionCall(name="send_money", args={"to": "B"})),
            ]
            yield LlmResponse(content=gtypes.Content(role="model", parts=parts))


async def send_money(to: str) -> dict:
    TIMELINE.append(f"SIDE-EFFECT {to}")
    return {"ok": True, "to": to}


def make_ledger_callback(locked: bool, veto_a: bool):
    lock = asyncio.Lock()
    head = {"digest": "GENESIS"}
    entries: list[tuple[str, str]] = []

    async def ledger_cb(tool, args, tool_context):
        TIMELINE.append(f"DECISION {args.get('to')}")
        if locked:
            async with lock:
                parent = head["digest"]
                await asyncio.sleep(0)          # the one await: interleaving window
                digest = hashlib.sha256(f"{parent}|{args.get('to')}".encode()).hexdigest()[:12]
                head["digest"] = digest
                entries.append((args.get("to"), parent))
        else:
            parent = head["digest"]
            await asyncio.sleep(0)              # the one await: interleaving window
            digest = hashlib.sha256(f"{parent}|{args.get('to')}".encode()).hexdigest()[:12]
            head["digest"] = digest
            entries.append((args.get("to"), parent))
        if veto_a and args.get("to") == "A":
            return {"status": "vetoed", "reason": "policy blocks A"}
        return None

    return ledger_cb, entries


async def run_row(locked: bool, veto_a: bool) -> dict:
    TIMELINE.clear()
    cb, entries = make_ledger_callback(locked, veto_a)
    agent = LlmAgent(
        name="fork_repro",
        model=TwoCaller(),
        instruction="Call send_money twice (A and B) in one response.",
        tools=[send_money],
        before_tool_callback=cb,
    )
    runner = InMemoryRunner(agent=agent, app_name="phm_fork_repro")
    session = await runner.session_service.create_session(
        app_name="phm_fork_repro", user_id="repro"
    )
    n_events = 0
    async for _ev in runner.run_async(
        user_id="repro", session_id=session.id,
        new_message=gtypes.Content(role="user", parts=[gtypes.Part(text="go")]),
    ):
        n_events += 1

    fork = len(entries) >= 2 and entries[1][1] == "GENESIS" and entries[0][0] != entries[1][0]
    return {
        "locked": locked,
        "veto_a": veto_a,
        "order": list(TIMELINE),
        "ledger": [list(e) for e in entries],
        "fork": fork,
        "n_events": n_events,
    }


async def main() -> dict:
    rows = []
    for locked in (False, True):
        for veto_a in (False, True):
            rows.append(await run_row(locked, veto_a))

    out = {
        "test": "ADK concurrent-dispatch ledger fork + per-call veto repro (#6099 thread, 2026-10-22)",
        "date": time.strftime("%F %T"),
        "env": {"adk_version": ADK_VERSION, "python": platform.python_version(),
                "platform": platform.platform()},
        "rows": rows,
        "honesty_notes": [
            "independent re-implementation from the third-party comment's textual description; their 70-line script was not seen",
            "canned BaseLlm (deterministic, no external API, no network egress)",
            "tool is an async function; sync-tool thread-pool behavior untested (matches third-party's untested list)",
            "single run per row, mechanism demonstration not statistical claim",
        ],
    }
    out_path = HERE / "results" / "adk_fork_repro_results.json"
    out_path.parent.mkdir(exist_ok=True)
    out_path.write_text(json.dumps(out, ensure_ascii=False, indent=1))
    chain = hashlib.sha256(out_path.read_bytes()).hexdigest()
    (HERE / "results" / "adk_fork_repro_results.sha256").write_text(
        chain + "  adk_fork_repro_results.json\n")
    out["artifact_sha256"] = chain

    print(f"=== ADK {ADK_VERSION} fork/veto repro ===")
    for r in rows:
        print(f"locked={r['locked']!s:5} vetoA={'yes' if r['veto_a'] else 'no ':3} "
              f"order: {' | '.join(r['order'])}")
        print(f"{'':22} ledger: {r['ledger']}  fork={r['fork']}")
    print(f"artifact sha256[:16]={chain[:16]}")
    return out


if __name__ == "__main__":
    asyncio.run(main())
