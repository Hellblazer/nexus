# SPDX-License-Identifier: AGPL-3.0-or-later
"""nexus-uwggq.1.4 (RDR-241 Phase 1): the ``nx doctor --check-references`` report, pinned.

``reference_only_candidates`` walks the whole catalog today (``all_documents(limit=0)``); nexus-uwggq.1.5
replaces that walk with one ``list_scoped(source_uri_prefix=...)`` read per registered scheme. RDR-241
allows the report to differ only by rows whose scheme has no stat handler (T2 nexus_rdr/241-research-5).
This test pins today's report, byte for byte, on one fixture, so that diff is visible in .1.5.

The golden (``tests/fixtures/doctor_references_check_report.golden.txt``) is the real output of
``run_check_references`` plus its exit code, generated from the code with
``NX_REGEN_DOCTOR_REFERENCES_GOLDEN=1 uv run pytest tests/test_doctor_references_golden.py``, never typed.

Fixture rows (one fake catalog; ``FixtureCatalog.list_scoped`` is the prefix read .1.5 will call, case
sensitive like the engine's ``startswith``, so .1.5 can reuse the same fake unchanged):

* one row per registered non-file scheme: obsidian, x-devonthink-item, nx-scratch, nx-note, https;
* a ``chroma://`` row (scheme registered nowhere);
* a Windows drive path ``C:\\Users\\a\\x.md`` (``urlparse`` reads scheme ``c``, registered nowhere);
* an upper-case registered scheme ``HTTPS://example.org/x`` (``urlparse`` lower-cases it to ``https``);
* a ``file://`` row and an alias row (both excluded today).

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
    """Serves the fixture rows through ``all_documents`` (today's walk) and ``list_scoped``
    (the per-scheme prefix read of nexus-uwggq.1.5), one list behind both."""

    def __init__(self, entries: list[FixtureEntry]) -> None:
        self._entries = entries

    def all_documents(self, limit: int = 0) -> list[FixtureEntry]:
        return list(self._entries)

    def list_scoped(self, *, source_uri_prefix: str = "", **_kw: Any) -> list[FixtureEntry]:
        return [e for e in self._entries if e.source_uri.startswith(source_uri_prefix)]


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
    assert GOLDEN.read_text(encoding="utf-8") == report

