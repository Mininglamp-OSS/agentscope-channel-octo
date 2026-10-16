# 用 Octo 验证 AgentScope 2.0 的 Channel Worker 架构

对应文章：《AgentScope 2.0 如何隔离 Channel 长连接：分布式多智能体的连接管理实战》
（PR [#2390](https://github.com/agentscope-ai/agentscope/pull/2390) /
[#2395](https://github.com/agentscope-ai/agentscope/pull/2395)，已包含在 v2.0.7+，本环境基于 **v2.0.10**）。

`examples/` 目录把文章讲的几件事落成可观察的实验（下文路径均相对 `examples/`）：

| 文章里的概念 | 代码位置（agentscope/src/agentscope/app/） | 本环境里怎么看 |
|---|---|---|
| ChannelClients：只构建 REST 半部分，绝不 `start_listening`，按 `updated_at` 缓存 | `channel/_clients.py` | API 副本以 `enable_channel_worker=False` 启动，仍能查 `chat_ids`、直发回复 |
| 两个入口：`create_app(enable_channel_worker=…)` / `run_channel_worker()` | `_app.py`、`channel/worker/__init__.py` | `api.py`（`CHANNEL_MODE=worker|embedded`）/ `channel_worker.py` |
| TTL 心跳 `channel:liveness:{cid}` 取代永久标记 | `channel/_dispatcher.py`（`LIVENESS_REFRESH_SECS=10`）、`channel/_base.py`（`LIVENESS_TTL_SECS=30`）、`_service/_channel.py:get_status` | `./watch.sh` 实时看 Redis Hash + TTL + 各副本 `/status` |
| 删除出站转发链（`enqueue_channel_output` / `channel_outbound_queue` / `_forward`…），运行节点直发（#2395） | `channel/_stream.py`、`_service/_chat.py` → `ChannelClients.deliver` | 回复出现在 **API 副本** 日志而非 worker；Redis 没有 outbound 队列键 |
| 连接数 = worker 副本数，平台语义（随机选一/广播/单光标）按平台建模 | — | Octo 基于 WuKongIM：同一 bot 多条连接会**互踢**，embedded 双副本可直接复现"越扩越错" |

## 目录

```
deploy/
├── .env               密钥与地址（权限 600，已 gitignore）：REDIS_URL、LLM_*、OCTO_*
├── backends.py        Redis 存储/总线、本地工作区、渠道类型列表（OctoChannel + 内置三种）
├── api.py             一个 API 副本；CHANNEL_MODE=worker（默认，不持连接）或 embedded（旧架构）
├── channel_worker.py  专用 channel worker：只持长连接 + 心跳 + 入站转触发器
├── bootstrap.py       一键创建：模型凭证 → agent → 注册 octo bot → 创建 octo 渠道（幂等，见 state.json）
├── run.sh             启停：api <port> [embedded] / worker [name] / stop / status
├── watch.sh           每 2s 打印 liveness 心跳键、TTL、各副本 status
└── logs/ run/ workspaces/
```

Octo 适配器源码：`src/agentscope_channel_octo/`
（`_protocol.py` WuKongIM 二进制协议，`_api.py` Bot REST，`_channel.py` 渠道类，`_card.py` 审批卡，`_tools/` 平台工具），
单元测试：`tests/test_channel_octo.py`。

## 前置条件

- Python 3.11+，`pip install "agentscope[service,storage-redis]" agentscope-channel-octo`（或从本仓库 `pip install -e .`）
- Redis：`docker run -d --name agentscope-redis -p 6379:6379 redis:7`
- 模型网关：OpenAI 兼容，`.env` 里的 `LLM_BASE_URL/LLM_API_KEY/LLM_MODEL`
- Octo：在 Octo 里向 BotFather 发 `/newbot` 得到 bot token，填入 `.env` 的 `OCTO_API_URL`（形如 `https://<host>/api`）和 `OCTO_BOT_TOKEN`

## 启动

```bash
cd deploy
docker start agentscope-redis
./run.sh api 8000          # API 副本 A（不持连接）
./run.sh worker            # channel worker（持连接）
python bootstrap.py        # 建凭证/agent/注册 bot/建渠道；没有 octo token 时加 --skip-octo
./watch.sh                 # 另开一个终端观察心跳
```

所有 HTTP 调用都带 `X-User-ID: demo`（当前版本用请求头当身份）。

## 特性验证清单

### F1 连接只在 worker，API 副本是"嘴"不是"耳朵"

```bash
./run.sh api 8001                         # 再起一个副本 B
./watch.sh                                # liveness Hash 里只有 1 个 node_id（worker 的主机名:随机id）
curl -s -H 'X-User-ID: demo' localhost:8001/channels/<cid>/status      # 副本 B 没有连接也能答 connected（读心跳）
curl -s -H 'X-User-ID: demo' localhost:8001/channels/<cid>/chat_ids    # 走 ChannelClients 的 REST（GET /v1/bot/groups）
```
期望：Octo 侧只有一条 bot 连接；两个 API 副本的 `/status` 一致；`chat_ids` 能列出群。

### F2 TTL 心跳自愈（文章第四节）

```bash
./watch.sh                 # TTL 在 20~30s 之间来回（10s 续租一次），reported_at 递增
./run.sh stop worker       # 杀掉 worker
```
期望：最晚 30s 后 liveness 键过期消失，`/status` 从 `connected` 变成 `connecting`（已启用但无人持有）；
`./run.sh worker` 重启后 ≤10s 内回到 `connected`。没有任何"清理逻辑"参与。

### F3 回复由运行节点直发（#2395）

在 Octo 里私聊机器人发一句话：
- `logs/worker.log`：只有 `inbound message … -> gateway`，**没有** 投递；
- `logs/api-8000.log`（或 8001，取决于哪个副本抢到了 run）：`delivered reply to dm:… directly over REST`；
- `docker exec agentscope-redis redis-cli --scan --pattern '*outbound*'` 为空（转发链已删）。

### F4 复现旧架构的"越扩越错"（WuKongIM 互踢）

```bash
./run.sh stop worker
./run.sh api 8001 embedded    # 两个 embedded 副本 = 每个节点都全连
./run.sh api 8002 embedded
tail -f logs/api-8001.log logs/api-8002.log
```
期望：两条同 bot 连接互相踢——Octo/WuKongIM 返回 `DISCONNECT reason_code=12 账号在其他设备上登录`，
日志里反复出现 `disconnected by server` / `connected as`，liveness Hash 里出现两个 node，
一个 `connected` 一个 `retrying`；三个副本的 `/status` 却都答 `connected`（读心跳时优先取 connected 的报告），
所以从状态接口根本看不出有副本在空转。这就是文章说的"每个节点都全连 → 越扩越错"。
恢复：`./run.sh stop api-8001; ./run.sh stop api-8002; ./run.sh worker`。

### F5 ChannelClients 按 updated_at 重建实例（凭证/配置轮换不重启）

```bash
curl -s -X PATCH -H 'X-User-ID: demo' -H 'Content-Type: application/json' \
  localhost:8000/channels/<cid> -d '{"platform_config":{"only_at_reply":false}}'
```
期望：worker 日志 `channel '<cid>' stopped` → `started`（dispatcher 比较 updated_at 重连）；
API 副本里下一次回复用新实例（旧实例被 retire 而不是关闭，进行中的运行不受影响）。
群里不 @ 机器人也会回复了。

### F6 工具审批卡（interactive 能力）

不需要群也能测：在私聊里对机器人说
"用 SendOctoMessage 工具给 dm:<你的uid> 发一句 hello"（你的 uid 就是注册时的 owner_uid，见 `state.json`）。
有群的话也可以："给 group:<群号> 发一条 hello"（先让它 `ListOctoGroups`）。
期望：`SendOctoMessage` 需要确认 → Octo 里收到审批卡（服务端启用了交互卡片时）→ 点"批准"
→ worker 的事件轮询（`POST /v1/bot/events`）收到 `card_action` → 转成 `ChannelConfirmationResultEvent`
→ 运行恢复并真的发出消息；卡片原地变成"✅ 已批准"。服务端没开卡片时退化为文本提示。

### F7 上游测试

```bash
pytest -q            # 本仓库的适配器测试
# agentscope 仓库里与 channel worker 相关的回归测试：
# tests/channel_clients_test.py tests/channel_worker_test.py tests/channel_stream_test.py
# tests/channel_gateway_test.py tests/channel_routing_test.py tests/service_chat_channel_delivery_test.py
```

## Octo 渠道说明

- 入站：`POST /v1/bot/register` 拿 `im_token/ws_url` → WebSocket 上跑 WuKongIM v4 协议
  （CONNECT 携带 X25519 公钥 → CONNACK 返回服务端公钥+salt → AES-128-CBC 解 RECV → 回 RECVACK；30s PING，3 次无 PONG 重连；
  另有 30s REST 心跳保持"在线"）。
- 出站：`POST /v1/bot/sendMessage`（文本自动引用原消息、群里自动 @ 发送者，4000 字切分），图片/文件走 presigned 上传。
- 聊天 id：`dm:<uid>` / `group:<group_no>` / `thread:<group_no>____<short_id>`；路由规则可按 `chat_id`、`user_id`、`chat_type`（dm/group/thread）匹配。
- 配置项：`only_at_reply`（群里仅 @ 回复，默认 true）、`reply_mention_sender`、`ignore_bots`、`typing_indicator`、`card_approval`、`show_tool_process`、`show_thinking`、`heartbeat_interval_secs`。
- 凭证：`api_url`、`bot_token`、`robot_id`（bot 唯一键，`bootstrap.py` 会自动注册获取）、可选 `ws_url`。
- 代理：长连接默认**直连**（与参考实现一致，忽略 `HTTPS_PROXY`），需要时用配置项 `ws_proxy` 显式指定；
  本机 Surge/Clash 类代理（127.0.0.1:6152）不支持 wss CONNECT，走代理会报 `InvalidProxyMessage`。REST 仍遵循环境代理。
- 同一 bot 只允许一条连接：第二条连接（即使换 deviceFlag）会被服务端踢掉或静默关闭——这正是长连接必须收口到 worker 的原因。
- 已知限制：没有表情回应 API；语音/视频只给占位文本；审批卡需要服务端为该 bot 开启 `interaction_enabled`。

## 排错

| 现象 | 看哪里 |
|---|---|
| status 一直 connecting | worker 日志：注册失败（token/URL）、CONNACK 拒绝、`gave up after 2 connect attempts`（编辑渠道会重试） |
| 群里不回复 | 默认需要 @ 机器人；或 `PATCH platform_config.only_at_reply=false` |
| 回复没发出 | API 副本日志 `failed to deliver`；检查模型凭证（`/credential/`）与模型名 |
| 两个 node 抖动 | 有 embedded 副本或多个 worker 同时持有同一 bot，见 F4 |

## 实测记录（2026-10-15/16，一台 MacBook 上的真实 Octo 服务器）

| 实验 | 观察 |
|---|---|
| 启动 worker | 注册 → `wss://octo.example.com/ws` CONNACK → `connected as <robot_id>`，不持连接的 :8000 副本 `/status` 立刻读到 connected |
| 心跳续租 | `reported_at` 每 10s 递增，键 TTL 在 23~27s 之间来回 |
| `kill -9` worker | 状态仍 connected（过期前心跳仍"新鲜"）→ TTL 21→16→1 → 键消失（TTL=-2）→ `/status` 自动变 `connecting`，用时 ≤32s，没有任何清理逻辑参与 |
| 优雅停止（SIGTERM） | 3s 内 `registry_del` 撤回本节点字段，状态立刻变 connecting |
| 重启 worker | 12s 内回到 connected（前 ≤10s 可能短暂读到 `stopped`：dispatcher 在监听任务启动前就发了一次心跳，属上游小瑕疵） |
| PATCH 渠道配置 | `updated_at` 变化 0.1s 后 worker `stopped`→`started`，0.5s 后重新 connected；期间 ≤10s 状态为 connecting |
| 两个 embedded 副本 | 5s 内互踢 3 轮（reason_code=12），一个胜出一个重试；三个 `/status` 全部显示 connected |
| 副本列群 | `/channels/{id}/chat_ids` 由无连接副本经 `ChannelClients` 走 REST 返回（bot 未入群时为空） |
| 卡片能力 | `GET /v1/bot/card/profile`：`interaction_enabled=true`，profiles `octo/v1,v2`，可发审批卡 |
| 私聊收发（F3） | 21:55:42 worker 收到「在吗」→ 派生会话 `dm:<owner_uid>`（昵称解析为用户名）→ :8000 副本运行 → 21:55:45 回复「在的，有什么需要帮忙的？」直发 Octo；API 日志无投递错误，Redis 无 `*outbound*`/`*forward*` 键，端到端 3s |
| 审批卡（F6） | 10-15 22:00「看下当前环境情况」：agent 调 Bash → 审批卡（octo/v2）→ 点批准 → worker 事件轮询收到 `card_action` → 恢复运行 → 第二次 Bash 再发卡 → 批准 → 22:00:39 给出答案；两次往返均成功。发现并修复：批准后下一段投递误发「(Agent returned no text content)」占位；卡片点击后未改成「✅ 已批准」（编辑目标应取发卡时的 chat）|
| 夜间自愈 | 10-16 06:36–09:20 本机网络/Docker 波动：注册 ConnectError、Redis 列表失败、心跳失败；worker 09:22:06 自动重连，无需人工干预 |
| 审批卡复测（修复后） | 10-16 10:41:23 worker `inbound message … -> gateway` → 10:41:26 API `delivered reply … (1 text parts, 1 approvals)`（文本「好，那我调一下。」+ 审批卡）→ 10:41:30 worker `card click by <owner>: approve tool call call_7617…` → 运行恢复，工具发出「hello」→ 10:41:33 API 投递收尾回复；中间不再出现占位文本。注意：bot 侧 `messages/sync` 只返回原始 payload，不含编辑后的卡片内容，卡片是否变为「✅ 已批准」以客户端显示为准（编辑接口返回 200）|
