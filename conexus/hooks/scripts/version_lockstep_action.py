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
  4. Marker on confirmed success only: re-read ``nx --version`` and write
     the marker iff it now equals the target. Any failure leaves the
     marker stale so the next session re-nudges and retries.

Stdlib-only (bare interpreter via ``_run_python_hook.sh``; the conexus
package is not importable here). No structlog under bare interp -> the
NX_HOOK_DEBUG stderr convention.
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
import shutil
import subprocess
from pathlib import Path

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
_NX_UPGRADE_TIMEOUT = _env_int("NX_LOCKSTEP_NX_TIMEOUT", 120)
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
    """Per-user lockstep marker (see version_lockstep_hook.marker_path)."""
    override = os.environ.get("NX_LOCKSTEP_MARKER")
    if override:
        return Path(override)
    return Path.home() / ".config" / "nexus" / "cli_lockstep_marker"


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
    return Path.home() / ".config" / "nexus" / "lockstep.log"


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
        with path.open("a") as f:
            f.write(line)
    except OSError as exc:
        debug(f"log_event failed (non-fatal): {exc}")


#: The generation root's env override and default, DUPLICATED FROM
#: ``nexus.install_layout`` by necessity: this hook runs under a bare python3
#: (``_run_python_hook.sh`` probes python3.13, python3.12, then bare python3)
#: and cannot import the conexus package -- the same constraint that already
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
    if shutil.which("uv") is None:
        return False
    try:
        out = subprocess.run(
            ["uv", "tool", "dir"],
            capture_output=True, text=True, timeout=10, check=True,
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
    if shutil.which("nx") is None:
        return None
    try:
        out = subprocess.run(
            ["nx", "--version"],
            capture_output=True, text=True, timeout=15, check=True,
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


def run_cmd(cmd: list[str], timeout: int = 300) -> bool:
    """Run a command; return True on exit 0, False otherwise. Never raises."""
    try:
        result = subprocess.run(
            cmd, capture_output=True, text=True, timeout=timeout,
        )
        if DEBUG and result.stdout:
            debug(f"{cmd[0]} stdout: {result.stdout[:500]}")
        if result.returncode != 0:
            debug(f"{' '.join(cmd)} exited {result.returncode}: {result.stderr[:500]}")
            return False
        return True
    except (subprocess.SubprocessError, OSError) as exc:
        debug(f"{' '.join(cmd)} raised: {exc}")
        return False


def write_marker(version: str) -> None:
    """Record *version* as the confirmed-in-lockstep CLI version."""
    path = marker_path()
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(version)
    debug(f"wrote marker {path} = {version}")


def main(argv: list[str]) -> None:
    """Perform the gated, ordered, confirmed upgrade. Always fail-safe."""
    try:
        if len(argv) < 2 or not argv[1].strip():
            debug("no target version argument; nothing to do")
            return
        target = argv[1].strip()

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
        if not run_cmd(upgrade_cmd, timeout=_UV_TIMEOUT):
            debug("uv tool upgrade failed; leaving marker stale for retry")
            log_event("lockstep_upgrade_result", target=target, outcome="uv_upgrade_failed")
            return
        if not run_cmd(["nx", "upgrade"], timeout=_NX_UPGRADE_TIMEOUT):
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
