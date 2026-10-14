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
next flip retargets modules inside the running process. Measured 2026-10-13 on
macOS (uv 0.8.0) and Linux (uv 0.12.24): a console script uv built has a shebang
that names its generation's own interpreter, so started through ``current`` it
shows no ``current`` in ``sys.prefix`` or ``sys.path``; the interpreter itself
started through ``current`` shows it in both. The pointer is resolved anyway. It
is one ``readlink``, and the rule then does not depend on how a generation's
scripts happen to be written.

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
        # No home directory known: ``expanduser`` hands "~" back, and a relative
        # root would be read against the working directory.
        return os.path.join(home, ".local", "share", "nexus", "tools") if os.path.isabs(home) else None
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

    The path returned is absolute. On POSIX ``shutil.which`` returns a relative
    hit for a relative ``PATH`` entry, and the callers change directory to the
    project before they start the program, so a relative hit would name whatever
    the project holds at that path. Relative entries are therefore dropped first,
    as ``which_off_cwd`` already drops them on Windows.
    """
    if not PROGRAM_NAME.match(name):
        return None
    generation = current_generation()
    if generation is not None:
        hit = _exec_path.which_off_cwd(name, path=os.path.join(generation, "bin"))
        if hit is not None:
            return hit
    if sys.platform == "win32":
        return _exec_path.which_off_cwd(name)
    entries = [e for e in os.environ.get("PATH", "").split(os.pathsep) if os.path.isabs(e)]
    return _exec_path.which_off_cwd(name, path=os.pathsep.join(entries))


#: How much of a program is read to find its shebang line.
_SHEBANG_BYTES = 256


def _venv_problem(exe: str) -> str | None:
    """Why the venv *exe* sits in cannot run, from its ``pyvenv.cfg``, or ``None``.

    ``<venv>/bin/<name>`` (POSIX) and ``<venv>\\bin\\<name>.exe`` or
    ``<venv>\\Scripts\\<name>.exe`` (Windows) all have the venv two levels up.
    ``pyvenv.cfg`` names the directory of the interpreter the venv was built from
    (``home``), and ``uv`` removes that directory when it prunes the Python. A
    generation's launchers are copies with no shebang on Windows, so this is the
    only evidence there. A missing or unreadable ``pyvenv.cfg``, or one with no
    ``home`` line, is no evidence.
    """
    root = os.path.dirname(os.path.dirname(exe))
    try:
        with open(os.path.join(root, "pyvenv.cfg"), encoding="utf-8", errors="replace") as cfg:
            text = cfg.read(8192)
    except OSError:
        return None
    for line in text.splitlines():
        key, sep, value = line.partition("=")
        if sep and key.strip() == "home":
            home = value.strip()
            if home and not os.path.isdir(home):
                return f"the Python it was built with ({home}) is gone"
            return None
    return None


def _shebang_problem(exe: str) -> str | None:
    """Why the script *exe* cannot be started, from its ``#!`` line, or ``None``.

    Only a shebang whose meaning is certain counts: an absolute interpreter path
    (it must exist and be executable; a dangling symlink, which is how ``uv``
    leaves a pruned interpreter, does not exist) or ``/usr/bin/env <name>`` (the
    name must be on ``PATH``). A relative interpreter, ``env`` with options or an
    assignment, and any file that does not start with ``#!`` are not judged.
    """
    try:
        with open(exe, "rb") as handle:
            head = handle.read(_SHEBANG_BYTES)
    except OSError:
        return None
    if not head.startswith(b"#!"):
        return None
    fields = head[2:].split(b"\n", 1)[0].decode("utf-8", "replace").split()
    if not fields:
        return None
    interpreter = fields[0]
    if os.path.basename(interpreter) == "env":
        name = fields[1] if len(fields) > 1 else ""
        if not name or name.startswith("-") or "=" in name or os.path.dirname(name):
            return None
        return None if _exec_path.which_off_cwd(name) else f"it needs `{name}`, which is not on PATH"
    if not os.path.isabs(interpreter):
        return None
    if not os.path.exists(interpreter):
        return f"its interpreter ({interpreter}) is missing"
    if not os.access(interpreter, os.X_OK):
        return f"its interpreter ({interpreter}) is not executable"
    return None


def cannot_start(exe: str, *, windows: bool | None = None) -> str | None:
    """Why the program at *exe* cannot start, or ``None`` when nothing says so.

    RDR-228 Phase 2 Step 4 (nexus-sv2wx.17; Sam, 2026-10-14). The lookup finds a
    program by its file, and an install whose interpreter ``uv`` pruned still has
    the file. Started, it fails. The answer is from files alone, a few stats and
    two small reads, because the launcher and the session-start hook decide with
    it in milliseconds: no process is started to ask. It claims "cannot start" only
    on positive evidence, so a program in a directory with no ``pyvenv.cfg``
    (the suite's fakes, a binary) is runnable here.

    Two sources of evidence. The venv around the program (``pyvenv.cfg``'s
    ``home`` is not a directory), on every platform. On POSIX also the program's
    own shebang (:func:`_shebang_problem`), for a program outside a venv or whose
    venv answers. The path is checked as given and, when it is a link, as it
    resolves. *windows* selects the Windows branch (no shebang) for a test.
    """
    windows = (os.name == "nt") if windows is None else windows
    try:
        candidates = [exe]
        real = os.path.realpath(exe)
        if real != exe:
            candidates.append(real)
        for candidate in candidates:
            problem = _venv_problem(candidate)
            if problem is not None:
                return problem
        return None if windows else _shebang_problem(exe)
    except (OSError, ValueError):
        return None


def missing_note(program: str, what: str) -> str:
    """The one line written when *program* is not found and *what* is skipped."""
    return (
        f"conexus: `{program}` is not installed (not in the Nexus install "
        f"directory, not on PATH), so {what} is skipped.\n"
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
