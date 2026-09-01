#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""MCP Configuration for A2A Proxy — 8 tools across three domains (§5).

One loop: **discover → delegate → track → abandon.**

| Area     | Tools                                    |
|----------|------------------------------------------|
| Discovery | 3 (discover_a2a_agents, get_a2a_agent, get_a2a_agent_card) |
| Delegation | 2 (send_a2a_message, send_a2a_message_stream) |
| Task tracking | 3 (get_a2a_task, list_a2a_tasks, cancel_a2a_task) |
| **Total** | **8** |
"""

from __future__ import annotations

__author__ = "Idea Bosque"

# MCP Configuration
MCP_CONFIGURATION = {
    "tools": [
        # ---- Discovery Tools (3) ----
        {
            "name": "discover_a2a_agents",
            "description": (
                "Find registered A2A agents (peers) — who exists. "
                "Optional filters: agent_name (substring match), status "
                "(default 'active', uses the status-index GSI), page_number "
                "(default 1), limit (default 20). "
                "The 'capabilities' field arrives as a JSON string from the "
                "daemon and is parsed to a list when present; when it is NULL "
                "the peer is still usable — absence does not mean incapable. "
                "Discovery results are cached by the daemon, so a newly "
                "registered peer may not appear immediately — an empty list "
                "is not proof of absence."
            ),
            "inputSchema": {
                "type": "object",
                "properties": {
                    "agent_name": {
                        "type": "string",
                        "description": "Filter by agent name (substring match)",
                    },
                    "status": {
                        "type": "string",
                        "description": "Filter by status (active, inactive, error)",
                        "default": "active",
                    },
                    "page_number": {
                        "type": "integer",
                        "description": "Page number for pagination",
                        "default": 1,
                    },
                    "limit": {
                        "type": "integer",
                        "description": "Maximum number of results per page",
                        "default": 20,
                    },
                },
                "required": [],
            },
            "annotations": None,
        },
        {
            "name": "get_a2a_agent",
            "description": (
                "Get one peer agent's registry record by agent_id — its name, "
                "status, endpoint URL, backend metadata, and capabilities. "
                "No delegation-time validation depends on this tool; it is an "
                "optional aid for reading a peer's metadata before trusting it. "
                "The 'capabilities' field is parsed from a JSON string to a "
                "list when present."
            ),
            "inputSchema": {
                "type": "object",
                "properties": {
                    "agent_id": {
                        "type": "string",
                        "description": "The agent's unique identifier",
                    },
                },
                "required": ["agent_id"],
            },
            "annotations": None,
        },
        {
            "name": "get_a2a_agent_card",
            "description": (
                "Get this daemon's own agent card — skills, input/output modes, "
                "protocol version. This is daemon-level: one card per daemon, "
                "not one per registered agent. Takes no agent_id in 0.1.0; "
                "fetching remote peers' cards is deferred to 0.2.0. "
                "When extended=true, uses the auth-gated extended card."
            ),
            "inputSchema": {
                "type": "object",
                "properties": {
                    "extended": {
                        "type": "boolean",
                        "description": (
                            "If true, fetch the auth-gated extended agent card "
                            "via JSON-RPC agent/getAuthenticatedExtendedCard. "
                            "Default false — REST GET .well-known/agent-card.json."
                        ),
                        "default": False,
                    },
                },
                "required": [],
            },
            "annotations": None,
        },
        # ---- Delegation Tools (2) ----
        {
            "name": "send_a2a_message",
            "description": (
                "Delegate work to an A2A peer agent via message/send. "
                "Returns the A2A response (decamelized) — typically a task_id "
                "and initial state. "
                "agent_id is optional and never pre-validated by the proxy — "
                "omit it to use the daemon's default agent. "
                "This is also the follow-up call in a multi-turn exchange: "
                "passing the same task_id / context_id resumes an existing "
                "input_required task. input_required is non-terminal — the "
                "peer is waiting on you; call get_a2a_task to read the question, "
                "then call send_a2a_message again with the same task_id to resume. "
                "Flow: discover_a2a_agents → get_a2a_agent_card (optional) → "
                "send_a2a_message → get_a2a_task."
            ),
            "inputSchema": {
                "type": "object",
                "properties": {
                    "message": {
                        "type": "object",
                        "description": (
                            "A2A message object with protocol field names intact: "
                            '{"role": "user", "parts": [{"kind": "text", "text": "..."}]}'
                        ),
                    },
                    "agent_id": {
                        "type": "string",
                        "description": (
                            "Target agent ID. Passed through as metadata for "
                            "daemon routing — never pre-validated by the proxy. "
                            "Omit to use the daemon's default agent."
                        ),
                    },
                    "task_id": {
                        "type": "string",
                        "description": (
                            "Sent as taskId — resume an existing task in a "
                            "multi-turn exchange (input_required follow-up)."
                        ),
                    },
                    "context_id": {
                        "type": "string",
                        "description": (
                            "Sent as contextId — resume an existing task context."
                        ),
                    },
                    "metadata": {
                        "type": "object",
                        "description": "Additional routing metadata.",
                    },
                    "thread_id": {
                        "type": "string",
                        "description": (
                            "Promoted by the daemon into ServerCallContext.state "
                            "as thread_uuid."
                        ),
                    },
                    "run_id": {
                        "type": "string",
                        "description": (
                            "Promoted by the daemon into ServerCallContext.state "
                            "as run_uuid."
                        ),
                    },
                },
                "required": ["message"],
            },
            "annotations": None,
        },
        {
            "name": "send_a2a_message_stream",
            "description": (
                "Delegate work to an A2A peer agent via message/stream, "
                "returning the full ordered event list — task creation, status "
                "transitions, artifact updates, final message. "
                "Returns {status: 'streaming_complete', events_emitted, events[]}. "
                "Bounded by a2a_stream_timeout (330s default). "
                "On timeout, returns STREAM_TIMEOUT and does NOT auto-cancel "
                "the task — the peer may still be running. Recover with "
                "list_a2a_tasks / get_a2a_task or cancel_a2a_task deliberately. "
                "Same args and multi-turn semantics as send_a2a_message."
            ),
            "inputSchema": {
                "type": "object",
                "properties": {
                    "message": {
                        "type": "object",
                        "description": (
                            "A2A message object with protocol field names intact: "
                            '{"role": "user", "parts": [{"kind": "text", "text": "..."}]}'
                        ),
                    },
                    "agent_id": {
                        "type": "string",
                        "description": (
                            "Target agent ID. Passed through, never pre-validated. "
                            "Omit for the daemon default."
                        ),
                    },
                    "task_id": {
                        "type": "string",
                        "description": "Resume an existing task (taskId).",
                    },
                    "context_id": {
                        "type": "string",
                        "description": "Resume an existing context (contextId).",
                    },
                    "metadata": {
                        "type": "object",
                        "description": "Additional routing metadata.",
                    },
                    "thread_id": {
                        "type": "string",
                        "description": "Daemon routing (thread_uuid).",
                    },
                    "run_id": {
                        "type": "string",
                        "description": "Daemon routing (run_uuid).",
                    },
                },
                "required": ["message"],
            },
            "annotations": None,
        },
        # ---- Task Tracking Tools (3) ----
        {
            "name": "get_a2a_task",
            "description": (
                "Get the current state, result, artifacts, and optionally "
                "history of an A2A task via tasks/get. "
                "Use history_length to read a peer's question when the task "
                "state is input_required (non-terminal — the peer is waiting "
                "on you). Then call send_a2a_message with the same task_id to "
                "resume the task."
            ),
            "inputSchema": {
                "type": "object",
                "properties": {
                    "task_id": {
                        "type": "string",
                        "description": "The task identifier.",
                    },
                    "history_length": {
                        "type": "integer",
                        "description": (
                            "Include the last N messages in the task's history "
                            "— use to read a peer's question when "
                            "input_required."
                        ),
                    },
                },
                "required": ["task_id"],
            },
            "annotations": None,
        },
        {
            "name": "list_a2a_tasks",
            "description": (
                "List delegated A2A tasks via tasks/list — the recovery tool "
                "after context loss or a stream timeout. "
                "Supports filtering by status, priority, task_type, and "
                "assigned_agent_id. Use page_token for pagination."
            ),
            "inputSchema": {
                "type": "object",
                "properties": {
                    "page_size": {
                        "type": "integer",
                        "description": "Maximum number of tasks per page.",
                    },
                    "page_token": {
                        "type": "string",
                        "description": "Opaque pagination token from a prior call.",
                    },
                    "status": {
                        "type": "string",
                        "description": "Filter by task status.",
                    },
                    "priority": {
                        "type": "string",
                        "description": "Filter by priority.",
                    },
                    "task_type": {
                        "type": "string",
                        "description": "Filter by task type.",
                    },
                    "assigned_agent_id": {
                        "type": "string",
                        "description": "Filter by assigned agent.",
                    },
                },
                "required": [],
            },
            "annotations": None,
        },
        {
            "name": "cancel_a2a_task",
            "description": (
                "Cancel an in-flight A2A task via tasks/cancel — abandon work. "
                "Cascades to child tasks: the daemon's cancellation tree cancels "
                "the delegation subtree and notifies each agent. Scoped to the "
                "caller's partition. Task rows survive with a cancelled status; "
                "the effect is reversible by re-delegating. "
                "A second cancel on an already-terminal task is idempotent or "
                "returns TASK_ALREADY_TERMINAL."
            ),
            "inputSchema": {
                "type": "object",
                "properties": {
                    "task_id": {
                        "type": "string",
                        "description": "The task to cancel.",
                    },
                },
                "required": ["task_id"],
            },
            "annotations": None,
        },
    ],
    "resources": [],
    "prompts": [],
    "module_links": [
        # Discovery
        {
            "type": "tool",
            "name": "discover_a2a_agents",
            "module_name": "mcp_a2a_proxy",
            "class_name": "MCPA2AProxy",
            "function_name": "discover_a2a_agents",
            "return_type": "text",
        },
        {
            "type": "tool",
            "name": "get_a2a_agent",
            "module_name": "mcp_a2a_proxy",
            "class_name": "MCPA2AProxy",
            "function_name": "get_a2a_agent",
            "return_type": "text",
        },
        {
            "type": "tool",
            "name": "get_a2a_agent_card",
            "module_name": "mcp_a2a_proxy",
            "class_name": "MCPA2AProxy",
            "function_name": "get_a2a_agent_card",
            "return_type": "text",
        },
        # Delegation
        {
            "type": "tool",
            "name": "send_a2a_message",
            "module_name": "mcp_a2a_proxy",
            "class_name": "MCPA2AProxy",
            "function_name": "send_a2a_message",
            "return_type": "text",
        },
        {
            "type": "tool",
            "name": "send_a2a_message_stream",
            "module_name": "mcp_a2a_proxy",
            "class_name": "MCPA2AProxy",
            "function_name": "send_a2a_message_stream",
            "return_type": "text",
        },
        # Task tracking
        {
            "type": "tool",
            "name": "get_a2a_task",
            "module_name": "mcp_a2a_proxy",
            "class_name": "MCPA2AProxy",
            "function_name": "get_a2a_task",
            "return_type": "text",
        },
        {
            "type": "tool",
            "name": "list_a2a_tasks",
            "module_name": "mcp_a2a_proxy",
            "class_name": "MCPA2AProxy",
            "function_name": "list_a2a_tasks",
            "return_type": "text",
        },
        {
            "type": "tool",
            "name": "cancel_a2a_task",
            "module_name": "mcp_a2a_proxy",
            "class_name": "MCPA2AProxy",
            "function_name": "cancel_a2a_task",
            "return_type": "text",
        },
    ],
    "modules": [
        {
            "package_name": "mcp_a2a_proxy",
            "module_name": "mcp_a2a_proxy",
            "class_name": "MCPA2AProxy",
            # Default settings consumed by MCPA2AProxy / A2AClient.
            # Override per-deployment (e.g. via environment-specific config).
            "setting": {
                # Gateway base URL — the proxy always talks to its own daemon
                # through the silvaengine_gateway (§1).
                "gateway_base_url": "http://localhost:8765",
                # JSON-RPC endpoint (§3.1): POST /{endpoint_id}/a2a.
                "a2a_jsonrpc_endpoint": "http://localhost:8765/{endpoint_id}/a2a",
                # Agent card REST endpoint (§3.2):
                # GET /{endpoint_id}/.well-known/agent-card.json.
                "a2a_agent_card_endpoint": (
                    "http://localhost:8765/{endpoint_id}/.well-known/agent-card.json"
                ),
                # GraphQL modules (§3.3): two read queries only.
                "graphql_modules": {
                    "a2a_daemon_engine": {
                        "endpoint": "http://localhost:8765/{endpoint_id}/a2a_core_graphql",
                        # silvaengine_gateway JWT Bearer auth (per module): the
                        # client logs in at {gateway_base_url}/auth/token with
                        # token_username/token_password and sends
                        # "Authorization: Bearer ***". A pre-issued gateway_token
                        # skips the login. x_api_key is the fallback AWS API
                        # Gateway auth, used only when no JWT auth is configured.
                        "gateway_base_url": "http://localhost:8765",
                        "token_username": "svc",
                        "token_password": "replace-me",
                        "gateway_token": None,
                        "x_api_key": None,
                    }
                },
                # Stream timeout (§6.1): 300s (Hermes/OpenClaw handler default)
                # + 30s margin = 330. NOT 120 — that bounds only the core-engine
                # bridge, not the peer handlers this module delegates to.
                "a2a_stream_timeout": 330,
                "default_page_limit": 20,
            },
        }
    ],
}