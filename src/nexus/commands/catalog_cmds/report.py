# SPDX-License-Identifier: AGPL-3.0-or-later
# Copyright (c) 2026 Hal Hildebrand. All rights reserved.
"""Catalog reporting commands for the ``nx catalog`` group (nexus-whh61.4).

Carved out of ``commands.catalog``: the read-only reporting verbs ``stats``,
``orphans``, ``session-summary`` and ``coverage``, plus the private
``_taxonomy_stats`` helper that only ``stats`` uses. Behaviour-preserving;
``register`` attaches all four to the shared ``catalog`` group.

The companion integrity verbs ``audit-membership`` and ``verify`` (which carry
their own private helpers and route the shared ``_make_t3``) are carved
separately into ``catalog_cmds/integrity.py`` (nexus-whh61.4), so this module
stays reporting-only.

``_get_catalog`` is reached through the ``nexus.commands.catalog`` module
object inside each command body — keeping imports acyclic and preserving the
``patch("nexus.commands.catalog._get_catalog", …)`` test seam.
"""
from __future__ import annotations

import json

import click


def _taxonomy_stats() -> dict | None:
    """Return a taxonomy stats block for ``nx catalog stats`` or ``None``.

    The catalog is three layers: nexus-catalog (owners / documents /
    links), ChromaDB (the physical rows), and CatalogTaxonomy (topics
    and projection assignments). ``stats`` previously enumerated only
    the first layer; this helper adds the third. Returns ``None`` when
    T2 is absent or carries no topic rows. Skips the block silently
    for users who have not run discover / project yet. bead nexus-iojz
    (formerly nexus-1n0t).
    """
    from nexus.commands._helpers import default_db_path  # noqa: PLC0415 — deferred import; rare/branch-local path or circular-dep / startup-cost avoidance
    from nexus.db.t2 import T2Database  # noqa: PLC0415 — deferred import; rare/branch-local path or circular-dep / startup-cost avoidance

    try:
        db_path = default_db_path()
    except Exception:  # noqa: BLE001 — best-effort fallback path; failure is non-fatal here
        return None
    if not db_path.exists():
        return None

    try:
        with T2Database(db_path) as db:  # epsilon-allow: read-only T2 access, no WAL writer contention (RDR-128 P3)
            # nexus-pyzk7: service-backed taxonomy has no raw .conn. This is a
            # read-only topic-stats display; skip cleanly in service mode rather
            # than relying on the AttributeError → except below (the docstring
            # promised a graceful skip the bare access did not deliver).
            if not hasattr(db.taxonomy, "conn"):
                return None
            conn = db.taxonomy.conn  # epsilon-allow: guarded by hasattr(db.taxonomy,'conn') skip above (nexus-pyzk7 Part 3)
            with db.taxonomy._lock:  # epsilon-allow: guarded by hasattr(db.taxonomy,'conn') skip above (nexus-pyzk7 Part 3)
                topic_total = conn.execute(
                    "SELECT count(*) FROM topics"
                ).fetchone()[0]
                if not topic_total:
                    return None
                assignment_total = conn.execute(
                    "SELECT count(*) FROM topic_assignments"
                ).fetchone()[0]
                distinct_topics = conn.execute(
                    "SELECT count(DISTINCT topic_id) FROM topic_assignments"
                ).fetchone()[0]
                by_source_rows = conn.execute(
                    "SELECT source_collection, count(*) "
                    "FROM topic_assignments "
                    "WHERE assigned_by = 'projection' "
                    "AND source_collection IS NOT NULL "
                    "AND source_collection != '' "
                    "GROUP BY source_collection "
                    "ORDER BY count(*) DESC"
                ).fetchall()
    except Exception:  # noqa: BLE001 — best-effort fallback path; failure is non-fatal here
        return None

    return {
        "topics": int(topic_total),
        "assignments": int(assignment_total),
        "distinct_topics_assigned": int(distinct_topics),
        "projection_by_source": {row[0]: int(row[1]) for row in by_source_rows},
    }


@click.command("stats")
@click.option("--json", "as_json", is_flag=True)
def stats_cmd(as_json: bool) -> None:
    """Show catalog statistics."""
    from nexus.commands import catalog as _cat_cmd  # noqa: PLC0415 — module-routed helper access keeps import acyclic + monkeypatch-visible

    cat = _cat_cmd._get_catalog()
    # nexus-xnz0o: stats() is uniform across SQLite and service mode.
    s = cat.stats()
    owner_count = s.get("owner_count", 0)
    doc_count = s.get("doc_count", 0)
    link_count = s.get("link_count", 0)
    chunk_count = s.get("chunk_count", 0)  # nexus-aeceu: now present on both backends
    # by_content_type is included in the stats() response (nexus-xnz0o).
    type_counts = s.get("by_content_type", {})
    link_type_counts = s.get("links_by_type", {})
    tax = _taxonomy_stats()
    if as_json:
        payload: dict = {
            "owners": owner_count,
            "documents": doc_count,
            "links": link_count,
            "chunks": chunk_count,
            "by_type": type_counts,
            "by_link_type": link_type_counts,
        }
        if tax is not None:
            payload["taxonomy"] = tax
        click.echo(json.dumps(payload, indent=2))
    else:
        click.echo(f"Owners:    {owner_count}")
        click.echo(f"Documents: {doc_count}")
        click.echo(f"Links:     {link_count}")
        click.echo(f"Chunks:    {chunk_count}")
        if type_counts:
            click.echo("By type:")
            for t, c in sorted(type_counts.items()):
                click.echo(f"  {t:<12} {c}")
        if link_type_counts:
            click.echo("By link type:")
            for t, c in sorted(link_type_counts.items()):
                click.echo(f"  {t:<12} {c}")
        if tax is not None:
            click.echo(
                f"Topics:    {tax['topics']} topics, "
                f"{tax['assignments']} assignments "
                f"({tax['distinct_topics_assigned']} distinct topics assigned)"
            )
            by_source = tax["projection_by_source"]
            if by_source:
                click.echo("Projection by source:")
                for src, count in sorted(
                    by_source.items(), key=lambda kv: -kv[1],
                ):
                    click.echo(f"  {src:<40} {count}")


@click.command("orphans")
@click.option("--no-links", "no_links", is_flag=True, help="Show entries with zero incoming and outgoing links")
def orphans_cmd(no_links: bool) -> None:
    """Find catalog entries that are not connected to anything.

    \b
    Examples:
      nx catalog orphans --no-links    # entries with no links at all
    """
    from nexus.commands import catalog as _cat_cmd  # noqa: PLC0415 — module-routed helper access keeps import acyclic + monkeypatch-visible

    if not no_links:
        raise click.UsageError("Specify a mode: --no-links")

    cat = _cat_cmd._get_catalog()
    # nexus-xnz0o: orphaned_docs() is uniform across SQLite and service mode.
    orphans = cat.orphaned_docs()
    rows = [(d["tumbler"], d["title"], d["content_type"], d["file_path"]) for d in orphans]

    if not rows:
        click.echo("No orphan entries (all documents have at least one link).")
        return

    click.echo(f"Orphan entries ({len(rows)} with no links):")
    for tumbler, title, content_type, file_path in sorted(rows, key=lambda r: (r[2], r[0])):
        loc = f"  [{file_path}]" if file_path else ""
        click.echo(f"  {tumbler:<12} {content_type:<10} {title}{loc}")


@click.command("session-summary")
@click.option("--since", default=24, type=int, help="Hours to look back for git changes")
def session_summary_cmd(since: int) -> None:
    """Show link graph summary for recently modified files.

    \b
    Examples:
      nx catalog session-summary            # files modified in last 24 hours
      nx catalog session-summary --since 48 # last 48 hours
    """
    import subprocess  # noqa: PLC0415 — deferred import; rare/branch-local path or circular-dep / startup-cost avoidance

    from nexus.commands import catalog as _cat_cmd  # noqa: PLC0415 — module-routed helper access keeps import acyclic + monkeypatch-visible

    try:
        cat = _cat_cmd._get_catalog()
    except click.ClickException:
        click.echo("Catalog not initialized — skipping session summary.")
        return

    try:
        result = subprocess.run(
            [
                "git", "log",
                f"--since={since} hours ago",
                "--name-only",
                "--pretty=format:",
                "--diff-filter=ACMR",
            ],
            capture_output=True, text=True, timeout=5,
        )
        files = {f.strip() for f in result.stdout.splitlines() if f.strip()}
    except Exception:  # noqa: BLE001 — best-effort; error surfaced via log/echo, must not crash caller
        click.echo("Could not determine recent file changes.")
        return

    if not files:
        click.echo(f"No files modified in the last {since} hours.")
    else:
        # nexus-xnz0o: replaced raw SQL with catalog API (uniform across backends).
        found_any = False
        for fp in sorted(files):
            entry = cat.find_by_file_path(fp)
            if not entry:
                continue
            tumbler = entry.tumbler
            # Collect titles of linked RDR documents (content_type == 'rdr').
            rdr_titles: list[str] = []
            for lnk in cat.links_from(tumbler):
                peer_t = getattr(lnk, "to_tumbler", None)
                if peer_t:
                    peer = cat.resolve(peer_t)
                    if peer and peer.content_type == "rdr":
                        rdr_titles.append(peer.title)
            for lnk in cat.links_to(tumbler):
                peer_t = getattr(lnk, "from_tumbler", None)
                if peer_t:
                    peer = cat.resolve(peer_t)
                    if peer and peer.content_type == "rdr":
                        rdr_titles.append(peer.title)
            rdr_titles = list(dict.fromkeys(rdr_titles))  # deduplicate order-preserving
            if rdr_titles:
                rdrs = ", ".join(rdr_titles)
                click.echo(f"  {fp} — {len(rdr_titles)} RDR(s): {rdrs}")
                found_any = True

        if not found_any:
            click.echo("No linked RDRs found for recently modified files.")

    total = cat.stats().get("link_count", 0)
    click.echo(f"\nLink graph: {total} links active.")


@click.command("coverage")
@click.option("--owner", "owner_prefix", default="", help="Filter by tumbler prefix (e.g. '1.1')")
def coverage_cmd(owner_prefix: str) -> None:
    """Show what percentage of catalog entries have at least one link, by content type.

    \b
    Examples:
      nx catalog coverage                # all types
      nx catalog coverage --owner 1.1   # only entries under owner 1.1
    """
    from nexus.commands import catalog as _cat_cmd  # noqa: PLC0415 — module-routed helper access keeps import acyclic + monkeypatch-visible

    cat = _cat_cmd._get_catalog()
    rows = cat.coverage_by_content_type(owner_prefix)
    if not rows:
        click.echo("No documents in catalog.")
        return

    click.echo("Link coverage by content type:")
    for row in sorted(rows, key=lambda r: r["content_type"]):
        ct = row["content_type"] or "(none)"
        total = row["total"]
        linked = row["linked"]
        pct = (linked / total * 100) if total else 0.0
        click.echo(f"  {ct:<12} {linked:>4}/{total:<4} = {pct:5.1f}%")


def register(group: click.Group) -> None:
    """Attach the reporting commands to the shared ``catalog`` group."""
    group.add_command(stats_cmd)
    group.add_command(orphans_cmd)
    group.add_command(session_summary_cmd)
    group.add_command(coverage_cmd)
