"""T3: commands/index.py — nx index repo registration and indexing."""
import contextlib
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest
from click.testing import CliRunner

from nexus.cli import main


@pytest.fixture
def runner() -> CliRunner:
    return CliRunner()


@pytest.fixture
def index_home(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    monkeypatch.setenv("HOME", str(tmp_path))
    return tmp_path


def test_index_repo_registers_and_indexes(runner: CliRunner, index_home: Path) -> None:
    """nx index repo <path> registers the repo and calls index_repository."""
    repo = index_home / "myrepo"
    repo.mkdir()

    mock_reg = MagicMock()
    mock_reg.get.return_value = None  # not yet registered

    with patch("nexus.commands.index._registry", return_value=mock_reg):
        with patch("nexus.indexer.index_repository") as mock_index:
            result = runner.invoke(main, ["index", "repo", str(repo)])

    assert result.exit_code == 0
    mock_reg.add.assert_called_once()
    mock_index.assert_called_once()
    assert "Registered" in result.output
    assert "Done" in result.output


def test_index_repo_idempotent_when_already_registered(runner: CliRunner, index_home: Path) -> None:
    """If repo is already registered, skip add() and just re-index."""
    repo = index_home / "myrepo"
    repo.mkdir()

    mock_reg = MagicMock()
    mock_reg.get.return_value = {"collection": "code__myrepo"}  # already registered

    with patch("nexus.commands.index._registry", return_value=mock_reg):
        with patch("nexus.indexer.index_repository") as mock_index:
            result = runner.invoke(main, ["index", "repo", str(repo)])

    assert result.exit_code == 0
    mock_reg.add.assert_not_called()
    mock_index.assert_called_once()
    assert "Registered" not in result.output


def test_index_repo_invalid_path(runner: CliRunner, index_home: Path) -> None:
    """Non-existent path produces a non-zero exit code."""
    result = runner.invoke(main, ["index", "repo", str(index_home / "nonexistent")])
    assert result.exit_code != 0


# ── nx index pdf ──────────────────────────────────────────────────────────────

def test_index_pdf_command_indexes_file(runner: CliRunner, index_home: Path) -> None:
    """nx index pdf <path> calls index_pdf and reports chunk count."""
    pdf = index_home / "doc.pdf"
    pdf.write_bytes(b"fake pdf")

    with patch("nexus.doc_indexer.index_pdf", return_value=3) as mock_index:
        result = runner.invoke(main, ["index", "pdf", str(pdf)])

    assert result.exit_code == 0, result.output
    mock_index.assert_called_once()
    assert "3" in result.output


def test_index_pdf_nonexistent_path_fails(runner: CliRunner, index_home: Path) -> None:
    """Non-existent PDF path produces a non-zero exit code."""
    result = runner.invoke(main, ["index", "pdf", str(index_home / "missing.pdf")])
    assert result.exit_code != 0


# ── nx index md ───────────────────────────────────────────────────────────────

def test_index_md_command_indexes_file(runner: CliRunner, index_home: Path) -> None:
    """nx index md <path> calls index_markdown and reports chunk count."""
    md = index_home / "doc.md"
    md.write_text("# Hello\n\nWorld.\n")

    with patch("nexus.doc_indexer.index_markdown", return_value=2) as mock_index:
        result = runner.invoke(main, ["index", "md", str(md)])

    assert result.exit_code == 0, result.output
    mock_index.assert_called_once()
    assert "2" in result.output


def test_index_md_nonexistent_path_fails(runner: CliRunner, index_home: Path) -> None:
    """Non-existent markdown path produces a non-zero exit code."""
    result = runner.invoke(main, ["index", "md", str(index_home / "missing.md")])
    assert result.exit_code != 0


# ── --frecency-only flag ──────────────────────────────────────────────────────

def test_index_repo_frecency_only_flag_passed_through(runner: CliRunner, index_home: Path) -> None:
    """nx index repo <path> --frecency-only passes frecency_only=True to index_repository."""
    repo = index_home / "myrepo"
    repo.mkdir()

    mock_reg = MagicMock()
    mock_reg.get.return_value = {"collection": "code__myrepo"}  # already registered

    with patch("nexus.commands.index._registry", return_value=mock_reg):
        with patch("nexus.indexer.index_repository") as mock_index:
            result = runner.invoke(main, ["index", "repo", str(repo), "--frecency-only"])

    assert result.exit_code == 0, result.output
    mock_index.assert_called_once()
    _, call_kwargs = mock_index.call_args
    assert call_kwargs.get("frecency_only") is True
    assert "frecency" in result.output.lower()


def test_index_repo_default_is_full_index(runner: CliRunner, index_home: Path) -> None:
    """nx index repo <path> without --frecency-only passes frecency_only=False."""
    repo = index_home / "myrepo"
    repo.mkdir()

    mock_reg = MagicMock()
    mock_reg.get.return_value = {"collection": "code__myrepo"}

    with patch("nexus.commands.index._registry", return_value=mock_reg):
        with patch("nexus.indexer.index_repository") as mock_index:
            result = runner.invoke(main, ["index", "repo", str(repo)])

    assert result.exit_code == 0, result.output
    mock_index.assert_called_once()
    _, call_kwargs = mock_index.call_args
    assert call_kwargs.get("frecency_only") is False


# ── --force flag ──────────────────────────────────────────────────────────────


def test_index_repo_force_flag_passed_through(runner: CliRunner, index_home: Path) -> None:
    """nx index repo <path> --force passes force=True to index_repository."""
    repo = index_home / "myrepo"
    repo.mkdir()

    mock_reg = MagicMock()
    mock_reg.get.return_value = {"collection": "code__myrepo"}

    with patch("nexus.commands.index._registry", return_value=mock_reg):
        with patch("nexus.indexer.index_repository") as mock_index:
            result = runner.invoke(main, ["index", "repo", str(repo), "--force"])

    assert result.exit_code == 0, result.output
    mock_index.assert_called_once()
    _, call_kwargs = mock_index.call_args
    assert call_kwargs.get("force") is True


def test_index_repo_force_frecency_mutual_exclusion(runner: CliRunner, index_home: Path) -> None:
    """--force and --frecency-only are mutually exclusive on nx index repo."""
    repo = index_home / "myrepo"
    repo.mkdir()

    mock_reg = MagicMock()
    mock_reg.get.return_value = {"collection": "code__myrepo"}

    with patch("nexus.commands.index._registry", return_value=mock_reg):
        result = runner.invoke(main, ["index", "repo", str(repo), "--force", "--frecency-only"])

    assert result.exit_code != 0
    assert "mutually exclusive" in result.output.lower()


def test_index_repo_force_output_message(runner: CliRunner, index_home: Path) -> None:
    """nx index repo <path> --force prints 'Force-indexing' instead of 'Indexing'."""
    repo = index_home / "myrepo"
    repo.mkdir()

    mock_reg = MagicMock()
    mock_reg.get.return_value = {"collection": "code__myrepo"}

    with patch("nexus.commands.index._registry", return_value=mock_reg):
        with patch("nexus.indexer.index_repository"):
            result = runner.invoke(main, ["index", "repo", str(repo), "--force"])

    assert result.exit_code == 0, result.output
    assert "Force-indexing" in result.output


def test_index_pdf_force_flag(runner: CliRunner, index_home: Path) -> None:
    """nx index pdf <path> --force passes force=True to index_pdf."""
    pdf = index_home / "doc.pdf"
    pdf.write_bytes(b"fake pdf")

    with patch("nexus.doc_indexer.index_pdf", return_value=5) as mock_index:
        result = runner.invoke(main, ["index", "pdf", str(pdf), "--force"])

    assert result.exit_code == 0, result.output
    mock_index.assert_called_once()
    _, call_kwargs = mock_index.call_args
    assert call_kwargs.get("force") is True


def test_index_pdf_force_dry_run_mutual_exclusion(runner: CliRunner, index_home: Path) -> None:
    """--force and --dry-run are mutually exclusive on nx index pdf."""
    pdf = index_home / "doc.pdf"
    pdf.write_bytes(b"fake pdf")

    result = runner.invoke(main, ["index", "pdf", str(pdf), "--force", "--dry-run"])

    assert result.exit_code != 0
    assert "mutually exclusive" in result.output.lower()


def test_index_md_force_flag(runner: CliRunner, index_home: Path) -> None:
    """nx index md <path> --force passes force=True to index_markdown."""
    md = index_home / "doc.md"
    md.write_text("# Hello\n\nWorld.\n")

    with patch("nexus.doc_indexer.index_markdown", return_value=2) as mock_index:
        result = runner.invoke(main, ["index", "md", str(md), "--force"])

    assert result.exit_code == 0, result.output
    mock_index.assert_called_once()
    _, call_kwargs = mock_index.call_args
    assert call_kwargs.get("force") is True


# ── --monitor flag ─────────────────────────────────────────────────────────────

@pytest.mark.parametrize("subcmd,extra_args", [
    ("repo", []),
    ("rdr", []),
    ("pdf", []),
    ("md", []),
])
def test_monitor_flag_accepted(
    runner: CliRunner, index_home: Path, subcmd: str, extra_args: list[str]
) -> None:
    """--monitor flag is accepted by all four index subcommands (exit 0)."""
    if subcmd == "repo":
        target = index_home / "myrepo"
        target.mkdir()
    elif subcmd in ("pdf", "md"):
        target = index_home / f"doc.{subcmd}"
        target.write_bytes(b"fake")
    else:
        target = index_home / "myrepo"
        rdr_dir = target / "docs" / "rdr"
        rdr_dir.mkdir(parents=True)
        (rdr_dir / "001.md").write_text("# RDR\n")

    mock_target = {
        "repo": "nexus.indexer.index_repository",
        "rdr": "nexus.doc_indexer.batch_index_markdowns",
        "pdf": "nexus.doc_indexer.index_pdf",
        "md": "nexus.doc_indexer.index_markdown",
    }[subcmd]
    # pdf/md with --monitor calls return_metadata=True, so mock must return a dict
    mock_rv: dict | int
    if subcmd in ("repo", "rdr"):
        mock_rv = {}
    elif subcmd == "pdf":
        mock_rv = {"chunks": 0, "pages": [], "title": "", "author": ""}
    else:
        mock_rv = {"chunks": 0, "sections": 0}

    patches = [patch(mock_target, return_value=mock_rv)]
    if subcmd == "repo":
        mock_reg = MagicMock()
        mock_reg.get.return_value = {"collection": "code__x"}
        patches.append(patch("nexus.commands.index._registry", return_value=mock_reg))

    with contextlib.ExitStack() as stack:
        for p in patches:
            stack.enter_context(p)
        result = runner.invoke(main, ["index", subcmd, str(target), "--monitor"] + extra_args)

    assert result.exit_code == 0, f"{subcmd}: {result.output}"


# ── index_repo_cmd monitor behaviour ──────────────────────────────────────────

def test_repo_callbacks_always_passed(runner: CliRunner, index_home: Path) -> None:
    """index_repository is called with on_start and on_file callables (even without --monitor)."""
    repo = index_home / "myrepo"
    repo.mkdir()
    mock_reg = MagicMock()
    mock_reg.get.return_value = {"collection": "code__myrepo"}

    with patch("nexus.commands.index._registry", return_value=mock_reg):
        with patch("nexus.indexer.index_repository") as mock_index:
            result = runner.invoke(main, ["index", "repo", str(repo)])

    assert result.exit_code == 0, result.output
    _, kwargs = mock_index.call_args
    assert callable(kwargs.get("on_start")), "on_start must be callable"
    assert callable(kwargs.get("on_file")), "on_file must be callable"


def test_repo_monitor_nontty_output_format(runner: CliRunner, index_home: Path) -> None:
    """With --monitor in non-TTY, output contains [N/total] lines."""
    repo = index_home / "myrepo"
    repo.mkdir()
    mock_reg = MagicMock()
    mock_reg.get.return_value = {"collection": "code__myrepo"}

    def fake_index(path, reg, **kwargs):
        on_start = kwargs.get("on_start")
        on_file = kwargs.get("on_file")
        if on_start:
            on_start(2)
        if on_file:
            on_file(Path("a.py"), 5, 0.1)
            on_file(Path("b.py"), 0, 0.05)
        return {}

    with patch("nexus.commands.index._registry", return_value=mock_reg):
        with patch("nexus.indexer.index_repository", side_effect=fake_index):
            result = runner.invoke(main, ["index", "repo", str(repo), "--monitor"])

    assert result.exit_code == 0, result.output
    assert "[1/2]" in result.output
    assert "[2/2]" in result.output


def test_repo_monitor_skipped_label(runner: CliRunner, index_home: Path) -> None:
    """on_file with chunks=0 produces 'skipped' in monitor output."""
    repo = index_home / "myrepo"
    repo.mkdir()
    mock_reg = MagicMock()
    mock_reg.get.return_value = {"collection": "code__myrepo"}

    def fake_index(path, reg, **kwargs):
        if kwargs.get("on_start"):
            kwargs["on_start"](1)
        if kwargs.get("on_file"):
            kwargs["on_file"](Path("skip.py"), 0, 0.02)
        return {}

    with patch("nexus.commands.index._registry", return_value=mock_reg):
        with patch("nexus.indexer.index_repository", side_effect=fake_index):
            result = runner.invoke(main, ["index", "repo", str(repo), "--monitor"])

    assert result.exit_code == 0, result.output
    assert "skipped" in result.output


def test_repo_monitor_chunks_label(runner: CliRunner, index_home: Path) -> None:
    """on_file with chunks>0 produces 'N chunks' in monitor output."""
    repo = index_home / "myrepo"
    repo.mkdir()
    mock_reg = MagicMock()
    mock_reg.get.return_value = {"collection": "code__myrepo"}

    def fake_index(path, reg, **kwargs):
        if kwargs.get("on_start"):
            kwargs["on_start"](1)
        if kwargs.get("on_file"):
            kwargs["on_file"](Path("code.py"), 7, 0.3)
        return {}

    with patch("nexus.commands.index._registry", return_value=mock_reg):
        with patch("nexus.indexer.index_repository", side_effect=fake_index):
            result = runner.invoke(main, ["index", "repo", str(repo), "--monitor"])

    assert result.exit_code == 0, result.output
    assert "7 chunks" in result.output


def test_repo_monitor_nontty_no_cr(runner: CliRunner, index_home: Path) -> None:
    """Non-TTY monitor output contains no carriage return characters."""
    repo = index_home / "myrepo"
    repo.mkdir()
    mock_reg = MagicMock()
    mock_reg.get.return_value = {"collection": "code__myrepo"}

    def fake_index(path, reg, **kwargs):
        if kwargs.get("on_start"):
            kwargs["on_start"](1)
        if kwargs.get("on_file"):
            kwargs["on_file"](Path("f.py"), 3, 0.1)
        return {}

    with patch("nexus.commands.index._registry", return_value=mock_reg):
        with patch("nexus.indexer.index_repository", side_effect=fake_index):
            result = runner.invoke(main, ["index", "repo", str(repo), "--monitor"])

    assert result.exit_code == 0, result.output
    assert "\r" not in result.output


# ── index_rdr_cmd monitor behaviour ───────────────────────────────────────────

def test_rdr_monitor_on_file_passed(runner: CliRunner, index_home: Path) -> None:
    """With --monitor, batch_index_markdowns is called with on_file kwarg."""
    repo = index_home / "myrepo"
    rdr_dir = repo / "docs" / "rdr"
    rdr_dir.mkdir(parents=True)
    (rdr_dir / "001.md").write_text("# RDR\n")

    with patch("nexus.doc_indexer.batch_index_markdowns", return_value={}) as mock_batch:
        result = runner.invoke(main, ["index", "rdr", str(repo), "--monitor"])

    assert result.exit_code == 0, result.output
    _, kwargs = mock_batch.call_args
    assert callable(kwargs.get("on_file")), "on_file must be callable"


def test_rdr_monitor_bar_total(runner: CliRunner, index_home: Path) -> None:
    """tqdm bar is created with total=len(rdr_files)."""
    repo = index_home / "myrepo"
    rdr_dir = repo / "docs" / "rdr"
    rdr_dir.mkdir(parents=True)
    (rdr_dir / "001.md").write_text("# A\n")
    (rdr_dir / "002.md").write_text("# B\n")
    (rdr_dir / "003.md").write_text("# C\n")

    with patch("nexus.doc_indexer.batch_index_markdowns", return_value={}):
        with patch("nexus.commands.index.tqdm") as mock_tqdm:
            mock_tqdm.return_value = MagicMock()
            result = runner.invoke(main, ["index", "rdr", str(repo), "--monitor"])

    assert result.exit_code == 0, result.output
    mock_tqdm.assert_called_once()
    call_args = mock_tqdm.call_args
    total = call_args[1].get("total") if call_args[1] else call_args[0][0] if call_args[0] else None
    assert total == 3, f"expected total=3, got {total}"


# ── index_pdf_cmd and index_md_cmd monitor behaviour ──────────────────────────

def test_pdf_monitor_return_metadata(runner: CliRunner, index_home: Path) -> None:
    """With --monitor, index_pdf is called with return_metadata=True."""
    pdf = index_home / "doc.pdf"
    pdf.write_bytes(b"fake pdf")

    mock_result = {"chunks": 3, "pages": [1, 2, 3], "title": "Test", "author": "Author"}
    with patch("nexus.doc_indexer.index_pdf", return_value=mock_result) as mock_index:
        result = runner.invoke(main, ["index", "pdf", str(pdf), "--monitor"])

    assert result.exit_code == 0, result.output
    _, kwargs = mock_index.call_args
    assert kwargs.get("return_metadata") is True
    assert "Chunks: 3" in result.output


def test_md_monitor_return_metadata(runner: CliRunner, index_home: Path) -> None:
    """With --monitor, index_markdown is called with return_metadata=True."""
    md = index_home / "doc.md"
    md.write_text("# Hello\n\nWorld.\n")

    mock_result = {"chunks": 2, "sections": 1}
    with patch("nexus.doc_indexer.index_markdown", return_value=mock_result) as mock_index:
        result = runner.invoke(main, ["index", "md", str(md), "--monitor"])

    assert result.exit_code == 0, result.output
    _, kwargs = mock_index.call_args
    assert kwargs.get("return_metadata") is True
    assert "Chunks: 2" in result.output
    assert "Sections: 1" in result.output
