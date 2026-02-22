# SPDX-License-Identifier: AGPL-3.0-or-later
import click

from nexus.corpus import resolve_corpus
from nexus.commands.store import _t3


@click.command("search")
@click.argument("query")
@click.option("--corpus", "-C", multiple=True, default=("knowledge",),
              show_default=True, help="Corpus prefix or full collection name (repeatable)")
@click.option("--n", default=10, show_default=True, help="Max results to return")
def search_cmd(query: str, corpus: tuple[str, ...], n: int) -> None:
    """Semantic search across T3 knowledge collections.

    --corpus may be a prefix (code, docs, knowledge) or a fully-qualified
    collection name (code__myrepo).  Repeat --corpus to search multiple corpora.
    """
    db = _t3()
    all_collections = [c["name"] for c in db.list_collections()]

    target_collections: list[str] = []
    for c in corpus:
        matched = resolve_corpus(c, all_collections)
        if not matched:
            click.echo(f"Warning: no collections match --corpus {c!r}", err=True)
        target_collections.extend(matched)

    # deduplicate while preserving order
    seen: set[str] = set()
    target_collections = [x for x in target_collections if not (x in seen or seen.add(x))]  # type: ignore[func-returns-value]

    if not target_collections:
        click.echo("No matching collections found.")
        return

    results = db.search(query, target_collections, n_results=n)
    if not results:
        click.echo("No results.")
        return

    for r in results:
        click.echo(f"[{r['id'][:8]}] {r.get('title', '-')}  dist={r['distance']:.4f}")
        tags = r.get("tags", "")
        if tags:
            click.echo(f"  tags: {tags}")
        preview = r["content"][:200].replace("\n", " ")
        click.echo(f"  {preview}")
