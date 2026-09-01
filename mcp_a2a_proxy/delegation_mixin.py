#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""Delegation mixin — 2 tools for delegating work to A2A peer agents.

| Tool                     | Method           | Notes                             |
|--------------------------|------------------|-----------------------------------|
| send_a2a_message          | ``message/send`` | Delegate work                     |
| send_a2a_message_stream   | ``message/stream``| Delegate, collecting all events   |

Per §5.1a, ``agent_id`` is optional and never pre-validated by the proxy — it
passes straight through to the daemon, which resolves it. Omitting
``agent_id`` uses the daemon's default agent.

Per §5.4, ``send_a2a_message`` is also the follow-up call in a multi-turn
exchange: passing the same ``task_id`` / ``context_id`` resumes an existing
``input_required`` task instead of starting a new one. ``input_required`` is a
non-terminal state — the peer is waiting on the caller.
"""

from __future__ import annotations

__author__ = "Idea Bosque"

from typing import Any, Dict

import humps
from silvaengine_utility import convert_decimal_to_number

from .a2a_backed_processor import A2ABackedProcessor
from .error_handler import (
    handle_errors,
    propagate_error_if_present,
    validate_not_empty,
)


class DelegationMixin(A2ABackedProcessor):
    """Delegation tools: send a message to a peer agent, optionally streaming."""

    @handle_errors(operation_name="send_a2a_message")
    def send_a2a_message(self, **arguments: Dict[str, Any]) -> Dict[str, Any] | str:
        """Delegate work to an A2A peer agent via ``message/send``.

        Returns the A2A ``SendMessageResponse`` (decamelized) — typically
        carrying a ``task_id`` and initial ``state``.

        Args:
            message (required): An A2A message object with protocol field
                names intact::

                    {"role": "user", "parts": [{"kind": "text", "text": "..."}]}

            agent_id (optional): Passed through as metadata for daemon routing.
                Never pre-validated by the proxy (§5.1a). Omit to use the
                daemon's default agent.
            task_id (optional): Send as ``taskId`` — resumes an existing task
                in a multi-turn exchange (§5.4).
            context_id (optional): Send as ``contextId`` — resumes an existing
                task context.
            metadata (optional): Additional routing metadata.
            thread_id (optional): Promoted by the daemon into
                ``ServerCallContext.state`` as ``thread_uuid``.
            run_id (optional): Promoted by the daemon into
                ``ServerCallContext.state`` as ``run_uuid``.

        If the returned state is ``input_required``, the peer is waiting on
        you — call ``get_a2a_task`` to read the question, then call
        ``send_a2a_message`` again with the same ``task_id`` to resume.
        ``input_required`` is non-terminal.
        """
        message = arguments.get("message")
        validate_not_empty(message, "message")

        params: Dict[str, Any] = {
            "message": message,
        }
        # agent_id is optional — no pre-flight validation (§5.1a).
        if arguments.get("agent_id"):
            params["agentId"] = arguments["agent_id"]
        # Protocol field names: taskId / contextId (camelCase in JSON-RPC).
        if arguments.get("task_id"):
            params["taskId"] = arguments["task_id"]
        if arguments.get("context_id"):
            params["contextId"] = arguments["context_id"]
        if arguments.get("metadata"):
            params["metadata"] = arguments["metadata"]
        if arguments.get("thread_id"):
            params["threadId"] = arguments["thread_id"]
        if arguments.get("run_id"):
            params["runId"] = arguments["run_id"]

        result = self._execute_jsonrpc("message/send", params)
        if error := propagate_error_if_present(result):
            return error

        return humps.decamelize(convert_decimal_to_number(result))

    @handle_errors(operation_name="send_a2a_message_stream")
    def send_a2a_message_stream(self, **arguments: Dict[str, Any]) -> Dict[str, Any] | str:
        """Delegate work to an A2A peer agent via ``message/stream``, collecting
        the full ordered event list.

        Returns ``{status: "streaming_complete", events_emitted, events[]}`` —
        task creation, status transitions, artifact updates, and the final
        message in order. Bounded by ``a2a_stream_timeout`` (330s default,
        §6.1).

        Same args as ``send_a2a_message``. On timeout, returns
        ``STREAM_TIMEOUT`` and does NOT auto-cancel the task — the peer may
        still be running. Recover with ``list_a2a_tasks`` / ``get_a2a_task``
        or call ``cancel_a2a_task`` deliberately.

        If the returned state is ``input_required``, the peer is waiting on
        you — call ``get_a2a_task`` to read the question, then call
        ``send_a2a_message`` (or ``send_a2a_message_stream``) with the same
        ``task_id`` to resume. ``input_required`` is non-terminal.
        """
        message = arguments.get("message")
        validate_not_empty(message, "message")

        params: Dict[str, Any] = {
            "message": message,
        }
        if arguments.get("agent_id"):
            params["agentId"] = arguments["agent_id"]
        if arguments.get("task_id"):
            params["taskId"] = arguments["task_id"]
        if arguments.get("context_id"):
            params["contextId"] = arguments["context_id"]
        if arguments.get("metadata"):
            params["metadata"] = arguments["metadata"]
        if arguments.get("thread_id"):
            params["threadId"] = arguments["thread_id"]
        if arguments.get("run_id"):
            params["runId"] = arguments["run_id"]

        result = self._execute_jsonrpc(
            "message/stream",
            params,
            timeout=self.setting.get("a2a_stream_timeout", 330),
        )
        if error := propagate_error_if_present(result):
            return error

        return humps.decamelize(convert_decimal_to_number(result))