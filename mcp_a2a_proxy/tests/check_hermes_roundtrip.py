#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""check_hermes_roundtrip.py — Hermes end-to-end check via mcp_a2a_proxy.

Uses ONLY the MCP tool functions of mcp_a2a_proxy (which route through
a2a_daemon_engine to the Hermes peer) — no direct backend calls:

    send_a2a_message(agent_id=hermes-agent)   -> delegate
    get_a2a_task(task_id)                     -> track to terminal state

PASS requires: reply text matches the prompt contract, a task_id is issued,
and tasks/get reaches `completed` for that same task id.
"""
from __future__ import annotations

import json
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))

from mcp_a2a_proxy.tests.run_all_functions import (  # noqa: E402
    GATEWAY_BASE_URL, HERMES_AGENT_ID, PART_ID, _build_proxy, _state_of,
    _first_text, _extract_task_id, _is_error, TERMINAL_STATES,
)

PROMPT = "Health check: reply with exactly the word PONG and nothing else."
EXPECTED_WORD = "PONG"


def main() -> int:
    print("=" * 70)
    print("Hermes end-to-end check — via mcp_a2a_proxy MCP functions only")
    print(f"gateway={GATEWAY_BASE_URL} part={PART_ID}")
    print("=" * 70)

    proxy = _build_proxy()

    # 1. Delegate through the MCP tool function.
    print(f"\n1. send_a2a_message(agent_id={HERMES_AGENT_ID}):")
    send = proxy.send_a2a_message(
        **{"message": {"role": "user",
                       "parts": [{"kind": "text", "text": PROMPT}]},
           "agent_id": HERMES_AGENT_ID})
    if _is_error(send):
        print(f"   FAILED: {json.dumps(send, default=str)[:200]}")
        return 1
    reply_text = _first_text(send)
    task_id = _extract_task_id(send)
    print(f"   reply text : {reply_text!r}")
    print(f"   task_id    : {task_id}")
    print(f"   context_id : {send.get('context_id')}")

    # 2. Track through the MCP tool function.
    print("\n2. get_a2a_task(task_id):")
    last, waited = None, 0.0
    while waited < 60.0:
        last = proxy.get_a2a_task(task_id=task_id)
        if _state_of(last) in TERMINAL_STATES:
            break
        import time as _time
        _time.sleep(2)
        waited += 2
    state = _state_of(last)
    print(f"   state      : {state}")

    # 3. Assertions.
    checks = [
        ("reply text matches prompt contract",
         reply_text.strip().upper() == EXPECTED_WORD.upper()),
        ("task_id issued on the reply", bool(task_id)),
        ("task reached terminal state", state in TERMINAL_STATES),
        ("task completed", state == "completed"),
    ]
    all_ok = True
    for label, ok in checks:
        print(f"   [{'PASS' if ok else 'FAIL'}] {label}")
        all_ok = all_ok and ok

    print("\nVERDICT:", "PASS — Hermes answered correctly through "
          "mcp_a2a_proxy -> a2a_daemon_engine" if all_ok else "FAIL")
    return 0 if all_ok else 1


if __name__ == "__main__":
    sys.exit(main())