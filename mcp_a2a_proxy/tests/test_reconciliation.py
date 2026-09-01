#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""P3 reconciliation tests (§8 P3 exit criteria).

- 8 unique tool names
- 8 unique module_links
- Bijection: every tool has a link and vice versa
- Every function_name resolves via getattr on the facade
- endpoint_id / part_id both present on the facade
"""

from __future__ import annotations

__author__ = "Idea Bosque"

import logging
from typing import Any, Dict

import pytest

from mcp_a2a_proxy.mcp_configuration import MCP_CONFIGURATION
from mcp_a2a_proxy.mcp_a2a_proxy import MCPA2AProxy

logger = logging.getLogger("test-reconcile")


class TestReconciliation:
    def test_eight_tools(self):
        tools = MCP_CONFIGURATION["tools"]
        assert len(tools) == 8, f"Expected 8 tools, got {len(tools)}"

    def test_eight_module_links(self):
        links = MCP_CONFIGURATION["module_links"]
        assert len(links) == 8, f"Expected 8 module_links, got {len(links)}"

    def test_unique_tool_names(self):
        names = [t["name"] for t in MCP_CONFIGURATION["tools"]]
        assert len(names) == len(set(names)), f"Duplicate tool names: {names}"

    def test_unique_link_names(self):
        names = [l["name"] for l in MCP_CONFIGURATION["module_links"]]
        assert len(names) == len(set(names)), f"Duplicate link names: {names}"

    def test_tool_link_bijection(self):
        tool_names = {t["name"] for t in MCP_CONFIGURATION["tools"]}
        link_names = {l["name"] for l in MCP_CONFIGURATION["module_links"]}
        assert tool_names == link_names, (
            f"Tools without links: {tool_names - link_names}, "
            f"Links without tools: {link_names - tool_names}"
        )

    def test_every_function_name_resolves(self):
        proxy = MCPA2AProxy(
            logger,
            gateway_base_url="http://localhost:8765",
            graphql_modules={"a2a_daemon_engine": {"x_api_key": "k"}},
        )
        for link in MCP_CONFIGURATION["module_links"]:
            assert hasattr(proxy, link["function_name"]), (
                f"Facade missing method: {link['function_name']}"
            )

    def test_endpoint_id_property(self):
        proxy = MCPA2AProxy(
            logger,
            gateway_base_url="http://localhost:8765",
            graphql_modules={"a2a_daemon_engine": {"x_api_key": "k"}},
        )
        proxy.endpoint_id = "test-ep"
        assert proxy.endpoint_id == "test-ep"

    def test_part_id_property(self):
        proxy = MCPA2AProxy(
            logger,
            gateway_base_url="http://localhost:8765",
            graphql_modules={"a2a_daemon_engine": {"x_api_key": "k"}},
        )
        proxy.part_id = "test-part"
        assert proxy.part_id == "test-part"

    def test_all_links_use_correct_module_and_class(self):
        for link in MCP_CONFIGURATION["module_links"]:
            assert link["module_name"] == "mcp_a2a_proxy", (
                f"Wrong module_name for {link['name']}: {link['module_name']}"
            )
            assert link["class_name"] == "MCPA2AProxy", (
                f"Wrong class_name for {link['name']}: {link['class_name']}"
            )
            assert link["return_type"] == "text", (
                f"Wrong return_type for {link['name']}: {link['return_type']}"
            )

    def test_required_args_match_plan(self):
        """Spot-check that required fields match §4.1 contracts."""
        tools_by_name = {t["name"]: t for t in MCP_CONFIGURATION["tools"]}
        # get_a2a_agent: agent_id required
        assert "agent_id" in tools_by_name["get_a2a_agent"]["inputSchema"]["required"]
        # send_a2a_message: message required
        assert "message" in tools_by_name["send_a2a_message"]["inputSchema"]["required"]
        # send_a2a_message_stream: message required
        assert "message" in tools_by_name["send_a2a_message_stream"]["inputSchema"]["required"]
        # get_a2a_task: task_id required
        assert "task_id" in tools_by_name["get_a2a_task"]["inputSchema"]["required"]
        # cancel_a2a_task: task_id required
        assert "task_id" in tools_by_name["cancel_a2a_task"]["inputSchema"]["required"]
        # discover_a2a_agents: no required
        assert tools_by_name["discover_a2a_agents"]["inputSchema"]["required"] == []
        # get_a2a_agent_card: no required
        assert tools_by_name["get_a2a_agent_card"]["inputSchema"]["required"] == []
        # list_a2a_tasks: no required
        assert tools_by_name["list_a2a_tasks"]["inputSchema"]["required"] == []