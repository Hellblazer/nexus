# SPDX-License-Identifier: AGPL-3.0-or-later
from pathlib import Path

import click

from nexus.config import load_config
from nexus.corpus import resolve_corpus
from nexus.commands.store import _t3
from nexus.ripgrep_cache import search_ripgrep
from nexus.formatters import format_json, format_plain_with_context, format_vimgrep
from nexus.answer import answer_mode
from nexus.scoring import apply_hybrid_scoring, rerank_results, round_robin_interleave
from nexus.search_engine import agentic_search, fetch_mxbai_results, search_cross_corpus
from nexus.types import SearchResult


def _parse_where(where_pairs: tuple[str, ...]) -> dict | None:
    """Parse ``KEY=VALUE`` strings into a ChromaDB where dict.

    Multiple pairs are ANDed by merging into a single dict.
    Returns ``None`` when *where_pairs* is empty.
    """
    if not where_pairs:
        return None
    result: dict = {}
    for pair in where_pairs:
        if "=" not in pair:
            raise click.BadParameter(
                f"--where value {pair!r} must be in KEY=VALUE format",
                param_hint="'--where'",
            )
        key, _, value = pair.partition("=")
        result[key] = value
    return result

_CONTENT_MAX_CHARS: int = 200

# Directory where ripgrep cache files are stored (overridable in tests via monkeypatch)
_CONFIG_DIR: Path = Path.home() / ".config" / "nexus"


def _find_rg_cache_paths() -> list[Path]:
    """Return all ripgrep cache files in the nexus config directory."""
    return list(_CONFIG_DIR.glob("*.cache"))


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
@click.option("--mxbai", is_flag=True, default=False,
              help="Fan out to Mixedbread-indexed collections (read-only)")
@click.option("--agentic", is_flag=True, default=False,
              help="Multi-step Haiku query refinement before returning results")
@click.option("-a", "--answer", "answer", is_flag=True, default=False,
              envvar="NX_ANSWER",
              help="Synthesize cited answer via Haiku after retrieval")
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
              help="Exclude chunks from files larger than N chunks (filters on chunk_count)")
@click.option("-A", "lines_after", default=0, type=int, metavar="N",
              help="Show N lines of context after each result chunk")
@click.option("-C", "lines_context", default=0, type=int, metavar="N",
              help="Show N lines of context after each result chunk (alias for -A N)")
@click.option("--reverse", "-r", is_flag=True, default=False,
              help="Reverse output order (highest-scoring last)")
def search_cmd(
    query: str,
    path: str | None,
    corpus: tuple[str, ...],
    n: int,
    hybrid: bool,
    no_rerank: bool,
    mxbai: bool,
    agentic: bool,
    answer: bool,
    vimgrep: bool,
    json_out: bool,
    files_only: bool,
    no_color: bool,
    show_content: bool,
    where_pairs: tuple[str, ...],
    max_file_chunks: int | None,
    lines_after: int,
    lines_context: int,
    reverse: bool,
) -> None:
    """Semantic search across T3 knowledge collections.

    QUERY is the search query string.

    PATH (optional) scopes results to files under that directory path.
    Relative paths are resolved against the current working directory.

    --corpus may be a prefix (code, docs, knowledge) or a fully-qualified
    collection name (code__myrepo).  Repeat --corpus to search multiple corpora.
    """
    # -C N is alias for -A N
    if lines_context:
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

    if not target_collections and not mxbai:
        click.echo("no matching collections found — use: nx collection list", err=True)
        return

    config = load_config()
    reranker_model = config["embeddings"]["rerankerModel"]

    def _retrieve(q: str) -> list[SearchResult]:
        raw = search_cross_corpus(q, target_collections, n_results=n, t3=db, where=where_filter)
        if mxbai:
            stores = config.get("mxbai", {}).get("stores", [])
            num = len(target_collections) or 1
            per_k = max(5, (n // num) * 2)
            mxbai_results = fetch_mxbai_results(q, stores=stores, per_k=per_k)
            raw.extend(mxbai_results)
        if hybrid:
            for cache_path in _find_rg_cache_paths():
                rg_hits = search_ripgrep(q, cache_path, n_results=n * 2)
                raw.extend(_rg_hit_to_result(h) for h in rg_hits)
        return raw

    # Retrieval (agentic or direct)
    if agentic:
        results = agentic_search(
            initial_query=query,
            retrieve_fn=_retrieve,
            max_iterations=3,
        )
    else:
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

    # Hybrid scoring
    results = apply_hybrid_scoring(results, hybrid=hybrid)

    # Reranking
    if not no_rerank and len(set(r.collection for r in results)) > 1:
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

    # --reverse: invert final order
    if reverse:
        results = list(reversed(results))

    # Answer mode
    if answer:
        click.echo(answer_mode(query=query, results=results))
        return

    # Output format
    if json_out:
        click.echo(format_json(results))
    elif vimgrep:
        for line in format_vimgrep(results):
            click.echo(line)
    elif files_only:
        seen: set[str] = set()
        for r in results:
            file_path = r.metadata.get("source_path", "")
            if file_path and file_path not in seen:
                seen.add(file_path)
                click.echo(file_path)
    else:
        for result in results:
            for line in format_plain_with_context(
                [result], lines_after=lines_after
            ):
                click.echo(line)
            if show_content:
                text = result.content
                if len(text) > _CONTENT_MAX_CHARS:
                    text = text[:_CONTENT_MAX_CHARS] + "..."
                click.echo(f"  {text}")
