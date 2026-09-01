#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
A2A Client for MCP A2A Proxy.

Three call styles over shared auth and error mapping (§4 of the development plan):

| Style    | Used by          | Endpoint                                         |
|----------|------------------|---------------------------------------------------|
| JSON-RPC | delegation, task | {gateway_base_url}/{endpoint_id}/a2a              |
| GraphQL  | discovery        | {gateway_base_url}/{endpoint_id}/a2a_core_graphql |
| REST GET | agent card       | {gateway_base_url}/{endpoint_id}/.well-known/agent-card.json |

Auth is ported from ``mcp_hospirfq_processor/graphql_client.py``: best-effort
JWT ``exp`` parse, 60 s proactive-refresh skew, ``POST {gateway_base_url}/auth/token``
password grant, one reactive re-auth-and-retry on 401/403. Falls back to
``x-api-key`` when no JWT is configured. Headers on every call: ``Authorization``
or ``x-api-key``, plus ``Part-Id`` and ``Content-Type``, with ``None`` values
dropped.
"""

from __future__ import annotations

__author__ = "Idea Bosque"

import base64
import json
import logging
import time
import traceback
from typing import Any, Dict, Optional
from urllib.parse import quote

import httpx

from .error_handler import (
    ErrorCode,
    build_error_response,
    extract_error_message,
)

# Seconds of safety margin before a JWT's ``exp`` at which the client proactively
# re-authenticates, so a token never expires mid-request.
_TOKEN_EXPIRY_SKEW_SECONDS = 60

# Hand-written GraphQL documents (§4 — only two read queries are needed;
# generating them would require importing a2a_daemon_engine for no benefit).

_GRAPHQL_A2A_AGENT_LIST = """
query A2AAgentList($pageNumber: Int, $limit: Int, $status: String, $agentName: String) {
  a2aAgentList(pageNumber: $pageNumber, limit: $limit, status: $status, agentName: $agentName) {
    pageSize
    pageNumber
    total
    a2aAgentList {
      partitionKey
      agentId
      endpointId
      partId
      agentName
      capabilities
      endpointUrl
      status
      metadata
      updatedBy
      createdAt
      updatedAt
    }
  }
}
"""

_GRAPHQL_A2A_AGENT = """
query A2AAgent($agentId: String!) {
  a2aAgent(agentId: $agentId) {
    partitionKey
    agentId
    endpointId
    partId
    agentName
    capabilities
    endpointUrl
    status
    metadata
    updatedBy
    createdAt
    updatedAt
  }
}
"""


def _jwt_expiry(token: str | None) -> float | None:
    """Best-effort parse of a JWT's ``exp`` claim (epoch seconds).

    Reads the payload segment only (no signature verification — the client is
    the token bearer, not the validator). Returns ``None`` when the token is
    absent, not a JWT, or has no ``exp`` claim, in which case the caller falls
    back to reactive 401 re-authentication.
    """
    if not token:
        return None
    try:
        payload_b64 = token.split(".")[1]
        payload_b64 += "=" * (-len(payload_b64) % 4)  # restore base64 padding
        payload = json.loads(base64.urlsafe_b64decode(payload_b64))
        exp = payload.get("exp")
        return float(exp) if exp is not None else None
    except Exception:
        return None


def _redact_headers(
    headers: Dict[str, str], safe_keys: frozenset[str] | None = None
) -> Dict[str, str]:
    """Return a copy of *headers* with secret values redacted for logging."""
    if safe_keys is None:
        safe_keys = frozenset({"Part-Id", "Content-Type"})
    redacted: Dict[str, str] = {}
    for k, v in headers.items():
        if k in safe_keys:
            redacted[k] = v
        elif v:
            redacted[k] = "***"
        else:
            redacted[k] = v
    return redacted


# ==================== Module (auth + endpoint) ====================


class A2AGraphQLModule:
    """Encapsulates GraphQL module configuration and auth (port of
    ``mcp_hospirfq_processor/graphql_client.py:GraphQLModule``).

    Auth is configured per module (under ``graphql_modules.<module_name>``):
    a module authenticates with an AWS API Gateway ``x_api_key``, or with a
    silvaengine_gateway JWT Bearer token (``gateway_base_url`` +
    ``token_username``/``token_password``, or a pre-issued ``gateway_token``).
    """

    def __init__(
        self,
        endpoint_id: str,
        module_name: str | None = None,
        class_name: str | None = None,
        endpoint: str | None = None,
        x_api_key: str | None = None,
        gateway_base_url: str | None = None,
        token_username: str | None = None,
        token_password: str | None = None,
        gateway_token: str | None = None,
    ):
        self.endpoint_id = endpoint_id
        self._module_name = module_name
        self._class_name = class_name
        # The endpoint template references only ``{endpoint_id}``. part_id is
        # never part of the URL path — it is carried in the ``Part-Id`` request
        # header.
        self._endpoint = (
            endpoint.format(endpoint_id=endpoint_id) if endpoint else None
        )
        self._x_api_key = x_api_key
        # Per-module gateway JWT Bearer auth.
        self._gateway_base_url = gateway_base_url
        self._token_username = token_username
        self._token_password = token_password
        self._gateway_token = gateway_token
        # Cached JWT expiry (epoch seconds) for proactive refresh; parsed from
        # the token. ``None`` -> unknown, rely on reactive 401 retry.
        self._gateway_token_exp = _jwt_expiry(self._gateway_token)

    @property
    def module_name(self) -> str | None:
        return self._module_name

    @property
    def class_name(self) -> str | None:
        return self._class_name

    @property
    def endpoint(self) -> str | None:
        return self._endpoint

    @property
    def x_api_key(self) -> str | None:
        return self._x_api_key

    @property
    def gateway_base_url(self) -> str | None:
        return self._gateway_base_url

    @property
    def auth_mode(self) -> str:
        """``"jwt"``, ``"api_key"``, or ``"none"`` for this module."""
        if self._gateway_token or (self._token_username and self._token_password):
            return "jwt"
        if self._x_api_key:
            return "api_key"
        return "none"

    def _token_expired(self) -> bool:
        """True when the cached JWT is at/near its ``exp`` (within the skew).

        Unknown expiry (unparseable token) returns ``False`` so the token is
        still used and the reactive 401 retry remains the safety net.
        """
        if self._gateway_token_exp is None:
            return False
        return time.time() >= (self._gateway_token_exp - _TOKEN_EXPIRY_SKEW_SECONDS)

    def get_gateway_token(self) -> str | None:
        """Obtain (or reuse) this module's JWT Bearer token for the gateway.

        Returns ``None`` when JWT auth is not configured for this module (the
        caller then falls back to ``x_api_key``). A cached token is reused until
        it nears its ``exp`` claim, then proactively re-issued so it never
        expires mid-request (long-lived processes such as the MCP daemon).
        """
        if self._gateway_token and not self._token_expired():
            return self._gateway_token
        if not (self._token_username and self._token_password):
            # No credentials to (re)authenticate — return what we have (possibly
            # a pre-issued token) rather than nothing.
            return self._gateway_token
        if not self._gateway_base_url:
            return self._gateway_token

        resp = httpx.post(
            f"{self._gateway_base_url.rstrip('/')}/auth/token",
            data={
                "username": self._token_username,
                "password": self._token_password,
            },
            timeout=15,
        )
        resp.raise_for_status()
        self._gateway_token = resp.json()["access_token"]
        self._gateway_token_exp = _jwt_expiry(self._gateway_token)
        return self._gateway_token

    def invalidate_gateway_token(self) -> None:
        """Drop the cached JWT so the next call re-authenticates (reactive 401)."""
        self._gateway_token = None
        self._gateway_token_exp = None


# ==================== Client ====================


# JSON-RPC error code → ErrorCode mapping (§4, §7).
_JSONRPC_ERROR_MAP = {
    -32601: ErrorCode.A2A_METHOD_NOT_FOUND,
    -32602: ErrorCode.A2A_INVALID_PARAMS,
    -32603: ErrorCode.A2A_INTERNAL_ERROR,
}


class A2AClient:
    """Client for A2A protocol calls (JSON-RPC), GraphQL discovery, and
    REST agent-card retrieval, all via the silvaengine_gateway.

    The client holds ``endpoint_id`` and ``part_id`` (assigned by the MCP host
    from the consuming side's ``partition_key``). Three URL templates are
    resolved at construction time from the settings dict:

    - ``a2a_jsonrpc_endpoint`` → ``{gateway_base_url}/{endpoint_id}/a2a``
    - ``a2a_agent_card_endpoint`` → ``{gateway_base_url}/{endpoint_id}/.well-known/agent-card.json``
    - ``graphql_modules.a2a_daemon_engine.endpoint`` → ``{gateway_base_url}/{endpoint_id}/a2a_core_graphql``

    ``endpoint_id`` is URL-quoted as a single path segment before substitution
    (never concatenated raw into a path). ``part_id`` goes in the ``Part-Id``
    header and is dropped when ``None``.
    """

    def __init__(self, logger: logging.Logger, **setting: Dict[str, Any]):
        self.logger = logger
        self.setting = setting
        self._endpoint_id: str | None = None
        self._part_id: str | None = None
        self._graphql_module: A2AGraphQLModule | None = None
        # JSON-RPC request id — monotonic counter (deterministic in tests).
        self._rpc_id = 0
        # Endpoint templates (§6).
        self._jsonrpc_endpoint_template: str | None = setting.get(
            "a2a_jsonrpc_endpoint"
        )
        self._agent_card_endpoint_template: str | None = setting.get(
            "a2a_agent_card_endpoint"
        )
        # Stream timeout (§6.1 — 330s default: 300s peer handler + 30s margin).
        self._stream_timeout: float = float(setting.get("a2a_stream_timeout", 330))
        # Default page limit for GraphQL discovery queries.
        self._default_page_limit: int = int(setting.get("default_page_limit", 20))

    # ---- host contract: endpoint_id / part_id ----

    @property
    def endpoint_id(self) -> str | None:
        return self._endpoint_id

    @endpoint_id.setter
    def endpoint_id(self, value: str):
        self._endpoint_id = value
        # Re-resolve the GraphQL module so its endpoint picks up the new
        # endpoint_id.
        if self._graphql_module is not None:
            self._graphql_module = None

    @property
    def part_id(self) -> str | None:
        return self._part_id

    @part_id.setter
    def part_id(self, value: str):
        self._part_id = value

    # ---- endpoint URL resolution ----

    def _quote_endpoint_id(self) -> str:
        """URL-quote endpoint_id as a single path segment (§4)."""
        return quote(self._endpoint_id or "", safe="")

    def _resolve_jsonrpc_endpoint(self) -> str:
        """Build the JSON-RPC endpoint URL with the current endpoint_id."""
        if self._jsonrpc_endpoint_template:
            return self._jsonrpc_endpoint_template.format(
                endpoint_id=self._quote_endpoint_id()
            )
        # Fallback: construct from gateway_base_url.
        base = self.setting.get("gateway_base_url", "http://localhost:8765")
        return f"{base.rstrip('/')}/{self._quote_endpoint_id()}/a2a"

    def _resolve_agent_card_endpoint(self) -> str:
        """Build the agent-card REST endpoint URL with the current endpoint_id."""
        if self._agent_card_endpoint_template:
            return self._agent_card_endpoint_template.format(
                endpoint_id=self._quote_endpoint_id()
            )
        base = self.setting.get("gateway_base_url", "http://localhost:8765")
        return f"{base.rstrip('/')}/{self._quote_endpoint_id()}/.well-known/agent-card.json"

    def _get_graphql_module(self) -> A2AGraphQLModule:
        """Get (lazy-create) the GraphQL module for ``a2a_daemon_engine``."""
        if self._graphql_module is None:
            module_setting = (
                self.setting.get("graphql_modules", {}).get("a2a_daemon_engine", {})
                or {}
            )
            self._graphql_module = A2AGraphQLModule(
                endpoint_id=self._quote_endpoint_id(),
                module_name="a2a_daemon_engine",
                class_name=module_setting.get("class_name"),
                endpoint=module_setting.get("endpoint"),
                x_api_key=module_setting.get("x_api_key"),
                gateway_base_url=module_setting.get("gateway_base_url"),
                token_username=module_setting.get("token_username"),
                token_password=module_setting.get("token_password"),
                gateway_token=module_setting.get("gateway_token"),
            )
        return self._graphql_module

    # ---- header building ----

    def _build_headers(
        self,
        graphql_module: A2AGraphQLModule,
        token: str | None,
        extra: Dict[str, str] | None = None,
    ) -> Dict[str, str]:
        """Build request headers: Authorization or x-api-key + Part-Id +
        Content-Type, dropping None values (§4)."""
        if token:
            hdrs: Dict[str, str] = {
                "Authorization": f"Bearer {token}",
                "Part-Id": self.part_id,
                "Content-Type": "application/json",
            }
        else:
            hdrs = {
                "x-api-key": graphql_module.x_api_key,
                "Part-Id": self.part_id,
                "Content-Type": "application/json",
            }
        if extra:
            hdrs.update(extra)
        # httpx rejects None-valued headers with a TypeError. Drop any that
        # came out None so the request goes through cleanly. The common case
        # is Part-Id when the tool is invoked via /{endpoint_id}/mcp instead
        # of /{endpoint_id}/{part_id}/mcp; x-api-key can also be None when
        # neither Bearer nor API-key auth is configured.
        return {k: v for k, v in hdrs.items() if v is not None}

    # ---- JSON-RPC ----

    def _next_rpc_id(self) -> int:
        self._rpc_id += 1
        return self._rpc_id

    def execute_jsonrpc(
        self,
        method: str,
        params: Dict[str, Any],
        timeout: float | None = None,
    ) -> Dict[str, Any]:
        """Execute a JSON-RPC 2.0 call against ``/{endpoint_id}/a2a``.

        The envelope (``jsonrpc``, ``id``) is built by the client. Success
        unwraps ``result``; ``error`` maps to :func:`build_error_response` so no
        tool raises. Returns a dict: either the unwrapped ``result`` or an
        error-response dict.
        """
        graphql_module = self._get_graphql_module()
        endpoint = self._resolve_jsonrpc_endpoint()
        rpc_id = self._next_rpc_id()
        payload = json.dumps(
            {
                "jsonrpc": "2.0",
                "method": method,
                "params": params,
                "id": rpc_id,
            }
        )

        request_timeout = httpx.Timeout(
            timeout or 60.0, connect=15.0
        )

        try:
            token = graphql_module.get_gateway_token()
            headers = self._build_headers(graphql_module, token)
            self.logger.debug(
                f"JSON-RPC {method} → {endpoint} | headers={_redact_headers(headers)}"
            )

            with httpx.Client(http2=True, timeout=request_timeout) as client:
                response = client.post(endpoint, headers=headers, content=payload)

                # Reactive re-auth on 401/403 for JWT mode (one retry).
                if response.status_code in (401, 403) and (
                    graphql_module.auth_mode == "jwt"
                ):
                    graphql_module.invalidate_gateway_token()
                    token = graphql_module.get_gateway_token()
                    headers = self._build_headers(graphql_module, token)
                    self.logger.debug(
                        f"JSON-RPC retry {method} → {endpoint} | "
                        f"headers={_redact_headers(headers)}"
                    )
                    response = client.post(endpoint, headers=headers, content=payload)

            return self._parse_jsonrpc_response(response, method)

        except httpx.TimeoutException:
            # A stream timeout returns STREAM_TIMEOUT and does NOT auto-cancel
            # the task (§4). The peer may still be running inside the daemon.
            self.logger.error(f"Timeout calling JSON-RPC {method}")
            error_code = (
                ErrorCode.STREAM_TIMEOUT
                if method == "message/stream"
                else ErrorCode.API_CONNECTION_FAILED
            )
            return build_error_response(
                f"Request timed out calling {method}",
                error_code,
                {"method": method},
            )
        except httpx.ConnectError as e:
            self.logger.error(f"Connection failed calling JSON-RPC {method}: {e}")
            return build_error_response(
                f"Cannot connect to A2A daemon: {e}",
                ErrorCode.API_CONNECTION_FAILED,
                {"method": method},
            )
        except Exception as e:
            log = traceback.format_exc()
            self.logger.error(log)
            return build_error_response(
                extract_error_message(str(e)),
                ErrorCode.UNKNOWN_ERROR,
                {"method": method},
            )

    def _parse_jsonrpc_response(
        self, response: httpx.Response, method: str
    ) -> Dict[str, Any]:
        """Parse an HTTP response into a JSON-RPC result or error dict.

        Handles: non-JSON responses, HTTP error status codes, JSON-RPC
        ``error`` objects (mapped via :data:`_JSONRPC_ERROR_MAP`), and the
        special ``"A2A SDK not initialized"`` message →
        ``A2A_SDK_NOT_INITIALIZED`` (§7).
        """
        # Non-JSON / empty body — treat as a transport error.
        try:
            body = response.json()
        except Exception:
            return build_error_response(
                f"Non-JSON response from A2A daemon (HTTP {response.status_code})",
                ErrorCode.API_CONNECTION_FAILED,
                {"method": method, "status_code": response.status_code},
            )

        # HTTP-level errors (non-2xx) that still returned JSON.
        if response.status_code >= 400:
            message = (
                body.get("detail")
                or body.get("message")
                or f"HTTP {response.status_code}"
            )
            return build_error_response(
                message,
                ErrorCode.API_CONNECTION_FAILED,
                {"method": method, "status_code": response.status_code},
            )

        # JSON-RPC envelope.
        if isinstance(body, dict):
            if "error" in body:
                err = body["error"]
                code = err.get("code", -32603) if isinstance(err, dict) else -32603
                message = (
                    err.get("message", "Unknown JSON-RPC error")
                    if isinstance(err, dict)
                    else str(err)
                )
                # Special-case the "SDK not initialized" message (§7).
                if "not initialized" in message.lower():
                    mapped_code = ErrorCode.A2A_SDK_NOT_INITIALIZED
                else:
                    mapped_code = _JSONRPC_ERROR_MAP.get(
                        code, ErrorCode.A2A_INTERNAL_ERROR
                    )
                return build_error_response(message, mapped_code, {"method": method})

            if "result" in body:
                result = body["result"]
                return result if isinstance(result, dict) else {"result": result}

        # Fallback — unexpected shape.
        return body if isinstance(body, dict) else {"result": body}

    # ---- GraphQL ----

    def execute_graphql(
        self,
        operation_name: str,
        query: str,
        variables: Dict[str, Any],
    ) -> Dict[str, Any]:
        """Execute a hand-written GraphQL query against the
        ``a2a_daemon_engine`` module's endpoint.

        Returns the ``data.<operation_name>`` payload, or an error-response
        dict on failure.
        """
        graphql_module = self._get_graphql_module()
        if graphql_module.endpoint is None:
            return build_error_response(
                "GraphQL endpoint not configured for a2a_daemon_engine",
                ErrorCode.API_CONNECTION_FAILED,
                {"operation": operation_name},
            )

        payload = json.dumps({"query": query, "variables": variables})

        try:
            token = graphql_module.get_gateway_token()
            headers = self._build_headers(graphql_module, token)
            self.logger.debug(
                f"GraphQL {operation_name} → {graphql_module.endpoint} | "
                f"headers={_redact_headers(headers)}"
            )

            timeout = httpx.Timeout(60.0, connect=15.0)
            with httpx.Client(http2=True, timeout=timeout) as client:
                response = client.post(
                    graphql_module.endpoint, headers=headers, content=payload
                )

                # Reactive re-auth on 401/403 for JWT mode (one retry).
                if response.status_code in (401, 403) and (
                    graphql_module.auth_mode == "jwt"
                ):
                    graphql_module.invalidate_gateway_token()
                    token = graphql_module.get_gateway_token()
                    headers = self._build_headers(graphql_module, token)
                    self.logger.debug(
                        f"GraphQL retry {operation_name} → {graphql_module.endpoint} | "
                        f"headers={_redact_headers(headers)}"
                    )
                    response = client.post(
                        graphql_module.endpoint, headers=headers, content=payload
                    )

            return self._parse_graphql_response(response, operation_name)

        except httpx.TimeoutException:
            self.logger.error(f"Timeout calling GraphQL {operation_name}")
            return build_error_response(
                f"Request timed out calling GraphQL {operation_name}",
                ErrorCode.API_CONNECTION_FAILED,
                {"operation": operation_name},
            )
        except httpx.ConnectError as e:
            self.logger.error(f"Connection failed calling GraphQL {operation_name}: {e}")
            return build_error_response(
                f"Cannot connect to gateway: {e}",
                ErrorCode.API_CONNECTION_FAILED,
                {"operation": operation_name},
            )
        except Exception as e:
            log = traceback.format_exc()
            self.logger.error(log)
            return build_error_response(
                extract_error_message(str(e)),
                ErrorCode.GRAPHQL_QUERY_FAILED,
                {"operation": operation_name},
            )

    def _parse_graphql_response(
        self, response: httpx.Response, operation_name: str
    ) -> Dict[str, Any]:
        """Parse a GraphQL HTTP response into a data payload or error dict."""
        try:
            body = response.json()
        except Exception:
            return build_error_response(
                f"Non-JSON response from GraphQL endpoint (HTTP {response.status_code})",
                ErrorCode.GRAPHQL_QUERY_FAILED,
                {"operation": operation_name, "status_code": response.status_code},
            )

        if response.status_code >= 400:
            message = (
                body.get("detail")
                or body.get("message")
                or f"HTTP {response.status_code}"
            )
            return build_error_response(
                message,
                ErrorCode.API_CONNECTION_FAILED,
                {"operation": operation_name, "status_code": response.status_code},
            )

        if isinstance(body, dict) and "errors" in body:
            error_message = (
                body["errors"][0].get("message", "GraphQL error")
                if body["errors"]
                else "GraphQL error"
            )
            return build_error_response(
                error_message,
                ErrorCode.GRAPHQL_QUERY_FAILED,
                {"operation": operation_name},
            )

        data = body.get("data", {}) if isinstance(body, dict) else {}
        return data.get(operation_name) or {}

    # ---- REST (agent card) ----

    def get_agent_card(self, extended: bool = False) -> Dict[str, Any]:
        """GET the daemon's agent card from
        ``/{endpoint_id}/.well-known/agent-card.json``.

        When ``extended=True``, uses JSON-RPC ``agent/getAuthenticatedExtendedCard``
        instead (auth-gated). Returns the card dict or an error-response dict.
        """
        if extended:
            return self.execute_jsonrpc(
                "agent/getAuthenticatedExtendedCard",
                {},
            )

        graphql_module = self._get_graphql_module()
        endpoint = self._resolve_agent_card_endpoint()

        try:
            token = graphql_module.get_gateway_token()
            headers = self._build_headers(
                graphql_module, token, extra={"Accept": "application/json"}
            )
            # For GET we don't want Content-Type: application/json (no body).
            headers.pop("Content-Type", None)
            self.logger.debug(
                f"GET agent card → {endpoint} | headers={_redact_headers(headers)}"
            )

            timeout = httpx.Timeout(30.0, connect=15.0)
            with httpx.Client(http2=True, timeout=timeout) as client:
                response = client.get(endpoint, headers=headers)

                # Reactive re-auth on 401/403 for JWT mode (one retry).
                if response.status_code in (401, 403) and (
                    graphql_module.auth_mode == "jwt"
                ):
                    graphql_module.invalidate_gateway_token()
                    token = graphql_module.get_gateway_token()
                    headers = self._build_headers(
                        graphql_module, token, extra={"Accept": "application/json"}
                    )
                    headers.pop("Content-Type", None)
                    self.logger.debug(
                        f"GET agent card retry → {endpoint} | "
                        f"headers={_redact_headers(headers)}"
                    )
                    response = client.get(endpoint, headers=headers)

            return self._parse_agent_card_response(response)

        except httpx.TimeoutException:
            self.logger.error("Timeout fetching agent card")
            return build_error_response(
                "Request timed out fetching agent card",
                ErrorCode.API_CONNECTION_FAILED,
            )
        except httpx.ConnectError as e:
            self.logger.error(f"Connection failed fetching agent card: {e}")
            return build_error_response(
                f"Cannot connect to gateway: {e}",
                ErrorCode.API_CONNECTION_FAILED,
            )
        except Exception as e:
            log = traceback.format_exc()
            self.logger.error(log)
            return build_error_response(
                extract_error_message(str(e)),
                ErrorCode.UNKNOWN_ERROR,
            )

    def _parse_agent_card_response(self, response: httpx.Response) -> Dict[str, Any]:
        """Parse the agent-card REST response."""
        try:
            body = response.json()
        except Exception:
            return build_error_response(
                f"Non-JSON response from agent card endpoint (HTTP {response.status_code})",
                ErrorCode.AGENT_CARD_UNAVAILABLE,
                {"status_code": response.status_code},
            )

        if response.status_code >= 400:
            message = (
                body.get("detail")
                or body.get("message")
                or f"HTTP {response.status_code}"
            )
            return build_error_response(
                message,
                ErrorCode.AGENT_CARD_UNAVAILABLE,
                {"status_code": response.status_code},
            )

        # The daemon returns {"error": "A2A SDK not initialized", ...}
        # when the SDK module failed to load (main.py:378-382).
        if isinstance(body, dict) and "error" in body and "name" not in body:
            message = body.get("error", "Agent card unavailable")
            return build_error_response(
                message,
                ErrorCode.AGENT_CARD_UNAVAILABLE,
            )

        return body if isinstance(body, dict) else {"result": body}

    # ---- GraphQL documents (exposed for mixins) ----

    @staticmethod
    def graphql_a2a_agent_list_query() -> str:
        return _GRAPHQL_A2A_AGENT_LIST

    @staticmethod
    def graphql_a2a_agent_query() -> str:
        return _GRAPHQL_A2A_AGENT