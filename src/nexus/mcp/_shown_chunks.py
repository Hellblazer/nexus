# SPDX-License-Identifier: AGPL-3.0-or-later
"""Short chunk ids on text-mode ``search`` hits, and their resolution in ``store_get_many`` (nexus-2k66t.6, 1a).

Text ``search`` prints ``id <first 8 hex of the chunk_text_hash>`` on each hit it shows and :func:`record`s the full
chash here. ``store_get_many`` passes its ids through :func:`expand`: an id of 8 to 63 lower-case hex characters that
is the prefix of exactly one recorded chash becomes that chash. The record lives in this MCP server process, which
serves one session, and holds the last :data:`CAPACITY` chashes shown. The engine has no chash-prefix lookup, so an
id this process never showed cannot be expanded; it is passed on unchanged and, if it then comes back missing, the
tool says why (:func:`notes_for`).
"""
from __future__ import annotations

import re
import threading
from collections import OrderedDict
from collections.abc import Iterable

__all__ = ["CAPACITY", "SHORT_LEN", "expand", "notes_for", "record", "short_id"]

#: Hex characters printed on a hit. At 8, two of 4,096 recorded chashes share a prefix with odds of about 1 in 500;
#: a shared prefix is reported as ambiguous, never guessed.
SHORT_LEN = 8
CAPACITY = 4096
_FULL = re.compile(r"[0-9a-f]{64}")
_PREFIX = re.compile(r"[0-9a-f]{%d,63}" % SHORT_LEN)

_lock = threading.Lock()
_shown: OrderedDict[str, None] = OrderedDict()


def short_id(chash: str) -> str:
    """The id printed on a hit: the chash's first :data:`SHORT_LEN` characters, or ``""`` for a value that is not a
    full chash."""
    return chash[:SHORT_LEN] if _FULL.fullmatch(chash or "") else ""


def record(chashes: Iterable[str]) -> None:
    """Remember chashes a text search showed; the oldest fall out past :data:`CAPACITY`."""
    with _lock:
        for c in chashes:
            if not _FULL.fullmatch(c or ""):
                continue
            _shown[c] = None
            _shown.move_to_end(c)
        while len(_shown) > CAPACITY:
            _shown.popitem(last=False)


def _clear() -> None:
    """Tests only."""
    with _lock:
        _shown.clear()


def _resolve(token: str, unresolved: dict[str, str]) -> str:
    if not _PREFIX.fullmatch(token):
        return token
    with _lock:
        matches = [c for c in _shown if c.startswith(token)]
    if len(matches) == 1:
        return matches[0]
    if matches:
        shown = ", ".join(m[:16] for m in matches[:5]) + (" ..." if len(matches) > 5 else "")
        unresolved[token] = f"id {token} matches {len(matches)} chunks this session's searches showed ({shown}); give more characters"
    else:
        unresolved[token] = (
            f"id {token} is not the start of a chunk id this session's searches showed; pass the full id "
            "(search with structured=True prints it)"
        )
    return token


def expand(ids: str | list) -> tuple[str | list, dict[str, str]]:
    """*ids* with every short id expanded to its full chash, in the same shape (comma-separated string, flat list
    or list of lists), and ``{short id: why it was not expanded}`` for the ones left as given."""
    unresolved: dict[str, str] = {}
    if isinstance(ids, list):
        if ids and all(isinstance(s, list) for s in ids):
            return [[_resolve(str(x), unresolved) if x else x for x in s] for s in ids], unresolved
        return [_resolve(str(x), unresolved) if x else x for x in ids], unresolved
    tokens = [s.strip() for s in str(ids or "").split(",")]
    return ",".join(_resolve(t, unresolved) if t else t for t in tokens), unresolved


def notes_for(unresolved: dict[str, str], missing: Iterable[str]) -> list[str]:
    """The notes for short ids that came back missing. A 32- to 63-character id that resolved on its own needs no
    note, even though this process never showed it."""
    gone = set(missing)
    return [why for token, why in unresolved.items() if token in gone]
