# -*- coding: utf-8 -*-
"""WuKongIM wire protocol (v4), the long connection an Octo bot listens on.

Octo runs on WuKongIM, and a bot receives its messages the way any
WuKongIM client does: one WebSocket carrying binary packets. This module
is the subset a bot needs — the CONNECT/CONNACK handshake with its
X25519 key agreement, RECV (an AES-CBC encrypted inbound message) and
the RECVACK it must be answered with, PING/PONG, and DISCONNECT. It
mirrors ``src/socket.ts`` of openclaw-channel-octo byte for byte.

Everything here is pure: no I/O, no asyncio, so it is testable against
a locally simulated server side.
"""
from __future__ import annotations

import base64
import hashlib
import time
import uuid
from dataclasses import dataclass

PROTO_VERSION = 4
"""Protocol version announced in CONNECT."""

DEVICE_FLAG_APP = 0
"""Device flag a bot connects with (the SDK default for app/bot)."""

PACKET_CONNECT = 1
PACKET_CONNACK = 2
PACKET_SEND = 3
PACKET_SENDACK = 4
PACKET_RECV = 5
PACKET_RECVACK = 6
PACKET_PING = 7
PACKET_PONG = 8
PACKET_DISCONNECT = 9

REASON_SUCCESS = 1
"""CONNACK reason code of an accepted connection."""


class ProtocolError(ValueError):
    """A frame that cannot be decoded."""


def new_device_id() -> str:
    """A fresh device id for one connection, suffixed like the SDK's."""
    return uuid.uuid4().hex + "W"


def _encode_varint(length: int) -> bytes:
    """Encode a remaining-length prefix (little-endian 7-bit groups).

    Args:
        length (`int`): The body length to encode.
    """
    out = bytearray()
    while True:
        digit = length % 0x80
        length //= 0x80
        if length > 0:
            digit |= 0x80
        out.append(digit)
        if length == 0:
            return bytes(out)


class _Writer:
    """Big-endian packet body builder."""

    def __init__(self) -> None:
        self._buf = bytearray()

    def byte(self, value: int) -> None:
        """Append one byte."""
        self._buf.append(value & 0xFF)

    def u16(self, value: int) -> None:
        """Append a 16-bit big-endian integer."""
        self._buf += (value & 0xFFFF).to_bytes(2, "big")

    def u32(self, value: int) -> None:
        """Append a 32-bit big-endian integer."""
        self._buf += (value & 0xFFFFFFFF).to_bytes(4, "big")

    def u64(self, value: int) -> None:
        """Append a 64-bit big-endian integer."""
        self._buf += (value & 0xFFFFFFFFFFFFFFFF).to_bytes(8, "big")

    def string(self, value: str) -> None:
        """Append a length-prefixed (u16) UTF-8 string."""
        data = value.encode("utf-8")
        self.u16(len(data))
        self._buf += data

    def raw(self, data: bytes) -> None:
        """Append raw bytes."""
        self._buf += data

    def getvalue(self) -> bytes:
        """The bytes written so far."""
        return bytes(self._buf)


class _Reader:
    """Big-endian packet body parser."""

    def __init__(self, data: bytes) -> None:
        self._data = data
        self._pos = 0

    def _take(self, size: int) -> bytes:
        if self._pos + size > len(self._data):
            raise ProtocolError(
                f"packet truncated: need {size} bytes at offset "
                f"{self._pos}, have {len(self._data) - self._pos}",
            )
        chunk = self._data[self._pos : self._pos + size]
        self._pos += size
        return chunk

    def byte(self) -> int:
        """Read one byte."""
        return self._take(1)[0]

    def u16(self) -> int:
        """Read a 16-bit big-endian integer."""
        return int.from_bytes(self._take(2), "big")

    def u32(self) -> int:
        """Read a 32-bit big-endian integer."""
        return int.from_bytes(self._take(4), "big")

    def u64(self) -> int:
        """Read a 64-bit big-endian integer."""
        return int.from_bytes(self._take(8), "big")

    def string(self) -> str:
        """Read a length-prefixed (u16) UTF-8 string."""
        size = self.u16()
        if size == 0:
            return ""
        return self._take(size).decode("utf-8", errors="replace")

    def remaining(self) -> bytes:
        """Read everything left."""
        chunk = self._data[self._pos :]
        self._pos = len(self._data)
        return chunk


def _frame(packet_type: int, flags: int, body: bytes) -> bytes:
    """Wrap a body in the fixed header and remaining-length prefix.

    Args:
        packet_type (`int`): One of the ``PACKET_*`` constants.
        flags (`int`): The low four header bits.
        body (`bytes`): The packet body.
    """
    header = ((packet_type << 4) | (flags & 0x0F)) & 0xFF
    return bytes([header]) + _encode_varint(len(body)) + body


def encode_connect(
    *,
    uid: str,
    token: str,
    client_key: str,
    device_id: str | None = None,
    timestamp_ms: int | None = None,
) -> bytes:
    """Build the CONNECT packet that opens a bot session.

    Args:
        uid (`str`): The bot's uid (``robot_id``).
        token (`str`): The IM token from ``POST /v1/bot/register``.
        client_key (`str`): Base64 X25519 public key for the handshake.
        device_id (`str | None`): Device id; a fresh one when omitted.
        timestamp_ms (`int | None`): Client clock in ms; now when omitted.
    """
    writer = _Writer()
    writer.byte(PROTO_VERSION)
    writer.byte(DEVICE_FLAG_APP)
    writer.string(device_id or new_device_id())
    writer.string(uid)
    writer.string(token)
    writer.u64(
        timestamp_ms if timestamp_ms is not None else int(time.time() * 1000),
    )
    writer.string(client_key)
    return _frame(PACKET_CONNECT, 0, writer.getvalue())


def encode_ping() -> bytes:
    """The one-byte PING packet."""
    return bytes([PACKET_PING << 4])


def encode_recvack(message_id: int, message_seq: int) -> bytes:
    """Acknowledge one received message.

    Args:
        message_id (`int`): The server message id (64-bit).
        message_seq (`int`): The message sequence in its channel.
    """
    writer = _Writer()
    writer.u64(message_id)
    writer.u32(message_seq)
    return _frame(PACKET_RECVACK, 0, writer.getvalue())


@dataclass
class Frame:
    """One packet split off the byte stream, header decoded."""

    packet_type: int
    flags: int
    body: bytes


class FrameBuffer:
    """Reassembles packets from WebSocket messages.

    The server may pack several packets into one message or split one
    across two (sticky packets), so bytes are buffered and only complete
    packets are returned.
    """

    def __init__(self) -> None:
        self._buf = bytearray()

    def feed(self, data: bytes) -> list[Frame]:
        """Append ``data`` and return every packet now complete.

        Args:
            data (`bytes`): The next WebSocket message.
        """
        self._buf += data
        frames: list[Frame] = []
        while self._buf:
            header = self._buf[0]
            packet_type = header >> 4
            if packet_type in (PACKET_PING, PACKET_PONG):
                frames.append(Frame(packet_type, header & 0x0F, b""))
                del self._buf[:1]
                continue
            pos = 1
            length = 0
            multiplier = 1
            complete = False
            while pos < len(self._buf):
                digit = self._buf[pos]
                pos += 1
                length += (digit & 0x7F) * multiplier
                multiplier *= 0x80
                if not digit & 0x80:
                    complete = True
                    break
                if multiplier > 0x80**4:
                    raise ProtocolError("remaining length prefix too long")
            if not complete:
                break
            total = pos + length
            if total > len(self._buf):
                break
            frames.append(
                Frame(packet_type, header & 0x0F, bytes(self._buf[pos:total])),
            )
            del self._buf[:total]
        return frames


@dataclass
class ConnAck:
    """The server's answer to CONNECT."""

    reason_code: int
    server_key: str
    salt: str
    server_version: int = 0
    time_diff: int = 0
    node_id: int = 0

    @property
    def accepted(self) -> bool:
        """Whether the connection was accepted."""
        return self.reason_code == REASON_SUCCESS


def parse_connack(frame: Frame) -> ConnAck:
    """Decode a CONNACK body.

    Args:
        frame (`Frame`): The CONNACK frame.
    """
    reader = _Reader(frame.body)
    server_version = reader.byte() if frame.flags & 0x01 else 0
    time_diff = reader.u64()
    reason_code = reader.byte()
    server_key = reader.string()
    salt = reader.string()
    node_id = reader.u64() if server_version >= 4 else 0
    return ConnAck(
        reason_code=reason_code,
        server_key=server_key,
        salt=salt,
        server_version=server_version,
        time_diff=time_diff,
        node_id=node_id,
    )


@dataclass
class RecvPacket:
    """One inbound message, payload still encrypted."""

    from_uid: str
    channel_id: str
    channel_type: int
    message_id: int
    message_seq: int
    timestamp: int
    payload: bytes
    client_msg_no: str = ""
    msg_key: str = ""
    topic: str = ""
    setting: int = 0


def parse_recv(frame: Frame, server_version: int) -> RecvPacket:
    """Decode a RECV body.

    Args:
        frame (`Frame`): The RECV frame.
        server_version (`int`): The version CONNACK reported; it decides
            whether the ``expire`` field is present.
    """
    reader = _Reader(frame.body)
    setting = reader.byte()
    msg_key = reader.string()
    from_uid = reader.string()
    channel_id = reader.string()
    channel_type = reader.byte()
    if server_version >= 3:
        reader.u32()  # expire
    client_msg_no = reader.string()
    message_id = reader.u64()
    message_seq = reader.u32()
    timestamp = reader.u32()
    topic = reader.string() if (setting >> 3) & 0x01 else ""
    return RecvPacket(
        from_uid=from_uid,
        channel_id=channel_id,
        channel_type=channel_type,
        message_id=message_id,
        message_seq=message_seq,
        timestamp=timestamp,
        payload=reader.remaining(),
        client_msg_no=client_msg_no,
        msg_key=msg_key,
        topic=topic,
        setting=setting,
    )


@dataclass
class Disconnect:
    """The server closing the session (kicked, token revoked, ...)."""

    reason_code: int
    reason: str


def parse_disconnect(frame: Frame) -> Disconnect:
    """Decode a DISCONNECT body.

    Args:
        frame (`Frame`): The DISCONNECT frame.
    """
    reader = _Reader(frame.body)
    reason_code = reader.byte()
    try:
        reason = reader.string()
    except ProtocolError:
        reason = ""
    return Disconnect(reason_code=reason_code, reason=reason)


class SessionCrypto:
    """The per-connection key agreement and payload cipher.

    CONNECT carries a fresh X25519 public key; CONNACK answers with the
    server's and a salt. Both sides derive AES-128-CBC material as
    ``md5(base64(shared_secret))[:16]`` for the key and ``salt[:16]`` for
    the IV, and every RECV payload is the base64 text of the PKCS7-padded
    ciphertext.
    """

    def __init__(self) -> None:
        from cryptography.hazmat.primitives import serialization
        from cryptography.hazmat.primitives.asymmetric import x25519

        self._private = x25519.X25519PrivateKey.generate()
        public = self._private.public_key().public_bytes(
            serialization.Encoding.Raw,
            serialization.PublicFormat.Raw,
        )
        self.client_key = base64.b64encode(public).decode("ascii")
        self._key: bytes | None = None
        self._iv: bytes | None = None

    @staticmethod
    def derive_keys(shared_secret: bytes, salt: str) -> tuple[bytes, bytes]:
        """Turn the X25519 shared secret and salt into AES key and IV.

        Args:
            shared_secret (`bytes`): The 32-byte X25519 result.
            salt (`str`): The salt from CONNACK.
        """
        secret_b64 = base64.b64encode(shared_secret).decode("ascii")
        digest = hashlib.md5(
            secret_b64.encode("ascii"),
            usedforsecurity=False,
        ).hexdigest()
        key = digest[:16].encode("ascii")
        iv = (salt[:16] if len(salt) > 16 else salt).encode("utf-8")
        if len(iv) != 16:
            raise ProtocolError("CONNACK salt does not yield a 16-byte IV")
        return key, iv

    def complete(self, ack: ConnAck) -> None:
        """Finish the handshake with the server's key and salt.

        Args:
            ack (`ConnAck`): The accepted CONNACK.
        """
        from cryptography.hazmat.primitives.asymmetric import x25519

        server_public = x25519.X25519PublicKey.from_public_bytes(
            base64.b64decode(ack.server_key),
        )
        self._key, self._iv = self.derive_keys(
            self._private.exchange(server_public),
            ack.salt,
        )

    @property
    def ready(self) -> bool:
        """Whether :meth:`complete` has run."""
        return self._key is not None

    def decrypt(self, payload: bytes) -> bytes:
        """Decrypt one RECV payload to its JSON bytes.

        Args:
            payload (`bytes`): The base64 text carried in the packet.
        """
        from cryptography.hazmat.primitives import padding
        from cryptography.hazmat.primitives.ciphers import (
            Cipher,
            algorithms,
            modes,
        )

        if self._key is None or self._iv is None:
            raise ProtocolError("session keys not derived yet")
        raw = base64.b64decode(payload)
        decryptor = Cipher(
            algorithms.AES(self._key),
            modes.CBC(self._iv),
        ).decryptor()
        padded = decryptor.update(raw) + decryptor.finalize()
        unpadder = padding.PKCS7(128).unpadder()
        return unpadder.update(padded) + unpadder.finalize()

    @staticmethod
    def encrypt_with(key: bytes, iv: bytes, data: bytes) -> bytes:
        """Encrypt ``data`` the way the server does (for tests).

        Args:
            key (`bytes`): The 16-byte AES key.
            iv (`bytes`): The 16-byte IV.
            data (`bytes`): The plaintext JSON.
        """
        from cryptography.hazmat.primitives import padding
        from cryptography.hazmat.primitives.ciphers import (
            Cipher,
            algorithms,
            modes,
        )

        padder = padding.PKCS7(128).padder()
        padded = padder.update(data) + padder.finalize()
        encryptor = Cipher(algorithms.AES(key), modes.CBC(iv)).encryptor()
        return base64.b64encode(
            encryptor.update(padded) + encryptor.finalize()
        )
