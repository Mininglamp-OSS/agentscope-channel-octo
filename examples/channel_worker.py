# -*- coding: utf-8 -*-
"""The dedicated channel worker (PR #2390).

Holds the IM long connections, heartbeats ``agentscope:channel:liveness``
in Redis, and turns inbound messages into run triggers on the bus. It
runs no agent, serves no HTTP and delivers no reply.
"""
import asyncio
import logging
import os
import sys

from agentscope.app.channel.worker import run_channel_worker

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from backends import (  # noqa: E402
    CHANNELS,
    build_message_bus,
    build_storage,
    build_workspace_manager,
    load_env,
)


async def main() -> None:
    load_env()
    logging.basicConfig(
        level=os.environ.get("LOG_LEVEL", "INFO").upper(),
        format="%(asctime)s %(levelname)s %(name)s %(message)s",
    )
    print("[worker] starting channel worker", flush=True)
    await run_channel_worker(
        storage=build_storage(),
        message_bus=build_message_bus(),
        workspace_manager=build_workspace_manager(),
        channels=CHANNELS,
    )


if __name__ == "__main__":
    asyncio.run(main())
