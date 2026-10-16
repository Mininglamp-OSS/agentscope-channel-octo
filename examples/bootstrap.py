# -*- coding: utf-8 -*-
"""一键初始化演示资源（幂等，已创建的 id 记录在 deploy/state.json）。

    python bootstrap.py                 # 模型凭证 → agent → 注册 octo bot → 创建 octo 渠道
    python bootstrap.py --skip-octo     # 只建凭证和 agent（还没有 octo token 时）

环境变量来自 deploy/.env：LLM_BASE_URL / LLM_API_KEY / LLM_MODEL，
OCTO_API_URL / OCTO_BOT_TOKEN。AgentScope API 默认 http://localhost:8000。
"""
import argparse
import json
import os
import sys
from pathlib import Path

import httpx

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from backends import DEPLOY_DIR, load_env  # noqa: E402

STATE = DEPLOY_DIR / "state.json"
SYSTEM_PROMPT = (
    "你是接入 Octo IM 的智能助手。用简体中文、口语化、简短地回答，"
    "一次只说一件事，不要用 Markdown 标题。"
    "需要联系其他会话时才使用 SendOctoMessage 工具。"
)


def _state() -> dict:
    return json.loads(STATE.read_text()) if STATE.exists() else {}


def _save(state: dict) -> None:
    STATE.write_text(json.dumps(state, ensure_ascii=False, indent=2))


def _die(msg: str) -> None:
    print(f"[bootstrap] 错误：{msg}", file=sys.stderr)
    sys.exit(1)


def main() -> None:
    load_env()
    parser = argparse.ArgumentParser()
    parser.add_argument("--api", default=os.environ.get("AGENTSCOPE_API", "http://localhost:8000"))
    parser.add_argument("--user", default=os.environ.get("AGENTSCOPE_USER", "demo"))
    parser.add_argument("--model", default=os.environ.get("LLM_MODEL", "your-model-name"))
    parser.add_argument("--skip-octo", action="store_true")
    parser.add_argument("--only-at-reply", default="true", choices=["true", "false"])
    args = parser.parse_args()

    api = httpx.Client(base_url=args.api.rstrip("/"), headers={"X-User-ID": args.user}, timeout=30)
    state = _state()

    # 1. 模型凭证（OpenAI 兼容网关）
    if not state.get("credential_id"):
        base_url, key = os.environ.get("LLM_BASE_URL"), os.environ.get("LLM_API_KEY")
        if not base_url or not key:
            _die("请在 deploy/.env 里填写 LLM_BASE_URL / LLM_API_KEY")
        r = api.post("/credential/", json={"data": {"type": "openai_credential", "api_key": key, "base_url": base_url}})
        r.raise_for_status()
        state["credential_id"] = r.json()["credential_id"]
        _save(state)
        print(f"[bootstrap] 模型凭证已创建 credential_id={state['credential_id']}")
    else:
        print(f"[bootstrap] 复用模型凭证 credential_id={state['credential_id']}")

    # 2. agent
    if not state.get("agent_id"):
        r = api.post("/agent/", json={"name": "octo-demo", "system_prompt": SYSTEM_PROMPT})
        r.raise_for_status()
        state["agent_id"] = r.json()["agent_id"]
        _save(state)
        print(f"[bootstrap] agent 已创建 agent_id={state['agent_id']}")
    else:
        print(f"[bootstrap] 复用 agent agent_id={state['agent_id']}")

    chat_model_config = {
        "type": "openai_chat",
        "credential_id": state["credential_id"],
        "model": args.model,
        "parameters": {},
    }
    state["chat_model_config"] = chat_model_config
    _save(state)

    if args.skip_octo:
        print("[bootstrap] 已跳过 octo 渠道（--skip-octo）")
        return

    # 3. 注册 octo bot，拿 robot_id
    octo_api, token = os.environ.get("OCTO_API_URL", "").rstrip("/"), os.environ.get("OCTO_BOT_TOKEN", "")
    if not octo_api or not token:
        _die("请在 deploy/.env 里填写 OCTO_API_URL / OCTO_BOT_TOKEN（或加 --skip-octo）")
    r = httpx.post(f"{octo_api}/v1/bot/register", headers={"Authorization": f"Bearer {token}"}, json={"agent_platform": "agentscope"}, timeout=30)
    if r.status_code >= 400:
        _die(f"octo 注册失败 {r.status_code}: {r.text[:300]}")
    reg = r.json()
    robot_id = reg.get("robot_id")
    if not robot_id:
        _die(f"octo 注册返回无 robot_id: {reg}")
    print(f"[bootstrap] octo bot 注册成功 robot_id={robot_id} name={reg.get('name')} owner_uid={reg.get('owner_uid')} ws_url={reg.get('ws_url')}")
    state["octo"] = {"robot_id": robot_id, "owner_uid": reg.get("owner_uid"), "bot_name": reg.get("name"), "ws_url": reg.get("ws_url")}
    _save(state)

    # 4. 创建 octo 渠道
    if not state.get("channel_id"):
        body = {
            "channel_type": "octo",
            "name": f"octo:{reg.get('name') or robot_id}",
            "credentials": {"api_url": octo_api, "bot_token": token, "robot_id": robot_id},
            "platform_config": {"only_at_reply": args.only_at_reply == "true"},
            "routing": {"bindings": [{"match_key": "chat_id", "match_value": "*", "agent_id": state["agent_id"], "session_scope": "per_chat"}]},
            "session": {"chat_model_config": chat_model_config, "permission_mode": "default"},
            "enabled": True,
        }
        r = api.post("/channels/", json=body)
        if r.status_code == 409:
            # Bot 'x' already registered as channel 'y'.
            existing = r.json().get("detail", "").split("'")
            cid = existing[3] if len(existing) > 3 else ""
            if not cid:
                _die(f"渠道已存在但无法解析 id: {r.text}")
            state["channel_id"] = cid
            print(f"[bootstrap] 该 bot 已绑定渠道，复用 channel_id={cid}")
        else:
            r.raise_for_status()
            state["channel_id"] = r.json()["id"]
            print(f"[bootstrap] octo 渠道已创建 channel_id={state['channel_id']}")
        _save(state)
    else:
        print(f"[bootstrap] 复用渠道 channel_id={state['channel_id']}")

    cid = state["channel_id"]
    status = api.get(f"/channels/{cid}/status").json()
    print(f"[bootstrap] 渠道状态：{status}")
    print()
    print("下一步：")
    print(f"  watch:   ./watch.sh {cid}")
    print(f"  status:  curl -s -H 'X-User-ID: {args.user}' {args.api}/channels/{cid}/status")
    print(f"  redis:   docker exec agentscope-redis redis-cli HGETALL agentscope:channel:liveness:{cid}")
    print(f"  在 Octo 里私聊机器人（{reg.get('name') or robot_id}），或在群里 @它 发消息")


if __name__ == "__main__":
    main()
