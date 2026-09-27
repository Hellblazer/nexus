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

nexus-r1tnx round 2 (substantive-critic finding): silence on ``created=False``
traded one wrong claim for a different gap. The engine's idempotency leg
(``CatalogRepository.registerDocumentWithOutcome``) returns a cross-owner
resolve's row VERBATIM — it never touches ``physical_collection`` — while the
SAME-owner resolve branches in ``doc_indexer.py``/``pipeline_stages.py``
compare the resolved row's ``physical_collection`` against the run's target
and repoint it on mismatch (nexus-2t63u: a stale value there makes the
engine's manifest write stamp every row from the WRONG collection, tripping
the RUNFENCE verify). The cross-owner mint-fallback branches had no such
check, so the exact scenario the original bug report came from (a resolve
onto another owner's document) got neither a correctly-worded signal nor a
collection reconcile. Two more pieces close that:

* :func:`announce_cross_owner_resolve` — the ``created=False`` counterpart
  to :func:`announce_cross_owner_mint`: logs that this path resolved onto an
  existing document under another owner instead of minting one, so a
  cross-owner path collision is never TOTALLY silent, whichever way
  ``register()`` went.
* :func:`reconcile_stale_physical_collection` — the same compare-and-repoint
  the same-owner branches already do, extracted so the mint-fallback paths
  can call it too instead of copying it again at each one. Runs whenever
  ``created`` is ``False`` (register() resolved onto an existing row), but
  ONLY writes when the resolved document belongs to the SAME owner as this
  call (round 4 below) — a cross-owner resolve is reported, never written.

nexus-r1tnx round 4 (fix-check CRITICAL): round 2's ``reconcile_stale_
physical_collection`` repointed a resolved row's ``physical_collection``
unconditionally, without checking whose document it actually was. The
engine's ``source_uri`` idempotency leg that produces a cross-owner
``created=False`` resolve is NOT owner-scoped (unlike its ``file_path``
leg), so the resolved row can belong to a DIFFERENT owner — repointing it
reassigned that owner's document's storage based on an unrelated caller's
own target, reproducing the nexus-2t63u RUNFENCE class against the WRONG
owner. The helper now resolves the row and compares its own tumbler
against the caller's *owner* before ever writing; see its docstring for
the full argument, including why a same-owner divergence still reconciles
(matching the same-owner branches) while a cross-owner one only logs.
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


def created_from_register_result(result: Any) -> bool:
    """Unwrap a ``writer.register(..., with_created=True)`` return.

    ``HttpCatalogClient.register`` returns ``(tumbler, created)`` when
    ``with_created=True``. A test double that predates the kwarg (several
    exist across the writer-fake population) ignores it and returns a bare
    tumbler/string instead. Treat that the same way
    ``HttpCatalogClient.register`` itself treats an older engine that omits
    the wire field entirely — ``created=True`` is the historical assumption
    every caller ignoring this parameter already makes, not a guess this
    helper invents.

    Shared by every ``register()`` call site that requests the signal, so
    the isinstance-tuple unwrap is written once rather than copied at each
    one (nexus-r1tnx round 2, code-review minor finding).
    """
    if isinstance(result, tuple):
        return bool(result[1])
    return True


def tumbler_from_register_result(result: Any) -> Any:
    """Unwrap a ``writer.register(..., with_created=True)`` return to just
    the tumbler, the companion half of :func:`created_from_register_result`
    (same tuple-vs-bare-value shape, same "older/predating fake" fallback).
    """
    if isinstance(result, tuple):
        return result[0]
    return result


def announce_cross_owner_resolve(
    conflict: list[str] | None,
    *,
    file_path: str,
    owner: Any,
    context: str,
    created: bool,
) -> None:
    """Log that *file_path* resolved onto an EXISTING document under
    another owner instead of minting a new one.

    The ``created=False`` counterpart to :func:`announce_cross_owner_mint`.
    Call this AFTER the same ``register()`` — silent unless BOTH hold:
    *conflict* names at least one document already at this path (the
    pre-register answer from :func:`find_cross_owner_conflict`), AND
    ``created`` is ``False``. A conflict list alongside ``created=True`` is
    the OTHER function's case (a genuine additional mint); a conflict list
    alongside ``created=False`` means ``register()``'s own idempotency leg
    (matching ``source_uri``/``file_path`` across owners) resolved this
    call onto a document some other owner already registered — allowed,
    same as an additional mint is allowed, but worth knowing about: the
    original nexus-r1tnx bug report's exact scenario (owner 1.14 resolving
    onto 1.12.25) is precisely this branch, and prior to this function it
    got no signal at all once the false "ADDITIONAL document" claim was
    removed.
    """
    if not conflict or created:
        return
    _log.warning(
        "catalog_mint_resolved_existing_document",
        file_path=file_path,
        owner=str(owner),
        context=context,
        existing=len(conflict),
        existing_tumblers=list(conflict),
        detail="this path is already catalogued under another owner; "
               "register() resolved onto that existing document instead "
               "of minting a new one for this owner. No additional "
               "document was created.",
    )


def reconcile_stale_physical_collection(
    reader: Any,
    writer: Any,
    *,
    tumbler: Any,
    target_collection: str,
    file_path: str,
    owner: Any,
) -> bool:
    """Repoint *tumbler*'s ``physical_collection`` to *target_collection*
    if the resolved row's is stale — but ONLY when *tumbler* belongs to
    *owner*.

    nexus-r1tnx round 4 (fix-check CRITICAL): a resolve reaching a
    cross-owner mint-fallback branch (``created=False`` with a non-empty
    :func:`find_cross_owner_conflict` answer) can ONLY have happened via
    the engine's ``source_uri`` idempotency leg
    (``CatalogRepository.registerDocumentWithOutcome``), which matches
    ``(tenant, source_uri)`` GLOBALLY — no ``owner_prefix`` scoping. So
    that resolved document can belong to a DIFFERENT owner than *owner*,
    with its ``physical_collection`` set by THAT owner's own run. Silently
    repointing it to *this* caller's target would reassign another
    owner's document's storage out from under them — reproducing the
    nexus-2t63u RUNFENCE-refusal class AGAINST THE WRONG OWNER, a new blast
    radius rather than one this fix closes. It also contradicts the
    design principle the ``source_uri`` branch immediately above the mint
    fallback in ``doc_indexer._register_or_lookup_doc_id`` already states:
    a ``source_uri`` + collection divergence is "a move, not a re-index"
    and should be refused (``SourceUriCollectionMismatchError``), never
    silently reconciled. A cross-owner divergence found here is logged
    instead, at WARNING, with a distinct event, and left untouched.

    A SAME-owner resolve (*tumbler* found under *owner*'s own prefix) is
    the genuine nexus-2t63u case this helper was written for: mirrors what
    the same-owner resolve branches (``doc_indexer._register_or_lookup_doc_id``,
    ``doc_indexer._catalog_markdown_hook``, ``pipeline_stages.
    _catalog_pdf_hook``) already do on their own owner-scoped hit — that
    the SAME owner's document diverged into a different ``source_uri``/
    ``file_path`` match than plain ``by_file_path`` found (a genuinely
    ambiguous identity RESOLVING to a live row this owner already holds)
    is exactly the "not an explicit --source-uri move" case those branches
    reconcile rather than raise on, so this helper reconciles too, extracted
    here so the mint-fallback branches can reuse it instead of each
    copying the compare-and-repoint block again.

    The owner check compares the RESOLVED row's own tumbler prefix (via
    ``reader.resolve``) against *owner* — never the pre-register
    :func:`find_cross_owner_conflict` answer, which only ever named OTHER
    owners' tumblers and could never confirm same-owner-ness on its own.

    Without a repoint, the engine's ``writeManifestRows``/
    ``appendManifestChunks`` stamp every manifest row from
    ``catalog_documents.physical_collection`` at write time (read
    unconditionally), so a stale value there makes ``manifest_verify``
    join against the WRONG collection and report live, present chunks as
    missing — the nexus-2t63u RUNFENCE-refusal class, only ever safe to
    close on the resolved document's OWN owner's say-so.

    Best-effort / advisory by construction, mirroring the same-owner
    branches' own fail-open contract (nexus-ir68m): a register call that
    already resolved a live tumbler must never have that tumbler's identity
    discarded because a follow-up repoint probe or write failed — the
    caller still has a perfectly good ``doc_id`` either way.

    Returns ``True`` iff a repoint was written. ``False`` covers: the
    resolve probe failed, the row wasn't found, the row belongs to a
    DIFFERENT owner (logged, not silent), it has no ``physical_collection``
    yet (a ghost/never-indexed row — nothing to compare against; NOTE this
    is NOT "the same exemption" the cited same-owner branch
    (``_register_or_lookup_doc_id``'s own early reconcile, doc_indexer.py)
    uses — that branch repoints unconditionally on any inequality,
    including from an empty ``physical_collection``; only the OTHER two
    same-owner branches, which fold the repoint into a broader
    ``update()`` call with several other fields, incidentally skip
    LOGGING on a ghost row while still writing. This helper's ghost-skip
    is a deliberate, narrower choice, not parity with either), or it
    already matches *target_collection*.
    """
    try:
        entry = reader.resolve(tumbler)
    except Exception:  # noqa: BLE001 — advisory probe, must never fail a write
        _log.debug(
            "catalog_cross_owner_reconcile_probe_failed",
            file_path=file_path, tumbler=str(tumbler), exc_info=True,
        )
        return False
    if entry is None:
        return False
    resolved_tumbler = getattr(entry, "tumbler", None)
    if resolved_tumbler is None or not str(resolved_tumbler).startswith(f"{owner}."):
        old_collection = getattr(entry, "physical_collection", "")
        _log.warning(
            "catalog_physical_collection_reconcile_skipped_foreign_owner",
            tumbler=str(tumbler), file_path=file_path, owner=str(owner),
            resolved_tumbler=str(resolved_tumbler),
            existing_collection=old_collection,
            target_collection=target_collection,
            detail="the resolved document belongs to a DIFFERENT owner "
                   "than this run's — repointing its physical_collection "
                   "to this caller's target would reassign another "
                   "owner's storage. Left untouched; see "
                   "announce_cross_owner_resolve for the informational "
                   "signal.",
        )
        return False
    old_collection = getattr(entry, "physical_collection", "")
    if not old_collection or old_collection == target_collection:
        return False
    try:
        writer.update(tumbler, physical_collection=target_collection)
    except Exception:  # noqa: BLE001 — advisory write, must never discard an already-resolved tumbler (nexus-ir68m fail-open contract)
        _log.warning(
            "doc_physical_collection_reconcile_write_failed",
            tumbler=str(tumbler), file_path=file_path,
            old_collection=old_collection, new_collection=target_collection,
        )
        return False
    _log.warning(
        "doc_physical_collection_reconciled",
        tumbler=str(tumbler), file_path=file_path,
        old_collection=old_collection, new_collection=target_collection,
    )
    from nexus.mcp_infra import _record_physical_collection_reconciled  # noqa: PLC0415 — circular-dep avoidance (nexus.mcp_infra)
    _record_physical_collection_reconciled()
    return True
