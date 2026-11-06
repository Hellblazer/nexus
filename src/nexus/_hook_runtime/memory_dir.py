# SPDX-License-Identifier: AGPL-3.0-or-later
"""Find Claude Code's auto-memory directory for a repository (RDR-238 gate residual S-10).

Standard library only, on purpose, at import time AND at call time. The SessionStart curation
verb (nexus-qo7ie.16) imports this on every session start, so nothing here may pull in a heavier
module (``tests/hooks/test_hook_runtime_thin.py`` pins the import; ``tests/test_memory_curate.py``
pins the call, in a fresh interpreter), and ``nx memory curate --files`` (nexus-qo7ie.17) imports
the same function, so the two never disagree about which directory they are looking at.

THE MAPPING, :func:`memory_dir`. Given a working directory *cwd*:

1. A custom memory path wins. Claude Code's ``autoMemoryDirectory`` setting (an absolute or
   ``~/``-prefixed path) is read from, in order of precedence: ``<cwd>/.claude/settings.local.json``,
   ``<cwd>/.claude/settings.json``, the same two files in the primary checkout, then
   ``<config dir>/settings.json``. The first file that holds a usable value decides: a string
   that is absolute or starts with ``~/``, naming a directory that exists. Anything else
   (a relative path, a non-string, malformed JSON, a missing directory) is skipped and the
   next source is tried. The managed (policy) settings file is not read: its location is
   per operating system and a hook has no business probing system directories.
2. Otherwise the directory is ``<config dir>/projects/<key>/memory`` where

   * ``<config dir>`` is ``$CLAUDE_CONFIG_DIR`` when set and non-empty, else ``~/.claude``
     (a relocated config directory is followed, never mixed with the default);
   * ``<key>`` is the PRIMARY checkout's absolute path with every character that is not an
     ASCII letter or digit replaced by ``-`` (:func:`mangle_path`; ``/Users/a.b/git/r-x``
     becomes ``-Users-a-b-git-r-x``);
   * the primary checkout is the parent of the git common directory, so every linked worktree of
     a repository (and any subdirectory of one) resolves to the one directory its primary
     checkout owns. The common directory is found by READING, with no process spawned
     (:func:`primary_checkout_root`): walk up from *cwd* to the first ``.git``. A ``.git``
     directory is the common directory. A ``.git`` file holds ``gitdir: <path>``; that gitdir's
     ``commondir`` file (relative to the gitdir) names the common directory, and a gitdir with no
     ``commondir`` (a ``--separate-git-dir`` or submodule layout) is its own common directory,
     the answer ``git rev-parse --git-common-dir`` gives. A spawn costs about 55 ms here (it
     loads structlog and ~230 modules) against 0.1 ms for the read.
     FALLBACK: only when ``GIT_DIR``, ``GIT_COMMON_DIR`` or ``GIT_CEILING_DIRECTORIES`` is set
     (they redirect or stop git's search away from the layout on disk) or the layout is
     unrecognised (a ``.git`` directory or a ``gitdir:`` target without ``HEAD``, a ``gitdir:``
     line that is malformed or points nowhere, a ``commondir`` that points nowhere)
     does it run ``git rev-parse --path-format=absolute --git-common-dir`` through
     ``run_bounded`` (deferred import; 5 s limit). Not matched to git by the read:
     filesystem-boundary stops and ``safe.directory``. In a ``--separate-git-dir``, submodule or
     bare-repository worktree layout the primary is the git directory's parent, as git reports
     it, which (inferred, not observed) is not the directory Claude Code keyed its memory by;
     the result is that parent's memory directory when one exists, which is the wrong one, else
     ``None`` (step 3).
3. :func:`memory_dir` returns ``None`` when *cwd* is not inside a git repository (no ``.git``
   above it), git is unavailable or slow on the fallback path, or the resulting directory does
   not exist. The caller stays silent; this module never raises.

Claude Code also shortens a very long mangled key; such a path is not reproduced here and
resolves to ``None`` (silent), which is the safe failure for a hook.
"""
from __future__ import annotations

import json
import os
import re
from collections.abc import Mapping
from pathlib import Path

_NON_ALNUM = re.compile(r"[^A-Za-z0-9]")


def mangle_path(path: str | os.PathLike[str]) -> str:
    """Claude Code's project key: every non-alphanumeric character becomes ``-``."""
    return _NON_ALNUM.sub("-", os.fspath(path))


def claude_config_dir(env: Mapping[str, str] | None = None, home: Path | None = None) -> Path:
    """``$CLAUDE_CONFIG_DIR`` when set and non-empty, else ``~/.claude``."""
    e = os.environ if env is None else env
    configured = e.get("CLAUDE_CONFIG_DIR", "")
    if configured:
        return Path(configured).expanduser()
    return (home if home is not None else Path.home()) / ".claude"


class _Unrecognised(Exception):
    """The ``.git`` layout on disk is not one the read understands; ask git."""


def _real(path: str | os.PathLike[str]) -> Path:
    return Path(os.path.realpath(path))


def _first_line(path: Path) -> str:
    try:
        with path.open(encoding="utf-8", errors="replace") as fh:
            return fh.readline().strip()
    except OSError as exc:
        raise _Unrecognised(f"cannot read {path}") from exc


def _common_dir_from_gitfile(dot_git: Path) -> Path:
    line = _first_line(dot_git)
    if not line.startswith("gitdir:"):
        raise _Unrecognised(f"{dot_git} has no gitdir line")
    target = line[len("gitdir:"):].strip()
    if not target:
        raise _Unrecognised(f"{dot_git} has an empty gitdir line")
    gitdir = Path(os.path.join(dot_git.parent, target))  # an absolute target wins the join
    if not gitdir.is_dir():
        raise _Unrecognised(f"{gitdir} is not a directory")
    if not (gitdir / "HEAD").is_file():
        raise _Unrecognised(f"{gitdir} has no HEAD")
    commondir_file = gitdir / "commondir"
    if not commondir_file.is_file():
        return _real(gitdir)  # a separate-git-dir or submodule gitdir is its own common directory
    relative = _first_line(commondir_file)
    if not relative:
        raise _Unrecognised(f"{commondir_file} is empty")
    common = Path(os.path.join(gitdir, relative))
    if not common.is_dir():
        raise _Unrecognised(f"{common} is not a directory")
    return _real(common)


def _common_dir_by_reading(cwd: Path) -> Path | None:
    """The git common directory of the repository containing *cwd*, read from disk.

    None when no ``.git`` exists at or above *cwd* (not a repository). Raises
    :class:`_Unrecognised` for a layout it cannot interpret. Spawns nothing.
    """
    here = Path(os.path.abspath(cwd))
    if not here.is_dir():
        return None  # git -C refuses a directory that does not exist; so does this
    for directory in (here, *here.parents):
        dot_git = directory / ".git"
        if dot_git.is_dir():
            if not (dot_git / "HEAD").is_file():
                raise _Unrecognised(f"{dot_git} is a directory without HEAD")
            return _real(dot_git)
        if dot_git.is_file():
            return _common_dir_from_gitfile(dot_git)
    return None


def primary_checkout_root(cwd: Path, env: Mapping[str, str] | None = None) -> Path | None:
    """The primary checkout's root: the parent of the git common directory, or None.

    Reads ``.git`` and spawns nothing; asks git (:func:`_primary_via_git`) only when ``GIT_DIR``,
    ``GIT_COMMON_DIR`` or ``GIT_CEILING_DIRECTORIES`` is set in *env* or the layout is unrecognised. See the module docstring.
    """
    e = os.environ if env is None else env
    if not (e.get("GIT_DIR") or e.get("GIT_COMMON_DIR") or e.get("GIT_CEILING_DIRECTORIES")):
        try:
            common = _common_dir_by_reading(cwd)
        except _Unrecognised:
            pass
        else:
            return None if common is None else common.parent
    return _primary_via_git(cwd)


def _primary_via_git(cwd: Path) -> Path | None:
    """The fallback: ask git. The imports are deferred because they pull structlog and ~230 modules."""
    # A module-scope run_bounded import takes 14 ms / 106 modules to 62-84 ms / 337 (measured on the
    # deleted hooks/verification_config); only a caller that reaches this fallback pays it.
    from nexus.bounded_subprocess import run_bounded  # noqa: PLC0415 — deferred by the hooks convention, measurement above
    from nexus.util.nx_argv import argv_off_cwd  # noqa: PLC0415 — stdlib-only; kept off the import path until a spawn

    try:
        proc = run_bounded(
            argv_off_cwd(["git", "-C", os.fspath(cwd), "rev-parse", "--path-format=absolute", "--git-common-dir"]),
            timeout=5,
        )
    except Exception:  # noqa: BLE001 — a missing git, a timeout or a spawn failure is "not found", never an error
        return None
    common = proc.stdout.strip() if proc.returncode == 0 else ""
    if not common:
        return None
    return Path(common).parent


def _custom_from(settings: Path, home: Path) -> Path | None:
    try:
        data = json.loads(settings.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None
    value = data.get("autoMemoryDirectory") if isinstance(data, dict) else None
    if not isinstance(value, str) or not value:
        return None
    if value.startswith("~/"):
        candidate = home / value[2:]
    elif os.path.isabs(value):
        candidate = Path(value)
    else:
        return None
    return candidate if candidate.is_dir() else None


def memory_dir(
    cwd: Path | None = None,
    *,
    env: Mapping[str, str] | None = None,
    home: Path | None = None,
) -> Path | None:
    """The auto-memory directory for the repository containing *cwd*, or None. See the module docstring."""
    try:
        start = Path.cwd() if cwd is None else Path(cwd)
        h = home if home is not None else Path.home()
        config = claude_config_dir(env, h)
        primary = primary_checkout_root(start, env)
        if primary is None:
            return None
        sources = [
            start / ".claude" / "settings.local.json",
            start / ".claude" / "settings.json",
            primary / ".claude" / "settings.local.json",
            primary / ".claude" / "settings.json",
            config / "settings.json",
        ]
        for settings in sources:
            custom = _custom_from(settings, h)
            if custom is not None:
                return custom
        default = config / "projects" / mangle_path(primary) / "memory"
        return default if default.is_dir() else None
    except Exception:  # noqa: BLE001 — a hook-importable helper never raises; the caller stays silent
        return None
