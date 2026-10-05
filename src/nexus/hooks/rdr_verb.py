# SPDX-License-Identifier: AGPL-3.0-or-later
# Copyright (c) 2026 Hal Hildebrand. All rights reserved.
"""The ``rdr`` SessionStart hook verb (RDR-215 bead nexus-q02nx.21), ported
from ``conexus/hooks/scripts/rdr_hook.py``: detect the RDR dir and report
document count, T2 status breakdown, and whether the tree is indexed.
Read-only. Registered in ``hooks.json`` as a standalone ``SessionStart``
entry -- it takes no stdin payload of its own (see :func:`run`) and runs
alongside, not instead of, ``session-start``/:mod:`session_start_verb`.

**Move, do not rewrite** (RDR-215 Approach item 9): every helper below is
carried unchanged from the script, including the ``nexus-e19sa`` ruling
this module's original docstring records. The one authorised substitution
is the logging bridge: the script's ``import _hook_logging`` /
``_hook_logging.configure_hook_logging()`` (a plugin-local module that
cannot move into the wheel) becomes
:func:`nexus._hook_runtime._io.configure_hook_logging`, a same-name,
same-behaviour equivalent -- both defer-import
``nexus.logging_setup.configure_logging(mode="hook")`` and swallow every
exception. This verb DOES log (through ``nexus.catalog``/``nexus.db``'s own
ambient ``structlog.get_logger()`` calls it triggers), so unlike its
stdlib-only siblings it is entitled to pay for that sink.

**Structural adaptation, not behavioural.** Two things changed shape
because the *hosting* mechanism changed, not because anything the script
computed was wrong:

* The ``sys.version_info < (3, 12)`` guard and its ``sys.exit(1)`` are
  dropped, matching every other ported verb (:mod:`session_start_verb`). That guard existed because ``_run_python_hook.sh``
  could fall through to an arbitrary system ``python3``; a verb reached
  only via ``importlib.import_module`` from inside the installed ``nexus``
  package has no such path -- the interpreter is whatever conexus itself
  requires (3.12+, ``pyproject.toml``).
* ``main()``'s ``print(...)`` calls and its three ``sys.exit(0)`` points
  become an accumulated list of lines returned as one
  :class:`~nexus._hook_runtime._io.HookResult`. ``nexus._hook_runtime.entry.main``
  writes ``result.stdout + "\\n"`` when ``stdout is not None``, so the
  join point is ``"\\n".join(lines)`` with no trailing newline -- the
  concatenation of the original script's individual ``print()`` calls,
  each of which supplied its own trailing newline, and every early
  ``sys.exit(0)`` with nothing printed becomes ``HookResult()``
  (``stdout=None``), the same silent-success contract.

Original docstring, preserved verbatim below the import block:

nexus-e19sa (Sam's ruling, 2026-09-02): this hook used to carry a second
half -- a file<->T2 status RECONCILE that rewrote whichever side ranked
lower (``_reconcile`` / ``_update_file_status`` / ``_update_t2_status`` and
the terminal-rank derivation feeding them). It never ran once: the file
filter was ``re.match(r"\\d+", p.stem)`` against stems shaped
``rdr-201-...``, so it matched zero files and the hook exited before any
logic, on every session since it was written. That killed both halves.
The writer half is DELETED rather than switched on: a never-watched
two-way writer whose first live run would have resolved nine known file/T2
disagreements by a ranking rule nobody had seen work was the risky thing
here, not the missing feature. (``nx rdr set-status`` mirrors a file flip
onto T2 best-effort since 2026-09-02 (``_write_t2_status``); a failed or
missing mirror prints a note and the file flip stands, so the drift class
still exists and is DETECTED, not reconciled: ``nx rdr preamble
rdr-audit`` prints a ``DRIFT:`` line per disagreement for a human to
settle.) The read-only summary is kept and the filter fixed so it finally
prints. The nine known drift rows are bead nexus-nxn5g.
"""
from __future__ import annotations

import datetime as _dt
import os
import re
import subprocess
import sys
import threading
import time
from collections import Counter
from collections.abc import Callable
from pathlib import Path
from typing import TypeVar

from nexus._hook_runtime._io import HookResult, _emit, configure_hook_logging  # noqa: PLC2701 — _emit is the shared never-stdout logging spine (its docstring says why); a hook verb must not log through an ambient structlog logger

_T = TypeVar("_T")

# ── the run's wall-clock budget (nexus-wozn6) ───────────────────────────────
#
# hooks.json gives this hook 10 s, and Claude Code cancels it at 10 s with
# nothing printed. Measured 2026-10-05 against the cloud tenant: the run spent
# 3.3 s in the T3 existence probe (a tenant-wide listing, since replaced by a
# one-collection count) and 1.7-8.9 s fetching the 1527-row ``nexus_rdr`` T2
# project, and 295 of 447 SessionStart firings since 2026-09-30 were
# cancelled. Every network call below now runs under the time left in ONE
# budget, so the run returns before the cap whatever the engine does. The
# 10 s cap leaves ~3 s beyond this budget for interpreter start, imports and
# the write. ``tests/test_hook_budgets_pinned_to_hooks_json.py`` holds this
# number against the timeout hooks.json declares.

#: Seconds from the start of :func:`run` that the network legs may use.
_HOOK_BUDGET_S = 7.0

#: ``time.monotonic()`` at which the current :func:`run` must stop waiting on
#: the network; ``None`` outside a run, so a helper called on its own gets its
#: own per-leg cap.
_run_deadline: float | None = None


def _remaining(cap: float) -> float:
    """The smaller of *cap* and what is left of the run's budget, never
    negative."""
    if _run_deadline is None:
        return cap
    return max(0.0, min(cap, _run_deadline - time.monotonic()))


class _Pending:
    """A call running in a DAEMON thread that the caller waits on later.

    A daemon thread, never a ``ThreadPoolExecutor`` worker:
    ``concurrent.futures`` joins its non-daemon workers at interpreter exit,
    so a hung call would hold the hook process past its own deadline
    (nexus-r8643). Starting and waiting are separate so the slowest leg (the
    T2 fetch) can start first and run alongside the others."""

    def __init__(self, fn: Callable[[], object]) -> None:
        self._outcome: dict[str, object] = {}
        self.started = time.monotonic()
        self._thread = threading.Thread(target=self._work, args=(fn,), daemon=True)
        self._thread.start()

    def _work(self, fn: Callable[[], object]) -> None:
        try:
            self._outcome["value"] = fn()
        except BaseException as exc:  # noqa: BLE001 — carried back via the outcome dict, not raised across the thread boundary
            self._outcome["error"] = exc

    def elapsed(self) -> float:
        """Seconds since the call started."""
        return time.monotonic() - self.started

    def wait(self, seconds: float) -> object:
        """The call's value, waiting at most *seconds*. Raises
        :class:`TimeoutError` past it and re-raises whatever the call
        raised."""
        self._thread.join(timeout=seconds)
        if self._thread.is_alive():
            raise TimeoutError(f"no answer within {seconds:.1f}s")
        if "error" in self._outcome:
            raise self._outcome["error"]  # type: ignore[misc]
        return self._outcome["value"]


def _call_with_deadline(fn: Callable[[], _T], seconds: float) -> _T:
    """Run *fn* in a daemon thread and wait at most *seconds* for it
    (:class:`_Pending`). Raises :class:`TimeoutError` past the deadline and
    re-raises whatever *fn* raised."""
    return _Pending(fn).wait(seconds)  # type: ignore[return-value]

_EXCLUDE_FILES = {
    "readme.md", "template.md", "index.md", "overview.md",
    "workflow.md", "templates.md", "agents.md",
}

#: The stems this repo's RDR files actually have: ``rdr-201-foo`` (the
#: standard shape), ``rdr137-foo`` (one legacy file with no second hyphen),
#: and the bare ``001-foo`` shape the original filter was written for and
#: nothing here ever used. Anchored at the start of the stem, so a sibling
#: like ``status-census-2026-09-01`` (digits, not leading) is not an RDR.
_RDR_STEM_RE = re.compile(r"(?:rdr-?)?(\d+)", re.IGNORECASE)


def _repo_root() -> Path | None:
    from nexus.bounded_subprocess import run_bounded  # noqa: PLC0415 — deferred: a hook process pays its import cost on every invocation, and a module-scope import of this pulls structlog + ~231 modules (measured on verification_config: 14ms/106 -> 62-84ms/337). Deferred, it is paid only when we actually spawn

    try:
        result = run_bounded(
            ["git", "rev-parse", "--show-toplevel"],
            timeout=_remaining(5),
        )
        if result.returncode == 0:
            return Path(result.stdout.strip())
    except Exception:  # noqa: BLE001 — a hook never fails the prompt over a git probe; None is the honest degraded answer
        pass
    return None


def _repo_name(root: Path) -> str:
    """The checkout's own name: the git COMMON dir's parent basename, the
    same derivation ``nx rdr preamble`` uses (``_preamble_resolve_repo``),
    so a linked worktree reads the repo's T2 project, not one named after
    the worktree directory (nexus-u1jxt.7). Falls back to *root*'s name."""
    from nexus.bounded_subprocess import run_bounded  # noqa: PLC0415 — deferred: a hook process pays its import cost on every invocation, and a module-scope import of this pulls structlog + ~231 modules (measured on verification_config: 14ms/106 -> 62-84ms/337). Deferred, it is paid only when we actually spawn

    try:
        result = run_bounded(
            ["git", "-C", str(root), "rev-parse", "--path-format=absolute", "--git-common-dir"],
            timeout=_remaining(5),
        )
        if result.returncode == 0 and result.stdout.strip():
            return Path(result.stdout.strip()).resolve().parent.name
    except Exception:  # noqa: BLE001 — a hook never fails the prompt over a git probe; the fallback name is the honest degraded answer
        pass
    return root.name


def _file_frontmatter(path: Path) -> dict[str, str]:
    """The first ``key: value`` lines of *path*'s YAML frontmatter, lower-
    cased keys, quotes stripped -- enough to read ``status``, ``kind`` and
    ``id`` without a YAML parser the hook cannot import. ``{}`` for a file
    with no frontmatter or one it cannot read."""
    out: dict[str, str] = {}
    try:
        with path.open(encoding="utf-8", errors="replace") as fh:
            first = fh.readline()
            if first.strip() != "---":
                return out
            for line in fh:
                stripped = line.strip()
                if stripped == "---":
                    break
                if ":" in stripped and not stripped.startswith("#"):
                    key, _, val = stripped.partition(":")
                    out[key.strip().lower()] = val.strip().strip('"').strip("'")
    except OSError:
        return {}
    return out


def _is_companion(path: Path) -> bool:
    """A companion note carries no lifecycle status (nexus-u1jxt.7, the
    same two markers ``nx rdr``'s ``_rdr_meta_is_companion`` reads:
    ``kind: companion``, or the older ``id: companion-note``)."""
    meta = _file_frontmatter(path)
    return meta.get("kind") == "companion" or meta.get("id", "").lower() == "companion-note"


def _resolve_rdr_collection(repo_root: Path) -> str | None:
    """Resolve the indexed RDR collection name for ``repo_root``.

    Returns the conformant ``rdr__<owner>__voyage-context-3__v1`` name
    when both the catalog and an owner row exist; otherwise asks the
    indexer's :func:`_conformant_name_for_repo` for the path-derived
    conformant fallback so SessionStart keeps working before ``nx index
    repo`` has run. Returns ``None`` when no in-process resolution is
    available; the caller treats that as "not indexed" rather than
    splicing a non-conformant 2-segment shape that the post-Phase-5
    strict-naming guard would later reject.

    The fallback is the pure synthesis, not ``_repo_collection_or_legacy``:
    since nexus-n9xjy that wrapper lets an unreachable catalog or an
    unparseable resolver result propagate (a synthesized name must never
    reach a writer), and this read-only hook is the one caller whose
    contract is a best-effort guess.
    """
    try:
        # RDR-158 P4 (nrxs9 final review Critical-1): this branch imported
        # the deleted local ``Catalog``, so the broad except silently forced
        # EVERY session onto the path-derived fallback. The service catalog
        # carries the same lookup.
        from nexus.catalog.factory import make_catalog_reader  # noqa: PLC0415

        def _lookup() -> str | None:
            cat = make_catalog_reader()
            try:
                return cat.collection_for_repo(repo_root, "rdr").render()
            except LookupError:
                return None  # owner not registered yet, fall through

        # nexus-wozn6: the catalog call is a network call with the client's
        # own 30 s request timeout, three times this hook's cap.
        found = _call_with_deadline(_lookup, _remaining(_CATALOG_DEADLINE_S))
        if found is not None:
            return found
    except Exception as exc:  # noqa: BLE001 — the SessionStart hook must never fail; the reason is logged, not swallowed (nexus-owna8)
        _log_resolution_error("catalog", exc)
    try:
        from nexus.indexer import _conformant_name_for_repo  # noqa: PLC0415

        return _conformant_name_for_repo(repo_root, "rdr")
    except Exception as exc:  # noqa: BLE001 — same contract as above
        _log_resolution_error("path-derived", exc)
        return None


#: Every resolution failure this run, oldest first: the NOT-indexed verdict
#: names them on stdout, the only channel a session sees from an exit-0
#: SessionStart hook (nexus-4ti7e; stderr needs `claude --debug`).
_RESOLUTION_FAILURES: list[str] = []


def _hook_log_path() -> Path:
    """Durable failure log beside the lockstep hook's, honouring
    NEXUS_CONFIG_DIR: ``<config>/rdr_hook.log`` (override: NX_RDR_HOOK_LOG)."""
    override = os.environ.get("NX_RDR_HOOK_LOG")
    if override:
        return Path(override)
    cfg = os.environ.get("NEXUS_CONFIG_DIR") or str(Path.home() / ".config" / "nexus")
    return Path(cfg) / "rdr_hook.log"


def _log_resolution_error(source: str, exc: BaseException) -> None:
    """nexus-owna8: a blind except here forced every session onto the
    path-derived fallback, whose owner id can differ from the catalog's, and
    the hook then reported a fully indexed tree as NOT indexed. The failure
    is recorded for the verdict line, appended to a durable log, written to
    stderr, and only then handed to structlog (nexus-4ti7e: the interpreter
    that ran this hook on 2026-09-08 had neither nexus nor structlog, and an
    exit-0 SessionStart hook's stderr is never shown)."""
    detail = str(exc)
    if len(detail) > 200:
        detail = detail[:200] + "..."
    line = f"{source}: {type(exc).__name__}: {detail} [python {sys.executable}]"
    _RESOLUTION_FAILURES.append(line)
    try:
        path = _hook_log_path()
        path.parent.mkdir(parents=True, exist_ok=True)
        with path.open("a", encoding="utf-8") as fh:
            fh.write(f"{_dt.datetime.now(_dt.timezone.utc).isoformat()} resolution failed {line}\n")
    except Exception:  # noqa: BLE001 — the log is best-effort in a hook
        pass
    # stderr FIRST, with nothing but sys: on 2026-09-08 the interpreter that
    # ran this hook had neither nexus nor structlog, so the structlog line
    # below could not be written and the false NOT-indexed verdict shipped
    # with an empty stderr (nexus-4ti7e). A guard that needs the package it
    # guards is no guard.
    try:
        sys.stderr.write(
            f"rdr_hook: collection resolution failed ({source}): "
            f"{type(exc).__name__}: {exc} [python {sys.executable}]\n"
        )
    except Exception:  # noqa: BLE001 — even stderr is best-effort in a hook
        pass
    try:
        import structlog  # noqa: PLC0415

        structlog.get_logger(__name__).warning(
            "rdr_hook_collection_resolution_failed",
            source=source, error_type=type(exc).__name__, error=str(exc),
        )
    except Exception:  # noqa: BLE001 — even the log is best-effort in a hook
        pass


# hooks.json caps this hook at 10s and the T3 client's own request timeout
# is 30s, so a slow-but-reachable store would have the harness kill the hook
# before the listing fallback ever ran (review of a71c93e92). Every leg gets
# a cap, and inside a run each cap is further cut to what the run's budget
# has left (:func:`_remaining`). The caps are several times the measured
# cost (2026-10-05, cloud tenant): catalog lookup 0.4 s, one-collection
# count 0.31 s.
_CATALOG_DEADLINE_S = 2.0
_T3_DEADLINE_S = 2.0
_LISTING_TIMEOUT_S = 4
#: The listing fallback spawns the ``nx`` CLI, whose start alone costs about
#: a second, so it is not worth starting with less than this left.
_LISTING_MIN_S = 1.0


def _collection_exists(target: str) -> bool | None:
    """Whether *target* holds chunks in T3, asked of the store itself
    (nexus-owna8: the previous substring match over ``nx collection list``
    output missed a listed collection when the resolved name and the
    listed name were rendered differently). The T3 call runs under
    ``_T3_DEADLINE_S``; past it, or on any error, the listing is the fallback.

    Returns ``None`` when neither the probe nor the listing answered (the
    probe failed or timed out and the listing was skipped for budget, failed
    or timed out): the hook does not know, and :func:`_run` says so rather
    than claiming "NOT indexed" and telling the user to re-index a tree that
    may be fine.

    nexus-wozn6: the probe is ``collection_info`` (one collection's STORED
    count, ``KeyError`` when it holds none), not ``collection_exists``. On
    the HTTP client ``collection_exists`` lists every collection's LIVE
    stats to answer for one, which measured 3.3 s on a 98-collection tenant
    against 0.31 s here. The two differ for a collection whose every chunk
    is trashed or unowned: its stored count is above zero, so this reads it
    as indexed, where the live-stats listing read it as absent. For a line
    whose other answer is "run ``nx index repo``", that edge is acceptable.

    nexus-r8643 (intrastate review [26115] #3): the T3 call runs in a DAEMON
    thread (:func:`_call_with_deadline`), never a ``ThreadPoolExecutor``
    worker, so a hung client cannot hold the process past the deadline."""
    from nexus.bounded_subprocess import run_bounded  # noqa: PLC0415 — deferred: a hook process pays its import cost on every invocation, and a module-scope import of this pulls structlog + ~231 modules (measured on verification_config: 14ms/106 -> 62-84ms/337). Deferred, it is paid only when we actually spawn

    try:
        from nexus.db import make_t3  # noqa: PLC0415

        def _probe() -> bool:
            try:
                return int(make_t3().collection_info(target).get("count", 0)) > 0
            except KeyError:
                return False

        return _call_with_deadline(_probe, _remaining(_T3_DEADLINE_S))
    except Exception as exc:  # noqa: BLE001 — the hook must never fail; fall back to the listing
        _log_resolution_error("t3-exists", exc)
    left = _remaining(_LISTING_TIMEOUT_S)
    if left < _LISTING_MIN_S:
        return None  # the probe failed and there is no budget to ask the listing: unknown, not absent
    try:
        result = run_bounded(
            ["nx", "collection", "list"],
            timeout=left,
        )
        if result.returncode == 0:
            return target in result.stdout
    except Exception:  # noqa: BLE001 — best-effort fallback
        pass
    return None


def _extract_rdr_id(filepath: Path) -> str | None:
    """Numeric RDR id from a ``docs/rdr`` filename, or ``None`` for a file
    that is not an RDR document (see :data:`_RDR_STEM_RE`). This is the
    file filter :func:`run` applies; nexus-e19sa's whole lesson is that a
    filter selecting nothing looks exactly like a quiet success."""
    m = _RDR_STEM_RE.match(filepath.stem)
    return m.group(1) if m else None


_T2_ROWS_CACHE: dict[str, list[dict]] = {}

#: Projects whose T2 fetch did not answer inside the run's budget this run.
_T2_LATE: set[str] = set()

#: The line :func:`run` adds when the T2 fetch ran out of budget. Without it
#: a late fetch reads exactly like a project with no recorded statuses, and a
#: filter that selects nothing looking like a quiet success is the whole
#: lesson of nexus-e19sa.
_T2_LATE_NOTE = "(T2 RDR statuses did not arrive within the hook budget; status breakdown and rdr-fix pointers omitted)"

#: Cap on the T2 fetch when it is called outside a run. Inside a run the
#: remainder of the budget binds, and the cap is the whole budget because the
#: fetch now STARTS first (:func:`_start_t2_fetch`) and runs beside the
#: catalog and T3 legs: it can use all of it, and raising the budget raises
#: this with it. Measured 1.7-8.9 s for the 1527-row ``nexus_rdr`` project
#: (2026-10-05).
_T2_DEADLINE_S = _HOOK_BUDGET_S

#: T2 fetches started this run and not yet collected, by repo name.
_T2_PENDING: dict[str, _Pending] = {}


def _start_t2_fetch(repo_name: str) -> _Pending:
    """Start the project's T2 fetch in a daemon thread and return at once.

    The fetch is the slowest leg and independent of the catalog and T3 legs,
    so it starts first and is collected last by :func:`_fetch_rdr_rows`. The
    handle is opened on the db/ side of the RDR-120 storage boundary: this
    module is in the wheel now, where the lint can see it, and T2Database
    construction outside src/nexus/db/ is a violation there. ``rdr_rows()``
    also owns the never-raise contract. A second call for the same repo
    returns the fetch already running."""
    pending = _T2_PENDING.get(repo_name)
    if pending is not None:
        return pending
    from nexus.db.t2_reads import rdr_rows  # noqa: PLC0415 — deferred: only a real T2 lookup pays for this

    project = f"{repo_name}_rdr"
    pending = _Pending(lambda: rdr_rows(project))
    _T2_PENDING[repo_name] = pending
    return pending


def _fetch_rdr_rows(repo_name: str) -> list[dict]:
    """One ``get_all`` per hook run, shared by the status and gate loaders
    (code review [24883] finding 5: two full fetches of a 1000-row project
    inside a 10s SessionStart budget).

    nexus-wozn6: the fetch runs under what is left of the run's budget. Past
    it the project reads as empty for this run, lands in :data:`_T2_LATE` so
    :func:`run` can say so, and is logged as ``rdr_hook_t2_late`` with the
    time it had taken, so the rate of degraded sessions is countable from
    ``hook.log`` instead of from transcripts. The fetch is the whole project
    because the engine has no narrower read: ``/v1/memory/list`` carries no
    content and ``/v1/memory/all`` no title filter (nexus-pxp44)."""
    if repo_name in _T2_ROWS_CACHE:
        return _T2_ROWS_CACHE[repo_name]
    rows: list[dict] = []
    try:
        pending = _start_t2_fetch(repo_name)
        rows = pending.wait(_remaining(_T2_DEADLINE_S))  # type: ignore[assignment]
    except TimeoutError:
        _T2_LATE.add(repo_name)
        _emit(
            "warning", "rdr_hook_t2_late",
            project=f"{repo_name}_rdr",
            elapsed_s=round(pending.elapsed(), 2),
            budget_s=_HOOK_BUDGET_S,
        )
    except Exception:  # noqa: BLE001 — rdr_rows never raises; this keeps the hook's never-fail contract if that ever changes
        rows = []
    _T2_ROWS_CACHE[repo_name] = rows
    return rows


def _load_all_t2_statuses(repo_name: str) -> dict[str, str]:
    """Batch-load all T2 RDR statuses. Returns ``{bare_number: status}``.

    A status record is titled either the bare number (``"42"``, ``"042"``)
    or ``"RDR-42"``; gate-latest and research records carry a suffix and
    are not statuses. Keyed on the bare number so an RDR recorded under
    two title shapes counts once (a naive keep of the ``RDR-`` shape
    double-counted). Two shapes that DISAGREE are left out entirely, the
    same rule ``_t2_rdr_status_census`` applies in ``nx rdr preamble
    rdr-audit``, which reports them as ambiguous: which shape is
    authoritative is not this loader's call to make (nexus-e19sa: no
    ranking rule between ledgers, and none between title shapes either).
    ``if "-" in title: continue`` used to drop every ``RDR-NNN``-titled
    record ([26115] #4, nexus-nc08w.1)."""
    seen: dict[str, set[str]] = {}
    try:
        for entry in _fetch_rdr_rows(repo_name):
            title = entry.get("title", "")
            m = re.match(r"^(?:RDR-)?(\d+)$", title)
            if not m:
                continue  # gate-latest, research, etc.
            key = str(int(m.group(1)))
            content = entry.get("content", "")
            for line in content.splitlines():
                stripped = line.strip()
                if stripped.startswith("status:"):
                    val = stripped.split(":", 1)[1].strip().strip('"').strip("'")
                    if val:
                        seen.setdefault(key, set()).add(val.lower())
                    break
    except Exception:  # noqa: BLE001 — the hook must never fail; an unreachable T2 degrades to an empty status map
        pass
    return {key: next(iter(vals)) for key, vals in seen.items() if len(vals) == 1}


def _load_gated_commits(repo_name: str) -> dict[str, str]:
    """``{rdr_id: commit}`` from every ``<id>-gate-latest`` T2 record that
    carries a ``commit:`` field (nexus-zbdm0)."""
    gated: dict[str, str] = {}
    try:
        for entry in _fetch_rdr_rows(repo_name):
            title = entry.get("title", "")
            if not title.endswith("-gate-latest"):
                continue
            # Keyed on the bare number ("RDR-105-gate-latest" and
            # "097-gate-latest" both normalise), matching _extract_rdr_id.
            num = re.search(r"(\d+)", title[: -len("-gate-latest")])
            if not num:
                continue
            for line in entry.get("content", "").splitlines():
                stripped = line.strip()
                if stripped.startswith("commit:"):
                    val = stripped.split(":", 1)[1].strip().strip('"').strip("'")
                    if val:
                        gated[str(int(num.group(1)))] = val
                    break
    except Exception:  # noqa: BLE001 — the hook must never fail; an unreachable T2 degrades to no gated commits
        pass
    return gated


#: The line :func:`_unchecked_fix_edits` adds when the budget ended its walk
#: before every draft RDR was checked.
_FIX_CHECK_CUT_SHORT_NOTE = (
    "(rdr-fix check cut short: the hook budget ran out before every draft RDR "
    "was checked; pointers above may be incomplete)"
)


def _unchecked_fix_edits(root: Path, rdr_files: list[Path], statuses: dict[str, str], gated: dict[str, str]) -> list[str]:
    """Lines naming draft RDRs whose file tip is past the gated commit."""
    from nexus.bounded_subprocess import run_bounded  # noqa: PLC0415 — deferred: a hook process pays its import cost on every invocation, and a module-scope import of this pulls structlog + ~231 modules (measured on verification_config: 14ms/106 -> 62-84ms/337). Deferred, it is paid only when we actually spawn

    lines: list[str] = []
    cut_short = False
    for path in rdr_files:
        rid = _extract_rdr_id(path)
        if rid is None:
            continue
        key = str(int(rid))
        gated_norm = {str(int(k)): v for k, v in gated.items() if k.isdigit()}
        if key not in gated_norm:
            continue
        # nexus-u1jxt.7: a closed or accepted RDR with no T2 status row (or
        # two disagreeing shapes, which the loader drops) defaulted to draft
        # and was told to run rdr-fix. The file's own frontmatter is the
        # fallback, and only a file that states no status defaults.
        status = statuses.get(rid, statuses.get(key))
        if status is None:
            status = _file_frontmatter(path).get("status") or "draft"
        if status.lower() not in ("draft", "open"):
            continue
        left = _remaining(10.0)
        if left <= 0:
            cut_short = True
            break  # nexus-wozn6: out of budget; a pointer missed this session beats a cancelled hook
        try:
            tip = run_bounded(
                ["git", "-C", str(root), "log", "-1", "--format=%h", "--", str(path)],
                timeout=left,
            ).stdout.strip()
        except subprocess.TimeoutExpired:
            cut_short = True  # this file was not checked either
            continue
        except (OSError, subprocess.SubprocessError):
            continue
        if not tip:
            continue  # untracked or uncommitted file: unknown, not "past the gate"
        commit = gated_norm[key]
        n = min(len(tip), len(commit))
        if n >= 7 and tip[:n] == commit[:n]:
            continue
        lines.append(
            f"RDR-{rid}: edits since the gated commit {commit}; run /conexus:rdr-fix {rid} "
            "before re-gating (fix check first)."
        )
    if cut_short:
        # A filter that selects nothing looks exactly like a quiet success
        # (nexus-e19sa); without this line a budget that ran out here reads
        # as a tree with no unchecked edits.
        lines.append(_FIX_CHECK_CUT_SHORT_NOTE)
    return lines


def _rdr_status_counts(repo_name: str, preloaded: dict[str, str] | None = None) -> Counter[str]:
    """Status counts from T2. Uses preloaded statuses if available."""
    statuses = preloaded if preloaded is not None else _load_all_t2_statuses(repo_name)
    return Counter(statuses.values())


def _rdr_dir(root: Path) -> Path:
    """Resolve RDR directory from .nexus.yml or fall back to docs/rdr."""
    config_path = root / ".nexus.yml"
    if config_path.exists():
        try:
            import yaml  # noqa: PLC0415 — deferred: only a repo with a .nexus.yml pays for this
            with config_path.open() as fh:
                data = yaml.safe_load(fh) or {}
            paths = data.get("indexing", {}).get("rdr_paths", [])
            if isinstance(paths, str):  # nexus-u1jxt.10: a scalar resolved to root / its first character
                paths = [paths]
            if paths:
                return root / paths[0]
        except Exception:  # noqa: BLE001 — the hook must never fail; a malformed .nexus.yml falls back to the default path
            pass
    return root / "docs" / "rdr"


def _rdr_files(rdr_dir: Path) -> list[Path]:
    """The RDR documents directly under *rdr_dir* -- non-recursive, so
    ``docs/rdr/post-mortem/`` (a separate document set) never carries an
    RDR status, with the index/template/agents files excluded by name."""
    return [
        p for p in rdr_dir.glob("*.md")
        if p.name.lower() not in _EXCLUDE_FILES and _extract_rdr_id(p) is not None
        and not _is_companion(p)  # nexus-u1jxt.7: a companion is not an RDR
    ]


def _indexed_document_count(rdr_dir: Path) -> int:
    """Every markdown file the repo indexer registers under *rdr_dir*: the
    same recursive walk as ``nx index repo`` (joint/ and post-mortem/
    included, README and AGENTS included). nexus-owna8: the hook reported
    the RDR count against a collection holding this count, and the two
    numbers (215 vs 298) read as a partial index."""
    return sum(1 for p in rdr_dir.rglob("*.md") if p.is_file() and not p.is_symlink())


def run(payload: dict | None) -> HookResult:  # noqa: ARG001 — this hook takes no stdin payload; see the module docstring
    """Run the RDR SessionStart summary.

    Mirrors the script's ``main()`` exactly: the same early-exit points
    (no repo root, no RDR dir, no RDR files) now return ``HookResult()``
    (``stdout=None``) instead of printing nothing and calling
    ``sys.exit(0)``, and the accumulated ``print()`` lines become one
    ``"\\n".join(...)`` string -- see the module docstring's "Structural
    adaptation" section for why the join point is exactly there.

    nexus-wozn6: every network leg runs under one budget,
    :data:`_HOOK_BUDGET_S` from here, so the run returns before hooks.json's
    10 s cap instead of being cancelled with nothing printed.
    """
    global _run_deadline
    _run_deadline = time.monotonic() + _HOOK_BUDGET_S
    try:
        return _run()
    finally:
        _run_deadline = None
        _T2_PENDING.clear()


def _run() -> HookResult:
    root = _repo_root()
    if root is None:
        return HookResult()

    rdr_dir = _rdr_dir(root)
    if not rdr_dir.exists():
        return HookResult()

    rdr_files = _rdr_files(rdr_dir)
    if not rdr_files:
        return HookResult()

    repo_name = _repo_name(root)
    # Logging is configured BEFORE any worker thread starts: a worker that
    # logs through an unconfigured structlog writes to stdout, the hook's
    # decision channel.
    configure_hook_logging()
    # The slowest leg starts first and runs beside the catalog and T3 legs,
    # so it has the whole budget rather than what they leave over. A failure
    # to start it (an import error) must not cost the rest of the summary:
    # _fetch_rdr_rows retries the start inside its own never-fail guard.
    try:
        _start_t2_fetch(repo_name)
    except Exception:  # noqa: BLE001 — never-fail hook contract
        pass
    rdr_collection = _resolve_rdr_collection(root)
    indexed: bool | None = _collection_exists(rdr_collection) if rdr_collection else False

    statuses = _load_all_t2_statuses(repo_name)
    counts = _rdr_status_counts(repo_name, statuses)
    documents = _indexed_document_count(rdr_dir)
    if counts:
        breakdown = ", ".join(f"{n} {s}" for s, n in counts.most_common())
        status_info = f"{documents} documents ({len(rdr_files)} RDRs: {breakdown})"
    else:
        status_info = f"{documents} documents ({len(rdr_files)} RDRs)"

    lines: list[str] = []
    if indexed:
        lines.append(f"RDR: {status_info}, indexed in {rdr_collection}")
    else:
        if indexed is None:
            # The T3 probe did not answer and the listing could not: the hook
            # does not know, so it must not tell the user to re-index.
            lines.append(
                f"RDR: {status_info} in {rdr_dir.relative_to(root)}; whether it is "
                "indexed is unknown (the T3 check did not answer within the hook budget)."
            )
        else:
            # nexus-3o4lt: the remedy is the REPO indexer. This line used to
            # say ``nx index rdr <root>``, which registered every RDR under
            # the curator owner with an absolute path; on the work box that
            # produced 198 such rows that no owner-scoped reader could see.
            # ``nx index repo`` walks docs/rdr under the repo owner. The
            # single-file ``nx index rdr <file>`` now lands there too, but the
            # whole-tree remedy is the repo index.
            lines.append(f"RDR: {status_info} in {rdr_dir.relative_to(root)} but NOT indexed.")
        if _RESOLUTION_FAILURES:
            # The verdict may be the hook's own failure, not the tree's state.
            try:
                where = f"; log: {_hook_log_path()}"
            except Exception:  # noqa: BLE001 — Path.home() can raise in a scrubbed container
                where = ""
            lines.append(f"     (resolution failed: {'; '.join(_RESOLUTION_FAILURES)}{where})")
        if indexed is not None:
            lines.append(f"     Run: nx index repo {root}")

    if repo_name in _T2_LATE:
        lines.append(f"     {_T2_LATE_NOTE}")

    lines.extend(_unchecked_fix_edits(root, rdr_files, statuses, _load_gated_commits(repo_name)))

    return HookResult(stdout="\n".join(lines))
