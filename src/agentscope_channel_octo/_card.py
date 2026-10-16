# -*- coding: utf-8 -*-
"""Interactive-card payloads for tool approvals.

Octo renders Adaptive Cards 1.5 (``payload.type=17``). The ``octo/v2``
profile adds ``Action.Submit`` buttons whose clicks come back as
``card_action`` events on ``POST /v1/bot/events``; the ``data`` a button
carries is echoed verbatim, which is how a click finds its way back to
the exact run that asked.
"""
from __future__ import annotations

from typing import Any

from ._api import MT_CARD

CARD_VERSION = "1.5"
PROFILE_DISPLAY = "octo/v1"
PROFILE_INTERACTIVE = "octo/v2"
CARD_KIND = "agentscope_tool_confirm"
"""Marks a card (and its click events) as an AgentScope approval."""
ACTION_APPROVE = "approve"
ACTION_REJECT = "reject"

_SCHEMA = "http://adaptivecards.io/schemas/adaptive-card.json"


def build_approval_card(
    *,
    tool_call_id: str,
    chat_id: str,
    tool_name: str,
    tool_input: str,
    agent_id: str,
    session_id: str,
) -> dict[str, Any]:
    """An approve/reject card for one pending tool call.

    Args:
        tool_call_id (`str`): The tool call awaiting confirmation.
        chat_id (`str`): The channel-encoded chat the card is posted in.
        tool_name (`str`): The tool's name.
        tool_input (`str`): The call arguments, already truncated.
        agent_id (`str`): The agent running the session.
        session_id (`str`): The parked session to resume on click.
    """
    base = {
        "kind": CARD_KIND,
        "tool_call_id": tool_call_id,
        "chat_id": chat_id,
        "agent_id": agent_id,
        "session_id": session_id,
    }
    return {
        "type": MT_CARD,
        "profile": PROFILE_INTERACTIVE,
        "card_version": CARD_VERSION,
        "card": {
            "type": "AdaptiveCard",
            "version": CARD_VERSION,
            "$schema": _SCHEMA,
            "body": [
                {
                    "type": "TextBlock",
                    "text": "⚠️ 工具调用需要确认",
                    "weight": "Bolder",
                    "size": "Medium",
                    "wrap": True,
                },
                {
                    "type": "FactSet",
                    "facts": [
                        {"title": "工具", "value": tool_name},
                        {"title": "参数", "value": tool_input or "(无)"},
                    ],
                },
            ],
            "actions": [
                {
                    "type": "Action.Submit",
                    "id": ACTION_APPROVE,
                    "title": "批准",
                    "style": "positive",
                    "data": {**base, "decision": ACTION_APPROVE},
                },
                {
                    "type": "Action.Submit",
                    "id": ACTION_REJECT,
                    "title": "拒绝",
                    "style": "destructive",
                    "data": {**base, "decision": ACTION_REJECT},
                },
            ],
        },
        "plain": f"工具调用需要确认：{tool_name}",
    }


def build_decision_card(
    *,
    tool_name: str,
    approved: bool,
    operator: str,
) -> dict[str, Any]:
    """The display-only card an approval card turns into once clicked.

    Args:
        tool_name (`str`): The tool that was decided on.
        approved (`bool`): The decision.
        operator (`str`): Who clicked.
    """
    verdict = "✅ 已批准" if approved else "❌ 已拒绝"
    return {
        "type": MT_CARD,
        "profile": PROFILE_DISPLAY,
        "card_version": CARD_VERSION,
        "card": {
            "type": "AdaptiveCard",
            "version": CARD_VERSION,
            "$schema": _SCHEMA,
            "body": [
                {
                    "type": "TextBlock",
                    "text": f"{verdict}：{tool_name}",
                    "weight": "Bolder",
                    "wrap": True,
                },
                {
                    "type": "TextBlock",
                    "text": f"操作人：{operator or '未知'}",
                    "isSubtle": True,
                    "wrap": True,
                },
            ],
        },
        "plain": f"{verdict}：{tool_name}",
    }


def cards_enabled(profile: dict[str, Any] | None) -> bool:
    """Whether the server lets this bot send ``octo/v2`` submit cards.

    Follows the manifest rules of the reference adapter: the per-bot
    ``config.interaction_enabled`` must be exactly ``True``, and when the
    manifest lists profiles or a version they must match.

    Args:
        profile (`dict | None`): ``GET /v1/bot/card/profile``, or
            ``None`` when the route does not exist.
    """
    if not isinstance(profile, dict):
        return False
    config = profile.get("config")
    if not isinstance(config, dict):
        return False
    if config.get("interaction_enabled") is not True:
        return False
    profiles = profile.get("profiles")
    if isinstance(profiles, list) and PROFILE_INTERACTIVE not in profiles:
        return False
    version = profile.get("card_version")
    if isinstance(version, str) and version != CARD_VERSION:
        return False
    return True
