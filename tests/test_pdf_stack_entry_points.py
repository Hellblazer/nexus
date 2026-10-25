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


def test_index_repo_exits_69_over_held_pdfs_and_names_the_command(runner, repo_cmd, monkeypatch) -> None:
    # a generation install: the plan's command is the `--extras` form (other layouts get theirs:
    # tests/test_pdf_stack_index_repo_hold.py, tests/test_pdf_repair_command_per_layout.py)
    monkeypatch.setattr(pdf_stack, "install_plan", lambda: pdf_stack.InstallPlan("generation", True, pdf_stack.INSTALL_COMMAND))
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


# The hold's wiring (keep= to housekeeping, pdf_stack_held in the stats, the
# since-head base staying put, the exit 69) is pinned by behaviour in
# tests/test_pdf_stack_index_repo_hold.py; source-text greps here let three
# mutations through.


def test_the_post_commit_hook_never_starts_a_download() -> None:
    from nexus.commands.hooks import _STANZA

    dispatch = [ln for ln in _STANZA.splitlines() if ln.lstrip().startswith("NX_PDF_STACK=refuse nx index repo")]
    assert len(dispatch) == 1, "the dispatch line must carry the refuse policy"
    assert "--since-head" in dispatch[0]


# ── nx collection reindex: the purge comes first, so the gate comes before it ─


@pytest.fixture
def reindex_bed(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    """A docs__ collection whose one source is a PDF on disk. The catalog and
    the T3 client are fakes (tenant-free); the purge is a spy."""
    from nexus.db.collection_purge import CascadeCounts
    from nexus.db.http_vector_client import HttpVectorClient

    pdf = tmp_path / "paper.pdf"
    pdf.write_bytes(b"%PDF-1.4 fake")
    md = tmp_path / "note.md"
    md.write_text("# note\n")

    class _Bed:
        pass

    bed = _Bed()
    bed.pdf, bed.md = pdf, md
    bed.db = MagicMock(spec=HttpVectorClient)
    bed.db.collection_info.side_effect = lambda _name: {"count": 4, "metadata": {}}
    bed.sources = [str(pdf)]

    def _col_get(**_kw):
        return {"ids": [f"id{i}" for i in range(len(bed.sources))], "metadatas": [{"source_path": s} for s in bed.sources]}

    col = MagicMock()
    col.get.side_effect = _col_get
    bed.db.get_or_create_collection.return_value = col
    bed.purge = MagicMock(return_value=CascadeCounts())
    monkeypatch.setattr("nexus.catalog.factory.make_catalog_reader", lambda *a, **k: MagicMock())
    monkeypatch.setattr("nexus.catalog.tombstones.read_tombstones", lambda cat: [])
    monkeypatch.setattr("nexus.catalog.tombstones.deleted_only_sources", lambda cat, tombs, paths, collection=None: [])
    monkeypatch.setattr("nexus.db.collection_purge.purge_collection_cascade", bed.purge)
    monkeypatch.setattr("nexus.commands.collection._t3", lambda: bed.db)
    monkeypatch.setattr("nexus.commands.index.run_collection_postprocessing", lambda *a, **k: None)
    monkeypatch.setattr("nexus.corpus.discard_cached_registration", lambda name: None)
    monkeypatch.setattr("nexus.corpus.ensure_collection_registered", lambda *a, **k: None)
    monkeypatch.setattr("nexus.db.t3.verify_collection_deep", MagicMock())
    return bed


@pytest.mark.parametrize("exc, code", [(_REFUSED, 69), (PdfStackNetworkError("could not be downloaded; nothing was written."), 75)])
def test_collection_reindex_gates_on_the_stack_before_it_purges_the_collection(runner, reindex_bed, monkeypatch, exc, code) -> None:
    ensure, calls = _ensure_raising(exc)
    monkeypatch.setattr(pdf_stack, "ensure", ensure)
    with patch("nexus.doc_indexer.index_pdf") as index_pdf:
        result = runner.invoke(main, ["collection", "reindex", "docs__papers", "--force"])
    assert result.exit_code == code, result.output
    assert calls == ["nx collection reindex"]
    reindex_bed.purge.assert_not_called()  # the data is still there
    index_pdf.assert_not_called()
    assert "Deleting collection" not in result.output


def test_collection_reindex_after_an_install_reruns_and_purges_nothing_here(runner, reindex_bed, monkeypatch) -> None:
    monkeypatch.setattr(pdf_stack, "ensure", lambda entry: Outcome.INSTALLED)
    rerun = MagicMock(return_value=0)
    monkeypatch.setattr(pdf_stack, "rerun_command", rerun)
    result = runner.invoke(main, ["collection", "reindex", "docs__papers", "--force"])
    assert result.exit_code == 0, result.output
    rerun.assert_called_once()
    reindex_bed.purge.assert_not_called()


def test_collection_reindex_of_markdown_only_never_asks(runner, reindex_bed, monkeypatch) -> None:
    reindex_bed.sources = [str(reindex_bed.md)]
    ensure, calls = _ensure_raising(_REFUSED)
    monkeypatch.setattr(pdf_stack, "ensure", ensure)
    with patch("nexus.doc_indexer.index_markdown", return_value=1):
        result = runner.invoke(main, ["collection", "reindex", "docs__papers", "--force"])
    assert calls == [], result.output
    reindex_bed.purge.assert_called_once()


def test_collection_reindex_does_not_turn_a_stack_error_into_a_warning(runner, reindex_bed, monkeypatch) -> None:
    """The gate passed, then the stack broke: every remaining PDF would fail
    alike. The old shape was 'Warning: failed to re-index', 'N -> 0 chunks', exit 0."""
    monkeypatch.setattr(pdf_stack, "ensure", lambda entry: Outcome.PRESENT)
    with patch("nexus.doc_indexer.index_pdf", side_effect=_REFUSED):
        result = runner.invoke(main, ["collection", "reindex", "docs__papers", "--force"])
    assert result.exit_code == 69, result.output
    assert "Warning: failed to re-index" not in result.output
    assert "Re-indexed:" not in result.output
    assert "nx self install --extras pdf" in result.output


# ── nx dt capture: the gate comes before DEVONthink is touched ───────────────


@pytest.fixture
def capture_bed(monkeypatch: pytest.MonkeyPatch):
    """DEVONthink present; every DT side effect is a spy. ``bed.touched`` lists
    what reached DEVONthink."""
    monkeypatch.setattr("nexus.commands.dt._is_darwin", lambda: True)
    monkeypatch.setattr("nexus.mcp_client.devonthink.available", lambda **k: True)

    class _Bed:
        touched: list[str]

    bed = _Bed()
    bed.touched = []

    def _spy(label: str):
        def _fn(*a, **kw):
            bed.touched.append(label)
            return "NEW-UUID"
        return _fn

    monkeypatch.setattr("nexus.mcp_client.devonthink.dt_capture_web_page", _spy("capture_web_page"))
    monkeypatch.setattr("nexus.mcp_client.devonthink.dt_download_pdf_from_doi", _spy("download_pdf_from_doi"))
    monkeypatch.setattr("nexus.mcp_client.devonthink.dt_import_file", _spy("import_file"))
    monkeypatch.setattr("nexus.commands.dt._gather_records", lambda **kw: [(kw["uuids"][0], "/captured.pdf")])
    monkeypatch.setattr("nexus.commands.dt._index_record", lambda uuid, path, **kw: (True, 1))
    monkeypatch.setattr("nexus.commands.dt._index_dt_content_record", lambda uuid, **kw: True)
    return bed


_PDF_CAPTURES = [
    pytest.param(["https://e.com/a", "--type", "pdf"], id="url-as-pdf"),
    pytest.param(["--doi", "10.1000/xyz"], id="doi"),
    pytest.param(["--file", "/tmp/paper.PDF"], id="file-pdf"),
]


@pytest.mark.parametrize("argv", _PDF_CAPTURES)
def test_dt_capture_of_a_pdf_gates_before_any_devonthink_side_effect(runner, capture_bed, monkeypatch, argv) -> None:
    ensure, calls = _ensure_raising(_REFUSED)
    monkeypatch.setattr(pdf_stack, "ensure", ensure)
    result = runner.invoke(main, ["dt", "capture", *argv])
    assert result.exit_code == 69, result.output
    assert calls == ["nx dt capture"]
    assert capture_bed.touched == [], "a refused capture must leave DEVONthink untouched"


@pytest.mark.parametrize("argv", _PDF_CAPTURES)
def test_dt_capture_after_an_install_reruns_without_capturing_in_this_process(runner, capture_bed, monkeypatch, argv) -> None:
    """The re-run child repeats the whole argv, so a capture here would be a
    second capture of the same source: two DEVONthink records."""
    monkeypatch.setattr(pdf_stack, "ensure", lambda entry: Outcome.INSTALLED)
    rerun = MagicMock(return_value=0)
    monkeypatch.setattr(pdf_stack, "rerun_command", rerun)
    result = runner.invoke(main, ["dt", "capture", *argv])
    assert result.exit_code == 0, result.output
    rerun.assert_called_once()
    assert capture_bed.touched == []


@pytest.mark.parametrize("argv", [
    ["https://e.com/a"],  # webarchive (the default)
    ["https://e.com/a", "--type", "markdown"],
    ["--file", "/tmp/notes.md"],
])
def test_dt_capture_of_a_non_pdf_never_asks(runner, capture_bed, monkeypatch, argv) -> None:
    ensure, calls = _ensure_raising(_REFUSED)
    monkeypatch.setattr(pdf_stack, "ensure", ensure)
    monkeypatch.setattr("nexus.commands.dt._gather_records", lambda **kw: [(kw["uuids"][0], "/captured.md")])
    result = runner.invoke(main, ["dt", "capture", *argv])
    assert calls == [], result.output
