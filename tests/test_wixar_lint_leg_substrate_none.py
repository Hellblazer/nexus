# SPDX-License-Identifier: AGPL-3.0-or-later
"""nexus-wixar: the `pytest (lint markers)` CI leg executes for real under
its own env shape, not a vacuous mass-skip.

ROOT CAUSE: CI's `test-lint` job runs `uv run pytest -m lint -q` on a
runner that deliberately provisions no service jar. The autouse
`_pin_t2_substrate` fixture (tests/conftest.py) pulled in the engine
substrate for every test regardless of marker, and `t2_service_env`'s own
CI-only graceful-skip branch (`GITHUB_ACTIONS=='true'` AND no
`NX_T2_SUBSTRATE_EXPECTED` AND no fresh jar) then fired for every single
lint-marked item -- the job's ENTIRE corpus (no other job runs `-m lint`).
`pytest`'s own exit code is 0 when everything selected is skipped, so the
real 2026-08-18 CI run (PR #1459) reported success having executed zero of
~830 lint tests (`938 skipped, 13459 deselected`).

THE FIX mirrors the `test-mode-census` job's own prior fix for the identical
shape (nexus-vdti6, 2026-08-06, `.github/workflows/ci.yml`): `test-lint` now
sets `NX_TEST_T2_SUBSTRATE=none` in its job env, which makes
`_pin_t2_substrate` return before ever calling `t2_service_env` (see that
fixture's own `=none` branch in tests/conftest.py) -- unconditionally,
regardless of `GITHUB_ACTIONS` or jar presence.

Same proof shape as
`test_mode_declarations_census_executes_for_real_under_ci_env_shape`
(tests/test_mode_declarations_are_explicit.py): a REAL subprocess pytest
invocation against the job's exact env combination, asserting the tests
actually PASS (not SKIPPED) -- independent of whether this box happens to
have a fresh service jar sitting around (this test never inspects or relies
on that state either way).
"""

from __future__ import annotations

import os
import pathlib
import subprocess
import sys

_REPO_ROOT = pathlib.Path(__file__).resolve().parent.parent


def test_lint_marked_tests_execute_for_real_under_ci_env_shape() -> None:
    """Simulate `test-lint`'s exact env combination (`GITHUB_ACTIONS=true`,
    `NX_TEST_T2_SUBSTRATE=none`, no jar-provisioning steps run) against a
    real slice of the lint-marked corpus and confirm it executes rather
    than skipping.

    Scoped to `tests/test_marker_selection_coverage.py` -- itself entirely
    `@pytest.mark.lint` (module-level `pytestmark`) -- rather than the
    whole ~830-test corpus, to keep this subprocess fast; the mechanism
    under test (`_pin_t2_substrate`'s `NX_TEST_T2_SUBSTRATE=none` branch)
    does not care which lint test it is.
    """
    proc = subprocess.run(
        [
            sys.executable,
            "-m",
            "pytest",
            "tests/test_marker_selection_coverage.py",
            "-m",
            "lint",
            "-q",
            "-rs",
            "--no-header",
        ],
        cwd=_REPO_ROOT,
        env={
            **os.environ,
            "GITHUB_ACTIONS": "true",
            "NX_TEST_T2_SUBSTRATE": "none",
        },
        capture_output=True,
        text=True,
        timeout=120,
    )
    output = proc.stdout + proc.stderr
    assert "service jar not provisioned" not in output, (
        "the lint slice hit the engine-substrate graceful skip under the "
        "test-lint job's exact env shape (GITHUB_ACTIONS=true, no jar) -- "
        "NX_TEST_T2_SUBSTRATE=none did not bypass it as expected:\n" + output
    )
    assert " 0 passed" not in output and "no tests ran" not in output, (
        f"expected the lint slice to execute real tests under this env "
        f"shape, got:\n{output}"
    )
    assert proc.returncode == 0, f"lint slice did not pass cleanly:\n{output}"


def test_lint_marker_pin_returns_before_substrate_lookup_for_none() -> None:
    """Direct unit check on the fixture itself (no subprocess): with
    `NX_TEST_T2_SUBSTRATE=none`, `_pin_t2_substrate` must return WITHOUT
    ever calling `request.getfixturevalue` -- proving the bypass is
    unconditional, not merely "usually fires before the skip branch is
    reached"."""
    from tests import conftest

    fixture = conftest._pin_t2_substrate
    wrapped = getattr(fixture, "__pytest_wrapped__", None)
    func = wrapped.obj if wrapped is not None else fixture.__wrapped__

    class _Boom:
        def getfixturevalue(self, name: str):
            raise AssertionError(
                f"_pin_t2_substrate resolved {name!r} despite "
                "NX_TEST_T2_SUBSTRATE=none"
            )

    old = os.environ.get("NX_TEST_T2_SUBSTRATE")
    os.environ["NX_TEST_T2_SUBSTRATE"] = "none"
    try:
        func(_Boom())  # must not raise
    finally:
        if old is None:
            del os.environ["NX_TEST_T2_SUBSTRATE"]
        else:
            os.environ["NX_TEST_T2_SUBSTRATE"] = old
