#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""Discovery mixin — 3 tools for finding and inspecting A2A peer agents.

| Tool                  | Call                                    |
|-----------------------|-----------------------------------------|
| discover_a2a_agents   | GraphQL ``a2aAgentList``                |
| get_a2a_agent         | GraphQL ``a2aAgent``                    |
| get_a2a_agent_card    | REST GET or JSON-RPC ``extended=true``   |

Per §5.1, ``capabilities`` is a JSON string from the daemon and is parsed to a
list when present, falling back to the raw string on parse failure. Discovery
results are cached by the daemon (``method_cache(ttl=…)``), so a freshly
registered peer may not appear immediately.
"""

from __future__ import annotations

__author__ = "Idea Bosque"

import json
import re
from typing import Any, Dict

import humps
from silvaengine_utility import convert_decimal_to_number

from .a2a_backed_processor import A2ABackedProcessor
from .error_handler import (
    handle_errors,
    is_empty_result,
    propagate_error_if_present,
    validate_not_empty,
)


def _parse_capabilities(data: Any) -> Any:
    """Parse the ``capabilities`` field from a JSON string to a list (§5.1).

    The daemon stores capabilities as ``UnicodeAttribute()`` and writes it with
    ``json_dumps(...)``; the read path never reverses it, so it arrives as a
    string like ``'["chat","streaming"]'``. Parse it; if parsing fails, keep the
    raw string. When ``data`` is a dict, operate on its ``capabilities`` key
    in-place.
    """
    if isinstance(data, dict):
        cap = data.get("capabilities")
        if isinstance(cap, str):
            try:
                data["capabilities"] = json.loads(cap)
            except (json.JSONDecodeError, ValueError):
                pass  # keep the raw string
        return data
    if isinstance(data, list):
        for item in data:
            _parse_capabilities(item)
        return data
    return data


# Keys inside registry ``metadata`` that carry credentialed values (DEF-001).
# The daemon returns agent metadata verbatim; the proxy is the surface handing
# it to the calling model, so credential-shaped values are redacted here
# (presence preserved — the model still sees that a key exists).
_SENSITIVE_METADATA_KEY = re.compile(
    r"(api_key|apikey|secret|password|token)", re.IGNORECASE
)
_REDACTED_SUFFIX_LENGTH = 4


def _redact_metadata(value: Any) -> Any:
    """Recursively redact credential-shaped values in agent metadata (DEF-001).

    Registry metadata carries per-agent handler configuration which can
    include secrets (``hermes_api_key``, ``openclaw_api_key``,
    ``core_engine_token``, ...). The calling model has no business reading
    them; any key matching the sensitive pattern has its value replaced with a
    short ``…<last-4>`` marker. Non-string scalars are fully redacted.
    """
    if isinstance(value, dict):
        redacted: Dict[str, Any] = {}
        for key, item in value.items():
            if _SENSITIVE_METADATA_KEY.search(str(key)):
                redacted[key] = _redact_sensitive_value(item)
            else:
                redacted[key] = _redact_metadata(item)
        return redacted
    if isinstance(value, list):
        return [_redact_metadata(item) for item in value]
    return value


def _redact_sensitive_value(value: Any) -> Any:
    if value is None or isinstance(value, (bool, int, float)):
        return "[REDACTED]"
    text = str(value)
    if len(text) <= _REDACTED_SUFFIX_LENGTH:
        return "[REDACTED]"
    return f"[REDACTED]…{text[-_REDACTED_SUFFIX_LENGTH:]}"


class DiscoveryMixin(A2ABackedProcessor):
    """Discovery tools: find peers, read one peer's record, read the daemon card."""

    @handle_errors(operation_name="discover_a2a_agents")
    def discover_a2a_agents(self, **arguments: Dict[str, Any]) -> Dict[str, Any] | str:
        """Find registered A2A agents (peers) — who exists.

        Optional filters: ``agent_name`` (substring match), ``status`` (default
        ``"active"``, runs through the ``status-index`` GSI), ``page_number``
        (default 1), ``limit`` (default 20).

        The ``capabilities`` field arrives as a JSON string from the daemon and
        is parsed to a list when present. Discovery results are cached by the
        daemon — a newly registered peer may not appear immediately, so an
        empty list is not proof of absence.
        """
        variables = {
            "agentName": arguments.get("agent_name"),
            "status": arguments.get("status"),          # schema default: "active"
            "pageNumber": arguments.get("page_number"),  # schema default: 1
            "limit": arguments.get("limit"),            # schema default: 20
        }
        variables = {k: v for k, v in variables.items() if v is not None and v != ""}

        result = self._execute_graphql_query(
            "a2a_daemon_engine",
            "a2aAgentList",
            "Query",
            variables,
        )
        if error := propagate_error_if_present(result):
            return error

        data = humps.decamelize(convert_decimal_to_number(result))
        if is_empty_result(data.get("a2a_agent_list")):
            return "No A2A agents found matching this query."

        # Parse capabilities JSON strings → lists (§5.1), then redact
        # credential-shaped metadata values (DEF-001).
        agents = data.get("a2a_agent_list", [])
        _parse_capabilities(agents)
        for agent in agents:
            if isinstance(agent, dict) and agent.get("metadata"):
                agent["metadata"] = _redact_metadata(agent["metadata"])
        return data

    @handle_errors(operation_name="get_a2a_agent")
    def get_a2a_agent(self, **arguments: Dict[str, Any]) -> Dict[str, Any] | str:
        """Get one peer agent's registry record by ``agent_id``.

        No delegation-time validation depends on this tool — it is an optional
        aid for reading a peer's metadata before trusting it.
        """
        agent_id = arguments.get("agent_id")
        validate_not_empty(agent_id, "agent_id")

        variables = {"agentId": agent_id}

        result = self._execute_graphql_query(
            "a2a_daemon_engine",
            "a2aAgent",
            "Query",
            variables,
        )
        if error := propagate_error_if_present(result):
            return error

        data = humps.decamelize(convert_decimal_to_number(result))
        if is_empty_result(data):
            return f"No A2A agent found with agent_id '{agent_id}'."

        # Parse capabilities JSON string → list (§5.1), redact credential
        # metadata (DEF-001).
        _parse_capabilities(data)
        if isinstance(data, dict) and data.get("metadata"):
            data["metadata"] = _redact_metadata(data["metadata"])
        return data

    @handle_errors(operation_name="get_a2a_agent_card")
    def get_a2a_agent_card(self, **arguments: Dict[str, Any]) -> Dict[str, Any] | str:
        """Get this daemon's own agent card — skills, input/output modes,
        protocol version.

        Takes no ``agent_id`` in 0.1.0: the card is daemon-level (one card per
        daemon, describing the daemon itself — not one per registered agent).
        Fetching remote peers' cards is deferred to 0.2.0.

        When ``extended=true``, uses the JSON-RPC ``getAuthenticatedExtendedCard``
        method (auth-gated). Otherwise, REST GET
        ``/{endpoint_id}/.well-known/agent-card.json``.
        """
        extended = arguments.get("extended", False)
        result = self.a2a_client.get_agent_card(extended=bool(extended))

        if isinstance(result, dict) and "error" in result:
            return result

        return result