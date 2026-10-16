# -*- coding: utf-8 -*-
"""Shared backend wiring for the API replicas and the channel worker.

Copy this directory, fill in ``.env`` (see ``.env.example``) and run
``./run.sh api 8000 && ./run.sh worker``.

Both processes must talk to the SAME Redis (storage + message bus): the
worker heartbeats channel status and publishes run triggers there, and
the API replicas read status / deliver replies from there.
"""
import os
from pathlib import Path
from urllib.parse import urlparse

from agentscope.app.channel import (
    DingTalkChannel,
    DiscordChannel,
    FeishuChannel,
)
from agentscope_channel_octo import OctoChannel
from agentscope.app.message_bus import RedisMessageBus
from agentscope.app.storage import RedisStorage
from agentscope.app.workspace_manager import LocalWorkspaceManager

DEPLOY_DIR = Path(__file__).resolve().parent

#: Channel types every process (API replicas and worker) accepts. A
#: record whose type is missing from the worker's list is skipped.
CHANNELS = [OctoChannel, FeishuChannel, DingTalkChannel, DiscordChannel]


def load_env(path: Path | None = None) -> None:
    """Load ``deploy/.env`` into ``os.environ`` (existing values win)."""
    env_file = path or DEPLOY_DIR / ".env"
    if not env_file.exists():
        return
    for line in env_file.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, _, value = line.partition("=")
        os.environ.setdefault(key.strip(), value.strip().strip("'\""))


def _redis_params() -> dict:
    url = urlparse(os.environ.get("REDIS_URL", "redis://localhost:6379/0"))
    db = int((url.path or "/0").lstrip("/") or 0)
    return {
        "host": url.hostname or "localhost",
        "port": url.port or 6379,
        "db": db,
        "password": url.password,
    }


def build_storage() -> RedisStorage:
    """Redis-backed storage (channel records, sessions, agents...)."""
    return RedisStorage(**_redis_params())


def build_message_bus() -> RedisMessageBus:
    """Redis-backed bus (lifecycle, run triggers, liveness heartbeat)."""
    return RedisMessageBus(**_redis_params())


def build_workspace_manager() -> LocalWorkspaceManager:
    """Local workspaces under ``deploy/workspaces``."""
    return LocalWorkspaceManager(basedir=str(DEPLOY_DIR / "workspaces"))
