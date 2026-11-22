# SPDX-License-Identifier: AGPL-3.0-or-later
"""The push record of a branch (RDR-252, bead nexus-igea9): one tuple per push on ``board/ci/<base>--push``.

``nx ci record-push`` writes it for a push script (``scripts/git-push-develop.sh`` is the first caller); *base* is the
board topic's base (``nexus-develop``), an argument and never fixed here. The owner stamper links the body's ``base`` (the
branch tip the push replaced) to the tip to order the branch's tips and maps the record's sender-session stamp to the
pushing identity; it never reads ``from``, a label (so the caller writes the identity id there, or ``unknown``).

The write is the stamped one (the client sends the session header, the engine stamps the sender session). When it fails,
one minimal record (``base``, the tip, ``from``; no header, so no stamp; the same nonce, so a write that did land is not
doubled) is tried so the chain keeps its link, and the status line says which was written. A push that overrode the
develop-red hold carries the reason in the body as ``red_fix`` (cut to :data:`RED_FIX_MAX`), on either record, so the
board shows every escape. Nothing here raises: the caller's push has already landed.
"""
from __future__ import annotations

import json
from typing import Any

#: The longest ``red_fix`` reason the body carries, in characters; the template's body cap is 4096 bytes.
RED_FIX_MAX = 500


def body(from_sha: str, count: int | None, red_fix: str | None) -> str:
    """The record body as JSON: ``base`` (the tip the push replaced), ``n`` when given, ``red_fix`` when given."""
    fields: dict[str, Any] = {"base": from_sha}
    if count is not None:
        fields["n"] = count
    if red_fix:
        fields["red_fix"] = red_fix[:RED_FIX_MAX]
    return json.dumps(fields)


def _why(exc: BaseException) -> str:
    text = f"{type(exc).__name__}: {exc}".strip().splitlines()
    return (text[-1] if text else type(exc).__name__)[:400]


def write(store: Any, base: str, from_sha: str, to_sha: str, count: int | None, label: str, red_fix: str | None) -> str:
    """Write the push record; the one status line: ``PUSH_RECORD_OK``, ``PUSH_RECORD_FALLBACK`` (the stamped write failed,
    the minimal unstamped record stands) or ``PUSH_RECORD_FAILED`` (neither landed). Never raises."""
    from nexus.db.t2.http_tuple_store import without_session_header  # noqa: PLC0415 -- deferred: CLI startup cost

    subspace, topic = f"board/ci/{base}--push", f"{base}--push"
    dims = {"from": label, "kind": "push", "sha": to_sha}
    nonce = f"push-{to_sha}"
    try:
        store.out(subspace, {"topic": topic}, dims, body(from_sha, count, red_fix), nonce=nonce)
        return f"PUSH_RECORD_OK {to_sha[:12]} from={label}"
    except Exception as first:  # noqa: BLE001 -- the push landed; a record fault is reported, never raised
        why = _why(first)
    try:
        with without_session_header():
            store.out(subspace, {"topic": topic}, dims, body(from_sha, None, red_fix), nonce=nonce)
    except Exception as second:  # noqa: BLE001
        return f"PUSH_RECORD_FAILED {why}; the minimal unstamped retry failed too ({_why(second)}) (the push stands, no record was written)"
    return (f"PUSH_RECORD_FALLBACK the stamped write failed ({why}); an unstamped record with base, tip and from={label} was written, "
            "so the chain keeps its link but this push has no pusher")
