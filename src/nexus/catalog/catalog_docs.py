# SPDX-License-Identifier: AGPL-3.0-or-later
"""Document / owner / collection lookup ops (nexus-mbm extraction 4/5).

Read-only catalog queries: tumbler resolution, file-path / owner /
content-type / corpus filters, collection registry lookups, and
the BFS-like ``descendants`` walk. Writes (``register_owner``,
``register``, ``register_collection``, ``update``,
``delete_document``, alias/collection mutations) stay on
``Catalog`` — they touch the event-sourcing + JSONL append
machinery that lives there.

Composed onto ``Catalog`` as ``self._docs`` (T2Database-style
facade pattern).  Public ``Catalog.resolve`` / ``find`` /
``by_doc_id`` / ... are thin delegates so the public API is
unchanged.
"""
from __future__ import annotations

import json
import os
from datetime import UTC, datetime
from pathlib import Path
from typing import TYPE_CHECKING, Sequence

import structlog

from nexus.catalog.collection_name import CollectionName, owner_segment_for_tumbler
from nexus.catalog.tumbler import Tumbler
from nexus.corpus import (
    CANONICAL_EMBEDDING_MODELS,
    CONTENT_TYPES,
    LOCAL_EMBEDDING_MODELS,
    effective_embedding_model_for_writes,
)
from nexus.repo_identity import _repo_identity

# ``CatalogEntry`` is imported lazily inside the methods that need
# it. ``catalog.py`` imports this module from inside
# ``Catalog.__init__`` (instance time, after ``catalog`` finishes
# loading), so a top-level ``from nexus.catalog.catalog import ...``
# would also work — but the lazy form keeps the import direction
# one-way (catalog → catalog_docs only at instantiation), which
# makes the dependency easier to read.
if TYPE_CHECKING:
    from nexus.catalog.catalog import Catalog, CatalogEntry

_log = structlog.get_logger(__name__)


# Column ordering for the ``collections`` projection. Co-located with
# the read methods that consume it so a row-shape change touches one
# module, not two. ``Catalog.register_collection`` (the writer) reads
# from here via the module path; no class attribute on Catalog is
# needed (architectural review nexus-mbm follow-up).
_COLLECTION_COLUMNS: tuple[str, ...] = (
    "name",
    "content_type",
    "owner_id",
    "embedding_model",
    "model_version",
    "display_name",
    "legacy_grandfathered",
    "superseded_by",
    "superseded_at",
    "created_at",
)


def _row_to_collection_dict(row: tuple) -> dict:
    """Coerce a ``collections``-table row tuple into a dict.

    The ``legacy_grandfathered`` column is stored as 0/1 in SQLite;
    we cast to ``bool`` for callers.
    """
    d = dict(zip(_COLLECTION_COLUMNS, row))
    d["legacy_grandfathered"] = bool(d.get("legacy_grandfathered") or 0)
    return d


class _DocumentOps:
    """Read-only catalog queries composed onto ``Catalog``.

    Methods access catalog state via ``self._cat.<attr>``.  When a
    moved method needs to call another moved method, it goes through
    ``self._cat.<method>`` so the call hits Catalog's delegate (and
    test patches on ``cat.<method>`` propagate).
    """

    def __init__(self, catalog: "Catalog") -> None:
        self._cat = catalog

    def owner_for_repo(self, repo_hash: str) -> Tumbler | None:
        cat = self._cat
        row = cat._db.execute(
            "SELECT tumbler_prefix FROM owners WHERE repo_hash = ?", (repo_hash,)
        ).fetchone()
        return Tumbler.parse(row[0]) if row else None

    def owner_tumblers_by_name(self, name: str) -> list[Tumbler]:
        """Return tumblers of all owners with this name.

        UNIQUE constraint is ``(name, owner_type)`` per nexus-7vuw, so
        a single name can map to multiple owners across types (e.g.
        a repo and a curator both named ``nexus``). Callers that need
        a unique answer should disambiguate on the returned list
        (typical CLI flow: error when ``len(...) > 1`` and surface
        the candidates).

        Returns ``[]`` if no owner has this name. Used by the
        ``--owner`` CLI flags on ``nx catalog list`` (and friends)
        to resolve operator-typed names to tumblers without leaking
        the ``Tumbler.parse → int()`` ``ValueError`` (#537,
        nexus-1lx7).
        """
        cat = self._cat
        rows = cat._db.execute(
            "SELECT tumbler_prefix FROM owners WHERE name = ? "
            "ORDER BY tumbler_prefix",
            (name,),
        ).fetchall()
        return [Tumbler.parse(r[0]) for r in rows]

    def resolve(self, tumbler: Tumbler, *, follow_alias: bool = True) -> CatalogEntry | None:
        """Return the document entry for ``tumbler``.

        With ``follow_alias=True`` (default), transparently dereferences
        ``alias_of`` — external callers get the canonical entry even
        when they asked by an old tumbler. Pass ``follow_alias=False`` to
        see the raw entry (needed by dedupe tooling to inspect the alias
        graph itself).
        """
        cat = self._cat
        from nexus.catalog.catalog import CatalogEntry
        target = cat.resolve_alias(tumbler) if follow_alias else tumbler
        row = cat._db.execute(
            "SELECT tumbler, title, author, year, content_type, file_path, "
            "corpus, physical_collection, chunk_count, head_hash, indexed_at, "
            "metadata, source_mtime, alias_of, source_uri "
            "FROM documents WHERE tumbler = ?",
            (str(target),),
        ).fetchone()
        if not row:
            return None
        return CatalogEntry(
            tumbler=Tumbler.parse(row[0]),
            title=row[1],
            author=row[2],
            year=row[3],
            content_type=row[4],
            file_path=row[5],
            corpus=row[6],
            physical_collection=row[7],
            chunk_count=row[8],
            head_hash=row[9],
            indexed_at=row[10],
            meta=json.loads(row[11]) if row[11] else {},
            source_mtime=row[12] or 0.0,
            alias_of=row[13] or "",
            source_uri=row[14] or "",
        )

    def list_by_collection(
        self, physical_collection: str, *, limit: int | None = None,
    ) -> list[CatalogEntry]:
        """Return every document entry whose ``physical_collection``
        matches.

        One entry per source document (NOT per chunk) — what callers
        like ``nx enrich aspects`` need to drive a per-document
        operation. Ordered by ``tumbler ASC`` for deterministic
        iteration. ``limit=None`` returns every match.

        Reads the SQLite cache without acquiring the JSONL-truth
        flock — consistent with ``resolve``, ``find``, and
        ``by_file_path``. Callers driving downstream writes (e.g.
        ``nx enrich aspects``) should treat the result as a
        best-effort sweep; a document registered concurrently may
        be missed and can be picked up by a subsequent run or by
        ``--re-extract`` re-sweeps.
        """
        cat = self._cat
        from nexus.catalog.catalog import CatalogEntry
        sql = (
            "SELECT tumbler, title, author, year, content_type, file_path, "
            "corpus, physical_collection, chunk_count, head_hash, indexed_at, "
            "metadata, source_mtime, alias_of, source_uri "
            "FROM documents WHERE physical_collection = ? "
            "ORDER BY tumbler ASC"
        )
        params: tuple = (physical_collection,)
        if limit is not None:
            sql += " LIMIT ?"
            params = (physical_collection, limit)
        rows = cat._db.execute(sql, params).fetchall()
        return [
            CatalogEntry(
                tumbler=Tumbler.parse(row[0]),
                title=row[1],
                author=row[2],
                year=row[3],
                content_type=row[4],
                file_path=row[5],
                corpus=row[6],
                physical_collection=row[7],
                chunk_count=row[8],
                head_hash=row[9],
                indexed_at=row[10],
                meta=json.loads(row[11]) if row[11] else {},
                source_mtime=row[12] or 0.0,
                alias_of=row[13] or "",
                source_uri=row[14] or "",
            )
            for row in rows
        ]

    def list_collections(self) -> list[dict]:
        """Return every row in the ``collections`` projection, ordered by name."""
        cat = self._cat
        sql = (
            "SELECT " + ", ".join(_COLLECTION_COLUMNS) + " "
            "FROM collections ORDER BY name"
        )
        rows = cat._db.execute(sql).fetchall()
        return [_row_to_collection_dict(r) for r in rows]

    def get_collection(self, name: str) -> dict | None:
        cat = self._cat
        sql = (
            "SELECT " + ", ".join(_COLLECTION_COLUMNS) + " "
            "FROM collections WHERE name = ?"
        )
        row = cat._db.execute(sql, (name,)).fetchone()
        return _row_to_collection_dict(row) if row else None

    def is_legacy_collection(self, name: str) -> bool:
        """Return True if ``name`` is registered AND flagged legacy.

        Unknown names return False (read paths are operationally hostile
        to fail-loud per RDR-101 §"Phase 6"). Callers wanting strict
        membership should query :meth:`get_collection` and check for None.
        """
        cat = self._cat
        row = cat._db.execute(
            "SELECT legacy_grandfathered FROM collections WHERE name = ?",
            (name,),
        ).fetchone()
        return bool(row and row[0])

    def collection_for(
        self,
        content_type: str,
        owner: Tumbler | str,
        embedding_model: str,
        *,
        bump: bool = False,
    ) -> CollectionName:
        """Resolve the canonical ``CollectionName`` for a tuple.

        RDR-103 Phase 2. The catalog is the authority for collection
        naming: callers describe the tuple they want, the catalog renders
        the physical name. Validation is strict at the public boundary
        (per pinned decision #4): ``content_type`` must be in
        :data:`nexus.corpus.CONTENT_TYPES`, ``embedding_model`` must be in
        :data:`nexus.corpus.CANONICAL_EMBEDDING_MODELS`, and the derived
        owner segment must be non-empty.

        Version handling:

        - New tuple ``(c, o, m)`` returns ``v1``.
        - Existing tuple at ``vN`` returns ``vN`` (idempotent).
        - With ``bump=True``, an existing ``vN`` returns ``vN+1``; a
          new tuple still returns ``v1`` (bump only fires when prior
          versions exist).

        Pinned decision #2: a new ``embedding_model`` is NOT a version
        bump. ``(c, o, m_new)`` is a different tuple from ``(c, o, m_old)``
        and naturally lands in ``v1``. The operator runs
        ``nx catalog supersede-collection`` to retire the old tuple.

        Grandfathered legacy rows do NOT contribute to the version
        lookup: their canonical fields are typically empty strings, and
        the WHERE clause filters them out via ``legacy_grandfathered = 0``
        belt-and-suspenders. Pinned decision #1.

        This method does NOT register the returned name in the catalog
        projection. Callers must follow up with
        :meth:`register_collection` once they have actually created (or
        otherwise materialised) the T3 collection. The indexer's
        ``_catalog_hook_repo`` already pairs creation with registration;
        Phase 3 wires that pattern through every indexer call site.

        The returned ``CollectionName`` is constructed directly rather
        than round-tripped through ``CollectionName.parse(render(...))``;
        the fields are validated above against the same closed sets,
        making the round-trip redundant.
        """
        cat = self._cat
        if content_type not in CONTENT_TYPES:
            raise ValueError(
                f"collection_for: unknown content_type {content_type!r}; "
                f"expected one of {CONTENT_TYPES}"
            )
        if (
            embedding_model not in CANONICAL_EMBEDDING_MODELS
            and embedding_model not in LOCAL_EMBEDDING_MODELS
        ):
            allowed = sorted(
                CANONICAL_EMBEDDING_MODELS | LOCAL_EMBEDDING_MODELS
            )
            raise ValueError(
                f"collection_for: non-canonical embedding_model "
                f"{embedding_model!r}; expected one of {allowed}"
            )
        owner_id = owner_segment_for_tumbler(owner)
        if not owner_id:
            raise ValueError(
                f"collection_for: cannot derive owner_id segment from "
                f"owner {owner!r}"
            )
        # The compound index ``idx_collections_tuple`` covers this lookup.
        # ``model_version`` is stored as TEXT (``v1``..``vN``). SUBSTR
        # strips the ``v`` prefix so SQLite can CAST the digit string to
        # INTEGER; ``CAST('v3' AS INTEGER)`` returns 0 because SQLite
        # cannot parse a leading non-digit. The INTEGER cast is what
        # gives MAX integer ordering rather than lexical (otherwise
        # ``v10`` would sort before ``v9``).
        row = cat._db.execute(
            "SELECT MAX(CAST(SUBSTR(model_version, 2) AS INTEGER)) "
            "FROM collections "
            "WHERE content_type = ? AND owner_id = ? "
            "AND embedding_model = ? AND legacy_grandfathered = 0",
            (content_type, owner_id, embedding_model),
        ).fetchone()
        existing_version = int(row[0]) if row and row[0] is not None else 0
        if existing_version == 0:
            new_version = 1
        elif bump:
            new_version = existing_version + 1
        else:
            new_version = existing_version
        return CollectionName(
            content_type=content_type,
            owner_id=owner_id,
            embedding_model=embedding_model,
            model_version=new_version,
        )

    def collection_for_repo(
        self,
        repo: Path,
        content_type: str,
        *,
        bump: bool = False,
    ) -> CollectionName:
        """Resolve the canonical ``CollectionName`` for ``content_type`` in ``repo``.

        Convenience wrapper around :meth:`collection_for` that handles
        the repo-to-owner-to-collection-name pipeline:

        1. Compute ``repo_hash`` via
           :func:`nexus.registry._repo_identity`.
        2. Look up the owner via :meth:`owner_for_repo`. Raises
           ``LookupError`` when no owner exists; the indexer's
           ``_catalog_hook`` flow registers the owner up front, so a
           missing owner indicates a bypass of the standard write path.
        3. Resolve the effective embedding model via
           :func:`nexus.corpus.effective_embedding_model_for_writes`
           (cloud mode delegates to the canonical model; local mode
           returns the local embedder's token, RDR-109 Phase 2).
        4. Delegate to :meth:`collection_for`.

        This is the helper that Phase 3 indexer call sites use. The
        pre-RDR-103 ``_docs_collection_name(repo)`` family that this
        replaced was removed in Phase 5.
        """
        cat = self._cat
        from nexus.repo_identity import _repo_identity  # noqa: PLC0415

        _, repo_hash = _repo_identity(repo)
        owner = cat.owner_for_repo(repo_hash)
        if owner is None:
            raise LookupError(
                f"collection_for_repo: no owner registered for "
                f"repo_hash {repo_hash!r} (repo {repo!s}). "
                f"Call register_owner(...) before requesting a "
                f"collection name; the indexer's _catalog_hook normally "
                f"registers owners up front."
            )
        return cat.collection_for(
            content_type=content_type,
            owner=owner,
            embedding_model=effective_embedding_model_for_writes(content_type),
            bump=bump,
        )

    def resolve_alias(self, tumbler: Tumbler, *, max_hops: int = 16) -> Tumbler:
        """Walk the alias chain to its canonical terminus.

        Returns ``tumbler`` itself when no alias is set (the common case
        and the pre-nexus-s8yz behaviour). Walks at most ``max_hops``
        links and bails on cycles — a broken chain is treated as
        terminating at the last-seen tumbler rather than raising, so
        reads stay available even in a pathological catalog.
        """
        cat = self._cat
        seen: set[str] = set()
        current = str(tumbler)
        for _ in range(max_hops):
            if current in seen:
                _log.warning("catalog.alias_cycle", tumbler=str(tumbler), seen=sorted(seen))
                break
            seen.add(current)
            row = cat._db.execute(
                "SELECT alias_of FROM documents WHERE tumbler = ?",
                (current,),
            ).fetchone()
            if not row:
                # Dangling alias — return the last valid hop. Callers that
                # need to detect this can compare to the input tumbler.
                break
            target = (row[0] or "").strip()
            if not target:
                # Canonical — this is the terminus.
                return Tumbler.parse(current)
            current = target
        return Tumbler.parse(current)

    def resolve_path(self, tumbler: Tumbler) -> Path | None:
        """Return absolute path for the document's file_path.

        Resolution order:
        1. Look up entry via cat.resolve(tumbler)
        2. If entry not found: return None
        3. Find owner: tumbler.owner_address() -> str, look up in SQLite
        4. If owner not found or owner.owner_type == "curator": return None
        5. If entry.file_path is already absolute: return Path(entry.file_path)
        6. If owner.repo_root is non-empty: return Path(owner.repo_root) / entry.file_path
        7. Otherwise: log ``catalog_resolve_path_legacy_owner_missing_repo_root``
           at DEBUG and return None.

        RDR-137 Phase 3.6 (nexus-tts0d.11, OQ-11): the previous
        legacy-registry fallback (iterate registry rows matching
        repo_hash) is removed. Post-nexus-nzyrh every freshly-registered owner
        gets a canonical main_repo path in ``repo_root``; any
        ``repo_root=''`` row in the live catalog is a pre-RDR-137
        artifact that the next ``nx index repo`` run on the same
        repo will heal. The DEBUG event surfaces which owners still
        need that healing pass.
        """
        cat = self._cat

        entry = cat.resolve(tumbler)
        if not entry:
            return None

        # Find owner via SQLite (avoids re-reading JSONL on every call)
        owner_prefix = str(tumbler.owner_address())
        row = cat._db.execute(
            "SELECT owner_type, repo_root, repo_hash FROM owners WHERE tumbler_prefix = ?",
            (owner_prefix,),
        ).fetchone()
        if not row:
            return None
        owner_type, repo_root, repo_hash = row[0], row[1], row[2]

        # Curators (PDFs, standalone docs) are not resolvable
        if owner_type == "curator":
            return None

        # If file_path is already absolute, return it directly
        fp = Path(entry.file_path)
        if fp.is_absolute():
            return fp

        # Primary: use repo_root from owner
        if repo_root:
            return Path(repo_root) / entry.file_path

        # Post-RDR-137 P3.6: no registry fallback. Legacy owners with
        # empty repo_root cannot be resolved without a re-index pass.
        _log.debug(
            "catalog_resolve_path_legacy_owner_missing_repo_root",
            tumbler=str(tumbler),
            owner_prefix=owner_prefix,
            repo_hash=repo_hash,
            file_path=entry.file_path,
            hint="re-run 'nx index repo' on the source repo to backfill repo_root",
        )
        return None

    def descendants(self, prefix: str) -> list[dict]:
        """All documents whose tumbler starts with *prefix* (any depth).

        Unlike ``by_owner`` which returns only direct children, this returns
        the full subtree.  The prefix itself is excluded.
        """
        cat = self._cat
        return cat._db.descendants(prefix)

    def resolve_chunk(self, tumbler: Tumbler) -> dict | None:
        """Resolve a 4-segment chunk tumbler to its document + chunk metadata.

        Chunks are implicit addresses — the catalog tracks document-level entries
        only; chunk sub-addresses are resolved on demand from the document's
        ``chunk_count``.  Resolution parses the document prefix, verifies the
        document exists, and checks the chunk index is in range.

        Returns ``{"document_tumbler", "chunk_index", "physical_collection", ...}``
        or None if the tumbler is not a chunk address or the document/chunk is
        missing.
        """
        cat = self._cat
        if tumbler.chunk is None:
            return None
        doc_tumbler = tumbler.document_address()
        entry = cat.resolve(doc_tumbler)
        if entry is None:
            return None
        chunk_idx = tumbler.chunk
        # chunk_count of 0 or None means count is not yet known — skip bounds check
        if entry.chunk_count and chunk_idx >= entry.chunk_count:
            return None
        return {
            "document_tumbler": str(doc_tumbler),
            "chunk_index": chunk_idx,
            "physical_collection": entry.physical_collection,
            "title": entry.title,
            "content_type": entry.content_type,
        }

    def find(self, query: str, *, content_type: str | None = None) -> list[CatalogEntry]:
        cat = self._cat
        from nexus.catalog.catalog import CatalogEntry
        rows = cat._db.search(query, content_type=content_type)
        return [
            CatalogEntry(
                tumbler=Tumbler.parse(r["tumbler"]),
                title=r["title"],
                author=r["author"],
                year=r["year"],
                content_type=r["content_type"],
                file_path=r["file_path"],
                corpus=r["corpus"],
                physical_collection=r["physical_collection"],
                chunk_count=r["chunk_count"],
                head_hash=r["head_hash"] or "",
                indexed_at=r["indexed_at"] or "",
                meta=json.loads(r["metadata"]) if r.get("metadata") else {},
                source_mtime=r["source_mtime"] if "source_mtime" in r else 0.0,
            )
            for r in rows
        ]

    def by_file_path(self, owner: Tumbler, file_path: str) -> CatalogEntry | None:
        cat = self._cat
        from nexus.catalog.catalog import CatalogEntry
        row = cat._db.execute(
            "SELECT tumbler, title, author, year, content_type, file_path, "
            "corpus, physical_collection, chunk_count, head_hash, indexed_at, metadata, source_mtime, source_uri "
            f"FROM documents WHERE {cat._prefix_sql(str(owner))[0]} AND file_path = ?",
            (*cat._prefix_sql(str(owner))[1], file_path),
        ).fetchone()
        if not row:
            return None
        return CatalogEntry(
            tumbler=Tumbler.parse(row[0]),
            title=row[1],
            author=row[2],
            year=row[3],
            content_type=row[4],
            file_path=row[5],
            corpus=row[6],
            physical_collection=row[7],
            chunk_count=row[8],
            head_hash=row[9],
            indexed_at=row[10],
            meta=json.loads(row[11]) if row[11] else {},
            source_mtime=row[12] or 0.0,
            source_uri=row[13] or "",
        )

    def by_owner(self, owner: Tumbler) -> list[CatalogEntry]:
        cat = self._cat
        from nexus.catalog.catalog import CatalogEntry
        rows = cat._db.execute(
            "SELECT tumbler, title, author, year, content_type, file_path, "
            "corpus, physical_collection, chunk_count, head_hash, indexed_at, metadata, source_mtime, source_uri "
            f"FROM documents WHERE {cat._prefix_sql(str(owner))[0]}",
            cat._prefix_sql(str(owner))[1],
        ).fetchall()
        return [
            CatalogEntry(
                tumbler=Tumbler.parse(r[0]),
                title=r[1],
                author=r[2],
                year=r[3],
                content_type=r[4],
                file_path=r[5],
                corpus=r[6],
                physical_collection=r[7],
                chunk_count=r[8],
                head_hash=r[9],
                indexed_at=r[10],
                meta=json.loads(r[11]) if r[11] else {},
                source_mtime=r[12] or 0.0,
                source_uri=r[13] or "",
            )
            for r in rows
        ]

    def by_content_type(self, content_type: str) -> list[CatalogEntry]:
        """List all entries with the given content type (code, paper, rdr, knowledge)."""
        cat = self._cat
        from nexus.catalog.catalog import CatalogEntry
        rows = cat._db.execute(
            "SELECT tumbler, title, author, year, content_type, file_path, "
            "corpus, physical_collection, chunk_count, head_hash, indexed_at, metadata, source_mtime, source_uri "
            "FROM documents WHERE content_type = ?",
            (content_type,),
        ).fetchall()
        return [
            CatalogEntry(
                tumbler=Tumbler.parse(r[0]), title=r[1], author=r[2], year=r[3],
                content_type=r[4], file_path=r[5], corpus=r[6],
                physical_collection=r[7], chunk_count=r[8], head_hash=r[9],
                indexed_at=r[10], meta=json.loads(r[11]) if r[11] else {},
                source_mtime=r[12] or 0.0,
                source_uri=r[13] or "",
            )
            for r in rows
        ]

    def by_corpus(self, corpus: str) -> list[CatalogEntry]:
        """List all entries with the given corpus tag."""
        cat = self._cat
        from nexus.catalog.catalog import CatalogEntry
        rows = cat._db.execute(
            "SELECT tumbler, title, author, year, content_type, file_path, "
            "corpus, physical_collection, chunk_count, head_hash, indexed_at, metadata, source_mtime, source_uri "
            "FROM documents WHERE corpus = ?",
            (corpus,),
        ).fetchall()
        return [
            CatalogEntry(
                tumbler=Tumbler.parse(r[0]), title=r[1], author=r[2], year=r[3],
                content_type=r[4], file_path=r[5], corpus=r[6],
                physical_collection=r[7], chunk_count=r[8], head_hash=r[9],
                indexed_at=r[10], meta=json.loads(r[11]) if r[11] else {},
                source_mtime=r[12] or 0.0,
                source_uri=r[13] or "",
            )
            for r in rows
        ]

    def doc_count(self) -> int:
        """Return the total number of documents in the catalog."""
        cat = self._cat
        row = cat._db.execute("SELECT COUNT(*) FROM documents").fetchone()
        return row[0] if row else 0

    def all_documents(
        self, limit: int = 0, *, content_type: str = "",
    ) -> list[CatalogEntry]:
        """Return all catalog entries. limit=0 means unlimited.

        GH #568: ``content_type`` pushes the filter into the SQL
        ``WHERE`` clause so pagination works correctly when the
        requested content_type is small-cardinality. Pre-fix the
        CLI ``nx catalog list --type rdr`` filtered Python-side
        AFTER ``LIMIT/OFFSET`` and returned empty whenever the
        pre-LIMIT slice held no matching rows -- e.g. 15K-entry
        catalog with only 2 rdr rows: ``--type rdr -n 3`` got 0.
        Mirrors PR #533's fix for the MCP ``catalog_list`` surface.
        """
        cat = self._cat
        from nexus.catalog.catalog import CatalogEntry
        sql = (
            "SELECT tumbler, title, author, year, content_type, file_path, "
            "corpus, physical_collection, chunk_count, head_hash, indexed_at, metadata, source_mtime, source_uri "
            "FROM documents"
        )
        params: tuple = ()
        if content_type:
            sql += " WHERE content_type = ?"
            params = (content_type,)
        if limit > 0:
            sql += f" LIMIT {limit}"
        rows = cat._db.execute(sql, params).fetchall()
        return [
            CatalogEntry(
                tumbler=Tumbler.parse(r[0]), title=r[1], author=r[2], year=r[3],
                content_type=r[4], file_path=r[5], corpus=r[6],
                physical_collection=r[7], chunk_count=r[8], head_hash=r[9],
                indexed_at=r[10], meta=json.loads(r[11]) if r[11] else {},
                source_mtime=r[12] or 0.0,
                source_uri=r[13] or "",
            )
            for r in rows
        ]

    def by_doc_id(self, doc_id: str) -> CatalogEntry | None:
        """Look up catalog entry by T3 doc_id stored in meta.doc_id."""
        cat = self._cat
        from nexus.catalog.catalog import CatalogEntry
        row = cat._db.execute(
            "SELECT tumbler, title, author, year, content_type, file_path, "
            "corpus, physical_collection, chunk_count, head_hash, indexed_at, metadata, source_mtime, source_uri "
            "FROM documents WHERE json_extract(metadata, '$.doc_id') = ?",
            (doc_id,),
        ).fetchone()
        if not row:
            return None
        return CatalogEntry(
            tumbler=Tumbler.parse(row[0]),
            title=row[1],
            author=row[2],
            year=row[3],
            content_type=row[4],
            file_path=row[5],
            corpus=row[6],
            physical_collection=row[7],
            chunk_count=row[8],
            head_hash=row[9],
            indexed_at=row[10],
            meta=json.loads(row[11]) if row[11] else {},
            source_mtime=row[12] or 0.0,
            source_uri=row[13] or "",
        )

