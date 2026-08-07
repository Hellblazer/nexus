# SPDX-License-Identifier: AGPL-3.0-or-later
"""Unit tests for the MinerU re-arm gate (nexus-6xkdu).

Proves the mechanism itself, independent of whether mineru is actually
installed on this box: a nonexistent module name stands in for "mineru is
missing" so these tests are deterministic in every environment.
"""
from __future__ import annotations

import pytest

from tests._mineru_rearm import NX_MINERU_EXPECTED, mineru_importorskip

_ABSENT_MODULE = "nexus_tests_definitely_nonexistent_module_6xkdu"


def test_skips_when_missing_and_not_expected(monkeypatch: pytest.MonkeyPatch) -> None:
    """Default (env unset): a missing import skips, matching the prior
    pytest.importorskip behavior on a dev box without mineru installed."""
    monkeypatch.delenv(NX_MINERU_EXPECTED, raising=False)
    with pytest.raises(pytest.skip.Exception):
        mineru_importorskip(modname=_ABSENT_MODULE)


def test_fails_loud_when_missing_and_expected(monkeypatch: pytest.MonkeyPatch) -> None:
    """NX_MINERU_EXPECTED=1 (release shakedown / a CI leg that provisions
    mineru): a missing import FAILS -- raises ModuleNotFoundError, not
    pytest's Skipped outcome. Absent-dependency and absent-coverage must
    stay distinguishable in an environment that is supposed to have it.

    nexus-6xkdu round 2 (substantive-critic Significant 2, empirically
    falsified the prior version of this test): ``pytest.skip.Exception``
    (``_pytest.outcomes.Skipped``) is a ``BaseException`` pytest's own
    runner recognizes as a SKIP outcome no matter where it is raised --
    including inside a ``with pytest.raises(ModuleNotFoundError):`` block,
    which does NOT catch it (wrong exception type) and lets it propagate
    straight through to pytest's collector as a skip, not a failure. So a
    regression that makes the env-set branch fall back to
    ``pytest.importorskip`` (silently re-introducing the exact bug this
    gate exists to catch) previously made this test SKIP, not FAIL -- the
    green suite stayed green. Proven by the critic: inject that regression,
    re-run, get "3 passed, 1 skipped" instead of a failure; revert.

    Fixed by handling ``Skipped`` explicitly rather than relying on
    ``pytest.raises`` to reject it implicitly: the env-set path must NEVER
    produce a skip, so a caught ``Skipped`` is turned into an explicit
    ``pytest.fail`` (a real failure, distinguishable from both a skip and
    a pass) instead of being allowed to propagate as one.
    """
    monkeypatch.setenv(NX_MINERU_EXPECTED, "1")
    try:
        mineru_importorskip(modname=_ABSENT_MODULE)
    except pytest.skip.Exception:
        pytest.fail(
            "mineru_importorskip(env_var set) skipped instead of failing "
            "loudly on a missing import -- the re-arm regressed to a "
            "silent skip (nexus-6xkdu Significant 2)"
        )
    except ModuleNotFoundError:
        pass  # expected: fails loud, not a skip
    else:
        pytest.fail(
            "mineru_importorskip(env_var set) did not raise at all for a "
            "missing module"
        )


def test_succeeds_silently_when_present_and_not_expected(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A present module neither skips nor fails, env unset."""
    monkeypatch.delenv(NX_MINERU_EXPECTED, raising=False)
    mineru_importorskip(modname="os")  # must not raise


def test_succeeds_silently_when_present_and_expected(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A present module neither skips nor fails, env set -- the re-arm only
    changes behavior on a MISSING import, never on a successful one."""
    monkeypatch.setenv(NX_MINERU_EXPECTED, "1")
    mineru_importorskip(modname="os")  # must not raise
