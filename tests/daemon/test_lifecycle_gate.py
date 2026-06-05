# SPDX-License-Identifier: AGPL-3.0-or-later
"""RDR-149 P6: the daemon-lifecycle standing gate, mechanically enforced.

The standing gate (``src/nexus/daemon/AGENTS.md``) says any future lifecycle
fix lands in the shared primitive plus the conformance suite, never in one
tier's bespoke copy. A docs rule alone degrades to "hope" for a fast-moving
or context-compressed agent, so this test is the tripwire -- the lifecycle
analogue of ``test_storage_boundary_lint.py`` (RDR-120/146 boundary-lint
discipline). It fails CI if a deleted bespoke pattern is reintroduced or if
the lease record is reimplemented outside the primitive.

It does NOT try to ban lifecycle *verbs* in tier consumers (they legitimately
orchestrate publish/heartbeat/relinquish by CALLING the primitive); it bans
the specific dead-code shapes P5 removed and pins the primitive's single
source of truth.
"""
from __future__ import annotations

import pathlib

REPO_ROOT = pathlib.Path(__file__).parent.parent.parent
SRC_ROOT = REPO_ROOT / "src" / "nexus"
PRIMITIVE = SRC_ROOT / "daemon" / "service_registry.py"

# The bespoke T1 addr-file lifecycle functions deleted in RDR-149 P5. None may
# be redefined anywhere in the package: liveness is lease freshness (TTL), not
# a pid-keyed addr file. (RDR-149 §Validation: "no per-tier orphan sweep".)
_BANNED_DEFS = (
    "write_t1_addr",
    "read_t1_addr_for",
    "unlink_t1_addr",
    "t1_addr_path",
    "sweep_orphan_t1_addr_files",
)

_ALLOW_TOKEN = "# lifecycle-gate-allow:"


def _py_files() -> list[pathlib.Path]:
    return [p for p in SRC_ROOT.rglob("*.py") if "__pycache__" not in p.parts]


def test_deleted_addr_file_functions_stay_deleted() -> None:
    """No bespoke pid-keyed T1 addr-file function is redefined (P5 deletion
    stays at zero). Reintroducing one is the exact recurrence the gate stops."""
    offenders: list[str] = []
    for path in _py_files():
        for lineno, line in enumerate(path.read_text(encoding="utf-8").splitlines(), 1):
            stripped = line.lstrip()
            for name in _BANNED_DEFS:
                if stripped.startswith(f"def {name}(") and _ALLOW_TOKEN not in line:
                    rel = path.relative_to(REPO_ROOT)
                    offenders.append(f"{rel}:{lineno}: def {name}")
    assert not offenders, (
        "RDR-149 lifecycle gate: a deleted bespoke addr-file function was "
        "reintroduced. Lifecycle ownership/liveness lives in the leased "
        "registry (daemon/service_registry.py), not a pid-keyed addr file. "
        "Offenders:\n  " + "\n  ".join(offenders)
    )


def test_lease_record_defined_only_in_primitive() -> None:
    """``LeaseRecord`` -- the on-disk lease shape -- is defined exactly once,
    in the primitive. A tier defining its own lease record is reimplementing
    the substrate (the bug class the gate exists to prevent)."""
    definitions: list[pathlib.Path] = []
    for path in _py_files():
        for line in path.read_text(encoding="utf-8").splitlines():
            if line.lstrip().startswith("class LeaseRecord") and _ALLOW_TOKEN not in line:
                definitions.append(path)
    assert definitions == [PRIMITIVE], (
        "RDR-149 lifecycle gate: LeaseRecord must be defined ONLY in "
        "daemon/service_registry.py (the single substrate). Found: "
        f"{[str(p.relative_to(REPO_ROOT)) for p in definitions]}"
    )


def test_election_flock_only_in_primitive() -> None:
    """The per-scope election flock (``_elect``) is a method of the primitive's
    ``ServiceRegistry`` only. A tier opening its own per-scope election lock is
    bespoke election outside the substrate (RDR-149 §Validation)."""
    offenders: list[str] = []
    for path in _py_files():
        if path == PRIMITIVE:
            continue
        for lineno, line in enumerate(path.read_text(encoding="utf-8").splitlines(), 1):
            if line.lstrip().startswith("def _elect(") and _ALLOW_TOKEN not in line:
                offenders.append(f"{path.relative_to(REPO_ROOT)}:{lineno}")
    assert not offenders, (
        "RDR-149 lifecycle gate: a per-scope election flock was defined "
        "outside the primitive. Election lives in ServiceRegistry._elect; "
        "tiers consume publish()/heartbeat(). Offenders:\n  "
        + "\n  ".join(offenders)
    )


def test_gate_doc_exists() -> None:
    """The standing-gate doc itself must exist and name the primitive + the
    conformance suite -- the two artifacts every lifecycle fix must touch."""
    gate = SRC_ROOT / "daemon" / "AGENTS.md"
    assert gate.exists(), "daemon/AGENTS.md (the standing gate) is missing"
    text = gate.read_text(encoding="utf-8")
    assert "service_registry.py" in text
    assert "test_rdr149_lifecycle_conformance.py" in text
