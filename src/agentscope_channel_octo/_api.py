# -*- coding: utf-8 -*-
"""The Octo Bot REST API, authenticated with the bot token.

Everything a bot does besides listening goes through here: register (to
obtain the IM token the socket connects with), send a message, show
typing, heartbeat its online status, look up groups and users, upload a
file, and poll the card-click events a tool approval comes back on.
Paths follow ``skills/octo-bot-api/SKILL.md`` of openclaw-channel-octo.
"""
from __future__ import annotations

import asyncio
import uuid
from typing import Any
from urllib.parse import quote

import httpx

from agentscope import __version__ as _agentscope_version

from ._version import __version__

_DEFAULT_TIMEOUT = 30.0
_MAX_429_RETRIES = 2
_MAX_RETRY_AFTER_SECS = 10.0
_MAX_DOWNLOAD_BYTES = 20 * 1024 * 1024

CT_DM = 1
"""channel_type of a direct message."""
CT_GROUP = 2
"""channel_type of a group."""
CT_THREAD = 5
"""channel_type of a thread (sub-topic) inside a group."""

MT_TEXT = 1
MT_IMAGE = 2
MT_GIF = 3
MT_VOICE = 4
MT_VIDEO = 5
MT_LOCATION = 6
MT_CONTACT = 7
MT_FILE = 8
MT_RICH_TEXT = 14
MT_CARD = 17


class OctoAPIError(Exception):
    """A non-2xx answer, or a malformed one, from the Octo API."""

    def __init__(self, status_code: int, path: str, body: str = "") -> None:
        """Record what failed.

        Args:
            status_code (`int`): The HTTP status (``0`` when not HTTP).
            path (`str`): The API path that was called.
            body (`str`): The response body, truncated.
        """
        super().__init__(
            f"Octo API {path} failed ({status_code})"
            + (f": {body}" if body else ""),
        )
        self.status_code = status_code
        self.path = path
        self.body = body

    @property
    def is_rate_limited(self) -> bool:
        """Whether the server asked us to slow down."""
        return self.status_code == 429


class OctoBotAPI:
    """Thin async client over the ``/v1/bot`` endpoints."""

    def __init__(
        self,
        http: httpx.AsyncClient,
        api_url: str,
        bot_token: str,
    ) -> None:
        """Bind the HTTP client and the bot's credentials.

        Args:
            http (`httpx.AsyncClient`): The client to issue requests on.
            api_url (`str`): The server's API base, e.g.
                ``https://octo.example.com/api``.
            bot_token (`str`): The bot token from BotFather.
        """
        self._http = http
        self._base = api_url.rstrip("/")
        self._token = bot_token

    @property
    def api_url(self) -> str:
        """The API base URL without a trailing slash."""
        return self._base

    def _headers(self) -> dict[str, str]:
        return {"Authorization": f"Bearer {self._token}"}

    async def _request(
        self,
        method: str,
        path: str,
        *,
        json: dict[str, Any] | None = None,
        params: dict[str, Any] | None = None,
        timeout: float = _DEFAULT_TIMEOUT,
        retry_429: bool = True,
    ) -> Any:
        """Issue one request and return the decoded JSON (or ``None``).

        Args:
            method (`str`): The HTTP method.
            path (`str`): The path under the API base.
            json (`dict | None`): The JSON body.
            params (`dict | None`): Query parameters.
            timeout (`float`): Per-attempt timeout in seconds.
            retry_429 (`bool`): Whether to honour ``Retry-After`` on 429.
        """
        url = f"{self._base}{path}"
        attempt = 0
        while True:
            response = await self._http.request(
                method,
                url,
                json=json,
                params=params,
                headers=self._headers(),
                timeout=timeout,
            )
            if (
                response.status_code == 429
                and retry_429
                and attempt < _MAX_429_RETRIES
            ):
                attempt += 1
                await asyncio.sleep(_retry_after(response))
                continue
            if response.status_code >= 400:
                raise OctoAPIError(
                    response.status_code,
                    path,
                    response.text[:500],
                )
            if not response.content:
                return None
            try:
                return response.json()
            except ValueError as e:
                raise OctoAPIError(
                    response.status_code,
                    path,
                    f"invalid JSON: {response.text[:200]}",
                ) from e

    # -- Session --

    async def register(self, *, force_refresh: bool = False) -> dict:
        """Register the bot and obtain its IM token and WebSocket URL.

        Args:
            force_refresh (`bool`): Rotate the IM token (invalidates the
                one other connections hold).
        """
        path = "/v1/bot/register"
        if force_refresh:
            path += "?force_refresh=true"
        data = await self._request(
            "POST",
            path,
            json={
                "agent_platform": "agentscope",
                "agent_version": _agentscope_version,
                "plugin_version": __version__,
            },
        )
        if not isinstance(data, dict) or not data.get("robot_id"):
            raise OctoAPIError(0, path, "registration returned no robot_id")
        return data

    async def heartbeat(self) -> None:
        """Keep the bot shown as online."""
        await self._request(
            "POST",
            "/v1/bot/heartbeat",
            json={},
            timeout=10.0,
            retry_429=False,
        )

    # -- Messaging --

    async def send_message(
        self,
        channel_id: str,
        channel_type: int,
        payload: dict[str, Any],
        *,
        client_msg_no: str | None = None,
    ) -> dict | None:
        """Send one message payload to a chat.

        Args:
            channel_id (`str`): Target uid / group_no / thread id.
            channel_type (`int`): ``CT_DM`` / ``CT_GROUP`` / ``CT_THREAD``.
            payload (`dict`): The message payload (``type`` + fields).
            client_msg_no (`str | None`): Idempotency key; generated when
                omitted.
        """
        if not channel_id or not channel_id.strip():
            raise ValueError("channel_id is required to send a message")
        data = await self._request(
            "POST",
            "/v1/bot/sendMessage",
            json={
                "channel_id": channel_id,
                "channel_type": channel_type,
                "payload": payload,
                "client_msg_no": client_msg_no or uuid.uuid4().hex,
            },
        )
        return data if isinstance(data, dict) else None

    async def send_text(
        self,
        channel_id: str,
        channel_type: int,
        content: str,
        *,
        mention_uids: list[str] | None = None,
        reply_message_id: str | None = None,
    ) -> dict | None:
        """Send a text message.

        Args:
            channel_id (`str`): Target uid / group_no / thread id.
            channel_type (`int`): ``CT_DM`` / ``CT_GROUP`` / ``CT_THREAD``.
            content (`str`): The text (Markdown is rendered by clients).
            mention_uids (`list[str] | None`): Users to @mention.
            reply_message_id (`str | None`): Message to quote-reply to.
        """
        payload: dict[str, Any] = {"type": MT_TEXT, "content": content}
        if mention_uids:
            payload["mention"] = {"uids": list(mention_uids)}
        if reply_message_id:
            payload["reply"] = {"message_id": reply_message_id}
        return await self.send_message(channel_id, channel_type, payload)

    async def edit_message(
        self,
        message_id: str,
        channel_id: str,
        channel_type: int,
        payload: dict[str, Any],
    ) -> None:
        """Replace a previously sent bot message (used for cards).

        Args:
            message_id (`str`): The message to edit.
            channel_id (`str`): The chat it was sent to.
            channel_type (`int`): The chat's channel type.
            payload (`dict`): The complete replacement payload.
        """
        import json as _json

        await self._request(
            "POST",
            "/v1/bot/message/edit",
            json={
                "message_id": message_id,
                "channel_id": channel_id,
                "channel_type": channel_type,
                "content_edit": _json.dumps(payload, ensure_ascii=False),
            },
        )

    async def typing(self, channel_id: str, channel_type: int) -> None:
        """Show the typing indicator in a chat.

        Args:
            channel_id (`str`): The chat.
            channel_type (`int`): The chat's channel type.
        """
        await self._request(
            "POST",
            "/v1/bot/typing",
            json={"channel_id": channel_id, "channel_type": channel_type},
            timeout=10.0,
            retry_429=False,
        )

    async def read_receipt(self, channel_id: str, channel_type: int) -> None:
        """Mark a chat's messages as read.

        Args:
            channel_id (`str`): The chat.
            channel_type (`int`): The chat's channel type.
        """
        await self._request(
            "POST",
            "/v1/bot/readReceipt",
            json={"channel_id": channel_id, "channel_type": channel_type},
            timeout=10.0,
            retry_429=False,
        )

    # -- Directory --

    async def list_groups(self, *, timeout: float = 10.0) -> list[dict]:
        """Groups the bot is a member of (``group_no`` + ``name``)."""
        data = await self._request("GET", "/v1/bot/groups", timeout=timeout)
        return _as_list(data, "groups", "list")

    async def group_info(self, group_no: str, *, timeout: float = 5.0) -> dict:
        """One group's profile (name, notice, creator).

        Args:
            group_no (`str`): The group.
            timeout (`float`): Request timeout in seconds.
        """
        data = await self._request(
            "GET",
            f"/v1/bot/groups/{quote(group_no, safe='')}",
            timeout=timeout,
        )
        return data if isinstance(data, dict) else {}

    async def group_members(
        self,
        group_no: str,
        *,
        timeout: float = 5.0,
    ) -> list[dict]:
        """A group's members (``uid``, ``name``, ``role``, ``robot``).

        Args:
            group_no (`str`): The group.
            timeout (`float`): Request timeout in seconds.
        """
        data = await self._request(
            "GET",
            f"/v1/bot/groups/{quote(group_no, safe='')}/members",
            timeout=timeout,
        )
        return _as_list(data, "members", "list")

    async def user_info(
        self, uid: str, *, timeout: float = 5.0
    ) -> dict | None:
        """A user's profile, or ``None`` when the server has no endpoint.

        Args:
            uid (`str`): The user.
            timeout (`float`): Request timeout in seconds.
        """
        try:
            data = await self._request(
                "GET",
                "/v1/bot/user/info",
                params={"uid": uid},
                timeout=timeout,
            )
        except OctoAPIError as e:
            if e.status_code in (404, 403):
                return None
            raise
        return data if isinstance(data, dict) and data.get("name") else None

    # -- Cards --

    async def card_profile(self) -> dict | None:
        """The per-bot card policy manifest, or ``None`` when unsupported."""
        try:
            data = await self._request(
                "GET",
                "/v1/bot/card/profile",
                timeout=10.0,
            )
        except OctoAPIError as e:
            if e.status_code in (403, 404):
                return None
            raise
        return data if isinstance(data, dict) else None

    async def fetch_events(
        self,
        since_event_id: int,
        *,
        limit: int = 50,
        wait_secs: int = 0,
    ) -> list[dict]:
        """Poll the bot's event queue (card clicks and server tasks).

        Args:
            since_event_id (`int`): Return events after this id.
            limit (`int`): Max events per poll (1-100).
            wait_secs (`int`): Server-side long-poll hold; ``0`` = short
                poll.
        """
        body: dict[str, Any] = {
            "event_id": since_event_id,
            "limit": max(1, min(100, limit)),
        }
        if wait_secs > 0:
            body["wait"] = min(30, wait_secs)
        data = await self._request(
            "POST",
            "/v1/bot/events",
            json=body,
            timeout=float(wait_secs) + 10.0,
            retry_429=False,
        )
        if isinstance(data, dict):
            return _as_list(data.get("results"), "results")
        return _as_list(data, "results")

    async def ack_event(self, event_id: int) -> None:
        """Acknowledge (delete) one processed event.

        Args:
            event_id (`int`): The event to acknowledge.
        """
        await self._request(
            "POST",
            f"/v1/bot/events/{int(event_id)}/ack",
            json={},
            timeout=10.0,
            retry_429=False,
        )

    # -- Files --

    async def presign_upload(
        self,
        filename: str,
        size: int,
        content_type: str | None = None,
    ) -> dict:
        """Ask for a presigned PUT URL for one file.

        Args:
            filename (`str`): The file name shown to users.
            size (`int`): Exact byte size of the upload.
            content_type (`str | None`): MIME type hint.
        """
        params: dict[str, Any] = {"filename": filename, "fileSize": size}
        if content_type:
            params["contentType"] = content_type
        data = await self._request(
            "GET",
            "/v1/bot/upload/presigned",
            params=params,
        )
        if (
            not isinstance(data, dict)
            or not isinstance(data.get("uploadUrl"), str)
            or not isinstance(data.get("downloadUrl"), str)
        ):
            raise OctoAPIError(
                0,
                "/v1/bot/upload/presigned",
                "incomplete presign response",
            )
        return data

    async def upload_presigned(self, presign: dict, data: bytes) -> str:
        """PUT the bytes to the presigned URL; returns the download URL.

        Args:
            presign (`dict`): The :meth:`presign_upload` answer.
            data (`bytes`): The exact bytes the presign was asked for.
        """
        headers = {
            "Content-Type": presign.get("contentType")
            or "application/octet-stream",
            "Content-Length": str(len(data)),
        }
        if isinstance(presign.get("contentDisposition"), str):
            headers["Content-Disposition"] = presign["contentDisposition"]
        response = await self._http.put(
            presign["uploadUrl"],
            content=data,
            headers=headers,
            timeout=120.0,
        )
        if response.status_code >= 400:
            raise OctoAPIError(
                response.status_code,
                "presigned PUT",
                response.text[:300],
            )
        return presign["downloadUrl"]

    async def download(
        self,
        url: str,
        *,
        max_bytes: int = _MAX_DOWNLOAD_BYTES,
    ) -> tuple[bytes, str]:
        """Fetch a media attachment; returns ``(bytes, content_type)``.

        Absolute URLs are fetched as-is; a storage path is fetched via
        the authenticated download route (a 302 to a presigned URL).

        Args:
            url (`str`): Absolute URL or the relative storage path a
                payload carries.
            max_bytes (`int`): Refuse bodies larger than this.
        """
        if url.startswith("http://") or url.startswith("https://"):
            target = url
            headers: dict[str, str] = {}
        else:
            path = url
            for prefix in ("file/preview/", "file/"):
                if path.startswith(prefix):
                    path = path[len(prefix) :]
                    break
            target = f"{self._base}/v1/bot/file/download/{path.lstrip('/')}"
            headers = self._headers()
        async with self._http.stream(
            "GET",
            target,
            headers=headers,
            timeout=120.0,
            follow_redirects=True,
        ) as response:
            if response.status_code >= 400:
                raise OctoAPIError(response.status_code, "download")
            declared = response.headers.get("content-length")
            if declared and declared.isdigit() and int(declared) > max_bytes:
                raise OctoAPIError(0, "download", "attachment too large")
            chunks = bytearray()
            async for chunk in response.aiter_bytes():
                chunks += chunk
                if len(chunks) > max_bytes:
                    raise OctoAPIError(0, "download", "attachment too large")
            content_type = (
                response.headers.get("content-type", "").split(";")[0].strip()
            )
        return bytes(chunks), content_type


def _retry_after(response: httpx.Response) -> float:
    """Seconds to wait after a 429, capped.

    Args:
        response (`httpx.Response`): The 429 response.
    """
    raw = response.headers.get("retry-after", "")
    try:
        secs = float(raw)
    except ValueError:
        secs = 1.0
    return max(0.5, min(secs, _MAX_RETRY_AFTER_SECS))


def _as_list(data: Any, *keys: str) -> list[dict]:
    """Normalise a bare array or ``{key: [...]}`` envelope to a list.

    Args:
        data (`Any`): The decoded response.
        *keys (`str`): Envelope keys to look under, in order.
    """
    if isinstance(data, list):
        return [item for item in data if isinstance(item, dict)]
    if isinstance(data, dict):
        for key in keys:
            if isinstance(data.get(key), list):
                return [item for item in data[key] if isinstance(item, dict)]
    return []
