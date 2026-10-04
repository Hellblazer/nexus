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
release.properties, or gate-jar cache. Of the new round-2 suites, only
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

import os
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



def _bin_bash_is_pre_4() -> bool:
    """True when this host's /bin/bash is older than 4, the same probe the
    suite's own Test F guard uses. There Test F runs its 3 extra assertions,
    so the floor is 19; on bash 4+ (every Linux CI runner) it is 16. A single
    floor of 16 would let those 3 assertions stop running on macOS unseen."""
    bash = Path("/bin/bash")
    if not bash.exists():
        return False
    probe = subprocess.run(
        [str(bash), "-c", "(( BASH_VERSINFO[0] >= 4 ))"],
        capture_output=True,
        check=False,
    )
    return probe.returncode != 0

# Floors are the exact passed count measured 2026-09-14 (nexus-fcjt7) --
# tight enough to catch a suite quietly running fewer assertions than it
# used to, loose enough that adding a new assertion to one of these suites
# never requires touching this file.
SUITES = [
    _Suite("scripts/build-gate-jar_test.sh", 24),
    _Suite("scripts/lib/bare_mvnw_lint_test.sh", 2),
    _Suite("scripts/lib/gate-jar-cache_test.sh", 24),
    _Suite("scripts/lib/release-props-lease_test.sh", 28),
    _Suite("tests/e2e/local_service_gate_guard_test.sh", 9),
    # Round 2 (nexus-fcjt7): found by the substantive-critic pass on round 1
    # plus a re-check of round 1's own "already wired" claim about the two
    # build-lease suites (see module docstring).
    _Suite("tests/e2e/lib/lock_test.sh", 30),
    _Suite("tests/e2e/lib/harness_lock_test.sh", 35),
    # 16, not the 19 this suite reports on macOS: its Test F only runs its
    # 3 assertions under the stock macOS /bin/bash 3.2 (the guard's actual
    # target); on any host whose /bin/bash is already 4+ -- every Linux CI
    # runner included -- Test F degrades to one [skip] line and those 3
    # assertions never execute. 16 is the floor this suite ACTUALLY reports
    # on ubuntu-latest (verified in an ubuntu:24.04 container, nexus-fcjt7
    # round 2), so the floor follows the host's /bin/bash, not the OS name.
    _Suite(
        "tests/e2e/lib/commit_scope_audit_test.sh",
        19 if _bin_bash_is_pre_4() else 16,
    ),
    # 40 as an ordinary user, 39 as root: Test 10 (the pgid-liveness pin) takes
    # one `ok` instead of two when the process is root (measured in
    # python:3.12-slim, which runs as root: "39 passed, 0 failed"; round-3 review
    # L1). Same precedent as the /bin/bash floor above: the floor follows the
    # host, here the effective uid. Nothing known in CI runs as root; a root hand
    # run or container does. Deleting Test 10 still turns either floor red.
    _Suite("scripts/lib/build-lease_test.sh", 39 if os.geteuid() == 0 else 40),
    _Suite("scripts/mvnw-leased_test.sh", 22),
    # nexus-20onx round 3: leg B3's compare logic, sourced from the real cloud
    # gate script and fed canned /v1/status bodies (11 cases + 3 wiring checks;
    # round 4: 4 unreadable-body cases, S4, 17 in all). nexus-wbfpw.50: leg J's reaper verdict (17
    # cases), its 3 wiring checks and the leg-K read-only audit, 38 in all. Fix round: 6 more J cases
    # (last_pass, since-boot counter), 10 _edge_expect cases (the engine's own message fragment) and 21
    # audit negatives over mutated copies of the gate, 75 in all.
    _Suite("tests/e2e/cloud_client_path_gate_b3_test.sh", 75),
    # nexus-u67ow: lib/python.sh, the one-interpreter resolver the e2e harness uses in place of a
    # bare python3 (hellmini's is 3.9.6). Stub interpreters on a PATH of their own; 37 measured.
    _Suite("tests/e2e/lib/python_test.sh", 37),
]


#: The lease variables a suite must never inherit. CI's lease step exports
#: NX_BUILD_LEASE_ROOT to every later step, and an nxtest hand run sets it by
#: design; each suite assumes the lease root is its own fake repo's.
_LEASE_ENV_PREFIXES = ("NX_BUILD_LEASE_ROOT", "NX_SUITE_LEASE_")

#: EVERY suite, not a hand-kept list of the ones known to be sensitive. Measured on
#: the qwentescence host (nxtest exports NX_BUILD_LEASE_ROOT=/var/lib/nx-suite-lease,
#: where another run holds the leases): six suites fail or hang there and the other
#: five pass. Three of them (build-lease, mvnw-leased, and a since-deleted run.sh guard suite) failed against
#: an EMPTY inherited root; the other three (release-props-lease,
#: local_service_gate_guard, build-gate-jar; nexus-mntbl) only fail when the shared
#: root carries a live holder, which the test below pre-holds. A list of "the
#: sensitive ones" went stale exactly this way, so a new suite is covered by being
#: in SUITES.
_LEASE_SENSITIVE = tuple(str(s.path.relative_to(REPO_ROOT)) for s in SUITES)


def _env_without_lease_vars() -> dict[str, str]:
    return {k: v for k, v in os.environ.items() if not k.startswith(_LEASE_ENV_PREFIXES)}


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
            env=_env_without_lease_vars(),
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


class TestSuitesIgnoreAnInheritedLeaseRoot:
    """A hand run with the documented export must neither fail nor write fixtures into the shared root.

    The wrapper above scrubs the variables, so it cannot see this: the suites'
    own ``unset`` is what is under test, and a run that inherits the variable
    is the only way to reach it.
    """

    @pytest.mark.parametrize("relpath", _LEASE_SENSITIVE, ids=lambda p: Path(p).name)
    def test_suite_passes_with_the_lease_root_exported_and_leaves_it_untouched(
        self, relpath: str, tmp_path: Path
    ) -> None:
        shared = tmp_path / "shared-lease-root"
        shared.mkdir()
        # A live peer holds both leases in the shared root, as another run does on
        # the shared qwentescence host. A suite that inherits the root queues behind
        # this holder and fails; a suite that unsets it never sees it. This process
        # is alive for the whole test, so its pid is a live holder.
        held = {}
        for resource in ("service", "suite"):
            lease = shared / resource
            lease.mkdir()
            (lease / "pid").write_text(f"{os.getpid()}\n")
            (lease / "label").write_text("a peer run holding the shared lease\n")
            held[resource] = sorted(p.name for p in lease.iterdir())
        env = _env_without_lease_vars()
        env["NX_BUILD_LEASE_ROOT"] = str(shared)
        env["NX_SUITE_LEASE_WAIT"] = "1"
        path = REPO_ROOT / relpath
        result = subprocess.run(
            ["bash", str(path)], cwd=REPO_ROOT, capture_output=True, text=True, timeout=120, env=env
        )
        assert result.returncode == 0, (
            f"{path.name} failed with NX_BUILD_LEASE_ROOT exported\n"
            f"--- stdout ---\n{result.stdout}\n--- stderr ---\n{result.stderr}"
        )
        assert sorted(p.name for p in shared.iterdir()) == ["service", "suite"], (
            f"{path.name} wrote into the inherited lease root: "
            f"{sorted(p.name for p in shared.iterdir())}"
        )
        for resource, files in held.items():
            lease = shared / resource
            assert sorted(p.name for p in lease.iterdir()) == files, f"{path.name} touched the peer's {resource} lease"
            assert (lease / "pid").read_text().strip() == str(os.getpid()), f"{path.name} took over the peer's {resource} lease"
