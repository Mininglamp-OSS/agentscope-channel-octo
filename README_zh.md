# agentscope-channel-octo

[AgentScope 2.0](https://github.com/agentscope-ai/agentscope) 的 Octo IM（基于 WuKongIM 的企业 IM）渠道适配器。

它把一个 Octo 机器人接进 AgentScope 的 agent service：入站消息通过机器人的 WuKongIM 长连接到达并变成 agent 运行；
回复、输入中提示、查询、上传和工具审批卡全部走 Octo Bot REST API。English: [README.md](README.md).

## 为什么要有专用 worker

AgentScope 2.0.7（[#2390](https://github.com/agentscope-ai/agentscope/pull/2390)、
[#2395](https://github.com/agentscope-ai/agentscope/pull/2395)）把 IM 长连接从 API 副本里挪进专用的 channel worker，
其余操作全部变成普通 REST。Octo 正是教科书案例：**一个 bot 只允许一条连接**，第二条同 bot 会话会被踢
（`reason_code=12 账号在其他设备上登录`）。本适配器严格遵守这个契约：

- 只有 `start_listening()` 碰 socket；
- `send_response()`、`list_bot_chats()`、`chat_name()`、平台工具和审批卡都能在**从未监听过**的实例上工作，
  因此任意 API 副本都可以通过 `ChannelClients` 使用它们；
- 状态通过总线心跳（`agentscope:channel:liveness:{cid}`）上报，不持连接的副本也能正确回答 `GET /channels/{id}/status`。

[docs/channel-worker-testing.md](docs/channel-worker-testing.md) 记录了在真实 Octo 服务器上逐项复现的过程
（TTL 自愈、旧架构多副本互踢、运行节点直发回复、凭证轮换重建、审批卡）。

## 功能

- 私聊、群聊、子区（chat id：`dm:` / `group:` / `thread:`）。
- 群里默认仅在被 @ 时回复（可配置）；对未带 mention 结构的消息做文本兜底；屏蔽其他机器人避免互相对话。
- 引用消息作为上下文、群聊回复自动 @ 发送者、Markdown 文本、超过 4000 字自动分段。
- 图片和文件双向支持（入站附件下载后以 data block 交给 agent，出站走 presigned 上传）。
- 输入中提示、30 秒在线心跳。
- 工具审批使用 `octo/v2` 交互卡片：点击通过 `POST /v1/bot/events` 轮询取回，转成 `ChannelConfirmationResultEvent`，
  卡片原地冻结为"已批准 / 已拒绝"；bot 没有卡片权限时退化为文本提示。
- 提供给 agent 的平台工具：`ListOctoGroups`、`SendOctoMessage`（后者必定需要确认）。
- 带退避的自动重连、应用层 PING/PONG、服务端拒绝会话时重新注册。

## 环境要求

- Python ≥ 3.11，`agentscope >= 2.0.7`（基于 2.0.10 开发），安装 `service` extra；多进程部署需要 Redis 存储/总线。
- 提供 Bot API（`/v1/bot/...`）的 Octo 服务器，以及 BotFather `/newbot` 生成的 bot token。

## 安装

```bash
pip install "agentscope[service,storage-redis]" \
  "git+https://github.com/Mininglamp-OSS/agentscope-channel-octo.git"
```

## 快速开始

API 副本（不持连接）：

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
    enable_channel_worker=False,   # True = 单进程 / 桌面版布局
)
```

Channel worker（持连接）：

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

先注册一次拿到 `robot_id`，再通过 AgentScope API 创建渠道（`X-User-ID` 标识所有者）：

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

`examples/bootstrap.py` 一键完成上述步骤（凭证 → agent → 注册 bot → 创建渠道），`examples/run.sh` 负责启停副本和 worker。

## 配置

凭证（`credentials`）：

| 字段 | 说明 |
|---|---|
| `api_url` | Octo 服务端 API 地址，如 `https://octo.example.com/api` |
| `bot_token` | BotFather 签发的 token |
| `robot_id` | 机器人 uid（`..._bot`），由 `POST /v1/bot/register` 返回；同时作为唯一键 |
| `ws_url` | 可选，覆盖注册返回的 WebSocket 地址 |

选项（`platform_config`）：

| 选项 | 默认 | 说明 |
|---|---|---|
| `only_at_reply` | `true` | 群/子区仅在被 @ 时回复；私聊总是回复 |
| `reply_mention_sender` | `true` | 群聊回复时 @ 发送者 |
| `ignore_bots` | `true` | 丢弃其他机器人（`*_bot`）的消息 |
| `typing_indicator` | `true` | 生成回复期间显示"输入中" |
| `card_approval` | `true` | 用交互卡片做工具审批 |
| `show_tool_process` / `show_thinking` | `false` | 在回复中内联展示工具调用 / 推理过程 |
| `heartbeat_interval_secs` | `30` | REST 心跳间隔 |
| `ws_proxy` | `""` | 长连接代理地址；空表示直连（与参考实现一致，忽略环境代理） |

chat id 自描述，可直接用于路由规则：`dm:<uid>`、`group:<group_no>`、`thread:<group_no>____<short_id>`；
路由也可按 `metadata.chat_type`（`dm` / `group` / `thread`）匹配。

## 协议要点

- 注册：`POST /v1/bot/register` → `im_token`、`ws_url`。
- 长连接：WebSocket 上的 WuKongIM v4 二进制协议——CONNECT 携带新生成的 X25519 公钥，CONNACK 返回服务端公钥与 salt，
  payload 为 AES-128-CBC（`key = md5(base64(shared))[:16]`，`iv = salt[:16]`）；收到 RECV 回 RECVACK；每 30 秒 PING。
- 出站：`POST /v1/bot/sendMessage`（带引用/@ 的文本、图片、文件、`type=17` 卡片）、`POST /v1/bot/message/edit`、
  `/typing`、`/heartbeat`、`/upload/presigned`、`/groups`、`/user/info`、`/card/profile`。
- 参考实现：[openclaw-channel-octo](https://github.com/Mininglamp-OSS/openclaw-channel-octo)。

## 已知限制

- 没有表情回应（Bot API 不提供）；语音 / 视频以带 URL 的占位文本交给 agent。
- 审批卡需要 bot 在 `GET /v1/bot/card/profile` 中具备 `interaction_enabled`，否则发文本提示，需在 AgentScope 控制台审批。
- `POST /v1/bot/messages/sync` 只返回原始 payload，看不到编辑后的卡片。

## 开发

```bash
pip install -e ".[dev]"
pytest -q
black --check src tests && flake8 --max-line-length=79 --extend-ignore=E203 src tests
```

## 许可证

Apache-2.0。
