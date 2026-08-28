# SPDX-License-Identifier: AGPL-3.0-or-later
# Copyright (c) 2026 Hal Hildebrand. All rights reserved.
"""Catalog integrity / verification commands for the ``nx catalog`` group (nexus-whh61.4).

Carved out of ``commands.catalog``: ``audit-membership`` (audit owner/collection
membership consistency) and ``verify`` (reconcile catalog vs T3 on the
RDR-108/180 chash identity — vanished collections / damaged manifests / lost
documents — with ``--collection``-scoped healing),
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
from pathlib import Path
from typing import TYPE_CHECKING

import click
import structlog

from nexus.catalog.tumbler import Tumbler
from nexus.classifier import looks_like_binary_content

_log = structlog.get_logger(__name__)

# nexus-xeux8: bounded sample of ghost-document tumblers in the census
# section (below) — mirrors reconcile_stale.py's _CAP_ACTION/_CAP_INFO
# convention of never dumping an unbounded list into --json output.
_CAP_GHOST_SAMPLE = 20

if TYPE_CHECKING:
    from nexus.catalog.catalog_protocol import CatalogReader  # noqa: F401 — used in _heal_ghosts annotation (PEP 563 deferred)


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


def _exit_incomplete_if_unreadable(unreadable: list[str]) -> None:
    """Fail the command when part of the store could not be verified.

    nexus-ou4tb / nexus-sj4a3. A finding (vanished collection, damaged
    manifest, lost document) is a SUCCESSFUL verification with findings and
    exits 1 through the separate ``click.exceptions.Exit(1)`` path. A check
    or collection that could not be read AT ALL is a different thing —
    INCOMPLETE — and a script that only parses the JSON findings would
    otherwise read "nothing found" as "clean". The exit code (via
    ``ClickException``, distinct from ``Exit(1)``) is the one channel that
    carries this without changing the documented stdout shape.
    """
    if unreadable:
        raise click.ClickException(
            f"verification INCOMPLETE: {len(unreadable)} check(s)/collection(s) "
            f"could not be read ({', '.join(sorted(unreadable))}). The results "
            "above cover only what could be probed."
        )


def _class_a_vanished_collections(
    cat: "CatalogReader", t3: object, unreadable: list[str],
) -> tuple[list[dict], set[str]]:
    """Class A: catalog collections with docs but absent from T3 entirely.

    Mirrors ``doctor.py``'s ``_run_t3_vs_catalog`` predicate: a physical
    collection the catalog still has documents for, that T3 no longer
    knows about at all (deleted, renamed out from under the catalog).
    Bypass-schema collections (``taxonomy__*``) are excluded, same as the
    doctor check.

    nexus-sj4a3 code-review CRITICAL (false-vanished storm):
    ``HttpVectorClient.list_collections()`` catches any non-404
    ``VectorServiceError`` (e.g. a 500/503/timeout from
    ``/v1/vectors/stats``) and returns ``[]`` instead of raising — the
    ``except Exception`` guard below never fires for that case. An empty
    ``t3_names`` against a catalog that still has populated (non-bypass)
    collections is therefore INDISTINGUISHABLE from that swallowed error
    (and from a genuinely-empty T3 — a fresh or wiped store). Reporting
    "every collection vanished" for either is exactly the "confident wrong
    verdict" class this rewrite exists to kill, just in the false-DIRTY
    direction instead of false-clean, so this ambiguity is folded into
    *unreadable* (INCOMPLETE) instead. ``list_collections`` itself is NOT
    modified — it is a 57-caller primitive whose other callers depend on
    the empty-list-on-error contract.

    Both catalog and T3 reads are exception-isolated (code-review
    IMPORTANT): a mid-sweep failure must not propagate an unhandled
    traceback out of ``verify``.

    Returns ``(vanished, vanished_names)`` — the report rows and the bare
    name set (so callers can exclude these docs from the Class C census
    without double-reporting them; Class B, formerly excluded the same way,
    is retired — RDR-191 Phase 6, nexus-o8dil.33).
    """
    from nexus.db.t3 import _BYPASS_SCHEMA_PREFIXES  # noqa: PLC0415 — deferred import; rare/branch-local path or circular-dep / startup-cost avoidance

    try:
        doc_counts = cat.collection_doc_counts()
    except Exception as exc:  # noqa: BLE001 — isolated: reported, not swallowed
        unreadable.append("catalog:collection_doc_counts")
        _log.warning("catalog_verify_collection_doc_counts_failed", error=str(exc))
        return [], set()

    try:
        t3_names = {c["name"] for c in t3.list_collections()}
    except Exception as exc:  # noqa: BLE001 — isolated: reported, not swallowed
        unreadable.append("t3:list_collections")
        _log.warning("catalog_verify_t3_list_collections_failed", error=str(exc))
        return [], set()

    populated = {
        coll: count for coll, count in doc_counts.items()
        if count > 0 and not coll.startswith(_BYPASS_SCHEMA_PREFIXES)
    }

    if not t3_names and populated:
        # A swallowed non-404 VectorServiceError (list_collections() -> [])
        # and a genuinely-empty T3 under a populated catalog look IDENTICAL
        # from here — neither can be told apart from this call alone, and
        # both deserve INCOMPLETE, never "N vanished collections".
        unreadable.append("t3:list_collections:empty-with-populated-catalog")
        _log.warning(
            "catalog_verify_t3_empty_with_populated_catalog",
            populated_collections=len(populated),
        )
        return [], set()

    vanished: list[dict] = []
    vanished_names: set[str] = set()
    for coll, count in sorted(populated.items()):
        if coll not in t3_names:
            vanished.append({"physical_collection": coll, "doc_count": int(count)})
            vanished_names.add(coll)
    return vanished, vanished_names


    # RDR-191 Phase 6 (nexus-o8dil.33), 2026-08-15: Class B (per-collection
    # manifest damage via manifest_verify_all's engine-side anti-join) is
    # RETIRED here. This was NOT on Decision item 4's explicit list — it is
    # a separate consumer of manifest_verify_all discovered while retiring
    # that function for health.py's _check_dangling_manifests — but the
    # underlying defect class is identical: the manifest-chunk FK now
    # guarantees any manifest row that exists already references a real
    # chunk, so manifest_verify_all's `missing` is provably always 0 and
    # Class B could never find damage again even if left in place.
    # _class_c_unverifiable_rows (below) is retired in the SAME change: its
    # entire purpose was cross-checking Class C's client-side manifest-row
    # totals against Class B's engine-side `referenced` counts, which no
    # longer exist. Full-mode `damaged` is now always `[]`; --collection
    # scoped mode (_verify_scoped) is UNAFFECTED — it computes damage via
    # get_manifests + t3.existing_ids directly, never manifest_verify_all.


def _classify_never_chunked(e: object, owner_roots: dict[str, str] | None = None) -> str:
    """Classify a zero-chunk, no-manifest doc for the ``never_chunked`` report.

    nexus-sj4a3 substantive critique CRIT-1: RDR-145's "legitimate by
    design" framing scopes the manifest-less ``store_put`` exemption to
    ``knowledge__*`` documents with no indexable ``file_path``/
    ``source_uri`` — an MCP-authored note has neither. Everything else
    with ``chunk_count == 0`` and no manifest (dominantly ``code__*``/
    ``docs__*``/``rdr__*`` docs indexed via ``nx index repo``, per
    nexus-cdypx's own production evidence — code__1-20 alone: 2,286)
    is ``unclassified``: a candidate data-loss population RDR-145 says
    nothing about, UNLESS *owner_roots* is given and the source resolves
    to a verifiably unchunkable file (nexus-rqsh1) — see
    ``zero_content_by_design`` below.

    ``owner_roots`` (``cat.owners_with_roots()``) is optional and defaults
    to ``None``, which skips zero-content-by-design detection entirely
    (no filesystem resolution attempted) — callers that don't have it, or
    don't want the filesystem probe, get the pre-rqsh1 two-way split
    unchanged.

    zero_content_by_design -- the source resolves (via the SAME provenance
    rules ``reconcile_stale._resolve_provenance`` uses) to a file that
    EXISTS but is verifiably unchunkable by construction: zero bytes, or
    binary content (``_is_zero_content_by_design``). Re-indexing such a
    doc can never produce a chunk — it must never be prescribed a re-index
    (nexus-rqsh1: 11 of nexus owner 1-1's 12 "gapped" docs were exactly
    this, reappearing in every census forever since re-index is not a
    valid remedy for them).

    ``rdr145_exempt`` population is LEGACY-ONLY going forward (nexus-sdp0u):
    since that fix, every non-empty-title ``store_put`` / ``memory promote``
    document is registered with a synthesized ``chroma://<collection>/
    <title>`` ``source_uri``, so a post-fix document can no longer satisfy
    ``not e.source_uri`` here — it is deliberately left to fall through to
    ``unclassified`` (and, in ``nx catalog reconcile-stale``, to
    ``unresolvable_provenance``/``source_uri_only``) instead. This is
    intentional, not a predicate that needs forking: manifests are written
    synchronously at put time and a write failure surfaces loudly (see
    ``commands/store.py``'s "stored but NOT cataloged" error path), so a
    post-fix ``store_put`` doc that is STILL ``chunk_count == 0`` is
    anomalous and worth investigating, not something to wave through as
    "legitimate by design".
    """
    if (
        e.physical_collection.startswith("knowledge__")
        and not e.file_path
        and not e.source_uri
    ):
        return "rdr145_exempt"
    if owner_roots is not None and _resolves_to_zero_content_by_design(e, owner_roots):
        return "zero_content_by_design"
    return "unclassified"


def _is_zero_content_by_design(path: Path) -> bool:
    """True when *path* is a verifiably unchunkable source: zero bytes, or
    binary content per :func:`nexus.classifier.looks_like_binary_content`
    (nexus-rqsh1) -- the same sniff the indexer's producer-side fix
    (7733b48a9) uses to skip registering a catalog document in the first
    place, reused here rather than forked so the two never drift.

    A doc pointing at either can NEVER produce a chunk no matter how many
    times it is re-indexed -- unlike ``reindex_candidate``/``unclassified``,
    where re-index is at least a plausible remedy. Caller must confirm
    ``path.exists()`` first; a missing source is a different (orphaned-path)
    population and this function never guesses its way into that case.
    """
    try:
        size = path.stat().st_size
    except OSError:
        return False
    if size == 0:
        return True
    return looks_like_binary_content(path)


def _resolves_to_zero_content_by_design(e: object, owner_roots: dict[str, str]) -> bool:
    """Resolve *e*'s on-disk source (same provenance rules
    ``reconcile_stale._resolve_provenance`` uses) and, only when it
    resolves to a CONFIRMED-existing file, apply
    :func:`_is_zero_content_by_design`.

    Deferred import (not a module-level one): ``reconcile_stale.py``
    already imports predicates FROM this module at module scope (see this
    module's own docstring); importing back at module scope would cycle.
    """
    from nexus.commands.catalog_cmds.reconcile_stale import (  # noqa: PLC0415 — see docstring
        _resolve_provenance,
    )
    resolved, reason = _resolve_provenance(e, owner_roots)
    if reason or resolved is None or not resolved.exists():
        return False
    return _is_zero_content_by_design(resolved)


def _owner_roots_for_zero_content_check(cat: "CatalogReader", never_chunked_entries: list) -> dict[str, str]:
    """Fetch ``owners_with_roots()`` for the zero-content-by-design filesystem
    probe, but only when there's actually a never-chunked doc to disambiguate
    (verify's "cheap, CI-gateable" design, nexus-sj4a3 — no owner-root round
    trip when the census has nothing to resolve).

    Exception-isolated (not a hard requirement of ``CatalogReader``): a
    reader/test-double that doesn't support it degrades to no resolution
    (every entry stays ``unclassified``, the pre-rqsh1 behavior) rather than
    failing the whole verify sweep over a best-effort filesystem probe.
    """
    if not never_chunked_entries:
        return {}
    try:
        return cat.owners_with_roots()
    except Exception as exc:  # noqa: BLE001 — best-effort only, see docstring
        _log.debug("catalog_verify_owners_with_roots_unavailable", error=str(exc))
        return {}


# nexus-0y0gk critique fix-round (observation item, integrity.py side):
# rdr145_exempt is "legitimate by design", not "definitely unrepairable" —
# chunk_count is a CACHED value (see zero_count_recount in
# catalog_cmds.reconcile_stale, whose whole existence is proof this cache
# can be stale), so an rdr145_exempt doc's T3 chunk state cannot be read
# off this classification alone. This note names the actual authority
# without claiming FK-safety here or touching verify's exit code.
_RDR145_EXEMPT_NOTE = (
    "Legitimate store_put note by design (RDR-145) -- NOT a claim that "
    "these are unrepairable. chunk_count is a cached value and can be "
    "stale; `nx t3 backfill-manifest --dry-run --only-gapped` is the "
    "authority for whether a given doc in this population is actually a "
    "manifest-only backfill candidate. This note does not change verify's "
    "exit code."
)

# nexus-rqsh1: an HONEST bucket, not a suppression -- these docs stay in
# the census forever until they are actually tombstoned (killed the
# nexus-cotmr round-1 exemption error, which hid a population like this
# from the count entirely).
_ZERO_CONTENT_BY_DESIGN_NOTE = (
    "Zero-byte or binary source -- will NEVER chunk, no matter how many "
    "times it is re-indexed. The producer no longer registers new catalog "
    "documents for these (commit 7733b48a9); this population is the "
    "pre-fix residue. Remedy is `nx catalog reconcile-stale --execute "
    "tombstone-zero-content` (dry-run by default), never re-index. This "
    "note does not change verify's exit code."
)

# nexus-rqsh1: capped sample of zero_content_by_design tumblers, same
# convention as the ghost-document census sample above.
_CAP_ZERO_CONTENT_SAMPLE = _CAP_GHOST_SAMPLE


def note_chunks_present(t3, collection: str, chash: str, title: str) -> tuple[bool, str]:
    """Does a store_put note have ANY chunk in T3? Returns ``(present, verified_by)``.

    nexus-1uekf: THE ONE LOOKUP both integrity instruments use, so they can
    never again disagree about a note by having looked at different things.
    Two keys, in order:

    * ``chash`` (``meta.doc_id``, the chunk natural id) -- exact, cheap;
    * ``title`` -- the key a knowledge note's chunks carry. A note re-stored
      under a NEW chash keeps its title (the nexus-5axey dedup shape), so a
      chash miss alone is not "content is gone"; only a miss on both is.

    ``verified_by`` names which key(s) were actually consulted, so a report
    can say HOW a verdict was reached rather than merely that it was.
    Raises whatever the T3 client raises: a transient failure must surface
    as UNVERIFIABLE at the caller, never as a ghost.
    """
    if chash:
        if t3.get_by_id(collection, chash) is not None:
            return True, "chash"
        if not title:
            return False, "chash"
        return bool(t3.find_ids_by_title(collection, title)), "chash+title"
    if title:
        return bool(t3.find_ids_by_title(collection, title)), "title"
    return False, "no_identity"


def _never_chunked_breakdown(
    entries: list, owner_roots: dict[str, str] | None = None, *, t3=None,
) -> dict:
    """Per-collection breakdown of never-chunked docs (nexus-sj4a3 CRIT-1).

    Replaces the old bare int: a reconciler cannot act on "N never-chunked
    docs" without knowing which collections they live in and which
    sub-class (RDR-145-exempt store_put note, zero_content_by_design, or
    unclassified candidate data loss) each falls into.

    *owner_roots* is threaded through to :func:`_classify_never_chunked` —
    ``None`` (the default) skips zero-content-by-design detection, same as
    that function's own default.

    *t3* (nexus-1uekf): given a T3 handle, every ``rdr145_exempt`` candidate
    is PROBED by title -- the one key a legacy store_put note's chunks
    carry -- and split: chunks present stays ``rdr145_exempt`` (a
    manifest-only gap, backfillable, the case RDR-145 actually described);
    no chunks becomes ``rdr145_ghost`` (nothing to backfill; the title is
    the only surviving record). Without a handle the shape class stands
    alone, exactly as before. The shape class rendered as "legitimate by
    design" for 226 rows on 2026-08-27 whose content was gone from T3 --
    the same 226 rows ``--store-put-integrity`` called ghosts -- so the two
    instruments disagreed about one population without either having
    looked at it. This is the lookup; the same lookup now backs both.
    """
    rdr145_counts: dict[str, int] = {}
    rdr145_ghost_counts: dict[str, int] = {}
    rdr145_ghost_tumblers: list[str] = []
    rdr145_unverified = 0
    zero_content_counts: dict[str, int] = {}
    zero_content_tumblers: list[str] = []
    unclassified_counts: dict[str, int] = {}
    for e in entries:
        cls = _classify_never_chunked(e, owner_roots)
        if cls == "rdr145_exempt":
            bucket = rdr145_counts
            if t3 is not None:
                chash = (getattr(e, "meta", None) or {}).get("doc_id", "") or ""
                try:
                    present, _ = note_chunks_present(
                        t3, e.physical_collection, chash, e.title or "",
                    )
                except Exception:  # noqa: BLE001 — a probe failure keeps the safer class; it is never a ghost verdict
                    rdr145_unverified += 1
                else:
                    if not present:
                        bucket = rdr145_ghost_counts
                        rdr145_ghost_tumblers.append(str(e.tumbler))
        elif cls == "zero_content_by_design":
            bucket = zero_content_counts
            zero_content_tumblers.append(str(e.tumbler))
        else:
            bucket = unclassified_counts
        bucket[e.physical_collection] = bucket.get(e.physical_collection, 0) + 1

    def _rows(counts: dict[str, int]) -> list[dict]:
        return [
            {"physical_collection": c, "count": n}
            for c, n in sorted(counts.items())
        ]

    rdr145_total = sum(rdr145_counts.values())
    rdr145_ghost_total = sum(rdr145_ghost_counts.values())
    zero_content_total = sum(zero_content_counts.values())
    unclassified_total = sum(unclassified_counts.values())
    return {
        "total": rdr145_total + rdr145_ghost_total + zero_content_total + unclassified_total,
        "rdr145_exempt": {
            "total": rdr145_total,
            "by_collection": _rows(rdr145_counts),
            "note": _RDR145_EXEMPT_NOTE,
        },
        "rdr145_ghost": {
            "total": rdr145_ghost_total,
            "by_collection": _rows(rdr145_ghost_counts),
            "tumblers": sorted(rdr145_ghost_tumblers),
            "note": (
                "store_put-origin notes with NO chunks in T3 under their title "
                "(nexus-1uekf): nothing to backfill -- the title is the only "
                "surviving record. Disposition (re-create or tombstone) is a "
                "human decision; `nx catalog doctor --store-put-integrity` "
                "reports the same population by the same lookup."
            ),
        },
        "rdr145_unverified": rdr145_unverified,
        "zero_content_by_design": {
            "total": zero_content_total,
            "by_collection": _rows(zero_content_counts),
            "sample_tumblers": sorted(zero_content_tumblers)[:_CAP_ZERO_CONTENT_SAMPLE],
            "note": _ZERO_CONTENT_BY_DESIGN_NOTE,
        },
        "unclassified": {"total": unclassified_total, "by_collection": _rows(unclassified_counts)},
    }


def _census_lost_and_never_chunked(
    cat: "CatalogReader", entries: list, unreadable: list[str], *, t3=None,
) -> tuple[list[dict], dict, dict[str, int]]:
    """Class C: chunk_count vs manifest-length census (manifest_heal.py semantics).

    ``lost``: chunk_count > 0 and the manifest has fewer rows than that
    (includes a wholly absent manifest) — a REAL gap.
    ``never_chunked``: chunk_count == 0 and no manifest, broken down by
    :func:`_never_chunked_breakdown` (nexus-sj4a3 critique CRIT-1) — the
    RDR-145 "legitimate by design" framing applies to the ``rdr145_exempt``
    sub-class only. Exit stays neutral for BOTH sub-classes; classifying
    ``unclassified`` further is nexus-cdypx's reconcile job, not verify's.

    Also returns ``manifest_row_totals`` — total manifest ROWS (not
    chunk_count) per COLLECTION, computed from this SAME ``get_manifests``
    call (no second round trip). Feeds :func:`_class_c_unverifiable_rows`
    (critique CRIT-2): ``get_manifests`` reads ``catalog_document_chunks``
    directly with no ``collection IS NOT NULL`` filter (confirmed against
    ``CatalogRepository.getManifestMany``), so this total includes
    pre-backfill (NULL-``collection``) rows that ``manifest_verify_all``
    silently excludes.

    Row-level keying (nexus-kzso5, RDR-191 follow-up to nexus-bo2d1):
    ``get_manifest``/``get_manifests`` now carry each row's OWN stamped
    ``collection`` on the wire (additive field, engine floor ``>=
    v0.1.74``) — the caller-supplied, NOT NULL truth RDR-191 stamps
    verbatim, independent of the owning document's ``physical_collection``.
    Each row is attributed to ``row.collection`` when the wire carries it;
    ``getattr(row, "collection", None)`` tolerates both a pre-field engine
    (``ManifestRow.collection is None``) and a bare test double with no
    such attribute, falling back to ``e.physical_collection`` — the exact
    pre-kzso5 behavior — only when the row itself carries no collection of
    its own. A ghost/sourceless document (``physical_collection == ""``)
    therefore no longer needs to be excluded from row attribution: on a
    field-aware engine its rows attribute correctly to whatever collection
    they were actually written into, resolving the nexus-bo2d1
    ghost-contamination INCOMPLETE hedge; on a pre-field engine the
    fallback still yields ``key=None`` (empty ``physical_collection``, no
    row collection) and the row is skipped exactly as before.

    Ghost entries are still EXCLUDED from ``lost``/``never_chunked``
    (document-health classification) — a ghost has no owning-collection
    identity for a chunk_count-vs-manifest comparison to be meaningful
    against, mirroring ``manifest_heal.py``'s identical skip
    (``catalog/manifest_heal.py`` ~161: ``if not entry.physical_collection:
    continue``) — verified SAFE by the nexus-bo2d1 audit. Callers decide
    whether to include ghosts in ``entries`` at all; ``_verify_full`` now
    does (nexus-kzso5), purely so their rows are visible for attribution.

    ``cat.get_manifests`` is exception-isolated (code-review IMPORTANT): a
    mid-sweep failure must still let the report render — with whatever
    Class A/B already computed — rather than propagate an unhandled
    traceback, especially under ``--json``.
    """
    if not entries:
        return [], _never_chunked_breakdown([]), {}

    try:
        manifests = cat.get_manifests([str(e.tumbler) for e in entries])
    except Exception as exc:  # noqa: BLE001 — isolated: reported, not swallowed
        unreadable.append("catalog:get_manifests")
        _log.warning("catalog_verify_get_manifests_failed", error=str(exc))
        return [], _never_chunked_breakdown([]), {}

    lost: list[dict] = []
    never_chunked_entries: list = []
    manifest_row_totals: dict[str, int] = {}
    for e in entries:
        rows = manifests.get(str(e.tumbler), [])
        # nexus-kzso5: row-own collection first, e.physical_collection as
        # the version-tolerant fallback — see docstring.
        for r in rows:
            key = getattr(r, "collection", None) or e.physical_collection or None
            if key:
                manifest_row_totals[key] = manifest_row_totals.get(key, 0) + 1
        if not e.physical_collection:
            # Ghost/sourceless doc — no per-document health classification
            # (see docstring); its rows were still attributed above.
            continue
        if e.chunk_count > 0 and len(rows) < e.chunk_count:
            lost.append({
                "tumbler": str(e.tumbler), "title": e.title,
                "collection": e.physical_collection,
                "chunk_count": e.chunk_count, "manifest_count": len(rows),
            })
        elif e.chunk_count == 0 and not rows:
            never_chunked_entries.append(e)
    owner_roots = _owner_roots_for_zero_content_check(cat, never_chunked_entries)
    # *t3* threads through to the rdr145 title probe (nexus-1uekf). This is
    # the FULL-catalog path's breakdown; the scoped path computes its own.
    return (
        lost,
        _never_chunked_breakdown(never_chunked_entries, owner_roots, t3=t3),
        manifest_row_totals,
    )


    # _class_c_unverifiable_rows RETIRED (RDR-191 Phase 6, nexus-o8dil.33) —
    # its entire purpose was cross-checking Class C's client-side
    # manifest-row totals against Class B's engine-side `referenced` counts
    # (manifest_verify_all), which no longer exist. See the Class B removal
    # note above _classify_never_chunked for the full rationale.


def _census_ghosts(ghost_entries: list) -> dict:
    """Read-only census of ghost documents (nexus-xeux8).

    Ghost documents — blank/NULL ``physical_collection`` — are excluded
    from BOTH this command's own health classification
    (``_census_lost_and_never_chunked`` skips them: no owning-collection
    identity for a chunk_count-vs-manifest comparison to be meaningful
    against) AND ``nx catalog reconcile-stale``'s candidate filter
    (``not e.alias_of and e.physical_collection``) — so nothing sizes this
    population today. This section is a pure READ-ONLY count; it changes
    nothing about what ``verify``/``reconcile-stale`` repair. A ghost has
    no collection to write a manifest into or re-chunk against, so it is
    UNREPAIRABLE without a manual ``physical_collection`` assignment (see
    the nexus-3n7pr remediation plan, Phase 1b/6).

    Computed from the SAME ``all_documents`` sweep ``_verify_full``
    already performs for Class A/C — deliberately NOT a second engine
    round trip. Membership is decided purely from an already-in-memory
    field (``physical_collection``); no manifest or chunk read is needed
    to count a ghost, so this stays cheap even when the surrounding
    ``get_manifests`` sweep is already minutes-scale over ~300k rows.

    ``by_owner`` buckets by the tumbler's 2-segment owner address
    (``Tumbler.parse(str(e.tumbler)).owner_address()``, best-effort — a
    malformed tumbler is counted under an ``"<unparseable>"`` bucket
    rather than raising). Tenant is NOT broken out: this client's reads
    are already scoped to one tenant by the request's own credentials
    (RLS), and ``CatalogEntry`` carries no per-row ``tenant_id`` to group
    by even if it were — a cross-tenant breakdown would need
    BYPASSRLS/server-side access this client does not have.
    """
    by_owner: dict[str, int] = {}
    for e in ghost_entries:
        try:
            owner = str(Tumbler.parse(str(e.tumbler)).owner_address())
        except Exception:  # noqa: BLE001 — best-effort bucketing only
            owner = "<unparseable>"
        by_owner[owner] = by_owner.get(owner, 0) + 1

    sample = sorted(str(e.tumbler) for e in ghost_entries)[:_CAP_GHOST_SAMPLE]

    return {
        "count": len(ghost_entries),
        "by_owner": [
            {"owner": o, "count": n}
            for o, n in sorted(by_owner.items(), key=lambda kv: (-kv[1], kv[0]))
        ],
        "by_tenant": {
            "available": False,
            "reason": (
                "catalog client reads are single-tenant scoped via RLS; "
                "CatalogEntry carries no per-row tenant_id to break out by"
            ),
        },
        "sample_tumblers": sample,
        "sample_truncated": len(ghost_entries) > _CAP_GHOST_SAMPLE,
        "note": (
            "Docs with blank/NULL physical_collection. Unrepairable without "
            "a manual physical_collection assignment; excluded from both "
            "verify's health classification and reconcile-stale's candidate "
            "set by design. Read-only census, not a repair candidate list."
        ),
    }


def _emit_verify_report(
    *,
    total_docs: int,
    vanished_collections: list[dict],
    damaged: list[dict],
    lost: list[dict],
    never_chunked: dict,
    unreadable: list[str],
    json_out: bool,
    exit_dirty: bool,
    mode: str,
    unverifiable_rows: list[dict] | None = None,
    ghosts: dict | None = None,
    orphan_collections: list[dict] | None = None,
    raise_on_dirty: bool = True,
) -> None:
    """Shared renderer for full-catalog and ``--collection``-scoped verify.

    ``--json``'s stdout shape is the CI contract (nexus-sj4a3): a
    ``summary`` block plus the finding lists. Deliberately BREAKS the
    pre-existing ``{collection: [{tumbler, title, doc_id}]}`` shape — that
    shape encoded the retired pre-RDR-108 identity this rewrite deletes.

    ``mode`` (``"full"`` | ``"scoped"``) drives two mode-specific
    semantics (nexus-sj4a3 substantive critique SIG-3). Full mode's
    ``damaged`` rows were COLLECTION-granular (one ``manifest_verify_all``
    round trip; no per-doc count existed) — RETIRED RDR-191 Phase 6
    (nexus-o8dil.33): full mode's ``damaged`` is now always ``[]``. Scoped
    mode's are per-DOCUMENT, unaffected — reusing one summary key
    (``damaged_docs``) for two different units was the original bug, so
    full mode's key is still ``damaged_collections`` (permanently 0). The
    clean-percent calculation follows the same split: scoped mode can
    safely subtract damaged docs (a real per-doc count); full mode cannot
    (there is no per-doc count to subtract) and prints an explicit caveat
    instead of silently overstating cleanliness.
    """
    unverifiable_rows = unverifiable_rows or []
    damaged_key = "damaged_docs" if mode == "scoped" else "damaged_collections"
    summary = {
        "docs": total_docs,
        "vanished_collections": len(vanished_collections),
        damaged_key: len(damaged),
        "lost_docs": len(lost),
        "never_chunked_docs": never_chunked["total"],
        "unreadable_collections": len(unreadable),
        "exit": 1 if exit_dirty else 0,
    }
    if unverifiable_rows:
        summary["unverifiable_collections"] = len(unverifiable_rows)
    if ghosts is not None:
        # nexus-xeux8: count-only in the summary — never rolled into
        # `exit` or any clean-percent math (see the docstring above and
        # _census_ghosts): a ghost is a census entry, never a finding.
        summary["ghost_docs"] = ghosts["count"]
    if orphan_collections is not None:
        # nexus-8tnz2: same treatment as ghosts — count-only, never rolled
        # into `exit` or clean-percent. This is a T3-side finding (a whole
        # collection, not a catalog document) that ``nx catalog
        # reconcile-stale --execute drop-orphan-collections`` acts on;
        # ``nx catalog verify`` surfaces it purely so S5 stops hand-
        # counting `nx catalog doctor --t3-vs-catalog` output.
        summary["orphan_collections"] = len(orphan_collections)

    if json_out:
        payload = {
            "summary": summary,
            "vanished_collections": vanished_collections,
            "damaged": damaged,
            "lost": lost,
            "never_chunked": never_chunked,
            "unreadable": unreadable,
        }
        if unverifiable_rows:
            payload["unverifiable_rows"] = unverifiable_rows
        if ghosts is not None:
            payload["ghosts"] = ghosts
        if orphan_collections is not None:
            payload["orphan_collections"] = orphan_collections
        click.echo(json.dumps(payload, indent=2))
    else:
        click.echo(f"Verified {total_docs} catalog document(s).")
        if vanished_collections:
            click.echo(f"\nVanished collections ({len(vanished_collections)}):")
            for v in vanished_collections:
                click.echo(
                    f"  {v['physical_collection']}: {v['doc_count']} doc(s) — "
                    "collection absent from T3"
                )
        if damaged:
            click.echo(f"\nDamaged manifest(s) ({len(damaged)}):")
            for d in damaged:
                if "tumbler" in d:
                    click.echo(
                        f"  {d['tumbler']:<12} {d['title']}  "
                        f"({d['missing']} of {d['referenced']} chash(es) missing)"
                    )
                else:
                    click.echo(
                        f"  {d['collection']}: {d['missing']} of "
                        f"{d['referenced']} manifest chash(es) missing from T3"
                    )
        if lost:
            click.echo(f"\nLost document(s) ({len(lost)}):")
            for e in lost:
                click.echo(
                    f"  {e['tumbler']:<12} {e['title']}  ({e['collection']}, "
                    f"{e['manifest_count']}/{e['chunk_count']} chunks in manifest)"
                )
        if never_chunked["total"]:
            click.echo(
                f"\nNever-chunked: {never_chunked['total']} doc(s) (report-only):"
            )
            exempt = never_chunked["rdr145_exempt"]
            if exempt["total"]:
                click.echo(
                    f"  {exempt['total']} RDR-145 exempt (store_put-origin "
                    "notes with chunks in T3 but no manifest — legitimate by "
                    "design, backfillable):"
                )
                for row in exempt["by_collection"]:
                    click.echo(f"    {row['count']:5d}  {row['physical_collection']}")
            ghost_notes = never_chunked.get("rdr145_ghost", {"total": 0})
            if ghost_notes["total"]:
                click.echo(
                    f"  {ghost_notes['total']} store_put-origin note(s) with NO "
                    "content in T3 (ghosts — nothing to backfill; the title is "
                    "the only surviving record; see `nx catalog doctor "
                    "--store-put-integrity`):"
                )
                for row in ghost_notes["by_collection"]:
                    click.echo(f"    {row['count']:5d}  {row['physical_collection']}")
            if never_chunked.get("rdr145_unverified"):
                click.echo(
                    f"  {never_chunked['rdr145_unverified']} store_put-origin "
                    "note(s) could not be probed in T3 (counted as exempt, "
                    "not as ghosts)"
                )
            zero_content = never_chunked["zero_content_by_design"]
            if zero_content["total"]:
                click.echo(
                    f"  {zero_content['total']} zero-content-by-design — "
                    "will never chunk (zero-byte or binary source, "
                    "nexus-rqsh1); producer no longer registers these; "
                    "remedy is tombstone, not re-index:"
                )
                for row in zero_content["by_collection"]:
                    click.echo(f"    {row['count']:5d}  {row['physical_collection']}")
                if zero_content["sample_tumblers"]:
                    click.echo("    Sample tumblers:")
                    for t in zero_content["sample_tumblers"]:
                        click.echo(f"      {t}")
            unclassified = never_chunked["unclassified"]
            if unclassified["total"]:
                click.echo(
                    f"  {unclassified['total']} zero-chunk, unclassified — "
                    "candidate data loss, see nexus-cdypx:"
                )
                for row in unclassified["by_collection"]:
                    click.echo(f"    {row['count']:5d}  {row['physical_collection']}")
        if unverifiable_rows:
            click.echo(
                f"\nUnverifiable manifest rows ({len(unverifiable_rows)} "
                "collection(s) with pre-backfill rows the engine's anti-join "
                "excludes — true damage state unknown):"
            )
            for u in unverifiable_rows:
                click.echo(
                    f"  {u['collection']}: {u['unverifiable_rows']} "
                    "un-backfilled manifest row(s)"
                )
        if ghosts is not None and ghosts["count"]:
            click.echo(
                f"\nGhost documents ({ghosts['count']} — blank/NULL "
                "physical_collection; read-only census, UNREPAIRABLE "
                "without a manual collection assignment; never a finding, "
                "never affects exit code):"
            )
            click.echo("  By owner:")
            for row in ghosts["by_owner"][:_CAP_GHOST_SAMPLE]:
                click.echo(f"    {row['count']:5d}  {row['owner']}")
            click.echo("  Sample tumblers:")
            for t in ghosts["sample_tumblers"]:
                click.echo(f"    {t}")
            if ghosts["sample_truncated"]:
                click.echo(
                    f"    ... and {ghosts['count'] - len(ghosts['sample_tumblers'])} more"
                )
        if orphan_collections:
            click.echo(
                f"\nT3 orphan collections ({len(orphan_collections)} — chunks present "
                "in T3, zero catalog documents reference them; benchmark/gate debris "
                "candidate for `nx catalog reconcile-stale --execute "
                "drop-orphan-collections`; never affects exit code, see `nx catalog "
                "doctor --t3-vs-catalog` for the FAIL verdict):"
            )
            for o in orphan_collections[:_CAP_GHOST_SAMPLE]:
                if "error" in o:
                    click.echo(f"    {o['name']}  ERROR: {o['error']}")
                elif o.get("class") == "tombstoned-only":
                    click.echo(
                        f"    {o['name']}  chunks={o['chunk_count']}  "
                        f"TOMBSTONED-ONLY (tombstoned_docs={o['tombstoned_count']}, "
                        "restorable, NOT a drop-orphan-collections target)"
                    )
                else:
                    click.echo(f"    {o['name']}  chunks={o['chunk_count']}  class=orphan")
            if len(orphan_collections) > _CAP_GHOST_SAMPLE:
                click.echo(f"    ... and {len(orphan_collections) - _CAP_GHOST_SAMPLE} more")
        if unreadable:
            # nexus-ou4tb: a verify that could not read part of the store must
            # say so. Reporting a clean verdict over collections/checks it
            # never managed to probe is exactly the confident-but-blind
            # verdict this bead exists to kill. To STDERR, always — --json's
            # stdout is the documented machine-parseable CI contract.
            click.echo(
                f"WARNING: {len(unreadable)} check(s)/collection(s) could not be read and "
                f"were SKIPPED (not verified): {', '.join(sorted(unreadable))}",
                err=True,
            )
            click.echo(
                "The results above cover only what could be read — "
                "the skipped ones above are unverified."
            )

        vanished_doc_total = sum(v["doc_count"] for v in vanished_collections)
        bad_docs = len(lost) + vanished_doc_total
        if mode == "scoped":
            # Scoped mode's `damaged` rows are per-document, a real count
            # of bad docs to subtract. Full mode's are collection-granular
            # (see the mode-caveat printed below) and cannot be.
            bad_docs += len(damaged)
        clean_pct = ((total_docs - bad_docs) * 100.0 / total_docs) if total_docs else 100.0
        click.echo(
            f"\nSummary: {total_docs} doc(s) examined; {clean_pct:.1f}% clean. "
            f"{len(vanished_collections)} vanished collection(s) "
            f"({vanished_doc_total} doc(s)), {len(damaged)} damaged "
            f"{'document' if mode == 'scoped' else 'manifest'} report(s), "
            f"{len(lost)} lost doc(s), {never_chunked['total']} "
            "never-chunked (report-only)."
        )
        # The full-mode "damaged is COLLECTION-granular" caveat (formerly
        # printed here) is unreachable dead code now (RDR-191 Phase 6,
        # nexus-o8dil.33): full mode's `damaged` is always [] since Class B
        # (manifest_verify_all) is retired — removed rather than left as a
        # branch that can never fire. --collection scoped mode still gives
        # real per-document damage detail, unaffected.
        if not exit_dirty and not unreadable:
            click.echo("All good.")

    _exit_incomplete_if_unreadable(unreadable)
    if exit_dirty and raise_on_dirty:
        raise click.exceptions.Exit(1)


def _verify_full(cat: "CatalogReader", *, json_out: bool) -> None:
    """Full-catalog default mode: cheap, CI-gateable (nexus-sj4a3).

    Class A (vanished collections) + Class C (chunk_count vs manifest
    census) — see module docstring / design memo (T1 scratch 7afd8b6f) for
    the full rationale. Docs whose ``physical_collection`` is a Class-A
    vanished collection are counted under Class A only, never
    double-reported as Class C ``lost``.

    RDR-191 Phase 6 (nexus-o8dil.33), 2026-08-15: Class B (damaged
    manifests via one ``manifest_verify_all`` engine-side round trip) and
    its cross-check against Class C's ``manifest_row_totals`` (critique
    CRIT-2, pre-backfill rows the engine's anti-join silently excluded) are
    RETIRED — both functions ``_class_b_damaged_collections`` and
    ``_class_c_unverifiable_rows`` deleted. The manifest-chunk FK makes the
    dangling state Class B detected unreachable by construction; the
    cross-check's entire input (Class B's ``referenced_by_collection``) no
    longer exists. Full mode's ``damaged``/``unverifiable_rows`` are now
    always ``[]``. ``manifest_row_totals`` is still computed by
    ``_census_lost_and_never_chunked`` (Class C's own core census) but is
    otherwise unconsumed.

    nexus-kzso5 (RDR-191 follow-up to nexus-bo2d1): ghost/sourceless docs
    (``physical_collection == ""``) are still excluded from ``all_entries``
    / ``total_docs`` — they have no owning-collection identity for the
    doc-count or ``lost``/``never_chunked`` stats to mean anything — but
    are now ALSO fetched (``ghost_entries``, merged into the census-only
    entry list) purely so a field-aware engine's per-row ``collection``
    lets their manifest rows attribute to whatever REAL collection they
    were actually written into, instead of being invisible to
    ``manifest_row_totals`` entirely. ``_census_lost_and_never_chunked``
    itself still skips ghosts for lost/never_chunked classification.
    """
    from nexus.commands import catalog as _cat_cmd  # noqa: PLC0415 — module-routed helper access keeps import acyclic + monkeypatch-visible

    all_documents = [e for e in cat.all_documents(limit=0) if not e.alias_of]
    all_entries = [e for e in all_documents if e.physical_collection]
    ghost_entries = [e for e in all_documents if not e.physical_collection]
    total_docs = len(all_entries)

    unreadable: list[str] = []
    vanished_collections: list[dict] = []
    vanished_names: set[str] = set()
    damaged: list[dict] = []
    lost: list[dict] = []
    never_chunked = _never_chunked_breakdown([])
    unverifiable_rows: list[dict] = []

    t3 = None
    if all_entries:
        t3 = _cat_cmd._make_t3()
        vanished_collections, vanished_names = _class_a_vanished_collections(cat, t3, unreadable)
        # Class B (damaged, via manifest_verify_all) RETIRED here (RDR-191
        # Phase 6, nexus-o8dil.33) — see the removal note above
        # _classify_never_chunked. `damaged` stays [] permanently in full
        # mode; --collection scoped mode still computes real per-document
        # damage independently (_verify_scoped, unaffected).
        census_entries = [
            e for e in all_entries if e.physical_collection not in vanished_names
        ]
        # nexus-kzso5: ghosts ride along for row-collection attribution
        # only (see docstring) — _census_lost_and_never_chunked still
        # excludes them from lost/never_chunked. `manifest_row_totals` is
        # otherwise unconsumed now that _class_c_unverifiable_rows (its
        # only consumer) is retired alongside Class B.
        lost, never_chunked, _manifest_row_totals = _census_lost_and_never_chunked(
            cat, census_entries + ghost_entries, unreadable, t3=t3,
        )

    # nexus-xeux8: ghost census — from the all_documents() sweep already
    # fetched above, no additional engine round trip. Never affects
    # exit_dirty; see _census_ghosts docstring.
    ghosts = _census_ghosts(ghost_entries)

    # nexus-8tnz2: T3 orphan collections — shared classification with
    # `nx catalog doctor --t3-vs-catalog` and the reconcile-stale sweep
    # arm (classify_t3_orphan_collections is the ONE definition all three
    # consume). Global-catalog concept, computed regardless of whether
    # this run found any catalog documents at all (an empty catalog with
    # a populated T3 is exactly the orphan case). A classification failure
    # is reported via `unreadable`, never silently treated as "no
    # orphans" (the nexus-pyv0e false-PASS class).
    from nexus.commands.catalog_cmds.t3_orphans import classify_t3_orphan_collections  # noqa: PLC0415 — command-local import
    orphan_collections: list[dict] = []
    try:
        t3_for_orphans = t3 if t3 is not None else _cat_cmd._make_t3()
        orphan_collections = classify_t3_orphan_collections(cat, t3_for_orphans)
    except Exception as exc:  # noqa: BLE001 — boundary catch; reported via `unreadable`, not swallowed
        unreadable.append("t3:orphan_collections")
        _log.warning("catalog_verify_orphan_collections_failed", error=str(exc))

    exit_dirty = bool(vanished_collections or damaged or lost)
    _emit_verify_report(
        total_docs=total_docs,
        vanished_collections=vanished_collections,
        damaged=damaged,
        lost=lost,
        never_chunked=never_chunked,
        unreadable=unreadable,
        json_out=json_out,
        exit_dirty=exit_dirty,
        mode="full",
        unverifiable_rows=unverifiable_rows,
        ghosts=ghosts,
        orphan_collections=orphan_collections,
    )


def _verify_scoped(cat: "CatalogReader", collection: str, *, heal: bool, json_out: bool) -> None:
    """``--collection``-scoped mode: per-document detail (nexus-sj4a3).

    Per doc: manifest via ``get_manifests``, T3 presence via
    ``existing_ids`` (batched — the ou4tb fail-loud probe; never
    ``HttpVectorCollection.get`` with its default ``limit=10``, nexus-hdx2u).
    ``damaged`` = manifest-referenced chashes missing from T3 (per-doc
    detail; full mode has no per-doc damage detection at all since Class B
    was retired, RDR-191 Phase 6 nexus-o8dil.33 — this scoped path is now
    the ONLY damage-detection surface remaining in this command, computed
    entirely client-side via ``get_manifests``/``existing_ids``, independent
    of ``manifest_verify``/``manifest_verify_all``); ``lost``/``never_chunked``
    are the same chunk_count-vs-manifest census as full mode, scoped to this
    collection.

    ``cat.get_manifests`` is exception-isolated (code-review IMPORTANT):
    a failure must still render the (empty-detail) report rather than
    propagate an unhandled traceback, especially under ``--json``.
    """
    from nexus.commands import catalog as _cat_cmd  # noqa: PLC0415 — module-routed helper access keeps import acyclic + monkeypatch-visible

    entries = [e for e in cat.list_by_collection(collection) if not e.alias_of]
    total_docs = len(entries)

    if not entries:
        _emit_verify_report(
            total_docs=0, vanished_collections=[], damaged=[], lost=[],
            never_chunked=_never_chunked_breakdown([]), unreadable=[], json_out=json_out,
            exit_dirty=False, mode="scoped",
        )
        return

    unreadable: list[str] = []
    try:
        manifests = cat.get_manifests([str(e.tumbler) for e in entries])
    except Exception as exc:  # noqa: BLE001 — isolated: reported, not swallowed
        unreadable.append("catalog:get_manifests")
        _log.warning(
            "catalog_verify_get_manifests_failed", collection=collection, error=str(exc),
        )
        _emit_verify_report(
            total_docs=total_docs, vanished_collections=[], damaged=[], lost=[],
            never_chunked=_never_chunked_breakdown([]), unreadable=unreadable,
            json_out=json_out, exit_dirty=False, mode="scoped",
            raise_on_dirty=not heal,
        )
        return

    all_chashes = sorted({
        row.chash
        for e in entries
        for row in manifests.get(str(e.tumbler), [])
        if row.chash
    })
    present: set[str] = set()
    if all_chashes:
        t3 = _cat_cmd._make_t3()
        # nexus-ou4tb: existing_ids raises now instead of returning the empty
        # set — a degraded service must not be able to produce a clean-
        # looking integrity report. Isolated to this one collection: the
        # scoped sweep still reports the parts of its own detail that don't
        # depend on the T3 probe (lost/never_chunked below).
        try:
            present = t3.existing_ids(collection, all_chashes)
        except Exception as exc:  # noqa: BLE001 — per-collection isolation; reported, not swallowed
            unreadable.append(collection)
            _log.warning(
                "catalog_verify_collection_unreadable",
                collection=collection, expected=len(all_chashes), error=str(exc),
            )

    damaged: list[dict] = []
    lost: list[dict] = []
    never_chunked_entries: list = []
    if collection not in unreadable:
        for e in entries:
            rows = manifests.get(str(e.tumbler), [])
            manifest_chashes = [r.chash for r in rows if r.chash]
            missing_chashes = [c for c in manifest_chashes if c not in present]
            if missing_chashes:
                damaged.append({
                    "tumbler": str(e.tumbler), "title": e.title,
                    "referenced": len(manifest_chashes),
                    "missing": len(missing_chashes),
                })
            if e.chunk_count > 0 and len(rows) < e.chunk_count:
                lost.append({
                    "tumbler": str(e.tumbler), "title": e.title,
                    "collection": collection,
                    "chunk_count": e.chunk_count, "manifest_count": len(rows),
                })
            elif e.chunk_count == 0 and not rows:
                never_chunked_entries.append(e)

    owner_roots = _owner_roots_for_zero_content_check(cat, never_chunked_entries)
    never_chunked = _never_chunked_breakdown(never_chunked_entries, owner_roots, t3=t3)
    exit_dirty = bool(damaged or lost)
    _emit_verify_report(
        total_docs=total_docs,
        vanished_collections=[],
        damaged=damaged,
        lost=lost,
        never_chunked=never_chunked,
        unreadable=unreadable,
        json_out=json_out,
        exit_dirty=exit_dirty,
        mode="scoped",
        # A successful interactive heal is the corrective action itself;
        # skip the hard Exit(1) so a clean heal session reports success.
        raise_on_dirty=not heal,
    )

    if heal and damaged and collection not in unreadable:
        writer = _cat_cmd._get_catalog_writer()
        try:
            _heal_ghosts(cat, {collection: damaged}, writer=writer)
        finally:
            writer.close()


@click.command("verify")
@click.option(
    "--heal",
    is_flag=True,
    default=False,
    help="For each damaged document, prompt to drop the tumbler or print "
         "the `nx store put` invocation that would repopulate it. Requires "
         "--collection (per-document detail only exists in scoped mode). "
         "Incompatible with --json (interactive prompts would corrupt "
         "JSON stdout).",
)
@click.option(
    "--collection",
    "-c",
    default="",
    help="Restrict verification to a single physical_collection name "
         "(per-document detail mode).",
)
@click.option(
    "--json",
    "json_out",
    is_flag=True,
    default=False,
    help="Emit the machine-readable CI contract shape (see below).",
)
def verify_cmd(heal: bool, collection: str, json_out: bool) -> None:
    """Reconcile the catalog against T3 on the RDR-108/180 chash identity.

    Independent finding classes, all keyed on tumbler ->
    document_chunks.chash -> T3 chunk id (never the retired pre-RDR-108
    ``meta.doc_id``):

    \b
      vanished collections  a physical_collection with catalog docs that
                             T3 no longer knows about at all (deleted,
                             renamed). FINDING.
      damaged manifests     a document's manifest references chashes T3
                             does not have. Full mode reports this per
                             COLLECTION (one engine-side round trip);
                             ``--collection`` mode reports it per DOCUMENT.
                             FINDING.
      lost documents        chunk_count > 0 but the manifest has fewer
                             rows than that (including no manifest at
                             all) — a real gap. FINDING.
      never-chunked         chunk_count == 0 and no manifest. Split into
                             ``rdr145_exempt`` (knowledge__* store_put
                             notes with no file_path/source_uri —
                             legitimate BY DESIGN) and ``unclassified``
                             (everything else — candidate data loss, see
                             nexus-cdypx). Report-only, never a finding,
                             never affects the exit code either way.
      ghosts (full mode only)  read-only CENSUS of docs with blank/NULL
                             physical_collection (nexus-xeux8) — a
                             population BOTH this command's own health
                             classification and ``nx catalog
                             reconcile-stale`` exclude outright, so
                             nothing else sizes it. UNREPAIRABLE without
                             a manual collection assignment; never a
                             finding, never affects the exit code.

    Exit code: 0 when clean (never-chunked alone still exits 0); 1 when any
    vanished/damaged/lost finding exists. A check or collection that could
    not be read at all (degraded T3, pre-fence engine, un-backfilled
    manifest rows the engine's anti-join excludes) is INCOMPLETE, not
    clean — that raises a distinct, louder ``ClickException`` regardless of
    findings.

    ``--json`` is the CI contract: ``{"summary": {...}, "vanished_collections":
    [...], "damaged": [...], "lost": [...], "never_chunked": {...},
    "unreadable": [...]}`` (plus ``"unverifiable_rows": [...]`` when
    present, and ``"ghosts": {...}`` in full-catalog mode — see the
    ``ghosts`` entry above; absent in ``--collection`` scoped mode, since
    a ghost by definition belongs to no collection to scope into).
    Full-catalog mode is cheap and meant to gate CI; ``--collection``
    mode trades that cheapness for per-document detail (needed for
    ``--heal``). ``--heal`` cannot be combined with ``--json``.

    \b
    Examples:
      nx catalog verify                                  # full sweep (CI)
      nx catalog verify --json                           # CI-friendly output
      nx catalog verify --collection knowledge__foo      # per-doc detail
      nx catalog verify --collection knowledge__foo --heal   # interactive fix
    """
    if heal and json_out:
        raise click.ClickException(
            "--heal is interactive and cannot be combined with --json."
        )
    from nexus.commands import catalog as _cat_cmd  # noqa: PLC0415 — module-routed helper access keeps import acyclic + monkeypatch-visible
    cat = _cat_cmd._get_catalog()

    if collection:
        _verify_scoped(cat, collection, heal=heal, json_out=json_out)
        return

    if heal:
        raise click.ClickException(
            "nx catalog verify --heal needs per-document detail — scope the "
            "sweep first: nx catalog verify --collection <name> --heal."
        )
    _verify_full(cat, json_out=json_out)


def _heal_ghosts(
    cat: "CatalogReader",
    damaged_by_collection: dict[str, list[dict]],
    *,
    writer: object = None,
) -> None:
    """Interactive heal loop for `nx catalog verify --collection <c> --heal`.

    nexus-sj4a3: damaged docs (manifest chash(es) missing from T3) replace
    the retired ghost/doc_id shape. Per damaged doc, prompt for one of:
      d  drop the tumbler (catalog.delete_document)
      p  print the `nx store put` invocation that would repopulate it
      s  skip
      q  quit the heal loop
    """
    w = writer if writer is not None else cat
    dropped = 0
    for coll, docs in sorted(damaged_by_collection.items()):
        click.echo(f"\nHealing {coll}:")
        for d in docs:
            click.echo(
                f"  {d['tumbler']} — {d['title']} "
                f"({d['missing']} of {d['referenced']} chash(es) missing)"
            )
            choice = click.prompt(
                "    [d]rop tumbler / [p]rint put cmd / [s]kip / [q]uit",
                default="s",
                show_default=False,
            ).strip().lower()
            if choice == "q":
                click.echo(f"\nHealed: {dropped} tumbler(s) dropped.")
                return
            if choice == "d":
                if w.delete_document(Tumbler.parse(d["tumbler"])):
                    dropped += 1
                    click.echo("    dropped.")
                else:
                    click.echo("    already gone.")
            elif choice == "p":
                # The put command needs the original content. We emit a
                # template so the user can paste their source material.
                click.echo(
                    f"    nx store put --collection {coll} "
                    f"--title {d['title']!r} < source.md"
                )
            # anything else = skip
    click.echo(f"\nHealed: {dropped} tumbler(s) dropped.")


def register(group: click.Group) -> None:
    """Attach the integrity/verification commands to the shared ``catalog`` group."""
    group.add_command(audit_membership_cmd)
    group.add_command(verify_cmd)
