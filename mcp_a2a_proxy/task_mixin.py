#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""Task tracking mixin — 3 tools for monitoring and abandoning delegated work.

| Tool             | Method          | Notes                                    |
|------------------|-----------------|------------------------------------------|
| get_a2a_task     | ``tasks/get``    | Status, result, history                  |
| list_a2a_tasks   | ``tasks/list``   | Recover in-flight work after context loss |
| cancel_a2a_task  | ``tasks/cancel`` | Abandon work; **cascades to child tasks** |

``resubscribe_a2a_task`` is deferred to 0.2.0 — ``get_a2a_task`` with
``history_length`` covers every recovery case a delegating agent has.
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


class TaskMixin(A2ABackedProcessor):
    """Task tracking tools: get status, list in-flight, cancel."""

    @handle_errors(operation_name="get_a2a_task")
    def get_a2a_task(self, **arguments: Dict[str, Any]) -> Dict[str, Any] | str:
        """Get the current state, result, artifacts, and optionally history of
        an A2A task via ``tasks/get``.

        Args:
            task_id (required): The task identifier.
            history_length (optional): When set, include the last N messages
                in the task's history — use this to read a peer's question
                when the task state is ``input_required`` (§5.4).

        ``input_required`` is a non-terminal state: the peer is waiting on you.
        Read the question from history, then call ``send_a2a_message`` with the
        same ``task_id`` to resume the task.
        """
        task_id = arguments.get("task_id")
        validate_not_empty(task_id, "task_id")

        params: Dict[str, Any] = {"id": task_id}
        if arguments.get("history_length") is not None:
            params["historyLength"] = arguments["history_length"]

        result = self._execute_jsonrpc("tasks/get", params)
        if error := propagate_error_if_present(result):
            return error

        return humps.decamelize(convert_decimal_to_number(result))

    @handle_errors(operation_name="list_a2a_tasks")
    def list_a2a_tasks(self, **arguments: Dict[str, Any]) -> Dict[str, Any] | str:
        """List delegated A2A tasks via ``tasks/list`` — the recovery tool after
        context loss or a stream timeout.

        Args:
            page_size (optional): Maximum number of tasks per page.
            page_token (optional): Opaque pagination token from a prior call.
            status (optional): Filter by task status.
            priority (optional): Filter by priority.
            task_type (optional): Filter by task type.
            assigned_agent_id (optional): Filter by assigned agent.
        """
        params: Dict[str, Any] = {}
        if arguments.get("page_size") is not None:
            params["pageSize"] = arguments["page_size"]
        if arguments.get("page_token"):
            params["pageToken"] = arguments["page_token"]
        if arguments.get("status"):
            params["status"] = arguments["status"]
        if arguments.get("priority"):
            params["priority"] = arguments["priority"]
        if arguments.get("task_type"):
            params["type"] = arguments["task_type"]
        if arguments.get("assigned_agent_id"):
            params["assignedAgentId"] = arguments["assigned_agent_id"]

        result = self._execute_jsonrpc("tasks/list", params)
        if error := propagate_error_if_present(result):
            return error

        data = humps.decamelize(convert_decimal_to_number(result))
        if isinstance(data, dict):
            tasks = data.get("tasks")
            if tasks is not None and len(tasks) == 0:
                return "No A2A tasks found matching this query."
        return data

    @handle_errors(operation_name="cancel_a2a_task")
    def cancel_a2a_task(self, **arguments: Dict[str, Any]) -> Dict[str, Any] | str:
        """Cancel an in-flight A2A task via ``tasks/cancel`` — abandon work.

        Args:
            task_id (required): The task to cancel.

        **Cascades to child tasks** — the daemon's cancellation tree cancels the
        delegation subtree and notifies each agent. Scoped to the caller's
        partition. Task rows survive with a ``cancelled`` status; the effect
        is reversible by re-delegating.

        A second cancel on an already-terminal task is idempotent or returns
        ``TASK_ALREADY_TERMINAL``.
        """
        task_id = arguments.get("task_id")
        validate_not_empty(task_id, "task_id")

        params: Dict[str, Any] = {"id": task_id}

        result = self._execute_jsonrpc("tasks/cancel", params)
        if error := propagate_error_if_present(result):
            return error

        return humps.decamelize(convert_decimal_to_number(result))