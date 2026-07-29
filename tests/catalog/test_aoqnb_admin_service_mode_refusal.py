# SPDX-License-Identifier: AGPL-3.0-or-later
# Copyright (c) 2026 Hal Hildebrand. All rights reserved.
"""Deep-maintenance catalog verbs must not write the frozen .catalog.db.

nexus-aoqnb (GH #1419 Issue 4). At one backup timestamp ``.catalog.db`` showed
532 docs / 13 links against Postgres's 592 / 52 — a stale-but-plausible file
that a recovery procedure reaches for first.

The bead asked two things. This file answers the first and harder one: **is a
writer still live in 6.x service mode, or did writes simply stop at migration?**

A writer was live. ``make_catalog_writer`` and ``make_catalog_reader`` both
route to the HTTP client in service mode, but ``make_catalog_admin`` did NOT —
it checked initialization and daemon liveness, then returned a direct
``Catalog(path, path / ".catalog.db")``. Its only two callers are the ones that
MUTATE:

    nx catalog dedupe-owners --apply     (commands/catalog_cmds/owners.py)
    the backup restore verb              (commands/catalog_cmds/backups.py)

So on a migrated install, a restore would re-emit every document into a file
nothing reads and report success. That is worse than a stale mirror: it is a
silent write to the wrong substrate, which is exactly the divergence signature
the bead reported.

The refusal is at ``make_catalog_admin`` — the single choke point — rather than
in each command, so a third caller cannot reintroduce it by forgetting.

WHY REFUSE RATHER THAN ROUTE: these verbs mutate through the catalog's
low-level event log, not the 22 daemon write ops, so there is no equivalent
service path to forward them to. A silent no-op or a partial port would both be
less honest than saying the verb is unavailable.
"""
from __future__ import annotations

import pytest


@pytest.fixture()
def service_mode(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("NX_STORAGE_BACKEND_CATALOG", "service")


def test_admin_open_refuses_in_service_mode(service_mode: None) -> None:
    """The headline: no direct handle on the frozen file."""
    from nexus.catalog.factory import CatalogAdminServiceModeError, make_catalog_admin

    with pytest.raises(CatalogAdminServiceModeError) as exc:
        make_catalog_admin()

    msg = str(exc.value)
    assert "service mode" in msg.lower()
    assert "frozen" in msg.lower(), "the operator must learn WHY, not just that it failed"
    assert "nexus-aoqnb" in msg


def test_refusal_precedes_initialization_and_daemon_probes(
    service_mode: None, monkeypatch: pytest.MonkeyPatch, tmp_path
) -> None:
    """Ordering is load-bearing.

    The pre-existing guard returns ``None`` for an uninitialised catalog. If it
    ran first, a service-mode box with no local catalog would get a bare ``None``
    (read by callers as "not initialized — run nx catalog setup", sending the
    operator to create a SQLite catalog on a PG install) instead of the real
    explanation.
    """
    from nexus.catalog import factory
    from nexus.catalog.factory import CatalogAdminServiceModeError, make_catalog_admin

    probed = {"init": False, "daemon": False}

    def _init_probe(_path):
        probed["init"] = True
        return False

    monkeypatch.setattr(factory.Catalog, "is_initialized", staticmethod(_init_probe))
    monkeypatch.setattr("nexus.config.catalog_path", lambda: tmp_path)

    with pytest.raises(CatalogAdminServiceModeError):
        make_catalog_admin()

    assert probed["init"] is False, (
        "initialization was probed before the service-mode refusal — an "
        "uninitialised service box would get a misleading 'run nx catalog "
        "setup' instead of the real reason"
    )


# NO test_it_is_a_distinct_type_from_the_daemon_live_error: the distinction was
# between this error ("no retry exists") and CatalogAdminDaemonLiveError ("stop
# the daemon and retry"). The latter retired with the T2 daemon (nexus-i711w
# Stage 2 sub-stage B), so there is no second type left to be distinct from and
# no wrong remedy a caller could reach for.


@pytest.mark.parametrize("argv", [
    ["dedupe-owners", "--apply"],
    ["undelete", "somebackup"],
])
def test_cli_callers_surface_it_cleanly(argv, service_mode, monkeypatch) -> None:
    """Both mutating callers must show a clean CLI error, never a traceback.

    Parametrized over both so adding a third caller that forgets the handler is
    visible here rather than as a stack trace in front of an operator who is
    already mid-recovery.
    """
    import click
    from click.testing import CliRunner

    from nexus.commands.catalog import catalog as catalog_group

    result = CliRunner().invoke(catalog_group, argv)

    combined = (result.output or "") + (result.stderr or "")
    # NOT skipped on a name miss: a skip here would prove nothing while looking
    # green, and the first cut of this test DID silently skip (it guessed
    # "restore-backup"; the real verb is "undelete"). Fail loudly instead so a
    # renamed verb is visible rather than quietly uncovered.
    assert "No such command" not in combined, (
        f"verb {argv[0]!r} is not registered — this test is not exercising "
        f"anything. Update the name rather than leaving it to skip."
    )
    assert result.exit_code != 0, combined
    assert "Traceback" not in combined, combined
    assert "service mode" in combined.lower(), combined
    assert isinstance(result.exception, (SystemExit, click.ClickException, type(None))), (
        f"leaked a non-Click exception: {result.exception!r}"
    )


def test_sqlite_mode_is_untouched(monkeypatch, tmp_path) -> None:
    """Non-regression: the refusal must be scoped to service mode only.

    Deep maintenance on a genuine SQLite install is still the supported path —
    breaking it would trade a silent-wrong-write for a lost capability.
    """
    from nexus.catalog import factory
    from nexus.catalog.factory import make_catalog_admin

    # SERVICE is the HARD DEFAULT (storage_mode.storage_backend_for: per-store
    # env -> global env -> SERVICE). `sqlite` is the explicit OPT-OUT, so
    # clearing the env selects service, not sqlite — the first cut of this test
    # got that backwards and "failed" against a correct guard.
    #
    # Worth stating because it raises this bead's severity: the unguarded admin
    # opener was handing back a direct .catalog.db writer on every
    # default-configured install, not only on explicitly-migrated ones.
    monkeypatch.setenv("NX_STORAGE_BACKEND_CATALOG", "sqlite")
    monkeypatch.setattr("nexus.config.catalog_path", lambda: tmp_path)
    monkeypatch.setattr(
        factory.Catalog, "is_initialized", staticmethod(lambda _p: False)
    )

    # Uninitialised -> None, NOT a refusal. Proves the guard did not fire.
    assert make_catalog_admin() is None
