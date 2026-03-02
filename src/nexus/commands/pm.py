# SPDX-License-Identifier: AGPL-3.0-or-later
"""nx pm command group -- project management infrastructure."""
from pathlib import Path

import click

from nexus.commands._helpers import default_db_path as _default_db_path
from nexus.db.t2 import T2Database
from nexus.pm import (
    pm_block,
    pm_init,
    pm_phase_next,
    pm_resume,
    pm_search,
    pm_status,
    pm_unblock,
)


def _infer_project() -> str:
    """Infer the project name from the current git repo name, or fallback."""
    import subprocess
    try:
        result = subprocess.run(
            ["git", "rev-parse", "--show-toplevel"],
            capture_output=True, text=True, check=True,
        )
        return Path(result.stdout.strip()).name
    except (subprocess.CalledProcessError, FileNotFoundError):
        return Path.cwd().name


@click.group()
def pm() -> None:
    """Project management: phases, blockers, search, and expire."""


@pm.command("init")
@click.option("--project", default=None, help="Project name (defaults to repo name)")
def init_cmd(project: str | None) -> None:
    """Initialise PM docs for the current project in T2."""
    proj = project or _infer_project()
    with T2Database(_default_db_path()) as db:
        pm_init(db, project=proj)
    click.echo(f"Initialised PM for project '{proj}' (4 standard docs created).")


@pm.command("resume")
@click.option("--project", default=None, help="Project name (defaults to repo name)")
def resume_cmd(project: str | None) -> None:
    """Print computed PM continuation (phase, blockers, recent activity; capped at 2000 chars)."""
    proj = project or _infer_project()
    with T2Database(_default_db_path()) as db:
        content = pm_resume(db, project=proj)
    if content is None:
        raise click.ClickException(f"No PM project found for '{proj}'. Run `nx pm init` first.")
    click.echo(content)


@pm.command("status")
@click.option("--project", default=None, help="Project name (defaults to repo name)")
def status_cmd(project: str | None) -> None:
    """Show current phase, last-updated agent, and open blockers."""
    proj = project or _infer_project()
    with T2Database(_default_db_path()) as db:
        status = pm_status(db, project=proj)
    click.echo(f"Phase   : {status['phase']}")
    click.echo(f"Agent   : {status['agent'] or '(none)'}")
    blockers = status["blockers"]
    if blockers:
        click.echo("Blockers:")
        for i, b in enumerate(blockers, 1):
            click.echo(f"  {i}. {b}")
    else:
        click.echo("Blockers: none")


@pm.command("block")
@click.argument("blocker")
@click.option("--project", default=None)
def block_cmd(blocker: str, project: str | None) -> None:
    """Add a blocker line to the project's BLOCKERS.md.

    BLOCKER is the text describing what is blocking progress.
    """
    proj = project or _infer_project()
    with T2Database(_default_db_path()) as db:
        pm_block(db, project=proj, blocker=blocker)
    click.echo(f"Blocker added: {blocker}")


@pm.command("unblock")
@click.argument("line", type=int)
@click.option("--project", default=None)
def unblock_cmd(line: int, project: str | None) -> None:
    """Remove a blocker by line number from BLOCKERS.md.

    LINE is the 1-based line number to remove.
    """
    proj = project or _infer_project()
    with T2Database(_default_db_path()) as db:
        pm_unblock(db, project=proj, line=line)
    click.echo(f"Blocker {line} removed.")


@pm.group("phase")
def phase_group() -> None:
    """Phase management commands."""


@phase_group.command("next")
@click.option("--project", default=None)
def phase_next_cmd(project: str | None) -> None:
    """Advance to the next project phase."""
    proj = project or _infer_project()
    with T2Database(_default_db_path()) as db:
        new_phase = pm_phase_next(db, project=proj)
    click.echo(f"Advanced to phase {new_phase}.")


@pm.command("search")
@click.argument("query")
@click.option("--project", default=None, help="Scope to a specific project")
def search_cmd(query: str, project: str | None) -> None:
    """FTS5 keyword search across all PM doc namespaces."""
    with T2Database(_default_db_path()) as db:
        results = pm_search(db, query=query, project=project)
    if not results:
        click.echo("No results found.")
        return
    for r in results:
        click.echo(f"[{r['id']}] {r['project']}/{r['title']}  ({r['timestamp']})")
        preview = (r.get("content") or "")[:200].replace("\n", " ")
        click.echo(f"  {preview}")



@pm.command("expire")
def expire_cmd() -> None:
    """Remove TTL-expired PM docs from T2 (all projects)."""
    with T2Database(_default_db_path()) as db:
        count = db.expire()
    click.echo(f"Expired {count} {'entry' if count == 1 else 'entries'}.")
