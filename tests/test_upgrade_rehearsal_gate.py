# SPDX-License-Identifier: AGPL-3.0-or-later
"""Offline self-test for the upgrade rehearsal gate's judgement and its safety rails (nexus-f3yqd.5).

The per-start upgrade ceilings (and the pending one), the verdict states and the combined gate line,
the non-default flags, the store row count and version checks, the watchdog's exit handling, the
postmaster reaper and the wrapper script's last line. No network, no engine, no uv: fake commands.
"""

from __future__ import annotations

import argparse
import importlib.util
import os
import shutil
import signal
import subprocess
import sys
import time
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent
DRIVER = ROOT / "scripts" / "upgrade_rehearsal.py"
WRAPPER = ROOT / "tests" / "e2e" / "upgrade-rehearsal.sh"


def _load():
    spec = importlib.util.spec_from_file_location("_upgrade_rehearsal_gate", DRIVER)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    sys.modules["_upgrade_rehearsal_gate"] = module
    spec.loader.exec_module(module)
    return module


ur = _load()

PREPARED = {"verdict": "prepared", "collections": {"a": 60_000}}


def _steps(*, upgrade_ceiling_pending: bool = False, bad: str | None = None):
    out = []
    for n in ur.JUDGED_STEPS:
        s = ur.StepResult(name=n, status="ok", exit_code=0, wall_s=1.0)
        if n == "upgrade":
            s.ceiling_pending = upgrade_ceiling_pending
        if n == bad:
            s.status = "ceiling"
        out.append(s)
    return out


def _verdict(version: str, **kw):
    findings = kw.pop("findings", [])
    return ur.build_verdict(
        from_version=version,
        target="wheel w",
        steps=kw.pop("steps", None) or _steps(),
        findings=findings,
        prepare=PREPARED,
        floor=1,
    )


# --- the per-start upgrade ceiling -----------------------------------------------------------------


def test_the_upgrade_ceiling_is_per_start_and_a_none_entry_is_pending(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    assert ur.start_kind("8.0.0") == "newest-below"
    assert ur.start_kind("8.0.0.post1") == "newest-below"
    assert ur.start_kind("7.99.0") == "last-7x"
    assert ur.start_kind("7.77.0") == "last-7x"
    assert ur.start_kind("7.76.0") == "pre-pci"
    # a pending entry is a None in the table; the test sets it, so a real number later breaks nothing
    monkeypatch.setitem(ur.UPGRADE_CEILINGS_S, "pre-pci", None)
    assert ur.upgrade_ceiling_for("8.0.0") == ur.UPGRADE_CEILINGS_S["newest-below"]
    assert ur.upgrade_ceiling_for("7.99.0") == ur.UPGRADE_CEILINGS_S["last-7x"]
    assert ur.upgrade_ceiling_for("7.76.0") is None, "no interim guess for 7.76.0"
    assert ur.upgrade_ceiling_for("7.76.0", override=300.0) == 300.0
    monkeypatch.setitem(ur.UPGRADE_CEILINGS_S, "pre-pci", 700.0)
    assert ur.upgrade_ceiling_for("7.76.0") == 700.0, "a set number is a ceiling"
    assert not hasattr(ur, "UPGRADE_CEILING_S"), "the single ceiling is gone"
    assert ur.UPGRADE_PENDING_HANG_GUARD_S > 662.0, (
        "a hang guard above the measured 460-662 s"
    )


def test_every_measured_figure_is_recorded_beside_the_ceilings() -> None:
    src = DRIVER.read_text()
    for figure in (
        "12.7 s",
        "12.6-14.2 s",
        "460-662 s",
        "d258282c6",
        "211 s",
        "e979863a6",
        "nexus-mjmjl",
        "86,000",
        "140,000",
        "209.1 s",
    ):
        assert figure in src, figure


def test_the_pre_pci_ceiling_is_set_from_the_develop_measurement() -> None:
    # 2 x the 209-211 s measured with the rung-scan fix; the pre-fix 460-662 s must still fail
    assert ur.UPGRADE_CEILINGS_S["pre-pci"] == 420.0
    assert ur.upgrade_ceiling_for("7.76.0") == 420.0
    assert ur.UPGRADE_CEILINGS_S["pre-pci"] < 460.0


# --- one start's verdict and the gate's last line ----------------------------------------------------


def test_a_start_with_a_pending_ceiling_is_pending_not_passed_even_when_every_step_is_ok() -> (
    None
):
    v = _verdict("7.76.0", steps=_steps(upgrade_ceiling_pending=True))
    assert v["state"] == "PENDING" and v["passed"] is False
    assert v["pending"] == ["upgrade"]
    line = ur.verdict_line(v)
    assert "PENDING" in line and "UPGRADE_CEILINGS_S" in line and "PASSED" not in line
    assert "PENDING" in ur.format_table(v)
    assert "UPGRADE_CEILINGS_S" in ur.format_table(v)
    assert "mjmjl" not in ur.UPGRADE_CEILING_PENDING_NOTE, (
        "the note covers any None entry, not one start's wait"
    )


def test_a_failed_step_or_a_finding_outranks_pending() -> None:
    v = _verdict("7.76.0", steps=_steps(upgrade_ceiling_pending=True, bad="doctor"))
    assert v["state"] == "FAILED"
    assert _verdict("7.76.0", findings=["x"])["state"] == "FAILED"
    assert _verdict("8.0.0")["state"] == "PASSED"


def test_the_combined_line_is_the_only_line_carrying_the_gate_marker() -> None:
    ok, ok2 = _verdict("8.0.0"), _verdict("7.99.0")
    for v in (ok, _verdict("7.76.0", steps=_steps(upgrade_ceiling_pending=True))):
        assert not ur.verdict_line(v).startswith(ur.GATE_MARK)
    word, line, code = ur.gate_result([ok, ok2])
    assert (word, code) == ("PASSED", 0)
    assert line == "UPGRADE REHEARSAL GATE: PASSED: from 8.0.0, 7.99.0 to wheel w"


def test_a_pending_start_makes_the_gate_not_run_exit_2_and_names_the_ceiling() -> None:
    pend = _verdict("7.76.0", steps=_steps(upgrade_ceiling_pending=True))
    word, line, code = ur.gate_result([_verdict("8.0.0"), _verdict("7.99.0"), pend])
    assert (word, code) == ("NOT RUN", 2)
    assert line.startswith("UPGRADE REHEARSAL GATE: NOT RUN")
    assert "ceiling" in line and "7.76.0" in line and "UPGRADE_CEILINGS_S" in line
    assert "PASSED" not in line.split("passed:")[0]


def test_a_real_failure_outranks_a_pending_ceiling_and_exits_1() -> None:
    pend = _verdict("7.76.0", steps=_steps(upgrade_ceiling_pending=True))
    bad = _verdict("8.0.0", steps=_steps(bad="upgrade"))
    word, line, code = ur.gate_result([bad, pend])
    assert (word, code) == ("FAILED", 1)
    assert "8.0.0 (at upgrade)" in line and "pending: 7.76.0" in line
    assert ur.gate_result([])[0] == "FAILED"


def test_a_non_default_flag_turns_a_pass_into_not_the_release_gate() -> None:
    word, line, code = ur.gate_result(
        [_verdict("8.0.0")], ["--standin", "--rows-floor"]
    )
    assert (word, code) == ("NOT THE RELEASE GATE", 2)
    assert "NOT THE RELEASE GATE (--standin, --rows-floor)" in line
    assert "PASSED" not in line
    # a failure stays a failure, and says what was relaxed
    _, fline, fcode = ur.gate_result(
        [_verdict("8.0.0", steps=_steps(bad="upgrade"))], ["--silence-limit"]
    )
    assert fcode == 1 and "NOT THE RELEASE GATE: --silence-limit" in fline


def _non_default_argv(action: argparse.Action) -> list[str]:
    """argv that sets ``action``'s option to a value other than its default."""
    if isinstance(action, argparse.BooleanOptionalAction):
        return [
            action.option_strings[1]
        ]  # --no-x: the default is None, so False differs
    opt = action.option_strings[0]
    if action.nargs == 0:  # store_true
        return [opt]
    if action.type in (int, float):
        return [opt, str((action.default or 0) + 1)]
    return [opt, "x"]


def test_every_option_outside_the_neutral_set_is_a_non_default_flag_when_set() -> None:
    ap = ur._build_parser()
    base = ["--work", "w", "--target-release", "9.9.9"]
    assert ur.non_default_flags(ap, ap.parse_args(base)) == []
    # the neutral set is pinned: adding to it silently stops a relaxing option counting
    assert ur._NEUTRAL_DESTS == {
        "work",
        "target_wheel",
        "target_release",
        "json_out",
        "keep_work",
        "help",
    }
    assert (
        ur.non_default_flags(ap, ap.parse_args([*base, "--json-out", "x.json"])) == []
    ), "a neutral option is not a non-default flag"
    assert ur.non_default_flags(ap, ap.parse_args([*base, "--keep-work"])) == [], (
        "keeping the store changes nothing that is judged"
    )
    examined = 0
    for action in ap._actions:
        if not action.option_strings or action.dest in ur._NEUTRAL_DESTS:
            continue
        examined += 1
        extra = _non_default_argv(action)
        got = ur.non_default_flags(ap, ap.parse_args([*base, *extra]))
        assert got == [extra[0]], (action.dest, extra, got)
    # non-vacuity: the parser has at least this many options outside the neutral set
    assert examined >= 18, examined
    for dest in ("restart_gap", "profile_upgrade", "pci_collections", "pci_end_bound"):
        assert any(a.dest == dest for a in ap._actions), dest


@pytest.fixture
def restore_signal_handlers():
    """main() installs SIGTERM/SIGHUP handlers in the pytest process: put the old ones back."""
    saved = {s: signal.getsignal(s) for s in (signal.SIGTERM, signal.SIGHUP)}
    yield
    for sig, handler in saved.items():
        signal.signal(sig, handler)


def test_a_wheel_whose_version_is_already_published_is_refused(
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
    restore_signal_handlers: None,
) -> None:
    wheel = tmp_path / "conexus-8.0.0-py3-none-any.whl"
    wheel.write_bytes(b"x")
    rc = ur.main(
        [
            "--work",
            str(tmp_path / "w"),
            "--target-wheel",
            str(wheel),
            "--published-versions",
            "7.76.0,7.99.0,8.0.0",
        ]
    )
    err = capsys.readouterr().err
    assert rc == 2 and "UPGRADE REHEARSAL GATE: NOT RUN" in err
    assert "already published" in err and "8.0.0" in err


# --- what the store holds, and which client ran ---------------------------------------------------------


def test_the_stores_rows_must_be_counted_reach_the_floor_and_survive() -> None:
    assert ur.store_rows_findings(86_000, 86_000, floor=50_000) == []
    assert ur.store_rows_findings(86_000, 90_000, floor=50_000) == []
    assert any(
        "below the floor" in f for f in ur.store_rows_findings(10, 10, floor=50_000)
    )
    lost = ur.store_rows_findings(86_000, 85_999, floor=50_000)
    assert len(lost) == 1 and "lost rows" in lost[0] and "1 fewer" in lost[0]
    assert any(
        "before" in f for f in ur.store_rows_findings(None, 60_000, floor=50_000)
    )
    assert any("after" in f for f in ur.store_rows_findings(60_000, None, floor=50_000))


def test_count_rows_reads_the_chunks_table_and_retries_while_pg_is_down() -> None:
    answers = [None, None, [["86000"]]]
    asked: list[str] = []
    now = [0.0]

    def sql(q: str):
        asked.append(q)
        return answers.pop(0)

    n = ur.count_rows(
        sql, clock=lambda: now[0], sleep=lambda s: now.__setitem__(0, now[0] + s)
    )
    assert n == 86000 and "nexus.chunks" in asked[0] and len(asked) == 3
    with pytest.raises(ur.ProbeError):
        ur.count_rows(
            lambda q: None,
            bound_s=2.0,
            clock=lambda: now[0],
            sleep=lambda s: now.__setitem__(0, now[0] + s),
        )


def test_the_client_after_the_package_upgrade_must_be_the_candidate() -> None:
    assert ur.version_findings("nx, version 8.1.0\n", "8.1.0") == []
    assert ur.version_findings("\x1b[1m8.1.0\x1b[0m", "8.1.0") == []
    bad = ur.version_findings("nx, version 8.0.0", "8.1.0")
    assert len(bad) == 1 and "8.1.0" in bad[0]
    assert ur.version_findings("nx 18.1.0", "8.1.0"), (
        "a longer version containing it is not it"
    )
    assert ur.version_findings("nx 8.1.0.1", "8.1.0"), (
        "nor a longer one starting with it"
    )


# --- the watchdog ---------------------------------------------------------------------------------------


def test_the_silence_after_the_last_output_counts_up_to_the_exit(
    tmp_path: Path,
) -> None:
    code = "import time\nprint('hi', flush=True)\ntime.sleep(0.8)\n"
    r = ur.run_step(
        "tail",
        [sys.executable, "-u", "-c", code],
        env=dict(os.environ),
        cwd=tmp_path,
        silence_limit_s=5.0,
        ceiling_s=20.0,
        log_dir=tmp_path / "logs",
    )
    assert r.status == "ok"
    assert 0.7 <= r.longest_gap_s < 2.5, r


def test_a_driver_stopped_mid_step_takes_the_steps_process_group_with_it(
    tmp_path: Path,
) -> None:
    marker = tmp_path / "child.pid"
    code = (
        "import subprocess, sys, time\n"
        "p = subprocess.Popen([sys.executable, '-c', 'import time; time.sleep(60)'])\n"
        f"open({str(marker)!r}, 'w').write(str(p.pid))\n"
        "print('up', flush=True)\n"
        "time.sleep(60)\n"
    )
    calls = [0]

    def clock() -> float:
        calls[0] += 1
        if marker.exists() and calls[0] > 30:
            raise SystemExit(143)  # what the SIGTERM handler raises
        return time.monotonic()

    with pytest.raises(SystemExit):
        ur.run_step(
            "x",
            [sys.executable, "-u", "-c", code],
            env=dict(os.environ),
            cwd=tmp_path,
            silence_limit_s=None,
            ceiling_s=60.0,
            log_dir=tmp_path / "logs",
            clock=clock,
        )
    pid = int(marker.read_text())
    deadline = time.monotonic() + 10
    while time.monotonic() < deadline:
        try:
            os.kill(pid, 0)
        except ProcessLookupError:
            return
        time.sleep(0.1)
    os.kill(pid, signal.SIGKILL)
    pytest.fail("the step's child outlived the driver's exit")


def test_sigterm_and_sighup_unwind_the_driver_through_its_finally_blocks(
    tmp_path: Path,
) -> None:
    flag = tmp_path / "ready"
    done = tmp_path / "finally-ran"
    script = tmp_path / "victim.py"
    script.write_text(
        "import importlib.util, sys, time, pathlib\n"
        f"spec = importlib.util.spec_from_file_location('ur', {str(DRIVER)!r})\n"
        "ur = importlib.util.module_from_spec(spec); sys.modules['ur'] = ur; spec.loader.exec_module(ur)\n"
        "ur.install_exit_handlers()\n"
        "try:\n"
        f"    pathlib.Path({str(flag)!r}).write_text('1')\n"
        "    time.sleep(60)\n"
        "finally:\n"
        f"    pathlib.Path({str(done)!r}).write_text('1')\n"
    )
    for sig in (signal.SIGTERM, signal.SIGHUP):
        flag.unlink(missing_ok=True)
        done.unlink(missing_ok=True)
        proc = subprocess.Popen([sys.executable, str(script)])
        for _ in range(100):
            if flag.exists():
                break
            time.sleep(0.1)
        assert flag.exists()
        proc.send_signal(sig)
        assert proc.wait(timeout=20) == 128 + sig
        assert done.exists(), "the finally block (the stack stop and the reap) must run"


# --- the environment a step sees ---------------------------------------------------------------------------


def test_the_step_environment_is_exactly_this_key_set(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    for k in (
        "USER",
        "LOGNAME",
        "LANG",
        "HTTPS_PROXY",
        "HTTP_PROXY",
        "https_proxy",
        "http_proxy",
    ):
        monkeypatch.delenv(k, raising=False)
    monkeypatch.setenv("NX_VOYAGE_API_KEY", "x")
    monkeypatch.setenv("NEXUS_SERVICE_JAR", "x")
    monkeypatch.setenv("VOYAGE_API_KEY", "x")
    env = ur.build_env(tmp_path / "home", tmp_path / "config")
    assert set(env) == {
        "HOME",
        "PATH",
        "TERM",
        "NEXUS_CONFIG_DIR",
        "NX_LOCAL",
        "NX_NO_TELEMETRY",
        "COLUMNS",
        "LINES",
    }
    assert env["NX_LOCAL"] == "1" and env["NX_NO_TELEMETRY"] == "1"
    assert env["HOME"] == str(tmp_path / "home") and env["NEXUS_CONFIG_DIR"] == str(
        tmp_path / "config"
    )
    assert int(env["COLUMNS"]) >= 200, "rich must not wrap the sentinel"
    monkeypatch.setenv("USER", "u")
    monkeypatch.setenv("HTTPS_PROXY", "p")
    env = ur.build_env(tmp_path / "home", tmp_path / "config")
    assert env["USER"] == "u" and env["HTTPS_PROXY"] == "p"


# --- the postmaster reaper -------------------------------------------------------------------------------------


def _pidfile(run_dir: Path, pid: int) -> None:
    d = run_dir / "config" / "postgres"
    d.mkdir(parents=True, exist_ok=True)
    (d / "postmaster.pid").write_text(f"{pid}\n{d}\n")


def _proc_cmdline(*args: str) -> bytes:
    """The bytes /proc/<pid>/cmdline holds: each argument ended by a NUL."""
    return b"".join(a.encode() + b"\0" for a in args)


def _ours(raw: bytes, run: Path) -> bool:
    """The reaper's own path: the argv read from /proc bytes by the driver's parser."""
    return ur.is_our_postmaster(ur.parse_cmdline(raw), run)


def test_only_a_postgres_with_its_data_dir_under_the_run_dir_is_ours(
    tmp_path: Path,
) -> None:
    run = tmp_path / "from-8.0.0"
    data = run / "config" / "postgres"
    data.mkdir(parents=True)
    assert _ours(_proc_cmdline("/x/bundle/bin/postgres", "-D", str(data)), run)
    assert _ours(_proc_cmdline("postgres", f"-D{data}"), run)
    spaced = (
        run / "config" / "post gres"
    )  # an argument with a space: only the NUL bytes keep it whole
    assert _ours(_proc_cmdline("/x/bin/postgres", "-D", str(spaced)), run)
    assert not _ours(
        _proc_cmdline("/x/bin/postgres", "-D", str(tmp_path / "other")), run
    )
    assert not _ours(_proc_cmdline("/x/bin/sleep", "-D", str(data)), run)
    assert not _ours(
        _proc_cmdline("postgres", "-D", str(tmp_path / "from-8.0.0-evil")), run
    )
    assert not _ours(b"", run)
    assert not ur.is_our_postmaster(None, run) and not ur.is_our_postmaster([], run)


def test_a_proc_cmdline_is_split_on_its_nuls_and_an_empty_argument_dropped() -> None:
    assert ur.parse_cmdline(b"a\0b c\0-Dx\0") == ["a", "b c", "-Dx"]
    assert ur.parse_cmdline(b"a\0\0b") == ["a", "b"]
    assert ur.parse_cmdline(b"") == []
    assert ur.parse_cmdline(b"p\0\xff\0") == ["p", "\ufffd"], (
        "a byte that is not UTF-8 never raises"
    )


def test_cmdline_of_reads_a_live_process_through_the_real_path() -> None:
    victim = subprocess.Popen(["sleep", "60"], start_new_session=True)
    try:
        assert ur.cmdline_of(victim.pid) == ["sleep", "60"]
    finally:
        victim.kill()
        victim.wait()
    assert ur.cmdline_of(victim.pid) is None


def _spawn_sleeper() -> subprocess.Popen:
    return subprocess.Popen(["sleep", "60"], start_new_session=True)


def _reaper_sleep(proc: subprocess.Popen, ticks: list[float]):
    def sleep(dt: float) -> None:
        ticks.append(dt)
        proc.poll()  # reap the zombie the way init would

    return sleep


def test_a_pid_that_is_not_our_postgres_is_not_signalled(tmp_path: Path) -> None:
    run = tmp_path / "from-8.0.0"
    victim = _spawn_sleeper()
    try:
        _pidfile(run, victim.pid)
        notes = ur._reap_postmasters(
            run, grace_s=1.0, cmdline=lambda pid: ["sleep", "60"]
        )
        assert victim.poll() is None, "a reused pid must never be killed"
        assert (
            len(notes) == 1
            and "left alone" in notes[0]
            and not notes[0].startswith("stray postmaster")
        )
    finally:
        victim.kill()
        victim.wait()


def test_a_stray_postmaster_gets_sigint_and_then_a_bounded_grace(
    tmp_path: Path,
) -> None:
    run = tmp_path / "from-8.0.0"
    victim = _spawn_sleeper()
    ticks: list[float] = []
    try:
        _pidfile(run, victim.pid)
        data = str(run / "config" / "postgres")
        notes = ur._reap_postmasters(
            run,
            grace_s=30.0,
            cmdline=lambda pid: ["/b/postgres", "-D", data],
            sleep=_reaper_sleep(victim, ticks),
        )
        assert victim.poll() == -signal.SIGINT
        assert (
            notes
            and notes[0].startswith("stray postmaster")
            and "ended after SIGINT" in notes[0]
        )
    finally:
        victim.kill()
        victim.wait()


def test_a_postmaster_that_ignores_sigint_is_killed_only_after_the_grace(
    tmp_path: Path,
) -> None:
    run = tmp_path / "from-8.0.0"
    stubborn = subprocess.Popen(
        [
            sys.executable,
            "-c",
            "import signal, time\nsignal.signal(signal.SIGINT, signal.SIG_IGN)\nprint('up', flush=True)\ntime.sleep(60)",
        ],
        start_new_session=True,
        stdout=subprocess.PIPE,
    )
    assert stubborn.stdout is not None and stubborn.stdout.readline().strip() == b"up"
    now = [0.0]

    def sleep(dt: float) -> None:
        now[0] += (
            dt  # the grace runs on this fake clock; the kill itself needs real time to land
        )
        time.sleep(0.005)
        stubborn.poll()

    try:
        _pidfile(run, stubborn.pid)
        data = str(run / "config" / "postgres")
        notes = ur._reap_postmasters(
            run,
            grace_s=60.0,
            cmdline=lambda pid: ["postgres", "-D", data],
            sleep=sleep,
            clock=lambda: now[0],
        )
        assert now[0] >= 60.0, "SIGKILL only after the full grace"
        assert stubborn.poll() == -signal.SIGKILL
        assert "SIGKILLed" in notes[0] and "60s" in notes[0]
    finally:
        stubborn.kill()
        stubborn.wait()


def test_a_pid_that_changed_hands_during_the_grace_is_not_killed(
    tmp_path: Path,
) -> None:
    """The postmaster ends after the SIGINT and its pid is reused: the grace's end must look again."""
    run = tmp_path / "from-8.0.0"
    stranger = subprocess.Popen(
        [
            sys.executable,
            "-c",
            "import signal, time\nsignal.signal(signal.SIGINT, signal.SIG_IGN)\nprint('up', flush=True)\ntime.sleep(60)",
        ],
        start_new_session=True,
        stdout=subprocess.PIPE,
    )
    assert stranger.stdout is not None and stranger.stdout.readline().strip() == b"up"
    data = str(run / "config" / "postgres")
    answers = [["postgres", "-D", data]]  # the first look: ours; every later one: not

    def cmdline(pid: int) -> list[str] | None:
        return answers.pop(0) if answers else ["python3", "-c", "x"]

    now = [0.0]

    def sleep(dt: float) -> None:
        now[0] += dt
        time.sleep(0.005)

    try:
        _pidfile(run, stranger.pid)
        notes = ur._reap_postmasters(
            run, grace_s=5.0, cmdline=cmdline, sleep=sleep, clock=lambda: now[0]
        )
        assert stranger.poll() is None, (
            "a pid that is no longer our postgres is never killed"
        )
        assert "left alone" in notes[0] and "SIGKILLed" not in notes[0]
    finally:
        stranger.kill()
        stranger.wait()


def test_a_stale_pid_file_for_a_dead_process_is_ignored(tmp_path: Path) -> None:
    run = tmp_path / "from-8.0.0"
    gone = _spawn_sleeper()
    gone.kill()
    gone.wait()
    _pidfile(run, gone.pid)
    assert (
        ur._reap_postmasters(run, grace_s=1.0, cmdline=lambda pid: ["postgres"]) == []
    )


# --- freeing a finished start's store -------------------------------------------------------------------------------


def test_a_finished_starts_store_bulk_is_freed_and_its_logs_stay(
    tmp_path: Path,
) -> None:
    run = tmp_path / "from-8.0.0"
    config = run / "config"
    data = config / "postgres"
    for sub in ("base", "pg_wal", "global"):
        (data / sub).mkdir(parents=True)
        (data / sub / "f").write_text("x" * 10)
    (data / "pg.log").write_text("log")
    (config / "pg_credentials").write_text(f"PG_DATA={data}\nPG_PORT=1\n")
    notes = ur.reclaim_store(config, run)
    assert not (data / "base").exists() and not (data / "pg_wal").exists()
    assert (data / "pg.log").read_text() == "log" and (data / "global" / "f").exists()
    assert notes and "base" in notes[0]


def test_a_store_outside_the_run_dir_is_never_touched(tmp_path: Path) -> None:
    run = tmp_path / "from-8.0.0"
    config = run / "config"
    config.mkdir(parents=True)
    outside = tmp_path / "operator-pg"
    (outside / "base").mkdir(parents=True)
    (config / "pg_credentials").write_text(f"PG_DATA={outside}\n")
    notes = ur.reclaim_store(config, run)
    assert (outside / "base").is_dir() and "outside" in notes[0]
    (config / "pg_credentials").write_text(f"PG_DATA={run}\n")
    assert "outside" in ur.reclaim_store(config, run)[0], (
        "the run dir itself is not a store"
    )
    assert ur.reclaim_store(tmp_path / "nowhere", run) == []


# --- the wrapper's last line and exit -------------------------------------------------------------------------------------


def _wrapper_tree(tmp_path: Path, driver_body: str) -> tuple[Path, dict[str, str]]:
    tree = tmp_path / "tree"
    (tree / "tests" / "e2e" / "lib").mkdir(parents=True)
    (tree / "scripts").mkdir()
    shutil.copy(WRAPPER, tree / "tests" / "e2e" / "upgrade-rehearsal.sh")
    shutil.copy(
        ROOT / "tests" / "e2e" / "lib" / "python.sh",
        tree / "tests" / "e2e" / "lib" / "python.sh",
    )
    (tree / "scripts" / "upgrade_rehearsal.py").write_text(driver_body)
    tmpdir = tmp_path / "tmp"
    tmpdir.mkdir()
    env = {
        "PATH": os.environ["PATH"],
        "HOME": str(tmp_path / "home"),
        "TMPDIR": str(tmpdir),
        "NEXUS_TARGET_RELEASE": "9.9.9",
        "NX_E2E_PYTHON": sys.executable,
    }
    return tree, env


def _run_wrapper(tree: Path, env: dict[str, str]) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        ["bash", str(tree / "tests" / "e2e" / "upgrade-rehearsal.sh")],
        env=env,
        capture_output=True,
        text=True,
        timeout=120,
    )


def test_the_wrapper_passes_only_on_the_gate_line_and_exit_0(tmp_path: Path) -> None:
    tree, env = _wrapper_tree(
        tmp_path,
        "print('UPGRADE REHEARSAL START (from 8.0.0): PASSED')\nprint('UPGRADE REHEARSAL GATE: PASSED: from 8.0.0 to x')\n",
    )
    r = _run_wrapper(tree, env)
    assert r.returncode == 0, r.stderr
    assert (
        r.stdout.strip().splitlines()[-1]
        == "UPGRADE REHEARSAL GATE: PASSED: from 8.0.0 to x"
    )


def test_a_driver_that_crashes_after_a_start_line_is_not_a_pass(tmp_path: Path) -> None:
    tree, env = _wrapper_tree(
        tmp_path,
        "import sys\nprint('UPGRADE REHEARSAL START (from 8.0.0): PASSED')\nsys.exit(1)\n",
    )
    r = _run_wrapper(tree, env)
    last = r.stdout.strip().splitlines()[-1]
    assert r.returncode == 1
    assert (
        last.startswith("UPGRADE REHEARSAL GATE: FAILED")
        and "without a verdict line" in last
    )
    assert "PASSED" not in last


def test_a_driver_that_dies_without_a_gate_line_makes_the_wrapper_fail_with_exit_1(
    tmp_path: Path,
) -> None:
    # an argparse error exits 2, which the release skill reads as NOT RUN: the wrapper must say FAILED, exit 1
    tree, env = _wrapper_tree(
        tmp_path,
        "import sys\nprint('usage: upgrade_rehearsal.py: error: bad flag', file=sys.stderr)\nsys.exit(2)\n",
    )
    r = _run_wrapper(tree, env)
    last = r.stdout.strip().splitlines()[-1]
    assert r.returncode == 1, (r.returncode, r.stdout, r.stderr)
    assert last.startswith("UPGRADE REHEARSAL GATE: FAILED")
    assert "exit 2" in last and "without a verdict line" in last


def test_the_wrapper_names_a_not_run_and_a_not_the_release_gate_line_exit_2(
    tmp_path: Path,
) -> None:
    for word in ("NOT RUN: pending", "NOT THE RELEASE GATE (--standin): x"):
        tree, env = _wrapper_tree(
            tmp_path / word.replace(" ", "-"),
            f"import sys\nprint('UPGRADE REHEARSAL GATE: {word}')\nsys.exit(2)\n",
        )
        r = _run_wrapper(tree, env)
        assert r.returncode == 2
        assert r.stdout.strip().splitlines()[-1] == f"UPGRADE REHEARSAL GATE: {word}"


def test_a_harness_with_no_usable_python_prints_a_not_run_gate_line_and_exits_2(
    tmp_path: Path,
) -> None:
    tree, env = _wrapper_tree(tmp_path, "print('never runs')\n")
    env["NX_E2E_PYTHON"] = str(tmp_path / "no-such-python")
    r = _run_wrapper(tree, env)
    assert r.returncode == 2
    last = r.stdout.strip().splitlines()[-1]
    assert last.startswith("UPGRADE REHEARSAL GATE: NOT RUN")


def test_keep_work_reaches_the_driver_so_the_store_is_kept_too(tmp_path: Path) -> None:
    seen = tmp_path / "argv.txt"
    body = (
        "import sys, pathlib\n"
        f"pathlib.Path({str(seen)!r}).write_text(' '.join(sys.argv[1:]))\n"
        "print('UPGRADE REHEARSAL GATE: PASSED: from 8.0.0 to x')\n"
    )
    tree, env = _wrapper_tree(tmp_path, body)
    r = subprocess.run(
        ["bash", str(tree / "tests" / "e2e" / "upgrade-rehearsal.sh"), "--keep-work"],
        env=env,
        capture_output=True,
        text=True,
        timeout=120,
    )
    assert r.returncode == 0, r.stderr
    assert "--keep-work" in seen.read_text().split()
    assert "WORK KEPT" in r.stderr


def test_a_postmaster_that_survives_the_run_fails_the_gate_even_on_a_passed_line(
    tmp_path: Path,
) -> None:
    survivor = tmp_path / "survivor.pid"
    body = (
        "import os, subprocess, sys, pathlib\n"
        "work = sys.argv[sys.argv.index('--work') + 1]\n"
        # A real postmaster gets the step's pty, never the wrapper's fifo: no inherited stdout.
        "p = subprocess.Popen(['sleep', '60'], start_new_session=True, stdin=subprocess.DEVNULL,"
        " stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)\n"
        f"pathlib.Path({str(survivor)!r}).write_text(str(p.pid))\n"
        "d = pathlib.Path(work, 'from-8.0.0', 'config', 'postgres'); d.mkdir(parents=True)\n"
        "(d / 'postmaster.pid').write_text(str(p.pid) + '\\n')\n"
        "print('UPGRADE REHEARSAL GATE: PASSED: from 8.0.0 to x')\n"
    )
    tree, env = _wrapper_tree(tmp_path, body)
    try:
        r = _run_wrapper(tree, env)
        assert r.returncode != 0, "a surviving postmaster must fail the gate"
        last = r.stdout.strip().splitlines()[-1]
        assert (
            last.startswith("UPGRADE REHEARSAL GATE: FAILED") and "postmaster" in last
        )
        assert "WORK KEPT" in r.stderr
    finally:
        if survivor.exists():
            try:
                os.kill(int(survivor.read_text()), signal.SIGKILL)
            except ProcessLookupError:
                pass


def test_a_signal_to_the_wrapper_reaches_the_driver_and_waits_for_its_cleanup(
    tmp_path: Path,
) -> None:
    ready, cleaned = tmp_path / "ready", tmp_path / "cleaned"
    body = (
        "import signal, sys, time, pathlib\n"
        f"ready, cleaned = pathlib.Path({str(ready)!r}), pathlib.Path({str(cleaned)!r})\n"
        "def stop(signum, frame):\n"
        "    time.sleep(1.0)  # the driver's stack stop and reap\n"
        "    cleaned.write_text('done')\n"
        "    sys.exit(143)\n"
        "signal.signal(signal.SIGTERM, stop)\n"
        "ready.write_text('1')\n"
        "time.sleep(60)\n"
    )
    tree, env = _wrapper_tree(tmp_path, body)
    proc = subprocess.Popen(
        ["bash", str(tree / "tests" / "e2e" / "upgrade-rehearsal.sh")],
        env=env,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
    )
    for _ in range(200):
        if ready.exists():
            break
        time.sleep(0.1)
    assert ready.exists()
    proc.send_signal(signal.SIGTERM)
    out, _err = proc.communicate(timeout=60)
    assert proc.returncode == 143
    assert cleaned.read_text() == "done", (
        "the wrapper must wait for the driver's own cleanup"
    )
    assert out.strip().splitlines()[-1].startswith("UPGRADE REHEARSAL GATE: FAILED")
