# SPDX-License-Identifier: AGPL-3.0-or-later
"""A file or directory name for a collection label (RDR-229 P3.6, review I4).

A collection label is free-form: up to 255 UTF-8 bytes, any character but a control character and ``/``. It can hold
``:`` or ``\\`` (not legal in a Windows file name), be ``..`` or ``.`` (a path escape), be ``CON`` or ``NUL`` (a reserved
Windows device name), or be longer than a filesystem allows in one path component. Any file or directory a command names
after a label goes through :func:`safe_label_filename`.
"""
from __future__ import annotations

import hashlib
import re

#: The bytes kept as they are. Everything else is percent-encoded (``%`` itself included), so the encoding is
#: injective and a conformant collection name (alphanumerics, ``_``, ``-``) comes through unchanged.
_KEEP = re.compile(rb"[A-Za-z0-9._-]")

#: Windows device names, reserved with or without an extension (``CON``, ``con.txt``).
_WINDOWS_RESERVED = frozenset({"CON", "PRN", "AUX", "NUL", *(f"COM{i}" for i in range(1, 10)),
                               *(f"LPT{i}" for i in range(1, 10))})

#: A name longer than this is cut to it (on an escape boundary) and takes ``-<hash>`` of the whole label, so two long
#: labels with one prefix stay distinct. About 100 bytes plus the hash stays well under a 255-byte component limit
#: with a date and an extension appended.
_MAX_BODY = 100
_HASH_CHARS = 8


def _escape(byte: int) -> str:
    return f"%{byte:02X}"


def safe_label_filename(label: str) -> str:
    """One path component for ``label``: percent-encoded outside ``[A-Za-z0-9._-]``, never ``.`` or ``..``, never ending
    in ``.``, never a Windows reserved name, and at most about 110 bytes. Distinct labels give distinct names."""
    raw = label.encode("utf-8", "surrogatepass")
    parts = [chr(b) if _KEEP.fullmatch(bytes([b])) else _escape(b) for b in raw]
    # A trailing dot is dropped by Windows, and '.' / '..' are the current and parent directory: encode the run of
    # trailing dots (the whole of '.' and '..' included).
    i = len(parts)
    while i > 0 and parts[i - 1] == ".":
        parts[i - 1] = _escape(ord("."))
        i -= 1
    # A reserved device name, with or without an extension: encode its first character.
    if parts and "".join(parts).split(".", 1)[0].upper() in _WINDOWS_RESERVED:
        parts[0] = _escape(raw[0])
    name = "".join(parts)
    if not name:
        return "label-" + hashlib.sha256(raw).hexdigest()[:_HASH_CHARS]
    if len(name) <= _MAX_BODY:
        return name
    cut = _MAX_BODY
    while cut > 0 and "%" in name[max(0, cut - 2):cut]:  # never cut inside an escape
        cut -= 1
    return f"{name[:cut]}-{hashlib.sha256(raw).hexdigest()[:_HASH_CHARS]}"
