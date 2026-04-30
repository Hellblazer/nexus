# SPDX-License-Identifier: AGPL-3.0-or-later
# Copyright (c) 2026 Hal Hildebrand. All rights reserved.
"""Tests for the ``nx dt`` Click command surface (RDR-099 P2).

Coverage:

* Group + subcommand registration on ``nx`` root.
* Each selector flag (``--selection``, ``--tag``, ``--group``,
  ``--smart-group``, ``--uuid``) routes to the matching
  ``nexus.devonthink._dt_*`` helper with the right keyword args.
* ``--database`` is forwarded to the helpers that accept it.
* ``--uuid`` is ``multiple=True`` — each UUID becomes its own
  ``_dt_uuid_record`` call.
* Mutual exclusion: zero or 2+ selector flags exit non-zero.
* Per-record dispatch by extension: ``.pdf`` and ``.md`` go to the
  shared ``_index_record`` helper; other extensions are skipped.
* ``--dry-run``: records are listed on stdout, zero indexer calls.
* ``--collection`` and ``--corpus`` flags are forwarded to the
  per-record dispatcher.
* Error paths: ``DTNotAvailableError`` and non-darwin platform both
  exit non-zero with operator-friendly messages on stdout/stderr.

Tests run on every platform via fake-helper monkeypatching plus
``monkeypatch.setattr("sys.platform", ...)``.
"""
from __future__ import annotations

from unittest.mock import MagicMock

import pytest
from click.testing import CliRunner


@pytest.fixture
def runner() -> CliRunner:
    return CliRunner()


@pytest.fixture
def fake_selectors(monkeypatch) -> dict[str, MagicMock]:
    """Replace each ``nexus.devonthink._dt_*`` selector with a
    ``MagicMock`` so tests can assert call args/counts and stub return
    values. Default return is the empty list, mirroring "no records".
    """
    import nexus.devonthink as dt_mod  # noqa: PLC0415

    fakes = {
        "selection": MagicMock(return_value=[]),
        "uuid": MagicMock(return_value=[]),
        "tag": MagicMock(return_value=[]),
        "group": MagicMock(return_value=[]),
        "smart_group": MagicMock(return_value=[]),
    }
    monkeypatch.setattr(dt_mod, "_dt_selection", fakes["selection"])
    monkeypatch.setattr(dt_mod, "_dt_uuid_record", fakes["uuid"])
    monkeypatch.setattr(dt_mod, "_dt_tag_records", fakes["tag"])
    monkeypatch.setattr(dt_mod, "_dt_group_records", fakes["group"])
    monkeypatch.setattr(
        dt_mod, "_dt_smart_group_records", fakes["smart_group"],
    )
    return fakes


@pytest.fixture
def fake_dispatcher(monkeypatch) -> list[dict]:
    """Replace the per-record ``_index_record`` helper inside
    ``nexus.commands.dt`` with a stub that records calls. Returns the
    list of recorded calls so tests can assert what would have been
    indexed.
    """
    calls: list[dict] = []

    def record(
        uuid: str,
        path: str,
        *,
        collection: str | None,
        corpus: str,
        dry_run: bool,
    ) -> bool:
        calls.append({
            "uuid": uuid,
            "path": path,
            "collection": collection,
            "corpus": corpus,
            "dry_run": dry_run,
        })
        # Default success — tests that want to exercise the
        # stamp-failed summary path replace the dispatcher with
        # their own fake.
        return True

    monkeypatch.setattr("nexus.commands.dt._index_record", record)
    return calls


# ── Group + subcommand registration ──────────────────────────────────────────


class TestDtGroupRegistration:
    def test_dt_help_shows_group(self, runner):
        from nexus.cli import main

        result = runner.invoke(main, ["dt", "--help"])
        assert result.exit_code == 0
        assert "DEVONthink" in result.output

    def test_dt_index_help_shows_selector_flags(self, runner):
        from nexus.cli import main

        result = runner.invoke(main, ["dt", "index", "--help"])
        assert result.exit_code == 0
        assert "--selection" in result.output
        assert "--uuid" in result.output
        assert "--tag" in result.output
        assert "--group" in result.output
        assert "--smart-group" in result.output


# ── Selector routing ─────────────────────────────────────────────────────────


class TestSelectorRouting:
    def test_selection_invokes_dt_selection(
        self, runner, fake_selectors, fake_dispatcher,
    ):
        from nexus.cli import main

        fake_selectors["selection"].return_value = [("U1", "/a.pdf")]
        result = runner.invoke(main, ["dt", "index", "--selection"])
        assert result.exit_code == 0, result.output
        fake_selectors["selection"].assert_called_once_with()

    def test_tag_invokes_dt_tag_records(
        self, runner, fake_selectors, fake_dispatcher,
    ):
        from nexus.cli import main

        result = runner.invoke(main, ["dt", "index", "--tag", "research"])
        assert result.exit_code == 0, result.output
        fake_selectors["tag"].assert_called_once_with(
            "research", database=None,
        )

    def test_tag_with_database_passes_scope(
        self, runner, fake_selectors, fake_dispatcher,
    ):
        from nexus.cli import main

        result = runner.invoke(main, [
            "dt", "index", "--tag", "research", "--database", "MyLib",
        ])
        assert result.exit_code == 0, result.output
        fake_selectors["tag"].assert_called_once_with(
            "research", database="MyLib",
        )

    def test_group_invokes_dt_group_records(
        self, runner, fake_selectors, fake_dispatcher,
    ):
        from nexus.cli import main

        result = runner.invoke(main, [
            "dt", "index", "--group", "/Research/Papers",
        ])
        assert result.exit_code == 0, result.output
        fake_selectors["group"].assert_called_once_with(
            "/Research/Papers", database=None,
        )

    def test_group_with_database_passes_scope(
        self, runner, fake_selectors, fake_dispatcher,
    ):
        from nexus.cli import main

        result = runner.invoke(main, [
            "dt", "index", "--group", "/Research", "--database", "MyLib",
        ])
        assert result.exit_code == 0, result.output
        fake_selectors["group"].assert_called_once_with(
            "/Research", database="MyLib",
        )

    def test_smart_group_invokes_dt_smart_group_records(
        self, runner, fake_selectors, fake_dispatcher,
    ):
        from nexus.cli import main

        result = runner.invoke(main, [
            "dt", "index", "--smart-group", "Recent PDFs",
        ])
        assert result.exit_code == 0, result.output
        fake_selectors["smart_group"].assert_called_once_with(
            "Recent PDFs", database=None,
        )

    def test_smart_group_with_database_passes_scope(
        self, runner, fake_selectors, fake_dispatcher,
    ):
        from nexus.cli import main

        result = runner.invoke(main, [
            "dt", "index", "--smart-group", "Recent PDFs",
            "--database", "MyLib",
        ])
        assert result.exit_code == 0, result.output
        fake_selectors["smart_group"].assert_called_once_with(
            "Recent PDFs", database="MyLib",
        )

    def test_uuid_single_invokes_dt_uuid_record(
        self, runner, fake_selectors, fake_dispatcher,
    ):
        from nexus.cli import main

        fake_selectors["uuid"].return_value = [("U1", "/a.pdf")]
        result = runner.invoke(main, ["dt", "index", "--uuid", "U1"])
        assert result.exit_code == 0, result.output
        fake_selectors["uuid"].assert_called_once_with("U1")

    def test_uuid_multiple_invokes_per_uuid(
        self, runner, fake_selectors, fake_dispatcher,
    ):
        """Click's ``multiple=True`` packs repeated ``--uuid`` flags
        into a tuple. Each UUID becomes its own
        ``_dt_uuid_record`` call (the resolver is single-UUID by
        construction). Asserting the exact call args catches a
        regression that fans out incorrectly (e.g. passing all UUIDs
        as one argument)."""
        from nexus.cli import main

        fake_selectors["uuid"].return_value = [("X", "/x.pdf")]
        result = runner.invoke(main, [
            "dt", "index", "--uuid", "U1", "--uuid", "U2",
        ])
        assert result.exit_code == 0, result.output
        assert fake_selectors["uuid"].call_count == 2
        # Per-UUID args, in CLI order — locks the fan-out shape.
        assert fake_selectors["uuid"].call_args_list[0].args == ("U1",)
        assert fake_selectors["uuid"].call_args_list[1].args == ("U2",)


# ── Mutual exclusion ─────────────────────────────────────────────────────────


class TestMutualExclusion:
    def test_no_selector_errors(self, runner):
        from nexus.cli import main

        result = runner.invoke(main, ["dt", "index"])
        assert result.exit_code != 0
        # Hint at the missing selector so the operator knows what to add.
        assert "selector" in result.output.lower()

    def test_selection_plus_tag_errors(self, runner):
        from nexus.cli import main

        result = runner.invoke(main, [
            "dt", "index", "--selection", "--tag", "research",
        ])
        assert result.exit_code != 0
        assert "exclusive" in result.output.lower()

    def test_group_plus_smart_group_errors(self, runner):
        from nexus.cli import main

        result = runner.invoke(main, [
            "dt", "index", "--group", "/X", "--smart-group", "Y",
        ])
        assert result.exit_code != 0
        assert "exclusive" in result.output.lower()


# ── Per-record dispatch by extension ─────────────────────────────────────────


class TestPerRecordDispatch:
    def test_pdf_is_dispatched(
        self, runner, fake_selectors, fake_dispatcher,
    ):
        from nexus.cli import main

        fake_selectors["selection"].return_value = [("U1", "/foo/a.pdf")]
        result = runner.invoke(main, ["dt", "index", "--selection"])
        assert result.exit_code == 0, result.output
        assert len(fake_dispatcher) == 1
        assert fake_dispatcher[0]["uuid"] == "U1"
        assert fake_dispatcher[0]["path"] == "/foo/a.pdf"

    def test_md_is_dispatched(
        self, runner, fake_selectors, fake_dispatcher,
    ):
        from nexus.cli import main

        fake_selectors["selection"].return_value = [("U2", "/foo/note.md")]
        result = runner.invoke(main, ["dt", "index", "--selection"])
        assert result.exit_code == 0, result.output
        assert len(fake_dispatcher) == 1
        assert fake_dispatcher[0]["path"] == "/foo/note.md"

    def test_unknown_extension_is_skipped(
        self, runner, fake_selectors, fake_dispatcher,
    ):
        """Records whose path has no supported extension are skipped
        with a structured WARN, not a hard failure — the operator can
        still index the rest of the selection."""
        from nexus.cli import main

        fake_selectors["selection"].return_value = [
            ("U1", "/foo/a.pdf"),
            ("U2", "/bar/spec.docx"),
            ("U3", "/baz/note.md"),
        ]
        result = runner.invoke(main, ["dt", "index", "--selection"])
        assert result.exit_code == 0, result.output
        assert len(fake_dispatcher) == 2
        assert {c["uuid"] for c in fake_dispatcher} == {"U1", "U3"}


# ── Dry-run ──────────────────────────────────────────────────────────────────


class TestDryRun:
    def test_dry_run_lists_records_zero_indexer_calls(
        self, runner, fake_selectors, fake_dispatcher,
    ):
        from nexus.cli import main

        fake_selectors["selection"].return_value = [
            ("U1", "/a.pdf"),
            ("U2", "/b.md"),
        ]
        result = runner.invoke(main, [
            "dt", "index", "--selection", "--dry-run",
        ])
        assert result.exit_code == 0, result.output
        assert "U1" in result.output
        assert "/a.pdf" in result.output
        assert "U2" in result.output
        assert "/b.md" in result.output
        # The dispatcher must NOT be invoked at all under --dry-run.
        assert fake_dispatcher == []


# ── Passthrough flags ────────────────────────────────────────────────────────


class TestPassthroughFlags:
    def test_collection_passthrough(
        self, runner, fake_selectors, fake_dispatcher,
    ):
        from nexus.cli import main

        fake_selectors["selection"].return_value = [("U", "/a.pdf")]
        result = runner.invoke(main, [
            "dt", "index", "--selection",
            "--collection", "knowledge__test",
        ])
        assert result.exit_code == 0, result.output
        assert fake_dispatcher[0]["collection"] == "knowledge__test"

    def test_corpus_passthrough(
        self, runner, fake_selectors, fake_dispatcher,
    ):
        from nexus.cli import main

        fake_selectors["selection"].return_value = [("U", "/a.pdf")]
        result = runner.invoke(main, [
            "dt", "index", "--selection", "--corpus", "knowledge",
        ])
        assert result.exit_code == 0, result.output
        assert fake_dispatcher[0]["corpus"] == "knowledge"


# ── nexus-cvaw: paper PDFs route to knowledge__ by default ──────────────────


class TestDefaultCollectionByExtension:
    """nexus-cvaw: nx dt index without --collection should pick a
    paper-shaped home for PDFs (knowledge__<corpus>-papers, where
    aspect extraction routes to scholarly-paper-v1) and a doc-shaped
    home for markdown (docs__<corpus>). Pre-fix, both extensions
    landed in docs__<corpus>, which after nexus-z70w (PR #393)
    cannot route to any aspect extractor. PDFs ingested by the
    default were stranded.

    Tests assert the resolved collection_name passed to the
    fake dispatcher, since the per-record routing is what determines
    the paper's downstream eligibility for aspects + bib enrichment.
    """

    def test_pdf_default_routes_to_knowledge_papers(
        self, runner, fake_selectors, fake_dispatcher,
    ):
        from nexus.cli import main

        fake_selectors["selection"].return_value = [("U", "/foo/paper.pdf")]
        result = runner.invoke(main, ["dt", "index", "--selection"])
        assert result.exit_code == 0, result.output
        # No --collection: PDF default is knowledge__dt-papers.
        assert fake_dispatcher[0]["collection"] == "knowledge__dt-papers"

    def test_pdf_with_corpus_routes_to_knowledge_papers_corpus(
        self, runner, fake_selectors, fake_dispatcher,
    ):
        from nexus.cli import main

        fake_selectors["selection"].return_value = [("U", "/foo/paper.pdf")]
        result = runner.invoke(main, [
            "dt", "index", "--selection", "--corpus", "rag",
        ])
        assert result.exit_code == 0, result.output
        assert fake_dispatcher[0]["collection"] == "knowledge__rag-papers"

    def test_markdown_default_routes_to_docs_dt(
        self, runner, fake_selectors, fake_dispatcher,
    ):
        from nexus.cli import main

        fake_selectors["selection"].return_value = [("U", "/foo/note.md")]
        result = runner.invoke(main, ["dt", "index", "--selection"])
        assert result.exit_code == 0, result.output
        # Markdown notes go to docs__<corpus> (current behavior, but
        # corpus default flipped from "default" to "dt" so the note
        # corpus matches the paper corpus by convention).
        assert fake_dispatcher[0]["collection"] == "docs__dt"

    def test_markdown_with_corpus_routes_to_docs_corpus(
        self, runner, fake_selectors, fake_dispatcher,
    ):
        from nexus.cli import main

        fake_selectors["selection"].return_value = [("U", "/foo/note.md")]
        result = runner.invoke(main, [
            "dt", "index", "--selection", "--corpus", "rag",
        ])
        assert result.exit_code == 0, result.output
        assert fake_dispatcher[0]["collection"] == "docs__rag"

    def test_explicit_collection_overrides_default(
        self, runner, fake_selectors, fake_dispatcher,
    ):
        """``--collection X`` always wins over the extension-based
        default — the operator has explicitly requested X."""
        from nexus.cli import main

        fake_selectors["selection"].return_value = [("U", "/foo/paper.pdf")]
        result = runner.invoke(main, [
            "dt", "index", "--selection",
            "--collection", "knowledge__custom-thing",
        ])
        assert result.exit_code == 0, result.output
        assert fake_dispatcher[0]["collection"] == "knowledge__custom-thing"


# ── Error handling ───────────────────────────────────────────────────────────


class TestErrorHandling:
    def test_dt_not_available_exits_with_friendly_message(
        self, runner, fake_selectors,
    ):
        from nexus.cli import main
        from nexus.devonthink import DTNotAvailableError

        fake_selectors["selection"].side_effect = DTNotAvailableError(
            "DEVONthink is not running. Open it and retry, or pass "
            "--uuid for a UUID you already have.",
        )
        result = runner.invoke(main, ["dt", "index", "--selection"])
        assert result.exit_code != 0
        assert "DEVONthink is not running" in result.output

    def test_non_darwin_exits_with_macos_only_message(
        self, runner, monkeypatch,
    ):
        """Without faked selectors, the platform gate inside
        ``_dt_selection`` fires and the CLI surfaces the
        ``macOS-only`` message."""
        from nexus.cli import main

        monkeypatch.setattr("sys.platform", "linux")
        result = runner.invoke(main, ["dt", "index", "--selection"])
        assert result.exit_code != 0
        assert "macOS-only" in result.output


# ── stamp-failure summary surfacing ──────────────────────────────────────────


class TestStampFailedSummary:
    """When ``_index_record`` returns ``False`` (the stamp helper
    couldn't apply the DT identity), ``index_cmd`` must surface the
    miss in its summary line so the operator knows the round-trip
    is broken for some records. Silent stamp failures were a
    significant audit finding from v4.19.1 post-release scrub.
    """

    def test_summary_includes_stamp_failed_count(
        self, runner, fake_selectors, monkeypatch,
    ):
        from nexus.cli import main

        fake_selectors["selection"].return_value = [
            ("U-OK", "/a.pdf"),
            ("U-FAIL-1", "/b.pdf"),
            ("U-FAIL-2", "/c.md"),
        ]

        # Dispatcher returns False for the two that should fail to stamp.
        def maybe_fail(uuid, path, *, collection, corpus, dry_run):
            return uuid == "U-OK"

        monkeypatch.setattr("nexus.commands.dt._index_record", maybe_fail)

        result = runner.invoke(main, ["dt", "index", "--selection"])
        assert result.exit_code == 0, result.output
        assert "Indexed 3 record(s)" in result.output
        assert "2 DT-URI stamp-failed" in result.output
        # Recovery hint should appear so the operator knows what to do.
        assert "nx catalog update" in result.output

    def test_summary_omits_stamp_failed_when_zero(
        self, runner, fake_selectors, fake_dispatcher,
    ):
        """No stamp failures → no mention of stamp-failed in the
        summary line. Keeps the happy path uncluttered."""
        from nexus.cli import main

        fake_selectors["selection"].return_value = [("U", "/a.pdf")]
        result = runner.invoke(main, ["dt", "index", "--selection"])
        assert result.exit_code == 0, result.output
        assert "Indexed 1 record(s)" in result.output
        assert "stamp-failed" not in result.output


# ── nx dt open ───────────────────────────────────────────────────────────────


@pytest.fixture
def fake_open(monkeypatch) -> list[list[str]]:
    """Replace ``subprocess.run`` inside ``nexus.commands.dt`` with a
    stub that records the argv it was asked to launch and returns a
    success ``CompletedProcess``. Lets tests assert ``open <uri>`` is
    invoked without spawning a real ``open(1)`` process.
    """
    import subprocess as _subprocess  # noqa: PLC0415

    calls: list[list[str]] = []

    def fake_run(argv, *args, **kwargs):
        calls.append(list(argv))
        return _subprocess.CompletedProcess(args=argv, returncode=0)

    monkeypatch.setattr("nexus.commands.dt.subprocess.run", fake_run)
    return calls


@pytest.fixture
def fake_resolve_tumbler(monkeypatch) -> dict:
    """Replace the catalog tumbler resolver with a stub. Default
    behaviour raises ``click.ClickException("tumbler not found ...")``;
    tests override ``store["uri"]`` or ``store["error"]`` to drive
    specific resolution outcomes.
    """
    import click as _click  # noqa: PLC0415

    store: dict = {"uri": None, "error": None}

    def fake_resolve(tumbler: str) -> str | None:
        if store["error"] is not None:
            raise _click.ClickException(store["error"])
        return store["uri"]

    monkeypatch.setattr(
        "nexus.commands.dt._resolve_dt_uri_from_tumbler", fake_resolve,
    )
    return store


class TestDtOpenUuidForm:
    def test_uuid_builds_uri_directly(
        self, runner, fake_open, monkeypatch,
    ):
        """A UUID-shaped argument bypasses the catalog entirely — the
        URI is just ``x-devonthink-item://<UUID>``. Saves a DB hit
        when the operator already has the UUID in hand."""
        from nexus.cli import main

        monkeypatch.setattr("sys.platform", "darwin")
        uuid = "8EDC855D-213F-40AD-A9CF-9543CC76476B"
        result = runner.invoke(main, ["dt", "open", uuid])
        assert result.exit_code == 0, result.output
        assert fake_open == [["open", f"x-devonthink-item://{uuid}"]]

    def test_uuid_on_non_darwin_exits_with_macos_only(
        self, runner, fake_open, monkeypatch,
    ):
        """``open(1)`` is darwin-only and the URL scheme requires DT
        to handle it — refuse on Linux/Windows with the same
        operator-friendly message the index command uses."""
        from nexus.cli import main

        monkeypatch.setattr("sys.platform", "linux")
        uuid = "8EDC855D-213F-40AD-A9CF-9543CC76476B"
        result = runner.invoke(main, ["dt", "open", uuid])
        assert result.exit_code != 0
        assert "macOS-only" in result.output
        assert fake_open == []  # no spawn attempt

    def test_tumbler_form_on_non_darwin_does_not_touch_catalog(
        self, runner, fake_open, monkeypatch,
    ):
        """The platform gate fires BEFORE tumbler resolution. A
        non-darwin user passing a tumbler argument should see
        ``macOS-only``, not a catalog-not-initialized error or a
        tumbler-not-found error. Asserts the resolver helper isn't
        called at all on non-darwin."""
        from nexus.cli import main

        resolver_calls: list[str] = []

        def must_not_resolve(tumbler):
            resolver_calls.append(tumbler)
            raise AssertionError("resolver must not run on non-darwin")

        monkeypatch.setattr(
            "nexus.commands.dt._resolve_dt_uri_from_tumbler",
            must_not_resolve,
        )
        monkeypatch.setattr("sys.platform", "linux")
        result = runner.invoke(main, ["dt", "open", "1.2.3"])
        assert result.exit_code != 0
        assert "macOS-only" in result.output
        assert resolver_calls == []
        assert fake_open == []


class TestDtOpenTumblerForm:
    def test_tumbler_uses_devonthink_uri_from_meta(
        self, runner, fake_open, fake_resolve_tumbler, monkeypatch,
    ):
        """``meta.devonthink_uri`` is the canonical reverse-lookup
        per RDR-099 — when the catalog entry carries it, that's the
        URI we open."""
        from nexus.cli import main

        monkeypatch.setattr("sys.platform", "darwin")
        fake_resolve_tumbler["uri"] = "x-devonthink-item://META-UUID"
        result = runner.invoke(main, ["dt", "open", "1.2.3"])
        assert result.exit_code == 0, result.output
        assert fake_open == [["open", "x-devonthink-item://META-UUID"]]

    def test_tumbler_falls_back_to_source_uri(
        self, runner, fake_open, fake_resolve_tumbler, monkeypatch,
    ):
        """When ``meta.devonthink_uri`` is absent but ``source_uri``
        is itself a DT URI (the entry was registered with a DT
        identity from the start), fall through to it. The fake
        resolver mimics the production helper that checks meta first
        and source_uri second."""
        from nexus.cli import main

        monkeypatch.setattr("sys.platform", "darwin")
        fake_resolve_tumbler["uri"] = "x-devonthink-item://SOURCE-UUID"
        result = runner.invoke(main, ["dt", "open", "1.2.3"])
        assert result.exit_code == 0, result.output
        assert fake_open == [["open", "x-devonthink-item://SOURCE-UUID"]]

    def test_tumbler_with_no_dt_uri_exits_non_zero(
        self, runner, fake_open, fake_resolve_tumbler, monkeypatch,
    ):
        from nexus.cli import main

        monkeypatch.setattr("sys.platform", "darwin")
        fake_resolve_tumbler["uri"] = None  # no DT URI on the entry
        result = runner.invoke(main, ["dt", "open", "1.2.3"])
        assert result.exit_code != 0
        assert "DEVONthink URI" in result.output or "not found" in result.output
        assert fake_open == []

    def test_tumbler_not_found_exits_non_zero(
        self, runner, fake_open, fake_resolve_tumbler, monkeypatch,
    ):
        from nexus.cli import main

        monkeypatch.setattr("sys.platform", "darwin")
        fake_resolve_tumbler["error"] = "tumbler not found: 9.9.9"
        result = runner.invoke(main, ["dt", "open", "9.9.9"])
        assert result.exit_code != 0
        assert "not found" in result.output.lower()
        assert fake_open == []


class TestDtOpenMalformedArg:
    def test_malformed_argument_exits_with_usage_error(
        self, runner, fake_open, monkeypatch,
    ):
        from nexus.cli import main

        monkeypatch.setattr("sys.platform", "darwin")
        result = runner.invoke(main, ["dt", "open", "not-a-tumbler-or-uuid"])
        assert result.exit_code != 0
        # Hint mentions both accepted shapes so the operator can correct it.
        assert "tumbler" in result.output.lower()
        assert "uuid" in result.output.lower()
        assert fake_open == []


class TestDtOpenHelp:
    def test_open_help_renders(self, runner):
        from nexus.cli import main

        result = runner.invoke(main, ["dt", "open", "--help"])
        assert result.exit_code == 0
        assert "tumbler" in result.output.lower() or "uuid" in result.output.lower()


# ── _select_dt_uri_from_entry (pure unit) ────────────────────────────────────


class _FakeEntry:
    """Minimal duck-typed shape that ``_select_dt_uri_from_entry``
    inspects: ``meta`` dict + ``source_uri`` string."""

    def __init__(self, meta=None, source_uri=""):
        self.meta = meta if meta is not None else {}
        self.source_uri = source_uri


class TestSelectDtUriFromEntry:
    """Locks the meta-first / source-second / None fall-through rule
    independently of catalog plumbing. The CLI tumbler tests in
    TestDtOpenTumblerForm stub the whole resolver, so without these
    tests a regression that reorders the branches inside
    _select_dt_uri_from_entry would slip through.
    """

    def test_meta_devonthink_uri_wins_over_source_uri(self):
        from nexus.commands.dt import _select_dt_uri_from_entry

        entry = _FakeEntry(
            meta={"devonthink_uri": "x-devonthink-item://META-UUID"},
            source_uri="x-devonthink-item://SOURCE-UUID",
        )
        assert (
            _select_dt_uri_from_entry(entry)
            == "x-devonthink-item://META-UUID"
        )

    def test_source_uri_used_when_meta_absent(self):
        from nexus.commands.dt import _select_dt_uri_from_entry

        entry = _FakeEntry(
            meta={},
            source_uri="x-devonthink-item://SOURCE-UUID",
        )
        assert (
            _select_dt_uri_from_entry(entry)
            == "x-devonthink-item://SOURCE-UUID"
        )

    def test_source_uri_used_when_meta_devonthink_uri_empty(self):
        """Empty string in meta is not a match — fall through."""
        from nexus.commands.dt import _select_dt_uri_from_entry

        entry = _FakeEntry(
            meta={"devonthink_uri": ""},
            source_uri="x-devonthink-item://SOURCE-UUID",
        )
        assert (
            _select_dt_uri_from_entry(entry)
            == "x-devonthink-item://SOURCE-UUID"
        )

    def test_returns_none_when_neither_present(self):
        from nexus.commands.dt import _select_dt_uri_from_entry

        entry = _FakeEntry(meta={}, source_uri="")
        assert _select_dt_uri_from_entry(entry) is None

    def test_returns_none_when_uris_not_devonthink_scheme(self):
        """``file://`` and ``https://`` source URIs are common; the
        helper must not treat them as DT URIs even though they share
        the ``://`` shape."""
        from nexus.commands.dt import _select_dt_uri_from_entry

        entry = _FakeEntry(
            meta={"devonthink_uri": "file:///Users/x/doc.pdf"},
            source_uri="https://example.com/paper.pdf",
        )
        assert _select_dt_uri_from_entry(entry) is None

    def test_meta_none_is_tolerated(self):
        """Some catalog rows may surface ``meta=None`` rather than
        ``{}``; the helper coerces via ``or {}`` so callers don't need
        to special-case the shape."""
        from nexus.commands.dt import _select_dt_uri_from_entry

        entry = _FakeEntry(
            meta=None,
            source_uri="x-devonthink-item://FALLBACK",
        )
        assert (
            _select_dt_uri_from_entry(entry)
            == "x-devonthink-item://FALLBACK"
        )


# ── _stamp_dt_uri_on_entry (post-index identity stamp) ───────────────────────


class TestStampDtUriOnEntry:
    """RDR-099 AC-1 requires every catalog entry produced by
    ``nx dt index`` to have ``source_uri == x-devonthink-item://<UUID>``
    AND ``meta.devonthink_uri`` matching. The indexer registers the
    entry with the resolved local ``file://`` path; ``_stamp_dt_uri_on_entry``
    runs afterwards to overwrite both fields with the DT identity.
    """

    def _setup_catalog_with_entry(self, tmp_path, file_path):
        """Stand up a catalog with a single registered entry pointing
        at ``file_path`` (mimics the post-index state before the
        stamp helper runs). Returns the catalog instance."""
        from nexus.catalog.catalog import Catalog  # noqa: PLC0415

        cat = Catalog.init(tmp_path / "catalog")
        owner = cat.register_owner(
            "test-repo", "repo", repo_hash="cafebabe",
        )
        cat.register(
            owner=owner,
            title="A Test PDF",
            file_path=str(file_path),
            content_type="paper",
        )
        return cat

    def test_stamps_source_uri_and_meta_devonthink_uri(
        self, tmp_path, monkeypatch,
    ):
        """Happy path: indexer-registered entry with file:// source_uri
        becomes a DT-keyed entry after the stamp runs."""
        from nexus.commands.dt import _stamp_dt_uri_on_entry

        file_path = tmp_path / "a.pdf"
        file_path.write_bytes(b"%PDF-1.4 dt-stamp")
        cat = self._setup_catalog_with_entry(tmp_path, file_path)
        cat_dir = tmp_path / "catalog"
        monkeypatch.setattr(
            "nexus.config.catalog_path", lambda: cat_dir,
        )

        uuid = "8EDC855D-213F-40AD-A9CF-9543CC76476B"
        _stamp_dt_uri_on_entry(file_path, uuid)

        # Reopen so we read post-write state.
        from nexus.catalog.catalog import Catalog  # noqa: PLC0415

        cat2 = Catalog(cat_dir, cat_dir / ".catalog.db")
        try:
            entries = cat2.all_documents()
            target = next(
                e for e in entries if e.file_path == str(file_path)
            )
            assert target.source_uri == f"x-devonthink-item://{uuid}"
            assert target.meta.get("devonthink_uri") == (
                f"x-devonthink-item://{uuid}"
            )
        finally:
            cat2._db.close()

    def test_no_entry_match_logs_and_returns(
        self, tmp_path, monkeypatch,
    ):
        """When no catalog entry matches the file_path (rare —
        post-index, but possible if a concurrent purge runs), the
        stamp helper logs a warning and returns cleanly. It must not
        raise."""
        from nexus.commands.dt import _stamp_dt_uri_on_entry

        # Initialise a catalog but DON'T register any entry.
        from nexus.catalog.catalog import Catalog  # noqa: PLC0415

        cat_dir = tmp_path / "catalog"
        Catalog.init(cat_dir)
        monkeypatch.setattr(
            "nexus.config.catalog_path", lambda: cat_dir,
        )

        file_path = tmp_path / "ghost.pdf"
        # Should not raise.
        _stamp_dt_uri_on_entry(file_path, "GHOST-UUID")

    def test_uninitialized_catalog_logs_and_returns(
        self, tmp_path, monkeypatch,
    ):
        """When the catalog is not initialised at all, the stamp
        helper logs and returns instead of raising. Production callers
        always initialise the catalog before indexing, but the
        defence keeps the dt index summary intact rather than
        bubbling a startup error."""
        from nexus.commands.dt import _stamp_dt_uri_on_entry

        # No Catalog.init call — catalog dir doesn't exist as a catalog.
        bogus = tmp_path / "no-catalog-here"
        bogus.mkdir()
        monkeypatch.setattr(
            "nexus.config.catalog_path", lambda: bogus,
        )

        # Should not raise.
        _stamp_dt_uri_on_entry(tmp_path / "x.pdf", "ANY-UUID")

    def test_index_record_invokes_stamp_helper(
        self, monkeypatch, tmp_path,
    ):
        """``_index_record`` MUST call ``_stamp_dt_uri_on_entry`` after
        the indexer runs — that's the contract that turns the
        ``file://`` source_uri the indexer registers into the DT
        identity AC-1 requires. Mocks both the indexer and the stamp
        helper so we just verify the wiring."""
        from nexus.commands import dt as dt_module

        called: list[tuple[Path, str]] = []
        pdf_kwargs: list[dict] = []

        def fake_stamp(file_path, uuid):
            called.append((file_path, uuid))

        def fake_index_pdf(*args, **kwargs):
            pdf_kwargs.append(kwargs)
            return 0

        monkeypatch.setattr(
            dt_module, "_stamp_dt_uri_on_entry", fake_stamp,
        )
        monkeypatch.setattr(
            "nexus.doc_indexer.index_pdf", fake_index_pdf,
        )

        dt_module._index_record(
            uuid="UUID-WIRING",
            path=str(tmp_path / "a.pdf"),
            collection="knowledge__test",
            corpus="default",
            dry_run=False,
        )
        assert len(called) == 1
        assert called[0][0] == tmp_path / "a.pdf"
        assert called[0][1] == "UUID-WIRING"
        # PDF path must forward --collection.
        assert pdf_kwargs[0].get("collection_name") == "knowledge__test"
        assert pdf_kwargs[0].get("corpus") == "default"

    def test_index_record_md_forwards_collection(
        self, monkeypatch, tmp_path,
    ):
        """The .md branch must forward ``--collection`` the same as
        the .pdf branch. This test catches a regression where
        index_markdown was invoked without ``collection_name``,
        silently dropping the operator's flag and routing every .md
        file into ``docs__default`` regardless of intent.
        """
        from nexus.commands import dt as dt_module

        md_kwargs: list[dict] = []

        def fake_index_markdown(*args, **kwargs):
            md_kwargs.append(kwargs)
            return 0

        # Stamp + indexer fakes — we only care about the collection
        # forwarding, not the catalog stamp here.
        monkeypatch.setattr(
            dt_module, "_stamp_dt_uri_on_entry", lambda *a, **kw: None,
        )
        monkeypatch.setattr(
            "nexus.doc_indexer.index_markdown", fake_index_markdown,
        )

        dt_module._index_record(
            uuid="UUID-MD",
            path=str(tmp_path / "note.md"),
            collection="knowledge__notes",
            corpus="default",
            dry_run=False,
        )
        assert len(md_kwargs) == 1
        assert md_kwargs[0].get("collection_name") == "knowledge__notes"
        assert md_kwargs[0].get("corpus") == "default"

    def test_index_record_dry_run_skips_stamp(
        self, monkeypatch, tmp_path,
    ):
        """``--dry-run`` short-circuits — no indexer call, no stamp."""
        from nexus.commands import dt as dt_module

        stamps: list = []

        def fake_stamp(file_path, uuid):
            stamps.append((file_path, uuid))

        monkeypatch.setattr(
            dt_module, "_stamp_dt_uri_on_entry", fake_stamp,
        )

        dt_module._index_record(
            uuid="UUID-DRY",
            path=str(tmp_path / "a.pdf"),
            collection=None,
            corpus="default",
            dry_run=True,
        )
        assert stamps == []
