# SPDX-License-Identifier: AGPL-3.0-or-later
# Copyright (c) 2026 Hal Hildebrand. All rights reserved.

"""nexus-9dkxu: ``pg_provision._run`` is bounded, and never leaks a password.

TWO CLAIMS, both of which were FALSE before this bead:

1. Every PostgreSQL subprocess this module spawns had no timeout at all.
   ``_run`` is the choke point for initdb, pg_ctl, psql and createdb, so a
   wedged one of those blocked ``nx init --service`` and the supervisor's
   PG start forever.

2. ``_run`` put live credentials in the terminal and the log. ``_psql``
   passes SQL through ``psql -c``, and several statements are
   ``CREATE ROLE ... PASSWORD '<generated>'``. argv carries the password;
   ``subprocess`` copies argv into ``CalledProcessError`` and
   ``TimeoutExpired``, whose ``str()`` is what ``commands/init.py`` echoes
   to the user at default verbosity and logs on a provisioning failure.
   The ``pg_provision_run`` debug line printed the same argv directly.

The second is why these ship together: bounding the call adds a THIRD
exception type carrying the same payload through the same two lines.

EVERY TEST HERE FAILS WITH THE FIX REMOVED. That is the point — a test
that merely passes against the fixed code proves nothing about whether the
fix is what makes it pass, and this repo has paid for that lesson more than
once. Each test's docstring names what it would look like unfixed.
"""

from __future__ import annotations

import subprocess

import pytest

from nexus.db import pg_provision as pp

#: A password-shaped literal that must never survive into a message. Not a
#: real credential; the shape is what matters to the redactor.
_SECRET = "s3cr3tpassw0rdvalue"


def _psql_argv_with_password() -> list[str]:
    """The real shape: an ALTER ROLE that carries a live password in argv."""
    return [
        "/bundle/bin/psql", "-h", "127.0.0.1", "-p", "5432",
        "-U", "someuser", "-d", "nexus",
        "-c", f"ALTER ROLE nexus_admin PASSWORD '{_SECRET}'",
    ]


def test_the_bounds_are_backstops_not_budgets() -> None:
    """Ordering and magnitude, pinned so a later "tuning" pass cannot
    quietly make them tight.

    Unfixed: these constants did not exist. The specific relation worth
    pinning is the pg_ctl one -- ``pg_ctl start -w`` runs its own 60 s
    wait (PGCTLTIMEOUT), so an outer bound at or below that would SIGKILL
    pg_ctl before it could report its own, better-worded failure.
    """
    assert pp._PG_CTL_WAIT_TIMEOUT_S > 60.0, (
        "the outer pg_ctl bound must exceed pg_ctl's own -w default of 60 s, or "
        "we kill it before it can name the log file to read"
    )
    # initdb is the fsync-bound tail; measured worst was 2.86 s on a fast
    # box, and the bound carries a large multiple of that on purpose.
    assert pp._INITDB_TIMEOUT_S >= 60.0 * 2
    for name in (
        "_INITDB_TIMEOUT_S", "_CREATEDB_TIMEOUT_S", "_PSQL_TIMEOUT_S",
        "_PG_CTL_STATUS_TIMEOUT_S", "_PG_CTL_WAIT_TIMEOUT_S",
    ):
        assert getattr(pp, name) >= 30.0, (
            f"{name} is below 30 s. These are backstops against a hang, not "
            "performance targets; a bound tight enough to fire on a slow box is "
            "a new failure mode on the install path (nexus-9dkxu)."
        )


def test_a_hung_spawn_is_bounded_rather_than_forever(monkeypatch) -> None:
    """The load-bearing one: a child that never exits must not block.

    Unfixed, ``_run`` called ``subprocess.run`` with no timeout and this
    test hung until pytest's own timeout killed it.
    """
    monkeypatch.setattr(pp, "refuse_root", lambda: None)
    monkeypatch.setattr(pp, "_bundle_lib_env", lambda _cmd, _env: None)

    with pytest.raises(subprocess.TimeoutExpired):
        pp._run(["/bin/sh", "-c", "sleep 30"], check=False, timeout=0.5)


def test_a_timed_out_spawn_takes_its_grandchildren_with_it(monkeypatch, tmp_path) -> None:
    """The POSIX half of the defect: reaping the direct child says nothing
    about its descendants.

    pg_ctl spawns the postmaster, which spawns its own children, so a
    timed-out start that only reaps pg_ctl leaves a cluster running that
    nothing is tracking. Routing through run_bounded group-kills.

    Unfixed (or with the group kill removed from run_bounded), the
    grandchild survives and writes the marker after the bound has passed.
    """
    monkeypatch.setattr(pp, "refuse_root", lambda: None)
    monkeypatch.setattr(pp, "_bundle_lib_env", lambda _cmd, _env: None)
    marker = tmp_path / "grandchild-survived"

    # sh backgrounds a grandchild that outlives it, then sleeps so the
    # bound fires on the direct child while the grandchild still holds on.
    script = f"( sleep 3; touch {marker!s} ) & sleep 30"
    with pytest.raises(subprocess.TimeoutExpired):
        pp._run(["/bin/sh", "-c", script], check=False, timeout=0.5)

    import time

    time.sleep(4.0)
    assert not marker.exists(), (
        "a grandchild of the timed-out spawn outlived the bound and kept "
        "running -- the process group was not killed"
    )


def test_the_debug_log_line_carries_no_password(monkeypatch) -> None:
    """``_log.debug("pg_provision_run", cmd=...)``.

    Unfixed this logged raw argv, and a real provisioning run printed the
    generated nexus_admin / nexus_svc / nexus_diag passwords to the
    terminal.
    """
    monkeypatch.setattr(pp, "refuse_root", lambda: None)
    monkeypatch.setattr(pp, "_bundle_lib_env", lambda _cmd, _env: None)
    seen: list[object] = []
    monkeypatch.setattr(pp._log, "debug", lambda _event, **kw: seen.append(kw.get("cmd")))
    monkeypatch.setattr(
        pp, "run_bounded",
        lambda *_a, **_kw: subprocess.CompletedProcess([], 0, "", ""),
    )

    pp._run(_psql_argv_with_password(), check=False)

    assert seen, "the debug line did not fire; this test is asserting nothing"
    assert _SECRET not in str(seen[0]), f"password reached the debug log: {seen[0]}"
    assert "PASSWORD" in str(seen[0]), (
        "the whole command was dropped rather than redacted -- the log should "
        "still be useful for diagnosis, just without the secret"
    )


@pytest.mark.parametrize(
    "raised",
    [
        subprocess.CalledProcessError(1, _psql_argv_with_password()),
        subprocess.TimeoutExpired(_psql_argv_with_password(), 60.0),
    ],
    ids=["CalledProcessError", "TimeoutExpired"],
)
def test_no_exception_carries_the_password_into_its_message(
    monkeypatch, raised: Exception,
) -> None:
    """Both exception types embed argv in ``str()``.

    This is the path that reached the user: ``commands/init.py`` catches
    broadly and does ``click.echo(f"...failed: {exc}")`` at default
    verbosity, plus ``_log.error(error=str(exc))``.

    Unfixed, ``str(exc)`` contained the password verbatim for both types.
    Parametrised over both because bounding the call is what ADDS the
    second one -- covering only CalledProcessError would have passed while
    the new type leaked.
    """
    monkeypatch.setattr(pp, "refuse_root", lambda: None)
    monkeypatch.setattr(pp, "_bundle_lib_env", lambda _cmd, _env: None)
    monkeypatch.setattr(pp._log, "debug", lambda *_a, **_kw: None)

    def _boom(*_a, **_kw):
        raise raised

    monkeypatch.setattr(pp, "run_bounded", _boom)

    with pytest.raises(type(raised)) as caught:
        pp._run(_psql_argv_with_password(), check=True)

    assert _SECRET not in str(caught.value), (
        f"{type(raised).__name__}.__str__ leaked the password: {caught.value}"
    )
    # The type must survive the rewrap: callers catch CalledProcessError by
    # name, and provision() documents it.
    assert isinstance(caught.value, type(raised))


def test_the_redactor_is_actually_doing_it_not_the_absence_of_a_password() -> None:
    """Non-vacuity: prove the fixture's secret WOULD survive an unredacted
    round trip, so the assertions above are testing the scrub rather than a
    password that was never there.
    """
    argv = _psql_argv_with_password()
    assert _SECRET in str(subprocess.CalledProcessError(1, argv)), (
        "the control is broken: the raw exception does not contain the secret, "
        "so the redaction tests above would pass with no redactor at all"
    )
    assert _SECRET not in str(pp._redacted(argv))
