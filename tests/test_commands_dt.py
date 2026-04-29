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
    ) -> None:
        calls.append({
            "uuid": uuid,
            "path": path,
            "collection": collection,
            "corpus": corpus,
            "dry_run": dry_run,
        })

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

    def test_selection_plus_tag_errors(self, runner):
        from nexus.cli import main

        result = runner.invoke(main, [
            "dt", "index", "--selection", "--tag", "research",
        ])
        assert result.exit_code != 0

    def test_group_plus_smart_group_errors(self, runner):
        from nexus.cli import main

        result = runner.invoke(main, [
            "dt", "index", "--group", "/X", "--smart-group", "Y",
        ])
        assert result.exit_code != 0


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
        assert fake_open == []


class TestDtOpenHelp:
    def test_open_help_renders(self, runner):
        from nexus.cli import main

        result = runner.invoke(main, ["dt", "open", "--help"])
        assert result.exit_code == 0
        assert "tumbler" in result.output.lower() or "uuid" in result.output.lower()
