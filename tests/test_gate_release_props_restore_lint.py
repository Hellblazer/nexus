# SPDX-License-Identifier: AGPL-3.0-or-later
# Copyright (c) 2026 Hal Hildebrand. All rights reserved.
"""Gates that stamp ``release.properties`` restore its pre-invocation BYTES,
never ``git checkout`` it back to HEAD.

nexus-iws18: ``tests/e2e/local-service-gate.sh`` restored the file with
``git checkout -- <path>`` at two sites (the mid-run ``_restore_props`` and
the EXIT-trap backstop). ``git checkout`` reverts to HEAD, so every gate run
silently destroyed any UNCOMMITTED edit to ``release.properties`` — it bit the
nexus-308ph implementer twice mid-verification. ``scripts/build-gate-jar.sh``
already used the right shape (``cp`` to a temp snapshot, ``cp`` back on
exit); the gate now does the same.

Static pin over every shell script that names ``release.properties``:

* no COMMAND line runs ``git checkout`` (comments may mention it);
* the local-service gate takes a ``cp`` snapshot of ``$RELEASE_PROPS`` and
  restores from that snapshot at both sites.

Non-vacuity: the scan must find at least the two scripts known to stamp the
file; an empty scan is a failure, not a pass.
"""
from __future__ import annotations

import re
from pathlib import Path

import pytest

pytestmark = pytest.mark.lint

REPO_ROOT = Path(__file__).resolve().parent.parent
_SCAN_DIRS = (REPO_ROOT / "tests" / "e2e", REPO_ROOT / "scripts")
_GATE = REPO_ROOT / "tests" / "e2e" / "local-service-gate.sh"
_JAR_SCRIPT = REPO_ROOT / "scripts" / "build-gate-jar.sh"
_LEASE_LIB = REPO_ROOT / "scripts" / "lib" / "release-props-lease.sh"

#: A ``git checkout`` COMMAND (leading whitespace, then the verb) that touches
#: ``release.properties`` — not a comment that names it, and not a checkout of
#: some other ref. On first run this pin found the same defect in two more
#: scripts the bead had not named (run.sh's _guided_restore and
#: published-client-write-gate.sh's cleanup); all three now snapshot bytes.
_CHECKOUT_CMD = re.compile(r"^\s*git checkout\b[^\n]*(release\.properties|RELEASE_PROPS)", re.M)


def _scripts_naming_release_props() -> list[Path]:
    found: list[Path] = []
    for base in _SCAN_DIRS:
        for path in sorted(base.rglob("*.sh")):
            if "release.properties" in path.read_text(encoding="utf-8", errors="replace"):
                found.append(path)
    return found


def test_scan_is_not_vacuous() -> None:
    found = _scripts_naming_release_props()
    assert _GATE in found and _JAR_SCRIPT in found, (
        f"expected both known stampers in the scan, got {[p.name for p in found]}"
    )


def test_no_script_restores_release_props_via_git_checkout() -> None:
    offenders: list[str] = []
    for path in _scripts_naming_release_props():
        text = path.read_text(encoding="utf-8", errors="replace")
        for m in _CHECKOUT_CMD.finditer(text):
            line_no = text.count("\n", 0, m.start()) + 1
            offenders.append(f"{path.relative_to(REPO_ROOT)}:{line_no}")
    assert not offenders, (
        "`git checkout` reverts release.properties to HEAD and destroys uncommitted "
        f"edits on every run (nexus-iws18); snapshot the bytes instead: {offenders}"
    )


def test_local_service_gate_snapshots_and_restores_bytes() -> None:
    """Since nexus-iexvl the gate takes its byte snapshot and restores it
    through scripts/lib/release-props-lease.sh, the helper run.sh and
    build-gate-jar.sh share, instead of its own ``cp`` lines. Pin both halves:
    the gate routes through the helper, and the helper copies bytes."""
    text = _GATE.read_text(encoding="utf-8")
    lib = _LEASE_LIB.read_text(encoding="utf-8")
    assert re.search(r'^source "[^"]*scripts/lib/release-props-lease\.sh"', text, re.M), (
        "the gate no longer sources the shared release-props helper"
    )
    assert re.search(
        r'^\s*RELEASE_PROPS_SNAPSHOT="\$\(release_props_stamp_under_lease ', text, re.M
    ), "the gate no longer snapshots release.properties through the shared helper"
    restores = re.findall(
        r'^\s*release_props_restore_and_release "\$RELEASE_PROPS" "\$RELEASE_PROPS_SNAPSHOT"',
        text,
        re.M,
    )
    assert len(restores) >= 2, (
        f"expected every stamp exit path to restore through the helper; found {len(restores)}"
    )
    # The EXIT backstop still restores bytes from the snapshot, and the
    # snapshot variable exists before cleanup() is defined.
    assert re.search(r'cp "\$RELEASE_PROPS_SNAPSHOT" "\$RELEASE_PROPS"', text), (
        "the EXIT backstop no longer restores from the snapshot"
    )
    assert text.index('RELEASE_PROPS_SNAPSHOT=""') < text.index("cleanup() {")
    # The helper itself snapshots and restores BYTES with cp.
    assert re.search(r'^\s*cp "\$props" "\$snapshot"$', lib, re.M), (
        "the shared helper no longer snapshots release.properties bytes"
    )
    assert re.search(r'^\s*cp "\$snapshot" "\$props"', lib, re.M), (
        "the shared helper no longer restores release.properties bytes"
    )
