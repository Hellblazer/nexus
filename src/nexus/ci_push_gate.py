# SPDX-License-Identifier: AGPL-3.0-or-later
"""The hold-your-pushes gate of a develop red (RDR-252, bead nexus-igea9): who may push to develop while a red is open.

``scripts/git-push-develop.sh`` asks ``nx ci push-gate`` before it pushes. While a red is open on
``board/ci/<topic>--owner``, the red's owner may push, and so may the pusher of one of its suspects (the fix often comes
from there); anyone else is refused, unless the push names a reason (``--red-fix``). The red is the one
:func:`nexus.ci_reds.split` reports open, as ``nx ci ack`` reads it. A suspect's pusher is the identity its push record's
sender-session stamp maps to, through the same session index the engine and ``nx group ack-times`` read: the ``from`` of a
push record is a label and is never read (an unstamped record names nobody). A caller with no identity is neither the owner nor a suspect's pusher, so it is held like any other (Sam, 2026-11-22: only a
failed read of the red is warn-and-allow). A check that cannot be made because a read or a lookup failed is *unchecked* and
never a refusal: the gate holds a push for a reason it can name, not for a fault of its own.
"""
from __future__ import annotations

from dataclasses import dataclass
from typing import Any

from nexus import ci_reds

#: Rows of a subspace one check reads, newest first: far more than one red writes.
READ_ROWS = 300


@dataclass(frozen=True)
class Verdict:
    """The gate's answer. ``refuse`` is a hold; ``unchecked`` is a check that could not be made (allowed, said so)."""

    refuse: bool = False
    unchecked: bool = False
    detail: str = ""
    owner: str | None = None
    owner_alias: str | None = None
    sha: str | None = None


def default_resolver() -> ci_reds.AckResolver:
    """The resolver over this host's identity service (a seam for tests)."""
    from nexus.tuple_directory import identity_record_lookup, identity_session_lookup  # noqa: PLC0415 -- deferred: CLI startup cost

    return ci_reds.AckResolver(identity_session_lookup(), identity_record_lookup())


def _open_red(owner_rows: list[Any]) -> ci_reds.Episode | None:
    reds, _ = ci_reds.split(list(owner_rows))
    return reds[-1] if reds and reds[-1].cleared is None else None


def _suspects(red: ci_reds.Episode) -> set[str]:
    """Every suspect sha the red's owner rows list (each row cuts its list to ten, earliest first)."""
    found: set[str] = set()
    for _at, _row, body in red.rows:
        listed = body.get("suspects")
        if isinstance(listed, list):
            found.update(str(s).lower() for s in listed)
    return found


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
    for row in push_rows:
        sha = str((row.dims or {}).get("sha") or "").lower()
        if identity and sha in suspects and resolver.poster(row) == identity:
            return Verdict(detail=f"you pushed suspect {sha[:7]} of the red at {tip[:7]}", owner=owner, sha=tip)
    if resolver.unresolved:
        return Verdict(unchecked=True, detail=f"{resolver.unresolved} suspect push record(s) could not be attributed",
                       owner=owner, sha=tip)
    return Verdict(refuse=True, detail="open red", owner=owner, owner_alias=latest.get("owner_alias") or None, sha=tip)


def check(store: Any, base: str, identity: str, resolver: ci_reds.AckResolver | None = None) -> Verdict:
    """Read ``board/ci/<topic>--owner`` and ``--push`` from *store* and :func:`evaluate`. Raises what the read raises."""
    owner_sub, push_sub = f"board/ci/{base}--owner", f"board/ci/{base}--push"
    owner_rows = store.rd(owner_sub, {"topic": f"{base}--owner"}, n=READ_ROWS, order="desc")
    if _open_red(list(owner_rows)) is None:
        return Verdict(detail="no open red")
    push_rows = store.rd(push_sub, {"topic": f"{base}--push"}, n=READ_ROWS, order="desc")
    return evaluate(list(owner_rows), list(push_rows), identity, resolver or default_resolver())
