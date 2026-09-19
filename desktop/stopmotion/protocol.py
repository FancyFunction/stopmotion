"""Framing codec for the desktop <-> phone link.

Pure stdlib on purpose: no Qt import, so this module is usable headless, from
unit tests, and from ``tools/fake_phone.py``.

Wire format (Plan.md section 4)::

    [4B BE total_len][1B type][payload]

``total_len`` covers the type byte plus the payload. The payload always starts
with a JSON header::

    [2B BE header_len][utf-8 JSON header][raw bytes...]

Messages that carry no binary blob simply have no trailing bytes. Messages that
carry no header at all (PING/PONG) are encoded with ``header_len == 0`` and
decode back to an empty dict.
"""

from __future__ import annotations

import json
import struct
from enum import IntEnum

__all__ = [
    "MsgType",
    "ProtocolError",
    "MAX_MESSAGE_SIZE",
    "MAX_HEADER_SIZE",
    "FRAME_PREFIX_SIZE",
    "encode",
    "decode",
    "MessageReader",
]

#: Hard cap on a single framed message (type byte + payload), per interfaces.md.
MAX_MESSAGE_SIZE = 64 * 1024 * 1024

#: The JSON header length is a 16-bit field.
MAX_HEADER_SIZE = 0xFFFF

#: Bytes consumed by the length prefix.
FRAME_PREFIX_SIZE = 4

_LEN = struct.Struct(">I")
_HDRLEN = struct.Struct(">H")
_FRAME_HEAD = struct.Struct(">IBH")


class MsgType(IntEnum):
    """Message type byte."""

    HELLO = 1
    SET_CONFIG = 2
    CONFIG_ACK = 3
    PREVIEW_CTL = 4
    PREVIEW_FRAME = 5
    CAPTURE = 6
    CAPTURE_RESULT = 7
    ERROR = 8
    PING = 9
    PONG = 10


class ProtocolError(Exception):
    """Raised on a malformed, oversized or otherwise unusable frame."""


def encode(msg: MsgType, header: dict | None = None, blob: bytes | None = None) -> bytes:
    """Encode one message into bytes ready for the socket.

    ``header`` of ``None`` encodes a zero-length header (used for PING/PONG).
    ``blob`` of ``None`` or ``b""`` encodes a pure-JSON message.
    """
    if header is None:
        raw_header = b""
    else:
        try:
            raw_header = json.dumps(header, separators=(",", ":")).encode("utf-8")
        except (TypeError, ValueError) as exc:  # pragma: no cover - programmer error
            raise ProtocolError(f"header is not JSON-serialisable: {exc}") from exc
        if len(raw_header) > MAX_HEADER_SIZE:
            raise ProtocolError(
                f"JSON header of {len(raw_header)} bytes exceeds the {MAX_HEADER_SIZE} byte field"
            )

    body = blob or b""
    total_len = 1 + _HDRLEN.size + len(raw_header) + len(body)
    if total_len > MAX_MESSAGE_SIZE:
        raise ProtocolError(
            f"message of {total_len} bytes exceeds the {MAX_MESSAGE_SIZE} byte cap"
        )

    return b"".join(
        (_FRAME_HEAD.pack(total_len, int(msg), len(raw_header)), raw_header, bytes(body))
    )


def decode(msg: MsgType, payload: bytes) -> tuple[dict, bytes | None]:
    """Decode one payload (everything after the type byte).

    Returns ``(header, blob)``; ``blob`` is ``None`` when the message carries no
    trailing binary data.
    """
    if len(payload) < _HDRLEN.size:
        name = msg.name if isinstance(msg, MsgType) else str(msg)
        raise ProtocolError(
            f"{name}: payload of {len(payload)} bytes is too short for a header length"
        )
    (header_len,) = _HDRLEN.unpack_from(payload, 0)
    end = _HDRLEN.size + header_len
    if len(payload) < end:
        raise ProtocolError(
            f"declared header length {header_len} exceeds the {len(payload) - _HDRLEN.size} "
            "bytes of payload available"
        )

    if header_len == 0:
        header: dict = {}
    else:
        raw = payload[_HDRLEN.size:end]
        try:
            header = json.loads(raw.decode("utf-8"))
        except UnicodeDecodeError as exc:
            raise ProtocolError(f"JSON header is not valid utf-8: {exc}") from exc
        except json.JSONDecodeError as exc:
            raise ProtocolError(f"malformed JSON header: {exc}") from exc
        if header is None:
            header = {}
        elif not isinstance(header, dict):
            raise ProtocolError(
                f"JSON header must be an object, got {type(header).__name__}"
            )

    blob = payload[end:]
    return header, (bytes(blob) if blob else None)


class MessageReader:
    """Incremental parser for the framed stream.

    ``feed(data)`` accepts whatever came out of ``recv()`` -- a partial frame,
    exactly one frame, or several frames plus a fragment -- and returns the
    messages that are now complete, as ``(MsgType, header, blob)`` tuples.

    Unknown type bytes are skipped rather than fatal: the frame length is known,
    so a newer phone build adding a message type cannot desynchronise us. Real
    framing damage (oversize length, malformed JSON) raises :class:`ProtocolError`
    and poisons the reader, because after that the byte stream cannot be trusted.
    """

    def __init__(self, max_message_size: int = MAX_MESSAGE_SIZE) -> None:
        self._buf = bytearray()
        self._max = int(max_message_size)
        self._broken: str | None = None
        self.skipped_unknown = 0

    @property
    def pending_bytes(self) -> int:
        """Bytes buffered for an incomplete frame."""
        return len(self._buf)

    @property
    def broken(self) -> bool:
        return self._broken is not None

    def reset(self) -> None:
        """Drop all buffered data and clear the error state (new connection)."""
        self._buf.clear()
        self._broken = None
        self.skipped_unknown = 0

    def feed(self, data: bytes) -> list[tuple[MsgType, dict, bytes | None]]:
        if self._broken is not None:
            raise ProtocolError(f"reader is broken: {self._broken}")
        if data:
            self._buf += data

        out: list[tuple[MsgType, dict, bytes | None]] = []
        while True:
            if len(self._buf) < FRAME_PREFIX_SIZE:
                break
            (total_len,) = _LEN.unpack_from(self._buf, 0)
            if total_len < 1:
                self._fail("zero-length frame")
            if total_len > self._max:
                self._fail(
                    f"frame of {total_len} bytes exceeds the {self._max} byte cap"
                )
            frame_end = FRAME_PREFIX_SIZE + total_len
            if len(self._buf) < frame_end:
                break

            type_byte = self._buf[FRAME_PREFIX_SIZE]
            payload = bytes(self._buf[FRAME_PREFIX_SIZE + 1:frame_end])
            del self._buf[:frame_end]

            try:
                msg = MsgType(type_byte)
            except ValueError:
                self.skipped_unknown += 1
                continue

            try:
                header, blob = decode(msg, payload)
            except ProtocolError as exc:
                self._fail(str(exc))
            out.append((msg, header, blob))

        return out

    def _fail(self, reason: str) -> None:
        self._broken = reason
        self._buf.clear()
        raise ProtocolError(reason)
