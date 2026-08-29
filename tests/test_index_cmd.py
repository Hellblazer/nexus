# SPDX-License-Identifier: AGPL-3.0-or-later
import contextlib
import time
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest
from click.testing import CliRunner

from nexus.cli import main

# RDR-109 Phase 2: this file asserts cloud-mode canonical behavior
# (voyage-* embedder names, canonical-set defaults). The cloud_mode
# fixture sets credentials and forces ``is_local_mode()`` to False so
# the assertions hold regardless of the host environment.
pytestmark = pytest.mark.usefixtures("cloud_mode")

PDF_RESULT = {"chunks": 3, "pages": [], "title": "", "author": ""}
MD_RESULT = {"chunks": 2, "sections": 0}

@pytest.fixture
def runner() -> CliRunner:
    return CliRunner()


@pytest.fixture
def home(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    monkeypatch.setenv("HOME", str(tmp_path))
    return tmp_path


@pytest.fixture
def repo_dir(home: Path) -> Path:
    d = home / "myrepo"
    d.mkdir()
    (d / ".git").mkdir()
    return d


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


@pytest.fixture
def mock_reg():
    reg = MagicMock()
    reg.get.return_value = {"collection": "code__myrepo"}
    return reg


def _invoke_repo(runner, args, mock_reg, index_side_effect=None, index_return=None):
    """Run `nx index repo ...` with registry + indexer mocked."""
    kw = {}
    if index_side_effect:
        kw["side_effect"] = index_side_effect
    else:
        kw["return_value"] = index_return or {}
    with patch("nexus.commands.index._registry", return_value=mock_reg):
        with patch("nexus.indexer.index_repository", **kw) as mock_idx:
            result = runner.invoke(main, ["index", "repo"] + args)
    return result, mock_idx


# ── nx index repo basic ─────────────────────────────────────────────────────

def test_index_repo_registers_and_indexes(runner, repo_dir, home):
    reg = MagicMock()
    reg.get.return_value = None
    result, mock_idx = _invoke_repo(runner, [str(repo_dir)], reg)
    assert result.exit_code == 0
    reg.add.assert_called_once()
    mock_idx.assert_called_once()
    assert "Registered" in result.output
    assert "Done" in result.output


def test_index_repo_pdf_quality_gate_failures_exit_nonzero(runner, repo_dir, mock_reg):
    """nexus-wi1uv round-2 (code-review-expert + substantive-critic
    Critical): PDF(s) that failed the post-extraction quality gate are
    contained per-file inside index_repository (indexer._contain_
    extraction_quality_gate) -- the run itself must still complete and
    report a non-zero exit, naming the count + remedy, rather than a
    silent rc=0 for a run that quietly skipped documents."""
    result, mock_idx = _invoke_repo(
        runner, [str(repo_dir)], mock_reg,
        index_return={"files_changed": 3, "pdf_quality_gate_failed": 2},
    )
    assert result.exit_code != 0, result.output
    assert "2 PDF(s) failed the post-extraction quality gate" in result.output
    assert "--allow-degraded-extraction" in result.output
    # "Done." (the rest of the run, incl. post-processing) still printed --
    # the exception fires LAST, after useful work completes.
    assert "Done." in result.output


def test_index_repo_no_pdf_quality_gate_failures_exit_zero(runner, repo_dir, mock_reg):
    """Regression: the new stats key must not itself flip a clean run non-zero."""
    result, mock_idx = _invoke_repo(
        runner, [str(repo_dir)], mock_reg,
        index_return={"files_changed": 3, "pdf_quality_gate_failed": 0},
    )
    assert result.exit_code == 0, result.output


def test_index_repo_pdf_quality_gate_key_absent_exit_zero(runner, repo_dir, mock_reg):
    """Backward compat: an older/mocked index_repository return dict with
    no pdf_quality_gate_failed key at all must not be treated as a
    failure (.get default of 0)."""
    result, mock_idx = _invoke_repo(
        runner, [str(repo_dir)], mock_reg,
        index_return={"files_changed": 3},
    )
    assert result.exit_code == 0, result.output


def test_index_repo_skipped_unextractable_files_reported_on_stderr(runner, repo_dir, mock_reg):
    """nexus-deyd5 round 2 (code-review finding): the skip-count note must
    land on STDERR, alongside the WARNING/ERROR log line(s) it points at
    ('see the WARNING/ERROR log line(s) above') -- those are stderr-only
    (mode="cli" logging, logging_setup.py). A plain stdout click.echo would
    let `nx index repo path/ > report.txt` capture a note referencing
    detail that never lands in the file. This is informational, not fatal
    -- exit_code stays 0."""
    result, mock_idx = _invoke_repo(
        runner, [str(repo_dir)], mock_reg,
        index_return={"files_changed": 3159, "skipped_unextractable_files": 1},
    )
    assert result.exit_code == 0, result.output
    assert "1 file(s) could not be extracted and were skipped" in result.stderr
    assert "1 file(s) could not be extracted and were skipped" not in result.stdout


def test_index_repo_systemic_extraction_failure_exits_nonzero_with_clean_message(
    runner, repo_dir, mock_reg,
):
    """nexus-deyd5 round 3 (coordinator directive, closing the round-2
    HIGH finding): a run-level systemic-skip breach must exit non-zero
    with a CLEAN click.ClickException message -- never an uncaught
    traceback -- naming the numbers and the most likely legitimate cause
    (scanned PDFs without OCR). This is a run-LEVEL decision distinct
    from pdf_quality_gate_failed; both can be present simultaneously
    without interfering."""
    result, mock_idx = _invoke_repo(
        runner, [str(repo_dir)], mock_reg,
        index_return={
            "files_changed": 12,
            "skipped_unextractable_files": 13,
            "files_attempted_total": 25,
            "systemic_extraction_failure": True,
        },
    )
    assert result.exit_code != 0, result.output
    # No standalone "not a traceback" assertion here (round 3 code review):
    # CliRunner(catch_exceptions=True) leaves result.output EMPTY for BOTH
    # a raw uncaught exception and a clean click.ClickException, so a
    # `"Traceback" not in result.output` check cannot structurally
    # distinguish the two -- it would pass either way, proving nothing.
    # The message-content assertions below ARE the real proof: they
    # require actual text to have reached result.output, which only a
    # click.ClickException (caught and formatted by Click's own error
    # handling) produces -- a raw unhandled exception leaves this content
    # absent and these assertions would fail.
    assert "skipped 13 of 25 files" in result.output
    assert "52%" in result.output
    assert "extraction may be broken" in result.output
    assert "mineru" in result.output
    # "Done." (the rest of the run, incl. drain/RDR-loop/post-processing)
    # still printed -- the exception fires LAST, after useful work
    # completes and is committed; nothing successful is discarded.
    assert "Done." in result.output


def test_index_repo_no_systemic_extraction_failure_exit_zero(runner, repo_dir, mock_reg):
    """Regression: the new stats key must not itself flip a clean run
    non-zero when False -- and a nonzero skip count alone (with the flag
    False) stays informational, matching the coordinator's "one bad
    fixture among many still exits 0" requirement at the CLI boundary."""
    result, mock_idx = _invoke_repo(
        runner, [str(repo_dir)], mock_reg,
        index_return={
            "files_changed": 3159,
            "skipped_unextractable_files": 1,
            "files_attempted_total": 3160,
            "systemic_extraction_failure": False,
        },
    )
    assert result.exit_code == 0, result.output


def test_index_repo_systemic_extraction_failure_key_absent_exit_zero(runner, repo_dir, mock_reg):
    """Backward compat: an older/mocked index_repository return dict with
    no systemic_extraction_failure key at all must not be treated as a
    failure (.get default of False)."""
    result, mock_idx = _invoke_repo(
        runner, [str(repo_dir)], mock_reg,
        index_return={"files_changed": 3},
    )
    assert result.exit_code == 0, result.output


def test_index_repo_no_skipped_unextractable_files_no_note(runner, repo_dir, mock_reg):
    """Regression: zero (or an absent key, back-compat) prints nothing."""
    result, mock_idx = _invoke_repo(
        runner, [str(repo_dir)], mock_reg,
        index_return={"files_changed": 3},
    )
    assert result.exit_code == 0, result.output
    assert "could not be extracted and were skipped" not in result.output


def test_index_repo_idempotent_when_already_registered(runner, repo_dir, mock_reg):
    result, mock_idx = _invoke_repo(runner, [str(repo_dir)], mock_reg)
    assert result.exit_code == 0
    mock_reg.add.assert_not_called()
    mock_idx.assert_called_once()
    assert "Registered" not in result.output


def test_index_repo_invalid_path(runner, home):
    result = runner.invoke(main, ["index", "repo", str(home / "nonexistent")])
    assert result.exit_code != 0


def test_index_repo_refuses_directory_without_git(runner, home):
    """Indexing a directory with no ``.git`` (e.g. the parent of many repos,
    ``~/git`` instead of ``~/git/myrepo``) must refuse loudly instead of
    silently registering an owner spanning unrelated content. Real incident
    2026-07-10: `nx index repo ~/git` created a bogus owner named "git"
    sweeping in an unrelated project's files."""
    not_a_repo = home / "git"
    not_a_repo.mkdir()
    (not_a_repo / "some_unrelated_project").mkdir()
    result = runner.invoke(main, ["index", "repo", str(not_a_repo)])
    assert result.exit_code != 0
    assert ".git" in result.output


def test_index_repo_accepts_git_worktree(runner, home):
    """A git worktree's ``.git`` is a FILE (containing ``gitdir: ...``), not
    a directory — the guard must accept that shape too, not just a real
    ``.git/`` directory."""
    worktree = home / "myrepo-worktree"
    worktree.mkdir()
    (worktree / ".git").write_text("gitdir: /somewhere/else/.git/worktrees/myrepo-worktree\n")
    reg = MagicMock()
    reg.get.return_value = None
    result, mock_idx = _invoke_repo(runner, [str(worktree)], reg)
    assert result.exit_code == 0, result.output
    mock_idx.assert_called_once()


# ── nx index pdf / md basic ──────────────────────────────────────────────────

def test_index_pdf_command_indexes_file(runner, fake_pdf):
    with patch("nexus.doc_indexer.index_pdf", return_value=PDF_RESULT) as m:
        result = runner.invoke(main, ["index", "pdf", str(fake_pdf)])
    assert result.exit_code == 0, result.output
    m.assert_called_once()
    assert "3" in result.output


def test_index_md_command_indexes_file(runner, fake_md):
    with patch("nexus.doc_indexer.index_markdown", return_value=MD_RESULT) as m:
        result = runner.invoke(main, ["index", "md", str(fake_md)])
    assert result.exit_code == 0, result.output
    m.assert_called_once()
    assert "2" in result.output


@pytest.mark.parametrize("subcmd,ext", [("pdf", "pdf"), ("md", "md")])
def test_index_nonexistent_path_fails(runner, home, subcmd, ext):
    result = runner.invoke(main, ["index", subcmd, str(home / f"missing.{ext}")])
    assert result.exit_code != 0


@pytest.mark.parametrize("subcmd,fixture", [
    ("pdf", "fake_pdf"),
    ("md", "fake_md"),
])
def test_index_credentials_missing_exits_nonzero_with_message(
    runner, request, subcmd, fixture, monkeypatch,
):
    """GH #336: when voyage_api_key / chroma_api_key are unset, the
    indexer raises ``CredentialsMissingError`` instead of silently
    returning 0. The CLI handler converts it to a ``ClickException``
    so the operator sees a clear message + non-zero exit.
    """
    from nexus.errors import CredentialsMissingError

    fixture_path = request.getfixturevalue(fixture)
    fn_name = "index_pdf" if subcmd == "pdf" else "index_markdown"

    # Make the indexer raise as if credentials were missing.
    def _raise(*args, **kwargs):
        raise CredentialsMissingError(
            "cannot index without voyage_api_key, chroma_api_key. "
            "Set via 'nx config set <key> <value>' ..."
        )

    with patch(f"nexus.doc_indexer.{fn_name}", side_effect=_raise):
        result = runner.invoke(main, ["index", subcmd, str(fixture_path)])

    assert result.exit_code != 0, result.output
    # Click's ClickException prints the message to stdout in CliRunner.
    assert "voyage_api_key" in result.output
    assert "Set via" in result.output or "config set" in result.output


def test_index_md_command_empty_file_exits_nonzero_without_registering(
    runner, home,
):
    """nexus-rqsh1 round 2: ``nx index md`` on a zero-byte file must fail
    loud (non-zero exit, clean message naming the file) and must NEVER
    register a catalog document -- the end-to-end wiring through the
    REAL ``index_markdown`` guard (only ``_register_or_lookup_doc_id``
    is doubled, to assert non-call without needing a live catalog)."""
    empty = home / "empty.md"
    empty.write_bytes(b"")
    with patch("nexus.doc_indexer._register_or_lookup_doc_id") as mock_register:
        result = runner.invoke(main, ["index", "md", str(empty)])
    assert result.exit_code != 0, result.output
    assert str(empty) in result.output
    assert "Traceback (most recent call last)" not in result.output
    mock_register.assert_not_called()


def test_index_pdf_command_empty_file_exits_nonzero_without_registering(
    runner, home,
):
    """nexus-1sd0f (round 3): ``nx index pdf`` on a zero-byte file must
    fail loud (non-zero exit, clean message naming the file) and must
    NEVER register a catalog document -- the end-to-end wiring through
    the REAL ``index_pdf`` guard (only ``_register_or_lookup_doc_id``
    is doubled, to assert non-call without needing a live catalog),
    mirroring ``test_index_md_command_empty_file_exits_nonzero_without_
    registering``."""
    empty = home / "empty.pdf"
    empty.write_bytes(b"")
    with patch("nexus.doc_indexer._register_or_lookup_doc_id") as mock_register:
        result = runner.invoke(main, ["index", "pdf", str(empty)])
    assert result.exit_code != 0, result.output
    assert str(empty) in result.output
    assert "Traceback (most recent call last)" not in result.output
    mock_register.assert_not_called()


def test_index_pdf_indexing_error_exits_nonzero_with_clean_message(runner, fake_pdf):
    """nexus-w6wp0 review round (code-review-expert Critical, 2026-08-05):
    index_pdf's streaming return_metadata path can raise IndexingError
    (pipeline reported chunks written but the metadata query found none)
    -- the CLI wrapper must translate it to a ClickException (clean
    message, non-zero exit) rather than let it propagate as a raw
    traceback, same nexus-2fyb convention as CredentialsMissingError etc.
    above.
    """
    from nexus.errors import IndexingError

    def _raise(*args, **kwargs):
        raise IndexingError(
            "index_pdf: pipeline reported 5 chunk(s) written for x.pdf "
            "(content_hash=abc123) but the metadata query found none -- "
            "cannot determine pages/title/author"
        )

    with patch("nexus.doc_indexer.index_pdf", side_effect=_raise):
        result = runner.invoke(main, ["index", "pdf", str(fake_pdf)])

    assert result.exit_code != 0, result.output
    assert "pipeline reported 5 chunk(s) written" in result.output
    # No raw traceback leaked to the CliRunner output -- ClickException's
    # clean rendering, not an unhandled-exception dump.
    assert "Traceback (most recent call last)" not in result.output


def test_index_pdf_conflict_running_exits_nonzero_with_remedy(runner, fake_pdf):
    """nexus-lcmbp: a retry against a stranded 'running' pipeline row must
    exit non-zero and print the error + remedy — never rc=0 with 0 chunks.
    PipelineConflictRunning subclasses RuntimeError, so it is caught by the
    same except clause CredentialsMissingError etc. route through
    (commands/index.py's `except (ImportError, RuntimeError)`), with no
    extra CLI wiring needed."""
    from nexus.db.http_pipeline_client import PipelineConflictRunning

    def _raise(*args, **kwargs):
        raise PipelineConflictRunning(
            "pipeline for content_hash=abc123 is already running",
            content_hash="abc123",
            started_at="2026-08-01T12:00:00+00:00",
            heartbeat_age_seconds=42,
            stale_threshold_seconds=300,
            remedy="wait for the resume window (retry after the heartbeat "
                   "exceeds the stale threshold) or inspect the pipeline "
                   "row via GET /v1/pipeline/state (engine route; requires "
                   "service auth)",
        )

    with patch("nexus.doc_indexer.index_pdf", side_effect=_raise):
        result = runner.invoke(main, ["index", "pdf", str(fake_pdf)])

    assert result.exit_code != 0, result.output
    assert "already running" in result.output
    assert "resume window" in result.output


# ── --frecency-only flag ─────────────────────────────────────────────────────

@pytest.mark.parametrize("flag,expected", [
    (["--frecency-only"], True),
    ([], False),
])
def test_index_repo_frecency_only(runner, repo_dir, mock_reg, flag, expected):
    result, mock_idx = _invoke_repo(runner, [str(repo_dir)] + flag, mock_reg)
    assert result.exit_code == 0, result.output
    _, kw = mock_idx.call_args
    assert kw.get("frecency_only") is expected
    if expected:
        assert "frecency" in result.output.lower()


# ── --force flag ─────────────────────────────────────────────────────────────

def test_index_repo_force_flag(runner, repo_dir, mock_reg):
    result, mock_idx = _invoke_repo(runner, [str(repo_dir), "--force"], mock_reg)
    assert result.exit_code == 0, result.output
    _, kw = mock_idx.call_args
    assert kw.get("force") is True
    assert "Force-indexing" in result.output


def test_index_repo_force_frecency_mutual_exclusion(runner, repo_dir, mock_reg):
    with patch("nexus.commands.index._registry", return_value=mock_reg):
        result = runner.invoke(
            main, ["index", "repo", str(repo_dir), "--force", "--frecency-only"]
        )
    assert result.exit_code != 0
    assert "mutually exclusive" in result.output.lower()


def test_index_pdf_force_flag(runner, fake_pdf):
    with patch("nexus.doc_indexer.index_pdf", return_value={**PDF_RESULT, "chunks": 5}) as m:
        result = runner.invoke(main, ["index", "pdf", str(fake_pdf), "--force"])
    assert result.exit_code == 0, result.output
    _, kw = m.call_args
    assert kw.get("force") is True


def test_index_pdf_force_dry_run_mutual_exclusion(runner, fake_pdf):
    result = runner.invoke(main, ["index", "pdf", str(fake_pdf), "--force", "--dry-run"])
    assert result.exit_code != 0
    assert "mutually exclusive" in result.output.lower()


def test_index_pdf_dry_run_threads_flag_to_index_pdf(runner, fake_pdf):
    """nexus-uxg4u: --dry-run must reach doc_indexer.index_pdf as an
    explicit dry_run=True kwarg, not be inferred from the dry-run
    branch's ephemeral T3 client or no-op embedder -- the phantom-
    registration bug this flag closes is gated on the flag itself."""
    with patch("nexus.doc_indexer.index_pdf", return_value=3) as m:
        result = runner.invoke(main, ["index", "pdf", str(fake_pdf), "--dry-run"])
    assert result.exit_code == 0, result.output
    _, kw = m.call_args
    assert kw.get("dry_run") is True


def test_index_pdf_allow_degraded_extraction_flag_defaults_false(runner, fake_pdf):
    """nexus-wi1uv: the override must be opt-in, not the default posture."""
    with patch("nexus.doc_indexer.index_pdf", return_value=PDF_RESULT) as m:
        result = runner.invoke(main, ["index", "pdf", str(fake_pdf)])
    assert result.exit_code == 0, result.output
    _, kw = m.call_args
    assert kw.get("allow_degraded_extraction") is False


def test_index_pdf_allow_degraded_extraction_flag_threads_through(runner, fake_pdf):
    with patch("nexus.doc_indexer.index_pdf", return_value=PDF_RESULT) as m:
        result = runner.invoke(
            main, ["index", "pdf", str(fake_pdf), "--allow-degraded-extraction"],
        )
    assert result.exit_code == 0, result.output
    _, kw = m.call_args
    assert kw.get("allow_degraded_extraction") is True


def test_index_pdf_quality_gate_failure_exits_nonzero_with_remedy(runner, fake_pdf):
    """nexus-wi1uv: a post-extraction quality-gate failure (the space-
    stripped-garbage signature) must surface as a clean ClickException —
    non-zero exit, no raw traceback — naming both remedies (retry with a
    different extractor, or --allow-degraded-extraction)."""
    from nexus.errors import ExtractionQualityError

    def _raise(*args, **kwargs):
        raise ExtractionQualityError(
            "PDF doc.pdf failed the post-extraction quality gate "
            "(extraction_method=docling): whitespace_ratio=0.0114 < floor "
            "0.05. Remedy: retry with `--extractor mineru`, or rerun with "
            "`--allow-degraded-extraction` to index it anyway."
        )

    with patch("nexus.doc_indexer.index_pdf", side_effect=_raise):
        result = runner.invoke(main, ["index", "pdf", str(fake_pdf)])

    assert result.exit_code != 0, result.output
    assert "quality gate" in result.output
    assert "--allow-degraded-extraction" in result.output
    assert "Traceback (most recent call last)" not in result.output


def test_index_pdf_monitor_quality_gate_failure_exits_nonzero(runner, fake_pdf):
    """Same gate failure on the --monitor/non-tty branch (a separate
    index_pdf call site with its own except clause)."""
    from nexus.errors import ExtractionQualityError

    def _raise(*args, **kwargs):
        raise ExtractionQualityError("PDF doc.pdf failed the post-extraction quality gate: boom")

    with patch("nexus.doc_indexer.index_pdf", side_effect=_raise):
        result = runner.invoke(main, ["index", "pdf", str(fake_pdf), "--monitor"])

    assert result.exit_code != 0, result.output
    assert "quality gate" in result.output
    assert "Traceback (most recent call last)" not in result.output


def test_index_md_force_flag(runner, fake_md):
    with patch("nexus.doc_indexer.index_markdown", return_value=MD_RESULT) as m:
        result = runner.invoke(main, ["index", "md", str(fake_md), "--force"])
    assert result.exit_code == 0, result.output
    _, kw = m.call_args
    assert kw.get("force") is True


# ── --monitor flag ───────────────────────────────────────────────────────────

@pytest.mark.parametrize("subcmd,extra_args", [
    ("repo", []),
    ("rdr", []),
    ("pdf", []),
    ("md", []),
])
def test_monitor_flag_accepted(runner, home, subcmd, extra_args):
    if subcmd == "repo":
        target = home / "myrepo"
        target.mkdir()
        (target / ".git").mkdir()
    elif subcmd in ("pdf", "md"):
        target = home / f"doc.{subcmd}"
        target.write_bytes(b"fake")
    else:
        target = home / "myrepo"
        rdr_dir = target / "docs" / "rdr"
        rdr_dir.mkdir(parents=True)
        (rdr_dir / "001.md").write_text("# RDR\n")

    mock_target = {
        "repo": "nexus.indexer.index_repository",
        "rdr": "nexus.doc_indexer.batch_index_markdowns",
        "pdf": "nexus.doc_indexer.index_pdf",
        "md": "nexus.doc_indexer.index_markdown",
    }[subcmd]
    mock_rv = {
        "repo": {},
        "rdr": {},
        "pdf": {"chunks": 0, "pages": [], "title": "", "author": ""},
        "md": {"chunks": 0, "sections": 0},
    }[subcmd]

    patches = [patch(mock_target, return_value=mock_rv)]
    if subcmd == "repo":
        reg = MagicMock()
        reg.get.return_value = {"collection": "code__x"}
        patches.append(patch("nexus.commands.index._registry", return_value=reg))

    with contextlib.ExitStack() as stack:
        for p in patches:
            stack.enter_context(p)
        result = runner.invoke(main, ["index", subcmd, str(target), "--monitor"] + extra_args)
    assert result.exit_code == 0, f"{subcmd}: {result.output}"


# ── repo monitor behaviour ──────────────────────────────────────────────────

def test_repo_callbacks_always_passed(runner, repo_dir, mock_reg):
    result, mock_idx = _invoke_repo(runner, [str(repo_dir)], mock_reg)
    assert result.exit_code == 0, result.output
    _, kw = mock_idx.call_args
    assert callable(kw.get("on_start"))
    assert callable(kw.get("on_file"))


def _make_fake_index(files):
    """Build a fake index_repository that calls on_start/on_file with given (path, chunks, time) tuples."""
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


@pytest.mark.parametrize("files,expected_in_output", [
    ([("a.py", 5, 0.1), ("b.py", 0, 0.05)], ["[1/2]", "[2/2]"]),
    ([("skip.py", 0, 0.02)], ["skipped"]),
    ([("code.py", 7, 0.3)], ["7 chunks"]),
])
def test_repo_monitor_output(runner, repo_dir, mock_reg, files, expected_in_output):
    result, _ = _invoke_repo(
        runner,
        [str(repo_dir), "--monitor"],
        mock_reg,
        index_side_effect=_make_fake_index(files),
    )
    assert result.exit_code == 0, result.output
    for text in expected_in_output:
        assert text in result.output


def test_repo_monitor_nontty_no_cr(runner, repo_dir, mock_reg):
    result, _ = _invoke_repo(
        runner,
        [str(repo_dir), "--monitor"],
        mock_reg,
        index_side_effect=_make_fake_index([("f.py", 3, 0.1)]),
    )
    assert result.exit_code == 0, result.output
    assert "\r" not in result.output


# ── GH #1371: manifest-write-failure summary ────────────────────────────────

def test_manifest_write_failure_summary_silent_on_no_failures(runner, repo_dir, mock_reg):
    result, _ = _invoke_repo(runner, [str(repo_dir)], mock_reg)
    assert result.exit_code == 0, result.output
    assert "catalog manifest write failed" not in result.output


def test_manifest_write_failure_summary_surfaces_failures(runner, repo_dir, mock_reg, monkeypatch):
    monkeypatch.setattr(
        "nexus.mcp_infra.get_manifest_write_failures",
        lambda: ["1.9.0", "1.9.1"],
    )
    result, _ = _invoke_repo(runner, [str(repo_dir)], mock_reg)
    # nexus-tp8yk D2b: a manifest write failure now fails the run's exit
    # code — was WARNING-only (rc=0) before this bead.
    assert result.exit_code != 0, result.output
    assert "WARNING: catalog manifest write failed for 2 document(s)" in result.output
    assert "nx catalog reconcile" in result.output
    assert "nx catalog show" in result.output


# ── nexus-u8n4r: ephemeral-path registration-skip summary ───────────────────

def test_ephemeral_registration_skip_collector_round_trips_reason():
    """nexus-u8n4r review fix (Significant): the collector must carry
    ``reason`` through record -> get, not just ``path``/``owner``."""
    from nexus.mcp_infra import (
        _record_ephemeral_registration_skip,
        get_ephemeral_registration_skips,
        reset_ephemeral_registration_skips,
    )

    reset_ephemeral_registration_skips()
    try:
        _record_ephemeral_registration_skip(
            "/r/.claude/worktrees/a/x.py", "1.1", reason="worktree_or_tempdir",
        )
        _record_ephemeral_registration_skip(
            "/r/draft.md", "1.1", reason="worktree_unique_no_main_mirror",
        )
        skips = get_ephemeral_registration_skips()
        assert len(skips) == 2
        assert skips[0]["reason"] == "worktree_or_tempdir"
        assert skips[1]["reason"] == "worktree_unique_no_main_mirror"
    finally:
        reset_ephemeral_registration_skips()


def test_ephemeral_skip_summary_silent_on_no_skips(runner, repo_dir, mock_reg):
    result, _ = _invoke_repo(runner, [str(repo_dir)], mock_reg)
    assert result.exit_code == 0, result.output
    assert "nexus-u8n4r" not in result.output


def test_ephemeral_skip_summary_surfaces_per_reason_breakdown(
    runner, repo_dir, mock_reg, monkeypatch,
):
    """nexus-u8n4r review fix (Significant): the summary must break down
    by reason, not just print an aggregate count — the two reasons are
    operationally different (structural worktree/temp-marker debris vs.
    an uncommitted draft that never made it into the index)."""
    monkeypatch.setattr(
        "nexus.mcp_infra.get_ephemeral_registration_skips",
        lambda: [
            {"path": "/r/.claude/worktrees/a/x.py", "owner": "1.1", "reason": "worktree_or_tempdir"},
            {"path": "/r/.claude/worktrees/a/y.py", "owner": "1.1", "reason": "worktree_or_tempdir"},
            {"path": "/r/.claude/worktrees/a/z.md", "owner": "1.1", "reason": "worktree_or_tempdir"},
            {"path": "/r/draft1.md", "owner": "1.1", "reason": "worktree_unique_no_main_mirror"},
            {"path": "/r/draft2.md", "owner": "1.1", "reason": "worktree_unique_no_main_mirror"},
        ],
    )
    result, _ = _invoke_repo(runner, [str(repo_dir)], mock_reg)
    assert result.exit_code == 0, result.output
    assert (
        "5 file(s) skipped — not registered (nexus-u8n4r): "
        "3 worktree/temp-marker, 2 worktree-unique (no main mirror)"
    ) in result.output


def test_ephemeral_skip_summary_labels_unknown_reason_verbatim(
    runner, repo_dir, mock_reg, monkeypatch,
):
    """An unrecognized reason string (future-proofing) still renders —
    verbatim, not swallowed — rather than crashing the summary."""
    monkeypatch.setattr(
        "nexus.mcp_infra.get_ephemeral_registration_skips",
        lambda: [{"path": "/r/x.py", "owner": "1.1", "reason": "some_new_reason"}],
    )
    result, _ = _invoke_repo(runner, [str(repo_dir)], mock_reg)
    assert result.exit_code == 0, result.output
    assert "1 file(s) skipped — not registered (nexus-u8n4r): 1 some_new_reason" in result.output


# ── RDR monitor behaviour ───────────────────────────────────────────────────

def _make_rdr_dir(home: Path, count: int = 1) -> Path:
    repo = home / "myrepo"
    rdr_dir = repo / "docs" / "rdr"
    rdr_dir.mkdir(parents=True, exist_ok=True)
    for i in range(1, count + 1):
        (rdr_dir / f"{i:03d}.md").write_text(f"# RDR {i}\n")
    return repo


def test_rdr_monitor_on_file_passed(runner, home):
    repo = _make_rdr_dir(home)
    with patch("nexus.doc_indexer.batch_index_markdowns", return_value={}) as m:
        result = runner.invoke(main, ["index", "rdr", str(repo), "--monitor"])
    assert result.exit_code == 0, result.output
    _, kw = m.call_args
    assert callable(kw.get("on_file"))


def test_rdr_monitor_bar_total(runner, home):
    repo = _make_rdr_dir(home, count=3)
    with patch("nexus.doc_indexer.batch_index_markdowns", return_value={}):
        with patch("nexus.commands.index.tqdm") as mock_tqdm:
            mock_tqdm.return_value = MagicMock()
            result = runner.invoke(main, ["index", "rdr", str(repo), "--monitor"])
    assert result.exit_code == 0, result.output
    mock_tqdm.assert_called_once()
    call_args = mock_tqdm.call_args
    total = call_args[1].get("total") if call_args[1] else call_args[0][0] if call_args[0] else None
    assert total == 3, f"expected total=3, got {total}"


# ── pdf/md monitor metadata ─────────────────────────────────────────────────

def test_pdf_monitor_return_metadata(runner, fake_pdf):
    rv = {"chunks": 3, "pages": [1, 2, 3], "title": "Test", "author": "Author"}
    with patch("nexus.doc_indexer.index_pdf", return_value=rv) as m:
        result = runner.invoke(main, ["index", "pdf", str(fake_pdf), "--monitor"])
    assert result.exit_code == 0, result.output
    _, kw = m.call_args
    assert kw.get("return_metadata") is True
    assert "Chunks: 3" in result.output


def test_md_monitor_return_metadata(runner, fake_md):
    rv = {"chunks": 2, "sections": 1}
    with patch("nexus.doc_indexer.index_markdown", return_value=rv) as m:
        result = runner.invoke(main, ["index", "md", str(fake_md), "--monitor"])
    assert result.exit_code == 0, result.output
    _, kw = m.call_args
    assert kw.get("return_metadata") is True
    assert "Chunks: 2" in result.output
    assert "Sections: 1" in result.output


# ── --collection normalization ───────────────────────────────────────────────

@pytest.mark.parametrize("flag_val,expected", [
    # RDR-103 Phase 5: ``t3_collection_name`` auto-promotes
    # 1-segment / 2-segment user input to a conformant 4-segment name
    # so the strict-naming guard at ``T3Database.get_or_create_collection``
    # passes for fresh writes.
    ("knowledge", "knowledge__knowledge__voyage-context-3__v1"),
    ("knowledge__delos", "knowledge__delos__voyage-context-3__v1"),
])
def test_pdf_collection_flag_normalization(runner, fake_pdf, flag_val, expected):
    # nexus-hmxi: ``nx index pdf --collection`` does NOT probe T3 for
    # legacy grandfathering (see ``pdf_cmd`` in ``commands/index.py``);
    # the auto-promoted conformant shape is the bulk-index target.
    rv = {"chunks": 5, "pages": [1], "title": "T", "author": "A"}
    with patch("nexus.doc_indexer.index_pdf", return_value=rv) as m:
        result = runner.invoke(main, ["index", "pdf", str(fake_pdf), "--collection", flag_val])
    assert result.exit_code == 0, result.output
    _, kw = m.call_args
    assert kw["collection_name"] == expected


# ── nx index md --collection flag (GH #981) ──────────────────────────────────

@pytest.mark.parametrize("flag_val,expected", [
    # Bare name: auto-normalized to knowledge__<name>__voyage-context-3__v1
    ("x", "knowledge__x__voyage-context-3__v1"),
    # 2-segment legacy: auto-promoted to conformant 4-segment
    ("knowledge__mydocs", "knowledge__mydocs__voyage-context-3__v1"),
    # Already-conformant 4-segment: passed through unchanged
    ("knowledge__mydocs__voyage-context-3__v1", "knowledge__mydocs__voyage-context-3__v1"),
])
def test_md_collection_flag_normalization(runner, fake_md, flag_val, expected):
    """--collection on nx index md routes to knowledge__ (GH #981, fix #1)."""
    with patch("nexus.doc_indexer.index_markdown", return_value=MD_RESULT) as m:
        result = runner.invoke(main, ["index", "md", str(fake_md), "--collection", flag_val])
    assert result.exit_code == 0, result.output
    _, kw = m.call_args
    assert kw["collection_name"] == expected


def test_md_collection_flag_absent_uses_corpus_default(runner, fake_md):
    """Without --collection, docs__<corpus> default is preserved (no regression)."""
    with patch("nexus.doc_indexer.index_markdown", return_value=MD_RESULT) as m:
        result = runner.invoke(main, ["index", "md", str(fake_md), "--corpus", "myproject"])
    assert result.exit_code == 0, result.output
    _, kw = m.call_args
    # collection_name must be None so index_markdown derives docs__myproject itself
    assert kw.get("collection_name") is None
    assert kw.get("corpus") == "myproject"


def test_md_collection_flag_produces_knowledge_prefix(runner, fake_md):
    """Collection produced by --collection starts with knowledge__ (aspect-eligible)."""
    with patch("nexus.doc_indexer.index_markdown", return_value=MD_RESULT) as m:
        result = runner.invoke(main, ["index", "md", str(fake_md), "--collection", "mynotes"])
    assert result.exit_code == 0, result.output
    _, kw = m.call_args
    assert kw["collection_name"].startswith("knowledge__")


def test_md_collection_knowledge_target_emits_prose_extractor_warning(runner, fake_md):
    """--collection with knowledge__ target emits the scholarly-paper warning (GH #981)."""
    # CliRunner (Click 8.x) mixes stdout+stderr into result.output by default.
    with patch("nexus.doc_indexer.index_markdown", return_value=MD_RESULT):
        result = runner.invoke(main, ["index", "md", str(fake_md), "--collection", "mynotes"])
    assert result.exit_code == 0, result.output
    assert "scholarly-paper extractor" in result.output
    assert "hallucinate" in result.output
    assert "GH #981 fix #2" in result.output


def test_md_collection_no_warning_when_corpus_default(runner, fake_md):
    """No prose-extractor warning when --collection is absent (docs__ path)."""
    with patch("nexus.doc_indexer.index_markdown", return_value=MD_RESULT):
        result = runner.invoke(main, ["index", "md", str(fake_md)])
    assert result.exit_code == 0, result.output
    assert "scholarly-paper extractor" not in result.output


# ── --extractor flag ─────────────────────────────────────────────────────────

_PDF_STUB = {"chunks": 1, "pages": [], "title": "", "author": ""}


@pytest.mark.parametrize("extractor", ["auto", "mineru", "docling"])
def test_extractor_valid_values(runner, fake_pdf, extractor):
    args = ["index", "pdf", str(fake_pdf)]
    if extractor != "auto":
        args += ["--extractor", extractor]
    with patch("nexus.doc_indexer.index_pdf", return_value=_PDF_STUB) as m:
        result = runner.invoke(main, args)
    assert result.exit_code == 0, result.output
    _, kw = m.call_args
    assert kw["extractor"] == extractor


def test_extractor_invalid_rejected(runner, fake_pdf):
    result = runner.invoke(main, ["index", "pdf", str(fake_pdf), "--extractor", "magic"])
    assert result.exit_code != 0


def test_mineru_not_installed_gives_helpful_error(runner, fake_pdf):
    # nexus-2fyb: mineru is a default dep; the absence indicates a corrupt
    # install, so the error message points at `uv tool install --reinstall`
    # rather than a now-removed [mineru] extra.
    with patch(
        "nexus.doc_indexer.index_pdf",
        side_effect=ImportError(
            "MinerU is not importable but is a required dependency since "
            "nexus-2fyb. Reinstall conexus: `uv tool install --reinstall conexus`."
        ),
    ):
        result = runner.invoke(main, ["index", "pdf", str(fake_pdf), "--extractor", "mineru"])
    assert result.exit_code != 0
    assert "MinerU" in result.output


# ── ETA ticker (nexus-vatx Gap 3) ────────────────────────────────────────────


def test_format_eta_with_samples():
    """After a few files complete, the ETA line carries n/total, chunk total,
    avg s/file, and a minute estimate."""
    from nexus.commands.index import _format_eta
    # 100 files, 10 done in 20s → 2.0s/file avg, 90 files * 2s = 180s ≈ 3 min
    line = _format_eta(n=10, total=100, chunks=1234, elapsed_s=20.0)
    assert line.startswith("[eta] 10/100 files")
    assert "1,234 chunks" in line
    assert "2.0s/file avg" in line
    assert "~3 min remaining" in line


def test_format_eta_pending_before_first_file():
    """The first tick can fire before any file completes — the formatter
    must degrade gracefully, not divide by zero."""
    from nexus.commands.index import _format_eta
    line = _format_eta(n=0, total=100, chunks=0, elapsed_s=5.0)
    assert "0/100 files" in line
    assert "no samples yet" in line
    assert "pending" in line


def test_format_eta_floors_eta_to_minimum_one_minute():
    """A nearly-done run (2 files remaining, 1s/file) shouldn't report
    '~0 min remaining' — floor to 1 min so the signal stays positive."""
    from nexus.commands.index import _format_eta
    line = _format_eta(n=998, total=1000, chunks=50_000, elapsed_s=998.0)
    assert "~1 min remaining" in line


def test_eta_ticker_emits_at_interval():
    """The ticker fires at least once when started + given enough wall
    time, and the emitted line is `[eta] ...` format."""
    from nexus.commands.index import _ETATicker
    emitted: list[str] = []
    # Very short interval so the test runs fast; CI ≤ 100ms reliably.
    t = _ETATicker(interval=0.02, emit=emitted.append)
    t.start(total=10)
    t.record(chunks=100)
    t.record(chunks=200)
    time.sleep(0.08)
    t.stop()
    assert emitted, "ticker never emitted despite wall-clock > interval"
    assert all(ln.startswith("[eta]") for ln in emitted)


def test_eta_ticker_stop_is_idempotent_and_joins_thread():
    """Double-stop must not raise; after stop the thread is gone."""
    from nexus.commands.index import _ETATicker
    t = _ETATicker(interval=0.05, emit=lambda _: None)
    t.start(total=10)
    t.stop()
    # Second stop is a no-op on the already-set event.
    t.stop()
    assert t._thread is None


def test_eta_ticker_no_emit_before_start():
    """Ticker created but never started must not spawn a thread or emit."""
    from nexus.commands.index import _ETATicker
    emitted: list[str] = []
    t = _ETATicker(interval=0.01, emit=emitted.append)
    # No start() call.
    time.sleep(0.03)
    assert emitted == []
    # stop() without start() must also be safe.
    t.stop()


# ── nexus-vatx dead-ticker latch fix (index-output-ux-assessment) ──────────


def test_eta_ticker_not_stopped_by_first_phase_marker(runner, repo_dir, mock_reg, monkeypatch):
    """The ticker used to be killed on the FIRST ``on_phase`` call, on the
    (false) theory that the file walk was already done by then. In
    production the first phase marker fires BEFORE the per-file loop even
    starts (indexer.py on_start at :3496 precedes the first run_file_loop
    at :4252) — the fix must let the ticker survive phase markers and
    still emit during a simulated long phase that follows one.

    Heartbeat double-fire fix (T2 22168 follow-up): the ticker no longer
    emits its ``0/N … pending`` tick during the pre-loop phase (mutual
    exclusion with ``_PhaseHeartbeat`` — see
    ``test_eta_ticker_and_phase_heartbeat_never_double_fire`` below), so
    this test's "still emits" claim now needs a real per-file tick — a
    sleep AFTER the first ``on_file`` call, before the loop completes —
    rather than relying on a pending-state tick during the earlier phase
    sleep.
    """
    import nexus.commands.index as index_mod

    class _FastETATicker(index_mod._ETATicker):
        def __init__(self, interval=60.0, emit=None):
            super().__init__(interval=0.02, emit=emit)

    monkeypatch.setattr(index_mod, "_ETATicker", _FastETATicker)

    def fake_index(path, reg, **kwargs):
        on_start = kwargs.get("on_start")
        on_phase = kwargs.get("on_phase")
        on_file = kwargs.get("on_file")
        on_start(2)
        on_phase("Registering 1 catalog entries…")  # fires pre-loop in production
        time.sleep(0.06)  # simulated long phase: ticker must survive it
        on_file(Path("a.py"), 3, 0.1)
        time.sleep(0.06)  # real per-file progress now exists — ticker may tick
        on_file(Path("b.py"), 2, 0.1)
        return {}

    result, _ = _invoke_repo(
        runner, [str(repo_dir)], mock_reg, index_side_effect=fake_index,
    )
    assert result.exit_code == 0, result.output
    assert "[eta]" in result.output


def test_eta_ticker_stops_when_file_loop_completes_not_on_phase(
    runner, repo_dir, mock_reg, monkeypatch,
):
    """Exact-count regression for the latch fix: ``.stop()`` must not be
    called as a side effect of a phase marker, and must be called once
    the file loop reaches its total."""
    import nexus.commands.index as index_mod

    stop_calls: list[int] = []
    orig_stop = index_mod._ETATicker.stop

    def spy_stop(self):
        stop_calls.append(1)
        orig_stop(self)

    monkeypatch.setattr(index_mod._ETATicker, "stop", spy_stop)

    def fake_index(path, reg, **kwargs):
        on_start = kwargs.get("on_start")
        on_phase = kwargs.get("on_phase")
        on_file = kwargs.get("on_file")
        on_start(2)
        on_phase("Registering 1 catalog entries…")
        assert len(stop_calls) == 0, "a phase marker must not stop the ticker"
        on_file(Path("a.py"), 3, 0.1)
        assert len(stop_calls) == 0, "the ticker must not stop before the loop completes"
        on_file(Path("b.py"), 2, 0.1)
        assert len(stop_calls) == 1, "the ticker must stop exactly once n reaches total"
        return {}

    result, _ = _invoke_repo(
        runner, [str(repo_dir)], mock_reg, index_side_effect=fake_index,
    )
    assert result.exit_code == 0, result.output


# ── heartbeat double-fire fix (T2 22168 engine-w0-503 follow-up) ───────────


def test_eta_ticker_and_phase_heartbeat_never_double_fire(runner, repo_dir, mock_reg, monkeypatch):
    """Regression for the double-fire gap left by
    ``test_eta_ticker_not_stopped_by_first_phase_marker`` above: that test's
    sleep (0.06s) was shorter than ``_PhaseHeartbeat``'s REAL 10s/30s
    interval, so the heartbeat never actually got a chance to tick during
    it — it only proved the ETA ticker survives a phase marker, never that
    the two tickers stay mutually exclusive when BOTH are genuinely live at
    once. This test fast-forwards BOTH intervals to the same short value
    and sleeps long enough for each to tick multiple times over, driving a
    real collision:

    1. Pre-loop window (before any file completes): with both intervals
       elapsed several times over, the OLD code would emit
       ``[eta] 0/N files … pending`` concurrently with the heartbeat's
       ``still running`` tick for the armed pre-loop phase. The fix
       confines the ticker's live window to n >= 1, so no such line ever
       appears.
    2. Once the file loop genuinely starts (first ``on_file``), the
       heartbeat must disarm — sleeping again afterward must produce ONLY
       real ``[eta] 1/N files`` progress ticks, never another
       ``still running`` line interleaved with them.
    """
    import nexus.commands.index as index_mod

    class _FastETATicker(index_mod._ETATicker):
        def __init__(self, interval=60.0, emit=None):
            super().__init__(interval=0.02, emit=emit)

    class _FastPhaseHeartbeat(index_mod._PhaseHeartbeat):
        def __init__(self, *, is_tty, echo, interval=None):
            super().__init__(is_tty=is_tty, echo=echo, interval=0.02)

    monkeypatch.setattr(index_mod, "_ETATicker", _FastETATicker)
    monkeypatch.setattr(index_mod, "_PhaseHeartbeat", _FastPhaseHeartbeat)

    def fake_index(path, reg, **kwargs):
        on_start = kwargs.get("on_start")
        on_phase = kwargs.get("on_phase")
        on_file = kwargs.get("on_file")
        on_start(2)
        on_phase("Registering 1 catalog entries…")  # arms the heartbeat, pre-loop
        time.sleep(0.09)  # both 0.02s intervals elapse several times over
        on_file(Path("a.py"), 3, 0.1)  # real progress begins — must disarm the heartbeat
        time.sleep(0.09)  # only the eta ticker may tick from here on
        on_file(Path("b.py"), 2, 0.1)
        return {}

    result, _ = _invoke_repo(
        runner, [str(repo_dir)], mock_reg, index_side_effect=fake_index,
    )
    assert result.exit_code == 0, result.output
    # (1) no pending-state tick ever fired during the pre-loop window.
    assert "0/2 files" not in result.output
    # (2) once real per-file progress exists, no MORE heartbeat ticks —
    # everything from the first genuine eta line onward is eta-only.
    idx = result.output.find("1/2 files")
    assert idx != -1, "expected a real per-file eta tick once the loop started"
    assert "still running" not in result.output[idx:]


# ── _PhaseHeartbeat (index-output-ux-assessment-2026-08-10 §7.1) ───────────


def test_phase_heartbeat_emits_at_interval_tty():
    """TTY rendering: in-place (\\r, nl=False), no scrollback growth."""
    from nexus.commands.index import _PhaseHeartbeat
    calls: list[tuple[str, bool]] = []
    hb = _PhaseHeartbeat(is_tty=True, echo=lambda msg, nl: calls.append((msg, nl)), interval=0.02)
    hb.arm("Pruning deleted files…")
    time.sleep(0.07)
    hb.disarm()
    ticks = [c for c in calls if "still running" in c[0]]
    assert len(ticks) >= 2
    for msg, nl in ticks:
        assert nl is False
        assert msg.startswith("\r")
        assert "Pruning deleted files…" in msg
        assert "elapsed)" in msg


def test_phase_heartbeat_emits_at_interval_nontty_no_cr():
    """Non-TTY rendering: real appended lines, never a \\r."""
    from nexus.commands.index import _PhaseHeartbeat
    calls: list[tuple[str, bool]] = []
    hb = _PhaseHeartbeat(
        is_tty=False, echo=lambda msg, nl: calls.append((msg, nl)), interval=0.02,
    )
    hb.arm("Pruning misclassified chunks…")
    time.sleep(0.07)
    hb.disarm()
    ticks = [c for c in calls if "still running" in c[0]]
    assert len(ticks) >= 2
    for msg, nl in ticks:
        assert "\r" not in msg
        assert nl is True


def test_phase_heartbeat_never_emits_bracket_nm_form():
    """Constraint: heartbeat lines must never match ``^\\s*\\[N/M\\]`` —
    that shape is reserved for the per-file progress lines counted by
    tests/e2e/migration-rehearsal/rehearse_shakeout.sh."""
    import re
    from nexus.commands.index import _PhaseHeartbeat
    calls: list[str] = []
    hb = _PhaseHeartbeat(is_tty=False, echo=lambda msg, nl: calls.append(msg), interval=0.02)
    hb.arm("Pruning deleted files…")
    time.sleep(0.05)
    hb.disarm()
    assert calls, "expected at least one heartbeat tick"
    pattern = re.compile(r"^\s*\[[0-9]+/[0-9]+\]")
    for msg in calls:
        assert not pattern.match(msg)


def test_phase_heartbeat_disarm_before_first_tick_emits_nothing():
    from nexus.commands.index import _PhaseHeartbeat
    calls: list[str] = []
    hb = _PhaseHeartbeat(is_tty=False, echo=lambda msg, nl: calls.append(msg), interval=5.0)
    hb.arm("Stamping pipeline version…")
    hb.disarm()
    assert calls == []


def test_phase_heartbeat_rearm_stops_previous_phase_ticks():
    """arm() for a new phase must fully stop the previous phase's ticker —
    no interleaved/overlapping threads."""
    from nexus.commands.index import _PhaseHeartbeat
    calls: list[str] = []
    hb = _PhaseHeartbeat(is_tty=False, echo=lambda msg, nl: calls.append(msg), interval=0.02)
    hb.arm("Phase A…")
    time.sleep(0.03)
    hb.arm("Phase B…")  # must fully disarm Phase A's thread before returning
    time.sleep(0.05)
    hb.disarm()
    a_indices = [i for i, c in enumerate(calls) if "Phase A" in c]
    b_indices = [i for i, c in enumerate(calls) if "Phase B" in c]
    assert b_indices, "expected at least one Phase B tick"
    if a_indices:
        assert max(a_indices) < min(b_indices), "a Phase A tick fired after Phase B was armed"


def test_on_phase_heartbeat_fires_for_silent_long_phase(runner, repo_dir, mock_reg, monkeypatch):
    """Integration: on_phase wiring arms/disarms the heartbeat and prints
    it on the ``[post]`` channel with the exact liveness constraints."""
    import re
    import nexus.commands.index as index_mod

    class _FastPhaseHeartbeat(index_mod._PhaseHeartbeat):
        def __init__(self, *, is_tty, echo, interval=None):
            super().__init__(is_tty=is_tty, echo=echo, interval=0.02)

    monkeypatch.setattr(index_mod, "_PhaseHeartbeat", _FastPhaseHeartbeat)

    def fake_index(path, reg, **kwargs):
        on_start = kwargs.get("on_start")
        on_phase = kwargs.get("on_phase")
        on_start(1)
        on_phase("Pruning deleted files…")
        time.sleep(0.07)
        on_phase("Pruning deleted files done (0.1s)")
        return {}

    result, _ = _invoke_repo(
        runner, [str(repo_dir)], mock_reg, index_side_effect=fake_index,
    )
    assert result.exit_code == 0, result.output
    assert "  [post] Pruning deleted files…" in result.output
    assert "still running" in result.output
    assert "elapsed)" in result.output
    assert "  [post] Pruning deleted files done (0.1s)" in result.output
    assert not re.search(r"^\s*\[[0-9]+/[0-9]+\]", result.output, re.MULTILINE)
    assert "\r" not in result.output  # CliRunner's stdout is never a TTY


# ── mid-loop chunk-flush progress (nexus-rhwg5 / GH #1432 ask 3 residue) ────


def test_monitor_wires_on_flush_and_renders_progress_line(runner, repo_dir, mock_reg):
    """--monitor must thread a real on_flush callback through to
    index_repository, and calling it must render the exact shape the
    bead specifies -- never a fabricated N/M denominator."""
    import re

    def fake_index(path, reg, **kwargs):
        on_start = kwargs.get("on_start")
        on_flush = kwargs["on_flush"]
        on_start(1)
        assert on_flush is not None
        on_flush(1, 42, "code__myrepo", 0.37, None)
        on_flush(2, 7, "docs__myrepo", 1.2, None)
        return {}

    result, _ = _invoke_repo(
        runner, [str(repo_dir), "--monitor"], mock_reg, index_side_effect=fake_index,
    )
    assert result.exit_code == 0, result.output
    assert "  [post] flush #1 complete (0.4s, 42 chunks, code__myrepo)" in result.output
    assert "  [post] flush #2 complete (1.2s, 7 chunks, docs__myrepo)" in result.output
    # constraint 1: never a bracketed fraction -- "#N" is a bare ordinal,
    # not "[N/M]".
    assert not re.search(r"^\s*\[[0-9]+/[0-9]+\]", result.output, re.MULTILINE)
    assert "\r" not in result.output  # CliRunner's stdout is never a TTY
    assert result.output.rstrip().splitlines()[-1] == "Done."


def test_default_no_monitor_on_flush_is_none(runner, repo_dir, mock_reg):
    """Default (non---monitor) path: on_flush must be None, not a
    real-but-silent callback -- this is the assertion that pins the
    level assignment (assessment 7.5) rather than assuming it."""
    result, mock_idx = _invoke_repo(runner, [str(repo_dir)], mock_reg)
    assert result.exit_code == 0, result.output
    _, kw = mock_idx.call_args
    assert kw.get("on_flush") is None


def test_monitor_flush_progress_emits_nothing_without_monitor_flag(runner, repo_dir, mock_reg):
    """Same fake_index shape as the --monitor test above, invoked WITHOUT
    --monitor: on_flush must be None so the fake harness itself proves
    nothing CAN be emitted on the default path (not just "wasn't
    called")."""
    def fake_index(path, reg, **kwargs):
        on_start = kwargs.get("on_start")
        on_start(1)
        assert kwargs["on_flush"] is None
        return {}

    result, _ = _invoke_repo(
        runner, [str(repo_dir)], mock_reg, index_side_effect=fake_index,
    )
    assert result.exit_code == 0, result.output
    assert "flush #" not in result.output


def test_monitor_flush_failure_surfaces_before_done(runner, repo_dir, mock_reg):
    """Failures surface AT OCCURRENCE (mid-run), not only in the
    end-of-run summary -- the literal ask-3 gap. Non-vacuity: the FAILED
    marker's position must be strictly before 'Done.', not merely
    present somewhere in the output."""
    def fake_index(path, reg, **kwargs):
        on_start = kwargs.get("on_start")
        on_flush = kwargs["on_flush"]
        on_start(1)
        on_flush(1, 3, "code__myrepo", 0.1, "gateway 503")
        return {}

    result, _ = _invoke_repo(
        runner, [str(repo_dir), "--monitor"], mock_reg, index_side_effect=fake_index,
    )
    assert result.exit_code == 0, result.output
    assert "FAILED: gateway 503" in result.output
    failed_pos = result.output.index("FAILED: gateway 503")
    done_pos = result.output.index("Done.")
    assert failed_pos < done_pos, "failure must surface before Done., not only in a final summary"


# ── --debug-timing (nexus-7niu) ─────────────────────────────────────────────


def test_debug_timing_flag_subscribes_on_stage_timers(runner, repo_dir, mock_reg):
    """Passing ``--debug-timing`` must thread an ``on_stage_timers``
    callback through to ``index_repository``; the default invocation
    must pass ``None`` so the fast path stays zero-overhead."""
    # Default run: on_stage_timers should be None
    result, mock_idx = _invoke_repo(runner, [str(repo_dir)], mock_reg)
    assert result.exit_code == 0
    _, kw = mock_idx.call_args
    assert kw.get("on_stage_timers") is None

    # --debug-timing run: on_stage_timers should be callable
    result, mock_idx = _invoke_repo(
        runner, [str(repo_dir), "--debug-timing"], mock_reg,
    )
    assert result.exit_code == 0, result.output
    _, kw = mock_idx.call_args
    cb = kw.get("on_stage_timers")
    assert cb is not None
    assert callable(cb)


def test_debug_timing_flag_emits_breakdown_when_timers_arrive(
    runner, repo_dir, mock_reg,
):
    """End-of-run stderr breakdown renders the per-stage totals when
    the callback collected any timers. Silent (no breakdown line) when
    no timers arrive — e.g. frecency-only run, or a no-file repo."""
    from nexus.stage_timers import StageTimers

    def _side_effect(*_a, on_stage_timers=None, **_kw):
        # Simulate the indexer firing the callback twice with known
        # per-stage times so the CLI's end-of-run table has real data.
        if on_stage_timers is not None:
            t1 = StageTimers(
                chunking_s=1.0, embed_s=4.0, upload_s=0.5, retry_s=0.0,
            )
            t2 = StageTimers(
                chunking_s=2.0, embed_s=6.0, upload_s=1.5, retry_s=1.0,
            )
            on_stage_timers(Path("a.py"), t1)
            on_stage_timers(Path("b.py"), t2)
        return {}

    result, _ = _invoke_repo(
        runner,
        [str(repo_dir), "--debug-timing"],
        mock_reg,
        index_side_effect=_side_effect,
    )
    assert result.exit_code == 0, result.output
    # Breakdown header + per-stage rows
    assert "[debug-timing] per-stage totals across 2 files" in result.output
    assert "chunking_s" in result.output and "3.0s" in result.output   # 1+2
    assert "embed_s"   in result.output and "10.0s" in result.output   # 4+6
    assert "upload_s"  in result.output and "2.0s"  in result.output   # 0.5+1.5
    assert "retry_s"   in result.output and "1.0s"  in result.output   # 0+1
    assert "total"     in result.output and "16.0s" in result.output   # 3+10+2+1


def test_debug_timing_absent_emits_no_breakdown(runner, repo_dir, mock_reg):
    """Without ``--debug-timing`` the per-stage breakdown must not
    appear — normal runs stay tidy."""
    result, _ = _invoke_repo(runner, [str(repo_dir)], mock_reg)
    assert result.exit_code == 0
    assert "debug-timing" not in result.output
    assert "per-stage totals" not in result.output


def test_project_cross_collections_passes_source_collection(monkeypatch) -> None:
    """nexus-g25dk: the auto-discover cross-collection projection persist must
    pass source_collection. The old inline call omitted it, so
    _persist_assignments raised TypeError (swallowed by the caller's except)
    and the projection silently persisted nothing.

    RDR-151 Phase 3: the persist now routes through ``t2_index_write``; patch it
    to the fake taxonomy (its no-daemon fallback would otherwise open a real
    T2Database) and assert source_collection survives the routed
    ``persist_assignments`` path."""
    from nexus.commands.index import _project_cross_collections

    recorded: list[tuple] = []

    class _FakeTaxonomy:
        def project_against(self, col, others, chroma_client, threshold=0.85):
            # One assignment per projection call.
            return {"chunk_assignments": [("doc-1", 7, 0.9)]}

        def persist_assignments(self, assignments):
            for a in assignments:
                recorded.append(
                    (a["doc_id"], a["topic_id"], a["source_collection"])
                )
            return len(assignments)

        def refresh_projection_links(self):
            pass

    fake = _FakeTaxonomy()

    class _FakeDb:
        taxonomy = fake

    import nexus.mcp_infra as _mi
    monkeypatch.setattr(_mi, "t2_index_write", lambda fn: fn(_FakeDb()))

    total, incomplete = _project_cross_collections(
        fake, ["code__a", "code__b"], chroma_client=None,
    )
    # a-vs-b and b-vs-a each persist one assignment.
    assert total == 2
    assert incomplete == 0
    # Each persisted assignment is stamped with its own source collection.
    assert {r[2] for r in recorded} == {"code__a", "code__b"}


def test_project_cross_collections_single_collection_noop() -> None:
    """A lone collection has no 'others' to project against → 0, no persist."""
    from nexus.commands.index import _project_cross_collections

    class _FakeTaxonomy:
        def project_against(self, *a, **k):  # pragma: no cover - must not be called
            raise AssertionError("project_against should not run with no others")

    assert _project_cross_collections(_FakeTaxonomy(), ["only__one"], None) == (0, 0)


# ── nx index repo: per-run log file (nexus-mjc9l) ───────────────────────────


def _log_path_for(home: Path, repo_dir: Path) -> Path:
    from nexus.repo_identity import _repo_identity
    basename, h8 = _repo_identity(repo_dir)
    return home / ".config" / "nexus" / "logs" / f"index-{basename}-{h8}.log"


def test_index_repo_writes_run_log(runner, repo_dir, home, mock_reg):
    """The wiring test exercises the Phase-1 primitive end-to-end via the
    real structlog path — a structlog INFO event emitted during indexing
    must land in the per-run log file (this fails against a handler-only
    primitive, since cli mode's structlog WARNING filter would drop it)."""
    import structlog

    def _fake_index(*a, **kw):
        structlog.get_logger("nexus.test").info("indexing_probe", probe="xyz")
        return {}

    result, _ = _invoke_repo(runner, [str(repo_dir)], mock_reg, index_side_effect=_fake_index)
    assert result.exit_code == 0

    log_path = _log_path_for(home, repo_dir)
    assert log_path.exists()
    contents = log_path.read_text()
    assert "indexing_probe" in contents


def test_index_repo_run_log_not_leaked_to_stderr(runner, repo_dir, home, mock_reg):
    """Quiet-contract guard: the INFO probe must land in the file, not the
    terminal — nx index's existing stdout/stderr output must be unaffected."""
    import structlog

    def _fake_index(*a, **kw):
        structlog.get_logger("nexus.test").info("indexing_probe", probe="xyz")
        return {}

    result, _ = _invoke_repo(runner, [str(repo_dir)], mock_reg, index_side_effect=_fake_index)
    assert result.exit_code == 0
    assert "indexing_probe" not in result.output


def test_index_repo_no_handler_leak_after_run(runner, repo_dir, home, mock_reg):
    """No RotatingFileHandler for the run-log path survives after the
    command returns — success path."""
    import logging.handlers

    result, _ = _invoke_repo(runner, [str(repo_dir)], mock_reg, index_return={})
    assert result.exit_code == 0

    log_path = _log_path_for(home, repo_dir)
    leaked = [
        h for h in logging.getLogger().handlers
        if isinstance(h, logging.handlers.RotatingFileHandler)
        and h.baseFilename == str(log_path)
    ]
    assert leaked == []


def test_index_repo_no_handler_leak_after_exception(runner, repo_dir, home, mock_reg):
    """Same guard, but index_repository raises — the handler must still be
    removed (the context manager's finally, not just the happy path)."""
    import logging.handlers

    def _fake_index(*a, **kw):
        raise RuntimeError("boom")

    result, _ = _invoke_repo(runner, [str(repo_dir)], mock_reg, index_side_effect=_fake_index)
    assert result.exit_code != 0

    log_path = _log_path_for(home, repo_dir)
    leaked = [
        h for h in logging.getLogger().handlers
        if isinstance(h, logging.handlers.RotatingFileHandler)
        and h.baseFilename == str(log_path)
    ]
    assert leaked == []


# ── GH #1397 / nexus-94fxl: identity-drop summary ───────────────────────────

def test_identity_drop_summary_silent_when_none(runner, repo_dir, mock_reg):
    result, _ = _invoke_repo(runner, [str(repo_dir)], mock_reg)
    assert result.exit_code == 0, result.output
    assert "WITHOUT a catalog document identity" not in result.output


def test_identity_drop_summary_surfaces_drops(runner, repo_dir, mock_reg, monkeypatch):
    monkeypatch.setattr(
        "nexus.mcp_infra.get_manifest_identity_drops",
        lambda: [
            {"collection": "rdr__nexus", "batch_size": 23},
            {"collection": "rdr__nexus", "batch_size": 4},
        ],
    )
    result, _ = _invoke_repo(runner, [str(repo_dir)], mock_reg)
    # nexus-tp8yk D2b: an identity drop now fails the run's exit code —
    # was WARNING-only (rc=0) before this bead.
    assert result.exit_code != 0, result.output
    assert (
        "WARNING: 2 chunk batch(es) (27 chunks; collection(s): rdr__nexus) "
        "were indexed WITHOUT a catalog document identity" in result.output
    )
    assert "nx catalog reconcile" in result.output
    assert "nx catalog show" in result.output


# ── nexus-7f5qj: identity-drop / manifest-write-failure / completion-
# refusal collector wiring for the SINGLE-FILE nx index pdf / nx index md
# commands — previously ZERO wiring existed (the collector-check helper
# was only reachable from index_repo_cmd's batch path), so a catalog-
# register failure during ``nx index pdf <file>`` / ``nx index md <file>``
# left chunks written and searchable but with no catalog Document/tumbler,
# while the command printed "Indexed N chunk(s)." and exited 0. Same class
# as pbawi acceptance item 3 (fixed on the ``nx dt index`` surface by
# nexus-tp8yk D2, pinned by nexus-2xu6t's tests) — this bead closes the
# sibling gap and extracts the now-thrice-needed pattern into
# ``nexus.commands._helpers`` (see test_commands_helpers_identity_drop.py
# for the helper's own unit tests).
#
# Two layers, mirroring the split already established for ``nx index
# repo`` (this file) and ``nx dt index`` (test_commands_dt.py::
# TestIdentityDropSummary):
#   * ``TestPdfMdIdentityDropSummaryWiring`` — collector -> exit-code
#     wiring, isolated from the register mechanism (monkeypatches the
#     collector getters directly, same idiom as
#     ``test_identity_drop_summary_surfaces_drops`` above).
#   * ``TestPdfMdIdentityDropRegisterThrow`` — drives the REAL
#     ``_register_or_lookup_doc_id`` swallow through a broken catalog
#     writer, proving the register-failure -> collector -> CLI exit code
#     link end to end (mirrors test_commands_dt.py::TestIdentityDropSummary
#     and tests/test_doc_indexer.py::
#     test_preflight_register_failure_feeds_identity_drop_collector).
#
# Streaming-route note (2xu6t lesson): the collector-population mechanism
# itself is already proven correct on BOTH the non-streaming batch path
# (tests/test_doc_indexer.py::test_preflight_register_failure_feeds_
# identity_drop_collector) and the streaming path
# (tests/test_pipeline_stages.py::TestPipelineIndexPdf::
# test_streaming_register_failure_feeds_identity_drop_collector) — neither
# of which this bead touches (no doc_indexer.py / pipeline_stages.py
# production diff). What is NEW here is the CLI-layer reset+check+raise
# wiring in index_pdf_cmd / index_md_cmd, and that wiring reads the SAME
# process-global collector regardless of which internal route populated
# it — it is route-agnostic by construction (emit_identity_drop_summary
# calls get_manifest_identity_drops() etc. with no knowledge of streaming
# vs non-streaming). The non-streaming register-throw test below
# (``fake_pdf`` is unopenable-by-pymupdf bytes, same fixture shape as
# ``sample_pdf`` in test_doc_indexer.py) therefore proves the CLI wiring
# end-to-end without needing to re-derive the streaming-route fixture
# plumbing (fake pipeline-engine DB, PDFExtractor/PDFChunker patches) a
# second time at the CLI layer — that plumbing is exercised, unmodified
# and unaffected by this diff, by test_pipeline_stages.py already.


class TestPdfMdIdentityDropSummaryWiring:
    """Collector -> exit-code wiring, isolated from the register
    mechanism. Mirrors ``test_identity_drop_summary_surfaces_drops`` /
    ``test_manifest_write_failure_summary_surfaces_failures`` above.
    """

    def test_pdf_silent_when_no_problems(self, runner, fake_pdf):
        with patch("nexus.doc_indexer.index_pdf", return_value=PDF_RESULT):
            result = runner.invoke(main, ["index", "pdf", str(fake_pdf)])
        assert result.exit_code == 0, result.output
        assert "WITHOUT a catalog document identity" not in result.output
        assert "catalog manifest write failed" not in result.output

    def test_md_silent_when_no_problems(self, runner, fake_md):
        with patch("nexus.doc_indexer.index_markdown", return_value=MD_RESULT):
            result = runner.invoke(main, ["index", "md", str(fake_md)])
        assert result.exit_code == 0, result.output
        assert "WITHOUT a catalog document identity" not in result.output
        assert "catalog manifest write failed" not in result.output

    def test_pdf_identity_drop_exits_nonzero_and_names_file(
        self, runner, fake_pdf, monkeypatch,
    ):
        monkeypatch.setattr(
            "nexus.mcp_infra.get_manifest_identity_drops",
            lambda: [{"collection": "docs__default", "batch_size": 3}],
        )
        with patch("nexus.doc_indexer.index_pdf", return_value=PDF_RESULT):
            result = runner.invoke(main, ["index", "pdf", str(fake_pdf)])
        assert result.exit_code != 0, result.output
        assert "WITHOUT a catalog document identity" in result.output
        assert str(fake_pdf) in result.output, result.output
        assert "nx catalog reconcile" in result.output

    def test_md_identity_drop_exits_nonzero_and_names_file(
        self, runner, fake_md, monkeypatch,
    ):
        monkeypatch.setattr(
            "nexus.mcp_infra.get_manifest_identity_drops",
            lambda: [{"collection": "docs__default", "batch_size": 2}],
        )
        with patch("nexus.doc_indexer.index_markdown", return_value=MD_RESULT):
            result = runner.invoke(main, ["index", "md", str(fake_md)])
        assert result.exit_code != 0, result.output
        assert "WITHOUT a catalog document identity" in result.output
        assert str(fake_md) in result.output, result.output
        assert "nx catalog reconcile" in result.output

    def test_pdf_manifest_write_failure_exits_nonzero_and_names_file(
        self, runner, fake_pdf, monkeypatch,
    ):
        monkeypatch.setattr(
            "nexus.mcp_infra.get_manifest_write_failures",
            lambda: ["1.9.0"],
        )
        with patch("nexus.doc_indexer.index_pdf", return_value=PDF_RESULT):
            result = runner.invoke(main, ["index", "pdf", str(fake_pdf)])
        assert result.exit_code != 0, result.output
        assert "WARNING: catalog manifest write failed" in result.output
        assert str(fake_pdf) in result.output, result.output

    def test_md_manifest_write_failure_exits_nonzero_and_names_file(
        self, runner, fake_md, monkeypatch,
    ):
        monkeypatch.setattr(
            "nexus.mcp_infra.get_manifest_write_failures",
            lambda: ["1.9.0"],
        )
        with patch("nexus.doc_indexer.index_markdown", return_value=MD_RESULT):
            result = runner.invoke(main, ["index", "md", str(fake_md)])
        assert result.exit_code != 0, result.output
        assert "WARNING: catalog manifest write failed" in result.output
        assert str(fake_md) in result.output, result.output


class TestPdfMdIdentityDropRegisterThrow:
    """Real end-to-end proof: a genuine ``_register_or_lookup_doc_id``
    swallow (broken catalog writer) feeds the collector and the CLI fails
    loud. Fixtures/idioms mirror
    ``test_commands_dt.py::TestIdentityDropSummary``.
    """

    @staticmethod
    def _empty_t3():
        t3 = MagicMock()
        t3.get_or_create_collection.return_value = MagicMock(
            get=MagicMock(return_value={"ids": [], "metadatas": []}),
        )
        return t3

    @staticmethod
    def _t3_with_readback_chunk():
        """Like ``_empty_t3()`` but ``.get()`` returns one fake row.

        Only the STREAMING route's ``return_metadata=True`` branch needs
        this (nexus-w6wp0): it does an extra post-upload T3 query,
        content_hash-scoped when ``doc_id`` is empty (the register-throw
        case), to build the CLI's pages/title/author summary. An always-
        empty ``.get()`` (``_empty_t3()``) makes that query find nothing
        for a real write, raising ``IndexingError`` ("chunks committed
        but metadata read found none") BEFORE this test's own identity-
        drop check ever runs — an artifact of the double's simplicity,
        not of the code under test (the non-streaming route derives
        metadata from in-memory chunk data instead, so ``_empty_t3()``
        alone is sufficient there).
        """
        t3 = MagicMock()
        t3.get_or_create_collection.return_value = MagicMock(
            get=MagicMock(return_value={
                "ids": ["fake-chash-0"],
                "metadatas": [{"page_number": 1, "title": "", "source_author": ""}],
            }),
        )
        return t3

    @staticmethod
    def _broken_catalog(*, register_raises: bool):
        reader = MagicMock()
        reader.by_file_path.return_value = None
        reader.by_source_uri.return_value = None
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

    def test_md_register_throw_exits_nonzero_names_file(
        self, runner, home, monkeypatch,
    ):
        from nexus.cli import main as cli_main

        # nexus-sghyo (2026-08-06): client-side Voyage embedding is retired
        # (Hal determination 2026-07-28); the non-service dispatch this test
        # needs to reach _register_or_lookup_doc_id via is now local-mode
        # (ONNX), not cloud-mode-with-credentials. The module-level
        # ``cloud_mode`` fixture (pytestmark above) already monkeypatched
        # ``nexus.config.is_local_mode`` to a hardcoded ``False`` — an env
        # var flip alone would not undo that, since the function object
        # itself was replaced. Re-patch it directly, run-order guaranteed
        # to win (fixture setup happens before the test body).
        monkeypatch.setattr("nexus.config.is_local_mode", lambda: True)
        monkeypatch.setenv("NX_STORAGE_BACKEND_VECTORS", "chroma")
        md = home / "orphan.md"
        md.write_text("# Orphan\n\nSome real prose body for orphan.\n")

        reader, writer = self._broken_catalog(register_raises=True)

        with patch("nexus.doc_indexer.make_t3", return_value=self._empty_t3()), \
             patch("nexus.catalog.factory.make_catalog_reader", return_value=reader), \
             patch("nexus.catalog.factory.make_catalog_writer", return_value=writer):
            result = runner.invoke(cli_main, ["index", "md", str(md)])

        assert result.exit_code != 0, result.output
        assert str(md) in result.output, result.output
        assert "orphaned" in result.output.lower(), result.output
        assert "nx catalog reconcile" in result.output

    def test_md_register_ok_summary_unchanged(self, runner, home, monkeypatch):
        from nexus.cli import main as cli_main

        # nexus-sghyo (2026-08-06): see test_md_register_throw above — local
        # mode is the surviving non-service dispatch path.
        monkeypatch.setattr("nexus.config.is_local_mode", lambda: True)
        monkeypatch.setenv("NX_STORAGE_BACKEND_VECTORS", "chroma")
        md = home / "healthy.md"
        md.write_text("# Healthy\n\nSome real prose body for healthy.\n")

        reader, writer = self._broken_catalog(register_raises=False)

        with patch("nexus.doc_indexer.make_t3", return_value=self._empty_t3()), \
             patch("nexus.doc_indexer._fence_begin"), \
             patch("nexus.doc_indexer._fence_complete"), \
             patch("nexus.catalog.factory.make_catalog_reader", return_value=reader), \
             patch("nexus.catalog.factory.make_catalog_writer", return_value=writer):
            result = runner.invoke(cli_main, ["index", "md", str(md)])

        assert result.exit_code == 0, result.output
        assert "orphaned" not in result.output.lower(), result.output
        assert "WITHOUT a catalog document identity" not in result.output

    def test_pdf_register_throw_nonstreaming_exits_nonzero_names_file(
        self, runner, home, monkeypatch,
    ):
        """Non-streaming (batch/single-flush) route: ``pymupdf.open``
        cannot parse the fake bytes, so ``index_pdf`` falls through to
        ``_index_document`` (same fixture shape and rationale as
        ``sample_pdf`` in test_doc_indexer.py)."""
        from tests.test_doc_indexer import pdf_extract_patches_ctx

        from nexus.cli import main as cli_main

        # nexus-sghyo (2026-08-06): client-side Voyage embedding is retired
        # (Hal determination 2026-07-28); local mode is the surviving
        # non-service dispatch path this test needs.
        monkeypatch.setattr("nexus.config.is_local_mode", lambda: True)
        monkeypatch.setenv("NX_STORAGE_BACKEND_VECTORS", "chroma")

        pdf = home / "orphan.pdf"
        pdf.write_bytes(b"fake pdf bytes for testing")

        reader, writer = self._broken_catalog(register_raises=True)

        with patch("nexus.doc_indexer.make_t3", return_value=self._empty_t3()), \
             patch("nexus.catalog.factory.make_catalog_reader", return_value=reader), \
             patch("nexus.catalog.factory.make_catalog_writer", return_value=writer), \
             pdf_extract_patches_ctx():
            result = runner.invoke(cli_main, ["index", "pdf", str(pdf)])

        assert result.exit_code != 0, result.output
        assert str(pdf) in result.output, result.output
        assert "orphaned" in result.output.lower(), result.output
        assert "nx catalog reconcile" in result.output

    def test_pdf_register_ok_summary_unchanged(self, runner, home, monkeypatch):
        from tests.test_doc_indexer import pdf_extract_patches_ctx

        from nexus.cli import main as cli_main

        # nexus-sghyo (2026-08-06): see the throw variant above — local mode
        # is the surviving non-service dispatch path.
        monkeypatch.setattr("nexus.config.is_local_mode", lambda: True)
        monkeypatch.setenv("NX_STORAGE_BACKEND_VECTORS", "chroma")

        pdf = home / "healthy.pdf"
        pdf.write_bytes(b"fake pdf bytes for testing")

        reader, writer = self._broken_catalog(register_raises=False)

        with patch("nexus.doc_indexer.make_t3", return_value=self._empty_t3()), \
             patch("nexus.doc_indexer._fence_begin"), \
             patch("nexus.doc_indexer._fence_complete"), \
             patch("nexus.catalog.factory.make_catalog_reader", return_value=reader), \
             patch("nexus.catalog.factory.make_catalog_writer", return_value=writer), \
             pdf_extract_patches_ctx():
            result = runner.invoke(cli_main, ["index", "pdf", str(pdf)])

        assert result.exit_code == 0, result.output
        assert "orphaned" not in result.output.lower(), result.output
        assert "WITHOUT a catalog document identity" not in result.output

    def test_pdf_register_throw_streaming_exits_nonzero_names_file(
        self, runner, home, monkeypatch,
    ):
        """Coordinator review follow-up (2026-08-05, T2 [21484]): the
        non-streaming test above exercises the MINORITY route only —
        ``_STREAMING_THRESHOLD = 0`` (doc_indexer.py) means every REAL,
        pymupdf-openable PDF routes through ``pipeline_index_pdf``
        (streaming) unconditionally; the non-streaming batch/single-flush
        fallback is reached only when pymupdf cannot open the file at
        all. Forcing ``--streaming always`` drives the CLI through the
        DOMINANT route without needing genuinely valid PDF bytes (``use_
        streaming`` short-circuits True on ``streaming == "always"``
        before the pymupdf-openability probe even matters — doc_indexer.py
        ~2172). PDFExtractor/PDFChunker mocked at the pipeline seam
        (``nexus.pipeline_stages.PDFExtractor`` / ``PDFChunker``), same
        idiom as tests/test_pipeline_stages.py's ``_P_EXT``/``_P_CHK``/
        ``_er``/``_fx``/``_tc`` helpers (reused here, not re-derived) —
        this is the exact 2xu6t-lesson class: a fixture that accidentally
        exercises only the fallback path while the production-forced
        route goes unpinned. The pipeline-coordination ``HttpPipelineDB``
        is left unmocked (defaults to the real local engine this repo's
        test suite already boots for T2 — see ``tests/conftest.py``'s
        ``ensure_engine``); only the catalog reader/writer, T3 vector
        write, and PDF extraction are doubled, mirroring the non-streaming
        test's scope.

        CliRunner is always non-tty, so ``index_pdf_cmd`` always takes its
        ``return_metadata=True`` branch regardless of flags (``monitor or
        not sys.stdout.isatty()`` — patching ``sys.stdout.isatty`` does
        NOT help here: CliRunner installs its own capture stream around
        the invocation, after any pre-invoke monkeypatch on the original
        ``sys.stdout`` object). That branch does an EXTRA post-upload T3
        query (nexus-w6wp0), content_hash-scoped when ``doc_id`` is empty
        (the register-throw case) — an always-empty ``.get()`` double
        (``_empty_t3()``) makes that query find nothing for a real write,
        raising ``IndexingError`` ("chunks committed but metadata read
        found none") BEFORE this test's own identity-drop check ever
        runs, an artifact of the double's simplicity, not of the code
        under test. Fixed by ``_t3_with_readback_chunk()`` instead, which
        answers that query with one fake row. That display-metadata path
        already has its own dedicated coverage
        (test_index_pdf_indexing_error_exits_nonzero_with_clean_message
        above); this test's job is the identity-drop wiring, unaffected
        by which T3 double the metadata read satisfies itself with.
        """
        from tests.test_doc_indexer import pdf_extract_patches_ctx
        from tests.test_pipeline_stages import _P_CHK, _P_EXT, _er, _fx, _tc

        from nexus.cli import main as cli_main

        # nexus-sghyo (2026-08-06): client-side Voyage embedding is retired
        # (Hal determination 2026-07-28); local mode is the surviving
        # non-service dispatch path — doc_indexer.index_pdf resolves
        # embed_fn via _make_local_embed_fn() before delegating to the
        # streaming pipeline, so pipeline_stages never hits its own
        # (now unconditional) non-service-embedding-retired raise.
        monkeypatch.setattr("nexus.config.is_local_mode", lambda: True)
        monkeypatch.setenv("NX_STORAGE_BACKEND_VECTORS", "chroma")

        pdf = home / "orphan_streaming.pdf"
        pdf.write_bytes(b"fake pdf bytes for testing")

        reader, writer = self._broken_catalog(register_raises=True)

        fake_result = _er(2)
        fake_chunks = _tc(
            ("chunk one", 0, {"page_number": 1, "chunk_type": "text"}),
        )

        with patch(_P_EXT) as ext_cls, patch(_P_CHK) as chk_cls, \
             patch("nexus.doc_indexer.make_t3", return_value=self._t3_with_readback_chunk()), \
             patch("nexus.catalog.factory.make_catalog_reader", return_value=reader), \
             patch("nexus.catalog.factory.make_catalog_writer", return_value=writer), \
             pdf_extract_patches_ctx():
            ext_cls.return_value.extract.side_effect = _fx(
                fake_result.metadata["page_count"], fake_result,
            )
            chk_cls.return_value.chunk.return_value = fake_chunks
            result = runner.invoke(
                cli_main, ["index", "pdf", str(pdf), "--streaming", "always"],
            )

        assert result.exit_code != 0, result.output
        assert str(pdf) in result.output, result.output
        assert "orphaned" in result.output.lower(), result.output
        assert "nx catalog reconcile" in result.output


# ── nexus-7f5qj AC4: --dir batch mode audit ─────────────────────────────────

def test_pdf_dir_batch_identity_drop_surfaces_as_failure_entry(runner, home):
    """A per-file identity drop during ``--dir`` batch indexing raises no
    exception (same as the single-file case — the register swallow never
    propagates), so the existing per-file ``except Exception`` isolation
    never sees it. Symmetric fix: reset+check per file, folded into the
    existing ``failures`` list/summary convention rather than aborting the
    batch. nexus-uqq9z: the batch now ALSO exits non-zero once any file
    lands in the failures list (identity drops included), mirroring
    ``nx dt index``'s run-level fail-loud behavior — the batch itself
    still processes every file (isolation unchanged), only the exit code
    is new."""
    d = home / "pdfs"
    d.mkdir()
    (d / "a.pdf").write_bytes(b"fake pdf a")
    (d / "b.pdf").write_bytes(b"fake pdf b")

    calls = {"n": 0}

    def _fake_index_pdf(path, **kwargs):
        calls["n"] += 1
        if calls["n"] == 1:
            from nexus.mcp_infra import _record_manifest_identity_drop
            _record_manifest_identity_drop("docs__default", 2)
        return 2

    with patch("nexus.doc_indexer.index_pdf", side_effect=_fake_index_pdf):
        result = runner.invoke(
            main, ["index", "pdf", "--dir", str(d), "--extractor", "docling"],
        )

    assert result.exit_code != 0, result.output
    assert calls["n"] == 2, "both files must still be processed"
    assert "2 chunks" in result.output
    assert "1 failure(s)" in result.output, result.output
    assert "identity" in result.output.lower()
    assert "1 of 2 file(s) failed" in result.output, result.output
