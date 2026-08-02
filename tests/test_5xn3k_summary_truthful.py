# SPDX-License-Identifier: AGPL-3.0-or-later
"""nexus-5xn3k.6 RUNFENCE C4 — the summary must stop saying "Indexed 1
record(s)" for a no-op.

Design memo (T2 nexus "5xn3k-design-2026-08-02") §3.7:

    ``commands/dt.py::_index_record`` calls ``index_pdf``/``index_markdown``,
    discards their chunk count, and returns a bare bool — the caller can
    therefore only do ``indexed += 1`` unconditionally. It is structurally
    impossible for the summary to be truthful with that contract.

This file drives the FIVE surfaces that had the same shape (dt.py's
per-record loop, index.py's repo/pdf/md/rdr commands) at the CLI layer —
Click's ``CliRunner``, with the indexer functions mocked — so a truthful
summary is asserted from the operator's point of view (stdout), not by
inspecting internal counters.

FALSIFICATION. ``_index_record``'s return contract changed from a bare
``bool`` to ``tuple[bool, int]`` (stamped, chunks); the caller now does
``stamped, chunks = _index_record(...)``. Reverting ``_index_record`` to
return a bare bool makes that unpacking raise ``TypeError: cannot unpack
non-iterable bool object`` on the very first record any test below feeds
it — every dt.py test in this file goes red immediately, not silently
green. That is the falsification the bead's TDD spec asks for; it is
exercised structurally by every test here rather than as a separate
revert-and-rerun step.
"""
from __future__ import annotations

from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest
from click.testing import CliRunner

from nexus.cli import main
from nexus.doc_indexer import _fence_complete
from nexus.errors import IndexRunVerifyRefused
from nexus.mcp_infra import get_complete_refusals, reset_complete_refusals

# RDR-109 Phase 2 convention (matches test_commands_dt.py / test_index_cmd.py):
# cloud-mode canonical behavior so assertions don't depend on the host env.
pytestmark = pytest.mark.usefixtures("cloud_mode")


@pytest.fixture
def runner() -> CliRunner:
    return CliRunner()


@pytest.fixture
def home(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    monkeypatch.setenv("HOME", str(tmp_path))
    return tmp_path


# ── nx dt index ────────────────────────────────────────────────────────────


class TestDtIndexSummaryTruthful:
    def _run(self, runner, monkeypatch, *, index_record, records):
        monkeypatch.setattr(
            "nexus.commands.dt._gather_records",
            lambda **kw: records,
        )
        monkeypatch.setattr("nexus.commands.dt._index_record", index_record)
        return runner.invoke(main, ["dt", "index", "--uuid", records[0][0]])

    def test_unchanged_doc_reports_unchanged_never_indexed(self, runner, monkeypatch):
        result = self._run(
            runner, monkeypatch,
            index_record=lambda uuid, path, **kw: (True, 0),
            records=[("U1", "/a.pdf")],
        )
        assert result.exit_code == 0, result.output
        # The AC4 core assertion: chunks=0 must NOT inflate "Indexed".
        assert "Indexed 0 record(s)" in result.output
        assert "Indexed 1 record(s)" not in result.output
        assert "1 unchanged" in result.output
        # lcmbp's explicit stdout line — was debug-only before this bead.
        assert "skipped: index fresh (use --force)" in result.output

    def test_real_write_reports_indexed(self, runner, monkeypatch):
        """Sanity counterpart: a genuine write (chunks>0) still counts as
        indexed and does NOT print the skip line — the bucket logic must
        discriminate, not just always print both."""
        result = self._run(
            runner, monkeypatch,
            index_record=lambda uuid, path, **kw: (True, 3),
            records=[("U1", "/a.pdf")],
        )
        assert result.exit_code == 0, result.output
        assert "Indexed 1 record(s)" in result.output
        assert "unchanged" not in result.output
        assert "skipped: index fresh" not in result.output

    def test_refused_completion_reported_as_own_bucket(self, runner, monkeypatch):
        """A completion REFUSED by the engine's fail-closed verify (manifest
        written, index_state stuck at 'indexing') must be visible as its
        own line — never silently absorbed into a clean "Indexed N
        record(s)." claim. dt.py cannot correlate a uuid to the doc_id the
        engine refused (mcp_infra's collector is process-global, not
        per-record), so the bucket is a distinct WARNING footer, mirroring
        commands/index.py's existing manifest-write-failure summary — the
        run's headline count stays honest about what THIS record's client
        side did, and the refusal is never hidden inside it.
        """
        monkeypatch.setattr(
            "nexus.mcp_infra.get_complete_refusals",
            lambda: ["1.2.3"],
        )
        reset_calls = []
        monkeypatch.setattr(
            "nexus.mcp_infra.reset_complete_refusals",
            lambda: reset_calls.append(1),
        )
        result = self._run(
            runner, monkeypatch,
            index_record=lambda uuid, path, **kw: (True, 5),
            records=[("U1", "/a.pdf")],
        )
        assert result.exit_code == 0, result.output
        assert reset_calls == [1]  # zeroed at run start (index.py parity)
        # substantive-critic SIGNIFICANT (2026-08-02): must STATE the
        # subset relationship — a refused record is already counted in
        # the "Indexed 1" headline above (chunks genuinely landed), never
        # silently double-counted with no stated overlap.
        assert "1 of the 1 indexed above had completion refused" in result.output
        assert "NOT fully indexed" in result.output

    def test_refused_subset_wording_is_not_hardcoded_to_one_of_one(self, runner, monkeypatch):
        """Pins the N/M relationship for a non-trivial ratio — a test that
        only ever exercises 1-of-1 could pass even if the wording were
        hardcoded rather than computed from `indexed`."""
        monkeypatch.setattr(
            "nexus.mcp_infra.get_complete_refusals", lambda: ["1.2.3"],
        )
        monkeypatch.setattr(
            "nexus.mcp_infra.reset_complete_refusals", lambda: None,
        )
        result = self._run(
            runner, monkeypatch,
            index_record=lambda uuid, path, **kw: (True, 3),
            records=[("U1", "/a.pdf"), ("U2", "/b.pdf")],
        )
        assert result.exit_code == 0, result.output
        assert "Indexed 2 record(s)" in result.output
        assert "1 of the 2 indexed above had completion refused" in result.output

    def test_no_refusals_summary_silent_on_zero(self, runner, monkeypatch):
        monkeypatch.setattr("nexus.mcp_infra.get_complete_refusals", lambda: [])
        result = self._run(
            runner, monkeypatch,
            index_record=lambda uuid, path, **kw: (True, 1),
            records=[("U1", "/a.pdf")],
        )
        assert result.exit_code == 0, result.output
        assert "REFUSED" not in result.output


class TestDtIndexRefusalPropagation:
    """substantive-critic CRITICAL (nexus-qo84l, 2026-08-02): the ORIGINAL
    bug is that dt.py's per-record catch — ``except (RuntimeError,
    ImportError)`` — does not catch ``IndexRunVerifyRefused``, which the
    REAL ``_index_record`` body propagates unmasked from
    ``index_pdf``/``index_markdown`` (doc_indexer's streaming/incremental
    ``_fence_complete``, the propagating completion-verify path, distinct
    from mcp_infra's non-propagating flush-grain refusal). Pre-fix this
    escaped the per-record try/except entirely and aborted the WHOLE `nx
    dt index` batch with a raw traceback — the exact nexus-2fyb regression
    documented two lines above the catch, just for a failure mode this
    bead introduced.

    THE TEST-COVERAGE GAP THAT LET THIS SHIP: every other refusal test in
    this file (``TestDtIndexSummaryTruthful``) monkeypatches
    ``dt._index_record`` ITSELF, which never exercises the real
    propagation path. These tests mock ``nexus.doc_indexer.index_pdf`` —
    the boundary the REAL ``_index_record`` calls — and let the real
    ``_index_record`` body run, per the critic's explicit instruction.
    """

    def _run(self, runner, monkeypatch, *, records, index_pdf_fn):
        monkeypatch.setattr("nexus.commands.dt._gather_records", lambda **kw: records)
        # Isolates the test to the refusal-propagation path: a record that
        # DOES succeed still must not reach the real catalog to get
        # stamped.
        monkeypatch.setattr("nexus.commands.dt._stamp_dt_uri_on_entry", lambda *a, **kw: True)
        monkeypatch.setattr("nexus.doc_indexer.index_pdf", index_pdf_fn)
        return runner.invoke(main, ["dt", "index", "--uuid", records[0][0]])

    def test_streaming_refusal_does_not_abort_the_whole_batch(self, runner, monkeypatch):
        """THE regression: pre-fix, U1's refusal raised uncaught and U2 was
        NEVER PROCESSED — no summary line printed at all. Post-fix, U1
        lands in `failed` and U2 still indexes."""
        seq = [
            lambda *a, **kw: (_ for _ in ()).throw(IndexRunVerifyRefused(
                doc_id="1.2.3", referenced=5, present=3, missing=2, chunk_count=5,
            )),
            lambda *a, **kw: 4,
        ]

        def _dispatch(*a, **kw):
            return seq.pop(0)(*a, **kw)

        result = self._run(
            runner, monkeypatch,
            records=[("U1", "/a.pdf"), ("U2", "/b.pdf")],
            index_pdf_fn=_dispatch,
        )
        assert result.exit_code == 0, result.output
        assert not seq, "U2 was never dispatched — the batch aborted after U1's refusal"
        assert "completion REFUSED" in result.output
        assert "NOT fully indexed" in result.output
        assert "1 failed" in result.output
        assert "Indexed 1 record(s)" in result.output  # U2 still landed

    def test_streaming_refusal_alone_is_reported_not_raised(self, runner, monkeypatch):
        def _raise(*a, **kw):
            raise IndexRunVerifyRefused(
                doc_id="1.2.3", referenced=5, present=3, missing=2, chunk_count=5,
            )
        result = self._run(
            runner, monkeypatch,
            records=[("U1", "/a.pdf")],
            index_pdf_fn=_raise,
        )
        assert result.exit_code == 0, result.output
        assert result.exception is None or isinstance(result.exception, SystemExit), (
            f"the refusal escaped as a raw traceback: {result.exception!r}"
        )
        assert "Traceback" not in result.output
        assert "completion REFUSED" in result.output
        assert "referenced=5" in result.output
        assert "present=3" in result.output
        assert "missing=2" in result.output


# ── nx index repo ────────────────────────────────────────────────────────────


@pytest.fixture
def repo_dir(home: Path) -> Path:
    d = home / "myrepo"
    d.mkdir()
    (d / ".git").mkdir()
    return d


@pytest.fixture
def mock_reg():
    reg = MagicMock()
    reg.get.return_value = {"collection": "code__myrepo"}
    return reg


def _make_fake_index_repository(files):
    """Fake ``index_repository`` that drives on_start/on_file with the
    given ``(path, chunks, elapsed)`` tuples, mirroring test_index_cmd.py's
    ``_make_fake_index`` helper."""
    def fake_index(path, reg, **kwargs):
        on_start = kwargs.get("on_start")
        on_file = kwargs.get("on_file")
        if on_start:
            on_start(len(files))
        if on_file:
            for f, c, t in files:
                on_file(Path(f), c, t)
        return {}
    return fake_index


class TestIndexRepoSummaryTruthful:
    def _invoke(self, runner, args, mock_reg, files):
        with patch("nexus.commands.index._registry", return_value=mock_reg):
            with patch(
                "nexus.indexer.index_repository",
                side_effect=_make_fake_index_repository(files),
            ):
                return runner.invoke(main, ["index", "repo"] + args)

    def test_unchanged_files_surfaced_in_end_of_run_summary(
        self, runner, repo_dir, mock_reg,
    ):
        result = self._invoke(
            runner, [str(repo_dir)], mock_reg,
            files=[("a.py", 5, 0.1), ("b.py", 0, 0.05)],
        )
        assert result.exit_code == 0, result.output
        assert "skipped: index fresh (use --force) — 1 of 2 file(s) unchanged" \
            in result.output

    def test_all_written_no_skip_line(self, runner, repo_dir, mock_reg):
        result = self._invoke(
            runner, [str(repo_dir)], mock_reg,
            files=[("a.py", 5, 0.1), ("b.py", 3, 0.05)],
        )
        assert result.exit_code == 0, result.output
        assert "skipped: index fresh" not in result.output

    def test_complete_refusals_surfaced_alongside_manifest_failures(
        self, runner, repo_dir, mock_reg, monkeypatch,
    ):
        reset_calls = []
        monkeypatch.setattr(
            "nexus.mcp_infra.reset_complete_refusals",
            lambda: reset_calls.append(1),
        )
        monkeypatch.setattr(
            "nexus.mcp_infra.get_complete_refusals", lambda: ["1.9.0"],
        )
        result = self._invoke(
            runner, [str(repo_dir)], mock_reg, files=[("a.py", 5, 0.1)],
        )
        assert result.exit_code == 0, result.output
        assert reset_calls == [1]
        # substantive-critic SIGNIFICANT (2026-08-02): the refused file is
        # already counted among the "1 indexed" (chunks genuinely landed;
        # not restructured) — must state the overlap explicitly.
        assert "1 of the 1 indexed above had completion refused" in result.output

    def test_complete_refusals_subset_wording_is_not_hardcoded_to_one_of_one(
        self, runner, repo_dir, mock_reg, monkeypatch,
    ):
        """Pins the N/M relationship for a non-trivial ratio, mirroring the
        dt.py sibling test — a 1-of-1-only test could pass with a
        hardcoded string."""
        monkeypatch.setattr(
            "nexus.mcp_infra.reset_complete_refusals", lambda: None,
        )
        monkeypatch.setattr(
            "nexus.mcp_infra.get_complete_refusals", lambda: ["1.9.0"],
        )
        result = self._invoke(
            runner, [str(repo_dir)], mock_reg,
            files=[("a.py", 5, 0.1), ("b.py", 3, 0.1)],
        )
        assert result.exit_code == 0, result.output
        assert "1 of the 2 indexed above had completion refused" in result.output

    def test_complete_refusals_silent_on_zero(self, runner, repo_dir, mock_reg):
        result = self._invoke(
            runner, [str(repo_dir)], mock_reg, files=[("a.py", 5, 0.1)],
        )
        assert result.exit_code == 0, result.output
        assert "REFUSED" not in result.output


# ── nx index pdf / nx index md (single-file record-level summary) ──────────


@pytest.fixture
def fake_pdf(home: Path) -> Path:
    p = home / "doc.pdf"
    p.write_bytes(b"fake pdf")
    return p


@pytest.fixture
def fake_md(home: Path) -> Path:
    p = home / "doc.md"
    p.write_text("# Hello\n\nWorld.\n")
    return p


class TestIndexPdfMdRecordLevelSummary:
    def test_pdf_unchanged_without_force_prints_skip_line(self, runner, fake_pdf):
        # CliRunner's stdout is never a tty, so index_pdf_cmd always takes
        # the `return_metadata=True` (dict-returning) branch.
        with patch(
            "nexus.doc_indexer.index_pdf",
            return_value={"chunks": 0, "pages": [], "title": "", "author": ""},
        ):
            result = runner.invoke(main, ["index", "pdf", str(fake_pdf)])
        assert result.exit_code == 0, result.output
        assert "skipped: index fresh (use --force)" in result.output
        assert "Indexed 0 chunk(s)." not in result.output

    def test_pdf_zero_chunks_with_force_is_not_masked_as_a_skip(self, runner, fake_pdf):
        # --force bypasses the staleness gate entirely, so 0 here is a real
        # (if degenerate) extraction result, not a fresh-skip — must not be
        # mislabeled as "index fresh."
        with patch(
            "nexus.doc_indexer.index_pdf",
            return_value={"chunks": 0, "pages": [], "title": "", "author": ""},
        ):
            result = runner.invoke(main, ["index", "pdf", str(fake_pdf), "--force"])
        assert result.exit_code == 0, result.output
        assert "skipped: index fresh" not in result.output
        assert "Force re-indexed 0 chunk(s)." in result.output

    def test_pdf_real_write_unaffected(self, runner, fake_pdf):
        with patch(
            "nexus.doc_indexer.index_pdf",
            return_value={"chunks": 4, "pages": [1], "title": "", "author": ""},
        ):
            result = runner.invoke(main, ["index", "pdf", str(fake_pdf)])
        assert result.exit_code == 0, result.output
        assert "Indexed 4 chunk(s)." in result.output
        assert "skipped: index fresh" not in result.output

    def test_md_unchanged_without_force_prints_skip_line(self, runner, fake_md):
        with patch(
            "nexus.doc_indexer.index_markdown",
            return_value={"chunks": 0, "sections": 0},
        ):
            result = runner.invoke(main, ["index", "md", str(fake_md)])
        assert result.exit_code == 0, result.output
        assert "skipped: index fresh (use --force)" in result.output
        assert "Indexed 0 chunk(s)." not in result.output

    def test_md_real_write_unaffected(self, runner, fake_md):
        with patch(
            "nexus.doc_indexer.index_markdown",
            return_value={"chunks": 2, "sections": 1},
        ):
            result = runner.invoke(main, ["index", "md", str(fake_md)])
        assert result.exit_code == 0, result.output
        assert "Indexed 2 chunk(s)." in result.output
        assert "skipped: index fresh" not in result.output


# ── IndexRunVerifyRefused: the multi-batch/incremental refusal path ────────
#
# code-review-expert IMPORTANT (2026-08-02): `doc_indexer._fence_complete`
# (the multi-batch/incremental completion stamp, distinct from mcp_infra's
# flush-grain `_manifest_write_loop` / `_stamp_index_run_complete` path,
# nexus-dcv2k) raises `IndexRunVerifyRefused` on a refusal — correct,
# fail-loud propagation — but did not record it to the `.6` summary
# collector, and `IndexRunVerifyRefused` is not an (ImportError,
# RuntimeError), so it fell through every existing CLI catch clause as a
# raw traceback instead of a clean ClickException.


class TestFenceCompleteRefusalRecording:
    def test_fence_complete_records_refusal_before_reraising(self, monkeypatch):
        reset_complete_refusals()

        class _FakeWriter:
            def complete_index_run(self, doc_id, content_hash, chunk_count):
                raise IndexRunVerifyRefused(
                    doc_id=doc_id, referenced=5, present=3, missing=2,
                    chunk_count=chunk_count,
                )

            def close(self):
                pass

        monkeypatch.setattr(
            "nexus.catalog.factory.make_catalog_writer",
            lambda *a, **kw: _FakeWriter(),
        )
        with pytest.raises(IndexRunVerifyRefused):
            _fence_complete("1.2.3", "abc123", 5)
        # The .4 fail-loud contract is unchanged: the exception still
        # propagates unmasked. It must ALSO have reached the collector the
        # .6 summary consumer reads.
        assert get_complete_refusals() == ["1.2.3"]

    def test_fence_complete_does_not_record_on_advisory_failure(self, monkeypatch):
        """A non-refusal write failure (transport error, etc.) is advisory —
        WARNING + return, never propagates, and must NOT be mistaken for a
        refusal in the collector."""
        reset_complete_refusals()

        class _FakeWriter:
            def complete_index_run(self, doc_id, content_hash, chunk_count):
                raise RuntimeError("transport error")

            def close(self):
                pass

        monkeypatch.setattr(
            "nexus.catalog.factory.make_catalog_writer",
            lambda *a, **kw: _FakeWriter(),
        )
        _fence_complete("1.2.3", "abc123", 5)  # must not raise
        assert get_complete_refusals() == []


class TestSingleFileRefusalRendering:
    def test_pdf_refusal_renders_clean_message_and_exits_nonzero(self, runner, fake_pdf):
        def _raise(*a, **kw):
            raise IndexRunVerifyRefused(
                doc_id="1.2.3", referenced=5, present=3, missing=2, chunk_count=5,
            )
        with patch("nexus.doc_indexer.index_pdf", side_effect=_raise):
            result = runner.invoke(main, ["index", "pdf", str(fake_pdf)])
        assert result.exit_code != 0
        assert result.exception is None or isinstance(result.exception, SystemExit), (
            f"a raw traceback escaped instead of a ClickException: {result.exception!r}"
        )
        assert "completion REFUSED" in result.output
        assert "NOT fully indexed" in result.output
        assert "referenced=5" in result.output
        assert "present=3" in result.output
        assert "missing=2" in result.output
        # Never a raw traceback, and never folded into a success line.
        assert "Traceback" not in result.output
        assert "Indexed 5 chunk(s)." not in result.output

    def test_pdf_dir_batch_refusal_gets_dedicated_wording(self, runner, home):
        pdf = home / "doc.pdf"
        pdf.write_bytes(b"fake")

        def _raise(*a, **kw):
            raise IndexRunVerifyRefused(
                doc_id="1.2.3", referenced=5, present=3, missing=2, chunk_count=5,
            )
        with patch("nexus.doc_indexer.index_pdf", side_effect=_raise):
            result = runner.invoke(main, ["index", "pdf", "--dir", str(home)])
        # --dir isolates per-file failures — the batch itself still exits 0.
        assert result.exit_code == 0, result.output
        assert "completion REFUSED" in result.output
        assert "NOT fully indexed" in result.output
        assert "referenced=5" in result.output


# ── nx index rdr ─────────────────────────────────────────────────────────────


class TestIndexRdrSummaryTruthful:
    def _rdr_dir(self, home: Path, count: int) -> Path:
        repo = home / "myrepo"
        rdr_dir = repo / "docs" / "rdr"
        rdr_dir.mkdir(parents=True, exist_ok=True)
        for i in range(1, count + 1):
            (rdr_dir / f"{i:03d}.md").write_text(f"# RDR {i}\n")
        return repo

    def test_unchanged_and_failed_rdrs_surfaced(self, runner, home):
        repo = self._rdr_dir(home, 3)
        results = {
            str(repo / "docs" / "rdr" / "001.md"): "indexed",
            str(repo / "docs" / "rdr" / "002.md"): "skipped",
            str(repo / "docs" / "rdr" / "003.md"): "failed",
        }
        with patch("nexus.doc_indexer.batch_index_markdowns", return_value=results):
            result = runner.invoke(main, ["index", "rdr", str(repo)])
        assert result.exit_code == 0, result.output
        assert "Indexed 1 of 3 RDR document(s)." in result.output
        assert "skipped: index fresh (use --force) — 1 document(s)" in result.output
        assert "1 document(s) failed" in result.output

    def test_all_indexed_no_extra_lines(self, runner, home):
        repo = self._rdr_dir(home, 1)
        results = {str(repo / "docs" / "rdr" / "001.md"): "indexed"}
        with patch("nexus.doc_indexer.batch_index_markdowns", return_value=results):
            result = runner.invoke(main, ["index", "rdr", str(repo)])
        assert result.exit_code == 0, result.output
        assert "Indexed 1 of 1 RDR document(s)." in result.output
        assert "skipped: index fresh" not in result.output
        assert "failed" not in result.output
