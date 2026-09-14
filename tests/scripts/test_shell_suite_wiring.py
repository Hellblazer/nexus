# SPDX-License-Identifier: AGPL-3.0-or-later
"""Wiring gate for six shell *_test.sh suites nothing ran (nexus-fcjt7).

Found by the nexus-iexvl round-2 critique and confirmed by a repo-wide
reference scan 2026-09-14: six ``*_test.sh`` suites under ``scripts/`` and
``tests/e2e/`` were referenced by NOTHING — no pytest wrapper, no CI job, no
e2e runner step, no release battery step. Every other ``*_test.sh`` suite in
this repo is driven by a pytest wrapper following this same shape (see
``tests/hooks/test_expectations_shellib_gate.py``,
``tests/scripts/test_exit_diagnostics_sh.py``); these six were not, so a
regression in any of them — the build-gate-jar cache, the release.properties
lease, the bare-mvnw lint's own exemption lists, or run.sh's / local-service-
gate.sh's own guard wiring — could break silently. ``bare_mvnw_lint_test.sh``
itself was two failures deep (nexus-q1upi) before this file existed to
report them to anything.

NON-VACUITY. Each suite prints its own ``<name>: N passed, M failed``
summary line (all six share this convention). A wrapper that only checked
the subprocess return code could pass on a suite that silently ran zero
assertions; this checks, in order: (1) the summary line is present at all —
an abort before the final print produces neither a summary line nor a
"[FAIL]" tail, so a bare stdout-grep-for-FAIL wrapper would see nothing and
call it clean (the exact shape ``tests/hooks/test_expectations_shellib_gate.py``
guards against for its own suite); (2) the subprocess return code is 0 —
every suite here ends ``[[ $FAIL -eq 0 ]]``, so the exit code is the honest
signal an abort cannot fake; (3) the reported ``passed`` count is at least
FLOOR (never a bare "some tests ran") and ``failed`` is exactly 0. A floor,
not an exact pin, because these six suites have six different authors and
purposes and this file's job is "did anything not run", not "did the exact
assertion count of someone else's suite drift" — the per-suite exact pin
belongs in that suite's own file, if anyone wants one.

ISOLATION. All six suites self-provision a throwaway git repository (or, for
``local_service_gate_guard_test.sh``, a throwaway repo plus a sed-extracted
fragment of the real file under test) under ``mktemp -d`` and copy the real
``scripts/lib/*.sh`` library files into it; ``build_lease_acquire`` and
``gate_jar_cache_*`` resolve their storage root from the SOURCED file's own
``BASH_SOURCE``, not the caller's cwd, so a copied library resolves against
the throwaway repo's own ``.git``, never this checkout's real build lease,
release.properties, or gate-jar cache. ``run_sh_guard_test.sh`` additionally
stubs ``docker``/``uv`` via a fixture ``PATH`` and patches its copy of
``run.sh``'s hard-coded lock directory into the same tmpdir — no real
Docker, no real `uv build`, no network. Verified by hand 2026-09-14: none of
the six suites' real functions or copied libraries touch this checkout's
``.git/nexus-build-lease``, ``.git/nexus-gate-jar-cache``, or
``service/src/main/resources/META-INF/nexus/release.properties``.

TIMING (2026-09-14, this box, ``TMPDIR`` on external SSD): all six run in
0-8 seconds, well under the ~10s line this repo draws between the default
loop and the lint bucket (``tests/AGENTS.md``) — all six run in the DEFAULT
suite. ``bare_mvnw_lint_test.sh`` itself does a repo-wide grep sweep but
finishes in well under a second; it does not need lint-bucket treatment
just because its subject matter is a lint.
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
