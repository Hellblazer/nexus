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
When the write fails, one minimal record (``base``, the tip, ``from``; no session header; the same nonce, so a write that
did land is not doubled) is tried so the chain keeps its link, and the status line says which was written. A push that
overrode the develop-red hold carries the reason in the body as ``red_fix``, on either record, so the board shows every
escape. The whole body stays under the template's cap (:data:`BODY_MAX_BYTES`, ``max_body_bytes`` in ``board-ci.yaml``), so
the reason is cut by serialized UTF-8 bytes, never by characters, and control characters and newlines in it become spaces.
Nothing here raises: the caller's push has already landed.
"""
from __future__ import annotations

import json
import re
from typing import Any

#: The ``board/ci/<topic>`` template's ``max_body_bytes`` (service/src/main/resources/tuples/templates/board-ci.yaml): the
#: whole serialized body, UTF-8, must fit.
BODY_MAX_BYTES = 1024
#: The most characters of a ``red_fix`` reason the body carries before the byte cut.
RED_FIX_MAX = 500

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


def body(from_sha: str, count: int | None, red_fix: str | None) -> str:
    """The record body as JSON: ``base`` (the tip the push replaced), ``n`` when given, ``red_fix`` when given and not empty
    after cleaning. The reason is cut to the longest prefix with which the whole body fits :data:`BODY_MAX_BYTES`."""
    fields: dict[str, Any] = {"base": from_sha}
    if count is not None:
        fields["n"] = count
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


def write(store: Any, base: str, from_sha: str, to_sha: str, count: int | None, label: str, red_fix: str | None) -> str:
    """Write the push record; the one status line: ``PUSH_RECORD_OK`` (stamped), ``PUSH_RECORD_UNSTAMPED`` (written, with no
    session stamp), ``PUSH_RECORD_FALLBACK`` (the stamped write failed, the minimal unstamped record stands) or
    ``PUSH_RECORD_FAILED`` (neither landed). Never raises."""
    from nexus.db.t2.http_tuple_store import without_session_header  # noqa: PLC0415 -- deferred: CLI startup cost

    subspace, topic = f"board/ci/{base}--push", f"{base}--push"
    dims = {"from": label, "kind": "push", "sha": to_sha}
    nonce = f"push-{to_sha}"
    would_stamp = bool(_hint(store, "would_stamp", True))
    retries_before = int(_hint(store, "headerless_retries", 0))
    try:
        store.out(subspace, {"topic": topic}, dims, body(from_sha, count, red_fix), nonce=nonce)
    except Exception as first:  # noqa: BLE001 -- the push landed; a record fault is reported, never raised
        why = _why(first)
    else:
        resent = int(_hint(store, "headerless_retries", 0)) > retries_before
        if not would_stamp:
            lost = "no session token or identity token to send with it"
        elif resent and not bool(_hint(store, "identity_stamps", False)):
            lost = "the engine rejected the session token and the write was resent without it"
        else:
            return f"PUSH_RECORD_OK {to_sha[:12]} from={label} stamped"
        return (f"PUSH_RECORD_UNSTAMPED the record for {to_sha[:12]} was written with no session stamp ({lost}): the chain keeps "
                "its link but this push has no pusher")
    try:
        with without_session_header():
            store.out(subspace, {"topic": topic}, dims, body(from_sha, None, red_fix), nonce=nonce)
    except Exception as second:  # noqa: BLE001
        return f"PUSH_RECORD_FAILED {why}; the minimal unstamped retry failed too ({_why(second)}) (the push stands, no record was written)"
    return (f"PUSH_RECORD_FALLBACK the stamped write failed ({why}); an unstamped record with base, tip and from={label} was written, "
            "so the chain keeps its link but this push has no pusher")
