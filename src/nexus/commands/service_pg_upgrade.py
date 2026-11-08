# SPDX-License-Identifier: AGPL-3.0-or-later
"""``nx daemon service pg-upgrade``: upgrade a local cluster made by PostgreSQL below 17 (nexus-j738q).

The guided form of the runbook section "A local cluster older than PostgreSQL 17". The logic lives in
:mod:`nexus.db.pg_cluster_upgrade`; this module is the Click surface and the one place that talks to the service.
"""
from __future__ import annotations

import os
from pathlib import Path

import click

from nexus import config as _config
from nexus.bounded_subprocess import run_bounded
from nexus.commands._help_links import link_epilog

_SERVICE_VERB_TIMEOUT_S: float = 600.0


def _nx_service(verb: str, config_dir: Path) -> int:
    """Run ``nx daemon service <verb> --config-dir <dir>``; return its exit code.

    The child gets no ``PGPASSWORD``: the service must not inherit one.
    """
    from nexus.commands.daemon import _resolve_nx_bin  # noqa: PLC0415 — deferred: daemon.py imports this module

    env = {k: v for k, v in os.environ.items() if k != "PGPASSWORD"}
    argv = [*_resolve_nx_bin(), "daemon", "service", verb, "--config-dir", str(config_dir)]
    return run_bounded(argv, timeout=_SERVICE_VERB_TIMEOUT_S, env=env, stdout=None, stderr=None).returncode


@click.command(
    "pg-upgrade",
    epilog=link_epilog("Upgrading a local cluster older than PostgreSQL 17", "migration-runbook.md"),
)
@click.option(
    "--old-bin",
    "old_bin",
    required=True,
    type=click.Path(path_type=Path, file_okay=False),
    help="The bin/ directory of the PostgreSQL that created the cluster (its pg_ctl --version must match the "
    "cluster's PG_VERSION). If that PostgreSQL is gone, install the same major version first.",
)
@click.option(
    "--check-only",
    "check_only",
    is_flag=True,
    default=False,
    help="Run initdb into a temporary directory and pg_upgrade --check against it, then remove it. "
    "Never creates or moves the real data directory. The old server must not be running. Holds the same lock "
    "as --apply; an interrupt stops the old server pg_upgrade --check started.",
)
@click.option(
    "--apply",
    "do_apply",
    is_flag=True,
    default=False,
    help="Do the upgrade: stop the service and the old server, move the data directory to <data>.old, "
    "initdb, pg_upgrade --check, pg_upgrade, start the service, then update_extensions.sql and "
    "vacuumdb --all --analyze-in-stages. Any failure after the move removes the new directory and "
    "moves the original back. <data>.old is kept. "
    "This command is not yet run against a real pre-17 cluster; copy the data directory first. "
    "Holds the storage service's spawn lock for the whole run (freed just before the service is started "
    "again), so a concurrent `nx daemon service start` or an autostart unit waits instead of starting on the "
    "half-built cluster; it does not stop a service that is already running (--apply stops it) or a "
    "PostgreSQL server that brew services or systemd restarts.",
)
@click.option("--config-dir", "config_dir_str", default=None, help="Config directory override.")
def pg_upgrade_cmd(old_bin: Path, check_only: bool, do_apply: bool, config_dir_str: str | None) -> None:
    """Upgrade a local cluster made by PostgreSQL below 17 to the bundle's PostgreSQL 17.

    With neither flag this is a dry run: it validates the cluster and --old-bin, prints the steps, and
    changes nothing. Linux and macOS only. Not yet run against a real pre-17 cluster; copy the data
    directory first.
    """
    if check_only and do_apply:
        raise click.UsageError("--check-only and --apply are separate modes: give one")
    from nexus.db.pg_cluster_upgrade import (  # noqa: PLC0415 — deferred: CLI startup cost, this command only
        ClusterUpgrade,
        PgUpgradeFailed,
        PgUpgradeRefused,
        build_plan,
    )

    config_dir = Path(config_dir_str) if config_dir_str else _config.nexus_config_dir()
    try:
        plan = build_plan(config_dir, old_bin, require_no_old_dir=not check_only)
        upgrade = ClusterUpgrade(plan, say=click.echo, service=_nx_service)
        if do_apply:
            upgrade.apply()
        elif check_only:
            upgrade.check_only()
        else:
            upgrade.dry_run()
    except (PgUpgradeRefused, PgUpgradeFailed) as exc:
        raise click.ClickException(str(exc)) from None
