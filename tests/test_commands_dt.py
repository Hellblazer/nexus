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

from unittest.mock import MagicMock, patch

import pytest

from tests._catalog_fixture_ops import ActiveCatalog
from click.testing import CliRunner

# RDR-109 Phase 2: this file asserts cloud-mode canonical behavior
# (voyage-* embedder names, canonical-set defaults). The cloud_mode
# fixture sets credentials and forces ``is_local_mode()`` to False so
# the assertions hold regardless of the host environment.
pytestmark = pytest.mark.usefixtures("cloud_mode")


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
        extractor: str = "auto",
    ) -> tuple[bool, int]:
        calls.append({
            "uuid": uuid,
            "path": path,
            "collection": collection,
            "corpus": corpus,
            "dry_run": dry_run,
            "extractor": extractor,
        })
        # Default success (stamped, chunks=1) — tests that want to exercise
        # the stamp-failed / unchanged summary paths replace the dispatcher
        # with their own fake.
        return True, 1

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
        assert fake_dispatcher[0]["collection"] == "knowledge__test__voyage-context-3__v1"  # nexus-t952k: --collection is normalised

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

    def test_extractor_passthrough(
        self, runner, fake_selectors, fake_dispatcher,
    ):
        # nexus-pxxyn: --extractor reaches the PDF indexer so the MinerU-failure
        # recovery (--extractor docling) is actionable on the DT path.
        from nexus.cli import main

        fake_selectors["selection"].return_value = [("U", "/a.pdf")]
        result = runner.invoke(main, [
            "dt", "index", "--selection", "--extractor", "docling",
        ])
        assert result.exit_code == 0, result.output
        assert fake_dispatcher[0]["extractor"] == "docling"

    def test_extractor_defaults_to_auto(
        self, runner, fake_selectors, fake_dispatcher,
    ):
        from nexus.cli import main

        fake_selectors["selection"].return_value = [("U", "/a.pdf")]
        result = runner.invoke(main, ["dt", "index", "--selection"])
        assert result.exit_code == 0, result.output
        assert fake_dispatcher[0]["extractor"] == "auto"


# ── nexus-cvaw: paper PDFs route to knowledge__ by default ──────────────────


class TestDefaultCollectionByExtension:
    """nexus-cvaw + RDR-103 Phase 5: nx dt index without --collection
    picks a paper-shaped home for PDFs
    (``knowledge__<corpus>-papers__voyage-context-3__v1``, where aspect
    extraction routes to scholarly-paper-v1) and a doc-shaped home for
    markdown (``docs__<corpus>__voyage-context-3__v1``). Phase 5
    promoted both defaults from the legacy 2-segment shape to the
    conformant 4-segment shape so the strict-naming guard at
    ``T3Database.get_or_create_collection`` accepts them.

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
        # No --collection: PDF default is the conformant
        # knowledge__dt-papers__voyage-context-3__v1 shape.
        assert (
            fake_dispatcher[0]["collection"]
            == "knowledge__dt-papers__voyage-context-3__v1"
        )

    def test_pdf_with_corpus_routes_to_knowledge_papers_corpus(
        self, runner, fake_selectors, fake_dispatcher,
    ):
        from nexus.cli import main

        fake_selectors["selection"].return_value = [("U", "/foo/paper.pdf")]
        result = runner.invoke(main, [
            "dt", "index", "--selection", "--corpus", "rag",
        ])
        assert result.exit_code == 0, result.output
        assert (
            fake_dispatcher[0]["collection"]
            == "knowledge__rag-papers__voyage-context-3__v1"
        )

    def test_markdown_default_routes_to_docs_dt(
        self, runner, fake_selectors, fake_dispatcher,
    ):
        from nexus.cli import main

        fake_selectors["selection"].return_value = [("U", "/foo/note.md")]
        result = runner.invoke(main, ["dt", "index", "--selection"])
        assert result.exit_code == 0, result.output
        # Markdown notes go to docs__<corpus> (current behavior, but
        # corpus default flipped from "default" to "dt" so the note
        # corpus matches the paper corpus by convention). Phase 5
        # added the conformant model+version trailer.
        assert (
            fake_dispatcher[0]["collection"]
            == "docs__dt__voyage-context-3__v1"
        )

    def test_markdown_with_corpus_routes_to_docs_corpus(
        self, runner, fake_selectors, fake_dispatcher,
    ):
        from nexus.cli import main

        fake_selectors["selection"].return_value = [("U", "/foo/note.md")]
        result = runner.invoke(main, [
            "dt", "index", "--selection", "--corpus", "rag",
        ])
        assert result.exit_code == 0, result.output
        assert (
            fake_dispatcher[0]["collection"]
            == "docs__rag__voyage-context-3__v1"
        )

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
        assert fake_dispatcher[0]["collection"] == "knowledge__custom-thing__voyage-context-3__v1"  # nexus-t952k: --collection is normalised


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
        def maybe_fail(uuid, path, *, collection, corpus, dry_run, extractor="auto"):
            return uuid == "U-OK", 1

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


# ── nexus-hb10j: --dt-content per-record catches must include the two ──────
# NexusError subclasses tp8yk/w6wp0 introduced (ChunkLandingUnverifiedError, ─
# IndexRunVerifyRefused) — collect-and-continue, mirroring the file-backed ──
# _index_record call site (dt.py 748-805), not a whole-batch abort. ─────────


class TestDtContentExceptionHandling:
    """``_index_dt_content_record`` (the ``--dt-content`` non-file-backed
    ingest path) only caught ``(RuntimeError, ImportError, OSError)`` around
    its ``index_markdown()`` call — ``ChunkLandingUnverifiedError`` and
    ``IndexRunVerifyRefused`` (both ``NexusError`` subclasses raised since
    tp8yk/w6wp0) fell through uncaught and aborted the WHOLE ``--dt-content``
    batch on the first affected record (third occurrence of the
    nexus-2fyb/qo84l/9800y regression class — filed by the 2xu6t critic, T2
    [21480]).
    """

    @pytest.fixture(autouse=True)
    def _dt_available(self, monkeypatch):
        """``dt_content_active`` gates on ``_dt.available()`` — force it on
        so the ``--dt-content`` branch is reached without a real DT."""
        import nexus.mcp_client.devonthink as _dt_mod

        monkeypatch.setattr(_dt_mod, "available", lambda **kw: True)

    def test_chunk_landing_unverified_collects_and_continues(
        self, runner, fake_selectors, monkeypatch,
    ):
        from nexus.cli import main
        from nexus.errors import ChunkLandingUnverifiedError

        fake_selectors["selection"].return_value = [
            ("U-BAD", "x-devonthink-item://bad"),
            ("U-OK", "x-devonthink-item://ok"),
        ]

        def fake_index(uuid, *, collection, corpus, extraction_source="dt_content"):
            if uuid == "U-BAD":
                raise ChunkLandingUnverifiedError(collection=collection, count=3)
            return True

        monkeypatch.setattr(
            "nexus.commands.dt._index_dt_content_record", fake_index,
        )

        result = runner.invoke(main, ["dt", "index", "--selection", "--dt-content"])

        # Collect-and-continue: U-OK must still be processed despite
        # U-BAD's exception — a whole-batch abort would report "Indexed 0
        # record(s)" (and, pre-fix, a raw traceback / empty output — see
        # the RED run) and never reach U-OK at all.
        assert "Indexed 1 record(s)" in result.output, result.output
        assert "1 from DT content" in result.output, result.output
        assert "1 failed" in result.output, result.output
        assert "U-BAD" in result.output, result.output
        assert "cannot confirm 3 chunk(s)" in result.output, result.output
        # ChunkLandingUnverifiedError fires BEFORE any manifest write
        # (doc_indexer.py:1202-1206, D1's whole point) and — unlike
        # IndexRunVerifyRefused's _record_complete_refusal side effect —
        # touches none of the three run-level gate collectors
        # (get_manifest_write_failures / get_manifest_identity_drops /
        # get_complete_refusals). So the per-record ``failed`` bucket
        # alone does not force a nonzero exit here; that's the run-level
        # gate's job (see test_dt_content_refusal_still_honours_run_
        # level_gate below), and this pin matches the file-backed
        # sibling's identical existing contract (dt.py 782-805).
        assert result.exit_code == 0, result.output

    def test_index_run_verify_refused_collects_and_continues(
        self, runner, fake_selectors, monkeypatch,
    ):
        from nexus.cli import main
        from nexus.errors import IndexRunVerifyRefused

        fake_selectors["selection"].return_value = [
            ("U-BAD", "x-devonthink-item://bad"),
            ("U-OK", "x-devonthink-item://ok"),
        ]

        def fake_index(uuid, *, collection, corpus, extraction_source="dt_content"):
            if uuid == "U-BAD":
                raise IndexRunVerifyRefused(
                    doc_id="1.99.1", referenced=5, present=3, missing=2,
                    chunk_count=5,
                )
            return True

        monkeypatch.setattr(
            "nexus.commands.dt._index_dt_content_record", fake_index,
        )

        result = runner.invoke(main, ["dt", "index", "--selection", "--dt-content"])

        assert "Indexed 1 record(s)" in result.output, result.output
        assert "1 from DT content" in result.output, result.output
        assert "1 failed" in result.output, result.output
        assert "U-BAD" in result.output, result.output
        assert "completion REFUSED" in result.output, result.output
        # A bare raise from this stub does not, by itself, populate
        # get_complete_refusals() (only the real doc_indexer._fence_
        # complete -> _record_complete_refusal call site does that, as a
        # side effect of the SAME exception in production) — see the
        # dedicated integration test below for that wiring. The run-level
        # gate still decides the exit code, and since nexus-l6tr7 it also
        # counts the refusals dt.py bucketed into `failed`, so a refusal
        # fails the run here too (nexus-tp8yk D2b) and the footer names
        # the collector divergence instead of staying silent.
        assert result.exit_code != 0, result.output
        assert "listed under failed" in result.output, result.output

    def test_dt_content_refusal_still_honours_run_level_gate(
        self, runner, fake_selectors, monkeypatch,
    ):
        """Integration check (nexus-hb10j fix-shape guidance: 'verify the
        record-level catches feed it consistently'): the run-level
        identity-drop/refusal gate (commands._helpers, nexus-7f5qj) is the
        ONLY thing that drives nonzero exit for a --dt-content batch —
        mirrors TestIdentityDropSummary.test_summary_surfaces_drops_and_
        batch_continues, which pins the same contract for the file-backed
        branch. Simulates the collector entry doc_indexer._fence_complete's
        real IndexRunVerifyRefused raise site (_record_complete_refusal)
        would populate, alongside the SAME exception being converted to a
        per-record ``failed`` entry by our new except clause — proving the
        new catch does not shadow, reset, or otherwise interfere with the
        collector-driven exit gate.
        """
        from nexus.errors import IndexRunVerifyRefused

        monkeypatch.setattr(
            "nexus.mcp_infra.get_complete_refusals",
            lambda: ["1.99.1"],
        )
        fake_selectors["selection"].return_value = [
            ("U-BAD", "x-devonthink-item://bad"),
        ]

        def fake_index(uuid, *, collection, corpus, extraction_source="dt_content"):
            raise IndexRunVerifyRefused(
                doc_id="1.99.1", referenced=5, present=3, missing=2,
                chunk_count=5,
            )

        monkeypatch.setattr(
            "nexus.commands.dt._index_dt_content_record", fake_index,
        )

        from nexus.cli import main

        result = runner.invoke(main, ["dt", "index", "--selection", "--dt-content"])

        assert "1 failed" in result.output, result.output
        assert "completion refused" in result.output.lower(), result.output
        assert result.exit_code != 0, result.output

    def test_register_ok_path_unchanged(
        self, runner, fake_selectors, monkeypatch,
    ):
        """Regression pin: no exception -> both records index cleanly via
        the dt-content path, exit 0, no 'failed' mention."""
        from nexus.cli import main

        fake_selectors["selection"].return_value = [
            ("U-A", "x-devonthink-item://a"),
            ("U-B", "x-devonthink-item://b"),
        ]

        monkeypatch.setattr(
            "nexus.commands.dt._index_dt_content_record",
            lambda uuid, *, collection, corpus, extraction_source="dt_content": True,
        )

        result = runner.invoke(main, ["dt", "index", "--selection", "--dt-content"])

        assert "Indexed 2 record(s)" in result.output, result.output
        assert "2 from DT content" in result.output, result.output
        assert "failed" not in result.output, result.output
        assert result.exit_code == 0, result.output

    def test_skip_path_unchanged_when_dt_content_returns_false(
        self, runner, fake_selectors, monkeypatch,
    ):
        """Kill control: the existing False-return (not an exception) path
        — e.g. empty DT text — must still bucket as skipped, not failed.
        Proves the new except clauses don't accidentally widen to catch
        the plain bool-return contract."""
        from nexus.cli import main

        fake_selectors["selection"].return_value = [
            ("U-EMPTY", "x-devonthink-item://empty"),
        ]

        monkeypatch.setattr(
            "nexus.commands.dt._index_dt_content_record",
            lambda uuid, *, collection, corpus, extraction_source="dt_content": False,
        )

        result = runner.invoke(main, ["dt", "index", "--selection", "--dt-content"])

        assert "Indexed 0 record(s) (1 skipped)" in result.output, result.output
        assert "failed" not in result.output, result.output
        assert result.exit_code == 0, result.output


# ── nexus-cy4oy: handler-BODY coverage for PER_RECORD_SURVIVABLE_EXCEPTIONS ─
#
# substantive-critic CRITICAL (round-2 review of nexus-rlkgu, T2 [21492]):
# the AST tripwire (tests/test_rlkgu_per_record_catch_tripwire.py) inspects
# except-clause TYPES, not handler BODIES. Its gates went green on a dt.py
# dispatch shaped `if isinstance(exc, IndexRunVerifyRefused): ... else: #
# assumes ChunkLandingUnverifiedError` — a hypothetical THIRD
# PER_RECORD_SURVIVABLE_EXCEPTIONS member would hit the else branch, access
# an attribute it doesn't have (.collection/.count), raise AttributeError
# INSIDE the handler, and escape the try/except — occurrence-4 of the
# nexus-2fyb/qo84l/9800y/hb10j class reproduced with both AST gates green.
# dt.py's dispatch is now TOTAL (explicit isinstance branch per known type
# + a generic final else with no attribute assumptions); the two test
# classes below are the handler-body coverage the AST gates structurally
# cannot give:
#
# * TestAllTupleMembersSurviveTheRealPerRecordPath — registry-driven
#   (iterates the REAL nexus.errors.PER_RECORD_SURVIVABLE_EXCEPTIONS tuple,
#   not hand-enumerated pytest functions) so a future third REAL member
#   gets this coverage automatically as long as _MEMBER_KWARGS stays in
#   sync (enforced by test_member_kwargs_registry_covers_every_tuple_member).
# * TestGenericFallbackHandlesUnknownTupleMember — the actual kill control:
#   monkeypatches in a SYNTHETIC third member with neither known shape and
#   proves the generic else branch survives it. Manually verified during
#   implementation: reverting dt.py's total dispatch back to the binary
#   if/else form (`if isinstance(exc, IndexRunVerifyRefused): ... else:
#   <ChunkLandingUnverifiedError-shaped access>`) turns both tests in that
#   class RED with an AttributeError escaping the handler; restoring the
#   fix turns them green again (see the developer's T1 scratch write-back
#   for the exact revert/restore transcript).

from nexus.errors import (  # noqa: E402 — grouped with this section's test-only imports
    ChunkLandingUnverifiedError,
    ExtractionQualityError,
    IndexRunVerifyRefused,
    NexusError as _NexusError,
    UnchunkableContentError,
    UnextractableContentError,
)

_MEMBER_KWARGS: dict[type, dict] = {
    ChunkLandingUnverifiedError: {
        "collection": "docs__dt-test__voyage-context-3__v1", "count": 3,
    },
    IndexRunVerifyRefused: {
        "doc_id": "1.99.1", "referenced": 5, "present": 3, "missing": 2,
        "chunk_count": 5,
    },
    # nexus-wi1uv round-2 (code-review-expert + substantive-critic
    # Critical, both independently, 2026-08-06): PDF post-extraction
    # quality-gate failures must survive nx dt index's per-record loop
    # exactly like the two members above.
    ExtractionQualityError: {
        "message": (
            "PDF paper.pdf failed the post-extraction quality gate "
            "(extraction_method=docling): whitespace_ratio=0.0114 < "
            "floor 0.05."
        ),
    },
    # nexus-rqsh1/nexus-1sd0f: pre-registration chunkability guard in
    # index_markdown/index_pdf — one zero-byte/binary record must fail
    # that record only, never abort the rest of the batch.
    UnchunkableContentError: {
        "message": (
            "refusing to index empty.md: file is zero bytes — nothing "
            "to chunk, no catalog document registered"
        ),
    },
    # nexus-deyd5: extraction completed but yielded zero usable text
    # (pymupdf/docling both fail an image-only or damaged-text-layer
    # PDF). Reachability differs by branch (verified against production
    # call graphs, not assumed):
    #   - FILE-BACKED branch: REAL production path. doc_indexer.index_pdf
    #     -> _pdf_chunks -> PDFExtractor().extract() -- the exact call
    #     chain ExtractionQualityError above already uses, one step
    #     earlier (extraction dispatch, before the post-extraction
    #     quality gate even runs). This test genuinely exercises a path
    #     that can happen in production.
    #   - DT-CONTENT branch: NOT reachable in production.
    #     _index_dt_content_record (commands/dt.py) sources DT-extracted
    #     TEXT and routes it through index_markdown -- it never touches
    #     PDFExtractor at all, so neither this type nor
    #     ExtractionQualityError (both PDFExtractor-only) can genuinely
    #     arise there. That branch's coverage is dispatch-robustness only
    #     (does the catch-and-continue logic around whatever
    #     _index_dt_content_record raises survive this SHAPE), matching
    #     how ExtractionQualityError is already treated here -- not a new
    #     gap this entry introduces, the same pre-existing distinction
    #     the section's own header comment describes ("handler-BODY
    #     coverage the AST gates structurally cannot give").
    UnextractableContentError: {
        "message": (
            "pymupdf produced empty output for scanned.pdf (page_count=3); "
            "the PDF may be image-only or have a damaged text layer. Try "
            "--extractor mineru or rerun OCR before indexing."
        ),
    },
}


def test_member_kwargs_registry_covers_every_tuple_member() -> None:
    """Completeness guard for the registry itself: a new
    PER_RECORD_SURVIVABLE_EXCEPTIONS member with no _MEMBER_KWARGS entry
    would silently skip the mechanical real-path drive-through below —
    this fails loud instead of silently under-covering."""
    from nexus.errors import PER_RECORD_SURVIVABLE_EXCEPTIONS

    missing = [
        c.__name__ for c in PER_RECORD_SURVIVABLE_EXCEPTIONS
        if c not in _MEMBER_KWARGS
    ]
    assert not missing, (
        "PER_RECORD_SURVIVABLE_EXCEPTIONS member(s) with no _MEMBER_KWARGS "
        f"registry entry in this test file — add one: {missing}"
    )


class TestAllTupleMembersSurviveTheRealPerRecordPath:
    """Registry-driven: iterates the REAL production
    ``PER_RECORD_SURVIVABLE_EXCEPTIONS`` tuple (not hand-enumerated test
    functions) and drives EACH member through both per-record branches,
    proving collect-and-continue. A future third REAL member is covered
    automatically as long as ``_MEMBER_KWARGS`` is kept in sync."""

    from nexus.errors import PER_RECORD_SURVIVABLE_EXCEPTIONS as _MEMBERS

    @pytest.mark.parametrize(
        "member_cls", list(_MEMBERS), ids=lambda c: c.__name__,
    )
    def test_dt_content_branch_collects_and_continues(
        self, runner, fake_selectors, monkeypatch, member_cls,
    ) -> None:
        from nexus.cli import main
        import nexus.mcp_client.devonthink as _dt_mod

        monkeypatch.setattr(_dt_mod, "available", lambda **kw: True)
        kwargs = _MEMBER_KWARGS[member_cls]
        fake_selectors["selection"].return_value = [
            ("U-BAD", "x-devonthink-item://bad"),
            ("U-OK", "x-devonthink-item://ok"),
        ]

        def fake_index(uuid, *, collection, corpus, extraction_source="dt_content"):
            if uuid == "U-BAD":
                raise member_cls(**kwargs)
            return True

        monkeypatch.setattr(
            "nexus.commands.dt._index_dt_content_record", fake_index,
        )

        result = runner.invoke(main, ["dt", "index", "--selection", "--dt-content"])

        assert "Traceback" not in result.output, result.output
        assert result.exception is None or isinstance(result.exception, SystemExit), (
            f"{member_cls.__name__} escaped the handler as a raw "
            f"traceback: {result.exception!r}"
        )
        assert "1 failed" in result.output, result.output
        assert "U-BAD" in result.output, result.output
        assert "Indexed 1 record(s)" in result.output, result.output  # U-OK still landed

    @pytest.mark.parametrize(
        "member_cls", list(_MEMBERS), ids=lambda c: c.__name__,
    )
    def test_file_backed_branch_collects_and_continues(
        self, runner, monkeypatch, member_cls,
    ) -> None:
        from nexus.cli import main

        kwargs = _MEMBER_KWARGS[member_cls]
        records = [("U1", "/a.pdf"), ("U2", "/b.pdf")]
        monkeypatch.setattr("nexus.commands.dt._gather_records", lambda **kw: records)
        monkeypatch.setattr("nexus.commands.dt._stamp_dt_uri_on_entry", lambda *a, **kw: True)

        seq = [
            lambda *a, **kw: (_ for _ in ()).throw(member_cls(**kwargs)),
            lambda *a, **kw: 4,
        ]

        def _dispatch(*a, **kw):
            return seq.pop(0)(*a, **kw)

        monkeypatch.setattr("nexus.doc_indexer.index_pdf", _dispatch)

        result = runner.invoke(main, ["dt", "index", "--uuid", records[0][0]])

        assert "Traceback" not in result.output, result.output
        assert result.exception is None or isinstance(result.exception, SystemExit), (
            f"{member_cls.__name__} escaped the handler as a raw "
            f"traceback: {result.exception!r}"
        )
        assert not seq, "U2 was never dispatched — the batch aborted after U1's exception"
        assert "1 failed" in result.output, result.output
        assert "Indexed 1 record(s)" in result.output, result.output


class _SyntheticThirdMember(_NexusError):
    """Test-only third ``PER_RECORD_SURVIVABLE_EXCEPTIONS`` member — NEVER
    added to the real production tuple. Deliberately carries neither
    ``ChunkLandingUnverifiedError``'s ``(.collection, .count)`` nor
    ``IndexRunVerifyRefused``'s field set: a handler whose fallback branch
    blindly assumes either shape raises ``AttributeError`` on this class."""

    def __init__(self, *, detail: str) -> None:
        self.detail = detail
        super().__init__(f"synthetic third member: {detail}")


class TestGenericFallbackHandlesUnknownTupleMember:
    """THE kill control for nexus-cy4oy: monkeypatches
    ``nexus.errors.PER_RECORD_SURVIVABLE_EXCEPTIONS`` to include a
    synthetic third member with an incompatible shape, raises it from the
    per-record helper, and proves dt.py's generic final ``else`` branch
    (type name + ``str(exc)``, no attribute assumptions) survives it —
    collect-and-continue, not a crash. dt.py's deferred
    ``from nexus.errors import (..., PER_RECORD_SURVIVABLE_EXCEPTIONS)``
    re-imports the name fresh on every ``index_cmd()`` call, so patching
    the module attribute before ``runner.invoke`` is picked up by the
    ``except PER_RECORD_SURVIVABLE_EXCEPTIONS`` clause at call time."""

    def test_dt_content_branch(self, runner, fake_selectors, monkeypatch) -> None:
        from nexus.cli import main
        import nexus.errors as errors_mod
        import nexus.mcp_client.devonthink as _dt_mod

        monkeypatch.setattr(_dt_mod, "available", lambda **kw: True)
        monkeypatch.setattr(
            errors_mod, "PER_RECORD_SURVIVABLE_EXCEPTIONS",
            (*errors_mod.PER_RECORD_SURVIVABLE_EXCEPTIONS, _SyntheticThirdMember),
        )
        fake_selectors["selection"].return_value = [
            ("U-BAD", "x-devonthink-item://bad"),
            ("U-OK", "x-devonthink-item://ok"),
        ]

        def fake_index(uuid, *, collection, corpus, extraction_source="dt_content"):
            if uuid == "U-BAD":
                raise _SyntheticThirdMember(detail="unknown-shape")
            return True

        monkeypatch.setattr(
            "nexus.commands.dt._index_dt_content_record", fake_index,
        )

        result = runner.invoke(main, ["dt", "index", "--selection", "--dt-content"])

        assert "Traceback" not in result.output, result.output
        assert result.exception is None or isinstance(result.exception, SystemExit), (
            f"the synthetic third member escaped the handler: {result.exception!r}"
        )
        assert "1 failed" in result.output, result.output
        assert "Indexed 1 record(s)" in result.output, result.output  # U-OK still landed
        assert "synthetic third member: unknown-shape" in result.output, result.output

    def test_file_backed_branch(self, runner, monkeypatch) -> None:
        from nexus.cli import main
        import nexus.errors as errors_mod

        monkeypatch.setattr(
            errors_mod, "PER_RECORD_SURVIVABLE_EXCEPTIONS",
            (*errors_mod.PER_RECORD_SURVIVABLE_EXCEPTIONS, _SyntheticThirdMember),
        )
        records = [("U1", "/a.pdf"), ("U2", "/b.pdf")]
        monkeypatch.setattr("nexus.commands.dt._gather_records", lambda **kw: records)
        monkeypatch.setattr("nexus.commands.dt._stamp_dt_uri_on_entry", lambda *a, **kw: True)

        seq = [
            lambda *a, **kw: (_ for _ in ()).throw(
                _SyntheticThirdMember(detail="unknown-shape"),
            ),
            lambda *a, **kw: 4,
        ]

        def _dispatch(*a, **kw):
            return seq.pop(0)(*a, **kw)

        monkeypatch.setattr("nexus.doc_indexer.index_pdf", _dispatch)

        result = runner.invoke(main, ["dt", "index", "--uuid", records[0][0]])

        assert "Traceback" not in result.output, result.output
        assert result.exception is None or isinstance(result.exception, SystemExit), (
            f"the synthetic third member escaped the handler: {result.exception!r}"
        )
        assert not seq, "U2 was never dispatched — the batch aborted after U1's exception"
        assert "1 failed" in result.output, result.output
        assert "Indexed 1 record(s)" in result.output, result.output
        assert "synthetic third member: unknown-shape" in result.output, result.output


# ── nexus-2xu6t: nx dt index must NOT report success when the catalog ───────
# register failed (pbawi acceptance item 3) ──────────────────────────────────


class TestIdentityDropSummary:
    """``nx dt index`` reports SUCCESS when the preflight catalog register
    failed — chunks land in T3 (searchable) but no catalog Document exists
    for them (no tumbler, no ``--link-semantic``, no ``--writeback``).

    nexus-tp8yk D2 already wires ``get_manifest_identity_drops()`` into
    this command's exit code (see ``index_cmd``'s tail, mirroring
    ``index_repo_cmd``'s ``_emit_manifest_write_failure_summary``); these
    tests are the first to actually exercise that wiring for ``nx dt
    index`` specifically. Two layers, mirroring test_index_cmd.py's own
    split for ``nx index repo``:

    * ``test_register_throw_*`` drives the REAL ``_register_or_lookup_
      doc_id`` swallow through a broken catalog writer (forcing
      ``NX_STORAGE_BACKEND_VECTORS=chroma`` so the write lands on the
      injected ``make_t3`` double rather than the real local engine's
      ``/v1/vectors`` service path — service mode is the pytest-env
      default per ``is_vector_service_mode``'s RDR-155 P4a.2 docstring),
      proving the register-failure -> collector link end to end.
    * ``test_summary_*`` mirrors ``test_identity_drop_summary_surfaces_
      drops`` / ``test_identity_drop_summary_silent_when_none`` in
      test_index_cmd.py: mocks the collector directly to pin the
      collector -> exit-code wiring in isolation, with a REAL
      multi-record dispatch (via ``fake_dispatcher``) proving the drop
      does not abort per-record processing.
    """

    @staticmethod
    def _empty_t3() -> MagicMock:
        t3 = MagicMock()
        t3.get_or_create_collection.return_value = MagicMock(
            get=MagicMock(return_value={"ids": [], "metadatas": []}),
        )
        return t3

    @staticmethod
    def _broken_catalog(*, register_raises: bool):
        """Reader/writer doubles mirroring the pbawi 409/wedge shape:
        first-time file, curator owner already resolves (no
        ``register_owner`` round-trip), and — when *register_raises* —
        ``writer.register`` throws exactly like a wedged owner's real
        engine 409 (nexus-pbawi)."""
        reader = MagicMock()
        reader.by_file_path.return_value = None
        reader.curator_owner_tumbler_by_name.return_value = "1.99"
        reader.find_by_file_path.return_value = MagicMock(tumbler="1.99.1")
        writer = MagicMock()
        if register_raises:
            writer.register.side_effect = RuntimeError(
                "integrity constraint violation",
            )
        else:
            writer.register.return_value = "1.99.1"
        return reader, writer

    def _make_md(self, tmp_path, name: str):
        p = tmp_path / f"{name}.md"
        p.write_text(f"# {name}\n\nSome real prose body for {name}.\n")
        return p

    def test_register_throw_exits_nonzero_with_distinct_summary_and_batch_continues(
        self, runner, fake_selectors, monkeypatch, tmp_path,
    ):
        from nexus.cli import main

        # Force the injected make_t3() double onto the write path instead of
        # the real local engine's /v1/vectors service route — see class
        # docstring. index_markdown stays on the single-flush
        # _index_document path either way (no streaming pipeline
        # involved), so this is the only override single-file .md needs.
        monkeypatch.setenv("NX_STORAGE_BACKEND_VECTORS", "chroma")
        # nexus-sghyo (2026-08-06): client-side Voyage embedding is retired
        # (Hal determination 2026-07-28) — local mode (ONNX) is the
        # surviving non-service dispatch path this test needs to reach
        # _register_or_lookup_doc_id via. The module-level ``cloud_mode``
        # fixture (pytestmark above) already monkeypatched
        # ``nexus.config.is_local_mode`` to a hardcoded ``False``; an env
        # var flip alone would not undo that, since the function object
        # itself was replaced. Re-patch it directly.
        monkeypatch.setattr("nexus.config.is_local_mode", lambda: True)

        md_a = self._make_md(tmp_path, "recA")
        md_b = self._make_md(tmp_path, "recB")
        fake_selectors["selection"].return_value = [
            ("U-A", str(md_a)),
            ("U-B", str(md_b)),
        ]

        reader, writer = self._broken_catalog(register_raises=True)

        with patch("nexus.doc_indexer.make_t3", return_value=self._empty_t3()), \
             patch("nexus.catalog.factory.make_catalog_reader", return_value=reader), \
             patch("nexus.catalog.factory.make_catalog_writer", return_value=writer):
            result = runner.invoke(main, ["dt", "index", "--selection"])

        # Collect-and-continue (nexus-9800y convention): the register
        # exception on record A must not abort record B — both land.
        assert "Indexed 2 record(s)" in result.output, result.output
        # Distinct, non-clean outcome — never the plain success summary.
        assert (
            "WITHOUT a catalog document identity" in result.output
        ), result.output
        assert "nx catalog reconcile" in result.output
        # pbawi acceptance item 3, verbatim requirement: must NOT exit 0.
        assert result.exit_code != 0, result.output

    def test_register_ok_summary_unchanged(
        self, runner, fake_selectors, monkeypatch, tmp_path,
    ):
        """Baseline regression pin: when registration succeeds, the
        identity-drop WARNING must not appear and the run exits 0 —
        the fix must not cry wolf on the healthy path."""
        from nexus.cli import main

        monkeypatch.setenv("NX_STORAGE_BACKEND_VECTORS", "chroma")
        # nexus-sghyo (2026-08-06): see test_register_throw_* above — local
        # mode is the surviving non-service dispatch path.
        monkeypatch.setattr("nexus.config.is_local_mode", lambda: True)

        md_a = self._make_md(tmp_path, "recOK")
        fake_selectors["selection"].return_value = [("U-OK", str(md_a))]

        reader, writer = self._broken_catalog(register_raises=False)

        with patch("nexus.doc_indexer.make_t3", return_value=self._empty_t3()), \
             patch("nexus.doc_indexer._fence_begin"), \
             patch("nexus.doc_indexer._fence_complete"), \
             patch("nexus.catalog.factory.make_catalog_reader", return_value=reader), \
             patch("nexus.catalog.factory.make_catalog_writer", return_value=writer):
            result = runner.invoke(main, ["dt", "index", "--selection"])

        assert "Indexed 1 record(s)" in result.output, result.output
        assert "WITHOUT a catalog document identity" not in result.output
        assert result.exit_code == 0, result.output

    def test_summary_surfaces_drops_and_batch_continues(
        self, runner, fake_selectors, fake_dispatcher, monkeypatch,
    ):
        """Collector -> exit-code wiring, isolated from the register
        mechanism (mirrors test_index_cmd.py's ``test_identity_drop_
        summary_surfaces_drops``). ``fake_dispatcher`` reports two clean
        per-record successes; the collector nonetheless reports drops
        (as it would after a real register failure) — the run must
        still process BOTH records (collect-and-continue) and THEN
        fail loud at the tail.
        """
        monkeypatch.setattr(
            "nexus.mcp_infra.get_manifest_identity_drops",
            lambda: [
                {"collection": "docs__dt", "batch_size": 3},
                {"collection": "docs__dt", "batch_size": 5},
            ],
        )
        fake_selectors["selection"].return_value = [
            ("U-A", "/a.pdf"), ("U-B", "/b.md"),
        ]
        from nexus.cli import main

        result = runner.invoke(main, ["dt", "index", "--selection"])

        assert len(fake_dispatcher) == 2, "both records must be dispatched"
        assert "Indexed 2 record(s)" in result.output, result.output
        assert (
            "WARNING: 2 chunk batch(es) (8 chunks; collection(s): "
            "docs__dt) were indexed WITHOUT a catalog document identity"
        ) in result.output, result.output
        assert "nx catalog reconcile" in result.output
        assert result.exit_code != 0, result.output

    def test_summary_silent_when_no_drops(
        self, runner, fake_selectors, fake_dispatcher,
    ):
        fake_selectors["selection"].return_value = [("U", "/a.pdf")]
        from nexus.cli import main

        result = runner.invoke(main, ["dt", "index", "--selection"])

        assert result.exit_code == 0, result.output
        assert "WITHOUT a catalog document identity" not in result.output


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

    def _setup_catalog_with_entry(self, tmp_path, file_path, monkeypatch=None):
        """Stand up a catalog with a single registered entry pointing
        at ``file_path`` (mimics the post-index state before the
        stamp helper runs). Returns the catalog instance."""
        # nexus-aqbrk: seed through the ACTIVE catalog — _stamp_dt_uri_on_entry
        # resolves via the factory, so a local-only seed left the service
        # catalog empty and the read-back found the pre-stamp source_uri.
        # (The local Catalog.init that used to run here died with the local
        # catalog in the terminal nexus-i711w deletion.)
        if monkeypatch is not None:
            monkeypatch.setenv("NEXUS_CATALOG_PATH", str(tmp_path / "catalog"))
        cat = ActiveCatalog()
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
        cat = self._setup_catalog_with_entry(tmp_path, file_path, monkeypatch)
        cat_dir = tmp_path / "catalog"
        monkeypatch.setattr(
            "nexus.config.catalog_path", lambda: cat_dir,
        )

        uuid = "8EDC855D-213F-40AD-A9CF-9543CC76476B"
        _stamp_dt_uri_on_entry(file_path, uuid)

        # Reopen so we read post-write state.
        cat2 = ActiveCatalog()
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
            # ActiveCatalog resolves a fresh reader per call and owns no handle
            # to close (its __getattr__ refuses _-prefixed attributes on
            # purpose); the SQLite arm's factory reader is closed by the
            # factory.
            pass

    def test_no_entry_match_logs_and_returns(
        self, tmp_path, monkeypatch,
    ):
        """When no catalog entry matches the file_path (rare —
        post-index, but possible if a concurrent purge runs), the
        stamp helper logs a warning and returns cleanly. It must not
        raise."""
        from nexus.commands.dt import _stamp_dt_uri_on_entry

        # Service catalog with NO matching entry registered (nexus-i711w:
        # the local Catalog.init that used to run here died with the local
        # catalog; the per-test tenant starts empty).
        cat_dir = tmp_path / "catalog"
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


# ── Layer F write-back (RDR-139 P1.7) ─────────────────────────────────────────


class TestWriteback:
    """``nx dt index --writeback`` stamps the nexus identity back onto DT.

    The DT-side stamp itself is exercised by ``tests/test_dt_writeback.py``
    against a fake DT client; here we pin the CLI wiring: the flag gates the
    call, it fires once per successfully-stamped record, and the summary
    reports the count.
    """

    @pytest.fixture(autouse=True)
    def _dt_available(self, monkeypatch):
        """nexus-fdk1x: index_cmd now probes DT reachability once up front
        for any of dt-content/link-semantic/writeback/highlights before
        running the per-record layer calls below (which are themselves
        monkeypatched to bypass DT entirely) -- force the probe on so these
        CLI-wiring tests keep exercising the stubbed layer functions rather
        than tripping the new "DEVONthink MCP unreachable" exit-2 path.
        """
        import nexus.mcp_client.devonthink as _dt_mod

        monkeypatch.setattr(_dt_mod, "available", lambda **kw: True)

    def test_writeback_invoked_per_stamped_record(
        self, runner, fake_selectors, fake_dispatcher, monkeypatch,
    ):
        from nexus.cli import main

        calls: list[str] = []
        monkeypatch.setattr(
            "nexus.commands.dt._writeback_record",
            lambda uuid: calls.append(uuid) or True,
        )
        fake_selectors["uuid"].return_value = [("U1", "/a.pdf")]
        result = runner.invoke(main, ["dt", "index", "--uuid", "U1", "--writeback"])
        assert result.exit_code == 0, result.output
        assert calls == ["U1"]
        assert "written back to DT" in result.output

    def test_no_writeback_flag_skips_call(
        self, runner, fake_selectors, fake_dispatcher, monkeypatch,
    ):
        from nexus.cli import main

        calls: list[str] = []
        monkeypatch.setattr(
            "nexus.commands.dt._writeback_record",
            lambda uuid: calls.append(uuid) or True,
        )
        fake_selectors["uuid"].return_value = [("U1", "/a.pdf")]
        result = runner.invoke(main, ["dt", "index", "--uuid", "U1"])
        assert result.exit_code == 0, result.output
        assert calls == []
        assert "written back" not in result.output

    def test_writeback_help_documents_namespace(self, runner):
        from nexus.cli import main

        result = runner.invoke(main, ["dt", "index", "--help"])
        assert result.exit_code == 0
        assert "--writeback" in result.output


# ── Layer B semantic linking (RDR-139 P1.5 CLI wiring) ────────────────────────


class TestLinkSemantic:
    """``nx dt index --link-semantic`` invokes Layer B edge generation."""

    @pytest.fixture(autouse=True)
    def _dt_available(self, monkeypatch):
        """nexus-fdk1x: see TestWriteback._dt_available."""
        import nexus.mcp_client.devonthink as _dt_mod

        monkeypatch.setattr(_dt_mod, "available", lambda **kw: True)

    def test_link_semantic_invoked_per_stamped_record(
        self, runner, fake_selectors, fake_dispatcher, monkeypatch,
    ):
        from nexus.cli import main

        calls: list[str] = []
        monkeypatch.setattr(
            "nexus.commands.dt._link_semantic_record",
            lambda uuid: calls.append(uuid) or True,
        )
        fake_selectors["uuid"].return_value = [("U1", "/a.pdf")]
        result = runner.invoke(main, ["dt", "index", "--uuid", "U1", "--link-semantic"])
        assert result.exit_code == 0, result.output
        assert calls == ["U1"]
        assert "semantically linked" in result.output

    def test_no_link_semantic_flag_skips_call(
        self, runner, fake_selectors, fake_dispatcher, monkeypatch,
    ):
        from nexus.cli import main

        calls: list[str] = []
        monkeypatch.setattr(
            "nexus.commands.dt._link_semantic_record",
            lambda uuid: calls.append(uuid) or True,
        )
        fake_selectors["uuid"].return_value = [("U1", "/a.pdf")]
        result = runner.invoke(main, ["dt", "index", "--uuid", "U1"])
        assert result.exit_code == 0, result.output
        assert calls == []
        assert "semantically linked" not in result.output

    def test_stamp_failed_record_skips_link_and_writeback(
        self, runner, fake_selectors, monkeypatch,
    ):
        # A record that fails to stamp has no resolvable tumbler, so neither
        # link nor write-back may run on it (the `continue` after stamp_failed).
        from nexus.cli import main

        link_calls: list[str] = []
        wb_calls: list[str] = []
        monkeypatch.setattr(
            "nexus.commands.dt._index_record",
            lambda uuid, path, *, collection, corpus, dry_run, extractor="auto": (uuid == "U-OK", 1),
        )
        monkeypatch.setattr(
            "nexus.commands.dt._link_semantic_record",
            lambda uuid: link_calls.append(uuid) or True,
        )
        monkeypatch.setattr(
            "nexus.commands.dt._writeback_record",
            lambda uuid: wb_calls.append(uuid) or True,
        )
        fake_selectors["selection"].return_value = [("U-OK", "/a.pdf"), ("U-FAIL", "/b.pdf")]
        result = runner.invoke(
            main, ["dt", "index", "--selection", "--link-semantic", "--writeback"],
        )
        assert result.exit_code == 0, result.output
        # Only the stamped record reached link + write-back.
        assert link_calls == ["U-OK"]
        assert wb_calls == ["U-OK"]
        assert "1 DT-URI stamp-failed" in result.output

    def test_link_and_writeback_compose(
        self, runner, fake_selectors, fake_dispatcher, monkeypatch,
    ):
        from nexus.cli import main

        link_calls: list[str] = []
        wb_calls: list[str] = []
        monkeypatch.setattr(
            "nexus.commands.dt._link_semantic_record",
            lambda uuid: link_calls.append(uuid) or True,
        )
        monkeypatch.setattr(
            "nexus.commands.dt._writeback_record",
            lambda uuid: wb_calls.append(uuid) or True,
        )
        fake_selectors["uuid"].return_value = [("U1", "/a.pdf")]
        result = runner.invoke(
            main, ["dt", "index", "--uuid", "U1", "--link-semantic", "--writeback"],
        )
        assert result.exit_code == 0, result.output
        assert link_calls == ["U1"] and wb_calls == ["U1"]
        assert "semantically linked" in result.output
        assert "written back to DT" in result.output


class TestEnrichWiring:
    """RDR-139 Layer C: ``nx dt index --enrich`` runs a DT-CrossRef bib
    gap-fill pass over each touched collection after indexing."""

    def test_enrich_runs_bib_enrichment_once_per_collection(
        self, runner, fake_selectors, fake_dispatcher, monkeypatch,
    ):
        from nexus.cli import main

        calls: list[tuple[str, dict]] = []
        monkeypatch.setattr(
            "nexus.commands.enrich.run_bib_enrichment",
            lambda coll, **kw: calls.append((coll, kw)),
        )
        # Two records, one explicit collection -> a single enrichment pass.
        fake_selectors["selection"].return_value = [
            ("U1", "/a.pdf"), ("U2", "/b.pdf"),
        ]
        result = runner.invoke(main, [
            "dt", "index", "--selection",
            "--collection", "knowledge__test", "--enrich",
        ])
        assert result.exit_code == 0, result.output
        assert calls == [("knowledge__test__voyage-context-3__v1", {"source": "dt"})]  # nexus-t952k
        assert "Enriching bibliographic metadata" in result.output

    def test_no_enrich_flag_skips_enrichment(
        self, runner, fake_selectors, fake_dispatcher, monkeypatch,
    ):
        from nexus.cli import main

        calls: list[str] = []
        monkeypatch.setattr(
            "nexus.commands.enrich.run_bib_enrichment",
            lambda coll, **kw: calls.append(coll),
        )
        fake_selectors["selection"].return_value = [("U1", "/a.pdf")]
        result = runner.invoke(main, ["dt", "index", "--selection"])
        assert result.exit_code == 0, result.output
        assert calls == []


class TestIncorporateCmd:
    """nexus-goypg: `nx dt incorporate` is the relocated dt_incorporate
    composite from the retired nx-mcp-devonthink proxy — the one capability
    DEVONthink's own MCP server cannot provide."""

    def test_help_registered(self, runner):
        from nexus.commands.dt import dt

        result = runner.invoke(dt, ["incorporate", "--help"])
        assert result.exit_code == 0
        assert "already-indexed DT record" in result.output

    def test_rejects_non_uuid_argument(self, runner, monkeypatch):
        import nexus.commands.dt as dt_cmd_mod

        monkeypatch.setattr(dt_cmd_mod, "_is_darwin", lambda: True)
        result = runner.invoke(dt_cmd_mod.dt, ["incorporate", "not-a-uuid"])
        assert result.exit_code != 0
        assert "UUID" in result.output

    def test_not_indexed_record_errors_with_remedy(self, runner, monkeypatch, tmp_path):
        import nexus.commands.dt as dt_cmd_mod

        monkeypatch.setattr(dt_cmd_mod, "_is_darwin", lambda: True)
        monkeypatch.setattr("nexus.config.catalog_path", lambda: tmp_path / "cat")
        fake_cat = MagicMock()
        fake_cat.by_source_uri.return_value = None
        monkeypatch.setattr("nexus.catalog.factory.make_catalog_reader", lambda: fake_cat)

        uuid = "8EDC855D-213F-40AD-A9CF-9543CC76476B"
        result = runner.invoke(dt_cmd_mod.dt, ["incorporate", uuid])
        assert result.exit_code != 0
        assert "nx dt index" in result.output
        fake_cat.by_source_uri.assert_called_once_with(f"x-devonthink-item://{uuid}")
        fake_cat.close.assert_called_once()

    def test_happy_path_links_and_writeback(self, runner, monkeypatch, tmp_path):
        import nexus.commands.dt as dt_cmd_mod

        monkeypatch.setattr(dt_cmd_mod, "_is_darwin", lambda: True)
        monkeypatch.setattr("nexus.config.catalog_path", lambda: tmp_path / "cat")
        entry = MagicMock()
        entry.tumbler = "1.2.3"
        fake_cat = MagicMock()
        fake_cat.by_source_uri.return_value = entry
        fake_writer = MagicMock()
        fake_links = MagicMock(return_value={"relates": 2})
        fake_writeback = MagicMock(return_value={"tags": True})
        monkeypatch.setattr("nexus.catalog.factory.make_catalog_reader", lambda: fake_cat)
        monkeypatch.setattr(
            "nexus.catalog.factory.make_catalog_writer",
            lambda priority: fake_writer,
        )
        monkeypatch.setattr("nexus.catalog.dt_link_generator.generate_dt_links", fake_links)
        monkeypatch.setattr("nexus.dt_writeback.writeback_record", fake_writeback)

        uuid = "8EDC855D-213F-40AD-A9CF-9543CC76476B"
        result = runner.invoke(dt_cmd_mod.dt, ["incorporate", uuid])
        assert result.exit_code == 0, result.output
        assert "1.2.3" in result.output
        fake_links.assert_called_once_with(fake_cat, "1.2.3", uuid, writer=fake_writer)
        fake_writeback.assert_called_once_with(uuid, "1.2.3")
        fake_writer.close.assert_called_once()
        fake_cat.close.assert_called_once()
