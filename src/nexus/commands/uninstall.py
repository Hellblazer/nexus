# SPDX-License-Identifier: AGPL-3.0-or-later
# Copyright (c) 2026 Hal Hildebrand. All rights reserved.
"""``nx uninstall`` — first-class agent teardown (RDR-165 Phase 3).

The complete teardown a user needs to cleanly remove nexus, covering BOTH
install shapes (auto-detected; each branch is a no-op when its target is absent):

* **Local service** (eu4u4): stop the engine-service + Postgres stack, stop the
  T2 daemon, remove the OS autostart unit, clear the first-run marker, and
  (only with ``--remove-data``) wipe the local data dir. Orchestrated through
  ``installer.uninstall_daemon`` which shells the existing
  ``nx daemon service stop --with-pg`` (RDR-149: no duplicated lifecycle).
* **Managed-only client** (wigzi): clear the managed endpoint + token from
  ``config.yml`` and reset the capability-probe cache. SKIPs service-stop (no
  local service) and SKIPs data-wipe (the data lives in the remote tenant).

``nx uninstall`` is DRY-RUN by default — it prints what would be removed and
touches nothing; pass ``--yes`` to perform the teardown (mirrors the
``daemon_uninstall`` MCP tool's ``confirm`` semantics).
"""
from __future__ import annotations

import os

import click

from nexus.config import get_credential, unset_credential
from nexus.daemon.installer import uninstall_daemon

#: The managed-client credentials cleared by the managed-only teardown branch.
_MANAGED_CREDENTIALS = ("service_url", "service_token")


def _teardown_managed(*, confirm: bool) -> tuple[list[str], list[str]]:
    """Clear the managed endpoint config (wigzi). Returns (lines, warnings).

    Idempotent: a no-op (empty lines/warnings) when no managed endpoint is
    configured. On ``confirm`` it removes ``service_url``/``service_token`` from
    config.yml; on dry-run it only reports. A SHELL-env override
    (``NX_SERVICE_URL``/``NX_SERVICE_TOKEN``) cannot be unset from the parent
    shell — that is surfaced as a loud warning, never silently "cleared".
    SKIPs service-stop (no local service) and never touches the remote tenant.
    """
    if not (get_credential("service_url") or "").strip():
        return [], []  # no managed endpoint configured — nothing to tear down

    lines: list[str] = []
    warnings: list[str] = []
    if confirm:
        cleared = [name for name in _MANAGED_CREDENTIALS if unset_credential(name)]
        lines.append(
            "Managed client: cleared the managed endpoint config from config.yml"
            + (f" ({', '.join(cleared)})." if cleared else " (nothing persisted).")
        )
    else:
        lines.append(
            "Managed client: would clear the managed endpoint config "
            "(service_url + service_token) from config.yml."
        )
    # Honesty guard: a shell-exported env var overrides config.yml and survives.
    env_overrides = [e for e in ("NX_SERVICE_URL", "NX_SERVICE_TOKEN") if os.environ.get(e, "").strip()]
    if env_overrides:
        warnings.append(
            f"{' and '.join(env_overrides)} {'is' if len(env_overrides) == 1 else 'are'} set in "
            "your shell environment and override config.yml — `nx` cannot unset the parent "
            f"shell; unset {'it' if len(env_overrides) == 1 else 'them'} manually "
            f"(e.g. `unset {' '.join(env_overrides)}`)."
        )
    return lines, warnings


@click.command("uninstall")
@click.option(
    "--yes",
    "assume_yes",
    is_flag=True,
    default=False,
    help="Perform the teardown. Without this, uninstall only PREVIEWS what would "
    "be removed (dry-run default).",
)
@click.option(
    "--remove-data",
    "remove_data",
    is_flag=True,
    default=False,
    help="ALSO wipe the local nexus data dir (notes + search index). Irreversible; "
    "only acts with --yes. Does NOT touch a managed (remote) tenant's data.",
)
def uninstall_cmd(assume_yes: bool, remove_data: bool) -> None:
    """Cleanly remove the local nexus service stack and/or managed client config.

    Dry-run by default: shows what would be removed. Use --yes to proceed.
    """
    # Managed-only branch (wigzi) — clear the managed endpoint config if present.
    managed_lines, managed_warnings = _teardown_managed(confirm=assume_yes)
    for line in managed_lines:
        click.echo(line)

    # Local branch (eu4u4) — stop the service stack + daemon, remove autostart +
    # marker, optionally wipe local data. Idempotent: no-op when no local service.
    report = uninstall_daemon(confirm=assume_yes, remove_data=remove_data)
    click.echo(report.message)

    for w in (*managed_warnings, *report.warnings):
        click.echo(f"  warning: {w}", err=True)
    if not assume_yes:
        click.echo("\nDry run — nothing was removed. Re-run with --yes to proceed.")
