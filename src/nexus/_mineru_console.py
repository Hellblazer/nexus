# SPDX-License-Identifier: AGPL-3.0-or-later
"""Run one of MinerU's console scripts with nx's app-local DLL directories in place (nexus-vyy65).

``python -m nexus._mineru_console <script> [args...]`` is ``<script> [args...]`` for any console
script the installed ``mineru`` declares (``mineru-api``, ``mineru-models-download``, ...). On
Windows those scripts are launcher stubs that never import nexus, and ``os.add_dll_directory``
is per process, so a stub on a box with no VC++ redistributable dies with ``WinError 126`` on
torch's ``c10.dll`` or ``DLL load failed while importing onnxruntime_pybind11_state``. Importing
nexus first (the package init runs ``_add_vc_runtime_dirs``) puts nx's app-local copies on this
process's search path before mineru is imported. The entry point is resolved through
``importlib.metadata`` from the ``console_scripts`` the ``mineru`` distribution itself declares (no
other package's script is launchable), so a mineru release that moves a script's function still runs.

Nothing here may import mineru, torch, onnxruntime or magika above the ``import nexus`` line.
"""
from __future__ import annotations

import importlib.metadata
import sys
from collections.abc import Callable

import nexus  # noqa: F401 -- runs _add_vc_runtime_dirs() before any import below it


def console_entry(name: str) -> Callable[[], object] | None:
    """The function the installed ``mineru`` distribution declares for console script *name*, or ``None``
    (also when mineru is not installed)."""
    try:
        distribution = importlib.metadata.distribution("mineru")
    except importlib.metadata.PackageNotFoundError:
        return None
    for entry_point in distribution.entry_points:
        if entry_point.group == "console_scripts" and entry_point.name == name:
            return entry_point.load()
    return None


def main(argv: list[str] | None = None) -> None:
    """Run ``argv[0]`` (a mineru console script) with ``argv[1:]`` and exit with its status.

    ``sys.argv`` becomes ``[script, *arguments]``, as a console-script stub leaves it. (click's usage line
    does not come from it under ``python -m``: it reads ``python -m nexus.<script>``, which is cosmetic.)"""
    args = sys.argv[1:] if argv is None else argv
    if not args or not (args[0] == "mineru" or args[0].startswith("mineru-")):
        sys.stderr.write("usage: python -m nexus._mineru_console <mineru-console-script> [args...]\n")
        raise SystemExit(2)
    name, rest = args[0], args[1:]
    entry = console_entry(name)
    if entry is None:
        sys.stderr.write(f"no console script named {name!r} is declared by the installed mineru\n")
        raise SystemExit(2)
    sys.argv = [name, *rest]
    raise SystemExit(entry())


if __name__ == "__main__":
    main()
