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
* stamp null (or not a string): ``unverified``. Null means any of: no live
  minted session at write time (including a stale token the client dropped), a
  reply row written by an engine before nexus-g2ckl (engine v0.1.163 stamps
  the acker's session on an ack reply row), a row that predates the column, or
  a writer that is not session-bound (a CI board post). The label cannot tell
  which, so renderers say "no session on record" for this case, never "does
  not match".
* ``from`` is an agent id (``a`` plus 16 hex) and the row is stamped with a
  canonical UUID: it can never equal a session, so it reads
  ``unverified, sent under session <S>``. Subagents share the parent's token
  (JDR-001), so that is the most the stamp can say about one.
* anything else that differs: ``unverified``.

The session id appears in a label only when the stamp is a canonical UUID.
The engine stores whatever string the minted token carried, so a stamp is
attacker-chosen text; one with a newline in it must not reach text the reader
is told to trust. Any other stamp reads a bare ``unverified``.

NO LABEL IS NOT VERIFICATION. Any data-token holder can mint a session token
for any session id, so a match says the writer presented the claimed session's
token, not that it is that session. The label catches a careless or naive
forgery; the tenant is still the security boundary (JDR-002).
"""
from __future__ import annotations

UNVERIFIED = "unverified"

#: Why a row is unverified, for renderers that word each case differently.
CASE_MISMATCH = "mismatch"  # the claim differs from a session the engine wrote down
CASE_NULL = "null"  # the engine has no session on record for the writer
CASE_AGENT = "agent"  # the claim is an agent id; the stamp is a canonical UUID

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


def canonical_session(stamp) -> str | None:
    """The stamp when it is a canonical UUID string, else ``None``. The only
    form a renderer may put in text the reader is told to trust."""
    return stamp if isinstance(stamp, str) and _is_uuid(stamp) else None


def sender_case(sender_from, sender_session, *, stamp_known: bool = True) -> str | None:
    """``None`` for no label, else one of ``CASE_MISMATCH``, ``CASE_NULL``,
    ``CASE_AGENT``. See the module table."""
    if not stamp_known:
        return None
    if sender_from is None or sender_from == "":
        return None
    if not isinstance(sender_session, str) or not sender_session:
        return CASE_NULL
    if isinstance(sender_from, str):
        if _same_session(sender_from, sender_session):
            return None
        if _is_agent_id(sender_from) and _is_uuid(sender_session):
            return CASE_AGENT
    return CASE_MISMATCH


def sender_label(sender_from, sender_session, *, stamp_known: bool = True) -> str | None:
    """The label for one message, or ``None`` for no label. See the module table."""
    case = sender_case(sender_from, sender_session, stamp_known=stamp_known)
    if case is None:
        return None
    if case == CASE_AGENT:
        return f"{UNVERIFIED}, sent under session {sender_session}"
    return UNVERIFIED
