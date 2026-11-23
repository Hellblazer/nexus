# SPDX-License-Identifier: AGPL-3.0-or-later
"""The bootstrap's cleanup keeps a live holder's generation, and every gc run leaves a durable record.

Incident 2026-11-21/22: long-lived MCP servers ran from install generations
deleted under them, which broke their T1 scratch tool. The 06:08Z rebuild on
2026-11-21 replaced an 8.5.3 generation with an 8.5.3[pdf] one, the shape the
bootstrap's ``_reap_superseded`` handles (keep=1, and it drops the ``previous``
pointer to the generation it replaced), so only the live-holder census stood
between those servers and the reap. Nothing recorded what was deleted: the
SessionStart ``nx-hook self-gc`` discards its output.

1. ``_reap_superseded`` reaps through gc_core's plan (``_reap_generations`` ->
   ``gc.sh`` -> ``gc_core``), one deleter with one set of rules: a live holder
   of the replaced generation keeps it, and so does a census that failed.
2. ``gc_core.gc_generations`` appends one JSON line per reaped generation
   (path, stamp, reason, holders seen, census ok or failed) and one summary
   line per run to ``<config>/logs/install-gc.log``, an append-only file apart
   from the rotating hook.log. A run that deletes nothing writes the summary.
"""
from __future__ import annotations

import json
import os
import stat
import sys
from pathlib import Path

import pytest

from nexus._install import gc_core
from nexus._install import layout_core as lc
from nexus.commands import self_cmd

pytestmark = pytest.mark.skipif(sys.platform == "win32", reason="POSIX symlink pointers and a stub ps on PATH")

VERSION = "1.0.0"


def _receipted(tools: Path, name: str, version: str = VERSION) -> Path:
    gen = tools / name
    (gen / "bin").mkdir(parents=True)
    receipt = lc.Receipt(
        version=version, spec=f"conexus=={version}", source_kind="registry", source="conexus",
        python="3.12.0", base_interpreter=str(Path(sys.base_prefix)), created_at="2026-01-01T00:00:00Z",
    )
    (gen / lc.RECEIPT_NAME).write_text(receipt.to_json())
    (gen / "bin" / "python").symlink_to(sys.executable)  # an interpreter that is not gone
    return gen


def _ps(stub_bin: Path, body: str) -> None:
    stub_bin.mkdir(parents=True, exist_ok=True)
    ps = stub_bin / "ps"
    ps.write_text("#!/bin/sh\n" + body + "\n")
    ps.chmod(ps.stat().st_mode | stat.S_IXUSR)


@pytest.fixture
def bed(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    """A bootstrap that just rebuilt the running version: ``old`` (same version,
    the outgoing current) is what ``_reap_superseded`` would remove; ``new`` is
    current and ``previous`` names ``old``."""
    tools = tmp_path / "tools"
    tools.mkdir()
    config = tmp_path / "config"
    stub_bin = tmp_path / "stubbin"
    monkeypatch.setenv("NX_TOOLS_DIR", str(tools))
    monkeypatch.setenv("NX_BIN_DIR", str(tmp_path / "bin"))
    monkeypatch.setenv("NEXUS_CONFIG_DIR", str(config))
    monkeypatch.setenv("PATH", f"{stub_bin}{os.pathsep}{os.environ.get('PATH', '/usr/bin:/bin')}")
    old = _receipted(tools, "gen-20261121T000000Z")
    new = _receipted(tools, "gen-20261121T060803Z")
    os.symlink(new, tools / "current")
    os.symlink(old, tools / "previous")
    return tools, stub_bin, config, old, new


def _bootstrap_reap(tools: Path, old: Path) -> None:
    self_cmd._reap_superseded(self_cmd.packaged_install_dir(), tools, outgoing=old, built_version=VERSION)


def _log(config: Path) -> list[dict]:
    path = config / "logs" / "install-gc.log"
    assert path.is_file(), f"no gc log at {path}"
    return [json.loads(line) for line in path.read_text().splitlines() if line.strip()]


# ── 1. the bootstrap's cleanup goes through the census ─────────────────────────

def test_a_live_holder_of_the_replaced_generation_survives_a_bootstrap_rebuild(bed) -> None:
    tools, stub_bin, _config, old, new = bed
    _ps(stub_bin, f"echo ' 33870 {old}/bin/python {old}/bin/nx-mcp'")

    _bootstrap_reap(tools, old)

    assert old.is_dir(), "the bootstrap reaped the generation a live MCP server runs from"
    assert new.is_dir()


def test_a_failed_census_keeps_the_replaced_generation(bed) -> None:
    tools, stub_bin, _config, old, _new = bed
    _ps(stub_bin, "exit 1")

    _bootstrap_reap(tools, old)

    assert old.is_dir(), "the bootstrap reaped on a process census it could not take"


def test_an_unheld_replaced_generation_is_still_reaped(bed) -> None:
    """The control: with a working census naming no holder, the leftover goes."""
    tools, stub_bin, _config, old, new = bed
    _ps(stub_bin, "echo '  999 /usr/bin/vim unrelated.txt'")

    _bootstrap_reap(tools, old)

    assert not old.exists()
    assert new.is_dir()


# ── 2. every gc run leaves a durable record ───────────────────────────────────

def test_a_reap_writes_one_line_per_reaped_generation_and_a_summary(bed) -> None:
    tools, stub_bin, config, old, _new = bed
    _ps(stub_bin, "echo '  999 /usr/bin/vim unrelated.txt'")

    _bootstrap_reap(tools, old)

    records = _log(config)
    reaped = [r for r in records if r["event"] == "gc_reaped"]
    assert len(reaped) == 1, records
    line = reaped[0]
    assert line["generation"] == str(old)
    assert line["stamp"] == "20261121T000000Z"
    assert line["reason"]
    assert line["holders"] == 0
    assert line["census"] == "ok"
    assert line["caller"] == "bootstrap"
    summary = [r for r in records if r["event"] == "gc_run"]
    assert len(summary) == 1 and summary[0]["reaped"] == 1 and summary[0]["census"] == "ok", records


def test_a_run_that_deletes_nothing_writes_one_summary_line(bed) -> None:
    tools, stub_bin, config, old, _new = bed
    _ps(stub_bin, f"echo ' 33870 {old}/bin/python {old}/bin/nx-mcp'")

    _bootstrap_reap(tools, old)

    records = _log(config)
    assert [r["event"] for r in records] == ["gc_run"], records
    summary = records[0]
    assert summary["reaped"] == 0
    assert summary["census"] == "ok"
    assert {"generation": str(old), "detail": "held by 33870"} in summary["kept"], summary


def test_a_failed_census_is_recorded_as_failed(bed) -> None:
    tools, stub_bin, config, old, _new = bed
    _ps(stub_bin, "exit 1")

    _bootstrap_reap(tools, old)

    (summary,) = _log(config)
    assert summary["event"] == "gc_run" and summary["census"] == "failed" and summary["reaped"] == 0, summary


def test_the_log_is_appended_never_rewritten(bed) -> None:
    tools, stub_bin, config, old, _new = bed
    _ps(stub_bin, f"echo ' 33870 {old}/bin/python {old}/bin/nx-mcp'")

    _bootstrap_reap(tools, old)
    _bootstrap_reap(tools, old)

    assert [r["event"] for r in _log(config)] == ["gc_run", "gc_run"]


def test_the_self_gc_caller_is_named(bed) -> None:
    """``nx self gc`` (the SessionStart sweep) names itself, so a reap in the log
    says which caller ran it."""
    tools, stub_bin, config, old, _new = bed
    _ps(stub_bin, "echo '  999 /usr/bin/vim unrelated.txt'")
    (tools / "previous").unlink()

    assert self_cmd.perform_self_gc(keep=1) is not None

    assert {r["caller"] for r in _log(config)} == {"self-gc"}


def test_a_dry_run_writes_nothing(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """A dry run deletes nothing and is the operator reading the plan; it is not a gc run to record."""
    tools = tmp_path / "tools"
    tools.mkdir()
    monkeypatch.setenv("NEXUS_CONFIG_DIR", str(tmp_path / "config"))
    _receipted(tools, "gen-A")
    _receipted(tools, "gen-B")

    gc_core.gc_generations(tools, keep=1, dry_run=True, snapshot="999 /usr/bin/vim\n")

    assert not (tmp_path / "config" / "logs" / "install-gc.log").exists()
