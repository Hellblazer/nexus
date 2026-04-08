# SPDX-License-Identifier: AGPL-3.0-or-later
import logging
import os
from pathlib import Path

import click
import structlog

from nexus.config import get_tuning_config, load_config
from nexus.corpus import resolve_corpus
from nexus.commands.store import _t3
from nexus.ripgrep_cache import search_ripgrep
from nexus.formatters import (
    _format_with_bat,
    _is_bat_installed,
    format_compact,
    format_json,
    format_plain_with_context,
    format_vimgrep,
)
from nexus.scoring import RG_FLOOR_SCORE, apply_hybrid_scoring, rerank_results, round_robin_interleave
from nexus.search_engine import search_cross_corpus
from nexus.types import SearchResult


from nexus.filters import parse_where as _parse_where_core


def _parse_where(where_pairs: tuple[str, ...]) -> dict | None:
    """Parse ``KEY{op}VALUE`` strings into a ChromaDB where dict.

    Wraps shared ``parse_where`` with Click-specific error handling.
    """
    try:
        return _parse_where_core(where_pairs, strict=True)
    except ValueError as e:
        raise click.BadParameter(str(e), param_hint="'--where'")

_CONTENT_MAX_CHARS: int = 200
EXACT_MATCH_BOOST: float = 0.15
RG_ONLY_PENALTY: float = 0.8  # Multiplier for rg-only results (files not in vector top-K)

# Directory where ripgrep cache files are stored (overridable in tests via monkeypatch)
_CONFIG_DIR: Path = Path.home() / ".config" / "nexus"

# Prefixes to strip when mapping collection names to cache file names
_COLLECTION_PREFIXES = ("code__", "docs__", "rdr__", "knowledge__")


def _find_rg_cache_paths(corpus: str | None = None) -> list[Path]:
    """Return ripgrep cache files, optionally filtered by corpus.

    When *corpus* is provided (e.g. ``"code__nexus-a1b2c3d4"``), strip the
    collection prefix and glob for ``{slug}.cache`` instead of ``*.cache``.
    """
    if corpus is None:
        return list(_CONFIG_DIR.glob("*.cache"))
    slug = corpus
    for prefix in _COLLECTION_PREFIXES:
        if slug.startswith(prefix):
            slug = slug[len(prefix):]
            break
    return list(_CONFIG_DIR.glob(f"{slug}.cache"))


def _rg_hit_to_result(hit: dict) -> SearchResult:
    """Convert a ripgrep hit dict to a SearchResult for hybrid scoring."""
    file_path = hit["file_path"]
    line_number = hit["line_number"]
    return SearchResult(
        id=f"rg:{file_path}:{line_number}",
        content=hit["line_content"],
        distance=0.0,
        collection="rg__cache",
        metadata={
            "file_path": file_path,
            "source_path": file_path,
            "line_start": line_number,
            "frecency_score": hit.get("frecency_score", 0.5),
            "source": "ripgrep",
        },
        hybrid_score=0.0,
    )


@click.command("search")
@click.argument("query")
@click.argument("path", required=False, default=None)
@click.option("--corpus", multiple=True, default=("knowledge", "code", "docs"),
              show_default=True, help="Corpus prefix or full collection name (repeatable)")
@click.option("--n", "-m", "--max-results", "n", default=10, show_default=True,
              help="Max results to return")
@click.option("--hybrid", is_flag=True, default=False,
              help="Merge semantic + ripgrep results for code (0.7*vector + 0.3*frecency)")
@click.option("--no-rerank", "no_rerank", is_flag=True, default=False,
              help="Disable cross-corpus reranking (use round-robin instead)")
@click.option("--vimgrep", is_flag=True, default=False,
              help="Output in path:line:col:content format")
@click.option("--json", "json_out", is_flag=True, default=False,
              help="Output as JSON array")
@click.option("--files", "files_only", is_flag=True, default=False,
              help="Output only unique file paths")
@click.option("--no-color", is_flag=True, default=False,
              help="Disable colour output")
@click.option("-c", "--content", "show_content", is_flag=True, default=False,
              help="Show matched text inline under each result (truncated at 200 chars).")
@click.option("--where", "where_pairs", multiple=True, metavar="KEY=VALUE",
              help="Filter by metadata field (repeatable; multiple flags are ANDed)")
@click.option("--max-file-chunks", "max_file_chunks", default=None, type=int, metavar="N",
              help="Exclude chunks from files larger than N chunks (code corpora only; "
                   "knowledge/docs corpora lack chunk_count and will return no results)")
@click.option("-A", "lines_after", default=0, type=int, metavar="N",
              help="Show N lines of context after each matching line")
@click.option("-B", "lines_before", default=0, type=int, metavar="N",
              help="Show N lines of context before each matching line (within-chunk)")
@click.option("-C", "lines_context", default=0, type=int, metavar="N",
              help="Show N lines before and after each match (equivalent to -B N -A N)")
@click.option("--bat", "use_bat", is_flag=True, default=False,
              help="Syntax highlight with bat (ignored with --json/--vimgrep/--files)")
@click.option("--compact", is_flag=True, default=False,
              help="One line per result: path:line:text (grep-compatible)")
@click.option("--reverse", "-r", is_flag=True, default=False,
              help="Reverse output order (highest-scoring last)")
def search_cmd(
    query: str,
    path: str | None,
    corpus: tuple[str, ...],
    n: int,
    hybrid: bool,
    no_rerank: bool,
    vimgrep: bool,
    json_out: bool,
    files_only: bool,
    no_color: bool,
    show_content: bool,
    where_pairs: tuple[str, ...],
    max_file_chunks: int | None,
    lines_after: int,
    lines_before: int,
    lines_context: int,
    use_bat: bool,
    compact: bool,
    reverse: bool,
) -> None:
    """Semantic search across T3 knowledge collections.

    QUERY is the search query string.

    PATH (optional) scopes results to files under that directory path.
    Relative paths are resolved against the current working directory.

    --corpus may be a prefix (code, docs, knowledge) or a fully-qualified
    collection name (code__myrepo).  Repeat --corpus to search multiple corpora.
    """
    # Structured output modes (--json, --vimgrep, --files, --compact) must
    # produce clean machine-parseable output.  Suppress log messages below
    # ERROR so warnings don't pollute stdout.
    if json_out or vimgrep or files_only or compact:
        logging.getLogger().setLevel(logging.ERROR)
        structlog.configure(
            wrapper_class=structlog.make_filtering_bound_logger(logging.ERROR),
        )

    # -C N = -B N -A N (grep semantics: before + after)
    if lines_context:
        lines_before = lines_context
        lines_after = lines_context

    # Build where filter: --where pairs only ($startswith is not a valid ChromaDB operator;
    # path scoping is applied Python-side after retrieval).
    resolved: str | None = str(Path(path).resolve()) if path is not None else None

    try:
        where_filter = _parse_where(where_pairs)
    except click.BadParameter as exc:
        raise click.ClickException(str(exc)) from exc

    if max_file_chunks is not None:
        size_filter: dict = {"chunk_count": {"$lte": max_file_chunks}}
        if where_filter is None:
            where_filter = size_filter
        else:
            where_filter = {"$and": [size_filter, where_filter]}

    db = _t3()
    all_collections = [c["name"] for c in db.list_collections()]

    target_collections: list[str] = []
    for c in corpus:
        matched = resolve_corpus(c, all_collections)
        if not matched:
            click.echo(f"Warning: no collections match --corpus {c!r}", err=True)
        target_collections.extend(matched)

    target_collections = list(dict.fromkeys(target_collections))

    if not target_collections:
        click.echo("no matching collections found — use: nx collection list", err=True)
        return

    config = load_config()
    reranker_model = config["embeddings"]["rerankerModel"]
    tuning = get_tuning_config()

    # Apply per-project hybrid default if --hybrid was not explicitly passed
    if not hybrid:
        hybrid = config.get("search", {}).get("hybrid_default", False)

    def _retrieve(q: str) -> list[SearchResult]:
        raw = search_cross_corpus(q, target_collections, n_results=n, t3=db, where=where_filter)
        if hybrid:
            # Scope ripgrep to matching caches when a single corpus is targeted
            rg_corpus = target_collections[0] if len(target_collections) == 1 else None
            for cache_path in _find_rg_cache_paths(corpus=rg_corpus):
                rg_hits = search_ripgrep(q, cache_path, n_results=n * 2, timeout=tuning.ripgrep_timeout)
                raw.extend(_rg_hit_to_result(h) for h in rg_hits)
        return raw

    results = _retrieve(query)

    if not results:
        click.echo("No results.")
        return

    # Python-side path scoping (ChromaDB has no $startswith operator)
    if resolved is not None:
        results = [
            r for r in results
            if r.metadata.get("file_path", "").startswith(resolved)
            or r.metadata.get("source_path", "").startswith(resolved)
        ]
        if not results:
            click.echo("No results.")
            return

    # Pre-reranker: capture rg file paths and matched line numbers while all
    # results are present. The reranker's top_k may drop rg hits.
    rg_file_paths: set[str] = set()
    rg_matched_lines: dict[str, list[int]] = {}  # file_path → [line_numbers]
    if hybrid:
        for r in results:
            if r.collection == "rg__cache":
                fp = r.metadata.get("file_path", "")
                if fp:
                    rg_file_paths.add(fp)
                    ln = r.metadata.get("line_start")
                    if ln is not None:
                        rg_matched_lines.setdefault(fp, []).append(int(ln))

    # Hybrid scoring — pass tuning weights from config (honours per-repo .nexus.yml)
    results = apply_hybrid_scoring(
        results,
        hybrid=hybrid,
        vector_weight=tuning.vector_weight,
        frecency_weight=tuning.frecency_weight,
        file_size_threshold=tuning.file_size_threshold,
    )

    # RDR-055 E2: quality boost from bibliographic metadata (no-op when unenriched)
    from nexus.scoring import apply_quality_boost
    results = apply_quality_boost(results)

    # Reranking (skipped in local mode — no Voyage AI reranker available)
    from nexus.config import is_local_mode
    if not no_rerank and not is_local_mode() and len(set(r.collection for r in results)) > 1:
        try:
            results = rerank_results(results, query=query, model=reranker_model, top_k=n)
        except Exception as exc:
            click.echo(f"Warning: reranking failed ({exc}), using raw order", err=True)
    else:
        # Group by collection and interleave round-robin for even distribution
        groups: dict[str, list[SearchResult]] = {}
        for r in results:
            groups.setdefault(r.collection, []).append(r)
        results = round_robin_interleave(list(groups.values()))[:n]

    # Post-reranker: apply exact-match boost using pre-captured rg file paths.
    # Fires unconditionally after both reranked and no-rerank paths.
    # Also attach matched line numbers for downstream context windowing (RDR-027).
    if rg_file_paths:
        for r in results:
            src = r.metadata.get("source_path", r.metadata.get("file_path", ""))
            if src in rg_file_paths:
                r.hybrid_score = min(1.0, r.hybrid_score + EXACT_MATCH_BOOST)
                if src in rg_matched_lines:
                    r.metadata["rg_matched_lines"] = rg_matched_lines[src]

    # Filter rg__cache signals from output. Promote rg-only results (files that
    # ripgrep found but vector search missed) with a penalty score.
    if hybrid:
        vector_paths = {
            r.metadata.get("source_path", r.metadata.get("file_path", ""))
            for r in results if r.collection != "rg__cache"
        }
        seen_rg_paths: set[str] = set()
        kept: list[SearchResult] = []
        for r in results:
            if r.collection != "rg__cache":
                kept.append(r)
            else:
                fp = r.metadata.get("file_path", "")
                if fp not in vector_paths and fp not in seen_rg_paths:
                    # rg-only: file not in vector results — keep first hit, penalized
                    r.hybrid_score = RG_FLOOR_SCORE * RG_ONLY_PENALTY
                    kept.append(r)
                    seen_rg_paths.add(fp)
                # else: rg hit for a file already in vector results → drop (signal only)
        results = kept if kept else results

    # --reverse: invert final order
    if reverse:
        results = list(reversed(results))

    # Output format
    if json_out:
        click.echo(format_json(results))
    elif vimgrep:
        for line in format_vimgrep(results, query=query):
            click.echo(line)
    elif files_only:
        seen: set[str] = set()
        for r in results:
            file_path = r.metadata.get("source_path", "")
            if file_path and file_path not in seen:
                seen.add(file_path)
                click.echo(file_path)
    elif compact:
        for line in format_compact(results, query=query):
            click.echo(line)
    else:
        # Check bat applicability
        use_bat_effective = (
            use_bat
            and not no_color
            and not os.environ.get("NO_COLOR")
        )
        if use_bat_effective and not _is_bat_installed():
            click.echo("Warning: bat not found; showing plain output", err=True)
            use_bat_effective = False

        if use_bat_effective:
            click.echo(_format_with_bat(results))
        else:
            for result in results:
                for line in format_plain_with_context(
                    [result],
                    lines_after=lines_after,
                    lines_before=lines_before,
                    query=query,
                ):
                    click.echo(line)
                if show_content:
                    text = result.content
                    if len(text) > _CONTENT_MAX_CHARS:
                        text = text[:_CONTENT_MAX_CHARS] + "..."
                    click.echo(f"  {text}")
