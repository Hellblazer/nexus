# SPDX-License-Identifier: AGPL-3.0-or-later
# Copyright (c) 2026 Hal Hildebrand. All rights reserved.
"""`scan()` never puts a diagnostic on stdout (bead nexus-b5ugt).

stdout is a hook's output channel. This module is called from
``subagent_start``, whose whole product is text on stdout, so anything
structlog writes there lands inside the context block a subagent
receives.

The interesting half is not this module's own ``_log``. Going in-process
drags the TRANSITIVE logging surface of everything it calls: with the
engine unreachable, ``HttpMemoryStore`` -> ``DataTokenManager`` emits
``data_token_mint_failed`` through its own ambient logger, correctly,
as a library should. Measured before the fix: two lines on stdout, one
of them from a wheel module nobody would audit while reviewing a hook.

Run in a SUBPROCESS deliberately. pytest configures logging, so an
in-process assertion would pass whether or not the fix is present — the
defect only exists in an interpreter that has not configured structlog,
which is exactly the bare-python case a hook driver runs in.
"""
from __future__ import annotations

import subprocess
import sys

_PROBE = """
import sys, io
from nexus.hooks.t2_prefix_scan import scan
buf = io.StringIO(); real = sys.stdout; sys.stdout = buf
try:
    out = scan('nexus')
finally:
    sys.stdout = real
sys.stderr.write('RETURNED:' + out[:40] + '\\n')
print(buf.getvalue(), end='')
"""


def _run_probe() -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        [sys.executable, "-c", _PROBE],
        capture_output=True,
        text=True,
        timeout=120,
        env={"PATH": "/usr/bin:/bin", "HOME": "/tmp", "NX_SERVICE_URL": "http://127.0.0.1:1"},
    )


def test_an_unreachable_engine_puts_nothing_on_stdout() -> None:
    proc = _run_probe()
    assert proc.returncode == 0, proc.stderr[-2000:]
    assert proc.stdout.strip() == "", (
        f"scan() leaked diagnostics onto stdout, which for subagent_start is "
        f"the context channel: {proc.stdout[:400]!r}"
    )


def test_the_probe_actually_exercised_the_failure_path() -> None:
    """Non-vacuity: stdout being empty proves nothing if scan() no-opped.

    The unreachable path must have been taken AND produced its warning,
    otherwise the test above is satisfied by a function that did nothing.
    """
    proc = _run_probe()
    assert "RETURNED:WARNING: T2 memory unreachable" in proc.stderr, (
        f"the probe did not reach the unreachable path; stderr={proc.stderr[-500:]!r}"
    )
