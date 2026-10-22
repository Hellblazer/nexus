# SPDX-License-Identifier: AGPL-3.0-or-later
"""The working directory a detached nexus process runs in (nexus-x3m8e).

Stdlib only, so the session hooks and the session-end launcher, which must
stay import-light, can use the same rule as the daemons. The daemon-facing
names are re-exported from :mod:`nexus.daemon.service_registry`, the home of
the lifecycle rules (``nexus/daemon/AGENTS.md``); import them from there
unless the caller cannot afford that module.

THE FAULT CLASS. Sessions run in disposable worktrees. A long-lived process
that inherited its launcher's working directory loses it when that worktree is
deleted, and from then on every ``os.getcwd()``, ``os.path.abspath()`` of a
relative path and ``Path.cwd()`` in the process or in any library it imports
raises ``FileNotFoundError``. The measured instance: a MinerU server answered
every task with ``FileNotFoundError`` for hours while ``/health`` said 200,
because importing ``torchvision.ops`` pulls in ``torch._dynamo.config``, whose
module body runs ``os.path.join(os.getcwd(), "torch_compile_debug")``
(``torch/_dynamo/config.py``, ``default_debug_dir_root``). Python's own import
system is NOT part of this class: CPython's path finder guards ``getcwd`` for
the empty ``sys.path`` entry, so a bare ``import`` of an uncached module works
in a deleted directory (measured, CPython 3.12). What fails is the code that
asks for the directory.

THE RULE. A detached process starts in the filesystem root: a directory that
always exists, that nothing deletes, that holds no Python module for the empty
``sys.path`` entry to find, and that never blocks the removal of a config
directory (a Windows process holding its cwd keeps that directory from being
deleted, which broke ``nx uninstall`` once). A process that needs a project
directory is handed it explicitly: as an argument, or as ``NEXUS_REPO_ROOT``
(:data:`REPO_ROOT_ENV`), which ``config.load_config`` reads where it used to
read the cwd.
"""
from __future__ import annotations

import os
from collections.abc import Mapping
from typing import Any

#: Environment variable naming the project directory a detached child should
#: read its per-repo ``.nexus.yml`` from, in place of the working directory it
#: no longer has. Read by ``nexus.config.load_config``.
REPO_ROOT_ENV: str = "NEXUS_REPO_ROOT"


def stable_daemon_cwd() -> str:
    """The working directory every detached process starts in: the filesystem
    root (the current drive's root on Windows). Touches nothing on disk."""
    try:
        return os.path.abspath(os.sep)
    except OSError:  # defensive: abspath of an absolute path does not consult the cwd
        return os.sep


def current_project_root() -> str | None:
    """The caller's working directory as an absolute path, or ``None`` when it
    cannot be read (it was deleted). What a spawner passes as *project_root*
    so the child reads the same per-repo config the spawner would have."""
    try:
        return os.getcwd()
    except OSError:
        return None


def _absolute_or(path: str) -> str:
    try:
        return os.path.abspath(path)
    except OSError:  # the cwd is already gone: the relative name has no referent
        return path


def detached_spawn_kwargs(
    env: Mapping[str, str] | None = None,
    *,
    project_root: str | os.PathLike[str] | None = None,
) -> dict[str, Any]:
    """``Popen`` kwargs that make a detached child independent of its caller's
    working directory. Every long-lived spawn merges these in.

    ``cwd`` is :func:`stable_daemon_cwd`. Two things in the environment name a
    path relative to the CALLER's directory, and would name another path once
    the child's directory changes, so the result carries an ``env`` copy only
    when one applies: a relative ``NEXUS_CONFIG_DIR`` made absolute, and
    ``NEXUS_REPO_ROOT`` set to *project_root* (a child that reads per-repo
    config). Otherwise no ``env`` key is returned and the child inherits as
    before. Never creates a directory.
    """
    kwargs: dict[str, Any] = {"cwd": stable_daemon_cwd()}
    source = env if env is not None else os.environ
    overrides: dict[str, str] = {}
    raw = source.get("NEXUS_CONFIG_DIR", "").strip()
    if raw and not os.path.isabs(raw):
        overrides["NEXUS_CONFIG_DIR"] = _absolute_or(raw)
    if project_root is not None:
        overrides[REPO_ROOT_ENV] = os.fspath(project_root)
    if overrides:
        kwargs["env"] = {**source, **overrides}
    return kwargs


def enter_stable_daemon_cwd(*, project_root: str | os.PathLike[str] | None = None) -> str:
    """``chdir`` the CURRENT process off whatever directory launched it: the
    in-process half of the rule, for a daemon started by launchd, by hand from
    a worktree, or by an older client that passed no ``cwd=``.

    A relative ``NEXUS_CONFIG_DIR`` is made absolute in ``os.environ`` FIRST,
    while the old directory still names it. *project_root* is recorded as
    ``NEXUS_REPO_ROOT`` for the same reason. A caller holding other relative
    paths makes them absolute before calling. Returns the directory entered, or
    ``""`` when the chdir failed (the process then keeps its directory; a
    chdir failure is never what stops a daemon starting).
    """
    raw = os.environ.get("NEXUS_CONFIG_DIR", "").strip()
    if raw and not os.path.isabs(raw):
        os.environ["NEXUS_CONFIG_DIR"] = _absolute_or(raw)
    if project_root is not None:
        os.environ[REPO_ROOT_ENV] = os.fspath(project_root)
    target = stable_daemon_cwd()
    try:
        os.chdir(target)
    except OSError:
        return ""
    return target


def heal_deleted_cwd() -> bool:
    """Move a long-lived process off a working directory that was deleted under it.

    For a process that cannot start in the stable cwd because its host chose the
    directory: Claude Desktop starts its MCP servers in the plugin's install
    directory and deletes that directory when it updates the plugin, while the
    servers keep running (nexus-x6qc7, shakeout F19: every ``claude -p`` the
    server spawned then exited 1, "The current working directory was deleted").
    A directory that still exists is left alone, so a Claude Code server keeps
    its project directory, and so does one whose ``getcwd`` fails for any reason
    other than the directory being gone (``EACCES``). Unlike
    :func:`enter_stable_daemon_cwd` it rewrites no environment: with the old
    directory gone, a relative ``NEXUS_CONFIG_DIR`` has no referent to pin.
    Returns ``True`` when it moved the process.
    """
    try:
        os.getcwd()
        return False
    except FileNotFoundError:
        pass
    except OSError:
        return False
    try:
        os.chdir(stable_daemon_cwd())
    except OSError:
        return False
    return True
