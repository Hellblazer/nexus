# SPDX-License-Identifier: AGPL-3.0-or-later
import contextlib
import time
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest
from click.testing import CliRunner

from nexus.cli import main

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


def test_index_repo_idempotent_when_already_registered(runner, repo_dir, mock_reg):
    result, mock_idx = _invoke_repo(runner, [str(repo_dir)], mock_reg)
    assert result.exit_code == 0
    mock_reg.add.assert_not_called()
    mock_idx.assert_called_once()
    assert "Registered" not in result.output


def test_index_repo_invalid_path(runner, home):
    result = runner.invoke(main, ["index", "repo", str(home / "nonexistent")])
    assert result.exit_code != 0


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
    ("knowledge", "knowledge__knowledge"),
    ("knowledge__delos", "knowledge__delos"),
])
def test_pdf_collection_flag_normalization(runner, fake_pdf, flag_val, expected):
    rv = {"chunks": 5, "pages": [1], "title": "T", "author": "A"}
    with patch("nexus.doc_indexer.index_pdf", return_value=rv) as m:
        result = runner.invoke(main, ["index", "pdf", str(fake_pdf), "--collection", flag_val])
    assert result.exit_code == 0, result.output
    _, kw = m.call_args
    assert kw["collection_name"] == expected


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
    with patch(
        "nexus.doc_indexer.index_pdf",
        side_effect=ImportError("MinerU is not installed. Install with: uv pip install 'conexus[mineru]'"),
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
