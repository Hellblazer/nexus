# SPDX-License-Identifier: AGPL-3.0-or-later
"""Find a Nexus CLI program: the generation layout first, then PATH.

RDR-228 Phase 1 Step 1 (nexus-sv2wx.5). ``conexus/launch/nx_launch.py`` and
``nx_hook_shim.py`` both start CLI programs (``nx-mcp``, ``nx-hook``, ...) and
must find a fresh install the same way, so the lookup is stated once, here.

The order is the generation layout's ``<tools>/current``, then a ``PATH`` lookup
that skips the current directory (``_exec_path.which_off_cwd``). The layout is
first because an application started before the install does not see a ``PATH``
change until it restarts.

The pointer is resolved BEFORE the program is named: the path returned is under
``<tools>/gen-<stamp>``, never under ``<tools>/current``. ``layout_core`` (WHY
THE SHIM READS THE POINTER BEFORE IT EXECS) gives the reason: an interpreter
started through ``current`` carries that component into ``sys.prefix``, and the
next flip retargets modules inside the running process. A console script's
shebang names its generation's own interpreter, so on POSIX a start through
``current`` was measured not to leak (macOS and Linux, 2026-10-13); the pointer
is resolved anyway, because it is one ``readlink`` and it also holds a server to
the generation it started from across a flip.

On Windows the layout's programs are ``<gen>\\bin\\<name>.exe``, copies of the
generation's own launchers (``generation_core.populate_launchers``). On POSIX
they are ``<gen>/bin/<name>``. Both are found by ``which_off_cwd`` with that one
directory as its ``PATH``, which also gives the Windows branch its ``PATHEXT``
handling.

:func:`tools_dir` restates ``layout_core.tools_dir`` because this module cannot
import it: standard library only, no ``nexus`` import, since hook scripts run
under ``uv tool run python`` on a machine that may have no CLI.
``tests/hooks/test_nx_launch.py`` pins the two statements equal.
"""
from __future__ import annotations

import json
import os
import re
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import _exec_path  # noqa: E402 -- must follow the sys.path insert

#: ``layout_core.TOOLS_DIR_ENV``.
TOOLS_DIR_ENV = "NX_TOOLS_DIR"

#: The only shape a program name may have: a single path component.
#: ``layout_core._COMPONENT_RE``.
PROGRAM_NAME = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]*$")


def tools_dir() -> str | None:
    """The generation root, or ``None`` when an override is refused.

    ``layout_core._resolve_dir``'s rule: unset, empty or blank is the
    ``$HOME``-derived default; a leading ``~`` or ``~/`` is expanded; an absolute
    path is used as given; anything else (a relative path, ``~user``) is refused
    there with an error, and here means there is no layout to search.
    """
    home = os.path.expanduser("~")
    raw = (os.environ.get(TOOLS_DIR_ENV) or "").strip()
    if not raw:
        return os.path.join(home, ".local", "share", "nexus", "tools")
    if raw == "~":
        raw = home
    elif raw.startswith("~/"):
        raw = os.path.join(home, raw[2:])
    return raw if os.path.isabs(raw) else None


def current_generation() -> str | None:
    """The directory ``<tools>/current`` points at, or ``None``.

    One ``readlink``. A junction's target on Windows carries an extended-length
    prefix, which is stripped. A relative target is not followed.
    """
    root = tools_dir()
    if root is None:
        return None
    try:
        target = os.readlink(os.path.join(root, "current"))
    except (OSError, ValueError):
        return None
    for prefix in ("\\\\?\\UNC\\", "\\\\?\\", "\\??\\"):
        if target.startswith(prefix):
            target = ("\\\\" if "UNC" in prefix else "") + target[len(prefix):]
            break
    return target if os.path.isabs(target) else None


def resolve(name: str) -> str | None:
    """The absolute path of the CLI program *name*, or ``None``.

    *name* is a bare program name. Anything with a directory part is refused.
    """
    if not PROGRAM_NAME.match(name):
        return None
    generation = current_generation()
    if generation is not None:
        hit = _exec_path.which_off_cwd(name, path=os.path.join(generation, "bin"))
        if hit is not None:
            return hit
    return _exec_path.which_off_cwd(name)


def missing_note(program: str, what: str) -> str:
    """The one line written when *program* is not found and *what* is skipped."""
    return (
        f"conexus: `{program}` is not installed (no Nexus CLI in the generation "
        f"layout or on PATH), so {what} is skipped.\n"
    )


def project_dir(payload: bytes) -> str | None:
    """The directory a hook program should run in: the payload's ``cwd``, else
    ``CLAUDE_PROJECT_DIR``, else ``None`` (inherit).

    hooks.json starts its scripts with ``uv tool run --directory
    ${CLAUDE_PLUGIN_ROOT}`` so uv cannot execute an interpreter planted in a
    ``.venv`` (finding C, nexus-f9bgu.36); the script's own cwd is therefore the
    plugin root, and the verbs resolve the project from theirs. Only a directory
    that exists counts.
    """
    candidates: list[object] = []
    try:
        data = json.loads(payload)
        if isinstance(data, dict):
            candidates.append(data.get("cwd"))
    except ValueError:
        pass
    candidates.append(os.environ.get("CLAUDE_PROJECT_DIR"))
    for cand in candidates:
        if isinstance(cand, str) and cand and os.path.isdir(cand):
            return cand
    return None
