# SPDX-License-Identifier: AGPL-3.0-or-later
# Copyright (c) 2026 Hal Hildebrand. All rights reserved.
"""Catalog integrity / verification commands for the ``nx catalog`` group (nexus-whh61.4).

Carved out of ``commands.catalog``: ``audit-membership`` (audit owner/collection
membership consistency) and ``verify`` (verify catalog vs T3, heal ghosts),
together with the private helpers they exclusively use
(``_audit_membership_all``, ``_home_matches_root``, ``_source_uri_home_key``,
``_heal_ghosts``). Behaviour-preserving; ``register`` attaches both commands to
the shared ``catalog`` group.

The reporting verbs (stats / orphans / session-summary / coverage) are carved
into ``catalog_cmds/report.py``. ``_get_catalog`` / ``_get_catalog_writer`` /
``_make_t3`` are reached through the ``nexus.commands.catalog`` module object
inside each command/helper body — keeping imports acyclic and preserving the
``patch("nexus.commands.catalog._get_catalog", …)`` /
``patch("nexus.commands.catalog._make_t3", …)`` test seams (``_make_t3`` stays
in ``commands.catalog``; it is shared with setup / consolidate / backfill).
"""
from __future__ import annotations

import json
import os

import click

from nexus.catalog.tumbler import Tumbler


@click.command("audit-membership")
@click.argument("collection", required=False)
@click.option(
    "--all-collections",
    is_flag=True,
    help=(
        "Sweep every physical_collection in the catalog and emit a "
        "single summary report. nexus-3e4s Phase 3 — the post-fix "
        "health check. Incompatible with --purge-non-canonical and "
        "--canonical-home (per-collection contexts)."
    ),
)
@click.option(
    "--purge-non-canonical",
    is_flag=True,
    help=(
        "Delete catalog entries whose source_uri does not match the "
        "canonical home for COLLECTION. Default canonical = the home "
        "with the most entries; override with --canonical-home. "
        "Use with --dry-run to preview. Asks for confirmation unless "
        "--yes is passed."
    ),
)
@click.option(
    "--canonical-home",
    default="",
    help=(
        "Override the dominant-home calculation by specifying a "
        "substring that the canonical home must contain (e.g., "
        "'/git/ART'). Use when the contaminating entries outnumber "
        "the legitimate ones, so dominance is a misleading heuristic."
    ),
)
@click.option(
    "--dry-run",
    is_flag=True,
    help="Report what would be deleted without writing.",
)
@click.option(
    "--yes", "-y", is_flag=True, help="Skip confirmation prompt for --purge-non-canonical.",
)
@click.option(
    "--json", "as_json", is_flag=True,
    help="Emit per-home counts as JSON instead of human-readable lines.",
)
def audit_membership_cmd(
    collection: str | None,
    all_collections: bool,
    purge_non_canonical: bool,
    canonical_home: str,
    dry_run: bool,
    yes: bool,
    as_json: bool,
) -> None:
    """Detect cross-project source_uri contamination in COLLECTION.

    Originated from ART-lhk1 (nexus-ow9f): 140 of 245 catalog rows
    in ``rdr__ART-8c2e74c0`` had ``source_uri`` rooted in
    ``/Users/.../nexus/`` rather than the project's expected
    ``/Users/.../ART/`` root. The collection's chunks live under one
    project's identity, so every contaminated entry was a guaranteed
    skip in ``nx enrich aspects`` (no chunks would match).

    The audit groups entries by source_uri "home" (the first 4 path
    segments for ``file://`` URIs, ``scheme://netloc`` otherwise),
    surfaces per-home counts, and identifies the dominant home.
    With ``--purge-non-canonical`` the non-dominant entries are
    soft-deleted (tombstoned in JSONL, removed from SQLite) by the
    standard ``delete_document`` path.

    With ``--all-collections`` the audit runs across every
    physical_collection in the catalog and emits one summary report
    (nexus-3e4s Phase 3). Read-only — purge is per-collection only.
    """
    if all_collections:
        if purge_non_canonical:
            raise click.UsageError(
                "--all-collections is read-only; --purge-non-canonical "
                "must be invoked per-collection so the canonical-home "
                "decision can be reviewed before deletion.",
            )
        if canonical_home:
            raise click.UsageError(
                "--canonical-home is per-collection by definition; "
                "use it with a single COLLECTION argument.",
            )
        if collection:
            raise click.UsageError(
                "Pass either COLLECTION or --all-collections, not both.",
            )
        _audit_membership_all(as_json=as_json)
        return
    if not collection:
        raise click.UsageError(
            "Specify a COLLECTION or use --all-collections.",
        )
    from nexus.commands import catalog as _cat_cmd  # noqa: PLC0415 — module-routed helper access keeps import acyclic + monkeypatch-visible
    cat = _cat_cmd._get_catalog()
    entries = cat.list_by_collection(collection)
    rows = [(str(e.tumbler), e.source_uri or "") for e in entries]

    if not rows:
        if as_json:
            click.echo(json.dumps({
                "collection": collection,
                "total_entries": 0,
                "distinct_homes": 0,
                "by_home": {},
                "dominant_home": None,
            }))
        else:
            click.echo(f"No entries in '{collection}'.")
        return

    by_home: dict[str, list[str]] = {}
    for tumbler_str, source_uri in rows:
        host = _source_uri_home_key(source_uri or "")
        by_home.setdefault(host, []).append(tumbler_str)

    home_counts = {k: len(v) for k, v in by_home.items()}
    dominant_home = max(home_counts.items(), key=lambda kv: kv[1])[0]
    distinct = len(home_counts)

    # Resolve canonical home: explicit substring match wins over the
    # numerical dominant. ART-lhk1 needs this when the contamination
    # exceeds 50% — the user knows /git/ART is canonical even though
    # the leaked /git/nexus entries outnumber the legitimate ones.
    if canonical_home:
        matches = [h for h in home_counts if canonical_home in h]
        if not matches:
            raise click.ClickException(
                f"--canonical-home substring {canonical_home!r} matches "
                f"no home in {sorted(home_counts.keys())!r}"
            )
        if len(matches) > 1:
            raise click.ClickException(
                f"--canonical-home substring {canonical_home!r} is "
                f"ambiguous; matches {matches!r}. Tighten the substring."
            )
        resolved_canonical = matches[0]
    else:
        resolved_canonical = dominant_home

    if as_json:
        click.echo(json.dumps({
            "collection": collection,
            "total_entries": len(rows),
            "distinct_homes": distinct,
            "by_home": home_counts,
            "dominant_home": dominant_home,
            "canonical_home": resolved_canonical,
        }, indent=2))
    else:
        click.echo(
            f"Collection '{collection}': {len(rows)} entries, "
            f"{distinct} distinct source_uri home(s)."
        )
        for home, count in sorted(home_counts.items(), key=lambda kv: -kv[1]):
            tags = []
            if home == dominant_home:
                tags.append("dominant")
            if home == resolved_canonical:
                tags.append("canonical")
            tag_str = f" [{', '.join(tags)}]" if tags else ""
            click.echo(f"  {count:5d}  {home or '(empty source_uri)'}{tag_str}")
        if distinct == 1:
            click.echo(
                "Single source_uri home; no contamination detected."
            )

    if not purge_non_canonical:
        return
    if distinct < 2:
        return  # Nothing to purge.

    purge_targets: list[str] = []
    for home, tumblers in by_home.items():
        if home != resolved_canonical:
            purge_targets.extend(tumblers)

    if dry_run:
        click.echo(
            f"\n[dry-run] Would delete {len(purge_targets)} entries "
            f"whose source_uri home differs from {resolved_canonical!r}."
        )
        return

    if not yes:
        click.confirm(
            f"\nDelete {len(purge_targets)} catalog entries whose "
            f"source_uri home differs from {resolved_canonical!r}? "
            f"Links will be preserved (orphaned).",
            abort=True,
        )

    writer = _cat_cmd._get_catalog_writer()
    deleted = 0
    for t_str in purge_targets:
        try:
            t = Tumbler.parse(t_str)
        except Exception as e:  # noqa: BLE001 — best-effort per-item; logged and skipped, must not abort batch
            click.echo(f"  skip {t_str}: parse error {e}")
            continue
        if writer.delete_document(t):
            deleted += 1
    click.echo(f"\nDeleted {deleted} of {len(purge_targets)} non-canonical entries.")


def _audit_membership_all(*, as_json: bool) -> None:
    """Sweep every physical_collection and emit a contamination summary.

    nexus-3e4s Phase 3 + critique-followup C2. Reads the catalog in a
    single pass and groups rows by (collection, home). Owner context is
    layered on top so single-home collections whose dominant home does
    not match the owning ``repo``'s ``repo_root`` are flagged as 100%
    contaminated rather than silently passing as "clean" — the failure
    mode that masked ~4,200 wrong-home rows in ``code__ART-...``
    pre-fix.
    """
    from nexus.commands import catalog as _cat_cmd  # noqa: PLC0415 — module-routed helper access keeps import acyclic + monkeypatch-visible
    cat = _cat_cmd._get_catalog()
    # nexus-xnz0o: replaced _db.execute with uniform catalog API.
    # all_documents() paginates through the full catalog 200 at a time.
    all_docs: list = []
    offset = 0
    while True:
        page = cat.all_documents(limit=200, offset=offset)
        if not page:
            break
        all_docs.extend(page)
        if len(page) < 200:
            break
        offset += 200
    rows = [
        (e.physical_collection, e.source_uri or "", str(e.tumbler))
        for e in all_docs
        if e.physical_collection
    ]

    owner_list = cat.list_owners()
    owners_by_prefix: dict[str, dict[str, str]] = {
        o["tumbler_prefix"]: {
            "owner_type": o.get("owner_type") or "",
            "repo_root":  o.get("repo_root") or "",
        }
        for o in owner_list
    }

    by_collection: dict[str, dict[str, int]] = {}
    collection_owners: dict[str, set[str]] = {}
    for collection, source_uri, tumbler_str in rows:
        home = _source_uri_home_key(source_uri or "")
        bucket = by_collection.setdefault(collection, {})
        bucket[home] = bucket.get(home, 0) + 1
        try:
            owner_prefix = str(Tumbler.parse(tumbler_str).owner_address())
            collection_owners.setdefault(collection, set()).add(owner_prefix)
        except Exception:  # noqa: BLE001 — best-effort fallback path; failure is non-fatal here
            pass

    records: list[dict] = []
    for collection, home_counts in by_collection.items():
        total = sum(home_counts.values())
        dominant = max(home_counts.items(), key=lambda kv: kv[1])[0]
        contaminated = total - home_counts[dominant]

        # Owner-aware overlay (nexus-3e4s C2): when the collection is
        # owned by exactly one ``repo`` owner with a known ``repo_root``,
        # check that the dominant home matches the owner's tree. A
        # mismatch flips the count to 100% — single-home + wrong-home
        # is the worst failure mode and otherwise reads as "clean".
        expected_root = ""
        wrong_home = False
        owner_prefixes = collection_owners.get(collection, set())
        if len(owner_prefixes) == 1:
            owner_info = owners_by_prefix.get(next(iter(owner_prefixes)))
            if (
                owner_info
                and owner_info["owner_type"] == "repo"
                and owner_info["repo_root"]
            ):
                expected_root = owner_info["repo_root"]
                if not _home_matches_root(dominant, expected_root):
                    contaminated = total
                    wrong_home = True

        records.append({
            "collection": collection,
            "total_entries": total,
            "distinct_homes": len(home_counts),
            "by_home": dict(home_counts),
            "dominant_home": dominant,
            "contaminated_entries": contaminated,
            "expected_home": expected_root,
            "wrong_home": wrong_home,
        })

    # Sort: contaminated count desc, then total desc, then name asc so
    # the worst offenders surface first and the order is stable.
    records.sort(key=lambda r: (
        -r["contaminated_entries"], -r["total_entries"], r["collection"],
    ))

    contaminated_count = sum(1 for r in records if r["contaminated_entries"] > 0)
    clean_count = len(records) - contaminated_count

    if as_json:
        click.echo(json.dumps({
            "total_collections": len(records),
            "contaminated_count": contaminated_count,
            "clean_count": clean_count,
            "collections": records,
        }, indent=2))
        return

    if not records:
        click.echo("0 collections in catalog.")
        return

    click.echo(
        f"Audited {len(records)} collections: "
        f"{contaminated_count} contaminated, {clean_count} clean.",
    )
    if contaminated_count == 0:
        click.echo("No contamination detected.")
        return

    click.echo()
    click.echo(f"Contaminated collections ({contaminated_count}):")
    for r in records:
        if r["contaminated_entries"] == 0:
            continue
        wrong_tag = " [wrong-home]" if r["wrong_home"] else ""
        expected_tag = (
            f"  expected={r['expected_home']}" if r["expected_home"] else ""
        )
        click.echo(
            f"  {r['contaminated_entries']:6d} of {r['total_entries']:6d}  "
            f"{r['collection']:40s}  "
            f"{r['distinct_homes']} homes  "
            f"dominant={r['dominant_home']}{expected_tag}{wrong_tag}",
        )

    if clean_count:
        click.echo()
        click.echo(f"Clean collections ({clean_count}):")
        for r in records:
            if r["contaminated_entries"] != 0:
                continue
            click.echo(
                f"  {r['total_entries']:6d}  {r['collection']}  "
                f"({r['dominant_home']})",
            )


def _home_matches_root(home: str, repo_root: str) -> bool:
    """Return True when ``home`` corresponds to the same project as ``repo_root``.

    ``home`` is the 4-segment prefix returned by ``_source_uri_home_key``
    for ``file://`` URIs; ``repo_root`` is the absolute path stored on
    the owner. They match when one is a prefix of the other (the home
    may be shallower than the root for nested repos, or deeper when the
    root itself sits at a non-standard depth).
    """
    if not home or not repo_root:
        return False
    real_home = os.path.realpath(home)
    real_root = os.path.realpath(repo_root)
    return real_home.startswith(real_root) or real_root.startswith(real_home)


_EMPTY_HOME_KEY = ""
_DEVONTHINK_HOME_KEY = "x-devonthink-item://"


def _source_uri_home_key(uri: str) -> str:
    """Stable grouping key for source_uri "home" detection.

    For ``file://`` URIs, returns the first four path segments
    (e.g. ``/Users/hal.hildebrand/git/ART``) so two entries from the
    same repo cluster regardless of the file inside that repo.

    For ``x-devonthink-item://`` URIs (RDR-099 DEVONthink integration),
    returns a fixed sentinel ``x-devonthink-item://`` so every UUID-
    netlocked DEVONthink reference collapses to ONE bucket. Pre-fix
    this returned ``<scheme>://<uuid>`` per chunk, making every
    DEVONthink import look like its own home. ``knowledge__art-
    grossberg-papers`` reported 110+ homes when it has at most 4
    logical roots; the audit's contamination signal was unreadable.

    Other schemes return ``<scheme>://<netloc>``.

    Empty URIs return :data:`_EMPTY_HOME_KEY` so the audit can
    distinguish "no source_uri" rows from real "single home" rows;
    callers that want a non-empty home count must filter the empty
    bucket explicitly (the contamination signal is "≥2 distinct
    NON-EMPTY homes", not just "≥2 distinct buckets"; a single self-
    marker row was previously enough to flip a small clean
    collection to "contaminated").

    Constants ``_EMPTY_HOME_KEY`` and ``_DEVONTHINK_HOME_KEY`` are
    exposed so consumers (audit-membership, doctor checks, tests)
    can pattern-match on them without re-implementing the literals.
    """
    from urllib.parse import urlparse  # noqa: PLC0415 — deferred import; rare/branch-local path or circular-dep / startup-cost avoidance

    if not uri:
        return _EMPTY_HOME_KEY
    p = urlparse(uri)
    if p.scheme == "file":
        # path = "/Users/hal.hildebrand/git/ART/docs/rdr/X.md"
        # parts = ["", "Users", "hal.hildebrand", "git", "ART", ...]
        # Take through the 5th component (the project root).
        parts = p.path.split("/")
        return "/".join(parts[:5]) if len(parts) >= 5 else p.path
    if p.scheme == "x-devonthink-item":
        # nexus-n3md: collapse all DEVONthink items to one bucket.
        # Per RDR-099 the UUID netloc is an opaque doc handle, not a
        # repo / namespace identifier; treating each as a distinct
        # home produced 110+ false-positive homes per collection.
        return _DEVONTHINK_HOME_KEY
    return f"{p.scheme}://{p.netloc}"


@click.command("verify")
@click.option(
    "--heal",
    is_flag=True,
    default=False,
    help="For each ghost, prompt to drop the tumbler or print the "
         "`nx store put` invocation that would repopulate it.",
)
@click.option(
    "--collection",
    "-c",
    default="",
    help="Restrict verification to a single physical_collection name.",
)
@click.option(
    "--json",
    "json_out",
    is_flag=True,
    default=False,
    help="Emit machine-readable JSON: {collection: [{tumbler, title, doc_id}]}.",
)
def verify_cmd(heal: bool, collection: str, json_out: bool) -> None:
    """Reconcile catalog tumblers against their T3 collection.

    Reports *ghost* tumblers — entries in the catalog with no matching
    row in ChromaDB. Ghosts most commonly survive from 4.9.7 / 4.9.8
    installs where an oversize `store_put` silently truncated before
    #244's guard landed. Fresh 4.9.9+ writes can no longer create new
    ghosts.

    The sweep is cheap: one `col.get(ids=[...], include=[])` per 300-id
    page — no ANN, no payload. Collections missing from T3 (deleted,
    renamed) are treated the same as missing ids.

    \b
    Examples:
      nx catalog verify                                  # full sweep
      nx catalog verify --collection knowledge__foo      # one collection
      nx catalog verify --heal                           # interactive fix
      nx catalog verify --json                           # CI-friendly output
    """
    import json as _json  # noqa: PLC0415 — deferred import; rare/branch-local path or circular-dep / startup-cost avoidance

    from nexus.commands import catalog as _cat_cmd  # noqa: PLC0415 — module-routed helper access keeps import acyclic + monkeypatch-visible
    cat = _cat_cmd._get_catalog()
    # nexus-xnz0o: replaced raw SQL with catalog API.
    # Fetch docs for a single collection or all distinct collections.
    if collection:
        coll_entries = cat.list_by_collection(collection)
        all_entries = [(e, collection) for e in coll_entries]
    else:
        # Paginate through all documents.
        all_entries = []
        offset = 0
        while True:
            page = cat.all_documents(limit=200, offset=offset)
            if not page:
                break
            for e in page:
                if e.physical_collection and not e.alias_of:
                    all_entries.append((e, e.physical_collection))
            if len(page) < 200:
                break
            offset += 200

    # Build rows: (tumbler, title, physical_collection, doc_id)
    # Only entries with a non-empty meta.doc_id are verifiable; entries
    # without doc_id are silently skipped (same semantics as original SQL
    # ``WHERE metadata->>'doc_id' != ''``).
    rows = [
        (str(e.tumbler), e.title, coll, e.meta.get("doc_id"))
        for e, coll in all_entries
        if not e.alias_of and coll and e.meta.get("doc_id")
    ]

    if not rows:
        if collection:
            click.echo(f"No catalog tumblers with doc_id in {collection}.")
        else:
            click.echo("No catalog tumblers with doc_id metadata — nothing to verify.")
        return

    # Group by physical_collection → list[(tumbler, title, doc_id)]
    by_collection: dict[str, list[tuple[str, str, str]]] = {}
    for tumbler_str, title, coll, doc_id in rows:
        by_collection.setdefault(coll, []).append((tumbler_str, title, doc_id))

    total_tumblers = sum(len(v) for v in by_collection.values())
    if not json_out:
        click.echo(
            f"Verifying {total_tumblers} catalog tumbler(s) across "
            f"{len(by_collection)} collection(s)..."
        )

    t3 = _cat_cmd._make_t3()
    ghosts_by_collection: dict[str, list[dict]] = {}
    for coll, tumblers in sorted(by_collection.items()):
        expected_ids = [doc_id for _, _, doc_id in tumblers]
        present = t3.existing_ids(coll, expected_ids)
        ghosts = [
            {"tumbler": t, "title": title, "doc_id": doc_id}
            for t, title, doc_id in tumblers
            if doc_id not in present
        ]
        if ghosts:
            ghosts_by_collection[coll] = ghosts

    if json_out:
        click.echo(_json.dumps(ghosts_by_collection, indent=2))
        return

    total_ghosts = sum(len(v) for v in ghosts_by_collection.values())
    if not ghosts_by_collection:
        click.echo(f"Summary: 0 ghosts / {total_tumblers} tumblers. All good.")
        return

    for coll, ghosts in sorted(ghosts_by_collection.items()):
        click.echo(f"  {coll}: {len(ghosts)} ghost(s) found")
        for g in ghosts:
            click.echo(f"    {g['tumbler']:<12} {g['title']}  (doc_id {g['doc_id']})")

    pct = (total_ghosts * 100.0) / max(total_tumblers, 1)
    click.echo(
        f"Summary: {total_ghosts} ghosts / {total_tumblers} tumblers ({pct:.1f}%)."
    )
    if not heal:
        click.echo("Run with --heal for remediation options.")
        return

    writer = _cat_cmd._get_catalog_writer()
    try:
        _heal_ghosts(cat, ghosts_by_collection, writer=writer)
    finally:
        writer.close()


def _heal_ghosts(
    cat: Catalog,
    ghosts_by_collection: dict[str, list[dict]],
    *,
    writer: object = None,
) -> None:
    """Interactive heal loop for `nx catalog verify --heal`.

    Per ghost, prompt for one of:
      d  drop the tumbler (catalog.delete_document)
      p  print the `nx store put` invocation that would repopulate it
      s  skip
      q  quit the heal loop
    """
    w = writer if writer is not None else cat
    dropped = 0
    for coll, ghosts in sorted(ghosts_by_collection.items()):
        click.echo(f"\nHealing {coll}:")
        for g in ghosts:
            click.echo(f"  {g['tumbler']} — {g['title']} (doc_id {g['doc_id']})")
            choice = click.prompt(
                "    [d]rop tumbler / [p]rint put cmd / [s]kip / [q]uit",
                default="s",
                show_default=False,
            ).strip().lower()
            if choice == "q":
                click.echo(f"\nHealed: {dropped} tumbler(s) dropped.")
                return
            if choice == "d":
                if w.delete_document(Tumbler.parse(g["tumbler"])):
                    dropped += 1
                    click.echo("    dropped.")
                else:
                    click.echo("    already gone.")
            elif choice == "p":
                # The put command needs the original content. We emit a
                # template so the user can paste their source material.
                click.echo(
                    f"    nx store put --collection {coll} "
                    f"--title {g['title']!r} < source.md"
                )
            # anything else = skip
    click.echo(f"\nHealed: {dropped} tumbler(s) dropped.")


def register(group: click.Group) -> None:
    """Attach the integrity/verification commands to the shared ``catalog`` group."""
    group.add_command(audit_membership_cmd)
    group.add_command(verify_cmd)
