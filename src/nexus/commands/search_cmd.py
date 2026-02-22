# SPDX-License-Identifier: AGPL-3.0-or-later
import os
from pathlib import Path

import click

from nexus.config import load_config
from nexus.corpus import resolve_corpus
from nexus.commands.store import _t3
from nexus.search_engine import (
    SearchResult,
    answer_mode,
    apply_hybrid_scoring,
    agentic_search,
    fetch_mxbai_results,
    format_json,
    format_plain,
    format_vimgrep,
    rerank_results,
    round_robin_interleave,
    search_cross_corpus,
)

_CONTENT_MAX_CHARS: int = 200


@click.command("search")
@click.argument("query")
@click.argument("path", required=False, default=None)
@click.option("--corpus", "-C", multiple=True, default=("knowledge", "code", "docs"),
              show_default=True, help="Corpus prefix or full collection name (repeatable)")
@click.option("--n", default=10, show_default=True, help="Max results to return")
@click.option("--hybrid", is_flag=True, default=False,
              help="Merge semantic + ripgrep results for code (0.7*vector + 0.3*frecency)")
@click.option("--no-rerank", "no_rerank", is_flag=True, default=False,
              help="Disable cross-corpus reranking (use round-robin instead)")
@click.option("--mxbai", is_flag=True, default=False,
              help="Fan out to Mixedbread-indexed collections (read-only)")
@click.option("--agentic", is_flag=True, default=False,
              help="Multi-step Haiku query refinement before returning results")
@click.option("-a", "--answer", "answer", is_flag=True, default=False,
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
              help="Show matched text inline under each result.")
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
) -> None:
    """Semantic search across T3 knowledge collections.

    QUERY is the search query string.

    PATH (optional) scopes results to files under that directory path.
    Relative paths are resolved against the current working directory.

    --corpus may be a prefix (code, docs, knowledge) or a fully-qualified
    collection name (code__myrepo).  Repeat --corpus to search multiple corpora.
    """
    # Build path-scoping where filter
    where_filter: dict | None = None
    if path is not None:
        resolved = str(Path(path).resolve())
        where_filter = {"file_path": {"$startswith": resolved}}

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
        click.echo("No matching collections found.")
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
            for line in format_plain([result]):
                click.echo(line)
            if show_content:
                text = result.content
                if len(text) > _CONTENT_MAX_CHARS:
                    text = text[:_CONTENT_MAX_CHARS] + "..."
                click.echo(f"  {text}")
