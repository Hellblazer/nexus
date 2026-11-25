# SPDX-License-Identifier: AGPL-3.0-or-later
"""The push record of a branch (RDR-252, bead nexus-igea9): one tuple per push on ``board/ci/<base>--push``.

``nx ci record-push`` writes it for a push script (``scripts/git-push-develop.sh`` is the first caller); *base* is the
board topic's base (``nexus-develop``), an argument and never fixed here. The owner stamper links the body's ``base`` (the
branch tip the push replaced) to the tip to order the branch's tips and maps the record's sender-session stamp to the
pushing identity; it never reads ``from``, a label (so the caller writes the identity id there, or ``unknown``).

The write is the stamped one (the client sends its session header and identity token, the engine stamps the sender
session). The status line says whether it was: ``PUSH_RECORD_OK ... stamped``, or ``PUSH_RECORD_UNSTAMPED`` when the write
went out with nothing to stamp it with (no session token and no identity token) or after the client had to resend it without
a session token the engine rejected; an unstamped record still links ``base`` to the tip in the chain, but names no pusher.
When the write fails, one minimal record (``base``, the tip, ``from``; no T1 session header; the same nonce, so a write that
did land is not doubled) is tried so the chain keeps its link. Only the T1 session header is dropped: an RDR-253 identity token
still rides the write, so a process that holds one gets the record stamped by it, and one that does not writes it unstamped;
the status line says which was written, and which of the two it is. A push that
overrode the develop-red hold carries the reason in the body as ``red_fix``, on either record, so the board shows every
escape, and so does each push-time check it overrode (``skips``, a name to reason map: ``--skip CHECK=REASON``, nexus-igea9).
The whole body stays under the template's cap (:data:`BODY_MAX_BYTES`, ``max_body_bytes`` in ``board-ci.yaml``), so
the reason is cut by serialized UTF-8 bytes, never by characters, and control characters and newlines in it become spaces.
Nothing here raises: the caller's push has already landed.
"""
from __future__ import annotations

import json
import re
from collections.abc import Mapping
from typing import Any

#: The ``board/ci/<topic>`` template's ``max_body_bytes`` (service/src/main/resources/tuples/templates/board-ci.yaml): the
#: whole serialized body, UTF-8, must fit.
BODY_MAX_BYTES = 1024
#: The most characters of a ``red_fix`` reason the body carries before the byte cut.
RED_FIX_MAX = 500
#: The most characters of one ``skips`` reason, and the bytes all the skips may take together before the ``red_fix`` reason
#: is fitted: the skips are cut first (never a check name), so a push that overrode many checks still leaves room for both.
SKIP_MAX = 200
SKIPS_BUDGET_BYTES = 400
#: The most characters of a proof's ``host`` the body carries.
HOST_MAX = 40

_CONTROL_RUNS = re.compile(r"[\s\x00-\x1f\x7f-\x9f  ]+")


def clean_reason(reason: str | None) -> str:
    """*reason* on one line: control characters, newlines and runs of whitespace become one space, the ends are trimmed, and
    anything that is not valid UTF-8 (a lone surrogate from an undecodable argument) is replaced. "" for no reason."""
    if not reason:
        return ""
    text = reason.encode("utf-8", "replace").decode("utf-8")
    return _CONTROL_RUNS.sub(" ", text).strip()


def _dumps(fields: dict[str, Any]) -> str:
    # ensure_ascii off: the template's cap counts bytes, and an escape (\uXXXX) costs six where UTF-8 costs two to four.
    return json.dumps(fields, ensure_ascii=False)


def _fits(text: str) -> bool:
    return len(text.encode("utf-8")) <= BODY_MAX_BYTES


def _skips_for(fields: dict[str, Any], skips: Mapping[str, str] | None) -> dict[str, Any]:
    """*fields* with the ``skips`` map added: each reason cleaned to one line and cut to the longest cap in a falling ladder
    with which the skips alone stay under :data:`SKIPS_BUDGET_BYTES`; a check name is kept at any cap."""
    cleaned = {name: clean_reason(reason) for name, reason in (skips or {}).items()}
    if not cleaned:
        return fields
    for cap in (SKIP_MAX, 100, 50, 25, 10, 0):
        shown = {name: text[:cap].rstrip() for name, text in cleaned.items()}
        if len(_dumps(shown).encode("utf-8")) <= SKIPS_BUDGET_BYTES or cap == 0:
            return {**fields, "skips": shown}
    return fields  # unreachable: the ladder ends at 0


def _proofs_for(fields: dict[str, Any], proofs: Mapping[str, Mapping[str, Any]] | None) -> dict[str, Any]:
    """*fields* with the ``proofs`` map added: per heavy leg, the tree sha prefix, the start (UTC ISO), the duration in seconds
    and the host of the run the push relied on. The host is cleaned and cut; the rest is fixed-width."""
    if not proofs:
        return fields
    shown = {leg: {"tree": str(info.get("tree", ""))[:12], "at": str(info.get("at", ""))[:20], "s": int(info.get("s", 0)),
                   "host": clean_reason(str(info.get("host", "")))[:HOST_MAX]} for leg, info in proofs.items()}
    return {**fields, "proofs": shown}


def body(from_sha: str, count: int | None, red_fix: str | None, skips: Mapping[str, str] | None = None,
         proofs: Mapping[str, Mapping[str, Any]] | None = None) -> str:
    """The record body as JSON: ``base`` (the tip the push replaced), ``n`` when given, ``skips`` (the push-time checks the
    push overrode, name to reason) and ``proofs`` (the heavy-leg runs it relied on) when any, ``red_fix`` when given and not empty after cleaning. The skips are fitted first
    (:func:`_skips_for`), then the reason is cut to the longest prefix with which the whole body fits :data:`BODY_MAX_BYTES`."""
    fields: dict[str, Any] = {"base": from_sha}
    if count is not None:
        fields["n"] = count
    fields = _skips_for(_proofs_for(fields, proofs), skips)
    reason = clean_reason(red_fix)[:RED_FIX_MAX]
    if not reason:
        return _dumps(fields)
    low, high = 0, len(reason)            # the largest k with reason[:k] fitting; fits(0) is the body with no reason at all
    while low < high:
        mid = (low + high + 1) // 2
        if _fits(_dumps({**fields, "red_fix": reason[:mid]})):
            low = mid
        else:
            high = mid - 1
    cut = reason[:low].rstrip()
    return _dumps({**fields, "red_fix": cut}) if cut else _dumps(fields)


def _why(exc: BaseException) -> str:
    text = f"{type(exc).__name__}: {exc}".strip().splitlines()
    return (text[-1] if text else type(exc).__name__)[:400]


def _hint(store: Any, name: str, default: Any) -> Any:
    """What *store* says through its optional *name* method or attribute; *default* for a store that does not say (an older
    client object), or one whose answer cannot be had."""
    found = getattr(store, name, None)
    try:
        return found() if callable(found) else (default if found is None else found)
    except Exception:  # noqa: BLE001 -- a hint about the stamp never fails the record
        return default


def _stamp_state(store: Any) -> tuple[bool, bool, int, int]:
    """What *store* says it stamps a write with, and how often it has dropped those stamps so far: ``(session header,
    identity token, headerless retries, identity-token resends without the token)``. A store that does not say is taken to
    stamp with its session header and to have dropped nothing."""
    session = _hint(store, "session_stamps", None)
    if session is None:
        session = _hint(store, "would_stamp", True)
    return (bool(session), bool(_hint(store, "identity_stamps", False)), int(_hint(store, "headerless_retries", 0)),
            int(_hint(store, "identity_dropped_resends", 0)))


def write(store: Any, base: str, from_sha: str, to_sha: str, count: int | None, label: str, red_fix: str | None,
          skips: Mapping[str, str] | None = None, proofs: Mapping[str, Mapping[str, Any]] | None = None) -> str:
    """Write the push record; the one status line: ``PUSH_RECORD_OK`` (stamped), ``PUSH_RECORD_UNSTAMPED`` (written, with no
    session stamp), ``PUSH_RECORD_FALLBACK`` (the stamped write failed, the minimal unstamped record stands) or
    ``PUSH_RECORD_FAILED`` (neither landed). Never raises."""
    from nexus.db.t2.http_tuple_store import without_session_header  # noqa: PLC0415 -- deferred: CLI startup cost

    subspace, topic = f"board/ci/{base}--push", f"{base}--push"
    dims = {"from": label, "kind": "push", "sha": to_sha}
    nonce = f"push-{to_sha}"
    session, identity, retries, id_drops = _stamp_state(store)
    try:
        store.out(subspace, {"topic": topic}, dims, body(from_sha, count, red_fix, skips, proofs), nonce=nonce)
    except Exception as first:  # noqa: BLE001 -- the push landed; a record fault is reported, never raised
        why = _why(first)
    else:
        _, _, retries_after, id_drops_after = _stamp_state(store)
        session_dropped, identity_dropped = retries_after > retries, id_drops_after > id_drops
        if (session and not session_dropped) or (identity and not identity_dropped):
            return f"PUSH_RECORD_OK {to_sha[:12]} from={label} stamped"
        if not session and not identity:
            lost = "no session token or identity token to send with it"
        else:
            rejected = [name for name, dropped in (("session token", session and session_dropped),
                                                    ("identity token", identity and identity_dropped)) if dropped]
            lost = f"the engine rejected the {' and the '.join(rejected)} and the write was resent without it"
        return (f"PUSH_RECORD_UNSTAMPED the record for {to_sha[:12]} was written with no session stamp ({lost}): the chain keeps "
                "its link but this push has no pusher")
    # The fallback drops the T1 session header only. An RDR-253 identity token still rides the write (`token_header` attaches it
    # to a send), so a process that holds one still gets the record stamped; one that does not, or whose token is dropped on a
    # 401 resend, writes an unstamped record. The line says which.
    _, _, _, drops_before = _stamp_state(store)
    try:
        with without_session_header():
            store.out(subspace, {"topic": topic}, dims, body(from_sha, None, red_fix, skips, proofs), nonce=nonce)
    except Exception as second:  # noqa: BLE001
        return f"PUSH_RECORD_FAILED {why}; the minimal unstamped retry failed too ({_why(second)}) (the push stands, no record was written)"
    _, _, _, drops_after = _stamp_state(store)
    head = (f"PUSH_RECORD_FALLBACK the stamped write failed ({why}); a minimal record (base, tip, from={label}) was written "
            "without the T1 session header")
    if identity and drops_after == drops_before:
        return f"{head} and is still stamped by the identity token this process holds: the chain keeps its link and the push names its pusher"
    return f"{head} and no identity token stamps it, so it is unstamped and this push has no pusher: the chain keeps its link"
