# SPDX-License-Identifier: AGPL-3.0-or-later
"""RDR-168 P2.1 (bead nexus-ja47l): caller-facing catalog interface contract.

`CatalogReader` / `CatalogWriter` are the explicit `typing.Protocol` pair capturing the
CALLER-FACING SUBSET of `Catalog` -- the methods reached by the non-substrate consumer
layer (indexer / doc_indexer / CLI `commands/**` / MCP `mcp/**` / post-store hook
`mcp_infra` / `pipeline_stages` / `search_engine`, including `getattr`-dispatched calls)
PLUS the 19 audited service-mode divergences (RDR-168 Research Finding #1). It is
deliberately NOT all 87 public `Catalog` methods: the 7 pure-internal helpers (`defrag`,
`jsonl_paths`, `mtime_paths`, `purge_manifest_for_doc`, `resolve_chunk`,
`is_legacy_collection`, `validate_link`) are not part of the caller contract and excluded.

WHAT THIS ENCODES. Each Protocol method mirrors the canonical `Catalog` parameter shape
-- parameter NAMES and KINDS (positional-or-keyword / keyword-only / `*args` / `**kwargs`)
-- which is exactly the conformance dimension RDR-168 guards (a service-mode client must
expose every caller-passed parameter by explicit name; a `**kwargs` must not silently
swallow it). Defaults are elided to `...` and full type annotations are intentionally
NOT duplicated here: the canonical signatures live on `Catalog`, and
`tests/catalog/test_catalog_protocol_fidelity.py` asserts each Protocol method's
(name, kind) parameter list matches `Catalog` exactly, so this file cannot drift.

WHY A SPLIT PAIR. It mirrors the read/write factory seam (`make_catalog_reader` /
`make_catalog_writer`, RDR-152): `CatalogWriter`'s method set is exactly the
tooling-enforced write whitelist `CATALOG_WRITE_OPS`; everything else the consumer layer
calls is a read on `CatalogReader`. (`update_document_collection` mutates but is absent
from `CATALOG_WRITE_OPS` -- a latent classification gap tracked for the reconciliation
phase; it sits on `CatalogReader` here to keep `CatalogWriter == CATALOG_WRITE_OPS`.)

The conformance test (`tests/catalog/test_catalog_conformance.py`) parametrizes over this
Protocol pair as the single source of truth for the caller surface.
"""
from __future__ import annotations

from typing import Protocol, runtime_checkable


@runtime_checkable
class CatalogReader(Protocol):
    """Caller-facing read surface of the catalog (canonical = local `Catalog`)."""

    def all_documents(self, limit=..., *, content_type=..., offset=...) -> object:  # canonical DIVERGENT
        ...

    def by_content_type(self, content_type) -> object:  # canonical
        ...

    def by_corpus(self, corpus) -> object:  # canonical
        ...

    def by_doc_id(self, doc_id) -> object:  # canonical
        ...

    def by_file_path(self, owner, file_path) -> object:  # canonical
        ...

    def by_owner(self, owner) -> object:  # canonical
        ...

    def by_source_uri(self, uri) -> object:  # canonical
        ...

    def chashes_for_collection(self, physical_collection) -> object:  # canonical
        ...

    def close(self) -> object:  # canonical
        ...

    def collection_doc_counts(self) -> object:  # canonical
        ...

    def collection_for(self, content_type, owner, embedding_model, *, bump=...) -> object:  # canonical DIVERGENT
        ...

    def collection_for_repo(self, repo, content_type, *, bump=...) -> object:  # canonical DIVERGENT
        ...

    def coverage_by_content_type(self, owner_prefix=...) -> object:  # canonical
        ...

    def curator_owner_tumbler_by_name(self, name) -> object:  # canonical
        ...

    def descendants(self, prefix) -> object:  # canonical
        ...

    def distinct_doc_collections(self) -> object:  # canonical
        ...

    def doc_count(self) -> object:  # canonical
        ...

    def docs_for_chashes(self, chashes) -> object:  # canonical
        ...

    def find(self, query, *, content_type=...) -> object:  # canonical
        ...

    def find_by_file_path(self, file_path) -> object:  # canonical
        ...

    def get_collection(self, name) -> object:  # canonical
        ...

    def get_manifest(self, doc_id) -> object:  # canonical
        ...

    def get_manifests(self, doc_ids) -> object:  # canonical
        ...

    def get_owner_by_prefix(self, tumbler_prefix) -> object:  # canonical
        ...

    def graph(self, tumbler, depth=..., direction=..., link_type=..., link_types=..., include_heuristic=...) -> object:  # canonical DIVERGENT
        ...

    def graph_many(self, seeds, depth=..., direction=..., link_type=..., link_types=..., include_heuristic=...) -> object:  # canonical DIVERGENT
        ...

    def is_initialized(self, catalog_path) -> object:  # canonical DIVERGENT
        ...

    def link_audit(self, *, t3=...) -> object:  # canonical
        ...

    def link_query(self, from_t=..., to_t=..., link_type=..., created_by=..., direction=..., tumbler=..., created_at_before=..., limit=..., offset=...) -> object:  # canonical
        ...

    def links_from(self, tumbler, link_type=..., link_types=...) -> object:  # canonical DIVERGENT
        ...

    def links_to(self, tumbler, link_type=..., link_types=...) -> object:  # canonical DIVERGENT
        ...

    def list_by_collection(self, physical_collection, *, limit=...) -> object:  # canonical DIVERGENT
        ...

    def list_collections(self) -> object:  # canonical
        ...

    def list_owners(self) -> object:  # canonical
        ...

    def lookup_doc_id_by_collection_and_path(self, collection, source_path) -> object:  # canonical DIVERGENT
        ...

    def orphaned_docs(self) -> object:  # canonical
        ...

    def owner_for_repo(self, repo_hash) -> object:  # canonical
        ...

    def owner_tumblers_by_name(self, name) -> object:  # canonical
        ...

    def owners_with_roots(self) -> object:  # canonical
        ...

    def resolve(self, tumbler, *, follow_alias=...) -> object:  # canonical
        ...

    def resolve_chash(self, chash, t3, chash_index, *, prefer_collection=...) -> object:  # canonical DIVERGENT
        ...

    def resolve_many(self, doc_ids) -> object:  # canonical
        ...

    def resolve_span(self, span, physical_collection, t3) -> object:  # canonical DIVERGENT
        ...

    def resolve_span_text(self, tumbler, span) -> object:  # canonical
        ...

    def stats(self) -> object:  # canonical
        ...

    def update_document_collection(self, tumbler, new_collection) -> object:  # canonical DIVERGENT
        ...

@runtime_checkable
class CatalogWriter(Protocol):
    """Caller-facing write surface -- method set is exactly `CATALOG_WRITE_OPS`."""

    def register_owner(self, name, owner_type, *, repo_hash=..., description=..., repo_root=...) -> object:  # canonical
        ...

    def ensure_owner_for_repo(self, repo, *, repo_name=..., description=...) -> object:  # canonical DIVERGENT
        ...

    def register(self, owner, title, *, content_type=..., file_path=..., corpus=..., physical_collection=..., chunk_count=..., head_hash=..., author=..., year=..., meta=..., source_mtime=..., source_uri=...) -> object:  # canonical
        ...

    def update(self, tumbler, **fields) -> object:  # canonical
        ...

    def link(self, from_t, to_t, link_type, created_by, *, from_span=..., to_span=..., allow_dangling=..., **meta) -> object:  # canonical DIVERGENT
        ...

    def link_if_absent(self, from_t, to_t, link_type, created_by, *, from_span=..., to_span=..., allow_dangling=..., **meta) -> object:  # canonical DIVERGENT
        ...

    def unlink(self, from_t, to_t, link_type=...) -> object:  # canonical
        ...

    def delete_document(self, tumbler) -> object:  # canonical
        ...

    def register_collection(self, name, *, content_type=..., owner_id=..., embedding_model=..., model_version=..., display_name=...) -> object:  # canonical
        ...

    def delete_collection_projection(self, name, *, reason) -> object:  # canonical
        ...

    def supersede_collection(self, old_name, new_name, *, reason=...) -> object:  # canonical DIVERGENT
        ...

    def set_owner_head_hash(self, owner, head_hash) -> object:  # canonical
        ...

    def write_manifest(self, doc_id, chunks) -> object:  # canonical
        ...

    def append_manifest_chunks(self, doc_id, chunks) -> object:  # canonical
        ...

    def atomic_manifest_replace(self, doc_id, chunks) -> object:  # canonical
        ...

    def resync_chunk_count_cache(self, doc_id) -> object:  # canonical
        ...

    def rename_collection(self, old, new) -> object:  # canonical
        ...

    def bulk_unlink(self, from_t=..., to_t=..., link_type=..., created_by=..., created_at_before=..., dry_run=...) -> object:  # canonical DIVERGENT
        ...

    def update_documents_collection_batch(self, pairs) -> object:  # canonical DIVERGENT
        ...

    def sync(self, message=...) -> object:  # canonical
        ...

    def pull(self) -> object:  # canonical
        ...

    def compact(self) -> object:  # canonical
        ...
