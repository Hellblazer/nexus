# SPDX-License-Identifier: AGPL-3.0-or-later
"""RDR-231 Step 2 (nexus-bivz3.11): every client mint, reader, scheme set
and label for a note's source_uri speaks ``nx-note://``, not ``chroma://``.

The helpers (``note_uri``, ``canonical_note_uri``, ``NOTE_URI_SCHEME``) are
nexus-bivz3.10's and are pinned in ``tests/test_aspect_readers.py``; this
file pins that the SITES use them. Each pin fails when its site is put back.
"""
from __future__ import annotations

from pathlib import Path

import pytest

import nexus
from nexus import aspect_readers
from nexus.catalog.types import _KNOWN_URI_SCHEMES
from nexus.doc.catalog_links import _LINK_SAFE_SCHEMES
from nexus.doctor_references import format_report, is_reference_only, stat_one
from nexus.aspect_readers import (
    ReadFail,
    StatFail,
    read_source,
    stat_source,
    uri_for,
)

_SRC = Path(nexus.__file__).resolve().parent


class TestUriForMintsNoteScheme:
    def test_knowledge_collection(self) -> None:
        assert uri_for("knowledge__delos", "decision-x") == (
            "nx-note://knowledge__delos/decision-x"
        )

    def test_future_prefix_still_a_note(self) -> None:
        assert uri_for("future__x", "src") == "nx-note://future__x/src"

    def test_file_routed_prefix_unchanged(self) -> None:
        assert uri_for("docs__x", "/a/b.md") == "file:///a/b.md"


class TestReaderAndStatRegistries:
    def test_readers_key_nx_note_and_drop_chroma(self) -> None:
        assert "nx-note" in aspect_readers._READERS
        assert "chroma" not in aspect_readers._READERS

    def test_stat_handlers_key_nx_note_and_drop_chroma(self) -> None:
        assert "nx-note" in aspect_readers._STAT_HANDLERS
        assert "chroma" not in aspect_readers._STAT_HANDLERS

    def test_stat_nx_note_is_fresh(self) -> None:
        result = stat_source("nx-note://knowledge__x/some-doc")
        assert not isinstance(result, StatFail)
        assert result.current_mtime is None

    def test_stat_chroma_residue_is_scheme_unknown(self) -> None:
        result = stat_source("chroma://knowledge__x/some-doc")
        assert isinstance(result, StatFail)
        assert result.reason == "scheme_unknown"

    def test_read_chroma_residue_is_scheme_unknown(self) -> None:
        result = read_source("chroma://knowledge__x/some-doc", t3=object())
        assert isinstance(result, ReadFail)
        assert result.reason == "scheme_unknown"

    def test_read_nx_note_dispatches_to_the_note_reader(self) -> None:
        # no t3 -> the note reader's own "no client" failure, not scheme_unknown
        result = read_source("nx-note://knowledge__x/some-doc")
        assert isinstance(result, ReadFail)
        assert result.reason != "scheme_unknown"


class TestSchemeAllowlists:
    def test_known_schemes_gain_nx_note_and_drop_chroma(self) -> None:
        assert "nx-note" in _KNOWN_URI_SCHEMES
        assert "chroma" not in _KNOWN_URI_SCHEMES

    def test_link_safe_schemes_exclude_nx_note_by_name(self) -> None:
        assert "nx-note" not in _LINK_SAFE_SCHEMES
        assert "chroma" not in _LINK_SAFE_SCHEMES


class TestDoctorReferencesResidueRow:
    """``nx doctor --check-references``: a ``chroma://`` residue row is an
    unknown scheme (staleness 'unknown', never a failure); a note is fresh."""

    class _Entry:
        def __init__(self, uri: str) -> None:
            self.source_uri = uri
            self.meta: dict = {}
            self.tumbler = "1.1.1"
            self.title = "t"
            self.source_mtime = 0.0

    def test_chroma_residue_reads_unknown(self) -> None:
        uri = "chroma://knowledge__x/old-note"
        assert is_reference_only(uri) is True  # still walked
        r = stat_one(self._Entry(uri), http_client=None)
        assert r.error is None
        assert r.scheme == "chroma"
        assert r.signal == "unknown"

    def test_nx_note_reads_fresh(self) -> None:
        r = stat_one(self._Entry("nx-note://knowledge__x/note"), http_client=None)
        assert r.scheme == "nx-note"
        assert r.signal == "fresh"

    def test_residue_row_does_not_fail_the_report(self) -> None:
        r = stat_one(self._Entry("chroma://knowledge__x/old-note"), http_client=None)
        lines, ok = format_report([r], total_candidates=1, sample=10, seed=1)
        assert ok is True
        assert any("1 unknown" in ln for ln in lines)
