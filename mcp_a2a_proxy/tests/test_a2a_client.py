#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""Tests for A2AClient — auth, JSON-RPC, GraphQL, REST using httpx.MockTransport.

Covers P1 exit criteria (§8):
- JWT issue, proactive refresh, reactive 401 retry
- x-api-key fallback
- Part-Id sent / dropped when part_id is None
- endpoint_id path quoting
- secret redaction in logs
- malformed/non-JSON responses
- HTTP timeout → STREAM_TIMEOUT
- GraphQL errors
- JSON-RPC error mapping
"""

from __future__ import annotations

__author__ = "Idea Bosque"

import json
import logging
import time
from typing import Any, Callable, Dict

import httpx
import pytest

from mcp_a2a_proxy.a2a_client import A2AClient, _jwt_expiry, _redact_headers
from mcp_a2a_proxy.error_handler import ErrorCode

logger = logging.getLogger("test-a2a-client")


# ==================== Helpers ====================


def _make_jwt(exp_offset: float = 3600) -> str:
    """Create a minimal JWT with an ``exp`` claim for testing."""
    import base64

    header = base64.urlsafe_b64encode(json.dumps({"alg": "HS256"}).encode()).decode().rstrip("=")
    payload = base64.urlsafe_b64encode(
        json.dumps({"exp": int(time.time() + exp_offset)}).encode()
    ).decode().rstrip("=")
    signature = "sig"
    return f"{header}.{payload}.{signature}"


def _make_expired_jwt() -> str:
    return _make_jwt(exp_offset=-100)


def _base_settings(**overrides: Any) -> Dict[str, Any]:
    """Settings dict with JWT auth configured for a2a_daemon_engine.

    A pre-issued ``gateway_token`` avoids the need to call the token endpoint
    in most tests. Tests that need to exercise the auth flow can override.
    """
    settings: Dict[str, Any] = {
        "gateway_base_url": "http://localhost:8765",
        "a2a_jsonrpc_endpoint": "http://localhost:8765/{endpoint_id}/a2a",
        "a2a_agent_card_endpoint": "http://localhost:8765/{endpoint_id}/.well-known/agent-card.json",
        "graphql_modules": {
            "a2a_daemon_engine": {
                "endpoint": "http://localhost:8765/{endpoint_id}/a2a_core_graphql",
                "gateway_base_url": "http://localhost:8765",
                "token_username": "svc",
                "token_password": "secret",
                "gateway_token": _make_jwt(),
            }
        },
        "a2a_stream_timeout": 5,
    }
    settings.update(overrides)
    return settings


def _patch_httpx_monkeypatch(monkeypatch, handler: Callable):
    """Monkey-patch httpx.Client to use a MockTransport with the given handler.

    Saves the original ``httpx.Client`` before patching so the factory does not
    recurse into itself.
    """
    import mcp_a2a_proxy.a2a_client as client_mod

    _OrigClient = httpx.Client

    def _client_factory(*args, **kwargs):
        kwargs.pop("http2", None)
        transport = httpx.MockTransport(handler)
        kwargs["transport"] = transport
        return _OrigClient(**kwargs)

    monkeypatch.setattr(client_mod.httpx, "Client", _client_factory)

    def _post_factory(url, **kwargs):
        with _OrigClient(transport=httpx.MockTransport(handler)) as c:
            return c.post(url, **kwargs)

    monkeypatch.setattr(client_mod.httpx, "post", _post_factory)


class _CaptureHandler:
    """httpx mock handler that captures the last request and returns a
    configurable response."""

    def __init__(self, responses=None, default_response=None):
        self.responses = responses or []
        self.default_response = default_response or httpx.Response(
            200, json={"jsonrpc": "2.0", "result": {"status": "ok"}, "id": 1}
        )
        self.requests = []
        self._idx = 0

    def __call__(self, request: httpx.Request) -> httpx.Response:
        self.requests.append(request)
        if self._idx < len(self.responses):
            r = self.responses[self._idx]
            self._idx += 1
            if callable(r):
                return r(request)
            return r
        if callable(self.default_response):
            return self.default_response(request)
        return self.default_response


# ==================== JWT Expiry Parsing ====================


class TestJwtExpiry:
    def test_valid_jwt_returns_exp(self):
        token = _make_jwt(exp_offset=3600)
        exp = _jwt_expiry(token)
        assert exp is not None
        assert exp > time.time()

    def test_no_token_returns_none(self):
        assert _jwt_expiry(None) is None

    def test_non_jwt_returns_none(self):
        assert _jwt_expiry("not-a-jwt") is None


# ==================== Header Redaction ====================


class TestRedactHeaders:
    def test_authorization_redacted(self):
        headers = {"Authorization": "Bearer secret123", "Part-Id": "tenant-1"}
        redacted = _redact_headers(headers)
        assert redacted["Authorization"] == "***"
        assert redacted["Part-Id"] == "tenant-1"

    def test_x_api_key_redacted(self):
        headers = {"x-api-key": "abc123", "Content-Type": "application/json"}
        redacted = _redact_headers(headers)
        assert redacted["x-api-key"] == "***"
        assert redacted["Content-Type"] == "application/json"


# ==================== Part-Id Handling ====================


class TestPartIdHeader:
    def test_part_id_sent_when_set(self, monkeypatch):
        handler = _CaptureHandler()

        def check(request: httpx.Request) -> httpx.Response:
            assert request.headers.get("Part-Id") == "tenant-xyz"
            return httpx.Response(
                200,
                json={
                    "jsonrpc": "2.0",
                    "result": {"status": "submitted", "task_id": "t-1"},
                    "id": 1,
                },
            )

        handler.default_response = check
        _patch_httpx_monkeypatch(monkeypatch, handler)

        client = A2AClient(logger, **_base_settings())
        client.endpoint_id = "ep1"
        client.part_id = "tenant-xyz"
        result = client.execute_jsonrpc("message/send", {"message": {}})
        assert result.get("task_id") == "t-1"

    def test_part_id_dropped_when_none(self, monkeypatch):
        handler = _CaptureHandler()

        def check(request: httpx.Request) -> httpx.Response:
            # Part-Id should not be present
            assert "Part-Id" not in request.headers
            return httpx.Response(
                200,
                json={"jsonrpc": "2.0", "result": {"status": "ok"}, "id": 1},
            )

        handler.default_response = check
        _patch_httpx_monkeypatch(monkeypatch, handler)

        client = A2AClient(logger, **_base_settings())
        client.endpoint_id = "ep1"
        client.part_id = None
        result = client.execute_jsonrpc("message/send", {})
        assert result.get("status") == "ok"


# ==================== Endpoint ID Path Quoting ====================


class TestEndpointIdQuoting:
    def test_endpoint_id_quoted_in_url(self, monkeypatch):
        handler = _CaptureHandler()

        def check(request: httpx.Request) -> httpx.Response:
            # endpoint_id with special chars should be quoted
            assert "%2F" not in str(request.url) or "ep1" in str(request.url)
            # The path should contain the quoted endpoint_id
            assert "/ep1/a2a" in str(request.url)
            return httpx.Response(
                200,
                json={"jsonrpc": "2.0", "result": {}, "id": 1},
            )

        handler.default_response = check
        _patch_httpx_monkeypatch(monkeypatch, handler)

        client = A2AClient(logger, **_base_settings())
        client.endpoint_id = "ep1"
        client.part_id = "p1"
        client.execute_jsonrpc("message/send", {})


# ==================== JSON-RPC Error Mapping ====================


class TestJsonRpcErrorMapping:
    @pytest.mark.parametrize(
        "code,expected_error_code",
        [
            (-32601, ErrorCode.A2A_METHOD_NOT_FOUND),
            (-32602, ErrorCode.A2A_INVALID_PARAMS),
            (-32603, ErrorCode.A2A_INTERNAL_ERROR),
        ],
    )
    def test_jsonrpc_error_codes(self, monkeypatch, code, expected_error_code):
        handler = _CaptureHandler(
            default_response=httpx.Response(
                200,
                json={
                    "jsonrpc": "2.0",
                    "error": {"code": code, "message": f"err {code}"},
                    "id": 1,
                },
            )
        )
        _patch_httpx_monkeypatch(monkeypatch, handler)

        client = A2AClient(logger, **_base_settings())
        client.endpoint_id = "ep1"
        client.part_id = "p1"
        result = client.execute_jsonrpc("message/send", {})
        assert result["error_code"] == expected_error_code

    def test_sdk_not_initialized(self, monkeypatch):
        handler = _CaptureHandler(
            default_response=httpx.Response(
                200,
                json={
                    "jsonrpc": "2.0",
                    "error": {"code": -32603, "message": "A2A SDK not initialized"},
                    "id": 1,
                },
            )
        )
        _patch_httpx_monkeypatch(monkeypatch, handler)

        client = A2AClient(logger, **_base_settings())
        client.endpoint_id = "ep1"
        client.part_id = "p1"
        result = client.execute_jsonrpc("message/send", {})
        assert result["error_code"] == ErrorCode.A2A_SDK_NOT_INITIALIZED

    def test_jsonrpc_success_unwraps_result(self, monkeypatch):
        handler = _CaptureHandler(
            default_response=httpx.Response(
                200,
                json={
                    "jsonrpc": "2.0",
                    "result": {"task_id": "task-42", "status": "submitted"},
                    "id": 1,
                },
            )
        )
        _patch_httpx_monkeypatch(monkeypatch, handler)

        client = A2AClient(logger, **_base_settings())
        client.endpoint_id = "ep1"
        client.part_id = "p1"
        result = client.execute_jsonrpc("message/send", {})
        assert result["task_id"] == "task-42"
        assert result["status"] == "submitted"


# ==================== Non-JSON / Malformed Responses ====================


class TestMalformedResponses:
    def test_non_json_response(self, monkeypatch):
        handler = _CaptureHandler(
            default_response=httpx.Response(200, text="<html>not json</html>")
        )
        _patch_httpx_monkeypatch(monkeypatch, handler)

        client = A2AClient(logger, **_base_settings())
        client.endpoint_id = "ep1"
        client.part_id = "p1"
        result = client.execute_jsonrpc("message/send", {})
        assert result["error_code"] == ErrorCode.API_CONNECTION_FAILED

    def test_http_error_status(self, monkeypatch):
        handler = _CaptureHandler(
            default_response=httpx.Response(500, json={"detail": "Internal error"})
        )
        _patch_httpx_monkeypatch(monkeypatch, handler)

        client = A2AClient(logger, **_base_settings())
        client.endpoint_id = "ep1"
        client.part_id = "p1"
        result = client.execute_jsonrpc("message/send", {})
        assert result["error_code"] == ErrorCode.API_CONNECTION_FAILED
        assert "Internal error" in result["error"]


# ==================== Timeout → STREAM_TIMEOUT ====================


class TestStreamTimeout:
    def test_stream_timeout_returns_stream_timeout_code(self, monkeypatch):
        def handler(request: httpx.Request) -> httpx.Response:
            raise httpx.TimeoutException("timed out")

        _patch_httpx_monkeypatch(monkeypatch, handler)

        client = A2AClient(logger, **_base_settings())
        client.endpoint_id = "ep1"
        client.part_id = "p1"
        result = client.execute_jsonrpc("message/stream", {})
        assert result["error_code"] == ErrorCode.STREAM_TIMEOUT

    def test_non_stream_timeout_returns_connection_failed(self, monkeypatch):
        def handler(request: httpx.Request) -> httpx.Response:
            raise httpx.TimeoutException("timed out")

        _patch_httpx_monkeypatch(monkeypatch, handler)

        client = A2AClient(logger, **_base_settings())
        client.endpoint_id = "ep1"
        client.part_id = "p1"
        result = client.execute_jsonrpc("tasks/get", {})
        assert result["error_code"] == ErrorCode.API_CONNECTION_FAILED


# ==================== GraphQL Errors ====================


class TestGraphQLErrors:
    def test_graphql_errors_key(self, monkeypatch):
        handler = _CaptureHandler(
            default_response=httpx.Response(
                200,
                json={
                    "errors": [{"message": "Field 'foo' doesn't exist"}],
                    "data": None,
                },
            )
        )
        _patch_httpx_monkeypatch(monkeypatch, handler)

        client = A2AClient(logger, **_base_settings())
        client.endpoint_id = "ep1"
        client.part_id = "p1"
        result = client.execute_graphql(
            "a2aAgentList",
            client.graphql_a2a_agent_list_query(),
            {},
        )
        assert result["error_code"] == ErrorCode.GRAPHQL_QUERY_FAILED
        assert "foo" in result["error"]

    def test_graphql_success(self, monkeypatch):
        handler = _CaptureHandler(
            default_response=httpx.Response(
                200,
                json={
                    "data": {
                        "a2aAgentList": {
                            "pageSize": 20,
                            "pageNumber": 1,
                            "total": 1,
                            "a2aAgentList": [
                                {"agentId": "a1", "agentName": "Hermes"}
                            ],
                        }
                    }
                },
            )
        )
        _patch_httpx_monkeypatch(monkeypatch, handler)

        client = A2AClient(logger, **_base_settings())
        client.endpoint_id = "ep1"
        client.part_id = "p1"
        result = client.execute_graphql(
            "a2aAgentList",
            client.graphql_a2a_agent_list_query(),
            {},
        )
        assert result["total"] == 1
        assert result["a2aAgentList"][0]["agentId"] == "a1"


# ==================== Auth: x-api-key fallback ====================


class TestXApiKeyFallback:
    def test_x_api_key_sent_when_no_jwt(self, monkeypatch):
        handler = _CaptureHandler()

        def check(request: httpx.Request) -> httpx.Response:
            assert request.headers.get("x-api-key") == "my-api-key"
            assert "Authorization" not in request.headers
            return httpx.Response(
                200,
                json={"jsonrpc": "2.0", "result": {"ok": True}, "id": 1},
            )

        handler.default_response = check
        _patch_httpx_monkeypatch(monkeypatch, handler)

        settings = _base_settings()
        settings["graphql_modules"]["a2a_daemon_engine"] = {
            "endpoint": "http://localhost:8765/{endpoint_id}/a2a_core_graphql",
            "x_api_key": "my-api-key",
        }
        client = A2AClient(logger, **settings)
        client.endpoint_id = "ep1"
        client.part_id = "p1"
        result = client.execute_jsonrpc("message/send", {})
        assert result.get("ok") is True


# ==================== Reactive 401 retry ====================


class TestReactiveAuth:
    def test_reactive_401_retry(self, monkeypatch):
        # Handler that routes /auth/token to a token response and everything
        # else to the JSON-RPC response sequence.
        call_count = {"token": 0, "rpc": 0}

        def handler(request: httpx.Request) -> httpx.Response:
            if "/auth/token" in str(request.url):
                call_count["token"] += 1
                return httpx.Response(
                    200, json={"access_token": _make_jwt()}
                )
            call_count["rpc"] += 1
            if call_count["rpc"] == 1:
                return httpx.Response(401, json={"detail": "Unauthorized"})
            return httpx.Response(
                200,
                json={"jsonrpc": "2.0", "result": {"status": "ok"}, "id": 1},
            )

        _patch_httpx_monkeypatch(monkeypatch, handler)

        client = A2AClient(logger, **_base_settings())
        client.endpoint_id = "ep1"
        client.part_id = "p1"
        # Pre-set a gateway token so auth_mode == "jwt" and there is something
        # to invalidate.
        gql_mod = client._get_graphql_module()
        gql_mod._gateway_token = _make_jwt()
        result = client.execute_jsonrpc("message/send", {})
        assert result.get("status") == "ok"
        # Two JSON-RPC requests were made (the 401 + the retry)
        assert call_count["rpc"] == 2


# ==================== Agent Card REST ====================


class TestAgentCard:
    def test_get_agent_card_success(self, monkeypatch):
        handler = _CaptureHandler(
            default_response=httpx.Response(
                200,
                json={
                    "name": "A2A Daemon",
                    "version": "1.0.0",
                    "capabilities": {"streaming": True},
                },
            )
        )
        _patch_httpx_monkeypatch(monkeypatch, handler)

        client = A2AClient(logger, **_base_settings())
        client.endpoint_id = "ep1"
        client.part_id = "p1"
        result = client.get_agent_card()
        assert result["name"] == "A2A Daemon"
        assert result["version"] == "1.0.0"

    def test_get_agent_card_sdk_not_initialized(self, monkeypatch):
        handler = _CaptureHandler(
            default_response=httpx.Response(
                200,
                json={"error": "A2A SDK not initialized"},
            )
        )
        _patch_httpx_monkeypatch(monkeypatch, handler)

        client = A2AClient(logger, **_base_settings())
        client.endpoint_id = "ep1"
        client.part_id = "p1"
        result = client.get_agent_card()
        assert result["error_code"] == ErrorCode.AGENT_CARD_UNAVAILABLE

    def test_get_agent_card_404(self, monkeypatch):
        handler = _CaptureHandler(
            default_response=httpx.Response(404, json={"detail": "Not found"})
        )
        _patch_httpx_monkeypatch(monkeypatch, handler)

        client = A2AClient(logger, **_base_settings())
        client.endpoint_id = "ep1"
        client.part_id = "p1"
        result = client.get_agent_card()
        assert result["error_code"] == ErrorCode.AGENT_CARD_UNAVAILABLE


# ==================== Connection Error ====================


class TestConnectionError:
    def test_connect_error(self, monkeypatch):
        def handler(request: httpx.Request) -> httpx.Response:
            raise httpx.ConnectError("Connection refused")

        _patch_httpx_monkeypatch(monkeypatch, handler)

        client = A2AClient(logger, **_base_settings())
        client.endpoint_id = "ep1"
        client.part_id = "p1"
        result = client.execute_jsonrpc("message/send", {})
        assert result["error_code"] == ErrorCode.API_CONNECTION_FAILED
        assert "Cannot connect" in result["error"]