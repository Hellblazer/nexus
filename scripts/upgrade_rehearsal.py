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
    pci-restart-check (pre-PCI starts only, below)

Every judged step runs under a pty (nx's tty output and progress lines appear), every output
chunk is timestamped, and a step FAILS when it prints nothing for more than the silence limit,
runs past its ceiling, or exits non-zero. A breached step is killed with its whole process group
and the run stops there; the steps after it are reported as not run.

Non-vacuity: the prepared store's row count must reach a floor, every judged step must have run,
doctor must have printed something and no ``✗``, and the search must return the sentinel.

Write rate. Around ``nx upgrade`` the driver reads the install's own PG (its bundled psql and
``pg_credentials``): ``pg_current_wal_lsn()`` and the ``pg_stat_wal`` / ``pg_stat_io`` write counters,
before and after. The verdict and the table carry the WAL bytes, the step's seconds and MB/s
(MB = 2**20 bytes). Extrapolation: the bytes an upgrade writes scale with the store, so at the
measured MB/s a store of S GB spends about S*1024*r/MBps seconds on that work, r being this run's
WAL bytes over store bytes (also recorded; r = 1 gives S*1024/MBps). The 2.4 GB fixture bounds only
itself, and a step of a couple of seconds gives a noisy rate.

PCI keep check (user's-seat proof of the restart fix). A start from a release that predates the
per-collection indexes (``PRE_PCI_VERSION``, pins engine 0.1.156) has its upgrade boot build every
``pci_`` index, and ``nx upgrade`` waits for every build before it returns, so a restart AFTER it
proves nothing. Sam's case is a restart while the long upgrade is still running. A watcher polls
``pg_stat_progress_create_index`` from before ``nx upgrade``; the first time it shows a ``pci_``
build in flight while ``nx upgrade`` is still running, a second thread restarts the engine the way
a user in a second shell does (``nx daemon service stop`` then ``start``, same HOME and environment,
no ``--with-pg``) and records the (index, pid, progress) at that moment, the restart's own outputs
and exit codes, and what ``nx upgrade`` did after it (its exit code and output are judged by its own
step rules). After the search step the driver waits (bounded) for every ``pci_`` index to be valid
and judges by backend pid: the in-flight build is kept only if the same (index, pid) is seen building
after the restart, the engine log carries ``event=pci_anchor_adopted`` for the index, and it ends
valid; a new pid, a ``failed_build`` drop, ``CONNECTION_LOST`` or ``57P01`` after the restart is the
failure (``PCI_FAILURE_EVENTS``). Never having seen a build in flight during ``nx upgrade`` is a
FAILURE (vacuous), not a pass. The fix ships in an engine newer than the wheel's pin: pass
``--engine-jar`` (or ``NEXUS_SERVICE_JAR``) to run the check against a jar built from the fix; the
override applies from ``start-old`` on, never to the old client's stack.

The ceilings are set from the first real-store runs (chas lab-m, 2026-11-10, the f3yqd.1 seeder's
86,000 rows, 2.4 GB): about eight to ten times the slowest measured step. They bound THIS fixture;
a much larger store needs its own measurement. The silence limit is Sam's "a few seconds".

    upgrade_rehearsal.py --work DIR --target-wheel dist/conexus-X-py3-none-any.whl
    upgrade_rehearsal.py --work DIR --target-release 8.1.0 --from-version 8.0.0
    upgrade_rehearsal.py --work DIR --target-wheel W --from-version 7.76.0 --engine-jar service/target/nexus-service-X.jar

Exit 0: every run passed. 1: a breach, a step failure or a non-vacuity finding in any run.
2: the harness could not start (unreachable index, unknown version, bad flags).
"""

from __future__ import annotations

import argparse
import dataclasses
import errno
import fcntl
import getpass
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
import threading
import time
import urllib.request
from collections.abc import Callable, Sequence
from pathlib import Path

# --- named limits, from the 2026-11-10 real-store runs (slowest measured in brackets) ---------
DEFAULT_SILENCE_LIMIT_S: float = (
    10.0  # [longest gap: 5.7 s doctor; 11.9 s nx upgrade from 7.99.0, a breach]
)
UPGRADE_CEILING_S: float = 120.0  # [13.9 s from 7.99.0, 2.3 s from 8.0.0]
DOCTOR_CEILING_S: float = 60.0  # [7.5 s]
SEARCH_CEILING_S: float = 30.0  # [2.8 s]
PACKAGE_UPGRADE_CEILING_S: float = 120.0  # [0.4 s]
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

# --- the write rate and the PCI keep check: every named string and bound, in one place -----------
MB: int = 2**20  # the "MB" of every figure here; 1 GB = 1024 MB
#: Last conexus before the per-collection indexes (pins engine 0.1.156); PCI first ships in
#: 0.1.158 / 7.77.0. Starting from it, the upgrade boot builds every ``pci_`` index.
PRE_PCI_VERSION: str = "7.76.0"
PCI_FIRST_VERSION: str = "7.77.0"
PCI_STEP: str = "pci-restart-check"
#: A collection builds its own index at or above this row count (NX_SEARCH_PCI_BUILD_MIN_ROWS default).
PCI_BUILD_MIN_ROWS: int = 20_000
#: Large collections the pre-PCI prepare seeds (each above the threshold, ~27k rows, one ``pci_``
#: build each, ~30 s at defaults): five, so a queue exists when the first build is seen.
PCI_COLLECTIONS_DEFAULT: int = 5
PRE_PCI_PREPARE_CMD = REAL_PREPARE_CMD + " --no-pci-wait --collections {collections}"
#: A per-collection index name as ``index_relid::regclass::text`` prints it (PciCatalog.PREFIX + 24 hex).
PCI_INDEX_RE = re.compile(r"^(?:nexus\.)?(pci_[0-9a-f]{24})$")
#: Engine log events, as the needles one line must carry together (wren, the reviewed fix commit).
PCI_FAILURE_EVENTS: dict[str, tuple[str, ...]] = {
    "pci_index_dropped": ("event=pci_index_dropped", "reason=failed_build"),
    "pci_index_build_not_done": (
        "event=pci_index_build_not_done",
        "outcome=CONNECTION_LOST",
    ),
    "pci_ddl_failed": ("event=pci_ddl_failed", "sqlstate=57P01"),
}
EVENT_BUILDERS_KEPT: tuple[str, ...] = (
    "event=pci_builders_kept",
    "reason=only_run_always_pending",
)
#: A surviving build, adopted by the next boot (PciReconciler.java:977): the line carries
#: ``index=<name>``, which ties it to the build; an adopted survivor logs ``reason=missing``.
EVENT_INDEX_ADOPTED: tuple[str, ...] = ("event=pci_anchor_adopted",)
_INDEX_FIELD_RE = re.compile(r"\bindex=(pci_[0-9a-f]{24})\b")
#: The supervisor's engine child-log stems: native binary first, then a NEXUS_SERVICE_JAR launch.
ENGINE_LOG_STEMS: tuple[str, ...] = ("storage_service_native", "storage_service_jar")
PCI_POLL_S: float = 0.5
#: The restart trigger looks at the watcher's latest sighting this often (the watcher itself polls
#: every ``PCI_POLL_S``; the trigger only adds this to the delay between a build starting and the stop).
PCI_TRIGGER_POLL_S: float = 0.1
#: How long past the end of nx upgrade a restart already under way is given to finish.
PCI_RESTART_JOIN_S: float = 2 * 300.0 + 30.0
PCI_END_STATE_BOUND_S: float = (
    900.0  # a killed build's rebuild can wait for a 600 s sweep pass
)
PCI_RESTART_CEILING_S: float = 300.0
PROBE_SNAPSHOT_BOUND_S: float = 30.0

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
        chunkf.write(
            json.dumps(
                {
                    "t": round(now - start, 3),
                    "n": len(data),
                    "s": data[:160].decode("utf-8", "replace"),
                }
            )
            + "\n"
        )

    with open(log_path, "wb") as logf, open(chunks_path, "w") as chunkf:
        while True:
            now = clock()
            if ceiling_s is not None and now - start > ceiling_s:
                longest = max(longest, now - last)
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
    required_steps: Sequence[str] = JUDGED_STEPS,
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
    for name in required_steps:
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
    """``both`` is [newest published below the candidate, last published 7.x, PRE_PCI_VERSION],
    deduplicated: the third is the start whose upgrade boot builds every pci_ index (the PCI check)."""
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
    pre = _vkey(PRE_PCI_VERSION)
    if pre is not None and pre < ckey and PRE_PCI_VERSION not in out:
        if PRE_PCI_VERSION not in published:
            raise RehearsalError(
                f"{PRE_PCI_VERSION}, the pre-PCI start of the keep check, is not a published conexus version"
            )
        out.append(PRE_PCI_VERSION)
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


# --- the install's own PG: credentials, bundled psql, counters --------------------------------


class ProbeError(Exception):
    """The install's PG could not be queried (no credentials, no psql, a SQL error, a timeout)."""


def parse_credentials(text: str) -> dict[str, str]:
    out: dict[str, str] = {}
    for line in text.splitlines():
        line = line.strip()
        if line and not line.startswith("#") and "=" in line:
            k, _, v = line.partition("=")
            out[k.strip()] = v.strip()
    return out


_PG_DOWN = (
    "could not connect",
    "Connection refused",
    "starting up",
    "shutting down",
    "server closed the connection",
    "terminating connection",
)


class PgProbe:
    """Queries of the install's PG through its own bundled psql and ``pg_credentials``.

    The password travels in PGPASSWORD, never on a command line, and no message built here repeats
    it. A cluster that is not accepting connections is ``None`` (the caller retries); anything else
    that fails is a ProbeError.
    """

    def __init__(self, config_dir: Path, *, timeout: float = 30.0) -> None:
        self.config_dir = Path(config_dir)
        self.timeout = timeout

    def _creds(self) -> dict[str, str]:
        path = self.config_dir / "pg_credentials"
        try:
            return parse_credentials(path.read_text(encoding="utf-8"))
        except OSError as exc:
            raise ProbeError(f"cannot read {path} (pg_credentials): {exc}") from exc

    def sql(self, query: str) -> list[list[str]] | None:
        c = self._creds()
        bundle = self.config_dir / "pg-bundle" / "bundle"
        try:
            port, url = c["PG_PORT"], c["NX_DB_URL"]
        except KeyError as exc:
            raise ProbeError(f"pg_credentials has no {exc.args[0]}") from exc
        # The cluster superuser where the install recorded its password; installs that predate
        # PG_SUPERUSER_PASS record only the schema owner, which owns every nexus table.
        if "PG_SUPERUSER_PASS" in c:
            user, password = getpass.getuser(), c["PG_SUPERUSER_PASS"]
        else:
            try:
                user, password = c["NX_DB_ADMIN_USER"], c["NX_DB_ADMIN_PASS"]
            except KeyError as exc:
                raise ProbeError(f"pg_credentials has no {exc.args[0]}") from exc
        env = {
            "PATH": "/usr/bin:/bin",
            "LD_LIBRARY_PATH": f"{bundle / 'lib'}:{os.environ.get('LD_LIBRARY_PATH', '')}".rstrip(
                ":"
            ),
            "PGPASSWORD": password,
            "PGCONNECT_TIMEOUT": "3",
        }
        dbname = url.rsplit("/", 1)[-1].split("?", 1)[0]
        argv = [
            str(bundle / "bin" / "psql"),
            "-XAtq",
            "-F",
            "\t",
            "-h",
            "127.0.0.1",
            "-p",
            port,
            "-U",
            user,
            "-d",
            dbname,
            "-v",
            "ON_ERROR_STOP=1",
            "-c",
            query,
        ]
        try:
            r = subprocess.run(
                argv, env=env, capture_output=True, text=True, timeout=self.timeout
            )
        except (OSError, subprocess.TimeoutExpired) as exc:
            raise ProbeError(f"psql did not run: {type(exc).__name__}: {exc}") from exc
        if r.returncode != 0:
            err = r.stderr.replace(password, "***") if password else r.stderr
            if any(t in err for t in _PG_DOWN):
                return None
            raise ProbeError(f"psql failed: {err.strip()[:400]}")
        return [line.split("\t") for line in r.stdout.splitlines() if line]


def lsn_bytes(lsn: str) -> int:
    hi, lo = lsn.split("/")
    return (int(hi, 16) << 32) + int(lo, 16)


@dataclasses.dataclass(frozen=True)
class PgCounters:
    lsn: int  # pg_current_wal_lsn() as a byte position: the figure that cannot be reset
    wal_bytes: int | None  # pg_stat_wal.wal_bytes
    io_write_bytes: int | None  # pg_stat_io, summed over every backend and context
    io_extend_bytes: int | None


_IO_SQL_BYTES = "select coalesce(sum(write_bytes),0)::bigint, coalesce(sum(extend_bytes),0)::bigint from pg_stat_io"
_IO_SQL_OPS = (
    "select coalesce(sum(writes*op_bytes),0)::bigint, coalesce(sum(extends*op_bytes),0)::bigint "
    "from pg_stat_io"
)
SqlFn = Callable[[str], "list[list[str]] | None"]


def read_counters(sql: SqlFn) -> PgCounters | None:
    """One reading, or None while PG is not accepting connections. The LSN must read; the stat
    views are best-effort (older PG, a stat that is not there) and come back None."""
    rows = sql("select pg_current_wal_lsn()::text")
    if rows is None:
        return None
    lsn = lsn_bytes(rows[0][0])
    wal: int | None = None
    try:
        w = sql("select wal_bytes::bigint from pg_stat_wal")
        wal = int(w[0][0]) if w else None
    except (ProbeError, ValueError, IndexError):
        pass
    io_w = io_e = None
    for q in (
        _IO_SQL_BYTES,
        _IO_SQL_OPS,
    ):  # PG18 has write_bytes; PG16-17 have op_bytes
        try:
            io = sql(q)
            if io:
                io_w, io_e = int(io[0][0]), int(io[0][1])
                break
        except (ProbeError, ValueError, IndexError):
            continue
    return PgCounters(lsn, wal, io_w, io_e)


def take_counters(
    sql: SqlFn,
    *,
    bound_s: float = PROBE_SNAPSHOT_BOUND_S,
    clock: Callable[[], float] = time.monotonic,
    sleep: Callable[[float], None] = time.sleep,
) -> PgCounters:
    """A reading, retried while PG is down (an upgrade can restart it); loud at the bound."""
    deadline = clock() + bound_s
    while True:
        got = read_counters(sql)
        if got is not None:
            return got
        if clock() >= deadline:
            raise ProbeError(f"PG did not accept connections within {bound_s:g}s")
        sleep(0.5)


def write_rate(
    before: PgCounters,
    after: PgCounters,
    wall_s: float,
    store_bytes: int | None = None,
) -> dict:
    """What PG wrote between two readings. The LSN is the figure; the stat counters reset on a
    crash, so one that went backwards is dropped (None) rather than reported negative."""
    wal = after.lsn - before.lsn
    if wal < 0:
        raise ProbeError(
            f"the WAL position went backwards ({before.lsn} to {after.lsn}): not the same cluster"
        )

    def delta(a: int | None, b: int | None) -> int | None:
        if a is None or b is None or b < a:
            return None
        return b - a

    mb_per_s = (wal / MB) / wall_s if wall_s > 0 else None
    return {
        "wal_bytes": wal,
        "wal_mb": round(wal / MB, 1),
        "seconds": round(wall_s, 1),
        "mb_per_s": None if mb_per_s is None else round(mb_per_s, 2),
        "pg_stat_wal_bytes": delta(before.wal_bytes, after.wal_bytes),
        "io_write_bytes": delta(before.io_write_bytes, after.io_write_bytes),
        "io_extend_bytes": delta(before.io_extend_bytes, after.io_extend_bytes),
        "store_bytes": store_bytes,
        "wal_per_store_byte": round(wal / store_bytes, 4) if store_bytes else None,
        "seconds_per_gb_of_wal": round(1024 / mb_per_s, 1) if mb_per_s else None,
    }


# --- the PCI keep check ------------------------------------------------------------------------


def is_pre_pci(version: str) -> bool:
    key, first = _vkey(version), _vkey(PCI_FIRST_VERSION)
    return key is not None and first is not None and key < first


def pci_check_enabled(flag: bool | None, version: str, *, standin: bool) -> bool:
    """On by default for a pre-PCI start (not with the stand-in prepare, whose store is too small to
    build any pci_ index); an explicit flag decides otherwise."""
    if flag is not None:
        return flag
    return is_pre_pci(version) and not standin


def prepare_template_for(version: str, prepare_cmd: str, *, standin: bool) -> str:
    """The default real prepare, for a pre-PCI start, asks for N large collections and does not wait
    for per-collection indexes its engine cannot build. An explicit --prepare-cmd is left alone."""
    if not standin and prepare_cmd == REAL_PREPARE_CMD and is_pre_pci(version):
        return PRE_PCI_PREPARE_CMD
    return prepare_cmd


def expected_pci_indexes(prepare: dict) -> int:
    cols = prepare.get("collections") or {}
    return sum(1 for v in cols.values() if int(v) >= PCI_BUILD_MIN_ROWS)


@dataclasses.dataclass(frozen=True)
class PciObs:
    t: float
    index: str
    pid: int
    phase: str
    blocks_done: int
    blocks_total: int
    tuples_done: int
    tuples_total: int

    def as_dict(self) -> dict:
        return {
            "index": self.index,
            "pid": self.pid,
            "phase": self.phase,
            "blocks_done": self.blocks_done,
            "blocks_total": self.blocks_total,
            "tuples_done": self.tuples_done,
            "tuples_total": self.tuples_total,
            "t_s": round(self.t, 1),
        }


PROGRESS_SQL = (
    "select index_relid::regclass::text, pid, phase, blocks_done, blocks_total, tuples_done, "
    "tuples_total from pg_stat_progress_create_index"
)
VALIDITY_SQL = (
    "select c.relname, i.indisvalid from pg_index i join pg_class c on c.oid = i.indexrelid "
    "where c.relname like 'pci\\_%' escape '\\'"
)


def parse_progress(rows: Sequence[Sequence[str]], t: float) -> list[PciObs]:
    out: list[PciObs] = []
    for r in rows:
        m = PCI_INDEX_RE.match(r[0])
        if m:
            out.append(
                PciObs(t, m.group(1), int(r[1]), r[2], *(int(x or 0) for x in r[3:7]))
            )
    return out


def parse_validity(rows: Sequence[Sequence[str]]) -> dict[str, bool]:
    out: dict[str, bool] = {}
    for name, ok in rows:
        m = PCI_INDEX_RE.match(name)
        if m:
            out[m.group(1)] = ok == "t"
    return out


class PciWatcher:
    """Polls pg_stat_progress_create_index for pci_ builds and keeps every sighting: the pid history
    that tells a kept build from a rebuilt one. ``poll_once`` is the unit; ``start`` runs it on a
    thread every ``poll_s`` until ``stop``."""

    def __init__(
        self,
        sql: SqlFn,
        *,
        poll_s: float = PCI_POLL_S,
        clock: Callable[[], float] = time.monotonic,
    ) -> None:
        self._sql, self._poll_s, self._clock = sql, poll_s, clock
        self._t0 = clock()
        self._lock = threading.Lock()
        self._history: list[PciObs] = []
        self._latest: list[PciObs] = []
        self.errors = 0
        self._stop = threading.Event()
        self._thread: threading.Thread | None = None

    def now(self) -> float:
        return self._clock() - self._t0

    def poll_once(self) -> list[PciObs]:
        try:
            rows = self._sql(PROGRESS_SQL)
        except ProbeError:
            with self._lock:
                self.errors += 1
                self._latest = []
            return []
        obs = parse_progress(rows or [], self.now()) if rows is not None else []
        with self._lock:
            self._latest = obs
            self._history.extend(obs)
        return obs

    def latest(self) -> list[PciObs]:
        with self._lock:
            return list(self._latest)

    def history(self) -> list[PciObs]:
        with self._lock:
            return list(self._history)

    def start(self) -> None:
        def loop() -> None:
            while not self._stop.is_set():
                self.poll_once()
                self._stop.wait(self._poll_s)

        self._thread = threading.Thread(target=loop, name="pci-watcher", daemon=True)
        self._thread.start()

    def stop(self) -> None:
        self._stop.set()
        if self._thread is not None:
            self._thread.join(timeout=10)


@dataclasses.dataclass
class RestartOutcome:
    """What the restart did: ``error`` is None when the engine stopped and started; ``steps`` carries
    each command's own status, exit code, wall time and output tail."""

    error: str | None
    steps: list[dict] = dataclasses.field(default_factory=list)


@dataclasses.dataclass
class RestartRecord:
    """The restart the trigger made: what was in flight at the first sighting, when (watcher clock and
    trigger clock), whether nx upgrade was still running at the start and at the end, the engine-log
    marks taken before the stop, and the restart's own outcome."""

    in_flight: list[PciObs]
    t_s: float
    at_clock: float
    upgrade_running_at_start: bool
    marks: dict
    outcome: RestartOutcome | None = None
    restarted_s: float | None = None
    done_clock: float | None = None
    upgrade_running_after: bool | None = None


class PciRestartTrigger:
    """The second shell. The watcher polls from before nx upgrade; the first time it shows a pci_
    build in flight while nx upgrade is still running, this restarts the engine the way a user would
    (``restart`` runs nx daemon service stop, then start) and records what was in flight and what the
    restart printed. It fires once. ``poll_once`` is the unit (True when finished: fired, or nx
    upgrade ended with nothing in flight); ``start`` runs it on a thread."""

    def __init__(
        self,
        watcher: PciWatcher,
        restart: Callable[[], RestartOutcome],
        upgrade_running: Callable[[], bool],
        engine_log: EngineLog,
        *,
        poll_s: float = PCI_TRIGGER_POLL_S,
        clock: Callable[[], float] = time.monotonic,
    ) -> None:
        self._watcher, self._restart, self._running = watcher, restart, upgrade_running
        self._log, self._poll_s, self._clock = engine_log, poll_s, clock
        self.record: RestartRecord | None = None
        self.finished = False
        self._cancel = threading.Event()
        self._thread: threading.Thread | None = None

    def poll_once(self) -> bool:
        if self.finished:
            return True
        if not self._running():
            self.finished = (
                True  # nx upgrade is over; a later sighting is not "during" it
            )
            return True
        obs = self._watcher.latest()
        if not obs:
            return False
        rec = RestartRecord(
            in_flight=obs,
            t_s=self._watcher.now(),
            at_clock=self._clock(),
            upgrade_running_at_start=True,
            marks=self._log.mark(),
        )
        self.record = rec
        try:
            rec.outcome = self._restart()
        except (
            OSError,
            RehearsalError,
        ) as exc:  # the restart itself broke: evidence, not a crash
            rec.outcome = RestartOutcome(
                f"the restart raised {type(exc).__name__}: {exc}"
            )
        rec.restarted_s = self._watcher.now()
        rec.done_clock = self._clock()
        rec.upgrade_running_after = self._running()
        self.finished = True
        return True

    def start(self) -> None:
        def loop() -> None:
            while not self._cancel.is_set() and not self.poll_once():
                self._cancel.wait(self._poll_s)

        self._thread = threading.Thread(target=loop, name="pci-restart", daemon=True)
        self._thread.start()

    def join(self, timeout: float) -> bool:
        """Wait for the thread; False when a restart is still under way at the timeout."""
        if self._thread is not None:
            self._thread.join(timeout=timeout)
            return not self._thread.is_alive()
        return True

    def cancel(self) -> None:
        self._cancel.set()


class EngineLog:
    """The engine's child log (both stems), read from a mark. A restart can rotate the file at open:
    a file now smaller than its mark is the new one (read whole) and its ``.1`` the old one (read from
    the mark), so a line written during the stop is not lost."""

    def __init__(self, config_dir: Path) -> None:
        self._dir = Path(config_dir) / "logs"

    def mark(self) -> dict[str, int]:
        out: dict[str, int] = {}
        for stem in ENGINE_LOG_STEMS:
            try:
                out[stem] = (self._dir / f"{stem}.log").stat().st_size
            except OSError:
                pass
        return out

    def _read(
        self, path: Path, offset: int, lines: list[str], files: list[str]
    ) -> bool:
        try:
            with path.open("rb") as f:
                f.seek(offset)
                lines.extend(raw.decode("utf-8", "replace") for raw in f)
        except OSError:
            return False
        files.append(path.name)
        return True

    def lines_since(self, marks: dict[str, int]) -> tuple[list[str], list[str]]:
        lines: list[str] = []
        files: list[str] = []
        for stem in ENGINE_LOG_STEMS:
            path = self._dir / f"{stem}.log"
            off = marks.get(stem, 0)
            try:
                size = path.stat().st_size
            except OSError:
                continue
            if (
                size < off
            ):  # rotated at open: the old file's remainder, then the new file
                self._read(Path(f"{path}.1"), off, lines, files)
                off = 0
            self._read(path, off, lines, files)
        return lines, files


@dataclasses.dataclass
class PciReport:
    passed: bool
    findings: list[str]
    evidence: dict
    wall_s: float


def _find_events(lines: Sequence[str], needles: tuple[str, ...]) -> list[str]:
    return [ln.strip()[:300] for ln in lines if all(n in ln for n in needles)]


def adopted_indexes(lines: Sequence[str]) -> set[str]:
    """Indexes the engine log says it adopted (a build kept through the restart)."""
    out: set[str] = set()
    for ln in lines:
        if all(n in ln for n in EVENT_INDEX_ADOPTED):
            out.update(_INDEX_FIELD_RE.findall(ln))
    return out


def judge_pci(
    *,
    in_flight: Sequence[PciObs],
    history: Sequence[PciObs],
    valid: dict[str, bool] | None,
    expected: int,
    still_building: Sequence[PciObs],
    failure_events: dict[str, list[str]],
    log_files: Sequence[str],
    adopted: set[str] = frozenset(),
    restart_t: float = 0.0,
) -> list[str]:
    """The pass rule. By pid: a build is identified by (index, backend pid); a new pid for the same
    index is a rebuild (what the restart fix must prevent). The same pid is not enough to call a
    build kept: it leaves pg_stat_progress_create_index when it finishes AND when the next boot
    terminates it (0.1-3.4 s in), so a kept build also needs the same (index, pid) seen after the
    restart, the engine log's adoption line naming the index, and the index valid at the end."""
    why: list[str] = []
    pids: dict[str, list[int]] = {}
    for o in history:
        if o.pid not in pids.setdefault(o.index, []):
            pids[o.index].append(o.pid)
    still = {o.index: o.pid for o in still_building}
    for o in in_flight:
        seen = pids.get(o.index, [o.pid])
        if len(seen) > 1:
            why.append(
                f"{o.index} was building under pid {o.pid} at the restart and was then built "
                f"under pid(s) {[p for p in seen if p != o.pid]}: the restart killed the build "
                "and it was rebuilt"
            )
    for o in in_flight:
        if not any(
            h.index == o.index and h.pid == o.pid and h.t > restart_t for h in history
        ):
            why.append(
                f"{o.index} (pid {o.pid}) was not seen building again after the restart: it left the "
                "progress view during the stop"
            )
        if o.index not in adopted:
            why.append(
                f"{o.index} (pid {o.pid}) was in flight at the restart but the engine log carries no "
                f"{' '.join(EVENT_INDEX_ADOPTED)} line with index={o.index}: a build that leaves the "
                "progress view without an adoption line was ended by the boot (kept, then terminated), "
                "not kept"
            )
    for idx, ps in sorted(pids.items()):
        if len(ps) > 1 and not any(f"{idx} was building" in w for w in why):
            why.append(f"{idx} was built by {len(ps)} pids {ps}")
    v = valid or {}
    n_valid = sum(1 for ok in v.values() if ok)
    bad = sorted(i for i, ok in v.items() if not ok)
    if n_valid < expected or bad:
        part = (
            f"only {n_valid} of {expected} expected pci_ indexes were valid at the end"
        )
        if bad:
            part += f"; invalid: {bad}"
        why.append(part)
    for o in in_flight:
        if not v.get(o.index):
            why.append(
                f"{o.index}, in flight at the restart under pid {o.pid}, is not valid at the end "
                f"({'invalid' if o.index in v else 'absent from pg_index'})"
            )
        if o.index in still and not v.get(o.index):
            why.append(
                f"{o.index} was still building under pid {still[o.index]} at the end of the bound "
                f"({'its original pid' if still[o.index] == o.pid else 'not its original pid'})"
            )
    for name, lines in failure_events.items():
        if lines:
            why.append(
                f"engine log carries {len(lines)} {name} event(s) after the restart, e.g. {lines[0]}"
            )
    if not log_files:
        why.append(
            "no engine log was readable under logs/ (storage_service_native.log or "
            "storage_service_jar.log): the event scan proved nothing"
        )
    return why


def run_pci_check(
    *,
    watcher: PciWatcher,
    sql: SqlFn,
    record: RestartRecord | None,
    engine_log: EngineLog,
    expected: int,
    end_bound_s: float = PCI_END_STATE_BOUND_S,
    poll_s: float = PCI_POLL_S,
    clock: Callable[[], float] = time.monotonic,
    sleep: Callable[[float], None] = time.sleep,
    note: Callable[[str], None] = lambda _msg: None,
    upgrade: dict | None = None,
) -> PciReport:
    """Judge a restart made DURING nx upgrade. ``record`` is what the trigger did (None: it never saw
    a build in flight while nx upgrade ran, a vacuous FAILURE). After a restart, wait for every pci_
    index to be valid, then judge by pid and by the engine log. ``watcher`` must still be polling (a
    thread in production). ``upgrade`` is nx upgrade's own outcome after the restart, recorded as
    evidence (the step's own rules judge it)."""
    start = clock()
    evidence: dict = {"expected_pci_indexes": expected}
    if upgrade is not None:
        evidence["nx_upgrade"] = upgrade

    def done(findings: list[str]) -> PciReport:
        evidence["builders"] = _builders(watcher.history())
        evidence["timeline"] = _timeline(watcher.history())
        evidence["watcher_probe_errors"] = watcher.errors
        return PciReport(not findings, findings, evidence, round(clock() - start, 1))

    if record is None:
        evidence["in_flight_at_restart"] = []
        seen = sorted({o.index for o in watcher.history()})
        return done(
            [
                "no pci_ index build was seen in flight while nx upgrade was running "
                f"(builds seen at any time, including after it returned: {seen or 'none'}): "
                "the restart was not exercised, so nothing was proved; the builds may have "
                "finished first or never started, so raise --pci-collections"
            ]
        )
    in_flight = record.in_flight
    evidence["in_flight_at_restart"] = [o.as_dict() for o in in_flight]
    evidence["restart_s"] = round(record.t_s, 1)
    evidence["restart"] = {
        "upgrade_running_at_start": record.upgrade_running_at_start,
        "upgrade_running_after": record.upgrade_running_after,
        "steps": list(record.outcome.steps) if record.outcome else [],
    }
    note(
        "restarted during nx upgrade with in flight: "
        + ", ".join(f"{o.index} pid {o.pid}" for o in in_flight)
    )
    if record.outcome is None or record.outcome.error:
        err = record.outcome.error if record.outcome else "the restart never finished"
        return done([f"the engine restart did not complete: {err}"])
    evidence["restarted_s"] = round(record.restarted_s or 0.0, 1)

    deadline = clock() + end_bound_s
    valid: dict[str, bool] | None = None
    reached = False
    while True:
        try:
            rows = sql(VALIDITY_SQL)
        except ProbeError:
            rows = None
        if rows is not None:
            valid = parse_validity(rows)
            if (
                sum(1 for ok in valid.values() if ok) >= expected
                and all(valid.values())
                and not watcher.latest()
            ):
                reached = True
                break
        if clock() >= deadline:
            break
        sleep(poll_s)
    evidence["end_state_reached"] = reached
    evidence["end_state_s"] = round(watcher.now(), 1)
    note(
        f"end state {'reached' if reached else 'NOT reached'} at {evidence['end_state_s']} s: "
        f"{sum(1 for ok in (valid or {}).values() if ok)}/{expected} valid"
    )
    lines, files = engine_log.lines_since(record.marks)
    failure_events = {
        name: _find_events(lines, needles)
        for name, needles in PCI_FAILURE_EVENTS.items()
    }
    evidence["valid_at_end"] = sorted(i for i, ok in (valid or {}).items() if ok)
    evidence["invalid_at_end"] = sorted(i for i, ok in (valid or {}).items() if not ok)
    evidence["log"] = {
        "files": list(files),
        "failure_events": [ln for evs in failure_events.values() for ln in evs],
        "kept": _find_events(lines, EVENT_BUILDERS_KEPT),
        "adopted": _find_events(lines, EVENT_INDEX_ADOPTED),
        "adopted_indexes": sorted(adopted_indexes(lines)),
    }
    why = judge_pci(
        in_flight=in_flight,
        history=watcher.history(),
        valid=valid,
        expected=expected,
        still_building=watcher.latest(),
        failure_events=failure_events,
        log_files=files,
        adopted=adopted_indexes(lines),
        restart_t=record.t_s,
    )
    return done(why)


def output_after(chunks_path: Path, t_s: float, *, limit: int = 12) -> list[str]:
    """The last ``limit`` printable output chunks a step logged at or after ``t_s`` seconds into it
    (``<step>.chunks.jsonl``: one {"t", "n", "s"} per read, ``s`` its first 160 bytes)."""
    out: list[str] = []
    try:
        raw = chunks_path.read_text(errors="replace").splitlines()
    except OSError:
        return out
    for line in raw:
        try:
            c = json.loads(line)
        except ValueError:
            continue
        if float(c.get("t", 0.0)) < t_s:
            continue
        text = " ".join(strip_ansi(str(c.get("s", ""))).replace("\r", " ").split())
        if text and (not out or out[-1] != text):
            out.append(text[:160])
    return out[-limit:]


def upgrade_after_restart(
    step: StepResult, chunks_path: Path, restart_at_s: float, restarted_at_s: float
) -> dict:
    """What nx upgrade did after the second shell restarted the engine under it: its own status,
    exit code, how long it ran on, and the output it printed from the restart on."""
    return {
        "status": step.status,
        "exit_code": step.exit_code,
        "reason": step.reason,
        "wall_s": round(step.wall_s, 1),
        "restart_at_s": round(restart_at_s, 1),
        "restart_done_at_s": round(restarted_at_s, 1),
        "ran_on_after_restart_s": round(max(0.0, step.wall_s - restarted_at_s), 1),
        "ended_during_restart": step.wall_s < restarted_at_s,
        "output_after_restart": output_after(chunks_path, restart_at_s),
    }


def _builders(history: Sequence[PciObs]) -> dict[str, list[int]]:
    out: dict[str, list[int]] = {}
    for o in history:
        if o.pid not in out.setdefault(o.index, []):
            out[o.index].append(o.pid)
    return out


def _timeline(history: Sequence[PciObs]) -> list[dict]:
    """Each (index, pid) build, first and last sighting and where it had got to."""
    spans: dict[tuple[str, int], list[PciObs]] = {}
    for o in history:
        spans.setdefault((o.index, o.pid), []).append(o)
    out = []
    for (idx, pid), xs in sorted(spans.items(), key=lambda kv: kv[1][0].t):
        last = xs[-1]
        out.append(
            {
                "index": idx,
                "pid": pid,
                "first_s": round(xs[0].t, 1),
                "last_s": round(last.t, 1),
                "phase": last.phase,
                "blocks": f"{last.blocks_done}/{last.blocks_total}",
                "tuples": f"{last.tuples_done}/{last.tuples_total}",
            }
        )
    return out


# --- the engine supply override ------------------------------------------------------------------


def resolve_engine_jar(value: str | None) -> str | None:
    """``--engine-jar`` or NEXUS_SERVICE_JAR: a built engine jar to run from the upgrade on."""
    if not value:
        return None
    p = Path(value).expanduser().resolve()
    if not p.is_file():
        raise RehearsalError(f"engine jar {value!r} does not exist")
    return str(p)


def _ambient_java_dir() -> str:
    java = shutil.which("java")
    return str(Path(java).resolve().parent) if java else ""


def engine_env(
    base: dict[str, str], jar: str | None, *, java_dir: str
) -> dict[str, str]:
    """The environment of every step from the old stack's start on: a jar override applies to the
    new client's engine launches only (never to the old client's stack), and a JVM launch needs java."""
    if not jar:
        return dict(base)
    env = dict(base)
    env["NEXUS_SERVICE_JAR"] = jar
    if java_dir:
        env["PATH"] = f"{java_dir}:{env.get('PATH', '')}".rstrip(":")
    return env


# --- verdict ---------------------------------------------------------------------------------


def build_verdict(
    *,
    from_version: str,
    target: str,
    steps: Sequence[StepResult],
    findings: list[str],
    prepare: dict | None,
    floor: int,
    write_rate: dict | None = None,
    pci_check: dict | None = None,
    engine_jar: str | None = None,
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
        "engine_jar": engine_jar,
        "steps": [dataclasses.asdict(s) for s in steps],
        "write_rate": write_rate,
        "pci_check": pci_check,
    }


def _mb(n: int | None) -> str:
    return "n/a" if n is None else f"{n / MB:.1f} MB"


def format_write_rate(wr: dict | None) -> str:
    """One line: what PG wrote during ``nx upgrade`` and at what rate (MB = 2**20 bytes)."""
    if not wr:
        return ""
    rate = "n/a" if wr.get("mb_per_s") is None else f"{wr['mb_per_s']:.1f} MB/s"
    ratio = wr.get("wal_per_store_byte")
    return (
        f"write rate (nx upgrade): {wr['wal_mb']:.1f} MB WAL in {wr['seconds']:.1f} s = {rate}"
        f" (MB = 2**20 bytes); pg_stat_wal {_mb(wr.get('pg_stat_wal_bytes'))};"
        f" pg_stat_io writes {_mb(wr.get('io_write_bytes'))}, extends {_mb(wr.get('io_extend_bytes'))};"
        f" WAL per store byte {'n/a' if ratio is None else ratio}"
    )


def format_pci(pci: dict | None) -> list[str]:
    if not pci:
        return []
    out: list[str] = []
    if pci.get("not_run"):
        for t in pci.get("timeline") or []:
            out.append(
                f"pci watch (check not run): {t['index']} pid {t['pid']} seen {t['first_s']}-{t['last_s']} s"
                f" ({t['phase']}; blocks {t['blocks']}, tuples {t['tuples']})"
            )
        return out or ["pci watch (check not run): no pci_ build seen"]
    rs = pci.get("restart") or {}
    for f in pci.get("in_flight_at_restart") or []:
        out.append(
            f"pci restart check: in flight at the restart (nx upgrade still running: "
            f"{rs.get('upgrade_running_at_start')}): {f['index']} pid {f['pid']}"
            f" ({f['phase']}; blocks {f['blocks_done']}/{f['blocks_total']},"
            f" tuples {f['tuples_done']}/{f['tuples_total']}) at {f['t_s']} s"
        )
    if not pci.get("in_flight_at_restart"):
        out.append(
            "pci restart check: no pci_ build was in flight while nx upgrade ran "
            "(the restart was not exercised)"
        )
    if rs.get("steps"):
        out.append(
            "pci restart check: restart: "
            + "; ".join(
                f"{x['name']} {x['status']} exit {x['exit_code']} in {x['wall_s']:.1f} s"
                for x in rs["steps"]
            )
        )
    up = pci.get("nx_upgrade")
    if up:
        out.append(
            f"pci restart check: nx upgrade after the restart (at {up['restart_at_s']} s of "
            f"{up['wall_s']} s): {up['status']} exit {up['exit_code']}, ran on "
            f"{up['ran_on_after_restart_s']} s; output after it: "
            + (" | ".join(up.get("output_after_restart") or []) or "none")
        )
    for idx, pids in sorted((pci.get("builders") or {}).items()):
        out.append(f"pci restart check: {idx} seen building under pid(s) {pids}")
    if (
        "restarted_s" not in pci
    ):  # the engine was never restarted: nothing after it to report
        return out
    log = pci.get("log") or {}
    out.append(
        f"pci restart check: valid at end {len(pci.get('valid_at_end') or [])}/{pci.get('expected_pci_indexes')};"
        f" engine log after restart: failure events {len(log.get('failure_events') or [])},"
        f" builders_kept {len(log.get('kept') or [])}, index_adopted {len(log.get('adopted') or [])}"
    )
    return out


def format_table(verdict: dict) -> str:
    head = f"upgrade rehearsal: from {verdict['from_version']} to {verdict['target']}"
    cols = f"{'step':<19}{'status':<9}{'wall s':>9}{'longest gap s':>15}{'out bytes':>11}{'exit':>6}  note"
    lines = [head, cols, "-" * len(cols)]
    for s in verdict["steps"]:
        lines.append(
            f"{s['name']:<19}{s['status']:<9}{s['wall_s']:>9.1f}{s['longest_gap_s']:>15.1f}"
            f"{s['output_bytes']:>11}{'' if s['exit_code'] is None else s['exit_code']:>6}  {s['reason']}"
        )
    wr = format_write_rate(verdict.get("write_rate"))
    if wr:
        lines.append(wr)
    lines.extend(format_pci(verdict.get("pci_check")))
    if verdict.get("engine_jar"):
        lines.append(f"engine jar override: {verdict['engine_jar']}")
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


def profiled(logs: Path, argv: list[str]) -> list[str]:
    """``argv`` under py-spy (diagnosis only): idle frames included and every sample timestamped
    (chrometrace), so a silent stretch in the chunk log can be matched to what the process was
    doing. py-spy is the parent of the profiled process, so ptrace_scope 1 allows it. No
    --subprocesses: py-spy would follow the engine nx upgrade starts and never exit."""
    return [
        "uvx",
        "--from",
        "py-spy",
        "py-spy",
        "record",
        "--idle",
        "--format",
        "chrometrace",
        "--rate",
        "20",
        "-o",
        str(logs / "upgrade.pyspy.json"),
        "--",
        *argv,
    ]


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
    standin: bool = False
    engine_jar: str | None = None  # NEXUS_SERVICE_JAR for every step from start-old on
    pci_check: bool | None = None  # None: on for a pre-PCI start
    pci_collections: int = PCI_COLLECTIONS_DEFAULT
    pci_end_bound_s: float = PCI_END_STATE_BOUND_S
    profile_upgrade: bool = (
        False  # run nx upgrade under py-spy (timestamped samples, diagnosis only)
    )


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
    cur = {
        "env": env
    }  # swapped for the engine-override environment once the old stack is up
    pci_on = pci_check_enabled(cfg.pci_check, from_version, standin=cfg.standin)
    write_rate_info: dict | None = None
    pci_evidence: dict | None = None
    probe = PgProbe(config)
    watcher: PciWatcher | None = None
    trigger: PciRestartTrigger | None = None
    upgrade_done = threading.Event()
    upgrade_info: dict | None = None

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
            env=cur["env"],
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

    def restart_engine() -> RestartOutcome:
        """A second shell: nx daemon service stop, then start (no --with-pg), same HOME and env."""
        done: list[dict] = []
        for sub in ("stop", "start"):
            argv = [nx, "daemon", "service", sub]
            say(
                f"[{from_version}] pci-restart-{sub}: {' '.join(argv)} (nx upgrade still running)"
            )
            rr = run_step(
                f"pci-restart-{sub}",
                argv,
                env=cur["env"],
                cwd=cwd,
                silence_limit_s=None,
                ceiling_s=PCI_RESTART_CEILING_S,
                log_dir=logs,
            )
            say(
                f"[{from_version}] pci-restart-{sub}: {rr.status} exit {rr.exit_code} in {rr.wall_s:.1f}s {rr.reason}"
            )
            done.append(
                {
                    "name": f"nx daemon service {sub}",
                    "status": rr.status,
                    "exit_code": rr.exit_code,
                    "wall_s": round(rr.wall_s, 1),
                    "reason": rr.reason,
                    "tail": strip_ansi(rr.tail)[-300:],
                }
            )
            if not rr.ok:
                return RestartOutcome(
                    f"nx daemon service {sub} {rr.status}: {rr.reason}", done
                )
        return RestartOutcome(None, done)

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
                    collections=cfg.pci_collections,
                )
                for t in shlex.split(
                    prepare_template_for(
                        from_version, cfg.prepare_cmd, standin=cfg.standin
                    )
                )
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
        # From here the NEW client runs: a jar override applies to it, never to the old stack above.
        cur["env"] = engine_env(
            env,
            cfg.engine_jar,
            java_dir=_ambient_java_dir() if cfg.engine_jar else "",
        )

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
            (
                "upgrade",
                profiled(logs, [nx, "upgrade"])
                if cfg.profile_upgrade
                else [nx, "upgrade"],
            ),
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
            before: PgCounters | None = None
            if name == "upgrade":
                try:
                    before = take_counters(probe.sql)
                except ProbeError as exc:
                    findings.append(f"write-rate probe failed before nx upgrade: {exc}")
                if pci_on and before is not None:
                    # From before the upgrade boot: every sighting of a pci_ build and its pid counts.
                    watcher = PciWatcher(probe.sql)
                    watcher.start()
                    trigger = PciRestartTrigger(
                        watcher,
                        restart_engine,
                        lambda: not upgrade_done.is_set(),
                        EngineLog(config),
                    )
                    trigger.start()
            upgrade_t0 = time.monotonic()
            try:
                r = go(name, argv, silence=sil, ceiling=cfg.ceilings[name])
            finally:
                if name == "upgrade":
                    upgrade_done.set()
            stop = not r.ok
            if name == "upgrade" and trigger is not None:
                # A restart that began during nx upgrade is given time to finish before doctor starts.
                if not trigger.join(PCI_RESTART_JOIN_S):
                    findings.append(
                        "the PCI restart was still running long after nx upgrade ended"
                    )
                rec = trigger.record
                if rec is not None and rec.done_clock is not None:
                    upgrade_info = upgrade_after_restart(
                        r,
                        logs / "upgrade.chunks.jsonl",
                        rec.at_clock - upgrade_t0,
                        rec.done_clock - upgrade_t0,
                    )
            if name == "upgrade" and before is not None:
                try:
                    write_rate_info = write_rate(
                        before,
                        take_counters(probe.sql),
                        r.wall_s,
                        (prepare or {}).get("data_dir_bytes"),
                    )
                    say(f"[{from_version}] {format_write_rate(write_rate_info)}")
                except ProbeError as exc:
                    findings.append(f"write-rate probe failed after nx upgrade: {exc}")
        if pci_on:
            record = trigger.record if trigger is not None else None
            if watcher is None or (stop and record is None):
                steps.append(
                    StepResult(
                        name=PCI_STEP,
                        status="not-run",
                        reason="an earlier step failed"
                        if watcher is not None
                        else "no PG reading before nx upgrade",
                    )
                )
            else:
                rep_ = run_pci_check(
                    watcher=watcher,
                    sql=probe.sql,
                    record=record,
                    engine_log=EngineLog(config),
                    expected=expected_pci_indexes(prepare or {}),
                    end_bound_s=cfg.pci_end_bound_s,
                    note=lambda m: say(f"[{from_version}] {PCI_STEP}: {m}"),
                    upgrade=upgrade_info,
                )
                pci_evidence = rep_.evidence
                steps.append(
                    StepResult(
                        name=PCI_STEP,
                        status="ok" if rep_.passed else "failed",
                        wall_s=rep_.wall_s,
                        reason="" if rep_.passed else "see findings",
                    )
                )
                findings.extend(f"{PCI_STEP}: {w}" for w in rep_.findings)
    finally:
        upgrade_done.set()
        if trigger is not None:
            trigger.cancel()
            trigger.join(PCI_RESTART_JOIN_S)
        if watcher is not None:
            watcher.stop()
            if pci_evidence is None:
                # The check did not run (an earlier step failed): the builds the watcher saw are still
                # evidence of what the upgrade left running.
                hist = watcher.history()
                pci_evidence = {
                    "not_run": True,
                    "builders": _builders(hist),
                    "timeline": _timeline(hist),
                }
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
                required_steps=JUDGED_STEPS + ((PCI_STEP,) if pci_on else ()),
            )
        )
    verdict = build_verdict(
        from_version=from_version,
        target=cfg.target_label,
        steps=steps,
        findings=findings,
        prepare=prepare,
        floor=cfg.rows_floor,
        write_rate=write_rate_info,
        pci_check=pci_evidence,
        engine_jar=cfg.engine_jar,
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
        help="previous release to start from. 'both' (the release-gate default) runs three starts: "
        f"the newest published version below the candidate, the last published 7.x and "
        f"{PRE_PCI_VERSION} (the PCI keep check), each in its own work dir with its own table "
        "and verdict",
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
        "--engine-jar",
        default=None,
        metavar="PATH",
        help="a built engine jar (NEXUS_SERVICE_JAR) for every step from start-old on, e.g. one "
        "built from the restart fix; default: the NEXUS_SERVICE_JAR of this process, else the "
        "wheel's pinned engine",
    )
    ap.add_argument(
        "--pci-restart-check",
        action=argparse.BooleanOptionalAction,
        default=None,
        help=f"the PCI keep check (restart the engine mid-build, judge by pid); default on for a "
        f"start older than {PCI_FIRST_VERSION}",
    )
    ap.add_argument(
        "--profile-upgrade",
        action="store_true",
        help="diagnosis only: run nx upgrade under py-spy record --idle --format chrometrace, so each "
        "silent stretch can be matched to its stack samples (logs/upgrade.pyspy.json)",
    )
    ap.add_argument(
        "--pci-collections",
        type=int,
        default=PCI_COLLECTIONS_DEFAULT,
        help="large collections the pre-PCI prepare seeds (one pci_ build each, ~30 s; "
        "default %(default)s so a queue exists)",
    )
    ap.add_argument("--pci-end-bound", type=float, default=PCI_END_STATE_BOUND_S)
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
        engine_jar = resolve_engine_jar(
            args.engine_jar or os.environ.get("NEXUS_SERVICE_JAR")
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
        standin=args.standin,
        engine_jar=engine_jar,
        pci_check=args.pci_restart_check,
        pci_collections=args.pci_collections,
        pci_end_bound_s=args.pci_end_bound,
        profile_upgrade=args.profile_upgrade,
    )
    cfg.work.mkdir(parents=True, exist_ok=True)
    say(f"upgrade rehearsal: candidate {label}; starting from {', '.join(versions)}")
    if engine_jar:
        say(f"engine jar override (from start-old on): {engine_jar}")
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
