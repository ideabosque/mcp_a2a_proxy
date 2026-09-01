#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""MCP A2A Proxy — MCP-to-A2A adapter for SilvaEngine MCP hosts."""

from __future__ import annotations

__author__ = "Idea Bosque"
__version__ = "0.1.0"

from .mcp_a2a_proxy import MCPA2AProxy
from .mcp_configuration import MCP_CONFIGURATION

__all__ = ["MCPA2AProxy", "MCP_CONFIGURATION"]