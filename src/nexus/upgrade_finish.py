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

from nexus.engine_version import REQUIRED_ENGINE_VERSION, parse_engine_version

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


@dataclass(frozen=True)
class SelfStaleness:
    """Whether THIS process is executing code older than the install.

    nexus-g6vb4 (GH #1414): ``detect_stale_processes()`` excludes
    ``pid == me`` by construction (correct for ``nx doctor`` — don't report
    yourself), which means the primitive that diagnoses upgrade skew can
    never be pointed at the process suffering from it. This is the
    self-directed complement: the long-lived host captures
    ``install_mtime_and_version()`` once at startup and compares later.
    """

    stale: bool
    started_version: str
    installed_version: str


def self_staleness(baseline: tuple[float, str]) -> SelfStaleness:
    """Compare the installed distribution against a startup ``baseline``.

    ``baseline`` is the ``install_mtime_and_version()`` tuple captured when
    this process started. A newer dist-info mtime OR a changed version means
    site-packages moved under us — the running module graph is old code.
    Metadata resolution FAILING (venv replaced/removed under us) is itself a
    disk-changed signal: reported as stale with ``installed_version=
    "(unresolvable)"``, never an exception out of a per-tool-call hot path.
    """
    started_mtime, started_version = baseline
    try:
        mtime, version = install_mtime_and_version()
    except Exception:  # noqa: BLE001 — resolution failure IS the stale signal here
        return SelfStaleness(
            stale=True,
            started_version=started_version,
            installed_version="(unresolvable)",
        )
    return SelfStaleness(
        stale=mtime > started_mtime or version != started_version,
        started_version=started_version,
        installed_version=version,
    )


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


def install_dist_info() -> tuple[float, str, Path]:
    """(mtime, version, dist-info path) of the installed conexus distribution.

    The dist-info directory's mtime is when the venv last changed — any
    process started before it is executing old code. The returned path lets
    a long-lived host (nexus-g6vb4) re-check freshness with a single
    ``stat`` instead of a full importlib.metadata resolution per tool call:
    an upgrade either bumps the mtime (same-version reinstall) or replaces
    the directory with a differently-named one (version change → stat
    fails), so "path stats with an unchanged mtime" proves fresh.
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
    return dist_info.stat().st_mtime, version, dist_info


def install_mtime_and_version() -> tuple[float, str]:
    """(mtime, version) of the installed conexus distribution."""
    mtime, version, _ = install_dist_info()
    return mtime, version


def enumerate_processes(ps_output: str | None = None) -> list[tuple[int, int, str]]:
    """``[(pid, age_s, command)]`` for every running conexus process.

    ``ps -eo pid,etime,command`` is POSIX-portable (etime, unlike lstart,
    parses identically on macOS and Linux). Injectable for tests.
    """
    if ps_output is None:
        try:
            proc = subprocess.run(
                ["ps", "-wweo", "pid,etime,command"],
                capture_output=True, text=True, timeout=15,
            )
        except FileNotFoundError as exc:
            # nexus-cfgo9: a minimal-container deployment (no procps package)
            # has no `ps` binary at all -- distinct from "ps ran and failed"
            # below. Re-raised as a RuntimeError (not the bare
            # FileNotFoundError) so this is still fail-loud and diagnosable,
            # never a silent "zero processes" read (review 38b7db3d M5), but
            # with an actionable message. Every caller (check_version_
            # transition, nx doctor's _check_process_skew, nx daemon
            # restart-stale) already degrades this ONE leg gracefully on any
            # Exception and continues — this does not need its own recovery
            # path, just a clear cause.
            raise RuntimeError(
                "the 'ps' command is not available on this system "
                "(install procps, or run on a host that provides it) — "
                "process-skew detection cannot run"
            ) from exc
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
            # nexus-c7odl (critique 60ed904e): this is an AUTOMATED cycle,
            # so it honors the same spawn policy as every other automatic
            # trigger — an operator who set mineru_autostart: false manages
            # the server out-of-band, staleness included. The explicit
            # `nx mineru stop`/`start` verbs remain available and ungated.
            try:
                from nexus.daemon.mineru_lifecycle import spawn_policy_allows  # noqa: PLC0415 — deferred local import — avoids import-time cost / circular deps

                policy_ok = spawn_policy_allows()
            except Exception:  # noqa: BLE001 — policy probe must not break restart-stale
                policy_ok = True
            if not policy_ok:
                actions.append(
                    f"mineru pid {proc.pid} is stale but autostart policy is "
                    "off (pdf.mineru_autostart / NX_MINERU_AUTOSTART) — cycle "
                    "it yourself: `nx mineru stop && nx mineru start`"
                )
                continue
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


# ── nexus-cfgo9: ONE-engine model — converge the installed engine ─────────
#
# GH #1402 (2026-07-15, 14h delivery failure): 6.10.0 shipped
# REQUIRED_ENGINE_VERSION=(0,1,43) + PINNED_SERVICE_TAG=engine-service-v0.1.43,
# but the pin was consumed ONLY by fresh `nx init` — no upgrade path ever
# installed the fix on an EXISTING service-mode box, so the box kept
# crash-looping the old engine indefinitely. The fix: a local engine-version
# mismatch is a CONVERGENCE step (install the dependency, cycle the
# service), driven from the same finish-the-upgrade choreography that
# already restarts stale processes above — never a user-facing refusal.


@dataclass
class EngineConvergence:
    """Whether the local box's installed engine matches the release
    dependency (:data:`nexus.engine_version.REQUIRED_ENGINE_VERSION`).

    ``applicable`` is False for cloud-mode installs (the managed handshake
    governs there, see :mod:`nexus.db.managed_endpoint`) and for local
    installs that are not on the service stack at all (no ``pg_credentials``)
    — neither case has a local engine to converge. When ``applicable`` is
    True, ``converged`` is True only when the installed engine's parsed
    version exactly equals :data:`REQUIRED_ENGINE_VERSION`; an unreadable/
    absent provenance sidecar counts as a mismatch (the safe default is to
    converge, not to assume a match we cannot prove).
    """

    applicable: bool
    installed_version: tuple[int, int, int] | None
    required_version: tuple[int, int, int]
    converged: bool
    reason: str | None = None


def detect_engine_convergence(config_dir: Path) -> EngineConvergence:
    """Compare the box's installed engine against the release dependency.

    "Installed" is read from the provenance sidecar
    :func:`nexus.daemon.binary_lifecycle.read_installed_provenance` writes at
    ``nx daemon service install-binary`` time — the on-disk binary's own
    record, not a live ``/version`` probe. This is deliberate: the incident
    this fix addresses is a CRASH-LOOPING engine, where the running service
    may never answer ``/version`` at all; the disk record is available
    regardless of whether the service is currently up.
    """
    from nexus.config import is_local_mode  # noqa: PLC0415 — deferred for test patchability

    if not is_local_mode():
        return EngineConvergence(
            applicable=False,
            installed_version=None,
            required_version=REQUIRED_ENGINE_VERSION,
            converged=True,
            reason=(
                "cloud mode — the managed handshake governs engine "
                "compatibility, not local convergence"
            ),
        )

    from nexus.db.pg_provision import CREDENTIALS_FILENAME  # noqa: PLC0415 — deferred, circular-dep avoidance

    creds_path = config_dir / CREDENTIALS_FILENAME
    if not creds_path.exists():
        return EngineConvergence(
            applicable=False,
            installed_version=None,
            required_version=REQUIRED_ENGINE_VERSION,
            converged=True,
            reason="service mode not configured (pg_credentials absent)",
        )

    from nexus.daemon.binary_lifecycle import read_installed_provenance  # noqa: PLC0415 — deferred, CLI startup cost

    prov = read_installed_provenance(config_dir)
    raw = prov.get("version") if prov else None
    parsed = parse_engine_version(raw) if isinstance(raw, str) else None
    req_s = ".".join(str(p) for p in REQUIRED_ENGINE_VERSION)

    if parsed is None:
        return EngineConvergence(
            applicable=True,
            installed_version=None,
            required_version=REQUIRED_ENGINE_VERSION,
            converged=False,
            reason=(
                "installed engine version unknown (no readable install "
                f"provenance) — required v{req_s}"
            ),
        )

    converged = parsed == REQUIRED_ENGINE_VERSION
    reason = None
    if not converged:
        got_s = ".".join(str(p) for p in parsed)
        reason = f"installed engine v{got_s} != required v{req_s}"
    return EngineConvergence(
        applicable=True,
        installed_version=parsed,
        required_version=REQUIRED_ENGINE_VERSION,
        converged=converged,
        reason=reason,
    )


@dataclass(frozen=True)
class PoisonProbe:
    """Tri-state chash-poison gate verdict (nexus-pgdcv, GH #1414).

    The predecessor collapsed "probe ran and the store is clean" and "the
    probe could not run" into one ``None`` — and "probe cannot run because
    the service/PG is not up yet" is the ORDINARY ordering on a box being
    converged, so the gate was absent exactly when convergence was most
    likely to fire (Steve Harris's box converged 0.1.35 -> 0.1.49 blind
    over 35,477 poison rows that a later doctor then surfaced).

    Exactly one of three states:
    - POISONED: ``playbook`` is set (render its ``terminal_block()``).
    - UNKNOWN: ``unknown_reason`` is set — the probe could not VERIFY the
      store; the caller defers convergence loudly rather than proceeding
      blind (and never hard-blocks: ``nx daemon service install-binary``
      remains the explicit converge-now escape, with its own gate).
    - CLEAN: both fields ``None`` — the probe ran to completion and found
      zero width-non-conformant rows.
    """

    playbook: object | None = None
    unknown_reason: str | None = None


def _poison_probe(config_dir: Path) -> PoisonProbe:
    """Classify the store via the SAME probe ``nx daemon service
    install-binary``'s gate uses (:func:`nexus.health._check_migration_state`,
    nexus-pnwu0 / GH #1414). Never raises.

    Classification against the health contract:
    - a "Chunk chash conformance" result carrying ``POISON_DETAIL_TOKEN``
      -> POISONED (width-non-conformant rows counted; unhealed ladder debt
      — v0.1.48+ engines tolerate them at boot per nexus-joima, but
      automated convergence must not swap engines under an unhealed store);
    - a token-less "Chunk chash conformance" WARN -> UNKNOWN (health.py's
      explicit "the pre-upgrade poison check could NOT run" marker: missing
      nexus_diag credentials or a probe failure);
    - any ``fatal`` result -> UNKNOWN (``_check_migration_state``
      early-returns before the chash leg when PG is unreachable — absence
      of a conformance result must never read as clean);
    - an exception -> UNKNOWN;
    - otherwise -> CLEAN (the probe ran; a clean store appends nothing).

    The non-gating "Chash legacy debt" label is deliberately ignored (no
    CHECK constraint exists on those tables; nexus-z5j0t).
    """
    try:
        from nexus.db.pg_provision import CREDENTIALS_FILENAME  # noqa: PLC0415 — deferred, circular-dep avoidance
        from nexus.health import _check_migration_state  # noqa: PLC0415 — deferred, CLI startup cost

        creds_path = config_dir / CREDENTIALS_FILENAME
        from nexus.db.chash_tables import (  # noqa: PLC0415 — deferred, circular-dep avoidance
            CHASH_CONFORMANCE_LABEL,
            POISON_DETAIL_TOKEN,
        )

        results = _check_migration_state(creds_path=creds_path)
    except Exception as exc:  # noqa: BLE001 — an unverifiable store is UNKNOWN, never a crash and never a silent clean
        return PoisonProbe(unknown_reason=f"{type(exc).__name__}: {exc}"[:200])

    poison = [
        r for r in results
        if r.label == CHASH_CONFORMANCE_LABEL
        and not r.ok and POISON_DETAIL_TOKEN in r.detail
    ]
    if poison:
        from nexus.remediation import StoreState, emit_playbook  # noqa: PLC0415 — deferred, CLI startup cost

        return PoisonProbe(
            playbook=emit_playbook(
                "chash-poison", StoreState(detail=poison[0].detail),
            ),
        )

    probe_didnt_run = [
        r for r in results
        if r.label == CHASH_CONFORMANCE_LABEL and not r.ok
    ]
    if probe_didnt_run:
        return PoisonProbe(unknown_reason=probe_didnt_run[0].detail[:200])

    fatal = [r for r in results if r.fatal]
    if fatal:
        return PoisonProbe(unknown_reason=fatal[0].detail[:200])

    # Defensive 4th arm (round-2 code-review Low): a not-ok "Schema
    # migrations" result that is NOT fatal (e.g. the creds-absent "service
    # mode not configured" warn) still means the chash leg never ran.
    # Unreachable from converge_engine today (detect_engine_convergence
    # pre-gates on the same creds file), but a second caller without that
    # pre-gate must not read it as clean.
    unverified = [
        r for r in results if r.label == "Schema migrations" and not r.ok
    ]
    if unverified:
        return PoisonProbe(unknown_reason=unverified[0].detail[:200])

    return PoisonProbe()


def converge_engine(config_dir: Path, *, dry_run: bool = False) -> list[str]:
    """Install the release-dependency engine and cycle the service on a
    mismatch. Returns human-readable action lines (empty when not
    applicable or already converged — the common case, mirroring
    :func:`restart_stale`'s silence on the happy path).

    Never raises: a poison-gate block or an install/restart failure is
    reported as a loud ``NEEDS HUMAN`` action line, never a silent skip and
    never a crash that could leave the box worse off.
    """
    status = detect_engine_convergence(config_dir)
    if not status.applicable or status.converged:
        return []

    req_s = ".".join(str(p) for p in status.required_version)
    got_s = (
        ".".join(str(p) for p in status.installed_version)
        if status.installed_version else "unknown"
    )

    # nexus-cfgo9 code-review LOW: the poison gate is checked BEFORE the
    # dry-run early-return, never after — a dry-run preview must never
    # promise a convergence a real run would actually block. Previously the
    # poison check ran only on the real (non-dry-run) path, so `--dry-run`
    # could report "would converge" against a store that would immediately
    # hit NEEDS-HUMAN on the real run.
    probe = _poison_probe(config_dir)
    if probe.playbook is not None:
        playbook = probe.playbook
        if dry_run:
            return [
                f"would be BLOCKED by chash-poison gate ({got_s} -> {req_s}): "
                f"{playbook.terminal_block()}"
            ]
        return [
            "NEEDS HUMAN: engine convergence blocked — the store looks "
            f"chash-poisoned; installed engine stays at {got_s}, required "
            f"{req_s}. Remediate first, then re-run: "
            f"{playbook.terminal_block()}"
        ]
    if probe.unknown_reason is not None:
        # nexus-pgdcv (GH #1414): the probe could not VERIFY the store —
        # the ordinary state on a box being converged (service/PG not up
        # yet). Defer loudly instead of converging blind; the next finish
        # pass / doctor re-attempts once the store is reachable. NOT a
        # NEEDS-HUMAN (nothing is broken) and NOT a hard block: the
        # explicit converge-now escape is install-binary, whose own gate
        # re-checks (and documents --force for the will-not-boot class).
        if dry_run:
            return [
                f"would DEFER engine convergence ({got_s} -> {req_s}): "
                f"store chash conformance unverifiable "
                f"({probe.unknown_reason})"
            ]
        from nexus.daemon.binary_install import PINNED_SERVICE_TAG  # noqa: PLC0415 — deferred, CLI startup cost

        # Round-2 critique HIGH-2/MEDIUM-2: lead with the VERIFIED
        # convergence path (doctor / restart-stale re-run this same
        # tri-state gate), and never promise a passive retry —
        # check_version_transition stamps seen unconditionally, so the
        # re-attempt is operator-driven. install-binary is named last,
        # strictly for the will-not-boot class.
        return [
            f"DEFERRED: engine convergence ({got_s} -> {req_s}) held back — "
            f"the chash-poison gate could not verify the store "
            f"({probe.unknown_reason}). This is the ordinary ordering when "
            "the service/PG is not up yet (GH #1414). Once the service is "
            "up, run `nx doctor` (or `nx daemon restart-stale`) to "
            "converge verified. Only if the service cannot come UP on the "
            "current engine (the will-not-boot class): nx daemon service "
            f"install-binary {PINNED_SERVICE_TAG or '<engine-service-tag>'} "
            "(warns UNVERIFIED when it cannot probe the store)."
        ]

    if dry_run:
        return [
            f"would converge engine ({got_s} -> {req_s}): install the "
            "pinned tag and restart the storage service"
        ]

    from nexus.daemon.binary_install import (  # noqa: PLC0415 — deferred, CLI startup cost
        PINNED_SERVICE_TAG,
        install_binary,
    )

    tag = PINNED_SERVICE_TAG
    if not tag:
        return [
            f"NEEDS HUMAN: engine convergence needed ({got_s} -> {req_s}) "
            "but no pinned service tag is configured — set "
            "NEXUS_SERVICE_TAG or reinstall conexus."
        ]

    try:
        install_binary(tag, config_dir, installed_by="upgrade-finish engine convergence")
    except Exception as exc:  # noqa: BLE001 — code-review HIGH: install_binary
        # can raise more than BinaryVerificationError -- _atomic_copy
        # (binary_install.py) re-raises bare OSError/etc UNWRAPPED on
        # disk-full, permission-denied, or mkdir failure. A narrower catch
        # let those escape uncaught: silently absorbed by the auto path's
        # outer try/except in check_version_transition (the exact GH #1402
        # silent-failure shape -- the finish pass would look like "nothing
        # to converge"), and an unhandled traceback on the CLI path that
        # also skipped the heal leg entirely. "Never raises" (this
        # function's own docstring contract) means EVERY exception here,
        # not just the expected one.
        return [f"NEEDS HUMAN: engine convergence failed installing {tag}: {exc}"]

    actions = [f"converged engine: installed {tag} (was {got_s})"]
    try:
        stop = subprocess.run(
            ["nx", "daemon", "service", "stop"], capture_output=True, timeout=60,
        )
        start = subprocess.run(
            ["nx", "daemon", "service", "start"], capture_output=True, timeout=120,
        )
        if stop.returncode == 0 and start.returncode == 0:
            actions.append(
                "restarted the storage service to pick up the converged engine"
            )
        else:
            actions.append(
                "NEEDS HUMAN: engine installed but the service restart did "
                "not report success — run `nx daemon service stop && nx "
                "daemon service start` yourself to pick it up"
            )
    except Exception as exc:  # noqa: BLE001 — best-effort cycle; failure surfaced in the action line
        actions.append(
            f"NEEDS HUMAN: engine installed but restarting the service "
            f"raised {exc} — run `nx daemon service stop && nx daemon "
            "service start` yourself to pick it up"
        )
    return actions


def heal_diag_view(config_dir: Path) -> list[str]:
    """GH #1402's SECOND symptom: repair drift on
    ``nexus.diag_chash_conformance`` (grants + ownership only — no DDL that
    creates or alters the view's definition; see
    :func:`nexus.db.pg_provision.heal_diag_view_grants_and_ownership`, which
    this thinly wires up). Runs unconditionally alongside engine convergence
    in the finish pass — the grant/ownership drift is orthogonal to engine
    version, so this is not gated on a mismatch.

    Best-effort: degrades to ``[]`` on any probe failure (PG down, not
    service mode, no PG binaries on this box) — a probe that cannot run must
    never break the finish pass. Returns loud action lines only for what was
    actually healed (silent on the common case: nothing to fix).
    """
    from nexus.config import is_local_mode  # noqa: PLC0415 — deferred for test patchability

    if not is_local_mode():
        return []

    from nexus.db.pg_provision import CREDENTIALS_FILENAME  # noqa: PLC0415 — deferred, circular-dep avoidance

    creds_path = config_dir / CREDENTIALS_FILENAME
    if not creds_path.exists():
        return []

    try:
        from nexus.db.pg_provision import (  # noqa: PLC0415 — deferred, circular-dep avoidance
            _read_credentials,
            bootstrap_superuser,
            discover_pg_binaries,
            heal_diag_view_grants_and_ownership,
        )

        creds = _read_credentials(creds_path)
        port = int(creds.get("PG_PORT", 0) or 0)
        if port <= 0:
            return []
        bins = discover_pg_binaries()
        os_user = bootstrap_superuser()
        return heal_diag_view_grants_and_ownership(bins, port, os_user)
    except Exception as exc:  # noqa: BLE001 — best-effort heal; must never break the finish pass
        _log.debug("diag_view_heal_failed", error=str(exc))
        return []


# ── nexus-c0vby: service mode must never leave a T2 LaunchAgent installed ──
#
# GH #1405 defect 2 (2026-07-15, 6.10.1 shakeout): in service mode
# ``t2_daemon.py``'s own entry point immediately no-ops
# (``t2_daemon_not_started_service_mode``) — the T2 tier is the frozen
# migration source, never a live substrate, once the box is service-backed.
# But a com.nexus.t2 LaunchAgent installed BEFORE the box switched to
# service mode (or before this fix shipped) still has ``KeepAlive=true``,
# so launchd respawns the immediately-exiting process every ~10s FOREVER —
# 663KB of log in half a day, observed live. The fix mirrors
# converge_engine/heal_diag_view's shape exactly: an independent,
# never-raising leg of the finish pass with loud action lines only for
# what was actually done.

#: The unit is a LaunchAgent on macOS, a systemd user unit on Linux
#: (:func:`nexus.daemon.installer.uninstall_autostart` dispatches
#: launchctl/systemctl per platform) — code-review round 1, Low: the
#: user-facing action/NEEDS-HUMAN strings previously hardcoded
#: "LaunchAgent" unconditionally, which would misname the mechanism on a
#: Linux operator's screen. ``result.dest`` (the actual unit path)
#: already discloses which platform ran; this phrase is only so a Linux
#: reader isn't confused by "LaunchAgent" standing alone.
_T2_AUTOSTART_UNIT_KIND = (
    "com.nexus.t2 LaunchAgent on macOS, nexus-t2.service on Linux"
)


def unload_stale_t2_launchagent(config_dir: Path) -> list[str]:
    """Remove a service-mode box's stray ``com.nexus.t2`` LaunchAgent.

    ``config_dir`` is accepted but not read: the storage-mode flag and the
    autostart unit are both process/filesystem-global, not config-dir
    scoped. Kept as a parameter purely so this leg's call signature
    matches its siblings (:func:`converge_engine`, :func:`heal_diag_view`)
    at every call site (the finish pass, ``nx daemon restart-stale``,
    ``nx init --service``) without a special case.

    Gated on the SAME oracle ``t2_daemon.py`` itself checks
    (:func:`nexus.db.storage_mode.storage_backend_for` — env-based, no
    filesystem probe needed) — so this leg fires exactly when the T2
    daemon would have declined to start anyway. Local-mode boxes (or any
    box where the T2 tier is the live substrate) are untouched: a local
    ``nx daemon t2 install --autostart`` round-trip must keep recreating
    the agent (verified in the test suite), never fought by this leg.

    Delegates the actual removal to
    :func:`nexus.daemon.installer.uninstall_autostart` (``tier="t2"``) —
    the SAME launchctl-bootout + plist-removal primitive ``nx daemon t2
    uninstall --autostart`` already uses; no hand-typed duplicate of the
    launchd mechanics here.

    Never raises. Mirrors :func:`heal_diag_view`'s two-tier discipline:
    a failure just DETERMINING applicability (can't read the storage-mode
    flag, can't probe for the unit file) degrades SILENTLY to ``[]`` —
    the same "probe that cannot run must never break the finish pass"
    contract as every other best-effort check in this module. Only a
    failure while ACTUALLY REMOVING an agent this function has already
    confirmed is present is reported as a loud ``NEEDS HUMAN`` action
    line — there IS something a human needs to act on in that case.
    """
    try:
        from nexus.db.storage_mode import (  # noqa: PLC0415 — deferred, circular-dep avoidance
            StorageBackend,
            storage_backend_for,
        )

        if storage_backend_for("memory") != StorageBackend.SERVICE:
            return []

        from nexus.commands.daemon import _autostart_unit_installed  # noqa: PLC0415 — deferred, CLI startup cost

        if _autostart_unit_installed() is None:
            return []
    except Exception as exc:  # noqa: BLE001 — best-effort applicability probe; must never break the finish pass
        _log.debug("t2_launchagent_applicability_probe_failed", error=str(exc))
        return []

    try:
        from nexus.daemon.installer import (  # noqa: PLC0415 — deferred, CLI startup cost
            UninstallStatus,
            uninstall_autostart,
        )

        result = uninstall_autostart(tier="t2")
    except Exception as exc:  # noqa: BLE001 — a CONFIRMED-present agent failed to remove; loud, never a crash
        _log.warning("t2_launchagent_unload_failed", error=str(exc))
        return [
            f"NEEDS HUMAN: service mode detected a stray T2 autostart unit "
            f"({_T2_AUTOSTART_UNIT_KIND}) but could not remove it ({exc}) — "
            "run `nx daemon t2 uninstall --autostart` yourself"
        ]

    if result.status != UninstallStatus.REMOVED:
        return []  # NOT_INSTALLED — the probe above already filtered this, defensive only

    actions = [
        f"removed the stray T2 autostart unit ({_T2_AUTOSTART_UNIT_KIND}; "
        "service mode — the T2 daemon is never started; storage is the "
        f"engine service): {result.dest}"
    ]
    actions.extend(f"NOTE: {w}" for w in result.warnings)
    return actions


def pending_data_rung_callout() -> list[str]:
    """One summary line per pending DATA rung after an engine auto-converge
    (RDR-180 / critic-180-cohort finding 2). The chash-rekey rung gets an
    explicit consequence statement — its not-yet-run state silently breaks
    citation resolution for pre-existing content, unlike earlier rungs
    whose unconverted rows were merely inert. Best-effort and read-only:
    detect() failures degrade to no callout (doctor remains the backstop).
    """
    from nexus.upgrade_ladder.registry import default_registry  # noqa: PLC0415 — deferred, CLI startup cost

    lines: list[str] = []
    for rung in default_registry():
        try:
            status = rung.detect()
        except Exception:  # noqa: BLE001 — callout is best-effort; doctor is the backstop
            continue
        if not status.pending:
            continue
        if rung.name == "chash-rekey":
            lines.append(
                "chash-rekey PENDING — chash citations for existing content "
                "will not resolve until `nx upgrade` runs the rekey"
            )
        else:
            lines.append(f"data rung '{rung.name}' pending — run `nx upgrade`")
    return lines


def check_version_transition(config_dir: Path) -> str | None:
    """Version-stamp auto-trigger. Returns a one-line summary when a
    version transition was detected and the safe finish pass ran; None
    when the stamp is current (the overwhelmingly common case).

    uv offers no post-install hook, so the first invocation after an
    upgrade is the earliest the product can finish the job itself.

    TOPOLOGY GAP (inherited from nexus-4xgfy, same posture as MCP-host
    process-skew): this trigger fires on the first ``nx`` CLI invocation
    after a package upgrade. A long-lived MCP host process that survives
    the upgrade with no CLI invocation on the box in the meantime never
    hits this trigger, so engine convergence for that box is not
    automatic in that window either — MCP hosts are never auto-restarted
    (they belong to a live Claude session; ``restart_stale`` only NAMES
    them for the human, same as for process-skew). The backstop is
    ``nx doctor`` (:func:`nexus.health._check_engine_convergence`), which
    runs in its own fresh subprocess and surfaces "engine convergence
    pending" independent of whether any CLI trigger has fired — so a
    human path to detection always exists even when no automatic trigger
    does.
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
        # nexus-p78a0 rehearsal catch: this leg used to `return None`,
        # silently aborting the WHOLE finish pass — on a ps-less box
        # (minimal container, stripped host) engine convergence and the
        # pending-data-rung callout never ran, exactly the independent-legs
        # regression the nexus-cfgo9 comment below forbids for the other
        # legs. Degrade THIS leg and continue.
        _log.warning("upgrade_finish_failed", exc_info=True)
        actions = [
            "NOTE: process-skew detection unavailable on this box "
            "(see logs); continuing with the remaining finish legs"
        ]
    # nexus-cfgo9: engine convergence and the diag-view heal are two more
    # independent legs of the finish pass — each try/excepted on its own so
    # one leg's failure never swallows the actions already computed by the
    # others.
    try:
        actions = actions + converge_engine(config_dir)
    except Exception:  # noqa: BLE001 — the finish pass must never break CLI startup
        _log.warning("engine_convergence_failed", exc_info=True)
    try:
        actions = actions + heal_diag_view(config_dir)
    except Exception:  # noqa: BLE001 — the finish pass must never break CLI startup
        _log.warning("diag_view_heal_failed", exc_info=True)
    try:
        actions = actions + unload_stale_t2_launchagent(config_dir)
    except Exception:  # noqa: BLE001 — the finish pass must never break CLI startup
        _log.warning("t2_launchagent_unload_failed", exc_info=True)
    # critic-180-cohort finding 2: engine convergence swaps the binary (and
    # boot applies the RDR-180 schema) but does NOT walk the ladder — a box
    # can sit engine-converged-but-never-rekeyed, with citations for
    # PRE-EXISTING content silently unresolvable until `nx upgrade` runs the
    # chash-rekey rung. Surface that state in THIS summary, loudly, instead
    # of leaving it to nx doctor alone.
    try:
        actions = actions + pending_data_rung_callout()
    except Exception:  # noqa: BLE001 — the finish pass must never break CLI startup
        _log.warning("pending_rung_callout_failed", exc_info=True)
    _log.info(
        "upgrade_finish_ran",
        from_version=seen, to_version=version, actions=actions,
    )
    if not actions:
        return f"upgraded {seen} -> {version}; no stale processes"
    return (
        f"upgraded {seen} -> {version}; " + "; ".join(actions)
    )
