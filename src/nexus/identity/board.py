# SPDX-License-Identifier: AGPL-3.0-or-later
# Copyright (c) 2026 Hal Hildebrand. All rights reserved.
"""RDR-253 Step 5: the ``identity:`` line of a threads-board entry.

A threads entry carries ``identity: i<n>`` beside its ``session: <id>`` line through Phase 1. The identity outlives
``/clear`` and restart; the session id does not. Every board reader (the compaction checkpoint, ``nx ask`` answer
routing, the console, the due-date hook, the aura threads block) finds an entry by its identity first and falls back
to the ``session:`` line for an entry written before the identity line existed.

The line is read exactly as the engine's fence reads it (``service/.../db/IdentityLine.java``): lower-case
``identity:`` at the start of a line, then ``i`` and digits, nothing else on the line. A line the engine would not
fence is no identity here either, so the board never trusts a line the engine did not check.
"""
from __future__ import annotations

import re
from collections.abc import Iterable, Mapping
from typing import Any

#: The engine's pattern (IdentityLine.LINE), applied per line.
_LINE = re.compile(r"^identity:[ \t]*(i[0-9]+)[ \t\r]*$", re.MULTILINE)
_SESSION = re.compile(r"^\s*session:\s*(\S+)\s*$", re.MULTILINE | re.IGNORECASE)
#: An identity id as it is written and sent: ``i`` and digits.
IDENTITY_ID = re.compile(r"i[0-9]+")


def is_identity_id(value: str) -> bool:
    return bool(IDENTITY_ID.fullmatch(value or ""))


def identity_of(content: str) -> str:
    """The entry's identity (the first fenced ``identity:`` line), or ``""`` when it has none."""
    m = _LINE.search(content or "")
    return m.group(1) if m else ""


def session_of(content: str) -> str:
    """The entry's first ``session:`` value, or ``""``."""
    m = _SESSION.search(content or "")
    return m.group(1) if m else ""


def belongs_to(content: str, *, identity: str = "", session_ids: Iterable[str] = ()) -> bool:
    """Whether an entry is the caller's: its identity line names *identity*; or, for an entry with no identity line,
    its ``session:`` line names one of *session_ids*. An entry whose identity line names another identity is never
    the caller's, whatever its ``session:`` line says (a stale session line after a seat moved)."""
    own = identity_of(content)
    if own:
        return bool(identity) and own == identity
    sid = session_of(content).lower()
    return bool(sid) and sid in {s.lower() for s in session_ids if s}


def entries_for_identity(entries: Iterable[Mapping[str, Any]], identity: str) -> list[str]:
    """Titles of the entries whose identity line names *identity*, in the order given."""
    if not identity:
        return []
    return [str(e.get("title", "")) for e in entries if identity_of(str(e.get("content", ""))) == identity]
