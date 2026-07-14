# SPDX-License-Identifier: AGPL-3.0-or-later
"""Finish-the-upgrade: process-skew detection + safe restart (nexus-4xgfy).

Three live incidents motivated this module (2026-07-13/14, the 6.7.0 and
6.7.1 upgrades): after ``uv tool upgrade conexus``, ``nx --version`` and
``nx doctor`` both reported the new version while EVERY long-lived process
on the box (MCP hosts, the aspect-worker — twice orphaned to ppid 1 — and
the MinerU server) kept executing the old code from memory. Nothing
surfaced the skew and nothing fixed it short of tribal knowledge.

The disk is upgraded; the *machine* is not, until stale processes restart.
uv offers no post-install hook (no package manager in this class does), so
the finish choreography triggers from the product side:

- :func:`detect_stale_processes` — every running conexus-venv process whose
  start time predates the installed distribution's mtime is executing old
  code. Feeds the ``nx doctor`` check and the auto-trigger.
- :func:`restart_stale` — restarts the classes that are SAFE to cycle
  (detached daemons: aspect-worker, MinerU); reports the ones only the
  human can close (MCP hosts belong to live Claude sessions).
- :func:`install_source` — reads the uv receipt so "``uv tool upgrade``
  did nothing" is self-explanatory (directory-tracking vs pinned vs PyPI).
- The version stamp (:func:`check_version_transition`) — called at CLI
  startup; on the first invocation after a version change it runs the safe
  finish pass automatically and prints one summary line.
"""
from __future__ import annotations

import os
import re
import subprocess
import time
from dataclasses import dataclass, field
from pathlib import Path

import structlog

_log = structlog.get_logger(__name__)

#: Fallback marker substrings identifying conexus processes in `ps`
#: output. The AUTHORITATIVE marker is derived per-call from the running
#: distribution's actual install root (critique 38b7db3d: a hardcoded
#: production-only literal both fails open on custom install layouts and
#: let a dev-checkout invocation measure PRODUCTION processes against the
#: dev venv's mtime — the cross-venv confusion that could SIGTERM a live
#: worker from an unrelated dev command).
_PROC_MARKERS = ("uv/tools/conexus", ".local/bin/nx")


def _install_root() -> Path:
    """Site-packages root of the RUNNING conexus distribution."""
    import importlib.metadata as md  # noqa: PLC0415 — stdlib, deferred

    return Path(str(md.distribution("conexus").locate_file("")))


def running_from_tool_install() -> bool:
    """True when this interpreter IS the uv tool install (vs a dev
    checkout venv). The restart pass only ever acts from the tool
    install — a dev venv's mtime says nothing about production
    processes and must never kill them."""
    return "uv/tools/conexus" in str(_install_root())

#: Filename of the version stamp inside the nexus config dir.
STAMP_FILENAME = "last_seen_version"


@dataclass
class StaleProcess:
    pid: int
    kind: str  # "mcp-host" | "aspect-worker" | "mineru" | "service" | "other"
    command: str
    age_s: int  # process age in seconds

    @property
    def restartable(self) -> bool:
        """Safe to cycle without severing a live human session."""
        return self.kind in ("aspect-worker", "mineru")


@dataclass
class SkewReport:
    installed_version: str = ""
    install_mtime: float = 0.0
    stale: list[StaleProcess] = field(default_factory=list)

    @property
    def session_bound(self) -> list[StaleProcess]:
        return [p for p in self.stale if not p.restartable]

    @property
    def restartable(self) -> list[StaleProcess]:
        return [p for p in self.stale if p.restartable]


def _classify(command: str) -> str:
    if "aspect-worker" in command:
        return "aspect-worker"
    if "mineru" in command:
        return "mineru"
    if "nx-mcp" in command:
        return "mcp-host"
    if "daemon service" in command or "nexus-service" in command:
        return "service"
    return "other"


def _parse_etime(etime: str) -> int:
    """``[[dd-]hh:]mm:ss`` -> seconds (POSIX ps etime)."""
    days = 0
    if "-" in etime:
        d, etime = etime.split("-", 1)
        days = int(d)
    parts = [int(p) for p in etime.split(":")]
    while len(parts) < 3:
        parts.insert(0, 0)
    h, m, s = parts
    return ((days * 24 + h) * 60 + m) * 60 + s


def install_mtime_and_version() -> tuple[float, str]:
    """(mtime, version) of the installed conexus distribution.

    The dist-info directory's mtime is when the venv last changed — any
    process started before it is executing old code.
    """
    import importlib.metadata as md  # noqa: PLC0415 — stdlib, deferred for startup cost

    dist = md.distribution("conexus")
    version = dist.version
    # PUBLIC API only (review 38b7db3d Critical-1: the prior dist._path
    # private-attr read fell back to mtime=0.0 when absent, which made
    # `started < mtime` always false — silently disabling ALL skew detection,
    # the exact fail-open this module exists to eliminate). locate_file("")
    # is the documented site-packages root; the dist-info dir name is
    # deterministic from name+version. Missing => RAISE (fail loud).
    root = Path(str(dist.locate_file("")))
    dist_info = root / f"conexus-{version}.dist-info"
    if not dist_info.exists():
        raise RuntimeError(
            f"cannot locate conexus dist-info under {root} — "
            "process-skew detection unavailable in this environment"
        )
    return dist_info.stat().st_mtime, version


def enumerate_processes(ps_output: str | None = None) -> list[tuple[int, int, str]]:
    """``[(pid, age_s, command)]`` for every running conexus process.

    ``ps -eo pid,etime,command`` is POSIX-portable (etime, unlike lstart,
    parses identically on macOS and Linux). Injectable for tests.
    """
    if ps_output is None:
        proc = subprocess.run(
            ["ps", "-wweo", "pid,etime,command"],
            capture_output=True, text=True, timeout=15,
        )
        if proc.returncode != 0 or not proc.stdout.strip():
            # Review 38b7db3d M5: a silent empty ps = zero processes
            # detected = the fail-open class again. Fail loud instead.
            raise RuntimeError(
                f"ps failed (rc={proc.returncode}): {proc.stderr.strip()[:200]}"
            )
        ps_output = proc.stdout
    out: list[tuple[int, int, str]] = []
    me = os.getpid()
    try:
        # site-packages -> lib/pythonX.Y -> lib -> THE VENV ROOT: the one
        # path every process launched from this install carries.
        markers: tuple[str, ...] = (str(_install_root().parents[2]),)
    except Exception:  # noqa: BLE001 — metadata unavailable: fall back to the conventional layout
        markers = _PROC_MARKERS
    for line in ps_output.splitlines()[1:]:
        m = re.match(r"\s*(\d+)\s+(\S+)\s+(.*)", line)
        if not m:
            continue
        pid, etime, command = int(m.group(1)), m.group(2), m.group(3)
        if pid == me or not any(k in command for k in markers):
            continue
        try:
            age = _parse_etime(etime)
        except ValueError:
            continue
        out.append((pid, age, command))
    return out


def detect_stale_processes(
    ps_output: str | None = None,
    *,
    now: float | None = None,
) -> SkewReport:
    """Every conexus process older than the installed distribution."""
    mtime, version = install_mtime_and_version()
    report = SkewReport(installed_version=version, install_mtime=mtime)
    now = time.time() if now is None else now
    for pid, age_s, command in enumerate_processes(ps_output):
        started = now - age_s
        if started < mtime:
            report.stale.append(StaleProcess(
                pid=pid, kind=_classify(command),
                command=command, age_s=age_s,
            ))
    return report


def restart_stale(report: SkewReport, *, dry_run: bool = False) -> list[str]:
    """Cycle the restartable classes; return human-readable action lines.

    aspect-worker: killed — it respawns on demand from a fresh host (and
    an orphaned one at ppid 1 is executing old code with no owner at all;
    observed twice in two days). MinerU: cycled via its own lifecycle
    verbs. MCP hosts are never touched — they belong to live Claude
    sessions; the report names them for the human.
    """
    actions: list[str] = []
    for proc in report.restartable:
        if dry_run:
            actions.append(f"would restart {proc.kind} (pid {proc.pid})")
            continue
        if proc.kind == "aspect-worker":
            try:
                # Review 38b7db3d High-3 (pid-recycle TOCTOU): re-verify the
                # pid still runs OUR command immediately before signaling —
                # the same convention as t2_daemon's pre-kill re-check.
                probe = subprocess.run(
                    ["ps", "-p", str(proc.pid), "-o", "command="],
                    capture_output=True, text=True, timeout=10,
                )
                current = probe.stdout.strip()
                if "aspect-worker" not in current or not any(
                    k in current for k in _PROC_MARKERS
                ):
                    actions.append(
                        f"{proc.kind} pid {proc.pid}: gone or recycled; skipped"
                    )
                    continue
                import signal as _signal  # noqa: PLC0415 — stdlib, deferred

                os.kill(proc.pid, _signal.SIGTERM)
                # Critique 38b7db3d C3: the worker's graceful drain is
                # bounded at 10s while an in-flight claude -p child can run
                # far longer, and PDEATHSIG is inactive on macOS (the RF8
                # orphan gap). Poll for ACTUAL exit past the drain window;
                # never SIGKILL (that is what orphans the child), and never
                # claim success we did not observe.
                deadline = time.time() + 12
                exited = False
                while time.time() < deadline:
                    try:
                        os.kill(proc.pid, 0)
                    except ProcessLookupError:
                        exited = True
                        break
                    time.sleep(0.5)
                if exited:
                    actions.append(
                        f"restarted {proc.kind} (pid {proc.pid} drained; "
                        "respawns on demand)"
                    )
                else:
                    actions.append(
                        f"{proc.kind} pid {proc.pid}: SIGTERM sent but still "
                        "draining (likely an in-flight extraction) — left "
                        "running; re-check with `nx doctor`"
                    )
            except (ProcessLookupError, PermissionError) as exc:
                actions.append(f"{proc.kind} pid {proc.pid}: {exc}")
        elif proc.kind == "mineru":
            try:
                subprocess.run(["nx", "mineru", "stop"], capture_output=True,
                               timeout=60)
                subprocess.run(["nx", "mineru", "start"], capture_output=True,
                               timeout=300)
                actions.append(f"cycled MinerU (was pid {proc.pid})")
            except Exception as exc:  # noqa: BLE001 — best-effort cycle; failure surfaced in the action line
                actions.append(f"mineru cycle failed: {exc}")
    for proc in report.session_bound:
        if proc.kind == "mcp-host":
            remedy = (
                "belongs to a live Claude session — exit that session to "
                f"pick up {report.installed_version}"
            )
        elif proc.kind == "service":
            remedy = (
                "is the storage service — cycle it via its own lifecycle "
                "(`nx daemon service stop` / next use respawns it)"
            )
        else:
            remedy = f"predates {report.installed_version}; restart it manually"
        actions.append(f"NEEDS HUMAN: {proc.kind} (pid {proc.pid}) {remedy}")
    return actions


def install_source() -> str:
    """Human-readable uv-receipt source: directory / pinned / PyPI.

    Explains why ``uv tool upgrade`` may report "Nothing to upgrade":
    a directory-tracking install never consults PyPI, and an ==-pinned
    one never moves past its pin (both live incidents, 2026-07-13).
    """
    import tomllib  # noqa: PLC0415 — stdlib, deferred for startup cost

    receipt = Path.home() / ".local/share/uv/tools/conexus/uv-receipt.toml"
    try:
        data = tomllib.loads(receipt.read_text())
    except (OSError, tomllib.TOMLDecodeError):
        return "unknown (no readable uv receipt)"
    reqs = (data.get("tool") or {}).get("requirements") or data.get("requirements") or []
    req = next(
        (r for r in reqs if isinstance(r, dict) and r.get("name") == "conexus"),
        {},
    )
    if req.get("directory"):
        return (
            f"local checkout ({req['directory']}) — `uv tool upgrade` never "
            "consults PyPI for this install; use scripts/reinstall-tool.sh "
            "or reinstall from PyPI"
        )
    spec = str(req.get("specifier", ""))
    if spec.startswith("=="):
        return (
            f"PyPI, PINNED ({spec}) — `uv tool upgrade` will never move "
            "past the pin; reinstall unpinned "
            "(`uv tool install --reinstall conexus`)"
        )
    return "PyPI, unpinned — `uv tool upgrade conexus` upgrades normally"


def check_version_transition(config_dir: Path) -> str | None:
    """Version-stamp auto-trigger. Returns a one-line summary when a
    version transition was detected and the safe finish pass ran; None
    when the stamp is current (the overwhelmingly common case).

    uv offers no post-install hook, so the first invocation after an
    upgrade is the earliest the product can finish the job itself.
    """
    try:
        _, version = install_mtime_and_version()
    except Exception:  # noqa: BLE001 — metadata unavailable (frozen/test env): never block startup
        return None
    stamp = config_dir / STAMP_FILENAME
    try:
        seen = stamp.read_text().strip()
    except OSError:
        seen = ""
    if seen == version:
        return None
    try:
        config_dir.mkdir(parents=True, exist_ok=True)
        # Review 38b7db3d M4: two concurrent nx invocations right after an
        # upgrade must not BOTH run the finish pass (a doubled MinerU
        # stop/start can race itself broken). O_EXCL claim: exactly one
        # transitioner; losers skip (the winner's pass covers them).
        lock = config_dir / (STAMP_FILENAME + ".lock")
        try:
            fd = os.open(lock, os.O_CREAT | os.O_EXCL | os.O_WRONLY)
            os.close(fd)
        except FileExistsError:
            return None
        try:
            stamp.write_text(version + "\n")
        finally:
            try:
                lock.unlink()
            except OSError:
                pass
    except OSError:
        return None  # unwritable config dir: skip silently, retry next run
    if not seen:
        return None  # first-ever run: nothing stale to finish
    if not running_from_tool_install():
        # A dev checkout's venv mtime says nothing about the production
        # processes on this box — measuring (let alone killing) them from
        # here is the cross-venv confusion class. Report-only via doctor.
        return None
    try:
        report = detect_stale_processes()
        actions = restart_stale(report)
    except Exception:  # noqa: BLE001 — the finish pass must never break CLI startup
        _log.warning("upgrade_finish_failed", exc_info=True)
        return None
    _log.info(
        "upgrade_finish_ran",
        from_version=seen, to_version=version, actions=actions,
    )
    if not actions:
        return f"upgraded {seen} -> {version}; no stale processes"
    return (
        f"upgraded {seen} -> {version}; " + "; ".join(actions)
    )
