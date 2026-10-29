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
from nexus.commands import self_cmd

_REAL_INSTALL_PLAN = pdf_stack.install_plan

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
    monkeypatch.setattr(pdf_stack, "state", lambda: pdf_stack.StackState("absent", "MinerU and torch are not installed"))
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
    assert pdf_stack.skipped_count(pdf_stack.SKIP_CAUSE_STACK) == 1
    assert pdf_stack.skipped_count(pdf_stack.SKIP_CAUSE_BOUNDS) == 0


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


def _index_repo_cli(repo, monkeypatch):
    """Run `nx index repo` for real under the hidden stack; returns ``(result, mocks)``."""
    from nexus.cli import main

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
    return result, mocks


def test_the_cli_exits_69_after_code_and_prose_with_the_command_in_the_output(repo, monkeypatch) -> None:
    """The whole path through `nx index repo`: refuse, code indexed, Done., exit 69."""
    _hide_stack(monkeypatch)
    result, mocks = _index_repo_cli(repo, monkeypatch)

    assert result.exit_code == 69, result.output
    assert mocks["_index_code_file"].call_count == 1, "code is indexed before the exit"
    mocks["_index_pdf_file"].assert_not_called()
    assert "1 PDF(s) were NOT indexed" in result.output
    assert "nx self install --extras pdf" in result.output
    assert "Done." in result.output


def test_the_cli_names_the_legacy_trees_repair_not_the_generation_only_one(repo, monkeypatch) -> None:
    """On a uv tool tree `--extras` is refused ("this nx is not running from one"), so the run
    failure line must carry the plan's plain `nx self install` (7.86.1 shakeout, nexus-11x9o)."""
    _hide_stack(monkeypatch)
    monkeypatch.setattr(pdf_stack, "install_plan", _REAL_INSTALL_PLAN)  # the real plan, over a legacy layout
    monkeypatch.setattr(self_cmd, "running_generation", lambda: Path("/uv/tools/conexus"))
    monkeypatch.setattr(self_cmd, "_running_from_legacy_tool_install", lambda: True)
    result, _mocks = _index_repo_cli(repo, monkeypatch)

    assert result.exit_code == 69, result.output
    assert "Run: nx self install   then re-run `nx index repo`." in result.output
    assert "--extras pdf" not in result.output


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


# ── an indexed, unchanged PDF does not cost an install (nexus-2k66t.4.8) ─────


def _ensure_spy(monkeypatch: pytest.MonkeyPatch) -> list[str]:
    calls: list[str] = []

    def ensure(entry: str):
        calls.append(entry)
        return pdf_stack.Outcome.PRESENT

    monkeypatch.setattr(pdf_stack, "ensure", ensure)
    return calls


def test_the_gate_skips_the_install_when_the_probe_finds_every_pdf_current(monkeypatch, repo) -> None:
    from nexus.indexer import _hold_pdfs_without_stack

    _hide_stack(monkeypatch, policy="install")
    calls = _ensure_spy(monkeypatch)
    held = _hold_pdfs_without_stack([repo / "paper.pdf"], repo, probe=lambda paths: [])
    assert held == []
    assert calls == [], "nothing needs extracting, so nothing needs the stack"


def test_the_gate_installs_when_the_probe_finds_one_pdf_needing_work(monkeypatch, repo) -> None:
    from nexus.indexer import _hold_pdfs_without_stack

    _hide_stack(monkeypatch, policy="install")
    calls = _ensure_spy(monkeypatch)
    pdfs = [repo / "old.pdf", repo / "paper.pdf"]
    assert _hold_pdfs_without_stack(pdfs, repo, probe=lambda paths: [repo / "paper.pdf"]) == []
    assert calls == ["nx index repo"]


def test_a_refused_install_holds_only_the_pdfs_the_probe_says_need_work(monkeypatch, repo) -> None:
    from nexus.indexer import _hold_pdfs_without_stack

    _hide_stack(monkeypatch)  # refuse
    pdfs = [repo / "old.pdf", repo / "paper.pdf"]
    held = _hold_pdfs_without_stack(pdfs, repo, probe=lambda paths: [repo / "paper.pdf"])
    assert held == [repo / "paper.pdf"]
    assert pdf_stack.skipped_count() == 1


class _Entry:
    def __init__(self, file_path: str, state: str, content_hash: str, *, reported: bool = True) -> None:
        self.file_path = file_path
        self.index_state = state
        self.index_content_hash = content_hash
        self.index_state_reported = reported


class _Reader:
    def __init__(self, entries: list[_Entry], *, owner: object | None = "1.1") -> None:
        self._entries, self._owner = entries, owner

    def owner_for_repo(self, repo_hash: str):
        return self._owner

    def by_owner(self, owner):
        return list(self._entries)


def _sha(path: Path) -> str:
    import hashlib

    return hashlib.sha256(path.read_bytes()).hexdigest()


def test_the_probe_clears_only_a_pdf_the_catalog_fenced_complete_at_its_current_hash(monkeypatch, repo) -> None:
    from nexus.indexer import _pdfs_needing_index

    current, changed, mid_run, unknown, legacy = (repo / n for n in ("a.pdf", "b.pdf", "c.pdf", "d.pdf", "e.pdf"))
    for p in (current, changed, mid_run, unknown, legacy):
        p.write_bytes(b"%PDF " + p.name.encode())
    reader = _Reader([
        _Entry("a.pdf", "complete", _sha(current)),
        _Entry("b.pdf", "complete", "0" * 64),
        _Entry("c.pdf", "indexing", _sha(mid_run)),
        _Entry("e.pdf", "complete", _sha(legacy), reported=False),
    ])
    monkeypatch.setattr("nexus.catalog.factory.make_catalog_reader", lambda: reader)
    needing = _pdfs_needing_index([current, changed, mid_run, unknown, legacy], repo)
    assert needing == [changed, mid_run, unknown, legacy]


@pytest.mark.parametrize("reader", [None, _Reader([], owner=None)])
def test_the_probe_treats_an_unreadable_or_empty_catalog_as_everything_needing_work(monkeypatch, repo, reader) -> None:
    from nexus.indexer import _pdfs_needing_index

    monkeypatch.setattr("nexus.catalog.factory.make_catalog_reader", lambda: reader)
    assert _pdfs_needing_index([repo / "paper.pdf"], repo) == [repo / "paper.pdf"]


def test_the_probe_treats_a_catalog_error_as_everything_needing_work(monkeypatch, repo) -> None:
    from nexus.indexer import _pdfs_needing_index

    def boom():
        raise RuntimeError("service down")

    monkeypatch.setattr("nexus.catalog.factory.make_catalog_reader", boom)
    assert _pdfs_needing_index([repo / "paper.pdf"], repo) == [repo / "paper.pdf"]


def test_run_index_with_every_pdf_current_needs_no_stack_and_still_dispatches_them(repo, monkeypatch) -> None:
    """Stack absent, installs refused, every PDF fenced current: no hold, no exit 69,
    the PDF goes to the registrar and the indexer (which skips it as current)."""
    from nexus.indexer import _run_index

    _hide_stack(monkeypatch)
    run = _Run()
    with _doubles(run, _db()) as mocks, patch("nexus.indexer._pdfs_needing_index", return_value=[]), \
            patch("nexus.indexer.check_staleness", return_value=True):
        stats = _run_index(repo, _reg())

    assert stats["pdf_stack_held"] == 0
    assert repo / "paper.pdf" in run.registered
    assert mocks["_index_pdf_file"].call_count == 1


def test_a_pdf_the_probe_cleared_but_the_staleness_cache_calls_stale_is_still_held(repo, monkeypatch) -> None:
    """The probe reads the catalog fence only. A model change leaves the fence
    complete while the chunks are stale; extraction would then hit the library
    backstop mid-run. The run re-checks with the real staleness cache and holds."""
    from nexus.indexer import _run_index

    _hide_stack(monkeypatch)
    run, phases = _Run(), []
    with _doubles(run, _db()) as mocks, patch("nexus.indexer._pdfs_needing_index", return_value=[]), \
            patch("nexus.indexer.check_staleness", return_value=False):
        stats = _run_index(repo, _reg(), on_phase=phases.append)

    assert stats["pdf_stack_held"] == 1
    mocks["_index_pdf_file"].assert_not_called()
    assert mocks["_index_code_file"].call_count == 1
    assert any("NOT indexed" in p for p in phases)


def test_force_bypasses_the_probe_because_every_pdf_is_re_extracted(repo, monkeypatch) -> None:
    from nexus.indexer import _run_index

    _hide_stack(monkeypatch)
    run = _Run()
    with _doubles(run, _db()) as mocks, patch("nexus.indexer._pdfs_needing_index", return_value=[]):
        stats = _run_index(repo, _reg(), force=True)

    assert stats["pdf_stack_held"] == 1
    mocks["_index_pdf_file"].assert_not_called()


# ── the late recheck: merge, vanished file, install policy (nexus-2k66t.4.8 fix round) ──


def test_a_late_hold_merges_with_the_early_one_and_the_refusal_prints_once(repo, monkeypatch, capsys) -> None:
    """The probe holds a.pdf early; b.pdf is cleared by the probe but stale in the real
    cache, so it is held late. The skipped record is the union (nx doctor counts both),
    the stats count both, and the refusal line is not repeated."""
    from nexus.indexer import _run_index

    early = repo / "a.pdf"
    early.write_bytes(b"%PDF-1.4 early")
    _hide_stack(monkeypatch)
    run, phases = _Run(), []
    with _doubles(run, _db()) as mocks, \
            patch("nexus.indexer._pdfs_needing_index", return_value=[early]), \
            patch("nexus.indexer.check_staleness", return_value=False):
        stats = _run_index(repo, _reg(), on_phase=phases.append)

    assert stats["pdf_stack_held"] == 2
    mocks["_index_pdf_file"].assert_not_called()
    assert pdf_stack.skipped_count() == 2, "the late hold replaced the early record"
    assert sum("NOT indexed" in p for p in phases) == 1, phases
    err = capsys.readouterr().err
    assert sum(line.startswith("nx: ") for line in err.splitlines()) == 1, err
    assert run.housekeeping_keep == [early], "the early-held PDF stays visible to orphan housekeeping"


def test_a_pdf_removed_mid_run_does_not_abort_the_late_recheck(repo, monkeypatch) -> None:
    from nexus import indexer as idx
    from nexus.indexer import _run_index

    _hide_stack(monkeypatch)
    real = idx._file_sha256

    def vanished(path: Path) -> str:
        if path.name == "paper.pdf":
            raise FileNotFoundError(path)
        return real(path)

    run = _Run()
    with _doubles(run, _db()) as mocks, patch("nexus.indexer._pdfs_needing_index", return_value=[]), \
            patch("nexus.indexer._file_sha256", side_effect=vanished):
        stats = _run_index(repo, _reg())

    assert stats["pdf_stack_held"] == 0, "a vanished file is not a reason to hold or install"
    assert mocks["_index_pdf_file"].call_count == 1, "the normal path still meets the file"


def test_the_late_recheck_and_the_indexer_share_one_staleness_decision(tmp_path) -> None:
    """_pdf_is_current is what _index_pdf_file and the late recheck both call: it resolves the
    doc id and passes the cache, and an absent resolver is the unconditional-stale empty id."""
    from nexus.indexer import _pdf_is_current

    f = tmp_path / "p.pdf"
    f.write_bytes(b"%PDF")
    with patch("nexus.indexer.check_staleness", return_value=True) as stale:
        assert _pdf_is_current(f, "h", "col", "m", doc_id_resolver=lambda p: "1.1.7", staleness_cache="cache") is True
        stale.assert_called_once_with("col", f, "h", "m", doc_id="1.1.7", cache="cache")
        stale.reset_mock()
        _pdf_is_current(f, "h", "col", "m", doc_id_resolver=None, staleness_cache=None)
        assert stale.call_args.kwargs["doc_id"] == ""


def test_the_install_policy_at_the_late_site_installs_and_unwinds_for_a_rerun(repo, monkeypatch) -> None:
    """The common path after a collection bump: the probe clears the PDF (the fence is
    complete) but the real cache calls it stale, with the stack absent and installs allowed."""
    from nexus.indexer import _run_index

    _hide_stack(monkeypatch, policy="install")
    installed: list[int] = []
    monkeypatch.setattr(pdf_stack, "_install_generation", lambda **kw: installed.append(1))
    monkeypatch.setattr(pdf_stack, "_windows_runtime_missing", lambda: False)
    monkeypatch.setattr(pdf_stack, "current_generation_has_stack", lambda: False)
    run = _Run()
    with _doubles(run, _db()) as mocks, patch("nexus.indexer._pdfs_needing_index", return_value=[]), \
            patch("nexus.indexer.check_staleness", return_value=False), \
            pytest.raises(pdf_stack.RerunRequired):
        _run_index(repo, _reg())

    assert installed == [1], "the late site must install exactly as the early site does"
    mocks["_index_pdf_file"].assert_not_called()


def test_the_install_policy_at_the_late_site_is_not_reached_when_the_pdf_is_current(repo, monkeypatch) -> None:
    from nexus.indexer import _run_index

    _hide_stack(monkeypatch, policy="install")
    calls = _ensure_spy(monkeypatch)
    run = _Run()
    with _doubles(run, _db()) as mocks, patch("nexus.indexer._pdfs_needing_index", return_value=[]), \
            patch("nexus.indexer.check_staleness", return_value=True):
        stats = _run_index(repo, _reg())

    assert calls == [] and stats["pdf_stack_held"] == 0
    assert mocks["_index_pdf_file"].call_count == 1


# ── an unusable MinerU time limit holds the PDFs and keeps the rest (nexus-tr1rj.24) ──
#
# pdf.mineru_page_timeout_s x pdf.mineru_page_batch above 240 s used to refuse the whole repo run
# at the first PDF's hold (and before that, per file, after code and prose were written). A MinerU
# problem stops MinerU work only (nexus-tr1rj.13): the PDFs that need extracting are held and listed,
# code, prose and RDR files are indexed, and the run ends non-zero. No catalog row and no T3 write
# happens for a held PDF, a PDF stale only by embedding model included.

_BOUNDS = "pdf.mineru_page_timeout_s (180 s) x pdf.mineru_page_batch (2) = 360 s"


@pytest.fixture
def bad_bounds(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr("nexus.config.get_mineru_page_timeout_s", lambda repo_root=None: 180)
    monkeypatch.setattr("nexus.config.get_mineru_page_batch", lambda repo_root=None: 2)
    monkeypatch.setattr(pdf_stack, "state", lambda: pdf_stack.StackState("present", ""))


def test_run_index_over_a_bad_mineru_limit_holds_the_pdf_and_still_indexes_code(repo, bad_bounds) -> None:
    from nexus.indexer import _run_index

    run, phases = _Run(), []
    with _doubles(run, _db()) as mocks, patch("nexus.indexer._pdfs_needing_index", return_value=[repo / "paper.pdf"]):
        stats = _run_index(repo, _reg(), on_phase=phases.append)

    assert stats["pdf_mineru_bounds_held"] == 1
    assert stats["pdf_mineru_bounds_held_paths"] == [str(repo / "paper.pdf")]
    assert _BOUNDS in stats["pdf_mineru_bounds_problem"]
    assert stats["pdf_stack_held"] == 0, "the stack is present; the exit-69 message about it would be false"
    mocks["_index_pdf_file"].assert_not_called()
    assert run.registered == [repo / "ok.py"], "no catalog row for the held PDF"
    assert mocks["_index_code_file"].call_count == 1, "code is indexed past the held PDF"
    assert run.housekeeping_keep == [repo / "paper.pdf"], "orphan housekeeping still sees the held PDF as present"
    assert any(_BOUNDS in p and "NOT indexed" in p for p in phases), phases
    assert pdf_stack.skipped_count() == 1
    # nexus-tr1rj.26: the record carries WHY it holds the PDF, so nx doctor does not blame the stack.
    assert pdf_stack.skipped_count(pdf_stack.SKIP_CAUSE_BOUNDS) == 1
    assert pdf_stack.skipped_count(pdf_stack.SKIP_CAUSE_STACK) == 0


def test_run_index_with_usable_limits_holds_nothing_and_indexes_the_pdf(repo, monkeypatch) -> None:
    """The control for the test above: the same repo and doubles, default limits."""
    from nexus.indexer import _run_index

    monkeypatch.setattr(pdf_stack, "state", lambda: pdf_stack.StackState("present", ""))
    run = _Run()
    with _doubles(run, _db()) as mocks, patch("nexus.indexer._pdfs_needing_index", return_value=[repo / "paper.pdf"]):
        stats = _run_index(repo, _reg())
    assert stats["pdf_mineru_bounds_held"] == 0 and stats["pdf_mineru_bounds_held_paths"] == []
    assert mocks["_index_pdf_file"].call_count == 1


def test_a_pdf_stale_only_by_embedding_model_is_held_before_any_write(repo, bad_bounds) -> None:
    """The early probe is the catalog fence, which has no embedding model: it calls this PDF
    current. The late recheck reads the real staleness cache and holds it too, so the PDF is never
    handed to the indexer (whose backstop would refuse it per file, after code and prose were
    written and the catalog row was registered)."""
    from nexus.indexer import _run_index

    run, phases = _Run(), []
    with _doubles(run, _db()) as mocks, patch("nexus.indexer._pdfs_needing_index", return_value=[]), \
            patch("nexus.indexer.check_staleness", return_value=False):
        stats = _run_index(repo, _reg(), on_phase=phases.append)

    assert stats["pdf_mineru_bounds_held"] == 1
    mocks["_index_pdf_file"].assert_not_called()
    assert mocks["_index_code_file"].call_count == 1
    assert pdf_stack.skipped_count() == 1
    assert any(_BOUNDS in p for p in phases)


def test_a_current_pdf_passes_a_bad_mineru_limit_and_is_dispatched(repo, bad_bounds) -> None:
    from nexus.indexer import _run_index

    run = _Run()
    with _doubles(run, _db()) as mocks, patch("nexus.indexer._pdfs_needing_index", return_value=[]), \
            patch("nexus.indexer.check_staleness", return_value=True):
        stats = _run_index(repo, _reg())
    assert stats["pdf_mineru_bounds_held"] == 0
    assert repo / "paper.pdf" in run.registered
    assert mocks["_index_pdf_file"].call_count == 1


def test_force_holds_every_pdf_on_a_bad_mineru_limit(repo, bad_bounds) -> None:
    from nexus.indexer import _run_index

    run = _Run()
    with _doubles(run, _db()) as mocks, patch("nexus.indexer._pdfs_needing_index", return_value=[]):
        stats = _run_index(repo, _reg(), force=True)
    assert stats["pdf_mineru_bounds_held"] == 1
    mocks["_index_pdf_file"].assert_not_called()


def test_an_early_and_a_late_bounds_hold_are_counted_together(repo, bad_bounds) -> None:
    from nexus.indexer import _run_index

    early = repo / "a.pdf"
    early.write_bytes(b"%PDF-1.4 early")
    run, phases = _Run(), []
    with _doubles(run, _db()) as mocks, patch("nexus.indexer._pdfs_needing_index", return_value=[early]), \
            patch("nexus.indexer.check_staleness", return_value=False):
        stats = _run_index(repo, _reg(), on_phase=phases.append)
    assert stats["pdf_mineru_bounds_held"] == 2
    mocks["_index_pdf_file"].assert_not_called()
    assert pdf_stack.skipped_count() == 2
    assert sum("NOT indexed" in p for p in phases) == 1, phases
    assert run.housekeeping_keep == [early], "the early-held PDF stays visible to orphan housekeeping"


def test_the_since_head_base_does_not_advance_past_a_pdf_held_for_the_mineru_limit(repo, monkeypatch) -> None:
    from nexus import indexer as idx

    advanced: list[str] = []
    monkeypatch.setattr(idx, "_set_owner_head_hash", lambda repo_, head, **kw: advanced.append(head))
    monkeypatch.setattr(idx, "_current_head", lambda repo_: "HEAD1")
    monkeypatch.setattr(idx, "_relink_changed_pdfs", lambda state: None)
    monkeypatch.setattr(idx, "_run_index", lambda *a, **kw: {"pdf_mineru_bounds_held": 1, "files_changed": 1})
    idx.index_repository(repo, _reg(), since_head=True)
    assert advanced == [], "the base moved past a commit whose PDF was held"


def test_the_cli_lists_the_held_pdfs_exits_1_and_says_code_was_indexed(repo, bad_bounds, monkeypatch) -> None:
    """The whole path through `nx index repo`: hold, code indexed, `Done.`, the held PDFs listed, exit 1."""
    with patch("nexus.indexer._pdfs_needing_index", return_value=[repo / "paper.pdf"]):
        result, mocks = _index_repo_cli(repo, monkeypatch)

    assert result.exit_code == 1, result.output
    assert mocks["_index_code_file"].call_count == 1, "code is indexed before the exit"
    mocks["_index_pdf_file"].assert_not_called()
    assert "Done." in result.output
    assert f"Held: 1 PDF(s) NOT indexed, {_BOUNDS}" in result.output
    assert "paper.pdf: held" in result.output
    assert "1 PDF(s) were NOT indexed" in result.output and "Code, prose and RDR files were indexed" in result.output
    assert "PDF support is not installed" not in result.output
