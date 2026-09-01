#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
Centralized error handling module for MCP A2A Proxy.

Ported from ``mcp_hospirfq_processor/error_handler.py`` with A2A-specific
error codes covering transport, JSON-RPC, and domain failures (§7 of the
development plan).
"""

from __future__ import annotations

__author__ = "Idea Bosque"

import re
import traceback
from functools import wraps
from typing import Any, Callable, Dict, Optional


# ==================== Error Code Constants ====================


class ErrorCode:
    """Error codes for programmatic error handling."""

    # Transport
    API_CONNECTION_FAILED = "API_CONNECTION_FAILED"
    AUTH_FAILED = "AUTH_FAILED"
    GRAPHQL_QUERY_FAILED = "GRAPHQL_QUERY_FAILED"

    # JSON-RPC protocol errors (mapped from JSON-RPC error codes)
    A2A_METHOD_NOT_FOUND = "A2A_METHOD_NOT_FOUND"        # -32601
    A2A_INVALID_PARAMS = "A2A_INVALID_PARAMS"            # -32602
    A2A_INTERNAL_ERROR = "A2A_INTERNAL_ERROR"            # -32603
    A2A_SDK_NOT_INITIALIZED = "A2A_SDK_NOT_INITIALIZED"

    # Domain errors
    AGENT_NOT_FOUND = "AGENT_NOT_FOUND"
    TASK_NOT_FOUND = "TASK_NOT_FOUND"
    TASK_ALREADY_TERMINAL = "TASK_ALREADY_TERMINAL"
    AGENT_CARD_UNAVAILABLE = "AGENT_CARD_UNAVAILABLE"
    STREAM_TIMEOUT = "STREAM_TIMEOUT"

    # Validation
    VALIDATION_FAILED = "VALIDATION_FAILED"
    INVALID_ARGUMENTS = "INVALID_ARGUMENTS"
    MISSING_REQUIRED_FIELD = "MISSING_REQUIRED_FIELD"

    # General
    UNKNOWN_ERROR = "UNKNOWN_ERROR"
    OPERATION_FAILED = "OPERATION_FAILED"


# ==================== Custom Exception Classes ====================


class MCPError(Exception):
    """Base exception class for MCP A2A Proxy errors."""

    def __init__(
        self,
        message: str,
        error_code: str = ErrorCode.UNKNOWN_ERROR,
        details: Optional[Dict[str, Any]] = None,
    ):
        super().__init__(message)
        self.message = message
        self.error_code = error_code
        self.details = details or {}


class GraphQLError(MCPError):
    """Exception raised for GraphQL-related errors."""

    def __init__(
        self,
        message: str,
        error_code: str = ErrorCode.GRAPHQL_QUERY_FAILED,
        details: Optional[Dict[str, Any]] = None,
    ):
        super().__init__(message, error_code, details)


class ValidationError(MCPError):
    """Exception raised for validation errors."""

    def __init__(
        self,
        message: str,
        error_code: str = ErrorCode.VALIDATION_FAILED,
        details: Optional[Dict[str, Any]] = None,
    ):
        super().__init__(message, error_code, details)


# ==================== Error Message Extraction ====================


def extract_error_message(error_str: str) -> str:
    """Extract clean error message from GraphQL error response."""
    try:
        message_match = re.search(r"'message':\s*\"([^\"]+)\"", error_str)
        if not message_match:
            message_match = re.search(r"'message':\s*'([^']+)'", error_str)
        if message_match:
            return message_match.group(1)
        return str(error_str)
    except Exception:
        return str(error_str)


# ==================== Error Response Builders ====================


def build_error_response(
    message: str,
    error_code: str = ErrorCode.UNKNOWN_ERROR,
    details: Optional[Dict[str, Any]] = None,
    include_code: bool = True,
) -> Dict[str, Any]:
    """Build standardized error response dictionary."""
    response = {"error": message}
    if include_code:
        response["error_code"] = error_code
    if details:
        response["details"] = details
    return response


def build_error_from_exception(
    exception: Exception, include_code: bool = True
) -> Dict[str, Any]:
    """Build error response from an exception instance."""
    if isinstance(exception, MCPError):
        return build_error_response(
            message=exception.message,
            error_code=exception.error_code,
            details=exception.details,
            include_code=include_code,
        )
    else:
        clean_message = extract_error_message(str(exception))
        return build_error_response(
            message=clean_message,
            error_code=ErrorCode.UNKNOWN_ERROR,
            include_code=include_code,
        )


# ==================== Error Handler Decorator ====================


def handle_errors(
    operation_name: str, log_traceback: bool = False, include_error_code: bool = True
) -> Callable:
    """Decorator for consistent error handling across MCP methods."""

    def decorator(func: Callable) -> Callable:
        @wraps(func)
        def wrapper(self, *args, **kwargs) -> Dict[str, Any]:
            try:
                result = func(self, *args, **kwargs)
                if isinstance(result, dict) and "error" in result:
                    if include_error_code and "error_code" not in result:
                        result["error_code"] = ErrorCode.OPERATION_FAILED
                return result
            except MCPError as e:
                if log_traceback:
                    log = traceback.format_exc()
                    self.logger.error(log)
                else:
                    self.logger.error(f"Failed to {operation_name}: {e.message}")
                return build_error_from_exception(e, include_error_code)
            except Exception as e:
                if log_traceback:
                    log = traceback.format_exc()
                    self.logger.error(log)
                else:
                    self.logger.error(f"Failed to {operation_name}: {e}")
                return build_error_from_exception(e, include_error_code)

        return wrapper

    return decorator


# ==================== Validation Utilities ====================


def validate_not_empty(
    value: Any, field_name: str, error_message: Optional[str] = None
) -> None:
    """Validate that a value is not empty. Raises ValidationError otherwise."""
    if not value:
        message = error_message or f"{field_name} cannot be empty"
        raise ValidationError(
            message=message,
            error_code=ErrorCode.VALIDATION_FAILED,
            details={"field": field_name},
        )


def propagate_error_if_present(result: Dict[str, Any]) -> Optional[Dict[str, Any]]:
    """Check if result contains an error and return it if present."""
    if isinstance(result, dict) and "error" in result:
        return result
    return None


def is_empty_result(result: Any) -> bool:
    """True when a query result carries no data.

    Treats as empty: ``None``; an empty string; an empty list/tuple; an empty
    dict; and a list-envelope dict whose ``*_list`` values are all empty (e.g.
    ``{"total": 0, "item_list": []}``). A single-object result (a non-empty dict
    with no ``*_list`` keys) is NOT empty.
    """
    if result is None:
        return True
    if isinstance(result, str):
        return result.strip() == ""
    if isinstance(result, (list, tuple)):
        return len(result) == 0
    if isinstance(result, dict):
        if not result:
            return True
        list_values = [
            v
            for k, v in result.items()
            if k.endswith("_list") and isinstance(v, list)
        ]
        if list_values and all(len(v) == 0 for v in list_values):
            return True
    return False


def no_result_message(
    result: Any, message: str = "No results found."
) -> Optional[str]:
    """Return ``message`` when ``result`` is empty (see :func:`is_empty_result`),
    otherwise ``None``. Use as ``return no_result_message(data, "...")
    or data`` in a terminal read tool so an empty backend response becomes a
    friendly string instead of ``None``/an empty envelope."""
    return message if is_empty_result(result) else None