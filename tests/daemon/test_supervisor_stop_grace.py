# SPDX-License-Identifier: AGPL-3.0-or-later
"""The stopper's grace outlasts the supervisor's inner worst case, on Windows too
(RDR-224, nexus-f9bgu.33, code review m2).

``stop_storage_service`` waits ``_SUPERVISOR_STOP_GRACE`` for the supervisor to
exit before it hard-kills it. The constant is documented as strictly longer than
what a CLEAN shutdown can take. On Windows a shutdown also spends up to
``_WINDOWS_SHARING_RETRY_BUDGET_S`` on each of the four lease-file operations in
its two election blocks (read and replace in ``mark_shutting_down``, read and
unlink in ``relinquish``), which the POSIX figure never included: a sharing
violation lasting the whole budget got a clean shutdown hard-killed in the
middle. Both relations are asserted, and the count of four is MEASURED by running
those two calls against a violation that clears just before each budget ends, so
a fifth retried operation added later turns this red instead of silently
outrunning the grace.
"""
from __future__ import annotations

import os
import sys
from pathlib import Path

import pytest

from nexus.daemon import service_registry as sr
from nexus.daemon import storage_service_daemon as ssd

TIER = "storage_service"
SCOPE = "S-1-5-21-111-222-333-1001"


class _Clock:
    def __init__(self) -> None:
        self.now = 0.0

    def monotonic(self) -> float:
        return self.now

    def sleep(self, seconds: float) -> None:
        self.now += seconds


class _Violation:
    """A sharing violation that lasts almost the whole retry budget, then clears."""

    def __init__(self, clock: _Clock) -> None:
        self.clock = clock
        self.started: float | None = None
        self.hits = 0

    def maybe_raise(self) -> None:
        if self.started is None:
            self.started = self.clock.now
        if self.clock.now - self.started < sr._WINDOWS_SHARING_RETRY_BUDGET_S - 0.05:
            self.hits += 1
            raise PermissionError(13, "sharing violation")
        self.started = None


def _measured_sharing_seconds(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> tuple[float, int]:
    clock = _Clock()
    reg = sr.ServiceRegistry(
        dir=tmp_path, tier=TIER, monotonic=clock.monotonic, sleep=clock.sleep, platform="win32",
    )
    record = reg.publish(
        SCOPE, endpoint={"host": "127.0.0.1", "port": 1}, version="v", owner_token="owner",
    )
    violations = {"read": _Violation(clock), "replace": _Violation(clock), "unlink": _Violation(clock)}
    real_read, real_replace, real_unlink = Path.read_text, os.replace, Path.unlink

    def is_lease(path: object) -> bool:
        return Path(str(path)).name.startswith(f"{TIER}_addr.")

    def read_text(self: Path, *a: object, **k: object) -> str:
        if is_lease(self):
            violations["read"].maybe_raise()
        return real_read(self, *a, **k)  # type: ignore[arg-type]

    def replace(src: object, dst: object, *a: object, **k: object) -> None:
        if is_lease(dst):
            violations["replace"].maybe_raise()
        return real_replace(src, dst, *a, **k)  # type: ignore[arg-type]

    def unlink(self: Path, *a: object, **k: object) -> None:
        if is_lease(self):
            violations["unlink"].maybe_raise()
        return real_unlink(self, *a, **k)  # type: ignore[arg-type]

    start = clock.now
    with monkeypatch.context() as patched:
        patched.setattr(Path, "read_text", read_text)
        patched.setattr(os, "replace", replace)
        patched.setattr(Path, "unlink", unlink)
        reg.mark_shutting_down(record, budget=ssd._STOP_ELECTION_BUDGET)
        reg.relinquish(record, budget=ssd._STOP_ELECTION_BUDGET)
    return clock.now - start, sum(v.hits > 0 for v in violations.values())


class TestGraceRelation:
    @pytest.mark.parametrize("windows", [False, True], ids=["posix", "windows"])
    def test_the_grace_is_longer_than_the_inner_worst_case(self, windows: bool) -> None:
        assert ssd._supervisor_stop_grace(windows=windows) > ssd._supervisor_stop_inner_worst_case(
            windows=windows,
        )

    def test_the_windows_figure_carries_the_sharing_retry_budgets_the_posix_one_lacks(self) -> None:
        extra = (
            ssd._supervisor_stop_inner_worst_case(windows=True)
            - ssd._supervisor_stop_inner_worst_case(windows=False)
        )
        assert extra == pytest.approx(4 * sr._WINDOWS_SHARING_RETRY_BUDGET_S)

    def test_posix_keeps_the_figure_it_always_had(self) -> None:
        # 2 election waits + the engine's SIGTERM grace and reap + 1 s of exit overhead.
        assert ssd._supervisor_stop_grace(windows=False) == pytest.approx(12.0)

    def test_the_module_constant_is_the_figure_for_this_host(self) -> None:
        assert ssd._SUPERVISOR_STOP_GRACE == ssd._supervisor_stop_grace(windows=sys.platform == "win32")

    @pytest.mark.parametrize(("platform", "expected"), [("win32", 20.0), ("linux", 12.0), ("darwin", 12.0)])
    def test_the_module_constant_expression_is_the_per_platform_figure_under_a_patched_platform(
        self, platform: str, expected: float,
    ) -> None:
        """The check above compares the constant with the function evaluated for the host it
        is running on, so off Windows it cannot fail when the constant uses the POSIX figure
        there (A42). This evaluates the constant's OWN defining expression with ``sys.platform``
        replaced, without reloading the module (other tests hold its classes)."""
        import ast
        from types import SimpleNamespace

        tree = ast.parse(Path(ssd.__file__).read_text(encoding="utf-8"))
        exprs = [
            node.value
            for node in tree.body
            if isinstance(node, ast.AnnAssign)
            and isinstance(node.target, ast.Name)
            and node.target.id == "_SUPERVISOR_STOP_GRACE"
            and node.value is not None
        ]
        assert len(exprs) == 1, "the constant's defining statement was not found"
        value = eval(  # noqa: S307 — the repository's own source expression, fixed namespace
            compile(ast.Expression(exprs[0]), ssd.__file__, "eval"),
            {"sys": SimpleNamespace(platform=platform), "_supervisor_stop_grace": ssd._supervisor_stop_grace},
        )
        assert value == pytest.approx(expected)


class TestTheCountOfRetriedOperationsIsMeasured:
    def test_a_violation_on_every_lease_operation_costs_four_budgets_and_fits_in_the_grace(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        seconds, kinds_hit = _measured_sharing_seconds(tmp_path, monkeypatch)
        # Non-vacuity: all three kinds of operation were actually faulted.
        assert kinds_hit == 3, "the read, replace and unlink faults must each have fired"
        budget = sr._WINDOWS_SHARING_RETRY_BUDGET_S
        # Four operations, each held up nearly the whole budget.
        assert 4 * (budget - 0.1) <= seconds <= 4 * budget
        worst = seconds + 2 * ssd._STOP_ELECTION_BUDGET + ssd._GRACEFUL_STOP_TIMEOUT + ssd._POST_KILL_REAP_TIMEOUT
        assert ssd._supervisor_stop_grace(windows=True) > worst, (
            f"a clean Windows shutdown can take {worst:.1f}s but the grace is "
            f"{ssd._supervisor_stop_grace(windows=True):.1f}s"
        )
