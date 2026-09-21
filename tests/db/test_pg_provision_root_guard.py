# SPDX-License-Identifier: AGPL-3.0-or-later
"""Unit tests for provision()'s refuse-as-root guard (nexus-ov1oq).

The regression: on a root-default WSL2 Ubuntu 26.04 distro, `nx init` fetched
and verified the ~100MB bundled PostgreSQL and only then died with a bare

    Command '[.../initdb, -D, /root/.config/nexus/postgres, ...]' returned
    non-zero exit status 1.

which does not name the cause. initdb's own message does: "cannot be run as
root". No part of provisioning can succeed as root, so the guard refuses at
the top, before the download, and says what to do instead.

No PostgreSQL binaries required — every test here refuses before binary
discovery, and that ordering is itself what two of them assert.
"""
from __future__ import annotations

import contextlib
from pathlib import Path

import pytest

from nexus.commands import init as init_cmd
from nexus.db import pg_provision
from nexus.db.pg_provision import PgRootUserError, refuse_root, provision


class _Tripwire(Exception):
    """Raised by anything the guard is supposed to run BEFORE."""


def test_refuse_root_raises_when_euid_is_zero(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(pg_provision.os, "geteuid", lambda: 0, raising=False)
    with pytest.raises(PgRootUserError):
        refuse_root()


def test_refuse_root_is_silent_for_an_unprivileged_euid(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    # Non-vacuity for the test above: the guard keys on the euid VALUE, so it
    # cannot be passing merely because it raises unconditionally.
    monkeypatch.setattr(pg_provision.os, "geteuid", lambda: 1000, raising=False)
    refuse_root()


def test_refuse_root_treats_a_missing_geteuid_as_not_root(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    # os.geteuid is POSIX-only. Windows support is WSL2-only, so an absent
    # geteuid is 'not root' rather than an AttributeError mid-provision.
    monkeypatch.delattr(pg_provision.os, "geteuid", raising=False)
    refuse_root()


def test_provision_refuses_as_root_before_touching_the_bundle(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    monkeypatch.setattr(pg_provision.os, "geteuid", lambda: 0, raising=False)
    # Everything provision() would reach after the guard trips the tripwire,
    # so a PgRootUserError here proves the guard ran FIRST — which is the
    # point of the fix, the old failure being a paid-for download away.
    monkeypatch.setattr(
        pg_provision, "bootstrap_superuser", lambda: (_ for _ in ()).throw(_Tripwire())
    )
    monkeypatch.setattr(
        pg_provision,
        "discover_pg_binaries",
        lambda: (_ for _ in ()).throw(_Tripwire()),
    )

    with pytest.raises(PgRootUserError):
        provision(config_dir=tmp_path)


def test_provision_reaches_the_bundle_when_not_root(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    # Non-vacuity for the test above: with the same tripwires in place and the
    # only difference being the euid, provision() gets PAST the guard. Without
    # this, the assertion above would hold for a provision() that raised
    # PgRootUserError unconditionally.
    monkeypatch.setattr(pg_provision.os, "geteuid", lambda: 1000, raising=False)
    monkeypatch.setattr(
        pg_provision, "bootstrap_superuser", lambda: (_ for _ in ()).throw(_Tripwire())
    )

    with pytest.raises(_Tripwire):
        provision(config_dir=tmp_path)


def test_the_message_names_the_cause_and_the_remedy() -> None:
    msg = pg_provision.root_user_remedy()
    # The cause the old CalledProcessError omitted.
    assert "cannot be run as root" in msg
    # The remedy, in a form that can be pasted.
    assert "useradd -m -s /bin/bash nexus" in msg
    # The environment class that hits it.
    assert "wsl.conf" in msg


def _tripwired_init(monkeypatch: pytest.MonkeyPatch) -> list[str]:
    """Record which bundle paths get reached, rather than how the step exits.

    The step wraps bundle acquisition in `except Exception` and turns any
    failure into SystemExit(1), so asserting on SystemExit cannot tell the
    guard firing apart from the bundle blowing up. The first version of this
    test did exactly that and passed with the guard deleted.
    """
    reached: list[str] = []
    monkeypatch.setattr(
        init_cmd, "_select_bundled_pg",
        lambda *_a, **_k: (reached.append("extract"), None)[1],
    )
    monkeypatch.setattr(
        init_cmd, "_acquire_pg_bundle_step",
        lambda *_a, **_k: reached.append("download"),
    )
    return reached


def test_init_refuses_root_before_the_bundle_is_acquired(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The claim the FIRST cut of this fix made and did not deliver.

    provision()'s own guard is too late to save the download: init.py's
    _provision_postgres_step acquires the bundle and only THEN calls
    provision(). The effect worth asserting is that a root user never reaches
    the acquisition at all.
    """
    monkeypatch.setattr(pg_provision.os, "geteuid", lambda: 0, raising=False)
    reached = _tripwired_init(monkeypatch)

    with pytest.raises(SystemExit) as exc:
        init_cmd._provision_postgres_step()

    assert exc.value.code == 1
    assert reached == [], (
        f"root reached the bundle path(s) {reached} before being refused — "
        "the guard in _provision_postgres_step is missing or too late, which "
        "is the nexus-ov1oq defect its first fix claimed to have closed"
    )


def test_init_reaches_the_bundle_when_not_root(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Non-vacuity: with ONLY the euid changed, the bundle path IS reached."""
    monkeypatch.setattr(pg_provision.os, "geteuid", lambda: 1000, raising=False)
    reached = _tripwired_init(monkeypatch)

    # What happens AFTER the bundle path is not this test's business — the
    # step may fail for any number of reasons in a sandbox with no real
    # bundle. The assertion is only that a non-root user gets that far.
    with contextlib.suppress(BaseException):
        init_cmd._provision_postgres_step()

    assert reached, "the bundle path was never reached even as a normal user"
