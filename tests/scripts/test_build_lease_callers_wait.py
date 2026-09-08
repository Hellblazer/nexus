# SPDX-License-Identifier: AGPL-3.0-or-later
"""Every producer of service/target waits on the shared build lease
(nexus-pv93h): no shell script outside the lease library itself calls the
refuse-immediately ``build_lease_acquire``. The --shakeout native build was
the last bare caller and exited 75 the instant a cached gate-jar copy held
the lease on 2026-09-07; ``build_lease_acquire_wait`` bounded by
``NX_BUILD_LEASE_WAIT`` is the only acquire a caller may use.
"""
from __future__ import annotations

import re
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[2]
LIB = REPO_ROOT / "scripts" / "lib" / "build-lease.sh"
EXEMPT = {LIB, REPO_ROOT / "scripts" / "lib" / "build-lease_test.sh"}
#: A call anywhere on a non-comment line: start of line, or after `&&`, `||`, `;`, `(`, `{`, `then`, `do`.
_BARE = re.compile(r"(?:^|&&|\|\||;|\(|\{|\bthen\b|\bdo\b)\s*build_lease_acquire\s")


def _shell_files():
    for d in ("scripts", "tests", "conexus", "service", "deploy"):
        base = REPO_ROOT / d
        if base.exists():
            yield from base.rglob("*.sh")


def test_no_bare_acquire_outside_the_lease_library() -> None:
    offenders = []
    checked = 0
    for path in _shell_files():
        if path in EXEMPT:
            continue
        checked += 1
        for n, line in enumerate(path.read_text(errors="replace").splitlines(), 1):
            if line.lstrip().startswith("#"):
                continue
            if _BARE.search(line):
                offenders.append(f"{path.relative_to(REPO_ROOT)}:{n}: {line.strip()}")
    assert checked > 10, "the sweep examined almost nothing; check the roots"
    assert not offenders, "use build_lease_acquire_wait service \"${NX_BUILD_LEASE_WAIT:-3600}\" ...:\n" + "\n".join(offenders)
