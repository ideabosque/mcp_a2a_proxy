#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""MCP A2A Proxy — facade composing discovery, delegation, and task mixins.

Flat mixin composition: all domain mixins inherit only from
``A2ABackedProcessor`` and are composed into this single facade class that the
MCP runtime instantiates. The pattern mirrors
``mcp_hospirfq_processor/mcp_hospirfq_processor.py:31-57``.
"""

from __future__ import annotations

__author__ = "Idea Bosque"

import logging
from typing import Any, Dict

from .a2a_backed_processor import A2ABackedProcessor
from .discovery_mixin import DiscoveryMixin
from .delegation_mixin import DelegationMixin
from .task_mixin import TaskMixin


class MCPA2AProxy(
    DiscoveryMixin,
    DelegationMixin,
    TaskMixin,
):
    """Public interface aggregating all A2A proxy MCP tools.

    Flat composition — each mixin contributes its own MCP tool methods and only
    accesses ``self.logger``, ``self.setting``, and ``self.a2a_client`` /
    ``self._execute_graphql_query`` / ``self._execute_jsonrpc`` (all provided by
    ``A2ABackedProcessor``).  Inter-mixin calls resolve automatically via
    Python MRO on the facade instance.

    One loop: **discover → delegate → track → abandon.**
    """

    def __init__(self, logger: logging.Logger, **setting: Dict[str, Any]):
        # Explicitly call the root base class to initialise logger, setting,
        # and a2a_client exactly once — mixins only reference these attrs.
        A2ABackedProcessor.__init__(self, logger, **setting)