# -*- coding: utf-8 -*-
"""One AgentScope API replica.

CHANNEL_MODE=worker   (default) -> create_app(enable_channel_worker=False)
                                   this replica holds NO IM connection;
                                   run channel_worker.py alongside.
CHANNEL_MODE=embedded           -> create_app(enable_channel_worker=True)
                                   the pre-#2390 shape: every replica
                                   connects (the article's problem case).

PORT selects the listen port, so several replicas can run side by side.
"""
import os
import sys

import uvicorn
from fastapi.middleware import Middleware
from fastapi.middleware.cors import CORSMiddleware

from agentscope.app import create_app

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from backends import (  # noqa: E402
    CHANNELS,
    build_message_bus,
    build_storage,
    build_workspace_manager,
    load_env,
)

load_env()

EMBED_CHANNELS = os.environ.get("CHANNEL_MODE", "worker") == "embedded"

app = create_app(
    storage=build_storage(),
    message_bus=build_message_bus(),
    workspace_manager=build_workspace_manager(),
    enable_channel_worker=EMBED_CHANNELS,
    channels=CHANNELS,
    extra_middlewares=[
        Middleware(
            CORSMiddleware,
            allow_origins=["*"],
            allow_methods=["*"],
            allow_headers=["*"],
        ),
    ],
)

if __name__ == "__main__":
    port = int(os.environ.get("PORT", "8000"))
    print(
        f"[api] port={port} enable_channel_worker={EMBED_CHANNELS} "
        f"(CHANNEL_MODE={'embedded' if EMBED_CHANNELS else 'worker'})",
        flush=True,
    )
    uvicorn.run(app, host="0.0.0.0", port=port, log_level="info")
