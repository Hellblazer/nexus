# SPDX-License-Identifier: AGPL-3.0-or-later
# Copyright (c) 2026 Hal Hildebrand. All rights reserved.
"""ensure() at every PDF entry point, BEFORE anything is written. nexus-2k66t.4.2.

The contract (nexus-2fyb's lesson): a PDF index without the stack installs it or
refuses with the command, and never degrades quietly. So every entry point asks
``pdf_stack.ensure`` first, and a refusal leaves the catalog and T3 untouched.
These tests drive the real Click commands with ``ensure`` replaced by a fake
and assert the order (ensure, then writes), the exit status and the message.
"""
from __future__ import annotations

from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest
import structlog.testing
from click.testing import CliRunner

from nexus import pdf_stack
from nexus.cli import main
from nexus.pdf_stack import Outcome, PdfStackInstallError, PdfStackMissingError, PdfStackNetworkError

pytestmark = pytest.mark.usefixtures("cloud_mode")


@pytest.fixture
def runner() -> CliRunner:
    return CliRunner()


@pytest.fixture(autouse=True)
def _clean(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    monkeypatch.delenv(pdf_stack.POLICY_ENV, raising=False)
    monkeypatch.delenv(pdf_stack.REEXEC_ENV, raising=False)
    monkeypatch.setenv("HOME", str(tmp_path))
    monkeypatch.setenv("NEXUS_CONFIG_DIR", str(tmp_path / "cfg"))
    pdf_stack.reset_cache()


@pytest.fixture
def fake_pdf(tmp_path: Path) -> Path:
    p = tmp_path / "doc.pdf"
    p.write_bytes(b"%PDF-1.4 fake")
    return p


_REFUSED = PdfStackMissingError(
    "PDF support is not installed: nx index pdf needs docling and MinerU, and nothing was written. "
    "Run: nx self install --extras pdf",
)


def _ensure_raising(exc: BaseException):
    calls: list[str] = []

    def _ensure(entry: str):
        calls.append(entry)
        raise exc

    return _ensure, calls


# ── nx index pdf ─────────────────────────────────────────────────────────────


@pytest.mark.parametrize("exc, code", [
    (_REFUSED, 69),
    (PdfStackNetworkError("PDF support could not be downloaded (network or TLS failure); nothing was written."), 75),
    (PdfStackInstallError("PDF support could not be installed; nothing was written."), 70),
])
def test_index_pdf_stops_with_the_error_class_status_and_writes_nothing(runner, fake_pdf, monkeypatch, exc, code) -> None:
    ensure, calls = _ensure_raising(exc)
    monkeypatch.setattr(pdf_stack, "ensure", ensure)
    with patch("nexus.doc_indexer.index_pdf") as index_pdf:
        result = runner.invoke(main, ["index", "pdf", str(fake_pdf)])
    assert result.exit_code == code, result.output
    assert calls == ["nx index pdf"]
    index_pdf.assert_not_called()
    assert "nothing was written" in result.output


def test_index_pdf_dry_run_also_ensures_because_it_extracts(runner, fake_pdf, monkeypatch) -> None:
    ensure, calls = _ensure_raising(_REFUSED)
    monkeypatch.setattr(pdf_stack, "ensure", ensure)
    with patch("nexus.doc_indexer.index_pdf") as index_pdf:
        result = runner.invoke(main, ["index", "pdf", str(fake_pdf), "--dry-run"])
    assert result.exit_code == 69 and calls == ["nx index pdf"]
    index_pdf.assert_not_called()


def test_index_pdf_after_an_install_reruns_in_the_new_generation_and_exits_with_its_status(
    runner, fake_pdf, monkeypatch,
) -> None:
    monkeypatch.setattr(pdf_stack, "ensure", lambda entry: Outcome.INSTALLED)
    rerun = MagicMock(return_value=0)
    monkeypatch.setattr(pdf_stack, "rerun_command", rerun)
    with patch("nexus.doc_indexer.index_pdf") as index_pdf:
        result = runner.invoke(main, ["index", "pdf", str(fake_pdf)])
    assert result.exit_code == 0, result.output
    rerun.assert_called_once()
    index_pdf.assert_not_called()  # THIS process cannot import the stack it just installed


def test_index_pdf_goes_on_when_the_stack_is_present(runner, fake_pdf, monkeypatch) -> None:
    monkeypatch.setattr(pdf_stack, "ensure", lambda entry: Outcome.PRESENT)
    with patch("nexus.doc_indexer.index_pdf", return_value={"chunks": 3, "pages": [], "title": "", "author": ""}) as index_pdf:
        result = runner.invoke(main, ["index", "pdf", str(fake_pdf)])
    assert result.exit_code == 0, result.output
    index_pdf.assert_called_once()


def test_index_pdf_dir_asks_only_when_there_is_a_pdf_to_index(runner, tmp_path, monkeypatch) -> None:
    empty = tmp_path / "empty"
    empty.mkdir()
    ensure, calls = _ensure_raising(_REFUSED)
    monkeypatch.setattr(pdf_stack, "ensure", ensure)
    result = runner.invoke(main, ["index", "pdf", "--dir", str(empty)])
    assert result.exit_code == 0 and "No PDF files found" in result.output
    assert calls == [], "an empty directory must not start a 370 MB download"

    full = tmp_path / "full"
    full.mkdir()
    (full / "a.pdf").write_bytes(b"%PDF-1.4")
    with patch("nexus.doc_indexer.index_pdf") as index_pdf:
        result = runner.invoke(main, ["index", "pdf", "--dir", str(full)])
    assert result.exit_code == 69 and calls == ["nx index pdf --dir"]
    index_pdf.assert_not_called()


# ── nx dt index ──────────────────────────────────────────────────────────────


def test_dt_index_ensures_before_the_first_record_when_any_is_a_pdf(runner, monkeypatch) -> None:
    records = [("U1", "/a.md"), ("U2", "/b.pdf")]
    monkeypatch.setattr("nexus.commands.dt._gather_records", lambda **kw: records)
    ensure, calls = _ensure_raising(_REFUSED)
    monkeypatch.setattr(pdf_stack, "ensure", ensure)
    index_record = MagicMock()
    monkeypatch.setattr("nexus.commands.dt._index_record", index_record)
    result = runner.invoke(main, ["dt", "index", "--uuid", "U1"])
    assert result.exit_code == 69, result.output
    assert calls == ["nx dt index"]
    index_record.assert_not_called()  # not even the markdown record: ensure is settled first


def test_dt_index_of_markdown_only_never_asks(runner, monkeypatch) -> None:
    records = [("U1", "/a.md")]
    monkeypatch.setattr("nexus.commands.dt._gather_records", lambda **kw: records)
    ensure, calls = _ensure_raising(_REFUSED)
    monkeypatch.setattr(pdf_stack, "ensure", ensure)
    monkeypatch.setattr("nexus.commands.dt._index_record", lambda *a, **kw: (True, 1))
    monkeypatch.setattr("nexus.commands.dt._stamp_dt_uri_on_entry", lambda *a, **kw: True)
    result = runner.invoke(main, ["dt", "index", "--uuid", "U1"])
    assert calls == [], result.output


# ── nx mineru start ──────────────────────────────────────────────────────────


def test_mineru_start_ensures_before_it_spawns_anything(runner, monkeypatch) -> None:
    ensure, calls = _ensure_raising(_REFUSED)
    monkeypatch.setattr(pdf_stack, "ensure", ensure)
    with patch("nexus.commands.mineru.spawn_server_process") as spawn:
        result = runner.invoke(main, ["mineru", "start"])
    assert result.exit_code == 69 and calls == ["nx mineru start"]
    spawn.assert_not_called()


def test_mineru_status_and_stop_need_no_stack(runner, monkeypatch) -> None:
    ensure, calls = _ensure_raising(_REFUSED)
    monkeypatch.setattr(pdf_stack, "ensure", ensure)
    result = runner.invoke(main, ["mineru", "status"])
    assert result.exit_code == 0 and "not running" in result.output
    assert calls == []


# ── nx index repo ────────────────────────────────────────────────────────────


@pytest.fixture
def repo_cmd(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    # `nx index repo` derives per-engine state-file names from the resolved
    # endpoint before it indexes anything; with index_repository replaced there
    # is no engine in play, so the name is a constant (tenant-free test).
    monkeypatch.setattr("nexus.mcp_infra._engine_file_key", lambda: "test")
    monkeypatch.setattr("nexus.mcp_infra.engine_process_uptime_seconds", lambda *a, **k: None)
    for name in (
        "run_collection_postprocessing", "_drain_repo_collections", "_refresh_derived_taxonomy",
    ):
        monkeypatch.setattr(f"nexus.commands.index.{name}", lambda *a, **k: None)
    monkeypatch.setattr("nexus.commands.index._collections_without_topics", lambda *a, **k: set())
    monkeypatch.setattr("nexus.commands.index._collections_with_failed_discover", lambda *a, **k: set())
    monkeypatch.setattr("nexus.commands.index._any_collection_has_topics", lambda *a, **k: True)
    repo = tmp_path / "myrepo"
    repo.mkdir()
    (repo / ".git").mkdir()
    reg = MagicMock()
    reg.get.return_value = {"collection": "code__myrepo", "docs_collection": "docs__myrepo"}

    def _invoke(runner: CliRunner, stats=None, side_effect=None):
        kw = {"side_effect": side_effect} if side_effect else {"return_value": stats or {}}
        with patch("nexus.commands.index._registry", return_value=reg):
            with patch("nexus.indexer.index_repository", **kw) as idx:
                result = runner.invoke(main, ["index", "repo", str(repo)])
        return result, idx

    return _invoke


def test_index_repo_exits_69_over_held_pdfs_and_names_the_command(runner, repo_cmd) -> None:
    result, _ = repo_cmd(runner, stats={"pdf_stack_held": 2})
    assert result.exit_code == 69, result.output
    assert "2 PDF(s) were NOT indexed" in result.output
    assert "nx self install --extras pdf" in result.output
    assert "Done." in result.output, "code and prose finished; the exit status is what carries the refusal"


def test_index_repo_without_held_pdfs_is_unchanged(runner, repo_cmd) -> None:
    result, _ = repo_cmd(runner, stats={"pdf_stack_held": 0})
    assert result.exit_code == 0, result.output


def test_index_repo_reruns_in_the_new_generation_after_an_install_unwound_the_lock(runner, repo_cmd, monkeypatch) -> None:
    rerun = MagicMock(return_value=0)
    monkeypatch.setattr(pdf_stack, "rerun_command", rerun)
    result, _ = repo_cmd(runner, side_effect=pdf_stack.RerunRequired())
    assert result.exit_code == 0, result.output
    rerun.assert_called_once()


# ── the gate inside the repo walk ────────────────────────────────────────────


def test_the_gate_lets_every_pdf_through_when_the_stack_is_present(monkeypatch, tmp_path) -> None:
    from nexus.indexer import _hold_pdfs_without_stack

    monkeypatch.setattr(pdf_stack, "ensure", lambda entry: Outcome.PRESENT)
    pdf_stack.record_skipped([tmp_path / "old.pdf"], repo=tmp_path)
    assert _hold_pdfs_without_stack([tmp_path / "a.pdf"], tmp_path) == []
    assert pdf_stack.skipped_count() == 0, "a run that indexed them clears the doctor's count"


def test_the_gate_holds_every_pdf_under_refuse_names_each_and_records_them(monkeypatch, tmp_path) -> None:
    from nexus.indexer import _hold_pdfs_without_stack

    monkeypatch.setenv(pdf_stack.POLICY_ENV, "refuse")
    monkeypatch.setattr(pdf_stack, "state", lambda: pdf_stack.StackState("absent", ""))
    monkeypatch.setattr(pdf_stack, "install_plan", lambda: pdf_stack.InstallPlan("generation", True, pdf_stack.INSTALL_COMMAND))
    pdfs = [tmp_path / "a.pdf", tmp_path / "b.pdf"]
    phases: list[str] = []
    with structlog.testing.capture_logs() as logs:
        held = _hold_pdfs_without_stack(pdfs, tmp_path, on_phase=phases.append)
    assert held == pdfs
    errors = [e for e in logs if e["event"] == "pdf_not_indexed_stack_missing"]
    assert sorted(e["path"] for e in errors) == sorted(str(p) for p in pdfs)
    assert all(e["log_level"] == "error" for e in errors)
    assert pdf_stack.skipped_count() == 2
    assert any("NOT indexed" in p and pdf_stack.INSTALL_COMMAND in p for p in phases)


def test_the_gate_exits_at_once_on_a_network_failure_before_anything_is_registered(monkeypatch, tmp_path) -> None:
    from nexus.indexer import _hold_pdfs_without_stack

    ensure, _ = _ensure_raising(PdfStackNetworkError("could not be downloaded; nothing was written."))
    monkeypatch.setattr(pdf_stack, "ensure", ensure)
    with pytest.raises(SystemExit) as exit_:
        _hold_pdfs_without_stack([tmp_path / "a.pdf"], tmp_path)
    assert exit_.value.code == 75


def test_the_gate_unwinds_to_the_cli_after_an_install(monkeypatch, tmp_path) -> None:
    from nexus.indexer import _hold_pdfs_without_stack

    monkeypatch.setattr(pdf_stack, "ensure", lambda entry: Outcome.INSTALLED)
    with pytest.raises(pdf_stack.RerunRequired):
        _hold_pdfs_without_stack([tmp_path / "a.pdf"], tmp_path)


def test_a_held_pdf_still_counts_as_present_for_the_orphan_sweep(tmp_path) -> None:
    """Housekeeping deletes a catalog row whose file is absent from the walk
    after two runs. A refused run must not age a PDF's row toward that."""
    from nexus import indexer as idx

    seen: list[set[str]] = []
    pdf = tmp_path / "held.pdf"
    code = tmp_path / "a.py"
    with patch.object(idx, "_run_housekeeping", side_effect=lambda cat, owner, indexed, **kw: seen.append(set(indexed))):
        idx._maybe_run_housekeeping(
            object(), object(), [(code, "code", "code__x")], tmp_path,
            writer=None, skip_housekeeping=False, keep=[pdf],
        )
        idx._maybe_run_housekeeping(
            object(), object(), [(code, "code", "code__x")], tmp_path,
            writer=None, skip_housekeeping=False,
        )
    assert seen == [{"a.py", "held.pdf"}, {"a.py"}]


def test_the_catalog_hook_hands_the_held_pdfs_to_the_sweep() -> None:
    import inspect

    from nexus import indexer as idx

    assert "keep=housekeeping_keep" in inspect.getsource(idx._catalog_hook)
    assert "housekeeping_keep=_pdf_held" in inspect.getsource(idx._run_index)


def test_a_held_pdf_keeps_the_since_head_base_from_advancing() -> None:
    """--since-head diffs from owners.head_hash; advancing it past a commit
    whose PDF was held would never offer that PDF again."""
    import inspect

    from nexus import indexer as idx

    assert 'stats.get("pdf_stack_held", 0)' in inspect.getsource(idx.index_repository)


def test_the_post_commit_hook_never_starts_a_download() -> None:
    from nexus.commands.hooks import _STANZA

    dispatch = [ln for ln in _STANZA.splitlines() if ln.lstrip().startswith("NX_PDF_STACK=refuse nx index repo")]
    assert len(dispatch) == 1, "the dispatch line must carry the refuse policy"
    assert "--since-head" in dispatch[0]
