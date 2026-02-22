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
@click.option(
    "--frecency-only",
    is_flag=True,
    default=False,
    help="Update frecency scores only; skip re-embedding (faster, for re-ranking refresh).",
)
def index_code_cmd(path: Path, frecency_only: bool) -> None:
    """Register and immediately index a code repository at PATH."""
    from nexus.indexer import index_repository

    reg = _registry()
    path = path.resolve()
    if reg.get(path) is None:
        reg.add(path)
        click.echo(f"Registered {path}.")

    click.echo(f"{'Updating frecency scores' if frecency_only else 'Indexing'} {path}…")
    index_repository(path, reg, frecency_only=frecency_only)
    click.echo("Done.")


@index.command("pdf")
@click.argument("path", type=click.Path(exists=True, dir_okay=False, path_type=Path))
@click.option("--corpus", default="default", show_default=True, help="Corpus name for docs__ collection.")
def index_pdf_cmd(path: Path, corpus: str) -> None:
    """Extract and index a PDF document into T3 docs__CORPUS."""
    from nexus.doc_indexer import index_pdf

    path = path.resolve()
    click.echo(f"Indexing {path}…")
    n = index_pdf(path, corpus=corpus)
    click.echo(f"Indexed {n} chunk(s).")


@index.command("md")
@click.argument("path", type=click.Path(exists=True, dir_okay=False, path_type=Path))
@click.option("--corpus", default="default", show_default=True, help="Corpus name for docs__ collection.")
def index_md_cmd(path: Path, corpus: str) -> None:
    """Extract and index a Markdown file into T3 docs__CORPUS."""
    from nexus.doc_indexer import index_markdown

    path = path.resolve()
    click.echo(f"Indexing {path}…")
    n = index_markdown(path, corpus=corpus)
    click.echo(f"Indexed {n} chunk(s).")
