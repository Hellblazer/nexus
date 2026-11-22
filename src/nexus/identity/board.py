# SPDX-License-Identifier: AGPL-3.0-or-later
# Copyright (c) 2026 Hal Hildebrand. All rights reserved.
"""RDR-253 Step 5: the ``identity:`` line of a threads-board entry.

A threads entry carries ``identity: i<n>``. The identity outlives ``/clear`` and restart; the session id does not.
Every board reader (the compaction checkpoint, ``nx ask`` answer routing, the console, the due-date hook, the aura
threads block) finds an entry by its identity line only (Phase 2): an older entry's ``session:`` line is not read.

The line is read exactly as the engine's fence reads it (``service/.../db/IdentityLine.java``): lower-case
``identity:`` at the start of a line, then ``i`` and digits, nothing else on the line. A line the engine would not
fence is no identity here either, so the board never trusts a line the engine did not check.
"""
from __future__ import annotations

import re
import time
from collections.abc import Iterable, Mapping
from pathlib import Path
from typing import Any

#: The engine's pattern (IdentityLine.LINE), applied per line.
_LINE = re.compile(r"^identity:[ \t]*(i[0-9]+)[ \t\r]*$", re.MULTILINE)
#: An identity id as it is written and sent: ``i`` and digits.
IDENTITY_ID = re.compile(r"i[0-9]+")


def is_identity_id(value: str) -> bool:
    return bool(IDENTITY_ID.fullmatch(value or ""))


def identity_of(content: str) -> str:
    """The entry's identity (the first fenced ``identity:`` line), or ``""`` when it has none."""
    m = _LINE.search(content or "")
    return m.group(1) if m else ""


def entries_for_identity(entries: Iterable[Mapping[str, Any]], identity: str) -> list[str]:
    """Titles of the entries whose identity line names *identity*, in the order given."""
    if not identity:
        return []
    return [str(e.get("title", "")) for e in entries if identity_of(str(e.get("content", ""))) == identity]


def identity_for_session(session_id: str, config_dir: Path | None = None) -> str:
    """The identity a token file binds *session_id* to, or ``""`` (:func:`binding_for_session`'s first half)."""
    return binding_for_session(session_id, config_dir)[0]


def binding_for_session(session_id: str, config_dir: Path | None = None) -> tuple[str, str]:
    """``(identity, token)`` a token file binds *session_id* to, or ``("", "")``; *token* is ``""`` when the file
    holds none (an engine that issues no identity tokens).

    A hook knows only its payload's session id. The token files under ``<config>/identity/`` name each one's session
    id and every session id the identity has held (``session_ids``), so a session id from before ``/clear`` still
    finds its seat. When several files match, one whose CURRENT session id is the wanted one outranks one that merely
    lists it (after ``/clear`` plus a ``/resume`` under another seat, the old seat's file still lists the id the new
    seat now holds), then the newest by mtime wins.
    """
    if not session_id:
        return "", ""
    from nexus.identity import token_contract  # noqa: PLC0415 -- hook import cost
    from nexus.identity.token_file import identity_dir, read_token_file, token_files  # noqa: PLC0415 -- hook import cost

    if config_dir is None:
        from nexus import config as _nx_config  # noqa: PLC0415 -- a module attribute so a patched config dir reaches it

        config_dir = _nx_config.nexus_config_dir()
    want = session_id.lower()
    best: tuple[int, float, str, str] | None = None
    for path in token_files(identity_dir(config_dir)):
        record = read_token_file(path)
        if record is None or not is_identity_id(str(record.identity or "")):
            continue
        held = {s.lower() for s in (record.session_id, *record.session_ids) if s}
        if want not in held:
            continue
        try:
            stamp = path.stat().st_mtime
        except OSError:
            continue
        is_current = int(bool(record.session_id) and record.session_id.lower() == want)
        if best is None or (is_current, stamp) > best[:2]:
            fresh = record.token if record.token and not (
                record.expires_at is not None and record.expires_at <= time.time() + token_contract.EXPIRY_MARGIN_S
            ) else ""  # never an expired token
            best = (is_current, stamp, str(record.identity), fresh or "")
    return (best[2], best[3]) if best else ("", "")


#: The title word of an ephemeral identity launched with no display name (RDR-253 Step 5, nexus-hczdo.1.8). The RDR
#: titles an ephemeral's entry ``<display name> (i<n>)``; with no name the same shape keeps one pattern, so a reader
#: (or a person) can always take the id off the end of a title.
EPHEMERAL_TITLE_WORD = "ephemeral"
BOARD_PROJECT = "threads"


def board_title(*, identity: str, alias: str | None = None, display_name: str | None = None) -> str:
    """The title a session gives its threads entry. A seat's is its live alias; an ephemeral identity (no alias) is
    ``<display name> (i<n>)``, or ``ephemeral (i<n>)`` when the launch gave no display name. An identity id is never
    an alias (alias names shaped ``i<digits>`` are refused), so no title here collides with a seat's."""
    if alias:
        return alias
    return f"{(display_name or '').strip() or EPHEMERAL_TITLE_WORD} ({identity})"


def retitle_entry(memory: Any, identity: str, title: str) -> str:
    """Move *identity*'s threads entry to *title* with ``put_replacing`` (the new title and the old one's quarantine
    in one engine transaction). Returns one line saying what happened, or ``""`` when no entry carries the line.

    This is the fallback half of ``nx identity promote`` for an engine older than 0.1.198, whose promote does not
    retitle (from 0.1.198 on the engine does it in the promote's own transaction and ``ops.promote`` never calls
    this). The write is fenced like any write to
    the entry, so it lands only when the caller is the owning session; an operator's shell gets the engine's
    refusal, which comes back as a line, never an exception: the identity is a seat by then and the entry is still
    found by its ``identity:`` line whatever its title.
    """
    found = None
    try:
        for hit in memory.search(f"identity: {identity}", project=BOARD_PROJECT, access="silent")[:10]:
            row = memory.get(project=BOARD_PROJECT, title=hit.get("title", ""))
            if row and identity_of(str(row.get("content", ""))) == identity:
                found = row
                break
        if found is None:
            return ""
        old = str(found.get("title", ""))
        if old == title:
            return ""
        if memory.get(project=BOARD_PROJECT, title=title) is not None:
            return f"board entry keeps its title {old!r}: an entry titled {title!r} already exists"
        memory.put_replacing(BOARD_PROJECT, title, str(found.get("content", "")), old, tags=str(found.get("tags") or ""))
    except Exception as exc:  # noqa: BLE001 -- the promote already happened; the entry is found by its identity: line
        status = getattr(getattr(exc, "response", None), "status_code", None)
        why = ("only the owning session may write its entry: run promote inside that session, or retitle it there"
               if status in (401, 403) else f"{type(exc).__name__}: {exc}")
        old = str((found or {}).get("title", "")) or "its old title"
        return f"board entry keeps its title {old!r}: {why}"
    return f"board entry retitled {old!r} to {title!r}"
