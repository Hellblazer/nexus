# SPDX-License-Identifier: AGPL-3.0-or-later
"""Find Claude Code's auto-memory directory for a repository (RDR-238 gate residual S-10).

Standard library only, on purpose. The SessionStart curation verb (nexus-qo7ie.16) imports
this on every session start, so nothing here may pull in a heavier module
(``tests/hooks/test_hook_runtime_thin.py`` pins the property for the whole package), and
``nx memory curate --files`` (nexus-qo7ie.17) imports the same function, so the two never
disagree about which directory they are looking at.

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
   * the primary checkout is the parent of ``git rev-parse --path-format=absolute
     --git-common-dir`` run in *cwd*, so every linked worktree of a repository (and any
     subdirectory of one) resolves to the one directory its primary checkout owns.
3. :func:`memory_dir` returns ``None`` when *cwd* is not inside a git repository, git is
   unavailable or slow (5 s limit), or the resulting directory does not exist. The caller
   stays silent; this module never raises.

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


def primary_checkout_root(cwd: Path) -> Path | None:
    """The primary checkout's root: the parent of the git common directory, or None."""
    # Deferred, as every hook-side spawn is: a module-scope run_bounded import pulls structlog and
    # about 231 modules (14 ms / 106 modules becomes 62-84 ms / 337, measured on the deleted
    # hooks/verification_config), and only a caller that reaches the git call pays it.
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
        primary = primary_checkout_root(start)
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
