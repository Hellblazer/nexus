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

Cost note: this costs one owner-agnostic ``/list?file_path=`` per MINT, so it
belongs on per-document write paths and NOT inside a batched
``register_many`` loop, where it would turn one round trip into N+1.
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


def announce_cross_owner_mint(
    reader: Any,
    file_path: str,
    *,
    owner: Any,
    context: str,
) -> None:
    """Log that *file_path* is already catalogued elsewhere, before minting.

    Call immediately BEFORE the ``register`` that mints a new document, on a
    path where the owner-scoped lookup returned ``None``.

    Best-effort by construction: a catalog that cannot answer must never turn
    a successful index into a failed one, so every error is swallowed to a
    debug line. That is the same posture the surrounding write paths already
    take, and the reason this reports rather than guards — a check that can
    fail open must not be the thing a correctness argument rests on.

    Args:
        reader: a catalog reader exposing ``find_all_by_file_path``.
        file_path: the path about to be registered, exactly as it will be stored.
        owner: the owner the new document will be registered under.
        context: short name of the calling write path, so the log line says
            which indexer minted the row.
    """
    if not file_path:
        return
    try:
        finder = getattr(reader, "find_all_by_file_path", None)
        if finder is None:
            return
        existing = finder(file_path)
        if not existing:
            return
        with _mints_over_existing_lock:
            _MINTS_OVER_EXISTING.append({
                "file_path": file_path,
                "owner": str(owner),
                "context": context,
                "existing": [str(e.tumbler) for e in existing],
            })
        _log.warning(
            "catalog_mint_over_existing_file_path",
            file_path=file_path,
            owner=str(owner),
            context=context,
            existing=len(existing),
            existing_tumblers=[str(e.tumbler) for e in existing],
            detail="this path is already catalogued under another owner; "
                   "registering an ADDITIONAL document for it. Several "
                   "documents per path is allowed — this line exists so it "
                   "is never a surprise.",
        )
    except Exception:  # noqa: BLE001 — reporting must never fail a write
        _log.debug("catalog_mint_announce_failed", file_path=file_path, exc_info=True)
