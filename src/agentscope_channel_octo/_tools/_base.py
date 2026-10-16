# -*- coding: utf-8 -*-
"""Shared base for the Octo agent tools."""

from typing import Any, TYPE_CHECKING

from agentscope.permission import (
    PermissionBehavior,
    PermissionContext,
    PermissionDecision,
)
from agentscope.tool import ToolBase

if TYPE_CHECKING:
    from .._channel import OctoChannel


class _OctoToolBase(ToolBase):
    """Base for Octo tools bound to a channel instance."""

    is_concurrency_safe: bool = False
    is_state_injected: bool = False
    is_external_tool: bool = False
    is_mcp: bool = False
    mcp_name: str | None = None

    def __init__(self, channel: "OctoChannel") -> None:
        """Bind the channel the tool acts through.

        Args:
            channel (`OctoChannel`): The channel serving this session.
        """
        super().__init__()
        self._channel = channel

    async def check_permissions(
        self,
        tool_input: dict[str, Any],
        context: PermissionContext,
    ) -> PermissionDecision:
        """Allow lookups; ask before sending to another conversation.

        Args:
            tool_input (`dict[str, Any]`): Proposed tool arguments.
            context (`PermissionContext`): Session permission context.
        """
        del tool_input, context
        if self.is_read_only:
            return PermissionDecision(
                behavior=PermissionBehavior.ALLOW,
                message=f"{self.name} is a read-only lookup.",
            )
        return PermissionDecision(
            behavior=PermissionBehavior.ASK,
            message="Sending to another Octo conversation needs the "
            "user's confirmation.",
        )
