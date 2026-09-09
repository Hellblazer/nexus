# SPDX-License-Identifier: AGPL-3.0-or-later
"""Phase A pins for RDR-204 Phase 3 funnel slice 2 (nexus-ft04v.22).

This bead rewrites 15 raw collection-name parse sites in ``db/``,
``catalog/`` to call the three ``CollectionName`` helpers
(``collection_content_type``, ``collection_model``, ``collection_owner``)
defined by nexus-ft04v.21 in ``nexus.corpus``. Those helpers are not yet
landed on this tree (Phase A runs before .21 merges) — this file pins the
CURRENT (pre-rewrite) behaviour of every site so Phase B's rewrite can be
checked against it: same sites, same names, same asserted values, run
again unchanged after the rewrite. A rewrite with no such pin is not
reviewable (bead ACCEPTANCE).

Sites covered here, one section per file (docs/rdr/
rdr-204-embedding-profile-and-collection-authority.md, "Implementation
Plan / Phase 3" and "Validation / Testing Strategy"):

- src/nexus/db/http_vector_client.py:836 (``per_collection_chunk_cap``,
  ALREADY pinned by ``tests/db/test_http_vector_client.py::
  test_per_collection_chunk_cap_values`` — referenced, not duplicated)
  and :869 (``_upsert_byte_budget`` — pinned here, no prior test existed).
- src/nexus/db/http_vector_client.py:3680 (``HttpVectorClient.expire``'s
  ``knowledge__`` collection filter — pinned here).
- src/nexus/db/reconcile.py:253 (``_is_same_model_passthrough``), :343
  (``_dim_for_collection``) — both pinned here, no prior direct test
  existed. :585 (the passthrough ``declared_model`` derivation) is
  exercised end-to-end by the existing
  ``tests/migration/test_vector_etl_pg_source.py::
  TestVerifyFillPgSource::test_passthrough_model_end_to_end_stitch_reaches_target``
  round trip — the RDR names "reconciler round-trip green" as the check,
  not a duplicate unit pin.
- src/nexus/db/embed_migrate.py:94 (``_classify`` — pinned here), :262
  (``_default_reindex``'s ``rdr__`` branch dispatch — ALREADY pinned by
  ``tests/test_embed_migrate.py::TestDefaultReindexCounting``), :369
  (``migrate_collection_safe``'s ``corpus`` derivation — pinned here, no
  prior test asserted the derived value).
- src/nexus/catalog/chunk_quarantine.py:58 (``quarantine_collection_name``
  — pinned here for the two-branch split, extending the existing
  integration-level coverage in ``tests/test_rdr191_gc_serverside_prune.py``
  with a pure-function edge-case table).
- src/nexus/catalog/orphan_backfill.py:322 (``_content_type_for_collection``
  — ALREADY pinned by ``tests/test_orphan_backfill.py``; one edge case
  (no ``"__"`` at all) added here since the existing table only covers a
  leading-``"__"`` non-conformant name, not a fully prefix-less one).
- src/nexus/catalog/recovery_bundle.py:379 (``target_collection_for``'s
  conformant-name reduction to ``type__owner`` — ALREADY pinned by
  ``tests/catalog/test_recovery_bundle.py``; one direct isolation test
  added here to pin the exact two-segment ``base`` string independent of
  ``t3_collection_name``'s own resolution logic).

Byte-identical proof: every test in this file must still pass, UNCHANGED,
after Phase B's rewrite lands (nexus-ft04v.22 Phase B).
"""
from __future__ import annotations

import pytest


@pytest.fixture(autouse=True)
def _fake_collection_rows(monkeypatch: pytest.MonkeyPatch):
    """RDR-204 Phase 3 THE REPOINT (nexus-ft04v.26) landed after this
    file's Phase-A/B pins: collection_content_type/collection_owner/
    collection_model now read the catalog row
    (``nexus.mcp_infra.get_collection_row``) instead of parsing the name
    string. This file's tables are about the DOWNSTREAM dispatch (byte
    budgets, classify buckets, corpus derivation) each site computes from
    a content_type/owner, not about the row-lookup mechanism -- fake a
    row per name using the SAME first-segment convention the retired
    parser used, so every fixture name below keeps resolving to the
    content_type/owner its table already documents.
    """
    import nexus.mcp_infra as mi

    def _fake_get_collection_row(name: str) -> dict | None:
        content_type = name.partition("__")[0] if "__" in name else ""
        # RDR-204 Phase 3 (nexus-ft04v.26) class (b): per_collection_chunk_cap/
        # _upsert_byte_budget now resolve the write model via the row
        # first (_write_model_for_collection) -- a realistic embedding_model
        # (the canonical CCE/code split any real Voyage-mode row would
        # carry for this content_type), not a placeholder, so those
        # tests' CCE-vs-code dispatch is exercised through the row path
        # exactly like the other tables in this file are through content_type.
        embedding_model = "voyage-context-3" if content_type in ("docs", "knowledge", "rdr") else "voyage-code-3"
        return {
            "content_type": content_type,
            "owner_id": name.partition("__")[2] if "__" in name else name,
            "embedding_model": embedding_model,
            "lifecycle_state": "live",
        }

    monkeypatch.setattr(mi, "get_collection_row", _fake_get_collection_row)


# ── db/http_vector_client.py:869 — _upsert_byte_budget ─────────────────────


class TestUpsertByteBudget:
    """nexus-ft04v.22: pins ``_upsert_byte_budget``'s prefix-derived budget
    for the same collection-name shapes RDR-204 Phase 3.2b names: code__,
    docs__, rdr__, knowledge__, a quarantine- prefixed name, a two-segment
    legacy name, and a name with an underscored owner. Companion to
    ``test_per_collection_chunk_cap_values`` (same gating: onnx-local vs.
    voyage/unknown), which already pins :836's sibling call — this file
    adds the :869 half that had no prior test at all."""

    def test_voyage_or_unknown_mode(self, monkeypatch: pytest.MonkeyPatch) -> None:
        from nexus.db import http_vector_client as hvc

        monkeypatch.setattr(hvc, "_serving_embedding_mode", lambda: None)
        # CCE prefixes (docs/knowledge/rdr) -> no byte budget (CCE issues
        # one API call per text; cannot exceed a batch token ceiling).
        assert hvc._upsert_byte_budget("docs__proj__voyage-context-3__v1") is None
        assert hvc._upsert_byte_budget("knowledge__notes__voyage-context-3__v1") is None
        assert hvc._upsert_byte_budget("rdr__nexus__voyage-context-3__v1") is None
        # code__ and anything else (including a quarantine- sibling, a
        # two-segment legacy name, and an underscored-owner name) -> the
        # fixed code byte budget.
        assert hvc._upsert_byte_budget("code__nexus__voyage-code-3__v1") == hvc._CODE_UPSERT_BYTE_BUDGET
        assert hvc._upsert_byte_budget("quarantine-code__nexus__voyage-code-3__v1") == hvc._CODE_UPSERT_BYTE_BUDGET
        assert hvc._upsert_byte_budget("code__my_repo__voyage-code-3__v1") == hvc._CODE_UPSERT_BYTE_BUDGET
        assert hvc._upsert_byte_budget("weird-no-prefix") == hvc._CODE_UPSERT_BYTE_BUDGET
        assert hvc._CODE_UPSERT_BYTE_BUDGET == 180_000

        monkeypatch.setattr(hvc, "_serving_embedding_mode", lambda: "voyage")
        assert hvc._upsert_byte_budget("docs__proj__voyage-context-3__v1") is None
        assert hvc._upsert_byte_budget("code__nexus__voyage-code-3__v1") == 180_000

    def test_two_segment_legacy_name_prefix_only(self, monkeypatch: pytest.MonkeyPatch) -> None:
        """A two-segment legacy name (no model/version) is still classified
        by its FIRST segment alone -- ``"docs__proj".split("__", 1)[0]`` is
        ``"docs"``, which is in the CCE set regardless of segment count."""
        from nexus.db import http_vector_client as hvc

        monkeypatch.setattr(hvc, "_serving_embedding_mode", lambda: None)
        assert hvc._upsert_byte_budget("docs__proj") is None
        assert hvc._upsert_byte_budget("code__proj") == hvc._CODE_UPSERT_BYTE_BUDGET

    def test_onnx_local_mode_short_circuits_to_none(self, monkeypatch: pytest.MonkeyPatch) -> None:
        """onnx-local serving is a LOCAL process, not a Voyage network
        call, so the Voyage-token-derived byte proxy never applies --
        every prefix returns ``None`` regardless of CCE/code classification."""
        from nexus.db import http_vector_client as hvc

        monkeypatch.setattr(hvc, "_serving_embedding_mode", lambda: "onnx-local")
        assert hvc._upsert_byte_budget("code__nexus__voyage-code-3__v1") is None
        assert hvc._upsert_byte_budget("docs__proj__voyage-context-3__v1") is None
        assert hvc._upsert_byte_budget("weird-no-prefix") is None


# ── db/http_vector_client.py:3680 — HttpVectorClient.expire ────────────────


class TestHttpVectorClientExpireCollectionFilter:
    """nexus-ft04v.22: pins the ``knowledge__`` collection-name filter in
    ``HttpVectorClient.expire`` -- T3Database parity with
    ``tests/test_t3.py::test_expire_skips_non_knowledge_collections``, which
    pins the identical filter on the T3Database side. No prior test existed
    for this client's own copy of the filter."""

    def test_only_knowledge_prefixed_collections_are_queried(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        from nexus.db import http_vector_client as hvc

        # RDR-204 Phase 3 (nexus-ft04v.26) class (c): expire() now reads
        # each entry's `content_type` field directly (list_collections()
        # joins it from the catalog row on a real engine) instead of
        # parsing the name -- the mock must carry it too.
        client = hvc.HttpVectorClient()
        monkeypatch.setattr(
            client,
            "list_collections",
            lambda: [
                {"name": "code__myrepo", "count": 3, "content_type": "code"},
                {"name": "docs__papers", "count": 5, "content_type": "docs"},
                {"name": "knowledge__sec", "count": 2, "content_type": "knowledge"},
            ],
        )
        queried_collections: list[str] = []

        def fake_post(path, body, *, tenant="default", timeout=120):
            queried_collections.append(body["collection"])
            return {"ids": [], "metadatas": []}

        monkeypatch.setattr(hvc, "_post", fake_post)
        # No collection actually has expired chunks in this fixture, so
        # batch_delete / reap_catalog_manifest_for_chashes never fire --
        # isolating exactly the filter decision under test.
        assert client.expire() == 0
        assert queried_collections == ["knowledge__sec"]


# ── db/reconcile.py:253 — _is_same_model_passthrough ────────────────────────


class TestIsSameModelPassthrough:
    """nexus-ft04v.22: pins the passthrough classification for a
    representative name/target pair per the four-segment shape
    ``_is_same_model_passthrough`` parses via ``name.split("__")``. No
    prior direct unit test existed (only indirectly, through the full
    verify-fill round trip in tests/migration/test_vector_etl_pg_source.py)."""

    def test_cross_model_is_never_passthrough(self) -> None:
        from nexus.db.reconcile import _is_same_model_passthrough

        assert _is_same_model_passthrough(
            "docs__proj__minilm-l6-v2-384__v1",
            "docs__proj__bge-base-en-v15-768__v1",
        ) is False

    def test_same_name_wired_model_is_passthrough(self) -> None:
        from nexus.db.reconcile import _is_same_model_passthrough

        name = "docs__proj__bge-base-en-v15-768__v1"
        assert _is_same_model_passthrough(name, name) is True
        name = "code__nexus__voyage-code-3__v1"
        assert _is_same_model_passthrough(name, name) is True

    def test_same_name_unwired_model_is_not_passthrough(self) -> None:
        """minilm is deliberately absent from _PASSTHROUGH_MODELS -- the
        service wires no embedder for it, so it must be cross-model
        remapped even when name == target."""
        from nexus.db.reconcile import _is_same_model_passthrough

        name = "docs__proj__minilm-l6-v2-384__v1"
        assert _is_same_model_passthrough(name, name) is False

    def test_non_four_segment_name_is_not_passthrough(self) -> None:
        """A legacy two-segment name has no model segment to match against
        _PASSTHROUGH_MODELS -- len(segments) != 4 short-circuits False even
        when name == target."""
        from nexus.db.reconcile import _is_same_model_passthrough

        assert _is_same_model_passthrough("docs__proj", "docs__proj") is False


# ── db/reconcile.py:343 — _dim_for_collection ───────────────────────────────


class TestDimForCollection:
    """nexus-ft04v.22: pins the pgvector-dim resolution (or classification
    failure reason) for a representative set of conformant and
    non-conformant names. No prior direct unit test existed."""

    def test_conformant_known_model_resolves_dim(self) -> None:
        from nexus.db.reconcile import _dim_for_collection

        dim, reason = _dim_for_collection("code__nexus__voyage-code-3__v1")
        assert dim == 1024
        assert reason == ""

        dim, reason = _dim_for_collection("docs__proj__bge-base-en-v15-768__v1")
        assert dim == 768
        assert reason == ""

    def test_non_four_segment_name_cannot_dim_dispatch(self) -> None:
        from nexus.db.reconcile import _dim_for_collection

        dim, reason = _dim_for_collection("docs__proj")
        assert dim is None
        assert "not four-segment conformant" in reason

    def test_unknown_model_segment_cannot_dim_dispatch(self) -> None:
        from nexus.db.reconcile import _dim_for_collection

        dim, reason = _dim_for_collection("docs__proj__unknown-model-xyz__v1")
        assert dim is None
        assert "unknown embedding-model segment" in reason
        assert "unknown-model-xyz" in reason


# ── db/embed_migrate.py:94 — _classify ──────────────────────────────────────


class TestClassify:
    """nexus-ft04v.22: pins ``_classify``'s three-way ``StaleKind`` decision
    for a representative set of names/source-path shapes. No prior direct
    unit test existed."""

    def test_code_prefix_is_always_code_kind(self) -> None:
        from nexus.db.embed_migrate import _classify

        # code__ wins regardless of source_paths/sourceless -- checked first.
        assert _classify("code__nexus__minilm-l6-v2-384__v1", frozenset({"a.py"}), 0) == "code"
        assert _classify("code__nexus__minilm-l6-v2-384__v1", frozenset(), 3) == "code"

    def test_non_code_with_no_sources_is_sourceless(self) -> None:
        from nexus.db.embed_migrate import _classify

        assert _classify("knowledge__notes__minilm-l6-v2-384__v1", frozenset(), 5) == "sourceless"

    def test_non_code_with_sources_is_reindexable(self) -> None:
        from nexus.db.embed_migrate import _classify

        assert _classify(
            "docs__proj__minilm-l6-v2-384__v1", frozenset({"doc.md"}), 0
        ) == "reindexable"
        # Mixed (has both source-backed and sourceless chunks) is still
        # "reindexable" here -- migrate_collection_safe is what refuses on
        # sourceless > 0, not _classify itself.
        assert _classify(
            "docs__proj__minilm-l6-v2-384__v1", frozenset({"doc.md"}), 2
        ) == "reindexable"


# ── db/embed_migrate.py:369 — migrate_collection_safe's corpus derivation ──


class TestMigrateCollectionSafeCorpusDerivation:
    """nexus-ft04v.22: pins the exact ``corpus`` string
    ``migrate_collection_safe`` derives from ``stale.name`` and passes to
    the reindex driver -- ``stale.name.split("__", 1)[1] if "__" in
    stale.name else ""``. No prior test asserted this value; the existing
    ``TestMigrateSafe`` class exercises ``migrate_collection_safe`` widely
    but never inspects the ``corpus`` argument its own reindex_fn spies
    receive."""

    def _stale(self, name: str, target: str) -> "StaleCollection":
        from nexus.db.embed_migrate import StaleCollection

        return StaleCollection(
            name=name,
            count=2,
            source_paths=frozenset({"doc.md"}),
            sourceless=0,
            target_name=target,
            kind="reindexable",
        )

    def test_corpus_is_everything_after_the_first_double_underscore(self) -> None:
        from nexus.db.embed_migrate import migrate_collection_safe

        captured: list[str] = []

        def _reindex_fn(db, tgt, sources, corpus):
            captured.append(corpus)
            raise RuntimeError("stop before any real reindex/verify work")

        migrate_collection_safe(
            db=None,
            stale=self._stale(
                "docs__proj__minilm-l6-v2-384__v1",
                "docs__proj__bge-base-en-v15-768__v1",
            ),
            dry_run=False,
            reindex_fn=_reindex_fn,
        )
        # Byte-identical current behaviour: corpus is NOT just the owner
        # segment -- it is everything past the first "__", model+version
        # segments included.
        assert captured == ["proj__minilm-l6-v2-384__v1"]

    def test_name_with_no_double_underscore_yields_empty_corpus(self) -> None:
        from nexus.db.embed_migrate import migrate_collection_safe

        captured: list[str] = []

        def _reindex_fn(db, tgt, sources, corpus):
            captured.append(corpus)
            raise RuntimeError("stop before any real reindex/verify work")

        migrate_collection_safe(
            db=None,
            stale=self._stale("noprefixname", "noprefixname-target"),
            dry_run=False,
            reindex_fn=_reindex_fn,
        )
        assert captured == [""]


# ── catalog/chunk_quarantine.py:58 — quarantine_collection_name ────────────


class TestQuarantineCollectionName:
    """nexus-ft04v.22: pins both branches of ``quarantine_collection_name``,
    including the no-``"__"``-at-all edge case the existing integration
    tests (tests/test_rdr191_gc_serverside_prune.py) never exercise
    directly since they only ever pass real conformant collection names."""

    def test_conformant_name_keeps_content_type_in_prefix(self) -> None:
        from nexus.catalog.chunk_quarantine import quarantine_collection_name

        assert quarantine_collection_name("code__nexus-1-1__voyage-code-3__v1") == (
            "quarantine-code__nexus-1-1__voyage-code-3__v1"
        )
        assert quarantine_collection_name("docs__proj__bge-base-en-v15-768__v1") == (
            "quarantine-docs__proj__bge-base-en-v15-768__v1"
        )

    def test_name_with_no_double_underscore_falls_back_to_x(self) -> None:
        from nexus.catalog.chunk_quarantine import quarantine_collection_name

        assert quarantine_collection_name("noprefixname") == "quarantine-x__noprefixname"


# ── catalog/orphan_backfill.py:322 — _content_type_for_collection ─────────


class TestContentTypeForCollectionEdgeCase:
    """nexus-ft04v.22: the existing table in tests/test_orphan_backfill.py
    pins every conformant prefix plus a leading-``"__"`` non-conformant
    name (``"__weird"`` -> ``"knowledge"``); this adds the one shape it
    does not cover -- a name with NO ``"__"`` at all, where
    ``collection.split("__", 1)[0]`` is the WHOLE string, not empty."""

    def test_no_double_underscore_returns_whole_name_stripped(self) -> None:
        from nexus.catalog.orphan_backfill import _content_type_for_collection

        assert _content_type_for_collection("weird-no-prefix") == "weird-no-prefix"
        assert _content_type_for_collection("  padded  ") == "padded"


# ── catalog/recovery_bundle.py:379 — target_collection_for's split ────────


class TestTargetCollectionForSplitIsolated:
    """nexus-ft04v.22: existing tests in tests/catalog/test_recovery_bundle.py
    already pin the end-to-end conformant/non-conformant behaviour through
    the real (or mocked) ``t3_collection_name`` resolver. This isolates the
    raw split itself -- confirming ONLY parts[0] and parts[1] of the
    conformant 4-segment name reach the resolver as ``base``, independent
    of whatever t3_collection_name does with it."""

    def test_conformant_name_reduces_to_two_segment_base(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        import nexus.catalog.recovery_bundle as rb

        seen: list[str] = []
        monkeypatch.setattr(
            "nexus.corpus.t3_collection_name",
            lambda name, t3=None, for_write=False, allow_placeholder=False: seen.append(name) or name,
        )
        rb.target_collection_for("code__nexus__voyage-code-3__v1", t3=None)
        assert seen == ["code__nexus"]
