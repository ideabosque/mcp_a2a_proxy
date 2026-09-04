#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""check_agent_roundtrip.py — agent end-to-end check via mcp_a2a_proxy.

Uses ONLY the MCP tool functions of mcp_a2a_proxy (which route through
a2a_daemon_engine to the target peer) — no direct backend calls:

    send_a2a_message(agent_id=<agent>)   -> delegate
    get_a2a_task(task_id)                -> track to terminal state

Works for any registered peer: hermes-agent, core-engine-agent, or any
agent registered in the partition's a2a_agents registry.

PASS requires: reply text contains the expected word, a task_id is issued,
and tasks/get reaches `completed` for that same task id.

Usage:
    python mcp_a2a_proxy/tests/check_agent_roundtrip.py                  # default: hermes-agent, PONG
    python mcp_a2a_proxy/tests/check_agent_roundtrip.py --agent core-engine-agent --word ACK
    python mcp_a2a_proxy/tests/check_agent_roundtrip.py --list           # discover registered agents
    python mcp_a2a_proxy/tests/check_agent_roundtrip.py --all            # test every active agent
"""

from __future__ import annotations

import argparse
import json
import sys
import time
from pathlib import Path
from typing import Any, Dict, List, Optional

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))

from mcp_a2a_proxy.tests.run_all_functions import (  # noqa: E402
    GATEWAY_BASE_URL, PART_ID, _build_proxy, _state_of, _first_text,
    _extract_task_id, _is_error, TERMINAL_STATES,
)

POLL_TIMEOUT = 90.0
POLL_INTERVAL = 2.0


def _agent_list(proxy: Any) -> List[Dict[str, Any]]:
    """discover_a2a_agents wrapper returning the agent rows."""
    out = proxy.discover_a2a_agents(status="active")
    if isinstance(out, dict):
        lst = out.get("a2a_agent_list")
        if isinstance(lst, dict):
            lst = lst.get("a2a_agent_list") or []
        if isinstance(lst, list):
            return lst
    return []


def check_agent(proxy: Any, agent_id: str, expected_word: str,
                prompt: Optional[str] = None) -> bool:
    """Full delegate→track→assert cycle against one agent, MCP functions only."""
    prompt = prompt or (
        f"Health check: reply with exactly the word {expected_word} "
        f"and nothing else.")
    print(f"\n{'─' * 66}")
    print(f"agent_id: {agent_id} | expected: {expected_word!r}")
    print(f"{'─' * 66}")

    # 1. Delegate through the MCP tool function.
    print(f"1. send_a2a_message(agent_id={agent_id}):")
    send = proxy.send_a2a_message(
        **{"message": {"role": "user",
                       "parts": [{"kind": "text", "text": prompt}]},
           "agent_id": agent_id})
    if _is_error(send):
        print(f"   FAILED: {json.dumps(send, default=str)[:200]}")
        return False
    reply_text = _first_text(send)
    task_id = _extract_task_id(send)
    print(f"   reply text : {reply_text!r}")
    print(f"   task_id    : {task_id}")
    print(f"   context_id : {send.get('context_id')}")

    # Structural-failure shape (DEF-004 fix): daemon returned a FAILED Task.
    if _state_of(send) == "failed":
        print("   [FAIL] peer returned a structural FAILED task (routing/"
              "backend error)")
        return False

    # 2. Track through the MCP tool function.
    print("2. get_a2a_task(task_id):")
    if not task_id:
        print("   [FAIL] no task_id on reply")
        return False
    last, waited = None, 0.0
    while waited < POLL_TIMEOUT:
        last = proxy.get_a2a_task(task_id=task_id)
        if _state_of(last) in TERMINAL_STATES:
            break
        time.sleep(POLL_INTERVAL)
        waited += POLL_INTERVAL
    state = _state_of(last)
    print(f"   state      : {state}")

    # 3. Assertions.
    checks = [
        ("reply text contains expected word",
         expected_word.lower() in reply_text.lower()),
        ("task_id issued on the reply", bool(task_id)),
        ("task reached terminal state", state in TERMINAL_STATES),
        ("task completed", state == "completed"),
    ]
    all_ok = True
    for label, ok in checks:
        print(f"   [{'PASS' if ok else 'FAIL'}] {label}")
        all_ok = all_ok and ok
    return all_ok


def check_agent_stream(proxy: Any, agent_id: str, expected_word: str,
                       prompt: Optional[str] = None) -> bool:
    """Streaming variant: delegate via send_a2a_message_stream and assert
    streaming_complete + ordered events + the expected word in the final
    event text. MCP tool functions only (no direct backend calls)."""
    prompt = prompt or (
        f"Streaming health check: reply with exactly the word "
        f"{expected_word} and nothing else.")
    print(f"\n{'─' * 66}")
    print(f"agent_id: {agent_id} | expected: {expected_word!r} | mode: stream")
    print(f"{'─' * 66}")

    print(f"1. send_a2a_message_stream(agent_id={agent_id}):")
    out = proxy.send_a2a_message_stream(
        **{"message": {"role": "user",
                       "parts": [{"kind": "text", "text": prompt}]},
           "agent_id": agent_id})
    if _is_error(out):
        print(f"   FAILED: {json.dumps(out, default=str)[:200]}")
        return False

    status = out.get("status") if isinstance(out, dict) else None
    events = out.get("events") or [] if isinstance(out, dict) else []
    events_emitted = out.get("events_emitted") if isinstance(out, dict) else None

    # Last event carrying agent text — the final reply.
    final_text = ""
    for e in reversed(events):
        if isinstance(e, dict):
            for part in e.get("parts") or []:
                if isinstance(part, dict) and part.get("text"):
                    final_text = str(part["text"])
                    break
        if final_text:
            break

    print(f"   status        : {status}")
    print(f"   events_emitted: {events_emitted}")
    print(f"   final text    : {final_text[:80]!r}")

    checks = [
        ("status == streaming_complete", status == "streaming_complete"),
        ("events_emitted > 0", bool(events_emitted) and events_emitted > 0),
        ("events list present", isinstance(events, list) and len(events) > 0),
        ("final event text contains expected word",
         expected_word.lower() in final_text.lower()),
    ]
    all_ok = True
    for label, ok in checks:
        print(f"   [{'PASS' if ok else 'FAIL'}] {label}")
        all_ok = all_ok and ok
    return all_ok


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--agent", default="hermes-agent",
                        help="agent_id to test (default: hermes-agent)")
    parser.add_argument("--word", default="PONG",
                        help="expected reply word (default: PONG)")
    parser.add_argument("--prompt", default=None,
                        help="custom prompt (overrides the default template)")
    parser.add_argument("--stream", action="store_true",
                        help="use send_a2a_message_stream instead of "
                             "send_a2a_message")
    parser.add_argument("--list", action="store_true",
                        help="list registered active agents and exit")
    parser.add_argument("--all", action="store_true",
                        help="test every active agent in the partition")
    args = parser.parse_args()

    print("=" * 66)
    print("Agent end-to-end check — via mcp_a2a_proxy MCP functions only")
    print(f"gateway={GATEWAY_BASE_URL} part={PART_ID}")
    print("=" * 66)

    proxy = _build_proxy()

    if args.list:
        agents = _agent_list(proxy)
        print(f"registered active agents ({len(agents)}):")
        for a in agents:
            print(f"  - {a.get('agent_id')}  ({a.get('agent_name')})")
        return 0

    if args.all:
        agents = _agent_list(proxy)
        if not agents:
            print("no active agents discovered")
            return 1
        results: Dict[str, bool] = {}
        for a in agents:
            agent_id = a.get("agent_id")
            if args.stream:
                results[agent_id] = check_agent_stream(proxy, agent_id,
                                                       args.word, args.prompt)
            else:
                results[agent_id] = check_agent(proxy, agent_id, args.word,
                                                args.prompt)
        print("\n" + "=" * 66)
        print("SUMMARY:")
        all_ok = True
        for agent_id, ok in results.items():
            print(f"  [{'PASS' if ok else 'FAIL'}] {agent_id}")
            all_ok = all_ok and ok
        print("\nVERDICT:",
              "PASS — all active agents answered correctly through "
              "mcp_a2a_proxy -> a2a_daemon_engine" if all_ok else
              "FAIL — one or more agents failed")
        return 0 if all_ok else 1

    if args.stream:
        ok = check_agent_stream(proxy, args.agent, args.word, args.prompt)
    else:
        ok = check_agent(proxy, args.agent, args.word, args.prompt)
    print("\nVERDICT:",
          f"PASS — {args.agent} answered correctly through "
          "mcp_a2a_proxy -> a2a_daemon_engine" if ok
          else f"FAIL — {args.agent} check failed")
    return 0 if ok else 1


if __name__ == "__main__":
    sys.exit(main())