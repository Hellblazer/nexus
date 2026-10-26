# SPDX-License-Identifier: AGPL-3.0-or-later
# Copyright (c) 2026 Hal Hildebrand. All rights reserved.
"""``nx-hook self-gc``: the SessionStart generation sweep (RDR-215 item 6).

``hooks.json`` used to declare this as a shell string::

    nx self gc >/dev/null 2>&1 || true

Both redirects and the ``|| true`` are this module's job now. The contract is
the simplest one in the hook layer: reclaim what can be reclaimed, and say
nothing whatever happens. ``nx self gc`` removes superseded install
generations that no live process still holds; a session start is a good
moment to try and a terrible moment to report about it.

The subprocess boundary is kept for the same reason
:mod:`nexus.hooks.upgrade_auto` keeps it — ``nx self gc`` reasons about
generation trees and live holders, and ``nx-hook`` is running out of one of
them. See that module's docstring for the full argument.
"""
from __future__ import annotations

import os
import subprocess
import time
from pathlib import Path

from nexus._hook_runtime._io import HookResult

#: The directory the removed conexus git-integrity hook (nexus-i93tz, plugin
#: 7.83.x to 7.86.x) kept its per-session state in, under the nexus config dir.
#: The hook's own ``_prune`` was the only thing that reaped it; the hook is gone
#: from the tree, but installed plugin copies still write there. Same bounds as
#: that ``_prune``: a session directory untouched for two days, a scan capped at
#: 5000 entries, at most 200 removals per sweep.
GIT_INTEGRITY_DIR = "git-integrity"
GIT_INTEGRITY_STALE_SECONDS = 2 * 86400
_GIT_INTEGRITY_MAX_SCAN = 5000
_GIT_INTEGRITY_MAX_REMOVALS = 200


def reap_git_integrity_dirs(config_dir: Path, now: float | None = None) -> int:
    """Remove stale session directories under ``<config_dir>/git-integrity``.

    A session directory is stale when its own mtime is more than
    ``GIT_INTEGRITY_STALE_SECONDS`` old (exactly that old is kept, as the old
    ``_prune`` kept it) (the old hook rewrote a snapshot in it
    on every Bash call, which moves the directory mtime, so a live session's
    directory stays fresh). Only real directories are touched: a symlink and a
    plain file (the old hook's ``.last-prune`` marker) are left alone. A
    directory is emptied file by file and ``rmdir``-ed, never ``rmtree``-ed, so
    a surprise nested directory makes that one removal fail and nothing more.
    Every OS error is swallowed; returns the number of directories removed.
    """
    parent = Path(config_dir) / GIT_INTEGRITY_DIR
    cutoff = (time.time() if now is None else now) - GIT_INTEGRITY_STALE_SECONDS
    removed = 0
    try:
        with os.scandir(parent) as scan:
            for n, de in enumerate(scan):
                if n >= _GIT_INTEGRITY_MAX_SCAN or removed >= _GIT_INTEGRITY_MAX_REMOVALS:
                    break
                try:
                    if not de.is_dir(follow_symlinks=False):
                        continue
                    if de.stat(follow_symlinks=False).st_mtime >= cutoff:
                        continue
                    for leaf in os.listdir(de.path):
                        os.unlink(os.path.join(de.path, leaf))
                    os.rmdir(de.path)
                    removed += 1
                except OSError:
                    pass
    except OSError:
        pass
    return removed


def run(payload: dict | None) -> HookResult:  # noqa: ARG001 — reads no stdin, as the shell form read none
    """Spawn ``nx self gc``, discard everything it says, always succeed.

    No branch writes to stdout or stderr. A missing ``nx``, a nonzero exit and
    a failed spawn are all the ``|| true`` case: this hook has no opinion worth
    interrupting a session start for.

    First, and before the ``nx`` lookup so it also runs on a box with no ``nx``
    on PATH, it reaps stale ``git-integrity/`` session directories (see
    :func:`reap_git_integrity_dirs`): stdlib only, bounded, silent.
    """
    try:
        from nexus import config as _nx_config  # noqa: PLC0415 — deferred import; module attribute so a patched nexus.config reaches it (nexus-78blw)

        reap_git_integrity_dirs(_nx_config.nexus_config_dir())
    except Exception:  # noqa: BLE001 — this verb's contract is silence; a failed reap must not stop the generation sweep
        pass
    from nexus.util.nx_argv import nx_argv_for, which_off_cwd  # noqa: PLC0415 — stdlib-only, a few microseconds; function-level so the hook module itself stays import-light

    nx = which_off_cwd("nx")
    if nx is None:
        return HookResult()
    try:
        subprocess.run(  # noqa: S603 — argv list, absolute interpreter on Windows, no shell
            nx_argv_for(nx, "self", "gc"),
            capture_output=True,
            text=True,
            # nx writes UTF-8 to its pipes on Windows (nexus.cli reconfigures at import).
            # The output is discarded, so a stray byte must not raise out of the hook.
            encoding="utf-8",
            errors="replace",
            stdin=subprocess.DEVNULL,
        )
    except OSError:
        pass
    return HookResult()
