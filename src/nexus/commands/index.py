# SPDX-License-Identifier: AGPL-3.0-or-later
"""nx index — code repository indexing commands."""
from pathlib import Path

import click

from nexus.registry import RepoRegistry


def _detect_large_files(
    repo: Path,
    chunk_lines: int,
    threshold: int,
) -> list[tuple[int, Path]]:
    """Return code files whose line count exceeds *threshold* * *chunk_lines*.

    Uses a quick rglob scan limited to known code extensions.
    Returns a list of (line_count, path) sorted descending by line count.
    """
    from nexus.chunker import _AST_EXTENSIONS

    line_threshold = threshold * chunk_lines
    large: list[tuple[int, Path]] = []

    for path in repo.rglob("*"):
        if not path.is_file() or path.is_symlink():
            continue
        if any(part.startswith(".") for part in path.relative_to(repo).parts):
            continue
        if path.suffix.lower() not in _AST_EXTENSIONS:
            continue
        try:
            line_count = path.read_text(encoding="utf-8", errors="ignore").count("\n") + 1
        except OSError:
            continue
        if line_count > line_threshold:
            large.append((line_count, path))

    return sorted(large, key=lambda x: x[0], reverse=True)


def _registry_path() -> Path:
    return Path.home() / ".config" / "nexus" / "repos.json"


def _registry() -> RepoRegistry:
    return RepoRegistry(_registry_path())


@click.group()
def index() -> None:
    """Index repositories, PDFs, and Markdown into T3 collections."""


@index.command("repo")
@click.argument("path", type=click.Path(exists=True, file_okay=False, path_type=Path))
@click.option(
    "--frecency-only",
    is_flag=True,
    default=False,
    help="Update frecency scores only; skip re-embedding (faster, for re-ranking refresh).",
)
@click.option(
    "--chunk-size",
    type=int,
    default=None,
    help="Lines per chunk for code files (default: 150). Smaller values improve search "
    "precision for large files at the cost of more chunks.",
)
@click.option(
    "--no-chunk-warning",
    is_flag=True,
    default=False,
    help="Suppress the large-file warning emitted before indexing.",
)
def index_repo_cmd(
    path: Path, frecency_only: bool, chunk_size: int | None, no_chunk_warning: bool
) -> None:
    """Register and immediately index a code repository at PATH.

    Classifies files by extension: code files get voyage-code-3 embeddings (code__),
    prose and PDFs get voyage-context-3 embeddings (docs__), RDR documents are
    auto-discovered and indexed into rdr__.
    """
    from nexus.chunker import _CHUNK_LINES
    from nexus.indexer import index_repository
    from nexus.scoring import _FILE_SIZE_THRESHOLD

    reg = _registry()
    path = path.resolve()
    if reg.get(path) is None:
        reg.add(path)
        click.echo(f"Registered {path}.")

    # Warn if any code files exceed the large-file threshold
    if not frecency_only and not no_chunk_warning:
        effective_chunk_lines = chunk_size if chunk_size is not None else _CHUNK_LINES
        large = _detect_large_files(path, effective_chunk_lines, _FILE_SIZE_THRESHOLD)
        if large:
            line_threshold = _FILE_SIZE_THRESHOLD * effective_chunk_lines
            count = len(large)
            largest_count, largest_path = large[0]
            largest_rel = largest_path.relative_to(path)
            msg = (
                f"Warning: {count} file{'s' if count != 1 else ''} exceed the large-file "
                f"threshold ({line_threshold:,} lines; largest: {largest_rel}, "
                f"{largest_count:,} lines). Default chunk size may reduce search precision "
                f"— large files produce many chunks that dominate semantic scoring.\n"
                f"Consider: nx index repo . --chunk-size 80\n"
                f"Run with --no-chunk-warning to suppress this message."
            )
            click.echo(msg, err=True)

    click.echo(f"{'Updating frecency scores' if frecency_only else 'Indexing'} {path}…")
    stats = index_repository(path, reg, frecency_only=frecency_only, chunk_lines=chunk_size)
    if not frecency_only and stats:
        rdr_indexed = stats.get("rdr_indexed", 0)
        rdr_current = stats.get("rdr_current", 0)
        rdr_failed = stats.get("rdr_failed", 0)
        total_rdr = rdr_indexed + rdr_current + rdr_failed
        if total_rdr:
            parts = [f"{rdr_indexed} indexed"]
            if rdr_current:
                parts.append(f"{rdr_current} up to date")
            if rdr_failed:
                parts.append(f"{rdr_failed} failed")
            click.echo(f"  RDR documents: {', '.join(parts)} (collection rdr__)")
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


_RDR_EXCLUDES = {"README.md", "TEMPLATE.md"}


@index.command("rdr")
@click.argument("path", type=click.Path(exists=True, file_okay=False, path_type=Path), default=".")
def index_rdr_cmd(path: Path) -> None:
    """Discover and index RDR documents in docs/rdr/ into T3 rdr__REPO-HASH8."""
    from nexus.doc_indexer import batch_index_markdowns
    from nexus.registry import _repo_identity, _rdr_collection_name

    path = path.resolve()
    rdr_dir = path / "docs" / "rdr"

    if not rdr_dir.is_dir():
        click.echo("No docs/rdr/ directory found")
        return

    # Glob only top-level .md files, excluding README.md and TEMPLATE.md
    rdr_files = sorted(
        p for p in rdr_dir.glob("*.md")
        if p.is_file() and p.name not in _RDR_EXCLUDES
    )

    if not rdr_files:
        click.echo("0 RDR documents found.")
        return

    basename, _ = _repo_identity(path)
    collection = _rdr_collection_name(path)
    click.echo(f"Indexing {len(rdr_files)} RDR document(s) into {collection}…")
    results = batch_index_markdowns(rdr_files, corpus=basename, collection_name=collection)
    indexed = sum(1 for s in results.values() if s == "indexed")
    click.echo(f"Indexed {indexed} of {len(rdr_files)} RDR document(s).")
