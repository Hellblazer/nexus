# SPDX-License-Identifier: AGPL-3.0-or-later
# Copyright (c) 2026 Hal Hildebrand. All rights reserved.
"""RDR-242 Technical Design 6, Frames: 4-byte big-endian length, then UTF-8 JSON, with a cap per direction."""

from __future__ import annotations

import io
import struct

import pytest

from nexus.lua_runtime import frames


def _roundtrip(obj: dict, cap: int = 1 << 20) -> dict:
    buf = io.BytesIO()
    frames.write_frame(buf, obj, cap)
    buf.seek(0)
    return frames.read_frame(buf, cap)


def test_frame_is_a_big_endian_length_then_json() -> None:
    buf = io.BytesIO()
    frames.write_frame(buf, {"t": "ready"}, 1024)
    raw = buf.getvalue()
    body = b'{"t":"ready"}'
    assert raw == struct.pack(">I", len(body)) + body


def test_roundtrip_keeps_unicode_and_nesting() -> None:
    obj = {
        "t": "reply",
        "id": 3,
        "ok": True,
        "value": {"s": "naïve ☃ \U0001f600", "l": [1, 2.5, None, True]},
    }
    assert _roundtrip(obj) == obj


def test_header_over_the_cap_is_refused_before_any_body_is_read() -> None:
    # a 4 GB length with no body: a reader that allocated first would raise eof/MemoryError, not too_large
    stream = io.BytesIO(struct.pack(">I", 0xFFFFFFFF))
    with pytest.raises(frames.FrameError) as err:
        frames.read_frame(stream, frames.CALL_FRAME_CAP)
    assert err.value.kind == "too_large"


def test_a_frame_exactly_at_the_cap_passes_and_one_byte_over_does_not() -> None:
    body = b'{"t":"x","p":"' + b"a" * 100 + b'"}'
    at_cap = io.BytesIO(struct.pack(">I", len(body)) + body)
    assert frames.read_frame(at_cap, len(body))["t"] == "x"
    over = io.BytesIO(struct.pack(">I", len(body)) + body)
    with pytest.raises(frames.FrameError) as err:
        frames.read_frame(over, len(body) - 1)
    assert err.value.kind == "too_large"


def test_write_refuses_a_body_over_the_cap() -> None:
    with pytest.raises(frames.FrameError) as err:
        frames.write_frame(io.BytesIO(), {"t": "call", "p": "x" * 200}, 100)
    assert err.value.kind == "too_large"


@pytest.mark.parametrize(
    "body",
    [
        b"not json",
        b"[1,2]",
        b'{"no_t":1}',
        b'{"t":5}',
        b"\xff\xfe",
        b'{"t":"x"',
        b"NaN",
    ],
    ids=["text", "array", "no-t", "t-not-string", "bad-utf8", "truncated-json", "nan"],
)
def test_a_malformed_body_is_refused(body: bytes) -> None:
    stream = io.BytesIO(struct.pack(">I", len(body)) + body)
    with pytest.raises(frames.FrameError) as err:
        frames.read_frame(stream, 1024)
    assert err.value.kind == "malformed"


def test_end_of_stream_before_a_frame_and_inside_one_are_eof() -> None:
    with pytest.raises(frames.FrameError) as before:
        frames.read_frame(io.BytesIO(b""), 1024)
    assert before.value.kind == "eof"
    with pytest.raises(frames.FrameError) as header:
        frames.read_frame(io.BytesIO(b"\x00\x00"), 1024)
    assert header.value.kind == "eof"
    with pytest.raises(frames.FrameError) as body:
        frames.read_frame(io.BytesIO(struct.pack(">I", 50) + b'{"t":'), 1024)
    assert body.value.kind == "eof"


def test_the_caps_are_the_rdrs() -> None:
    assert frames.CALL_FRAME_CAP == 64 * 1024
    assert frames.END_FRAME_CAP == 1024 * 1024 + 64 * 1024


# ── integers outside 64 bits (lupa raises OverflowError converting them) ────


def _wide(n: int) -> io.BytesIO:
    body = ('{"t":"x","n":%d}' % n).encode()
    return io.BytesIO(struct.pack(">I", len(body)) + body)


@pytest.mark.parametrize(
    "n",
    [2**63, -(2**63) - 1, 2**70, -(10**40)],
    ids=["2^63", "-2^63-1", "2^70", "-10^40"],
)
def test_an_integer_outside_64_bits_is_a_malformed_frame(n: int) -> None:
    with pytest.raises(frames.FrameError) as err:
        frames.read_frame(_wide(n), 1024)
    assert err.value.kind == "malformed" and "64-bit" in err.value.detail


@pytest.mark.parametrize(
    "n", [2**63 - 1, -(2**63), 0, 10**18], ids=["max", "min", "zero", "10^18"]
)
def test_an_integer_inside_64_bits_passes_even_with_19_digits(n: int) -> None:
    assert frames.read_frame(_wide(n), 1024)["n"] == n


def test_a_long_digit_run_in_a_string_or_a_float_is_not_an_integer_out_of_range() -> (
    None
):
    body = b'{"t":"x","s":"123456789012345678901234567890","f":1e300,"g":12345678901234567890.5}'
    got = frames.read_frame(io.BytesIO(struct.pack(">I", len(body)) + body), 1024)
    assert got["s"] == "123456789012345678901234567890" and got["f"] == 1e300


def test_has_wide_ints_sees_what_the_reader_refuses_and_nothing_else() -> None:
    assert frames.has_wide_ints(b'{"a":[1,{"b":99999999999999999999}]}') is True
    assert (
        frames.has_wide_ints(b'{"a":"99999999999999999999","b":9223372036854775807}')
        is False
    )
    assert frames.has_wide_ints(b"not json 99999999999999999999") is False
