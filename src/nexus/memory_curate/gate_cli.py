# SPDX-License-Identifier: AGPL-3.0-or-later
"""The ``nx memory gate-rollup`` click command (RDR-209 Phase 3). ``commands/memory.py`` only registers it."""
from __future__ import annotations

from pathlib import Path

import click

from nexus.commands._helpers import t2_handle


@click.command("gate-rollup")
@click.option("--rdr", type=click.IntRange(1, 9999), required=True, help="The RDR number")
@click.option("--phase", default=None, help="The phase to roll up (3, 3a); needs --beads")
@click.option("--beads", default=None,
              help="The phase's bead ids, comma separated: the 'Item N = <bead-id>' lines of the phase-review-gate close step")
@click.option("--all", "all_phases", is_flag=True, default=False,
              help="Every phase of the RDR, by its rdr:<NNN> tag (the rdr-close step); alone")
@click.option("--accept-open", "accept_open", multiple=True,
              help="Let a bead through whose newest record is not justified (repeatable); the pointer records it")
@click.option("--project", "-p", default=None, help="The repository's T2 project (default: this repository)")
@click.option("--dry-run", is_flag=True, default=False, help="Print the listing; write nothing")
def gate_rollup_cmd(rdr: int, phase: str | None, beads: str | None, all_phases: bool, accept_open: tuple[str, ...],
                    project: str | None, dry_run: bool) -> None:
    """List a closed phase's gate records and keep them in T3 (RDR-209 Phase 3).

    Reads the repository's gate records (whole tag kind:gate, never <repo>_rdr). Before anything is written
    it refuses an open phase and a bead whose newest record is not justified (unless named with
    --accept-open). It then archives each listed record's full text as a T3 note in nexus-rdr-research and writes one
    pointer record, gate-records-rdr-<NNN>-phase-<P> or -all, that lists them with their verdicts, shas and
    timestamps, and reads it back. A record an earlier pointer listed that is no longer live stays listed.
    Records that map to no bead or have no readable verdict are unmapped: not archived, no TTL.
    """
    from nexus.db import make_t3  # noqa: PLC0415 — deferred: heavy T3 dep, only a write needs it
    from nexus.memory_curate import gate_rollup as gr  # noqa: PLC0415 — deferred: only this verb needs the modules
    from nexus.memory_curate import gate_ttl, repo_toplevel  # noqa: PLC0415 — deferred, as above

    try:
        scope = gr.parse_scope(rdr=rdr, phase=phase, beads=beads, all_phases=all_phases)
    except gr.ScopeError as exc:
        raise click.UsageError(str(exc)) from exc
    if project is None:
        from nexus.repo_identity import _repo_identity  # noqa: PLC0415 — deferred: only the default needs the repository name

        project = _repo_identity(Path.cwd())[0]
    try:
        with t2_handle() as db:
            result = gate_ttl.run(db.memory, project=project, scope=scope, dry_run=dry_run,
                                  accept_open=tuple(accept_open), root=repo_toplevel(Path.cwd()), t3_factory=make_t3)
    except gr.GateRollupError as exc:
        raise click.ClickException(str(exc)) from exc
    for line in result.lines:
        click.echo(line)
    if result.mismatches:
        raise click.ClickException(f"{len(result.mismatches)} field(s) did not read back as written; nothing was undone")
