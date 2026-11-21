# SPDX-License-Identifier: AGPL-3.0-or-later
"""RDR-229 P2.9 fix round (nexus-p2m1x.14): item 1's Row, NoRow and Unreadable outcomes at every write-affecting router.

A collection a write is about to create holds no chunk, so it has no row in the client cache. The routers of
item 12's "a collection name on this install" group then answer as they always did, from the candidate name's own
content-type segment (the mint-class derivation, one shared helper, :func:`nexus.corpus.routing_content_type`),
after one cache invalidate and re-read. A listing that cannot be read answers the same way and logs a warning; a
read-only scan propagates it. A prefix-free label with no row derives nothing (P3.8's case) and stays unrouted.

None of these tests takes the ``named_collection_rows`` fixture: that fixture gives every prefix-named collection a
row, which is exactly the thing under test.
"""
from __future__ import annotations

import sys

import pytest
from structlog.testing import capture_logs

from nexus.db.http_vector_client import VectorServiceError

#: These fixtures use POSIX absolute paths ("/abs/a.md"), which are not absolute on Windows, so uri_for
#: mints nothing there; the routing they pin is platform-independent and runs on the POSIX legs.
_POSIX_PATHS = pytest.mark.skipif(sys.platform == "win32", reason="POSIX-only: the fixture's source paths are POSIX absolute paths")

_NEW_DOCS = "docs__fixnorow__bge-base-en-v15-768__v1"
_NEW_KNOW = "knowledge__fixnorow__bge-base-en-v15-768__v1"
_NEW_RDR = "rdr__fixnorow__bge-base-en-v15-768__v1"
_NEW_CODE = "code__fixnorow__bge-base-en-v15-768__v1"


class _Rows:
    """The client row cache: ``table`` maps a name to a content type; ``later`` appears after one invalidate."""

    def __init__(self, table: dict[str, str] | None = None, *, unreadable: bool = False) -> None:
        self.table = dict(table or {})
        self.later: dict[str, str] = {}
        self.unreadable = unreadable
        self.invalidated = 0

    def get(self, name: str, *, refresh: bool = True, strict: bool = False) -> dict | None:
        if self.unreadable and strict:
            raise VectorServiceError("listing unreadable")
        ct = self.table.get(name)
        return {"content_type": ct, "owner_id": "1.1", "embedding_model": "m", "lifecycle_state": "live"} if ct else None

    def invalidate(self) -> None:
        self.invalidated += 1
        self.table.update(self.later)


@pytest.fixture
def rows(monkeypatch: pytest.MonkeyPatch) -> _Rows:
    r = _Rows()
    monkeypatch.setattr("nexus.mcp_infra.get_collection_row", r.get)
    monkeypatch.setattr("nexus.mcp_infra.invalidate_collections_cache", r.invalidate)
    monkeypatch.setattr("nexus.collection_route._NOROW_RECHECK_INTERVAL_S", 0.0, raising=False)
    return r


# ── the shared helper: Row, NoRow, Unreadable ───────────────────────────────


class TestTheSharedHelper:
    def test_a_row_wins_over_the_name(self, rows):
        from nexus.corpus import routing_content_type

        rows.table[_NEW_DOCS] = "code"
        assert routing_content_type(_NEW_DOCS) == "code"
        assert rows.invalidated == 0

    def test_no_row_after_one_invalidate_and_re_read_derives_from_the_name(self, rows):
        from nexus.corpus import routing_content_type

        assert routing_content_type(_NEW_DOCS) == "docs"
        assert routing_content_type(_NEW_CODE) == "code"
        assert rows.invalidated == 2

    def test_a_row_that_appears_after_the_invalidate_is_the_answer(self, rows):
        """The aspect worker's warm-daemon miss: the cache predates the collection's first chunk."""
        from nexus.corpus import routing_content_type

        rows.later["c5150"] = "docs"
        assert routing_content_type("c5150") == "docs"
        assert rows.invalidated == 1

    def test_an_unreadable_listing_derives_from_the_name_and_logs_a_warning(self, rows):
        from nexus.corpus import routing_content_type

        rows.unreadable = True
        with capture_logs() as logs:
            assert routing_content_type(_NEW_DOCS) == "docs"
        assert [e["log_level"] for e in logs if e["event"] == "routing_content_type_unreadable"] == ["warning"]

    @pytest.mark.expects_unresolvable_endpoint
    def test_an_unresolvable_service_endpoint_is_unreadable_too(self, rows, monkeypatch):
        """A box with no resolvable service (Windows CI, a hook before the engine is up) raises
        ServiceEndpointUnresolvableError, a sibling of the two listing errors, not a subclass: it is
        the Unreadable outcome too, so a write router derives from the name instead of raising
        (the aspect enqueue hook raised here on native Windows)."""
        from nexus.corpus import routing_content_type
        from nexus.db.service_endpoint import ServiceEndpointUnresolvableError

        def _unresolvable(name: str, *, refresh: bool = True, strict: bool = False) -> dict | None:
            raise ServiceEndpointUnresolvableError("no endpoint")

        monkeypatch.setattr("nexus.mcp_infra.get_collection_row", _unresolvable)
        with capture_logs() as logs:
            assert routing_content_type(_NEW_KNOW) == "knowledge"
        assert [e["log_level"] for e in logs if e["event"] == "routing_content_type_unreadable"] == ["warning"]

    def test_a_prefix_free_label_with_no_row_derives_nothing(self, rows):
        """P3.8's case: no string rule tells a label's type, so it stays unrouted."""
        from nexus.corpus import routing_content_type

        assert routing_content_type("c5151") is None

    def test_the_re_read_is_not_repeated_inside_the_interval(self, rows, monkeypatch):
        from nexus.corpus import routing_content_type

        monkeypatch.setattr("nexus.collection_route._NOROW_RECHECK_INTERVAL_S", 3600.0, raising=False)
        routing_content_type("docs__throttled")
        routing_content_type("docs__throttled")
        assert rows.invalidated == 1


# ── the routers, each at NoRow ──────────────────────────────────────────────


class TestRoutersAtNoRow:
    @_POSIX_PATHS
    def test_uri_for_gives_a_file_uri_for_a_new_docs_collection(self, rows):
        from nexus.aspect_readers import uri_for

        assert uri_for(_NEW_DOCS, "/abs/a.md") == "file:///abs/a.md"
        assert uri_for(_NEW_DOCS, "relative/a.md") is None  # file-routed, nothing to anchor on: never a note URI
        assert uri_for(_NEW_CODE, "/abs/m.py") == "file:///abs/m.py"
        assert uri_for(_NEW_RDR, "/abs/r.md") == "file:///abs/r.md"
        assert uri_for(_NEW_KNOW, "a title") == f"nx-note://{_NEW_KNOW}/a title"

    @_POSIX_PATHS

    def test_uri_for_with_an_unreadable_listing_still_gives_a_file_uri(self, rows):
        from nexus.aspect_readers import uri_for

        rows.unreadable = True
        assert uri_for(_NEW_DOCS, "/abs/a.md") == "file:///abs/a.md"

    def test_uri_for_leaves_a_prefix_free_label_with_no_row_unrouted(self, rows):
        from nexus.aspect_readers import uri_for

        assert uri_for("c5152", "/abs/a.md") == "nx-note://c5152//abs/a.md"

    @_POSIX_PATHS

    def test_uri_for_follows_a_row_for_a_prefix_free_label_found_on_the_re_read(self, rows):
        from nexus.aspect_readers import uri_for

        rows.later["c5153"] = "docs"
        assert uri_for("c5153", "/abs/a.md") == "file:///abs/a.md"

    def test_the_identity_fields(self, rows):
        from nexus.aspect_readers import _identity_fields_for, _legacy_identity_fields_for

        assert _legacy_identity_fields_for(_NEW_KNOW) == ("source_path", "title")
        assert _legacy_identity_fields_for(_NEW_DOCS) == ("source_path",)
        assert _identity_fields_for(_NEW_RDR) == ("doc_id",)

    def test_the_extractor_for_a_new_collection(self, rows):
        from nexus.aspect_extractor import select_config

        assert select_config(_NEW_KNOW).extractor_name == "scholarly-paper-v1"
        assert select_config(_NEW_RDR).extractor_name == "rdr-frontmatter-v1"
        assert select_config(_NEW_CODE) is None

    @_POSIX_PATHS

    def test_a_first_write_to_a_new_docs_collection_gets_file_identity_and_the_docs_extractor(self, rows, monkeypatch):
        """Baton's test: no cached row, opted in; the docs collection is routed as docs."""
        from nexus.aspect_extractor import docs_collection_opted_in, extraction_applies_to_source, select_config
        from nexus.aspect_readers import uri_for

        monkeypatch.setattr("nexus.aspect_extractor._engine_aspects_enabled", lambda c: (False, False))
        monkeypatch.setattr("nexus.aspect_extractor._docs_opt_in_patterns", lambda: ["docs__fixnorow*"])
        assert docs_collection_opted_in(_NEW_DOCS) is True
        assert select_config(_NEW_DOCS).extractor_name == "general-prose-v1"
        assert extraction_applies_to_source(_NEW_DOCS, "/r/data.json") is False
        assert uri_for(_NEW_DOCS, "/abs/a.md") == "file:///abs/a.md"

    def test_the_aspect_worker_attribution_guard(self, rows):
        from nexus.aspect_worker import _is_unattributable

        assert _is_unattributable(_record(_NEW_DOCS)) is True
        assert _is_unattributable(_record(_NEW_KNOW)) is False

    def test_the_aspect_worker_attribution_guard_for_a_prefix_free_label_with_a_row(self, rows):
        from nexus.aspect_worker import _is_unattributable

        rows.table["c5154"] = "docs"
        rows.table["c5155"] = "knowledge"
        assert _is_unattributable(_record("c5154")) is True
        assert _is_unattributable(_record("c5155")) is False

    def test_the_aspect_worker_finds_a_row_the_warm_cache_missed(self, rows):
        from nexus.aspect_worker import _is_unattributable

        rows.later["c5156"] = "docs"
        assert _is_unattributable(_record("c5156")) is True

    def test_the_chunk_content_type_still_derives_from_the_name(self, rows):
        from nexus.metadata_schema import chunk_content_type_for_collection

        assert chunk_content_type_for_collection(_NEW_CODE) == "code"
        assert chunk_content_type_for_collection(_NEW_RDR) == "markdown"


# ── the write paths the review named ────────────────────────────────────────


def test_a_first_store_put_into_a_new_docs_collection_stores_no_note_uri(rows, monkeypatch):
    """put_note -> catalog_store_hook_tracked -> store_hook uri_for, before the collection holds a chunk."""
    from nexus.catalog.store_hook import catalog_store_hook_tracked
    from tests.test_6pfgb5_note_meta_homes import _FakeCatalog

    fake = _FakeCatalog()
    monkeypatch.setattr("nexus.catalog.factory.make_catalog_reader", lambda *a, **k: fake)
    monkeypatch.setattr("nexus.catalog.factory.make_catalog_writer", lambda *a, **k: fake)
    catalog_store_hook_tracked("/abs/first-put.md", "ab" * 32, _NEW_DOCS)
    (doc,) = fake.docs.values()
    assert not (doc.source_uri or "").startswith("nx-note://"), doc.source_uri
    assert (doc.source_uri or "") in ("", "file:///abs/first-put.md")


@_POSIX_PATHS


def test_aspects_backfill_uri_gives_a_file_uri_to_a_docs_collection_with_no_row(rows):
    from nexus.aspect_uri_repair import plan_source_uri_backfill

    rec = _record(_NEW_DOCS, source_path="/abs/a.md", doc_id="1.1.1", confidence=0.9)
    plan = plan_source_uri_backfill([rec])
    assert [u for _r, u in plan.updates] == ["file:///abs/a.md"], plan


@_POSIX_PATHS


def test_the_exporters_first_page_into_a_new_docs_target_keys_its_owner_by_a_file_uri(rows):
    from nexus.exporter import _locate_owner_group

    group, _pos = _locate_owner_group(
        {}, {"title": "/abs/a.md", "content_type": "docs"},
        fallback_source_uri="", fallback_title="", fallback_content_type="docs", target_collection=_NEW_DOCS,
    )
    assert group["source_uri"] == "file:///abs/a.md"


@_POSIX_PATHS


def test_the_exporters_owner_key_reads_the_row_through_the_import_handle(rows):
    """A prefix-free target: the db's own resolver (another tenant's client) answers, not this process's cache."""
    from nexus.exporter import _locate_owner_group

    class _Db:
        def _resolve_collection_row(self, name, *, strict=False):
            return {"content_type": "docs"} if name == "c5157" else None

    group, _pos = _locate_owner_group(
        {}, {"title": "/abs/a.md", "content_type": "docs"},
        fallback_source_uri="", fallback_title="", fallback_content_type="docs", target_collection="c5157",
        db=_Db(),
    )
    assert group["source_uri"] == "file:///abs/a.md"
    other, _ = _locate_owner_group(
        {}, {"title": "/abs/a.md", "content_type": "docs"},
        fallback_source_uri="", fallback_title="", fallback_content_type="docs", target_collection="c5157",
    )
    assert other["source_uri"] == "nx-note://c5157//abs/a.md"  # no handle, no row: the label stays unrouted


def test_the_exporters_import_floor_check_reads_a_prefix_free_label_through_the_handle(monkeypatch, tmp_path):
    from nexus.errors import FormatVersionError
    from nexus.exporter import import_collection

    probes: list[int] = []
    monkeypatch.setattr("nexus.db.serving_engine_version.require_note_uri_engine", lambda: probes.append(1))

    class _Db:
        def _resolve_collection_row(self, name, *, strict=False):
            return {"content_type": "docs"} if name == "c5158" else None

    import json

    path = tmp_path / "h.nxexp"
    path.write_text(json.dumps({"format_version": 1, "collection_name": "c5158"}) + "\n")
    with pytest.raises(FormatVersionError, match="embedding_model"):
        import_collection(_Db(), path)
    assert probes == []  # a docs row: file-routed, the floor check does not apply


@_POSIX_PATHS


def test_the_recovery_bundles_link_re_derivation_finds_a_row_less_docs_target(rows, monkeypatch):
    from nexus.catalog import recovery_bundle as rb

    class _Reader:
        def by_source_uri(self, uri):
            return "ENTRY" if uri == "file:///abs/a.md" else None

    monkeypatch.setattr(rb, "target_collection_for", lambda collection, t3: collection)
    assert rb._resolve_link_endpoint(_Reader(), None, f"nx-note://{_NEW_DOCS}//abs/a.md") == "ENTRY"


# ── the exclusion default, at config load ───────────────────────────────────


class TestTheLocalExclusionRule:
    @pytest.fixture
    def cfg(self, monkeypatch, tmp_path):
        monkeypatch.setenv("NEXUS_CONFIG_DIR", str(tmp_path))
        monkeypatch.delenv("NEXUS_REPO_ROOT", raising=False)

        def write(text: str) -> None:
            (tmp_path / "config.yml").write_text(text, encoding="utf-8")

        return write

    def _taxonomy(self):
        from nexus.config import load_config

        return load_config()["taxonomy"]

    def test_no_user_setting_keeps_the_shipped_content_type_rule(self, cfg):
        tax = self._taxonomy()
        assert tax["local_exclude_content_types"] == ["code"] and tax["local_exclude_collections"] == []

    def test_the_documented_empty_list_re_enables_code_taxonomy_locally(self, cfg):
        cfg("taxonomy:\n  local_exclude_collections: []\n")
        tax = self._taxonomy()
        assert tax["local_exclude_collections"] == [] and tax["local_exclude_content_types"] == []

    def test_a_user_pattern_list_owns_the_exclusion(self, cfg):
        cfg('taxonomy:\n  local_exclude_collections: ["code__*"]\n')
        tax = self._taxonomy()
        assert tax["local_exclude_collections"] == ["code__*"] and tax["local_exclude_content_types"] == []

    def test_an_explicit_content_type_list_is_exactly_what_the_user_set(self, cfg):
        cfg("taxonomy:\n  local_exclude_content_types: [docs]\n")
        assert self._taxonomy()["local_exclude_content_types"] == ["docs"]
        cfg("taxonomy:\n  local_exclude_collections: []\n  local_exclude_content_types: [code, rdr]\n")
        assert self._taxonomy()["local_exclude_content_types"] == ["code", "rdr"]

    def test_a_per_repo_setting_counts_as_the_user_setting(self, cfg, monkeypatch, tmp_path):
        repo = tmp_path / "repo"
        repo.mkdir()
        (repo / ".nexus.yml").write_text("taxonomy:\n  local_exclude_collections: []\n", encoding="utf-8")
        monkeypatch.setenv("NEXUS_REPO_ROOT", str(repo))
        assert self._taxonomy()["local_exclude_content_types"] == []


# ── the note-tags rung reads strictly ───────────────────────────────────────


def test_the_note_tags_scan_propagates_a_failed_listing(monkeypatch):
    from nexus.upgrade_ladder.rungs.note_tags_array import NoteTagsArrayRung

    def _boom(*a, **k):
        raise VectorServiceError("listing failed")

    monkeypatch.setattr("nexus.mcp_infra._refresh_collections_cache_if_stale", _boom)
    monkeypatch.setattr("nexus.mcp_infra._collections_cache", ([], {}, {}, 0.0))

    class _Reader:
        def distinct_doc_collections(self):
            return ["knowledge__fixnorow__bge-base-en-v15-768__v1"]

        def list_by_collection(self, name):
            return []

        def close(self):
            pass

    with pytest.raises(VectorServiceError):
        NoteTagsArrayRung(reader_factory=lambda: _Reader())._scan(_Reader())


# ── search: an explicit --corpus with a cold row cache ──────────────────────


class TestTopicRouteProseAtNoRow:
    """``topic_route.is_prose`` sees the REQUEST's collection names, so a name with no row in a cold cache is
    answered from its own `<type>__` segment, as the thresholds are (item 1's NoRow)."""

    def test_a_conformant_prose_name_with_no_row_is_prose(self, rows):
        from nexus.topic_route import is_prose

        assert is_prose("knowledge__1-1__voyage-3__v1") is True
        assert is_prose("docs__1-1__voyage-3__v1") is True
        assert is_prose("rdr__1-1__voyage-3__v1") is True

    def test_a_conformant_code_name_with_no_row_is_not_prose(self, rows):
        from nexus.topic_route import is_prose

        assert is_prose("code__1-1__voyage-3__v1") is False

    def test_a_prefix_free_label_with_no_row_is_not_prose(self, rows):
        from nexus.topic_route import is_prose

        assert is_prose("c7101") is False  # nothing to derive (P3.8)

    def test_a_row_decides_over_the_name(self, rows):
        from nexus.topic_route import is_prose

        rows.table.update({"knowledge__1-1__voyage-3__v1": "code", "code__1-1__voyage-3__v1": "docs"})
        assert is_prose("knowledge__1-1__voyage-3__v1") is False
        assert is_prose("code__1-1__voyage-3__v1") is True


def _no_listing(*a, **k):
    raise AssertionError("an explicit-name search must not list the tenant")


class TestSearchWithAColdCache:
    def _client(self, monkeypatch, collection: str):
        from nexus.db.http_vector_client import HttpVectorClient

        client = HttpVectorClient()
        asked: dict[str, int] = {}

        def _search(self, query, collection_names, n_results=10, where=None, **_kw):
            asked[collection_names[0]] = n_results
            return [{"id": "near", "content": "k", "distance": 0.30}, {"id": "far", "content": "d", "distance": 0.60}]

        monkeypatch.setattr(HttpVectorClient, "search", _search)
        monkeypatch.setattr(HttpVectorClient, "embedding_mode", lambda self: "voyage")

        def _no_embeddings(self, *a, **k):
            raise RuntimeError("no embeddings in a tenant-free test")  # the diversity step isolates it per collection

        monkeypatch.setattr(HttpVectorClient, "get_embeddings", _no_embeddings)
        return client, asked

    def test_an_explicit_conformant_corpus_name_gets_its_rows_threshold_and_overfetch(self, monkeypatch):
        import nexus.mcp_infra as infra
        from nexus.search_engine import search_cross_corpus

        collection = "docs__fixnorow__voyage-3__v1"
        monkeypatch.setattr(infra, "_collections_cache", ([], {}, {}, 0.0))
        monkeypatch.setattr(infra, "_refresh_collections_cache_if_stale", _no_listing)
        monkeypatch.setattr("nexus.search_engine.load_config", lambda *a, **k: {"search": {"distance_threshold": {
            "docs": 0.45, "default": 0.99}}})
        client, asked = self._client(monkeypatch, collection)
        out = search_cross_corpus("q", [collection], 10, client, sensitivity="include")
        assert asked[collection] == 40  # 4x: the docs row, not the default 2x
        assert [r.id for r in out] == ["near"]  # the docs threshold (0.45) filtered the 0.60 hit, not the default (0.99)

    def test_a_failed_row_lookup_falls_back_to_the_candidate_name_not_to_the_default(self, monkeypatch):
        from nexus.search_engine import search_cross_corpus

        collection = "docs__fixnorow__voyage-3__v1"
        monkeypatch.setattr("nexus.search_engine.load_config", lambda *a, **k: {"search": {"distance_threshold": {
            "docs": 0.45, "default": 0.99}}})
        client, asked = self._client(monkeypatch, collection)

        def _rows(self, names):
            raise RuntimeError("listing failed")

        monkeypatch.setattr(type(client), "_resolve_collection_rows", _rows)
        out = search_cross_corpus("q", [collection], 10, client, sensitivity="include")
        assert asked[collection] == 40
        assert [r.id for r in out] == ["near"]


    def test_an_explicit_conformant_prose_name_with_a_cold_cache_still_asks_for_topic_routing(self, monkeypatch):
        import nexus.mcp_infra as infra
        from nexus.db.http_vector_client import PerCollectionEnvelopeError
        from nexus.search_engine import search_cross_corpus

        prose = "knowledge__fixnorow__voyage-3__v1"
        code = "code__fixnorow__voyage-3__v1"
        monkeypatch.setattr(infra, "_collections_cache", ([], {}, {}, 0.0))
        monkeypatch.setattr(infra, "_refresh_collections_cache_if_stale", _no_listing)
        client, _asked = self._client(monkeypatch, prose)
        sent: list[dict] = []

        def _per_collection(self, query, group, **kw):
            sent.append({"group": list(group), "topic_route": kw.get("topic_route")})
            raise PerCollectionEnvelopeError("test double: fall back to the batched path")

        monkeypatch.setattr(type(client), "supports_topic_route", lambda self: True)
        monkeypatch.setattr(type(client), "search_per_collection", _per_collection)
        search_cross_corpus("q", [prose, code], 10, client, sensitivity="include", topic_route={"mode": "top1"})
        assert sent, "the per-collection route was not taken: the prose name read as not prose"
        assert sent[0]["topic_route"] == {"mode": "top1"}
        sent.clear()
        search_cross_corpus("q", [code], 10, client, sensitivity="include", topic_route={"mode": "top1"})
        assert not sent  # a code name alone is never routed, and takes the default path


# ── listing rows with no content type ───────────────────────────────────────


def test_the_pdf_stub_check_says_how_many_collections_carry_no_catalog_row(monkeypatch):
    from nexus import health

    class _T3:
        def list_collections(self, *a, **k):
            return [{"name": "c6001", "count": 3}, {"name": "c6002", "count": 3, "content_type": "code"}]

    monkeypatch.setattr("nexus.db.make_t3", lambda *a, **k: _T3())
    (result,) = health._check_pdf_stub_metadata()
    assert "no catalog row" in result.detail and "1" in result.detail, result.detail


def test_the_visible_check_flags_collections_with_no_catalog_row():
    from nexus.commands.catalog_cmds import doctor

    class _T3:
        def list_collections(self, *a, **k):
            return [{"name": "c6003", "count": 1}, {"name": "c6004", "count": 1, "content_type": "docs"}]

    report = doctor._run_visible_outside_manifest(t3=_T3())
    assert report["not_applicable"] is True
    assert report["unrowed_collections"] == 1 and "no catalog row" in report["reason"]


# ── prefix-free labels through the moved sites that had no label test ───────


def test_doctor_search_threshold_reads_a_prefix_free_labels_row_through_the_handle():
    from nexus.doctor_search import _default_threshold_for

    class _Engine:
        _voyage_client = object()

        def embedding_mode(self):
            return "voyage"

        def _resolve_collection_rows(self, names):
            return {n: {"content_type": "docs", "embedding_model": "voyage-3"} for n in names}

    import nexus.config as config

    cfg = {"search": {"distance_threshold": {"docs": 0.65, "default": 0.55}}}
    orig = config.load_config
    config.load_config = lambda *a, **k: cfg
    try:
        assert _default_threshold_for("c6005", _Engine()) == pytest.approx(0.65)
    finally:
        config.load_config = orig


def test_the_content_type_resolver_reads_the_handles_rows_for_a_prefix_free_label():
    from nexus.search_engine import _collection_content_type_resolver

    class _T3:
        def _resolve_collection_rows(self, names):
            return {n: {"content_type": "rdr"} for n in names}

    assert _collection_content_type_resolver(_T3(), ["c6006"])("c6006") == "rdr"


def _record(collection: str, *, source_path: str = "p", doc_id: str = "", confidence: float = 0.5):
    from nexus.db.t2.records import AspectRecord

    return AspectRecord(
        collection=collection, source_path=source_path, problem_formulation="x", proposed_method="y",
        experimental_datasets=[], experimental_baselines=[], experimental_results="z",
        extras={}, confidence=confidence, extracted_at="2026-11-20T00:00:00Z",
        model_version="m", extractor_name="e", source_uri="", doc_id=doc_id,
    )


# ── the re-read is bounded across names, and the warning is not repeated ────


class TestTheReReadIsBoundedAcrossNames:
    @pytest.fixture
    def listing(self, monkeypatch):
        """A real collections cache over a counting listing that holds no collection; the default interval."""
        import nexus.collection_route as route
        import nexus.mcp_infra as infra

        calls: list[int] = []
        monkeypatch.setattr(infra, "_collections_cache", ([], {}, {}, 0.0))
        monkeypatch.setattr(infra, "get_t3", lambda: object())
        monkeypatch.setattr(infra, "_list_collections_remembering_failure", lambda t3, **k: calls.append(1) or [])
        monkeypatch.setattr(route, "_norow_rechecked", {})
        monkeypatch.setattr(route, "_unreadable_warned", {})
        monkeypatch.setattr(route, "_NOROW_RECHECK_INTERVAL_S", 10.0)
        return calls

    def test_fifty_distinct_rowless_names_cost_at_most_two_listings_and_a_second_pass_none(self, listing):
        from nexus.corpus import routing_content_type

        names = [f"docs__scan{i}__bge-base-en-v15-768__v1" for i in range(50)]
        assert [routing_content_type(n) for n in names] == ["docs"] * 50
        assert len(listing) <= 2, f"{len(listing)} listings for 50 names"
        after_first = len(listing)
        assert [routing_content_type(n) for n in names] == ["docs"] * 50
        assert len(listing) == after_first

    def test_a_cache_older_than_the_interval_is_re_read_once_for_the_whole_scan(self, listing, monkeypatch):
        import nexus.mcp_infra as infra
        from nexus.corpus import routing_content_type

        routing_content_type("docs__warmup")  # fills the cache
        clock = [infra._now() + 11.0]
        monkeypatch.setattr(infra, "_now", lambda: clock[0])
        before = len(listing)
        for i in range(20):
            routing_content_type(f"docs__stale{i}")
        assert len(listing) - before <= 2


class TestTheUnreadableWarningIsNotRepeated:
    def test_one_warning_per_name_inside_the_interval(self, rows, monkeypatch):
        import nexus.collection_route as route
        from nexus.corpus import routing_content_type

        monkeypatch.setattr(route, "_unreadable_warned", {})
        monkeypatch.setattr(route, "_NOROW_RECHECK_INTERVAL_S", 3600.0)
        rows.unreadable = True
        with capture_logs() as logs:
            for _ in range(5):
                assert routing_content_type(_NEW_DOCS) == "docs"
            assert routing_content_type(_NEW_CODE) == "code"
        warned = [e["collection"] for e in logs if e["event"] == "routing_content_type_unreadable"]
        assert sorted(warned) == sorted([_NEW_DOCS, _NEW_CODE])


# ── the test-only T3Database facade has no catalog ──────────────────────────


def test_the_test_only_t3_facade_stamps_its_chunk_type_without_reading_a_row(monkeypatch, local_t3):
    """``T3Database`` is the retired, test-only substrate: it derives its own listing rows from names, so a
    put into it must not refresh or fetch the client row cache (a tenant-free test has none). The schema-bypass
    check (P2.5) still peeks at what the cache already holds with ``refresh=False``; that read lists nothing."""
    def _no_cache(*a, **k):
        raise AssertionError("T3Database.put read the client row cache")

    def _peek_only(name, *, refresh=True, strict=False):
        if refresh or strict:
            _no_cache()
        return None

    monkeypatch.setattr("nexus.mcp_infra.get_collection_row", _peek_only)
    monkeypatch.setattr("nexus.mcp_infra.invalidate_collections_cache", _no_cache)
    local_t3.put(content="def f(): pass", collection="code__facade", title="m.py")
    from nexus.metadata_schema import chunk_content_type_for_collection

    assert chunk_content_type_for_collection("code__facade", catalog=False) == "code"
    assert chunk_content_type_for_collection("c7001", catalog=False) == "prose"
