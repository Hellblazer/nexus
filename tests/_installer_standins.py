# SPDX-License-Identifier: AGPL-3.0-or-later
"""Stand-ins and strings shared by ``tests/test_install_sh.py`` and ``tests/test_install_ps1.py``
(RDR-228 Phase 3, nexus-sv2wx.26).

POSIX shell stand-ins for ``uv``, ``nx`` and ``claude``: each appends its arguments to one record
file, which is how the installer tests see what the script ran. ``{rec}`` is that file and
``{root}`` the sandbox directory, filled in by the caller. The module imports nothing outside the
standard library, so it loads on Windows too.
"""

# What the script runs for the CLI build, word for word (the bead's Build item 3). The
# requirement is ``conexus@latest``: a bare ``conexus`` reuses the environment ``uv`` cached on an
# earlier run, which may predate ``--bootstrap`` (nexus-sv2wx.28).
BOOTSTRAP = "uv tool run --python 3.12 --from conexus@latest nx self install --bootstrap"

# Strings the script prints that the tests look for.
SERVICE_QUESTION = "How should Nexus run its storage service?"
AUTOSTART_QUESTION = "Start the service at login?"

UV_STANDIN = """#!/bin/sh
printf 'uv %s\\n' "$*" >> "{rec}"
if [ "$1" = tool ] && [ "$2" = run ]; then
    if [ -e "{root}/fail-bootstrap" ]; then
        echo "stand-in uv: the build failed" >&2
        exit 1
    fi
    printf 'UV_TORCH_BACKEND=%s\\n' "${{UV_TORCH_BACKEND-<unset>}}" >> "{root}/uv-env.log"
    if [ ! -e "{root}/skip-nx" ]; then
        mkdir -p "$HOME/.local/bin"
        cp "{root}/nx-standin" "$HOME/.local/bin/nx"
        chmod +x "$HOME/.local/bin/nx"
    fi
fi
exit 0
"""

NX_STANDIN = """#!/bin/sh
if [ "$1" = "--version" ]; then
    echo "nx, version 9.9.9"
    exit 0
fi
printf 'nx %s\\n' "$*" >> "{rec}"
printf '%s NX_LOCAL=%s\\n' "$1" "${{NX_LOCAL-<unset>}}" >> "{root}/nx-env.log"
if [ -e "{root}/fail-nx" ]; then
    exit 1
fi
exit 0
"""

FOREIGN_NX = """#!/bin/sh
echo "21.0.0"
"""

CLAUDE_STANDIN = """#!/bin/sh
printf 'claude %s\\n' "$*" >> "{rec}"
if [ -e "{root}/fail-claude" ]; then
    exit 1
fi
exit 0
"""

