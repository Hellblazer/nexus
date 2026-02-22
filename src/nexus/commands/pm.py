# SPDX-License-Identifier: AGPL-3.0-or-later
"""nx pm command group — project management infrastructure."""
from pathlib import Path

import click

from nexus.config import load_config
from nexus.db.t2 import T2Database
from nexus.pm import (
    pm_archive,
    pm_block,
    pm_init,
    pm_phase_next,
    pm_reference,
    pm_restore,
    pm_resume,
    pm_search,
    pm_status,
    pm_unblock,
)


def _default_db_path() -> Path:
    return Path.home() / ".config" / "nexus" / "memory.db"


def _infer_project() -> str:
    """Infer the project name from the current git repo name, or fallback."""
    import subprocess
    try:
        result = subprocess.run(
            ["git", "rev-parse", "--show-toplevel"],
            capture_output=True, text=True, check=True,
        )
        return Path(result.stdout.strip()).name
    except Exception:
        return Path.cwd().name


@click.group()
def pm() -> None:
    """T2-backed project management infrastructure (nx pm)."""


@pm.command("init")
@click.option("--project", default=None, help="Project name (defaults to repo name)")
def init_cmd(project: str | None) -> None:
    """Initialise PM docs for the current project in T2."""
    proj = project or _infer_project()
    db = T2Database(_default_db_path())
    pm_init(db, project=proj)
    click.echo(f"Initialised PM for project '{proj}' (5 standard docs created).")


@pm.command("resume")
@click.option("--project", default=None, help="Project name (defaults to repo name)")
def resume_cmd(project: str | None) -> None:
    """Print CONTINUATION.md content for session injection (capped at 2000 chars)."""
    proj = project or _infer_project()
    db = T2Database(_default_db_path())
    content = pm_resume(db, project=proj)
    if content is None:
        click.echo(f"No PM project found for '{proj}'. Run `nx pm init` first.", err=True)
        raise SystemExit(1)
    click.echo(content)


@pm.command("status")
@click.option("--project", default=None, help="Project name (defaults to repo name)")
def status_cmd(project: str | None) -> None:
    """Show current phase, last-updated agent, and open blockers."""
    proj = project or _infer_project()
    db = T2Database(_default_db_path())
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
    """Append a blocker bullet to BLOCKERS.md."""
    proj = project or _infer_project()
    db = T2Database(_default_db_path())
    pm_block(db, project=proj, blocker=blocker)
    click.echo(f"Blocker added: {blocker}")


@pm.command("unblock")
@click.argument("line", type=int)
@click.option("--project", default=None)
def unblock_cmd(line: int, project: str | None) -> None:
    """Remove blocker at 1-based LINE number."""
    proj = project or _infer_project()
    db = T2Database(_default_db_path())
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
    db = T2Database(_default_db_path())
    new_phase = pm_phase_next(db, project=proj)
    click.echo(f"Advanced to phase {new_phase}.")


@pm.command("search")
@click.argument("query")
@click.option("--project", default=None, help="Scope to a specific project")
def search_cmd(query: str, project: str | None) -> None:
    """FTS5 keyword search across all PM doc namespaces."""
    db = T2Database(_default_db_path())
    results = pm_search(db, query=query, project=project)
    if not results:
        click.echo("No results found.")
        return
    for r in results:
        click.echo(f"[{r['id']}] {r['project']}/{r['title']}  ({r['timestamp']})")
        preview = (r.get("content") or "")[:200].replace("\n", " ")
        click.echo(f"  {preview}")


@pm.command("archive")
@click.option("--project", default=None)
@click.option(
    "--status",
    "archive_status",
    type=click.Choice(["completed", "paused", "cancelled"]),
    default="completed",
    show_default=True,
)
def archive_cmd(project: str | None, archive_status: str) -> None:
    """Synthesize PM docs → T3 + start T2 decay."""
    proj = project or _infer_project()
    config = load_config()
    ttl = config["pm"]["archiveTtl"]
    db = T2Database(_default_db_path())
    try:
        pm_archive(db, project=proj, status=archive_status, archive_ttl=ttl)
        click.echo(f"Archived project '{proj}' (status={archive_status}).")
    except RuntimeError as exc:
        click.echo(f"Archive failed: {exc}", err=True)
        raise SystemExit(1)


@pm.command("close")
@click.option("--project", default=None)
def close_cmd(project: str | None) -> None:
    """Archive and mark the project as completed (alias for archive --status completed)."""
    proj = project or _infer_project()
    config = load_config()
    ttl = config["pm"]["archiveTtl"]
    db = T2Database(_default_db_path())
    try:
        pm_archive(db, project=proj, status="completed", archive_ttl=ttl)
        click.echo(f"Closed project '{proj}'.")
    except RuntimeError as exc:
        click.echo(f"Close failed: {exc}", err=True)
        raise SystemExit(1)


@pm.command("restore")
@click.argument("project")
def restore_cmd(project: str) -> None:
    """Restore an archived project from T2 (within the decay window)."""
    db = T2Database(_default_db_path())
    try:
        pm_restore(db, project=project)
        click.echo(f"Restored project '{project}'.")
    except RuntimeError as exc:
        click.echo(str(exc), err=True)
        raise SystemExit(1)


@pm.command("reference")
@click.argument("query", required=False)
def reference_cmd(query: str | None) -> None:
    """Semantic search across all archived project syntheses in T3."""
    if query is None:
        query = click.prompt("Query")
    db = T2Database(_default_db_path())
    results = pm_reference(db, query=query)
    if not results:
        click.echo("No archived syntheses found.")
        return
    for r in results:
        proj = r.get("project", "?")
        status = r.get("status", "?")
        archived_at = r.get("archived_at", "?")
        click.echo(f"[{proj}] status={status} archived={archived_at}")
        preview = (r.get("content") or "")[:300].replace("\n", " ")
        click.echo(f"  {preview}")


@pm.command("expire")
@click.option("--project", default=None)
def expire_cmd(project: str | None) -> None:
    """Remove TTL-expired PM docs from T2."""
    db = T2Database(_default_db_path())
    count = db.expire()
    click.echo(f"Expired {count} {'entry' if count == 1 else 'entries'}.")
