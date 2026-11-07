# SPDX-License-Identifier: AGPL-3.0-or-later
"""``nx daemon service note-uri-pairs``: list the RDR-231 note-URI pairs that halt the engine (nexus-bivz3.28).

The runbook step "If the RDR-231 note-URI backfill halts startup" starts with this query. The logic, including the
platform's own superuser name and the password handling, lives in :mod:`nexus.db.note_uri_pairs`; this module is
the Click surface.
"""
from __future__ import annotations

from pathlib import Path

import click

from nexus import config as _config
from nexus.commands._help_links import link_epilog

#: `--check` exit status when a pair exists (1 is every failure, so a script can tell them apart).
CHECK_EXIT_PAIRS = 3


@click.command(
    "note-uri-pairs",
    epilog=link_epilog("If the RDR-231 note-URI backfill halts startup", "migration-runbook.md"),
)
@click.option("--config-dir", "config_dir_str", default=None, help="Config directory override.")
@click.option(
    "--check",
    "check",
    is_flag=True,
    default=False,
    help="Exit 3 when any pair exists, 0 when there is none (the listing is printed either way).",
)
def note_uri_pairs_cmd(config_dir_str: str | None, check: bool) -> None:
    """List the notes stored under both the legacy and the nx-note spelling (RDR-231), from the local cluster.

    Connects to the nx-managed PostgreSQL cluster as its superuser, so it works while the engine is halted
    and on every platform; the password never reaches a command line or the output. A managed or cloud
    install has no local cluster: the command says so and exits 1. Merge each pair with
    `nx catalog merge <duplicate> <canonical>`. Exit 0 when the query ran; with --check, exit 3 when a pair exists.
    """
    from nexus.db.note_uri_pairs import RUNBOOK_URL, NoteUriPairsError, list_note_uri_pairs  # noqa: PLC0415 — deferred: CLI startup cost, this command only

    local_install = _config.is_local_mode()
    if config_dir_str is None and not local_install:
        raise click.ClickException(
            "this install uses a managed or cloud service, which has no local PostgreSQL cluster to ask. "
            "Run the query as the operator's diagnostic role; the Cloud steps of 'If the RDR-231 note-URI "
            f"backfill halts startup' are at {RUNBOOK_URL}"
        )
    config_dir = Path(config_dir_str) if config_dir_str else _config.nexus_config_dir()
    try:
        report = list_note_uri_pairs(config_dir, local_install=local_install)
    except NoteUriPairsError as exc:
        raise click.ClickException(str(exc)) from None
    for line in report.lines():
        click.echo(line)
    if check and report.pairs:
        click.get_current_context().exit(CHECK_EXIT_PAIRS)
