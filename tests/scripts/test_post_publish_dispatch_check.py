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
import re
import stat
import subprocess
import sys
import time
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parents[2]
SCRIPT = REPO_ROOT / "tests" / "e2e" / "post-publish-dispatch-check.sh"


def _extract_bash_functions(*names: str) -> str:
    """Pull the named top-level ``name() { ... }`` function bodies out of
    the REAL script's source, verbatim, in the order given. Used to unit
    test a helper function's actual text directly (with a controlled
    dependency override) rather than a reimplementation that could drift
    from what the script really runs. Every function in this script
    follows the same style throughout (opening brace on the def line,
    closing brace alone at column 0), so a non-greedy match up to the
    first column-0 ``}`` is exact for all of them."""
    text = SCRIPT.read_text()
    out = []
    for name in names:
        m = re.search(rf"^{re.escape(name)}\(\) \{{\n.*?\n\}}\n", text, re.M | re.S)
        assert m, f"could not find function {name!r} in {SCRIPT}; extraction regex may have rotted"
        out.append(m.group(0))
    return "\n".join(out)


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
        assert "PATH has no nx" in proc.stderr

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


class TestRemedyANotFoundListsCandidates:
    """nexus-7m6uc remedy (a): a ledger-not-found miss lists every ledger
    that DOES exist, newest first, with mtime and START/REPORTED counts --
    self-solving, so the runner sees the real candidate immediately instead
    of re-guessing blind."""

    def test_lists_existing_ledgers_when_named_session_not_found(self, tmp_path) -> None:
        env = _base_env(tmp_path)
        other_sid = "sess-other-1234567890"
        _write_tsv(_ledger_dir(tmp_path) / f"{other_sid}.expectations")
        proc = _run("sess-does-not-exist", env)
        assert proc.returncode == 2, proc.stdout + proc.stderr
        assert "no ledger file for session 'sess-does-not-exist'" in proc.stderr
        assert "Ledgers present under" in proc.stderr
        assert other_sid in proc.stderr
        assert "start=1 reported=1" in proc.stderr

    def test_no_listing_line_when_nothing_exists_at_all(self, tmp_path) -> None:
        env = _base_env(tmp_path)
        _ledger_dir(tmp_path)  # dir created, but holds no ledger at all
        proc = _run(SID, env)
        assert proc.returncode == 2, proc.stdout + proc.stderr
        assert "Ledgers present under" not in proc.stderr


class TestRemedyBAutoDiscovery:
    """nexus-7m6uc remedy (b): called with no argument, the script picks
    the sole ledger with recent agent-dispatch activity, and refuses --
    naming every candidate -- rather than guess on zero or more than one."""

    def test_sole_recent_ledger_is_auto_selected(self, tmp_path) -> None:
        env = _base_env(tmp_path)
        _write_tsv(_ledger_dir(tmp_path) / f"{SID}.expectations")
        proc = _run(None, env)
        assert proc.returncode == 0, proc.stdout + proc.stderr
        assert f"AUTO-DISCOVERED session_id={SID}" in proc.stderr
        assert "POST-PUBLISH DISPATCH CHECK PASSED" in proc.stdout

    def test_ambiguous_when_two_ledgers_are_recent(self, tmp_path) -> None:
        env = _base_env(tmp_path)
        second_sid = "sess-second-9876543210"
        d = _ledger_dir(tmp_path)
        _write_tsv(d / f"{SID}.expectations")
        _write_tsv(d / f"{second_sid}.expectations")
        proc = _run(None, env)
        assert proc.returncode == 2, proc.stdout + proc.stderr
        assert "AMBIGUOUS" in proc.stderr
        assert SID in proc.stderr
        assert second_sid in proc.stderr

    def test_zero_recent_candidates_lists_what_exists_and_refuses(self, tmp_path) -> None:
        env = _base_env(tmp_path)
        env["POST_PUBLISH_DISPATCH_RECENT_SECONDS"] = "1"
        _write_tsv(_ledger_dir(tmp_path) / f"{SID}.expectations")
        time.sleep(2)
        proc = _run(None, env)
        assert proc.returncode == 2, proc.stdout + proc.stderr
        assert "No ledger has agent-dispatch activity in the last" in proc.stderr
        assert SID in proc.stderr

    def test_no_ledgers_at_all_gives_the_usage_shaped_message(self, tmp_path) -> None:
        env = _base_env(tmp_path)
        proc = _run(None, env)
        assert proc.returncode == 2, proc.stdout + proc.stderr
        assert "no ledger files exist at all" in proc.stderr
        assert "usage" in proc.stderr

    def test_explicit_session_id_skips_auto_discovery_entirely(self, tmp_path) -> None:
        """A second, unrelated ledger existing must not make an EXPLICIT
        session_id call ambiguous -- auto-discovery only runs when no
        argument is given at all."""
        env = _base_env(tmp_path)
        d = _ledger_dir(tmp_path)
        _write_tsv(d / f"{SID}.expectations")
        _write_tsv(d / "sess-unrelated-0000000000.expectations")
        proc = _run(SID, env)
        assert proc.returncode == 0, proc.stdout + proc.stderr
        assert "AUTO-DISCOVERED" not in proc.stderr
        assert "AMBIGUOUS" not in proc.stderr


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


class TestLedgerListingSurvivesAVanishingLedger:
    """nexus-7m6uc round 2 (review finding, IMPORTANT): a TOCTOU race --
    the ledger file existed at the glob/``-f`` check but is gone (or
    otherwise unstattable) by the time ``_epoch_mtime`` runs on it, e.g. a
    peer session's ledger reaped mid-scan -- used to abort the WHOLE
    SCRIPT silently under ``set -euo pipefail``: no message, exit 1,
    indistinguishable from a genuine MISS.

    Drives the REAL ``_ledger_recency_epoch`` and ``_ledger_listing``
    function bodies, extracted verbatim from the script (never
    reimplemented), with a deterministic failure injection: ``_epoch_mtime``
    is overridden to fail for exactly one named path. This tests the actual
    fixed code with no dependency on winning a real race, so it cannot be
    flaky -- the injection point is code, not timing.
    """

    def _run_extracted(self, tmp_path: Path, *, failing_glob: str) -> subprocess.CompletedProcess[str]:
        # nexus-7m6uc round 3: extract the REAL (now-portable, python3-based)
        # _epoch_mtime too, renamed to _real_epoch_mtime so the injection
        # wrapper below can dispatch to it for every non-raced path --
        # never a hand-duplicated reimplementation that could drift from
        # the production function (which is exactly what this class's own
        # docstring promises for the other two extracted functions).
        epoch_mtime_src = _extract_bash_functions("_epoch_mtime").replace(
            "_epoch_mtime()", "_real_epoch_mtime()", 1,
        )
        functions_src = _extract_bash_functions("_ledger_recency_epoch", "_ledger_listing")
        script = f"""
set -euo pipefail
STATE_DIR={tmp_path!s}

{epoch_mtime_src}
{functions_src}

# Deterministic race injection (test-only): fails for the one path being
# raced, succeeds for every other -- see the class docstring for why this
# replaces a true concurrent race.
_epoch_mtime() {{
    case "$1" in
        {failing_glob}) return 1 ;;
        *) _real_epoch_mtime "$1" ;;
    esac
}}

_ledger_listing
"""
        return subprocess.run(
            ["bash", "-c", script], capture_output=True, text=True, timeout=30,
        )

    def test_a_vanished_ledger_is_skipped_with_a_note_not_a_silent_abort(self, tmp_path) -> None:
        good_sid = "sess-good-1111111111"
        vanished_sid = "sess-vanished-2222222222"
        _write_tsv(tmp_path / f"{good_sid}.expectations")
        _write_tsv(tmp_path / f"{vanished_sid}.expectations")
        proc = self._run_extracted(tmp_path, failing_glob=f"*{vanished_sid}*")
        assert proc.returncode == 0, (
            "the scan aborted instead of skipping the vanished ledger: "
            f"rc={proc.returncode} stdout={proc.stdout!r} stderr={proc.stderr!r}"
        )
        assert good_sid in proc.stdout
        assert vanished_sid not in proc.stdout
        assert "vanished or became unreadable mid-scan" in proc.stderr

    def test_the_good_ledger_alone_still_produces_a_correct_listing_line(self, tmp_path) -> None:
        """Non-vacuity: the skip must not ALSO eat the survivor's own
        counts -- a broken split could skip everything and still exit 0."""
        good_sid = "sess-good-3333333333"
        vanished_sid = "sess-vanished-4444444444"
        _write_tsv(tmp_path / f"{good_sid}.expectations")
        _write_tsv(tmp_path / f"{vanished_sid}.expectations")
        proc = self._run_extracted(tmp_path, failing_glob=f"*{vanished_sid}*")
        assert proc.returncode == 0, proc.stdout + proc.stderr
        lines = [ln for ln in proc.stdout.splitlines() if ln.strip()]
        assert len(lines) == 1, f"expected exactly one surviving listing line, got: {lines!r}"
        # _ledger_listing's own raw TSV shape: epoch, sid, start count, reported count.
        fields = lines[0].split("\t")
        assert fields[1] == good_sid, fields
        assert fields[2] == "1" and fields[3] == "1", fields
