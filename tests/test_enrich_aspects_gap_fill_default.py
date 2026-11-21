# SPDX-License-Identifier: AGPL-3.0-or-later
"""nexus-ym9ey: `nx enrich aspects` gap-fills by default instead of re-extracting.

Before this, a bare invocation re-extracted every document in the collection on
every run. Measured on the live install 2026-08-24: three consecutive runs over
rdr__1-1__voyage-context-3__v1 each reported "294 extracted", none of them
filling a gap. Free for a deterministic parser; on a Claude-CLI-backed
collection it re-spends on the whole corpus to fill one hole.
"""
from __future__ import annotations

from types import SimpleNamespace

import pytest

from nexus.commands.enrich import _select_entries


def _entry(path: str) -> SimpleNamespace:
    return SimpleNamespace(file_path=path, title=path)


@pytest.fixture()
def wiring(monkeypatch: pytest.MonkeyPatch):
    """Catalog holds three documents; two of them already have aspects."""
    import nexus.commands.enrich as mod

    catalog = [_entry("a.md"), _entry("b.md"), _entry("c.md")]
    have_aspects = {"a.md", "b.md"}

    monkeypatch.setattr(
        "nexus.catalog.factory.make_catalog_reader",
        lambda: SimpleNamespace(list_by_collection=lambda _c: list(catalog)),
    )

    class _Aspects:
        def list_by_collection(self, _c):
            return [SimpleNamespace(source_path=p) for p in sorted(have_aspects)]

        def list_by_extractor_version(self, _n, _v):
            return []

    class _DB:
        document_aspects = _Aspects()
        def __enter__(self): return self
        def __exit__(self, *a): return False

    monkeypatch.setattr("nexus.db.t2.T2Database", lambda *_a, **_k: _DB())
    monkeypatch.setattr("nexus.config.default_db_path", lambda: ":memory:")
    monkeypatch.setattr(mod.click, "echo", lambda *a, **k: None)
    return None


def _paths(entries) -> set[str]:
    return {e.file_path for e in entries}


class TestGapFillDefault:
    def test_default_selects_only_documents_without_aspects(self, wiring) -> None:
        """The whole point: one missing document costs one extraction."""
        got = _select_entries(
            collection="knowledge__x", re_extract=False,
            extractor_version="",
        )
        assert _paths(got) == {"c.md"}

    def test_all_flag_restores_full_re_extraction(self, wiring) -> None:
        """The old default remains reachable, explicitly."""
        got = _select_entries(
            collection="knowledge__x", re_extract=False, extract_all=True,
            extractor_version="",
        )
        assert _paths(got) == {"a.md", "b.md", "c.md"}

    def test_re_extract_path_is_unchanged(self, wiring) -> None:
        """--re-extract still means outdated-or-missing, not gap-fill.

        With no rows below the version threshold, it selects exactly the
        documents that have no aspect row at all.
        """
        got = _select_entries(
            collection="knowledge__x", re_extract=True,
            extractor_version="v2",
        )
        assert _paths(got) == {"c.md"}

    def test_a_fully_covered_collection_selects_nothing(self, monkeypatch) -> None:
        """Zero gaps must cost zero extractions, not a full re-run."""
        import nexus.commands.enrich as mod
        catalog = [_entry("a.md")]

        class _Aspects:
            def list_by_collection(self, _c):
                return [SimpleNamespace(source_path="a.md")]
            def list_by_extractor_version(self, _n, _v):
                return []

        class _DB:
            document_aspects = _Aspects()
            def __enter__(self): return self
            def __exit__(self, *a): return False

        monkeypatch.setattr(
            "nexus.catalog.factory.make_catalog_reader",
            lambda: SimpleNamespace(list_by_collection=lambda _c: list(catalog)),
        )
        monkeypatch.setattr("nexus.db.t2.T2Database", lambda *_a, **_k: _DB())
        monkeypatch.setattr("nexus.config.default_db_path", lambda: ":memory:")
        monkeypatch.setattr(mod.click, "echo", lambda *a, **k: None)

        got = _select_entries(
            collection="knowledge__x", re_extract=False,
            extractor_version="",
        )
        assert got == []


class TestReExtractConsidersEveryEligibleExtractor:
    """nexus-kk4ut: --re-extract must catch an outdated general-prose-v1 row.

    A knowledge__* document is routed PER DOCUMENT (nexus-kmbys): one shaped
    like a paper gets scholarly-paper-v1, one shaped like prose gets
    general-prose-v1, and each writes its row under THAT extractor_name.
    Before this fix, `--re-extract`'s outdated-version query was pinned to
    `select_config(collection).extractor_name` alone -- the prefix's BASE
    config, scholarly-paper-v1 for knowledge__* -- so a general-prose-v1 row
    never matched it and sat below the version threshold forever, invisible
    to --re-extract no matter how outdated it was.
    """

    def test_outdated_general_prose_row_is_selected(self, monkeypatch) -> None:
        import nexus.commands.enrich as mod

        catalog = [_entry("a.md"), _entry("b.md")]

        # a.md: a CURRENT scholarly-paper-v1 row (not outdated -- excluded).
        # b.md: an OUTDATED general-prose-v1 row -- the row this fix catches.
        rows_by_extractor = {
            "scholarly-paper-v1": [],
            "general-prose-v1": [SimpleNamespace(source_path="b.md")],
        }

        class _Aspects:
            def list_by_collection(self, _c):
                return [SimpleNamespace(source_path=p) for p in ("a.md", "b.md")]

            def list_by_extractor_version(self, extractor_name, _max_version):
                # Simulates the real engine's `WHERE extractor_name = ? AND
                # model_version < ?` (AspectRepository.listByExtractorVersion):
                # a query pinned to the wrong extractor_name returns nothing,
                # exactly as production does -- this is what makes the test
                # fail against the pre-fix single-name query.
                return list(rows_by_extractor.get(extractor_name, []))

        class _DB:
            document_aspects = _Aspects()
            def __enter__(self): return self
            def __exit__(self, *a): return False

        monkeypatch.setattr(
            "nexus.catalog.factory.make_catalog_reader",
            lambda: SimpleNamespace(list_by_collection=lambda _c: list(catalog)),
        )
        monkeypatch.setattr("nexus.db.t2.T2Database", lambda *_a, **_k: _DB())
        monkeypatch.setattr("nexus.config.default_db_path", lambda: ":memory:")
        monkeypatch.setattr(mod.click, "echo", lambda *a, **k: None)

        got = _select_entries(
            collection="knowledge__x", re_extract=True,
            extractor_version="claude-haiku-4-5-20260101",
        )
        assert _paths(got) == {"b.md"}
