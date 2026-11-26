# SPDX-License-Identifier: AGPL-3.0-or-later
"""Run a plugin hook script with the account home pointed at a tmp directory, and build the three audience fixtures.

The audience helper (``conexus/hooks/scripts/_audience.py``) reads the user's settings from the account home, which comes
from the OS account database, so no environment variable moves it. The one test seam is the in-process module attribute
``_ACCOUNT_HOME_OVERRIDE``; this harness sets it and then runs the script under ``runpy`` as ``__main__``, so the script's own
``import _audience`` gets the same, already-overridden module.

    python audience_harness.py <account-home> <script> [script args...]

stdin and stdout pass through. Keep this file out of ``tests/e2e/lib/`` and
``tests/e2e/hook-cli-skew/``: those prefixes make a change ineligible for the
plugin-only fast lane (``scripts/check_plugin_only_eligibility.py``).

The fixture convention (tests/AGENTS.md): every hook test runs as a **plain** user (a bare ``git init`` and an account home with
no settings file), and asserts a setting-gated behaviour only in a **project-strict** fixture (a repository whose tracked
``.nexus/project.json`` sets the key; :func:`strict_repo`, :func:`write_project_settings`) or a **user-strict** one (an account
home whose ``.config/nexus/conexus.json`` sets it; :func:`strict_home`, :func:`write_user_settings`).
"""
from __future__ import annotations

import json
import os
import runpy
import sys

#: The Phase 1 marker file, still accepted for two client releases and read as ``{"subagent_git": "strict"}``.
LEGACY_MARKER = "scripts/agent-worktree-preflight.sh"


def write_project_settings(repo: "os.PathLike[str] | str", **settings: object) -> None:
    """Write *repo*'s tracked ``.nexus/project.json`` with *settings* (default: ``subagent_git`` strict)."""
    body = {"schema": 1, **(settings or {"subagent_git": "strict"})}
    path = os.path.join(os.fspath(repo), ".nexus", "project.json")
    os.makedirs(os.path.dirname(path), exist_ok=True)
    with open(path, "w", encoding="utf-8") as handle:
        json.dump(body, handle)


def mark_strict(repo: "os.PathLike[str] | str") -> None:
    """Give *repo* a project settings file that turns the strict subagent git guard on."""
    write_project_settings(repo, subagent_git="strict")


def strict_repo(path: "os.PathLike[str] | str", **settings: object) -> str:
    """A directory with a ``.git`` entry and a ``.nexus/project.json`` (strict unless *settings* say otherwise): the
    project-strict fixture."""
    os.makedirs(os.path.join(os.fspath(path), ".git"), exist_ok=True)
    write_project_settings(path, **{"subagent_git": "strict", **settings})
    return os.fspath(path)


def legacy_marked_repo(path: "os.PathLike[str] | str") -> str:
    """A directory with a ``.git`` entry and only the Phase 1 marker file (read as strict)."""
    os.makedirs(os.path.join(os.fspath(path), ".git"), exist_ok=True)
    marker = os.path.join(os.fspath(path), *LEGACY_MARKER.split("/"))
    os.makedirs(os.path.dirname(marker), exist_ok=True)
    with open(marker, "w", encoding="utf-8") as handle:
        handle.write("#!/bin/sh\n")
    return os.fspath(path)


def plain_repo(path: "os.PathLike[str] | str") -> str:
    """A directory with a ``.git`` entry and no settings file: the plain fixture. Every setting reads its default."""
    os.makedirs(os.path.join(os.fspath(path), ".git"), exist_ok=True)
    return os.fspath(path)


def plain_home(home: "os.PathLike[str] | str") -> str:
    """An account home with no settings file; returns it as a string."""
    os.makedirs(os.fspath(home), exist_ok=True)
    return os.fspath(home)


def write_user_settings(home: "os.PathLike[str] | str", **settings: object) -> str:
    """Write the account home's ``.config/nexus/conexus.json`` with *settings*; returns the home as a string."""
    path = os.path.join(os.fspath(home), ".config", "nexus", "conexus.json")
    os.makedirs(os.path.dirname(path), exist_ok=True)
    with open(path, "w", encoding="utf-8") as handle:
        json.dump(settings, handle)
    return os.fspath(home)


def strict_home(home: "os.PathLike[str] | str") -> str:
    """An account home whose user settings turn the strict subagent git guard on: the user-strict fixture."""
    return write_user_settings(home, subagent_git="strict")


#: The src hooks (``nexus.hooks._audience``) run in ``nx-hook`` or the MCP server, never through ``runpy``, so a test
#: that runs one in a CHILD PROCESS cannot reach the in-process seam. Its driver script starts with this prelude, and the
#: test passes the home in ``NX_TEST_ACCOUNT_HOME`` (read here, in test code; the helper itself never reads env).
SRC_SEAM_ENV = "NX_TEST_ACCOUNT_HOME"
SRC_SEAM_PRELUDE = (
    "import os as _seam_os\n"
    f"_seam_home = _seam_os.environ.get({SRC_SEAM_ENV!r})\n"
    "if _seam_home:\n"
    "    from nexus.hooks import _audience as _seam_audience\n"
    "    _seam_audience._ACCOUNT_HOME_OVERRIDE = _seam_home\n"
)


def src_seam_env(home: "os.PathLike[str] | str") -> dict[str, str]:
    """The env entry that points a prelude-carrying child's src helper at *home*."""
    return {SRC_SEAM_ENV: os.fspath(home)}


SCRIPTS = os.path.join(
    os.path.dirname(os.path.abspath(__file__)), os.pardir, os.pardir, "conexus", "hooks", "scripts",
)


def path_without(names: "tuple[str, ...]", mirror_root: "os.PathLike[str] | str") -> str:
    """``PATH`` with *names* gone. A directory that holds one is mirrored under *mirror_root* by symlinks to every other entry,
    so the tools beside it (git, an interpreter) stay reachable. For a test that runs a hook which shells out to ``nx`` when
    ``nx`` is on PATH (SubagentStart lists T1 through it) and must reach no storage."""
    keep: list[str] = []
    for i, d in enumerate(os.environ.get("PATH", "").split(os.pathsep)):
        if not d:
            continue
        try:
            entries = os.listdir(d)
        except OSError:
            continue
        if not any(n in entries for n in names):
            keep.append(d)
            continue
        mirror = os.path.join(os.fspath(mirror_root), str(i))
        os.makedirs(mirror, exist_ok=True)
        for n in entries:
            link = os.path.join(mirror, n)
            if n in names or os.path.lexists(link):
                continue
            try:
                os.symlink(os.path.join(d, n), link)
            except OSError:
                pass
        keep.append(mirror)
    return os.pathsep.join(keep)


def plugin_scripts_dir(script: str) -> str:
    """The ``hooks/scripts`` directory of the plugin *script* belongs to: the nearest ancestor that holds ``_audience.py``.

    The behaviour leg runs the plugin under test (a copy, or the installed one), so the helper the seam is set on must be
    that plugin's own, not this checkout's. A script outside any plugin falls back to this checkout's."""
    d = os.path.dirname(os.path.abspath(script))
    while True:
        if os.path.isfile(os.path.join(d, "_audience.py")):
            return d
        parent = os.path.dirname(d)
        if parent == d:
            return SCRIPTS
        d = parent


def main(argv: list[str]) -> None:
    if len(argv) < 3:
        sys.exit("usage: audience_harness.py <account-home> <script> [args...]")
    home, script, rest = argv[1], argv[2], argv[3:]
    scripts_dir = os.path.normpath(plugin_scripts_dir(script))
    if scripts_dir not in sys.path:
        sys.path.insert(0, scripts_dir)
    import _audience  # noqa: PLC0415 -- after the sys.path insert

    _audience._ACCOUNT_HOME_OVERRIDE = home
    # The seam lives in this process. A script's reexec_if_needed() would execv into another interpreter when resolve() names
    # one that differs (a CI runner's thin venv beside uv's python), and the new process has no seam: the real account's flag
    # decides. The marker is the script's own "already re-exec'd" switch.
    os.environ["NX_HOOK_INTERPRETER_REEXEC"] = "1"
    sys.argv = [script, *rest]
    runpy.run_path(script, run_name="__main__")


if __name__ == "__main__":
    main(sys.argv)
