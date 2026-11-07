# SPDX-License-Identifier: AGPL-3.0-or-later
# Copyright (c) 2026 Hal Hildebrand. All rights reserved.
"""RDR-242 Phase 1 Step 1 (nexus-mdbds.3): the Lua runtime dependency.

The sandbox worker of Phase 1 Step 2 is a bare child process
(``sys.executable -I``, which ignores ``PYTHON*`` variables and the user
site directory), so the dependency has to import there on every platform
the package ships for. The worker's other start conditions (empty
environment, ``close_fds``, cwd, ``max_memory``) are Step 2's tests.
This test starts that child and asks Lua 5.4 for 1+1.
"""

from __future__ import annotations

import json
import subprocess
import sys

_CHILD = (
    "import json, platform, lupa, lupa.lua54 as l; "
    "rt = l.LuaRuntime(); "
    "print(json.dumps({'result': rt.eval('1+1'), 'lua': rt.lua_version, "
    "'lupa': lupa.__version__, 'platform': platform.platform()}))"
)


def test_lua54_evaluates_in_a_bare_child() -> None:
    proc = subprocess.run(
        [sys.executable, "-I", "-c", _CHILD],
        capture_output=True,
        text=True,
        timeout=60,
        check=False,
    )
    assert proc.returncode == 0, f"bare child failed (exit {proc.returncode}):\n{proc.stderr}"
    assert proc.stdout.strip(), f"bare child printed nothing:\n{proc.stderr}"
    report = json.loads(proc.stdout.strip().splitlines()[-1])
    print(f"lupa bare child: {report}")
    assert report["result"] == 2, report
    assert tuple(report["lua"][:2]) == (5, 4), report
