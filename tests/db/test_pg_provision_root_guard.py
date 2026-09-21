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

from pathlib import Path

import pytest

from nexus.db import pg_provision
from nexus.db.pg_provision import PgRootUserError, _refuse_root, provision


class _Tripwire(Exception):
    """Raised by anything the guard is supposed to run BEFORE."""


def test_refuse_root_raises_when_euid_is_zero(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(pg_provision.os, "geteuid", lambda: 0, raising=False)
    with pytest.raises(PgRootUserError):
        _refuse_root()


def test_refuse_root_is_silent_for_an_unprivileged_euid(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    # Non-vacuity for the test above: the guard keys on the euid VALUE, so it
    # cannot be passing merely because it raises unconditionally.
    monkeypatch.setattr(pg_provision.os, "geteuid", lambda: 1000, raising=False)
    _refuse_root()


def test_refuse_root_treats_a_missing_geteuid_as_not_root(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    # os.geteuid is POSIX-only. Windows support is WSL2-only, so an absent
    # geteuid is 'not root' rather than an AttributeError mid-provision.
    monkeypatch.delattr(pg_provision.os, "geteuid", raising=False)
    _refuse_root()


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
