# SPDX-License-Identifier: AGPL-3.0-or-later
"""nx index — code repository indexing commands."""
import sys
from pathlib import Path

import click
from tqdm import tqdm

from nexus.registry import RepoRegistry


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
    "--force",
    is_flag=True,
    default=False,
    help="Force re-indexing all files, bypassing staleness check (re-chunks and re-embeds in-place).",
)
@click.option("--monitor", is_flag=True, default=False,
              help="Print per-file progress lines. Auto-enabled when stdout is not a TTY.")
def index_repo_cmd(path: Path, frecency_only: bool, force: bool, monitor: bool) -> None:
    """Register and immediately index a code repository at PATH.

    Classifies files by extension: code files get voyage-code-3 embeddings (code__),
    prose and PDFs get voyage-context-3 embeddings (docs__), RDR documents are
    auto-discovered and indexed into rdr__.
    """
    from nexus.indexer import index_repository

    if force and frecency_only:
        raise click.UsageError("--force and --frecency-only are mutually exclusive.")

    reg = _registry()
    path = path.resolve()
    if reg.get(path) is None:
        reg.add(path)
        click.echo(f"Registered {path}.")

    label = "Force-indexing" if force else ("Updating frecency scores" if frecency_only else "Indexing")
    click.echo(f"{label} {path}…")

    bar: tqdm | None = None
    n = 0
    total = 0

    def on_start(count: int) -> None:
        nonlocal bar, total
        total = count
        bar = tqdm(total=count, disable=None, desc=path.name, unit="file")

    def on_file(fpath: Path, chunks: int, elapsed: float) -> None:
        nonlocal n
        n += 1
        if bar is not None:
            bar.update(1)
            bar.set_postfix(now=fpath.name)
        if monitor or not sys.stdout.isatty():
            lbl = f"{chunks} chunks" if chunks else "skipped"
            line = f"  [{n}/{total}] {fpath.name} \u2014 {lbl}  ({elapsed:.1f}s)"
            if bar is not None and sys.stdout.isatty():
                tqdm.write(line)
            else:
                click.echo(line)

    stats = index_repository(path, reg, frecency_only=frecency_only, force=force,
                             on_start=on_start, on_file=on_file)
    if bar:
        bar.close()
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
    if not frecency_only:
        try:
            from nexus.commands.hooks import SENTINEL_BEGIN, _effective_hooks_dir
            hdir = _effective_hooks_dir(path)
            hook_names = ("post-commit", "post-merge", "post-rewrite")
            any_managed = any(
                SENTINEL_BEGIN in (hdir / n).read_text()
                for n in hook_names
                if (hdir / n).exists()
            )
            if not any_managed:
                click.echo("Tip: run `nx hooks install` to auto-index this repo on every commit.")
        except Exception:
            pass  # Don't let hook detection break indexing
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
@click.option(
    "--force",
    is_flag=True,
    default=False,
    help="Force re-indexing, bypassing staleness check (re-chunks and re-embeds in-place).",
)
@click.option("--monitor", is_flag=True, default=False,
              help="Print chunking metadata after indexing. Auto-enabled when stdout is not a TTY.")
def index_pdf_cmd(path: Path, corpus: str, collection: str | None, dry_run: bool, force: bool, monitor: bool) -> None:
    """Extract and index a PDF document into T3 docs__CORPUS (or --collection)."""
    from nexus.doc_indexer import index_pdf

    if force and dry_run:
        raise click.UsageError("--force and --dry-run are mutually exclusive.")

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

    label = "Force re-indexing" if force else "Indexing"
    click.echo(f"{label} {path}…")
    if monitor or not sys.stdout.isatty():
        meta = index_pdf(path, corpus=corpus, collection_name=collection, force=force,
                         return_metadata=True)
        n = meta["chunks"]  # type: ignore[index]
        pages = meta.get("pages", [])  # type: ignore[union-attr]
        page_range = f"{pages[0]}–{pages[-1]}" if len(pages) > 1 else str(pages[0]) if pages else "?"
        title = meta.get("title", "")  # type: ignore[union-attr]
        author = meta.get("author", "")  # type: ignore[union-attr]
        parts = [f"Chunks: {n}", f"Pages: {page_range}"]
        if title:
            parts.append(f'Title: "{title}"')
        if author:
            parts.append(f'Author: "{author}"')
        click.echo(f"\n  {'  '.join(parts)}")
    else:
        n = index_pdf(path, corpus=corpus, collection_name=collection, force=force)
    result_label = "Force re-indexed" if force else "Indexed"
    click.echo(f"{result_label} {n} chunk(s).")


@index.command("md")
@click.argument("path", type=click.Path(exists=True, dir_okay=False, path_type=Path))
@click.option("--corpus", default="default", show_default=True, help="Corpus name for docs__ collection.")
@click.option(
    "--force",
    is_flag=True,
    default=False,
    help="Force re-indexing, bypassing staleness check.",
)
@click.option("--monitor", is_flag=True, default=False,
              help="Print chunking metadata after indexing. Auto-enabled when stdout is not a TTY.")
def index_md_cmd(path: Path, corpus: str, force: bool, monitor: bool) -> None:
    """Extract and index a Markdown file into T3 docs__CORPUS."""
    from nexus.doc_indexer import index_markdown

    path = path.resolve()
    label = "Force re-indexing" if force else "Indexing"
    click.echo(f"{label} {path}…")
    if monitor or not sys.stdout.isatty():
        meta = index_markdown(path, corpus=corpus, force=force, return_metadata=True)
        n = meta["chunks"]  # type: ignore[index]
        sections = meta.get("sections", 0)  # type: ignore[union-attr]
        click.echo(f"\n  Chunks: {n}  Sections: {sections}")
    else:
        n = index_markdown(path, corpus=corpus, force=force)
    result_label = "Force re-indexed" if force else "Indexed"
    click.echo(f"{result_label} {n} chunk(s).")


_RDR_EXCLUDES = {"README.md", "TEMPLATE.md"}


@index.command("rdr")
@click.argument("path", type=click.Path(exists=True, file_okay=False, path_type=Path), default=".")
@click.option(
    "--force",
    is_flag=True,
    default=False,
    help="Force re-indexing all RDR documents, bypassing staleness check.",
)
@click.option("--monitor", is_flag=True, default=False,
              help="Print per-file progress lines. Auto-enabled when stdout is not a TTY.")
def index_rdr_cmd(path: Path, force: bool, monitor: bool) -> None:
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
    label = "Force re-indexing" if force else "Indexing"
    click.echo(f"{label} {len(rdr_files)} RDR document(s) into {collection}…")

    bar = tqdm(total=len(rdr_files), disable=None, desc="RDR", unit="doc")
    n = 0

    def on_file(fpath: Path, chunks: int, elapsed: float) -> None:
        nonlocal n
        n += 1
        bar.update(1)
        bar.set_postfix(now=fpath.name)
        if monitor or not sys.stdout.isatty():
            lbl = f"{chunks} chunks" if chunks else "skipped"
            line = f"  [{n}/{len(rdr_files)}] {fpath.name} \u2014 {lbl}  ({elapsed:.1f}s)"
            if sys.stdout.isatty():
                tqdm.write(line)
            else:
                click.echo(line)

    results = batch_index_markdowns(rdr_files, corpus=basename, collection_name=collection,
                                    force=force, on_file=on_file)
    bar.close()
    indexed = sum(1 for s in results.values() if s == "indexed")
    result_label = "Force re-indexed" if force else "Indexed"
    click.echo(f"{result_label} {indexed} of {len(rdr_files)} RDR document(s).")
