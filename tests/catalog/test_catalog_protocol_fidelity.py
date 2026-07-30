# SPDX-License-Identifier: AGPL-3.0-or-later
"""RDR-168 P2.1 (bead nexus-ja47l): guards for the caller-facing catalog Protocol pair.

`nexus.catalog.catalog_protocol` declares the `CatalogReader` / `CatalogWriter` subset
that `test_catalog_conformance.py` enumerates. These tests guard two things that, if
wrong, would silently weaken the conformance test:

1. FIDELITY — each Protocol method's (name, kind) parameter list matches the canonical
   `Catalog` exactly, so the Protocol can never drift from the local source of truth.
2. SCOPE HONESTY — the subset hides none of the 19 audited divergences and excludes only
   genuine internal helpers; the writer surface is exactly the tooling-enforced
   `CATALOG_WRITE_OPS`. (This is the silent-scope-reduction guard the RDR gate flagged:
   a Protocol that quietly omitted a breaking method would make the conformance test pass
   for the wrong reason.)
"""
from __future__ import annotations

from collections.abc import Callable

from nexus.catalog.catalog_protocol import CatalogReader, CatalogWriter
from nexus.catalog.catalog_protocol import CATALOG_WRITE_OPS

# The 19 audited service-mode divergences (RDR-168 Research Finding #1: 18 breaking +
# link_if_absent silent). Every one MUST be in the Protocol, or the conformance test
# would never see it.
_DIVERGENT: frozenset[str] = frozenset(
    {
        "all_documents",
        "bulk_unlink",
        "collection_for",
        "collection_for_repo",
        "ensure_owner_for_repo",
        "graph",
        "graph_many",
        "is_initialized",
        "link",
        "link_if_absent",
        "links_from",
        "links_to",
        "list_by_collection",
        "lookup_doc_id_by_collection_and_path",
        "resolve_chash",
        "resolve_span",
        "supersede_collection",
        "update_document_collection",
        "update_documents_collection_batch",
    }
)

# Pure-internal Catalog helpers deliberately kept OUT of the caller-facing contract
# (zero non-substrate references, including getattr-dispatched ones). NOTE: get_manifests
# and resolve_many are NOT here — they are caller-facing via getattr() dispatch in
# search_engine.py and belong on CatalogReader.
_INTERNAL_HELPERS: frozenset[str] = frozenset(
    {
        "defrag",
        "jsonl_paths",
        "mtime_paths",
        "purge_manifest_for_doc",
        "resolve_chunk",
        "is_legacy_collection",
        "validate_link",
    }
)


def _protocol_methods(proto: type) -> dict[str, Callable]:
    return {n: m for n, m in vars(proto).items() if not n.startswith("_") and callable(m)}


_READER = _protocol_methods(CatalogReader)
_WRITER = _protocol_methods(CatalogWriter)
_UNION = {**_READER, **_WRITER}


# ── fidelity ─────────────────────────────────────────────────────────────────────
# The three fidelity tests anchored to the LOCAL ``Catalog`` class
# (test_client_return_types_match_local_typed_returns,
# test_every_protocol_method_exists_on_local_catalog,
# test_protocol_param_names_and_kinds_match_canonical) RETIRED in the same
# commit as the src (nexus-i711w terminal deletion), as this file always
# planned. With the local class gone, ``HttpCatalogClient`` is the sole
# implementation and the canonical signature source; the scope-honesty
# guards below remain the load-bearing checks.


def test_reader_and_writer_are_disjoint() -> None:
    overlap = sorted(set(_READER) & set(_WRITER))
    assert overlap == [], f"a method appears on both Protocols: {overlap}"


# ── scope honesty (the silent-scope-reduction guard) ─────────────────────────────


def test_writer_surface_is_exactly_the_write_whitelist() -> None:
    """CatalogWriter == CATALOG_WRITE_OPS (the tooling-enforced caller-facing writes)."""
    assert set(_WRITER) == set(CATALOG_WRITE_OPS), (
        f"\n  writer-only: {sorted(set(_WRITER) - set(CATALOG_WRITE_OPS))}"
        f"\n  whitelist-only: {sorted(set(CATALOG_WRITE_OPS) - set(_WRITER))}"
    )


def test_no_audited_divergence_is_hidden_from_the_protocol() -> None:
    """Every one of the 19 divergences is in the Protocol — none silently dropped.

    This is the load-bearing scope-honesty assertion: omitting a breaking method here is
    exactly how the conformance test would pass for the wrong reason.
    """
    hidden = sorted(_DIVERGENT - set(_UNION))
    assert hidden == [], f"audited divergences missing from the Protocol (HIDDEN): {hidden}"


def test_internal_helpers_are_excluded() -> None:
    """The contract is the caller-facing subset, not all 87 methods."""
    leaked = sorted(_INTERNAL_HELPERS & set(_UNION))
    assert leaked == [], f"internal helpers leaked into the caller contract: {leaked}"


def test_getattr_dispatched_reads_are_included() -> None:
    """`get_manifests` / `resolve_many` are caller-facing via getattr() in search_engine.

    Regression guard for the Phase 2 review finding: a `.method(` grep misses dynamic
    `getattr(catalog, "method")` dispatch, which nearly excluded these two genuine
    consumer reads as if they were internal helpers.
    """
    for name in ("get_manifests", "resolve_many"):
        assert name in _READER, f"{name} is caller-facing (search_engine getattr) but absent from CatalogReader"
