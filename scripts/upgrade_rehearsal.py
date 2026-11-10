#!/usr/bin/env python3
# SPDX-License-Identifier: AGPL-3.0-or-later
"""Upgrade rehearsal from the user's seat (nexus-f3yqd.5): the release gate's driver.

Two upgrades in a row did a ~20 minute silent rewrite on the first engine boot after
``nx upgrade``. Nothing measured what a user sees. This driver does, per starting version:

    install-prev      the previous published conexus as a uv tool, in a throwaway HOME   (setup)
    prepare           a sizeable local store written by that old client                  (setup)
    start-old         the old client starts its stack on that store: a user's engine is up (setup)
    package-upgrade   ``uv tool install --force <candidate>``, the way a user does it
    upgrade           ``nx upgrade``
    doctor            ``nx doctor``
    search            ``nx search`` for a seeded sentinel

Every judged step runs under a pty (nx's tty output and progress lines appear), every output
chunk is timestamped, and a step FAILS when it prints nothing for more than the silence limit,
runs past its ceiling, or exits non-zero. A breached step is killed with its whole process group
and the run stops there; the steps after it are reported as not run.

Non-vacuity: the prepared store's row count must reach a floor, every judged step must have run,
doctor must have printed something and no ``✗``, and the search must return the sentinel.

The ceilings and the silence limit below are generous starting points, PENDING A MEASURED RUN
(nexus-f3yqd.5): tighten them from the first green runs.

    upgrade_rehearsal.py --work DIR --target-wheel dist/conexus-X-py3-none-any.whl
    upgrade_rehearsal.py --work DIR --target-release 8.1.0 --from-version 8.0.0

Exit 0: every run passed. 1: a breach, a step failure or a non-vacuity finding in any run.
2: the harness could not start (unreachable index, unknown version, bad flags).
"""

from __future__ import annotations

import argparse
import dataclasses
import errno
import fcntl
import glob
import json
import os
import pty
import re
import select
import shlex
import shutil
import signal
import struct
import subprocess
import sys
import termios
import time
import urllib.request
from collections.abc import Callable, Sequence
from pathlib import Path

# --- named limits: generous, pending a measured run ------------------------------------------
DEFAULT_SILENCE_LIMIT_S: float = 10.0
UPGRADE_CEILING_S: float = 900.0
DOCTOR_CEILING_S: float = 120.0
SEARCH_CEILING_S: float = 60.0
PACKAGE_UPGRADE_CEILING_S: float = 600.0
# Setup steps are not under test: a generous ceiling, no silence limit.
INSTALL_PREV_CEILING_S: float = 900.0
PREPARE_CEILING_S: float = 3600.0
START_OLD_CEILING_S: float = 600.0
CLEANUP_CEILING_S: float = 120.0
DEFAULT_ROWS_FLOOR: int = 50_000
KILL_GRACE_S: float = 3.0

JUDGED_STEPS: tuple[str, ...] = ("package-upgrade", "upgrade", "doctor", "search")

# The real seeder's sentinel (marginalia's scripts/upgrade_boot_writes.py, f3yqd.1).
SENTINEL_QUERY = "heron counts amber lanterns ferry"
SENTINEL_EXPECT = "bootw-a-first-note"
REAL_PREPARE_CMD = (
    "{uv} run python scripts/upgrade_boot_writes.py --prepare-only --work {work} "
    "--prev-client {prev} --json {json}"
)
STANDIN_PREPARE_CMD = (
    "{python} scripts/upgrade_rehearsal_standin_prepare.py --work {work} "
    "--prev-client {prev} --json {json} --nx {nx}"
)
STANDIN_SENTINEL_QUERY = "kestrel counts copper lanterns harbour"
STANDIN_SENTINEL_EXPECT = "rehearsal-standin-sentinel"
STANDIN_ROWS_FLOOR = 100

_ANSI = re.compile(r"\x1b\[[0-9;?]*[ -/]*[@-~]|\x1b\][^\x07\x1b]*(?:\x07|\x1b\\)")


def say(text: str = "", *, err: bool = False) -> None:
    """One line to stdout (or stderr), flushed: a long run must show its progress as it goes."""
    stream = sys.stderr if err else sys.stdout
    stream.write(text + "\n")
    stream.flush()


class RehearsalError(Exception):
    """The harness cannot proceed (not a step failure)."""


def strip_ansi(text: str) -> str:
    return _ANSI.sub("", text)


@dataclasses.dataclass
class StepResult:
    name: str
    status: str  # ok | silent | ceiling | failed | not-run
    exit_code: int | None = None
    wall_s: float = 0.0
    longest_gap_s: float = 0.0
    output_bytes: int = 0
    reason: str = ""
    tail: str = ""
    argv: list[str] = dataclasses.field(default_factory=list)
    silence_limit_s: float | None = None
    ceiling_s: float | None = None

    @property
    def ok(self) -> bool:
        return self.status == "ok"


# --- the watchdog ----------------------------------------------------------------------------


def _set_winsize(fd: int, rows: int = 40, cols: int = 140) -> None:
    try:
        fcntl.ioctl(fd, termios.TIOCSWINSZ, struct.pack("HHHH", rows, cols, 0, 0))
    except OSError:
        pass


def _kill_group(proc: subprocess.Popen) -> None:
    for sig, wait in ((signal.SIGTERM, KILL_GRACE_S), (signal.SIGKILL, 5.0)):
        try:
            os.killpg(proc.pid, sig)
        except ProcessLookupError:
            break
        deadline = time.monotonic() + wait
        while time.monotonic() < deadline:
            if proc.poll() is not None:
                break
            time.sleep(0.05)
        if proc.poll() is not None:
            break
    try:
        proc.wait(timeout=5)
    except subprocess.TimeoutExpired:
        pass
    # A child that ignored TERM may outlive the leader: one last KILL to the group.
    try:
        os.killpg(proc.pid, signal.SIGKILL)
    except (ProcessLookupError, PermissionError):
        pass


def run_step(
    name: str,
    argv: Sequence[str],
    *,
    env: dict[str, str],
    cwd: Path,
    silence_limit_s: float | None,
    ceiling_s: float | None,
    log_dir: Path,
    clock: Callable[[], float] = time.monotonic,
) -> StepResult:
    """Run one command under a pty and judge it. ``None`` limits mean unlimited."""
    log_dir.mkdir(parents=True, exist_ok=True)
    log_path = log_dir / f"{name}.log"
    chunks_path = log_dir / f"{name}.chunks.jsonl"
    result = StepResult(
        name=name,
        status="failed",
        argv=list(argv),
        silence_limit_s=silence_limit_s,
        ceiling_s=ceiling_s,
    )
    master, slave = pty.openpty()
    _set_winsize(slave)
    start = clock()
    try:
        proc = subprocess.Popen(
            list(argv),
            stdin=subprocess.DEVNULL,
            stdout=slave,
            stderr=slave,
            env=env,
            cwd=str(cwd),
            start_new_session=True,
            close_fds=True,
        )
    except OSError as exc:
        os.close(master)
        os.close(slave)
        result.reason = f"could not start: {exc}"
        result.exit_code = 127
        log_path.write_text(result.reason + "\n")
        return result
    os.close(slave)

    last = start
    longest = 0.0
    total = 0
    tail = b""
    eof = False
    breach = ""
    status = ""

    def take(data: bytes, now: float) -> None:
        nonlocal last, longest, total, tail
        longest = max(longest, now - last)
        last = now
        total += len(data)
        tail = (tail + data)[-600:]
        logf.write(data)
        logf.flush()
        chunkf.write(json.dumps({"t": round(now - start, 3), "n": len(data)}) + "\n")

    with open(log_path, "wb") as logf, open(chunks_path, "w") as chunkf:
        while True:
            now = clock()
            if ceiling_s is not None and now - start > ceiling_s:
                status, breach = (
                    "ceiling",
                    f"ran past its ceiling of {ceiling_s:g}s (killed at {now - start:.1f}s)",
                )
                break
            if silence_limit_s is not None and now - last > silence_limit_s:
                longest = max(longest, now - last)
                status, breach = (
                    "silent",
                    f"silent: printed nothing for {now - last:.1f}s (limit {silence_limit_s:g}s)",
                )
                break
            if proc.poll() is not None:
                break
            if eof:
                time.sleep(0.1)
                continue
            ready, _, _ = select.select([master], [], [], 0.1)
            if ready:
                try:
                    data = os.read(master, 65536)
                except OSError as exc:
                    data = b"" if exc.errno == errno.EIO else b""
                if data:
                    take(data, clock())
                else:
                    eof = True
        if status:
            _kill_group(proc)
        else:
            # The leader exited: drain what the pty still holds (a daemon that kept the slave
            # open must not hold the step open, so stop at the first quiet moment).
            while True:
                ready, _, _ = select.select([master], [], [], 0.2)
                if not ready:
                    break
                try:
                    data = os.read(master, 65536)
                except OSError:
                    break
                if not data:
                    break
                take(data, clock())
    end = clock()
    os.close(master)
    result.wall_s = round(end - start, 3)
    result.longest_gap_s = round(max(longest, end - last if not status else longest), 3)
    result.output_bytes = total
    result.exit_code = proc.returncode
    result.tail = strip_ansi(tail.decode("utf-8", "replace")).strip()[-300:]
    if status:
        result.status, result.reason = status, breach
    elif proc.returncode != 0:
        result.status, result.reason = "failed", f"exit code {proc.returncode}"
    else:
        result.status = "ok"
        # The final stretch of silence before a clean exit counts against the limit too.
        if silence_limit_s is not None and result.longest_gap_s > silence_limit_s:
            result.status = "silent"
            result.reason = f"silent: printed nothing for {result.longest_gap_s:.1f}s (limit {silence_limit_s:g}s)"
    return result


# --- non-vacuity and version selection (pure) -------------------------------------------------


def total_rows(prepare: dict) -> int:
    cols = prepare.get("collections") or {}
    return int(sum(int(v) for v in cols.values()))


def nonvacuity_findings(
    prepare: dict,
    steps: Sequence[StepResult],
    *,
    floor: int,
    doctor_text: str,
    search_text: str,
    sentinel_expect: str,
    upgrade_text: str = "",
) -> list[str]:
    findings: list[str] = []
    if prepare.get("verdict") != "prepared":
        findings.append(
            f"prepare verdict was {prepare.get('verdict')!r}, not 'prepared'"
        )
    rows = total_rows(prepare)
    if rows < floor:
        findings.append(
            f"prepared store holds {rows} rows, below the floor of {floor}: the rehearsal would prove nothing"
        )
    by_name = {s.name: s for s in steps}
    for name in JUDGED_STEPS:
        s = by_name.get(name)
        if s is None or s.status == "not-run":
            findings.append(f"step {name} did not run")
    if (
        by_name.get("upgrade") is not None
        and by_name["upgrade"].ok
        and "DEFERRED" in strip_ansi(upgrade_text)
    ):
        # `nx upgrade` exits 0 when the engine is down and defers every rung: a green that did none
        # of the work. The rehearsal keeps the old stack up so the engine boot happens inside it.
        findings.append(
            "nx upgrade reported DEFERRED work: the engine was not reachable, so the upgrade did not run"
        )
    doctor = strip_ansi(doctor_text)
    if by_name.get("doctor") is not None and by_name["doctor"].ok:
        if not doctor.strip():
            findings.append("doctor printed no output: nothing was checked")
        elif "✗" in doctor:
            bad = [ln.strip() for ln in doctor.splitlines() if "✗" in ln]
            findings.append("doctor reported ✗: " + "; ".join(bad)[:300])
    if by_name.get("search") is not None and by_name["search"].ok:
        if sentinel_expect not in strip_ansi(search_text):
            findings.append(
                f"search did not return the seeded sentinel ({sentinel_expect!r})"
            )
    return findings


_VER = re.compile(r"(\d+(?:\.\d+)*)(?:\.post(\d+))?")


def _vkey(version: str) -> tuple[tuple[int, ...], int] | None:
    """Sort key for a final or post release; None for a pre/dev/local release."""
    m = _VER.fullmatch(version)
    if not m:
        return None
    return tuple(int(p) for p in m.group(1).split(".")), int(m.group(2) or 0)


def resolve_from_versions(
    spec: str, published: Sequence[str], *, candidate: str
) -> list[str]:
    """``both`` is [newest published below the candidate, last published 7.x], deduplicated."""
    finals = {v: _vkey(v) for v in published}
    finals = {v: k for v, k in finals.items() if k is not None}
    if spec != "both":
        if spec not in published:
            raise RehearsalError(
                f"--from-version {spec} is not a published conexus version"
            )
        return [spec]
    ckey = _vkey(candidate)
    if ckey is None:
        raise RehearsalError(
            f"candidate version {candidate!r} is not a final release version"
        )
    below = sorted((v for v, k in finals.items() if k < ckey), key=lambda v: finals[v])
    if not below:
        raise RehearsalError(
            f"no published conexus version below {candidate}: nothing to upgrade from"
        )
    out = [below[-1]]
    sevens = [v for v in below if finals[v][0][0] == 7]
    if sevens and sevens[-1] not in out:
        out.append(sevens[-1])
    return out


def fetch_published_versions(timeout: float = 30.0) -> list[str]:
    try:
        with urllib.request.urlopen(
            "https://pypi.org/pypi/conexus/json", timeout=timeout
        ) as resp:
            data = json.load(resp)
    except Exception as exc:  # noqa: BLE001 - any failure here is "cannot start", never a skip
        raise RehearsalError(
            f"could not read the published conexus versions from PyPI: {exc}"
        ) from exc
    return [
        v
        for v, files in data.get("releases", {}).items()
        if files and not any(f.get("yanked") for f in files)
    ]


# --- verdict ---------------------------------------------------------------------------------


def build_verdict(
    *,
    from_version: str,
    target: str,
    steps: Sequence[StepResult],
    findings: list[str],
    prepare: dict | None,
    floor: int,
) -> dict:
    failed = [s for s in steps if s.status not in ("ok", "not-run")]
    passed = not failed and not findings and bool(steps)
    return {
        "from_version": from_version,
        "target": target,
        "passed": passed,
        "findings": list(findings),
        "rows_floor": floor,
        "prepared_rows": total_rows(prepare) if prepare else 0,
        "prepare": prepare or {},
        "steps": [dataclasses.asdict(s) for s in steps],
    }


def format_table(verdict: dict) -> str:
    head = f"upgrade rehearsal: from {verdict['from_version']} to {verdict['target']}"
    cols = f"{'step':<16}{'status':<9}{'wall s':>9}{'longest gap s':>15}{'out bytes':>11}{'exit':>6}  note"
    lines = [head, cols, "-" * len(cols)]
    for s in verdict["steps"]:
        lines.append(
            f"{s['name']:<16}{s['status']:<9}{s['wall_s']:>9.1f}{s['longest_gap_s']:>15.1f}"
            f"{s['output_bytes']:>11}{'' if s['exit_code'] is None else s['exit_code']:>6}  {s['reason']}"
        )
    for f in verdict["findings"]:
        lines.append(f"finding: {f}")
    return "\n".join(lines)


def verdict_line(verdict: dict) -> str:
    word = "PASSED" if verdict["passed"] else "FAILED"
    bad = [s["name"] for s in verdict["steps"] if s["status"] not in ("ok", "not-run")]
    tail = "" if verdict["passed"] else f" at {', '.join(bad) or 'non-vacuity'}"
    return (
        f"UPGRADE REHEARSAL {word}: from {verdict['from_version']} to {verdict['target']}"
        f" ({verdict['prepared_rows']} rows){tail}"
    )


def combined_line(verdicts: Sequence[dict]) -> str:
    """The one line the wrapper ends on: every run passed, or which runs failed and where."""
    names = ", ".join(v["from_version"] for v in verdicts)
    if verdicts and all(v["passed"] for v in verdicts):
        return f"UPGRADE REHEARSAL PASSED: from {names} to {verdicts[0]['target']}"
    bad = []
    for v in verdicts:
        if not v["passed"]:
            at = [s["name"] for s in v["steps"] if s["status"] not in ("ok", "not-run")]
            bad.append(
                f"{v['from_version']} ({'at ' + ', '.join(at) if at else 'non-vacuity'})"
            )
    return f"UPGRADE REHEARSAL FAILED: from {', '.join(bad) or names}"


def write_verdict(verdict: dict, path: Path) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(verdict, indent=2, sort_keys=True) + "\n")


# --- one rehearsal ---------------------------------------------------------------------------


@dataclasses.dataclass
class Config:
    work: Path
    repo: Path
    target_spec: str  # what `uv tool install --force` receives
    target_label: str
    candidate: str
    prepare_cmd: str
    rows_floor: int
    sentinel_query: str
    sentinel_expect: str
    silence_limit_s: float
    ceilings: dict[str, float]
    python: str = "3.12"


def _ambient_uv_dir() -> str:
    uv = shutil.which("uv")
    return str(Path(uv).parent) if uv else ""


def build_env(home: Path, config: Path) -> dict[str, str]:
    """The only environment a step sees: no cloud, no Voyage key, no ambient steering."""
    path = ":".join(
        p
        for p in (str(home / ".local" / "bin"), _ambient_uv_dir(), "/usr/bin", "/bin")
        if p
    )
    env = {
        "HOME": str(home),
        "PATH": path,
        "TERM": "xterm-256color",
        "NEXUS_CONFIG_DIR": str(config),
        "NX_LOCAL": "1",
        "NX_NO_TELEMETRY": "1",
    }
    # A user's shell always carries its login name; without it getpass and the engine's PG
    # provisioning disagree on the cluster superuser (first measuring run, 2026-11-10).
    for k in (
        "USER",
        "LOGNAME",
        "LANG",
        "HTTPS_PROXY",
        "HTTP_PROXY",
        "https_proxy",
        "http_proxy",
    ):
        if os.environ.get(k):
            env[k] = os.environ[k]
    return env


def _reap_postmasters(run_dir: Path) -> list[str]:
    """Ensure no postmaster outlives the run. Only pids read from OUR work dir's pid files."""
    notes: list[str] = []
    for pidfile in glob.glob(str(run_dir / "**" / "postmaster.pid"), recursive=True):
        try:
            pid = int(Path(pidfile).read_text().splitlines()[0])
            os.kill(pid, 0)
        except (OSError, ValueError, IndexError):
            continue
        for sig in (signal.SIGINT, signal.SIGKILL):
            try:
                os.kill(pid, sig)
            except ProcessLookupError:
                break
            time.sleep(2)
        notes.append(f"stray postmaster {pid} under {run_dir} ended")
    return notes


def rehearse(from_version: str, cfg: Config) -> dict:
    run_dir = cfg.work / f"from-{from_version}"
    home, config, logs, cwd = (
        run_dir / "home",
        run_dir / "config",
        run_dir / "logs",
        run_dir / "cwd",
    )
    for d in (home, config, logs, cwd):
        d.mkdir(parents=True, exist_ok=True)
    env = build_env(home, config)
    nx = str(home / ".local" / "bin" / "nx")
    steps: list[StepResult] = []
    prepare: dict | None = None
    findings: list[str] = []
    texts: dict[str, str] = {}
    stop = False

    def go(
        name: str,
        argv: list[str],
        *,
        silence: float | None,
        ceiling: float,
        where: Path = cwd,
        judged: bool = True,
    ) -> StepResult:
        say(f"[{from_version}] {name}: {' '.join(shlex.quote(a) for a in argv)}")
        r = run_step(
            name,
            argv,
            env=env,
            cwd=where,
            silence_limit_s=silence,
            ceiling_s=ceiling,
            log_dir=logs,
        )
        say(
            f"[{from_version}] {name}: {r.status} in {r.wall_s:.1f}s (longest gap {r.longest_gap_s:.1f}s) {r.reason}"
        )
        if judged:
            steps.append(r)
        elif not r.ok:
            findings.append(
                f"setup step {name} {r.status}: {r.reason}; tail: {r.tail[-200:]}"
            )
        texts[name] = (
            Path(logs / f"{name}.log").read_text(errors="replace")
            if (logs / f"{name}.log").exists()
            else ""
        )
        return r

    try:
        r = go(
            "install-prev",
            [
                "uv",
                "tool",
                "install",
                "--python",
                cfg.python,
                f"conexus=={from_version}",
            ],
            silence=None,
            ceiling=INSTALL_PREV_CEILING_S,
            judged=False,
        )
        stop = not r.ok
        if not stop and not Path(nx).exists():
            findings.append(f"install-prev left no nx at {nx}")
            stop = True

        if not stop:
            json_path = run_dir / "prepare.json"
            argv = [
                t.format(
                    uv="uv",
                    python=sys.executable,
                    work=str(run_dir),
                    prev=from_version,
                    json=str(json_path),
                    nx=nx,
                )
                for t in shlex.split(cfg.prepare_cmd)
            ]
            r = go(
                "prepare",
                argv,
                silence=None,
                ceiling=PREPARE_CEILING_S,
                where=cfg.repo,
                judged=False,
            )
            stop = not r.ok
            if not stop:
                try:
                    prepare = json.loads(json_path.read_text())
                except (OSError, ValueError) as exc:
                    findings.append(
                        f"prepare left no readable JSON at {json_path}: {exc}"
                    )
                    stop = True
            if prepare is not None and prepare.get("verdict") != "prepared":
                findings.append(f"prepare verdict {prepare.get('verdict')!r}")
                stop = True

        # The box a user upgrades has its engine running: the old stack comes up on the old data
        # before the package moves, so the new engine's first boot happens inside `nx upgrade`.
        if not stop:
            r = go(
                "start-old",
                [nx, "daemon", "service", "start"],
                silence=None,
                ceiling=START_OLD_CEILING_S,
                judged=False,
            )
            stop = not r.ok

        sil = cfg.silence_limit_s
        plan = [
            (
                "package-upgrade",
                [
                    "uv",
                    "tool",
                    "install",
                    "--force",
                    "--python",
                    cfg.python,
                    cfg.target_spec,
                ],
            ),
            ("upgrade", [nx, "upgrade"]),
            ("doctor", [nx, "doctor"]),
            ("search", [nx, "search", cfg.sentinel_query, "-m", "5"]),
        ]
        for name, argv in plan:
            if stop:
                steps.append(
                    StepResult(
                        name=name, status="not-run", reason="an earlier step failed"
                    )
                )
                continue
            r = go(name, argv, silence=sil, ceiling=cfg.ceilings[name])
            stop = not r.ok
    finally:
        if Path(nx).exists():
            go(
                "cleanup-service",
                [nx, "daemon", "service", "stop", "--with-pg"],
                silence=None,
                ceiling=CLEANUP_CEILING_S,
                judged=False,
            )
            go(
                "cleanup-worker",
                [nx, "daemon", "aspect-worker", "stop", "--config-dir", str(config)],
                silence=None,
                ceiling=CLEANUP_CEILING_S,
                judged=False,
            )
        findings.extend(_reap_postmasters(run_dir))

    # A setup failure is already in findings; a step breach is in the step. Add non-vacuity only
    # when the prepare produced something to judge.
    if prepare is not None:
        findings.extend(
            nonvacuity_findings(
                prepare,
                steps,
                floor=cfg.rows_floor,
                doctor_text=texts.get("doctor", ""),
                search_text=texts.get("search", ""),
                sentinel_expect=cfg.sentinel_expect,
                upgrade_text=texts.get("upgrade", ""),
            )
        )
    verdict = build_verdict(
        from_version=from_version,
        target=cfg.target_label,
        steps=steps,
        findings=findings,
        prepare=prepare,
        floor=cfg.rows_floor,
    )
    write_verdict(verdict, run_dir / "verdict.json")
    say(format_table(verdict))
    say(verdict_line(verdict))
    return verdict


# --- command line ----------------------------------------------------------------------------


def _wheel_version(path: Path) -> str:
    m = re.match(r"conexus-([^-]+)-", path.name)
    if not m:
        raise RehearsalError(f"cannot read a version from the wheel name {path.name!r}")
    return m.group(1)


def _build_parser() -> argparse.ArgumentParser:
    ap = argparse.ArgumentParser(
        description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter
    )
    ap.add_argument(
        "--work",
        type=Path,
        required=True,
        help="work dir; one from-<version> subdir per run",
    )
    tgt = ap.add_mutually_exclusive_group(required=True)
    tgt.add_argument(
        "--target-wheel", type=Path, help="the candidate: a built conexus wheel"
    )
    tgt.add_argument(
        "--target-release",
        metavar="X.Y.Z",
        help="the candidate: a published conexus version",
    )
    ap.add_argument(
        "--from-version",
        default="both",
        metavar="X.Y.Z|both",
        help="previous release to start from. 'both' (the release-gate default) runs the "
        "newest published version below the candidate and the last published 7.x, "
        "each in its own work dir with its own table and verdict",
    )
    ap.add_argument(
        "--published-versions",
        help="comma-separated override for the PyPI listing (offline use)",
    )
    ap.add_argument(
        "--prepare-cmd",
        default=None,
        help=f"command that builds the old state; placeholders {{work}} {{prev}} {{json}} "
        f"{{nx}} {{uv}} {{python}}. Default: {REAL_PREPARE_CMD}",
    )
    ap.add_argument(
        "--standin",
        action="store_true",
        help="use the small stand-in prepare (a few hundred documents) instead of the real "
        "seeder, with its own sentinel and a floor of %d rows" % STANDIN_ROWS_FLOOR,
    )
    ap.add_argument(
        "--rows-floor",
        type=int,
        default=None,
        help=f"minimum prepared rows (default {DEFAULT_ROWS_FLOOR})",
    )
    ap.add_argument("--sentinel-query", default=None)
    ap.add_argument(
        "--sentinel-expect", default=None, help="text the search output must contain"
    )
    ap.add_argument("--silence-limit", type=float, default=DEFAULT_SILENCE_LIMIT_S)
    ap.add_argument("--package-ceiling", type=float, default=PACKAGE_UPGRADE_CEILING_S)
    ap.add_argument("--upgrade-ceiling", type=float, default=UPGRADE_CEILING_S)
    ap.add_argument("--doctor-ceiling", type=float, default=DOCTOR_CEILING_S)
    ap.add_argument("--search-ceiling", type=float, default=SEARCH_CEILING_S)
    ap.add_argument(
        "--json-out",
        type=Path,
        help="combined verdict file (default WORK/verdict.json)",
    )
    return ap


def main(argv: Sequence[str] | None = None) -> int:
    args = _build_parser().parse_args(argv)
    repo = Path(__file__).resolve().parent.parent
    try:
        if args.target_wheel is not None:
            wheel = args.target_wheel.resolve()
            if not wheel.is_file():
                raise RehearsalError(f"no wheel at {wheel}")
            candidate, spec, label = (
                _wheel_version(wheel),
                str(wheel),
                f"wheel {wheel.name}",
            )
        else:
            candidate, spec, label = (
                args.target_release,
                f"conexus=={args.target_release}",
                f"conexus {args.target_release}",
            )
        published = (
            args.published_versions.split(",")
            if args.published_versions
            else fetch_published_versions()
        )
        versions = resolve_from_versions(
            args.from_version, published, candidate=candidate
        )
    except RehearsalError as exc:
        say(f"UPGRADE REHEARSAL NOT RUN: {exc}", err=True)
        return 2

    if args.standin:
        prepare_cmd = args.prepare_cmd or STANDIN_PREPARE_CMD
        floor = args.rows_floor if args.rows_floor is not None else STANDIN_ROWS_FLOOR
        query = args.sentinel_query or STANDIN_SENTINEL_QUERY
        expect = args.sentinel_expect or STANDIN_SENTINEL_EXPECT
    else:
        prepare_cmd = args.prepare_cmd or REAL_PREPARE_CMD
        floor = args.rows_floor if args.rows_floor is not None else DEFAULT_ROWS_FLOOR
        query = args.sentinel_query or SENTINEL_QUERY
        expect = args.sentinel_expect or SENTINEL_EXPECT

    cfg = Config(
        work=args.work.resolve(),
        repo=repo,
        target_spec=spec,
        target_label=label,
        candidate=candidate,
        prepare_cmd=prepare_cmd,
        rows_floor=floor,
        sentinel_query=query,
        sentinel_expect=expect,
        silence_limit_s=args.silence_limit,
        ceilings={
            "package-upgrade": args.package_ceiling,
            "upgrade": args.upgrade_ceiling,
            "doctor": args.doctor_ceiling,
            "search": args.search_ceiling,
        },
    )
    cfg.work.mkdir(parents=True, exist_ok=True)
    say(f"upgrade rehearsal: candidate {label}; starting from {', '.join(versions)}")
    verdicts = [rehearse(v, cfg) for v in versions]
    combined = {"passed": all(v["passed"] for v in verdicts), "runs": verdicts}
    write_verdict(combined, args.json_out or cfg.work / "verdict.json")
    say("== combined ==")
    for v in verdicts:
        say(verdict_line(v))
    say(combined_line(verdicts))
    return 0 if combined["passed"] else 1


if __name__ == "__main__":
    sys.exit(main())
