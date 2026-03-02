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
    from nexus.chunker import AST_EXTENSIONS

    line_threshold = threshold * chunk_lines
    large: list[tuple[int, Path]] = []

    for path in repo.rglob("*"):
        if not path.is_file() or path.is_symlink():
            continue
        if any(part.startswith(".") for part in path.relative_to(repo).parts):
            continue
        if path.suffix.lower() not in AST_EXTENSIONS:
            continue
        try:
            line_count = len(path.read_text(encoding="utf-8", errors="ignore").splitlines())
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
    type=click.IntRange(min=1),
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
            suggest = (
                f"\nConsider: nx index repo . --chunk-size 80"
                if chunk_size is None
                else f"\nConsider reducing further with --chunk-size {max(10, effective_chunk_lines // 2)}"
            )
            msg = (
                f"Warning: {count} file{'s' if count != 1 else ''} exceed the large-file "
                f"threshold ({line_threshold:,} lines; largest: {largest_rel}, "
                f"{largest_count:,} lines). Large files produce many chunks that dominate "
                f"semantic scoring.{suggest}\n"
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
@click.option(
    "--collection",
    default=None,
    help=(
        "Fully-qualified T3 collection name (e.g. knowledge__delos). "
        "Overrides --corpus when set."
    ),
)
@click.option(
    "--dry-run",
    is_flag=True,
    default=False,
    help=(
        "Extract and embed locally using ONNX (no API keys, no cloud writes). "
        "Prints a chunk preview so you can verify extraction before indexing for real."
    ),
)
def index_pdf_cmd(path: Path, corpus: str, collection: str | None, dry_run: bool) -> None:
    """Extract and index a PDF document into T3 docs__CORPUS (or --collection)."""
    from nexus.doc_indexer import index_pdf

    path = path.resolve()

    if dry_run:
        import chromadb
        from chromadb.utils.embedding_functions import DefaultEmbeddingFunction

        from nexus.db import make_t3

        click.echo("Dry-run mode — local ONNX embeddings, no cloud writes.")
        ef = DefaultEmbeddingFunction()
        local_t3 = make_t3(_client=chromadb.EphemeralClient(), _ef_override=ef)

        def _local_embed(texts: list[str], model: str) -> tuple[list[list[float]], str]:
            return [v.tolist() for v in ef(texts)], model

        click.echo(f"Indexing {path}…")
        n = index_pdf(path, corpus=corpus, t3=local_t3, collection_name=collection, embed_fn=_local_embed)

        if n == 0:
            click.echo("No chunks produced (file may already be indexed or extraction failed).")
            return

        # Retrieve indexed chunks from the ephemeral collection for preview
        col_name = collection if collection else f"docs__{corpus}"
        col = local_t3.get_or_create_collection(col_name)
        result = col.get(include=["documents", "metadatas"])
        docs: list[str] = result.get("documents") or []
        metas: list[dict] = result.get("metadatas") or []

        # Summary line
        pages = sorted({int(m.get("page_number", 0)) for m in metas if m})
        page_range = f"{pages[0]}–{pages[-1]}" if len(pages) > 1 else str(pages[0]) if pages else "?"
        title = metas[0].get("source_title", "") if metas else ""
        author = metas[0].get("source_author", "") if metas else ""
        summary_parts = [f"Chunks: {n}", f"Pages: {page_range}"]
        if title:
            summary_parts.append(f'Title: "{title}"')
        if author:
            summary_parts.append(f'Author: "{author}"')
        click.echo(f"\n  {'  '.join(summary_parts)}\n")

        # Per-chunk preview
        for i, (doc, meta) in enumerate(zip(docs, metas), start=1):
            page = meta.get("page_number", "?") if meta else "?"
            preview = doc[:80].replace("\n", " ") if doc else ""
            ellipsis = "…" if doc and len(doc) > 80 else ""
            click.echo(f"  [{i}] p.{page}  {preview}{ellipsis}")

        click.echo("\n(no cloud write)")
        return

    click.echo(f"Indexing {path}…")
    n = index_pdf(path, corpus=corpus, collection_name=collection)
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
