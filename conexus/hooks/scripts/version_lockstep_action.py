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


def uv_receipt_present() -> bool:
    """True iff conexus was installed via ``uv tool`` (receipt present).

    Inline re-implementation of ``src/nexus/commands/init.py:52-68``
    (``_uv_receipt_path``). Intentionally NOT imported from nexus.commands.init
    (the bare hook interpreter cannot import the conexus package) and NOT
    extracted to a shared nexus/ helper (the two consumers run in different
    interpreters: the package vs this bare hook). If the detection logic in
    init.py changes, update this copy too.

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
        if not uv_receipt_present():
            debug("no uv-tool receipt (dev/editable tree or no uv); skipping")
            return

        # 2. No-op fast path: CLI already at or above target -> record
        #    lockstep and stop (also handles the plugin-downgrade case where
        #    the installed CLI is ahead of the pinned plugin version).
        if satisfies(installed_nx_version(), target):
            debug(f"nx already satisfies target {target}; writing marker")
            write_marker(target)
            return

        # 3. Two-command safe upgrade, strict order. Stop on first failure
        #    so a failed binary upgrade never proceeds to migrations.
        if not run_cmd(["uv", "tool", "upgrade", "conexus"], timeout=_UV_TIMEOUT):
            debug("uv tool upgrade failed; leaving marker stale for retry")
            return
        if not run_cmd(["nx", "upgrade"], timeout=_NX_UPGRADE_TIMEOUT):
            debug("nx upgrade failed; leaving marker stale for retry")
            return

        # 4. Marker only on confirmed lockstep (installed >= target).
        if satisfies(installed_nx_version(), target):
            write_marker(target)
        else:
            debug("version still below target after upgrade; leaving marker stale")
    except Exception as exc:  # noqa: BLE001 - detached action must never raise
        debug(f"swallowed unexpected error: {exc}")


if __name__ == "__main__":
    main(sys.argv)
    sys.exit(0)
