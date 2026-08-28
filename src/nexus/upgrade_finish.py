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
- :func:`install_source` — reads the CURRENT generation's receipt, else
  the uv receipt, so "the upgrade did nothing" is self-explanatory
  (directory-tracking vs pinned vs PyPI).
- The version stamp (:func:`check_version_transition`) — called at CLI
  startup; on the first invocation after a version change it runs the safe
  finish pass automatically and prints one summary line.
"""
from __future__ import annotations

import os
import subprocess
import sys
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import TYPE_CHECKING

import structlog

from nexus.daemon.service_registry import (
    _parse_etime,
    _procfs_enumerate,
    all_process_rows,
    process_command,
    process_state,
    storage_service_stack_matcher,
    terminate_pids,
)
#: SEMANTIC CHANGE (nexus-oyo2g round 3, critique T2 [21510] Significant-1):
#: this module used to define its OWN ``_pid_alive`` which treated any
#: non-ESRCH ``OSError`` as DEAD (``except OSError: return False``). The
#: round-2 pid_alive consolidation replaced it with this re-export of the
#: shared primitive, which treats an ambiguous (non-ESRCH) ``OSError`` as
#: ALIVE instead (``except OSError as exc: return exc.errno != errno.
#: ESRCH``). This module's one call site (``_sweep_surviving_stack``'s
#: pre-recycle-guard liveness check, which gates whether a pre-stop stack
#: survivor is re-verified and terminated by the nexus-cfgo9 convergence
#: sweep) is why alive-on-ambiguity is the correct direction here too:
#: declaring a stack member dead on an ambiguous errno would let the
#: sweep skip a genuinely-still-running process, the same false-all-clear
#: shape this whole bead exists to close in ``stop_storage_service``
#: itself (the primitive's other consumer). See
#: ``tests/test_upgrade_finish.py::TestPidAliveAmbiguousOSErrorSemantics``
#: for the call-site-level pin (the primitive's own identity test does
#: not exercise this behavior).
from nexus.daemon.service_registry import pid_alive as _pid_alive
from nexus.engine_version import REQUIRED_ENGINE_VERSION, parse_engine_version

if TYPE_CHECKING:
    from nexus import install_layout

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
    """True when this interpreter IS a MANAGED install (vs a dev checkout venv).

    The pass only ever acts from a managed install — a dev venv's mtime says
    nothing about the production processes on this box, and measuring, let
    alone killing, them from there is the cross-venv confusion class. That
    property is why this cannot simply return True.

    Two shapes are managed, and both must answer yes:

    * a GENERATION, ``<tools>/gen-<stamp>`` (nexus-utpuw)
    * the LEGACY uv tool tree, for a box that has not migrated yet. The
      migration window is deliberately long — .7 leaves the old tree in place
      until it has zero holders — so dropping this would turn the pass off on
      every un-migrated box, trading one silent no-op for another.

    THIS USED TO BE ``"uv/tools/conexus" in str(_install_root())``. Under the
    generation layout the install root is ``<tools>/gen-<stamp>/lib/...``, so it
    was False on every migrated box — and the ``return None`` it guards sits
    upstream of restart-stale, converge_engine, the diag-view heal, both
    launchagent unloads and the pending-data-rung callout. The whole finish pass
    went quiet, and nothing said so. nexus-p78a0 already fixed this exact
    coupling one leg down, for a ps-less box; the gate above it kept doing it.
    """
    root = _install_root()

    try:
        venv_root = root.parents[2]
    except IndexError:  # a root too shallow to be a venv layout
        return False

    if generation_of(venv_root) is not None:
        return True

    # nexus-orhp5: containment against uv's ACTUAL tool root, not a
    # substring. The old test answered No for the real tree under a
    # relocated UV_TOOL_DIR — and this predicate now routes install
    # convergence (nexus-gu9zo), so that miss sent a packaged box the
    # dev-checkout refusal.
    from nexus.install_layout import is_under_uv_tool_install  # noqa: PLC0415 — deferred, avoids an import cycle

    return is_under_uv_tool_install(root)


def generation_of(venv_root: Path) -> Path | None:
    """*venv_root* itself when it is a generation, else ``None``.

    The single predicate behind two questions that legitimately differ in
    WHICH path they ask about: :func:`running_from_tool_install` asks about
    the venv the running DISTRIBUTION resolves from, while
    :func:`running_generation` asks about ``sys.prefix``. Under a
    shim-launched generation those coincide; they are still different facts,
    and nexus-utpuw.10 has a recorded reason for its choice (a dev-checkout
    invocation must not measure production processes). So the basis stays
    with each caller and only the RULE is shared -- one vocabulary for "is
    this a generation", never two that can drift apart.

    A RECEIPT-LESS ``gen-*`` tree answers YES here, which is deliberate and
    is NOT a third definition of "generation". Contract 4 -- a ``gen-*``
    directory CONTAINING a receipt -- governs ENUMERATION, what
    :func:`install_layout.list_generations` reports and what ``gc.sh`` may
    reap, and those two must agree exactly. This asks something else: is the
    tree being RUN FROM part of the side-by-side layout. It is, whether or
    not its build finished writing the receipt, and answering "no" would
    fail CLOSED -- the silent no-op shape nexus-utpuw.10 exists to close.

    Never raises: an unreadable layout answers ``None`` and the caller falls
    through to whatever it does for a non-generation.
    """
    try:
        from nexus import install_layout  # noqa: PLC0415 — deferred, avoids an import cycle

        if venv_root.parent == install_layout.tools_dir() and venv_root.name.startswith(
            install_layout.GENERATION_PREFIX
        ):
            return venv_root
    except Exception:  # noqa: BLE001 — layout unreadable: not a generation we can name
        pass
    return None


def running_generation() -> Path | None:
    """The generation THIS interpreter runs from, or ``None``.

    ``sys.prefix``-based, matching the nexus-utpuw.9 staleness contract
    (``stale <=> sys.prefix != readlink(current)``) so the tripwire and the
    comparison it feeds cannot disagree about what "this generation" means.
    """
    return generation_of(Path(sys.prefix))


#: Design point 6: the spawn tripwire logs at most once per process.
#: Tests reset it with ``monkeypatch.setattr(upgrade_finish,
#: "_TRIPWIRE_FIRED", False)``.
_TRIPWIRE_FIRED = False


def _tripwire_log(**kw: object) -> None:
    """Seam for tests; emits the spawn-time generation-skew line."""
    _log.info("spawn_generation_skew", **kw)


def spawn_tripwire() -> None:
    """nexus-utpuw design point 6: log (NEVER fail) when this spawn is not
    running the current generation. One readlink at startup.

    WHAT IT CATCHES, because a reader who works this out later will
    otherwise delete it as dead code: a shim-launched entry point readlinks
    ``current`` and execs ``<gen>/bin/<cmd>``, so ``sys.prefix == current``
    and this is silent BY CONSTRUCTION. It fires exactly when something
    BYPASSED the shim -- a PATH entry pointing straight into a generation, a
    stale wrapper, an absolute generation path baked into a launchd plist or
    a hook config. That is the nexus-q3xrx leak shape, and nothing else on
    this box reports it.

    Long-lived hosts are the other half of design point 6 and are NOT this
    function's job: they start fresh and go stale later, which the per-call
    MCP hook catches (:mod:`nexus.mcp._stale_host`).

    The line is INFORMATIONAL, per the nexus-utpuw acceptance criterion: the
    bound tree is intact and serving coherent code, so nothing here is
    breakage and saying otherwise would contradict the zero-flag promise the
    whole arc is built on.

    It deliberately does NOT promise convergence. Design point 6's phrasing
    ("converges at next spawn") is true of a LONG-LIVED holder that went
    stale under a flip -- which is the MCP hook's case, not this one. Here
    the process has only just bound, so the two causes are a flip landing
    mid-startup (transient) and a launcher resolving a generation path
    outside the shim (persistent, recurs every spawn). One observation
    cannot tell them apart, so the line names both rather than asserting the
    happier one. Substantive-critic, 2026-08-26.

    "Intact" is not free-standing either: it holds because GC never reaps a
    generation with a live holder (nexus-utpuw.5/.6, and the census fence
    re-proved by execution at the top of this session). If that fence ever
    breaks, this line becomes a lie before anything else does.

    Absorbs everything, including a failing emit: a spawn is never failed by
    its own tripwire. The once-flag is set AFTER a successful emit, so a
    transient logging failure cannot silently consume the only notice.
    """
    global _TRIPWIRE_FIRED
    if _TRIPWIRE_FIRED:
        return
    try:
        from nexus import install_layout  # noqa: PLC0415 — deferred, avoids an import cycle

        generation = running_generation()
        if generation is None:
            return  # a checkout, or the legacy tree: not this rule's business
        if not install_layout.is_stale(generation):
            return
        current = install_layout.current_generation()
        _tripwire_log(
            running=str(generation),
            current=str(current),
            detail=(
                f"this process is bound to generation {generation.name}; "
                f"current is {current.name}. Its tree is intact, so it is "
                f"running coherent code. Two causes produce this and they "
                f"need different responses: a flip that landed during this "
                f"process's startup is transient and the next spawn binds to "
                f"current, while a launcher that resolves a generation path "
                f"directly instead of going through the shim will reproduce "
                f"it on every spawn until that launcher is fixed."
            ),
        )
        _TRIPWIRE_FIRED = True
    except Exception:  # noqa: BLE001 — a spawn is never failed by its own tripwire
        return

#: Filename of the version stamp inside the nexus config dir.
STAMP_FILENAME = "last_seen_version"

#: The argv token by which an invocation declares itself a PREVIEW.
PREVIEW_ARGV_FLAG = "--dry-run"


def invocation_is_preview(argv: list[str] | None = None) -> bool:
    """True when THIS process was invoked as a preview (``--dry-run``).

    nexus-8eaeg. ``--dry-run`` is a SUBCOMMAND flag, so the root ``nx`` group
    — where the finish-the-upgrade trigger fires (``nexus/cli.py``) — cannot
    see it through Click: the group callback runs before the subcommand is
    parsed. The finish pass therefore ran WET under ``nx upgrade --dry-run``,
    and its engine leg opened a ~190 MB release-asset download that a dry-run
    then threw away (fetch-and-discard, 4-5 minutes, nothing persisted).

    Reading argv directly is the honest fix: "--dry-run promises this process
    mutates nothing" is a PROCESS-wide promise, and the process's own argv is
    where that promise is stated. It is deliberately a bare token test — no
    subcommand allow-list — because a preview of ANY command is still a
    caller saying "do not change my machine on this invocation", and the
    cost of being wrong is one deferred finish pass (the version stamp is NOT
    consumed in preview mode, so the next ordinary invocation still finishes
    the job), never a missed convergence.

    MCP hosts (``nexus.mcp.core`` / ``nexus.mcp.catalog``) call the same
    trigger with a server argv that carries no ``--dry-run``, so they are
    unaffected.
    """
    return PREVIEW_ARGV_FLAG in (sys.argv[1:] if argv is None else argv)


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
    """What KIND of process this is, from its argv structure.

    This decides who gets a SIGTERM, so a false positive is not cosmetic. It
    used to ask whether a word appeared ANYWHERE in the command line, which made
    ``nx index /papers/mineru-benchmarks/`` a mineru daemon and
    ``nx search aspect-worker`` an aspect-worker. Worse, the aspect-worker
    TOCTOU re-verify re-checks the SAME predicate, so a misclassified process
    passes the one check placed there to catch exactly this -- and the mineru
    branch has no pid re-verify at all before running a 300s stop/start.

    Structural instead: the EXECUTABLE decides, and for `nx` the verb sequence
    decides. An argument is not a daemon.
    """
    parts = command.split()
    if not parts:
        return "other"

    exe = os.path.basename(parts[0])
    rest = parts[1:]
    # A shebang-wrapped entry point: the kernel rewrites argv to
    # [python, script, ...], so the SCRIPT is the real executable.
    if exe.startswith("python") and rest:
        exe = os.path.basename(rest[0])
        rest = rest[1:]

    if exe in ("mineru", "mineru-api"):
        return "mineru"
    if exe in ("nx-mcp", "nx-mcp-catalog"):
        return "mcp-host"
    # The engine ships as a native binary and as a jar; both name themselves.
    if exe.startswith("nexus-service") or any(
        os.path.basename(tok).startswith("nexus-service") for tok in rest
    ):
        return "service"
    if exe == "nx":
        if rest[:2] == ["daemon", "aspect-worker"]:
            return "aspect-worker"
        if rest[:2] == ["daemon", "service"]:
            return "service"
    return "other"


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


#: Process-table primitives (``PROCFS_ROOT``, ``_procfs_available``,
#: ``_procfs_enumerate``, ``_ps_enumerate``, ``_parse_ps_table``,
#: ``all_process_rows``, ``process_command``, ``pid_alive``,
#: ``terminate_pids``) moved to ``nexus.daemon.service_registry``
#: (nexus-oyo2g) — the RDR-149 shared primitive. The ones this module
#: still calls directly are imported above; ``stop_storage_service`` needed
#: the same mechanism this module already carried for the identical gap
#: (nexus-cfgo9), and the standing gate (daemon/AGENTS.md) is that a
#: lifecycle mechanism used by more than one tier/module lives in the
#: primitive, not duplicated.


def _process_markers() -> tuple[str, ...]:
    """Every substring that marks a command as belonging to a conexus venv.

    ONE definition, because two of them drifted and the drift was silent.
    nexus-utpuw.10 rewired :func:`enumerate_processes` onto the layout-derived
    markers and left :func:`restart_stale`'s pre-kill re-check on the hardcoded
    :data:`_PROC_MARKERS` -- which .10's own audit (finding F5) had called out
    as a SEPARATE must-fix item with its own test. The result on a migrated box:
    every stale aspect-worker was enumerated, reported by ``nx doctor``, and
    then skipped as "gone or recycled" at the instant of signalling, so both
    ``nx daemon restart-stale`` and the automatic finish pass silently restarted
    nothing (nexus-mjhwk). That is the exact silent-no-op class this arc exists
    to close, surviving in the one call site that does the actual work.

    Order matters and is not arbitrary. The layout-derived markers come first
    because on a generation box they are the only ones that can match; the
    running install's venv root is the pre-generation box's answer; and
    :data:`_PROC_MARKERS` is the last resort for a box whose metadata will not
    resolve at all. Returning empty is deliberately NOT possible -- an empty
    marker set makes ``any(...)`` False for every row, which reads as "nothing
    is ours" and is the under-reporting direction.
    """
    from nexus import install_census  # noqa: PLC0415 — deferred, avoids an import cycle

    markers: tuple[str, ...] = install_census.generation_match_prefixes()
    if markers:
        return markers
    try:
        # No generation layout readable: the venv root of the running install,
        # which is what a pre-generation box carries.
        return (str(_install_root().parents[2]),)
    except Exception:  # noqa: BLE001 — metadata unavailable: conventional layout
        return _PROC_MARKERS


def enumerate_processes(ps_output: str | None = None) -> list[tuple[int, int, str]]:
    """``[(pid, age_s, command)]`` for every running conexus-VENV process.

    Source resolution (ps, else /proc, else raise) lives in
    :func:`all_process_rows`; this adds only the venv-marker filter that
    :func:`detect_stale_processes` wants. ``ps_output`` is injectable for
    tests.
    """
    rows = all_process_rows(ps_output)
    me = os.getpid()
    # EVERY GENERATION, not the current one. A stale process runs from a
    # generation that is NOT current -- that is what makes it stale -- so a
    # filter pinned to the current install excludes precisely the processes
    # this pass exists to find, and report.stale is empty by construction.
    #
    # Not a typo but a design inversion: the old layout kept the path CONSTANT
    # across upgrades (in-place swap), so mtime was the only discriminator.
    # Generations make the path itself the version.
    #
    # The prefixes come from install_census so there is ONE definition of what
    # marks a holder, shared with the shell half and pinned by
    # tests/test_install_census_twins_agree.py. Giving this function its own
    # notion is how the markers it used to carry drifted into matching nothing.
    markers = _process_markers()
    return [
        (pid, age, command)
        for pid, age, command in rows
        if pid != me and any(k in command for k in markers)
    ]


def _current_generation() -> Path | None:
    """``<tools>/current``'s target, or ``None`` when the layout cannot say."""
    from nexus import install_layout  # noqa: PLC0415 — deferred, avoids an import cycle

    try:
        return install_layout.current_generation()
    except Exception:  # noqa: BLE001 — no resolvable pointer: no identity verdict
        return None


def detect_stale_processes(
    ps_output: str | None = None,
    *,
    now: float | None = None,
) -> SkewReport:
    """Every conexus process executing code older than the install.

    TWO REGIMES, PER ROW, and they answer with different evidence
    (nexus-ycw67). nexus-utpuw.10 moved the ENUMERATION half onto the layout;
    the VERDICT half stayed on ``started < install_mtime``, which under
    side-by-side generations is not merely imprecise but WRONG IN A DIRECTION:
    a process bound to ``gen-00`` and STARTED AFTER ``gen-01`` was installed
    reads FRESH, because its start time is newer than the current generation's
    dist-info mtime. That is precisely the shim-bypass shape -- a stale
    wrapper, a PATH entry into a generation, an absolute generation path in a
    launchd plist (the nexus-q3xrx class) -- so the one check meant to catch
    it was blind to it.

    * A row that ATTRIBUTES to a generation is judged by IDENTITY, which is
      the arc's own contract (nexus-utpuw.9 / :func:`install_layout.is_stale`,
      ``stale <=> prefix != readlink(current)``). Exact: no clock inference,
      no false positives or negatives. A holder of the legacy uv tree
      attributes here too -- .7 registers that tree as a ``gen-*`` pointer --
      and correctly reads stale on a migrated box.
    * Anything else keeps the AGE heuristic: an un-migrated box, where
      in-place replacement really does happen and mtime is the only
      discriminator there has ever been. .7 leaves boxes in that state until
      their legacy tree has zero holders, so this branch is live in the field
      and is not a fallback for the paranoid.

    ONE ``readlink`` FOR THE WHOLE SCAN, hoisted out of the loop deliberately
    rather than for speed: per-row resolution could straddle a flip and
    produce a report whose rows disagree about what ``current`` is, which is a
    state the machine never occupied at any instant. One snapshot of the
    pointer is the honest basis for one snapshot of the process table.

    WIDENING NOTE: :func:`restart_stale` SIGTERMs what this reports, so the
    identity regime also widens what gets cycled -- to processes that are
    genuinely running old code and were previously missed. Only the
    restartable classes are cycled; session-bound ones are still reported for
    a human.
    """
    from nexus import install_census  # noqa: PLC0415 — deferred, avoids an import cycle

    mtime, version = install_mtime_and_version()
    report = SkewReport(installed_version=version, install_mtime=mtime)
    now = time.time() if now is None else now

    pairs = install_census.generation_match_pairs()
    current = _current_generation()

    for pid, age_s, command in enumerate_processes(ps_output):
        generation = next(
            (gen for marker, gen in pairs if marker in command), None
        )
        if generation is not None and current is not None:
            stale = generation != current
        else:
            stale = (now - age_s) < mtime
        if stale:
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
                current = process_command(proc.pid)
                if "aspect-worker" not in current or not any(
                    k in current for k in _process_markers()
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


def _current_generation_receipt() -> "install_layout.Receipt | None":
    """The CURRENT generation's receipt, or ``None`` when there is none to read.

    CURRENT rather than the RUNNING generation, and the difference is not
    academic: ``perform_self_install`` reproduces THIS process's install and so
    reads the running one, while this string answers the forward-looking
    question ("why did my upgrade not move?"). The next ``nx`` a user types
    resolves through the shim to ``current``, so ``current`` is the tree whose
    source governs what that invocation will do.

    Never raises. A dangling pointer, an absent layout, a receipt written by a
    schema this nx does not read -- all of them mean "no generation answer
    available here", and the uv receipt is the better of two imperfect answers.
    """
    from nexus import install_layout  # noqa: PLC0415 — deferred, avoids an import cycle

    try:
        return install_layout.read_receipt(install_layout.current_generation())
    except Exception:  # noqa: BLE001 — no readable generation receipt: fall back to uv
        return None


def install_source() -> str:
    """Human-readable install source: directory / pinned / PyPI.

    Explains why an upgrade may report "Nothing to upgrade": a
    directory-tracking install never consults an index, and an ==-pinned uv
    one never moves past its pin (both live incidents, 2026-07-13). Rendered
    by ``nx doctor``'s Process-freshness row and by ``nx daemon
    finish-upgrade``.

    TWO LAYOUTS, AND THE GENERATION WINS (nexus-0za6e). This read the uv
    receipt and nothing else, which is wrong twice under the generation
    layout. During the migration window .7 leaves the legacy tree in place
    until it has zero holders, so the uv receipt stays READABLE and a
    directory-tracking legacy install migrated onto a PyPI generation reported
    the legacy source -- a confident wrong answer on the one string whose job
    is explaining an upgrade that did or did not move. After the legacy tree is
    reaped it said "unknown" forever. The right answer was already on disk and
    unused: the receipt the builder writes for every generation.

    THE VOCABULARY IS NOT MIRRORED ACROSS THE BRANCHES, deliberately (contract
    12). "``uv tool upgrade`` will never move past the pin" is TRUE on a uv
    tree and FALSE under generations: ``perform_self_install`` passes
    ``--source`` and ``--extras`` and OMITS the version, so a generation built
    with ``--version X`` upgrades normally at the next ``nx self install``
    (verified by execution, 2026-08-26). ``health.py`` renders only the part
    before the em-dash, so a summary that claimed stickiness would assert it
    standing alone.

    FORMAT: ``<summary> — <explanation>``. ``health.py`` splits on the
    separator to render the summary alone; keep both halves true in isolation.
    """
    receipt = _current_generation_receipt()
    if receipt is not None:
        from nexus.install_advice import (  # noqa: PLC0415 — deferred, avoids an import cycle
            GENERATION_INSTALLER,
        )

        if receipt.source_kind == "directory":
            return (
                f"local checkout ({receipt.source}) — `{GENERATION_INSTALLER}` "
                f"rebuilds from that checkout, so a new PyPI release will not "
                f"move it"
            )
        if receipt.version:
            return (
                f"PyPI, built with --version {receipt.version} — that pin was "
                f"one-shot; `{GENERATION_INSTALLER}` resolves the current release"
            )
        return f"PyPI — `{GENERATION_INSTALLER}` upgrades normally"

    return _uv_install_source()


def _uv_install_source() -> str:
    """The legacy uv-tree answer, in uv's own vocabulary.

    Kept verbatim rather than paraphrased: a box that has not migrated really
    does upgrade through uv, and .7 leaves boxes in that state until their
    legacy tree has zero holders. Telling such a user to run the generation
    installer is a different wrong answer (contract 12).

    The REMEDIATION inside it is a separate question from the DESCRIPTION
    around it, and the two are answered by different classifiers on purpose.
    Reaching this function means no generation RECEIPT was readable; it does
    NOT mean the box has no generation layout -- a resolvable ``current`` whose
    receipt is corrupt lands here. On that box ``uv tool install --reinstall
    conexus`` would rebuild the uv tree and re-symlink over the nexus-owned
    shims (nexus-utpuw.7's accepted risk), so the commands go through
    ``install_advice``, which asks the layout rather than the receipt. On a
    genuinely un-migrated box every one of them returns its uv form unchanged.
    """
    import tomllib  # noqa: PLC0415 — stdlib, deferred for startup cost

    from nexus import install_advice  # noqa: PLC0415 — deferred, avoids an import cycle

    # nexus-orhp5: was a hardcoded ~/.local/share/uv/tools/... — the
    # FOURTH resolution rule, and the only one wrong for BOTH
    # UV_TOOL_DIR and XDG_DATA_HOME.
    from nexus.install_layout import uv_conexus_venv  # noqa: PLC0415 — deferred, avoids an import cycle

    receipt = uv_conexus_venv() / "uv-receipt.toml"
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
            f"consults PyPI for this install; use "
            f"{install_advice.upgrade_command('scripts/reinstall-tool.sh')} "
            f"or reinstall from PyPI"
        )
    spec = str(req.get("specifier", ""))
    if spec.startswith("=="):
        reinstall = install_advice.upgrade_command("uv tool install --reinstall conexus")
        return (
            f"PyPI, PINNED ({spec}) — `uv tool upgrade` will never move "
            f"past the pin; reinstall unpinned "
            f"(`{reinstall}`)"
        )
    upgrade = install_advice.upgrade_command("uv tool upgrade conexus")
    return f"PyPI, unpinned — `{upgrade}` upgrades normally"


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


#: Full clickable https URL, pinned to ``main`` (releases promote develop ->
#: main, so an operator on a released build finds the section on main).
#: Relocated from the deleted ``nexus.remediation`` package (nexus-lgdel):
#: this is now the sole real consumer, via :class:`_ChashPoisonGuidance`.
MIGRATION_RUNBOOK_URL = (
    "https://github.com/Hellblazer/nexus/blob/main/docs/migration-runbook.md"
)

_PASTE_RULE = "  " + "-" * 64


@dataclass(frozen=True)
class _ChashPoisonGuidance:
    """Plain-text chash-poison gate guidance for ``PoisonProbe.playbook``.

    Replaces the RDR-182 ``Playbook`` DSL (deleted at nexus-lgdel — the
    chash-rekey upgrade rung every prior rendering steered operators
    toward no longer exists, and the RDR-182 guided-remediation MCP
    surface + CLI ``nx remediate`` are deleted with it). This carries only
    the two renderings the two REAL, independent safety gates still need
    (:func:`converge_engine`'s block and ``nx daemon service
    install-binary``'s refuse/force-override, nexus-pnwu0 / GH #1390) —
    remedy corrected to re-indexing (the mechanism that actually
    recomputes conformant ids today), no ordered-step/consent machinery,
    because none remains to guide.
    """

    store_detail: str

    def _agent_prompt(self) -> str:
        return (
            "My conexus/nexus store has width-non-conformant chash rows in "
            "pgvector (octet_length <> 32 — legacy pre-RDR-108 ids; the "
            "GH #1414 class). Resolve the affected collections to their "
            "repos (`nx catalog owners`) and re-index the file-backed ones "
            "(`nx index repo <path>` — additive, per-collection), then "
            "re-run `nx doctor` and confirm the 'Chunk chash conformance' "
            "warning has cleared. Do NOT drop the chash length constraints."
        )

    def terminal_block(self) -> str:
        """The CLI refusal body."""
        return (
            "\nRefusing to install (nexus-pnwu0 / GH #1414): this store has "
            "width-non-conformant chash rows — heal them by re-indexing "
            "before swapping engine binaries.\n"
            f"  {self.store_detail}\n\n"
            "Remediate first — full recovery runbook (clickable):\n"
            f"  {MIGRATION_RUNBOOK_URL}\n\n"
            "Or paste this to your Claude to be walked through it:\n"
            f"{_PASTE_RULE}\n"
            f"  {self._agent_prompt()}\n"
            f"{_PASTE_RULE}\n\n"
            "Do NOT drop the chash length constraints to force it through — "
            "that is the exact action that caused GH #1390. Re-run with "
            "--force ONLY after you have remediated."
        )

    def force_override_warning(self) -> str:
        """The one-line warning when the operator overrides the gate."""
        return (
            "WARNING (nexus-pnwu0 / GH #1414): --force overrides the "
            f"chash-poison gate. {self.store_detail} The rows stay unhealed "
            "debt until you re-index the affected collections, and a "
            "pre-v0.1.48 char-era engine can still crash-loop on boot. "
            f"Recovery: {MIGRATION_RUNBOOK_URL}."
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
    - POISONED: ``playbook`` is set to a :class:`_ChashPoisonGuidance`
      (render its ``terminal_block()``).
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

        # GH #1414 era-hop regression (2026-07-21): a pre-P2.1 install has
        # no nexus_diag credentials, so the probe could never run there and
        # every classification read UNKNOWN — permanently deferring engine
        # convergence on exactly the unattended-upgrade boxes RDR-185
        # serves, with `nx doctor` (the advertised re-attempt) failing the
        # same way. Self-heal first: the idempotent RDR-182 P2.1 backfill,
        # resolution-gated on the live local-cluster facts inside the
        # wrapper. Best-effort — a box the backfill cannot help still
        # classifies UNKNOWN below, defer semantics untouched.
        import nexus.db.diag_connection as _diag_conn  # noqa: PLC0415 — deferred, circular-dep avoidance

        if _diag_conn.resolve_diag_credentials(creds_path) is None:
            import nexus.db.pg_provision as _pgp  # noqa: PLC0415 — deferred, circular-dep avoidance

            _pgp.backfill_diag_role_best_effort()

        results = _check_migration_state(creds_path=creds_path)
    except Exception as exc:  # noqa: BLE001 — an unverifiable store is UNKNOWN, never a crash and never a silent clean
        return PoisonProbe(unknown_reason=f"{type(exc).__name__}: {exc}"[:200])

    poison = [
        r for r in results
        if r.label == CHASH_CONFORMANCE_LABEL
        and not r.ok and POISON_DETAIL_TOKEN in r.detail
    ]
    if poison:
        return PoisonProbe(
            playbook=_ChashPoisonGuidance(store_detail=poison[0].detail),
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


@dataclass(frozen=True)
class RunningEngine:
    """What the LIVE service reports — as opposed to what is on disk.

    RDR-185 Gap-4 (closed, implemented) pins the property that "am I
    converged?" is answered from the LIVE world every time, and names the
    banned form: a freestanding remembered verdict — "a cache file, a second
    table, a marker". :func:`detect_engine_convergence` answers from the
    provenance sidecar, which is a cache file. That is the RIGHT answer to
    "what is on DISK" (and it stays available when the service is down — the
    crash-looping-engine case it was deliberately chosen for), but it is the
    WRONG sole answer to "is the running system converged". This type is the
    other half.

    The Gap-4 pin never caught this because it AST-scans ladder RUNGS, and
    restart-stale's engine leg is not a rung — it is the bespoke mechanism
    outside the ladder (nexus-4yf4u, GH #1419 Issue 1).

    - ``up`` — a storage-service lease is discoverable (the canonical
      resolver, the same path every downstream consumer resolves through).
    - ``version`` — the ``release_version`` the RUNNING service reports, or
      ``None`` when the service is up but cannot be asked.
    - ``reason`` — why ``version`` is ``None``, for the operator-facing line.
    """

    up: bool
    version: tuple[int, int, int] | None
    reason: str | None = None


def _running_engine(config_dir: Path) -> RunningEngine:
    """Observe the live engine: is a service discoverable, and at what version?

    Never raises. Any probe defect degrades to ``up=False`` — the
    CONSERVATIVE reading: it preserves the ordinary deferral for a box whose
    service simply is not running, and never manufactures a NEEDS HUMAN out
    of our own probe failing.
    """
    try:
        from nexus.db import service_endpoint  # noqa: PLC0415 — deferred, heavy dep

        base_url, _token = service_endpoint.discover_lease()
    except Exception as exc:  # noqa: BLE001 — a probe defect is never a verdict
        return RunningEngine(
            up=False, version=None,
            reason=f"lease discovery failed ({type(exc).__name__}: {exc})"[:200],
        )
    if not base_url:
        return RunningEngine(
            up=False, version=None,
            reason="no discoverable storage-service lease",
        )

    # One probe, one parser: fetch_service_version returns the /version
    # handshake dict, parse_engine_version is the SAME parser the ladder's
    # verify_service_version pins on. (verify_service_version itself is a
    # fail-closed >= BOOLEAN and does not surface the observed number; the
    # operator-facing lines below need the actual running version, and
    # text-parsing its reason string would be fragile.)
    try:
        from urllib.parse import urlsplit  # noqa: PLC0415 — stdlib, deferred

        from nexus.daemon.binary_lifecycle import fetch_service_version  # noqa: PLC0415 — deferred, CLI startup cost

        parts = urlsplit(base_url)
        payload = fetch_service_version(
            parts.hostname or "127.0.0.1",
            parts.port,
            scheme=parts.scheme or "http",
        )
    except Exception as exc:  # noqa: BLE001 — up, but unaskable
        return RunningEngine(
            up=True, version=None,
            reason=f"/version probe raised {type(exc).__name__}: {exc}"[:200],
        )
    if not payload:
        return RunningEngine(
            up=True, version=None,
            reason=f"{base_url}/version did not answer",
        )
    raw = payload.get("release_version")
    parsed = parse_engine_version(raw if isinstance(raw, str) else None)
    if parsed is None:
        return RunningEngine(
            up=True, version=None,
            reason=f"{base_url}/version reported no usable release_version ({raw!r})",
        )
    return RunningEngine(up=True, version=parsed)


def service_stack_pids(config_dir: Path) -> list[tuple[int, str]]:
    """``[(pid, command)]`` for the storage-service SUPERVISOR and ENGINE
    processes belonging to *config_dir*, read from the OS process table.

    nexus-cfgo9 follow-up. ``stop_storage_service`` decides what to signal
    from the LEASE, and concludes "already stopped" when no live lease is
    discoverable (``storage_service_daemon.py``, the ``no_live_lease``
    arm). A lease is a DISCOVERY record on a TTL, not a liveness oracle: a
    supervisor whose heartbeat has stalled past the 15s TTL is alive and
    serving while being completely invisible to that check. The process
    table is the ground truth, and it is now readable without ``ps``.

    Identification is by argv, which is exact for both members of the
    stack: the supervisor is spawned as ``nx daemon service start
    --foreground --config-dir <config_dir>`` (``ensure_storage_supervisor``
    always passes the flag), and the engine's argv[0] IS the well-known
    binary path under *config_dir*. Never matches this process, and never
    matches ``nx daemon restart-stale`` itself.

    THE SUPERVISOR MATCH IS TOKEN-EXACT ON THE --config-dir ARGUMENT, not
    a substring test (review Critical, 2026-08-01): ``--config-dir`` is
    the documented multi-profile mechanism, and a bare
    ``str(config_dir) in command`` matches ``.config/nexus`` against
    ``.config/nexus-staging``'s command line — folding a HEALTHY sibling
    profile's supervisor into the sweep's kill set. A wrong-kill of an
    unrelated profile is precisely the failure class this function exists
    to eliminate for the matching one. (The engine match has no such
    exposure: the ``/service/nexus-service`` suffix forces a
    path-structure match.)

    The matcher itself (nexus-oyo2g) is now the shared
    ``service_registry.storage_service_stack_matcher`` — the same predicate
    ``stop_storage_service``'s lease-miss fallback uses — so there is one
    definition of "this pid belongs to this config_dir's storage-service
    stack", not two.
    """
    matcher = storage_service_stack_matcher(config_dir)
    me = os.getpid()
    return [
        (pid, command)
        for pid, _age, command in all_process_rows()
        if pid != me and matcher(command)
    ]


def _sweep_surviving_stack(
    config_dir: Path, before: list[tuple[int, str]],
) -> str:
    """Kill any pre-stop stack member that survived ``nx daemon service stop``.

    THE FIX for the nexus-cfgo9 convergence defect. ``stop`` reports success
    having signalled nothing whenever the old supervisor's lease has aged
    out (a stalled heartbeat under container/host load), and ``start`` is
    BY DESIGN a no-op whenever a live lease exists
    (``ensure_storage_supervisor``'s ``return existing``; ``_start_locked``'s
    short-circuit). Composing those two verbs therefore does not restart
    anything reliably — whether the engine is cycled comes down to whether
    the stalled supervisor re-stamps its lease in the window between the two
    calls. Observed BOTH ways on the same scenario: race lost, the old
    v0.1.52 engine kept serving and convergence reported a mystified NEEDS
    HUMAN; race won, a SECOND supervisor+engine stack was spawned alongside
    the surviving one, two engines against one Postgres.

    Verifying the stop against the process table removes the race from the
    convergence path: after this returns, nothing from the pre-stop stack is
    running, so ``start`` cannot short-circuit onto it.
    """
    # Pid-recycle TOCTOU guard (the convention review 38b7db3d High-3 set for
    # restart_stale): a pid that died in the stop and was immediately reused
    # by an unrelated process must never be signalled. Re-read each survivor's
    # CURRENT argv and require it to still be the process we recorded.
    survivors: list[tuple[int, str]] = []
    for pid, cmd in before:
        if not _pid_alive(pid):
            continue
        # A ZOMBIE is not a survivor (nexus-o8dil.21). ``_pid_alive`` is
        # ``os.kill(pid, 0)``, which succeeds for a terminated-but-unreaped
        # process: the stop DID kill it, and its parent simply has not
        # collected the exit status yet. Orphans of a PID 1 that is not a
        # real init (the MVV container's PID 1 is a shell script; CI
        # runners are the same shape) stay in that state indefinitely, so
        # this sweep re-signalled corpses and then reported the successful
        # stop as having "left pid(s) running". The zombie check is kept
        # SEPARATE from ``_pid_alive`` deliberately: that call keeps its
        # alive-on-ambiguous-errno semantics (see the import comment), and
        # only a POSITIVE ``Z`` reading — never an unknown one — excludes
        # a pid here.
        if process_state(pid) == "Z":
            _log.info("restart_stop_sweep_pid_already_dead", pid=pid, state="Z")
            continue
        current = process_command(pid)
        # An unreadable argv (permissions, a zombie mid-reap) is not evidence
        # of a recycle; fall back to the recorded command rather than skipping
        # a genuine survivor.
        if current and current.split() != cmd.split():
            _log.info(
                "restart_stop_sweep_pid_recycled", pid=pid,
                recorded=cmd[:120], current=current[:120],
            )
            continue
        survivors.append((pid, cmd))
    if not survivors:
        return ""
    engine_path = str(config_dir / "service" / "nexus-service")
    supervisors = [p for p, c in survivors if engine_path not in c]
    engines = [p for p, c in survivors if engine_path in c]
    stubborn = terminate_pids(supervisors)
    stubborn += terminate_pids(engines)
    listed = ", ".join(str(p) for p, _ in survivors)
    # Report what was OBSERVED, never a cause that was not (nexus-o8dil.21).
    # Both strings used to assert "(its lease-based check saw no live
    # lease)" unconditionally — a hardcoded diagnosis this function never
    # makes: it compares a pre-stop process snapshot against the process
    # table and has no visibility into the stop's lease outcome at all. In
    # the 2026-08-14 package-upgrade MVV that invented cause sent the
    # investigation after a cross-version lease-discovery gap that did not
    # exist (the stop's own output, "Storage service stopped (pid(s)=...)",
    # is emitted ONLY when the lease WAS discovered).
    if stubborn:
        return (
            f"[stop-sweep] pid(s) {listed} were still running after "
            f"`nx daemon service stop` returned; pid(s) "
            f"{', '.join(str(p) for p in stubborn)} were STILL running after "
            "a direct SIGKILL escalation (not merely awaiting reap)"
        )
    return (
        f"[stop-sweep] pid(s) {listed} were still running after "
        "`nx daemon service stop` returned — terminated them directly so the "
        "restart cycles the engine instead of short-circuiting"
    )


def _restart_and_verify(
    config_dir: Path,
    actions: list[str],
    req_s: str,
    *,
    settle_s: float = 20.0,
    poll_s: float = 1.0,
) -> list[str]:
    """Cycle the storage service, then OBSERVE whether it came up converged.

    nexus-4yf4u: the predecessor claimed "restarted the storage service to
    pick up the converged engine" whenever ``stop`` and ``start`` both exited
    0. A returncode proves the commands ran, not that the service came up on
    the new engine — the same fail-quiet class as the deferral loop above,
    one layer down. Convergence is now claimed only when the running service
    reports the required version.

    nexus-cfgo9 follow-up: the cycle no longer TRUSTS ``stop``. Both verbs
    can legitimately no-op — ``stop`` when it cannot discover a live lease,
    ``start`` when it can — so composing them restarts nothing whenever the
    old supervisor's heartbeat has stalled past its lease TTL. The pre-stop
    stack is now snapshotted from the process table and swept after the stop
    (:func:`_sweep_surviving_stack`), which is what makes the subsequent
    start a real spawn rather than a short-circuit.
    """
    try:
        before = service_stack_pids(config_dir)
    except Exception as exc:  # noqa: BLE001 — no process table: degrade to the old choreography
        _log.warning("restart_stack_snapshot_failed", error=str(exc))
        before = []
    try:
        stop = subprocess.run(
            ["nx", "daemon", "service", "stop"],
            capture_output=True, text=True, timeout=60,
        )
        try:
            sweep_note = _sweep_surviving_stack(config_dir, before)
        except Exception as exc:  # noqa: BLE001 — the sweep is belt, never the reason start doesn't run (review M2)
            _log.warning("restart_stack_sweep_failed", error=str(exc))
            sweep_note = f"(stack sweep failed: {exc} — proceeding to start)"
        start = subprocess.run(
            ["nx", "daemon", "service", "start"],
            capture_output=True, text=True, timeout=120,
        )
    except Exception as exc:  # noqa: BLE001 — best-effort cycle; surfaced in the line
        actions.append(
            f"NEEDS HUMAN: restarting the storage service raised {exc} — run "
            "`nx daemon service stop && nx daemon service start` yourself, "
            "then `nx doctor` to confirm the engine converged"
        )
        return actions
    cycle_transcript = _cycle_transcript(stop, start)
    if sweep_note:
        cycle_transcript = f"{sweep_note} {cycle_transcript}"
    if stop.returncode != 0 or start.returncode != 0:
        actions.append(
            "NEEDS HUMAN: the service restart did not report success — run "
            "`nx daemon service stop && nx daemon service start` yourself, "
            "then `nx doctor` to confirm the engine converged. "
            + cycle_transcript
        )
        return actions

    # A freshly started service does not publish its lease instantly, so an
    # INSTANT verdict here would emit a NEEDS HUMAN on every healthy
    # convergence that we merely probed too early. False alarms are how real
    # alarms stop being read — bound the wait instead.
    after = _running_engine(config_dir)
    if after.version is None:
        # Bounded by BOTH an attempt count and a wall-clock deadline. The
        # attempt cap is what keeps this deterministic and instant under a
        # patched `time.sleep` (a purely time-bounded loop busy-spins for the
        # entire budget in tests, and its iteration count then varies with
        # machine speed); the deadline is what still bounds it in production,
        # where each probe carries its own timeout on top of the sleep.
        deadline = time.time() + settle_s
        for _ in range(max(1, int(settle_s / poll_s) + 1)):
            if time.time() >= deadline:
                break
            time.sleep(poll_s)
            after = _running_engine(config_dir)
            if after.version is not None:
                break

    if after.version is not None and after.version == REQUIRED_ENGINE_VERSION:
        # The sweep note rides the SUCCESS line too: an incomplete `stop` is
        # a real event even when the cycle then went on to converge, and
        # silence here would hide the very condition that used to make this
        # a coin flip.
        actions.append(
            f"restarted the storage service — verified running v{req_s}"
            + (f" ({sweep_note})" if sweep_note else "")
        )
        return actions
    if after.version is not None:
        got_run = ".".join(str(p) for p in after.version)
        actions.append(
            "NEEDS HUMAN: the service restarted but is STILL running "
            f"v{got_run}, not the required v{req_s}. The restart exited "
            "cleanly, so something is holding the old engine (a supervisor "
            "that re-execs a different binary, or a second service process). "
            "Check `nx doctor` and `nx daemon service status`. "
            + cycle_transcript + " " + _holder_evidence(config_dir)
        )
        return actions
    actions.append(
        "NEEDS HUMAN: the service restarted but its version could not be "
        f"confirmed ({after.reason or 'no /version answer'}) — convergence to "
        f"v{req_s} is UNVERIFIED. Check `nx doctor`. " + cycle_transcript
    )
    return actions


def _cycle_transcript(
    stop: subprocess.CompletedProcess[str],
    start: subprocess.CompletedProcess[str],
) -> str:
    """One-line transcript of the stop/start cycle.

    The predecessor captured both commands' output and DISCARDED it, so
    every NEEDS-HUMAN line below described the outcome without a shred of
    evidence about the cycle that produced it — "already stopped" (the
    supervisor was never signalled) and "stopped (pid=N)" are opposite
    diagnoses and the operator could not tell which had happened.
    """
    def _fold(p: subprocess.CompletedProcess[str]) -> str:
        text = " ".join(
            ((p.stdout or "") + " " + (p.stderr or "")).split()
        )
        return text[:300] or "(no output)"

    return (
        f"[cycle] stop rc={stop.returncode}: {_fold(stop)} | "
        f"start rc={start.returncode}: {_fold(start)}"
    )


def _holder_evidence(config_dir: Path) -> str:
    """Name what is actually holding the old engine, instead of guessing.

    The NEEDS-HUMAN line above lists two candidate causes ("a supervisor
    that re-execs a different binary, or a second service process") and
    asks the operator to go look. The product can look: the lease says
    which endpoint answered and which supervisor published it, and
    :func:`enumerate_processes` (no longer ``ps``-dependent) says which
    conexus processes are alive. Best-effort — evidence gathering must
    never turn a diagnosis into a crash.
    """
    bits: list[str] = []
    try:
        from nexus.config import nexus_config_dir  # noqa: PLC0415 — deferred, CLI startup cost
        from nexus.daemon.service_registry import ServiceRegistry  # noqa: PLC0415 — deferred, CLI startup cost

        registry = ServiceRegistry(
            dir=config_dir or nexus_config_dir(), tier="storage_service",
        )
        record = registry.discover(str(os.getuid()))
        if record is None:
            bits.append("lease=none")
        else:
            ep = record.endpoint
            bits.append(
                f"lease=host {ep.get('host')}:{ep.get('port')} "
                f"service_pid={ep.get('pid')} "
                f"supervisor_pid={record.payload.get('supervisor_pid')} "
                f"generation={record.generation}"
            )
    except Exception as exc:  # noqa: BLE001 — evidence, never a verdict
        bits.append(f"lease=unreadable ({type(exc).__name__})")
    try:
        procs = [
            f"{pid}({age}s)" for pid, age, _cmd in enumerate_processes()
        ]
        bits.append(f"live conexus pids={', '.join(procs) or 'none'}")
    except Exception as exc:  # noqa: BLE001 — evidence, never a verdict
        bits.append(f"process table unavailable ({exc})")
    return "[holder] " + "; ".join(bits)


def converge_engine(
    config_dir: Path, *, dry_run: bool = False, unattended: bool = False,
) -> list[str]:
    """Install the release-dependency engine and cycle the service on a
    mismatch. Returns human-readable action lines.

    Empty means: not applicable, OR convergence was CONFIRMED (on-disk and
    running versions both equal the release dependency), OR the service is
    not running at all (the on-disk binary governs its next start, and a
    stopped service is an ordinary state rather than an alarm). It no longer
    means "already converged" unqualified — nexus-4yf4u: a correct on-disk
    binary with a stale, unanswerable, or NEWER live process each return a
    line now, because silence there asserted something about the running
    engine that nothing had observed.

    Never raises: a poison-gate block or an install/restart failure is
    reported as a loud ``NEEDS HUMAN`` action line, never a silent skip and
    never a crash that could leave the box worse off.

    ``unattended`` marks the caller as the automatic finish pass
    (:func:`check_version_transition`, which fires on the first ``nx``
    invocation after a version change) rather than a human running ``nx
    daemon restart-stale``. It suppresses ONLY the C1b restart-a-stale-live-
    process action: bouncing a storage service can sever a client mid-batch
    (GH #1419 Issue 3b), so that disruption is reserved for a human who
    explicitly asked for convergence. The unattended pass reports the same
    facts and names the manual verb. Hal decision, 2026-07-24.
    """
    status = detect_engine_convergence(config_dir)
    if not status.applicable:
        return []

    req_s = ".".join(str(p) for p in status.required_version)
    got_s = (
        ".".join(str(p) for p in status.installed_version)
        if status.installed_version else "unknown"
    )

    # nexus-8eaeg: "converged" was answered from the RECEIPT alone. The
    # receipt is a claim about bytes; verify it against the bytes — locally,
    # against the receipt's own recorded digest, NEVER by re-downloading the
    # asset to compare. A receipt that the on-disk binary does not back is
    # not a converged box: it is a box whose engine is missing or corrupt,
    # which must re-acquire on a WET run and be REPORTED as a planned
    # acquisition on a dry one. Only computed when the receipt otherwise
    # says converged (a version mismatch already decides the question, and
    # this costs a streaming sha256 of a ~190 MB file).
    integrity = None
    if status.converged:
        from nexus.daemon.binary_install import verify_installed_binary  # noqa: PLC0415 — deferred, CLI startup cost

        integrity = verify_installed_binary(config_dir)
        if not integrity.ok:
            _log.warning(
                "engine_receipt_unbacked",
                config_dir=str(config_dir),
                reason=integrity.reason,
            )
            got_s = f"{got_s} unverified"

    if status.converged and integrity is not None and integrity.ok:
        # nexus-4yf4u: the DISK is right — is the PROCESS? A correct on-disk
        # binary with a stale live service needs a RESTART, not a reinstall,
        # and the predecessor returned [] here ("already converged"), which
        # `nx doctor` then echoed as green while the old engine kept serving.
        running = _running_engine(config_dir)

        if running.version is not None and running.version < status.required_version:
            got_run = ".".join(str(p) for p in running.version)
            if unattended:
                return [
                    f"NOTE: the on-disk engine is v{req_s} but the RUNNING "
                    f"service is still v{got_run}. Not restarting it here — "
                    "this is the automatic post-upgrade pass, and cycling the "
                    "storage service can sever an in-flight client (GH #1419 "
                    "Issue 3b). Run `nx daemon restart-stale` when it suits "
                    "you and it will converge and verify."
                ]
            if dry_run:
                return [
                    f"would restart the storage service: the on-disk engine "
                    f"is v{req_s} but the RUNNING service reports v{got_run}"
                ]
            # nexus-v5lk3: transfer nexus.diag_chash_conformance's ownership
            # to nexus_admin BEFORE the restart that boots the on-disk
            # engine — see _reassign_diag_view_before_restart's own
            # docstring for the crash-loop this prevents.
            predrop_actions = _reassign_diag_view_before_restart(config_dir)
            return _restart_and_verify(
                config_dir,
                predrop_actions + [
                    f"on-disk engine is already v{req_s} but the RUNNING "
                    f"service reports v{got_run} — restarting to pick it up"
                ],
                req_s,
            )

        # Review CRE-A finding 1 (High): every remaining sub-case used to fall
        # into a bare `return []`, which the CLI renders as "converged". Three
        # of the four had NOT observed the running engine at all, so that
        # reassurance was unearned — the same silent-reassurance class this
        # bead exists to close, one layer down. Only a CONFIRMED equal
        # version, or a service that is not running at all (where the on-disk
        # binary governs the next start, and a stopped service is an ordinary
        # state rather than an alarm), may stay silent.
        if running.version is not None and running.version > status.required_version:
            got_run = ".".join(str(p) for p in running.version)
            return [
                f"NOTE: the RUNNING service reports v{got_run}, NEWER than the "
                f"release dependency v{req_s} on disk. Not converged, and "
                "deliberately not auto-fixed: restarting would silently "
                "DOWNGRADE the running engine. Cycle it yourself if that is "
                "what you want (`nx daemon service stop && nx daemon service "
                "start`)."
            ]
        if running.up and running.version is None:
            return [
                f"NOTE: the on-disk engine is v{req_s}, but the RUNNING "
                "service is not answering /version "
                f"({running.reason or 'no answer'}), so convergence is "
                "UNVERIFIED — this reports the disk, not the running system. "
                "Check `nx doctor` and `nx daemon service status`."
            ]
        return []

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
        # nexus-4yf4u (GH #1419 Issue 1): an unverifiable store means
        # something DIFFERENT depending on whether the service is actually
        # up, and the predecessor collapsed both into one unbounded,
        # unescalating deferral. A DOWN service is the ordinary ordering this
        # branch was written for. A service that is UP but cannot answer
        # /version, while the store also cannot be verified, is a WEDGED box
        # — Steve Harris's was pegged at 100-290% CPU — and deferring there
        # loops forever: the line reads as no-error, `nx doctor` keeps
        # reporting the same mismatch, and the remedy text ("once the service
        # is up, re-run") names a condition that is already true. Say so
        # loudly instead. Per Hal (2026-07-24) this does NOT auto-escalate to
        # install-binary: the product does not install an engine under a
        # store it cannot verify on its own initiative — it states the
        # situation and names the escape.
        # Probed HERE, not at the top of the function (review CRE-A finding
        # 3): this arm and the converged arm above are mutually exclusive, so
        # a single eager probe spent a lease discovery + /version GET on every
        # ordinary install path that never reads the result.
        running = _running_engine(config_dir)

        if running.up and running.version is None:
            from nexus.daemon.binary_install import PINNED_SERVICE_TAG  # noqa: PLC0415 — deferred, CLI startup cost

            verb = "would be BLOCKED" if dry_run else "NEEDS HUMAN: engine"
            return [
                f"{verb} convergence ({got_s} -> {req_s}) — the storage "
                "service is UP but is not answering /version "
                f"({running.reason or 'no answer'}), and the store could not "
                f"be verified either ({probe.unknown_reason}). This is NOT "
                "the ordinary not-up-yet ordering: a service that is running "
                "but unresponsive will not converge by re-running this "
                "command. Investigate the service first (`nx doctor`, "
                "`nx daemon service status`, and its log); if it is wedged, "
                "`nx daemon service stop` then `nx daemon service start`. "
                "Only once the store verifies will convergence proceed "
                "automatically. The explicit converge-now escape, which "
                "warns UNVERIFIED when it cannot probe the store: "
                "nx daemon service install-binary "
                f"{PINNED_SERVICE_TAG or '<engine-service-tag>'}"
            ]

        running_clause = ""
        if running.up and running.version is not None:
            got_run = ".".join(str(p) for p in running.version)
            running_clause = (
                f" The service IS up and running v{got_run}; convergence "
                "resumes once the store verifies."
            )

        if dry_run:
            return [
                f"would DEFER engine convergence ({got_s} -> {req_s}): "
                f"store chash conformance unverifiable "
                f"({probe.unknown_reason}){running_clause}"
            ]
        from nexus.daemon.binary_install import PINNED_SERVICE_TAG  # noqa: PLC0415 — deferred, CLI startup cost

        # Round-2 critique HIGH-2/MEDIUM-2: lead with the VERIFIED
        # convergence path (doctor / restart-stale re-run this same
        # tri-state gate), and never promise a passive retry —
        # check_version_transition stamps seen unconditionally, so the
        # re-attempt is operator-driven. install-binary is named last,
        # strictly for the will-not-boot class.
        ordering = (
            "This is the ordinary ordering when the service/PG is not up yet "
            "(GH #1414). Once the service is up, run"
            if not running.up else
            "The store is unverifiable for a reason other than the service "
            "being down. Once it verifies, run"
        )
        return [
            f"DEFERRED: engine convergence ({got_s} -> {req_s}) held back — "
            f"the chash-poison gate could not verify the store "
            f"({probe.unknown_reason}).{running_clause} {ordering} "
            "`nx doctor` (or `nx daemon restart-stale`) to "
            "converge verified. Only if the service cannot come UP on the "
            "current engine (the will-not-boot class): nx daemon service "
            f"install-binary {PINNED_SERVICE_TAG or '<engine-service-tag>'} "
            "(warns UNVERIFIED when it cannot probe the store)."
        ]

    # nexus-8eaeg: the acquisition seam. EVERY path that opens the network
    # for asset bytes is below this line, and this early return is what makes
    # a preview a preview — `nx upgrade --dry-run` reached
    # ``install_binary`` through the root-group finish trigger (which passed
    # no ``dry_run`` at all) and spent 4-5 minutes pulling ~190 MB it then
    # discarded. A dry run PLANS; it never acquires.
    if dry_run:
        why = (
            f" — {integrity.reason}"
            if integrity is not None and not integrity.ok else ""
        )
        return [
            f"would converge engine ({got_s} -> {req_s}): install the "
            f"pinned tag and restart the storage service{why}"
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

    # nexus-4yf4u: the install is a fact (it either raised or it did not);
    # the CONVERGENCE is a claim, and it is now observed rather than inferred
    # from the restart's returncodes.
    # nexus-v5lk3: transfer nexus.diag_chash_conformance's ownership to
    # nexus_admin BEFORE the restart that boots the freshly-installed
    # engine — see _reassign_diag_view_before_restart's own docstring for
    # the crash-loop this prevents.
    predrop_actions = _reassign_diag_view_before_restart(config_dir)
    return _restart_and_verify(
        config_dir,
        predrop_actions + [f"converged engine: installed {tag} (was {got_s})"],
        req_s,
    )


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


def _reassign_diag_view_before_restart(config_dir: Path) -> list[str]:
    """RDR-194 P3c companion fix (nexus-v5lk3, 2026-08-17). Wired into
    :func:`converge_engine`'s own restart-triggering call sites, immediately
    BEFORE each ``_restart_and_verify`` — see
    :func:`nexus.db.pg_provision.reassign_diag_view_owner_before_restart`'s
    own docstring for the full crash-loop this closes (essentially every
    existing local install carries a superuser-owned
    ``nexus.diag_chash_conformance``, which the first restart into a
    taxonomy-011-carrying engine would otherwise wedge on).

    SECONDARY, REDUNDANT-BUT-HARMLESS (RDR-194 critical fix round 4,
    nexus-rkn3i, 2026-08-17): this call site is KEPT as belt-and-braces, but
    is no longer the mechanism that makes the reassignment reachable from
    every restart path — that job now belongs to
    :func:`nexus.db.pg_provision.provision`'s own fast idempotency path
    (round 3's placement here only, without that wiring, left
    ``nx daemon service install-binary``'s own documented restart
    instruction unprotected, and gave a wedged box no automated recovery —
    see the target function's own docstring, "TWO CALLERS, ONE PRIMARY",
    for the full derivation). By the time ``_restart_and_verify`` below
    actually cycles the service, the fast-path call already ran once during
    that SAME restart's own ``nx daemon service start`` — so this call is
    ordinarily a no-op confirming what already happened, run one step
    earlier for the specific case where ``converge_engine`` itself decided
    to restart (shaving one settle/restart cycle off the very first
    floor-crossing convergence). It stays because two independent paths
    reaching the same safe state is a strictly stronger guarantee than one,
    at the cost of one extra idempotent psql round-trip.

    Same best-effort posture as :func:`heal_diag_view`: degrades to ``[]``
    on any probe failure (PG down, not service mode, no PG binaries on this
    box) — a probe that cannot run must never abort the restart it exists
    to protect. Deliberately NOT gated by ``dry_run``/``unattended`` the way
    :func:`converge_engine`'s other branches are: unlike a live-service
    restart (which can sever an in-flight client, GH #1419 Issue 3b) or an
    asset download (which a preview must never perform), this is a single
    metadata-only ``ALTER VIEW ... OWNER TO`` against a PostgreSQL cluster
    the service is not currently serving through — it carries none of the
    disruption those guards exist to prevent, and skipping it on a
    ``--dry-run`` or unattended pass would leave the box in exactly the
    wedged state this function exists to prevent on the very next real
    restart.
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
            reassign_diag_view_owner_before_restart,
        )

        creds = _read_credentials(creds_path)
        port = int(creds.get("PG_PORT", 0) or 0)
        if port <= 0:
            return []
        bins = discover_pg_binaries()
        os_user = bootstrap_superuser()
        return reassign_diag_view_owner_before_restart(bins, port, os_user)
    except Exception as exc:  # noqa: BLE001 — best-effort; must never abort the restart it protects
        _log.debug("diag_view_predrop_failed", error=str(exc))
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


def unload_stale_service_launchagent(config_dir: Path) -> list[str]:
    """Remove a NON-local box's stray ``com.nexus.service`` autostart unit
    (nexus-6bmph, RDR-183 residual — the :func:`unload_stale_t2_launchagent`
    sibling for the SERVICE unit; live evidence 2026-07-22: a cloud-mode box
    accumulated 810 err lines in one morning from the exit-2 respawn loop).

    Gated on ``is_local_mode()`` — the unit is LEGITIMATE on a local-service
    install (it is that box's boot autostart); only a managed/cloud-resolving
    box has nothing for it to run (no local pg_credentials -> immediate exit
    -> OS respawn churn). Same two-tier failure discipline as the t2 leg:
    applicability-probe failures degrade silently; a confirmed-present unit
    that fails to REMOVE is a loud NEEDS HUMAN line. Never raises.
    ``config_dir`` accepted-not-read for call-signature parity with siblings.
    """
    try:
        from nexus.config import is_local_mode  # noqa: PLC0415 — deferred, circular-dep avoidance

        if is_local_mode():
            return []

        from nexus.commands.daemon import _service_autostart_unit_installed  # noqa: PLC0415 — deferred, CLI startup cost

        if _service_autostart_unit_installed() is None:
            return []
    except Exception as exc:  # noqa: BLE001 — best-effort applicability probe; must never break the finish pass
        _log.debug("service_launchagent_applicability_probe_failed", error=str(exc))
        return []

    try:
        from nexus.daemon.installer import (  # noqa: PLC0415 — deferred, CLI startup cost
            UninstallStatus,
            uninstall_autostart,
        )

        result = uninstall_autostart(tier="service")
    except Exception as exc:  # noqa: BLE001 — a CONFIRMED-present unit failed to remove; loud, never a crash
        _log.warning("service_launchagent_unload_failed", error=str(exc))
        return [
            "NEEDS HUMAN: this box resolves to managed/cloud mode but a stray "
            f"storage-service autostart unit is installed and could not be removed ({exc}) — "
            "run `nx daemon service uninstall --autostart` yourself"
        ]

    if result.status != UninstallStatus.REMOVED:
        return []  # NOT_INSTALLED — the probe above already filtered this, defensive only

    actions = [
        "removed the stray storage-service autostart unit (managed/cloud mode — "
        f"it respawned an immediately-exiting local engine): {result.dest}"
    ]
    for w in getattr(result, "warnings", ()) or ():
        actions.append(f"note: {w}")
    return actions


def _autostart_probe_failure_action(exc: Exception) -> str:
    """NEEDS HUMAN line for a genuine (non-benign) failure while probing the
    service-tier autostart unit for drift -- used by
    :func:`converge_service_autostart_unit` when
    :func:`_probe_service_autostart_drift` raises."""
    return (
        f"NEEDS HUMAN: could not check the storage-service autostart unit "
        f"for drift ({exc}) -- run `nx daemon service install --autostart "
        "--force` if you believe the installed unit is stale, or `nx "
        "doctor` to investigate further."
    )


def _probe_service_autostart_drift() -> tuple[Path, str, str] | None:
    """Probe the LOCAL-mode service-tier autostart unit for content drift.

    Returns ``None`` for a BENIGN not-applicable result: this box is not
    local mode, or no service-tier unit is installed here. Returns
    ``(dest, existing, rendered)`` when a unit IS installed -- callers
    compare ``existing == rendered`` themselves rather than this function
    returning a bare bool, because the two current callers have
    DIFFERENT reactions to a match/mismatch (one is verbose and offers to
    converge; the other is a silent ``nx doctor`` row) and collapsing that
    into a bool here would just move the comparison, not remove it.

    Raises on a genuine probe failure (``is_local_mode()`` / the unit
    lookup / the render / the read blowing up) -- this function does NOT
    itself degrade a failure to "not applicable". That collapse is exactly
    the code-review Critical this split exists to prevent: a probe
    failure must never read the same as "nothing to do". Callers decide
    how loud to be about a raised exception (:func:`converge_service_autostart_unit`
    surfaces a NEEDS HUMAN line; ``nx health._check_service_autostart_drift``
    degrades silently -- the standing convention for every OTHER doctor
    check in that module, e.g. ``_check_engine_convergence``).

    Shared by :func:`converge_service_autostart_unit` (nexus-rlp0v) and
    :func:`nexus.health._check_service_autostart_drift` (its ``nx doctor``
    backstop, substantive-critic round 1) so the applicability + comparison
    logic exists in exactly one place -- the doctor check cannot silently
    drift from what the automatic/manual convergence path actually acts on.
    """
    from nexus.config import is_local_mode  # noqa: PLC0415 — deferred, circular-dep avoidance

    if not is_local_mode():
        return None

    from nexus.commands.daemon import _service_autostart_unit_installed  # noqa: PLC0415 — deferred, CLI startup cost

    dest = _service_autostart_unit_installed()
    if dest is None:
        return None

    from nexus.daemon import installer  # noqa: PLC0415 — deferred, CLI startup cost

    _, rendered = installer.rendered_unit_content(tier="service")
    existing = dest.read_text()
    return dest, existing, rendered


def converge_service_autostart_unit(
    config_dir: Path, *, dry_run: bool = False, unattended: bool = False,
) -> list[str]:
    """Re-render + re-activate a LOCAL-mode service-tier autostart unit
    whose installed content has drifted from the current template.

    nexus-rlp0v: ``conexus/daemon/com.nexus.service.plist`` dropped
    ``ProcessType=Background`` (launchd applied background QoS to the whole
    storage-service tree, confining the ONNX embedding inference to the
    E-cores -- a measured 15x throughput regression on an M4 Max, exactly
    reproducing the field-reported ~0.5 chunks/s local-mode indexing
    symptom). An installed unit is a RENDERED COPY under the OS autostart
    dir; launchd only re-reads it on bootout+bootstrap, and a plain package
    upgrade that overwrites the template in the wheel does not, by itself,
    touch an already-loaded launchd job. This leg closes that gap for an
    existing local install. Generalises past this one fix: any future
    template change converges the same way.

    Gated on ``is_local_mode()`` -- a managed/cloud box's stray service unit
    is :func:`unload_stale_service_launchagent`'s job, not this one. Returns
    ``[]`` (not applicable) when no service-tier unit is installed, or when
    the installed content already matches the current template (the
    overwhelmingly common case once a fix has rolled out once).

    On drift: ``unattended`` (the automatic post-upgrade finish pass) or
    ``dry_run`` never bounces the service -- same GH #1419 Issue 3b
    restraint :func:`converge_engine` already applies to a live restart --
    it only NAMES the manual command. The human path (``nx daemon
    restart-stale``) performs the actual convergence, reusing EXISTING
    machinery only: stop the service (the same ``nx daemon service stop``
    :func:`_restart_and_verify` already shells out to), tear down the stale
    unit via :func:`nexus.daemon.installer.uninstall_autostart` (bootout on
    macOS / disable on Linux -- the same removal path
    ``unload_stale_service_launchagent`` and ``daemon_uninstall`` already
    use), then reinstall via :func:`nexus.daemon.installer.install_autostart`
    (write + activate -- ``launchctl bootstrap`` with ``RunAtLoad`` /
    ``systemctl --user enable --now``, either of which starts the process
    itself). No separate ``nx daemon service start`` call: issuing one right
    after activation would race the freshly-bootstrapped process publishing
    its own lease against ``ensure_storage_supervisor``'s Popen fallback,
    risking a second supervisor. Convergence is instead OBSERVED via the
    same lease/``/version`` probe :func:`_restart_and_verify` polls with.

    Never raises -- every failure degrades to a NEEDS HUMAN action line.
    code-review round 1, Critical: the applicability probe (is_local_mode ->
    unit lookup -> render -> read) used to sit under ONE blanket
    ``except Exception: return []``, which could not tell "genuinely not
    applicable" (not local mode, no unit installed -- true []s) apart from
    "a probe call raised" (e.g. the render blowing up) -- the latter
    silently claimed "no action needed" exactly like the former,
    contradicting this docstring and the no-silent-fallbacks hot rule.
    :func:`_probe_service_autostart_drift` now carries that distinction on
    its own terms (``None`` return vs. a raised exception) -- a BENIGN
    not-applicable result returns ``[]`` directly (no exception involved),
    while an exception from the probe is a genuine FAILURE and always
    produces a NEEDS HUMAN line, never a silent [].
    """
    try:
        probe = _probe_service_autostart_drift()
    except Exception as exc:  # noqa: BLE001 — genuine probe failure, never a silent []
        _log.warning("service_autostart_convergence_probe_failed", error=str(exc))
        return [_autostart_probe_failure_action(exc)]
    if probe is None:
        return []  # benign: not local mode, or no service-tier unit installed here
    dest, existing, rendered = probe

    if existing == rendered:
        return []  # benign: already up to date

    note = (
        f"the storage-service autostart unit at {dest} differs from the "
        "current template"
    )
    if unattended or dry_run:
        return [
            f"NOTE: {note}. Not reinstalling it here -- run `nx daemon "
            "restart-stale` to converge it (stops the service, reinstalls "
            "the autostart unit, and restarts it)."
        ]

    manual_fallback = (
        "run `nx daemon service stop && nx daemon service uninstall "
        "--autostart && nx daemon service install --autostart` yourself, "
        "then `nx doctor` to confirm the service came back up."
    )

    try:
        stop = subprocess.run(
            ["nx", "daemon", "service", "stop"],
            capture_output=True, text=True, timeout=60,
        )
    except Exception as exc:  # noqa: BLE001 — best-effort convergence; surfaced in the line
        return [f"NEEDS HUMAN: {note}, but stopping it raised {exc} -- {manual_fallback}"]
    if stop.returncode != 0:
        detail = (stop.stderr or stop.stdout or "").strip()
        return [
            f"NEEDS HUMAN: {note}, but `nx daemon service stop` exited "
            f"{stop.returncode} ({detail}) -- {manual_fallback}"
        ]

    try:
        from nexus.daemon import installer  # noqa: PLC0415 — deferred, CLI startup cost

        uninstall_result = installer.uninstall_autostart(tier="service")
        if uninstall_result.status not in (
            installer.UninstallStatus.REMOVED,
            installer.UninstallStatus.NOT_INSTALLED,
        ):
            return [
                f"NEEDS HUMAN: {note}, but removing the stale unit reported "
                f"{uninstall_result.status} -- {manual_fallback}"
            ]
        install_result = installer.install_autostart(tier="service")
    except Exception as exc:  # noqa: BLE001 — never let convergence crash the finish pass
        _log.warning("service_autostart_convergence_failed", error=str(exc))
        return [f"NEEDS HUMAN: {note}, and converging it raised {exc} -- {manual_fallback}"]

    # The freshly-activated unit does not publish its lease instantly; bound
    # the wait the same way _restart_and_verify does rather than declaring
    # victory on returncode alone (nexus-4yf4u's lesson, one leg over). Bound
    # by BOTH an attempt count and a wall-clock deadline — the attempt cap is
    # what keeps this deterministic and instant under a patched `time.sleep`
    # in tests; the deadline is what still bounds it in production.
    settle_s, poll_s = 20.0, 1.0
    running = _running_engine(config_dir)
    if not running.up:
        deadline = time.time() + settle_s
        for _ in range(max(1, int(settle_s / poll_s) + 1)):
            if time.time() >= deadline:
                break
            time.sleep(poll_s)
            running = _running_engine(config_dir)
            if running.up:
                break

    if running.up:
        actions = [
            f"converged the storage-service autostart unit at "
            f"{install_result.dest} ({install_result.detail}) — verified "
            "the service came back up"
        ]
    else:
        actions = [
            f"NEEDS HUMAN: converged the storage-service autostart unit at "
            f"{install_result.dest} ({install_result.detail}), but the "
            f"service is not answering after the restart ({running.reason or 'no answer'}) "
            "-- check `nx daemon service status` and `nx doctor`."
        ]
    for w in getattr(install_result, "warnings", ()) or ():
        actions.append(f"note: {w}")
    return actions


def unload_stale_t2_launchagent(config_dir: Path) -> list[str]:
    """Remove a stray ``com.nexus.t2`` LaunchAgent left by an older install.

    ``config_dir`` is accepted but not read: the autostart unit is
    filesystem-global, not config-dir scoped. Kept as a parameter purely so
    this leg's call signature matches its siblings
    (:func:`converge_engine`, :func:`heal_diag_view`) at every call site
    (the finish pass, ``nx daemon restart-stale``, ``nx init --service``)
    without a special case.

    NO LONGER GATED ON SERVICE MODE (nexus-i711w Stage 2 sub-stage B). The
    gate used to read ``storage_backend_for("memory") == SERVICE``, mirroring
    the oracle ``t2_daemon.py`` itself checked, so that a local-mode box could
    keep its unit — on such a box the T2 daemon was the live substrate and a
    local ``nx daemon t2 install --autostart`` round-trip legitimately
    recreated the agent.

    That reasoning died with the daemon. No box of any storage mode can start
    a T2 daemon now, and no box can reinstall the unit, so a surviving unit is
    stale EVERYWHERE — it fires ``nx daemon t2 start``, a command that no
    longer exists, on every boot forever. Keeping the service-mode gate would
    have left exactly the SQLite-mode boxes, the ones most likely to carry a
    unit, unfixed.

    Delegates the actual removal to
    :func:`nexus.daemon.installer.uninstall_autostart` (``tier="t2"``), whose
    "t2" default survives the daemon for this caller's sake; no hand-typed
    duplicate of the launchd mechanics here.

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
            f"NEEDS HUMAN: found a stray T2 autostart unit "
            f"({_T2_AUTOSTART_UNIT_KIND}) but could not remove it ({exc}) — "
            f"delete it yourself; it fires a `nx daemon t2 start` that no "
            f"longer exists on every boot"
        ]

    if result.status != UninstallStatus.REMOVED:
        return []  # NOT_INSTALLED — the probe above already filtered this, defensive only

    actions = [
        f"removed the stray T2 autostart unit ({_T2_AUTOSTART_UNIT_KIND}; "
        "the T2 daemon is retired — storage is the engine service): "
        f"{result.dest}"
    ]
    actions.extend(f"NOTE: {w}" for w in result.warnings)
    return actions


def pending_data_rung_callout() -> list[str]:
    """One summary line per pending DATA rung after an engine auto-converge
    (RDR-180 / critic-180-cohort finding 2). The chash-rekey rung gets an
    explicit consequence statement — its not-yet-run state silently breaks
    citation resolution for pre-existing content, unlike earlier rungs
    whose unconverted rows were merely inert. Read-only: a raising detect()
    surfaces as an explicit unknown/unavailable line (never silently
    dropped — nexus-v2mdd: ``except Exception: continue`` used to make a
    raising detect() indistinguishable from "not pending", silently
    deleting exactly the chash-rekey warning this function exists to emit).
    ``nx doctor`` remains the backstop for full detail either way.

    nexus-jgac3: v2mdd fixed the per-rung ``detect()`` leak above but left
    the construction of the registry itself (the deferred import and the
    ``default_registry()`` call that FEEDS the loop) unguarded — a raising
    ``default_registry()`` used to propagate out of this function entirely,
    to be silently swallowed by the sole caller's own outer try/except
    (``check_version_transition``), the identical fail-open-to-silence
    shape one frame up. Guarded here the same way, mirroring
    ``health._check_pending_rungs``' outer handler (its twin fix in the
    same v2mdd commit).
    """
    try:
        from nexus.upgrade_ladder.registry import default_registry  # noqa: PLC0415 — deferred, CLI startup cost

        rungs = default_registry()
    except Exception as exc:  # noqa: BLE001 — must surface, not silently drop
        return [
            "pending data rungs status unknown — could not load the "
            f"upgrade-ladder registry: {exc}"
        ]

    lines: list[str] = []
    for rung in rungs:
        try:
            status = rung.detect()
        except Exception as exc:  # noqa: BLE001 — must surface, not silently drop
            lines.append(
                f"rung '{rung.name}' status unknown — detect failed: {exc}"
            )
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


def check_version_transition(
    config_dir: Path, *, preview: bool | None = None,
) -> str | None:
    """Version-stamp auto-trigger. Returns a one-line summary when a
    version transition was detected and the safe finish pass ran; None
    when the stamp is current (the overwhelmingly common case).

    uv offers no post-install hook, so the first invocation after an
    upgrade is the earliest the product can finish the job itself.

    PREVIEW MODE (nexus-8eaeg): this trigger fires from the ROOT ``nx``
    group, before Click has parsed the subcommand, so a ``--dry-run``
    invocation used to run the finish pass WET — including an engine
    convergence that opened a ~190 MB release-asset download and threw it
    away. ``preview`` (defaulted from :func:`invocation_is_preview`) makes
    the pass PLAN instead: no acquisition, no restarts, and — critically —
    the version stamp is NOT consumed, so the next ordinary invocation still
    finishes the job for real. The two legs that mutate and have no plan-only
    form (the diag-view grant heal and the stale-launchagent unloads) are
    skipped and said to be skipped, rather than performed under a flag that
    promised they would not be.

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
    if preview is None:
        preview = invocation_is_preview()
    if not preview:
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
        # nexus-g7ijj: best-effort install.mode backfill, gated on the
        # one-shot stamp claim above having already won the race (so two
        # concurrent transitioners don't both attempt it) and NEVER on the
        # preview path (the `if not preview:` this sits inside). Placed
        # ahead of the `if not seen: return None` below on purpose — an
        # ancient install that upgraded through several releases before
        # this stamp file ever existed hits exactly that first-ever-run
        # branch, and is exactly the never-recorded case this closes.
        try:
            from nexus.config import backfill_install_mode_record  # noqa: PLC0415 — deferred to avoid import cycle
            backfill_install_mode_record()
        except Exception:  # noqa: BLE001 — best-effort; the finish pass must never break CLI startup
            _log.warning("install_mode_backfill_failed", exc_info=True)
    # nexus-8eaeg: a preview claims NOTHING — not even the one-shot stamp.
    # Consuming it here would let `nx upgrade --dry-run` silently burn the
    # transition, so the real finish pass would never run automatically.
    if not seen:
        return None  # first-ever run: nothing stale to finish
    if not running_from_tool_install():
        # A dev checkout's venv mtime says nothing about the production
        # processes on this box — measuring (let alone killing) them from
        # here is the cross-venv confusion class. Report-only via doctor.
        return None
    try:
        report = detect_stale_processes()
        actions = restart_stale(report, dry_run=preview)
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
        actions = actions + converge_engine(
            config_dir, unattended=True, dry_run=preview,
        )
    except Exception:  # noqa: BLE001 — the finish pass must never break CLI startup
        _log.warning("engine_convergence_failed", exc_info=True)
    # nexus-rlp0v: a drifted service-tier autostart unit (e.g. a stale
    # ProcessType=Background) needs the same "never bounces the service
    # unattended" restraint as engine convergence above — report-only here,
    # same as converge_engine's unattended=True branch.
    try:
        actions = actions + converge_service_autostart_unit(
            config_dir, unattended=True, dry_run=preview,
        )
    except Exception:  # noqa: BLE001 — the finish pass must never break CLI startup
        _log.warning("service_autostart_convergence_failed", exc_info=True)
    if preview:
        # These two legs REMEDIATE (PG grant/ownership repair, launchd unit
        # removal) and have no plan-only form. Under `--dry-run` they are not
        # run — and that omission is stated rather than left as silence.
        actions = actions + [
            "not evaluated on a --dry-run invocation: the diag-view grant "
            "heal and the stale-launchagent unloads (both remediate; run "
            "`nx doctor` or any ordinary `nx` command to finish for real)"
        ]
    else:
        try:
            actions = actions + heal_diag_view(config_dir)
        except Exception:  # noqa: BLE001 — the finish pass must never break CLI startup
            _log.warning("diag_view_heal_failed", exc_info=True)
        try:
            actions = actions + unload_stale_t2_launchagent(config_dir)
            actions = actions + unload_stale_service_launchagent(config_dir)
        except Exception:  # noqa: BLE001 — the finish pass must never break CLI startup
            _log.warning("t2_launchagent_unload_failed", exc_info=True)
    # critic-180-cohort finding 2: engine convergence swaps the binary (and
    # boot applies the RDR-180 schema) but does NOT walk the ladder — a box
    # can sit engine-converged-but-never-rekeyed, with citations for
    # PRE-EXISTING content silently unresolvable until `nx upgrade` runs the
    # chash-rekey rung. Surface that state in THIS summary, loudly, instead
    # of leaving it to nx doctor alone.
    #
    # nexus-jgac3: this try/except is now DEFENSE-IN-DEPTH ONLY, not the
    # last line against a known-raising path. pending_data_rung_callout()
    # itself guards BOTH of its actual failure points (default_registry()
    # construction and each rung's detect()) and always returns a list —
    # one that names the failure in an explicit line when either guard
    # trips. What THIS handler can still catch is a genuinely unexpected
    # bug in pending_data_rung_callout()'s own list-building code, which
    # degrades to a structlog WARNING with no user-visible action line —
    # identical, deliberately, to every sibling leg above (process-skew
    # detection, engine convergence, diag-view heal, launchagent unloads):
    # none of them promote their own outer catch to a visible "NOTE: leg
    # X failed" line either. Left as-is rather than special-cased, so this
    # leg does not diverge from the established pattern the other legs
    # share.
    try:
        actions = actions + pending_data_rung_callout()
    except Exception:  # noqa: BLE001 — the finish pass must never break CLI startup
        _log.warning("pending_rung_callout_failed", exc_info=True)
    _log.info(
        "upgrade_finish_ran",
        from_version=seen, to_version=version, actions=actions,
        preview=preview,
    )
    if preview:
        # NOTHING was done and the stamp is untouched — say both, so the line
        # cannot be mistaken for a finished pass.
        return (
            f"PREVIEW ONLY (nothing changed): finishing {seen} -> {version} "
            "is still pending; " + "; ".join(actions)
        )
    if not actions:
        return f"upgraded {seen} -> {version}; no stale processes"
    return (
        f"upgraded {seen} -> {version}; " + "; ".join(actions)
    )
