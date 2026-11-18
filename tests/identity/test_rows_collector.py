# SPDX-License-Identifier: AGPL-3.0-or-later
"""The row collector of the engine-backed identity tests (``Rows`` in tests/identity/conftest.py), on its own.

Tenant-free by construction: no ``world`` fixture, no ``t2_tenant``. ``Rows`` only asks its world for ``scoped(tag)``, so
a stand-in with that one method drives it. What is pinned here is what the engine-backed tables rely on: a failing row,
a ``pytest.raises`` that saw nothing (``Failed``, a ``BaseException``), never stops the rows after it; every failure is
named; a table that is empty, or that lacks a ``row_*`` function the module defines, fails; ``NX_ROWS`` selects rows.
"""
from __future__ import annotations

from types import SimpleNamespace

import pytest

from tests.identity.conftest import Rows


class _StandInWorld:
    def scoped(self, tag: str) -> SimpleNamespace:
        return SimpleNamespace(tag=tag)


def _rows() -> Rows:
    return Rows(_StandInWorld())  # type: ignore[arg-type]


def _table(*fns):
    return tuple(fns), {fn.__name__: fn for fn in fns}


def test_a_failed_assert_and_a_raises_that_saw_nothing_do_not_stop_the_rows_after_them_and_both_are_named(
        monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("NX_ROWS", raising=False)
    ran: list[str] = []

    def row_asserts(world) -> None:
        ran.append("asserts")
        assert world.tag == "never", "the assert row fails"

    def row_expects_a_refusal_that_does_not_come(world) -> None:
        ran.append("raises")
        with pytest.raises(ValueError):
            pass                                    # DID NOT RAISE: pytest's Failed, a BaseException

    def row_passes(world) -> None:
        ran.append("passes")

    table, namespace = _table(row_asserts, row_expects_a_refusal_that_does_not_come, row_passes)
    rows = _rows()
    with pytest.raises(pytest.fail.Exception) as err:
        rows.run(table, namespace)
    assert ran == ["asserts", "raises", "passes"], "the passing row after both failures still ran"
    assert rows.ran == ["row_asserts", "row_expects_a_refusal_that_does_not_come", "row_passes"]
    message = str(err.value)
    assert "2 of 3 rows failed" in message
    assert "ROW 'row_asserts' FAILED" in message and "the assert row fails" in message
    assert "ROW 'row_expects_a_refusal_that_does_not_come' FAILED" in message and "DID NOT RAISE" in message
    assert "row_passes' FAILED" not in message
    assert "(row ran " in message, "a failed row's duration is in its body"
    assert set(rows.seconds) == set(rows.ran)
    assert "row times: row_asserts " in message and "row_passes " in message, "every row's time is listed, passing rows too"


def test_pytest_exit_inside_a_row_ends_the_run_and_is_not_kept_as_a_row_failure(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("NX_ROWS", raising=False)
    ran: list[str] = []

    def row_exits(world) -> None:
        ran.append("exits")
        pytest.exit("stop the session")

    def row_after(world) -> None:
        ran.append("after")

    table, namespace = _table(row_exits, row_after)
    rows = _rows()
    with pytest.raises(pytest.exit.Exception):
        rows.run(table, namespace)
    assert ran == ["exits"] and not rows.failed, "Exit subclasses Exception, but it is not a row failure"


def test_check_fails_for_a_failed_row_and_for_fewer_rows_than_expected() -> None:
    rows = _rows()
    with rows.row("good"):
        pass
    rows.check(expect=1)                            # all ran, none failed: passes
    with rows.row("bad"):
        raise AssertionError("boom")
    with pytest.raises(pytest.fail.Exception, match="1 of 2 rows failed"):
        rows.check(expect=2)
    with pytest.raises(pytest.fail.Exception, match="2 rows ran, the test expects 3"):
        rows.check(expect=3)


def test_an_empty_table_fails() -> None:
    with pytest.raises(AssertionError, match="row table is empty"):
        _rows().run((), {})


def test_a_row_function_missing_from_the_table_fails() -> None:
    def row_listed(world) -> None:
        pass

    def row_forgotten(world) -> None:
        pass

    with pytest.raises(AssertionError, match="row_forgotten"):
        _rows().run((row_listed,), {"row_listed": row_listed, "row_forgotten": row_forgotten})


def test_nx_rows_runs_only_the_rows_whose_name_contains_it_and_a_value_that_matches_none_fails(
        monkeypatch: pytest.MonkeyPatch) -> None:
    ran: list[str] = []

    def row_lapsed_one(world) -> None:
        ran.append("lapsed_one")

    def row_other(world) -> None:
        ran.append("other")

    def row_lapsed_two(world) -> None:
        ran.append("lapsed_two")

    table, namespace = _table(row_lapsed_one, row_other, row_lapsed_two)
    monkeypatch.setenv("NX_ROWS", "lapsed")
    rows = _rows()
    rows.run(table, namespace)
    assert ran == ["lapsed_one", "lapsed_two"] and rows.ran == ["row_lapsed_one", "row_lapsed_two"]
    monkeypatch.setenv("NX_ROWS", "no-such-row")
    with pytest.raises(AssertionError, match="matches no row"):
        _rows().run(table, namespace)


def test_a_skip_is_not_swallowed_and_a_skip_behind_a_failure_fails_naming_it(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("NX_ROWS", raising=False)

    def row_fails(world) -> None:
        raise AssertionError("first row broke")

    def row_skips(world) -> None:
        pytest.skip("the engine lacks a feature")

    def row_never_reached(world) -> None:
        raise AssertionError("must not run after a skip")

    with pytest.raises(pytest.skip.Exception):
        table, namespace = _table(row_skips, row_never_reached)
        _rows().run(table, namespace)
    table, namespace = _table(row_fails, row_skips, row_never_reached)
    rows = _rows()
    with pytest.raises(pytest.fail.Exception) as err:
        rows.run(table, namespace)
    assert "first row broke" in str(err.value) and "stopped by" in str(err.value)
    assert rows.ran == ["row_fails", "row_skips"]


def test_a_keyboard_interrupt_propagates_and_the_failures_so_far_go_to_stderr(
        monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]) -> None:
    monkeypatch.delenv("NX_ROWS", raising=False)

    def row_fails(world) -> None:
        raise AssertionError("broke before the interrupt")

    def row_interrupted(world) -> None:
        raise KeyboardInterrupt

    table, namespace = _table(row_fails, row_interrupted)
    with pytest.raises(KeyboardInterrupt):
        _rows().run(table, namespace)
    assert "broke before the interrupt" in capsys.readouterr().err
