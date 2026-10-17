# SPDX-License-Identifier: AGPL-3.0-or-later
# Copyright (c) 2026 Hal Hildebrand. All rights reserved.
"""Shadow a HOME: an empty directory plus a short allowlist of toolchain caches.

nexus-ld3aa. Python twin of ``tests/e2e/lib/fence_home.sh``. The e2e gates were
fenced first (nexus-pfuns); the UNIT SUITE was not, and it ran with the
operator's real ``$HOME``. That mattered because ``nexus_config_dir()`` falls
back to ``Path.home()/".config"/"nexus"`` whenever ``NEXUS_CONFIG_DIR`` is
absent, and because ``upgrade_finish.check_version_transition`` WRITES that
directory on a version transition.

THE POLICY IS SHADOW-EVERYTHING. The fence used to mirror every entry of the
real home and shadow a denylist (``.config/nexus``, the autostart unit dirs, the
credential stores). The two failure modes are asymmetric. A path the fence
fails to mirror breaks LOUDLY on the first run (2026-08-24: the Maven rebuild
died on an absent ``~/.testcontainers.properties``). A path it mirrors by
mistake fails SILENTLY and destructively (2026-09-12: a test overwrote the
operator's global identity file; 2026-09-30: a test replaced a live
``nexus-service`` autostart unit on qwentescence). A denylist is never finished,
because the set of things a home holds that a test can damage is open: a dotfile
the operator installs next week is mirrored by default. So the fence is
inverted. The test home starts EMPTY and :data:`MIRRORED_ENTRIES` names the
only entries linked through, each a read-mostly toolchain cache measured as
needed. Anything else a test needs it is given explicitly (a fixture-provided
file, an env pin such as ``UV_CACHE_DIR``), never by widening the mirror.

CREDENTIAL STORES ARE NEVER MIRRORED (:data:`NEVER_MIRRORED`), and the empty
default means none is. ``tests/test_fence_home_twins_agree.py`` pins that.

TWO IMPLEMENTATIONS, ONE CONTRACT. This module and ``fence_home.sh`` must
produce the same shape; ``tests/test_fence_home_twins_agree.py`` is what says
so. The alternative -- one implementation invoked across the language boundary
-- was rejected because the shell gate runs before ``uv sync`` in some paths and
cannot depend on a Python import.
"""
from __future__ import annotations

import os
import shutil
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


#: The ONLY entries of the real home a fenced home links through (top-level
#: names). Each is a read-mostly toolchain cache or daemon-socket directory that
#: a build or a Testcontainers run needs and that cannot be handed over another
#: way: ``.m2`` and ``.gradle`` (the dependency caches the jar rebuild resolves
#: from), ``.docker`` (the daemon socket at ``run/docker.sock``),
#: ``.testcontainers.properties`` (``testcontainers.ryuk.disabled``), ``.cargo``
#: and ``.rustup`` (Rust toolchains). uv is NOT here: its cache and managed
#: Pythons are pinned by env (:func:`fence_uv_env`), as is the ONNX model root
#: (:func:`fence_cache_env`). Both twins carry this list (``FENCE_MIRRORED_ENTRIES``
#: in ``fence_home.sh``) and ``test_fence_home_twins_agree`` pins the two
#: together. An entry the real home does not have is skipped.
MIRRORED_ENTRIES: tuple[str, ...] = (
    ".m2", ".gradle", ".docker", ".cargo", ".rustup", ".testcontainers.properties",
)

#: Never mirrored, whatever else changes: credential stores, the operator's
#: identity and tool config, and the autostart-unit and state trees whose
#: mirroring cost a live service (2026-09-30). A test pins that no
#: :data:`MIRRORED_ENTRIES` entry is, or sits under, one of these.
NEVER_MIRRORED: tuple[str, ...] = (
    ".ssh", ".aws", ".gnupg", ".kube", ".netrc", ".npmrc", ".gitconfig",
    ".claude", ".claude.json", ".dolt", ".beads",
    ".config", ".config/gh", ".config/op", ".config/1Password", ".config/git",
    ".config/systemd", ".local", ".local/state",
    "Library", "Library/Keychains", "Library/LaunchAgents",
)


def _windows_link(link: Path, entry: Path) -> None:
    """Link *entry* through at *link* without a symlink: a junction for a
    directory, a hard link for a file (a copy across volumes). Neither needs
    SeCreateSymbolicLinkPrivilege, which a non-admin account such as the
    win-release runner's lacks (WinError 1314, rehearsal run 37410530509)."""
    if entry.is_dir():
        import _winapi  # noqa: PLC0415 -- Windows only

        _winapi.CreateJunction(str(entry), str(link))
        return
    try:
        os.link(entry, link)
    except OSError:
        shutil.copy2(entry, link)


def _link_through(link: Path, entry: Path) -> None:
    try:
        link.symlink_to(entry)
    except OSError:
        if sys.platform != "win32":
            raise
        _windows_link(link, entry)


def fence_home(real_home: Path, gate_home: Path) -> Path:
    """Create *gate_home* empty and link each :data:`MIRRORED_ENTRIES` entry that
    exists in *real_home* into it. On Windows without the symlink privilege an
    entry is linked through by :func:`_windows_link` instead. Returns *gate_home*.

    Nothing else of the real home is visible from the result: not
    ``.config/nexus``, the autostart unit dirs, ``.local/state``, nor any
    credential store.
    """
    gate_home.mkdir(parents=True, exist_ok=True)
    for name in MIRRORED_ENTRIES:
        entry = real_home / name
        link = gate_home / name
        if (entry.exists() or entry.is_symlink()) and not (link.exists() or link.is_symlink()):
            _link_through(link, entry)
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
    launchd has no env seam; on macOS the empty home (no LaunchAgents) plus
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


def install_fence(gate_home: Path) -> Path | None:
    """Fence ``$HOME`` for this process and every child it spawns.

    Idempotent: a second call while already fenced is a no-op, so an xdist
    worker re-running session start does not fence a fenced home.
    Records the ORIGINAL home in :data:`REAL_HOME_ENV` first -- that value is
    what the real-config-dir guards must keep watching.
    """
    if os.environ.get(REAL_HOME_ENV):
        return None
    real_home = Path(os.path.expanduser("~")).resolve()
    fence_home(real_home, gate_home)
    os.environ[REAL_HOME_ENV] = str(real_home)
    os.environ[FENCED_HOME_ENV] = str(gate_home)
    os.environ["HOME"] = str(gate_home)
    fence_manager_env(gate_home)
    # THE INSTALL LAYOUT IS FENCED SEPARATELY. The suite's contract is that
    # install_layout resolves NO generation unless a test builds one (T2
    # nexus/generation-layout-tests-are-blind-by-default). The empty home holds
    # no ``.local`` now, so the layout is empty by construction; these three
    # roots stay pinned so a tool that falls back to a HOME-derived default
    # still lands in the fence, and so an inherited outer layout is not read as
    # the suite's own. setdefault: an explicit outer value wins.
    for var, sub in (
        ("NX_TOOLS_DIR", "nx-tools"),
        ("NX_BIN_DIR", "nx-bin"),
        ("UV_TOOL_DIR", "uv-tools"),
    ):
        root = gate_home / sub
        root.mkdir(parents=True, exist_ok=True)
        os.environ.setdefault(var, str(root))
    fence_uv_env(real_home)
    fence_cache_env(real_home)
    return gate_home


def fence_uv_env(real_home: Path) -> None:
    """Pin uv's HOME-derived roots to the real home; an explicit outer value wins.

    ``UV_PYTHON_INSTALL_DIR`` (nexus-t0pke): the fenced home has no ``.local``,
    so without the pin uv would look for managed Pythons under the fence, and a
    ``uv run`` in a checkout with no ``.venv`` would build ``.venv`` on a path
    inside the fenced home that dangles once it is deleted. ``UV_CACHE_DIR``:
    uv resolves its cache off HOME at process start; pinned so a cold resolve is
    not the price of the fence.

    Twin of ``fence_uv_env`` in ``tests/e2e/lib/fence_home.sh``, including its
    ``${VAR:-default}`` reading: an EMPTY outer value counts as unset.
    """
    if not os.environ.get("UV_CACHE_DIR"):
        os.environ["UV_CACHE_DIR"] = str(real_home / ".cache" / "uv")
    if not os.environ.get("UV_PYTHON_INSTALL_DIR"):
        data_home = os.environ.get("XDG_DATA_HOME") or str(real_home / ".local" / "share")
        os.environ["UV_PYTHON_INSTALL_DIR"] = str(Path(data_home) / "uv" / "python")


def fence_cache_env(real_home: Path) -> None:
    """Pin the ONNX model roots to the real home's read-mostly caches.

    The local engine resolves its model files from ``NX_ONNX_MODEL_DIR`` or
    ``$HOME/.cache/nexus/onnx_models`` (``nexus.db.onnx_model_root``), and the
    supervisor hands the resolved path to the engine it spawns. Under the empty
    fenced home that default is a directory with no model in it, so every
    substrate-backed vector leg would fail to find (or try to download) the
    ~500 MB model. The model cache is read-mostly and content-addressed, so it
    is shared by env rather than by mirroring ``.cache`` (which holds far more
    than models). An explicit outer value wins; an EMPTY one counts as unset, as in
    :func:`fence_uv_env`.

    Twin of ``fence_cache_env`` in ``tests/e2e/lib/fence_home.sh``.
    """
    if not os.environ.get("NX_ONNX_MODEL_DIR"):
        os.environ["NX_ONNX_MODEL_DIR"] = str(real_home / ".cache" / "nexus" / "onnx_models")
    # The MiniLM direct-path cache (``nexus.db.minilm_direct.CACHE_DIR_ENV``), the
    # Chroma-compatible root. tests/conftest.py also pins it, but from NX_REAL_HOME,
    # which is the EMPTY gate home when the suite runs inside the e2e gate.
    if not os.environ.get("NX_MINILM_CACHE_DIR"):
        os.environ["NX_MINILM_CACHE_DIR"] = str(real_home / ".cache" / "chroma" / "onnx_models")
    # The ONNX Runtime native-library extraction root (nexus-8ool4): written once per
    # ``<version>-<os-arch>`` and reused across engine starts, so a fenced suite shares
    # the real cache instead of extracting ~16 MB again into the empty home.
    if not os.environ.get("NX_ORT_NATIVE_DIR"):
        os.environ["NX_ORT_NATIVE_DIR"] = str(real_home / ".cache" / "nexus" / "ort_native")
