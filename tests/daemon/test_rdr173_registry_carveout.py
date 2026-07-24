# SPDX-License-Identifier: AGPL-3.0-or-later
"""RDR-173 X-DEP (bead nexus-s8na3) — registry carve-out guard.

RDR-152 Phase 4 (nexus-gmiaf.24) decommissions the SQLite single-writer daemon
lifecycle class: it deletes ``src/nexus/daemon/`` modules ``t2_daemon``,
``t3_daemon``, ``discovery``, ``spin_guard``, ``t1_lease``, ``t2_client``
(clients), ``catalog_write_shim``. But RDR-173's leased aspect-worker daemon is a
CONTINUING CONSUMER of the RDR-149 leased service-registry substrate, so two
files MUST SURVIVE that deletion:

  - ``src/nexus/daemon/service_registry.py`` — the leased-registry primitive
    (lease / heartbeat / single-flight election), a leaf module.
  - ``src/nexus/daemon/aspect_worker_daemon.py`` — the RDR-173 daemon that rides
    it.

DECISION (carve-out): RETAIN IN PLACE (do not relocate). The ``daemon/`` directory
is NOT fully deleted — it also retains the engine-binary management
(``binary_install`` / ``binary_lifecycle`` / ``installer`` /
``storage_service_daemon``), so there is no empty-directory reason to relocate;
relocating would add import churn + moved-file risk for no benefit.

This test MECHANICALLY pins the carve-out: the surviving substrate must import
NOTHING from the RDR-152-P4 delete-set, so deleting those modules cannot break
it. If a future change makes ``service_registry`` or ``aspect_worker_daemon``
depend on a to-be-deleted module, this fails — flagging the carve-out violation
before P4 trips over it.
"""
from __future__ import annotations

import ast
from pathlib import Path

import pytest

# The src/nexus/daemon/ modules RDR-152 Phase 4 (gmiaf.24) deletes.
_P4_DELETE_SET = frozenset({
    "t2_daemon", "discovery", "spin_guard",
    "t1_lease", "t2_client", "catalog_write_shim",
})

# The RDR-173 leased-registry substrate that MUST survive P4.
_SURVIVING = ("service_registry.py", "aspect_worker_daemon.py")

_DAEMON_DIR = Path(__file__).resolve().parents[2] / "src" / "nexus" / "daemon"


def _daemon_imports(path: Path) -> set[str]:
    """The set of ``nexus.daemon.<X>`` submodules *path* imports (X only)."""
    tree = ast.parse(path.read_text())
    found: set[str] = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.ImportFrom) and (node.module or "").startswith("nexus.daemon."):
            found.add(node.module.split(".")[2])
        elif isinstance(node, ast.Import):
            for alias in node.names:
                if alias.name.startswith("nexus.daemon."):
                    found.add(alias.name.split(".")[2])
    return found


@pytest.mark.parametrize("name", _SURVIVING)
def test_surviving_substrate_exists(name: str) -> None:
    assert (_DAEMON_DIR / name).is_file(), (
        f"{name} is the RDR-173 leased-registry substrate; it MUST survive the "
        "RDR-152 Phase-4 daemon/ deletion (nexus-s8na3 carve-out)"
    )


@pytest.mark.parametrize("name", _SURVIVING)
def test_surviving_substrate_imports_nothing_deleted(name: str) -> None:
    """The surviving files must not depend on any P4-deleted module, so deleting
    those modules cannot break the aspect-worker substrate."""
    bad = _daemon_imports(_DAEMON_DIR / name) & _P4_DELETE_SET
    assert not bad, (
        f"{name} imports {sorted(bad)} which RDR-152 P4 deletes — the carve-out "
        "(nexus-s8na3) requires the surviving substrate to be import-independent "
        "of the SQLite-daemon lifecycle class"
    )


def test_aspect_worker_daemon_rides_the_registry() -> None:
    """The substrate link itself: the aspect-worker daemon must depend on the
    registry primitive (else it isn't riding the leased substrate at all)."""
    assert "service_registry" in _daemon_imports(_DAEMON_DIR / "aspect_worker_daemon.py")
