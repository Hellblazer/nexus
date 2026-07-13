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

import ast
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


# nexus-9nv1d Part B: behaviour-based companion to the name-based bans above.
# The name-bans catch the SPECIFIC dead shapes P5 deleted; this catches a NEW
# bespoke election lock under ANY name by allowlisting the modules that may
# acquire an advisory flock and tripping on any new one.
#
# Why a MODULE allowlist and not "flock only in the primitive": fcntl.flock is
# used legitimately across the tree for distinct, non-election purposes — the
# general _locking primitive, per-tier SPAWN locks (t2/t3/storage daemons), the
# migration serializer, the daemon CLI lock. A "flock => election" test would be
# false-positive-ridden (the vacuous-gate trap nexus-9nv1d itself warns of).
# The defensible signal is: a flock acquire in a module NOT on this vetted list
# is an unreviewed lock — possibly a bespoke election — and must be justified
# (route election through ServiceRegistry._elect, or add the module here with a
# reason). Acquires WITHIN an allowed module are already vetted.
_FLOCK_ALLOWED_MODULES = frozenset({
    "_locking.py",                       # the general advisory-lock primitive
    "daemon/service_registry.py",        # the ONLY election flock (_elect)
    "daemon/storage_service_daemon.py",  # storage-daemon spawn lock
    "daemon/t2_daemon.py",               # T2 spawn / heartbeat locks
    "daemon/t3_daemon.py",               # T3 spawn lock
    "db/migrations.py",                  # migration serialization lock
    "commands/daemon.py",                # daemon CLI single-instance lock
    # verify-fill watermark file lock (nexus-te885.10, review c0e4493e f4):
    # serializes read-modify-write of migration/verify_fill_watermarks.json
    # across concurrent verify-fill runs. NOT daemon-scope election — no
    # lifecycle, no lease, no heartbeat; a plain critical-section around one
    # JSON file.
    "migration/verify_fill_watermark.py",
})


def test_flock_acquire_sites_are_allowlisted() -> None:
    """A flock acquire in a module not on the vetted list is an unreviewed lock
    (possibly a bespoke election) — route election through the primitive or
    justify the lock by adding the module to ``_FLOCK_ALLOWED_MODULES``."""
    offenders: list[str] = []
    for path in _py_files():
        rel = path.relative_to(SRC_ROOT).as_posix()
        if rel in _FLOCK_ALLOWED_MODULES:
            continue
        for lineno, line in _flock_acquire_lines(path.read_text(encoding="utf-8")):
            if _ALLOW_TOKEN not in line:
                offenders.append(f"{path.relative_to(REPO_ROOT)}:{lineno}")
    assert not offenders, (
        "RDR-149 lifecycle gate (nexus-9nv1d): a flock acquire appeared in a "
        "module not on _FLOCK_ALLOWED_MODULES. If this is daemon-scope election, "
        "it MUST go through ServiceRegistry._elect, not a bespoke lock. If it is "
        "a different, legitimate lock, add the module to the allowlist with a "
        "reason. Offenders:\n  " + "\n  ".join(offenders)
    )


def _flock_acquire_lines(text: str) -> list[tuple[int, str]]:
    """Yield (lineno, line) for every flock ACQUIRE in *text*.

    Matches ``fcntl.flock(`` anywhere on the line (not just at line start, so an
    assignment form ``x = fcntl.flock(...)`` is caught — HIGH-2) and excludes
    ``LOCK_UN`` release calls. Gate tests prefer over-matching (a false positive
    is a visible review prompt) to under-matching (a silent miss)."""
    out: list[tuple[int, str]] = []
    for lineno, line in enumerate(text.splitlines(), 1):
        if "fcntl.flock(" in line and "LOCK_UN" not in line:
            out.append((lineno, line))
    return out


def test_flock_acquire_allowlist_is_non_vacuous(tmp_path: pathlib.Path) -> None:
    """Prove the scan actually FIRES on a bespoke flock acquire under a novel
    name (the bead's explicit non-vacuity requirement). A synthetic offender in
    a non-allowlisted module must be detected by the scan logic."""
    synthetic = "import fcntl\n\ndef _my_bespoke_election(fd):\n    fcntl.flock(fd, fcntl.LOCK_EX)\n"
    hits = _flock_acquire_lines(synthetic)
    assert len(hits) == 1, "the scan must detect a bespoke flock acquire"
    assert "LOCK_EX" in hits[0][1]
    # And the release form must NOT be flagged.
    assert _flock_acquire_lines("    fcntl.flock(fd, fcntl.LOCK_UN)\n") == []


def test_gate_doc_exists() -> None:
    """The standing-gate doc itself must exist and name the primitive + the
    conformance suite -- the two artifacts every lifecycle fix must touch."""
    gate = SRC_ROOT / "daemon" / "AGENTS.md"
    assert gate.exists(), "daemon/AGENTS.md (the standing gate) is missing"
    text = gate.read_text(encoding="utf-8")
    assert "service_registry.py" in text
    assert "test_rdr149_lifecycle_conformance.py" in text


# nexus-w771r / GH #1369: "does my supervisor own the process I'm about to
# heartbeat" is a shared run-loop invariant (exit_if_process_unowned in the
# primitive), not a per-tier copy. A tier reimplementing its own
# "if not X.owns_process: ... return 0" prelude instead of calling the shared
# helper is exactly the drift class this gate exists to catch.
_OWNS_PROCESS_CONSUMER_MODULES = frozenset({
    "daemon/storage_service_daemon.py",
    "daemon/t3_daemon.py",
})


def _defines_exit_if_process_unowned(tree: ast.AST) -> bool:
    """AST-based (not substring) check for a top-level ``def
    exit_if_process_unowned(...)`` / ``async def`` in ``tree``. AST-based so
    a comment or docstring merely mentioning the name can never count as a
    definition, and an ``async def`` redefinition is caught the same as a
    plain ``def`` (a gap a prior substring-based version of this check had)."""
    return any(
        isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef))
        and node.name == "exit_if_process_unowned"
        for node in ast.walk(tree)
    )


def _defines_owns_process_property(tree: ast.AST) -> bool:
    """AST-based check for an ``@property def owns_process(self)``."""
    for node in ast.walk(tree):
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)) and node.name == "owns_process":
            if any(
                isinstance(d, ast.Name) and d.id == "property"
                for d in node.decorator_list
            ):
                return True
    return False


def _calls_exit_if_process_unowned(tree: ast.AST) -> bool:
    """AST-based check for a real call to ``exit_if_process_unowned(...)``,
    as a bare name or an attribute (``module.exit_if_process_unowned(...)``).
    A comment or docstring mentioning the name is NOT an ``ast.Call`` node,
    so it can never produce a false match -- unlike the substring check
    (``"exit_if_process_unowned(" in text``) this replaces, which a stray
    comment like ``# TODO: call exit_if_process_unowned(sup)`` would have
    silently satisfied without any real call existing."""
    for node in ast.walk(tree):
        if not isinstance(node, ast.Call):
            continue
        func = node.func
        if isinstance(func, ast.Name) and func.id == "exit_if_process_unowned":
            return True
        if isinstance(func, ast.Attribute) and func.attr == "exit_if_process_unowned":
            return True
    return False


def test_owns_process_short_circuit_defined_only_in_primitive() -> None:
    """``exit_if_process_unowned`` -- the shared "don't heartbeat what you
    don't own" run-loop prelude -- is defined exactly once, in the
    primitive. A tier defining its own copy is reimplementing it bespoke."""
    definitions: list[pathlib.Path] = []
    for path in _py_files():
        if _defines_exit_if_process_unowned(ast.parse(path.read_text(encoding="utf-8"))):
            definitions.append(path)
    assert definitions == [PRIMITIVE], (
        "RDR-149 lifecycle gate: exit_if_process_unowned must be defined "
        "ONLY in daemon/service_registry.py (the single substrate). Found: "
        f"{[str(p.relative_to(REPO_ROOT)) for p in definitions]}"
    )


def test_every_owns_process_consumer_calls_the_shared_helper() -> None:
    """Every tier that defines an ``owns_process`` property must call the
    shared ``exit_if_process_unowned`` helper from its run loop, not
    hand-roll an equivalent ``if not sup.owns_process: ... return 0``
    prelude (the exact GH #1369 duplication this gate now bans a second
    instance of). AST-based: a comment mentioning the helper's name cannot
    satisfy this check (see test_owns_process_helper_check_is_non_vacuous)."""
    offenders: list[str] = []
    for path in _py_files():
        if path == PRIMITIVE:
            continue
        tree = ast.parse(path.read_text(encoding="utf-8"))
        if _defines_owns_process_property(tree) and not _calls_exit_if_process_unowned(tree):
            offenders.append(str(path.relative_to(REPO_ROOT)))
    assert not offenders, (
        "RDR-149 lifecycle gate: a module defines owns_process but never "
        "calls the shared exit_if_process_unowned() helper from its run "
        "loop -- it is reimplementing the short-circuit bespoke instead of "
        "sharing it. Offenders:\n  " + "\n  ".join(offenders)
    )


def test_owns_process_consumer_allowlist_is_non_vacuous() -> None:
    """Companion sanity check: at least the two tiers this fix touched must
    actually define owns_process, or the assertion above is vacuously true."""
    for rel in _OWNS_PROCESS_CONSUMER_MODULES:
        path = SRC_ROOT / rel
        tree = ast.parse(path.read_text(encoding="utf-8"))
        assert _defines_owns_process_property(tree), (
            f"{rel} was expected to define owns_process; if it no longer "
            "does, update _OWNS_PROCESS_CONSUMER_MODULES and this test"
        )


def test_owns_process_helper_check_is_non_vacuous() -> None:
    """Mirrors test_flock_acquire_allowlist_is_non_vacuous: proves the
    must-call-the-shared-helper detector actually fires on a synthetic
    offender (a module defining owns_process, mentioning the helper's name
    only in a comment, but never calling it) -- not merely passing today
    because no real offender happens to exist. Also proves a genuinely
    compliant module (a real ast.Call, bare-name or attribute form) is NOT
    flagged, so the detector isn't so strict it bans the real call sites."""
    offender_src = (
        "class Foo:\n"
        "    @property\n"
        "    def owns_process(self) -> bool:\n"
        "        return self._proc is not None\n"
        "    def run(self):\n"
        "        # calls exit_if_process_unowned(self, ...) -- except it doesn't, this is a comment\n"
        "        if not self.owns_process:\n"
        "            return 0\n"
    )
    tree = ast.parse(offender_src)
    assert _defines_owns_process_property(tree)
    assert not _calls_exit_if_process_unowned(tree), (
        "the detector must not be fooled by a comment mentioning the helper's name"
    )

    compliant_bare_call_src = (
        "class Foo:\n"
        "    @property\n"
        "    def owns_process(self) -> bool:\n"
        "        return self._proc is not None\n"
        "    def run(self, sup, flush_logging):\n"
        "        if exit_if_process_unowned(sup, flush_logging, log=_log, event='x'):\n"
        "            return 0\n"
    )
    compliant_tree = ast.parse(compliant_bare_call_src)
    assert _defines_owns_process_property(compliant_tree)
    assert _calls_exit_if_process_unowned(compliant_tree)

    compliant_attribute_call_src = (
        "class Foo:\n"
        "    @property\n"
        "    def owns_process(self) -> bool:\n"
        "        return self._proc is not None\n"
        "    def run(self, sup, flush_logging):\n"
        "        if service_registry.exit_if_process_unowned(sup, flush_logging):\n"
        "            return 0\n"
    )
    attribute_tree = ast.parse(compliant_attribute_call_src)
    assert _calls_exit_if_process_unowned(attribute_tree), (
        "the detector must also recognize module.exit_if_process_unowned(...) form"
    )


def test_owns_process_defined_only_in_primitive_detector_is_non_vacuous() -> None:
    """Companion non-vacuity check for the sibling detector: proves
    _defines_exit_if_process_unowned actually fires on a synthetic
    reimplementation, including the async-def form the old substring-based
    check (``line.startswith("def exit_if_process_unowned(")``) would have
    missed entirely."""
    assert _defines_exit_if_process_unowned(
        ast.parse("def exit_if_process_unowned(sup):\n    return True\n")
    )
    assert _defines_exit_if_process_unowned(
        ast.parse("async def exit_if_process_unowned(sup):\n    return True\n")
    )
    assert not _defines_exit_if_process_unowned(
        ast.parse("# def exit_if_process_unowned(sup): return True\n")
    )
