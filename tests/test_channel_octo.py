# -*- coding: utf-8 -*-
"""Unit tests for the Octo channel.

Covers the WuKongIM codec against a locally simulated server side, the
inbound normalisation (mention gating, quoting, media), the outbound
rendering (text, split, attachments) and the approval-card round trip.
"""

# pylint: disable=protected-access,missing-function-docstring
import base64
import json
import struct
from typing import Any, AsyncIterator
from unittest import IsolatedAsyncioTestCase

from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric import x25519

from agentscope.app.channel import (
    ChannelConfirmationResultEvent,
    ChannelEvent,
    ChannelTypeRegistry,
    ChatKind,
)
from agentscope_channel_octo._card import CARD_KIND, cards_enabled
from agentscope_channel_octo import OctoChannel
from agentscope_channel_octo._channel import (
    _image_size,
    _strip_mentions,
    decode_chat_id,
    encode_chat_id,
)
from agentscope_channel_octo._protocol import (
    PACKET_CONNACK,
    PACKET_CONNECT,
    PACKET_DISCONNECT,
    PACKET_PONG,
    PACKET_RECV,
    PACKET_RECVACK,
    FrameBuffer,
    RecvPacket,
    SessionCrypto,
    _encode_varint,
    _Reader,
    _Writer,
    encode_connect,
    encode_ping,
    encode_recvack,
    parse_connack,
    parse_disconnect,
    parse_recv,
)
from agentscope.event import (
    DataBlockDeltaEvent,
    DataBlockEndEvent,
    DataBlockStartEvent,
    ReplyEndEvent,
    ReplyStartEvent,
    RequireUserConfirmEvent,
    TextBlockDeltaEvent,
    TextBlockEndEvent,
    TextBlockStartEvent,
    ToolCallEndEvent,
    ToolCallStartEvent,
)
from agentscope.message import DataBlock, TextBlock, ToolCallBlock

_REPLY_ID = "reply-1"
_SALT = "0123456789abcdefXYZ"
_PNG_1X1 = (
    (b"\x89PNG\r\n\x1a\n" + b"\x00\x00\x00\rIHDR" + struct.pack(">II", 3, 2))
    + b"\x08\x06\x00\x00\x00"
    + b"\x00" * 16
)


# -- simulated server side of the WuKongIM handshake --


def _server_connack(client_key: str) -> tuple[bytes, bytes, bytes]:
    server_private = x25519.X25519PrivateKey.generate()
    server_public = server_private.public_key().public_bytes(
        serialization.Encoding.Raw,
        serialization.PublicFormat.Raw,
    )
    shared = server_private.exchange(
        x25519.X25519PublicKey.from_public_bytes(base64.b64decode(client_key)),
    )
    key, iv = SessionCrypto.derive_keys(shared, _SALT)
    writer = _Writer()
    writer.byte(4)  # server version
    writer.u64(12)  # time diff
    writer.byte(1)  # reason: success
    writer.string(base64.b64encode(server_public).decode("ascii"))
    writer.string(_SALT)
    writer.u64(7)  # node id
    body = writer.getvalue()
    frame = bytes([(PACKET_CONNACK << 4) | 1]) + _encode_varint(len(body))
    return frame + body, key, iv


def _server_recv(
    key: bytes,
    iv: bytes,
    *,
    payload: dict,
    from_uid: str = "u1",
    channel_id: str = "g1",
    channel_type: int = 2,
    message_id: int = 2 << 60,
    message_seq: int = 9,
) -> bytes:
    encrypted = SessionCrypto.encrypt_with(
        key,
        iv,
        json.dumps(payload, ensure_ascii=False).encode("utf-8"),
    )
    writer = _Writer()
    writer.byte(0)  # setting
    writer.string("")  # msg key
    writer.string(from_uid)
    writer.string(channel_id)
    writer.byte(channel_type)
    writer.u32(0)  # expire (server version >= 3)
    writer.string("cmn-1")
    writer.u64(message_id)
    writer.u32(message_seq)
    writer.u32(1700000000)
    writer.raw(encrypted)
    body = writer.getvalue()
    return bytes([PACKET_RECV << 4]) + _encode_varint(len(body)) + body


class ProtocolTest(IsolatedAsyncioTestCase):
    """The codec round-trips against a simulated server."""

    def test_connect_packet_layout(self) -> None:
        packet = encode_connect(
            uid="r_bot",
            token="tok",
            client_key="ck",
            device_id="dev",
            timestamp_ms=1234,
        )
        self.assertEqual(packet[0] >> 4, PACKET_CONNECT)
        reader = _Reader(packet[1:])
        length = reader.byte()
        self.assertEqual(length, len(packet) - 2)
        self.assertEqual(reader.byte(), 4)  # protocol version
        self.assertEqual(reader.byte(), 0)  # device flag
        self.assertEqual(reader.string(), "dev")
        self.assertEqual(reader.string(), "r_bot")
        self.assertEqual(reader.string(), "tok")
        self.assertEqual(reader.u64(), 1234)
        self.assertEqual(reader.string(), "ck")
        self.assertEqual(reader.remaining(), b"")

    def test_handshake_then_encrypted_recv(self) -> None:
        crypto = SessionCrypto()
        connack, key, iv = _server_connack(crypto.client_key)
        frames = FrameBuffer()
        # Split mid-packet: nothing until the rest arrives.
        self.assertEqual(frames.feed(connack[:5]), [])
        got = frames.feed(connack[5:])
        self.assertEqual(len(got), 1)
        ack = parse_connack(got[0])
        self.assertTrue(ack.accepted)
        self.assertEqual(ack.server_version, 4)
        self.assertEqual(ack.salt, _SALT)
        self.assertEqual(ack.node_id, 7)
        crypto.complete(ack)
        self.assertTrue(crypto.ready)

        payload = {"type": 1, "content": "你好 @Bot", "mention": {"uids": []}}
        recv = _server_recv(key, iv, payload=payload)
        # Two packets glued together plus a PONG byte, delivered in two
        # chunks.
        stream = recv + recv + bytes([PACKET_PONG << 4])
        half = len(stream) // 2
        got = frames.feed(stream[:half]) + frames.feed(stream[half:])
        self.assertEqual(
            [f.packet_type for f in got],
            [PACKET_RECV, PACKET_RECV, PACKET_PONG],
        )
        packet = parse_recv(got[0], ack.server_version)
        self.assertEqual(packet.from_uid, "u1")
        self.assertEqual(packet.channel_id, "g1")
        self.assertEqual(packet.channel_type, 2)
        self.assertEqual(packet.message_id, 2 << 60)
        self.assertEqual(packet.message_seq, 9)
        self.assertEqual(packet.client_msg_no, "cmn-1")
        self.assertEqual(json.loads(crypto.decrypt(packet.payload)), payload)

        ackp = encode_recvack(packet.message_id, packet.message_seq)
        self.assertEqual(ackp[0] >> 4, PACKET_RECVACK)
        reader = _Reader(ackp[2:])
        self.assertEqual(reader.u64(), 2 << 60)
        self.assertEqual(reader.u32(), 9)
        self.assertEqual(encode_ping(), b"\x70")

    def test_disconnect_parse(self) -> None:
        writer = _Writer()
        writer.byte(3)
        writer.string("kicked")
        body = writer.getvalue()
        frame = FrameBuffer().feed(
            bytes([PACKET_DISCONNECT << 4]) + _encode_varint(len(body)) + body,
        )[0]
        info = parse_disconnect(frame)
        self.assertEqual((info.reason_code, info.reason), (3, "kicked"))

    def test_varint_multi_byte_length(self) -> None:
        body = b"x" * 300
        frame = bytes([PACKET_RECV << 4]) + _encode_varint(300) + body
        self.assertEqual(_encode_varint(300), b"\xac\x02")
        got = FrameBuffer().feed(frame)
        self.assertEqual(len(got), 1)
        self.assertEqual(len(got[0].body), 300)


class HelpersTest(IsolatedAsyncioTestCase):
    """Chat id encoding, mention stripping and image sniffing."""

    def test_chat_id_roundtrip(self) -> None:
        self.assertEqual(encode_chat_id("u1", 1), "dm:u1")
        self.assertEqual(encode_chat_id("g1", 2), "group:g1")
        self.assertEqual(encode_chat_id("g1____t9", 5), "thread:g1____t9")
        self.assertEqual(decode_chat_id("dm:u1"), ("u1", 1))
        self.assertEqual(decode_chat_id("user:u1"), ("u1", 1))
        self.assertEqual(decode_chat_id("group:g1"), ("g1", 2))
        self.assertEqual(decode_chat_id("group:g1____t9"), ("g1____t9", 5))
        self.assertEqual(decode_chat_id("thread:g1____t9"), ("g1____t9", 5))
        self.assertEqual(decode_chat_id("g1____t9"), ("g1____t9", 5))
        self.assertEqual(decode_chat_id("u1"), ("u1", 1))

    def test_strip_mentions_uses_utf16_offsets(self) -> None:
        text = "😀 @Bot hi @Bob"
        entities = [
            {"uid": "r_bot", "offset": 3, "length": 4},
            {"uid": "u2", "offset": 11, "length": 4},
        ]
        out = _strip_mentions(text, entities, "r_bot")
        self.assertEqual(out, "😀  hi @Bob")
        self.assertEqual(_strip_mentions(text, [], "r_bot"), text)
        self.assertEqual(
            _strip_mentions(text, [{"uid": "r_bot", "offset": "3"}], "r_bot"),
            text,
        )

    def test_image_size(self) -> None:
        self.assertEqual(_image_size(_PNG_1X1, "image/png"), (3, 2))
        gif = b"GIF89a" + struct.pack("<HH", 5, 4) + b"\x00" * 4
        self.assertEqual(_image_size(gif, "image/gif"), (5, 4))
        jpeg = (
            b"\xff\xd8"
            + b"\xff\xe0"
            + struct.pack(">H", 4)
            + b"\x00\x00"
            + b"\xff\xc0"
            + struct.pack(">H", 11)
            + b"\x08"
            + struct.pack(">HH", 20, 30)
            + b"\x00" * 6
        )
        self.assertEqual(_image_size(jpeg, "image/jpeg"), (30, 20))
        self.assertIsNone(_image_size(b"image bytes", "image/png"))

    def test_cards_enabled_rules(self) -> None:
        self.assertFalse(cards_enabled(None))
        self.assertFalse(cards_enabled({"enabled": True}))
        self.assertFalse(
            cards_enabled({"config": {"interaction_enabled": 1}}),
        )
        self.assertTrue(
            cards_enabled({"config": {"interaction_enabled": True}}),
        )
        self.assertFalse(
            cards_enabled(
                {
                    "config": {"interaction_enabled": True},
                    "profiles": ["octo/v1"],
                },
            ),
        )
        self.assertTrue(
            cards_enabled(
                {
                    "config": {"interaction_enabled": True},
                    "profiles": ["octo/v1", "octo/v2"],
                    "card_version": "1.5",
                },
            ),
        )


class _FakeAPI:
    """Records every REST call the channel makes."""

    def __init__(self) -> None:
        self.sent: list[dict[str, Any]] = []
        self.messages: list[dict[str, Any]] = []
        self.typing_calls: list[tuple[str, int]] = []
        self.edited: list[dict[str, Any]] = []
        self.uploaded: bytes | None = None
        self.profile: dict | None = None

    async def group_members(self, group_no: str, **_: Any) -> list[dict]:
        del group_no
        return [{"uid": "u1", "name": "Alice"}, {"uid": "u2", "name": "Bob"}]

    async def group_info(self, group_no: str, **_: Any) -> dict:
        return {"group_no": group_no, "name": "Team"}

    async def user_info(self, uid: str, **_: Any) -> dict | None:
        return {"uid": uid, "name": "Alice"} if uid == "u1" else None

    async def download(self, url: str, **_: Any) -> tuple[bytes, str]:
        del url
        return b"img", "image/png"

    async def list_groups(self, **_: Any) -> list[dict]:
        return [{"group_no": "g1", "name": "Team"}]

    async def send_text(
        self,
        channel_id: str,
        channel_type: int,
        content: str,
        *,
        mention_uids: list[str] | None = None,
        reply_message_id: str | None = None,
    ) -> dict:
        self.sent.append(
            {
                "channel_id": channel_id,
                "channel_type": channel_type,
                "content": content,
                "mention_uids": mention_uids,
                "reply_message_id": reply_message_id,
            },
        )
        return {"message_id": "m1"}

    async def send_message(
        self,
        channel_id: str,
        channel_type: int,
        payload: dict,
        **_: Any,
    ) -> dict:
        self.messages.append(
            {
                "channel_id": channel_id,
                "channel_type": channel_type,
                "payload": payload,
            },
        )
        return {"message_id": "m2"}

    async def typing(self, channel_id: str, channel_type: int) -> None:
        self.typing_calls.append((channel_id, channel_type))

    async def card_profile(self) -> dict | None:
        return self.profile

    async def edit_message(
        self,
        message_id: str,
        channel_id: str,
        channel_type: int,
        payload: dict,
    ) -> None:
        self.edited.append(
            {
                "message_id": message_id,
                "channel_id": channel_id,
                "channel_type": channel_type,
                "payload": payload,
            },
        )

    async def presign_upload(
        self,
        filename: str,
        size: int,
        content_type: str | None = None,
    ) -> dict:
        del size
        return {
            "uploadUrl": "https://up.test/x",
            "downloadUrl": f"https://cdn.test/{filename}",
            "contentType": content_type or "application/octet-stream",
        }

    async def upload_presigned(self, presign: dict, data: bytes) -> str:
        self.uploaded = data
        return presign["downloadUrl"]


def _channel(**config: Any) -> tuple[OctoChannel, _FakeAPI]:
    channel = OctoChannel(
        "octo-1",
        OctoChannel.Credentials(
            api_url="https://octo.test/api",
            bot_token="bf_test",
            robot_id="r_bot",
        ),
        OctoChannel.Config(**config),
    )
    api = _FakeAPI()
    channel._api = api  # type: ignore[assignment]
    channel._bot_name = "Bot"
    return channel, api


def _packet(
    from_uid: str,
    channel_id: str = "",
    channel_type: int = 1,
    message_id: int = 1001,
) -> RecvPacket:
    return RecvPacket(
        from_uid=from_uid,
        channel_id=channel_id,
        channel_type=channel_type,
        message_id=message_id,
        message_seq=1,
        timestamp=1700000000,
        payload=b"",
    )


class InboundTest(IsolatedAsyncioTestCase):
    """Platform messages become ChannelEvents (or are dropped)."""

    async def test_dm_text_becomes_event(self) -> None:
        channel, _ = _channel()
        event = await channel._to_event(
            _packet("u1"),
            {"type": 1, "content": "hi"},
        )
        assert event is not None
        self.assertEqual(event.chat_id, "dm:u1")
        self.assertEqual(event.channel_user_id, "u1")
        self.assertEqual(event.channel_user_name, "Alice")
        self.assertEqual(event.chat_name, "Alice")
        self.assertEqual(event.channel_message_id, "1001")
        self.assertEqual(event.message, "hi")
        self.assertEqual(event.metadata["chat_type"], "dm")
        self.assertFalse(event.metadata["mentioned"])

    async def test_group_without_mention_is_ignored(self) -> None:
        channel, _ = _channel()
        event = await channel._to_event(
            _packet("u1", "g1", 2),
            {"type": 1, "content": "just chatting"},
        )
        self.assertIsNone(event)

    async def test_group_mention_is_stripped_and_routed(self) -> None:
        channel, _ = _channel()
        event = await channel._to_event(
            _packet("u1", "g1", 2),
            {
                "type": 1,
                "content": "@Bot 帮我查一下",
                "mention": {
                    "uids": ["r_bot"],
                    "entities": [{"uid": "r_bot", "offset": 0, "length": 4}],
                },
            },
        )
        assert event is not None
        self.assertEqual(event.chat_id, "group:g1")
        self.assertEqual(event.chat_name, "Team")
        self.assertEqual(event.message, "帮我查一下")
        self.assertTrue(event.metadata["mentioned"])
        self.assertEqual(event.metadata["chat_type"], "group")
        self.assertEqual(event.channel_user_name, "Alice")

    async def test_thread_chat_name(self) -> None:
        channel, _ = _channel()
        event = await channel._to_event(
            _packet("u2", "g1____t9", 5),
            {"type": 1, "content": "x", "mention": {"uids": ["r_bot"]}},
        )
        assert event is not None
        self.assertEqual(event.chat_id, "thread:g1____t9")
        self.assertEqual(event.chat_name, "Team/t9")
        self.assertEqual(event.metadata["chat_type"], "thread")
        self.assertEqual(event.channel_user_name, "Bob")

    async def test_ai_broadcast_counts_unless_human_broadcast(self) -> None:
        channel, _ = _channel()
        ais = await channel._to_event(
            _packet("u1", "g1", 2),
            {"type": 1, "content": "all AIs", "mention": {"ais": 1}},
        )
        self.assertIsNotNone(ais)
        everyone = await channel._to_event(
            _packet("u1", "g1", 2),
            {"type": 1, "content": "all", "mention": {"ais": 1, "all": 1}},
        )
        self.assertIsNone(everyone)

    async def test_text_fallback_mention(self) -> None:
        channel, _ = _channel()
        event = await channel._to_event(
            _packet("u1", "g1", 2),
            {"type": 1, "content": "hey @Bot what's up"},
        )
        assert event is not None
        self.assertEqual(event.message, "hey what's up")

    async def test_only_at_reply_off_takes_every_message(self) -> None:
        channel, _ = _channel(only_at_reply=False)
        event = await channel._to_event(
            _packet("u1", "g1", 2),
            {"type": 1, "content": "chatter"},
        )
        self.assertIsNotNone(event)

    async def test_own_and_bot_messages_are_dropped(self) -> None:
        channel, _ = _channel()
        self.assertIsNone(
            await channel._to_event(
                _packet("r_bot"), {"type": 1, "content": "x"}
            ),
        )
        self.assertIsNone(
            await channel._to_event(
                _packet("other_bot"),
                {"type": 1, "content": "x"},
            ),
        )
        relaxed, _ = _channel(ignore_bots=False)
        relaxed._bot_name = ""
        self.assertIsNotNone(
            await relaxed._to_event(
                _packet("other_bot"),
                {"type": 1, "content": "x"},
            ),
        )

    async def test_platform_notice_is_dropped(self) -> None:
        channel, _ = _channel()
        self.assertIsNone(
            await channel._to_event(
                _packet("u1", "g1", 2),
                {"type": 1, "event": {"type": "group_md_updated"}},
            ),
        )

    async def test_quoted_reply_is_prefixed(self) -> None:
        channel, _ = _channel()
        event = await channel._to_event(
            _packet("u1"),
            {
                "type": 1,
                "content": "what does it mean?",
                "reply": {
                    "payload": {"type": 1, "content": "orig"},
                    "from_name": "Bob",
                },
            },
        )
        assert event is not None
        self.assertEqual(
            event.message,
            "[Quoted message from Bob]: orig\n---\nwhat does it mean?",
        )

    async def test_image_message_carries_data_block(self) -> None:
        channel, _ = _channel()
        event = await channel._to_event(
            _packet("u1"),
            {"type": 2, "url": "file/preview/pic.png", "name": "pic.png"},
        )
        assert event is not None
        self.assertEqual(len(event.content), 2)
        text, data = event.content
        assert isinstance(text, TextBlock) and isinstance(data, DataBlock)
        self.assertEqual(text.text, "[图片] https://octo.test/api/file/pic.png")
        self.assertEqual(data.source.media_type, "image/png")
        source = data.source
        assert hasattr(source, "data")
        self.assertEqual(base64.b64decode(source.data), b"img")

    async def test_rich_text_and_card_use_plain(self) -> None:
        channel, _ = _channel()
        rich = await channel._to_event(
            _packet("u1"),
            {
                "type": 14,
                "content": [
                    {"type": "text", "text": "see "},
                    {"type": "image", "url": "https://cdn/a.png"},
                ],
            },
        )
        assert rich is not None
        self.assertIsInstance(rich.content[0], TextBlock)
        self.assertEqual(rich.message, "see [图片]")
        self.assertEqual(len(rich.content), 2)
        card = await channel._to_event(
            _packet("u1"),
            {"type": 17, "card": {}, "plain": "Build passed"},
        )
        assert card is not None
        self.assertEqual(card.message, "Build passed")


def _inbound(chat_id: str, **metadata: Any) -> ChannelEvent:
    return ChannelEvent(
        channel_id="octo-1",
        channel_user_id="u1",
        chat_id=chat_id,
        channel_message_id="1001",
        metadata=metadata,
    )


async def _text_stream(text: str = "hello from agent") -> AsyncIterator[dict]:
    events = [
        ReplyStartEvent(
            session_id="session-1",
            reply_id=_REPLY_ID,
            name="assistant",
        ),
        TextBlockStartEvent(reply_id=_REPLY_ID, block_id="text-1"),
        TextBlockDeltaEvent(reply_id=_REPLY_ID, block_id="text-1", delta=text),
        TextBlockEndEvent(reply_id=_REPLY_ID, block_id="text-1"),
        ReplyEndEvent(session_id="session-1", reply_id=_REPLY_ID),
    ]
    for event in events:
        yield event.model_dump(mode="json")


async def _data_stream(data: bytes, media_type: str) -> AsyncIterator[dict]:
    events = [
        ReplyStartEvent(
            session_id="session-1",
            reply_id=_REPLY_ID,
            name="assistant",
        ),
        DataBlockStartEvent(
            reply_id=_REPLY_ID,
            block_id="data-1",
            media_type=media_type,
        ),
        DataBlockDeltaEvent(
            reply_id=_REPLY_ID,
            block_id="data-1",
            data=base64.b64encode(data).decode("ascii"),
            media_type=media_type,
        ),
        DataBlockEndEvent(reply_id=_REPLY_ID, block_id="data-1"),
        ReplyEndEvent(session_id="session-1", reply_id=_REPLY_ID),
    ]
    for event in events:
        yield event.model_dump(mode="json")


async def _confirm_stream() -> AsyncIterator[dict]:
    events = [
        ReplyStartEvent(
            session_id="session-1",
            reply_id=_REPLY_ID,
            name="assistant",
        ),
        RequireUserConfirmEvent(
            reply_id=_REPLY_ID,
            tool_calls=[
                ToolCallBlock(
                    id="tool-1",
                    name="SendOctoMessage",
                    input='{"target":"dm:u2","text":"hello"}',
                ),
            ],
        ),
    ]
    for event in events:
        yield event.model_dump(mode="json")


class OutboundTest(IsolatedAsyncioTestCase):
    """Replies, attachments and approvals go out over REST."""

    async def test_dm_reply_quotes_the_message(self) -> None:
        channel, api = _channel()
        await channel.send_response(_inbound("dm:u1"), _text_stream("hi!"))
        self.assertEqual(api.typing_calls, [("u1", 1)])
        self.assertEqual(len(api.sent), 1)
        self.assertEqual(api.sent[0]["channel_id"], "u1")
        self.assertEqual(api.sent[0]["channel_type"], 1)
        self.assertEqual(api.sent[0]["content"], "hi!")
        self.assertIsNone(api.sent[0]["mention_uids"])
        self.assertEqual(api.sent[0]["reply_message_id"], "1001")

    async def test_group_reply_mentions_sender(self) -> None:
        channel, api = _channel()
        await channel.send_response(_inbound("group:g1"), _text_stream())
        self.assertEqual(api.sent[0]["channel_type"], 2)
        self.assertEqual(api.sent[0]["mention_uids"], ["u1"])
        quiet, api2 = _channel(
            reply_mention_sender=False, typing_indicator=False
        )
        await quiet.send_response(_inbound("thread:g1____t9"), _text_stream())
        self.assertEqual(api2.typing_calls, [])
        self.assertEqual(api2.sent[0]["channel_id"], "g1____t9")
        self.assertEqual(api2.sent[0]["channel_type"], 5)
        self.assertIsNone(api2.sent[0]["mention_uids"])

    async def test_long_reply_is_split(self) -> None:
        channel, api = _channel()
        await channel.send_response(
            _inbound("dm:u1"), _text_stream("x" * 4500)
        )
        self.assertEqual([len(s["content"]) for s in api.sent], [4000, 500])
        self.assertEqual(api.sent[0]["reply_message_id"], "1001")
        self.assertIsNone(api.sent[1]["reply_message_id"])

    async def test_image_reply_is_uploaded(self) -> None:
        channel, api = _channel()
        await channel.send_response(
            _inbound("dm:u1"),
            _data_stream(_PNG_1X1, "image/png"),
        )
        self.assertEqual(api.uploaded, _PNG_1X1)
        payload = api.messages[0]["payload"]
        self.assertEqual(payload["type"], 2)
        self.assertEqual((payload["width"], payload["height"]), (3, 2))
        self.assertTrue(payload["url"].startswith("https://cdn.test/"))

    async def test_unknown_image_falls_back_to_file(self) -> None:
        channel, api = _channel()
        await channel.send_response(
            _inbound("dm:u1"),
            _data_stream(b"not really png", "image/png"),
        )
        payload = api.messages[0]["payload"]
        self.assertEqual(payload["type"], 8)
        self.assertEqual(payload["size"], len(b"not really png"))

    async def test_approval_card_when_supported(self) -> None:
        channel, api = _channel()
        api.profile = {
            "config": {"interaction_enabled": True},
            "profiles": ["octo/v1", "octo/v2"],
            "card_version": "1.5",
        }
        await channel.send_response(
            _inbound("group:g1", agent_id="a1", session_id="s1"),
            _confirm_stream(),
        )
        self.assertEqual(api.sent, [])
        payload = api.messages[-1]["payload"]
        self.assertEqual(payload["type"], 17)
        self.assertEqual(payload["profile"], "octo/v2")
        actions = payload["card"]["actions"]
        self.assertEqual([a["id"] for a in actions], ["approve", "reject"])
        data = actions[0]["data"]
        self.assertEqual(data["kind"], CARD_KIND)
        self.assertEqual(data["tool_call_id"], "tool-1")
        self.assertEqual(data["chat_id"], "group:g1")
        self.assertEqual(data["agent_id"], "a1")
        self.assertEqual(data["session_id"], "s1")
        self.assertEqual(data["tool_name"], "SendOctoMessage")

    async def test_approval_after_tool_calls_sends_no_placeholder(
        self,
    ) -> None:
        channel, api = _channel()
        api.profile = {"config": {"interaction_enabled": True}}

        async def stream() -> AsyncIterator[dict]:
            events = [
                ReplyStartEvent(
                    session_id="session-1",
                    reply_id=_REPLY_ID,
                    name="assistant",
                ),
                ToolCallStartEvent(
                    reply_id=_REPLY_ID,
                    tool_call_id="tool-0",
                    tool_call_name="Bash",
                ),
                ToolCallEndEvent(reply_id=_REPLY_ID, tool_call_id="tool-0"),
                RequireUserConfirmEvent(
                    reply_id=_REPLY_ID,
                    tool_calls=[
                        ToolCallBlock(id="tool-1", name="Bash", input="{}"),
                    ],
                ),
            ]
            for event in events:
                yield event.model_dump(mode="json")

        await channel.send_response(_inbound("dm:u1"), stream())
        self.assertEqual(api.sent, [])
        self.assertEqual(api.messages[-1]["payload"]["type"], 17)

    async def test_approval_falls_back_to_text(self) -> None:
        channel, api = _channel()
        api.profile = None
        await channel.send_response(_inbound("dm:u1"), _confirm_stream())
        self.assertEqual(api.messages, [])
        self.assertIn("工具调用需要确认", api.sent[-1]["content"])
        self.assertIn("SendOctoMessage", api.sent[-1]["content"])


class CardClickTest(IsolatedAsyncioTestCase):
    """A card click comes back as a decision for the exact run."""

    async def test_click_emits_decision_and_freezes_card(self) -> None:
        channel, api = _channel()
        emitted: list[Any] = []

        async def emit(event: Any) -> None:
            emitted.append(event)

        channel._emit = emit
        recognised = await channel._handle_bot_event(
            {
                "event_id": 5,
                "event_type": "card_action",
                "event_data": {
                    "message_id": "m2",
                    "channel_id": "g1",
                    "channel_type": 2,
                    "action_id": "reject",
                    "operator_uid": "u1",
                    "data": {
                        "kind": CARD_KIND,
                        "decision": "reject",
                        "tool_call_id": "tool-1",
                        "chat_id": "group:g1",
                        "agent_id": "a1",
                        "session_id": "s1",
                        "tool_name": "SendOctoMessage",
                    },
                },
            },
        )
        self.assertTrue(recognised)
        self.assertEqual(len(emitted), 1)
        decision = emitted[0]
        self.assertIsInstance(decision, ChannelConfirmationResultEvent)
        self.assertFalse(decision.approved)
        self.assertEqual(decision.tool_call_id, "tool-1")
        self.assertEqual(decision.session_id, "s1")
        self.assertEqual(decision.agent_id, "a1")
        self.assertEqual(decision.chat_id, "group:g1")
        self.assertEqual(decision.channel_user_id, "u1")
        self.assertEqual(api.edited[0]["message_id"], "m2")
        self.assertEqual(api.edited[0]["payload"]["profile"], "octo/v1")
        self.assertIn("已拒绝", api.edited[0]["payload"]["plain"])

    async def test_foreign_events_are_not_ours(self) -> None:
        channel, _ = _channel()
        emitted: list[Any] = []

        async def emit(event: Any) -> None:
            emitted.append(event)

        channel._emit = emit
        self.assertFalse(
            await channel._handle_bot_event(
                {"event_id": 1, "event_type": "doc_comment_mention"},
            ),
        )
        self.assertFalse(
            await channel._handle_bot_event(
                {
                    "event_id": 2,
                    "event_type": "card_action",
                    "event_data": {"action_id": "submit", "data": {}},
                },
            ),
        )
        self.assertEqual(emitted, [])


class DirectoryTest(IsolatedAsyncioTestCase):
    """Chat listing, kinds and names work without a connection."""

    async def test_registry_and_bot_id(self) -> None:
        registry = ChannelTypeRegistry([OctoChannel])
        channel = registry.create_channel(
            channel_type="octo",
            channel_id="c1",
            credentials={
                "api_url": "https://octo.test/api",
                "bot_token": "bf_x",
                "robot_id": "r_bot",
            },
            config={},
        )
        self.assertIsInstance(channel, OctoChannel)
        self.assertEqual(
            registry.extract_platform_bot_id("octo", {"robot_id": "r_bot"}),
            "r_bot",
        )
        schema = registry.list_types()[0]
        self.assertEqual(schema.platform_bot_id_field, "robot_id")
        self.assertEqual(
            schema.credentials_schema["properties"]["bot_token"]["format"],
            "password",
        )

    async def test_chats_kinds_and_names(self) -> None:
        channel, _ = _channel()
        self.assertEqual(
            await channel.list_bot_chats(),
            [{"chat_id": "group:g1", "name": "Team"}],
        )
        self.assertEqual(await channel.chat_kind("g1"), ChatKind.GROUP)
        self.assertEqual(await channel.chat_kind("dm:u1"), ChatKind.PRIVATE)
        self.assertEqual(await channel.chat_name("group:g1"), "Team")
        self.assertEqual(await channel.chat_name("thread:g1____t9"), "Team/t9")
        self.assertEqual(await channel.chat_name("dm:u1"), "Alice")
        self.assertTrue(await channel.send_message_to("group:g1", "hi"))
