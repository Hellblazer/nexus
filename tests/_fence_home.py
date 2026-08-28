# SPDX-License-Identifier: AGPL-3.0-or-later
# Copyright (c) 2026 Hal Hildebrand. All rights reserved.
"""Mirror a HOME, shadowing exactly one path. Python twin of fence_home.sh.

nexus-pfuns. The e2e gates were fenced first (``tests/e2e/lib/fence_home.sh``);
the UNIT SUITE was not, and it runs with the operator's real ``$HOME``. That
matters because ``nexus_config_dir()`` falls back to
``Path.home()/".config"/"nexus"`` whenever ``NEXUS_CONFIG_DIR`` is absent, and
because ``upgrade_finish.check_version_transition`` WRITES that directory on a
version transition -- ``install_mtime_and_version()`` reads the INSTALLED
distribution's version, so running the suite from a version-bumped worktree is
itself a transition.

WHY A DENYLIST. The shell twin's header records the measurement: an allowlist
attempt (symlink a hand-picked ``.cache``/``.local``/``.claude``) broke the
Maven build because ``~/.testcontainers.properties`` and ``~/.docker`` were
absent. The set of "things $HOME is for" cannot be completed by enumeration.
Mirror everything, shadow one path.

TWO IMPLEMENTATIONS, ONE CONTRACT. This module and ``fence_home.sh`` must
produce the same shape; ``tests/test_fence_home_twins_agree.py`` is what says
so. The alternative -- one implementation invoked across the language boundary
-- was rejected because the shell gate runs before ``uv sync`` in some paths and
cannot depend on a Python import.
"""
from __future__ import annotations

import os
from pathlib import Path

#: Set when a fence is installed, so xdist WORKERS (which inherit the fenced
#: HOME and would otherwise compute the fenced dir as "real") can still find
#: the operator's actual home. Without this the guard silently watches the
#: throwaway directory and stops guarding anything.
REAL_HOME_ENV = "NX_REAL_HOME"

#: The mirror this process installed. The guards use it to distinguish
#: "Path.home() is the fence, substitute the real one" from "a test has
#: monkeypatched Path.home and its answer must win" -- without this the
#: substitution is unconditional and silently defeats every test seam that
#: patches Path.home (6 guard tests, measured).
FENCED_HOME_ENV = "NX_FENCED_HOME"


def fence_home(real_home: Path, gate_home: Path, shadow: str = ".config/nexus") -> Path:
    """Symlink every entry of *real_home* into *gate_home*, shadowing *shadow*.

    The first component of *shadow* is recreated as a real directory whose own
    entries are symlinked through except the leaf, which becomes a fresh empty
    directory. Returns *gate_home*.
    """
    shadow_top, _, shadow_leaf = shadow.partition("/")
    gate_home.mkdir(parents=True, exist_ok=True)
    (gate_home / shadow_top).mkdir(parents=True, exist_ok=True)

    for entry in sorted(real_home.iterdir()):
        if entry.name == shadow_top:
            continue
        link = gate_home / entry.name
        if not link.exists() and not link.is_symlink():
            link.symlink_to(entry)

    real_top = real_home / shadow_top
    if real_top.is_dir():
        for entry in sorted(real_top.iterdir()):
            if entry.name == shadow_leaf:
                continue
            link = gate_home / shadow_top / entry.name
            if not link.exists() and not link.is_symlink():
                link.symlink_to(entry)

    (gate_home / shadow).mkdir(parents=True, exist_ok=True)
    return gate_home


def install_fence(gate_home: Path, shadow: str = ".config/nexus") -> Path | None:
    """Fence ``$HOME`` for this process and every child it spawns.

    Idempotent: a second call while already fenced is a no-op, so an xdist
    worker re-running session start does not fence a fenced home.
    Records the ORIGINAL home in :data:`REAL_HOME_ENV` first -- that value is
    what the real-config-dir guards must keep watching.
    """
    if os.environ.get(REAL_HOME_ENV):
        return None
    real_home = Path(os.path.expanduser("~")).resolve()
    fence_home(real_home, gate_home, shadow)
    os.environ[REAL_HOME_ENV] = str(real_home)
    os.environ[FENCED_HOME_ENV] = str(gate_home)
    os.environ["HOME"] = str(gate_home)
    # THE INSTALL LAYOUT IS FENCED SEPARATELY. The wholesale `.local` link
    # below shares the REAL layout too: <real>/.local/share/nexus/tools (the
    # generations, since nexus-utpuw), <real>/.local/bin (the shims) and
    # <real>/.local/share/uv/tools (uv's tree). The suite's contract is that
    # install_layout resolves NO generation unless a test builds one (T2
    # nexus/generation-layout-tests-are-blind-by-default); that held only
    # while the dev box had no layout. Measured 2026-08-28, the first day it
    # did: doctor's "Shim contents" row went FATAL inside the suite (the real
    # shims are rendered for the real path, the fenced path differs), 31
    # doctor tests read exit 2, every install-advice remedy string flipped
    # to the generation wording, and enumerate_processes matched nothing.
    # Three empty roots, set with setdefault so an explicit outer value wins.
    for var, sub in (
        ("NX_TOOLS_DIR", "nx-tools"),
        ("NX_BIN_DIR", "nx-bin"),
        ("UV_TOOL_DIR", "uv-tools"),
    ):
        root = gate_home / sub
        root.mkdir(parents=True, exist_ok=True)
        os.environ.setdefault(var, str(root))
    # uv resolves its cache off HOME at process start; pin it explicitly so the
    # mirror is not the only thing between the suite and a cold resolve.
    os.environ.setdefault("UV_CACHE_DIR", str(real_home / ".cache" / "uv"))
    return gate_home
