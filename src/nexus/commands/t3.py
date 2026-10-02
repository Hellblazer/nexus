# SPDX-License-Identifier: AGPL-3.0-or-later
# Copyright (c) 2026 Hal Hildebrand. All rights reserved.
"""``nx t3`` command group — T3 vector-store maintenance.

``nx t3 prune-stale`` (RDR-090 P1.4 / nexus-u7r0) sweeps each T3
collection's source_path values, removes chunks whose on-disk source
file is missing.

``nx t3 gc`` (RDR-101 Phase 6 / nexus-r5eo; RDR-192 Step 8 / nexus-wbfpw.18)
quarantines the chunks the engine's reapable predicate selects. It lists
candidates from ``POST /v1/vectors/reapable`` (advisory) and moves them with
the engine route ``POST /v1/vectors/gc/quarantine-orphans``, whose own
statement carries the predicate and takes the sweep gate. It never deletes by
chunk id.

The collection mode iterates ``T3Database.list_unique_source_paths``
plus a ``Path(p).exists()`` check; the staleness predicate is
intentionally simple (file present / absent) — broken-symlink
handling and partial-content checks are out of scope.

Out of scope:
  - Catalog-side prune-stale (``nx catalog prune-stale``) is a
    separate bead (nexus-zg4c).
  - The ``nx collection audit --verify-chroma`` cross-check between
    catalog chunk_ids and chroma chunk_ids (GH #335) shares the
    drift-detection idea but is a different surface.
"""
from __future__ import annotations

import json
import os
import signal
import sys
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import NoReturn

import click
import structlog

from nexus import config as _config

_log = structlog.get_logger(__name__)

# SIG-6 (nexus-872w): resumable backfill state file.
# The state file is a JSON dict mapping collection name → list of doc_ids
# that have been processed, or ["__done__"] when a collection is complete.
# nexus-69c94 critique S1+C2: a THIRD marker, ["__partial__", "fk_409=N",
# "chash_divergent=M", "zero_chunks=K"], means the collection finished this
# run WITHOUT error but left N+M+K docs un-healed (all three classes are
# caught per-doc and never raise -- zero_chunks is the dominant live gap
# class and can become recoverable after a Phase-5 remediation pass) --
# --resume treats it as pending, NOT done, and reprocesses the whole
# collection.
# Atomic writes via .tmp + rename avoid partial-write corruption on crash.
_BACKFILL_STATE_FILE_ENV = "NEXUS_BACKFILL_STATE_FILE"
# Doc-only literal (nexus-pfuns): `--resume`'s click help= string is built
# once at decorator-evaluation (module-import) time, so it can never
# reflect a per-invocation NEXUS_CONFIG_DIR override regardless -- this is
# purely informational text, NOT the runtime default (see
# `_backfill_state_path` below for that).
_BACKFILL_STATE_DEFAULT_DOC = "~/.config/nexus/backfill_state.json"
_PROGRESS_INTERVAL = 10  # emit progress every N docs across all collections


def _backfill_state_path() -> Path:
    """Return the path to the backfill state file.

    Respects ``NEXUS_BACKFILL_STATE_FILE`` env override so tests can
    redirect the file to a tmp directory without touching the real config.
    Absent that, falls back to ``nexus_config_dir()/backfill_state.json``
    -- NOT a hardcoded ``~/.config/nexus`` (nexus-pfuns: the old fallback
    was a module-level ``os.path.expanduser(...)`` constant, frozen at
    import and blind to ``NEXUS_CONFIG_DIR`` entirely, same import-time-
    default class already fixed once in ``gc_purge_marker.py`` -- T2
    nexus/gc-purge-marker-xdist-leak-2026-08-20). ``_config.nexus_config_dir``
    is read via module-attribute access (not a by-value ``from nexus.config
    import nexus_config_dir``) so a patch applied after this module is
    already imported still takes effect, and the function itself re-reads
    ``os.environ`` on every call -- both resolved at CALL time, never
    frozen.
    """
    override = os.environ.get(_BACKFILL_STATE_FILE_ENV)
    if override:
        return Path(override)
    return _config.nexus_config_dir() / "backfill_state.json"


def _load_backfill_state(path: Path) -> dict[str, list[str]]:
    """Load the backfill state file, returning an empty dict on miss/error."""
    try:
        return json.loads(path.read_text())
    except (FileNotFoundError, json.JSONDecodeError):
        return {}


def _save_backfill_state(path: Path, state: dict[str, list[str]]) -> None:
    """Atomically write the backfill state file (tmp + rename)."""
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(".tmp")
    tmp.write_text(json.dumps(state, indent=2))
    tmp.replace(path)


def _make_catalog():
    """Construct the default Catalog with the same init-gate that
    ``commands/catalog.py:_get_catalog`` enforces.

    Without the init gate, running ``nx t3 gc`` on a fresh install
    either crashes with an opaque traceback inside ``Catalog.__init__``
    or, worse, reads an empty catalog as having nothing to protect (the
    index-state breaker and the unknown-collection guard both read it).

    Patched in tests for isolation.
    """
    from nexus.catalog.factory import make_catalog_reader  # noqa: PLC0415 — command-local import deferred to avoid CLI startup cost (nexus.catalog.factory)

    cat = make_catalog_reader()
    if cat is None:
        raise click.ClickException(
            "Catalog is empty. Index or store documents before 'nx t3 gc' (nx index repo / nx store put)."
        )
    return cat


def _make_t3_for_backfill():
    """Construct the default T3Database for the backfill command.

    Patched in tests for isolation.
    """
    from nexus.db import make_t3  # noqa: PLC0415 — command-local import deferred to avoid CLI startup cost (nexus.db)
    return make_t3()


@click.group()
def t3() -> None:
    """T3 vector-store maintenance commands."""


@t3.command("prune-stale", hidden=True)
@click.option(
    "--collection",
    "-c",
    default="",
    help="Limit to one collection. Omit to scan every T3 collection.",
)
@click.option(
    "--dry-run/--no-dry-run",
    default=True,
    help="Report-only (default). Use --no-dry-run to perform deletions.",
)
@click.option(
    "--confirm",
    is_flag=True,
    default=False,
    help="Required alongside --no-dry-run to actually delete chunks "
    "(the explicit-affirmation belt-and-suspenders pattern).",
)
def prune_stale_cmd(collection: str, dry_run: bool, confirm: bool) -> None:
    """RETIRED — reported a false all-clear; superseded by an existing pair.

    \b
    It swept chunks by their ``source_path`` metadata. RDR-102 D2 hard-removed
    that key from the chunk schema, so the sweep matched nothing and every run
    printed a clean "0 stale" no matter how many indexed files had been deleted
    from disk. It also opened the LOCAL catalog by probing for
    ``documents.jsonl``, a file that has not existed since nexus-i711w — two
    independent reasons it could not have worked.

    \b
    It does not need rebuilding: the catalog-native pipeline already exists and
    covers the work, though NOT strictly — the retired verb swept every
    collection and ``nx t3 gc`` has no such mode, so the GC half must be looped
    (nexus-iitif). ``nx catalog prune-stale`` drops documents whose
    file_path is missing (resolving relative paths against the owner's
    repo_root, and refusing to classify what it cannot verify), and ``nx t3 gc``
    then collects the chunks those documents no longer reference. Doing it in
    that order also avoids the failure mode a chunk-only sweep creates: chunks
    deleted out from under a live manifest are exactly the dangling-manifest
    state ``nx doctor`` now flags (nexus-5xn3k). nexus-bm8dd.
    """
    _ = (collection, dry_run, confirm)
    raise click.ClickException(
        "nx t3 prune-stale is RETIRED (nexus-bm8dd).\n"
        "\n"
        "It swept T3 chunks by source_path metadata. RDR-102 D2 removed that "
        "key from the chunk schema, so the sweep has matched nothing since — "
        "and reported a clean '0 stale' on every collection rather than saying "
        "it could not look. A verb that cannot find anything must not be "
        "mistaken for one that found nothing.\n"
        "\n"
        "Use the catalog-native pipeline:\n"
        "  nx catalog prune-stale [--collection COLLECTION] --no-dry-run --confirm\n"
        "  nx t3 gc -c COLLECTION --no-dry-run --yes\n"
        "\n"
        "Prune first, GC second: deleting chunks while their document still "
        "references them leaves a dangling manifest (nexus-5xn3k).\n"
        "\n"
        "NOTE: `nx t3 gc` REQUIRES -c — the orphan diff is per-collection, so "
        "there is no sweep-every-collection mode to replace the one this verb "
        "advertised. `nx catalog prune-stale` does take all collections. To GC "
        "every collection, enumerate and loop:\n"
        "  nx collection list | awk '{print $1}' | xargs -I{} "
        "nx t3 gc -c {} --no-dry-run --yes\n"
        "(nexus-iitif tracks whether gc should gain an all-collections mode.)"
    )


#: Page size of the advisory reapable listing (the engine clamps to 300, the AGENTS.md paging
#: convention). A module constant so a test can force several pages without 300+ real rows.
_GC_LISTING_PAGE = 300

_ORPHAN_WINDOW_REMOVED = (
    "--orphan-window was removed (nexus-wbfpw.18). nx t3 gc now takes its candidates from the "
    "engine's reapable machinery, whose grace window (30 days, counted from when the chunk last "
    "lost an owner) is fixed in the engine and not tunable from a client, so there is no window "
    "to pass. Run the verb without the flag; `nx store list --reapable -c COLLECTION` shows what "
    "it would take."
)

_GC_NO_ROUTE_MESSAGE = (
    "The connected engine predates the reapable routes (POST /v1/vectors/reapable and the "
    "reapable-aware gc_quarantine_orphans; RDR-192 Step 8, beads nexus-wbfpw.16 and "
    "nexus-wbfpw.17), so nx t3 gc cannot run. Upgrade to an engine tag that carries them "
    "(compare the deployed engine's own version against REQUIRED_ENGINE_VERSION in "
    "src/nexus/engine_version.py)."
)


def _refuse_orphan_window(ctx: click.Context, param: click.Parameter, value: str | None) -> None:
    """Callback for the retired ``--orphan-window`` option: any use is a refusal that names the
    removal, so a script still passing it fails loudly instead of silently ignoring a window it
    believes it is setting."""
    if value is not None:
        raise click.UsageError(_ORPHAN_WINDOW_REMOVED)


#: The census buckets the verb refuses on when above zero (RDR-192 R8): ``legacy-unmanifested`` is a
#: live legacy note the route would take once old; ``unclassified`` is a row the census itself cannot
#: classify, so the reapable verdict for it is not understood. The reaper refuses on both.
_GC_CENSUS_BLOCKERS = ("legacy-unmanifested", "unclassified")


def _census_blocker_totals(census: dict, collection: str) -> dict[str, int]:
    """The blocking buckets' totals from one census response. A response missing a blocking bucket
    cannot be read as zero: refuse to act without it."""
    totals = census.get("totals") or {}
    missing = [b for b in _GC_CENSUS_BLOCKERS if totals.get(b) is None]
    if missing:
        raise click.ClickException(
            f"The manifest-less census for {collection!r} carried no {', '.join(missing)} total; "
            f"refusing to act without it (RDR-192 R8)."
        )
    return {b: int(totals[b]) for b in _GC_CENSUS_BLOCKERS}


def _census_scope_total(census: dict, collection: str) -> int:
    """``scope_chunk_total`` from one census response: every chunk the collection holds. It is the
    floor's denominator and half of the empty-manifest guard, so a response without it cannot be read
    as 0 (that would switch both off): refuse to act without it."""
    raw = census.get("scope_chunk_total")
    if raw is None:
        raise click.ClickException(
            f"The manifest-less census for {collection!r} carried no scope_chunk_total; refusing to "
            f"act without it, since the fraction floor and the empty-manifest guard both read it "
            f"(RDR-192 R8)."
        )
    return int(raw)


def _census_blocker_reasons(collection: str, blockers: dict[str, int], *, prior: bool = False) -> list[str]:
    """One refusal reason per blocking bucket above zero. *prior* words them for the re-read made
    immediately before the move ("now reads ...; it read 0 when this run began")."""
    reasons: list[str] = []
    lead = (
        f"the manifest-less census for '{collection}' now reads"
        if prior else f"the manifest-less census for '{collection}' reads"
    )
    tail = "; it read 0 when this run began" if prior else ""
    if blockers["legacy-unmanifested"]:
        reasons.append(
            f"{lead} legacy-unmanifested = {blockers['legacy-unmanifested']}, not 0{tail}. A live "
            f"legacy note with no manifest row reads reapable once old, and the engine route takes "
            f"no exclusion list, so moving this collection could quarantine a live note. Inspect "
            f"with 'nx t3 census-manifest-less -c {collection}', re-put those notes (so each has a "
            f"manifest row), then re-run (RDR-192 R8)."
        )
    if blockers["unclassified"]:
        reasons.append(
            f"{lead} unclassified = {blockers['unclassified']}, not 0{tail}. A row the census "
            f"cannot classify is a collection state nobody has understood, so the reapable "
            f"verdict on it cannot be trusted; the reaper refuses on it too. Inspect with "
            f"'nx t3 census-manifest-less -c {collection}' (it exits 1 on unclassified) and "
            f"resolve the rows, then re-run (RDR-192 R8)."
        )
    return reasons


def _expire_client_quarantine(t3_db, collection: str, qname: str, *, moved: int) -> None:
    """The client expiry ``nx t3 gc`` runs after its own move (and on a run with nothing to move), as
    ``nx index repo`` runs it (``indexer._gc_serverside``): rows in the quarantine sibling past the
    client cutoff (``NX_GC_QUARANTINE_DAYS``) that the engine's reaper did not tag, behind the same
    ``NX_GC_FLOOR_FRACTION`` floor (``NX_GC_FORCE=1`` overrides). The verb runs it so a collection no
    repo index sweeps (every ``knowledge__*``) still expires its own quarantine; without it nothing
    would ever expire what this verb moved. A failure is exit 1 after saying the move stands.

    *qname* is the sibling the verb moves into (the origin's catalog-row-derived name), not the only one
    it expires from (nexus-wbfpw.58): rows a client moved under an earlier name of the same origin, one
    the catalog row no longer derives (catalog-044-3 rewrote ``owner_id``), carry no ``quarantined_by`` tag
    for the engine's reaper to expire. So this is the one caller that asks the engine which siblings hold
    the origin's chunks (``resolve_quarantine_siblings(..., probe_engine=True)``; ``nx index repo`` uses the
    two derived names only, because the probe is an unindexed scan engine-side; nexus-wbfpw.64 moves the
    resolution into the engine and retires the probe). Each sibling is expired and reported on its own
    line: the engine judges the floor per sibling, so one sibling's refusal and another's expiry are
    both stated as what they are, never summed."""
    from nexus.catalog.chunk_quarantine import (  # noqa: PLC0415 — command-local import (nexus.catalog.chunk_quarantine)
        expire_quarantine_serverside,
        quarantine_days,
        resolve_quarantine_siblings,
    )
    from nexus.db.http_vector_client import VectorServiceError  # noqa: PLC0415 — command-local import (nexus.db.http_vector_client)
    from nexus.indexer import _GC_FLOOR_MIN_CHUNKS, _gc_floor_fraction  # noqa: PLC0415 — command-local import (nexus.indexer is heavy)

    force = os.environ.get("NX_GC_FORCE", "") == "1"
    cutoff = (datetime.now(UTC) - timedelta(days=quarantine_days())).strftime("%Y-%m-%dT%H:%M:%SZ")
    if getattr(t3_db, "gc_expire_quarantine", None) is None:
        click.echo(
            f"  Client expiry of {qname} did NOT run: this T3 handle carries no gc_expire_quarantine route.",
            err=True,
        )
        return
    siblings = resolve_quarantine_siblings(t3_db, collection, primary=qname, probe_engine=True)
    done: list[tuple[str, int]] = []  # (sibling, expired) for every sibling whose expiry completed
    for sibling in siblings:
        try:
            expiry = expire_quarantine_serverside(
                t3_db, sibling, collection, cutoff,
                floor_fraction=_gc_floor_fraction(), floor_min_chunks=_GC_FLOOR_MIN_CHUNKS, force=force,
            )
        except VectorServiceError as exc:
            earlier = (
                " Already expired before it: " + ", ".join(f"{n} from {s}" for s, n in done) + "."
                if done else ""
            )
            click.echo(
                f"\nSummary: {f'the move succeeded ({moved} chunk(s) quarantined) but' if moved else 'nothing needed moving, but'}"
                f" the client expiry of {sibling} FAILED: {exc}. Siblings are expired in this order: "
                f"{', '.join(siblings)}; that one and any after it were not.{earlier} Re-run this verb to retry it.",
                err=True,
            )
            raise click.exceptions.Exit(1) from exc
        if expiry is None:  # cannot happen past the route check above; never read as "nothing to expire"
            click.echo(f"  Client expiry of {sibling} did NOT run: the T3 handle lost its expire route.", err=True)
            continue
        expired, refused = expiry
        done.append((sibling, expired))
        # The engine's `refused` is two things: chunks the origin collection's manifest references
        # again (kept always; FORCE does not reach them) plus, when the floor fires, the whole eligible
        # set (the engine then reports expired = 0). The floor is judged per sibling on that sibling's
        # own rows, so for ONE sibling expired > 0 means its floor did not fire and every refusal is a
        # manifest keep; expired == 0 cannot tell the two apart.
        if not refused:
            why = ""
        elif expired:
            why = " (kept: the manifest references them again; NX_GC_FORCE=1 does not change that)"
        else:
            why = (
                " (kept: the manifest references them again, or the NX_GC_FLOOR_FRACTION floor held the "
                "whole expiry; NX_GC_FORCE=1 overrides only the floor)"
            )
        click.echo(
            f"  Client expiry of {sibling} (older than {quarantine_days()} day(s), rows the engine's "
            f"reaper tagged excluded): {expired} expired, {refused} refused{why}."
        )


@t3.command("gc")
@click.option(
    "--collection",
    "-c",
    required=True,
    help="Collection to GC. Required (the engine's reapable predicate is per-collection).",
)
@click.option(
    "--orphan-window",
    default=None,
    hidden=True,
    expose_value=False,
    callback=_refuse_orphan_window,
    help="REMOVED (nexus-wbfpw.18). The grace window is fixed in the engine.",
)
@click.option(
    "--dry-run/--no-dry-run",
    default=True,
    help="Report-only (default). Use --no-dry-run to actually quarantine.",
)
@click.option(
    "--yes",
    is_flag=True,
    default=False,
    help="Required alongside --no-dry-run to actually move chunks. "
    "Without --yes, the command falls back to report-only.",
)
@click.option(
    "--allow-empty-manifest-set",
    is_flag=True,
    default=False,
    help="Override the empty-manifest-set (nexus-jqrtp) and unknown-collection "
    "(nexus-v1zdu) refusals. DANGEROUS: only pass this once you've confirmed "
    "the collection really is fully orphaned, not a fresh/mis-scoped tenant, an "
    "unbackfilled manifest or a mistyped name.",
)
@click.option(
    "--allow-incomplete-index-state",
    is_flag=True,
    default=False,
    help="Override the RUNFENCE index-state refusal (nexus-g6k6b). "
    "DANGEROUS: only pass this once you've confirmed no reindex is "
    "concurrently running against this collection (an in-flight or "
    "fence-failed document's chunks are not garbage — they are a run "
    "in progress or a documented-damaged state with its own remedy).",
)
def gc_cmd(
    collection: str,
    dry_run: bool,
    yes: bool,
    allow_empty_manifest_set: bool,
    allow_incomplete_index_state: bool,
) -> None:
    """Quarantine T3 chunks the engine's reapable predicate selects (RDR-192 Step 8).

    \b
    A chunk is reapable when the ENGINE says so (``nexus.chunk_is_reapable``):
    no manifest row in this collection names it in any owner state (a
    tombstoned owner still counts, so ``nx catalog purge-trash`` owns those),
    and it has been ownerless for the engine's 30 day grace. Aging runs on the
    later of ``last_written_at`` and the moment the chunk last lost an owner
    row (recorded in the side table ``nexus.chunk_orphaned_at``), NOT on
    ``indexed_at``; a chunk with no ``indexed_at`` is therefore a candidate once
    it is old enough.

    \b
    The verb MOVES, it does not delete. The act is the engine route
    ``POST /v1/vectors/gc/quarantine-orphans`` (``gc_quarantine_orphans``, the
    BOUNDED form: 2000 rows per call, looped until ``remaining`` is 0): its
    own statement carries the predicate, takes the exclusive per-collection
    sweep gate, and moves the rows to the ``quarantine-*`` sibling. A client
    that re-writes a chunk after the listing wins, because the predicate is
    evaluated against the rows the statement actually locks. The route is
    collection-wide: it takes no chunk list, no exclusion list and no window.
    The engine records each batch in ``gc_audit`` (actor ``engine``); this verb
    writes no audit row of its own. The listing printed here is advisory (a
    lock-free snapshot, paged by keyset) and is what the route would take at
    that instant.

    \b
    The verb frees no storage by itself: the moved chunks sit in
    ``quarantine-*`` until they are expired, and each side expires only what it
    moved. After its own move this verb runs the client expiry (as
    ``nx index repo`` does for a repo's code, docs and rdr collections): rows in
    the quarantine sibling older than ``NX_GC_QUARANTINE_DAYS`` (default 14) that
    the engine's reaper did not tag are hard-deleted, behind the same
    ``NX_GC_FLOOR_FRACTION`` floor (``NX_GC_FORCE=1`` overrides). That is how a
    ``knowledge__*`` quarantine ever expires, since no repo index sweeps it. A
    chunk the engine's reaper moved (tagged ``quarantined_by``) is expired by the
    engine alone, on its own retention. A chunk whose document is re-registered
    is restored automatically by the ``nx index repo`` run. The operator restore
    verb ``nx t3 quarantine restore`` (nexus-wbfpw.49; on develop, its engine route
    in the same engine tag as the reaper) refuses
    this verb's ``gc_quarantine_orphans`` audit rows (they list a sample only), so
    restore a chunk this verb moved with its ``--quarantined-since`` /
    ``--quarantined-before`` window or explicit ``--chash`` values. Each engine batch commits on its own, so a run that stops part
    way leaves its earlier batches moved and re-running it is safe.

    \b
    ``--orphan-window`` was REMOVED: the engine exposes no tunable grace, so a
    script that still passes it is refused with a message naming the removal.

    \b
    Refusals (a ``--dry-run`` says which a real run would hit, and exits 1 when
    it names one, so ``nx t3 gc ... --dry-run && nx t3 gc ... --no-dry-run --yes``
    stops where the real run would):
      - RUNFENCE (nexus-g6k6b): any document in the collection not
        ``index_state='complete'`` (an in-flight or fence-failed run). Override:
        ``--allow-incomplete-index-state``.
      - Manifest-less census (RDR-192 R8): ``POST /v1/vectors/manifest-less-census``
        is called at the start of every run and again right before the move, and
        ``legacy-unmanifested`` AND ``unclassified`` must both read 0 (as the
        reaper requires). A live legacy note with no manifest row reads
        reapable once old, and the route has no exclusion list, so the verb
        refuses rather than move it. Clear the bucket (re-put the notes, see
        ``nx t3 census-manifest-less``) first.
      - Fraction floor: a pass of at least 100 reapable chunks that is more than
        ``NX_GC_FLOOR_FRACTION`` (default 0.25) of the collection's chunks is the
        manifest-gap misclassification shape (the engine's own reading: the 100
        minimum counts the reapable set, the fraction divides by every stored
        chunk). Override: ``NX_GC_FORCE=1``. The
        floor is this verb's own for now (nexus-wbfpw.52 tracks one on the route): the route this verb moves with
        carries none (the engine reaper's floor never reaches it, and
        ``indexer._prune_deleted_files`` calls the same route with no floor at
        all). Because it is checked on the advisory listing, the bounded drain
        can move more than the floor admits if chunks age in mid-run.
      - Empty manifest set (nexus-jqrtp): the collection holds chunks but none
        has a manifest row in it (read off the census: stored chunks minus the
        manifest-less buckets is 0), the shape of a fresh or mis-scoped tenant
        or an unbackfilled manifest. Override: ``--allow-empty-manifest-set``.
      - Unknown collection (nexus-v1zdu): a name the catalog does not know.
        Override: ``--allow-empty-manifest-set``.

    \b
    Needs an engine that carries the reapable routes (RDR-192 Step 8); against
    an older engine the verb refuses with a message saying so.

    \b
    Examples:
      nx t3 gc -c knowledge__delos --dry-run                # report only
      nx t3 gc -c rdr__nexus-571b8edd --no-dry-run --yes    # quarantine
    """
    from nexus.db import make_t3  # noqa: PLC0415 — command-local import deferred to avoid CLI startup cost (nexus.db)
    from nexus.db.http_vector_client import VectorServiceError  # noqa: PLC0415 — command-local import deferred to avoid CLI startup cost (nexus.db.http_vector_client)

    will_act = (not dry_run) and yes
    if (not dry_run) and not yes:
        click.echo(
            "--no-dry-run alone is treated as report-only. "
            "Add --yes to actually quarantine chunks."
        )

    t3_db = make_t3()
    cat = _make_catalog()

    # nexus-sis0m.3: refuse a name no collection carries, here, rather than
    # surface the engine's "register it first via POST
    # /v1/catalog/collections/upsert" 422 (shakeout 7.64.1 Surface E F10) --
    # advice that is wrong for a typo.
    # A registered collection with no chunks is absent from the chunk
    # listing but is a real target, so a catalog registration counts too.
    from nexus.catalog.membership import collection_is_known as _collection_is_known  # noqa: PLC0415 — command-local import (nexus.catalog.membership)

    in_t3 = collection in {c["name"] for c in t3_db.list_collections(strict=True)}
    if not in_t3 and not _collection_is_known(cat, collection):
        raise click.ClickException(
            f"no collection named {collection!r}; 'nx collection list' shows "
            "the names t3 gc takes."
        )

    # nexus-g6k6b (RUNFENCE precondition): one catalog read of the collection's documents, for the
    # index_state circuit breaker. Deliberately FAILS LOUD on lookup failure, not fail-open: an
    # unverifiable index-run state must refuse the run rather than risk moving chunks an
    # in-flight reindex has written but not yet manifested.
    from nexus.indexer_utils import (  # noqa: PLC0415 — command-local import deferred to avoid CLI startup cost (nexus.indexer_utils)
        catalog_documents_for_collection,
        non_complete_documents,
    )

    try:
        collection_documents = catalog_documents_for_collection(cat, collection)
    except Exception as exc:  # noqa: BLE001 — boundary catch; refuses the run rather than risk moving live content
        click.echo(
            f"Failed to verify index-run state for {collection!r}: {exc}. Refusing to compute "
            f"candidates without it — an in-flight or fence-failed document's chunks are not "
            f"garbage (nexus-g6k6b)."
        )
        raise click.exceptions.Exit(1)

    incomplete_docs = non_complete_documents(collection_documents)
    if incomplete_docs:
        _states = ", ".join(sorted({
            f"{d.title!r}={d.index_state!r}" for d in incomplete_docs
        }))
        click.echo(
            f"  {len(incomplete_docs)} document(s) in {collection!r} are "
            f"not index_state='complete' ({_states}) — a --no-dry-run "
            f"--yes run will REFUSE until they resolve, or pass "
            f"--allow-incomplete-index-state (nexus-g6k6b)"
        )

    # RDR-192 R8 (nexus-wbfpw.18): the census is read on EVERY run, immediately before acting. A
    # live legacy note (no manifest row) reads reapable once old and the route has no exclusion
    # list, so legacy-unmanifested must be 0 or the verb refuses. scope_chunk_total (every chunk the
    # collection holds, any manifest state) is the floor's denominator, from the same round trip.
    census_fn = getattr(t3_db, "manifest_less_census", None)
    reapable_fn = getattr(t3_db, "reapable_chunks", None)
    if census_fn is None or reapable_fn is None:
        raise click.ClickException(
            "This verb needs the engine-backed T3 handle (nexus.db.make_t3()); this one carries no "
            "reapable routes."
        )
    try:
        census = census_fn(collection, limit=1)
    except VectorServiceError as exc:
        if exc.code == 404:
            raise click.ClickException(_GC_NO_ROUTE_MESSAGE) from exc
        raise click.ClickException(
            f"Failed to read the manifest-less census for {collection!r}: {exc}"
        ) from exc
    census_blockers = _census_blocker_totals(census, collection)
    scope_chunk_total = _census_scope_total(census, collection)
    # Chunks carrying an own-collection manifest row = everything the collection holds minus the five
    # manifest-less buckets (the census classifies exactly the chunks with no such row).
    manifest_less_total = sum(int(n) for n in (census.get("totals") or {}).values())
    owned_chunks = scope_chunk_total - manifest_less_total

    # The advisory listing: what POST /v1/vectors/reapable (grace absent = the engine default,
    # the same grace gc_quarantine_orphans uses) would hand the route right now, paged by keyset.
    try:
        candidates = list(reapable_fn(collection, page_limit=_GC_LISTING_PAGE))
    except VectorServiceError as exc:
        if exc.code == 404:
            raise click.ClickException(_GC_NO_ROUTE_MESSAGE) from exc
        raise click.ClickException(
            f"Failed to list reapable chunks for {collection!r}: {exc}"
        ) from exc

    click.echo(f"{collection}: {len(candidates)} reapable chunk(s) (of {scope_chunk_total} stored)")
    if not will_act:
        for row in candidates:
            title = f"  {row['title']}" if row.get("title") else ""
            click.echo(f"  chash={row['chash']}{title}")

    if not candidates:
        click.echo("\nSummary: 0 reapable chunk(s); nothing to do.")
        if will_act:
            # Nothing to move still leaves what an earlier run moved: expire it on schedule.
            from nexus.catalog.chunk_quarantine import quarantine_collection_name  # noqa: PLC0415 — command-local import (nexus.catalog.chunk_quarantine)

            _expire_client_quarantine(t3_db, collection, quarantine_collection_name(collection), moved=0)
        return

    # The refusals. Each is a reason a --no-dry-run --yes run stops; a dry run prints them as
    # warnings so an operator planning a real run sees them coming.
    from nexus.indexer import _GC_FLOOR_MIN_CHUNKS, _gc_floor_fraction  # noqa: PLC0415 — command-local import (nexus.indexer is heavy)

    reasons: list[str] = []
    if incomplete_docs and not allow_incomplete_index_state:
        _names = ", ".join(sorted({
            f"{d.title!r} ({d.index_state!r})" for d in incomplete_docs
        }))
        reasons.append(
            f"{len(incomplete_docs)} document(s) in '{collection}' are not "
            f"index_state='complete': {_names}. An in-flight or fence-failed document's chunks "
            f"are not garbage — they are a run in progress or a documented-damaged state with "
            f"its own remedy (finish the reindex, or re-index with --force). If you have "
            f"confirmed no reindex is concurrently running against this collection, re-run "
            f"with --allow-incomplete-index-state."
        )
    reasons.extend(_census_blocker_reasons(collection, census_blockers))
    if scope_chunk_total <= 0:
        reasons.append(
            f"the listing names {len(candidates)} reapable chunk(s) in '{collection}' but the census "
            f"reads scope_chunk_total = {scope_chunk_total}. The two disagree, so the floor and the "
            f"empty-manifest guard have no denominator; refusing rather than moving on an "
            f"unverifiable collection (RDR-192 R8)."
        )
    if scope_chunk_total > 0 and owned_chunks <= 0 and not allow_empty_manifest_set:
        reasons.append(
            f"the catalog manifest for '{collection}' names NONE of the {scope_chunk_total} "
            f"chunk(s) it holds, so every one reads as an orphan candidate. That is "
            f"indistinguishable from a fresh/mis-scoped tenant or an unbackfilled manifest "
            f"without deciding the collection's disposition first (nexus-jqrtp). Investigate "
            f"with 'nx t3 backfill-manifest -c {collection}' or 'nx catalog reconcile', or, if "
            f"the collection really is fully orphaned, re-run with --allow-empty-manifest-set."
        )
    # THE FLOOR IS PERMANENT, and it is this verb's own. gc_quarantine_orphans (the route this verb
    # moves with) carries no fraction floor; the reaper's floor lives inside reaper_quarantine_chunks,
    # which has no HTTP route, so no engine-side floor reaches this verb (and
    # indexer._prune_deleted_files moves through the same route with none at all). The variable is
    # NX_GC_FLOOR_FRACTION (with NX_GC_FORCE), the name the indexer's quarantine-expiry floor already
    # reads through the same fail-safe parser, the same default (0.25) and the same 100-chunk minimum:
    # one name for the operator across the client GC floors. NX_REAPER_FLOOR_FRACTION is NOT reused:
    # it configures the engine-side reaper, a different process whose environment a CLI invocation
    # does not set, so honouring it here would make the floor follow a variable nobody exports.
    floor_fraction = _gc_floor_fraction()
    force = os.environ.get("NX_GC_FORCE", "") == "1"
    # The engine's reading (reaper_quarantine_chunks, gc_expire_quarantine): the 100-chunk minimum
    # counts the REAPABLE set, the fraction is reapable / every stored chunk, compared strictly by
    # division (never `n > f * total`, whose float product misjudges an exact boundary such as
    # 0.57 * 100 = 56.99999999999999).
    if (
        scope_chunk_total > 0
        and len(candidates) >= _GC_FLOOR_MIN_CHUNKS
        and len(candidates) / scope_chunk_total > floor_fraction
        and not force
    ):
        reasons.append(
            f"{len(candidates)} of {scope_chunk_total} chunk(s) "
            f"({len(candidates) / scope_chunk_total:.0%}) in '{collection}' are reapable, over "
            f"the NX_GC_FLOOR_FRACTION floor of {floor_fraction:.0%} (applies from "
            f"{_GC_FLOOR_MIN_CHUNKS} reapable chunks up). A verdict this large is the manifest-gap "
            f"misclassification shape, not routine churn. The engine route this verb moves with "
            f"carries no floor, so this verb holds it. If the collection really is mostly "
            f"garbage, re-run with NX_GC_FORCE=1 (the chunks go to quarantine, not away)."
        )

    if not will_act:
        for reason in reasons:
            click.echo(f"  a --no-dry-run --yes run will REFUSE: {reason}")
        if reasons:
            click.echo(
                f"\nSummary: a --no-dry-run --yes run would REFUSE for {collection} "
                f"({len(reasons)} reason(s) above); {len(candidates)} chunk(s) are listed but none "
                f"would move. Exiting 1 so a script gating the real run on this one stops."
            )
            raise click.exceptions.Exit(1)
        click.echo(
            f"\nSummary: would quarantine up to {len(candidates)} chunk(s) from {collection}."
        )
        return

    # The unknown-collection guard (nexus-v1zdu), last line of defence before the move.
    # catalog_documents_for_collection does NOT distinguish "collection known, zero documents"
    # from "collection unknown", so an unregistered/typo'd name reads as having nothing to
    # protect. Checked here, not early, so a --dry-run is not refused over a collection that
    # merely lacks a `collections` registry row.
    if not allow_empty_manifest_set:
        from nexus.catalog.membership import refuse_if_collection_unknown  # noqa: PLC0415 — command-local import (nexus.catalog.membership)

        refuse_if_collection_unknown(cat, collection, collection_documents)
    elif not collection_documents and not _collection_is_known(cat, collection):
        click.echo(
            f"WARNING: the catalog does not know a collection named "
            f"'{collection}'. --allow-empty-manifest-set overrides that "
            f"refusal. Check the name against `nx collection list`."
        )

    if reasons:
        for reason in reasons:
            click.echo(f"\nREFUSING to move: {reason}")
        raise click.exceptions.Exit(1)

    # R8, again, as close to the move as the verb can get: the census above was read before the
    # listing paged, and a note can go legacy-unmanifested in between. The in-engine per-pass
    # re-run is the reaper's requirement (nexus-2x9xa); this is the verb's own call.
    try:
        recheck = census_fn(collection, limit=1)
    except VectorServiceError as exc:
        raise click.ClickException(
            f"Failed to re-read the manifest-less census for {collection!r} before the move: {exc}"
        ) from exc
    recheck_reasons = _census_blocker_reasons(
        collection, _census_blocker_totals(recheck, collection), prior=True,
    )
    if recheck_reasons:
        for reason in recheck_reasons:
            click.echo(f"\nREFUSING to move: {reason}")
        raise click.exceptions.Exit(1)

    # The act: the engine's own move. No chunk ids cross the wire; the route's statement carries
    # the predicate and takes the sweep gate, so a racing client write wins.
    from nexus.catalog.chunk_quarantine import (  # noqa: PLC0415 — command-local import (nexus.catalog.chunk_quarantine)
        GC_AUDIT_MAX_CHASHES,
        BoundedDrainIncomplete,
        quarantine_collection_name,
        quarantine_orphans_bounded_serverside,
        quarantine_orphans_serverside,
    )

    qname = quarantine_collection_name(collection)
    stamp = datetime.now(UTC).strftime("%Y-%m-%dT%H:%M:%SZ")
    try:
        moved_result = quarantine_orphans_bounded_serverside(
            t3_db, collection, qname, stamp, sample_limit=GC_AUDIT_MAX_CHASHES, strict=True,
        )
        if moved_result is None:
            moved_result = quarantine_orphans_serverside(
                t3_db, collection, qname, stamp, sample_limit=GC_AUDIT_MAX_CHASHES,
            )
    except BoundedDrainIncomplete as exc:
        # Every batch commits on its own: what moved before the stop is moved, audited and sitting in
        # quarantine. The verb says so, so an operator does not read a failure as "nothing happened".
        left = "unknown" if exc.remaining is None else str(exc.remaining)
        detail = f": {exc.__cause__}" if exc.__cause__ is not None else ""
        unknown = (
            " The failed batch's own outcome is unknown (a timeout or transport error can follow a "
            "server commit); nx catalog gc-audit list shows what the engine recorded."
            if exc.reason == "batch failed" else ""
        )
        click.echo(
            f"\nSummary: the engine move for {collection} STOPPED ({exc.reason}{detail}). "
            f"{exc.moved} chunk(s) were moved into {qname} in {exc.batches} earlier batch(es); each "
            f"batch committed on its own and stays moved, so re-running this verb is safe."
            f"{unknown} Still reapable when it stopped: {left}.",
            err=True,
        )
        raise click.exceptions.Exit(1) from exc
    except VectorServiceError as exc:
        if exc.code == 404:
            raise click.ClickException(_GC_NO_ROUTE_MESSAGE) from exc
        click.echo(
            f"\nSummary: the engine move FAILED for {collection} on its first batch: {exc}. No batch "
            f"is known to have moved a chunk, but a timeout or transport error can follow a server "
            f"commit, so nx catalog gc-audit list is the record of what the engine did. Re-running "
            f"this verb is safe.",
            err=True,
        )
        raise click.exceptions.Exit(1) from exc
    if moved_result is None:
        raise click.ClickException(
            "this T3 handle carries no gc_quarantine_orphans route; this verb needs the "
            "engine-backed handle."
        )
    moved, _sample = moved_result
    click.echo(
        f"\nSummary: quarantined {moved} chunk(s) from {collection} into {qname}. The engine "
        f"recorded each batch in gc_audit (nx catalog gc-audit list). The chunks are moved, not "
        f"freed: the client expiry below hard-deletes them once they are older than "
        f"NX_GC_QUARANTINE_DAYS (a later run of this verb, or of 'nx index repo' for a repo "
        f"collection, does it)."
    )
    if moved < len(candidates):
        click.echo(
            f"  The listing named {len(candidates)}; the engine moved {moved}. It re-checks the "
            f"predicate under its own lock, so a chunk a client re-wrote since the listing stays."
        )
    elif moved > len(candidates):
        click.echo(
            f"  The listing named {len(candidates)}; the engine moved {moved}, MORE than listed: "
            f"chunks aged past the grace while the drain ran. The floor was judged on the listing, "
            f"so check {qname} (nx catalog gc-audit list) if the extra count matters."
        )

    _expire_client_quarantine(t3_db, collection, qname, moved=moved)


@t3.command("backfill-manifest")
@click.option(
    "--collection",
    "-c",
    default="",
    help=(
        "Limit to one collection. Omit to backfill all collections "
        "registered in the catalog."
    ),
)
@click.option(
    "--dry-run/--no-dry-run",
    default=True,
    help="Report-only (default). Use --no-dry-run to write manifest rows.",
)
@click.option(
    "--limit",
    "-n",
    default=0,
    type=int,
    help="If > 0, process at most N documents per collection.",
)
@click.option(
    "--resume/--no-resume",
    default=False,
    help=(
        "Resume a previous interrupted backfill. Reads state from "
        f"$NEXUS_BACKFILL_STATE_FILE (default: {_BACKFILL_STATE_DEFAULT_DOC}). "
        "Collections marked done are skipped; others are re-processed "
        "from scratch (per-doc idempotency comes from write_manifest)."
    ),
)
@click.option(
    "--only-gapped/--no-only-gapped",
    default=False,
    help=(
        "Touch ONLY documents with zero manifest rows (nexus-3n7pr). "
        "Skips any document that already has >=1 manifest row via a "
        "batched pre-pass, before any T3 read or write -- use this for a "
        "targeted repair pass so healthy manifests are never rewritten. "
        "Default off (unchanged pre-existing behavior: every document in "
        "the collection is processed)."
    ),
)
def backfill_manifest_cmd(
    collection: str,
    dry_run: bool,
    limit: int,
    resume: bool,
    only_gapped: bool,
) -> None:
    """Backfill document_chunks manifest from T3 chunk metadata (RDR-108 D2).

    \\b
    Reads T3 chunk metadata (doc_id, chunk_index, chunk_text_hash, span
    coordinates) per catalog document and writes one row per chunk into
    the ``document_chunks`` manifest table. After this runs the catalog
    can answer "what chunks compose a Document and in what order?" without
    consulting T3 metadata.

    \\b
    The backfill is idempotent: re-running overwrites the manifest with
    the same content (DELETE + INSERT in one transaction per document).

    \\b
    Carve-outs:
      - taxonomy__* collections are skipped (centroids have no chunk_text_hash).
      - Pre-RDR-053 chunks missing chunk_text_hash raise an error; re-index
        that collection before running backfill.

    \\b
    Progress is written to stderr. Use --resume to continue after Ctrl-C.
    On SIGINT the state file is flushed before exit so --resume can pick
    up where it left off.

    \\b
    Examples:
      nx t3 backfill-manifest --dry-run                         # report only
      nx t3 backfill-manifest -c code__nexus --no-dry-run       # one collection
      nx t3 backfill-manifest --no-dry-run                      # all collections
      nx t3 backfill-manifest --no-dry-run -n 100               # first 100 docs
      nx t3 backfill-manifest --no-dry-run --resume             # continue after Ctrl-C
      nx t3 backfill-manifest --no-dry-run --only-gapped        # repair pass: zero-manifest docs only
    """
    from nexus.catalog.manifest_backfill import (  # noqa: PLC0415 — command-local import deferred to avoid CLI startup cost (nexus.catalog.manifest_backfill)
        MissingChunkHashError,
        backfill_manifest_for_collection,
    )

    cat = _make_catalog()
    t3_db = _make_t3_for_backfill()

    if dry_run:
        click.echo("(dry-run: no manifest rows will be written)")

    if collection:
        collections_to_process = [collection]
    else:
        # All collections registered in the catalog.
        collections_to_process = [
            c["name"] for c in cat.list_collections()
        ]
        if not collections_to_process:
            click.echo("No collections registered in catalog; nothing to do.")
            return

    total = len(collections_to_process)

    # SIG-6: load resume state and skip already-done collections.
    state_path = _backfill_state_path()
    state: dict[str, list[str]] = {}
    if resume:
        state = _load_backfill_state(state_path)
        done_before = sum(1 for v in state.values() if v == ["__done__"])
        if done_before:
            print(
                f"Resuming: {done_before} collection(s) already done, skipping.",
                file=sys.stderr,
            )
        # nexus-69c94 critique S1: collections left __partial__ by a prior
        # run (nonzero fk_409/chash_divergent residual) are NOT skipped --
        # they will be reprocessed below like any other pending collection.
        # Surface the count so the operator knows this run's job includes
        # retrying them.
        partial_before = sum(
            1 for v in state.values() if v and v[0] == "__partial__"
        )
        if partial_before:
            print(
                f"Resuming: {partial_before} collection(s) left partial by "
                f"a prior run (fk_409/chash_divergent residual) will be "
                f"reprocessed.",
                file=sys.stderr,
            )

    # SIG-6: SIGINT handler — flush state then exit 130.
    def _on_sigint(signum: int, frame: object) -> None:  # noqa: ARG001
        if state:
            _save_backfill_state(state_path, state)
            print(
                f"\nInterrupted. Progress saved to {state_path}. "
                f"Re-run with --resume to continue.",
                file=sys.stderr,
            )
        sys.exit(130)

    try:
        signal.signal(signal.SIGINT, _on_sigint)
    except (ValueError, OSError):
        # Non-main thread (e.g. test runner) — skip signal registration.
        pass

    total_docs = 0
    total_chunks = 0
    total_skipped_no_t3 = 0
    total_skipped_zero_chunks = 0
    total_skipped_phase3_no_index = 0
    total_skipped_has_manifest = 0
    total_skipped_chash_divergent = 0
    total_skipped_fk_409 = 0
    total_reverse_discovered = 0
    total_cross_collection_forward_owner_skipped = 0
    total_reverse_multi_piece_skipped = 0
    total_chunk_count_mismatch_skipped = 0
    skipped_taxonomy = 0
    errors: list[str] = []
    docs_processed_overall = 0

    for idx, coll_name in enumerate(collections_to_process, start=1):
        # SIG-6: skip collections already complete in resume state.
        if resume and state.get(coll_name) == ["__done__"]:
            print(
                f"[{idx}/{total}] {coll_name}: skipped (already done)",
                file=sys.stderr,
            )
            continue

        print(
            f"[{idx}/{total}] {coll_name}: processing ...",
            file=sys.stderr,
        )

        try:
            result = backfill_manifest_for_collection(
                cat, t3_db, coll_name, dry_run=dry_run, limit=limit,
                only_gapped=only_gapped,
            )
        except MissingChunkHashError as exc:
            click.echo(
                f"ERROR: {exc}",
                err=True,
            )
            errors.append(str(exc))
            continue
        except Exception as exc:  # noqa: BLE001 — best-effort path; failure logged, must not crash caller
            click.echo(
                f"ERROR in {coll_name}: {exc}",
                err=True,
            )
            errors.append(f"{coll_name}: {exc}")
            continue

        if result.skipped_taxonomy:
            click.echo(f"  {coll_name}: skipped (taxonomy carve-out)")
            skipped_taxonomy += 1
            continue

        # SIG-6: per-collection stderr progress including skipped-no-t3.
        verb = "would write" if dry_run else "wrote"
        skipped_part = (
            f" ({result.docs_skipped_no_t3} skipped: no_t3)"
            if result.docs_skipped_no_t3
            else ""
        )
        # nexus-gvmbo: zero-chunk-match skips must be COUNTED and visible in
        # the command's own output, never silent -- this is what turns the
        # "never write empty" fix into an observable non-vacuity guarantee.
        zero_chunks_part = (
            f" ({result.docs_skipped_zero_chunks} skipped: zero_chunks)"
            if result.docs_skipped_zero_chunks
            else ""
        )
        # nexus-3n7pr G2: docs_skipped_phase3_no_index was counted but never
        # printed -- the dry run IS the sizing instrument for a remediation
        # pass, so an unhealable class that isn't printed under-reports it.
        phase3_no_index_part = (
            f" ({result.docs_skipped_phase3_no_index} skipped: phase3_no_index)"
            if result.docs_skipped_phase3_no_index
            else ""
        )
        # nexus-3n7pr G1: --only-gapped skips surfaced the same way as the
        # other skip classes -- never silent.
        has_manifest_part = (
            f" ({result.docs_skipped_has_manifest} skipped: has_manifest)"
            if result.docs_skipped_has_manifest
            else ""
        )
        # nexus-dmf7r: chash-id/metadata divergence skips -- never silent.
        chash_divergent_part = (
            f" ({result.docs_skipped_chash_divergent} skipped: chash_divergent)"
            if result.docs_skipped_chash_divergent
            else ""
        )
        # nexus-r7g3i: FK-409 skips -- never silent.
        fk_409_part = (
            f" ({result.docs_skipped_fk_409} skipped: fk_409)"
            if result.docs_skipped_fk_409
            else ""
        )
        # nexus-wbfpw.7: reverse-note discoveries -- reported separately
        # from forward discovery, never folded into docs_processed alone.
        reverse_part = (
            f" ({result.docs_reverse_discovered} via reverse notes-guard)"
            if result.docs_reverse_discovered
            else ""
        )
        # nexus-wbfpw.7 fix-round-1: cross-collection forward-owner and
        # multi-piece reverse skips -- never silent, same discipline as
        # every other skip class above.
        cross_collection_part = (
            f" ({result.docs_cross_collection_forward_owner_skipped} skipped: "
            f"cross_collection_forward_owner)"
            if result.docs_cross_collection_forward_owner_skipped
            else ""
        )
        reverse_multi_piece_part = (
            f" ({result.docs_reverse_multi_piece_skipped} skipped: "
            f"reverse_multi_piece)"
            if result.docs_reverse_multi_piece_skipped
            else ""
        )
        # nexus-wbfpw.41: forward-path documents whose matched chunks cannot
        # be one manifest (duplicate positions, or a count that differs from
        # the registered chunk_count under --only-gapped) -- never silent.
        chunk_count_mismatch_part = (
            f" ({result.docs_skipped_chunk_count_mismatch} skipped: "
            f"chunk_count_mismatch)"
            if result.docs_skipped_chunk_count_mismatch
            else ""
        )
        print(
            f"[{idx}/{total}] {coll_name}: processed {result.docs_processed} "
            f"doc(s), {verb} {result.chunks_would_write if dry_run else result.chunks_written} chunk manifest row(s)"
            f"{skipped_part}{zero_chunks_part}{phase3_no_index_part}"
            f"{has_manifest_part}{chash_divergent_part}{fk_409_part}{reverse_part}"
            f"{cross_collection_part}{reverse_multi_piece_part}{chunk_count_mismatch_part}",
            file=sys.stderr,
        )

        # Emit to stdout as well for the summary output.
        click.echo(
            f"  {coll_name}: processed {result.docs_processed} doc(s), "
            f"{verb} {result.chunks_would_write if dry_run else result.chunks_written} chunk manifest row(s)"
            + (
                f" ({result.docs_skipped_no_t3} skipped: no T3 collection)"
                if result.docs_skipped_no_t3
                else ""
            )
            + (
                f" ({result.docs_skipped_zero_chunks} skipped: zero chunk matches)"
                if result.docs_skipped_zero_chunks
                else ""
            )
            + (
                f" ({result.docs_skipped_phase3_no_index} skipped: phase3 no chunk_index)"
                if result.docs_skipped_phase3_no_index
                else ""
            )
            + (
                f" ({result.docs_skipped_has_manifest} skipped: already has manifest)"
                if result.docs_skipped_has_manifest
                else ""
            )
            + (
                f" ({result.docs_skipped_chash_divergent} skipped: chash id/metadata divergent)"
                if result.docs_skipped_chash_divergent
                else ""
            )
            + (
                f" ({result.docs_skipped_fk_409} skipped: FK conflict)"
                if result.docs_skipped_fk_409
                else ""
            )
            + (
                f" ({result.docs_reverse_discovered} via reverse notes-guard)"
                if result.docs_reverse_discovered
                else ""
            )
            + (
                f" ({result.docs_cross_collection_forward_owner_skipped} skipped: "
                f"cross-collection forward owner)"
                if result.docs_cross_collection_forward_owner_skipped
                else ""
            )
            + (
                f" ({result.docs_reverse_multi_piece_skipped} skipped: "
                f"reverse multi-piece note)"
                if result.docs_reverse_multi_piece_skipped
                else ""
            )
            + (
                f" ({result.docs_skipped_chunk_count_mismatch} skipped: "
                f"more matched chunks than the document's chunk count, or two at one position)"
                if result.docs_skipped_chunk_count_mismatch
                else ""
            )
        )

        total_docs += result.docs_processed
        total_chunks += result.chunks_would_write if dry_run else result.chunks_written
        total_skipped_no_t3 += result.docs_skipped_no_t3
        total_skipped_zero_chunks += result.docs_skipped_zero_chunks
        total_skipped_phase3_no_index += result.docs_skipped_phase3_no_index
        total_skipped_has_manifest += result.docs_skipped_has_manifest
        total_skipped_chash_divergent += result.docs_skipped_chash_divergent
        total_skipped_fk_409 += result.docs_skipped_fk_409
        total_reverse_discovered += result.docs_reverse_discovered
        total_cross_collection_forward_owner_skipped += (
            result.docs_cross_collection_forward_owner_skipped
        )
        total_reverse_multi_piece_skipped += result.docs_reverse_multi_piece_skipped
        total_chunk_count_mismatch_skipped += result.docs_skipped_chunk_count_mismatch
        docs_processed_overall += result.docs_processed

        # SIG-6: periodic progress every _PROGRESS_INTERVAL docs.
        if docs_processed_overall % _PROGRESS_INTERVAL == 0 and docs_processed_overall > 0:
            print(
                f"  ... {docs_processed_overall} docs processed so far",
                file=sys.stderr,
            )

        # SIG-6: mark collection done in state file (atomic write).
        #
        # nexus-69c94 critique S1+C2 (substantive-critic, T2 scratch
        # 79007753 / T2 [22640]): docs_skipped_fk_409 and
        # docs_skipped_chash_divergent are caught PER DOC inside
        # backfill_manifest_for_collection and never raise -- a collection
        # containing them still returns normally, so marking it __done__
        # unconditionally would make --resume PERMANENTLY skip those
        # un-healed docs (the plan's stated safety property, "a collection
        # that errored is NOT marked done", would silently stop holding for
        # this sub-case). docs_skipped_zero_chunks joins the residual too
        # (critique C2): it is the DOMINANT live gap class (894/895 in the
        # nexus-3n7pr population) and a future remediation pass (re-index /
        # re-put) can make those exact docs recoverable -- a collection
        # whose only gaps are zero_chunks must stay revisitable by
        # --resume, not be permanently marked done. Mark __partial__
        # instead, carrying the residual counts for operator visibility --
        # --resume then reprocesses the WHOLE collection (per-doc
        # idempotency makes a full re-pass safe and cheap for docs that
        # already healed) rather than trusting a bare --resume to have
        # picked the residual up.
        if not dry_run:
            # nexus-wbfpw.7 fix-round-1: cross-collection forward-owner and
            # reverse multi-piece skips are unhealed gaps of the same kind
            # as the three below -- a future fix (widened discovery, a
            # re-put that re-splits the note) can make them recoverable, so
            # a collection whose only gaps are these must also stay
            # revisitable by --resume, not be marked done.
            residual = (
                result.docs_skipped_fk_409
                + result.docs_skipped_chash_divergent
                + result.docs_skipped_zero_chunks
                + result.docs_cross_collection_forward_owner_skipped
                + result.docs_reverse_multi_piece_skipped
                + result.docs_skipped_chunk_count_mismatch
            )
            if residual > 0:
                state[coll_name] = [
                    "__partial__",
                    f"fk_409={result.docs_skipped_fk_409}",
                    f"chash_divergent={result.docs_skipped_chash_divergent}",
                    f"zero_chunks={result.docs_skipped_zero_chunks}",
                    f"cross_collection_forward_owner={result.docs_cross_collection_forward_owner_skipped}",
                    f"reverse_multi_piece={result.docs_reverse_multi_piece_skipped}",
                    f"chunk_count_mismatch={result.docs_skipped_chunk_count_mismatch}",
                ]
                click.echo(
                    f"  {coll_name}: NOT marked done -- {residual} doc(s) "
                    f"skipped (fk_409={result.docs_skipped_fk_409}, "
                    f"chash_divergent={result.docs_skipped_chash_divergent}, "
                    f"zero_chunks={result.docs_skipped_zero_chunks}, "
                    f"cross_collection_forward_owner="
                    f"{result.docs_cross_collection_forward_owner_skipped}, "
                    f"reverse_multi_piece={result.docs_reverse_multi_piece_skipped}, "
                    f"chunk_count_mismatch={result.docs_skipped_chunk_count_mismatch}); "
                    f"a future --resume will reprocess this collection"
                )
            else:
                state[coll_name] = ["__done__"]
            _save_backfill_state(state_path, state)

    verb = "would write" if dry_run else "wrote"
    skipped_no_t3_part = (
        f", {total_skipped_no_t3} doc(s) skipped (no T3 collection)"
        if total_skipped_no_t3
        else ""
    )
    skipped_zero_chunks_part = (
        f", {total_skipped_zero_chunks} doc(s) skipped (zero chunk matches)"
        if total_skipped_zero_chunks
        else ""
    )
    # nexus-3n7pr G2: print in the summary too -- was counted, never surfaced.
    skipped_phase3_no_index_part = (
        f", {total_skipped_phase3_no_index} doc(s) skipped (phase3 no chunk_index)"
        if total_skipped_phase3_no_index
        else ""
    )
    # nexus-3n7pr G1: --only-gapped skip total.
    skipped_has_manifest_part = (
        f", {total_skipped_has_manifest} doc(s) skipped (already has manifest)"
        if total_skipped_has_manifest
        else ""
    )
    # nexus-dmf7r: chash id/metadata divergence skip total.
    skipped_chash_divergent_part = (
        f", {total_skipped_chash_divergent} doc(s) skipped "
        f"(chash id/metadata divergent)"
        if total_skipped_chash_divergent
        else ""
    )
    # nexus-r7g3i: FK-409 skip total.
    skipped_fk_409_part = (
        f", {total_skipped_fk_409} doc(s) skipped (FK conflict)"
        if total_skipped_fk_409
        else ""
    )
    # nexus-wbfpw.7: reverse notes-guard discovery total -- reported
    # separately from forward discovery in every summary, dry-run included.
    reverse_discovered_part = (
        f", {total_reverse_discovered} doc(s) manifested via reverse notes-guard"
        if total_reverse_discovered
        else ""
    )
    # nexus-wbfpw.7 fix-round-1: cross-collection forward-owner and reverse
    # multi-piece skip totals -- never silent, same discipline as every
    # other skip class above.
    cross_collection_forward_owner_part = (
        f", {total_cross_collection_forward_owner_skipped} chash(es) skipped "
        f"(cross-collection forward owner)"
        if total_cross_collection_forward_owner_skipped
        else ""
    )
    reverse_multi_piece_part = (
        f", {total_reverse_multi_piece_skipped} note(s) skipped "
        f"(reverse multi-piece)"
        if total_reverse_multi_piece_skipped
        else ""
    )
    chunk_count_mismatch_part = (
        f", {total_chunk_count_mismatch_skipped} doc(s) skipped "
        f"(chunk count mismatch)"
        if total_chunk_count_mismatch_skipped
        else ""
    )
    click.echo(
        f"\nSummary: processed {total_docs} doc(s), "
        f"{verb} {total_chunks} manifest row(s)"
        + skipped_no_t3_part
        + skipped_zero_chunks_part
        + skipped_phase3_no_index_part
        + skipped_has_manifest_part
        + skipped_chash_divergent_part
        + skipped_fk_409_part
        + reverse_discovered_part
        + cross_collection_forward_owner_part
        + reverse_multi_piece_part
        + chunk_count_mismatch_part
        + (f", skipped {skipped_taxonomy} taxonomy collection(s)" if skipped_taxonomy else "")
        + (f", {len(errors)} error(s)" if errors else "")
    )

    if errors:
        raise SystemExit(1)


# ── RDR-192 Step 2, client half (bead nexus-wbfpw.5) ────────────────────────
#
# `nx t3 census-manifest-less` wraps the engine's read-only
# `POST /v1/vectors/manifest-less-census` route (bead nexus-wbfpw.4,
# `HttpVectorClient.manifest_less_census`). The route is first carried by
# engine-service-v0.1.133; against an older engine the verb exits 4 and the
# same census runs as direct SQL, `scripts/sql/manifest_less_census.sql`.

#: Bucket names the manifest-less-census route returns (RDR-192 S2, bead
#: nexus-wbfpw.4's response contract -- see that route's docstring and
#: `scripts/sql/manifest_less_census.sql`'s header for the full
#: definitions). The bead that requested this verb predates the route and
#: names the same five buckets; no reconciliation was needed. Order here
#: drives both text rendering and `--require-zero` validation.
_CENSUS_BUCKETS = (
    "superseded", "legacy-unmanifested", "dead-owner", "no-owner", "unclassified",
)

#: Page size `_census_one_collection` requests per call (AGENTS.md paging
#: convention, N <= 300; the engine clamps to this anyway --
#: `VectorHandler.MAX_CENSUS_LIMIT`). A module-level constant so a test can
#: force multi-page pagination without seeding 300+ real rows.
_CENSUS_PAGE_LIMIT = 300

#: Distinct exit code for "the connected engine predates the
#: manifest-less-census route" (RDR-192 S2, bead nexus-wbfpw.5's
#: EXECUTION note): a 404 here means the CONNECTED engine's build is
#: older than the one that shipped bead nexus-wbfpw.4's route -- an
#: EXPECTED, never-a-traceback outcome. This is a MECHANISM, not a dated
#: snapshot of which tag carries the route (review round 1 Significant-3
#: finding): which engine tags carry it changes over time (e.g.
#: engine-service-v0.1.133), so callers should check the connected
#: engine's own version / `REQUIRED_ENGINE_VERSION`
#: (`src/nexus/engine_version.py`), never a frozen "no tag carries it
#: yet" claim. Distinct from exit 1 (unclassified > 0), 2 (--require-zero
#: violated), 3 (--all found no collection), 5 (a real engine error).
_EXIT_NO_ROUTE = 4

#: Distinct exit code for a real engine error that is NOT "predates the
#: route" (review round 1 Important-1/Important-2 findings): a
#: `VectorServiceError` with any code other than 404 -- an explicitly
#: named `quarantine-*` --collection's 400
#: (`VectorHandler.requireNotQuarantineCollection`), a transient 5xx, or
#: a failed --all collection listing (`list_collections(strict=True)`).
#: Printed as one clear line naming the collection/listing and the
#: error, on stderr, never a traceback -- matching the rest of t3.py's
#: "except Exception: click.echo(..., err=True); exit non-zero"
#: convention. In --all, collections already censused before the
#: failure are still rendered (text, or a still-parseable --json document
#: carrying a "census_error" key) before this exit fires.
_EXIT_ENGINE_ERROR = 5

#: Human-readable message for the exit-4 (no-route) case. A module
#: constant so the wording lives in exactly one place (the runtime
#: message and the help text below both name the mechanism, not a date).
_NO_ROUTE_MESSAGE = (
    "This engine does not carry the manifest-less-census route "
    "(RDR-192 S2, bead nexus-wbfpw.4) -- the connected engine predates "
    "that route. Upgrade to an engine tag that carries it (compare the "
    "deployed engine's own version against REQUIRED_ENGINE_VERSION in "
    "src/nexus/engine_version.py)."
)


def _census_one_collection(client, collection: str) -> dict:
    """Page through :meth:`HttpVectorClient.manifest_less_census` for one
    collection, merging every page's ``chashes``/``owners`` (RDR-192 S2,
    bead nexus-wbfpw.5). ``totals``/``scope_chunk_total`` are read off the
    FIRST page only -- the route reports them collection-wide and
    identical on every page (see that method's docstring), so re-reading
    them per page would be redundant, never a correction.

    Raises whatever :meth:`~HttpVectorClient.manifest_less_census` raises
    (notably :class:`~nexus.db.http_vector_client.VectorServiceError`,
    ``code=404`` on a pre-route engine) -- the caller decides how to
    surface that.
    """
    offset = 0
    chashes: dict[str, list[str]] = {bucket: [] for bucket in _CENSUS_BUCKETS}
    owners: dict[str, dict] = {}
    totals: dict[str, int] = {}
    scope_chunk_total = 0
    first_page = True
    while True:
        page = client.manifest_less_census(
            collection, limit=_CENSUS_PAGE_LIMIT, offset=offset,
        )
        if first_page:
            totals = dict(page.get("totals") or {})
            scope_chunk_total = int(page.get("scope_chunk_total", 0))
            first_page = False
        for bucket, page_chashes in (page.get("chashes") or {}).items():
            chashes.setdefault(bucket, []).extend(page_chashes)
        owners.update(page.get("owners") or {})
        returned = int(page.get("returned", 0))
        if returned < _CENSUS_PAGE_LIMIT:
            break
        offset += _CENSUS_PAGE_LIMIT
    return {
        "collection": collection,
        "chashes": chashes,
        "owners": owners,
        "totals": totals,
        "scope_chunk_total": scope_chunk_total,
    }


def _legacy_owner_titles(results: list[dict]) -> dict[str, str]:
    """Titles of the owner documents of every legacy-unmanifested chunk,
    keyed by tumbler, in ONE batched catalog read (nexus-wbfpw.41). Those
    chunks are hidden from ``nx store get`` and search, so the owner's title
    is the only thing an operator has to recognise the note by and to re-put
    it under. Best-effort: a catalog that cannot answer just prints no titles."""
    tumblers = sorted({
        owner_tumbler
        for result in results
        for chash in result["chashes"].get("legacy-unmanifested", [])
        if (owner_tumbler := (result["owners"].get(chash) or {}).get("owner_tumbler"))
    })
    if not tumblers:
        return {}
    try:
        from nexus.catalog.factory import make_catalog_reader  # noqa: PLC0415 — command-local import deferred to avoid CLI startup cost (nexus.catalog.factory)

        reader = make_catalog_reader()
        if reader is None:
            return {}
        return {t: entry.title for t, entry in reader.resolve_many(tumblers).items() if entry.title}
    except Exception as exc:  # noqa: BLE001 — titles are a convenience; the census itself must still print
        _log.debug("census_owner_titles_unavailable", error=str(exc))
        return {}


def _render_census_text(result: dict, titles: dict[str, str] | None = None) -> None:
    """Print one collection's census in text form, naming each item's
    owner tumbler and path (forward, reverse, or none) so an operator can
    see which document keeps a chunk live and whether the reverse
    tie-break chose it (nexus-wbfpw.5 acceptance criteria). A
    legacy-unmanifested row also carries its owner document's title when
    ``titles`` has it (nexus-wbfpw.41)."""
    click.echo(f"{result['collection']}:")
    owners = result["owners"]
    for bucket in _CENSUS_BUCKETS:
        bucket_chashes = result["chashes"].get(bucket, [])
        click.echo(f"  {bucket}: {result['totals'].get(bucket, 0)}")
        for chash in sorted(bucket_chashes):
            owner = owners.get(chash) or {}
            tumbler = owner.get("owner_tumbler") or "-"
            path = owner.get("owner_path") or "none"
            title = (titles or {}).get(tumbler) if bucket == "legacy-unmanifested" else None
            title_part = f'  title="{title}"' if title else ""
            click.echo(f"    {chash}  owner={tumbler} ({path}){title_part}")
    click.echo(f"  total: {result['scope_chunk_total']}")


def _finish_census(
    *,
    as_json: bool,
    results: list[dict],
    collections_discovered: int | None,
    census_error: dict[str, str | None] | None,
    require_zero: tuple[str, ...],
    exit_code: int | None = None,
) -> NoReturn:
    """Render the census result exactly once -- text, or one JSON
    document -- and exit. Called on EVERY exit path (review round 2
    CRITICAL: under --json, stdout must carry exactly one parseable
    document regardless of exit code; round 1's fix only covered the
    mid-loop-failure flavor of exit 5, leaving exit 3, exit 4, and the
    listing-failure flavor of exit 5 emitting a plain sentence to stdout,
    nothing at all, or nothing at all respectively).

    ``unclassified``/``require_zero_violations`` are always computed over
    whatever collections DID succeed, even when ``census_error`` is also
    set (review round 2 Significant): --require-zero and unclassified are
    gate conditions computed from the collections that succeeded;
    ``census_error`` reports that the census itself is incomplete. Both
    facts belong in the document together, so a violation already
    observed is never hidden behind a later 4/5.

    Exit-code precedence (mirrored in the command's own help text and
    docs/cli-reference.md): ``census_error`` (4 no-route, 5 any other
    engine error) always wins over 1 (unclassified) and 2
    (--require-zero) -- an incomplete census cannot pass a gate. Between
    4 and 5: 4 is a whole-engine condition (the CONNECTED ENGINE predates
    the route) and is checked first, on the very first collection
    attempted, before any per-collection 5 could fire. Exit 3 is passed
    explicitly by the caller -- it is not a violation (the --all listing
    SUCCEEDED and is genuinely empty), so it is never derived from
    unclassified/require_zero/census_error, which would otherwise
    trivially compute to a clean 0 over zero results.
    """
    any_unclassified = any(
        result["totals"].get("unclassified", 0) > 0 for result in results
    )
    zero_violations = [
        bucket for bucket in require_zero
        if sum(result["totals"].get(bucket, 0) for result in results) > 0
    ]
    if exit_code is None:
        if census_error is not None:
            exit_code = (
                _EXIT_NO_ROUTE if census_error["kind"] == "no_route"
                else _EXIT_ENGINE_ERROR
            )
        elif any_unclassified:
            exit_code = 1
        elif zero_violations:
            exit_code = 2
        else:
            exit_code = 0

    if as_json:
        click.echo(json.dumps({
            "collections": results,
            "collections_discovered": collections_discovered,
            "collections_censused": len(results),
            "census_error": census_error,
            "unclassified": any_unclassified,
            "require_zero_violations": zero_violations,
            "exit_code": exit_code,
        }, indent=2))
    else:
        titles = _legacy_owner_titles(results)
        for result in results:
            _render_census_text(result, titles)

    if zero_violations:
        # Always stderr (review round 1 CRITICAL finding): this line used
        # to go to the SAME stdout stream as the --json payload above,
        # corrupting it for the one combination (--json + a violated
        # --require-zero) the exit-code contract exists to support.
        click.echo(f"--require-zero violated: {', '.join(zero_violations)}", err=True)

    sys.exit(exit_code)


@t3.command("census-manifest-less")
@click.option(
    "--collection", "-c", default=None,
    help="Collection to census. Exactly one of --collection/--all is required.",
)
@click.option(
    "--all", "all_collections", is_flag=True, default=False,
    help="Census every T3 collection except quarantine-* ones (live(c) "
    "applies to every collection, not only knowledge__*).",
)
@click.option(
    "--json", "as_json", is_flag=True, default=False,
    help="Emit JSON instead of text.",
)
@click.option(
    "--require-zero", "require_zero", multiple=True, metavar="BUCKET",
    help="Bucket that must be zero across every censused collection "
    f"(one of {', '.join(_CENSUS_BUCKETS)}); repeatable. Exit 2 if any "
    "named bucket's total is above zero.",
)
def census_manifest_less_cmd(
    collection: str | None,
    all_collections: bool,
    as_json: bool,
    require_zero: tuple[str, ...],
) -> None:
    """Census manifest-less T3 chunks via the engine's read-only census
    route (RDR-192 Step 2 MVV (a), bead nexus-wbfpw.5).

    \b
    Classifies every chunk carrying no own-collection manifest row into
    one of five buckets -- superseded, legacy-unmanifested, dead-owner,
    no-owner, unclassified -- and prints, per collection, the count in
    each bucket, the owning document (tumbler + how it was found: forward,
    reverse, or none) for each item, and a total. See
    ``scripts/sql/manifest_less_census.sql``'s header (the SAME text the
    engine route runs) for the full bucket definitions and the
    forward/reverse precedence rule.

    \b
    Exit codes:
      0  clean.
      1  unclassified > 0 -- a census that cannot classify a row has failed.
      2  --require-zero names a bucket whose count is above zero.
      3  --all finds no collection (excluding quarantine-*) -- the listing
         itself SUCCEEDED and is genuinely empty; a failed listing is exit 5.
      4  the connected engine predates the manifest-less-census route --
         upgrade the engine (compare its version against
         REQUIRED_ENGINE_VERSION in src/nexus/engine_version.py).
      5  a real engine error other than "predates the route": a
         quarantine-* --collection's 400, a transient 5xx, or a failed
         --all collection listing. In --all, collections already censused
         before the failure are still printed (or, under --json, still
         emitted as a parseable document naming the failed collection).

    \b
    Exit-code precedence: 4 and 5 (the census is INCOMPLETE) always win
    over 1 and 2 (a gate condition computed from what WAS censused) -- an
    incomplete census cannot pass a gate. Between 4 and 5: 4 is a
    whole-engine condition (the connected engine predates the route
    entirely) and is checked first, on the very first collection
    attempted, before a later per-collection 5 could ever fire. This
    never hides a finding: unclassified/--require-zero are always
    computed over whatever collections DID succeed, even when 4/5 also
    fires, and both are always present in the output (text and --json)
    alongside the incomplete-census report.

    \b
    Human-readable diagnostics (the --require-zero violation notice, an
    engine-error line) always go to stderr, never stdout -- with --json,
    stdout carries exactly one JSON document on every exit path listed
    above, parseable regardless of exit code. The document carries
    "collections" (per-collection results, possibly partial),
    "collections_discovered" (how many collections were found to census;
    null when discovery itself failed), "collections_censused" (how many
    actually completed -- compare against "collections_discovered" to
    tell "stopped after 1 of 50" from "stopped after 1 of 2"),
    "census_error" (null, or {"collection", "error", "kind"} naming which
    collection failed and why), "unclassified" (bool) and
    "require_zero_violations" (list of bucket names), and "exit_code".

    \b
    Needs engine-service-v0.1.133 or later; against an older engine it
    exits 4, and the same census runs as direct SQL
    (``scripts/sql/manifest_less_census.sql``).
    """
    if bool(collection) == bool(all_collections):
        raise click.UsageError(
            "Specify exactly one of --collection NAME or --all."
        )
    for bucket in require_zero:
        if bucket not in _CENSUS_BUCKETS:
            raise click.BadParameter(
                f"unknown bucket {bucket!r}; must be one of "
                f"{', '.join(_CENSUS_BUCKETS)}",
                param_hint="--require-zero",
            )

    from nexus.db import make_t3  # noqa: PLC0415 — command-local import deferred to avoid CLI startup cost (nexus.db)
    from nexus.db.http_vector_client import VectorServiceError  # noqa: PLC0415 — command-local import deferred to avoid CLI startup cost (nexus.db.http_vector_client)

    t3_db = make_t3()

    if all_collections:
        # RDR-204: exclude quarantine siblings via the catalog-authoritative
        # lifecycle_state field, never a raw "quarantine-" name-prefix parse
        # (tests/test_collection_name_parse_census.py's parse-site census
        # guards against exactly that regression). Absent means the engine
        # could not join a catalog row for this collection -- unregistered,
        # not quarantine, so it stays IN, matching
        # http_vector_client.is_live_collection_row's own "absent means
        # included" reading.
        try:
            # strict=True (nexus-wbfpw.5 review round 1, Significant-2):
            # list_collections's default swallows a non-404 failure and
            # returns [] -- the shared contract every OTHER caller relies
            # on, which this verb must not change. strict=True re-raises
            # instead, so exit 3 below can mean "listed zero collections",
            # never "the listing itself failed".
            names = [
                c["name"] for c in t3_db.list_collections(strict=True)
                if c.get("lifecycle_state") != "quarantine"
            ]
        except VectorServiceError as exc:
            click.echo(f"Failed to list T3 collections: {exc}", err=True)
            # Review round 2 CRITICAL: this used to sys.exit before the
            # JSON render block was ever reached, leaving stdout empty
            # under --json. collections_discovered is None -- the listing
            # itself failed, so there is no count to report.
            _finish_census(
                as_json=as_json, results=[], collections_discovered=None,
                census_error={
                    "collection": None, "error": str(exc), "kind": "listing_failed",
                },
                require_zero=require_zero,
            )
        if not names:
            # Review round 2 CRITICAL: this used to print a plain sentence
            # to stdout with no err=True, which is both a diagnostic-on-
            # stdout regression and, under --json, corrupts the "stdout is
            # always one parseable document" contract. Exit 3 is not a
            # violation (the listing SUCCEEDED and is genuinely empty), so
            # exit_code is passed explicitly rather than derived.
            click.echo(
                "No T3 collections found (excluding quarantine-*); nothing to census.",
                err=True,
            )
            _finish_census(
                as_json=as_json, results=[], collections_discovered=0,
                census_error=None, require_zero=require_zero, exit_code=3,
            )
    else:
        names = [collection]

    collections_discovered = len(names)

    # nexus.db.make_t3() with no injected _client (every production call,
    # local and cloud alike) returns the HttpVectorClient itself, not a
    # T3Database facade -- the facade wrap is test-injection-only (see
    # that function's docstring). manifest_less_census lives directly on
    # HttpVectorClient, so t3_db already IS the right object to call it on.
    client = t3_db

    results: list[dict] = []
    census_error: dict[str, str | None] | None = None
    for name in names:
        try:
            results.append(_census_one_collection(client, name))
        except VectorServiceError as exc:
            if exc.code == 404:
                click.echo(_NO_ROUTE_MESSAGE, err=True)
                census_error = {"collection": name, "error": str(exc), "kind": "no_route"}
            else:
                # Any other engine error (a quarantine-* collection's 400,
                # a transient 5xx, ...) -- review round 1 Important-1: this
                # used to fall through to a bare `raise` and surface as a
                # raw traceback. Stop censusing further collections but
                # keep what already succeeded (review round 1's --all
                # decision).
                click.echo(f"Census failed on collection {name!r}: {exc}", err=True)
                census_error = {"collection": name, "error": str(exc), "kind": "engine_error"}
            break

    _finish_census(
        as_json=as_json, results=results, collections_discovered=collections_discovered,
        census_error=census_error, require_zero=require_zero,
    )


# ── RDR-192 Step 9 Day-2 (bead nexus-2x9xa): `nx t3 quarantine ...` ─────────
# Registered here, defined in its own module: this file is already large and the
# verb shares nothing with it but the group.
from nexus.commands.t3_cmds.quarantine import quarantine_group as _quarantine_group  # noqa: E402 — must follow the `t3` group definition above

t3.add_command(_quarantine_group)
