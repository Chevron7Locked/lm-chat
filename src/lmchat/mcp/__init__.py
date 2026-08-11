# SPDX-License-Identifier: Apache-2.0
"""Native MCP host — client manager and tool discovery."""

from lmchat.mcp.host import (
    McpHost,
    McpServerConfig,
    split_secrets_for_transport,
)

__all__ = ["McpHost", "McpServerConfig", "split_secrets_for_transport"]
