# SPDX-License-Identifier: AGPL-3.0-or-later
"""nx index — code repository indexing commands."""
import subprocess
import sys
import threading
import time
from collections.abc import Callable
from pathlib import Path

import click
import structlog
from tqdm import tqdm

from nexus.registry import RepoRegistry

_log = structlog.get_logger()


def _registry_path() -> Path:
    from nexus.config import nexus_config_dir

    return nexus_config_dir() / "repos.json"


def _registry() -> RepoRegistry:
    return RepoRegistry(_registry_path())


@click.group()
def index() -> None:
    """Index repositories, PDFs, and Markdown into T3 collections."""


def _discover_taxonomy(collection_name, taxonomy, chroma_client, *, force=False):
    """Wrapper for discover_for_collection — importable for patching in tests."""
    from nexus.commands.taxonomy_cmd import discover_for_collection
    return discover_for_collection(
        collection_name, taxonomy, chroma_client, force=force,
    )


# ── ETA ticker (nexus-vatx Gap 3) ────────────────────────────────────────────


def _format_eta(n: int, total: int, chunks: int, elapsed_s: float) -> str:
    """Return the periodic `[eta] …` line for an in-progress indexing run.

    Pure for testability: given per-run counters and wall-clock elapsed,
    emit the exact stderr line the ETA ticker prints every interval. Falls
    back to ``pending`` for the remaining estimate when ``n==0`` so the
    first tick fired before any files complete still emits something useful.
    """
    avg = elapsed_s / n if n else 0.0
    remaining_files = max(0, total - n)
    if n == 0:
        eta = "pending"
    else:
        eta_seconds = remaining_files * avg
        eta_min = max(1, round(eta_seconds / 60))
        eta = f"~{eta_min} min remaining"
    avg_str = f"{avg:.1f}s/file avg" if n else "no samples yet"
    return (
        f"[eta] {n}/{total} files · {chunks:,} chunks · "
        f"{avg_str} · {eta}"
    )


class _ETATicker:
    """Background 60 s ticker that emits `[eta] …` to *emit* regardless of TTY.

    Exists so operators running `nx index` with stdout redirected to a file
    (CI, `nohup`, `tail -f`) keep seeing pace. Tqdm's own progress bar is
    TTY-gated and falls silent in those contexts (nexus-vatx Gap 3).

    Lifecycle: construct → :meth:`start` when the total file count is
    known → :meth:`record` on each completed file → :meth:`stop` in a
    ``finally`` block. Missing a ``stop`` leaks a daemon thread until
    process exit, which is graceful enough not to wedge a CLI run.
    """

    def __init__(
        self,
        interval: float = 60.0,
        emit: Callable[[str], None] | None = None,
    ) -> None:
        self._interval = interval
        self._emit = emit or (lambda _msg: None)
        self._done = threading.Event()
        self._thread: threading.Thread | None = None
        self._lock = threading.Lock()
        self._n = 0
        self._total = 0
        self._chunks = 0
        self._start_mono = 0.0

    def start(self, total: int) -> None:
        with self._lock:
            self._total = total
            self._start_mono = time.monotonic()
        self._done.clear()
        self._thread = threading.Thread(
            target=self._loop, name="nx-eta-ticker", daemon=True,
        )
        self._thread.start()

    def record(self, chunks: int) -> None:
        with self._lock:
            self._n += 1
            self._chunks += chunks

    def stop(self) -> None:
        self._done.set()
        thread = self._thread
        self._thread = None
        if thread is not None and thread.is_alive():
            thread.join(timeout=2.0)

    def _loop(self) -> None:
        # ``Event.wait`` returns True on set (stop requested) so a normal
        # completion breaks the loop without a stray final emission.
        while not self._done.wait(self._interval):
            self._tick()

    def _tick(self) -> None:
        with self._lock:
            n, total, chunks = self._n, self._total, self._chunks
            elapsed = time.monotonic() - self._start_mono
        if total <= 0:
            return
        self._emit(_format_eta(n, total, chunks, elapsed))


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
@click.option(
    "--force-stale",
    is_flag=True,
    default=False,
    help="Re-index only if collection pipeline version is outdated (smart force).",
)
@click.option(
    "--on-locked",
    type=click.Choice(["skip", "wait"]),
    default="wait",
    show_default=True,
    help="Behaviour when another process holds the repo lock: skip exits immediately, wait blocks.",
)
@click.option("--no-taxonomy", is_flag=True, default=False,
              help="Skip automatic topic discovery after indexing.")
def index_repo_cmd(path: Path, frecency_only: bool, force: bool, monitor: bool, force_stale: bool, on_locked: str, no_taxonomy: bool) -> None:
    """Register and immediately index a code repository at PATH.

    Classifies files by extension: code files get voyage-code-3 embeddings (code__),
    prose and PDFs get voyage-context-3 embeddings (docs__), RDR documents are
    auto-discovered and indexed into rdr__.
    """
    from nexus.indexer import index_repository

    if force and frecency_only:
        raise click.UsageError("--force and --frecency-only are mutually exclusive.")
    if force_stale and force:
        raise click.UsageError("--force-stale and --force are mutually exclusive.")
    if force_stale and frecency_only:
        raise click.UsageError("--force-stale and --frecency-only are mutually exclusive.")

    reg = _registry()
    path = path.resolve()
    if reg.get(path) is None:
        reg.add(path)
        click.echo(f"Registered {path}.")

    if force:
        label = "Force-indexing"
    elif force_stale:
        label = "Force-indexing stale"
    elif frecency_only:
        label = "Updating frecency scores"
    else:
        label = "Indexing"
    click.echo(f"{label} {path}…")

    # nexus-vatx Gap 4: zero the retry accumulators so the end-of-run
    # summary reflects only this run's backoffs.
    from nexus.retry import get_retry_stats, reset_retry_stats
    reset_retry_stats()

    bar: tqdm | None = None
    n = 0
    total = 0
    eta_ticker = _ETATicker(emit=lambda msg: click.echo(f"  {msg}", err=True))

    def on_start(count: int) -> None:
        nonlocal bar, total
        total = count
        bar = tqdm(total=count, disable=None, desc=path.name, unit="file")
        # nexus-vatx Gap 3: kick off the stderr ETA ticker once the total is
        # known. Runs every 60 s regardless of TTY so background / CI runs
        # see pace even when tqdm suppresses itself.
        eta_ticker.start(count)

    def on_file(fpath: Path, chunks: int, elapsed: float) -> None:
        nonlocal n
        n += 1
        eta_ticker.record(chunks)
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

    def on_phase(msg: str) -> None:
        # nexus-vatx Gap 2: surface post-processing phases so the operator
        # knows the indexer is still busy after the per-file bar finishes.
        # Stderr so the line is visible even when stdout is redirected.
        click.echo(f"  [post] {msg}", err=True)

    try:
        stats = index_repository(path, reg, frecency_only=frecency_only, force=force,
                                 force_stale=force_stale,
                                 on_locked=on_locked, on_start=on_start, on_file=on_file,
                                 on_phase=on_phase)
    finally:
        eta_ticker.stop()
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
    # Auto-discover taxonomy topics (RDR-070, nexus-0bg)
    if not frecency_only and not no_taxonomy and stats:
        try:
            from fnmatch import fnmatch

            from nexus.config import is_local_mode, load_config as _load_cfg
            from nexus.db import make_t3
            from nexus.db.t2 import T2Database
            from nexus.commands._helpers import default_db_path

            t3 = make_t3()
            info = reg.get(path) or {}
            cfg = _load_cfg()
            exclude_patterns = (
                cfg.get("taxonomy", {}).get("local_exclude_collections", [])
                if is_local_mode() else []
            )
            collections = []
            for key in ("collection", "docs_collection"):
                col = info.get(key)
                if col and not any(fnmatch(col, pat) for pat in exclude_patterns):
                    collections.append(col)

            total_topics = 0
            with T2Database(default_db_path()) as db:
                for col_name in collections:
                    try:
                        n = _discover_taxonomy(col_name, db.taxonomy, t3._client)
                        total_topics += n
                    except Exception:
                        _log.debug("taxonomy_discover_failed", collection=col_name, exc_info=True)
            if total_topics:
                click.echo(
                    f"  Taxonomy: {total_topics} topics across {len(collections)} collections."
                )
                # Auto-label with Claude if available and enabled
                auto_label = cfg.get("taxonomy", {}).get("auto_label", True)
                if auto_label:
                    try:
                        from nexus.commands.taxonomy_cmd import _claude_available, relabel_topics
                        if _claude_available():
                            labeled = 0
                            for col_name in collections:
                                labeled += relabel_topics(
                                    db.taxonomy, collection=col_name, only_pending=True,
                                )
                            if labeled:
                                click.echo(f"  Labels:   {labeled} topics labeled by Claude haiku.")
                    except Exception:
                        _log.debug("taxonomy_label_failed", exc_info=True)

                # Count remaining unreviewed
                unreviewed = len(db.taxonomy.get_unreviewed_topics())
                if unreviewed:
                    click.echo(
                        f"  Review:   {unreviewed} topics pending. "
                        f"Run `nx taxonomy review` to curate."
                    )
                # Cross-collection projection pass (RDR-075 SC-7)
                try:
                    proj_total = 0
                    for col_name in collections:
                        others = [c for c in collections if c != col_name]
                        if others:
                            result = db.taxonomy.project_against(
                                col_name, others, t3._client, threshold=0.85,
                            )
                            assignments = result.get("chunk_assignments", [])
                            if assignments:
                                from nexus.commands.taxonomy_cmd import _persist_assignments
                                _persist_assignments(db.taxonomy, assignments, quiet=True)
                                proj_total += len(assignments)
                    if proj_total:
                        click.echo(f"  Project:  {proj_total} cross-collection assignments.")
                except Exception:
                    _log.debug("taxonomy_projection_failed", exc_info=True)

                # Co-occurrence topic links from projections (RDR-075 SC-5)
                try:
                    cooc = db.taxonomy.generate_cooccurrence_links()
                    if cooc:
                        _log.info("cooccurrence_links_generated", count=cooc)
                except Exception:
                    _log.debug("cooccurrence_links_failed", exc_info=True)

                # Auto-populate topic links if catalog available
                try:
                    from nexus.commands.taxonomy_cmd import _try_load_catalog, compute_topic_links
                    cat = _try_load_catalog()
                    if cat:
                        for col_name in collections:
                            compute_topic_links(
                                db.taxonomy, cat, collection=col_name, persist=True,
                            )
                except Exception:
                    pass  # Non-fatal
                # Refresh L1 context cache
                try:
                    from nexus.context import generate_context_l1
                    generate_context_l1(db.taxonomy, repo_path=path)
                except Exception:
                    pass  # Non-fatal
        except Exception:
            _log.debug("taxonomy_discover_failed", exc_info=True)

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
        except Exception as exc:
            _log.debug("hook_detection_failed", error=str(exc))  # Don't let hook detection break indexing

    # nexus-vatx Gap 4: emit retry-time summary when any transient-error
    # backoff fired. Silent when zero so normal runs stay tidy.
    retry_stats = get_retry_stats()
    if retry_stats["total_count"]:
        parts = []
        if retry_stats["voyage_count"]:
            parts.append(
                f"voyage {retry_stats['voyage_seconds']:.1f}s over "
                f"{retry_stats['voyage_count']} retries"
            )
        if retry_stats["chroma_count"]:
            parts.append(
                f"chroma {retry_stats['chroma_seconds']:.1f}s over "
                f"{retry_stats['chroma_count']} retries"
            )
        click.echo(
            f"  Transient-error backoff: {retry_stats['total_seconds']:.1f}s total "
            f"({', '.join(parts)})",
            err=True,
        )
    click.echo("Done.")


@index.command("pdf")
@click.argument("path", type=click.Path(exists=True, path_type=Path), required=False, default=None)
@click.option("--dir", "dir_path", type=click.Path(exists=True, file_okay=False, path_type=Path),
              default=None, help="Index all PDFs in a directory.")
@click.option("--corpus", default="default", show_default=True, help="Corpus name for docs__ collection.")
@click.option(
    "--collection",
    default=None,
    help=(
        "T3 collection name. Bare names (e.g. 'knowledge') are auto-normalized "
        "to knowledge__<name>; qualified names (e.g. knowledge__delos) pass through. "
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
@click.option("--enrich", is_flag=True, default=False,
              help="Query Semantic Scholar for bibliographic metadata (year, venue, authors, citations). "
                   "Off by default. Use 'nx enrich <collection>' for bulk backfill.")
@click.option(
    "--extractor",
    type=click.Choice(["auto", "docling", "mineru"]),
    default=None,
    help=(
        "PDF extraction backend (default: from .nexus.yml pdf.extractor, or 'auto'). "
        "'auto' detects formulas via Docling and switches to MinerU when found. "
        "'docling' forces Docling. 'mineru' forces MinerU "
        "(requires: uv pip install 'conexus[mineru]')."
    ),
)
@click.option(
    "--streaming",
    type=click.Choice(["auto", "always", "never"]),
    default="auto",
    help="Streaming pipeline mode: auto (default, all PDFs), always, never.",
)
def index_pdf_cmd(path: Path | None, dir_path: Path | None, corpus: str, collection: str | None, dry_run: bool, force: bool, monitor: bool, enrich: bool, extractor: str | None, streaming: str) -> None:
    """Extract and index a PDF document into T3 docs__CORPUS (or --collection)."""
    import time as _time

    import structlog

    _log = structlog.get_logger(__name__)

    from nexus.config import get_pdf_extractor
    from nexus.corpus import t3_collection_name
    from nexus.doc_indexer import index_pdf

    if path is not None and dir_path is not None:
        raise click.UsageError("PATH and --dir are mutually exclusive.")
    if path is None and dir_path is None:
        raise click.UsageError("Provide either PATH or --dir.")

    if extractor is None:
        extractor = get_pdf_extractor()

    if force and dry_run:
        raise click.UsageError("--force and --dry-run are mutually exclusive.")
    if dry_run and dir_path is not None:
        raise click.UsageError("--dry-run is not supported with --dir.")

    # ── Batch mode (--dir) ──────────────────────────────────────────────
    if dir_path is not None:
        from nexus.indexer_utils import (
            find_repo_root,
            is_gitignored,
            load_ignore_patterns,
            should_ignore,
        )

        dir_path = dir_path.resolve()
        repo_root = find_repo_root(dir_path)

        # Collect PDFs and filter: resolve paths, respect git + .nexus.yml
        ignore_patterns = load_ignore_patterns(repo_root)
        raw_pdfs = sorted(
            p.resolve() for p in dir_path.iterdir()
            if p.is_file() and p.suffix.lower() == ".pdf"
        )
        pdfs: list[Path] = []
        for p in raw_pdfs:
            # Skip hidden files
            if p.name.startswith("."):
                continue
            # Apply .nexus.yml ignore patterns (relative to repo or dir)
            rel = p.relative_to(repo_root) if repo_root else p.relative_to(dir_path)
            if should_ignore(rel, ignore_patterns):
                _log.debug("skipping_ignored_pdf", path=str(p), pattern="ignore_patterns")
                continue
            # Respect .gitignore when inside a repository
            if repo_root and is_gitignored(p, repo_root):
                _log.debug("skipping_gitignored_pdf", path=str(p))
                continue
            pdfs.append(p)

        if not pdfs:
            click.echo(f"No PDF files found in {dir_path}")
            return

        skipped = len(raw_pdfs) - len(pdfs)
        if skipped:
            click.echo(f"Filtered {skipped} PDF(s) via gitignore/.nexus.yml patterns")

        if collection is not None:
            collection = t3_collection_name(collection)

        total = len(pdfs)
        total_chunks = 0
        failures: list[tuple[Path, str]] = []

        # Check if MinerU server is available for batch performance
        if extractor in ("auto", "mineru"):
            from nexus.pdf_extractor import PDFExtractor
            _extractor = PDFExtractor()
            server_up = _extractor._mineru_server_available()
            if server_up:
                click.echo(f"MinerU server available — using server-backed extraction")
            else:
                click.echo(
                    "MinerU server not running. Batch will use subprocess mode "
                    "(slower). Start with: nx mineru start"
                )

        batch_start = _time.monotonic()

        for i, pdf in enumerate(pdfs, 1):
            click.echo(f"[{i}/{total}] {pdf.name}…", nl=False)
            t0 = _time.monotonic()
            try:
                n = index_pdf(
                    pdf, corpus=corpus, collection_name=collection,
                    force=force, enrich=enrich, extractor=extractor,
                    streaming=streaming,
                )
                elapsed = _time.monotonic() - t0
                total_chunks += n
                click.echo(f" — {n} chunks, {elapsed:.1f}s")
            except Exception as exc:
                elapsed = _time.monotonic() - t0
                failures.append((pdf, str(exc)))
                _log.warning("batch_index_failed", path=str(pdf), error=str(exc))
                click.echo(f" — FAILED ({elapsed:.1f}s): {exc}")

        batch_elapsed = _time.monotonic() - batch_start
        click.echo(
            f"\nSummary: {total} PDFs, {total_chunks} chunks, "
            f"{batch_elapsed:.1f}s total"
        )
        if failures:
            click.echo(f"  {len(failures)} failure(s):")
            for fp, err in failures:
                click.echo(f"    {fp.name}: {err}")
        return

    # Normalize --collection through t3_collection_name() so bare names like
    # "knowledge" become "knowledge__knowledge", matching search conventions.
    # Without this, chunks end up in unsearchable bare collections.
    if collection is not None:
        collection = t3_collection_name(collection)

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
        try:
            n = index_pdf(path, corpus=corpus, t3=local_t3, collection_name=collection, embed_fn=_local_embed, enrich=enrich, extractor=extractor, streaming=streaming)
        except ImportError as e:
            raise click.ClickException(str(e)) from e

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
        chunk_bar = tqdm(total=0, desc="Embedding", unit="chunk", disable=None)

        def on_chunk_progress(current: int, total: int) -> None:
            chunk_bar.total = total
            chunk_bar.n = current
            chunk_bar.refresh()

        try:
            meta = index_pdf(path, corpus=corpus, collection_name=collection, force=force,
                             return_metadata=True, on_progress=on_chunk_progress, enrich=enrich, extractor=extractor, streaming=streaming)
        except ImportError as e:
            raise click.ClickException(str(e)) from e
        chunk_bar.close()
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
        try:
            n = index_pdf(path, corpus=corpus, collection_name=collection, force=force, enrich=enrich, extractor=extractor, streaming=streaming)
        except ImportError as e:
            raise click.ClickException(str(e)) from e
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
        chunk_bar = tqdm(total=0, desc="Embedding", unit="chunk", disable=None)

        def on_chunk_progress(current: int, total: int) -> None:
            chunk_bar.total = total
            chunk_bar.n = current
            chunk_bar.refresh()

        meta = index_markdown(path, corpus=corpus, force=force, return_metadata=True,
                              on_progress=on_chunk_progress)
        chunk_bar.close()
        n = meta["chunks"]  # type: ignore[index]
        sections = meta.get("sections", 0)  # type: ignore[union-attr]
        click.echo(f"\n  Chunks: {n}  Sections: {sections}")
    else:
        n = index_markdown(path, corpus=corpus, force=force)
    result_label = "Force re-indexed" if force else "Indexed"
    click.echo(f"{result_label} {n} chunk(s).")


_RDR_EXCLUDES = {"README.md", "TEMPLATE.md"}


@index.command("rdr")
@click.argument("path", type=click.Path(exists=True, file_okay=True, dir_okay=True, path_type=Path), default=".")
@click.option(
    "--force",
    is_flag=True,
    default=False,
    help="Force re-indexing, bypassing staleness check.",
)
@click.option("--monitor", is_flag=True, default=False,
              help="Print per-file progress lines. Auto-enabled when stdout is not a TTY.")
def index_rdr_cmd(path: Path, force: bool, monitor: bool) -> None:
    """Index RDR documents into T3 rdr__REPO-HASH8.

    PATH is either a repo root (glob all docs/rdr/*.md, excluding README/TEMPLATE)
    or a single `.md` file (index just that file — the preferred form when only
    one RDR changed, e.g. at rdr-close time).
    """
    from nexus.doc_indexer import batch_index_markdowns
    from nexus.registry import _repo_identity, _rdr_collection_name

    path = path.resolve()

    if path.is_file():
        # Single-file scoping: infer repo root from git so collection naming
        # stays consistent with directory-mode invocations.
        if path.suffix.lower() != ".md":
            click.echo(f"Not a markdown file: {path.name}")
            return
        try:
            repo_root = Path(subprocess.check_output(
                ["git", "rev-parse", "--show-toplevel"],
                cwd=path.parent, text=True, stderr=subprocess.DEVNULL,
            ).strip()).resolve()
        except Exception:
            # Fallback: assume conventional docs/rdr/<file>.md layout.
            if path.parent.name == "rdr" and path.parent.parent.name == "docs":
                repo_root = path.parent.parent.parent
            else:
                click.echo(f"Cannot resolve repo root from {path} — pass a repo directory instead.")
                return
        rdr_files = [path]
    else:
        repo_root = path
        rdr_dir = repo_root / "docs" / "rdr"
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

    basename, _ = _repo_identity(repo_root)
    collection = _rdr_collection_name(repo_root)
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

    # Honor NX_LOCAL by passing the local embed_fn — otherwise the RDR
    # collection gets created with Voyage 1024-dim expectations and fails
    # at query time with the local MiniLM 384-dim embedder.
    #
    # The doc_indexer embed_fn contract is `(texts, model) → (embeddings, model)`.
    # LocalEmbeddingFunction's __call__ is a ChromaDB EF — `(input) → embeddings`.
    # We wrap it to match the indexer contract.
    from nexus.config import is_local_mode
    _embed_fn = None
    if is_local_mode():
        from nexus.db.local_ef import LocalEmbeddingFunction
        _local_ef = LocalEmbeddingFunction()

        def _embed_fn(texts: list[str], model: str) -> tuple[list[list[float]], str]:
            # Force Python floats — ChromaDB rejects np.float32 in the
            # embedding list with "Expected embeddings to be a list of
            # floats or ints, got [[np.float32(...)..."
            return [[float(x) for x in v] for v in _local_ef(texts)], model

    results = batch_index_markdowns(rdr_files, corpus=basename, collection_name=collection,
                                    content_type="rdr", force=force, on_file=on_file,
                                    base_path=repo_root, embed_fn=_embed_fn)
    bar.close()
    indexed = sum(1 for s in results.values() if s == "indexed")
    result_label = "Force re-indexed" if force else "Indexed"
    click.echo(f"{result_label} {indexed} of {len(rdr_files)} RDR document(s).")
