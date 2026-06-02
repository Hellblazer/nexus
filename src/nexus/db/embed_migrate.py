# SPDX-License-Identifier: AGPL-3.0-or-later
"""RDR-144 P4: safe local-embedder migration (384 -> 768).

When a user indexes a corpus under the bundled 384-dim minilm fallback and
later upgrades to bge-768 (via ``nx init``), the old 384-dim collections do
not "automatically re-index". ``doc_indexer`` re-embeds changed content into
*new* (768-token) collection names and leaves the old 384-dim collections as
orphan dead weight that ``nx search`` silently returns nothing from (CA-3).

This module makes that cleanup an explicit, gate-locked operation:

    dry-run preview  ->  double-confirm  ->  reindex-first  ->  delete-after-verify

The load-bearing invariant is NO DATA LOSS: the old collection is deleted
ONLY after the new one is verified populated. A mid-reindex failure leaves
the old collection fully intact. There is no delete-before-reindex path.

The detection is dimension-based (the same probe ``nx doctor`` uses,
``health._check_t3_local``), so it covers the local 384 -> local 768 case,
not only local <-> cloud.

Pure engine: no ``click`` here. The CLI wiring (``nx init``) handles the
preview, confirmation, and reporting; this module owns the ordering that
must never be gotten wrong.
"""
from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass
from typing import Literal

import structlog

from nexus.corpus import _CONFORMANT_COLLECTION_RE
from nexus.db.t3 import T3Database

_log = structlog.get_logger(__name__)

#: A reindex driver: ``(db, target_name, source_paths, corpus) ->
#: (indexed_sources, after_count)``. Injected so the safety ordering is
#: testable without real embedding I/O.
ReindexFn = Callable[[T3Database, str, frozenset[str], str], tuple[int, int]]

StaleKind = Literal["reindexable", "code", "sourceless"]
MigrationStatus = Literal["migrated", "failed", "skipped", "dry-run"]


@dataclass(frozen=True)
class StaleCollection:
    """A collection whose stored vectors do not match the active embedder.

    ``kind`` decides the migration path:
    - ``reindexable`` — has resolvable source files; migrate it.
    - ``code`` — a ``code__`` collection; reindex needs ``nx index repo``,
      so it is reported (with the exact command) and never auto-deleted.
    - ``sourceless`` — every entry lacks a source (e.g. manual ``store_put``
      notes); there is nothing to reindex from, so deleting would be pure
      loss. Reported, never auto-deleted.
    """

    name: str
    count: int
    source_paths: frozenset[str]
    sourceless: int
    target_name: str
    kind: StaleKind


@dataclass(frozen=True)
class MigrationOutcome:
    name: str
    target_name: str
    status: MigrationStatus
    before: int
    after: int
    reason: str = ""


def _target_name(old: str, active_token: str) -> str:
    """Map an old collection name to its target under ``active_token``.

    Conformant four-segment names (``<ct>__<owner>__<model>__v<n>``) get
    their model segment swapped. Legacy two-segment names do not encode a
    model, so the reindex lands in place (same name) — returned unchanged.
    """
    m = _CONFORMANT_COLLECTION_RE.match(old)
    if not m:
        return old
    g = m.groupdict()
    return f"{g['ct']}__{g['owner']}__{active_token}__v{g['ver']}"


def _classify(name: str, source_paths: frozenset[str], sourceless: int) -> StaleKind:
    if name.startswith("code__"):
        return "code"
    if not source_paths:
        return "sourceless"
    return "reindexable"


def detect_stale_local_collections(
    db: T3Database,
    *,
    active_dim: int,
    active_token: str = "bge-base-en-v15-768",
    resolve_doc_id: Callable[[str], str] | None = None,
) -> list[StaleCollection]:
    """Return collections whose stored vectors mismatch ``active_dim``.

    Probes each non-empty collection with a dummy vector of ``active_dim``
    (the ``nx doctor`` approach): a ChromaDB "dimension" rejection means the
    collection was indexed with a different embedder. Source files are
    enumerated so the caller can preview and migrate.
    """
    from nexus.commands.collection import _doc_id_to_file_path

    resolver = resolve_doc_id or _doc_id_to_file_path
    stale: list[StaleCollection] = []
    dummy = [0.0] * active_dim

    for entry in db.list_collections():
        name = entry["name"]
        count = entry.get("count", 0)
        if not count:
            continue
        try:
            col = db._client_for(name).get_collection(name)
        except Exception:
            continue
        try:
            col.query(query_embeddings=[dummy], n_results=1)
            continue  # active dim accepted — not stale
        except Exception as exc:
            if "dimension" not in str(exc).lower():
                # A non-dimension error (transient, permission) — do not
                # misclassify it as a migration candidate.
                _log.debug("stale_probe_nondim_error", collection=name, error=str(exc))
                continue

        source_paths, sourceless = collection_source_paths(db, name, resolve_doc_id=resolver)
        kind = _classify(name, source_paths, len(sourceless))
        stale.append(
            StaleCollection(
                name=name,
                count=count,
                source_paths=source_paths,
                sourceless=len(sourceless),
                target_name=_target_name(name, active_token),
                kind=kind,
            )
        )
    return stale


def collection_source_paths(
    db: T3Database,
    name: str,
    *,
    resolve_doc_id: Callable[[str], str],
) -> tuple[frozenset[str], list[str]]:
    """Enumerate the source files backing a collection's chunks.

    Mirrors the source-resolution logic in ``nx collection reindex``
    (collection.py:reindex_cmd). The two are deliberately NOT merged: this
    path reads a CROSS-embedder collection (the stored vectors do not match
    the active EF — that is the whole point), so it must use a raw client
    handle; ``reindex_cmd`` operates on a same-EF collection and uses the
    EF-attached handle. Forcing one onto the other's handle either triggers
    a ChromaDB EF-conflict here or churns reindex_cmd's regression tests.

    A chunk is reindexable when it carries ``source_path`` (legacy chunks),
    ``doc_id`` (resolved to the catalog ``file_path`` via ``resolve_doc_id``),
    or — post RDR-108 Phase 3 — only ``chunk_text_hash``, resolved via the
    catalog chash->doc_id manifest. Returns ``(source_paths, sourceless_ids)``.
    """
    # Raw client handle: we only read metadata via ``.get()``, so we must
    # not attach the active EF (it would conflict with the collection's
    # persisted EF config for cross-embedder names — the whole point here).
    col = db._client_for(name).get_collection(name)
    source_paths: set[str] = set()
    sourceless: list[str] = []
    offset = 0

    _cat = None
    try:
        from nexus.catalog import Catalog
        from nexus.config import catalog_path

        _cp = catalog_path()
        if Catalog.is_initialized(_cp):
            _cat = Catalog(_cp, _cp / ".catalog.db")
    except Exception:
        _cat = None

    while True:
        batch = col.get(limit=300, offset=offset, include=["metadatas"])
        metas = batch["metadatas"] or []

        page_chashes = [(m or {}).get("chunk_text_hash", "") for m in metas]
        page_chashes_nonempty = [c for c in page_chashes if c]
        chash_to_doc: dict[str, str] = {}
        if _cat is not None and page_chashes_nonempty:
            try:
                by_chash = _cat.docs_for_chashes(page_chashes_nonempty)
            except Exception:
                by_chash = {}
            for c, doc_ids in by_chash.items():
                if doc_ids:
                    chash_to_doc[c] = sorted(doc_ids)[0]

        for mid, meta in zip(batch["ids"], metas):
            meta = meta or {}
            sp = meta.get("source_path", "")
            did = meta.get("doc_id", "")
            if not did:
                chash = meta.get("chunk_text_hash", "")
                if chash:
                    did = chash_to_doc.get(chash, "")
            if sp:
                source_paths.add(sp)
            elif did:
                resolved = resolve_doc_id(did)
                if resolved:
                    source_paths.add(resolved)
                else:
                    sourceless.append(mid)
            else:
                sourceless.append(mid)

        if len(batch["ids"]) < 300:
            break
        offset += 300

    return frozenset(source_paths), sourceless


def _default_reindex(
    db: T3Database, target_name: str, source_paths: frozenset[str], corpus: str
) -> tuple[int, int]:
    """Production reindex driver: re-embed ``source_paths`` into
    ``target_name`` under the active (768) embedder.

    Mirrors ``nx collection reindex``'s per-prefix dispatch, but writes into
    the NEW target collection and never deletes anything. Returns
    ``(indexed_sources, after_count)``. ``code__`` collections never reach
    here (classified ``code`` and deferred).
    """
    from pathlib import Path

    from nexus.doc_indexer import batch_index_markdowns, index_markdown, index_pdf

    indexed = 0
    if target_name.startswith("rdr__"):
        rdr_files = [Path(sp) for sp in source_paths if Path(sp).exists()]
        if rdr_files:
            batch_index_markdowns(
                rdr_files, corpus=corpus, collection_name=target_name, force=True
            )
            indexed = len(rdr_files)
    else:  # docs__ / knowledge__
        for sp in source_paths:
            p = Path(sp)
            if not p.exists():
                continue
            if p.suffix.lower() == ".pdf":
                index_pdf(p, corpus=corpus, collection_name=target_name, force=True)
            else:
                index_markdown(p, corpus=corpus, collection_name=target_name, force=True)
            indexed += 1

    try:
        after = db.collection_info(target_name)["count"]
    except KeyError:
        after = 0
    return indexed, after


def migrate_collection_safe(
    db: T3Database,
    stale: StaleCollection,
    *,
    dry_run: bool,
    reindex_fn: ReindexFn | None = None,
) -> MigrationOutcome:
    """Migrate one stale collection under the gate-locked safety protocol.

    Ordered, no shortcuts:
      1. ``dry_run`` short-circuits with ZERO mutation.
      2. Deferred kinds (``code``, ``sourceless``) are skipped — never
         deleted (no source to reindex from = deleting is pure loss).
      3. Reindex sources into ``target_name`` FIRST.
      4. Verify the target is populated AND every expected source indexed.
      5. Delete the old collection ONLY after that verification.

    On any reindex failure or verification shortfall the old collection is
    left fully intact and ``status="failed"`` is returned. Never
    delete-before-reindex.
    """
    driver = reindex_fn or _default_reindex
    before = stale.count

    if dry_run:
        return MigrationOutcome(
            name=stale.name,
            target_name=stale.target_name,
            status="dry-run",
            before=before,
            after=0,
            reason="preview only — no changes made",
        )

    if stale.kind != "reindexable":
        reason = (
            f"{stale.name} is a {stale.kind} collection — no safe automatic "
            f"reindex; left intact"
        )
        return MigrationOutcome(
            name=stale.name,
            target_name=stale.target_name,
            status="skipped",
            before=before,
            after=0,
            reason=reason,
        )

    expected_sources = len(stale.source_paths)
    corpus = stale.name.split("__", 1)[1] if "__" in stale.name else ""

    try:
        indexed, after = driver(db, stale.target_name, stale.source_paths, corpus)
    except Exception as exc:  # noqa: BLE001 — any failure must keep old data
        _log.warning(
            "embed_migrate_reindex_failed",
            collection=stale.name,
            target=stale.target_name,
            error=str(exc),
        )
        return MigrationOutcome(
            name=stale.name,
            target_name=stale.target_name,
            status="failed",
            before=before,
            after=0,
            reason=f"reindex failed ({exc}); old collection left intact",
        )

    # delete-after-verify: target must be non-empty AND every source indexed.
    if after <= 0 or indexed < expected_sources:
        _log.warning(
            "embed_migrate_verify_failed",
            collection=stale.name,
            target=stale.target_name,
            indexed=indexed,
            expected=expected_sources,
            after=after,
        )
        return MigrationOutcome(
            name=stale.name,
            target_name=stale.target_name,
            status="failed",
            before=before,
            after=after,
            reason=(
                f"reindex verification failed (indexed {indexed}/{expected_sources}, "
                f"target has {after} chunks); old collection left intact"
            ),
        )

    db.delete_collection(stale.name)
    _log.info(
        "embed_migrate_succeeded",
        collection=stale.name,
        target=stale.target_name,
        before=before,
        after=after,
    )
    return MigrationOutcome(
        name=stale.name,
        target_name=stale.target_name,
        status="migrated",
        before=before,
        after=after,
        reason="reindexed and verified; old collection removed",
    )
