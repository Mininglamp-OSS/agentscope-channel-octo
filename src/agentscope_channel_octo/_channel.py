# -*- coding: utf-8 -*-
"""Octo IM channel.

Octo (an IM built on WuKongIM) delivers a bot's inbound messages over one
WebSocket long connection and takes everything else over REST with the
bot token: replies, typing, group lookups, file uploads, and the card
clicks a tool approval comes back on. That split is exactly the channel
runtime's: :meth:`start_listening` is the only method that touches the
socket, and every other method is plain REST against the stored
credentials, so an instance built by
:class:`~agentscope.app.channel.ChannelClients` (never listened) can
send a reply, list chats and attach the platform tools from a replica
that holds no connection.

The connection itself is WuKongIM's binary protocol (see
:mod:`._protocol`); the REST surface is in :mod:`._api`. Both follow
openclaw-channel-octo, the reference adapter for Octo bots.

Chat ids are self-describing so a reply knows its channel type without
a lookup: ``dm:<uid>``, ``group:<group_no>`` and
``thread:<group_no>____<short_id>``.
"""
from __future__ import annotations

import asyncio
import base64
import json
import mimetypes
import re
import struct
import time
from typing import Any, AsyncIterator, Awaitable, Callable, TYPE_CHECKING

import httpx
from pydantic import BaseModel, Field, TypeAdapter

try:
    from agentscope._logging import logger
except ImportError:  # pragma: no cover
    import logging

    logger = logging.getLogger("agentscope_channel_octo")
from agentscope.event import (
    AgentEvent,
    ReplyEndEvent,
    RequireUserConfirmEvent,
)
from agentscope.message import Base64Source, DataBlock, Msg, TextBlock
from agentscope.app.channel import (
    ChannelBase,
    ChannelCapability,
    ChannelConfirmationResultEvent,
    ChannelEvent,
    ChannelStatus,
    ChatKind,
)
from ._api import (
    CT_DM,
    CT_GROUP,
    CT_THREAD,
    MT_CARD,
    MT_CONTACT,
    MT_FILE,
    MT_GIF,
    MT_IMAGE,
    MT_LOCATION,
    MT_RICH_TEXT,
    MT_TEXT,
    MT_VIDEO,
    MT_VOICE,
    OctoAPIError,
    OctoBotAPI,
)
from ._card import (
    ACTION_APPROVE,
    ACTION_REJECT,
    CARD_KIND,
    build_approval_card,
    build_decision_card,
    cards_enabled,
)
from ._protocol import (
    PACKET_CONNACK,
    PACKET_DISCONNECT,
    PACKET_PONG,
    PACKET_RECV,
    FrameBuffer,
    RecvPacket,
    SessionCrypto,
    encode_connect,
    encode_ping,
    encode_recvack,
    parse_connack,
    parse_disconnect,
    parse_recv,
)

if TYPE_CHECKING:
    from agentscope.tool import ToolBase
    from agentscope.workspace import WorkspaceBase

# Deserialise a bus event dict back into its typed AgentEvent.
_EVENT_ADAPTER: TypeAdapter = TypeAdapter(AgentEvent)

THREAD_SEP = "____"
"""Separator between group_no and short_id in a thread's channel id."""

# Bound on the handshake: TCP + upgrade + CONNECT + CONNACK.
_CONNECT_TIMEOUT_SECS = 15.0
# Application-level PING cadence and how many unanswered ones drop the
# socket (the socket then reconnects).
_PING_INTERVAL_SECS = 30.0
_PING_MAX_MISSED = 3
# Give up (and park in 'failed') after this many connects that never came
# up — a bad token or URL; editing the channel re-tries.
_MAX_CONNECT_ATTEMPTS = 2
_MAX_BACKOFF_SECS = 60.0
# A connection that lived this long resets the reconnect backoff.
_STABLE_AFTER_SECS = 30.0
# Card-click polling.
_EVENTS_POLL_SECS = 2.0
_EVENTS_MAX_BACKOFF_SECS = 30.0
_NAME_CACHE_TTL_SECS = 3600.0
_INPUT_PREVIEW_CHARS = 800

_EmitFn = Callable[
    ["ChannelEvent | ChannelConfirmationResultEvent"],
    Awaitable[None],
]


def encode_chat_id(channel_id: str, channel_type: int) -> str:
    """Encode a platform ``(channel_id, channel_type)`` as one chat id.

    Args:
        channel_id (`str`): uid, group_no or ``group_no____short_id``.
        channel_type (`int`): ``CT_DM`` / ``CT_GROUP`` / ``CT_THREAD``.
    """
    if channel_type == CT_DM:
        return f"dm:{channel_id}"
    if channel_type == CT_THREAD or THREAD_SEP in channel_id:
        return f"thread:{channel_id}"
    return f"group:{channel_id}"


def decode_chat_id(chat_id: str) -> tuple[str, int]:
    """Invert :func:`encode_chat_id`; bare ids are read like the SDK does.

    Args:
        chat_id (`str`): An encoded chat id, or a bare platform id.
    """
    kind, sep, rest = chat_id.partition(":")
    if sep and kind in ("dm", "user"):
        return rest, CT_DM
    if sep and kind == "thread":
        return rest, CT_THREAD
    if sep and kind in ("group", "channel", "octo"):
        return rest, (CT_THREAD if THREAD_SEP in rest else CT_GROUP)
    if THREAD_SEP in chat_id:
        return chat_id, CT_THREAD
    return chat_id, CT_DM


def _flag(value: Any) -> bool:
    """A mention flag the API sends as ``true`` or ``1``."""
    return value is True or value == 1


def _as_int(value: Any) -> int | None:
    """An int from an int or a numeric string, else ``None``."""
    if isinstance(value, bool):
        return None
    if isinstance(value, int):
        return value
    if isinstance(value, str) and value.strip().lstrip("-").isdigit():
        return int(value.strip())
    return None


def _strip_mentions(text: str, entities: Any, uid: str) -> str:
    """Remove the ``@name`` spans that mention ``uid`` from ``text``.

    Entity offsets count UTF-16 code units, as the platform defines.

    Args:
        text (`str`): The message text.
        entities (`Any`): ``payload.mention.entities``.
        uid (`str`): The mentioned user whose spans are removed.
    """
    spans: list[tuple[int, int]] = []
    for entity in entities if isinstance(entities, list) else []:
        if not isinstance(entity, dict) or str(entity.get("uid")) != uid:
            continue
        offset = _as_int(entity.get("offset"))
        length = _as_int(entity.get("length"))
        if offset is None or length is None or length <= 0:
            continue
        spans.append((offset, length))
    if not spans:
        return text
    units = text.encode("utf-16-le")
    for offset, length in sorted(spans, reverse=True):
        start, end = offset * 2, (offset + length) * 2
        if 0 <= start <= end <= len(units):
            units = units[:start] + units[end:]
    return units.decode("utf-16-le", errors="ignore")


def _image_size(data: bytes, media_type: str) -> tuple[int, int] | None:
    """Read a PNG/GIF/JPEG's pixel size from its header, else ``None``.

    Args:
        data (`bytes`): The image bytes.
        media_type (`str`): Its MIME type.
    """
    try:
        if media_type == "image/png" and data[:8] == b"\x89PNG\r\n\x1a\n":
            width, height = struct.unpack(">II", data[16:24])
            return width, height
        if media_type == "image/gif" and data[:4] == b"GIF8":
            width, height = struct.unpack("<HH", data[6:10])
            return width, height
        if media_type == "image/jpeg" and data[:2] == b"\xff\xd8":
            pos = 2
            while pos + 9 < len(data):
                if data[pos] != 0xFF:
                    pos += 1
                    continue
                marker = data[pos + 1]
                if marker in (0xD8, 0x01) or 0xD0 <= marker <= 0xD7:
                    pos += 2
                    continue
                size = struct.unpack(">H", data[pos + 2 : pos + 4])[0]
                if 0xC0 <= marker <= 0xCF and marker not in (
                    0xC4,
                    0xC8,
                    0xCC,
                ):
                    height, width = struct.unpack(
                        ">HH",
                        data[pos + 5 : pos + 9],
                    )
                    return width, height
                pos += 2 + size
    except (struct.error, IndexError):
        return None
    return None


class _ConnectRejected(RuntimeError):
    """CONNACK refused the session (bad token, revoked bot, ...)."""


class _Kicked(RuntimeError):
    """The server closed the session on purpose."""


class OctoChannel(ChannelBase):
    """Octo IM platform channel."""

    channel_type = "octo"
    display_name = "Octo IM"
    description = "Bot for Octo direct messages, groups and threads."
    platform_bot_id_field = "robot_id"

    class Credentials(BaseModel):
        """Octo bot credentials."""

        api_url: str = Field(
            title="API URL",
            description="Octo server API base URL, e.g. "
            "https://octo.example.com/api",
        )
        bot_token: str = Field(
            title="Bot Token",
            description="Token issued by BotFather (/newbot)",
            json_schema_extra={"format": "password"},
        )
        robot_id: str = Field(
            title="Robot ID",
            description="The bot's uid (ends with _bot), as returned by "
            "POST /v1/bot/register",
        )
        ws_url: str = Field(
            default="",
            title="WebSocket URL",
            description="Override the ws_url returned by registration "
            "(leave empty to use the server's)",
        )

    class Config(BaseModel):
        """Octo platform options."""

        only_at_reply: bool = Field(
            default=True,
            title="Reply only when mentioned",
            description="In groups and threads, reply only when the bot "
            "is @mentioned; direct messages always get a reply",
        )
        reply_mention_sender: bool = Field(
            default=True,
            title="Mention the sender",
            description="@mention the user when replying in a group",
        )
        ignore_bots: bool = Field(
            default=True,
            title="Ignore other bots",
            description="Drop messages sent by other bots (uid ending "
            "with _bot) to avoid bot-to-bot loops",
        )
        typing_indicator: bool = Field(
            default=True,
            title="Typing indicator",
            description="Show 'typing…' while the reply is produced",
        )
        card_approval: bool = Field(
            default=True,
            title="Approval cards",
            description="Ask for tool approvals with an interactive card "
            "(needs the server's card feature for this bot)",
        )
        show_tool_process: bool = Field(
            default=False,
            title="Show tool process",
            description="Show tool calls and results inline in the reply",
        )
        show_thinking: bool = Field(
            default=False,
            title="Show thinking",
            description="Show the model's reasoning inline in the reply",
        )
        heartbeat_interval_secs: int = Field(
            default=30,
            ge=5,
            title="Heartbeat interval (s)",
            description="How often the bot reports itself online",
        )
        ws_proxy: str = Field(
            default="",
            title="WebSocket proxy",
            description="HTTP/SOCKS proxy URL for the long connection; "
            "empty connects directly (environment proxies are ignored, "
            "as the reference adapter does)",
        )

    capabilities = ChannelCapability(
        text=True,
        markdown=True,
        image=True,
        file=True,
        interactive=True,
        streaming=False,
        max_message_length=4000,
    )

    def __init__(
        self,
        channel_id: str,
        credentials: "OctoChannel.Credentials",
        config: "OctoChannel.Config",
    ) -> None:
        """Store identity and options; nothing connects until listened.

        Args:
            channel_id (`str`): This channel instance's unique id.
            credentials (`OctoChannel.Credentials`): Validated secrets.
            config (`OctoChannel.Config`): Validated platform options.
        """
        self._channel_id = channel_id
        self._credentials = credentials
        self._config = config
        self.status = ChannelStatus()
        self._http: httpx.AsyncClient | None = None
        self._api: OctoBotAPI | None = None
        self._stopped = False
        self._ws: Any = None
        self._im_token = ""
        self._ws_url = credentials.ws_url
        self._bot_name = ""
        self._owner_uid = ""
        self._server_version = 0
        self._ever_connected = False
        self._pong_pending = 0
        self._events_cursor = 0
        self._cards_supported: bool | None = None
        self._group_names: dict[str, tuple[str, float]] = {}
        self._user_names: dict[str, tuple[str, float]] = {}
        self._members_refreshed: dict[str, float] = {}
        self._inflight: set[asyncio.Task] = set()

    # -- Identity & REST client --

    @property
    def channel_id(self) -> str:
        """The unique channel instance identifier."""
        return self._channel_id

    @property
    def robot_id(self) -> str:
        """The bot's own uid."""
        return self._credentials.robot_id

    def _client(self) -> OctoBotAPI:
        """The REST client, created lazily so unconnected instances work."""
        if self._api is None:
            self._http = httpx.AsyncClient(
                timeout=httpx.Timeout(30.0),
                follow_redirects=True,
            )
            self._api = OctoBotAPI(
                self._http,
                self._credentials.api_url,
                self._credentials.bot_token,
            )
        return self._api

    async def aclose(self) -> None:
        """Release the HTTP client a connection-free instance opened."""
        http, self._http, self._api = self._http, None, None
        if http is not None:
            await http.aclose()

    def _decode(self, chat_id: str) -> tuple[str, int]:
        """Decode a chat id, reading a bare known group as a group.

        Args:
            chat_id (`str`): The chat id to decode.
        """
        channel_id, channel_type = decode_chat_id(chat_id)
        if (
            ":" not in chat_id
            and channel_type == CT_DM
            and chat_id in self._group_names
        ):
            return chat_id, CT_GROUP
        return channel_id, channel_type

    # -- Connection lifecycle --

    async def start_listening(self, emit: _EmitFn) -> None:
        """Register, open the long connection and loop on it until
        cancelled; reconnect with backoff after a drop.

        Args:
            emit (`Callable`): Gateway callback for inbound events.
        """
        self._emit = emit
        self._stopped = False
        self.status.state = "connecting"
        self._ever_connected = False
        attempts = 0
        backoff = 1.0
        rejected = False
        try:
            while not self._stopped:
                started = time.monotonic()
                try:
                    await self._register(force_refresh=rejected)
                    rejected = False
                    await self._run_connection()
                    if not self._stopped:
                        self.status.state = "retrying"
                        self.status.last_error = "connection closed"
                except asyncio.CancelledError:
                    raise
                except _ConnectRejected as e:
                    rejected = True
                    self.status.state = "retrying"
                    self.status.last_error = str(e)
                    logger.error("Octo '%s': %s", self._channel_id, e)
                except Exception as e:  # pylint: disable=broad-except
                    self.status.state = "retrying"
                    self.status.last_error = str(e)
                    logger.exception(
                        "Octo '%s' connection error",
                        self._channel_id,
                    )
                if self._stopped:
                    break
                if time.monotonic() - started >= _STABLE_AFTER_SECS:
                    backoff = 1.0
                if not self._ever_connected:
                    attempts += 1
                    if attempts >= _MAX_CONNECT_ATTEMPTS:
                        self.status.state = "failed"
                        logger.error(
                            "Octo '%s' gave up after %d connect attempts: "
                            "%s",
                            self._channel_id,
                            attempts,
                            self.status.last_error,
                        )
                        # Park until the dispatcher cancels us (an edit of
                        # the channel record restarts a fresh instance).
                        await asyncio.Event().wait()
                await asyncio.sleep(backoff)
                backoff = min(backoff * 2, _MAX_BACKOFF_SECS)
        finally:
            self._stopped = True
            self.status.state = "stopped"
            for task in list(self._inflight):
                task.cancel()
            await asyncio.gather(*self._inflight, return_exceptions=True)
            await self.aclose()

    async def _register(self, *, force_refresh: bool = False) -> None:
        """Obtain the IM token and socket URL for this bot.

        Args:
            force_refresh (`bool`): Rotate the IM token first.
        """
        data = await self._client().register(force_refresh=force_refresh)
        self._im_token = str(data.get("im_token") or "")
        if not self._credentials.ws_url:
            self._ws_url = str(data.get("ws_url") or "")
        self._bot_name = str(data.get("name") or "")
        self._owner_uid = str(data.get("owner_uid") or "")
        robot_id = str(data.get("robot_id") or "")
        if robot_id and robot_id != self.robot_id:
            logger.warning(
                "Octo '%s': registered as %s but credentials say %s",
                self._channel_id,
                robot_id,
                self.robot_id,
            )
        if not self._im_token or not self._ws_url:
            raise RuntimeError(
                "Octo registration returned no im_token / ws_url",
            )

    async def _run_connection(self) -> bool:
        """Run one socket session; returns whether CONNACK accepted it.

        Returns normally when the socket drops, raises
        :class:`_ConnectRejected` / :class:`_Kicked` when the server
        refuses or ends the session.
        """
        from websockets.asyncio.client import connect
        from websockets.exceptions import ConnectionClosed

        crypto = SessionCrypto()
        frames = FrameBuffer()
        connected = False
        tasks: list[asyncio.Task] = []
        async with connect(
            self._ws_url,
            ping_interval=None,
            max_size=16 * 1024 * 1024,
            open_timeout=_CONNECT_TIMEOUT_SECS,
            proxy=self._config.ws_proxy or None,
        ) as ws:
            self._ws = ws
            await ws.send(
                encode_connect(
                    uid=self.robot_id,
                    token=self._im_token,
                    client_key=crypto.client_key,
                ),
            )
            try:
                while True:
                    raw = await asyncio.wait_for(
                        ws.recv(),
                        timeout=None if connected else _CONNECT_TIMEOUT_SECS,
                    )
                    if isinstance(raw, str):
                        continue
                    for frame in frames.feed(raw):
                        if frame.packet_type == PACKET_CONNACK:
                            ack = parse_connack(frame)
                            if not ack.accepted:
                                raise _ConnectRejected(
                                    "CONNACK rejected the session "
                                    f"(reason_code={ack.reason_code})",
                                )
                            self._server_version = ack.server_version
                            crypto.complete(ack)
                            connected = True
                            self._ever_connected = True
                            self._pong_pending = 0
                            self.status.state = "connected"
                            self.status.last_error = ""
                            logger.info(
                                "Octo '%s' connected as %s",
                                self._channel_id,
                                self.robot_id,
                            )
                            tasks = [
                                asyncio.create_task(self._ping_loop(ws)),
                                asyncio.create_task(self._heartbeat_loop()),
                                asyncio.create_task(self._events_loop()),
                            ]
                        elif frame.packet_type == PACKET_RECV:
                            if not connected:
                                continue
                            packet = parse_recv(frame, self._server_version)
                            await ws.send(
                                encode_recvack(
                                    packet.message_id,
                                    packet.message_seq,
                                ),
                            )
                            self._dispatch_recv(packet, crypto)
                        elif frame.packet_type == PACKET_PONG:
                            self._pong_pending = 0
                        elif frame.packet_type == PACKET_DISCONNECT:
                            info = parse_disconnect(frame)
                            raise _Kicked(
                                "disconnected by server "
                                f"(reason_code={info.reason_code} "
                                f"{info.reason})".rstrip(),
                            )
            except asyncio.TimeoutError as e:
                raise RuntimeError(
                    "no CONNACK within the connect deadline",
                ) from e
            except ConnectionClosed as e:
                logger.info(
                    "Octo '%s' socket closed: %s",
                    self._channel_id,
                    e,
                )
            finally:
                self._ws = None
                for task in tasks:
                    task.cancel()
                await asyncio.gather(*tasks, return_exceptions=True)
        return connected

    async def _ping_loop(self, ws: Any) -> None:
        """Send PING on a cadence; drop the socket when PONGs stop.

        Args:
            ws (`Any`): The open WebSocket.
        """
        while True:
            await asyncio.sleep(_PING_INTERVAL_SECS)
            if self._pong_pending >= _PING_MAX_MISSED:
                logger.warning(
                    "Octo '%s' missed %d pongs, reconnecting",
                    self._channel_id,
                    self._pong_pending,
                )
                await ws.close()
                return
            self._pong_pending += 1
            await ws.send(encode_ping())

    async def _heartbeat_loop(self) -> None:
        """Report the bot online on the REST heartbeat cadence."""
        interval = float(self._config.heartbeat_interval_secs)
        while True:
            try:
                await self._client().heartbeat()
            except asyncio.CancelledError:
                raise
            except Exception as e:  # pylint: disable=broad-except
                logger.warning(
                    "Octo '%s' heartbeat failed: %s: %s",
                    self._channel_id,
                    type(e).__name__,
                    e,
                )
            await asyncio.sleep(interval)

    # -- Inbound (platform → gateway) --

    def _dispatch_recv(
        self, packet: RecvPacket, crypto: SessionCrypto
    ) -> None:
        """Decrypt one RECV and hand it to the gateway off the socket loop.

        Args:
            packet (`RecvPacket`): The received packet.
            crypto (`SessionCrypto`): The session cipher.
        """
        try:
            payload = json.loads(crypto.decrypt(packet.payload))
        except Exception as e:  # pylint: disable=broad-except
            logger.warning(
                "Octo '%s' could not decode message %s: %s",
                self._channel_id,
                packet.message_id,
                e,
            )
            return
        if not isinstance(payload, dict):
            return
        task = asyncio.create_task(
            self._handle_inbound(packet, payload),
            name=f"octo-inbound:{packet.message_id}",
        )
        self._inflight.add(task)
        task.add_done_callback(self._inflight.discard)

    async def _handle_inbound(self, packet: RecvPacket, payload: dict) -> None:
        """Normalise and emit one inbound message.

        Args:
            packet (`RecvPacket`): The received packet.
            payload (`dict`): Its decrypted JSON payload.
        """
        try:
            event = await self._to_event(packet, payload)
            if event is not None and self._emit is not None:
                await self._emit(event)
                logger.info(
                    "Octo '%s' inbound message %s from %s in %s -> gateway",
                    self._channel_id,
                    packet.message_id,
                    packet.from_uid,
                    event.chat_id,
                )
        except asyncio.CancelledError:
            raise
        except Exception:  # pylint: disable=broad-except
            logger.exception(
                "Octo '%s' failed to handle message %s",
                self._channel_id,
                packet.message_id,
            )

    async def _to_event(
        self,
        packet: RecvPacket,
        payload: dict,
    ) -> ChannelEvent | None:
        """Turn a platform message into a :class:`ChannelEvent`.

        Returns ``None`` for the bot's own messages, other bots (when
        configured), platform notices, and un-mentioned group chatter.

        Args:
            packet (`RecvPacket`): The received packet.
            payload (`dict`): Its decrypted JSON payload.
        """
        from_uid = packet.from_uid
        if not from_uid or from_uid == self.robot_id:
            return None
        if self._config.ignore_bots and from_uid.endswith("_bot"):
            return None
        if isinstance(payload.get("event"), dict):
            return None  # GROUP.md / THREAD.md / mention-pref notices
        is_group = bool(packet.channel_id) and packet.channel_type in (
            CT_GROUP,
            CT_THREAD,
        )
        if is_group:
            chat_id = encode_chat_id(packet.channel_id, packet.channel_type)
            chat_type = (
                "thread" if packet.channel_type == CT_THREAD else "group"
            )
        else:
            chat_id = encode_chat_id(from_uid, CT_DM)
            chat_type = "dm"
        mention = payload.get("mention")
        mention = mention if isinstance(mention, dict) else {}
        mentioned = is_group and self._is_mentioned(mention, payload)
        if is_group and self._config.only_at_reply and not mentioned:
            return None
        content = await self._content_blocks(payload, mention)
        if not content:
            return None
        group_no = packet.channel_id.split(THREAD_SEP)[0] if is_group else ""
        user_name = await self._user_name(from_uid, group_no)
        if is_group:
            chat_name = await self._group_name(group_no)
            if packet.channel_type == CT_THREAD and chat_name:
                chat_name = (
                    f"{chat_name}/{packet.channel_id.split(THREAD_SEP)[-1]}"
                )
        else:
            chat_name = user_name
        return ChannelEvent(
            channel_id=self._channel_id,
            channel_user_id=from_uid,
            channel_user_name=user_name,
            chat_id=chat_id,
            chat_name=chat_name,
            channel_message_id=str(packet.message_id),
            content=content,
            metadata={
                "chat_type": chat_type,
                "mentioned": mentioned,
                "octo_channel_id": packet.channel_id,
                "octo_channel_type": packet.channel_type,
                "message_seq": packet.message_seq,
                "timestamp": packet.timestamp,
            },
        )

    def _is_mentioned(self, mention: dict, payload: dict) -> bool:
        """Whether a group message addresses this bot.

        An explicit uid always counts. ``@所有AI`` (``ais``) counts unless
        it rides on a human broadcast (``all`` / ``humans``), which the
        platform means for people. As a fallback for senders that omit
        the mention payload, ``@<bot name>`` in the text counts.

        Args:
            mention (`dict`): ``payload.mention``.
            payload (`dict`): The whole payload (for the text fallback).
        """
        uids = {str(u) for u in mention.get("uids") or [] if u}
        for entity in mention.get("entities") or []:
            if isinstance(entity, dict) and entity.get("uid"):
                uids.add(str(entity["uid"]))
        if self.robot_id in uids:
            return True
        broadcast = _flag(mention.get("all")) or _flag(mention.get("humans"))
        if _flag(mention.get("ais")) and not broadcast:
            return True
        if not uids and self._bot_name and payload.get("type") == MT_TEXT:
            pattern = (
                r"(?<![\w一-鿿぀-ヿ가-힯])@"
                + re.escape(self._bot_name)
                + r"(?![\w一-鿿぀-ヿ가-힯.\-])"
            )
            if re.search(pattern, str(payload.get("content") or "")):
                return True
        return False

    async def _content_blocks(
        self,
        payload: dict,
        mention: dict,
    ) -> list[TextBlock | DataBlock]:
        """Render a payload into content blocks for the agent.

        Args:
            payload (`dict`): The decrypted message payload.
            mention (`dict`): ``payload.mention`` (for stripping ``@bot``).
        """
        blocks: list[TextBlock | DataBlock] = []
        quote = self._quote_prefix(payload.get("reply"))
        mtype = payload.get("type")

        def text_block(text: str) -> None:
            text = (quote + text).strip()
            if text:
                blocks.append(TextBlock(text=text))

        if mtype == MT_TEXT:
            text = _strip_mentions(
                str(payload.get("content") or ""),
                mention.get("entities"),
                self.robot_id,
            )
            if self._bot_name:
                text = re.sub(rf"@{re.escape(self._bot_name)}\s?", "", text)
            text_block(text)
        elif mtype in (MT_IMAGE, MT_GIF):
            url = str(payload.get("url") or "")
            label = "[GIF]" if mtype == MT_GIF else "[图片]"
            text_block(f"{label} {self._media_url(url)}".strip())
            block = await self._media_block(
                url,
                "image/gif" if mtype == MT_GIF else "image/jpeg",
                payload.get("name"),
            )
            if block is not None:
                blocks.append(block)
        elif mtype == MT_FILE:
            url = str(payload.get("url") or "")
            name = str(payload.get("name") or "未知文件")
            text_block(f"[文件: {name}] {self._media_url(url)}".strip())
            block = await self._media_block(
                url,
                mimetypes.guess_type(name)[0] or "application/octet-stream",
                name,
            )
            if block is not None:
                blocks.append(block)
        elif mtype == MT_VOICE:
            text_block(
                f"[语音消息] {self._media_url(str(payload.get('url') or ''))}",
            )
        elif mtype == MT_VIDEO:
            text_block(
                f"[视频] {self._media_url(str(payload.get('url') or ''))}",
            )
        elif mtype == MT_LOCATION:
            lat = payload.get("latitude", payload.get("lat"))
            lng = payload.get("longitude", payload.get("lng"))
            text_block(
                f"[位置信息: {lat},{lng}]"
                if lat is not None and lng is not None
                else "[位置信息]",
            )
        elif mtype == MT_CONTACT:
            name = payload.get("name") or "未知"
            uid = payload.get("uid") or ""
            text_block(f"[名片: {name} ({uid})]" if uid else f"[名片: {name}]")
        elif mtype == MT_RICH_TEXT:
            parts: list[str] = []
            for item in payload.get("content") or []:
                if not isinstance(item, dict):
                    continue
                if item.get("type") == "image" and item.get("url"):
                    parts.append("[图片]")
                    block = await self._media_block(
                        str(item["url"]),
                        "image/jpeg",
                        item.get("name"),
                    )
                    if block is not None:
                        blocks.append(block)
                elif item.get("text"):
                    parts.append(str(item["text"]))
            plain = str(payload.get("plain") or "").strip()
            text_block(plain or "".join(parts) or "[图文消息]")
            # Keep the text first for readability.
            blocks.sort(key=lambda b: 0 if isinstance(b, TextBlock) else 1)
        elif mtype == MT_CARD:
            text_block(str(payload.get("plain") or "").strip() or "[卡片]")
        else:
            fallback = payload.get("content") or payload.get("url") or ""
            if fallback:
                text_block(str(fallback))
        return blocks

    @staticmethod
    def _quote_prefix(reply: Any) -> str:
        """The quoted-message context a reply carries, as a prefix.

        Args:
            reply (`Any`): ``payload.reply``.
        """
        if not isinstance(reply, dict):
            return ""
        inner = reply.get("payload")
        inner = inner if isinstance(inner, dict) else {}
        content = str(inner.get("content") or "").strip()
        if not content:
            placeholders = {
                MT_IMAGE: "[图片]",
                MT_GIF: "[GIF]",
                MT_VOICE: "[语音消息]",
                MT_VIDEO: "[视频]",
                MT_FILE: f"[文件: {inner.get('name') or '未知文件'}]",
                MT_RICH_TEXT: str(inner.get("plain") or "[图文消息]"),
                MT_CARD: str(inner.get("plain") or "[卡片]"),
            }
            content = placeholders.get(inner.get("type"), "")
        if not content:
            return ""
        who = reply.get("from_name") or reply.get("from_uid") or "unknown"
        return f"[Quoted message from {who}]: {content}\n---\n"

    def _media_url(self, url: str) -> str:
        """An absolute URL for a payload's media reference.

        Args:
            url (`str`): Absolute URL or storage path.
        """
        if not url or url.startswith("http://") or url.startswith("https://"):
            return url
        path = url
        for prefix in ("file/preview/", "file/"):
            if path.startswith(prefix):
                path = path[len(prefix) :]
                break
        return f"{self._credentials.api_url.rstrip('/')}/file/{path}"

    async def _media_block(
        self,
        url: str,
        default_type: str,
        name: Any,
    ) -> DataBlock | None:
        """Download an attachment into a base64 data block (best effort).

        Args:
            url (`str`): Absolute URL or storage path.
            default_type (`str`): MIME type when the server sends none.
            name (`Any`): The file name, if the payload carries one.
        """
        if not url:
            return None
        try:
            data, content_type = await self._client().download(url)
        except Exception as e:  # pylint: disable=broad-except
            logger.warning(
                "Octo '%s' could not fetch attachment %s: %s",
                self._channel_id,
                url,
                e,
            )
            return None
        media_type = content_type or default_type
        if media_type == "application/octet-stream" and default_type:
            media_type = default_type
        return DataBlock(
            source=Base64Source(
                data=base64.b64encode(data).decode("ascii"),
                media_type=media_type,
            ),
            name=str(name) if name else None,
        )

    async def _user_name(self, uid: str, group_no: str = "") -> str:
        """A user's display name, via the group roster or the user API.

        Args:
            uid (`str`): The user.
            group_no (`str`): The group to read the roster of, if any.
        """
        now = time.monotonic()
        cached = self._user_names.get(uid)
        if cached is not None and now - cached[1] < _NAME_CACHE_TTL_SECS:
            return cached[0]
        api = self._client()
        if group_no and (
            now - self._members_refreshed.get(group_no, 0.0)
            >= _NAME_CACHE_TTL_SECS
        ):
            self._members_refreshed[group_no] = now
            try:
                for member in await api.group_members(group_no):
                    if member.get("uid"):
                        self._user_names[str(member["uid"])] = (
                            str(member.get("name") or ""),
                            now,
                        )
            except Exception as e:  # pylint: disable=broad-except
                logger.debug("Octo group members lookup failed: %s", e)
            cached = self._user_names.get(uid)
            if cached is not None and cached[0]:
                return cached[0]
        name = ""
        try:
            info = await api.user_info(uid)
            if info:
                name = str(info.get("name") or "")
        except Exception as e:  # pylint: disable=broad-except
            logger.debug("Octo user info lookup failed: %s", e)
        self._user_names[uid] = (name, now)
        return name

    async def _group_name(self, group_no: str) -> str:
        """A group's name, cached.

        Args:
            group_no (`str`): The group.
        """
        now = time.monotonic()
        cached = self._group_names.get(group_no)
        if cached is not None and now - cached[1] < _NAME_CACHE_TTL_SECS:
            return cached[0]
        name = ""
        try:
            info = await self._client().group_info(group_no)
            name = str(info.get("name") or "")
        except Exception as e:  # pylint: disable=broad-except
            logger.debug("Octo group info lookup failed: %s", e)
        self._group_names[group_no] = (name, now)
        return name

    # -- Card clicks (platform → gateway) --

    async def _events_loop(self) -> None:
        """Poll the bot event queue for approval-card clicks."""
        if not self._config.card_approval:
            return
        api = self._client()
        backoff = _EVENTS_POLL_SECS
        while True:
            delay = _EVENTS_POLL_SECS
            try:
                events = await api.fetch_events(self._events_cursor)
                for event in sorted(
                    events,
                    key=lambda e: _as_int(e.get("event_id")) or 0,
                ):
                    event_id = _as_int(event.get("event_id"))
                    if event_id is None or event_id <= self._events_cursor:
                        continue
                    recognised = await self._handle_bot_event(event)
                    self._events_cursor = event_id
                    if recognised:
                        try:
                            await api.ack_event(event_id)
                        except Exception as e:  # pylint: disable=W0718
                            logger.debug("Octo ack %s failed: %s", event_id, e)
                backoff = _EVENTS_POLL_SECS
                if events:
                    delay = 0.0
            except asyncio.CancelledError:
                raise
            except Exception as e:  # pylint: disable=broad-except
                logger.warning(
                    "Octo '%s' event poll failed at cursor=%d: %s: %s",
                    self._channel_id,
                    self._events_cursor,
                    type(e).__name__,
                    e,
                )
                delay = backoff
                backoff = min(backoff * 2, _EVENTS_MAX_BACKOFF_SECS)
            await asyncio.sleep(delay)

    async def _handle_bot_event(self, event: dict) -> bool:
        """Turn an approval-card click into a decision event.

        Args:
            event (`dict`): One entry of ``POST /v1/bot/events``.

        Returns:
            `bool`: Whether the event was one of ours (and may be acked).
        """
        if event.get("event_type") != "card_action":
            return False
        data = event.get("event_data")
        if not isinstance(data, dict):
            return False
        meta = data.get("data")
        if not isinstance(meta, dict) or meta.get("kind") != CARD_KIND:
            return False
        decision = str(meta.get("decision") or data.get("action_id") or "")
        if decision not in (ACTION_APPROVE, ACTION_REJECT):
            return False
        operator = str(data.get("operator_uid") or "")
        chat_id = str(meta.get("chat_id") or "")
        if not chat_id:
            chat_id = encode_chat_id(
                str(data.get("channel_id") or ""),
                _as_int(data.get("channel_type")) or CT_GROUP,
            )
        approved = decision == ACTION_APPROVE
        logger.info(
            "Octo '%s' card click by %s: %s tool call %s in %s",
            self._channel_id,
            operator,
            decision,
            meta.get("tool_call_id"),
            chat_id,
        )
        if self._emit is not None:
            await self._emit(
                ChannelConfirmationResultEvent(
                    channel_id=self._channel_id,
                    chat_id=chat_id,
                    channel_user_id=operator,
                    agent_id=str(meta.get("agent_id") or ""),
                    session_id=str(meta.get("session_id") or ""),
                    tool_call_id=str(meta.get("tool_call_id") or ""),
                    approved=approved,
                    actor=operator,
                ),
            )
        # Freeze the card where we posted it: the chat encoded in the
        # button's data is the send target, which is what the edit route
        # expects (the event's own channel fields are the clicker's view).
        message_id = str(data.get("message_id") or "")
        channel_id, channel_type = self._decode(chat_id)
        if not channel_id:
            channel_id = str(data.get("channel_id") or "")
            channel_type = _as_int(data.get("channel_type")) or CT_GROUP
        logger.debug(
            "Octo card_action event: message_id=%r channel=%r/%r keys=%s",
            data.get("message_id"),
            data.get("channel_id"),
            data.get("channel_type"),
            sorted(data.keys()),
        )
        if message_id and channel_id:
            try:
                await self._client().edit_message(
                    message_id,
                    channel_id,
                    channel_type,
                    build_decision_card(
                        tool_name=str(meta.get("tool_name") or ""),
                        approved=approved,
                        operator=operator,
                    ),
                )
            except Exception as e:  # pylint: disable=broad-except
                logger.warning("Octo card edit failed: %s", e)
        return True

    # -- Outbound (gateway → platform) --

    async def send_response(
        self,
        event: ChannelEvent,
        events: AsyncIterator[dict],
    ) -> None:
        """Accumulate the reply and post it as text + attachments; present
        an approval card if the run parks.

        Args:
            event (`ChannelEvent`): The send target (chat id).
            events (`AsyncIterator[dict]`): The run's session events.
        """
        channel_id, channel_type = self._decode(event.chat_id)
        api = self._client()
        if self._config.typing_indicator:
            try:
                await api.typing(channel_id, channel_type)
            except Exception as e:  # pylint: disable=broad-except
                logger.debug("Octo typing indicator failed: %s", e)
        reply: Msg | None = None
        confirm: RequireUserConfirmEvent | None = None
        async for raw in events:
            evt = _EVENT_ADAPTER.validate_python(raw)
            if isinstance(evt, RequireUserConfirmEvent):
                confirm = evt
                break
            reply_id = getattr(evt, "reply_id", None)
            if reply_id is not None:
                if reply is None:
                    reply = Msg(name="assistant", role="assistant", content=[])
                    reply.id = reply_id
                reply.append_event(evt)
            if isinstance(evt, ReplyEndEvent):
                break
        has_output = reply is not None and any(
            (isinstance(b, TextBlock) and b.text.strip())
            or isinstance(b, DataBlock)
            for b in reply.content
        )
        if confirm is not None and not has_output:
            # Parked without saying anything (tool calls only): the card
            # is the whole reply, so do not send the "no text content"
            # placeholder first.
            blocks: list[TextBlock | DataBlock] = []
        else:
            blocks = self._render(
                reply,
                show_thinking=self._config.show_thinking,
                show_tool_process=self._config.show_tool_process,
            )
        in_group = channel_type != CT_DM
        mention = (
            [event.channel_user_id]
            if in_group
            and self._config.reply_mention_sender
            and event.channel_user_id
            else None
        )
        first = True
        sent_parts = 0
        sent_files = 0
        for block in blocks:
            if isinstance(block, TextBlock):
                for part in self._split_long_message(block.text):
                    if not part.strip():
                        continue
                    await api.send_text(
                        channel_id,
                        channel_type,
                        part,
                        mention_uids=mention if first else None,
                        reply_message_id=(
                            event.channel_message_id if first else None
                        ),
                    )
                    first = False
                    sent_parts += 1
            elif isinstance(block, DataBlock) and isinstance(
                block.source,
                Base64Source,
            ):
                try:
                    await self._send_data(channel_id, channel_type, block)
                    sent_files += 1
                except Exception:  # pylint: disable=broad-except
                    logger.exception(
                        "Octo '%s' failed to send an attachment",
                        self._channel_id,
                    )
        if confirm is not None:
            await self._present_confirm(
                event, confirm, channel_id, channel_type
            )
        logger.info(
            "Octo '%s' delivered reply to %s directly over REST "
            "(%d text parts, %d attachments, %d approvals)",
            self._channel_id,
            event.chat_id,
            sent_parts,
            sent_files,
            len(confirm.tool_calls) if confirm is not None else 0,
        )

    async def _send_data(
        self,
        channel_id: str,
        channel_type: int,
        block: DataBlock,
    ) -> None:
        """Upload a base64 data block and send it as an image or file.

        Args:
            channel_id (`str`): Target chat.
            channel_type (`int`): Target channel type.
            block (`DataBlock`): The attachment.
        """
        assert isinstance(block.source, Base64Source)
        data = base64.b64decode(block.source.data)
        media_type = block.source.media_type or "application/octet-stream"
        name = block.name or (
            "image" + (mimetypes.guess_extension(media_type) or ".bin")
        )
        api = self._client()
        presign = await api.presign_upload(name, len(data), media_type)
        url = await api.upload_presigned(presign, data)
        size = _image_size(data, media_type)
        if media_type.startswith("image/") and size is not None:
            payload: dict[str, Any] = {
                "type": MT_GIF if media_type == "image/gif" else MT_IMAGE,
                "url": url,
                "width": size[0],
                "height": size[1],
            }
        else:
            payload = {
                "type": MT_FILE,
                "url": url,
                "name": name,
                "size": len(data),
            }
        await api.send_message(channel_id, channel_type, payload)

    async def _present_confirm(
        self,
        event: ChannelEvent,
        req: RequireUserConfirmEvent,
        channel_id: str,
        channel_type: int,
    ) -> None:
        """Post one approval card (or text notice) per pending tool call.

        Args:
            event (`ChannelEvent`): The send target.
            req (`RequireUserConfirmEvent`): The approval request.
            channel_id (`str`): Decoded target chat.
            channel_type (`int`): Decoded channel type.
        """
        api = self._client()
        use_card = self._config.card_approval and await self._card_support()
        for tool in req.tool_calls:
            preview = str(tool.input)[:_INPUT_PREVIEW_CHARS]
            if use_card:
                card = build_approval_card(
                    tool_call_id=tool.id,
                    chat_id=event.chat_id,
                    tool_name=tool.name,
                    tool_input=preview,
                    agent_id=event.metadata.get("agent_id", ""),
                    session_id=event.metadata.get("session_id", ""),
                )
                for action in card["card"]["actions"]:
                    action["data"]["tool_name"] = tool.name
                try:
                    await api.send_message(channel_id, channel_type, card)
                    continue
                except Exception as e:  # pylint: disable=broad-except
                    logger.warning(
                        "Octo '%s' approval card rejected, sending text: %s",
                        self._channel_id,
                        e,
                    )
            await api.send_text(
                channel_id,
                channel_type,
                "⚠️ 工具调用需要确认\n"
                f"工具：{tool.name}\n参数：{preview}\n"
                "（此机器人未启用交互卡片，请在 AgentScope 控制台中审批）",
            )

    async def _card_support(self) -> bool:
        """Whether the server lets this bot send submit cards (cached)."""
        if self._cards_supported is None:
            try:
                profile = await self._client().card_profile()
            except Exception as e:  # pylint: disable=broad-except
                logger.debug("Octo card profile probe failed: %s", e)
                return False
            self._cards_supported = cards_enabled(profile)
        return self._cards_supported

    # -- Directory & tools (REST, work unconnected) --

    async def send_message_to(self, target: str, text: str) -> bool:
        """Send Markdown text to an encoded chat (platform tools).

        Args:
            target (`str`): ``dm:<uid>`` / ``group:<no>`` / ``thread:<id>``.
            text (`str`): The message body.
        """
        channel_id, channel_type = self._decode(target)
        try:
            await self._client().send_text(channel_id, channel_type, text)
        except (OctoAPIError, httpx.HTTPError) as e:
            logger.warning("Octo send to %s failed: %s", target, e)
            return False
        return True

    async def list_bot_chats(self) -> list[dict]:
        """Groups the bot is in, as ``{"chat_id", "name"}`` entries."""
        chats: list[dict] = []
        now = time.monotonic()
        for group in await self._client().list_groups():
            group_no = str(group.get("group_no") or "")
            if not group_no:
                continue
            name = str(group.get("name") or "")
            self._group_names[group_no] = (name, now)
            chats.append(
                {"chat_id": encode_chat_id(group_no, CT_GROUP), "name": name},
            )
        return chats

    async def chat_kind(self, chat_id: str) -> ChatKind | None:
        """Whether a chat is a DM or a group/thread.

        Args:
            chat_id (`str`): The encoded chat id.
        """
        _, channel_type = self._decode(chat_id)
        return ChatKind.PRIVATE if channel_type == CT_DM else ChatKind.GROUP

    async def chat_name(self, chat_id: str) -> str | None:
        """A chat's display name (group name, or the DM peer's name).

        Args:
            chat_id (`str`): The encoded chat id.
        """
        channel_id, channel_type = self._decode(chat_id)
        if channel_type == CT_DM:
            return await self._user_name(channel_id) or None
        group_no = channel_id.split(THREAD_SEP)[0]
        name = await self._group_name(group_no)
        if channel_type == CT_THREAD and name:
            name = f"{name}/{channel_id.split(THREAD_SEP)[-1]}"
        return name or None

    async def list_tools(
        self,
        workspace: "WorkspaceBase",
        channel_user_id: str | None = None,
    ) -> list["ToolBase"]:
        """Expose Octo tools to the agent.

        Args:
            workspace (`WorkspaceBase`): The calling session's workspace.
            channel_user_id (`str | None`): The platform user the session
                acts for (passed to inherited tools).
        """
        from ._tools import ListGroups, SendMessage

        return [
            ListGroups(self),
            SendMessage(self),
        ] + await super().list_tools(
            workspace,
            channel_user_id,
        )
