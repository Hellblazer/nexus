#!/usr/bin/env python3
# SPDX-License-Identifier: AGPL-3.0-or-later
# Copyright (c) 2026 Hal Hildebrand. All rights reserved.
"""RDR-143 detached upgrade action for the plugin<->CLI version lockstep.

Fire-and-forget worker dispatched by ``version_lockstep_hook.py`` after it
detects skew. Runs AFTER the current session has already started against
the old binary, so the upgrade takes effect next session (CA-4). It owns
the editable gate and the marker write.

Flow:
  1. Editable gate (CA-3): only act on a real ``uv tool install`` of
     conexus. A dev/editable tree has no uv-tool receipt -> SKIP, never
     clobber.
  2. No-op fast path: if ``nx --version`` already equals the target, just
     write the marker.
  3. Two-command safe upgrade (CA-2), in strict order:
       a. ``uv tool upgrade conexus``  (binary, extras-preserving:
          keeps the ``[local]`` extra; raw ``uv tool install`` /
          ``--reinstall`` / ``--force`` would strip it and reintroduce
          the 5.6.2 local-search P0 -- never use them).
       b. ``nx upgrade``               (migrations only, RDR-076;
          idempotent + flock-serialized; does NOT touch the binary).
          Started DETACHED by this script (nexus-qytet): an upgrade-time
          rebuild (the PDF carry-forward, the installer rebuild rung, about
          0.8 GB) outlasts any subprocess timeout, and a kill mid-build
          wastes the download. PLAIN ``nx upgrade``, not ``--auto``: --auto
          skips plugin convergence, the generated-agent and hooks refresh,
          the repos.json migration, beads PRIME and the engine install, and
          this action is where those run. See :func:`start_background_upgrade`.
  4. Marker on confirmed success only: re-read ``nx --version`` and write
     the marker iff it now equals the target. Any failure leaves the
     marker stale so the next session re-nudges and retries.

nexus-konsk (P0, 2026-09-11): a FIFTH entry point, gated by the sentinel
target ``_REF_DRIFT_SENTINEL`` (``version_lockstep_hook.detect_ref_drift``
dispatches it, never a real version string). Bypasses steps 2-4 above --
a same-version plugin-only cut (RDR-197) moves no CLI version, so there
is nothing to compare or write a marker for -- and runs ONLY ``nx
upgrade`` (still behind gate 1's editable/generation check), which is
where ``nexus.plugin_lockstep.converge_plugins``'s ref-drift step
(refreshes the marketplace over the network, re-confirms, then
uninstalls+reinstalls on a genuine mismatch) actually lives. See
``_run_nx_upgrade_for_ref_drift`` and ``main``'s ``ref_drift_only``
branch.

nexus-konsk (fix round 2, 2026-09-11): the hook now dispatches this
sentinel branch AT MOST ONCE per distinct (plugin, target sha) drift --
see ``version_lockstep_hook.write_ref_drift_marker``'s docstring. As a
direct consequence, every ``log_event("ref_drift_upgrade_result", ...)``
call below now fires at most once for a given drift rather than once
per session while it keeps failing, so the ``remedy=`` field each
failure outcome now carries is genuinely "logged once with the remedy",
not repeated noise.

Stdlib-only; the conexus package is not importable here. No structlog
under a bare interpreter -> the NX_HOOK_DEBUG stderr convention.

The interpreter is inherited, not resolved. ``version_lockstep_hook.py``
spawns this script with its OWN ``sys.executable`` (RDR-215 bead
nexus-q02nx.22, which deleted the ``_run_python_hook.sh`` launcher that
used to resolve one per dispatch). That is safe without a second probe
because the hook calls ``_interpreter.reexec_if_needed()`` at module
scope and then exits on ``sys.version_info < (3, 12)`` immediately after,
so nothing in it reaches a dispatch under an unvetted interpreter -- on
every path, including the one where resolution fails and
``reexec_if_needed()`` returns having changed nothing.
"""
from __future__ import annotations

import sys

if sys.version_info < (3, 12):
    sys.stderr.write(
        f"ERROR: conexus plugin hook requires Python 3.12+, got {sys.version.split()[0]}\n"
    )
    sys.exit(1)

import os
import re
import subprocess
from pathlib import Path
from typing import IO

# PATH-only lookup (finding A, nexus-f9bgu.36): on Windows a bare name is
# searched in the cwd first, and this detached action inherits the project cwd.
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import _cli_lookup  # noqa: E402 -- must follow the sys.path insert
import _endpoint_resolve  # noqa: E402 -- must follow the sys.path insert
import _exec_path  # noqa: E402 -- must follow the sys.path insert

which_off_cwd = _exec_path.which_off_cwd


def resolve_nx() -> str | None:
    """``nx`` by the launcher's lookup: the generation layout, then PATH.

    The hook that starts this action decides "is there a CLI" the same way
    (``version_lockstep_hook.cli_present``). A PATH-only lookup here disagreed
    with it on a machine whose install is newer than the application's PATH:
    the hook said an upgrade was dispatched and this action found no ``nx``,
    every session (RDR-228 Phase 1 review)."""
    return _cli_lookup.resolve("nx")

DEBUG = os.environ.get("NX_HOOK_DEBUG", "0") == "1"

def _env_int(name: str, default: int) -> int:
    """Parse an int env var; fall back to *default* on anything malformed.

    Module-level so a bad env value cannot raise at import time, before
    ``main()``'s fail-safe guard runs (the action must never crash).
    """
    try:
        return int(os.environ.get(name, str(default)))
    except (TypeError, ValueError):
        return default


# uv tool upgrade can be network-bound; it runs detached so a long wait is
# harmless, but bound it. nx upgrade is migration-only and fast.
_UV_TIMEOUT = _env_int("NX_LOCKSTEP_UV_TIMEOUT", 300)
#: Step 3a's ``nx self install`` under the generation layout. A first install
#: downloads about 0.8 GB, which a slow link does not finish in
#: ``_UV_TIMEOUT``. It is bounded, not detached, because step 4 needs its
#: result (the binary must be there to confirm the version). The action itself
#: runs detached from the session (``version_lockstep_hook.dispatch_action``:
#: Popen with ``start_new_session``), so a long wait blocks nothing. 1800 s is
#: 0.8 GB at about 0.5 MB/s; beyond that the install is retried by the next
#: session's nudge.
_SELF_INSTALL_TIMEOUT = _env_int("NX_LOCKSTEP_SELF_INSTALL_TIMEOUT", 1800)
#: The synchronous ``nx upgrade`` budget. Used only where the action needs the
#: upgrade's output or its exit before it can go on: the ref-drift branch, and
#: step 3's fallback when the detached child cannot be started. The normal
#: step 3 no longer waits on ``nx upgrade`` (nexus-qytet).
_NX_UPGRADE_TIMEOUT = _env_int("NX_LOCKSTEP_NX_TIMEOUT", 120)
#: How long to wait for the detached ``nx upgrade`` before calling it started.
#: A child that dies inside this window (an `nx` that rejects the invocation, a
#: missing dependency) exits nonzero in well under a second; one still running
#: after it is an upgrade in progress.
_UPGRADE_START_WAIT_S = 2.0
#: The child's log is rotated to ``<name>.1`` when a new child starts and finds
#: it at this size.
_UPGRADE_LOG_MAX_BYTES = 1024 * 1024
#: Windows, decided once so a test can steer the spawn path without patching
#: ``os.name`` (pathlib reads that on every ``Path()``).
_IS_WINDOWS = os.name == "nt"
#: Windows creation flags, by value (the constants exist only in
#: ``subprocess`` on Windows). Same set, same reasons as
#: ``nexus.hooks.upgrade_auto``: CREATE_NO_WINDOW rather than DETACHED_PROCESS
#: (a console grandchild of a detached process, e.g. ``nx.exe``'s trampoline,
#: would flash a visible window; the two are mutually exclusive),
#: CREATE_NEW_PROCESS_GROUP so a Ctrl-C aimed at this action stays off the
#: child, CREATE_BREAKAWAY_FROM_JOB so a kill-on-close job does not end it.
#: Untested on a real Windows host: the flags are pinned by a recording Popen.
_CREATE_NEW_PROCESS_GROUP = 0x00000200
_CREATE_NO_WINDOW = 0x08000000
_CREATE_BREAKAWAY_FROM_JOB = 0x01000000
#: nexus-konsk: the sentinel ``version_lockstep_hook.py`` passes as the
#: "target version" argv for a ref-drift-only dispatch. Same literal
#: under the same name in both files (the hook cannot import this module
#: -- both are bare stdlib scripts, see the hook's own module docstring);
#: ``tests/hooks/test_version_lockstep_hook.py::
#: TestRefDriftSentinelMatchesAction`` pins the two together.
_REF_DRIFT_SENTINEL = "__ref_drift__"
#: nexus-konsk fix round 2: the remedy text logged alongside every
#: ref-drift FAILURE outcome. The hook now dispatches at most once per
#: distinct drift (see this module's own docstring above), so this line
#: appears at most once for a given drift rather than once per session.
_REF_DRIFT_REMEDY = (
    "run `nx upgrade` manually once the blocker (network, permissions, "
    "or a `claude` CLI error) is resolved -- a further genuine ref move "
    "will also re-dispatch automatically"
)
# Matches a leading dotted-numeric core (X.Y.Z) plus an optional separated
# suffix. Nexus ships plain X.Y.Z release tags to users, so a bare
# pre-release like "5.7.0a1" (no separator before the suffix) is out of
# scope and would parse as its numeric core.
_VERSION_RE = re.compile(r"(\d+\.\d+\.\d+(?:[.\-+][0-9A-Za-z.\-]+)?)")


def debug(msg: str) -> None:
    """Print a debug line to stderr when NX_HOOK_DEBUG=1."""
    if DEBUG:
        print(f"[version-lockstep-action] {msg}", file=sys.stderr)


def marker_path() -> Path:
    """Per-user lockstep marker (see version_lockstep_hook.marker_path).

    In the config dir the CLI uses (``NEXUS_CONFIG_DIR``, else
    ``~/.config/nexus``; the stdlib mirror in ``_endpoint_resolve``), so the
    hook, this action and ``nexus.upgrade_ladder.preconditions`` read and
    write one file (nexus-f9bgu)."""
    override = os.environ.get("NX_LOCKSTEP_MARKER")
    if override:
        return Path(override)
    return _endpoint_resolve.default_config_dir() / "cli_lockstep_marker"


def log_path() -> Path:
    """Per-user durable log of every actual venv-mutating lockstep attempt.

    nexus-otnvr item 4: a live 7.4.0 session found the tool venv already
    swapped to PyPI at session start (dist-info mtime 00:17, MCP servers
    spawning 00:17:31 — the swap RACED the server boot) with no way to tell
    afterward that this action had run, let alone when or with what
    outcome. ``debug()`` above is gated behind ``NX_HOOK_DEBUG=1`` and
    ``dispatch_action()`` (the hook) launches this script with stdout/stderr
    both DEVNULL'd (fire-and-forget, CA-4) — so nothing this script prints
    is EVER visible by default, debug flag or not. This log is the loud,
    always-on, un-gated record: one line per actual `uv tool upgrade`
    attempt and one line per its outcome, appended regardless of
    NX_HOOK_DEBUG. ``NX_LOCKSTEP_LOG`` overrides the location for tests,
    mirroring ``NX_LOCKSTEP_MARKER``.
    """
    override = os.environ.get("NX_LOCKSTEP_LOG")
    if override:
        return Path(override)
    return _endpoint_resolve.default_config_dir() / "lockstep.log"


#: code-review Important-3 (2026-08-08): a bare unbounded `open("a")`
#: append has no rotation. In practice this is ~2 lines per infrequent
#: swap event, but an explicit cap costs one `stat()` and is cheap
#: insurance. Deliberately NOT `logging.handlers.RotatingFileHandler` —
#: this hook keeps a minimal footprint (same posture as
#: ``uv_receipt_present``'s docstring: it avoids importing the ``nexus``
#: package itself). A manual single-generation rotate (active file ->
#: `.1`, overwriting any prior `.1`) via `Path.replace()` (an atomic
#: POSIX rename) is enough. ``NX_LOCKSTEP_LOG_MAX_BYTES`` overrides the
#: threshold for tests.
_DEFAULT_LOG_MAX_BYTES = 1_000_000


def _log_max_bytes() -> int:
    try:
        return int(os.environ.get("NX_LOCKSTEP_LOG_MAX_BYTES", str(_DEFAULT_LOG_MAX_BYTES)))
    except (TypeError, ValueError):
        return _DEFAULT_LOG_MAX_BYTES


def log_event(event: str, **fields: str) -> None:
    """Append one human-readable, timestamped line — ALWAYS, never gated
    behind NX_HOOK_DEBUG (that is precisely the gap this closes). Best
    effort: an unwritable config dir must never break the action."""
    import datetime  # noqa: PLC0415 — stdlib, deferred for startup cost

    ts = datetime.datetime.now(datetime.timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
    kv = " ".join(f"{k}={v}" for k, v in fields.items())
    line = f"{ts} {event} {kv}".rstrip() + "\n"
    try:
        path = log_path()
        path.parent.mkdir(parents=True, exist_ok=True)
        try:
            if path.stat().st_size > _log_max_bytes():
                path.replace(path.with_name(path.name + ".1"))
        except FileNotFoundError:
            pass
        with path.open("a", encoding="utf-8") as f:
            f.write(line)
    except OSError as exc:
        debug(f"log_event failed (non-fatal): {exc}")


#: The generation root's env override and default, DUPLICATED FROM
#: ``nexus.install_layout`` by necessity: this hook runs under a bare python3
#: (whatever ``_interpreter.reexec_if_needed()`` resolved for the lockstep
#: hook that spawned it -- the generation python, else python3.13 / python3.12
#: by name, else bare) and cannot import the conexus package -- the same
#: constraint that already
#: forces ``uv_receipt_present`` to be inline rather than shared.
#:
#: Duplication that cannot be removed can still be PINNED.
#: ``TestInlineLayoutKnowledgeMatchesThePackage`` runs in the normal test
#: interpreter, imports both halves, and fails if they drift. Without that, the
#: hook would eventually look for the layout somewhere it is not -- and the
#: symptom would be this exact bug again: a silent no-op.
TOOLS_DIR_ENV = "NX_TOOLS_DIR"
_DEFAULT_TOOLS_SUBPATH = (".local", "share", "nexus", "tools")


def default_tools_dir() -> Path:
    """The generation root, honouring the env override."""
    override = os.environ.get(TOOLS_DIR_ENV)
    if override:
        return Path(override)
    return Path.home().joinpath(*_DEFAULT_TOOLS_SUBPATH)


def generation_install_present() -> bool:
    """True iff this box has a working side-by-side generation install.

    ``<tools>/current`` must resolve to a real directory. A dangling pointer is
    NOT a managed install: fail-safe to False, matching
    ``uv_receipt_present``'s posture, so a broken layout is never something the
    hook tries to upgrade through.
    """
    try:
        current = default_tools_dir() / "current"
        return current.is_dir()
    except OSError as exc:  # unreadable HOME, permissions
        debug(f"generation probe failed: {exc}")
        return False


def uv_receipt_present() -> bool:
    """True iff conexus was installed via ``uv tool`` (receipt present).

    Self-contained uv-receipt check. This used to mirror
    ``init.py:_uv_receipt_path``, but that helper was removed in RDR-174 P1.3
    (the embedder-picker extra-add path it served is gone); this is now the sole
    instance. Intentionally NOT imported from nexus.commands.init (the bare hook
    interpreter cannot import the conexus package) and NOT extracted to a shared
    nexus/ helper (the two consumers run in different interpreters: the package
    vs this bare hook).

    Absence of the receipt means a dev/editable tree (or no uv): SKIP, so we
    never clobber a developer checkout. All edge cases fail-safe to False.
    """
    uv = which_off_cwd("uv")
    if uv is None:
        return False
    try:
        out = subprocess.run(
            [uv, "tool", "dir"],
            # uv prints the directory as UTF-8 on every platform (nexus-ikoqj).
            capture_output=True, text=True, encoding="utf-8", errors="replace", timeout=10, check=True,
        )
    except (subprocess.SubprocessError, OSError) as exc:
        debug(f"`uv tool dir` failed: {exc}")
        return False
    receipt = Path(out.stdout.strip()) / "conexus" / "uv-receipt.toml"
    return receipt.is_file()


def installed_nx_version() -> str | None:
    """Return the installed CLI version parsed from ``nx --version``.

    ``nx --version`` prints e.g. ``nx, version 5.7.0``. Returns None when nx
    is absent or the output cannot be parsed.
    """
    nx = resolve_nx()
    if nx is None:
        return None
    try:
        out = subprocess.run(
            [nx, "--version"],
            # nx writes UTF-8 to its pipes on Windows (nexus.cli reconfigures at import).
            capture_output=True, text=True, encoding="utf-8", errors="replace", timeout=15, check=True,
        )
    except (subprocess.SubprocessError, OSError) as exc:
        debug(f"`nx --version` failed: {exc}")
        return None
    m = _VERSION_RE.search(out.stdout)
    return m.group(1) if m else None


def _version_core(version: str) -> tuple[int, ...] | None:
    """Parse the leading dotted-numeric core into a comparable tuple.

    "5.7.0" -> (5, 7, 0). Returns None when no numeric core is present.
    Used for ordering only; pre-release suffixes are ignored (see _VERSION_RE).
    """
    m = re.match(r"(\d+(?:\.\d+)*)", version.strip())
    if not m:
        return None
    return tuple(int(p) for p in m.group(1).split("."))


def satisfies(installed: str | None, target: str) -> bool:
    """True when the installed CLI is at least the target plugin version.

    Lockstep cares only that the CLI is not OLDER than the plugin (an older
    CLI lacks migrations/features the plugin expects). A CLI that equals or
    exceeds the target is in lockstep. This also breaks the downgrade loop:
    if the plugin ref is pinned back below the installed CLI, `uv tool
    upgrade` cannot reach the older target, so a strict-equality confirm
    would never write the marker and the nudge would fire forever. With a
    >= check the action records lockstep and goes quiet.
    """
    if installed is None:
        return False
    inst, tgt = _version_core(installed), _version_core(target)
    if inst is None or tgt is None:
        return installed == target  # conservative fallback
    return inst >= tgt


#: Standard output of the last :func:`run_cmd` that exited 0 (empty after a failure), so the caller
#: can read a line the command printed. Only ``nx self install``'s PDF line is read from it.
_last_stdout: str = ""

#: The start of the line `nx self install` prints when it builds without PDF support because this
#: Windows machine cannot load the stack (nexus-vyy65; src/nexus/pdf_stack.py, drop_pdf_on_runtime_gap).
_PDF_SLIM_PREFIXES: tuple[str, ...] = ("PDF support will not be installed", "PDF support will NOT be installed")


def pdf_slim_line(stdout: str) -> str | None:
    """The slim-build line in *stdout*, or None."""
    for line in stdout.splitlines():
        if line.strip().startswith(_PDF_SLIM_PREFIXES):
            return line.strip()
    return None


def run_cmd(cmd: list[str], timeout: int = 300) -> bool:
    """Run a command; return True on exit 0, False otherwise. Never raises.

    ``cmd[0]`` is resolved off the cwd and spawned by absolute path: ``nx`` by
    the launcher's lookup (:func:`resolve_nx`), anything else on PATH alone. A
    name that does not resolve is a failed command, never a cwd lookup."""
    global _last_stdout
    _last_stdout = ""
    exe = resolve_nx() if cmd[0] == "nx" else which_off_cwd(cmd[0])
    if exe is None:
        debug(f"{cmd[0]} not found")
        return False
    try:
        result = subprocess.run(
            # exe is nx, uv or git: all write UTF-8 on every platform (nexus-ikoqj).
            [exe, *cmd[1:]], capture_output=True, text=True, encoding="utf-8", errors="replace",
            timeout=timeout,
        )
        if DEBUG and result.stdout:
            debug(f"{cmd[0]} stdout: {result.stdout[:500]}")
        if result.returncode != 0:
            debug(f"{' '.join(cmd)} exited {result.returncode}: {result.stderr[-500:]}")
            return False
        _last_stdout = result.stdout or ""
        return True
    except (subprocess.SubprocessError, OSError) as exc:
        debug(f"{' '.join(cmd)} raised: {exc}")
        return False


def upgrade_log_path() -> Path:
    """``<config>/logs/lockstep-upgrade.log``: the detached ``nx upgrade``'s stderr.

    Its own file rather than ``upgrade-auto-child.log``: that one is rotated by
    ``nexus.hooks.upgrade_auto`` when ITS child starts, and two writers rotating
    one file lose lines. The config dir is the one the CLI uses
    (:func:`_endpoint_resolve.default_config_dir`)."""
    return _endpoint_resolve.default_config_dir() / "logs" / "lockstep-upgrade.log"


def _open_upgrade_log() -> IO[bytes] | None:
    """Open :func:`upgrade_log_path` for append (rotating at 1 MiB); None when it
    cannot be opened, so a log problem never stops the upgrade."""
    try:
        path = upgrade_log_path()
        path.parent.mkdir(parents=True, exist_ok=True)
        try:
            if path.stat().st_size >= _UPGRADE_LOG_MAX_BYTES:
                os.replace(path, path.with_name(path.name + ".1"))
        except FileNotFoundError:
            pass
        return path.open("ab")
    except (OSError, RuntimeError):
        return None


#: The filesystem root (the current drive's root on Windows): where a detached
#: child starts, by the nexus-x3m8e rule (``nexus/util/stable_cwd.py``; this file
#: cannot import nexus). A session's cwd is often a disposable worktree, and a
#: child that outlives it would fail every ``os.getcwd()`` once it is deleted.
_STABLE_CWD: str = os.path.abspath(os.sep)

#: How the last :func:`start_background_upgrade` call ended: ``running`` (the
#: child outlived the start window) or ``finished`` (it exited 0 inside it).
_last_start_state: str = ""


def _stable_child_env(env: dict[str, str]) -> dict[str, str]:
    """*env* for a child started in :data:`_STABLE_CWD`: a relative
    ``NEXUS_CONFIG_DIR`` made absolute while this cwd still names it, and
    ``NEXUS_REPO_ROOT`` set to this cwd (``config.load_config`` reads it where
    it used to read the cwd), unless already set or the cwd is gone."""
    out = dict(env)
    raw = out.get("NEXUS_CONFIG_DIR", "").strip()
    if raw and not os.path.isabs(raw):
        try:
            out["NEXUS_CONFIG_DIR"] = os.path.abspath(raw)
        except OSError:
            pass
    if not out.get("NEXUS_REPO_ROOT"):
        try:
            out["NEXUS_REPO_ROOT"] = os.getcwd()
        except OSError:
            pass
    return out


def _popen_detached(argv: list[str], env: dict[str, str], stderr: IO[bytes] | int) -> subprocess.Popen:
    """Popen *argv* outside this action's session (POSIX) or process group and
    job (Windows). Raises OSError when no attempt could start it."""
    # Binary pipes only (null device and a file), so there is no text decoding
    # to name an encoding for. The kwargs are spelled out at each call: the
    # subprocess-encoding census refuses a Popen it cannot read.
    if not _IS_WINDOWS:
        return subprocess.Popen(  # noqa: S603 -- argv list, resolved binary, no shell
            argv, stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL, stderr=stderr,
            close_fds=True, env=env, start_new_session=True, cwd=_STABLE_CWD,
        )
    base = _CREATE_NO_WINDOW | _CREATE_NEW_PROCESS_GROUP
    try:
        return subprocess.Popen(  # noqa: S603
            argv, stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL, stderr=stderr,
            close_fds=True, env=env, creationflags=base | _CREATE_BREAKAWAY_FROM_JOB, cwd=_STABLE_CWD,
        )
    except OSError:
        # A job that forbids breakaway refuses the flag; start without it.
        return subprocess.Popen(  # noqa: S603
            argv, stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL, stderr=stderr,
            close_fds=True, env=env, creationflags=base, cwd=_STABLE_CWD,
        )


def start_background_upgrade() -> int | None:
    """Start plain ``nx upgrade`` DETACHED; return its pid, or None when it did not start.

    nexus-qytet: step 3b used to wait on ``nx upgrade`` under a 120 s timeout.
    An upgrade-time rebuild (about 0.8 GB) does not fit, and the timeout KILLED
    it mid-build; only the next SessionStart's detached ``nx upgrade --auto``
    retried it, from scratch.

    The child is PLAIN ``nx upgrade``, not ``--auto`` (which the SessionStart
    hook runs): ``--auto`` skips plugin convergence, the generated-agent and
    hooks refresh, the repos.json migration, beads PRIME, the dimension
    advisory and the engine install, and this action is where those run. The
    child gets a session of its own, stdin and stdout on the null device and
    stderr appended to :func:`upgrade_log_path`. It starts in the filesystem
    root with ``NEXUS_REPO_ROOT`` naming this process's cwd (nexus-x3m8e: a
    child that outlives a deleted worktree cwd would fail; the old synchronous
    run was short-lived and kept the cwd). The fresh install's ``bin`` leads its
    ``PATH``, so a box whose application ``PATH`` lacks the install does not run
    the child's own ``nx`` subcommands from an older tree.

    "Started" is checked: the child is waited on for
    :data:`_UPGRADE_START_WAIT_S`, and one that has exited NONZERO by then (it
    rejected the invocation, or crashed on import) is not started. A child that
    exited 0 inside the window already finished, which counts. None on a missing
    ``nx``, a Popen ``OSError`` or an immediate nonzero exit: the caller falls
    back to the bounded synchronous run.
    """
    nx = resolve_nx()
    if nx is None:
        debug("nx not found; cannot start a detached upgrade")
        return None
    global _last_start_state
    _last_start_state = ""
    env = _stable_child_env(dict(os.environ))
    env["PATH"] = os.path.dirname(nx) + os.pathsep + env.get("PATH", "")
    log = _open_upgrade_log()
    try:
        try:
            proc = _popen_detached([nx, "upgrade"], env, log if log is not None else subprocess.DEVNULL)
        except OSError as exc:
            debug(f"detached nx upgrade could not start: {exc}")
            return None
    finally:
        if log is not None:
            log.close()  # the child holds its own descriptor
    try:
        rc = proc.wait(timeout=_UPGRADE_START_WAIT_S)
    except subprocess.TimeoutExpired:
        _last_start_state = "running"
        return proc.pid  # still running: an upgrade in progress
    if rc != 0:
        debug(f"detached nx upgrade exited {rc} at once; not started (see {upgrade_log_path()})")
        return None
    _last_start_state = "finished"
    return proc.pid


def write_marker(version: str) -> None:
    """Record *version* as the confirmed-in-lockstep CLI version."""
    path = marker_path()
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(version, encoding="utf-8")
    debug(f"wrote marker {path} = {version}")


def _run_nx_upgrade_for_ref_drift(timeout: int) -> None:
    """The nexus-konsk ref-drift-only branch's sole action: run
    ``nx upgrade`` and log what its own plugin-lockstep step reported
    (``nexus.plugin_lockstep.render``'s own text), never a hard
    success/failure signal back to the caller -- ``nx upgrade``'s own
    philosophy is that a failed plugin reinstall never fails ``nx
    upgrade`` itself (the reason is printed instead), so this is a
    durable record of the outcome, not a gate. No CLI binary upgrade and
    no marker write: ref drift is not a CLI-version fact.

    Synchronous under :data:`_NX_UPGRADE_TIMEOUT` (120 s) on purpose: it runs
    plugin convergence and parses three markers out of the output, which a
    detached child cannot give it. KNOWN GAP (nexus-qytet): an upgrade-time
    carry or installer rebuild that is owed at that moment is cut off by the
    cap and retried by the next SessionStart's ``nx upgrade --auto``."""
    nx = resolve_nx()
    try:
        if nx is None:
            raise FileNotFoundError("nx not found")
        result = subprocess.run(
            [nx, "upgrade"], capture_output=True, text=True, encoding="utf-8", errors="replace",
            timeout=timeout,
        )
    except (subprocess.SubprocessError, OSError) as exc:
        debug(f"nx upgrade raised for ref-drift reinstall: {exc}")
        log_event(
            "ref_drift_upgrade_result", outcome="nx_upgrade_raised",
            error=str(exc)[:200], remedy=_REF_DRIFT_REMEDY,
        )
        return
    text = (result.stdout or "") + (result.stderr or "")
    if result.returncode != 0:
        debug(f"nx upgrade exited {result.returncode} for ref-drift reinstall")
        log_event(
            "ref_drift_upgrade_result", outcome="nx_upgrade_exit_nonzero",
            rc=str(result.returncode), remedy=_REF_DRIFT_REMEDY,
        )
        return
    # Text markers from nexus.plugin_lockstep.render's own case statement
    # (`ref_moved` / `ref_check_failed`) -- best-effort parse of what `nx
    # upgrade` printed, not a second source of truth.
    if "picked up a plugin-only release" in text:
        log_event("ref_drift_upgrade_result", outcome="ref_moved")
    elif "reinstall failed" in text:
        log_event(
            "ref_drift_upgrade_result", outcome="ref_check_failed",
            remedy=_REF_DRIFT_REMEDY,
        )
    else:
        log_event("ref_drift_upgrade_result", outcome="ran_no_drift_confirmed")


def main(argv: list[str]) -> None:
    """Perform the gated, ordered, confirmed upgrade. Always fail-safe."""
    try:
        if len(argv) < 2 or not argv[1].strip():
            debug("no target version argument; nothing to do")
            return
        target = argv[1].strip()
        ref_drift_only = target == _REF_DRIFT_SENTINEL

        # 1. Editable gate first: never touch a dev/editable tree.
        # GATE 1, nexus-utpuw.15. This used to be `uv_receipt_present()` alone,
        # which is False FOREVER under the generation layout -- so the whole
        # auto-upgrade no-opped silently: no error, no nudge-loop escape, the
        # marker stayed stale and the hook re-nudged forever while this did
        # nothing.
        #
        # Both shapes are managed installs. A box that has not migrated is
        # still upgradable through uv, and .7 leaves boxes in that state
        # deliberately until the legacy tree has zero holders.
        generation = generation_install_present()
        if not generation and not uv_receipt_present():
            debug("no generation layout and no uv-tool receipt (dev tree); skipping")
            return

        if ref_drift_only:
            # nexus-konsk: a same-version plugin-only cut (RDR-197) needs
            # only the plugin-lockstep step inside `nx upgrade`
            # (nexus.plugin_lockstep.converge_plugins's ref-drift check,
            # which refreshes the marketplace over the network and
            # re-confirms before acting) -- never a CLI binary upgrade,
            # since the wheel version is unchanged by construction. Skip
            # gates 2-4 below (all keyed on a CLI version target) entirely.
            #
            # Blast radius: RDR-143 CA-4 accepted that a detached action
            # completes AFTER the current session has already started
            # against the old tree, so this delivers on the NEXT session,
            # not this one -- see dispatch_ref_drift_action's docstring in
            # version_lockstep_hook.py for the full citation. No marker
            # write here: the marker records a confirmed CLI version, and
            # this step moves the plugin, not the CLI (same reasoning as
            # nx upgrade's own plugin-lockstep step, which never writes it
            # either -- see plugin_lockstep.py's module docstring).
            log_event("ref_drift_upgrade_started")
            _run_nx_upgrade_for_ref_drift(_NX_UPGRADE_TIMEOUT)
            return

        # 2. No-op fast path: CLI already at or above target -> record
        #    lockstep and stop (also handles the plugin-downgrade case where
        #    the installed CLI is ahead of the pinned plugin version).
        # nexus-otnvr item 4: capture this ONCE and reuse it as the "before"
        # value for the started-log below — a second installed_nx_version()
        # call here would be a second real `nx --version` subprocess for no
        # behavioral gain, and (test seam note) would consume a second
        # element from callers' scripted installed_versions() sequences.
        before = installed_nx_version()
        if satisfies(before, target):
            debug(f"nx already satisfies target {target}; writing marker")
            write_marker(target)
            return

        # 3. Two-command safe upgrade, strict order. Stop on first failure
        #    so a failed binary upgrade never proceeds to migrations. LOUD
        #    (nexus-otnvr item 4): this is the actual venv-mutating step —
        #    log it starting, unconditionally, before it can race anything
        #    (an MCP host booting concurrently, another `nx` invocation).
        log_event(
            "lockstep_upgrade_started", target=target, installed=str(before),
        )
        # GATE 3's FIRST command follows the layout. On a generation box
        # `uv tool upgrade conexus` would REBUILD THE LEGACY UV TREE and
        # re-symlink over the nexus-owned shims -- nexus-utpuw.7's accepted
        # risk, fired automatically on every session start. That is why gate 1
        # and gate 3 had to move together: fixing detection alone would have
        # converted a silent no-op into an automated shim clobber, which is
        # strictly worse than the bug.
        #
        # `nx self install` (.14) carries extras forward out of
        # nexus-install.json, which is the property `uv tool upgrade` was
        # chosen for in the first place.
        upgrade_cmd = (
            ["nx", "self", "install"] if generation
            else ["uv", "tool", "upgrade", "conexus"]
        )
        # `nx self install` of a first ~0.8 GB generation gets the long
        # budget; the legacy `uv tool upgrade` keeps the short one.
        step_timeout = _SELF_INSTALL_TIMEOUT if generation else _UV_TIMEOUT
        if not run_cmd(upgrade_cmd, timeout=step_timeout):
            debug("uv tool upgrade failed; leaving marker stale for retry")
            log_event("lockstep_upgrade_result", target=target, outcome="uv_upgrade_failed")
            return
        # nexus-vyy65: this build runs detached and its output is shown only under NX_HOOK_DEBUG, so a
        # build that left PDF support out (a Windows machine that cannot load the stack) would be silent.
        # Always log the line it printed.
        slim_line = pdf_slim_line(_last_stdout) if generation else None
        if slim_line is not None:
            log_event("lockstep_pdf_slim", target=target, line=slim_line)
        # 3b. The migrations and any upgrade-time rebuild run DETACHED
        #     (nexus-qytet): this step must not be the timeout that kills a
        #     ~0.8 GB rebuild. Step 4 below confirms the BINARY (3a installed
        #     it); the marker means "binary confirmed", NOT "nx upgrade
        #     finished". A migration that fails or does not finish is retried
        #     by the next SessionStart's `nx upgrade --auto`. "Started" is
        #     checked (the child survived its first seconds); if it cannot be
        #     started, fall back to the bounded synchronous run, as before.
        child_pid = start_background_upgrade()
        if child_pid is not None:
            log_event(
                "lockstep_upgrade_background", target=target, state=_last_start_state or "started",
                pid=str(child_pid), log=str(upgrade_log_path()),
            )
        elif not run_cmd(["nx", "upgrade"], timeout=_NX_UPGRADE_TIMEOUT):
            debug("nx upgrade failed; leaving marker stale for retry")
            log_event("lockstep_upgrade_result", target=target, outcome="nx_upgrade_failed")
            return

        # 4. Marker only on confirmed lockstep (installed >= target).
        after = installed_nx_version()
        if satisfies(after, target):
            write_marker(target)
            log_event(
                "lockstep_upgrade_result", target=target, outcome="success",
                installed=str(after),
                upgrade="detached" if child_pid is not None else "finished",
            )
        else:
            debug("version still below target after upgrade; leaving marker stale")
            log_event(
                "lockstep_upgrade_result", target=target,
                outcome="version_still_mismatched", installed=str(after),
            )
    except Exception as exc:  # noqa: BLE001 - detached action must never raise
        debug(f"swallowed unexpected error: {exc}")


if __name__ == "__main__":
    main(sys.argv)
    sys.exit(0)
