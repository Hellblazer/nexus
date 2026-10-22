# SPDX-License-Identifier: AGPL-3.0-or-later
"""When a mailbox or board message is shown as "unverified" (bead nexus-ggmg7,
F3 step 2c, JDR-002).

``from`` on a tuple is a claim the writer typed. ``sender_session`` is a fact
the engine wrote: the session id of the minted session token the write came in
under, set on INSERT only and never a reason to refuse a write. This module is
the one decision that compares them, so every renderer (the mailbox drain hook,
both copies, and the channel push) labels the same rows.

STDLIB ONLY, and no import at all. The drain hook runs on every prompt, so its
import cost is measured, and its plugin copy cannot import ``nexus``.
``conexus/hooks/scripts/_tuple_sender.py`` is a byte-identical copy of this
file, pinned by ``tests/test_tuple_sender_label.py``; edit both.

THE TABLE (``stamp_known`` is whether the row carried the ``sender_session``
key at all):

* key absent (an engine that predates the stamp, or a client row that never saw
  the field): no label. Labelling every message across an engine/client skew
  would teach readers to ignore the label.
* no ``from`` claim: no label. There is nothing to compare.
* ``from`` equals the stamp (two UUIDs compare case-insensitively): no label.
* stamp null: ``unverified``. Null means any of: no live minted session at
  write time (including a stale token the client dropped), a reply row until
  nexus-g2ckl, or a row that predates the column. The label cannot tell which.
* ``from`` is an agent id (``a`` plus 16 hex) and the row is stamped: it can
  never equal a session, so it reads ``unverified, sent under session <S>``.
  Subagents share the parent's token (JDR-001), so that is the most the stamp
  can say about one.
* anything else that differs: ``unverified``.

NO LABEL IS NOT VERIFICATION. Any data-token holder can mint a session token
for any session id, so a match says the writer presented the claimed session's
token, not that it is that session. The label catches a careless or naive
forgery; the tenant is still the security boundary (JDR-002).
"""
from __future__ import annotations

UNVERIFIED = "unverified"

_HEX = frozenset("0123456789abcdef")
_UUID_HYPHENS = (8, 13, 18, 23)


def _is_uuid(value: str) -> bool:
    """Canonical 36-character form only, either case."""
    if len(value) != 36:
        return False
    lowered = value.lower()
    return all(
        (c == "-") if i in _UUID_HYPHENS else (c in _HEX) for i, c in enumerate(lowered)
    )


def _is_agent_id(value: str) -> bool:
    """``a`` plus 16 hex, the subagent id shape."""
    return len(value) == 17 and value[0] == "a" and all(c in _HEX for c in value[1:])


def _same_session(claimed: str, stamped: str) -> bool:
    if _is_uuid(claimed) and _is_uuid(stamped):
        return claimed.lower() == stamped.lower()
    return claimed == stamped


def sender_label(sender_from, sender_session, *, stamp_known: bool = True) -> str | None:
    """The label for one message, or ``None`` for no label. See the module table."""
    if not stamp_known:
        return None
    if sender_from is None or sender_from == "":
        return None
    if not isinstance(sender_session, str) or not sender_session:
        return UNVERIFIED
    if isinstance(sender_from, str):
        if _same_session(sender_from, sender_session):
            return None
        if _is_agent_id(sender_from):
            return f"{UNVERIFIED}, sent under session {sender_session}"
    return UNVERIFIED
