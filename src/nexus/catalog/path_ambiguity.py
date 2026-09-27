# SPDX-License-Identifier: AGPL-3.0-or-later
"""Say out loud when a writer mints a second catalog document for one path.

nexus-yzij1. Several catalog documents per ``file_path`` is a NORMAL STEADY
STATE, not damage: nothing in the schema forbids it — the only uniqueness is
the PARTIAL index on ``(tenant_id, source_uri)`` — and it arises whenever one
file is catalogued under two owners. The project's ruling on it is to
accommodate it and make it rational, not to force uniqueness.

What was irrational was the silence. Every registering path first asks
``by_file_path(owner, path)``, which is owner-scoped and structurally cannot
see a row owned by anyone else; on the miss it registers a new document. Both
halves are defensible — the writer usually has a genuinely different identity
in hand — but the result was that a catalog quietly grew a second document for
a file and no operator learned it had happened until a census counted 19 of
them in one run (nexus-z0lu4).

So this is deliberately NOT a guard. It does not refuse, deduplicate, or
change what gets written. It reports, at the moment the second row is minted,
that the path was already catalogued and under which tumblers — the same
contract ``HttpCatalogClient.find_by_file_path`` now honours when it chooses
among several, applied at the write instead of the read.

Cost note: this costs one owner-agnostic ``/list?file_path=`` per attempted
MINT (the owner-scoped lookup missed), so it belongs on per-document write
paths and NOT inside a batched ``register_many`` loop, where it would turn
one round trip into N+1.

nexus-r1tnx: the query and the announcement are deliberately TWO functions,
not one. A single call made *before* ``register()`` (the original shape) had
no way to know whether that ``register()`` would actually mint a second
document or resolve to the pre-existing one via its own idempotency leg
(matching ``source_uri``/``file_path`` across owners) — so it warned
"registering an ADDITIONAL document" even on runs where ``register()``
handed back the SAME existing tumbler and nothing new was written. It fired
twice this way on 2026-09-26 re-indexing a PDF that resolved to its existing
catalog row.

The fix threads the ``created`` signal ``register(with_created=True)``
already exposes (nexus-vfef0) through the split:

* :func:`find_cross_owner_conflict` runs BEFORE ``register()`` — it must,
  because a query run AFTER would see the just-minted row too and misreport
  a brand-new, uncontested path as "conflicting with itself".
* :func:`announce_cross_owner_mint` runs AFTER ``register()``, once the
  caller knows whether THIS call actually minted anything, and stays silent
  when it did not.
"""

from __future__ import annotations

import threading
from typing import Any

import structlog

_log = structlog.get_logger(__name__)

# A per-process, per-run collector the CLI summary layer reads and resets,
# exactly like ``mcp_infra``'s ``_EPHEMERAL_REGISTRATION_SKIPS`` /
# ``_SUPERSEDED_SWEEP_SKIPS``. A structlog line alone was the first shape of
# this, and this codebase has already paid for that once: nexus-39upx's own
# comment on the superseded-vector sweep says it "previously reported ONLY
# via structlog — invisible without log capture wired up, the exact 'every
# catalog-level check reports clean' shape this bead exists to close". A
# duplication nobody is told about at the end of the run is the same silence
# this module exists to break, moved one layer out.
_mints_over_existing_lock = threading.Lock()
_MINTS_OVER_EXISTING: list[dict] = []


def get_mints_over_existing_path() -> list[dict]:
    """Every announced mint-over-existing-path this process has recorded.

    Each entry: ``{"file_path": str, "owner": str, "context": str,
    "existing": list[str]}`` — ``existing`` holds the tumblers that already
    carried the path, so a summary can name them without re-querying.
    """
    with _mints_over_existing_lock:
        return list(_MINTS_OVER_EXISTING)


def reset_mints_over_existing_path() -> None:
    """Zero the collector so a run's summary reflects only that run."""
    with _mints_over_existing_lock:
        _MINTS_OVER_EXISTING.clear()


def find_cross_owner_conflict(reader: Any, file_path: str) -> list[str] | None:
    """Tumblers of every document already catalogued at *file_path*, under
    ANY owner — or ``None`` if there are none, the reader can't answer, or
    the query itself fails.

    Call this BEFORE the ``register()`` that might mint a second document
    for a path an owner-scoped lookup missed. Querying afterward would also
    see the just-minted row and misreport an uncontested path as a
    conflict with itself.

    Best-effort by construction: a catalog that cannot answer must never
    turn a successful index into a failed one, so every error is swallowed
    to a debug line.
    """
    if not file_path:
        return None
    try:
        finder = getattr(reader, "find_all_by_file_path", None)
        if finder is None:
            return None
        existing = finder(file_path)
        if not existing:
            return None
        return [str(e.tumbler) for e in existing]
    except Exception:  # noqa: BLE001 — reporting must never fail a write
        _log.debug("catalog_mint_announce_failed", file_path=file_path, exc_info=True)
        return None


def announce_cross_owner_mint(
    conflict: list[str] | None,
    *,
    file_path: str,
    owner: Any,
    context: str,
    created: bool,
) -> None:
    """Log + record that *file_path* was minted as an ADDITIONAL document.

    Call this AFTER the ``register()`` whose ``with_created=True`` answer
    this reports.

    Silent unless BOTH hold: *conflict* names at least one document that
    already carried this path (the pre-register answer from
    :func:`find_cross_owner_conflict`), AND *created* is ``True`` — a
    register call that resolved to a pre-existing row instead of minting a
    new one (``created=False``) minted nothing, so there is nothing to
    announce; that mismatch (a real conflict list alongside
    ``created=False``) is exactly the nexus-r1tnx false positive this split
    exists to prevent.
    """
    if not conflict or not created:
        return
    with _mints_over_existing_lock:
        _MINTS_OVER_EXISTING.append({
            "file_path": file_path,
            "owner": str(owner),
            "context": context,
            "existing": list(conflict),
        })
    _log.warning(
        "catalog_mint_over_existing_file_path",
        file_path=file_path,
        owner=str(owner),
        context=context,
        existing=len(conflict),
        existing_tumblers=list(conflict),
        detail="this path is already catalogued under another owner; "
               "registered an ADDITIONAL document for it. Several "
               "documents per path is allowed — this line exists so it "
               "is never a surprise.",
    )
