# SPDX-License-Identifier: AGPL-3.0-or-later
"""RDR-253 Step 5: find this session's identity from the token files, stdlib only.

A plugin hook script cannot import ``nexus``. It knows its payload's session id; the identity token files under
``<nexus config dir>/identity/<claude pid>.<startedAt>.token`` (written by ``nexus.identity.token_file``) each name
the identity, its token, the current session id and every session id the identity has held in that process
(``session_ids``, so an id from before ``/clear`` still finds the seat). This module reads them the way
``nexus.identity.board.identity_for_session`` does; the two are kept to one rule by
``tests/hooks/test_identity_lookup_parity.py``.

Never raises: a hook that cannot read a token file has no identity, and finds no board entry (RDR-253 Phase 2: the
``session:`` line is not read).
"""
from __future__ import annotations

import json
import re
import time
from pathlib import Path

#: token_contract.EXPIRY_MARGIN_S: a token inside this many seconds of its expiry is withheld too (pinned by
#: tests/hooks/test_identity_lookup_parity.py).
EXPIRY_MARGIN_S = 5.0
_TOKEN_NAME = re.compile(r"^[0-9]+\.[0-9]+\.token$")  #: token_file._TOKEN_NAME, without the groups
_IDENTITY_ID = re.compile(r"i[0-9]+")
_IDENTITY_LINE = re.compile(r"^identity:[ \t]*(i[0-9]+)[ \t\r]*$", re.MULTILINE)  #: the engine's IdentityLine.LINE


def identity_of(body: str) -> str:
    """The first fenced ``identity:`` line's value in *body*, or ``""``."""
    m = _IDENTITY_LINE.search(body or "")
    return m.group(1) if m else ""


def lookup(session_id: str, config_dir: Path) -> tuple[str, str]:
    """``(identity, token)`` a token file binds *session_id* to, or ``("", "")``. A file whose CURRENT ``session_id``
    is *session_id* outranks one that merely lists it in ``session_ids`` (after ``/clear`` plus a ``/resume`` under
    another seat, the old seat's file still lists the id the new seat now holds); then the newest by mtime wins.
    *token* is ``""`` when the file holds none (an engine that issues no identity tokens)."""
    if not session_id:
        return "", ""
    want = session_id.lower()
    best: tuple[int, float, str, str] | None = None
    try:
        paths = [p for p in (Path(config_dir) / "identity").iterdir() if _TOKEN_NAME.match(p.name)]
    except OSError:
        return "", ""
    for path in paths:
        try:
            data = json.loads(path.read_text(encoding="utf-8"))
            stamp = path.stat().st_mtime
        except (OSError, ValueError, RecursionError):
            continue
        if not isinstance(data, dict):
            continue
        identity = data.get("identity")
        if not isinstance(identity, str) or not _IDENTITY_ID.fullmatch(identity):
            continue
        current = data.get("session_id")
        listed = data.get("session_ids")
        held = [current, *(listed if isinstance(listed, list) else [])]
        if want not in {s.lower() for s in held if isinstance(s, str) and s}:
            continue
        is_current = int(isinstance(current, str) and current.lower() == want)
        token = data.get("token") if isinstance(data.get("token"), str) else ""
        expires = data.get("expires_at")
        if token and isinstance(expires, (int, float)) and expires <= time.time() + EXPIRY_MARGIN_S:
            token = ""  # never send an expired token (token_header.current withholds it too); the MCP server refreshes
        if best is None or (is_current, stamp) > best[:2]:
            best = (is_current, stamp, identity, token or "")
    return (best[2], best[3]) if best else ("", "")
