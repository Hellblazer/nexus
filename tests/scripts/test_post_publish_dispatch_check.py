# SPDX-License-Identifier: AGPL-3.0-or-later
"""tests/e2e/post-publish-dispatch-check.sh (nexus-0zsmg / shakeout-7.41.0-
ledger-projector-dead-on-cloud-2026-09-11): the real-dispatch leg of the
post-publish shakedown.

Drives the real script end to end against fixture ``.expectations`` TSV
ledgers and ``.tuple-projection.log`` files, with a stub ``nx`` on PATH
that answers ``tuple stats`` / ``tuple rd`` / ``tuple list`` deterministically
per scenario -- no real engine, no real Claude Code session. Covers the
pass path, each of the four named misses (a/b/c/d), and the three
prerequisite-absent (exit 2) cases: no ``nx``, no ledger file, no
session_id.
"""
from __future__ import annotations

import os
import stat
import subprocess
import sys
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parents[2]
SCRIPT = REPO_ROOT / "tests" / "e2e" / "post-publish-dispatch-check.sh"

SID = "sess-pp-dispatch-check"
AGENT_ID = "adispatch1234567890abc"
AGENT_TYPE = "Explore"

START_TS = "2026-09-11T08:00:00Z"
REPORTED_TS = "2026-09-11T08:01:00Z"
BEFORE_START_TS = "2026-09-11T07:00:00Z"
AFTER_START_TS = "2026-09-11T08:05:00Z"

_STUB_NX = '''#!/usr/bin/env python3
import os
import sys

args = sys.argv[1:]


def _emit(prefix):
    rc = int(os.environ.get(f"STUB_{prefix}_RC", "0"))
    if rc != 0:
        sys.stderr.write(os.environ.get(f"STUB_{prefix}_ERR", "stub nx: simulated failure") + "\\n")
        sys.exit(rc)
    sys.stdout.write(os.environ.get(f"STUB_{prefix}_JSON", "[]") + "\\n")
    sys.exit(0)


if args[:2] == ["tuple", "stats"]:
    _emit("TUPLE_STATS")
if args[:2] == ["tuple", "rd"]:
    _emit("TUPLE_RD")
if args[:2] == ["tuple", "list"]:
    _emit("TUPLE_LIST")

sys.stderr.write(f"stub nx: unhandled invocation {args!r}\\n")
sys.exit(9)
'''


def _tsv_row(ts: str, verb: str, *fields: str) -> str:
    return "\t".join([ts, verb, *fields])


def _write_tsv(path: Path, *, start: bool = True, reported: bool = True) -> None:
    lines = []
    if start:
        lines.append(_tsv_row(START_TS, "START", AGENT_ID, AGENT_TYPE))
    if reported:
        lines.append(_tsv_row(REPORTED_TS, "REPORTED", AGENT_ID))
    path.write_text("\n".join(lines) + ("\n" if lines else ""))


def _stub_bin_dir(tmp_path: Path) -> Path:
    bin_dir = tmp_path / "stub-bin"
    bin_dir.mkdir(exist_ok=True)
    nx = bin_dir / "nx"
    nx.write_text(_STUB_NX)
    nx.chmod(nx.stat().st_mode | stat.S_IEXEC | stat.S_IXGRP | stat.S_IXOTH)
    return bin_dir


def _good_stats_json() -> str:
    return f'{{"subspace": "ledger/{SID}", "total": 2}}'


def _good_rd_json() -> str:
    return (
        "["
        f'{{"keys": {{"agent_id": "{AGENT_ID}", "kind": "start"}}, '
        f'"dims": {{"agent_type": "{AGENT_TYPE}"}}}}, '
        f'{{"keys": {{"agent_id": "{AGENT_ID}", "kind": "report"}}, '
        f'"dims": {{"agent_type": "{AGENT_TYPE}"}}}}'
        "]"
    )


def _good_list_json() -> str:
    return (
        "["
        f'{{"subspace": "ledger/{SID}", "total": 2, '
        f'"newest_created_at": "{REPORTED_TS}"}}'
        "]"
    )


def _base_env(tmp_path: Path, *, with_stub_nx: bool = True) -> dict:
    env = dict(os.environ)
    env["XDG_STATE_HOME"] = str(tmp_path / "state")
    env.pop("HOME", None)
    env["HOME"] = str(tmp_path / "home")
    Path(env["HOME"]).mkdir(exist_ok=True)
    if with_stub_nx:
        stub_dir = _stub_bin_dir(tmp_path)
        env["PATH"] = f"{stub_dir}{os.pathsep}{env.get('PATH', '')}"
        env.setdefault("STUB_TUPLE_STATS_JSON", _good_stats_json())
        env.setdefault("STUB_TUPLE_RD_JSON", _good_rd_json())
        env.setdefault("STUB_TUPLE_LIST_JSON", _good_list_json())
    return env


def _ledger_dir(tmp_path: Path) -> Path:
    d = tmp_path / "state" / "nexus" / "orchestration"
    d.mkdir(parents=True, exist_ok=True)
    return d


def _run(sid: str | None, env: dict) -> subprocess.CompletedProcess[str]:
    args = [str(SCRIPT)]
    if sid is not None:
        args.append(sid)
    return subprocess.run(
        args, env=env, capture_output=True, text=True, timeout=60,
    )


class TestScriptShape:
    def test_executable(self) -> None:
        assert SCRIPT.exists() and os.access(SCRIPT, os.X_OK)


class TestPrerequisiteAbsent:
    """exit 2, never a silent skip-pass (nexus-moht0)."""

    def test_missing_session_id_arg(self, tmp_path) -> None:
        env = _base_env(tmp_path)
        proc = _run(None, env)
        assert proc.returncode == 2, proc.stdout + proc.stderr
        assert "prerequisite absent" in proc.stderr
        assert "usage" in proc.stderr

    def test_no_nx_on_path(self, tmp_path) -> None:
        env = _base_env(tmp_path, with_stub_nx=False)
        # A minimal PATH that cannot plausibly resolve `nx` on any box.
        env["PATH"] = "/usr/bin:/bin"
        proc = _run(SID, env)
        assert proc.returncode == 2, proc.stdout + proc.stderr
        assert "no nx on PATH" in proc.stderr

    def test_no_ledger_file_for_session(self, tmp_path) -> None:
        env = _base_env(tmp_path)
        _ledger_dir(tmp_path)  # dir exists, but no <sid>.expectations file
        proc = _run(SID, env)
        assert proc.returncode == 2, proc.stdout + proc.stderr
        assert "no ledger file for session" in proc.stderr

    def test_invalid_session_id_charset(self, tmp_path) -> None:
        env = _base_env(tmp_path)
        proc = _run("../../etc/passwd", env)
        assert proc.returncode == 2, proc.stdout + proc.stderr
        assert "invalid session_id" in proc.stderr


class TestPassPath:
    def test_all_four_checks_pass(self, tmp_path) -> None:
        env = _base_env(tmp_path)
        tsv = _ledger_dir(tmp_path) / f"{SID}.expectations"
        _write_tsv(tsv)
        proc = _run(SID, env)
        assert proc.returncode == 0, proc.stdout + proc.stderr
        assert "POST-PUBLISH DISPATCH CHECK PASSED" in proc.stdout
        assert "COUNT tsv_start=1 tsv_reported=1" in proc.stdout
        assert "COUNT space_stats_total=2" in proc.stdout
        assert "space_start_match=1 space_report_match=1" in proc.stdout
        assert "MISS" not in proc.stdout

    def test_pass_survives_an_old_skip_line_before_the_start_row(self, tmp_path) -> None:
        """A SKIP line OLDER than the session's own START row is stale
        history from an earlier session/run, not this dispatch's failure --
        must not fail criterion (c)."""
        env = _base_env(tmp_path)
        tsv = _ledger_dir(tmp_path) / f"{SID}.expectations"
        _write_tsv(tsv)
        log = _ledger_dir(tmp_path) / f"{SID}.tuple-projection.log"
        log.write_text(f"{BEFORE_START_TS}\tSKIP kind=start agent_id=stale old failure\n")
        proc = _run(SID, env)
        assert proc.returncode == 0, proc.stdout + proc.stderr
        assert "POST-PUBLISH DISPATCH CHECK PASSED" in proc.stdout


class TestMissA:
    def test_missing_reported_row(self, tmp_path) -> None:
        env = _base_env(tmp_path)
        tsv = _ledger_dir(tmp_path) / f"{SID}.expectations"
        _write_tsv(tsv, start=True, reported=False)
        proc = _run(SID, env)
        assert proc.returncode == 1, proc.stdout + proc.stderr
        assert "POST-PUBLISH DISPATCH CHECK FAILED" in proc.stdout
        assert "MISS: (a)" in proc.stdout
        assert "zero REPORTED rows" in proc.stdout


class TestMissB:
    def test_dead_projector_empty_tuple_space(self, tmp_path) -> None:
        """The real 7.41.0 shape: the TSV side is clean but the async
        projection never reached the engine at all."""
        env = _base_env(tmp_path)
        env["STUB_TUPLE_STATS_JSON"] = f'{{"subspace": "ledger/{SID}", "total": 0}}'
        env["STUB_TUPLE_RD_JSON"] = "[]"
        env["STUB_TUPLE_LIST_JSON"] = "[]"
        tsv = _ledger_dir(tmp_path) / f"{SID}.expectations"
        _write_tsv(tsv)
        proc = _run(SID, env)
        assert proc.returncode == 1, proc.stdout + proc.stderr
        assert "COUNT space_stats_total=0" in proc.stdout
        assert "MISS: (b)" in proc.stdout
        assert "reports total=0 tuples" in proc.stdout
        assert "no kind=start tuple" in proc.stdout
        assert "no kind=report tuple" in proc.stdout

    def test_tuple_rd_transport_failure(self, tmp_path) -> None:
        env = _base_env(tmp_path)
        env["STUB_TUPLE_RD_RC"] = "1"
        env["STUB_TUPLE_RD_ERR"] = "connection refused"
        tsv = _ledger_dir(tmp_path) / f"{SID}.expectations"
        _write_tsv(tsv)
        proc = _run(SID, env)
        assert proc.returncode == 1, proc.stdout + proc.stderr
        assert "MISS: (b) nx tuple rd" in proc.stdout
        assert "connection refused" in proc.stdout
        # A transport failure must not ALSO be double-reported as a
        # separate "no kind=start"/"no kind=report" miss.
        assert "no kind=start tuple" not in proc.stdout


class TestMissC:
    def test_skip_line_newer_than_last_start(self, tmp_path) -> None:
        env = _base_env(tmp_path)
        tsv = _ledger_dir(tmp_path) / f"{SID}.expectations"
        _write_tsv(tsv)
        log = _ledger_dir(tmp_path) / f"{SID}.tuple-projection.log"
        log.write_text(
            f"{AFTER_START_TS}\tSKIP kind=start agent_id={AGENT_ID} "
            "no service endpoint resolvable\n"
        )
        proc = _run(SID, env)
        assert proc.returncode == 1, proc.stdout + proc.stderr
        assert "MISS: (c)" in proc.stdout
        assert "SKIP line(s) newer than the last TSV START" in proc.stdout


class TestMissD:
    def test_space_fallback_when_tuple_list_unreachable(self, tmp_path) -> None:
        """(b) is decided by `tuple stats`/`tuple rd` for THIS subspace;
        (d) is decided by `expectations_census`'s own `tuple list` call --
        the two must be independently checkable, so a `tuple list` outage
        alone should trip only (d)."""
        env = _base_env(tmp_path)
        env["STUB_TUPLE_LIST_RC"] = "1"
        env["STUB_TUPLE_LIST_ERR"] = "engine unreachable"
        tsv = _ledger_dir(tmp_path) / f"{SID}.expectations"
        _write_tsv(tsv)
        proc = _run(SID, env)
        assert proc.returncode == 1, proc.stdout + proc.stderr
        assert "MISS: (d)" in proc.stdout
        assert "SPACE_FALLBACK" in proc.stdout
        assert "MISS: (b)" not in proc.stdout

    def test_space_blindspot_when_tuple_list_empty(self, tmp_path) -> None:
        env = _base_env(tmp_path)
        env["STUB_TUPLE_LIST_JSON"] = "[]"
        tsv = _ledger_dir(tmp_path) / f"{SID}.expectations"
        _write_tsv(tsv)
        proc = _run(SID, env)
        assert proc.returncode == 1, proc.stdout + proc.stderr
        assert "MISS: (d)" in proc.stdout
        assert "SPACE_BLINDSPOT" in proc.stdout


@pytest.mark.skipif(sys.platform == "win32", reason="posix permission bits only")
class TestExactCounts:
    def test_multiple_start_and_reported_rows_are_counted_exactly(self, tmp_path) -> None:
        env = _base_env(tmp_path)
        tsv = _ledger_dir(tmp_path) / f"{SID}.expectations"
        tsv.write_text(
            "\n".join(
                [
                    _tsv_row(START_TS, "START", AGENT_ID, AGENT_TYPE),
                    _tsv_row(START_TS, "START", "aother0000000000000000", AGENT_TYPE),
                    _tsv_row(REPORTED_TS, "REPORTED", AGENT_ID),
                ]
            )
            + "\n"
        )
        proc = _run(SID, env)
        assert "COUNT tsv_start=2 tsv_reported=1" in proc.stdout, proc.stdout
