# SPDX-License-Identifier: AGPL-3.0-or-later
"""nx index — code repository indexing commands."""
from pathlib import Path

import click

from nexus.registry import RepoRegistry


def _registry_path() -> Path:
    return Path.home() / ".config" / "nexus" / "repos.json"


def _registry() -> RepoRegistry:
    return RepoRegistry(_registry_path())


@click.group()
def index() -> None:
    """Index code repositories for semantic search."""


@index.command("code")
@click.argument("path", type=click.Path(exists=True, file_okay=False, path_type=Path))
def index_code_cmd(path: Path) -> None:
    """Register and immediately index a code repository at PATH."""
    from nexus.indexer import index_repository

    reg = _registry()
    path = path.resolve()
    if reg.get(path) is None:
        reg.add(path)
        click.echo(f"Registered {path}.")

    click.echo(f"Indexing {path}…")
    index_repository(path, reg)
    click.echo("Done.")
