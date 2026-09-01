#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""Base processor providing A2A client access for all mixins.

Analogous to ``mcp_hospirfq_processor/graphql_backed_processor.py`` — the
root base class that all domain mixins inherit from. It holds ``self.logger``,
``self.setting``, ``self.a2a_client``, and exposes ``_execute_graphql_query``
and ``_execute_jsonrpc`` convenience methods.
"""

from __future__ import annotations

__author__ = "Idea Bosque"

import logging
from typing import Any, Dict

from .a2a_client import A2AClient


class A2ABackedProcessor:
    """Root base class for all MCP A2A Proxy mixins.

    Provides ``self.logger``, ``self.setting``, ``self.a2a_client``, and the
    ``_execute_graphql_query`` / ``_execute_jsonrpc`` methods that every mixin
    uses to communicate with the A2A daemon through the silvaengine_gateway.
    """

    def __init__(self, logger: logging.Logger, **setting: Dict[str, Any]):
        self.logger = logger
        self.setting = setting
        self.a2a_client = A2AClient(logger, **setting)

    @property
    def endpoint_id(self) -> str | None:
        return self.a2a_client.endpoint_id

    @endpoint_id.setter
    def endpoint_id(self, value: str):
        self.a2a_client.endpoint_id = value

    @property
    def part_id(self) -> str | None:
        return self.a2a_client.part_id

    @part_id.setter
    def part_id(self, value: str):
        self.a2a_client.part_id = value

    def _execute_graphql_query(
        self,
        function_name: str,
        operation_name: str,
        operation_type: str,
        variables: Dict[str, Any],
        query: str = None,
    ) -> Dict[str, Any]:
        """Execute a GraphQL query against the a2a_daemon_engine module.

        The ``function_name`` / ``operation_type`` parameters are kept for
        sibling-module compatibility but are not used by this module since the
        GraphQL documents are hand-written (§4).
        """
        if query is None:
            if operation_name == "a2aAgentList":
                query = A2AClient.graphql_a2a_agent_list_query()
            elif operation_name == "a2aAgent":
                query = A2AClient.graphql_a2a_agent_query()
            else:
                return {
                    "error": f"Unknown GraphQL operation: {operation_name}",
                    "error_code": "GRAPHQL_QUERY_FAILED",
                }
        return self.a2a_client.execute_graphql(operation_name, query, variables)

    def _execute_jsonrpc(
        self,
        method: str,
        params: Dict[str, Any],
        timeout: float | None = None,
    ) -> Dict[str, Any]:
        """Execute a JSON-RPC 2.0 call against the A2A daemon."""
        return self.a2a_client.execute_jsonrpc(method, params, timeout=timeout)