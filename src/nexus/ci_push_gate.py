# SPDX-License-Identifier: AGPL-3.0-or-later
"""The hold-your-pushes gate of a develop red (RDR-252, bead nexus-igea9): who may push to develop while a red is open.

``scripts/git-push-develop.sh`` asks ``nx ci check-hold`` before it pushes. While a red is open on
``board/ci/<base>--owner``, the red's owner may push, and so may the pusher of one of its suspects (the fix often comes
from there); anyone else is refused, unless the push names a reason (``--red-fix``). The red is the one
:func:`nexus.ci_reds.split` reports open, as ``nx ci ack`` reads it. A suspect's pusher is the identity its push record's
sender-session stamp maps to, through the same session index the engine and ``nx group ack-times`` read: the ``from`` of a
push record is a label and is never read (an unstamped record names nobody, and a suspect with no record at all is
*unattributed*: the refusal names it and points at ``--red-fix``).

A caller that holds no identity is neither the owner nor a suspect's pusher, so it is held like any other (Sam, 2026-11-22:
only a failed read is warn-and-allow). A caller whose identity could not be READ is not that caller, and a check that
cannot be made because a read or a lookup failed is *unchecked*: allowed, with a warning that says what failed. The gate
holds a push for a reason it can name, not for a fault of its own.

Two marker lines carry an override to the push script, and this module is where they are defined: :data:`RED_FIX_APPLIED`
begins the one line printed when ``--red-fix`` overrode a refusal, :data:`RED_FIX_UNNEEDED` the one printed when the flag
was given but nothing held the push. The script matches the first as an exact line prefix, never by the free text after it.
"""
from __future__ import annotations

from dataclasses import dataclass
from typing import Any

from nexus import ci_reds
from nexus.tuple_directory import looks_like_session_id

#: Rows of a subspace one check reads, newest first: far more than one red writes.
READ_ROWS = 300
#: The marker of the line printed when ``--red-fix`` overrode a refusal; ``scripts/git-push-develop.sh`` matches it exactly.
RED_FIX_APPLIED = "PUSH_RED_FIX_APPLIED"
#: The marker of the line printed when ``--red-fix`` was given and nothing held the push.
RED_FIX_UNNEEDED = "PUSH_RED_FIX_UNNEEDED"


@dataclass(frozen=True)
class Verdict:
    """The gate's answer. ``refuse`` is a hold; ``unchecked`` is a check that could not be made (allowed, said so).
    ``unattributed`` lists the suspect shas (earliest first) whose pusher cannot be told: no push record, or one with no
    stamp the session index maps."""

    refuse: bool = False
    unchecked: bool = False
    detail: str = ""
    owner: str | None = None
    owner_alias: str | None = None
    sha: str | None = None
    unattributed: tuple[str, ...] = ()


def default_resolver() -> ci_reds.AckResolver:
    """The resolver over this host's identity service (a seam for tests)."""
    from nexus.tuple_directory import identity_record_lookup, identity_session_lookup  # noqa: PLC0415 -- deferred: CLI startup cost

    return ci_reds.AckResolver(identity_session_lookup(), identity_record_lookup())


def _open_red(owner_rows: list[Any]) -> ci_reds.Episode | None:
    reds, _ = ci_reds.split(list(owner_rows))
    return reds[-1] if reds and reds[-1].cleared is None else None


def _suspects(red: ci_reds.Episode) -> list[str]:
    """Every suspect sha the red's owner rows list, earliest first (each row cuts its list to ten)."""
    found: dict[str, None] = {}
    for _at, _row, body in red.rows:
        listed = body.get("suspects")
        if isinstance(listed, list):
            for s in listed:
                found.setdefault(str(s).lower(), None)
    return list(found)


def evaluate(owner_rows: list[Any], push_rows: list[Any], identity: str, resolver: ci_reds.AckResolver) -> Verdict:
    """The verdict for *identity* pushing now, from the rows of ``--owner`` and ``--push``. *identity* is the pusher's id, ""
    for none."""
    red = _open_red(owner_rows)
    if red is None:
        return Verdict(detail="no open red")
    _at, latest_row, latest = red.rows[-1]
    owner = latest.get("owner") or None
    tip = str((latest_row.dims or {}).get("sha") or "")
    if identity and owner and owner == identity:
        return Verdict(detail=f"you own the red at {tip[:7]}", owner=owner, sha=tip)
    suspects = _suspects(red)
    attributed: set[str] = set()
    for row in push_rows:
        sha = str((row.dims or {}).get("sha") or "").lower()
        if sha not in suspects:
            continue
        # A stamp that is no session id (a hand-posted row, an old client's) names nobody: this suspect is unattributed. That
        # is not a failed lookup, so it must not make the whole check UNCHECKED and let everyone through; only a real read or
        # lookup failure (an exception or transport error from the session index, counted in ``resolver.unresolved``) does.
        sender = getattr(row, "sender_session", None)
        who = resolver.poster(row) if sender and looks_like_session_id(str(sender)) else None
        if who is not None:
            attributed.add(sha)
        if identity and who == identity:
            return Verdict(detail=f"you pushed suspect {sha[:7]} of the red at {tip[:7]}", owner=owner, sha=tip)
    if resolver.unresolved:
        return Verdict(unchecked=True, detail=f"{resolver.unresolved} suspect push record(s) could not be attributed",
                       owner=owner, sha=tip)
    return Verdict(refuse=True, detail="open red", owner=owner, owner_alias=latest.get("owner_alias") or None, sha=tip,
                   unattributed=tuple(s for s in suspects if s not in attributed))


def check(store: Any, base: str, identity: str, resolver: ci_reds.AckResolver | None = None) -> Verdict:
    """Read ``board/ci/<base>--owner`` and ``--push`` from *store* and :func:`evaluate`. Raises what the read raises."""
    owner_sub, push_sub = f"board/ci/{base}--owner", f"board/ci/{base}--push"
    owner_rows = store.rd(owner_sub, {"topic": f"{base}--owner"}, n=READ_ROWS, order="desc")
    if _open_red(list(owner_rows)) is None:
        return Verdict(detail="no open red")
    push_rows = store.rd(push_sub, {"topic": f"{base}--push"}, n=READ_ROWS, order="desc")
    return evaluate(list(owner_rows), list(push_rows), identity, resolver or default_resolver())
