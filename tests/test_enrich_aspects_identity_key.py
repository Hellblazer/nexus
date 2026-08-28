# SPDX-License-Identifier: AGPL-3.0-or-later
# Copyright (c) 2026 Hal Hildebrand. All rights reserved.
"""The gap-fill and ``--missing`` share ONE identity key. nexus-bocft.

Two verbs asked "does this catalog entry have an aspect row" with two
different keys. The gap-fill (``_select_entries``) used ``file_path or
title``; the ``--missing`` audit used ``file_path and ...``, which is not a
gap test -- it is a gap test over the subset of entries that happen to
carry a file_path, and for a knowledge collection that subset is almost
nothing. Measured 2026-08-27 on knowledge__knowledge: 416 entries, 10 with
a file_path; the gap-fill would dispatch 407 at ~$480, the audit reported
1, and that "1" was the number the 7.18.0 changelog cited as proof the
gap-fill worked.

WHY THE EXISTING TESTS COULD NOT SEE IT: ``test_enrich_aspects_gap_fill_
default.py`` builds entries with ``file_path == title``, so the two keys
coincide on every fixture row. Every fixture below has TITLE-ONLY entries,
which is what a store_put document is.
"""
from __future__ import annotations

import json
from types import SimpleNamespace

import click
import pytest
from click.testing import CliRunner

import nexus.commands.enrich as enrich_mod
from nexus.commands.enrich import _aspect_identity, _select_entries, aspects_list_cmd

COLLECTION = "knowledge__knowledge__voyage-context-3__v1"

# Captured at import, BEFORE the fixture below silences ``click.echo`` -- the
# fixture patches the click module itself (enrich imports it as ``click``),
# so "restore with click.echo" inside a test hands back the silencer.
_REAL_ECHO = click.echo


def _note(title: str) -> SimpleNamespace:
    """A store_put document: identified by title, no file_path."""
    return SimpleNamespace(tumbler=f"1.11.{abs(hash(title)) % 1000}", title=title, file_path="")


def _file(path: str) -> SimpleNamespace:
    return SimpleNamespace(tumbler=f"1.11.{abs(hash(path)) % 1000}", title=path.rsplit("/", 1)[-1], file_path=path)


@pytest.fixture()
def wiring(monkeypatch):
    """Four title-only notes and one file-backed doc; aspect rows exist for
    one note (by title), the file-backed doc (by path), and two ORPHANS whose
    identities no current entry claims (a content hash, an old absolute path)."""
    catalog = [
        _note("rdr-112-refactor-impact-inventory"),
        _note("analysis-deep-hook-bridge"),
        _note("comparison-t8code-luciferase"),
        _note("shakedown-2026-08-27-umbrella"),
        _file("/papers/attention.pdf"),
    ]
    rows = {
        "rdr-112-refactor-impact-inventory",      # covers note 1, by title
        "/papers/attention.pdf",                  # covers the file doc, by path
        "a" * 64,                                 # orphan: chash-era identity
        "/Users/x/DEVONthink/Inbox.dtBase2/old",  # orphan: earlier path rule
    }

    monkeypatch.setattr(
        "nexus.catalog.factory.make_catalog_reader",
        lambda: SimpleNamespace(list_by_collection=lambda _c: list(catalog)),
    )

    class _Aspects:
        def list_by_collection(self, _c):
            return [SimpleNamespace(source_path=p) for p in sorted(rows)]

        def list_by_extractor_version(self, _n, _v):
            return []

    class _DB:
        document_aspects = _Aspects()

        def __enter__(self):
            return self

        def __exit__(self, *a):
            return False

    monkeypatch.setattr("nexus.db.t2.T2Database", lambda *_a, **_k: _DB())
    monkeypatch.setattr("nexus.config.default_db_path", lambda: ":memory:")
    monkeypatch.setattr("nexus.commands._helpers.default_db_path", lambda: ":memory:")
    monkeypatch.setattr(enrich_mod.click, "echo", lambda *a, **k: None)
    return catalog, rows


# --------------------------------------------------------------------------
# the key
# --------------------------------------------------------------------------

def test_identity_prefers_the_path_and_falls_back_to_the_title() -> None:
    assert _aspect_identity(SimpleNamespace(file_path="/p.md", title="t")) == "/p.md"
    assert _aspect_identity(SimpleNamespace(file_path="", title="t")) == "t"
    assert _aspect_identity(SimpleNamespace(file_path=None, title="t")) == "t"
    assert _aspect_identity(SimpleNamespace(file_path="", title=None)) == ""


# --------------------------------------------------------------------------
# the two verbs agree -- THE property
# --------------------------------------------------------------------------

def _gap_fill_selection(wiring) -> list:
    return _select_entries(
        collection=COLLECTION, re_extract=False,
        extractor_version="1", config_extractor_name="x",
    )


def _missing_json(monkeypatch) -> dict:
    # --json output goes through the real click.echo; undo the fixture's silencer.
    monkeypatch.setattr(enrich_mod.click, "echo", _REAL_ECHO)
    result = CliRunner().invoke(
        aspects_list_cmd, ["--collection", COLLECTION, "--missing", "--json"],
    )
    assert result.exit_code == 0, result.output
    return json.loads(result.stdout)


def test_gap_fill_selects_the_title_only_notes_without_rows(wiring) -> None:
    catalog, _ = wiring
    got = {_aspect_identity(e) for e in _gap_fill_selection(wiring)}
    assert got == {
        "analysis-deep-hook-bridge",
        "comparison-t8code-luciferase",
        "shakedown-2026-08-27-umbrella",
    }, got


def test_missing_reports_exactly_what_the_gap_fill_would_dispatch(wiring, monkeypatch) -> None:
    """THE test. Before nexus-bocft this pair read (3, 0): the audit could not
    see the population the billing verb charges for."""
    dispatched = {_aspect_identity(e) for e in _gap_fill_selection(wiring)}
    payload = _missing_json(monkeypatch)
    audited = {g["identity"] for g in payload["gaps"]}

    assert audited == dispatched, (dispatched, audited)
    assert len(audited) == 3


def test_missing_names_the_title_for_a_title_only_gap(wiring, monkeypatch) -> None:
    payload = _missing_json(monkeypatch)
    by_identity = {g["identity"]: g for g in payload["gaps"]}
    gap = by_identity["analysis-deep-hook-bridge"]
    assert gap["title"] == "analysis-deep-hook-bridge"
    assert gap["file_path"] == ""


def test_missing_counts_the_orphaned_rows_that_explain_the_arithmetic(wiring, monkeypatch) -> None:
    """437 rows but 407 gaps only makes sense once you see the rows no entry
    claims. The audit is not complete without that number."""
    payload = _missing_json(monkeypatch)
    assert payload["entries"] == 5
    assert payload["aspect_rows"] == 4
    assert set(payload["orphaned_aspect_rows"]) == {
        "a" * 64, "/Users/x/DEVONthink/Inbox.dtBase2/old",
    }


def test_missing_text_output_carries_both_numbers(wiring, monkeypatch) -> None:
    monkeypatch.setattr(enrich_mod.click, "echo", _REAL_ECHO)
    result = CliRunner().invoke(aspects_list_cmd, ["--collection", COLLECTION, "--missing"])
    assert result.exit_code == 0, result.output
    assert "3 of 5 catalog row(s)" in result.output, result.output
    assert "analysis-deep-hook-bridge" in result.output
    assert "2 aspect row(s)" in result.output and "match no current catalog entry" in result.output


def test_a_fully_covered_collection_is_quiet(wiring, monkeypatch) -> None:
    """Non-vacuity in the clean direction: no gaps AND no orphans says so, once."""
    catalog, rows = wiring
    rows.clear()
    rows.update(_aspect_identity(e) for e in catalog)
    monkeypatch.setattr(enrich_mod.click, "echo", _REAL_ECHO)
    result = CliRunner().invoke(aspects_list_cmd, ["--collection", COLLECTION, "--missing"])
    assert result.exit_code == 0, result.output
    assert "No missing aspects" in result.output
    assert "match no current catalog entry" not in result.output


# --------------------------------------------------------------------------
# kill control: the old audit key on this fixture
# --------------------------------------------------------------------------

def test_the_old_audit_key_undercounts_this_fixture(wiring) -> None:
    """The fixture must be one the old key gets WRONG, or the agreement test
    above proves nothing. ``file_path and ...`` over these entries sees the
    one file-backed doc (covered) and reports zero gaps; the truth is three."""
    catalog, rows = wiring
    old_key_gaps = [e for e in catalog if e.file_path and e.file_path not in rows]
    assert len(old_key_gaps) == 0
    assert len([e for e in catalog if not e.file_path]) == 4, "fixture has no title-only entries"
