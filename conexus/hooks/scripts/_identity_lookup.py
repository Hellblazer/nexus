# SPDX-License-Identifier: AGPL-3.0-or-later
"""RDR-253 Step 5: find this session's identity from the token files, stdlib only.

A plugin hook script cannot import ``nexus``. It knows its payload's session id; the identity token files under
``<nexus config dir>/identity/<claude pid>.<startedAt>.token`` (written by ``nexus.identity.token_file``) each name
the identity, its token, the current session id and every session id the identity has held in that process
(``session_ids``, so an id from before ``/clear`` still finds the seat). This module reads them the way
``nexus.identity.board.identity_for_session`` does; the two are kept to one rule by
``tests/hooks/test_identity_lookup_parity.py``.

Never raises: a hook that cannot read a token file falls back to the ``session:`` line, as an old client does.
"""
from __future__ import annotations

import json
import re
from pathlib import Path

_TOKEN_NAME = re.compile(r"^[0-9]+\.[0-9]+\.token$")  #: token_file._TOKEN_NAME, without the groups
_IDENTITY_ID = re.compile(r"i[0-9]+")
_IDENTITY_LINE = re.compile(r"^identity:[ \t]*(i[0-9]+)[ \t\r]*$", re.MULTILINE)  #: the engine's IdentityLine.LINE


def identity_of(body: str) -> str:
    """The first fenced ``identity:`` line's value in *body*, or ``""``."""
    m = _IDENTITY_LINE.search(body or "")
    return m.group(1) if m else ""


def lookup(session_id: str, config_dir: Path) -> tuple[str, str]:
    """``(identity, token)`` a token file binds *session_id* to, or ``("", "")``. The newest matching file by mtime
    wins; *token* is ``""`` when the file holds none (an engine that issues no identity tokens)."""
    if not session_id:
        return "", ""
    want = session_id.lower()
    best: tuple[float, str, str] | None = None
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
        held = [data.get("session_id"), *(data.get("session_ids") or [])] if isinstance(
            data.get("session_ids") or [], list) else [data.get("session_id")]
        if want not in {s.lower() for s in held if isinstance(s, str) and s}:
            continue
        token = data.get("token") if isinstance(data.get("token"), str) else ""
        if best is None or stamp > best[0]:
            best = (stamp, identity, token or "")
    return (best[1], best[2]) if best else ("", "")
