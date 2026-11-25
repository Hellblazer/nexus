# SPDX-License-Identifier: AGPL-3.0-or-later
"""Run a plugin hook script with the account home pointed at a tmp directory.

The audience helper (``conexus/hooks/scripts/_audience.py``) reads the account
home from the OS account database, so no environment variable moves it. The one
test seam is the in-process module attribute ``_ACCOUNT_HOME_OVERRIDE``; this
harness sets it and then runs the script under ``runpy`` as ``__main__``, so the
script's own ``import _audience`` gets the same, already-overridden module.

    python audience_harness.py <account-home> <script> [script args...]

stdin and stdout pass through. Keep this file out of ``tests/e2e/lib/`` and
``tests/e2e/hook-cli-skew/``: those prefixes make a change ineligible for the
plugin-only fast lane (``scripts/check_plugin_only_eligibility.py``).
"""
from __future__ import annotations

import os
import runpy
import sys

#: The tracked file only a nexus checkout has (``_audience.MARKERS``).
MARKER = "scripts/agent-worktree-preflight.sh"


def mark_nexus(repo: "os.PathLike[str] | str") -> None:
    """Give *repo* the nexus marker file, so the guard sees a nexus checkout."""
    marker = os.path.join(os.fspath(repo), *MARKER.split("/"))
    os.makedirs(os.path.dirname(marker), exist_ok=True)
    with open(marker, "w", encoding="utf-8") as handle:
        handle.write("#!/bin/sh\n")


def marked_repo(path: "os.PathLike[str] | str") -> str:
    """A directory with a ``.git`` entry and the nexus marker: ``nexus_repo`` is True for a payload naming it."""
    os.makedirs(os.path.join(os.fspath(path), ".git"), exist_ok=True)
    mark_nexus(path)
    return os.fspath(path)


def plain_repo(path: "os.PathLike[str] | str") -> str:
    """A directory with a ``.git`` entry and no marker: a user's repo. ``nexus_repo`` is False for a payload naming it."""
    os.makedirs(os.path.join(os.fspath(path), ".git"), exist_ok=True)
    return os.fspath(path)


def unflagged_home(home: "os.PathLike[str] | str") -> str:
    """An account home with no maintainer flag; returns it as a string."""
    os.makedirs(os.fspath(home), exist_ok=True)
    return os.fspath(home)


def flagged_home(home: "os.PathLike[str] | str") -> str:
    """An account home that carries the maintainer flag; returns it as a string."""
    flag = os.path.join(os.fspath(home), ".config", "nexus", "maintainer-profile")
    os.makedirs(os.path.dirname(flag), exist_ok=True)
    with open(flag, "w", encoding="utf-8"):
        pass
    return os.fspath(home)


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


def main(argv: list[str]) -> None:
    if len(argv) < 3:
        sys.exit("usage: audience_harness.py <account-home> <script> [args...]")
    home, script, rest = argv[1], argv[2], argv[3:]
    scripts_dir = os.path.normpath(SCRIPTS)
    if scripts_dir not in sys.path:
        sys.path.insert(0, scripts_dir)
    import _audience  # noqa: PLC0415 -- after the sys.path insert

    _audience._ACCOUNT_HOME_OVERRIDE = home
    sys.argv = [script, *rest]
    runpy.run_path(script, run_name="__main__")


if __name__ == "__main__":
    main(sys.argv)
