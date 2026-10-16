# agentscope-channel-octo

[![License: Apache-2.0](https://img.shields.io/badge/License-Apache_2.0-blue.svg)](LICENSE)
[![CI](https://github.com/Mininglamp-OSS/agentscope-channel-octo/actions/workflows/ci.yml/badge.svg)](https://github.com/Mininglamp-OSS/agentscope-channel-octo/actions/workflows/ci.yml)

Octo IM (WuKongIM-based corporate IM) channel adapter for
[AgentScope 2.0](https://github.com/agentscope-ai/agentscope).

It plugs an Octo bot into AgentScope's agent service: inbound messages
arrive over the bot's WuKongIM long connection and become agent runs;
replies, typing, lookups, uploads and tool-approval cards go out over the
Octo Bot REST API. 中文说明见 [README_zh.md](README_zh.md).

## Why a separate worker matters

AgentScope 2.0.7 ([#2390](https://github.com/agentscope-ai/agentscope/pull/2390),
[#2395](https://github.com/agentscope-ai/agentscope/pull/2395)) moved IM long
connections out of the API replicas into a dedicated channel worker and
made every other channel operation plain REST. Octo is a textbook case:
**one bot gets exactly one connection** — a second session with the same
bot is kicked (`reason_code=12 账号在其他设备上登录`). This adapter follows
the contract exactly:

- `start_listening()` is the only method that touches the socket;
- `send_response()`, `list_bot_chats()`, `chat_name()`, the platform tools
  and the approval cards all work on an instance that **never listened**,
  so `ChannelClients` can use them from any API replica;
- status is reported through the bus heartbeat
  (`agentscope:channel:liveness:{cid}`), so replicas without a connection
  still answer `GET /channels/{id}/status` correctly.

[docs/channel-worker-testing.md](docs/channel-worker-testing.md) walks
through reproducing all of this on a real Octo server (TTL self-healing,
replicas kicking each other in the pre-#2390 layout, direct reply delivery,
credential rotation, approval cards).

## Features

- Direct messages, groups and threads (`dm:` / `group:` / `thread:` chat ids).
- Reply only when @mentioned in groups (configurable), text fallback for
  senders that omit the mention payload, bot-to-bot loop protection.
- Quoted replies as context, @mention of the sender on group replies,
  Markdown text, long replies split at 4000 characters.
- Images and files in both directions (inbound attachments are fetched and
  handed to the agent as data blocks; outbound ones go through presigned
  uploads).
- Typing indicator and the 30 s online heartbeat.
- Tool approvals as `octo/v2` interactive cards; clicks are polled from
  `POST /v1/bot/events`, turned into `ChannelConfirmationResultEvent`, and the
  card is frozen into "approved / rejected". Falls back to a text notice when
  the bot has no card permission.
- Platform tools for the agent: `ListOctoGroups`, `SendOctoMessage`
  (the latter always asks for confirmation).
- Auto-reconnect with backoff; application-level PING/PONG; registration
  refresh when the server rejects the session.

## Requirements

- Python ≥ 3.11, `agentscope >= 2.0.7` (developed against 2.0.10) with the
  `service` extra and a Redis-backed storage / message bus for multi-process
  deployments.
- An Octo server exposing the Bot API (`/v1/bot/...`) and a bot token from
  BotFather (`/newbot`).

## Install

```bash
pip install "agentscope[service,storage-redis]" \
  "git+https://github.com/Mininglamp-OSS/agentscope-channel-octo.git"
```

## Quick start

API replica (holds no connection):

```python
from agentscope.app import create_app
from agentscope.app.message_bus import RedisMessageBus
from agentscope.app.storage import RedisStorage
from agentscope.app.workspace_manager import LocalWorkspaceManager
from agentscope_channel_octo import OctoChannel

app = create_app(
    storage=RedisStorage(host="localhost", port=6379),
    message_bus=RedisMessageBus(host="localhost", port=6379),
    workspace_manager=LocalWorkspaceManager(basedir="./workspaces"),
    channels=[OctoChannel],
    enable_channel_worker=False,   # True = single-process / desktop layout
)
```

Channel worker (holds the connection):

```python
import asyncio
from agentscope.app.channel.worker import run_channel_worker
from agentscope.app.message_bus import RedisMessageBus
from agentscope.app.storage import RedisStorage
from agentscope.app.workspace_manager import LocalWorkspaceManager
from agentscope_channel_octo import OctoChannel

asyncio.run(run_channel_worker(
    storage=RedisStorage(host="localhost", port=6379),
    message_bus=RedisMessageBus(host="localhost", port=6379),
    workspace_manager=LocalWorkspaceManager(basedir="./workspaces"),
    channels=[OctoChannel],
))
```

Register the bot once to learn its `robot_id`, then create the channel
through the AgentScope API (`X-User-ID` identifies the owner):

```bash
curl -X POST https://<octo-host>/api/v1/bot/register \
  -H "Authorization: Bearer $OCTO_BOT_TOKEN" -d '{}'
#  -> {"robot_id": "...", "ws_url": "wss://...", "owner_uid": "...", ...}

curl -X POST http://localhost:8000/channels/ -H 'X-User-ID: demo' \
  -H 'Content-Type: application/json' -d '{
  "channel_type": "octo",
  "name": "my-octo-bot",
  "credentials": {"api_url": "https://<octo-host>/api",
                  "bot_token": "bf_...", "robot_id": "<robot_id>"},
  "platform_config": {"only_at_reply": true},
  "routing": {"bindings": [{"match_key": "chat_id", "match_value": "*",
                             "agent_id": "<agent_id>", "session_scope": "per_chat"}]},
  "session": {"chat_model_config": {"type": "openai_chat",
                                    "credential_id": "<credential_id>",
                                    "model": "<model>", "parameters": {}},
              "permission_mode": "default"}
}'
```

`examples/bootstrap.py` does all of the above (credential → agent → bot
registration → channel) and `examples/run.sh` starts replicas and workers.

## Configuration

Credentials (`credentials`):

| field | description |
|---|---|
| `api_url` | Octo server API base, e.g. `https://octo.example.com/api` |
| `bot_token` | token issued by BotFather |
| `robot_id` | the bot's uid (`..._bot`), returned by `POST /v1/bot/register`; also the uniqueness key |
| `ws_url` | optional override of the WebSocket URL returned by registration |

Options (`platform_config`):

| option | default | description |
|---|---|---|
| `only_at_reply` | `true` | in groups/threads reply only when @mentioned; DMs always get a reply |
| `reply_mention_sender` | `true` | @mention the user on group replies |
| `ignore_bots` | `true` | drop messages from other bots (`*_bot`) |
| `typing_indicator` | `true` | show "typing…" while the reply is produced |
| `card_approval` | `true` | use interactive cards for tool approvals |
| `show_tool_process` / `show_thinking` | `false` | render tool calls / reasoning inline |
| `heartbeat_interval_secs` | `30` | REST heartbeat cadence |
| `ws_proxy` | `""` | proxy URL for the long connection; empty = direct (environment proxies are ignored, like the reference adapter) |

Chat ids are self-describing and usable in routing rules: `dm:<uid>`,
`group:<group_no>`, `thread:<group_no>____<short_id>`. Routing may also
match `metadata.chat_type` (`dm` / `group` / `thread`).

## Protocol notes

- Registration: `POST /v1/bot/register` → `im_token`, `ws_url`.
- Socket: WuKongIM v4 binary protocol over WebSocket — CONNECT carries a
  fresh X25519 public key, CONNACK returns the server key and salt, payloads
  are AES-128-CBC (`key = md5(base64(shared))[:16]`, `iv = salt[:16]`);
  RECV is acknowledged with RECVACK; PING every 30 s.
- Outbound: `POST /v1/bot/sendMessage` (text with quote/mention, image, file,
  `type=17` cards), `POST /v1/bot/message/edit`, `/typing`, `/heartbeat`,
  `/upload/presigned`, `/groups`, `/user/info`, `/card/profile`.
- Reference implementation:
  [openclaw-channel-octo](https://github.com/Mininglamp-OSS/openclaw-channel-octo).

## Limitations

- No emoji reactions (the Bot API has none); voice / video arrive as text
  placeholders with the media URL.
- Approval cards need `interaction_enabled` for the bot in
  `GET /v1/bot/card/profile`; otherwise a text notice is sent and the run
  must be approved from the AgentScope console.
- `POST /v1/bot/messages/sync` returns original payloads only — edited
  cards are not visible there.

## Development

```bash
pip install -e ".[dev]"
pytest -q
black --check src tests && flake8 --max-line-length=79 --extend-ignore=E203 src tests
```

## License

Apache-2.0.
