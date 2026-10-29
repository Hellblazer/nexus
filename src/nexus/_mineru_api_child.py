# SPDX-License-Identifier: AGPL-3.0-or-later
"""The mineru-api server's entry point on Windows (nexus-vyy65).

``python -m nexus._mineru_api_child --host H --port P`` replaces the ``mineru-api`` console
script there. The script is a launcher stub that never imports nexus, and
``os.add_dll_directory`` is per process, so a server started from it could not load
onnxruntime or torch from nx's app-local VC++ DLLs (``DLL load failed while importing
onnxruntime_pybind11_state``) on a box with no VC++ redistributable. Importing nexus first
(the package init runs ``_add_vc_runtime_dirs``) puts those directories on this process's
search path before mineru is imported.

The function it runs is the one mineru declares for ``mineru-api`` (its ``console_scripts``
entry point), so a mineru release that moves ``main`` still starts; ``mineru.cli.fast_api:main``
is the fallback when no installed distribution declares it. The lookup is
``nexus._mineru_console.console_entry``, the launcher for MinerU's other console scripts.

Nothing here may import mineru, torch, onnxruntime or magika above the ``import nexus`` line.
"""
from __future__ import annotations

from collections.abc import Callable

import nexus  # noqa: F401 -- runs _add_vc_runtime_dirs() before any import below it
from nexus import _mineru_console


def server_entry() -> Callable[[], object]:
    """mineru-api's ``main``: its ``console_scripts`` entry point, else ``mineru.cli.fast_api.main``."""
    entry = _mineru_console.console_entry("mineru-api")
    if entry is not None:
        return entry
    from mineru.cli.fast_api import main  # noqa: PLC0415 -- must follow `import nexus`

    return main


def main() -> None:
    server_entry()()


if __name__ == "__main__":
    main()
