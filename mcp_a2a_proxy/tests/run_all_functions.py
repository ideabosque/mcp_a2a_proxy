#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""run_all_functions.py — exercise all 8 MCP A2A Proxy tools directly and
print every result to stdout.

Standalone smoke driver (not a pytest module): builds MCPA2AProxy exactly as
the MCP host would, calls each of the 8 tools with realistic arguments —
happy paths plus representative error paths — and prints method, elapsed
time, and the returned payload for every call.

Credentials: read at runtime from the gateway .env (ADMIN_USERNAME /
ADMIN_PASSWORD) or AIT_A2A_TOKEN_USERNAME / AIT_A2A_TOKEN_PASSWORD env vars.
Never printed or persisted.

Usage:
    python -m mcp_a2a_proxy.tests.run_all_functions            # all tools
    python -m mcp_a2a_proxy.tests.run_all_functions --quick    # read-only tools
    python -m mcp_a2a_proxy.tests.run_all_functions --tool send_a2a_message
"""

from __future__ import annotations

__author__ = "Idea Bosque"

import argparse
import json
import sys
import time
import traceback
from pathlib import Path
from typing import Any, Callable, Dict, List, Optional

# ── Paths ────────────────────────────────────────────────────────────────────

_TESTS_DIR = Path(__file__).resolve().parent
_RESULTS_DIR = _TESTS_DIR / "results"

# ── Constants ────────────────────────────────────────────────────────────────

GATEWAY_BASE_URL = "http://localhost:8765"
ENDPOINT_ID = "gpt"
PART_ID = "nestaging"
HERMES_AGENT_ID = "hermes-agent"
CORE_ENGINE_AGENT_ID = "core-engine-agent"
BOGUS_AGENT_ID = "bogus-agent-runall"
BOGUS_TASK_ID = "bogus-task-run-all"
STREAM_TIMEOUT = 120.0
TERMINAL_STATES = {"completed", "failed", "canceled", "cancelled"}
POLL_INTERVAL = 2.0
POLL_TIMEOUT = 60.0

logger_name = "run-all-functions"


# ── Bootstrap ────────────────────────────────────────────────────────────────

def _load_credentials() -> Dict[str, str]:
    """Credential source: gateway .env or AIT_* env overrides (runtime only)."""
    creds: Dict[str, str] = {}
    candidates = [
        Path(r"C:\Users\bibo7\gitrepo\silvaengine\silvaengine_gateway")
        / "silvaengine_gateway" / "tests" / ".env",
        _TESTS_DIR.parents[2] / "silvaengine_gateway"
        / "silvaengine_gateway" / "tests" / ".env",
    ]
    for env_path in candidates:
        if env_path.exists():
            from dotenv import dotenv_values
            env = dotenv_values(env_path)
            creds["username"] = env.get("ADMIN_USERNAME") or ""
            creds["password"] = env.get("ADMIN_PASSWORD") or ""
            break
    import os
    creds["username"] = os.environ.get("AIT_A2A_TOKEN_USERNAME") or creds.get("username", "")
    creds["password"] = os.environ.get("AIT_A2A_TOKEN_PASSWORD") or creds.get("password", "")
    return creds


def _build_proxy() -> Any:
    import logging

    from mcp_a2a_proxy.mcp_a2a_proxy import MCPA2AProxy

    logging.basicConfig(level=logging.WARNING)
    logger = logging.getLogger(logger_name)
    creds = _load_credentials()
    setting: Dict[str, Any] = {
        "gateway_base_url": GATEWAY_BASE_URL,
        "a2a_jsonrpc_endpoint": f"{GATEWAY_BASE_URL}/{{endpoint_id}}/a2a",
        "a2a_agent_card_endpoint":
            f"{GATEWAY_BASE_URL}/{{endpoint_id}}/.well-known/agent-card.json",
        "graphql_modules": {
            "a2a_daemon_engine": {
                "endpoint":
                    f"{GATEWAY_BASE_URL}/{{endpoint_id}}/a2a_core_graphql",
                "gateway_base_url": GATEWAY_BASE_URL,
                "token_username": creds["username"],
                "token_password": creds["password"],
            }
        },
        "a2a_stream_timeout": STREAM_TIMEOUT,
        "default_page_limit": 20,
    }
    proxy = MCPA2AProxy(logger, **setting)
    proxy.endpoint_id = ENDPOINT_ID
    proxy.part_id = PART_ID
    return proxy


# ── Helpers ──────────────────────────────────────────────────────────────────

def _is_error(output: Any) -> bool:
    if isinstance(output, dict) and output.get("error_code"):
        return True
    if isinstance(output, str) and '"error_code"' in output:
        return True
    return False


def _state_of(output: Any) -> Optional[str]:
    if not isinstance(output, dict):
        return None
    raw = output.get("status") or output.get("state") or ""
    if isinstance(raw, dict):
        raw = raw.get("state") or ""
    name = str(raw).strip().replace("TASK_STATE_", "").replace("-", "_").lower()
    return name or None


def _first_text(output: Any) -> str:
    if isinstance(output, dict):
        parts = output.get("parts")
        if isinstance(parts, list) and parts and isinstance(parts[0], dict):
            return str(parts[0].get("text", ""))[:100]
    return ""


def _extract_task_id(output: Any) -> Optional[str]:
    if isinstance(output, dict):
        for key in ("task_id", "taskId", "id"):
            if output.get(key):
                return str(output[key])
        nested = output.get("result")
        if isinstance(nested, dict):
            return _extract_task_id(nested)
    return None


def _message(prompt: str) -> Dict[str, Any]:
    return {"role": "user", "parts": [{"kind": "text", "text": prompt}]}


def _scrub(text: str) -> str:
    import re
    # Redact credential-shaped values before printing (DEF-001 hygiene).
    text = re.sub(
        r'"(eyJ[A-Za-z0-9_-]{10,}\.[A-Za-z0-9_-]{10,}\.[A-Za-z0-9_-]{10,})"',
        '"[REDACTED_JWT]"', text)
    text = re.sub(
        r'("(?:[a-z_]*api_key|core_engine_token|[a-z_]*secret|password)"\s*:\s*")'
        r'([^"]{4})[^"]*(")',
        lambda m: f"{m.group(1)}{m.group(2)}...{m.group(3)}",
        text, flags=re.IGNORECASE)
    return text


# ── Call records ─────────────────────────────────────────────────────────────

class Runner:
    def __init__(self) -> None:
        self.records: List[Dict[str, Any]] = []

    def call(self, method: str, arguments: Dict[str, Any],
             fn: Callable[[], Any]) -> Any:
        import time as _time
        started = _time.perf_counter()
        try:
            output = fn()
            error = None
        except Exception as e:
            output = None
            error = f"{type(e).__name__}: {e}"
        elapsed_ms = int((_time.perf_counter() - started) * 1000)
        status = ("ERROR" if error
                  else "ERROR" if _is_error(output) else "OK")
        self.records.append({
            "method": method,
            "arguments": arguments,
            "status": status,
            "elapsed_ms": elapsed_ms,
            "output": output,
            "error": error,
        })
        # Print the result immediately.
        out_json = json.dumps(output, default=str, ensure_ascii=False) \
            if output is not None else (error or "")
        out_json = _scrub(out_json)
        if len(out_json) > 1200:
            out_json = out_json[:1200] + f" ...[truncated, {len(out_json)} chars]"
        print(f"    [{status}] {method} ({elapsed_ms} ms)")
        print(f"      args: {_scrub(json.dumps(arguments, default=str))[:300]}")
        print(f"      out : {out_json}")
        return output

    def summary(self) -> int:
        ok = sum(1 for r in self.records if r["status"] == "OK")
        err = len(self.records) - ok
        print("\n" + "=" * 70)
        print(f"SUMMARY: {len(self.records)} calls | OK: {ok} | ERROR: {err}")
        print("=" * 70)
        for r in self.records:
            marker = "OK   " if r["status"] == "OK" else "ERROR"
            print(f"  [{marker}] {r['method']} ({r['elapsed_ms']} ms)")
        # Persist machine-readable results alongside the tests.
        _RESULTS_DIR.mkdir(parents=True, exist_ok=True)
        from datetime import datetime
        stamp = datetime.now().strftime("%Y%m%d_%H%M%S")
        out_path = _RESULTS_DIR / f"run_all_functions_{stamp}.json"
        out_path.write_text(
            json.dumps(self.records, indent=2, default=str, ensure_ascii=False),
            encoding="utf-8")
        print(f"  results written: {out_path}")
        return 0 if err == 0 else 1


# ── Tool exercises ───────────────────────────────────────────────────────────

def ex_discover_a2a_agents(proxy, runner: Runner) -> None:
    runner.call("discover_a2a_agents", {},
                lambda: proxy.discover_a2a_agents())
    runner.call("discover_a2a_agents(status=active)", {"status": "active"},
                lambda: proxy.discover_a2a_agents(status="active"))
    runner.call("discover_a2a_agents(agent_name=Hermes)",
                {"agent_name": "Hermes"},
                lambda: proxy.discover_a2a_agents(agent_name="Hermes"))
    saved = proxy.part_id
    try:
        proxy.part_id = "wrong-partition-probe"
        # part_id is read inside the call; set it then restore.
        runner.call("discover_a2a_agents(wrong part_id)", {},
                    lambda: proxy.discover_a2a_agents())
    finally:
        proxy.part_id = saved


def ex_get_a2a_agent(proxy, runner: Runner) -> None:
    runner.call("get_a2a_agent", {"agent_id": HERMES_AGENT_ID},
                lambda: proxy.get_a2a_agent(agent_id=HERMES_AGENT_ID))
    runner.call("get_a2a_agent(unknown)", {"agent_id": BOGUS_AGENT_ID},
                lambda: proxy.get_a2a_agent(agent_id=BOGUS_AGENT_ID))
    runner.call("get_a2a_agent(missing arg)", {},
                lambda: proxy.get_a2a_agent())


def ex_get_a2a_agent_card(proxy, runner: Runner) -> None:
    runner.call("get_a2a_agent_card", {},
                lambda: proxy.get_a2a_agent_card())
    runner.call("get_a2a_agent_card(extended=true)", {"extended": True},
                lambda: proxy.get_a2a_agent_card(extended=True))


def ex_send_a2a_message(proxy, runner: Runner) -> Dict[str, Any]:
    out = runner.call(
        "send_a2a_message",
        {"message": _message("Run-all-functions probe: reply with exactly "
                             "the word ACK and nothing else."),
         "agent_id": HERMES_AGENT_ID},
        lambda: proxy.send_a2a_message(
            **{"message": _message(
                "Run-all-functions probe: reply with exactly the word ACK "
                "and nothing else."),
               "agent_id": HERMES_AGENT_ID}))
    runner.call(
        "send_a2a_message(bogus agent)",
        {"message": _message("ping"), "agent_id": BOGUS_AGENT_ID},
        lambda: proxy.send_a2a_message(
            **{"message": _message("ping"), "agent_id": BOGUS_AGENT_ID}))
    return {} if _is_error(out) else (out or {})


def ex_send_a2a_message_stream(proxy, runner: Runner) -> None:
    runner.call(
        "send_a2a_message_stream",
        {"message": _message("Run-all-functions probe: reply with exactly "
                             "the word STREAMED and nothing else."),
         "agent_id": CORE_ENGINE_AGENT_ID},
        lambda: proxy.send_a2a_message_stream(
            **{"message": _message(
                "Run-all-functions probe: reply with exactly the word "
                "STREAMED and nothing else."),
               "agent_id": CORE_ENGINE_AGENT_ID}))


def ex_get_a2a_task(proxy, runner: Runner, task_id: Optional[str]) -> None:
    if task_id:
        # Poll until terminal so the state/result are meaningful.
        import time as _time
        last: Any = None
        waited = 0.0
        while waited < POLL_TIMEOUT:
            last = proxy.get_a2a_task(task_id=task_id)
            if _state_of(last) in TERMINAL_STATES:
                break
            _time.sleep(POLL_INTERVAL)
            waited += POLL_INTERVAL
        runner.records.append({
            "method": "get_a2a_task(poll)", "arguments": {"task_id": task_id},
            "status": "OK" if not _is_error(last) else "ERROR",
            "elapsed_ms": int(waited * 1000), "output": last, "error": None})
        print(f"    [OK] get_a2a_task(poll)")
        print(f"      out : {_scrub(json.dumps(last, default=str))[:400]}")
    runner.call("get_a2a_task(unknown)", {"task_id": BOGUS_TASK_ID},
                lambda: proxy.get_a2a_task(task_id=BOGUS_TASK_ID))
    runner.call("get_a2a_task(missing arg)", {},
                lambda: proxy.get_a2a_task())


def ex_list_a2a_tasks(proxy, runner: Runner) -> None:
    runner.call("list_a2a_tasks", {"page_size": 10},
                lambda: proxy.list_a2a_tasks(page_size=10))
    runner.call("list_a2a_tasks(status filter)", {"page_size": 10, "status":
                                                  "COMPLETED"},
                lambda: proxy.list_a2a_tasks(page_size=10, status="COMPLETED"))


def ex_cancel_a2a_task(proxy, runner: Runner) -> None:
    # Start a delegation then cancel it (falls back to the terminal-cancel
    # branch when the delegation completes too quickly).
    out = runner.call(
        "send_a2a_message(for cancel)",
        {"message": _message("Run-all-functions probe: count slowly from 1 "
                             "to 100, one number per sentence."),
         "agent_id": CORE_ENGINE_AGENT_ID},
        lambda: proxy.send_a2a_message(
            **{"message": _message(
                "Run-all-functions probe: count slowly from 1 to 100, one "
                "number per sentence."),
               "agent_id": CORE_ENGINE_AGENT_ID}))
    task_id = _extract_task_id(out)
    if task_id:
        runner.call("cancel_a2a_task", {"task_id": task_id},
                    lambda: proxy.cancel_a2a_task(task_id=task_id))
        runner.call("cancel_a2a_task(2nd, idempotent)", {"task_id": task_id},
                    lambda: proxy.cancel_a2a_task(task_id=task_id))
    else:
        runner.call("cancel_a2a_task(unknown)", {"task_id": BOGUS_TASK_ID},
                    lambda: proxy.cancel_a2a_task(task_id=BOGUS_TASK_ID))
    runner.call("cancel_a2a_task(missing arg)", {},
                lambda: proxy.cancel_a2a_task())


# ── Main ─────────────────────────────────────────────────────────────────────

def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--quick", action="store_true",
                        help="Read-only tools only (no delegations)")
    parser.add_argument("--tool", default=None,
                        help="Run a single tool by name (e.g. list_a2a_tasks)")
    args = parser.parse_args()

    runner = Runner()
    try:
        proxy = _build_proxy()
    except Exception as e:
        print(f"FATAL: cannot build proxy: {e}")
        traceback.print_exc()
        return 2

    print("=" * 70)
    print("MCP A2A Proxy — run all 8 tool functions directly")
    print(f"gateway={GATEWAY_BASE_URL} endpoint={ENDPOINT_ID} part={PART_ID}")
    print("=" * 70)

    task_id: Optional[str] = None
    try:
        ex_discover_a2a_agents(proxy, runner)
        ex_get_a2a_agent(proxy, runner)
        ex_get_a2a_agent_card(proxy, runner)

        if not args.quick:
            out = ex_send_a2a_message(proxy, runner)
            task_id = _extract_task_id(out)
            ex_send_a2a_message_stream(proxy, runner)

        ex_get_a2a_task(proxy, runner, task_id)
        ex_list_a2a_tasks(proxy, runner)
        if not args.quick:
            ex_cancel_a2a_task(proxy, runner)
    except Exception as e:
        print(f"FATAL during execution: {type(e).__name__}: {e}")
        traceback.print_exc()
        return 2

    return runner.summary()


if __name__ == "__main__":
    sys.exit(main())