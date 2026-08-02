# SPDX-License-Identifier: AGPL-3.0-or-later
# Copyright (c) 2026 Hal Hildebrand. All rights reserved.
"""Catalog type aliases (RDR-086 Phase 2).

``ChunkRef`` documents the return shape of ``Catalog.resolve_chash``.
``Catalog.resolve_span`` returns the same shape (minus the explicit
``physical_collection`` and ``doc_id`` fields it leaves to its caller)
but pre-dates the TypedDict — we keep both ``chash`` and ``chunk_hash``
keys on the dict for back-compat so existing resolve_span consumers
continue to work unchanged.
"""
from __future__ import annotations

from typing import NotRequired, TypedDict


class ChunkRef(TypedDict):
    """A single resolved chunk, collection-aware.

    Every field except ``char_range`` is present on every return. The
    ``chash`` / ``chunk_hash`` pair is intentional — legacy ``resolve_span``
    callers read ``chunk_hash`` while Phase 2 callers read ``chash``.
    """

    chash: str
    chunk_hash: str              # alias of ``chash`` for resolve_span back-compat
    physical_collection: str
    doc_id: str
    chunk_text: str
    metadata: dict
    char_range: NotRequired[tuple[int, int]]


# ─────────────────────────────────────────────────────────────────────────────
# Substrate-neutral survivors relocated from ``nexus.catalog.catalog``
# (nexus-i711w, beads nexus-37f4v + nexus-npywj, 2026-07-30).
#
# The SURVIVING service substrate is written in terms of these symbols —
# ``HttpCatalogClient``'s read API returns ``CatalogEntry`` / ``CatalogLink``
# — and six live modules call ``make_relative`` via function-local imports,
# yet all of them were DEFINED in the module the SQLite-catalog deletion
# removes. Plain dataclasses and pure functions, no substrate, no I/O
# (the ``nexus.db.t2.records`` pattern). ``catalog.py`` re-exports every
# name here until its deletion, so un-swept importers and the pinned
# local-catalog tests keep working unchanged in the interim.
# ─────────────────────────────────────────────────────────────────────────────

import os
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any
from urllib.parse import urlparse

from nexus.catalog.tumbler import Tumbler


def make_relative(abs_path: str | Path, repo_root: Path) -> str:
    """Return path relative to repo_root, or original if not under repo_root."""
    try:
        return str(Path(abs_path).relative_to(repo_root))
    except ValueError:
        return str(abs_path)


# Set of URI schemes the catalog will accept verbatim. Each scheme
# corresponds to a reader registered in ``nexus.aspect_readers``;
# adding a new scheme is gated on landing the reader first so
# register-time validation can't silently allow URIs that have no
# downstream consumer. ``file`` and ``chroma`` ship in Phase 1
# (RDR-096); ``https`` and ``nx-scratch`` are reserved for Phase 4.
# ``http`` is intentionally excluded — Phase 4's https reader does
# NOT cover plain http; users with http URIs must upgrade to https
# or wait for a dedicated reader. ``x-devonthink-item`` (nexus-bqda)
# is macOS-only — DEVONthink-managed PDFs carry a stable identity
# URL that resolves to the current filesystem path via osascript;
# the reader gates on ``sys.platform == 'darwin'`` and surfaces a
# clear error elsewhere.
_KNOWN_URI_SCHEMES: frozenset[str] = frozenset({
    "file", "chroma", "https", "nx-scratch", "x-devonthink-item",
    # ``nx-orphan-backfill://<collection>/<title|chash/<hash>>`` is a
    # marker scheme used by ``nx catalog orphan-backfill synthetic`` to
    # register catalog Documents for T3 chunks that have no recoverable
    # source on disk and no DEVONthink match. No reader is registered:
    # consumers reading these URIs receive ``scheme_unknown`` and skip,
    # which is the correct behavior since the underlying source IS
    # genuinely unavailable. The Document still serves to populate the
    # ``document_chunks`` manifest so doctor reports clean.
    "nx-orphan-backfill",
})


def _normalize_source_uri(
    source_uri: str, file_path: str, *, repo_root: str = "",
) -> str:
    """RDR-096 P3.1 register-boundary URI validation.

    * Empty ``source_uri`` + non-empty ``file_path`` → derive
      ``file://<abspath>`` (back-compat for callers passing only a
      filesystem path).
    * Empty ``source_uri`` + empty ``file_path`` → return ``""``
      (legacy entries with no identity at all stay shapeless).
    * Non-empty ``source_uri`` → validate via ``urlparse``: must
      have a recognized scheme. Malformed URIs raise ``ValueError``
      at the register boundary, NOT silently persisted (RDR-096
      Risks and Mitigations).

    nexus-3e4s: when ``file_path`` is relative AND ``repo_root`` is
    provided, the abspath is anchored on ``repo_root`` rather than
    the process CWD. This is the upstream fix for the catalog
    contamination bug class — without it, indexing repo ``A`` from
    a CWD inside repo ``B`` produced ``source_uri`` rows pointing
    to ``B``'s tree, leaving the row attributed to ``A``'s owner.
    """
    if not source_uri:
        if file_path:
            base = file_path
            if repo_root and not os.path.isabs(file_path):
                base = os.path.join(repo_root, file_path)
            return "file://" + os.path.abspath(base)
        return ""

    parsed = urlparse(source_uri)
    scheme = parsed.scheme
    if not scheme:
        raise ValueError(
            f"malformed source_uri {source_uri!r}: no scheme. "
            f"Expected one of {sorted(_KNOWN_URI_SCHEMES)} or a "
            f"bare filesystem path (passed via file_path instead).",
        )
    if scheme not in _KNOWN_URI_SCHEMES:
        raise ValueError(
            f"unknown source_uri scheme {scheme!r} in {source_uri!r}. "
            f"Known schemes: {sorted(_KNOWN_URI_SCHEMES)}. To add a "
            f"new scheme, register a reader in nexus.aspect_readers.",
        )
    return source_uri


# nexus-3e4s: env-var escape hatch for the cross-project guard. Set to
# ``"1"`` only to recover from emergency situations (e.g. a known-good
# cleanup script that legitimately needs to register rows across project
# boundaries). Never the right answer for normal indexing.
_CROSS_PROJECT_OVERRIDE_ENV = "NEXUS_CATALOG_ALLOW_CROSS_PROJECT"


@dataclass
class CatalogEntry:
    tumbler: Tumbler
    title: str
    author: str
    year: int
    content_type: str
    file_path: str
    corpus: str
    physical_collection: str
    chunk_count: int
    head_hash: str
    indexed_at: str
    meta: dict = field(default_factory=dict)
    # nexus-8luh: POSIX mtime at index time; 0.0 → not captured.
    source_mtime: float = 0.0
    # nexus-s8yz: alias pointer to a canonical tumbler. '' means this
    # entry is canonical. Populated by dedupe-owners (nexus-tmbh) when
    # consolidating duplicate owner registrations.
    alias_of: str = ""
    # RDR-096 P3.1: persistent URI identity. Populated at register
    # time — bare paths normalize to ``file://<abspath>``; explicit
    # URIs (chroma://, https://, etc.) are stored verbatim. ''
    # only on legacy entries that predate P2.1's column migration.
    source_uri: str = ""
    # nexus-rzqto: bibliographic enrichment surfaced from catalog_documents
    # (RDR-101 columns) so the document-level query() text form can render
    # bib metadata in service mode without reading chunk metadata. Defaults
    # keep every existing CatalogEntry construction site compiling.
    bib_year: int = 0
    bib_authors: str = ""
    bib_venue: str = ""
    bib_citation_count: int = 0
    # nexus-9l2lg: the remaining 4 of 8 RDR-101 bib_* columns. The engine
    # (CatalogRepository) already persists and returns all 8; these were
    # missing here, which meant every local reader silently truncated
    # them even though the wire payload carried them.
    bib_semantic_scholar_id: str = ""
    bib_openalex_id: str = ""
    bib_doi: str = ""
    bib_enriched_at: str = ""
    # nexus-5xn3k.3 (RUNFENCE): the index-run fence fields (catalog-020,
    # landed by .1/.2). ``index_state`` is the ONLY nullable field on this
    # dataclass by design: NULL/None means "unknown" (a legacy pre-fence
    # row, or an engine that predates the fence entirely) -- never coerced
    # to ``'complete'`` or to any other sentinel string. The other three
    # mirror the engine's NOT NULL DEFAULT '' columns, same empty-string
    # default as ``alias_of``/``source_uri`` above.
    index_state: str | None = None
    index_content_hash: str = ""
    index_run_id: str = ""
    index_started_at: str = ""

    def to_dict(self) -> dict:
        return {
            "tumbler": str(self.tumbler),
            "title": self.title,
            "author": self.author,
            "year": self.year,
            "content_type": self.content_type,
            "file_path": self.file_path,
            "corpus": self.corpus,
            "physical_collection": self.physical_collection,
            "chunk_count": self.chunk_count,
            "head_hash": self.head_hash,
            "indexed_at": self.indexed_at,
            "meta": self.meta,
            "source_mtime": self.source_mtime,
            "alias_of": self.alias_of,
            "source_uri": self.source_uri,
            "bib_year": self.bib_year,
            "bib_authors": self.bib_authors,
            "bib_venue": self.bib_venue,
            "bib_citation_count": self.bib_citation_count,
            "bib_semantic_scholar_id": self.bib_semantic_scholar_id,
            "bib_openalex_id": self.bib_openalex_id,
            "bib_doi": self.bib_doi,
            "bib_enriched_at": self.bib_enriched_at,
            "index_state": self.index_state,
            "index_content_hash": self.index_content_hash,
            "index_run_id": self.index_run_id,
            "index_started_at": self.index_started_at,
        }


@dataclass
class CatalogLink:
    from_tumbler: Tumbler
    to_tumbler: Tumbler
    link_type: str
    from_span: str
    to_span: str
    created_by: str
    created_at: str
    meta: dict = field(default_factory=dict)

    def to_dict(self) -> dict:
        return {
            "from": str(self.from_tumbler),
            "to": str(self.to_tumbler),
            "type": self.link_type,
            "from_span": self.from_span,
            "to_span": self.to_span,
            "created_by": self.created_by,
            "created_at": self.created_at,
            "meta": self.meta,
        }


@dataclass(frozen=True, slots=True)
class ManifestRow:
    """One row from ``document_chunks``, ordered by position.

    Fields mirror the ``document_chunks`` schema (RDR-108 D2):
    ``(position, chash, chunk_index, line_start, line_end,
    char_start, char_end)``. Span columns are ``None`` for chunks
    that were inserted without span metadata.

    Moved verbatim from ``catalog_writes.py`` (nexus-i711w terminal
    deletion, 2026-07-30): surviving ``http_catalog_client.py`` returns
    it from ``get_manifest``/``get_manifests``.
    """

    position: int
    chash: str
    chunk_index: int | None = None
    line_start: int | None = None
    line_end: int | None = None
    char_start: int | None = None
    char_end: int | None = None


def _default_registry_path() -> Path:
    """Return the default path to the repos.json registry.

    Moved verbatim from ``catalog.py`` (nexus-i711w pre-flight, 2026-07-30):
    surviving ``commands/catalog.py`` + ``mcp/catalog.py`` import it.
    """
    from nexus.config import nexus_config_dir  # noqa: PLC0415 — config import kept lazy, matching the original call pattern

    return nexus_config_dir() / "repos.json"
