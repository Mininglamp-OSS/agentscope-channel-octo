# -*- coding: utf-8 -*-
"""Send text to a specified Octo user, group or thread."""

from pydantic import Field

from agentscope.message import TextBlock, ToolResultState
from agentscope.tool import ParamsBase, ToolChunk
from ._base import _OctoToolBase


class _SendMessageParams(ParamsBase):
    target: str = Field(
        pattern=r"^(dm|group|thread):.+$",
        description="Encoded target: ``dm:<uid>``, ``group:<group_no>`` "
        "(from ListOctoGroups) or ``thread:<group_no>____<short_id>``.",
    )
    text: str = Field(
        min_length=1,
        description="Markdown-formatted message body.",
    )


class SendMessage(_OctoToolBase):
    """Send text to a conversation other than the current one."""

    name: str = "SendOctoMessage"
    description: str = """Send Markdown text to an Octo user, group or thread.

Use this only when the user asks to contact a target other than the current \
conversation. Obtain group targets from ``ListOctoGroups``. The operation \
requires confirmation."""
    is_read_only: bool = False
    input_schema: dict = _SendMessageParams.model_json_schema()

    async def __call__(self, target: str, text: str) -> ToolChunk:
        """Send text to an encoded target.

        Args:
            target (`str`): Encoded Octo target.
            text (`str`): Markdown-formatted message body.

        Returns:
            `ToolChunk`: Whether Octo accepted the message.
        """
        if await self._channel.send_message_to(target, text):
            return ToolChunk(content=[TextBlock(text=f"Sent to {target}.")])
        return ToolChunk(
            content=[
                TextBlock(
                    text=f"Failed to send to {target}: Octo rejected the "
                    "request.",
                ),
            ],
            state=ToolResultState.ERROR,
        )
