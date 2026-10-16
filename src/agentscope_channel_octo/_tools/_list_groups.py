# -*- coding: utf-8 -*-
"""List the Octo groups the bot is a member of."""

from agentscope.message import TextBlock
from agentscope.tool import ToolChunk
from ._base import _OctoToolBase


class ListGroups(_OctoToolBase):
    """List the groups the bot can post to."""

    name: str = "ListOctoGroups"
    description: str = """List the Octo groups this bot is a member of.

Returns one line per group as ``<target>\t<name>``; pass the target to \
``SendOctoMessage`` to post there."""
    is_read_only: bool = True
    input_schema: dict = {"type": "object", "properties": {}}

    async def __call__(self) -> ToolChunk:
        """Fetch the group list over REST.

        Returns:
            `ToolChunk`: The encoded targets and names.
        """
        chats = await self._channel.list_bot_chats()
        if not chats:
            return ToolChunk(content=[TextBlock(text="No groups found.")])
        lines = [f"{c['chat_id']}\t{c['name']}" for c in chats]
        return ToolChunk(content=[TextBlock(text="\n".join(lines))])
