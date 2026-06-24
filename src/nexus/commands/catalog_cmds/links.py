# SPDX-License-Identifier: AGPL-3.0-or-later
# Copyright (c) 2026 Hal Hildebrand. All rights reserved.
"""Link command family for the ``nx catalog`` group (nexus-kgyoz).

Carved verbatim out of ``commands.catalog``: the link CRUD/query commands
(``link`` / ``unlink`` / ``links`` / ``link-bulk-delete`` / ``link-audit``)
and the link analysis/generation commands (``links-for-file`` /
``link-density`` / ``suggest-links`` / ``generate-links`` / ``link-generate``),
plus the two link-only render helpers (``_endpoint_label`` /
``_unique_edges_by_target``) that only ``links`` uses. Behaviour-preserving —
names, options, and output are identical; ``register`` attaches every command
to the shared ``catalog`` group so ``nx catalog link`` (etc.) resolve exactly
as before.

Shared helpers (``_get_catalog`` / ``_get_catalog_writer`` /
``_resolve_tumbler``) stay in ``commands.catalog`` and are reached through the
module object inside each command body — keeping this module's imports acyclic
(``commands.catalog`` imports this module at its tail) and preserving the
``patch("nexus.commands.catalog._get_catalog", …)`` test seam.
"""
from __future__ import annotations

import json
from typing import Any

import click

from nexus.catalog.tumbler import Tumbler


@click.command("link")
@click.argument("from_tumbler")
@click.argument("to_tumbler")
@click.option(
    "--type", "link_type", required=True,
    help="Link type (e.g. cites, implements, supersedes, relates, quotes, comments, or any custom type)",
)
@click.option("--from-span", default="", help="Span: 'line-line', 'chunk:char-char', 'chash:<hex>', or 'chash:<hex>:<start>-<end>'")
@click.option("--to-span", default="", help="Span: 'line-line', 'chunk:char-char', 'chash:<hex>', or 'chash:<hex>:<start>-<end>'")
def link_cmd(
    from_tumbler: str, to_tumbler: str, link_type: str,
    from_span: str, to_span: str,
) -> None:
    """Create a typed link between two documents.

    Both FROM and TO accept tumblers (1.1.5) or titles. Built-in link types:
    cites, implements, implements-heuristic, supersedes, relates, quotes,
    comments, formalizes. Custom types are also accepted. Duplicate links
    are merged.

    \b
    Spans (optional) identify the specific passage being referenced:
      --from-span "42-57"              lines 42-57 of the source document
      --to-span "3:100-250"            chars 100-250 of chunk 3 in T3
      --from-span "chash:<sha256hex>"  content-addressed chunk (preferred)
      --from-span "chash:<sha256hex>:100-250"  character range within a chunk
    Content-hash spans survive re-indexing. Use 'nx catalog show' to see resolved span text.
    """
    from nexus.commands import catalog as _cat_cmd  # noqa: PLC0415 — module-routed helper access keeps import acyclic + monkeypatch-visible

    cat = _cat_cmd._get_catalog()
    writer = _cat_cmd._get_catalog_writer()
    ft = _cat_cmd._resolve_tumbler(cat, from_tumbler)
    tt = _cat_cmd._resolve_tumbler(cat, to_tumbler)
    writer.link(ft, tt, link_type, created_by="user", from_span=from_span, to_span=to_span)
    click.echo(f"Linked: {ft} → {tt} ({link_type})")


@click.command("unlink")
@click.argument("from_tumbler")
@click.argument("to_tumbler")
@click.option("--type", "link_type", default="")
def unlink_cmd(from_tumbler: str, to_tumbler: str, link_type: str) -> None:
    """Remove link(s) between two documents.

    Both FROM and TO accept tumblers or titles. Omit --type to remove all
    link types between the pair.
    """
    from nexus.commands import catalog as _cat_cmd  # noqa: PLC0415 — module-routed helper access keeps import acyclic + monkeypatch-visible

    cat = _cat_cmd._get_catalog()
    writer = _cat_cmd._get_catalog_writer()
    ft = _cat_cmd._resolve_tumbler(cat, from_tumbler)
    tt = _cat_cmd._resolve_tumbler(cat, to_tumbler)
    removed = writer.unlink(ft, tt, link_type)
    click.echo(f"Removed {removed} link(s)")


def _endpoint_label(cat: Any, tumbler: Any) -> str:
    """Render a tumbler as ``'<title-or-path> (<tumbler>)'`` for human output.

    Used by ``nx catalog links --resolve``. Prefers ``title`` for
    documents that have one, falls back to ``file_path`` for code /
    prose entries that carry a path, and finally to the bare tumbler
    for entries without either. bead nexus-iojz (formerly nexus-i63n).
    """
    try:
        entry = cat.resolve(tumbler)
    except Exception:  # noqa: BLE001 — best-effort fallback path; failure is non-fatal here
        return str(tumbler)
    if entry is None:
        return str(tumbler)
    label = entry.title or entry.file_path or str(tumbler)
    if label == str(tumbler):
        return str(tumbler)
    return f"{label} ({tumbler})"


def _unique_edges_by_target(cat: Any, edges: list) -> list:
    """Return a stable de-duplicated edge list keyed by ``(from_tumbler,
    link_type, target.file_path or target.tumbler)``.

    Re-indexing the same file under multiple owner tumblers leaves many
    edges that point at the same source file via different tumblers.
    The natural emission order surfaces the duplicate run first, so
    the first copy wins and later aliases drop. Fails open: if a
    target tumbler does not resolve, its bare string is the dedup key,
    so orphan rows are preserved. bead nexus-iojz (formerly nexus-x6eu).
    """
    seen: set[tuple[str, str, str]] = set()
    out: list = []
    for edge in edges:
        try:
            target = cat.resolve(edge.to_tumbler)
            target_key = (
                target.file_path if target and target.file_path else str(edge.to_tumbler)
            )
        except Exception:  # noqa: BLE001 — boundary catch; third-party raises undocumented types, handled gracefully
            target_key = str(edge.to_tumbler)
        key = (str(edge.from_tumbler), edge.link_type, target_key)
        if key in seen:
            continue
        seen.add(key)
        out.append(edge)
    return out


@click.command("links")
@click.argument("tumbler", default="")
@click.option("--from", "from_t", default="", help="Filter by source tumbler or title")
@click.option("--to", "to_t", default="", help="Filter by target tumbler or title")
@click.option("--direction", default="both", type=click.Choice(["in", "out", "both"]))
@click.option("--type", "link_type", default="")
@click.option("--created-by", default="", help="Filter by creator (e.g. bib_enricher)")
@click.option("--depth", default=1, type=int, help="BFS depth for graph traversal")
@click.option("--limit", "-n", default=50, type=int)
@click.option("--offset", default=0, type=int)
@click.option("--json", "as_json", is_flag=True)
@click.option(
    "--resolve", "resolve_labels", is_flag=True,
    help=(
        "Render each endpoint as '<title-or-path> (<tumbler>)' "
        "instead of bare tumbler arrows."
    ),
)
@click.option(
    "--unique-targets", "unique_targets", is_flag=True,
    help=(
        "Collapse rows that point at the same file_path via "
        "different owner tumblers (e.g. after re-indexing). Keeps "
        "the first seen edge per target."
    ),
)
def links_cmd(
    tumbler: str, from_t: str, to_t: str, direction: str,
    link_type: str, created_by: str, depth: int,
    limit: int, offset: int, as_json: bool,
    resolve_labels: bool, unique_targets: bool,
) -> None:
    """Show or query links. TUMBLER (optional) accepts a tumbler or title.

    \b
    Examples:
      nx catalog links 1.1.5                    # links for document
      nx catalog links "auth module" --type cites
      nx catalog links --created-by bib_enricher # all links by creator
      nx catalog links --type cites --json       # all citation links
      nx catalog links 1.17.14 --resolve         # inline titles / file paths
      nx catalog links 1.17.14 --type implements --unique-targets
    """
    from nexus.commands import catalog as _cat_cmd  # noqa: PLC0415 — module-routed helper access keeps import acyclic + monkeypatch-visible

    cat = _cat_cmd._get_catalog()

    def _render_edge(edge: Any) -> str:
        if resolve_labels:
            src = _endpoint_label(cat, edge.from_tumbler)
            dst = _endpoint_label(cat, edge.to_tumbler)
            return f"{src} → {dst} ({edge.link_type}) by {edge.created_by}"
        return (
            f"{edge.from_tumbler} → {edge.to_tumbler} "
            f"({edge.link_type}) by {edge.created_by}"
        )

    # If a positional tumbler is given, use graph traversal (the common case)
    if tumbler:
        t = _cat_cmd._resolve_tumbler(cat, tumbler)
        result = cat.graph(t, depth=depth, direction=direction, link_type=link_type)
        edges = result["edges"]
        if unique_targets:
            edges = _unique_edges_by_target(cat, edges)
        if as_json:
            click.echo(json.dumps({
                "nodes": [n.to_dict() for n in result["nodes"]],
                "edges": [e.to_dict() for e in edges],
            }, indent=2))
        else:
            if not edges:
                click.echo("No links found.")
                return
            for edge in edges:
                click.echo(_render_edge(edge))
        return

    # No positional tumbler — flat filter query
    resolved_from = str(_cat_cmd._resolve_tumbler(cat, from_t)) if from_t else ""
    resolved_to = str(_cat_cmd._resolve_tumbler(cat, to_t)) if to_t else ""
    links = cat.link_query(
        from_t=resolved_from, to_t=resolved_to,
        link_type=link_type, created_by=created_by,
        limit=limit + 1, offset=offset,
    )
    has_more = len(links) > limit
    links = links[:limit]
    if unique_targets:
        links = _unique_edges_by_target(cat, links)
    if as_json:
        click.echo(json.dumps([lnk.to_dict() for lnk in links], indent=2))
    else:
        if not links:
            click.echo("No links found.")
            return
        for edge in links:
            click.echo(_render_edge(edge))
        if has_more:
            click.echo(f"\n  Next page: --offset {offset + limit}")


@click.command("link-bulk-delete", hidden=True)
@click.option("--from", "from_t", default="", help="From tumbler or title")
@click.option("--to", "to_t", default="", help="To tumbler or title")
@click.option("--type", "link_type", default="")
@click.option("--created-by", default="")
@click.option("--created-at-before", default="", help="ISO timestamp cutoff")
@click.option(
    "--dry-run/--no-dry-run", default=True,
    help="Report-only (default). Use --no-dry-run to perform deletions.",
)
@click.option(
    "--confirm", is_flag=True, default=False,
    help="Required alongside --no-dry-run to actually delete links.",
)
def link_bulk_delete_cmd(
    from_t: str, to_t: str, link_type: str, created_by: str,
    created_at_before: str, dry_run: bool, confirm: bool,
) -> None:
    """Bulk delete links matching filters.

    nexus-9nim: 4.29.1 inverted the default from "delete unless --dry-run"
    to "report unless --no-dry-run --confirm" + writes a backup snapshot
    before the actual delete. Recoverable via ``nx catalog undelete``.
    """
    from nexus.commands import catalog as _cat_cmd  # noqa: PLC0415 — module-routed helper access keeps import acyclic + monkeypatch-visible

    will_delete = (not dry_run) and confirm
    if (not dry_run) and not confirm:
        click.echo(
            "--no-dry-run alone is treated as report-only. "
            "Add --confirm to actually delete links."
        )

    cat = _cat_cmd._get_catalog()
    writer = _cat_cmd._get_catalog_writer()
    resolved_from = str(_cat_cmd._resolve_tumbler(cat, from_t)) if from_t else ""
    resolved_to = str(_cat_cmd._resolve_tumbler(cat, to_t)) if to_t else ""

    # First pass: dry-run to enumerate the matching links for the
    # backup snapshot. The cat.bulk_unlink dry_run path returns the
    # count; we need the actual rows for the snapshot, so query directly.
    matching_links = cat.link_query(
        from_t=resolved_from, to_t=resolved_to,
        link_type=link_type, created_by=created_by,
        created_at_before=created_at_before,
        limit=0,
    )
    count = len(matching_links)

    if not will_delete:
        click.echo(f"Would remove {count} link(s)")
        return

    # Backup snapshot before delete (RDR-106 Option A).
    from nexus.catalog.catalog_backup import snapshot_links  # noqa: PLC0415 — deferred import; rare/branch-local path or circular-dep / startup-cost avoidance
    backup_path = snapshot_links(
        cat,
        [
            {
                "from": str(lnk.from_tumbler),
                "to": str(lnk.to_tumbler),
                "link_type": lnk.link_type,
                "from_span": lnk.from_span,
                "to_span": lnk.to_span,
                "created_by": lnk.created_by,
                "created_at": lnk.created_at,
                "meta": lnk.meta,
            }
            for lnk in matching_links
        ],
        verb="link-bulk-delete",
        reason="bulk-unlink filters",
        args={
            "from_t": resolved_from, "to_t": resolved_to,
            "link_type": link_type, "created_by": created_by,
            "created_at_before": created_at_before,
        },
    )
    if backup_path:
        click.echo(
            f"Backup snapshot written: {backup_path}\n"
            f"  Restore with: nx catalog undelete {backup_path.name}"
        )

    actual = writer.bulk_unlink(
        from_t=resolved_from, to_t=resolved_to,
        link_type=link_type, created_by=created_by,
        created_at_before=created_at_before, dry_run=False,
    )
    click.echo(f"Removed {actual} link(s)")


@click.command("link-audit", hidden=True)
@click.option("--json", "as_json", is_flag=True)
def link_audit_cmd(as_json: bool) -> None:
    """Audit the link graph: stats, orphans, duplicates."""
    from nexus.commands import catalog as _cat_cmd  # noqa: PLC0415 — module-routed helper access keeps import acyclic + monkeypatch-visible

    cat = _cat_cmd._get_catalog()
    result = cat.link_audit()
    if as_json:
        click.echo(json.dumps(result, indent=2))
    else:
        click.echo(f"Total links:     {result['total']}")
        click.echo(f"Orphaned:        {result['orphaned_count']}")
        click.echo(f"Duplicates:      {result['duplicate_count']}")
        if result["by_type"]:
            click.echo("By type:")
            for t, c in sorted(result["by_type"].items()):
                click.echo(f"  {t:<12} {c}")
        if result["by_creator"]:
            click.echo("By creator:")
            for c, n in sorted(result["by_creator"].items()):
                click.echo(f"  {c:<20} {n}")
        if result["orphaned"]:
            click.echo("Orphaned links:")
            for o in result["orphaned"]:
                click.echo(f"  {o['from']} → {o['to']} ({o['type']})")


@click.command("links-for-file")
@click.argument("file_path")
def links_for_file_cmd(file_path: str) -> None:
    """Show catalog entries linked to a specific file.

    \b
    Examples:
      nx catalog links-for-file src/nexus/catalog/catalog.py
      nx catalog links-for-file docs/rdr/rdr-060.md
    """
    from nexus.commands import catalog as _cat_cmd  # noqa: PLC0415 — module-routed helper access keeps import acyclic + monkeypatch-visible

    cat = _cat_cmd._get_catalog()
    # nexus-xnz0o: replaced raw SQL with catalog API.
    entry = cat.find_by_file_path(file_path)
    if not entry:
        click.echo(f"No catalog entry for: {file_path}")
        return

    tumbler_str = str(entry.tumbler)
    click.echo(f"{tumbler_str} {entry.content_type}: {entry.title}")

    links_out = cat.links_from(entry.tumbler)
    links_in  = cat.links_to(entry.tumbler)

    all_link_rows = []
    for lnk in links_out:
        peer = cat.resolve(lnk.to_tumbler) if hasattr(lnk, "to_tumbler") else None
        to_t = getattr(lnk, "to_tumbler", "")
        all_link_rows.append((
            str(to_t),
            peer.title if peer else str(to_t),
            peer.content_type if peer else "",
            getattr(lnk, "link_type", ""),
            "outgoing",
        ))
    for lnk in links_in:
        peer = cat.resolve(lnk.from_tumbler) if hasattr(lnk, "from_tumbler") else None
        from_t = getattr(lnk, "from_tumbler", "")
        all_link_rows.append((
            str(from_t),
            peer.title if peer else str(from_t),
            peer.content_type if peer else "",
            getattr(lnk, "link_type", ""),
            "incoming",
        ))

    if not all_link_rows:
        click.echo("  No links.")
        return

    for t, t_title, t_type, l_type, direction in sorted(all_link_rows, key=lambda r: (r[3], r[2])):
        arrow = "→" if direction == "outgoing" else "←"
        click.echo(f"  {arrow} [{l_type}] {t} {t_type}: {t_title}")


@click.command("link-density")
@click.option(
    "--by-collection/--no-by-collection",
    "by_collection",
    default=True,
    help="Aggregate by physical_collection (default).",
)
@click.option(
    "--sample",
    default=50,
    type=int,
    help="Max seeds per collection to BFS (capped to keep latency bounded).",
)
@click.option("--depth", default=2, type=int, help="BFS depth (default 2).")
@click.option(
    "--threshold",
    default=3,
    type=int,
    help="Frontier-p50 below this flags the collection as low-density.",
)
def link_density_cmd(
    by_collection: bool, sample: int, depth: int, threshold: int
) -> None:
    """Measure catalog link-graph density per collection.

    For each ``physical_collection``, samples up to ``--sample`` seed
    tumblers, runs a depth-``--depth`` BFS from each, and reports the
    frontier-size distribution (p50, p90) plus the link types that
    fired during traversal.

    \b
    Use this before deciding whether a hybrid retrieval plan
    (RDR-097) makes sense for a given collection. A collection with
    median frontier <``--threshold`` nodes is flagged as a poor
    candidate — graph traversal will add latency with little recall
    gain. Vector-only retrieval is the better choice there.

    \b
    The frontier count for one seed is the number of nodes reachable
    within ``--depth`` hops, excluding the seed itself.
    """
    import statistics  # noqa: PLC0415  — stdlib deferred to call site (statistics)

    from nexus.commands import catalog as _cat_cmd  # noqa: PLC0415 — module-routed helper access keeps import acyclic + monkeypatch-visible

    cat = _cat_cmd._get_catalog()
    # By design the bead specifies physical_collection grouping; the
    # ``--no-by-collection`` flag is a placeholder for a future
    # global-density rollup so the option signature stays stable.
    if not by_collection:
        click.echo("Global rollup not yet implemented — use --by-collection.")
        return

    # nexus-xnz0o: replaced raw SQL GROUP BY with distinct_doc_collections() +
    # list_by_collection() for uniform SQLite + service mode support.
    collections = cat.distinct_doc_collections()
    # Build (collection, total) pairs via list_by_collection (cached per-call).
    coll_entries: dict[str, list] = {}
    for coll in collections:
        coll_entries[coll] = cat.list_by_collection(coll)
    rows = [(coll, len(entries)) for coll, entries in sorted(coll_entries.items())]

    if not rows:
        click.echo("No collections registered in catalog.")
        return

    click.echo(
        f"Link-graph density (depth={depth}, sample={sample} per collection):"
    )
    header = (
        f"  {'collection':<40} {'seeds':>5} {'p50':>5} {'p90':>5}  "
        f"{'flag':<8}  link_types"
    )
    click.echo(header)
    click.echo(f"  {'-' * 38:<40} {'-' * 5:>5} {'-' * 5:>5} {'-' * 5:>5}  "
               f"{'-' * 6:<8}  ----------")

    for coll, total in rows:
        seed_entries = coll_entries.get(coll, [])[:sample]
        seeds: list[Tumbler] = []
        for e in seed_entries:
            try:
                seeds.append(Tumbler.parse(str(e.tumbler)))
            except Exception:  # noqa: BLE001 — boundary catch; third-party raises undocumented types, handled gracefully
                continue

        if not seeds:
            click.echo(
                f"  {coll:<40} {0:>5} {0:>5} {0:>5}  "
                f"{'no-seed':<8}  -"
            )
            continue

        frontier_counts: list[int] = []
        link_types_seen: set[str] = set()
        for seed in seeds:
            try:
                result = cat.graph(seed, depth=depth, direction="both")
            except Exception:  # noqa: BLE001 — boundary catch; third-party raises undocumented types, handled gracefully
                continue
            nodes = result.get("nodes") or []
            edges = result.get("edges") or []
            frontier_counts.append(max(0, len(nodes) - 1))
            for e in edges:
                if getattr(e, "link_type", None):
                    link_types_seen.add(e.link_type)

        if not frontier_counts:
            click.echo(
                f"  {coll:<40} {len(seeds):>5} {0:>5} {0:>5}  "
                f"{'bfs-err':<8}  -"
            )
            continue

        sorted_counts = sorted(frontier_counts)
        p50_val = statistics.median(sorted_counts)
        # Positional p90: floor(0.9 * (n-1)). For n=1, that's index 0.
        p90_idx = max(0, int(0.9 * (len(sorted_counts) - 1) + 0.5))
        p90_val = sorted_counts[p90_idx]

        flag = "low" if p50_val < threshold else "ok"
        types_str = ",".join(sorted(link_types_seen)) or "-"
        click.echo(
            f"  {coll:<40} {len(seeds):>5} {p50_val:>5.1f} {p90_val:>5}  "
            f"{flag:<8}  {types_str}"
        )

    click.echo()
    click.echo(
        f"Flag legend: 'low' = frontier-p50 < {threshold} (consider "
        f"vector-only retrieval); 'ok' = sufficient density for hybrid."
    )


@click.command("suggest-links")
@click.option("--limit", "-n", default=50, type=int, help="Max suggestions to show")
@click.option("--threshold", default=0.0, type=float, help="Reserved for future similarity threshold (unused)")
def suggest_links_cmd(limit: int, threshold: float) -> None:
    """Suggest unlinked code-RDR pairs by module-name overlap.

    Finds code entries whose filename stem appears in an RDR title, where no
    link yet exists between the pair. Same heuristic as 'generate-links' but
    read-only — shows what would be created.

    \b
    Examples:
      nx catalog suggest-links
      nx catalog suggest-links --limit 20
    """
    from pathlib import Path as _Path  # noqa: PLC0415 — deferred import; rare/branch-local path or circular-dep / startup-cost avoidance

    from nexus.commands import catalog as _cat_cmd  # noqa: PLC0415 — module-routed helper access keeps import acyclic + monkeypatch-visible

    cat = _cat_cmd._get_catalog()
    entries = cat.all_documents(limit=10_000)
    rdr_entries = [e for e in entries if e.content_type == "rdr"]
    code_entries = [e for e in entries if e.content_type == "code" and e.file_path]

    if not rdr_entries or not code_entries:
        click.echo("0 suggestions (no code or RDR entries to match).")
        return

    # Pre-normalize RDR titles
    rdr_normalized = [
        (rdr, rdr.title.lower().replace("-", "").replace(" ", "").replace("_", ""))
        for rdr in rdr_entries
    ]

    suggestions: list[tuple[str, str, str, str]] = []  # (code_t, rdr_t, module, rdr_title)
    for code in code_entries:
        module_name = _Path(code.file_path).stem.replace("_", "").lower()
        if len(module_name) <= 3:
            continue
        for rdr, rdr_title_norm in rdr_normalized:
            if module_name not in rdr_title_norm:
                continue
            # Check if link already exists in either direction
            existing = cat.link_query(
                from_t=str(code.tumbler), to_t=str(rdr.tumbler),
                link_type="", limit=1,
            ) or cat.link_query(
                from_t=str(rdr.tumbler), to_t=str(code.tumbler),
                link_type="", limit=1,
            )
            if not existing:
                suggestions.append((
                    str(code.tumbler), str(rdr.tumbler),
                    module_name, rdr.title,
                ))
        if len(suggestions) >= limit:
            break

    suggestions = suggestions[:limit]
    if not suggestions:
        click.echo("0 suggestions (all matching pairs are already linked).")
        return

    click.echo(f"{len(suggestions)} suggestion(s):")
    for code_t, rdr_t, module, rdr_title in suggestions:
        click.echo(f"  {code_t} → {rdr_t}  [{module}] {rdr_title}")


@click.command("generate-links")
@click.option("--citations/--no-citations", default=True, help="Generate citation links from bib metadata")
@click.option("--filepath/--no-filepath", default=True, help="Generate RDR filepath links")
@click.option("--dry-run", is_flag=True, help="Show what would be created without writing")
def generate_links_cmd(citations: bool, filepath: bool, dry_run: bool) -> None:
    """Auto-generate typed links from metadata cross-matching."""
    from nexus.commands import catalog as _cat_cmd  # noqa: PLC0415 — module-routed helper access keeps import acyclic + monkeypatch-visible

    cat = _cat_cmd._get_catalog()
    from nexus.catalog.link_generator import (  # noqa: PLC0415 — deferred import; rare/branch-local path or circular-dep / startup-cost avoidance
        generate_citation_links,
        generate_rdr_filepath_links,
    )

    writer = _cat_cmd._get_catalog_writer() if not dry_run else None
    try:
        total = 0
        if citations:
            if dry_run:
                click.echo("Would generate citation links (dry-run mode not yet supported for link preview)")
            else:
                count = generate_citation_links(cat, writer=writer)
                click.echo(f"Citation links created: {count}")
                total += count

        if filepath:
            if dry_run:
                click.echo("Would generate RDR filepath links (dry-run mode not yet supported for link preview)")
            else:
                count = generate_rdr_filepath_links(cat, writer=writer)
                click.echo(f"RDR filepath links created: {count}")
                total += count

        if not dry_run:
            click.echo(f"Total links generated: {total}")
    finally:
        if writer is not None:
            writer.close()


@click.command("link-generate", hidden=True)
@click.option("--dry-run", is_flag=True, default=False, help="Show what would be done without writing")
@click.pass_context
def link_generate_cmd(ctx: click.Context, dry_run: bool) -> None:
    """Deprecated alias for ``nx catalog generate-links`` (nexus-2297).

    Pre-fix this verb generated only the RDR filepath links and emitted
    a different message. To converge on the verb-noun convention used
    by ``nx catalog link`` / ``unlink`` / ``links``, the canonical name
    is now ``generate-links`` -- this alias emits a deprecation warning
    and delegates to it. The alias will be removed in a future release.
    """
    click.echo(
        "WARNING: 'nx catalog link-generate' is deprecated and will be "
        "removed in a future release. Use 'nx catalog generate-links' "
        "instead.",
        err=True,
    )
    # Preserve the historical behaviour: filepath links only,
    # no citations. Operators who want both should switch to the
    # canonical ``generate-links`` verb.
    ctx.invoke(
        generate_links_cmd,
        citations=False,
        filepath=True,
        dry_run=dry_run,
    )


def register(group: click.Group) -> None:
    """Attach the link command family to the shared ``catalog`` group."""
    group.add_command(link_cmd)
    group.add_command(unlink_cmd)
    group.add_command(links_cmd)
    group.add_command(link_bulk_delete_cmd)
    group.add_command(link_audit_cmd)
    group.add_command(links_for_file_cmd)
    group.add_command(link_density_cmd)
    group.add_command(suggest_links_cmd)
    group.add_command(generate_links_cmd)
    group.add_command(link_generate_cmd)
