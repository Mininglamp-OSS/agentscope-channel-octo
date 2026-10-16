# -*- coding: utf-8 -*-
"""AgentScope 2.0 channel adapter for Octo IM.

Register the channel on the API replicas and the channel worker alike::

    from agentscope.app import create_app
    from agentscope_channel_octo import OctoChannel

    app = create_app(..., channels=[OctoChannel])
"""

from ._channel import OctoChannel, decode_chat_id, encode_chat_id
from ._version import __version__

__all__ = ["OctoChannel", "decode_chat_id", "encode_chat_id", "__version__"]
