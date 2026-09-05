# SPDX-License-Identifier: AGPL-3.0-or-later
"""Tombstone (nexus-i711w terminal deletion): the local SQLite ``Catalog``
unit suite that lived here — 109 tests over register/resolve/link/span/alias/
rebuild/guard behavior of ``nexus.catalog.catalog.Catalog`` — retired with the
module itself. The service catalog's contract coverage lives in
``tests/catalog/`` (conformance, protocol fidelity, shape parity).

The one substrate-independent test survives below: it locks the source-URI
scheme registry in ``nexus.catalog.types``, which outlived the local catalog.
"""

from __future__ import annotations


class TestSourceUriRegistration:
    def test_known_uri_schemes_table_is_locked_to_planned_set(self):
        """Lock the scheme registry against silent additions OR
        shrinking. Phase 1: ``file`` + ``chroma``. Phase 4:
        ``nx-scratch`` (P4.1) + ``https`` (P4.2). nexus-bqda adds
        ``x-devonthink-item`` (macOS-only DT identity URLs).
        nexus-h2pm adds ``nx-orphan-backfill`` for synthetic
        Documents covering pre-catalog T3 chunks. Plain
        ``http`` is intentionally excluded — Phase 4's https reader
        does NOT cover plain http, so accepting http URIs at register
        would succeed silently and fail at extraction. Adding a new
        scheme requires landing the reader first AND updating this
        lock.
        """
        from nexus.catalog.types import _KNOWN_URI_SCHEMES
        assert _KNOWN_URI_SCHEMES == frozenset({
            "file", "chroma", "https", "nx-scratch", "x-devonthink-item",
            "nx-orphan-backfill",
        })


class TestIsFixturePath:
    """nexus-4jj40 (RDR-200 Phase 1c evidence hygiene, review round 3):
    segment-anchored ``tests/fixtures/`` detection driving the
    ``non_evidentiary`` auto-stamp at register time (see
    ``nexus.indexer._register_time_meta``).
    """

    def test_matches_top_level_fixtures_dir(self) -> None:
        from nexus.catalog.types import is_fixture_path

        assert is_fixture_path("tests/fixtures/sample.json")

    def test_matches_nested_fixtures_dir(self) -> None:
        from nexus.catalog.types import is_fixture_path

        assert is_fixture_path("service/src/tests/fixtures/data/sample.json")

    def test_matches_the_bare_directory_itself(self) -> None:
        from nexus.catalog.types import is_fixture_path

        assert is_fixture_path("tests/fixtures")

    def test_sibling_test_module_does_not_match(self) -> None:
        """A test MODULE beside tests/fixtures/, not under it."""
        from nexus.catalog.types import is_fixture_path

        assert not is_fixture_path("tests/test_operator_dispatch.py")

    def test_similar_dirname_does_not_false_positive(self) -> None:
        """Segment-anchored: neither half may be a substring match."""
        from nexus.catalog.types import is_fixture_path

        assert not is_fixture_path("mytests/fixtures/sample.json")
        assert not is_fixture_path("tests/myfixtures/sample.json")

    def test_real_code_does_not_match(self) -> None:
        from nexus.catalog.types import is_fixture_path

        assert not is_fixture_path("src/nexus/plans/runner.py")


class TestRegisterTimeMeta:
    """The write side of the same stamp -- ``nexus.indexer.
    _register_time_meta`` -- builds the register/update ``meta`` dict a
    fixture path gets vs. an ordinary file."""

    def test_fixture_path_gets_stamped(self) -> None:
        from nexus.indexer import _register_time_meta

        meta = _register_time_meta("tests/fixtures/sample.json", "abc123")
        assert meta == {"content_hash": "abc123", "non_evidentiary": True}

    def test_ordinary_path_is_not_stamped(self) -> None:
        from nexus.indexer import _register_time_meta

        meta = _register_time_meta("src/nexus/plans/runner.py", "abc123")
        assert meta == {"content_hash": "abc123"}

    def test_sibling_test_module_is_not_stamped(self) -> None:
        from nexus.indexer import _register_time_meta

        meta = _register_time_meta("tests/test_operator_dispatch.py", "abc123")
        assert meta == {"content_hash": "abc123"}

    def test_fixture_path_with_no_content_hash_still_stamped(self) -> None:
        """An unreadable file (empty file_hash) still gets the fixture
        stamp -- only the content_hash key is conditional."""
        from nexus.indexer import _register_time_meta

        meta = _register_time_meta("tests/fixtures/sample.json", "")
        assert meta == {"non_evidentiary": True}

    def test_ordinary_path_with_no_content_hash_yields_none(self) -> None:
        """Preserves the pre-existing "meta: None when file_hash is
        empty" behaviour exactly, for a non-fixture path."""
        from nexus.indexer import _register_time_meta

        assert _register_time_meta("src/nexus/plans/runner.py", "") is None
