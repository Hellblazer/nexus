# SPDX-License-Identifier: AGPL-3.0-or-later
"""Wiring gate for eleven shell *_test.sh suites nothing ran (nexus-fcjt7).

Found by the nexus-iexvl round-2 critique and confirmed by a repo-wide
reference scan 2026-09-14: eleven ``*_test.sh`` suites under ``scripts/`` and
``tests/e2e/`` were referenced by NOTHING — no pytest wrapper, no CI job, no
e2e runner step, no release battery step. Every other ``*_test.sh`` suite in
this repo is driven by a pytest wrapper following this same shape (see
``tests/hooks/test_expectations_shellib_gate.py``,
``tests/scripts/test_exit_diagnostics_sh.py``); these eleven were not, so a
regression in any of them — the build-gate-jar cache, the release.properties
lease, the bare-mvnw lint's own exemption lists, run.sh's / local-service-
gate.sh's own guard wiring, the shared build lease, the mvnw signal-forwarding
wrapper, or the RDR-184 P0 lock/commit-scope-audit shellibs — could break
silently. ``bare_mvnw_lint_test.sh`` itself was two failures deep
(nexus-q1upi) before this file existed to report it to anything, and
``harness_lock_test.sh`` was found ALREADY RED (nexus-fcjt7 round 2, see
below) the same way.

Round 1 (2026-09-14) wired six of these. Round 2, the SAME day, found five
more of the identical unwired class:

- The substantive-critic pass on round 1 (nexus-fcjt7 T2 [25639]) found
  ``tests/e2e/lib/lock_test.sh``, ``harness_lock_test.sh``, and
  ``commit_scope_audit_test.sh`` — all three self-provisioning, all three
  referenced only in comments ("Test seam: ...") and cross-file prose, never
  executed by anything. ``harness_lock_test.sh`` was RED at the time it was
  found: 56 passed, 1 failed, on a stale table entry naming
  ``tests/e2e/t2-migration-sqlite/run.sh`` — a harness deleted outright at
  e3c00252a (RDR-158 P3, the SQLite opt-out backend retirement). The table's
  own non-vacuity check correctly tripped ("silently unwired"); nothing
  reported the trip because nothing ran the suite. Fixed here by removing
  the entry — the harness is gone, not renamed, so there is nothing to
  repoint it to.
- Round 1's own docstring asserted ``build-lease_test.sh`` and
  ``mvnw-leased_test.sh`` were "wired by pre-existing pytest wrappers"
  (citing ``tests/scripts/test_build_lease_callers_wait.py``). That claim
  does not survive a check of what that file actually does: it is a static
  grep-based lint (``test_no_bare_acquire_outside_the_lease_library``) that
  EXEMPTS both suites from a bare-``build_lease_acquire`` scan — it never
  invokes either one. Same unwired class as the other four, just harder to
  see because the wrapper's own docstring already discusses both files by
  name.

NON-VACUITY. Each suite prints its own ``<name>: N passed, M failed``
summary line (``mvnw-leased_test.sh`` appends a third ``, K skipped`` field
for one OS-signal-delivery-dependent check it cannot exercise in every
execution environment, with an inline explanation of why that is an
environment property and not a reported failure; the regex below matches
the shared ``N passed, M failed`` prefix regardless). A wrapper that only
checked the subprocess return code could pass on a suite that silently ran
zero assertions; this checks, in order: (1) the summary line is present at
all — an abort before the final print produces neither a summary line nor a
"[FAIL]" tail, so a bare stdout-grep-for-FAIL wrapper would see nothing and
call it clean (the exact shape ``tests/hooks/test_expectations_shellib_gate.py``
guards against for its own suite); (2) the subprocess return code is 0 —
every suite here ends ``[[ $FAIL -eq 0 ]]``, so the exit code is the honest
signal an abort cannot fake; (3) the reported ``passed`` count is at least
FLOOR (never a bare "some tests ran") and ``failed`` is exactly 0. A floor,
not an exact pin, because these eleven suites have several different authors
and purposes and this file's job is "did anything not run", not "did the
exact assertion count of someone else's suite drift" — the per-suite exact
pin belongs in that suite's own file, if anyone wants one.

ISOLATION. All eleven suites self-provision a throwaway git repository, a
throwaway lockdir under the machine-global lock root, or (for
``local_service_gate_guard_test.sh``) a throwaway repo plus a sed-extracted
fragment of the real file under test, under ``mktemp -d`` and copy the real
``scripts/lib/*.sh`` library files into it; ``build_lease_acquire`` and
``gate_jar_cache_*`` resolve their storage root from the SOURCED file's own
``BASH_SOURCE``, not the caller's cwd, so a copied library resolves against
the throwaway repo's own ``.git``, never this checkout's real build lease,
release.properties, or gate-jar cache. ``run_sh_guard_test.sh`` additionally
stubs ``docker``/``uv`` via a fixture ``PATH`` and patches its copy of
``run.sh``'s hard-coded lock directory into the same tmpdir — no real
Docker, no real `uv build`, no network. Of the new round-2 suites, only
``commit_scope_audit_test.sh`` makes real git commits — inside its own two
throwaway repos, both configured with LOCAL ``user.email``/``user.name``,
never ``--global`` — so a runner with no ambient git identity at all
(verified in an ``ubuntu:24.04`` container with no ``~/.gitconfig``) still
passes; ``lock_test.sh``, ``harness_lock_test.sh``, ``build-lease_test.sh``,
and ``mvnw-leased_test.sh`` make no git commits at all. Verified by hand
2026-09-14: none of the eleven suites' real functions or copied libraries
touch this checkout's
``.git/nexus-build-lease``, ``.git/nexus-gate-jar-cache``, or
``service/src/main/resources/META-INF/nexus/release.properties``.

PLACEMENT (default suite, not the lint bucket). ``tests/AGENTS.md`` draws
the default-loop/lint-bucket line on STRUCTURE, not wall-clock time: the
lint bucket is "only safe for FILESYSTEM-scanned censuses" — an
`rglob`-driven scan whose cost and scope grow with repo size, and which only
changes when repo structure changes, not application behavior. None of
these eleven wrappers is that. Each is a fixed-count subprocess invocation
of one self-contained shell suite testing that suite's OWN runtime
behavior (lock contention, lease acquisition, signal forwarding, commit-
scope detection) — the same shape as every other shell-suite wrapper
already in the default loop (`test_build_lease_callers_wait.py`,
`test_expectations_shellib_gate.py`, `test_exit_diagnostics_sh.py`). A
round-1 version of this docstring justified the placement by an observed
"~10s" runtime instead, which `tests/AGENTS.md` does not name as a
criterion anywhere; that framing is retracted here. Measured 2026-09-14
(this box, ``TMPDIR`` on external SSD): all eleven run in roughly 0-16
seconds combined — incidental, not the reason they belong here.
"""
from __future__ import annotations

import re
import subprocess
import time
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parents[2]

_SUMMARY_RE_TEMPLATE = r"{name}: (\d+) passed, (\d+) failed"


class _Suite:
    """One unwired shell suite: where it lives, and the non-vacuity floor
    for its own reported ``passed`` count (see module docstring)."""

    __slots__ = ("path", "min_passed")

    def __init__(self, relpath: str, min_passed: int) -> None:
        self.path = REPO_ROOT / relpath
        self.min_passed = min_passed

    @property
    def id(self) -> str:
        return self.path.name


# Floors are the exact passed count measured 2026-09-14 (nexus-fcjt7) --
# tight enough to catch a suite quietly running fewer assertions than it
# used to, loose enough that adding a new assertion to one of these suites
# never requires touching this file.
SUITES = [
    _Suite("scripts/build-gate-jar_test.sh", 24),
    _Suite("scripts/lib/bare_mvnw_lint_test.sh", 2),
    _Suite("scripts/lib/gate-jar-cache_test.sh", 24),
    _Suite("scripts/lib/release-props-lease_test.sh", 28),
    _Suite("tests/e2e/migration-rehearsal/run_sh_guard_test.sh", 10),
    _Suite("tests/e2e/local_service_gate_guard_test.sh", 9),
    # Round 2 (nexus-fcjt7): found by the substantive-critic pass on round 1
    # plus a re-check of round 1's own "already wired" claim about the two
    # build-lease suites (see module docstring).
    _Suite("tests/e2e/lib/lock_test.sh", 30),
    _Suite("tests/e2e/lib/harness_lock_test.sh", 56),
    # 16, not the 19 this suite reports on macOS: its Test F only runs its
    # 3 assertions under the stock macOS /bin/bash 3.2 (the guard's actual
    # target); on any host whose /bin/bash is already 4+ -- every Linux CI
    # runner included -- Test F degrades to one [skip] line and those 3
    # assertions never execute. 16 is the floor this suite ACTUALLY reports
    # on ubuntu-latest (verified in an ubuntu:24.04 container, nexus-fcjt7
    # round 2); pinning 19 here would red this gate on every CI run.
    _Suite("tests/e2e/lib/commit_scope_audit_test.sh", 16),
    _Suite("scripts/lib/build-lease_test.sh", 38),
    _Suite("scripts/mvnw-leased_test.sh", 22),
]


class TestSuitesExist:
    """Non-vacuity for the wrapper itself: a suite whose path went stale
    (renamed, moved) must fail loud here, not be silently skipped."""

    @pytest.mark.parametrize("suite", SUITES, ids=lambda s: s.id)
    def test_suite_file_exists(self, suite: _Suite) -> None:
        assert suite.path.is_file(), f"missing: {suite.path}"


class TestSuitesAreGreen:
    @pytest.mark.parametrize("suite", SUITES, ids=lambda s: s.id)
    def test_suite_runs_clean(self, suite: _Suite) -> None:
        start = time.monotonic()
        result = subprocess.run(
            ["bash", str(suite.path)],
            cwd=REPO_ROOT,
            capture_output=True,
            text=True,
            timeout=120,
        )
        elapsed = time.monotonic() - start

        summary_re = re.compile(_SUMMARY_RE_TEMPLATE.format(name=re.escape(suite.id)))
        match = summary_re.search(result.stdout)
        assert match is not None, (
            f"{suite.id} produced no 'N passed, M failed' summary line -- "
            "the suite aborted before reaching its own final print (elapsed "
            f"{elapsed:.1f}s).\n"
            f"rc={result.returncode}\n"
            f"--- stdout ---\n{result.stdout}\n"
            f"--- stderr ---\n{result.stderr}"
        )
        passed, failed = int(match.group(1)), int(match.group(2))

        assert result.returncode == 0, (
            f"{suite.id} exited {result.returncode} (reported passed={passed} "
            f"failed={failed}) -- the process exit code is the load-bearing "
            "signal here, not stdout content.\n"
            f"--- stdout ---\n{result.stdout}\n"
            f"--- stderr ---\n{result.stderr}"
        )
        assert passed >= suite.min_passed and failed == 0, (
            f"{suite.id} reported passed={passed} failed={failed}, expected "
            f"passed>={suite.min_passed} and failed==0 -- a count below the "
            "floor means fewer assertions ran than this file has ever seen "
            "pass, which is exactly the silent-degradation class nexus-fcjt7 "
            "exists to catch.\n"
            f"--- stdout ---\n{result.stdout}\n"
            f"--- stderr ---\n{result.stderr}"
        )
