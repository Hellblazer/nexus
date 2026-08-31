# SPDX-License-Identifier: AGPL-3.0-or-later
# Copyright (c) 2026 Hal Hildebrand. All rights reserved.
"""Recovery bundle: export/import for the catalog link graph and
store_put-origin knowledge content across a reinstall (nexus-xn3fr,
GH #1419 Issue 9).

Design of record: T2 ``nexus/design-xn3fr-recovery-bundle.md`` (Sam-approved
2026-08-31). The two things a reinstall cannot regenerate are the catalog
LINK GRAPH (cross-collection, tumbler-addressed — and tumblers are NOT
stable across reindex) and store_put-origin knowledge content (whose ONLY
copy is its single T3 chunk — nexus-b6enc). This module carries both in one
human-inspectable JSONL bundle:

- line 1: JSON header (``format_version``, counts, source fingerprint);
- then one JSON object per line: ``knowledge_doc`` records FIRST, then
  ``link`` records (import is sequential, and links resolve against docs
  the same bundle just imported).

IDENTITY is ``source_uri`` throughout — never tumblers. store_put-origin
docs carry the nexus-sdp0u synthesized ``uri_for(collection, title)``
identity (or none at all for pre-sdp0u legacy rows); ``knowledge_doc``
records therefore ALSO carry ``(collection, title)`` so import re-derives
identity under the TARGET install's collection set. NO EMBEDDINGS are
carried (locked decision): import re-runs the real store_put chain, which
re-embeds — model-portable by construction.

The store-put-origin classifier below is a LOCAL reimplementation of the
shape in ``commands/catalog_cmds/reconcile_stale.py`` (and its sibling in
``catalog_cmds/integrity.py``) — this layer must not reach up into
``commands/`` (see ``store_hook.py``'s layering comment), the same
three-implementations-one-contract pattern as the ``file://`` convention.
It DIVERGES deliberately on two audited points (nx_plan_audit rounds 1-2,
recorded on nexus-xn3fr): the ``chroma://`` leg carries a
collection-prefix guard (a stray ``chroma://`` URI under a file-routed
prefix is migration residue, never store_put-origin — importing it would
recreate a duplicate document beside what ``nx index repo`` rebuilds,
the nexus-53cae pathology), and the empty-``source_uri`` leg NEVER trusts
the cached ``chunk_count`` column — it ground-truths against the manifest,
because the cache bound exists in ``reconcile_stale.py`` only for an
upstream routing this module does not have, and trusting it here silently
drops exactly the legacy live-chunk rows (the nexus-1uekf population) this
bead exists to carry.
"""
from __future__ import annotations

import json
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Callable, Iterable

import structlog

from nexus.aspect_readers import FILE_ROUTED_PREFIXES, uri_for
from nexus.catalog.types import CatalogEntry

_log = structlog.get_logger(__name__)

#: Bundle format version written by this implementation; mirrors
#: ``exporter.py``'s FORMAT_VERSION / MAX_SUPPORTED_FORMAT_VERSION pair.
RECOVERY_BUNDLE_FORMAT_VERSION: int = 1
MAX_SUPPORTED_RECOVERY_FORMAT_VERSION: int = 1

#: Page size for the link_query exhaustion loop (the ``bulk_unlink``
#: dry_run idiom — http_catalog_client.py's own pagination shape).
_LINK_PAGE: int = 200


class SingleChunkInvariantError(RuntimeError):
    """A store_put-origin document's manifest carried more than one row.

    ``store_put`` (MCP and CLI) is single-chunk BY CONSTRUCTION
    (``single_chunk_manifest_metadata``; ``T3Database.put``'s docstring).
    If this fires, that invariant broke somewhere and the export's
    no-join content reconstruction is no longer sound — escalate, never
    silently join.
    """


def classify_store_put_origin(entry: CatalogEntry) -> str | None:
    """Return the store-put-origin leg (``"chroma_uri"`` /
    ``"knowledge_no_path"``) for *entry*, or ``None`` when it is not
    store_put-origin. See the module docstring for the two audited
    divergences from the ``commands/``-layer classifiers."""
    if entry.file_path:
        return None  # file-backed always wins
    if entry.physical_collection.startswith(FILE_ROUTED_PREFIXES):
        # uri_for's own docstring is the single source of truth: chroma://
        # identity is minted for everything OUTSIDE the file-routed
        # prefixes; a chroma:// URI under one of them is residue.
        return None
    source_uri = entry.source_uri or ""
    if source_uri.startswith("chroma://"):
        return "chroma_uri"
    if not source_uri:
        return "knowledge_no_path"
    return None  # non-empty, non-chroma URI without a file_path: not ours


def link_identity_uri(entry: CatalogEntry) -> str:
    """The source_uri a LINK record should carry for *entry*.

    For a normal entry this is its recorded ``source_uri``. For a
    pre-sdp0u legacy knowledge doc with NO source_uri, export the
    identity the doc WILL carry after import — the store_put chain mints
    ``uri_for(collection, title)`` at registration — so the link's
    endpoint join key actually resolves on the target. (Plan-gap fix,
    recorded on nexus-xn3fr: exporting the empty string here would make
    every such link unresolvable by construction.)
    """
    if entry.source_uri:
        return entry.source_uri
    if entry.title and not entry.physical_collection.startswith(FILE_ROUTED_PREFIXES):
        return uri_for(entry.physical_collection, entry.title)
    return ""


@dataclass(slots=True)
class ExportSummary:
    docs_exported: int = 0
    links_exported: int = 0
    ghosts_skipped: int = 0
    file_routed_excluded: int = 0
    single_chunk_violations: int = 0
    unresolvable_link_endpoints: int = 0


@dataclass(slots=True)
class ImportSummary:
    docs_imported: int = 0
    docs_failed: int = 0
    links_created: int = 0
    links_merged: int = 0
    links_missing_span: int = 0
    unresolvable_links: list[dict] = field(default_factory=list)
    doc_failures: list[dict] = field(default_factory=list)


def enumerate_all_documents(reader: Any) -> list[CatalogEntry]:
    """One full catalog walk (``all_documents`` paginates to exhaustion
    internally). Shared by the store-put enumeration and the link
    tumbler→source_uri map so the export pays exactly one walk."""
    return list(reader.all_documents())


def select_store_put_documents(
    entries: Iterable[CatalogEntry],
    manifests: dict[str, list],
    summary: ExportSummary,
) -> list[tuple[CatalogEntry, str, list]]:
    """Apply the classifier + manifest ground-truthing.

    Returns ``(entry, leg, manifest_rows)`` triples for exportable docs.
    ``manifests`` is the BATCH ``get_manifests`` result — a doc_id absent
    from it is identical to present-with-zero-rows (a true ghost for the
    ``knowledge_no_path`` leg), never "not yet checked".
    """
    out: list[tuple[CatalogEntry, str, list]] = []
    for entry in entries:
        leg = classify_store_put_origin(entry)
        if leg is None:
            if (
                not entry.file_path
                and (entry.source_uri or "").startswith("chroma://")
                and entry.physical_collection.startswith(FILE_ROUTED_PREFIXES)
            ):
                summary.file_routed_excluded += 1
            continue
        rows = manifests.get(str(entry.tumbler), [])
        if len(rows) == 0:
            # True ghost (row without content) — counted, never silent.
            summary.ghosts_skipped += 1
            _log.info(
                "recovery_export_ghost_skipped",
                tumbler=str(entry.tumbler),
                title=entry.title,
                leg=leg,
            )
            continue
        if len(rows) > 1:
            summary.single_chunk_violations += 1
            raise SingleChunkInvariantError(
                f"store_put-origin doc {entry.tumbler} ({entry.title!r}) has "
                f"{len(rows)} manifest rows — store_put is single-chunk by "
                "construction; the no-join export contract is unsound for "
                "this document. Escalate (nexus-xn3fr single-chunk tripwire)."
            )
        out.append((entry, leg, rows))
    return out


def export_knowledge_doc(t3: Any, entry: CatalogEntry, manifest_rows: list) -> dict | None:
    """Build one ``knowledge_doc`` record; ``None`` when the chunk is gone
    from T3 (reported by the caller, never raised)."""
    chash = manifest_rows[0].chash
    row = t3.get_by_id(entry.physical_collection, chash)
    if row is None or not row.get("content"):
        _log.warning(
            "recovery_export_chunk_missing",
            tumbler=str(entry.tumbler),
            title=entry.title,
            chash=chash,
        )
        return None
    return {
        "record": "knowledge_doc",
        "source_uri": entry.source_uri or "",
        "collection": entry.physical_collection,
        "title": entry.title,
        "tags": row.get("tags", "") or "",
        "category": row.get("category", "") or "",
        "content": row["content"],
    }


def export_links(
    reader: Any,
    tumbler_to_uri: dict[str, str],
    summary: ExportSummary,
) -> list[dict]:
    """Enumerate ALL catalog links (``link_query`` driven to exhaustion —
    the ``bulk_unlink`` dry_run idiom) and denormalize each to source_uri
    endpoints. A tumbler missing from the map falls back to one
    ``reader.resolve`` round trip; a still-unresolvable endpoint is
    recorded on the summary, never raised."""
    records: list[dict] = []
    cur = 0
    pages = 0
    while True:
        batch = reader.link_query(limit=_LINK_PAGE, offset=cur)
        pages += 1
        for link in batch:
            rec = _link_record(reader, link, tumbler_to_uri, summary)
            if rec is not None:
                records.append(rec)
        if len(batch) < _LINK_PAGE:
            break
        cur += _LINK_PAGE
    if pages == 0:
        raise AssertionError("link_query exhaustion loop made zero requests")
    return records


def _endpoint_uri(
    reader: Any, tumbler: str, tumbler_to_uri: dict[str, str], summary: ExportSummary,
) -> str | None:
    uri = tumbler_to_uri.get(tumbler)
    if uri:
        return uri
    entry = reader.resolve(tumbler)
    if entry is not None:
        derived = link_identity_uri(entry)
        if derived:
            return derived
    summary.unresolvable_link_endpoints += 1
    _log.warning("recovery_export_link_endpoint_unresolvable", tumbler=tumbler)
    return None


def _link_record(
    reader: Any, link: Any, tumbler_to_uri: dict[str, str], summary: ExportSummary,
) -> dict | None:
    from_uri = _endpoint_uri(reader, str(link.from_tumbler), tumbler_to_uri, summary)
    to_uri = _endpoint_uri(reader, str(link.to_tumbler), tumbler_to_uri, summary)
    if from_uri is None or to_uri is None:
        return None
    return {
        "record": "link",
        "from_source_uri": from_uri,
        "to_source_uri": to_uri,
        "link_type": link.link_type,
        "from_span": link.from_span or "",
        "to_span": link.to_span or "",
        "created_by": link.created_by or "",
    }


def write_bundle(
    path: Path,
    knowledge_docs: list[dict],
    links: list[dict],
    summary: ExportSummary,
) -> None:
    """JSONL writer: one JSON header line, then knowledge_doc records,
    then link records. Plain JSON per line — human-inspectable is a
    locked format decision."""
    header = {
        "format": "nexus-recovery-bundle",
        "format_version": RECOVERY_BUNDLE_FORMAT_VERSION,
        "exported_at": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
        "knowledge_docs": len(knowledge_docs),
        "links": len(links),
        "ghosts_skipped": summary.ghosts_skipped,
    }
    with path.open("w", encoding="utf-8") as f:
        f.write(json.dumps(header) + "\n")
        for rec in knowledge_docs:
            f.write(json.dumps(rec, ensure_ascii=False) + "\n")
        for rec in links:
            f.write(json.dumps(rec, ensure_ascii=False) + "\n")


def export_bundle(reader: Any, t3: Any, path: Path) -> ExportSummary:
    """Full export: one catalog walk, batch manifest fetch, link
    denormalization, JSONL write. Read-only against the store."""
    summary = ExportSummary()
    entries = enumerate_all_documents(reader)

    tumbler_to_uri: dict[str, str] = {}
    candidates: list[CatalogEntry] = []
    for entry in entries:
        uri = link_identity_uri(entry)
        if uri:
            tumbler_to_uri[str(entry.tumbler)] = uri
        if classify_store_put_origin(entry) is not None:
            candidates.append(entry)

    manifests = reader.get_manifests([str(e.tumbler) for e in candidates]) if candidates else {}
    docs: list[dict] = []
    for entry, _leg, rows in select_store_put_documents(entries, manifests, summary):
        rec = export_knowledge_doc(t3, entry, rows)
        if rec is None:
            summary.ghosts_skipped += 1
            continue
        docs.append(rec)

    links = export_links(reader, tumbler_to_uri, summary)
    summary.docs_exported = len(docs)
    summary.links_exported = len(links)
    write_bundle(path, docs, links, summary)
    _log.info(
        "recovery_bundle_exported",
        path=str(path),
        docs=summary.docs_exported,
        links=summary.links_exported,
        ghosts_skipped=summary.ghosts_skipped,
    )
    return summary


# ── Import ──────────────────────────────────────────────────────────────────


def read_bundle(path: Path) -> tuple[dict, list[dict]]:
    """Parse a bundle; returns (header, records). Fails loud on a future
    format_version (mirrors ``exporter.py``'s MAX_SUPPORTED contract)."""
    with path.open("r", encoding="utf-8") as f:
        header_line = f.readline()
        header = json.loads(header_line)
        if header.get("format") != "nexus-recovery-bundle":
            raise ValueError(f"{path} is not a nexus recovery bundle (header: {header})")
        version = int(header.get("format_version", 0))
        if version > MAX_SUPPORTED_RECOVERY_FORMAT_VERSION:
            raise ValueError(
                f"bundle format_version {version} exceeds the maximum this "
                f"build reads ({MAX_SUPPORTED_RECOVERY_FORMAT_VERSION}) — "
                "upgrade conexus before importing"
            )
        records = [json.loads(line) for line in f if line.strip()]
    return header, records


def target_collection_for(recorded: str, t3: Any) -> str:
    """Resolve the RECORDED (source-install) collection name to the TARGET
    install's collection (review-fold, the critique's ship-blocker): a
    conformant 4-part name embeds the SOURCE's embedding model, and
    ``t3_collection_name`` passes conformant names through verbatim — so
    importing the recorded name onto a mode-changed install either raises
    ``IncompatibleCollectionError`` or fragments the corpus into a dead
    second collection. Reduce to the mode-independent ``type__owner`` base
    first (the RDR-103 shape's first two segments); the resolver then
    grandfathers an existing legacy collection or promotes to the TARGET's
    active model. A non-conformant recorded name passes through to the
    resolver unchanged, exactly as an operator-typed --collection would."""
    from nexus.corpus import is_conformant_collection_name, t3_collection_name  # noqa: PLC0415 — deferred to avoid circular import

    base = recorded
    if is_conformant_collection_name(recorded):
        parts = recorded.split("__")
        base = f"{parts[0]}__{parts[1]}"
    return t3_collection_name(base, t3=t3, for_write=True)


def _default_import_doc(t3: Any, rec: dict) -> None:
    """The real store_put chain, mirroring ``commands/store.py::put_cmd`` /
    ``mcp/core.py::store_put`` (hook → fence begin → t3.put → manifest
    direct → fire_store_chains, with the b6enc compensation on failure —
    the hook-chain leg was a review-fold: without it an imported doc gets
    no chash-index row, no taxonomy assignment, and never enters the
    aspect queue, with no sweep to catch it later). Deferred imports:
    this module sits below ``commands/`` and the chain's pieces live in
    sibling modules with heavy import graphs."""
    from nexus.catalog.store_hook import (  # noqa: PLC0415 — deferred, sibling with heavy import graph
        catalog_store_hook_tracked,
        single_chunk_manifest_metadata,
        store_put_manifest_direct,
    )
    from nexus.doc_indexer import _fence_begin, _fence_fail  # noqa: PLC0415 — deferred; test patch target

    col_name = target_collection_for(rec["collection"], t3)
    content = rec["content"]
    chunk_id, manifest_metadatas = single_chunk_manifest_metadata(content)
    catalog_doc_id, minted = catalog_store_hook_tracked(
        title=rec["title"], doc_id=chunk_id, collection_name=col_name,
    )
    content_hash = (
        manifest_metadatas[0].get("chunk_text_hash", "") if manifest_metadatas else ""
    )
    if catalog_doc_id:
        _fence_begin(catalog_doc_id, content_hash, col_name)
    try:
        doc_id = t3.put(
            collection=col_name,
            content=content,
            title=rec["title"],
            tags=rec.get("tags", ""),
            category=rec.get("category", ""),
            catalog_doc_id=catalog_doc_id,
        )
    except Exception as put_exc:
        if catalog_doc_id:
            _fence_fail(catalog_doc_id, str(put_exc))
        if catalog_doc_id and minted:
            from nexus.catalog.store_hook import rollback_minted_catalog_entry  # noqa: PLC0415 — deferred, sibling module
            rollback_minted_catalog_entry(catalog_doc_id, original_error=str(put_exc))
        raise
    if catalog_doc_id:
        try:
            store_put_manifest_direct(
                catalog_doc_id, manifest_metadatas, collection=col_name,
            )
        except Exception as manifest_exc:
            _fence_fail(catalog_doc_id, str(manifest_exc))
            raise
    # Post-store hook chains (review-fold blocker): chash index, taxonomy,
    # aspect-queue enqueue — the same unconditional ride put_cmd/MCP
    # store_put fire; per-hook failures are isolated by fire_batch.
    from nexus.hook_registry import HookRegistry, install_default_hooks  # noqa: PLC0415 — deferred to avoid import cycle

    hooks = HookRegistry()
    install_default_hooks(hooks)
    hooks.fire_store_chains(
        [doc_id], col_name, [content],
        metadatas=manifest_metadatas,
        catalog_doc_id=catalog_doc_id,
        manifest_complete={catalog_doc_id: content_hash} if catalog_doc_id else None,
    )


def _resolve_link_endpoint(reader: Any, t3: Any, uri: str) -> Any:
    """``by_source_uri``, with a chroma-identity re-derivation fallback
    (review-fold, the critique's mode-change trace): a link exported from
    a source install carries ``chroma://<SOURCE-collection>/<title>``, but
    on a mode-changed target the imported doc's minted identity uses the
    TARGET collection name — the recorded URI then resolves to nothing.
    Re-derive: parse collection/title out of the chroma URI, resolve the
    collection under the target (``target_collection_for``), and retry
    with the re-derived identity."""
    entry = reader.by_source_uri(uri)
    if entry is not None or not uri.startswith("chroma://"):
        return entry
    rest = uri[len("chroma://"):]
    collection, sep, title = rest.partition("/")
    if not sep or not title:
        return None
    try:
        target_col = target_collection_for(collection, t3)
    except Exception:  # noqa: BLE001 — fallback resolution is best-effort; the caller reports unresolvable
        return None
    rederived = uri_for(target_col, title)
    if rederived and rederived != uri:
        return reader.by_source_uri(rederived)
    return None


def _validated_span(reader: Any, span: str, summary: ImportSummary) -> str:
    """Locked span contract (design record; review-fold — this was
    declared-but-dead): a ``chash:``-anchored span whose chunk no longer
    exists on the target is STRIPPED and counted, and the link still
    imports without it. Non-chash spans and resolvable chashes pass
    through verbatim."""
    if not span.startswith("chash:"):
        return span
    chash = span.split(":", 2)[1]
    try:
        holders = reader.docs_for_chashes([chash])
    except Exception:  # noqa: BLE001 — span validation is advisory; an oracle failure must not block the link
        _log.warning("recovery_import_span_probe_failed", span=span)
        return span
    if holders.get(chash):
        return span
    summary.links_missing_span += 1
    _log.info("recovery_import_span_stripped", span=span)
    return ""


def import_bundle(
    reader: Any,
    writer: Any,
    t3: Any,
    path: Path,
    *,
    import_doc: Callable[[Any, dict], None] | None = None,
) -> ImportSummary:
    """Import a bundle. Docs first (the bundle writes them first), then
    links resolved by source_uri. FAIL-LOUD SUMMARY, not abort: every
    failure class is enumerated on the returned summary; a partial import
    completes the resolvable records. Idempotent by construction — the
    store_put chain reconciles per sdp0u, and the engine merges duplicate
    links (``co_discovered_by``)."""
    _header, records = read_bundle(path)
    summary = ImportSummary()
    do_import = import_doc or _default_import_doc

    for rec in records:
        if rec.get("record") != "knowledge_doc":
            continue
        try:
            do_import(t3, rec)
            summary.docs_imported += 1
        except Exception as exc:  # noqa: BLE001 — per-record fail-loud summary; the import must complete for the rest
            summary.docs_failed += 1
            summary.doc_failures.append(
                {"title": rec.get("title", ""), "error": str(exc)}
            )
            _log.warning(
                "recovery_import_doc_failed",
                title=rec.get("title", ""),
                error=str(exc),
            )

    for rec in records:
        if rec.get("record") != "link":
            continue
        from_e = _resolve_link_endpoint(reader, t3, rec["from_source_uri"])
        to_e = _resolve_link_endpoint(reader, t3, rec["to_source_uri"])
        if from_e is None or to_e is None:
            summary.unresolvable_links.append(
                {
                    "from_source_uri": rec["from_source_uri"],
                    "to_source_uri": rec["to_source_uri"],
                    "link_type": rec["link_type"],
                    "missing": ("from" if from_e is None else "to"),
                }
            )
            _log.warning(
                "recovery_import_link_unresolvable",
                from_uri=rec["from_source_uri"],
                to_uri=rec["to_source_uri"],
                link_type=rec["link_type"],
            )
            continue
        from_span = _validated_span(reader, rec.get("from_span", ""), summary)
        to_span = _validated_span(reader, rec.get("to_span", ""), summary)
        created = writer.link(
            from_e.tumbler,
            to_e.tumbler,
            rec["link_type"],
            rec.get("created_by") or "recovery-import",
            from_span=from_span,
            to_span=to_span,
        )
        if created:
            summary.links_created += 1
        else:
            summary.links_merged += 1

    _log.info(
        "recovery_bundle_imported",
        path=str(path),
        docs_imported=summary.docs_imported,
        docs_failed=summary.docs_failed,
        links_created=summary.links_created,
        links_merged=summary.links_merged,
        unresolvable=len(summary.unresolvable_links),
    )
    return summary
