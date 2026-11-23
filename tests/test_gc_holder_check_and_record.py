# SPDX-License-Identifier: AGPL-3.0-or-later
"""Generation gc: a positive census, and a record that survives the run it describes.

Review round on aca2ee8519 (T2 nexus review-/critique-aca2ee8519-scratch-client-closed):

* The record was buffered until every deletion finished, so a run killed
  mid-``rmtree`` (or an exception out of ``_reap``) deleted with no trace: the
  incident again. A ``gc_reap_begin`` line is now appended and fsynced BEFORE
  each delete, and ``gc_reaped`` after it.
* "The census worked" was "the table had a row". It now means the table lists
  the gc process itself (a ``ps`` that lists a subset, as a sandbox can, is a
  failed census), and the record says why a census failed, how long ``ps``
  took and how many rows it gave.
* The record carries what the next investigation needs: the parent chain, the
  runner's generation and version, the hook-vs-hand caller, and the holder map.
* The log rotates at 1 MB, and writing it never breaks gc.
"""
from __future__ import annotations

import json
import os
import stat
import sys
from pathlib import Path

import pytest

from nexus._install import census_core, gc_core
from nexus._install import layout_core as lc
from tests._module_seam import setattr_in

POSIX = pytest.mark.skipif(sys.platform == "win32", reason="a stub ps on PATH")


def _receipted(tools: Path, name: str) -> Path:
    gen = tools / name
    (gen / "bin").mkdir(parents=True)
    receipt = lc.Receipt(
        version="1.0.0", spec="conexus==1.0.0", source_kind="registry", source="conexus",
        python="3.12.0", base_interpreter=str(Path(sys.base_prefix)), created_at="2026-01-01T00:00:00Z",
    )
    (gen / lc.RECEIPT_NAME).write_text(receipt.to_json())
    return gen


@pytest.fixture
def tools(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    root = tmp_path / "tools"
    root.mkdir()
    monkeypatch.setenv("NEXUS_CONFIG_DIR", str(tmp_path / "config"))
    for name in ("gen-20261101T000000Z", "gen-20261102T000000Z", "gen-20261103T000000Z"):
        _receipted(root, name)
    return root


def _log_path(tmp_path: Path) -> Path:
    return tmp_path / "config" / "logs" / gc_core.GC_LOG_NAME


def _records(tmp_path: Path) -> list[dict]:
    return [json.loads(line) for line in _log_path(tmp_path).read_text().splitlines() if line.strip()]


def _own_row() -> str:
    return f"{os.getpid()} python3 gc_core.py\n"


# ── the record is written as the run goes ─────────────────────────────────────

def test_a_reap_that_raises_partway_still_leaves_its_begin_line(tools: Path, tmp_path: Path, monkeypatch) -> None:
    oldest = tools / "gen-20261101T000000Z"

    def rmtree_dies_partway(path, *a, **kw):
        (Path(path) / "bin").rmdir()  # part of the tree is already gone
        raise KeyboardInterrupt("session torn down mid-delete")

    setattr_in(monkeypatch, gc_core, "shutil.rmtree", rmtree_dies_partway)

    with pytest.raises(KeyboardInterrupt):
        gc_core.gc_generations(tools, keep=2, snapshot=_own_row())

    events = [(r["event"], r.get("generation")) for r in _records(tmp_path)]
    assert ("gc_reap_begin", str(oldest)) in events, events
    assert ("gc_reap_failed", str(oldest)) in events, events
    begin = next(r for r in _records(tmp_path) if r["event"] == "gc_reap_begin")
    assert begin["reason"] and begin["census"]["state"] == "ok"


def test_each_reap_is_bracketed_and_the_run_is_summarised(tools: Path, tmp_path: Path) -> None:
    gc_core.gc_generations(tools, keep=2, snapshot=_own_row())

    assert [r["event"] for r in _records(tmp_path)] == ["gc_reap_begin", "gc_reaped", "gc_run"]


def test_a_missing_tools_directory_is_recorded(tmp_path: Path, monkeypatch) -> None:
    monkeypatch.setenv("NEXUS_CONFIG_DIR", str(tmp_path / "config"))

    assert gc_core.gc_generations(tmp_path / "no-such-tools", keep=2, snapshot=_own_row()) == []

    (rec,) = _records(tmp_path)
    assert rec["event"] == "gc_run" and rec["skipped"] == "tools directory missing", rec


def test_the_log_rotates_at_one_megabyte(tools: Path, tmp_path: Path) -> None:
    path = _log_path(tmp_path)
    path.parent.mkdir(parents=True)
    path.write_text("x" * (gc_core.GC_LOG_MAX_BYTES + 1))

    gc_core.gc_generations(tools, keep=3, snapshot=_own_row())

    rotated = path.with_name(gc_core.GC_LOG_NAME + ".1")
    assert rotated.stat().st_size == gc_core.GC_LOG_MAX_BYTES + 1
    assert [r["event"] for r in _records(tmp_path)] == ["gc_run"]


def test_an_unwritable_log_never_breaks_gc(tools: Path, tmp_path: Path, monkeypatch, capsys) -> None:
    def no_home():
        raise RuntimeError("Could not determine home directory.")

    monkeypatch.setattr(gc_core, "gc_log_path", no_home)

    lines = gc_core.gc_generations(tools, keep=2, snapshot=_own_row())

    assert any(line.startswith("reaped ") for line in lines), lines
    assert "could not write the gc record" in capsys.readouterr().err


# ── a positive census ─────────────────────────────────────────────────────────

def test_a_table_that_does_not_list_the_gc_process_is_a_failed_census(tools: Path, tmp_path: Path, monkeypatch) -> None:
    """A sandboxed ps can list a subset of the processes. A table without the
    gc process in it is not a table gc can trust for "nothing holds this"."""
    monkeypatch.setattr(gc_core._census(), "ps_snapshot", lambda **_k: "999 /usr/bin/vim unrelated.txt\n")

    lines = gc_core.gc_generations(tools, keep=1)

    assert not any(line.startswith("reaped ") for line in lines), lines
    (summary,) = _records(tmp_path)
    assert summary["census"]["state"] == "failed"
    assert f"own pid {os.getpid()}" in summary["census"]["why"], summary


def test_a_table_listing_the_gc_process_is_a_working_census(tools: Path, tmp_path: Path, monkeypatch) -> None:
    monkeypatch.setattr(gc_core._census(), "ps_snapshot", lambda **_k: _own_row() + "999 /usr/bin/vim x\n")

    lines = gc_core.gc_generations(tools, keep=1)

    assert sum(line.startswith("reaped ") for line in lines) == 2, lines
    assert _records(tmp_path)[-1]["census"]["state"] == "ok"


@POSIX
def test_ps_snapshot_reports_why_it_failed_and_how_long_it_took(tmp_path: Path, monkeypatch) -> None:
    stub = tmp_path / "bin"
    stub.mkdir()
    ps = stub / "ps"
    ps.write_text("#!/bin/sh\nexit 3\n")
    ps.chmod(ps.stat().st_mode | stat.S_IXUSR)
    monkeypatch.setenv("PATH", f"{stub}{os.pathsep}{os.environ.get('PATH', '')}")
    info: dict = {}

    assert census_core.ps_snapshot(info=info) == ""

    assert info["reason"] == "rc=3", info
    assert info["rows"] == 0 and info["duration_s"] >= 0


@POSIX
def test_ps_snapshot_reports_a_missing_ps(tmp_path: Path, monkeypatch) -> None:
    monkeypatch.setenv("PATH", str(tmp_path / "empty"))
    info: dict = {}

    assert census_core.ps_snapshot(info=info) == ""

    assert info["reason"].startswith("oserror:"), info


# ── what the record carries ──────────────────────────────────────────────────

def test_the_record_carries_the_holder_map_runner_and_caller(tools: Path, tmp_path: Path, monkeypatch) -> None:
    held = tools / "gen-20261101T000000Z"
    monkeypatch.setenv(gc_core.GC_CALLER_ENV, "self-gc-hook")
    monkeypatch.setenv(gc_core.GC_RUNNER_PREFIX_ENV, "/tools/gen-runner")
    monkeypatch.setenv(gc_core.GC_RUNNER_VERSION_ENV, "9.9.9")
    snapshot = _own_row() + f"4592 {held}/bin/python {held}/bin/nx-mcp\n"

    gc_core.gc_generations(tools, keep=1, snapshot=snapshot)

    records = _records(tmp_path)
    summary = records[-1]
    assert summary["holders"] == {str(held): [4592]}, summary
    assert summary["caller"] == "self-gc-hook"
    assert summary["runner"] == {"prefix": "/tools/gen-runner", "version": "9.9.9", "executable": sys.executable}
    assert summary["census"]["rows"] == 2
    reaped = [r for r in records if r["event"] == "gc_reaped"]
    assert [r["generation"] for r in reaped] == [str(tools / "gen-20261102T000000Z")]
    assert reaped[0]["holders"] == 0


@POSIX
def test_the_record_names_the_parent_chain(tools: Path, tmp_path: Path) -> None:
    gc_core.gc_generations(tools, keep=3, snapshot=_own_row())

    (summary,) = _records(tmp_path)
    assert summary["ppid"] == os.getppid()
    chain = summary["ancestry"]
    assert chain and chain[0]["pid"] == os.getppid() and chain[0]["command"], summary


# ── win32 ─────────────────────────────────────────────────────────────────────

def test_on_windows_an_empty_census_keeps_every_generation(tools: Path) -> None:
    plan = gc_core.plan(tools, keep=1, snapshot="", platform="win32")

    assert [a for a, _e, _d in plan if a == "reap"] == [], plan
    assert all("process census unavailable" in d for a, _e, d in plan if a == "keep"), plan


def test_on_windows_a_failed_process_table_keeps_every_generation(tools: Path, monkeypatch) -> None:
    """``_windows_snapshot`` answers "" when the table cannot be read (enumerate_processes raised)."""
    monkeypatch.setattr(gc_core._census(), "ps_snapshot", lambda **_k: "")

    lines = gc_core.gc_generations(tools, keep=1, platform="win32")

    assert not any(line.startswith("reaped ") for line in lines), lines
