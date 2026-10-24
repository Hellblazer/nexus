# SPDX-License-Identifier: AGPL-3.0-or-later
# Copyright (c) 2026 Hal Hildebrand. All rights reserved.
"""`nx index repo` with the PDF stack hidden and installs refused: behaviour,
not source text. nexus-2k66t.4.2.

The hold has four effects that a refusal must produce together, and the first
version of these tests pinned them with ``inspect.getsource`` substring checks,
which three mutations left green:

* the run's stats carry ``pdf_stack_held`` (the CLI's exit 69 reads it),
* no PDF is registered in the catalog or handed to the PDF indexer,
* the catalog's orphan housekeeping still sees the held PDF as present
  (``keep=``), so two refused runs cannot age its row toward deletion,
* the ``--since-head`` base is not advanced past the commit that brought the PDF.

``_run_index`` runs for real here against a git-less temp repo with the same
service-mode doubles tests/test_deyd5_systemic_skip_run_level.py uses; the
stack is hidden by making ``pdf_stack.state()`` report absent and the real
``ensure`` runs under ``NX_PDF_STACK=refuse``.
"""
from __future__ import annotations

from contextlib import contextmanager
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest
from click.testing import CliRunner

from nexus import pdf_stack

_DEFAULT_CONFIG = {
    "server": {"ignorePatterns": []},
    "indexing": {
        "code_extensions": [],
        "prose_extensions": [],
        "rdr_paths": ["docs/rdr"],
        "include_untracked": False,
    },
}


def _reg():
    m = MagicMock()
    m.get.return_value = {
        "collection": "code__repo",
        "code_collection": "code__repo__voyage-code-3__v1",
        "docs_collection": "docs__repo__voyage-context-3__v1",
    }
    return m


@pytest.fixture(autouse=True)
def _env(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    monkeypatch.setenv("NEXUS_CONFIG_DIR", str(tmp_path / "cfg"))
    monkeypatch.setenv("NX_STORAGE_BACKEND_VECTORS", "service")
    monkeypatch.setenv("NX_LOCAL", "0")
    monkeypatch.setenv("VOYAGE_API_KEY", "fake")
    monkeypatch.setenv("CHROMA_API_KEY", "fake")
    monkeypatch.delenv(pdf_stack.REEXEC_ENV, raising=False)
    pdf_stack.reset_cache()


def _hide_stack(monkeypatch: pytest.MonkeyPatch, *, policy: str = "refuse") -> None:
    monkeypatch.setenv(pdf_stack.POLICY_ENV, policy)
    monkeypatch.setattr(pdf_stack, "state", lambda: pdf_stack.StackState("absent", "docling, MinerU and torch are not installed"))
    monkeypatch.setattr(
        pdf_stack, "install_plan",
        lambda: pdf_stack.InstallPlan("generation", True, pdf_stack.INSTALL_COMMAND),
    )


@pytest.fixture
def repo(tmp_path: Path) -> Path:
    r = tmp_path / "repo"
    r.mkdir()
    (r / "ok.py").write_text("x = 1\n")
    (r / "paper.pdf").write_bytes(b"%PDF-1.4 fake content")
    return r


def _commit_all(repo: Path) -> None:
    import subprocess

    def run_git(*args: str) -> None:
        subprocess.run(["git", "-C", str(repo), *args], check=True, capture_output=True, text=True)

    run_git("init", "-q", "-b", "main")
    run_git("config", "user.email", "t@t")
    run_git("config", "user.name", "t")
    run_git("add", "ok.py", "paper.pdf")
    run_git("commit", "-q", "-m", "base")


class _Run:
    """What the doubles saw."""

    def __init__(self) -> None:
        self.registered: list[Path] = []
        self.housekeeping_keep: list[Path] | None = None
        self.hook_calls = 0


@contextmanager
def _doubles(run: _Run, db):
    def _catalog_hook(**kw):
        run.hook_calls += 1
        run.registered = [p for p, _t, _c in kw["indexed_files"]]
        run.housekeeping_keep = kw.get("housekeeping_keep")
        return {p: f"1.1.{i}" for i, (p, _t, _c) in enumerate(kw["indexed_files"], 1)}

    patches = {
        "nexus.frecency.batch_frecency": {"return_value": {}},
        "nexus.indexer._git_metadata": {"return_value": {}},
        "nexus.config.load_config": {"return_value": _DEFAULT_CONFIG},
        "nexus.config.get_credential": {"return_value": "fake-key"},
        "nexus.mcp_infra.get_t3": {"return_value": db},
        "nexus.db.make_t3": {"return_value": db},
        "nexus.indexer._index_code_file": {"return_value": 1},
        "nexus.indexer._index_prose_file": {"return_value": 0},
        "nexus.indexer._index_pdf_file": {"return_value": 1},
        "nexus.indexer._prune_misclassified": {},
        "nexus.indexer._prune_deleted_files": {},
        "nexus.indexer._migrate_legacy_collections": {"return_value": {}},
        "nexus.catalog.factory.make_catalog_reader": {"return_value": None},
        "nexus.catalog.factory.make_catalog_writer": {"return_value": None},
        "nexus.indexer._catalog_hook": {"side_effect": _catalog_hook},
        "nexus.corpus.ensure_collection_registered": {},
    }
    started, mocks = [], {}
    for target, kw in patches.items():
        p = patch(target, **kw)
        mocks[target.rsplit(".", 1)[-1]] = p.start()
        started.append(p)
    try:
        yield mocks
    finally:
        for p in reversed(started):
            p.stop()


def _db():
    from nexus.db.http_vector_client import HttpVectorClient

    return MagicMock(spec=HttpVectorClient)


def test_run_index_under_refuse_holds_the_pdf_and_still_indexes_code(repo, monkeypatch) -> None:
    from nexus.indexer import _run_index

    _hide_stack(monkeypatch)
    run, phases = _Run(), []
    with _doubles(run, _db()) as mocks:
        stats = _run_index(repo, _reg(), on_phase=phases.append)

    assert stats["pdf_stack_held"] == 1
    mocks["_index_pdf_file"].assert_not_called()
    assert run.registered == [repo / "ok.py"], "the PDF must not be offered to the catalog hook"
    assert mocks["_index_code_file"].call_count == 1, "code is indexed past a held PDF"
    assert run.housekeeping_keep == [repo / "paper.pdf"], "orphan housekeeping must still see the held PDF as present"
    assert any("NOT indexed" in p and pdf_stack.INSTALL_COMMAND in p for p in phases)
    assert pdf_stack.skipped_count() == 1


def test_run_index_with_the_stack_present_indexes_the_pdf_and_holds_nothing(repo, monkeypatch) -> None:
    """The control for the test above: same repo, same doubles, stack present."""
    from nexus.indexer import _run_index

    monkeypatch.setattr(pdf_stack, "state", lambda: pdf_stack.StackState("present", ""))
    run = _Run()
    with _doubles(run, _db()) as mocks:
        stats = _run_index(repo, _reg())

    assert stats["pdf_stack_held"] == 0
    assert mocks["_index_pdf_file"].call_count == 1
    assert repo / "paper.pdf" in run.registered
    assert run.housekeeping_keep == []


def test_run_index_unwinds_for_a_rerun_after_an_install_and_registers_nothing(repo, monkeypatch) -> None:
    from nexus.indexer import _run_index

    _hide_stack(monkeypatch, policy="install")
    monkeypatch.setattr(pdf_stack, "_install_generation", lambda **kw: None)
    monkeypatch.setattr(pdf_stack, "_windows_runtime_missing", lambda: False)
    monkeypatch.setattr(pdf_stack, "current_generation_has_stack", lambda: False)
    run = _Run()
    with _doubles(run, _db()) as mocks, pytest.raises(pdf_stack.RerunRequired):
        _run_index(repo, _reg())
    mocks["_index_pdf_file"].assert_not_called()
    assert run.hook_calls == 0, "nothing is registered before the re-run in the new generation"


def test_the_since_head_base_does_not_advance_past_a_held_pdf(repo, monkeypatch) -> None:
    """index_repository advances owners.head_hash only on a run that offered every
    changed file; a held PDF must stay in the next --since-head diff."""
    from nexus import indexer as idx

    advanced: list[str] = []
    monkeypatch.setattr(idx, "_set_owner_head_hash", lambda repo_, head, **kw: advanced.append(head))
    monkeypatch.setattr(idx, "_current_head", lambda repo_: "HEAD1")
    monkeypatch.setattr(idx, "_relink_changed_pdfs", lambda state: None)

    monkeypatch.setattr(idx, "_run_index", lambda *a, **kw: {"pdf_stack_held": 1, "files_changed": 1})
    idx.index_repository(repo, _reg(), since_head=True)
    assert advanced == [], "the base moved past a commit whose PDF was held"

    monkeypatch.setattr(idx, "_run_index", lambda *a, **kw: {"pdf_stack_held": 0, "files_changed": 1})
    idx.index_repository(repo, _reg(), since_head=True)
    assert advanced == ["HEAD1"], "control: a clean run advances the base"


def test_the_cli_exits_69_after_code_and_prose_with_the_command_in_the_output(repo, monkeypatch) -> None:
    """The whole path through `nx index repo`: refuse, code indexed, Done., exit 69."""
    from nexus.cli import main

    _hide_stack(monkeypatch)
    _commit_all(repo)  # the CLI wants a repository root, and the walk lists tracked files
    monkeypatch.setattr("nexus.mcp_infra._engine_file_key", lambda: "test")
    monkeypatch.setattr("nexus.mcp_infra.engine_process_uptime_seconds", lambda *a, **k: None)
    for name in ("run_collection_postprocessing", "_drain_repo_collections", "_refresh_derived_taxonomy"):
        monkeypatch.setattr(f"nexus.commands.index.{name}", lambda *a, **k: None)
    monkeypatch.setattr("nexus.commands.index._collections_without_topics", lambda *a, **k: set())
    monkeypatch.setattr("nexus.commands.index._collections_with_failed_discover", lambda *a, **k: set())
    monkeypatch.setattr("nexus.commands.index._any_collection_has_topics", lambda *a, **k: True)
    monkeypatch.setattr("nexus.indexer._set_owner_head_hash", lambda *a, **k: None)
    monkeypatch.setattr("nexus.indexer._relink_changed_pdfs", lambda state: None)
    run = _Run()
    with _doubles(run, _db()) as mocks, patch("nexus.commands.index._registry", return_value=_reg()):
        result = CliRunner().invoke(main, ["index", "repo", str(repo)])

    assert result.exit_code == 69, result.output
    assert mocks["_index_code_file"].call_count == 1, "code is indexed before the exit"
    mocks["_index_pdf_file"].assert_not_called()
    assert "1 PDF(s) were NOT indexed" in result.output
    assert "nx self install --extras pdf" in result.output
    assert "Done." in result.output


# ── the offline box: a network or installer failure names the way through ────


@pytest.mark.parametrize("failure, code", [
    ("generation build failed:\nerror: Failed to fetch: dns error: failed to lookup address", 75),
    ("generation build failed:\nNo solution found when resolving dependencies", 70),
])
def test_index_repo_install_failure_names_the_refuse_escape_for_code_and_prose(monkeypatch, capsys, repo, failure, code) -> None:
    import click

    from nexus.indexer import _hold_pdfs_without_stack

    _hide_stack(monkeypatch, policy="install")
    monkeypatch.setattr(pdf_stack, "_windows_runtime_missing", lambda: False)
    monkeypatch.setattr(pdf_stack, "current_generation_has_stack", lambda: False)

    def _boom(**kw):
        raise click.ClickException(failure)

    monkeypatch.setattr(pdf_stack, "_install_generation", _boom)
    with pytest.raises(SystemExit) as exit_:
        _hold_pdfs_without_stack([repo / "paper.pdf"], repo)
    assert exit_.value.code == code
    err = capsys.readouterr().err
    assert "NX_PDF_STACK=refuse" in err
    assert "index code and prose" in err
    assert pdf_stack.INSTALL_COMMAND in err


def test_commands_that_cannot_hold_do_not_get_the_refuse_hint(monkeypatch, capsys) -> None:
    """`nx index pdf` has nothing else to index: the escape would be false advice."""
    import click

    _hide_stack(monkeypatch, policy="install")
    monkeypatch.setattr(pdf_stack, "_windows_runtime_missing", lambda: False)
    monkeypatch.setattr(pdf_stack, "current_generation_has_stack", lambda: False)
    monkeypatch.setattr(pdf_stack, "_install_generation", lambda **kw: (_ for _ in ()).throw(click.ClickException("dns error")))
    with pytest.raises(SystemExit) as exit_:
        pdf_stack.ensure_or_exit("nx index pdf")
    assert exit_.value.code == 75
    assert "NX_PDF_STACK=refuse" not in capsys.readouterr().err
