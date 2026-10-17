# SPDX-License-Identifier: AGPL-3.0-or-later
"""Stand-ins and strings shared by ``tests/test_install_sh.py`` and ``tests/test_install_ps1.py``
(RDR-228 Phase 3, nexus-sv2wx.26).

POSIX shell stand-ins for ``uv``, ``nx`` and ``claude``: each appends its arguments to one record
file, which is how the installer tests see what the script ran. ``{rec}`` is that file and
``{root}`` the sandbox directory, filled in by the caller. The module imports nothing outside the
standard library, so it loads on Windows too.
"""

import json
from pathlib import Path

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


# ---- the CLI-only record (nexus-sv2wx.31) ------------------------------------------------------
# An installer run that ends CLI only writes the plugin's setup record (``setup/status.json``
# under the Nexus config directory), so the session-start hook does not ask about a service at
# every session. The two installer test files check it the same way.

#: The keys every reader of the record needs.
RECORD_REQUIRED = {"state", "stage", "mode", "started_at", "updated_at", "message", "next", "steps_done"}


def check_the_record_quiets_the_hook(config: Path, work: Path) -> None:
    """Read *config* (the Nexus config directory an installer run wrote) the way the plugin
    does, and run the real session-start hook against it on a machine that has the CLI and no
    service. The same machine without the record is told to ask; with it, the hook is quiet."""
    import importlib  # noqa: PLC0415 -- the hook tests are heavy; only the callers of this need them
    import shutil  # noqa: PLC0415 -- same

    hook = importlib.import_module("tests.hooks.test_nx_setup_notice")
    work.mkdir(parents=True, exist_ok=True)
    record = json.loads((config / "setup" / "status.json").read_text(encoding="utf-8"))
    assert RECORD_REQUIRED <= set(record), record
    assert record["state"] == "done" and record["mode"] == "cli-only" and record["steps_done"] == ["build"]

    view = hook._load(hook._LAUNCH / "nx_setup.py", "nx_setup_installer_record").status_view(config / "setup")
    assert (view["state"], view["mode"], view["live"], view["not_now"]) == ("done", "cli-only", False, False)
    assert hook._load(hook._HOOK, "nx_setup_notice_installer_record").chose_cli_only(view) is True

    bare = hook._build(hook._dirs(work / "without"), hook._cli)
    asks = hook._says(hook._run(bare))
    assert asks is not None and hook._ASK_FIRST.search(asks), "without the record the hook asks"

    recorded = hook._build(hook._dirs(work / "with"), hook._cli)
    shutil.copytree(config / "setup", recorded["config"] / "setup")
    quiet = hook._says(hook._run(recorded))
    assert hook._quiet(quiet), quiet

