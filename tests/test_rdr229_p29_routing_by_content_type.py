# SPDX-License-Identifier: AGPL-3.0-or-later
"""RDR-229 P2.9 (nexus-p2m1x.14): routing by content type reads the collection's catalog row.

Every router that used to test a collection NAME against ``code__``/``docs__``/``rdr__``/
``knowledge__`` now asks the row for its ``content_type``: the listing row where the caller holds one,
``nexus.mcp_infra.get_collection_row`` where it holds a name. These tests hold that for each content type
with a prefix-free ``c<digits>`` label, which no name test can route, and hold that a name that SAYS one
type while its row says another is routed by the row.
"""
from __future__ import annotations

import pytest

from nexus.types import SearchResult

#: label -> catalog content_type. Prefix-free on purpose: a name test cannot route any of them.
_DOCS, _KNOW, _RDR, _CODE = "c1001", "c1002", "c1003", "c1004"
_ROWS: dict[str, str] = {_DOCS: "docs", _KNOW: "knowledge", _RDR: "rdr", _CODE: "code"}


def _row(content_type: str) -> dict:
    return {
        "content_type": content_type, "owner_id": "1.1",
        "embedding_model": "voyage-context-3", "lifecycle_state": "live",
    }


@pytest.fixture
def rows(monkeypatch: pytest.MonkeyPatch) -> dict[str, str]:
    """The client row cache, as a dict a test can add to; an absent label has no row."""
    table = dict(_ROWS)

    def _get(name: str, *, refresh: bool = True, strict: bool = False) -> dict | None:
        ct = table.get(name)
        return _row(ct) if ct else None

    monkeypatch.setattr("nexus.mcp_infra.get_collection_row", _get)
    return table


def _hit(collection: str, *, citations: int = 500) -> SearchResult:
    from nexus.scoring import OWNER_QUALITY_KEY

    r = SearchResult(
        id="h1", content="chunk", distance=0.3, collection=collection,
        metadata={OWNER_QUALITY_KEY: {"bib_citation_count": citations}},
    )
    r.hybrid_score = 0.5
    return r


# ── docs ────────────────────────────────────────────────────────────────────


class TestDocsLabel:
    @pytest.fixture(autouse=True)
    def _opted_in(self, rows, monkeypatch):
        monkeypatch.setattr("nexus.aspect_extractor._engine_aspects_enabled", lambda c: (False, False))
        monkeypatch.setattr("nexus.aspect_extractor._docs_opt_in_patterns", lambda: ["c1*"])

    def test_aspect_extractor_when_opted_in(self):
        from nexus.aspect_extractor import docs_collection_opted_in, select_config

        assert docs_collection_opted_in(_DOCS) is True
        cfg = select_config(_DOCS)
        assert cfg is not None and cfg.extractor_name == "general-prose-v1"

    def test_the_opt_in_patterns_still_match_the_label(self, monkeypatch):
        from nexus.aspect_extractor import docs_collection_opted_in

        monkeypatch.setattr("nexus.aspect_extractor._docs_opt_in_patterns", lambda: ["zz*"])
        assert docs_collection_opted_in(_DOCS) is False

    def test_a_docs_row_is_the_only_thing_the_opt_in_gate_reads(self):
        from nexus.aspect_extractor import docs_collection_opted_in

        assert docs_collection_opted_in(_CODE) is False
        assert docs_collection_opted_in(_KNOW) is False

    def test_prose_rule_applies_to_an_opted_in_docs_label(self):
        from nexus.aspect_extractor import extraction_applies_to_source

        assert extraction_applies_to_source(_DOCS, "/r/data.json") is False
        assert extraction_applies_to_source(_DOCS, "/r/readme.md") is True

    def test_quality_boost(self):
        from nexus.scoring import apply_quality_boost

        r = _hit(_DOCS)
        apply_quality_boost([r])
        assert r.hybrid_score > 0.5

    def test_file_routed_identity(self):
        from nexus.aspect_readers import _identity_fields_for, _legacy_identity_fields_for, uri_for

        assert uri_for(_DOCS, "/abs/doc.md") == "file:///abs/doc.md"
        assert _identity_fields_for(_DOCS) == ("doc_id",)
        assert _legacy_identity_fields_for(_DOCS) == ("source_path",)

    def test_a_file_routed_record_with_no_uri_is_unattributable(self):
        from nexus.aspect_worker import _is_unattributable

        assert _is_unattributable(_record(_DOCS)) is True

    def test_search_threshold_and_overfetch(self):
        from nexus.search_engine import _overfetch_multiplier, _threshold_for_collection

        cfg = {"search": {"distance_threshold": {"docs": 0.7, "code": 0.4, "default": 0.5}}}
        assert _threshold_for_collection(_DOCS, cfg) == 0.7
        assert _overfetch_multiplier(_DOCS) == 4

    def test_chunk_content_type_and_topic_route(self):
        from nexus.metadata_schema import chunk_content_type_for_collection
        from nexus.topic_route import is_prose

        assert chunk_content_type_for_collection(_DOCS) == "prose"
        assert is_prose(_DOCS) is True


# ── knowledge ───────────────────────────────────────────────────────────────


class TestKnowledgeLabel:
    def test_base_extractor(self, rows):
        from nexus.aspect_extractor import select_config

        cfg = select_config(_KNOW)
        assert cfg is not None and cfg.extractor_name == "scholarly-paper-v1"

    def test_quality_boost(self, rows):
        from nexus.scoring import apply_quality_boost

        r = _hit(_KNOW)
        apply_quality_boost([r])
        assert r.hybrid_score > 0.5

    def test_note_identity(self, rows):
        from nexus.aspect_readers import _identity_fields_for, _legacy_identity_fields_for, uri_for

        assert uri_for(_KNOW, "a-title") == "nx-note://c1002/a-title"
        assert _identity_fields_for(_KNOW) == ("doc_id",)
        assert _legacy_identity_fields_for(_KNOW) == ("source_path", "title")

    def test_a_note_record_with_no_uri_is_attributable_by_title(self, rows):
        from nexus.aspect_worker import _is_unattributable

        assert _is_unattributable(_record(_KNOW)) is False

    def test_threshold_overfetch_and_chunk_type(self, rows):
        from nexus.metadata_schema import chunk_content_type_for_collection
        from nexus.search_engine import _overfetch_multiplier, _threshold_for_collection

        cfg = {"search": {"distance_threshold": {"knowledge": 0.65, "default": 0.5}}}
        assert _threshold_for_collection(_KNOW, cfg) == 0.65
        assert _overfetch_multiplier(_KNOW) == 4
        assert chunk_content_type_for_collection(_KNOW) == "prose"


# ── rdr ─────────────────────────────────────────────────────────────────────


class TestRdrLabel:
    def test_frontmatter_extractor(self, rows):
        from nexus.aspect_extractor import select_config

        cfg = select_config(_RDR)
        assert cfg is not None and cfg.extractor_name == "rdr-frontmatter-v1"

    def test_prose_rule_does_not_apply_to_a_registered_extractor_collection(self, rows):
        from nexus.aspect_extractor import extraction_applies_to_source

        assert extraction_applies_to_source(_RDR, "/r/data.json") is True

    def test_quality_boost(self, rows):
        from nexus.scoring import apply_quality_boost

        r = _hit(_RDR)
        apply_quality_boost([r])
        assert r.hybrid_score > 0.5

    def test_file_routed_identity(self, rows):
        from nexus.aspect_readers import _identity_fields_for, uri_for
        from nexus.aspect_worker import _is_unattributable

        assert uri_for(_RDR, "/abs/rdr-1.md") == "file:///abs/rdr-1.md"
        assert _identity_fields_for(_RDR) == ("doc_id",)
        assert _is_unattributable(_record(_RDR)) is True

    def test_chunk_content_type_is_markdown(self, rows):
        from nexus.metadata_schema import chunk_content_type_for_collection

        assert chunk_content_type_for_collection(_RDR) == "markdown"


# ── code ────────────────────────────────────────────────────────────────────


class TestCodeLabel:
    def test_search_threshold_and_overfetch(self, rows):
        from nexus.search_engine import _overfetch_multiplier, _threshold_for_collection

        cfg = {"search": {"distance_threshold": {"code": 0.45, "knowledge": 0.65, "default": 0.55}}}
        assert _threshold_for_collection(_CODE, cfg) == 0.45
        assert _overfetch_multiplier(_CODE) == 2

    def test_chunk_content_type(self, rows):
        from nexus.metadata_schema import chunk_content_type_for_collection

        assert chunk_content_type_for_collection(_CODE) == "code"

    def test_no_quality_boost_and_no_topic_route(self, rows):
        from nexus.scoring import apply_quality_boost
        from nexus.topic_route import is_prose

        r = _hit(_CODE)
        apply_quality_boost([r])
        assert r.hybrid_score == 0.5
        assert is_prose(_CODE) is False

    def test_file_routed_identity(self, rows):
        from nexus.aspect_readers import uri_for
        from nexus.aspect_worker import _is_unattributable

        assert uri_for(_CODE, "/abs/m.py") == "file:///abs/m.py"
        assert _is_unattributable(_record(_CODE)) is True

    def test_the_shipped_default_is_a_content_type_rule_not_a_code_glob(self):
        from nexus.config import _DEFAULTS

        tax = _DEFAULTS["taxonomy"]
        assert tax["local_exclude_collections"] == []
        assert tax["local_exclude_content_types"] == ["code"]

    def test_local_mode_taxonomy_exclusion_under_the_shipped_default(self, rows, monkeypatch):
        """A code ROW is excluded in local mode, whatever its label; no other content type is."""
        from nexus.collection_route import content_type_excluded, taxonomy_local_exclude_types
        from nexus.config import _DEFAULTS

        monkeypatch.setattr("nexus.config.is_local_mode", lambda: True)
        monkeypatch.setattr("nexus.config.load_config", lambda: _DEFAULTS)
        types = taxonomy_local_exclude_types()
        assert [content_type_excluded(n, types) for n in (_CODE, _DOCS, _KNOW, _RDR)] == [True, False, False, False]

    def test_cloud_mode_excludes_nothing(self, rows, monkeypatch):
        from nexus.collection_route import content_type_excluded, taxonomy_local_exclude_types
        from nexus.config import _DEFAULTS

        monkeypatch.setattr("nexus.config.is_local_mode", lambda: False)
        monkeypatch.setattr("nexus.config.load_config", lambda: _DEFAULTS)
        assert taxonomy_local_exclude_types() == []
        assert content_type_excluded(_CODE, taxonomy_local_exclude_types()) is False

    def test_the_per_flush_hook_skips_a_code_row_in_local_mode(self, rows, monkeypatch):
        from unittest.mock import MagicMock

        import nexus.mcp_infra as infra
        from nexus.config import _DEFAULTS

        monkeypatch.setattr("nexus.config.is_local_mode", lambda: True)
        monkeypatch.setattr("nexus.config.load_config", lambda: _DEFAULTS)
        monkeypatch.setattr("nexus.db.http_vector_client.is_service_backed", lambda t3: True)
        monkeypatch.setattr(infra, "get_t3", lambda: MagicMock())
        monkeypatch.setattr(infra, "taxonomy_deferral", lambda: True)
        deferred = MagicMock()
        monkeypatch.setattr(infra, "_record_taxonomy_deferred", deferred)
        infra.taxonomy_assign_batch_hook(["aa"], _CODE, [""], None, None)
        assert not deferred.called
        infra.taxonomy_assign_batch_hook(["aa"], _DOCS, [""], None, None)
        assert deferred.called

    def test_a_user_pattern_stays_a_label_pattern(self, rows, monkeypatch):
        from nexus.commands.taxonomy_cmd import _enumerate_discoverable_collections

        t3 = _service_listing(monkeypatch)
        assert sorted(_enumerate_discoverable_collections(t3, ["c1002*"])) == sorted([_CODE, _DOCS, _RDR])

    def test_discovery_enumeration_skips_a_code_row(self, rows, monkeypatch):
        from nexus.commands.taxonomy_cmd import _enumerate_discoverable_collections

        t3 = _service_listing(monkeypatch)
        got = _enumerate_discoverable_collections(t3, [], exclude_content_types=["code"])
        assert sorted(got) == [_DOCS, _KNOW, _RDR]

    def test_the_registry_post_pass_skips_a_code_row_in_local_mode(self, rows, monkeypatch):
        from nexus.commands.index import _collections_from_registry_info
        from nexus.config import _DEFAULTS

        monkeypatch.setattr("nexus.config.is_local_mode", lambda: True)
        monkeypatch.setattr("nexus.config.load_config", lambda: _DEFAULTS)
        info = {"code_collection": _CODE, "docs_collection": _DOCS, "rdr_collection": _RDR}
        assert _collections_from_registry_info(info) == [_DOCS, _RDR]


def _service_listing(monkeypatch: pytest.MonkeyPatch) -> object:
    """A service-backed handle whose live listing is the four content-typed rows."""
    listing = [{"name": n, "count": 9, "content_type": ct} for n, ct in _ROWS.items()]
    handle = _ListingT3(listing)
    monkeypatch.setattr("nexus.db.http_vector_client.is_service_backed", lambda t3: True)
    monkeypatch.setattr("nexus.db.http_vector_client.live_collection_rows", lambda t3: list(listing))
    return handle


# ── the name is never read ──────────────────────────────────────────────────


class TestTheNameIsNeverRead:
    def test_a_docs_named_collection_whose_row_is_code_routes_as_code(self, rows):
        from nexus.metadata_schema import chunk_content_type_for_collection
        from nexus.search_engine import _overfetch_multiplier

        rows["docs__x__m__v1"] = "code"
        assert chunk_content_type_for_collection("docs__x__m__v1") == "code"
        assert _overfetch_multiplier("docs__x__m__v1") == 2

    def test_a_knowledge_named_collection_whose_row_is_rdr_routes_as_rdr(self, rows):
        from nexus.aspect_extractor import select_config

        rows["knowledge__x__m__v1"] = "rdr"
        assert select_config("knowledge__x__m__v1").extractor_name == "rdr-frontmatter-v1"



def test_a_collection_a_write_may_be_creating_is_stamped_by_its_candidate_name_when_no_row_backs_it(rows):
    """Item 1's NoRow outcome (the non-stranding guard of item 9): a note put into a new ``code__`` collection
    is stamped ``code`` and takes no entity key; a row, when there is one, wins over the name."""
    from nexus.entity_keys import is_code_collection
    from nexus.metadata_schema import chunk_content_type_for_collection

    assert chunk_content_type_for_collection("code__brandnew") == "code"
    assert chunk_content_type_for_collection("knowledge__brandnew") == "prose"
    assert is_code_collection("code__brandnew") is True
    rows["code__brandnew"] = "docs"
    assert chunk_content_type_for_collection("code__brandnew") == "prose"
    assert is_code_collection("code__brandnew") is False


# ── listing rows ────────────────────────────────────────────────────────────


class _ListingT3:
    def __init__(self, listing: list[dict]) -> None:
        self._listing = listing

    def list_collections(self, *a, **k) -> list[dict]:
        return list(self._listing)

    def list_live_collections(self, *a, **k) -> list[dict]:
        return list(self._listing)


def test_the_pdf_stub_check_scans_the_rows_whose_content_type_is_prose(monkeypatch):
    from nexus import health

    listing = [
        {"name": n, "count": 3, "content_type": ct}
        for n, ct in (("c2001", "knowledge"), ("c2002", "docs"), ("c2003", "code"), ("c2004", "rdr"))
    ]
    # a quarantine sibling's row carries its origin's content type; its state, not its name, leaves it out
    listing.append({"name": "c2005", "count": 3, "content_type": "knowledge", "lifecycle_state": "quarantine"})
    monkeypatch.setattr("nexus.db.make_t3", lambda *a, **k: _ListingT3(listing))
    monkeypatch.setattr(health, "_pdf_stub_route", lambda: None)
    scanned: list[str] = []

    def _scan(t3, name, page_size):
        scanned.append(name)
        return {}, health._StubScanCoverage()

    monkeypatch.setattr(health, "_pdf_stubs_in_collection", _scan)
    health._check_pdf_stub_metadata()
    assert sorted(scanned) == ["c2001", "c2002"]


def test_the_visible_check_lists_the_knowledge_rows():
    from nexus.commands.catalog_cmds import doctor

    listing = [
        {"name": "c3001", "count": 1, "content_type": "knowledge"},
        {"name": "c3002", "count": 1, "content_type": "knowledge"},
        {"name": "c3003", "count": 1, "content_type": "docs"},
        {"name": "knowledge__named", "count": 1, "content_type": "code"},
        {"name": "c3004", "count": 1, "content_type": "knowledge", "lifecycle_state": "quarantine"},
    ]
    report = doctor._run_visible_outside_manifest(t3=_ListingT3(listing), time_budget_s=0)
    assert report["collections_total"] == 2


# ── the remaining row readers ───────────────────────────────────────────────


def test_the_note_tags_rung_scans_a_knowledge_row_and_skips_the_rest(rows):
    from nexus.upgrade_ladder.rungs.note_tags_array import NoteTagsArrayRung

    seen: list[str] = []

    class _Reader:
        def distinct_doc_collections(self):
            return [_DOCS, _KNOW, "knowledge__named"]

        def list_by_collection(self, name):
            seen.append(name)
            return []

        def close(self):
            pass

    rung = NoteTagsArrayRung(reader_factory=lambda: _Reader())
    rung._scan(_Reader())
    assert seen == [_KNOW]


def test_the_file_routed_content_types_are_the_frozen_prefix_list_less_its_separator():
    """The row-read list and the URI-string classifiers' frozen list (RDR-231) name the same three types."""
    from nexus.aspect_readers import FILE_ROUTED_CONTENT_TYPES, FILE_ROUTED_PREFIXES

    assert [f"{t}__" for t in FILE_ROUTED_CONTENT_TYPES] == list(FILE_ROUTED_PREFIXES)


def test_backfill_candidates_are_the_rows_that_route_to_a_file(rows):
    from nexus.aspect_readers import is_file_routed_collection

    assert [is_file_routed_collection(n) for n in (_DOCS, _KNOW, _RDR, _CODE)] == [True, False, True, True]


def test_the_exporter_stamps_notes_only_in_a_non_file_routed_collection(rows):
    """A file-routed collection holds no notes: nothing is read; a knowledge one reads the catalog."""
    from unittest.mock import MagicMock

    from nexus.exporter import _OwnerImport

    def _imp(label: str) -> _OwnerImport:
        imp = _OwnerImport(
            db=object(), collection_name=label, hooks=MagicMock(), embedding_model="m",
            file_hash="h", skip_existing=False,
        )
        imp._reader = MagicMock()
        imp._reader.resolve_many.side_effect = RuntimeError("catalog read")
        return imp

    docs = _imp(_DOCS)
    assert docs._stamp_note_keys(["1.1.1"]) == ["1.1.1"]
    assert not docs._reader.resolve_many.called
    know = _imp(_KNOW)
    know._stamp_note_keys(["1.1.1"])
    assert know._reader.resolve_many.called


def _record(collection: str):
    from nexus.db.t2.records import AspectRecord  # noqa: PLC0415

    return AspectRecord(
        collection=collection, source_path="p", problem_formulation="x", proposed_method="y",
        experimental_datasets=[], experimental_baselines=[], experimental_results="z",
        extras={}, confidence=0.5, extracted_at="2026-11-20T00:00:00Z",
        model_version="m", extractor_name="e", source_uri="",
    )
