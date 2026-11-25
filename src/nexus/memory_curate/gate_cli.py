# SPDX-License-Identifier: AGPL-3.0-or-later
"""The ``nx memory gate-rollup`` click command (RDR-209 Phase 3). ``commands/memory.py`` only registers it."""
from __future__ import annotations

from pathlib import Path

import click

from nexus.commands._helpers import t2_handle


def _head(root: Path) -> str:
    """The repository's current commit, recorded with a dry run's count; ``unknown`` when there is none."""
    from nexus.bounded_subprocess import run_bounded  # noqa: PLC0415 — deferred: only a dry run records it
    from nexus.util.nx_argv import argv_off_cwd  # noqa: PLC0415 — deferred, as above

    try:
        proc = run_bounded(argv_off_cwd(["git", "-C", str(root), "rev-parse", "--short=12", "HEAD"]), timeout=5)
    except Exception:  # noqa: BLE001 — no git, a timeout or no commit: the count is recorded without it
        return "unknown"
    return proc.stdout.strip() or "unknown" if proc.returncode == 0 else "unknown"


@click.command("gate-rollup")
@click.option("--rdr", type=click.IntRange(1, 9999), required=True, help="The RDR number")
@click.option("--phase", default=None, help="The phase to roll up (3, 3a); needs --beads")
@click.option("--beads", default=None,
              help="The phase's bead ids, comma separated: the 'Item N = <bead-id>' lines of the phase-review-gate close step")
@click.option("--all", "all_phases", is_flag=True, default=False,
              help="Every phase of the RDR, by its rdr:<NNN> tag (the rdr-close step); alone")
@click.option("--accept-open", "accept_open", multiple=True,
              help="Let a bead through whose newest record is not justified (repeatable; the pointer records it). "
                   "Pass it only with the user's stamp for that bead")
@click.option("--counted", type=click.IntRange(min=0), default=None,
              help="The count a --dry-run reported ('would be quarantined by the next expire sweep'); a real run that "
                   "writes a TTL needs it, and refuses unless it equals the count that dry run recorded")
@click.option("--project", "-p", default=None, help="The repository's T2 project (default: this repository)")
@click.option("--dry-run", is_flag=True, default=False, help="Print the listing and the count a real run would need; writes one T2 count row, no TTL and no T3 note")
def gate_rollup_cmd(rdr: int, phase: str | None, beads: str | None, all_phases: bool, accept_open: tuple[str, ...],
                    counted: int | None, project: str | None, dry_run: bool) -> None:
    """List a closed phase's gate records and keep them in T3.

    Reads the repository's gate records (whole tag kind:gate, never <repo>_rdr). --dry-run writes one T2 count
    row (no TTL, no T3 note), which a real run that writes a TTL requires, with --counted N. Before anything is written
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
    from nexus.repo_identity import _repo_identity  # noqa: PLC0415 — deferred: only this verb needs the repository name

    try:
        repo = _repo_identity(Path.cwd())[0]
    except Exception as exc:  # noqa: BLE001 — a deleted working directory or an identity failure: name it, write nothing
        raise click.ClickException(f"the repository could not be identified: {type(exc).__name__}: {exc}") from exc
    project = repo if project is None else project
    if project.endswith("_rdr"):
        raise click.UsageError(f"--project {project} is the RDR gate flow's project: gate-rollup never reads or changes it")
    if project != repo:
        raise click.UsageError(
            f"--project {project} is not the repository you are in ({repo}): the open-phase check reads this "
            "repository's RDR files, and no other checkout's. Run it from that repository's checkout.")
    root = repo_toplevel(Path.cwd())
    try:
        with t2_handle() as db:
            result = gate_ttl.run(db.memory, project=project, scope=scope, dry_run=dry_run,
                                  accept_open=tuple(accept_open), root=root, t3_factory=make_t3, counted=counted,
                                  head=_head(root))
    except gr.GateRollupError as exc:
        raise click.ClickException(str(exc)) from exc
    for line in result.lines:
        click.echo(line)
    if result.mismatches:
        raise click.ClickException(f"{len(result.mismatches)} field(s) did not read back as written; nothing was undone")
