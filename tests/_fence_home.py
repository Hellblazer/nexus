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
import sys
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


#: The OS autostart-unit directories (nexus-q81g7): ``~/.config/systemd`` (the
#: user units, drop-ins included) and ``~/Library/LaunchAgents``. Passed
#: through, a test's HOME resolves to the operator's REAL unit files, so
#: ``upgrade_finish.converge_service_autostart_unit`` finds a real
#: ``nexus-service.service`` / ``com.nexus.service.plist``, sees drift against
#: the fenced render, and the human path (``nx daemon restart-stale``) backs it
#: up, disables it, rewrites it and enables it against the real user manager.
#: That destroyed qwentescence's real unit on 2026-09-30.
AUTOSTART_SHADOWS: tuple[str, ...] = (".config/systemd", "Library/LaunchAgents")

#: Writers a mirrored entry would let a test reach (the q81g7 critique, S2):
#: ``~/.local/state/nexus`` (hook ledgers, ``~/.local/state`` log dir),
#: ``~/.claude/agents`` (``nx agents install`` writes ``worktree-developer.md``)
#: and ``~/.claude/projects`` (``command_context`` memory). Two components.
INTERIM_SHADOWS: tuple[str, ...] = (".local/state", ".claude/agents", ".claude/projects")

#: Credential stores are NEVER mirrored, so a fenced run holds none of them:
#: shadowed by an empty directory at the top level, or one level down for the
#: two under ``.config``. (A separate bead inverts the whole fence to
#: shadow-everything with a measured allowlist; this is the interim floor.)
CREDENTIAL_SHADOWS: tuple[str, ...] = (
    ".aws", ".gnupg", ".kube", ".ssh", ".config/gh", ".config/op",
)

#: Every shadow applied whatever the caller names. Both twins carry this list
#: (``FENCE_ALWAYS_SHADOWS`` in ``fence_home.sh``); ``test_fence_home_twins_agree``
#: pins the two together.
ALWAYS_SHADOWS: tuple[str, ...] = (*AUTOSTART_SHADOWS, *INTERIM_SHADOWS, *CREDENTIAL_SHADOWS)


def fence_home(real_home: Path, gate_home: Path, shadow: str = ".config/nexus") -> Path:
    """Symlink every entry of *real_home* into *gate_home*, shadowing *shadow*
    and :data:`ALWAYS_SHADOWS`.

    A shadow is ``<top>/<leaf>`` or a bare ``<top>``. A bare ``top`` becomes an
    empty real directory. For ``<top>/<leaf>`` the ``top`` is recreated as a
    real directory whose own entries are symlinked through except the shadowed
    leaves, which become fresh empty directories. Returns *gate_home*.
    """
    whole: set[str] = set()
    leaves_by_top: dict[str, set[str]] = {}
    for rel in (shadow, *ALWAYS_SHADOWS):
        top, sep, leaf = rel.partition("/")
        if sep:
            leaves_by_top.setdefault(top, set()).add(leaf)
        else:
            whole.add(top)

    gate_home.mkdir(parents=True, exist_ok=True)
    for top in (*whole, *leaves_by_top):
        (gate_home / top).mkdir(parents=True, exist_ok=True)

    for entry in sorted(real_home.iterdir()):
        if entry.name in whole or entry.name in leaves_by_top:
            continue
        link = gate_home / entry.name
        if not link.exists() and not link.is_symlink():
            link.symlink_to(entry)

    for top, leaves in leaves_by_top.items():
        real_top = real_home / top
        if real_top.is_dir():
            for entry in sorted(real_top.iterdir()):
                if entry.name in leaves:
                    continue
                link = gate_home / top / entry.name
                if not link.exists() and not link.is_symlink():
                    link.symlink_to(entry)
        for leaf in leaves:
            (gate_home / top / leaf).mkdir(parents=True, exist_ok=True)
    return gate_home


def fence_manager_env(gate_home: Path) -> None:
    """Make the OS user service manager unreachable from this process tree.

    ``systemctl --user`` finds the user manager through
    ``$XDG_RUNTIME_DIR/systemd/private`` and ``$XDG_RUNTIME_DIR/bus``, or
    ``$DBUS_SESSION_BUS_ADDRESS``. An empty runtime dir and no bus address make
    every ``systemctl --user`` fail to connect, as on a CI runner. Set
    unconditionally, not setdefault: an inherited real value is the hazard.

    LINUX ONLY (the q81g7 critique, S6): macOS has no consumer of the variable
    but ``_mineru_spawn``'s output root, which it would only redirect. Rootless
    docker/podman keep their socket in ``$XDG_RUNTIME_DIR``, so a real
    ``docker.sock`` there is pinned into ``DOCKER_HOST`` first (an existing
    ``DOCKER_HOST`` is never touched) or Testcontainers would lose its daemon.
    launchd has no env seam; on macOS the LaunchAgents shadow plus
    ``nexus.daemon.installer``'s manager tripwire are the fence.

    Twin of ``fence_home_env`` in ``tests/e2e/lib/fence_home.sh``.
    """
    os.environ.pop("DBUS_SESSION_BUS_ADDRESS", None)
    if not sys.platform.startswith("linux"):
        return
    real_runtime = os.environ.get("XDG_RUNTIME_DIR", "")
    if not os.environ.get("DOCKER_HOST") and real_runtime:
        sock = Path(real_runtime) / "docker.sock"
        if sock.is_socket():
            os.environ["DOCKER_HOST"] = f"unix://{sock}"
    runtime = gate_home / "xdg-runtime"
    runtime.mkdir(parents=True, exist_ok=True)
    os.environ["XDG_RUNTIME_DIR"] = str(runtime)


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
    fence_manager_env(gate_home)
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
