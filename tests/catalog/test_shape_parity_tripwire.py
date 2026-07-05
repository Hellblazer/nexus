# SPDX-License-Identifier: AGPL-3.0-or-later
"""nexus-8y1tm: mechanized runtime return-SHAPE tripwire for HttpCatalogClient.

The h8rf6.3 incident class: a client method returning a runtime shape that
differs from the local ``Catalog``'s (flat list vs dict, ``None`` vs ``""``,
bool vs int) passes the annotation-comparison fidelity test and every
MagicMock-consumer test, then breaks a production consumer silently.
``test_docs_for_chashes_shape_conformance.py`` pinned ONE method; this module
generalizes the paired-assertion pattern across the shared public surface
and — the mechanized part — FAILS when a shared method is neither registered
here nor excluded with a documented reason. A new/changed method cannot ship
without a parity entry.

Three pieces:

1. ``shape(value)`` — recursive runtime-shape descriptor. Exact types
   (``bool`` is not ``int``), dataclass class names, container element
   shapes, ``None`` distinct from ``""``.
2. ``REGISTRY`` — per-method parity entries: call args + which side of the
   seeded state they exercise. The harness calls the seeded local
   ``Catalog`` and the real ``HttpCatalogClient`` against the wire-faithful
   fake server, then asserts ``shape(local) == shape(http)``.
3. ``EXCLUSIONS`` — shared methods deliberately not parity-tested, each with
   a reason string. Auditable, never silent.

Vacuity guard: entries must produce a NON-EMPTY result on both sides unless
``empty_ok=True`` (with the reason in the entry's comment) — empty-vs-empty
parity proves nothing.
"""
from __future__ import annotations

import dataclasses
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Callable

import inspect

import pytest

from nexus.catalog.catalog import Catalog, Tumbler
from nexus.catalog.http_catalog_client import HttpCatalogClient
from tests.catalog.test_http_catalog_client import (
    CHUNK_SHA_A,
    CHUNK_SHA_B,
    start_fake_server,
)


# ── 1. shape descriptor ──────────────────────────────────────────────────────


def shape(value: Any, *, _depth: int = 0) -> Any:
    """Recursive runtime-shape descriptor for return-value parity.

    - exact type() (so bool != int, and None != "")
    - dataclasses collapse to ("dataclass", ClassName) — field-level drift
      is a CatalogEntry/QueueRow definition change, caught elsewhere
    - containers descend into elements; heterogeneous element shapes are
      preserved as a sorted set of reprs so ordering differences don't flake

    KNOWN LIMITATION (critique, 2026-07-04): container CARDINALITY is
    deliberately discarded (a 1-item and a 100-item list of the same
    element shape are identical) — this harness catches type-level drift
    (the h8rf6.3 incident class), not count drift; per-method tests in
    test_http_catalog_client.py own cardinality.
    """
    if _depth > 6:  # cycles / pathological nesting fail loud
        raise AssertionError("shape(): nesting too deep")
    if value is None:
        return "None"
    if dataclasses.is_dataclass(value) and not isinstance(value, type):
        return ("dataclass", type(value).__name__)
    t = type(value)
    if t in (bool, int, float, str, bytes):
        return t.__name__
    if t in (list, tuple, set, frozenset):
        elems = {repr(shape(v, _depth=_depth + 1)) for v in value}
        return (t.__name__, tuple(sorted(elems)))
    if t is dict:
        entries = {
            repr((shape(k, _depth=_depth + 1), shape(v, _depth=_depth + 1)))
            for k, v in value.items()
        }
        return ("dict", tuple(sorted(entries)))
    # Tumbler and other value objects: exact class name.
    return ("object", t.__name__)


def _is_empty(value: Any) -> bool:
    if isinstance(value, bool):
        return False  # False is a legitimate result, not vacuity (bool==0 trap)
    if value is None or value == "" or value == 0:
        return True
    if isinstance(value, (list, tuple, set, dict)):
        return len(value) == 0
    return False


# ── 2. registry ──────────────────────────────────────────────────────────────


@dataclass
class Parity:
    """One method's parity entry.

    ``args``/``kwargs`` may be callables taking the seeded ``Seeds``
    context (so entries can reference tumblers created at seed time).
    ``empty_ok`` requires a justification in a trailing comment.
    """

    method: str
    args: tuple = ()
    kwargs: dict = field(default_factory=dict)
    empty_ok: bool = False
    #: REQUIRED when empty_ok=True — machine-checked by the completeness gate.
    empty_reason: str = ""


@dataclass
class Seeds:
    """Identifiers created by the seed step, shared by both sides."""

    owner: Any = None                 # Tumbler of the seeded repo owner ("1.1")
    curator_owner: Any = None         # Tumbler of a curator-type owner (no repo_root)
    doc_a: Any = None                 # Tumbler of doc A (code, has manifest, linked -> B)
    doc_b: Any = None                 # Tumbler of doc B (prose, linked from A)
    # Isolated targets for destructive / void operations — kept SEPARATE from
    # doc_a/doc_b/collection so read-only REGISTRY entries stay valid
    # regardless of parametrize execution order.
    doc_c_orphan: Any = None          # zero links -> orphaned_docs
    doc_abs_path: Any = None          # absolute file_path -> docs_with_absolute_paths
    doc_delete: Any = None            # delete_document target
    doc_purge: Any = None             # purge_manifest_for_doc target (has its own manifest)
    doc_atomic: Any = None            # atomic_manifest_replace target
    doc_write_manifest: Any = None    # write_manifest target
    doc_alias_src: Any = None         # set_alias source (aliased -> doc_a)
    doc_update: Any = None            # update() target
    doc_update_collection: Any = None       # update_document_collection target
    doc_update_collection_batch: Any = None # update_documents_collection_batch target
    doc_append: Any = None                  # append_manifest_chunks/resync target (mutated additively)
    doc_unlink_a: Any = None
    doc_unlink_b: Any = None
    doc_rename_src: Any = None        # rename_collection target doc

    collection: str = "code__nexus-test__voyage-code-3__v1"
    docs_collection: str = "docs__nexus-test__voyage-context-3__v1"
    legacy_collection: str = "legacy-collection-name-8y1tm"       # non-conformant -> legacy=True
    delete_proj_collection: str = "code__delproj-8y1tm__v1"
    rename_old_collection: str = "code__renameold-8y1tm__v1"
    rename_new_collection: str = "code__renamenew-8y1tm__v1"
    supersede_old_collection: str = "code__supold-8y1tm__v1"
    supersede_new_collection: str = "code__supnew-8y1tm__v1"

    repo_hash: str = "8y1tmhash"
    # nexus-8y1tm: these MUST be the shared fixture chashes from
    # test_http_catalog_client.py, not arbitrary literals — the fake's
    # /manifest/get_many route echoes back CHASH_A unconditionally
    # (test_http_catalog_client.FakeCatalogHandler.do_POST), so
    # docs_for_chashes' second-round-trip intersection only produces a
    # non-empty result when the requested chash matches that literal.
    chash_a: str = CHUNK_SHA_A
    chash_b: str = CHUNK_SHA_B
    file_a: str = "src/alpha.py"
    file_b: str = "docs/beta.md"
    # nexus-8y1tm: these two literals MUST match FakeCatalogHandler._entry_dict()'s
    # defaults (tests/catalog/test_http_catalog_client.py) — by_file_path /
    # by_source_uri / find_by_file_path / resolve_path exact-match filter
    # client-side against whatever /list or /show returns, and the fake has no
    # stateful backing store, so the fixture and the fake must agree on the
    # literal value up front.
    source_uri_a: str = "file:///tmp/nexus-test/alpha.py"
    corpus_a: str = "test-corpus-8y1tm"


REGISTRY: list[Parity] = [
    # ── docs cluster ─────────────────────────────────────────────────────
    Parity("by_owner", args=(lambda s: s.owner,)),
    Parity("by_file_path", args=(lambda s: s.owner, lambda s: s.file_a)),
    Parity("by_content_type", args=("code",)),
    Parity("resolve", args=(lambda s: s.doc_a,)),
    Parity("docs_for_chashes", args=(lambda s: [s.chash_a],)),
    Parity(
        "lookup_doc_id_by_collection_and_path",
        args=(lambda s: s.collection, lambda s: s.file_a),
    ),
    Parity("all_documents"),
    Parity(
        "descendants", args=(lambda s: str(s.owner),),
        # nexus-u26b4: was KNOWN DRIFT (local raw-row passthrough vs HTTP's
        # Java-normalized /list rows) — local catalog_docs.py now normalizes
        # metadata to a parsed dict + full bib_* set, matching the HTTP side.
    ),
    Parity("by_corpus", args=(lambda s: s.corpus_a,)),
    Parity("by_doc_id", args=(lambda s: str(s.doc_a),)),
    Parity("by_source_uri", args=(lambda s: s.source_uri_a,)),
    Parity("find", args=("alpha",)),
    Parity("find_by_file_path", args=(lambda s: s.file_a,)),
    Parity("list_by_collection", args=(lambda s: s.collection,)),
    Parity("resolve_many", args=(lambda s: [str(s.doc_a)],)),
    Parity("resolve_alias", args=(lambda s: s.doc_a,)),
    Parity("resolve_path", args=(lambda s: s.doc_a,)),
    Parity(
        "resolve_chunk",
        args=(lambda s: Tumbler(segments=(*s.doc_a.segments, 0)),),
        # nexus-gc2ze: real /resolve_chunk wire route now exists. doc_a's
        # chunk_count is 0 (unset — write_manifest doesn't touch
        # documents.chunk_count; only resync_chunk_count_cache does), so the
        # local bounds check short-circuits (0 is falsy) and any chunk index
        # resolves — chunk index 0 is the simplest valid chunk address.
    ),
    Parity("doc_count"),
    Parity(
        "register",
        args=(lambda s: s.owner, "Registered Doc 8y1tm"),
        kwargs={"content_type": "code", "file_path": "src/registered_8y1tm.py"},
    ),
    Parity(
        # nexus-9dvqy: batch register returns list[Tumbler] aligned 1:1 with docs.
        "register_many",
        args=(lambda s: s.owner, lambda s: [
            {"title": "Batch Doc 9dvqy", "content_type": "code",
             "file_path": "src/batch_9dvqy.py"},
        ]),
    ),
    Parity(
        "update", args=(lambda s: s.doc_update,), kwargs={"title": "Updated Title 8y1tm"},
        empty_ok=True, empty_reason="void write: update() returns None on both sides",
    ),
    Parity("delete_document", args=(lambda s: s.doc_delete,)),
    Parity(
        "set_alias", args=(lambda s: s.doc_alias_src, lambda s: s.doc_a),
        empty_ok=True, empty_reason="void write: set_alias() returns None on both sides",
    ),

    # ── owners cluster ───────────────────────────────────────────────────
    Parity("owner_for_repo", args=(lambda s: s.repo_hash,)),
    Parity("list_owners"),
    Parity(
        "register_owner", args=("nexus-test-owner2-8y1tm", "repo"),
        kwargs={"repo_hash": "anotherhash8y1tm"},
    ),
    Parity(
        "ensure_owner_for_repo",
        args=(lambda s: Path("/tmp/nexus-test-ensure-8y1tm"),),
        kwargs={"repo_name": "nexus-ensure-8y1tm"},
    ),
    Parity("owner_tumblers_by_name", args=("nexus-test",)),
    Parity("curator_owner_tumbler_by_name", args=("nexus-curator-8y1tm",)),
    Parity("get_owner_by_prefix", args=(lambda s: str(s.owner),)),
    Parity("list_owners_by_type", args=("repo",)),
    Parity(
        "set_owner_head_hash", args=(lambda s: s.owner, "deadbeef8y1tm"),
    ),
    Parity("owners_with_roots"),
    Parity("distinct_doc_collections"),
    Parity("docs_with_absolute_paths"),
    Parity("orphaned_docs"),
    Parity("collection_doc_counts"),
    Parity("coverage_by_content_type", args=(lambda s: str(s.owner),)),
    Parity("get_collection_owner_root", args=(lambda s: s.collection,)),
    Parity("chunk_counts_for_docs", args=(lambda s: [str(s.doc_a)],)),
    Parity("links_from_batch", args=(lambda s: [str(s.doc_a)],)),
    Parity("stats"),
    Parity(
        "collection_health_meta", args=(lambda s: s.collection,),
        # nexus-u26b4: was KNOWN DRIFT (HttpCatalogClient silently dropped
        # 'stale_source_ratio') — now carried through on both sides.
    ),

    # ── links cluster ────────────────────────────────────────────────────
    Parity("links_from", args=(lambda s: s.doc_a,)),
    Parity(
        "links_to", args=(lambda s: s.doc_b,),
        # nexus-u26b4: was excluded (test-infra gap, not a wire-shape bug) —
        # FakeCatalogHandler's /links direction=in|both branch was hardcoded
        # to links_to=[]; now populates a real inbound row like direction=out
        # already did.
    ),
    Parity(
        "validate_link", args=(lambda s: s.doc_a, lambda s: s.doc_b, "cites"),
        # nexus-u26b4: was KNOWN DRIFT (HttpCatalogClient returned bool, local
        # returns list[str]) — doc_a->doc_b "cites" is already seeded, so both
        # sides report the "duplicate" validation error (non-empty list[str]).
    ),
    Parity("link_query", kwargs={"link_type": "cites"}),
    Parity(
        "link", args=("9.7.1", "9.7.2", "test-link", "parity-test"),
        kwargs={"allow_dangling": True},
    ),
    Parity(
        "link_if_absent",
        args=("9.9.9", "9.9.8", "test-link-if-absent", "parity-test"),
        kwargs={"allow_dangling": True},
        # FakeCatalogHandler.link_absent_from == "9.9.9": the fake's
        # /link_query reports NO existing link for this from_tumbler, driving
        # the same "insert" branch the local (genuinely new) pair takes.
    ),
    Parity(
        "unlink",
        args=(lambda s: str(s.doc_unlink_a), lambda s: str(s.doc_unlink_b)),
        kwargs={"link_type": "relates"},
    ),
    Parity(
        "bulk_unlink", kwargs={"link_type": "cites", "dry_run": True},
        # dry_run=True: read-only count via link_query, doesn't touch the
        # doc_a -> doc_b "cites" link other entries depend on.
    ),
    Parity(
        "graph", args=(lambda s: s.doc_a,),
        # nexus-u26b4: was KNOWN DRIFT (h8rf6.3 incident class) — local
        # returns {"nodes": list[CatalogEntry], "edges": list[CatalogLink]};
        # HttpCatalogClient.graph() returned the RAW wire dict unconverted.
        # Both sides always return the 2-key {"nodes","edges"} dict, so the
        # vacuity guard (len(dict) == 2) passes structurally; the shape
        # assertion is what pins the node/edge dataclass conversion.
    ),
    Parity(
        "graph_many", args=(lambda s: [s.doc_a, s.doc_b],),
        # nexus-u26b4: same fix as graph() (see that entry) applied to the
        # multi-seed traversal.
    ),

    # ── collections cluster ──────────────────────────────────────────────
    Parity("is_legacy_collection", args=(lambda s: s.legacy_collection,)),
    Parity(
        "get_collection", args=(lambda s: s.collection,),
        # nexus-u26b4: was KNOWN DRIFT ('legacy_grandfathered' int-vs-bool) —
        # HttpCatalogClient now coerces to bool like local's
        # _row_to_collection_dict does.
    ),
    Parity(
        "list_collections",
        # nexus-u26b4: same 'legacy_grandfathered' int-vs-bool fix as
        # get_collection() (see that entry).
    ),
    Parity(
        "collections_by_owner", args=(lambda s: str(s.owner),),
        # nexus-u26b4: inherits the get_collection()/list_collections() fix —
        # filters the now-coerced list_collections() result client-side.
    ),
    Parity(
        "collection_for", args=("code", lambda s: s.owner, "voyage-code-3"),
    ),
    Parity(
        "register_collection", args=(lambda s: s.collection,),
        kwargs={
            "content_type": "code", "owner_id": "1.1",
            "embedding_model": "voyage-code-3", "model_version": "v1",
        },
        empty_ok=True, empty_reason="void write: register_collection() returns None on both sides",
    ),
    Parity(
        "delete_collection_projection", args=(lambda s: s.delete_proj_collection,),
        kwargs={"reason": "parity-test"},
        empty_ok=True,
        empty_reason=(
            "HttpCatalogClient ALWAYS returns False (hard delete not "
            "implemented service-side, guard+track bead nexus-gmiaf.24 per "
            "its own docstring); shape (bool) still conforms to local's "
            "real True/False — only the HTTP-side value is falsy by design"
        ),
    ),
    Parity(
        "supersede_collection",
        args=(lambda s: s.supersede_old_collection, lambda s: s.supersede_new_collection),
        empty_ok=True, empty_reason="void write: returns None on both sides",
    ),
    Parity(
        "rename_collection",
        args=(lambda s: s.rename_old_collection, lambda s: s.rename_new_collection),
    ),
    Parity(
        "update_document_collection",
        args=(lambda s: str(s.doc_update_collection), "code__updated-8y1tm__v1"),
    ),
    Parity(
        "update_documents_collection_batch",
        args=(lambda s: [(str(s.doc_update_collection_batch), "code__updated2-8y1tm__v1")],),
    ),

    # ── manifest cluster ─────────────────────────────────────────────────
    Parity("chashes_for_collection", args=(lambda s: s.collection,)),
    Parity("get_manifest", args=(lambda s: str(s.doc_a),)),
    Parity("get_manifests", args=(lambda s: [str(s.doc_a)],)),
    Parity("get_chunk_chashes", args=(lambda s: str(s.doc_a),)),
    Parity(
        "write_manifest",
        args=(lambda s: str(s.doc_write_manifest), [{"chash": "e" * 64, "position": 0}]),
        empty_ok=True, empty_reason="void write: returns None on both sides",
    ),
    Parity(
        "append_manifest_chunks",
        args=(lambda s: str(s.doc_append), [{"chash": "f" * 64, "position": 1}]),
        empty_ok=True, empty_reason="void write: returns None on both sides",
    ),
    Parity(
        "atomic_manifest_replace",
        args=(lambda s: str(s.doc_atomic), [{"chash": "a" * 64, "position": 0}]),
        empty_ok=True, empty_reason="void write: returns None on both sides",
    ),
    Parity(
        "purge_manifest_for_doc", args=(lambda s: str(s.doc_purge),),
        empty_ok=True, empty_reason="void write: returns None on both sides",
    ),
    Parity(
        "resync_chunk_count_cache", args=(lambda s: str(s.doc_append),),
        empty_ok=True, empty_reason="void write: returns None on both sides",
    ),
]


#: Shared methods deliberately NOT parity-tested. Every entry carries a
#: reason; the completeness gate treats this list as the only escape hatch.
EXCLUSIONS: dict[str, str] = {
    "close": "lifecycle plumbing; no wire round-trip, nothing to compare",
    "is_initialized": "local filesystem probe vs service liveness — semantically different by design (RDR-168)",
    # ── pure local-mode semantics (catalog-git-DECISION Option C): Postgres
    # is the sole write authority in service mode; these are SQLite/git-only
    # artifacts with NO wire equivalent. HttpCatalogClient deliberately
    # raises NotImplementedError / returns a fixed empty value for every one
    # of these — there is no shape to compare against a real service route.
    "rebuild": "SQLite-projection rebuild; HttpCatalogClient.rebuild() raises NotImplementedError by design (catalog-git-DECISION Option C) — no wire equivalent",
    "defrag": "JSONL compaction; HttpCatalogClient.defrag() raises NotImplementedError by design (catalog-git-DECISION Option C) — no wire equivalent",
    "compact": "JSONL compaction; HttpCatalogClient.compact() raises NotImplementedError by design (catalog-git-DECISION Option C) — no wire equivalent",
    "sync": "git commit operation; HttpCatalogClient.sync() raises NotImplementedError by design — no wire equivalent",
    "pull": "git pull operation; HttpCatalogClient.pull() raises NotImplementedError by design — no wire equivalent",
    "jsonl_paths": "pure local event-log filesystem paths; HttpCatalogClient returns a fixed empty tuple — no wire equivalent",
    "mtime_paths": "pure local event-log filesystem paths; HttpCatalogClient returns a fixed empty tuple — no wire equivalent",
    # ── documented HttpCatalogClient stubs (not wire-shape bugs): the
    # docstrings on these methods say outright they are unsupported in
    # service mode. Comparing local's real result against a permanently
    # empty/None stub is not a meaningful shape assertion — it would just
    # encode "the stub is a stub" as a passing test.
    "link_audit": "HttpCatalogClient.link_audit() unconditionally returns {} per its own docstring ('not supported in initial service-mode implementation') — documented capability gap, not a wire-shape bug",
    "resolve_span_text": "HttpCatalogClient.resolve_span_text() unconditionally returns None per its own docstring ('not supported in initial service-mode implementation') — documented capability gap, not a wire-shape bug",
    # ── requires a live T3 (ChromaDB) handle the catalog-only parity
    # harness does not stand up: both resolve local chunk text via a real
    # ClientAPI.get_collection(...).get(where=...) call (catalog_spans.py);
    # HttpCatalogClient's t3 param is accepted-but-unused (resolution is
    # server-side). Still excluded from THIS metadata-only REGISTRY (it has
    # no T3 handle to exercise), but real shape parity is covered by a
    # dedicated T3-backed leg — see test_shape_parity_t3_leg.py (nexus-oq0tk),
    # which wires a real chromadb.EphemeralClient alongside a real ChashIndex
    # and asserts shape(local) == shape(http) with the same shape() helper.
    "resolve_chash": "parity covered by test_shape_parity_t3_leg.py (nexus-oq0tk) — requires a live T3 ClientAPI (catalog_spans.resolve_chash_globally queries a real chroma collection), out of scope for this catalog-metadata-only REGISTRY, but shape-parity is real and green in the T3-backed leg, not a wire-shape gap",
    "resolve_span": "parity covered by test_shape_parity_t3_leg.py (nexus-oq0tk) — requires a live T3 ClientAPI (catalog_spans.resolve_span_in_t3 queries a real chroma collection), out of scope for this catalog-metadata-only REGISTRY, but shape-parity is real and green in the T3-backed leg, not a wire-shape gap",
    # ── fragile / redundant coupling
    "collection_for_repo": "requires real git-repo identity resolution (_repo_identity/_resolve_main_repo) chained through owner_for_repo(repo_hash); collection_for (registered) already exercises the same /collections/for_tuple wire route and CollectionName rendering without coupling the harness to filesystem/git-subprocess behavior",
    # ── documented capability gap (bead follow-up, not a wire-shape bug):
    # nexus-u26b4 fixed the other 5 KNOWN DRIFT findings this bead's parent
    # (nexus-8y1tm) recorded (validate_link, descendants, collection_health_meta,
    # get_collection/list_collections/collections_by_owner, graph/graph_many —
    # all moved to REGISTRY below) plus the links_to test-infra gap.
    # nexus-gc2ze closed the last one (resolve_chunk) — see REGISTRY above.
}


# ── seeding + fixtures ────────────────────────────────────────────────────────


def _seed_local(cat: Catalog, s: Seeds) -> None:
    """Seed the local catalog with the shared state both sides mirror."""
    cat.register_owner(
        "nexus-test", "repo", repo_hash=s.repo_hash, repo_root="/tmp/nexus-test",
    )
    owner_t = cat.owner_for_repo(s.repo_hash)
    s.owner = owner_t
    # Curator owners have no repo_root and skip the cross-project source_uri
    # guard (_check_source_uri_in_repo_root) — used below for doc_abs_path,
    # whose absolute file_path would otherwise be rejected as "outside the
    # owner's repo_root".
    s.curator_owner = cat.register_owner("nexus-curator-8y1tm", "curator")

    s.doc_a = cat.register(
        owner_t, "alpha.py", content_type="code", file_path=s.file_a,
        physical_collection=s.collection, head_hash="h1",
        # nexus-8y1tm: by_doc_id/resolve_many key on json_extract(metadata,
        # '$.doc_id') (a legacy T3-doc_id field), NOT the tumbler — "1.1.1" is
        # the deterministic tumbler this doc gets as the FIRST document
        # registered under the FIRST owner of a fresh Catalog (mirroring the
        # same "1.1.1" convention test_http_catalog_client.py's fake hardcodes).
        meta={"content_hash": "f" * 64, "doc_id": "1.1.1"},
        source_uri=s.source_uri_a, corpus=s.corpus_a,
    )
    s.doc_b = cat.register(
        owner_t, "beta.md", content_type="prose", file_path=s.file_b,
        physical_collection=s.docs_collection, head_hash="h1",
    )
    cat.link(s.doc_a, s.doc_b, "cites", created_by="seed")
    cat.write_manifest(str(s.doc_a), [
        {"chash": s.chash_a, "position": 0},
        {"chash": s.chash_b, "position": 1},
    ])

    # Collection projections so collection-scoped lookups (get_collection,
    # get_collection_owner_root, collections_by_owner, is_legacy_collection)
    # have a real row to resolve.
    cat.register_collection(
        s.collection, content_type="code", owner_id=str(owner_t),
        embedding_model="voyage-code-3", model_version="v1",
    )
    cat.register_collection(s.legacy_collection)  # non-conformant name -> legacy=True
    cat.register_collection(
        s.delete_proj_collection, content_type="code", owner_id=str(owner_t),
        embedding_model="voyage-code-3", model_version="v1",
    )
    cat.register_collection(
        s.supersede_old_collection, content_type="code", owner_id=str(owner_t),
        embedding_model="voyage-code-3", model_version="v1",
    )
    # supersede_collection() requires new_name to ALREADY be registered — a
    # dangling superseded_by pointer is rejected with ValueError.
    cat.register_collection(
        s.supersede_new_collection, content_type="code", owner_id=str(owner_t),
        embedding_model="voyage-code-3", model_version="v1",
    )

    # Isolated targets for destructive / void operations — deliberately
    # SEPARATE from doc_a/doc_b/collection so read-only REGISTRY entries stay
    # valid regardless of pytest.mark.parametrize execution order.
    s.doc_c_orphan = cat.register(
        owner_t, "orphan.py", content_type="code", file_path="src/orphan_8y1tm.py",
    )
    s.doc_delete = cat.register(
        owner_t, "delete_me.py", content_type="code", file_path="src/delete_me_8y1tm.py",
    )
    s.doc_purge = cat.register(
        owner_t, "purge_target.py", content_type="code", file_path="src/purge_target_8y1tm.py",
    )
    cat.write_manifest(str(s.doc_purge), [{"chash": "d" * 64, "position": 0}])
    s.doc_atomic = cat.register(
        owner_t, "atomic_target.py", content_type="code", file_path="src/atomic_target_8y1tm.py",
    )
    s.doc_write_manifest = cat.register(
        owner_t, "write_manifest_target.py", content_type="code",
        file_path="src/write_manifest_target_8y1tm.py",
    )
    s.doc_alias_src = cat.register(
        owner_t, "alias_src.py", content_type="code", file_path="src/alias_src_8y1tm.py",
    )
    s.doc_update = cat.register(
        owner_t, "update_target.py", content_type="code", file_path="src/update_target_8y1tm.py",
    )
    s.doc_update_collection = cat.register(
        owner_t, "update_collection_target.py", content_type="code",
        file_path="src/update_collection_target_8y1tm.py",
        physical_collection="code__preupdate-8y1tm__v1",
    )
    s.doc_update_collection_batch = cat.register(
        owner_t, "update_collection_batch_target.py", content_type="code",
        file_path="src/update_collection_batch_target_8y1tm.py",
        physical_collection="code__preupdate-batch-8y1tm__v1",
    )
    s.doc_append = cat.register(
        owner_t, "append_target.py", content_type="code",
        file_path="src/append_target_8y1tm.py",
    )
    cat.write_manifest(str(s.doc_append), [{"chash": "9" * 64, "position": 0}])
    s.doc_unlink_a = cat.register(
        owner_t, "unlink_a.py", content_type="code", file_path="src/unlink_a_8y1tm.py",
    )
    s.doc_unlink_b = cat.register(
        owner_t, "unlink_b.py", content_type="code", file_path="src/unlink_b_8y1tm.py",
    )
    cat.link(s.doc_unlink_a, s.doc_unlink_b, "relates", created_by="seed")
    s.doc_rename_src = cat.register(
        owner_t, "rename_src.py", content_type="code", file_path="src/rename_src_8y1tm.py",
        physical_collection=s.rename_old_collection,
    )
    # Curator owner (no repo_root) skips the cross-project source_uri guard,
    # so an absolute file_path is safe to register here.
    s.doc_abs_path = cat.register(
        s.curator_owner, "abs_path_doc.txt", content_type="doc",
        file_path="/abs/path/doc-8y1tm.txt",
    )


# ── fixtures ──────────────────────────────────────────────────────────────────


@pytest.fixture(scope="module")
def local_catalog(tmp_path_factory: pytest.TempPathFactory):
    base = tmp_path_factory.mktemp("nexus-8y1tm-catalog")
    catalog_dir = base / "catalog"
    catalog_dir.mkdir()
    db_path = base / "catalog.sqlite"
    cat = Catalog(catalog_dir=catalog_dir, db_path=db_path)
    yield cat
    cat.close()


@pytest.fixture(scope="module")
def fake_server_url():
    server, url = start_fake_server()
    yield url
    server.shutdown()


@pytest.fixture(scope="module")
def http_client(fake_server_url: str):
    with HttpCatalogClient(base_url=fake_server_url, _token="parity-test-tok") as c:
        yield c


@pytest.fixture(scope="module")
def seeds(local_catalog: Catalog) -> Seeds:
    s = Seeds()
    _seed_local(local_catalog, s)
    return s


def _resolve(value: Any, s: Seeds) -> Any:
    return value(s) if callable(value) and not isinstance(value, type) else value


def _resolve_args(entry: Parity, s: Seeds) -> tuple[tuple, dict]:
    args = tuple(_resolve(a, s) for a in entry.args)
    kwargs = {k: _resolve(v, s) for k, v in entry.kwargs.items()}
    return args, kwargs


# ── parity harness ─────────────────────────────────────────────────────────


@pytest.mark.parametrize("entry", REGISTRY, ids=lambda p: p.method)
def test_shape_parity(
    entry: Parity, local_catalog: Catalog, http_client: HttpCatalogClient, seeds: Seeds,
) -> None:
    """Call the same method+args on the seeded local Catalog and the real
    HttpCatalogClient (over the wire-faithful fake); assert identical
    runtime shape, and (unless empty_ok) that neither side is vacuously
    empty."""
    args, kwargs = _resolve_args(entry, seeds)
    local_result = getattr(local_catalog, entry.method)(*args, **kwargs)
    http_result = getattr(http_client, entry.method)(*args, **kwargs)

    local_shape = shape(local_result)
    http_shape = shape(http_result)
    assert local_shape == http_shape, (
        f"{entry.method}: shape mismatch — local={local_shape!r} http={http_shape!r} "
        f"(local value: {local_result!r}, http value: {http_result!r})"
    )
    if not entry.empty_ok:
        assert not _is_empty(local_result), (
            f"{entry.method}: local result is vacuously empty — "
            f"strengthen the seed fixture or mark empty_ok=True with a reason"
        )
        assert not _is_empty(http_result), (
            f"{entry.method}: http result is vacuously empty — "
            f"strengthen the FakeCatalogHandler route or mark empty_ok=True with a reason"
        )


# ── 3. completeness gate ─────────────────────────────────────────────────────


def _shared_public_surface() -> set[str]:
    def pub(cls: type) -> set[str]:
        return {
            n for n, m in inspect.getmembers(cls, callable)
            if not n.startswith("_")
        }

    return pub(HttpCatalogClient) & pub(Catalog)


def test_every_shared_method_is_registered_or_excluded() -> None:
    """The mechanized gate: a shared public method with neither a parity
    entry nor a documented exclusion fails CI. This is what makes shape
    drift on a NEW method impossible to ship silently (nexus-8y1tm)."""
    shared = _shared_public_surface()
    registered = {p.method for p in REGISTRY}
    excluded = set(EXCLUSIONS)

    unknown_registered = registered - shared
    assert not unknown_registered, (
        f"REGISTRY entries for methods not on the shared surface: "
        f"{sorted(unknown_registered)}"
    )
    overlap = registered & excluded
    assert not overlap, f"methods both registered and excluded: {sorted(overlap)}"

    lazy = {k: v for k, v in EXCLUSIONS.items() if len(v.strip()) < 20}
    assert not lazy, (
        f"EXCLUSIONS entries with trivial reasons (gate-gaming guard): {lazy}"
    )
    missing_reason = [
        p.method for p in REGISTRY if p.empty_ok and not p.empty_reason.strip()
    ]
    assert not missing_reason, (
        f"empty_ok=True entries without empty_reason: {missing_reason}"
    )

    uncovered = shared - registered - excluded
    assert not uncovered, (
        f"{len(uncovered)} shared Catalog/HttpCatalogClient methods have "
        f"neither a shape-parity entry nor a documented exclusion: "
        f"{sorted(uncovered)}\n"
        f"Add a Parity entry to REGISTRY (preferred) or an EXCLUSIONS entry "
        f"with a reason."
    )


def test_to_entry_covers_every_catalog_entry_field() -> None:
    """Review suggestion (2026-07-04): shape() collapses dataclasses to
    their class name, and _to_entry's ``d.get(field) or default`` pattern
    means a NEW CatalogEntry field with a default that _to_entry forgets
    stays silently defaulted on the HTTP side — the h8rf6.3 class one
    layer down, invisible to the parity harness. Pin _to_entry's source
    against the CatalogEntry field set by reflection."""
    import dataclasses as _dc

    from nexus.catalog import http_catalog_client as _hcc
    from nexus.catalog.catalog import CatalogEntry

    src = inspect.getsource(_hcc._to_entry)
    missing = [
        f.name for f in _dc.fields(CatalogEntry)
        if f"{f.name}=" not in src
    ]
    assert not missing, (
        f"CatalogEntry fields not populated by HttpCatalogClient._to_entry: "
        f"{missing} — every field must be mapped from the wire dict (or the "
        f"omission documented here)"
    )
