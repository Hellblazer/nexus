# SPDX-License-Identifier: AGPL-3.0-or-later
"""RDR-193 Step 6 (nexus-u5m5g.7): a curator document resolved or registered by ONE ``resolve_or_register`` request.

``doc_indexer._register_or_lookup_doc_id`` (the pre-flight of ``nx index md``/``rdr``/``pdf``) and
``doc_indexer._catalog_markdown_hook`` (the post-index hook) used to probe the other owners, register, and reconcile in
three round trips. A document with an identity of its own (a source URI, or an absolute path one is derived from) is
now sent to the engine once. The engine looks the source URI up (a live row under another owner, a repository's row of
this very file included, comes back as ``foreign_owner`` and is neither adopted nor written; one under this owner is
returned with its empty title and zero year filled), and otherwise registers it, born flagged when *sensitive* and
carrying the title, year and source_mtime. The ephemeral-worktree guard stays on the client, in the callers; so do the
legs that resolve a document the older way (a legacy relative path, the repo owner's row), which are their lookups.

The module holds the two call sites' shared request and what follows it, so ``doc_indexer`` (over its size cap) does
not grow for it. Each function returns what its caller returned from the inline branch it replaces.
"""
from __future__ import annotations

from pathlib import Path
from typing import Any, Callable

import structlog

_log = structlog.get_logger(__name__)


def ephemeral_guard(reader: Any, owner: Any, fp: str) -> str | None:
    """The ephemeral-path guard of a new registration: the path to register under, or ``None`` when it is refused.

    nexus-u8n4r: refuse a registration when the identity about to be STORED (*fp*: absolute when no ``base_path`` was
    threaded, as in the standalone ``nx index md``/``nx index rdr`` pre-flight) sits under an agent worktree or system
    temp dir, UNLESS this owner's own repo_root is itself rooted there (throwaway owners, the pytest tmp-dir suite).
    See ``nexus.repo_identity.should_skip_ephemeral_registration`` for the rationale; curator owners normally carry an
    empty repo_root, so this is a documented residual, not a gap this guard closes on its own.

    nexus-kkumv: when the owner's own root is NOT itself worktree/tempdir-rooted, a worktree-marker *fp* is first
    rewritten to its primary-repo identity, but only when that mirror exists on disk (indexer.py's worktree-unique-file
    precedent). A file that only exists inside the worktree still falls through to the refusal, unchanged. A refusal
    is logged and recorded for the run's summary.
    """
    from nexus.repo_identity import (  # noqa: PLC0415 — circular-dep avoidance (nexus.repo_identity)
        canonicalize_worktree_path,
        is_worktree_or_tempdir_path,
        owner_repo_root_best_effort,
        should_skip_ephemeral_registration,
    )

    owner_repo_root = owner_repo_root_best_effort(reader, owner)
    if not is_worktree_or_tempdir_path(owner_repo_root):
        canonical = canonicalize_worktree_path(fp)
        if canonical != fp and Path(canonical).is_file():
            fp = canonical
    if should_skip_ephemeral_registration(fp, owner_repo_root):
        _log.warning("ephemeral_path_registration_skipped", path=fp, owner=str(owner), reason="worktree_or_tempdir")
        from nexus.mcp_infra import _record_ephemeral_registration_skip  # noqa: PLC0415 — circular-dep avoidance (nexus.mcp_infra)

        _record_ephemeral_registration_skip(fp, str(owner), reason="worktree_or_tempdir")
        return None
    return fp


def resolve_curator_document(
    reader: Any,
    writer: Any,
    *,
    owner: Any,
    owner_name: str,
    fp: str,
    source_uri: str,
    title: str,
    year: int | None,
    source_mtime: float,
    content_type: str,
    physical_collection: str,
    sensitive: bool,
    stamp: dict[str, Any],
    author: str,
    corpus: str,
    flag: Callable[[Any], None],
) -> tuple[str, bool]:
    """The pre-flight's branch: returns ``(tumbler, created)``.

    nexus-yzij1 / nexus-r1tnx: the cross-owner conflict (the same path catalogued under another owner) is read BEFORE
    the request, which would otherwise see the row it mints, and announced AFTER it, once its own ``created`` signal
    says whether it minted anything. A minted row gets its stamp, author and corpus in one update (advisory: the row is
    minted and resolved, so a failed write is logged and the run goes on); a resolved one gets the nexus-2t63u physical
    collection reconciliation and the sensitive flag (the engine raises it on a match by source URI only; this one may
    be a file-path match), through *flag*.
    """
    from nexus.catalog.path_ambiguity import (  # noqa: PLC0415 — circular-dep avoidance (nexus.catalog)
        announce_cross_owner_mint,
        announce_cross_owner_resolve,
        find_cross_owner_conflict,
        reconcile_stale_physical_collection,
    )

    conflict = find_cross_owner_conflict(reader, fp)
    outcome = writer.resolve_or_register(
        owner_name, source_uri=source_uri, file_path=fp, title=title, year=year or None, source_mtime=source_mtime,
        content_type=content_type, physical_collection=physical_collection, sensitive=sensitive,
    )
    tumbler, created = str(outcome.tumbler), outcome.created
    announce_cross_owner_mint(conflict, file_path=fp, owner=owner, context="register_or_lookup_doc_id", created=created)
    if not created:
        announce_cross_owner_resolve(
            conflict, file_path=fp, owner=owner, context="register_or_lookup_doc_id", created=created,
        )
    if outcome.foreign_owner:
        _log.warning(
            "doc_register_resolved_onto_foreign_owner_row",
            tumbler=tumbler, file_path=fp, owner=str(owner), context="register_or_lookup_doc_id",
        )
    if created:
        extra: dict[str, Any] = {}
        if stamp:
            extra["meta"] = stamp
        if author:
            extra["author"] = author
        if corpus:
            extra["corpus"] = corpus
        if extra:
            try:
                writer.update(tumbler, **extra)
            except Exception:  # noqa: BLE001 — boundary catch: advisory stamp write, must never discard a minted tumbler
                _log.warning("doc_register_stamp_write_failed", tumbler=tumbler, file_path=fp, exc_info=True)
    else:
        # resolved onto an existing row instead of minting one: the nexus-2t63u reconciliation of the branch that
        # finds a row by its path
        reconcile_stale_physical_collection(
            reader, writer, tumbler=tumbler, target_collection=physical_collection, file_path=fp, owner=owner,
        )
        flag(tumbler)
    return tumbler, created


def resolve_markdown_document(
    reader: Any,
    writer: Any,
    *,
    owner: Any,
    owner_name: str,
    fp: str,
    source_uri: str,
    title: str,
    year: int | None,
    source_mtime: float,
    content_type: str,
    collection_name: str,
    author: str,
    chunk_count: int,
    doc_meta: dict[str, Any],
    meta_of_row: Callable[[Any], dict[str, Any]],
    fill_only_keys: Any,
) -> str:
    """The markdown hook's branch: resolves or registers the document and returns its tumbler.

    The route registers neither the meta, the author nor the chunk count, so they follow in one update. A resolved row
    keeps its own meta (no ``meta`` merge on a resolve): the record rides an update, rebuilt from the row by
    *meta_of_row* (or from this call's *doc_meta* less the fill-only keys when the row cannot be read), and the author
    goes in only where the resolved row's is empty (nexus-6pfgb.7).
    """
    from nexus.catalog.path_ambiguity import (  # noqa: PLC0415 — circular-dep avoidance (nexus.catalog)
        announce_cross_owner_mint,
        announce_cross_owner_resolve,
        find_cross_owner_conflict,
        reconcile_stale_physical_collection,
    )

    conflict = find_cross_owner_conflict(reader, fp)
    outcome = writer.resolve_or_register(
        owner_name, source_uri=source_uri, file_path=fp, title=title, year=year or None, source_mtime=source_mtime,
        content_type=content_type, physical_collection=collection_name,
    )
    tumbler = str(outcome.tumbler)
    announce_cross_owner_mint(conflict, file_path=fp, owner=owner, context="catalog_markdown_hook", created=outcome.created)
    if not outcome.created:
        announce_cross_owner_resolve(
            conflict, file_path=fp, owner=owner, context="catalog_markdown_hook", created=outcome.created,
        )
    if outcome.foreign_owner:
        _log.warning(
            "doc_register_resolved_onto_foreign_owner_row",
            tumbler=tumbler, file_path=fp, owner=str(owner), context="catalog_markdown_hook",
        )
    if outcome.created:
        created_update: dict[str, Any] = {"meta": doc_meta, "chunk_count": chunk_count}
        if author:
            created_update["author"] = author
        writer.update(tumbler, **created_update)
        return tumbler
    # the nexus-2t63u reconciliation of the branch that finds a row by its path, for a row the request resolved onto
    reconcile_stale_physical_collection(
        reader, writer, tumbler=tumbler, target_collection=collection_name, file_path=fp, owner=owner,
    )
    try:
        resolved = reader.resolve(tumbler)
    except Exception:  # noqa: BLE001 — advisory read: without it the author is not filled and the stamp takes this call's arguments
        resolved = None
    resolved_update: dict[str, Any] = {
        "meta": meta_of_row(resolved) if resolved is not None
        else {k: v for k, v in doc_meta.items() if k not in fill_only_keys}
    }
    if author and resolved is not None and not (getattr(resolved, "author", "") or "").strip():
        resolved_update["author"] = author
    writer.update(tumbler, **resolved_update)
    return tumbler
