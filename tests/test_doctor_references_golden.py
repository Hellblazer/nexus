# SPDX-License-Identifier: AGPL-3.0-or-later
"""nexus-uwggq.1.4 (RDR-241 Phase 1): the ``nx doctor --check-references`` report, pinned.

``reference_only_candidates`` walked the whole catalog (``all_documents(limit=0)``) when this golden was
first written; nexus-uwggq.1.5 replaced that walk with one read of the engine's by-scheme route per
registered non-file scheme (RDR-241 Phase 2; Sam 2026-11-16, ask uwggq15-scheme-case option C). RDR-241
allows the report to differ only by rows whose scheme has no stat handler (T2 nexus_rdr/241-research-5),
and the .1.5 regeneration differs from the walk's report by exactly those two rows (``chroma://`` and the
drive path), with the upper-case ``HTTPS://`` row kept.

The golden (``tests/fixtures/doctor_references_check_report.golden.txt``) is the real output of
``run_check_references`` plus its exit code, generated from the code with
``NX_REGEN_DOCTOR_REFERENCES_GOLDEN=1 uv run pytest tests/test_doctor_references_golden.py``, never typed.

Fixture rows (one fake catalog; ``FixtureCatalog.documents_by_scheme`` answers as the engine route does,
``urlparse(source_uri).scheme`` equal to the scheme lowercased, alias rows included):

* one row per registered non-file scheme: obsidian, x-devonthink-item, nx-scratch, nx-note, https;
* a ``chroma://`` row (scheme registered nowhere);
* a Windows drive path ``C:\\Users\\a\\x.md`` (``urlparse`` reads scheme ``c``, registered nowhere);
* an upper-case registered scheme ``HTTPS://example.org/x`` (``urlparse`` lower-cases it to ``https``);
* a ``file://`` row and an alias row (both excluded).

I/O handlers are stubbed through ``_STAT_HANDLERS`` (https: network, x-devonthink-item: osascript,
obsidian and nx-scratch: vault and scratch context), so ``stat_source`` itself stays real and the
unregistered schemes take the real ``scheme_unknown`` path. ``nx-note`` keeps its real handler.
``sample=50, seed=1`` is larger than the candidate count, so ``sample_candidates`` returns every row and
no random draw happens. ``ThreadPoolExecutor.map`` keeps input order, so the output is deterministic.
"""
from __future__ import annotations

import os
from pathlib import Path
from typing import Any
from urllib.parse import urlparse

import pytest

from nexus import aspect_readers, doctor_references
from nexus.aspect_readers import HTTPS_ETAG_META_KEY, StatFail, StatOk

GOLDEN = Path(__file__).parent / "fixtures" / "doctor_references_check_report.golden.txt"
_REGEN_ENV = "NX_REGEN_DOCTOR_REFERENCES_GOLDEN"

#: The ETag the stubbed https handler reports for every https URL.
_CURRENT_ETAG = '"v2"'


class FixtureEntry:
    """The ``CatalogEntry`` fields ``doctor_references`` reads."""

    def __init__(
        self, tumbler: str, title: str, source_uri: str, *,
        alias_of: str = "", source_mtime: float = 0.0, meta: dict[str, Any] | None = None,
    ) -> None:
        self.tumbler = tumbler
        self.title = title
        self.alias_of = alias_of
        self.source_uri = source_uri
        self.source_mtime = source_mtime
        self.meta = meta or {}


class FixtureCatalog:
    """Serves the fixture rows through ``documents_by_scheme``, the engine's by-scheme route as the client
    reads it (nexus-uwggq.1.5), and refuses a walk."""

    def __init__(self, entries: list[FixtureEntry]) -> None:
        self._entries = entries

    def all_documents(self, limit: int = 0) -> list[FixtureEntry]:
        raise AssertionError("--check-references must not walk the catalog (RDR-241)")

    def documents_by_scheme(self, scheme: str) -> list[FixtureEntry]:
        return [e for e in self._entries if urlparse(e.source_uri).scheme == scheme.lower()]


def fixture_entries() -> list[FixtureEntry]:
    return [
        FixtureEntry("1.1.1", "Obsidian note", "obsidian://open?vault=v&file=a.md", source_mtime=100.0),
        FixtureEntry("1.1.2", "DEVONthink item", "x-devonthink-item://UUID-1", source_mtime=100.0),
        FixtureEntry("1.1.3", "Scratch entry", "nx-scratch://session/s1/e1"),
        FixtureEntry("1.1.4", "Note", "nx-note://knowledge__x/doc"),
        FixtureEntry(
            "1.1.5", "Web page", "https://example.org/page",
            meta={HTTPS_ETAG_META_KEY: _CURRENT_ETAG},
        ),
        FixtureEntry("1.1.6", "Legacy note", "chroma://knowledge__x/legacy"),
        FixtureEntry("1.1.7", "Windows path", "C:\\Users\\a\\x.md"),
        FixtureEntry(
            "1.1.8", "Upper-case web page", "HTTPS://example.org/x",
            meta={HTTPS_ETAG_META_KEY: '"v1"'},
        ),
        FixtureEntry("1.1.9", "File doc", "file:///tmp/a.md"),
        FixtureEntry("1.1.10", "Alias web page", "https://example.org/alias", alias_of="1.1.5"),
    ]


def _stub_https(uri: str, *, recorded_etag: str | None = None, **_kw: Any) -> StatOk:
    return StatOk(current_mtime=None, etag_stale=(recorded_etag != _CURRENT_ETAG))


def _stub_obsidian(uri: str, **_kw: Any) -> StatOk:
    return StatOk(current_mtime=200.0)  # recorded 100.0 < 200.0 -> stale


def _stub_devonthink(uri: str, **_kw: Any) -> StatFail:
    return StatFail(reason="absent", detail="gone")  # -> dangling


def _stub_scratch(uri: str, **_kw: Any) -> StatOk:
    return StatOk(current_mtime=None)  # -> fresh


def render_report(monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]) -> str:
    """Run ``run_check_references`` on the fixture; return its stdout plus the exit code."""
    monkeypatch.setitem(aspect_readers._STAT_HANDLERS, "https", _stub_https)
    monkeypatch.setitem(aspect_readers._STAT_HANDLERS, "obsidian", _stub_obsidian)
    monkeypatch.setitem(aspect_readers._STAT_HANDLERS, "x-devonthink-item", _stub_devonthink)
    monkeypatch.setitem(aspect_readers._STAT_HANDLERS, "nx-scratch", _stub_scratch)
    monkeypatch.setattr(
        "nexus.catalog.factory.make_catalog_reader", lambda: FixtureCatalog(fixture_entries()),
    )
    exit_code = 0
    try:
        doctor_references.run_check_references(sample=50, seed=1)
    except SystemExit as exc:
        exit_code = exc.code if isinstance(exc.code, int) else 1
    return capsys.readouterr().out + f"exit code: {exit_code}\n"


def test_check_references_report_matches_golden(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str],
) -> None:
    report = render_report(monkeypatch, capsys)
    if os.environ.get(_REGEN_ENV) == "1":
        GOLDEN.write_text(report, encoding="utf-8")
        # A regen run never reports green: it compared nothing, so it fails on purpose.
        pytest.fail(f"golden regenerated at {GOLDEN}; unset {_REGEN_ENV}, review the diff and rerun")
    assert GOLDEN.read_text(encoding="utf-8") == report



def test_the_fixture_has_a_row_for_every_registered_non_file_scheme() -> None:
    """The golden pins each registered scheme only if the fixture holds a row of it. A scheme added to
    the stat registry without a row here would leave the golden green and that scheme unpinned."""
    covered = {urlparse(e.source_uri).scheme for e in fixture_entries()}
    registered = set(aspect_readers._STAT_HANDLERS) - {"file"}  # file:// rows are not reference-only
    assert registered <= covered, f"registered schemes with no fixture row: {sorted(registered - covered)}"
