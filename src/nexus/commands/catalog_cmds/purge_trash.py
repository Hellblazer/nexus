# SPDX-License-Identifier: AGPL-3.0-or-later
"""``nx catalog purge-trash`` (nexus-3ck2g).

Catalog ``delete`` (``nx catalog delete``) soft-tombstones: it stamps
``deleted_at`` on the catalog row and deliberately leaves the
``document_chunks`` manifest and the T3 ``chunks_<dim>`` rows in place so
the engine's own ``nexus.purge_trash`` orphan predicate (``EXISTS`` a
manifest row AND ``NOT EXISTS`` a live parent document) can find them.
That sweep function has existed engine-side since RDR-145 / catalog-003
with no caller anywhere in the stack — this verb is that caller.

This is a RECLAIM verb, not the search-visibility fix: on an engine
carrying the nexus-3ck2g read-side tombstone filter, deleted content
already stops appearing in search results the moment ``nx catalog delete``
tombstones it. This verb reclaims the manifest/T3 storage those tombstoned
docs still hold.

AGE SEMANTICS ARE SYMMETRIC since catalog-026 (nexus-5da44, RDR-191
GATE-2) — and this section previously documented the OPPOSITE, the
original catalog-003 behaviour, for two weeks after the engine retired it
(nexus-kcm6c: the 2026-08-27 shakedown read 0 stranded at the default 30d
gate vs 1,041 at 1d and concluded the COUNTER was lying; it was this
prose). Current contract, verified against catalog-026/catalog-033's
function body: the chunk sweep (Steps 1-3) PROTECTS any chunk whose
manifest row belongs to a document that is live OR tombstoned but still
inside the ``--older-than-days`` grace window — the exact complement of
Step 4's document-delete predicate. A tombstoned document inside its
window keeps its catalog row, manifest rows, and chunks TOGETHER until
the window passes, then loses all three together on the next execute.
``nx catalog delete``'s "a manual restore stays possible" note
(nexus-xavu7) is therefore genuinely true for the whole grace window.
Consequence for the counts: the stranded-chunk preview IS scoped by
``--older-than-days`` — a small number at the default 30d alongside a
large one at 1d means recent tombstones are being protected, exactly as
designed, not that either number is wrong.

Default mode is a DRY-RUN count report (``dry_run=True`` on the wire) — a
per-dim stranded-chunk count preview plus an
aged-tombstone document count (both age-gated — see above), printed and exit 0. Mutation
(the real ``nexus.purge_trash(interval)`` call) is gated behind BOTH
``--no-dry-run`` AND ``--confirm`` (the reconcile-stale gate pattern,
nexus-cdypx: a forgotten flag reports, it never mutates).

WRITER CONSTRUCTION — deliberate deviation from reconcile-stale's lazy
pattern: reconcile-stale only opens a writer once a run is BOTH
``--no-dry-run`` and ``--confirm`` because its classification report is
computed entirely client-side from reads. Here the count PREVIEW is
itself computed engine-side, behind the write surface (a single
``purge_trash(dry_run=True)`` call carries the same op whether previewing
or mutating — see :meth:`~nexus.catalog.http_catalog_client.
HttpCatalogClient.purge_trash`) — there is no client-side way to produce
that preview without the writer. So this verb constructs the writer up
front, for the default dry-run path too, and closes it in a ``finally``.

Pre-fix engine degradation: a 404 from ``POST /v1/catalog/purge-trash``
(the route not existing on an engine older than nexus-3ck2g) raises a
clear ``ClickException`` naming the required engine release — never a
silent no-op, never a swallowed exception.

POPULATION (nexus-heizf part 3 — historical context for the disjointness
caveat this verb used to print in its output, RETIRED alongside its
counterpart, RDR-191 Phase 6 nexus-o8dil.33):

* THIS verb's stranded-chunk count (``nexus.purge_trash``'s orphan
  predicate): EXISTING ``chunks_<dim>`` rows that ARE manifest-backed but
  have NO LIVE parent document (every manifest row referencing the chash
  belongs to a TOMBSTONED document). Direction: chunk -> parent. UNCHANGED
  by RDR-191 — still a live, real population this verb sweeps.
* Doctor's former dangling-manifest census / the former ``manifest-verify
  --list`` (both RETIRED): manifest rows of LIVE documents whose chash had
  NO backing chunk row at all, direction manifest -> chunk. The
  manifest-chunk FK (catalog-029, VALIDATEd) now REJECTS that state at
  write time, making it structurally unreachable — there is no longer a
  second population this verb's stranded-chunk count could be confused
  with, so the disjointness caveat that used to print alongside every
  invocation (the nexus-55l58 shakedown's mixed-instrument mistake it
  existed to prevent) no longer has a live counterpart to warn against.
"""
from __future__ import annotations

import json

import click
import httpx
import structlog

_log = structlog.get_logger(__name__)

#: nexus-heizf / nexus-h1zu0 code-review fix round (2026-08-05): originally
#: the disjointness caveat vs `nx doctor`'s dangling-manifest census / `nx
#: catalog manifest-verify --list`, printed in the LIVE OUTPUT this command
#: actually prints (text AND --json), not docstring/help only. RDR-191
#: Phase 6 (nexus-o8dil.33), 2026-08-15: both counterparts are RETIRED (the
#: manifest-chunk FK makes the population they diagnosed unreachable), so
#: the disjointness they warned against no longer has a live second
#: instrument to be confused with — this note now simply names the
#: population this verb sweeps, without the (now-moot) comparison.
_POPULATION_NOTE = (
    "population: tombstoned-doc chunks with no live parent (chunk -> "
    "parent direction)"
)


def _echo_result(result: dict, older_than_days: int) -> None:
    """Echo the engine's response, split into the catalog-row count
    (``documents_purged``) and the chunk-storage sweep
    (``chunks_<dim>_stranded`` and anything else) — nexus-8j1zx fix round.

    BOTH sides honor ``--older-than-days`` since catalog-026 (see the module
    docstring's AGE SEMANTICS section — this function's heading claimed the
    chunk counts were NOT age-gated for two weeks after the engine made them
    so, which is what sent nexus-kcm6c hunting a counter bug that was
    actually prose). The split heading remains because the two counts answer
    different questions: rows deleted vs chunk storage reclaimed.
    ``documents_purged`` is the one field name this function hardcodes
    (it is part of the LOCKED nexus-3ck2g wire contract, T1 2fbc12df);
    everything else stays shape-agnostic so a future additional dim's
    ``chunks_<dim>_stranded`` key needs no code change here.
    ``dry_run`` is echoed separately by the caller, not repeated here.

    ``documents_eligible`` (execute responses only, nexus-ff85q) is the
    aged-tombstone population the engine measured in the SAME transaction,
    immediately before the purge ran. It is printed next to
    ``documents_purged``, NOT under the chunk-storage heading — it is an
    age-gated document count, and the shape-agnostic passthrough below
    would otherwise file it with the chunk sweep and repeat the exact
    mislabelling nexus-8j1zx fixed. When it exceeds ``documents_purged``
    the purge took a strict subset of what it found; see
    :func:`purge_trash_cmd` for why that is an ERROR and not a footnote.
    """
    docs_key = "documents_purged"
    eligible_key = "documents_eligible"
    if docs_key in result:
        click.echo(
            f"  {docs_key} (age-gated, tombstoned >= {older_than_days} day(s) ago): "
            f"{result[docs_key]}"
        )
    if eligible_key in result:
        click.echo(
            f"  {eligible_key} (age-gated population measured at purge time): "
            f"{result[eligible_key]}"
        )

    chunk_keys = sorted(k for k in result if k not in ("dry_run", docs_key, eligible_key))
    if chunk_keys:
        click.echo(
            "  Chunk storage swept — age-gated like the row count since "
            "catalog-026: tombstones inside the grace window keep their "
            "chunks (see module docstring):"
        )
        for key in chunk_keys:
            value = result[key]
            if isinstance(value, dict):
                click.echo(f"    {key}:")
                for subkey in sorted(value):
                    click.echo(f"      {subkey}: {value[subkey]}")
            else:
                click.echo(f"    {key}: {value}")


def _raise_engine_floor(exc: httpx.HTTPStatusError) -> None:
    raise click.ClickException(
        "nx catalog purge-trash: this engine does not yet expose "
        "POST /v1/catalog/purge-trash (needs the nexus-3ck2g engine "
        "release — the nexus.purge_trash sweep gained a caller route in "
        "that release). Upgrade the deployed engine-service, then re-run."
    ) from exc


@click.command("purge-trash")
@click.option(
    "--older-than-days", type=int, default=30,
    help="Tombstone age threshold in days (default 30). Must be >= 1.",
)
@click.option(
    "--dry-run/--no-dry-run", default=True,
    help="Report-only (default). Use --no-dry-run (with --confirm) to physically reclaim.",
)
@click.option(
    "--confirm", is_flag=True, default=False,
    help="Required alongside --no-dry-run to actually purge.",
)
@click.option(
    "--json", "json_out", is_flag=True, default=False,
    help="Emit the dry-run report as JSON on stdout. Cannot be combined with --no-dry-run.",
)
def purge_trash_cmd(older_than_days: int, dry_run: bool, confirm: bool, json_out: bool) -> None:
    """Physically reclaim tombstoned catalog rows + their orphaned T3 chunks (nexus-3ck2g).

    Default (no flags) is a read-only preview: per-dim stranded-chunk
    counts and an aged-tombstone document count, BOTH gated by
    --older-than-days (catalog-026: tombstones inside the grace window
    keep row, manifest, and chunks together), computed engine-side via
    ``nexus.purge_trash(older_than_days, dry_run=true)``. Nothing is
    deleted in this mode. A near-zero count at the default 30 days next
    to a large count at ``--older-than-days 1`` means recent tombstones
    are being protected — by design, not a counter defect (nexus-kcm6c).

    \\b
    To actually reclaim:
      nx catalog purge-trash --no-dry-run --confirm
      nx catalog purge-trash --older-than-days 90 --no-dry-run --confirm

    ``--no-dry-run`` alone (without ``--confirm``) is still treated as
    report-only — both flags are required to mutate, same gate as
    ``nx catalog reconcile-stale``.

    See the module docstring for why the writer is constructed up front
    here (deviates from reconcile-stale's lazy-writer pattern) and for
    the pre-nexus-3ck2g engine-floor refusal.

    POPULATION NOTE (nexus-heizf): the stranded-chunk count above is
    tombstoned-doc chunks with no live parent (chunk -> parent direction).
    See the module docstring's POPULATION section — RDR-191 Phase 6
    (nexus-o8dil.33) retired the counterpart population (`nx doctor`'s
    former "dangling manifest chashes" warn / the former `nx catalog
    manifest-verify --list`) this note used to disambiguate against; the
    manifest-chunk FK makes that population unreachable.
    """
    if json_out and not dry_run:
        raise click.ClickException(
            "--json cannot be combined with --no-dry-run: the mutation path "
            "prints a plain-text purge report, not JSON. Run --json alone "
            "for the dry-run preview, then --no-dry-run --confirm separately."
        )
    if older_than_days < 1:
        raise click.ClickException(
            f"--older-than-days must be >= 1, got {older_than_days}"
        )

    from nexus.commands import catalog as _cat_cmd  # noqa: PLC0415 — module-routed helper access keeps import acyclic + monkeypatch-visible

    will_act = (not dry_run) and confirm
    if (not dry_run) and not confirm:
        click.echo(
            "--no-dry-run alone is treated as report-only. "
            "Add --confirm to actually purge."
        )

    writer = _cat_cmd._get_catalog_writer()
    try:
        result = writer.purge_trash(older_than_days=older_than_days, dry_run=not will_act)
    except httpx.HTTPStatusError as exc:
        status = exc.response.status_code if exc.response is not None else 0
        if status == 404:
            _raise_engine_floor(exc)
        raise
    finally:
        writer.close()

    if will_act:
        # nexus-sybbh: local breadcrumb of a REAL purge execution — the
        # independent evidence `nx doctor`'s gc_audit-non-empty check
        # cross-references against the engine's (currently empty) audit
        # table. Best-effort; never affects the purge result above.
        from nexus.gc_purge_marker import record_purge_marker  # noqa: PLC0415 — deferred to avoid a module-load-time import for a rarely-hit branch

        record_purge_marker(result, older_than_days=older_than_days)

    if json_out:
        payload = dict(result)
        payload["population"] = _POPULATION_NOTE
        click.echo(json.dumps(payload, indent=2))
        return

    if will_act:
        click.echo("Purge-trash executed:")
    else:
        click.echo("Purge-trash dry-run:")
    click.echo(f"  [{_POPULATION_NOTE}]")
    _echo_result(result, older_than_days)

    if not will_act:
        click.echo(
            "\n(dry-run — no catalog/T3 rows purged. "
            "Add --no-dry-run --confirm to reclaim.)"
        )
    _log.info(
        "purge_trash_cmd_done",
        older_than_days=older_than_days, will_act=will_act,
    )

    if will_act:
        _fail_on_partial_purge(result, older_than_days)


def _fail_on_partial_purge(result: dict, older_than_days: int) -> None:
    """Refuse to report a partial purge as a plain success (nexus-ff85q).

    THE DEFECT THIS CLOSES: the first production execute purged 2 of the
    63 age-eligible documents its own dry-run had reported, printed a
    completion report, and exited 0. Nothing in the output said "61 of
    these are still here" — the operator's only signal was noticing that a
    follow-up dry-run still listed them. A purge that removes a strict
    SUBSET of the population it measured is by definition incomplete, so
    it exits non-zero. The full report is printed FIRST by the caller and
    the chunk sweep may well have completed, so this is emphatically not
    "nothing happened".

    THE EXIT CODE IS A SIGNAL, NOT A DIAGNOSIS. The engine's own
    ``CatalogRepository.purgeTrash`` treats this same shortfall as a
    soft WARN because one benign cause exists: the eligibility count and
    the purge run as two statements in one READ COMMITTED transaction, so
    a ``nexus.document_restore`` (or a fresh tombstone) committing between
    them legitimately moves the population under the purge's feet. That
    race is real but small — it moves the count by however many documents
    a concurrent operator touched in that window, typically one or two,
    never the 61 production saw. So the message below reports the
    MAGNITUDES and names both readings rather than asserting which
    occurred; the operator, who knows whether anyone else was working the
    trash, is better placed to tell them apart than this function is.

    This deliberately does NOT implement a repeat-detector, a
    magnitude threshold, or a retry. A threshold would need a defensible
    cut point nobody has evidence for, and any of them would reintroduce a
    silent window — the exact property the bead exists to remove. The
    contract is narrow and total: partial is never silent.

    Older engines (pre-nexus-ff85q) do not send ``documents_eligible`` at
    all; the check no-ops rather than inventing a verdict it cannot
    support, so this client stays usable against them.
    """
    eligible = result.get("documents_eligible")
    purged = result.get("documents_purged")
    if not isinstance(eligible, int) or not isinstance(purged, int):
        return
    if purged >= eligible:
        return

    shortfall = eligible - purged
    _log.warning(
        "purge_trash_partial",
        older_than_days=older_than_days,
        documents_eligible=eligible, documents_purged=purged,
        shortfall=shortfall,
    )
    raise click.ClickException(
        f"PARTIAL PURGE: {purged} of {eligible} age-eligible document(s) were "
        f"purged; {shortfall} still eligible. The chunk-storage counts above "
        f"still reflect what was swept.\n"
        f"Two readings, and the magnitude tells them apart:\n"
        f"  - A concurrent tombstone or restore committing between the "
        f"eligibility count and the delete is a legitimate read-committed "
        f"race. A shortfall of 1-2 on an actively-used catalog is plausibly "
        f"that.\n"
        f"  - A larger shortfall is not: it means the purge applied a "
        f"different population than it measured (nexus-ff85q).\n"
        f"Re-run the dry-run (`nx catalog purge-trash --older-than-days "
        f"{older_than_days}`) to see the current eligible count; purge-trash "
        f"is idempotent, so re-running the execute is safe either way."
    )


def register(group: click.Group) -> None:
    """Attach ``purge-trash`` to the shared ``catalog`` group."""
    group.add_command(purge_trash_cmd)
