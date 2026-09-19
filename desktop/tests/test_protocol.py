"""Framing codec tests: round trips, split reads, and hostile input."""

from __future__ import annotations

import json
import struct
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from stopmotion.protocol import (  # noqa: E402
    FRAME_PREFIX_SIZE,
    MAX_MESSAGE_SIZE,
    MessageReader,
    MsgType,
    ProtocolError,
    decode,
    encode,
)


def read_one(data: bytes):
    messages = MessageReader().feed(data)
    assert len(messages) == 1
    return messages[0]


# ------------------------------------------------------------------ framing


def test_frame_layout_matches_the_spec():
    raw = encode(MsgType.CAPTURE, {"request_id": "c1"})
    total_len = struct.unpack_from(">I", raw, 0)[0]
    assert total_len == len(raw) - FRAME_PREFIX_SIZE  # length covers type + payload
    assert raw[4] == int(MsgType.CAPTURE)
    header_len = struct.unpack_from(">H", raw, 5)[0]
    assert json.loads(raw[7:7 + header_len]) == {"request_id": "c1"}
    assert len(raw) == 7 + header_len  # no trailing bytes on a pure-JSON message


@pytest.mark.parametrize("msg", list(MsgType))
def test_every_type_round_trips(msg):
    header = {"n": int(msg), "name": msg.name}
    got_type, got_header, got_blob = read_one(encode(msg, header))
    assert got_type is msg
    assert got_header == header
    assert got_blob is None


def test_binary_payload_round_trips():
    jpeg = bytes(range(256)) * 40
    got_type, header, blob = read_one(
        encode(MsgType.PREVIEW_FRAME, {"seq": 7, "w": 1920, "h": 1080}, jpeg)
    )
    assert got_type is MsgType.PREVIEW_FRAME
    assert header == {"seq": 7, "w": 1920, "h": 1080}
    assert blob == jpeg


def test_empty_header_round_trips_as_empty_dict():
    for msg in (MsgType.PING, MsgType.PONG):
        got_type, header, blob = read_one(encode(msg))
        assert (got_type, header, blob) == (msg, {}, None)


def test_non_ascii_header_round_trips():
    header = {"message": "Fokus zu nah — bitte näher ran ✓"}
    assert read_one(encode(MsgType.ERROR, header))[1] == header


def test_empty_blob_is_reported_as_none():
    assert read_one(encode(MsgType.CAPTURE_RESULT, {"request_id": "x"}, b""))[2] is None


def test_decode_matches_the_reader():
    raw = encode(MsgType.CONFIG_ACK, {"iso": 200}, b"\x01\x02")
    header, blob = decode(MsgType.CONFIG_ACK, raw[FRAME_PREFIX_SIZE + 1:])
    assert header == {"iso": 200}
    assert blob == b"\x01\x02"


# ------------------------------------------------------------ stream shapes


def test_several_messages_in_one_chunk():
    chunk = (
        encode(MsgType.HELLO, {"a": 1})
        + encode(MsgType.PING)
        + encode(MsgType.PREVIEW_FRAME, {"seq": 1}, b"jpeg")
    )
    messages = MessageReader().feed(chunk)
    assert [m[0] for m in messages] == [
        MsgType.HELLO,
        MsgType.PING,
        MsgType.PREVIEW_FRAME,
    ]
    assert messages[2][2] == b"jpeg"


def test_partial_feed_byte_by_byte():
    stream = encode(MsgType.HELLO, {"hardware_level": "LEVEL_3"}) + encode(
        MsgType.PREVIEW_FRAME, {"seq": 2}, b"\xff\xd8\xff\xe0payload"
    )
    reader = MessageReader()
    collected = []
    for i in range(len(stream)):
        collected.extend(reader.feed(stream[i:i + 1]))
    assert [m[0] for m in collected] == [MsgType.HELLO, MsgType.PREVIEW_FRAME]
    assert collected[1][2] == b"\xff\xd8\xff\xe0payload"
    assert reader.pending_bytes == 0


def test_message_split_across_two_chunks_with_a_tail():
    first = encode(MsgType.CONFIG_ACK, {"iso": 100})
    second = encode(MsgType.CAPTURE_RESULT, {"request_id": "r"}, b"12345")
    stream = first + second
    reader = MessageReader()
    cut = len(first) + 6
    assert [m[0] for m in reader.feed(stream[:cut])] == [MsgType.CONFIG_ACK]
    assert reader.pending_bytes == cut - len(first)
    assert [m[0] for m in reader.feed(stream[cut:])] == [MsgType.CAPTURE_RESULT]


def test_feed_with_no_data_is_harmless():
    reader = MessageReader()
    assert reader.feed(b"") == []
    assert reader.feed(encode(MsgType.PING)) != []


def test_large_blob_round_trips():
    blob = b"\x00\xff" * (512 * 1024)  # 1 MiB, a plausible full-res still
    reader = MessageReader()
    raw = encode(MsgType.CAPTURE_RESULT, {"request_id": "big"}, blob)
    out = []
    for i in range(0, len(raw), 4096):  # arrive in socket-sized pieces
        out.extend(reader.feed(raw[i:i + 4096]))
    assert len(out) == 1
    assert out[0][2] == blob


# --------------------------------------------------------------- bad input


def test_oversize_frame_is_rejected_without_buffering_it():
    reader = MessageReader()
    with pytest.raises(ProtocolError, match="exceeds"):
        reader.feed(struct.pack(">I", MAX_MESSAGE_SIZE + 1) + b"\x05")
    assert reader.broken
    assert reader.pending_bytes == 0


def test_encoding_something_too_large_is_refused():
    with pytest.raises(ProtocolError, match="exceeds"):
        encode(MsgType.PREVIEW_FRAME, {"seq": 1}, b"\x00" * (MAX_MESSAGE_SIZE + 1))
    with pytest.raises(ProtocolError, match="header"):
        encode(MsgType.HELLO, {"junk": "x" * 70000})


def test_zero_length_frame_is_rejected():
    with pytest.raises(ProtocolError):
        MessageReader().feed(struct.pack(">I", 0))


def test_malformed_json_header_is_rejected():
    bad = b"{not json"
    frame = struct.pack(">IBH", 1 + 2 + len(bad), int(MsgType.HELLO), len(bad)) + bad
    with pytest.raises(ProtocolError, match="malformed JSON"):
        MessageReader().feed(frame)


def test_non_object_json_header_is_rejected():
    body = b"[1,2,3]"
    frame = struct.pack(">IBH", 1 + 2 + len(body), int(MsgType.HELLO), len(body)) + body
    with pytest.raises(ProtocolError, match="must be an object"):
        MessageReader().feed(frame)


def test_header_length_beyond_the_frame_is_rejected():
    frame = struct.pack(">IBH", 3, int(MsgType.HELLO), 99)
    with pytest.raises(ProtocolError, match="header length"):
        MessageReader().feed(frame)


def test_payload_too_short_for_a_header_length():
    frame = struct.pack(">IB", 1, int(MsgType.PING))  # no 2-byte header length
    with pytest.raises(ProtocolError, match="too short"):
        MessageReader().feed(frame)


def test_a_broken_reader_stays_broken_until_reset():
    reader = MessageReader()
    with pytest.raises(ProtocolError):
        reader.feed(struct.pack(">I", MAX_MESSAGE_SIZE * 2) + b"\x01")
    with pytest.raises(ProtocolError, match="broken"):
        reader.feed(encode(MsgType.PING))
    reader.reset()
    assert not reader.broken
    assert [m[0] for m in reader.feed(encode(MsgType.PING))] == [MsgType.PING]


def test_unknown_type_is_skipped_not_fatal():
    body = b'{"future":true}'
    unknown = struct.pack(">IBH", 1 + 2 + len(body), 99, len(body)) + body
    reader = MessageReader()
    messages = reader.feed(unknown + encode(MsgType.PONG))
    assert [m[0] for m in messages] == [MsgType.PONG]
    assert reader.skipped_unknown == 1


def test_a_smaller_cap_can_be_configured():
    reader = MessageReader(max_message_size=1024)
    with pytest.raises(ProtocolError, match="1024"):
        reader.feed(encode(MsgType.CAPTURE_RESULT, {"request_id": "r"}, b"\x00" * 2048))
