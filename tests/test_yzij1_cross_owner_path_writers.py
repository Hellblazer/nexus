# SPDX-License-Identifier: AGPL-3.0-or-later
"""nexus-yzij1: path-keyed WRITERS must refuse, report, or announce — never guess.

Companion to ``tests/catalog/test_cross_owner_file_path_resolution.py``, which
pins the same contract at the real catalog boundary. This file pins what the
CALLERS do with the answer, which is where the damage was:

* ``dt._stamp_dt_uri_on_entry`` WRITES identity onto the row it picks. Taking
  the first of several is how one ``nx dt index`` run stamped the wrong
  document (nexus-z0lu4). It must refuse when the weak key is ambiguous.
* ``indexer._delete_docs_for_paths`` decides a tombstone. Its owner-scoped miss
  is correct — a file leaving this repo is no reason to delete another owner's
  document — but it was indistinguishable from "no such document anywhere".
* ``catalog_cmds.report.session_summary_cmd`` reports to a HUMAN, and reported
  one row's RDR links as though they were the path's.
* ``catalog.path_ambiguity.announce_cross_owner_mint`` is the shared
  say-it-out-loud for writers that legitimately mint a second document.

Each test states which way it would fail against the pre-fix code, because the
failure mode here is silence, and a test asserting only that the happy path
still works would pass against every version of this code ever written.
"""
from __future__ import annotations

import subprocess
from pathlib import Path
from types import SimpleNamespace

import pytest
from click.testing import CliRunner
from structlog.testing import capture_logs

from nexus import indexer as indexer_mod
from nexus.catalog.path_ambiguity import announce_cross_owner_mint
from nexus.commands import catalog as _cat_cmd
from nexus.commands.catalog_cmds import report as report_mod
from nexus.commands.dt import _stamp_dt_uri_on_entry


def _entry(tumbler: str, **kw):
    return SimpleNamespace(
        tumbler=tumbler,
        title=kw.get("title", f"doc-{tumbler}"),
        year=kw.get("year", 0),
        content_type=kw.get("content_type", "paper"),
        file_path=kw.get("file_path", ""),
    )


# ── dt stamp: refuse rather than guess ──────────────────────────────────────


class _StampReader:
    def __init__(self, by_uri=None, by_path=()):
        self._by_uri = by_uri
        self._by_path = list(by_path)
        self.find_all_calls: list[str] = []

    def by_source_uri(self, uri):
        return self._by_uri

    def find_all_by_file_path(self, fp):
        self.find_all_calls.append(fp)
        return list(self._by_path)

    def find_by_file_path(self, fp):  # must NOT be what the writer reaches for
        return self._by_path[0] if self._by_path else None

    def close(self):
        pass


class _RecordingWriter:
    def __init__(self):
        self.updates: list[tuple] = []

    def update(self, tumbler, **kw):
        self.updates.append((str(tumbler), kw))

    def close(self):  # the real writer is closed in a finally
        pass


@pytest.fixture
def stamp_env(monkeypatch):
    writer = _RecordingWriter()
    holder: dict = {"reader": None}

    def install(reader):
        holder["reader"] = reader
        monkeypatch.setattr(
            "nexus.catalog.factory.make_catalog_reader", lambda *a, **k: reader,
        )
        monkeypatch.setattr(
            "nexus.catalog.factory.make_catalog_writer", lambda *a, **k: writer,
        )
        return writer

    return install


class TestDtStampRefusesAnAmbiguousPath:
    def test_two_documents_for_the_path_means_no_stamp(self, stamp_env) -> None:
        """PRE-FIX: ``find_by_file_path`` returned the first and it got stamped.

        The damage is not visible in the return value — stamping the wrong row
        "succeeds" — so the assertion is that NOTHING was written.
        """
        reader = _StampReader(by_uri=None, by_path=[_entry("9.1"), _entry("9.2")])
        writer = stamp_env(reader)

        with capture_logs() as logs:
            ok = _stamp_dt_uri_on_entry(Path("/tmp/shared.pdf"), "UUID-1")

        assert ok is False
        assert writer.updates == [], (
            "an ambiguous path must not be stamped at all — writing to either "
            "candidate is the nexus-z0lu4 defect, whichever one is chosen"
        )
        warned = [e for e in logs if e.get("event") == "dt_stamp_ambiguous_file_path"]
        assert len(warned) == 1
        assert warned[0]["matches"] == 2
        assert set(warned[0]["candidates"]) == {"9.1", "9.2"}

    def test_a_single_document_for_the_path_still_stamps(self, stamp_env) -> None:
        """The other arm: refusing everything would also pass the test above."""
        reader = _StampReader(by_uri=None, by_path=[_entry("9.1")])
        writer = stamp_env(reader)

        ok = _stamp_dt_uri_on_entry(Path("/tmp/solo.pdf"), "UUID-2")

        assert ok is True
        assert [t for t, _ in writer.updates] == ["9.1"]
        assert writer.updates[0][1]["source_uri"] == "x-devonthink-item://UUID-2"

    def test_the_uri_wins_and_the_path_is_never_consulted(self, stamp_env) -> None:
        """A URI hit must short-circuit: the strong key is not a tiebreaker
        applied after the weak one, it replaces it."""
        reader = _StampReader(
            by_uri=_entry("9.7"), by_path=[_entry("9.1"), _entry("9.2")],
        )
        writer = stamp_env(reader)

        ok = _stamp_dt_uri_on_entry(Path("/tmp/shared.pdf"), "UUID-3")

        assert ok is True
        assert [t for t, _ in writer.updates] == ["9.7"]
        assert reader.find_all_calls == [], (
            "the ambiguous-path branch must not even run when the URI resolved"
        )


# ── announce-on-mint ────────────────────────────────────────────────────────


class _AnnounceReader:
    def __init__(self, matches=(), raises: bool = False):
        self._matches = list(matches)
        self._raises = raises

    def find_all_by_file_path(self, fp):
        if self._raises:
            raise RuntimeError("catalog unreachable")
        return list(self._matches)


class TestAnnounceCrossOwnerMint:
    def test_it_names_every_existing_document(self) -> None:
        reader = _AnnounceReader(matches=[_entry("3.1"), _entry("3.2")])

        with capture_logs() as logs:
            announce_cross_owner_mint(
                reader, "a/b.md", owner="4.0", context="unit",
            )

        events = [e for e in logs
                  if e.get("event") == "catalog_mint_over_existing_file_path"]
        assert len(events) == 1
        assert events[0]["existing"] == 2
        assert events[0]["existing_tumblers"] == ["3.1", "3.2"]
        assert events[0]["owner"] == "4.0"
        assert events[0]["context"] == "unit"

    def test_a_genuinely_new_path_is_silent(self) -> None:
        """Without this, the warning could fire on every mint and the test
        above would still pass — making the signal worthless."""
        with capture_logs() as logs:
            announce_cross_owner_mint(
                _AnnounceReader(matches=[]), "a/new.md", owner="4.0", context="unit",
            )

        assert not [e for e in logs
                    if e.get("event") == "catalog_mint_over_existing_file_path"]

    def test_a_failing_catalog_never_propagates(self) -> None:
        """Reporting must not convert a successful index into a failed one."""
        announce_cross_owner_mint(
            _AnnounceReader(raises=True), "a/b.md", owner="4.0", context="unit",
        )

    def test_a_reader_without_the_method_is_tolerated(self) -> None:
        """Several catalog doubles predate ``find_all_by_file_path``."""
        announce_cross_owner_mint(
            SimpleNamespace(), "a/b.md", owner="4.0", context="unit",
        )


# ── delete: owner-scoped by design, but say so ──────────────────────────────


class _LogRecorder:
    """Record structlog calls at the module attribute.

    ``structlog.testing.capture_logs`` does not see ``indexer._log``: that
    proxy is already bound by the time this module's tests run, so it keeps
    the processor chain it bound with and never reaches the capture buffer.
    Patching the module attribute asserts the emission at its source, which is
    what this test is actually about.
    """

    def __init__(self):
        self.events: list[tuple[str, dict]] = []

    def _record(self, event, **kw):
        self.events.append((event, kw))

    info = warning = debug = error = _record

    def of(self, event: str) -> list[dict]:
        return [kw for name, kw in self.events if name == event]


class TestDeleteAnnouncesTheForeignOwnerSkip:
    def test_a_row_under_another_owner_is_left_live_and_logged(
        self, monkeypatch,
    ) -> None:
        """PRE-FIX: a bare ``continue``. The row stayed live — correctly — and
        nothing said so, which is why the 19-document population went unnoticed.
        """
        deleted: list[str] = []

        reader = SimpleNamespace(
            owner_for_repo=lambda h: "5.0",
            by_file_path=lambda owner, rel: None,       # owner-scoped blindness
            find_all_by_file_path=lambda rel: [_entry("6.1")],
            close=lambda: None,
        )
        writer = SimpleNamespace(
            delete_document=lambda t: deleted.append(str(t)), close=lambda: None,
        )

        monkeypatch.setattr(
            "nexus.catalog.factory.make_catalog_reader", lambda *a, **k: reader,
        )
        monkeypatch.setattr(
            "nexus.catalog.factory.make_catalog_writer", lambda *a, **k: writer,
        )
        monkeypatch.setattr(
            "nexus.repo_identity._repo_identity", lambda repo: ("name", "hash"),
        )

        rec = _LogRecorder()
        monkeypatch.setattr(indexer_mod, "_log", rec)
        indexer_mod._delete_docs_for_paths(Path("/repo"), ["a/gone.md"])

        assert deleted == [], (
            "widening the DELETE would be worse than the silence: this run owns "
            "neither the other owner's row nor the decision to remove it"
        )
        skipped = rec.of("since_head_delete_skipped_foreign_owner")
        assert len(skipped) == 1, f"recorded={rec.events}"
        assert skipped[0]["candidates"] == ["6.1"]
        assert skipped[0]["file_path"] == "a/gone.md"

    def test_no_row_anywhere_stays_quiet(self, monkeypatch) -> None:
        reader = SimpleNamespace(
            owner_for_repo=lambda h: "5.0",
            by_file_path=lambda owner, rel: None,
            find_all_by_file_path=lambda rel: [],
            close=lambda: None,
        )
        monkeypatch.setattr(
            "nexus.catalog.factory.make_catalog_reader", lambda *a, **k: reader,
        )
        monkeypatch.setattr(
            "nexus.catalog.factory.make_catalog_writer",
            lambda *a, **k: SimpleNamespace(
                delete_document=lambda t: None, close=lambda: None,
            ),
        )
        monkeypatch.setattr(
            "nexus.repo_identity._repo_identity", lambda repo: ("name", "hash"),
        )

        rec = _LogRecorder()
        monkeypatch.setattr(indexer_mod, "_log", rec)
        indexer_mod._delete_docs_for_paths(Path("/repo"), ["a/gone.md"])

        assert rec.of("since_head_delete_skipped_foreign_owner") == []


# ── session-summary: a human report shows every document for the path ───────


class _ReportCatalog:
    """Two documents for one path; only the SECOND carries the RDR link.

    Ordered deliberately: with ``find_by_file_path``'s first-match the report
    printed "No linked RDRs found" while the link sat one row away.
    """

    def __init__(self):
        self.rdr = SimpleNamespace(
            tumbler="2.9", title="RDR-217 something", content_type="rdr",
        )

    def find_all_by_file_path(self, fp):
        return [_entry("1.1", content_type="paper"), _entry("1.2", content_type="paper")]

    def find_by_file_path(self, fp):
        return self.find_all_by_file_path(fp)[0]

    def links_from(self, tumbler):
        if str(tumbler) == "1.2":
            return [SimpleNamespace(to_tumbler="2.9")]
        return []

    def links_to(self, tumbler):
        return []

    def resolve(self, t):
        return self.rdr if str(t) == "2.9" else None

    def stats(self):
        return {"link_count": 1}


class TestSessionSummaryShowsEveryDocumentForAPath:
    def test_a_link_on_the_second_row_is_reported(self, monkeypatch) -> None:
        """PRE-FIX: only row 1.1 was consulted, so this printed the
        'No linked RDRs found for recently modified files.' line instead."""
        monkeypatch.setattr(_cat_cmd, "_get_catalog", lambda: _ReportCatalog())
        monkeypatch.setattr(
            subprocess, "run",
            lambda *a, **k: SimpleNamespace(stdout="src/a.py\n", returncode=0),
        )

        runner = CliRunner()
        result = runner.invoke(report_mod.session_summary_cmd, [])

        assert result.exit_code == 0, result.output
        assert "RDR-217 something" in result.output, result.output
        assert "No linked RDRs found" not in result.output
        # With a choice to explain, the line says WHICH document it came from.
        assert "1.2" in result.output, result.output

    def test_a_failing_probe_is_not_reported_as_a_failed_delete(
        self, monkeypatch,
    ) -> None:
        """The widening lookup attempts no delete, so its failure must not be
        filed under ``since_head_delete_doc_failed`` — that event means a
        tombstone was tried and did not land."""
        def boom(rel):
            raise RuntimeError("catalog unreachable")

        reader = SimpleNamespace(
            owner_for_repo=lambda h: "5.0",
            by_file_path=lambda owner, rel: None,
            find_all_by_file_path=boom,
            close=lambda: None,
        )
        monkeypatch.setattr(
            "nexus.catalog.factory.make_catalog_reader", lambda *a, **k: reader,
        )
        monkeypatch.setattr(
            "nexus.catalog.factory.make_catalog_writer",
            lambda *a, **k: SimpleNamespace(
                delete_document=lambda t: None, close=lambda: None,
            ),
        )
        monkeypatch.setattr(
            "nexus.repo_identity._repo_identity", lambda repo: ("name", "hash"),
        )
        rec = _LogRecorder()
        monkeypatch.setattr(indexer_mod, "_log", rec)

        indexer_mod._delete_docs_for_paths(Path("/repo"), ["a/gone.md"])

        assert rec.of("since_head_delete_doc_failed") == [], (
            f"no delete was attempted; recorded={rec.events}"
        )
        assert len(rec.of("since_head_delete_foreign_owner_probe_failed")) == 1


# ── the announce CALL SITES, not just the helper ────────────────────────────


class TestTheMintSitesActuallyCallIt:
    """substantive-critic finding, 2026-09-21: every test above exercised
    ``announce_cross_owner_mint`` directly, so deleting all four CALL
    statements would have left the whole suite green — the helper would have
    been dead code with a tidy unit test.

    The behavioural arm below drives one real registrar end to end. The
    wiring arm covers the other three, whose mint branches sit behind
    filesystem, chunker and T3 preconditions that a unit test cannot reach
    honestly; a source-level guard is a weaker claim than execution, and it
    is stated as such rather than dressed up as coverage it is not.
    """

    def test_register_or_lookup_doc_id_announces_before_minting(
        self, monkeypatch, tmp_path,
    ) -> None:
        """The behavioural arm: a real mint through the doc_indexer pre-flight."""
        from nexus import doc_indexer as di

        md = tmp_path / "shared.md"
        md.write_text("# shared\n")

        announced: list[tuple] = []
        registered: list[str] = []

        reader = SimpleNamespace(
            by_source_uri=lambda uri: None,
            by_file_path=lambda owner, fp: None,
            find_all_by_file_path=lambda fp: [_entry("8.1")],
            curator_owner_tumbler_by_name=lambda name: "4.4",
            close=lambda: None,
        )
        writer = SimpleNamespace(
            register=lambda **kw: (registered.append(kw["file_path"]), "7.1")[1],
            close=lambda: None,
        )
        monkeypatch.setattr(
            "nexus.catalog.factory.make_catalog_reader", lambda *a, **k: reader,
        )
        monkeypatch.setattr(
            "nexus.catalog.factory.make_catalog_writer", lambda *a, **k: writer,
        )
        monkeypatch.setattr(di, "_repo_owner_document_for", lambda r, p: None)
        # tmp_path IS a system temp dir, which the nexus-u8n4r ephemeral guard
        # refuses outright — that refusal returns before the mint, so without
        # this the test would asserts-nothing pass on an unreached branch.
        monkeypatch.setattr(
            "nexus.repo_identity.owner_repo_root_best_effort", lambda r, o: "",
        )
        monkeypatch.setattr(
            "nexus.repo_identity.should_skip_ephemeral_registration",
            lambda fp, root: False,
        )
        monkeypatch.setattr(
            "nexus.catalog.path_ambiguity.announce_cross_owner_mint",
            lambda reader, fp, **kw: announced.append((fp, kw)),
        )

        di._register_or_lookup_doc_id(
            md, "corp", content_type="knowledge", physical_collection="c",
        )

        assert registered, "the test must reach the mint, or it proves nothing"
        assert len(announced) == 1, (
            "the mint branch must announce before registering; "
            f"registered={registered} announced={announced}"
        )
        assert announced[0][0] == registered[0], (
            "the announced path must be the one actually registered — "
            "announcing a different path would report on the wrong file"
        )

    def test_every_mint_site_is_wired(self) -> None:
        """The wiring arm. Deleting any of the four call statements reds this.

        Each entry names the function whose mint must be preceded by the
        announce. Asserting POSITION (announce before register, inside the
        same function body) is what makes this more than a grep for the
        string somewhere in the file.
        """
        import inspect

        from nexus import doc_indexer as di
        from nexus import pipeline_stages as ps
        from nexus.commands import catalog as cat_cmd

        sites = [
            (ps, "_catalog_pdf_hook"),
            (di, "_register_or_lookup_doc_id"),
            (di, "_catalog_markdown_hook"),
            (cat_cmd, "_backfill_per_file_from_t3"),
        ]
        missing = []
        for mod, fname in sites:
            raw = inspect.getsource(getattr(mod, fname))
            # Comments in these functions discuss ``cat.register()`` in prose;
            # a naive search finds the PROSE first and reports a correctly
            # ordered call site as inverted.
            src = "\n".join(
                line.split("#", 1)[0] for line in raw.splitlines()
            )
            a = src.find("announce_cross_owner_mint(")
            r = src.find(".register(")
            if a == -1:
                missing.append(f"{mod.__name__}.{fname}: no announce at all")
            elif r != -1 and a > r:
                missing.append(
                    f"{mod.__name__}.{fname}: announce comes AFTER the register",
                )
        assert not missing, (
            "every path-keyed mint must announce before it registers: "
            + "; ".join(missing)
        )


# ── the collector, and the run summary that reads it ────────────────────────


class TestTheAnnouncementReachesTheOperator:
    """substantive-critic finding, 2026-09-21: a bare ``structlog.warning``
    reproduces an anti-pattern this codebase already diagnosed and fixed.
    nexus-39upx's own comment on the superseded-vector sweep says it
    "previously reported ONLY via structlog — invisible without log capture
    wired up". A duplication nobody is told about at the end of the run is
    the same silence this module exists to break, one layer out.
    """

    def setup_method(self):
        from nexus.catalog.path_ambiguity import reset_mints_over_existing_path
        reset_mints_over_existing_path()

    def test_the_announce_records_into_the_run_collector(self) -> None:
        from nexus.catalog.path_ambiguity import get_mints_over_existing_path

        announce_cross_owner_mint(
            _AnnounceReader(matches=[_entry("3.1"), _entry("3.2")]),
            "a/b.md", owner="4.0", context="unit",
        )

        rows = get_mints_over_existing_path()
        assert len(rows) == 1
        assert rows[0]["file_path"] == "a/b.md"
        assert rows[0]["existing"] == ["3.1", "3.2"]
        assert rows[0]["context"] == "unit"

    def test_a_clean_mint_records_nothing(self) -> None:
        from nexus.catalog.path_ambiguity import get_mints_over_existing_path

        announce_cross_owner_mint(
            _AnnounceReader(matches=[]), "a/new.md", owner="4.0", context="unit",
        )

        assert get_mints_over_existing_path() == []

    def test_reset_clears_it_between_runs(self) -> None:
        from nexus.catalog.path_ambiguity import (
            get_mints_over_existing_path,
            reset_mints_over_existing_path,
        )

        announce_cross_owner_mint(
            _AnnounceReader(matches=[_entry("3.1")]),
            "a/b.md", owner="4.0", context="unit",
        )
        assert get_mints_over_existing_path()

        reset_mints_over_existing_path()

        assert get_mints_over_existing_path() == [], (
            "a second run must not report the first run's duplications"
        )

    def test_the_index_run_resets_and_emits_the_collector(self) -> None:
        """The wiring: reset at run start, emit at run end, in index.py.

        Without this the collector could be populated and never surfaced,
        which is the exact failure the critic named.
        """
        import inspect

        from nexus.commands import index as index_cmd

        src = inspect.getsource(index_cmd)
        assert "reset_mints_over_existing_path()" in src, (
            "the run must zero the collector, or run N reports run N-1's rows"
        )
        assert "_emit_cross_owner_mint_summary()" in src, (
            "the collector must be READ at end of run, not merely filled"
        )
        emitter = inspect.getsource(index_cmd)
        assert emitter.index("_emit_cross_owner_mint_summary()") > emitter.index(
            "def _emit_cross_owner_mint_summary",
        ), "the emitter must be defined before it is called"
