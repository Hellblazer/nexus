# SPDX-License-Identifier: AGPL-3.0-or-later
# Copyright (c) 2026 Hal Hildebrand. All rights reserved.
"""Length-prefixed JSON frames between the parent and the worker (RDR-242 Technical Design 6).

A frame is a 4-byte big-endian unsigned length, then that many bytes of UTF-8
JSON holding one object with a string ``"t"``. Each direction has a cap that
the reader enforces on the length *before* it reads the body, so a hostile or
broken peer cannot make the reader allocate more than the cap.

Standard library only: the worker imports this module under ``python -I``.
"""

from __future__ import annotations

import json
import re
import struct
from typing import Any, BinaryIO

#: worker -> parent: a verb-call frame (a verb name and plain arguments).
CALL_FRAME_CAP: int = 64 * 1024
#: worker -> parent: the one result, error or limit frame (the 1 MB result
#: bound of item 11, plus room for the frame's own fields).
END_FRAME_CAP: int = 1024 * 1024 + 64 * 1024
#: parent -> worker: the first (start) frame: a 64 KB script, its grants, the
#: verb names and the limits fit in far less.
START_FRAME_CAP: int = 1024 * 1024
#: worker -> parent, before the start frame: only the ready frame.
READY_FRAME_CAP: int = 4 * 1024

#: parent -> worker: room beyond the verb-result cap for the reply's own fields.
REPLY_SLACK: int = 64 * 1024

#: One client process runs at most this many workers at once (a fifth
#: ``supervise`` fails with ``ScriptBusyError``).
MAX_WORKERS: int = 4
#: A run's verb calls; the 10,001st ends it with ``ScriptWorkerError``.
MAX_CALLS: int = 10_000

_HEADER = struct.Struct(">I")

#: Lua integers are 64-bit. ``lupa`` raises ``OverflowError`` converting a Python int outside this range, and
#: ``json.loads`` makes one from any long digit run, so a frame is refused when it holds one.
INT64_MIN: int = -(2**63)
INT64_MAX: int = 2**63 - 1
#: 19 digits is the shortest run that can leave the range; most frames hold none, and skip the checked decode.
_LONG_DIGITS = re.compile(rb"\d{19}")


class IntOutOfRange(ValueError):
    """A JSON integer outside the 64-bit range."""


def _int64(text: str) -> int:
    n = int(text)
    if not INT64_MIN <= n <= INT64_MAX:
        raise IntOutOfRange(
            f"integer {text[:24]}{'...' if len(text) > 24 else ''} is outside the 64-bit range"
        )
    return n


def _loads(body: bytes) -> Any:
    """``json.loads`` of UTF-8 *body*; an integer outside 64 bits raises :class:`IntOutOfRange`."""
    text = body.decode("utf-8")
    if _LONG_DIGITS.search(body):
        return json.loads(text, parse_int=_int64)
    return json.loads(text)


def has_wide_ints(data: bytes) -> bool:
    """True when JSON *data* holds an integer outside the 64-bit range (a digit run inside a string does not count)."""
    if not _LONG_DIGITS.search(data):
        return False
    try:
        _loads(data)
    except IntOutOfRange:
        return True
    except ValueError:
        return False
    return False


def reply_frame_cap(verb_result_cap: int) -> int:
    """The cap on a parent-to-worker ``reply`` frame for a verb-result cap."""
    return verb_result_cap + REPLY_SLACK


class VerbReplyError(Exception):
    """The parent answered a verb call with ``ok: false``; the message is the script's Lua error."""


class FrameError(Exception):
    """A frame could not be read or written.

    ``kind`` is ``"eof"`` (the peer closed before a whole frame arrived),
    ``"too_large"`` (a length over the cap) or ``"malformed"`` (not UTF-8
    JSON, not an object, or no string ``"t"``).
    """

    def __init__(self, kind: str, detail: str) -> None:
        super().__init__(f"{kind}: {detail}")
        self.kind = kind
        self.detail = detail


def encode_frame(obj: dict[str, Any], cap: int) -> bytes:
    """Encode *obj* as a frame, refusing a body over *cap* bytes."""
    try:
        body = json.dumps(
            obj, separators=(",", ":"), ensure_ascii=False, allow_nan=False
        ).encode("utf-8")
    except (TypeError, ValueError, UnicodeEncodeError) as exc:
        raise FrameError("malformed", f"not encodable as JSON: {exc}") from exc
    if len(body) > cap:
        raise FrameError("too_large", f"{len(body)} bytes is over the {cap}-byte cap")
    return _HEADER.pack(len(body)) + body


def write_frame(stream: BinaryIO, obj: dict[str, Any], cap: int) -> int:
    """Write one frame and flush; returns the body length."""
    data = encode_frame(obj, cap)
    stream.write(data)
    stream.flush()
    return len(data) - _HEADER.size


def _read_exact(stream: BinaryIO, n: int, what: str) -> bytes:
    data = stream.read(n)
    if data is None or len(data) != n:
        got = 0 if data is None else len(data)
        raise FrameError("eof", f"end of stream in the {what} ({got} of {n} bytes)")
    return data


def read_frame(stream: BinaryIO, cap: int) -> dict[str, Any]:
    """Read one frame; raise :class:`FrameError` for eof, an over-cap length or a bad body."""
    return read_frame_sized(stream, cap)[0]


def read_frame_sized(stream: BinaryIO, cap: int) -> tuple[dict[str, Any], int]:
    """:func:`read_frame` and the body length in bytes."""
    header = stream.read(_HEADER.size)
    if not header:
        raise FrameError("eof", "end of stream before a frame")
    if len(header) != _HEADER.size:
        raise FrameError("eof", "end of stream in a frame header")
    (length,) = _HEADER.unpack(header)
    if length > cap:
        raise FrameError(
            "too_large", f"a frame of {length} bytes is over the {cap}-byte cap"
        )
    body = _read_exact(stream, length, "frame body")
    try:
        obj = _loads(body)
    except IntOutOfRange as exc:
        raise FrameError("malformed", str(exc)) from exc
    except (UnicodeDecodeError, ValueError, RecursionError) as exc:
        raise FrameError("malformed", f"not UTF-8 JSON: {exc}") from exc
    if not isinstance(obj, dict) or not isinstance(obj.get("t"), str):
        raise FrameError("malformed", 'a frame is a JSON object with a string "t"')
    return obj, length
