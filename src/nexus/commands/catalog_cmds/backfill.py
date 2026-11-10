# SPDX-License-Identifier: AGPL-3.0-or-later
# Copyright (c) 2026 Hal Hildebrand. All rights reserved.
"""Backfill verb family for the ``nx catalog`` group.

``backfill-owner-id`` — the one-time RDR-137 P1.5a migration that populated
``collections.owner_id`` on the LOCAL SQLite catalog — died with that catalog
(nexus-i711w terminal deletion): it wrote through a raw ``cat._db`` SQLite
handle by contract and already refused in service mode, which is now the only
mode.

``backfill-source-uri`` (nexus-poigc) is the live verb in this family: it
re-derives ``chroma://`` catalog ``source_uri`` values for filesystem-backed
collections (``rdr__``/``docs__``/``code__``) whose identity was minted by an
older ``uri_for`` that predated those prefixes routing to ``file://``. See
the command's own docstring for the mechanism and safety rails.

``backfill-etags`` (nexus-0ne1m critique, significant #2) is the second live
verb: every ``https://`` reference registered/refreshed BEFORE nexus-0ne1m
shipped has no recorded ``ETag`` and reads 'unknown' forever under
``nx doctor --check-references`` unless an operator individually re-runs
``nx catalog update <tumbler> --source-uri <same-url>``. This is the bulk
form, following this file's own ``backfill-source-uri`` shape: a plain
report first, ``--dry-run`` for a preview with zero network calls, and
``--limit`` to keep one run bounded rather than an unbounded fan-out.
``NX_REFERENCE_ETAG_CAPTURE=0`` (round-2 critique, nexus-0ne1m/nexus-tb2yj)
governs this sweep too, not only the register/update write path: the check
lives in ``aspect_readers.capture_https_etag`` itself, the single function
this command's loop calls, so setting it makes every candidate here read
'no ETag to capture' with zero outbound HEAD requests, exactly like
``--dry-run``'s zero-network guarantee.

``backfill-meta`` (nexus-6pfgb.10, RDR-236 Phase 1) is the third: the one-time, fill-only stamp of the
catalog ``meta`` keys the Phase 1 homes now write at index and put time, for documents written before
them. The rules live in :mod:`nexus.catalog.meta_backfill`.
"""
from __future__ import annotations

import json

import click


@click.command(name="backfill-source-uri")
@click.option(
    "--apply", is_flag=True,
    help="Perform the rewrite (default: dry-run report only).",
)
@click.option(
    "--json", "as_json", is_flag=True,
    help="Emit JSON instead of the human-readable report.",
)
def backfill_source_uri_cmd(apply: bool, as_json: bool) -> None:
    """Re-derive ``chroma://`` catalog ``source_uri`` values (nexus-poigc).

    A legacy ``chroma://`` spelling was ALSO the store_put-origin marker of
    ``knowledge__`` documents minted via ``nx store put`` / MCP ``store_put``
    (now ``nx-note://``, RDR-231). Those rows are legacy NOTES: the engine's
    backfill rewrites them to ``nx-note://``, so this verb leaves them alone.
    This verb ONLY rewrites
    rows whose PHYSICAL COLLECTION routes to ``file://`` in
    ``nexus.aspect_readers.uri_for`` (the ``rdr__``/``docs__``/``code__``
    prefixes, ``FILE_ROUTED_PREFIXES``): those rows carry a ``chroma://``
    identity ONLY because they were minted by an older ``uri_for`` build
    that predated those prefixes routing to ``file://`` — migration
    residue, not a legitimate origin marker.

    For each candidate row, parses the path component out of the stored
    ``chroma://<collection>/<path>`` URI and re-derives through the
    CURRENT ``uri_for(collection, path)``. Refuses (reports, never
    writes) a row whose parsed path is not absolute — there is no
    ``repo_root`` to anchor a relative one at backfill time, and this
    verb never guesses at one.

    Always prints a per-collection census of EVERY ``chroma://`` row
    first — candidates (file-routed, rewritten under ``--apply``) vs.
    legacy notes (left for the engine backfill) — regardless of ``--apply``,
    so the number is explained rather than eyeballed.
    """
    from collections import Counter  # noqa: PLC0415 — command-local

    from nexus.aspect_readers import FILE_ROUTED_PREFIXES, uri_for  # noqa: PLC0415 — command-local
    from nexus.commands import catalog as _cat_cmd  # noqa: PLC0415 — module-routed helper access keeps import acyclic + monkeypatch-visible

    cat = _cat_cmd._get_catalog()

    chroma_rows = [
        d for d in cat.all_documents()
        if (getattr(d, "source_uri", "") or "").startswith("chroma://")
    ]

    by_collection = Counter(d.physical_collection for d in chroma_rows)
    census = {
        coll: {
            "count": n,
            "candidate": coll.startswith(FILE_ROUTED_PREFIXES),
        }
        for coll, n in sorted(by_collection.items())
    }

    candidates = [d for d in chroma_rows if d.physical_collection.startswith(FILE_ROUTED_PREFIXES)]

    updates: list[tuple[object, str]] = []
    refused: list[tuple[object, str]] = []
    for d in candidates:
        uri = d.source_uri or ""
        prefix = f"chroma://{d.physical_collection}/"
        if not uri.startswith(prefix):
            refused.append((d, f"malformed chroma:// URI: does not start with {prefix!r}"))
            continue
        path = uri[len(prefix):]
        if not path.startswith("/"):
            refused.append((d, f"parsed path is not absolute: {path!r}"))
            continue
        new_uri = uri_for(d.physical_collection, path)
        if not new_uri:
            refused.append((d, f"uri_for did not re-derive a file:// URI (got {new_uri!r})"))
            continue
        updates.append((d, new_uri))

    report = {
        "total_chroma_rows": len(chroma_rows),
        "by_collection": census,
        "candidates": len(candidates),
        "rederivable": len(updates),
        "refused": len(refused),
        "apply": apply,
        "updates": [
            {"tumbler": str(d.tumbler), "collection": d.physical_collection,
             "was": d.source_uri, "now": new_uri}
            for d, new_uri in updates
        ],
        "refusals": [
            {"tumbler": str(d.tumbler), "collection": d.physical_collection,
             "source_uri": d.source_uri, "reason": reason}
            for d, reason in refused
        ],
    }

    if as_json:
        click.echo(json.dumps(report, indent=2, default=str))
    else:
        click.echo(f"chroma:// source_uri rows: {report['total_chroma_rows']}")
        for coll, info in census.items():
            tag = "candidate" if info["candidate"] else "legacy note (the engine backfill rewrites it to nx-note://)"
            click.echo(f"  {coll}: {info['count']}  [{tag}]")
        click.echo(
            f"Re-derivable: {report['rederivable']}, refused: {report['refused']}"
        )
        for d, reason in refused[:10]:
            click.echo(f"  REFUSED {d.tumbler} {d.source_uri!r}: {reason}")
        if len(refused) > 10:
            click.echo(f"  … {len(refused) - 10} more refusal(s) (--json for all)")

    if not updates:
        if not as_json:
            click.echo("No file-routed chroma:// rows to backfill.")
        return

    if not apply:
        if not as_json:
            click.echo(f"Dry-run: would rewrite {len(updates)} row(s). Re-run with --apply.")
            for d, new_uri in updates[:10]:
                click.echo(f"  {d.tumbler}  {d.source_uri!r} -> {new_uri!r}")
            if len(updates) > 10:
                click.echo(f"  … {len(updates) - 10} more (--json for all)")
        return

    writer = _cat_cmd._get_catalog_writer()
    try:
        payload = [
            {"tumbler": str(d.tumbler), "source_uri": new_uri}
            for d, new_uri in updates
        ]
        counts = writer.update_many(payload)
    finally:
        writer.close()
    updated = sum(1 for c in counts if c)
    click.echo(f"Rewrote {updated}/{len(updates)} source_uri value(s).")
    if updated != len(updates):
        raise click.exceptions.Exit(1)


@click.command(name="backfill-etags")
@click.option(
    "--owner", default="",
    help="Restrict to this owner tumbler (e.g. 1.1). Default: every owner.",
)
@click.option(
    "--dry-run", is_flag=True,
    help="Report what would be captured; makes NO network calls and writes nothing.",
)
@click.option(
    "--limit", type=click.IntRange(min=1, max=2000), default=500, show_default=True,
    help="Cap on candidate documents processed in one run — a bounded sweep, "
         "never an unlimited fan-out. Re-run to continue past the cap; "
         "idempotent, so already-recorded documents are excluded from the "
         "next run's candidates automatically.",
)
def backfill_etags_cmd(owner: str, dry_run: bool, limit: int) -> None:
    """Capture the ``ETag`` for every ``https://`` reference that lacks one
    (nexus-0ne1m critique, significant #2).

    Every ``https://`` reference registered or refreshed BEFORE nexus-0ne1m
    shipped has no ``catalog_documents.metadata[HTTPS_ETAG_META_KEY]`` and
    reads 'unknown' forever under ``nx doctor --check-references`` — this is
    the bulk remedy. Candidates: live (non-alias) documents whose
    ``source_uri`` starts with ``https://`` and whose ``meta`` does not
    already carry an ETag. IDEMPOTENT by construction: a document that
    already has one is excluded from candidates on every run, so re-running
    after a partial run (or after the ``--limit`` cap) only touches what is
    still missing.

    ``--dry-run`` lists the candidates and makes NO network call and NO
    write at all. Without it, one bounded HEAD request per candidate
    (``capture_https_etag``, the same tight write-path budget
    ``nx catalog register``/``update`` use — see
    ``aspect_readers.HTTPS_ETAG_CAPTURE_TIMEOUT_S``) shares ONE
    ``httpx.Client`` across the whole run. A document whose HEAD fails or
    returns no ``ETag`` header is reported, not retried; a document whose
    catalog write fails is reported and counted as a failure (exit 1),
    without aborting the rest of the run.

    ``NX_REFERENCE_ETAG_CAPTURE=0`` (round-2 critique, nexus-0ne1m/
    nexus-tb2yj) disables capture here too: ``capture_https_etag`` checks
    it before ever touching the shared ``httpx.Client``, so every
    candidate comes back with no ETag and this run makes ZERO outbound
    HEAD requests — the same "no outbound HTTP from capture" promise the
    register/update write path makes, honored here without a second,
    separately-maintained check.
    """
    from nexus.aspect_readers import (  # noqa: PLC0415 — command-local
        HTTPS_ETAG_CAPTURE_CONNECT_TIMEOUT_S,
        HTTPS_ETAG_CAPTURE_TIMEOUT_S,
        HTTPS_ETAG_META_KEY,
        NX_REFERENCE_ETAG_CAPTURE_ENV,
        _https_etag_capture_enabled,
        capture_https_etag,
    )
    from nexus.catalog.tumbler import Tumbler  # noqa: PLC0415 — command-local
    from nexus.commands import catalog as _cat_cmd  # noqa: PLC0415 — module-routed helper access keeps import acyclic + monkeypatch-visible

    cat = _cat_cmd._get_catalog()
    entries = cat.by_owner(Tumbler.parse(owner)) if owner else cat.all_documents(limit=0)

    candidates = [
        e for e in entries
        if not e.alias_of
        and (e.source_uri or "").startswith("https://")
        and not (e.meta or {}).get(HTTPS_ETAG_META_KEY)
    ]
    scoped = candidates[:limit]

    click.echo(
        f"https:// reference(s) missing an ETag: {len(candidates)}"
        + (f" under owner {owner}" if owner else "")
        + f"; this run processes up to --limit {limit}: {len(scoped)}"
    )

    if dry_run:
        for e in scoped[:20]:
            click.echo(f"  would capture: {e.tumbler}  {e.source_uri}")
        if len(scoped) > 20:
            click.echo(f"  ... and {len(scoped) - 20} more")
        click.echo("Dry-run: no network calls made, nothing recorded. Re-run without --dry-run to apply.")
        return

    if not scoped:
        click.echo("Nothing to backfill.")
        return

    # capture_https_etag enforces the opt-out on its own; saying so here as
    # well keeps an operator who inherited the variable from reading
    # "HEAD failed or no ETag header" for documents nothing ever contacted.
    if not _https_etag_capture_enabled():
        click.echo(
            f"ETag capture is disabled ({NX_REFERENCE_ETAG_CAPTURE_ENV}=0): "
            f"{len(scoped)} document(s) left without an ETag; no network calls "
            "made, nothing recorded."
        )
        return

    import httpx  # noqa: PLC0415 — optional/heavy dependency deferred (httpx)

    writer = _cat_cmd._get_catalog_writer()
    recorded = 0
    no_etag = 0
    failed = 0
    try:
        with httpx.Client(
            timeout=httpx.Timeout(
                HTTPS_ETAG_CAPTURE_TIMEOUT_S, connect=HTTPS_ETAG_CAPTURE_CONNECT_TIMEOUT_S,
            ),
            follow_redirects=True,
        ) as client:
            for e in scoped:
                etag = capture_https_etag(e.source_uri, http_client=client)
                if not etag:
                    no_etag += 1
                    continue
                try:
                    writer.update(e.tumbler, meta={HTTPS_ETAG_META_KEY: etag})
                    recorded += 1
                except Exception as exc:  # noqa: BLE001 — one row's failure is reported, never aborts the sweep
                    failed += 1
                    click.echo(f"  FAILED to record {e.tumbler}: {type(exc).__name__}: {exc}", err=True)
    finally:
        writer.close()

    click.echo(
        f"Recorded {recorded} ETag(s); {no_etag} had none to capture "
        f"(HEAD failed or no ETag header); {failed} write failure(s)."
    )
    if failed:
        raise click.exceptions.Exit(1)


@click.command(name="backfill-meta")
@click.option(
    "--apply", is_flag=True,
    help="Write the keys (default: dry run, counts only).",
)
@click.option(
    "--json", "as_json", is_flag=True,
    help="Emit JSON instead of the human-readable report.",
)
@click.option(
    "--batch-size", type=click.IntRange(min=1), default=50, show_default=True,
    help="Documents examined per round: each round reads every collection it needs once, then writes once.",
)
@click.option(
    "--pause-ms", type=click.IntRange(min=0), default=0, show_default=True,
    help="Sleep this many milliseconds after each round's reads and again after its write (0: no pause).",
)
@click.option(
    "--max-rps", type=click.FloatRange(min=0, min_open=True), default=None,
    help="Hard limit on HTTP requests a second, enforced on every request the command sends: listing pages, "
         "manifest, owner and store-get pages, writes, retries and fallbacks (default: no limit). "
         "THE guard for a shared or cloud service.",
)
@click.option(
    "--stop-after-failed-rounds", type=click.IntRange(min=0), default=3, show_default=True,
    help="Stop cleanly (resumable, exit 1) after this many consecutive rounds with a failed lookup or write "
         "(0: never).",
)
@click.option(
    "--start-after", metavar="TUMBLER", default="",
    help="Resume: skip the listing up to and including this tumbler (the interrupt message prints the exact "
         "command). The listing is in the catalog's own order, so the skip is by position, not by comparing tumblers.",
)
@click.option(
    "--list-ttl-first-sign", "list_first_sign", is_flag=True,
    help="Dry run only: also list the notes whose expiry RDR-236 Step 1 changes (the TTL sweep's guard held them "
         "by the first sign of catalog_ttl_stale alone), with whether each has already lapsed. Reads the "
         "manifests and chunk rows (text included) of every note with a TTL: see LOAD below. Refused with --apply.",
)
@click.option(
    "--list-ttl-first-sign-only", "list_only", is_flag=True,
    help="Dry run only: --list-ttl-first-sign without the stamping plan (no owner lookups, no document rows, "
         "no per-key counts). Refused with --apply.",
)
def backfill_meta_cmd(
    apply: bool, as_json: bool, batch_size: int, pause_ms: int, max_rps: float | None,
    stop_after_failed_rounds: int, start_after: str, list_first_sign: bool, list_only: bool,
) -> None:
    """Stamp the new catalog meta keys of pre-existing documents from their chunk rows (nexus-6pfgb.10).

    One-time and fill-only: a key the entry already holds (a retraction value included) and a non-empty
    catalog column are never touched, and a key is sent only with a real value. Notes (Sam's option B)
    take ``ttl_days`` and ``put_at`` only from a row they wrote themselves, and ``tags``, ``category`` and
    ``source_agent`` from that row or, failing one, from a piece they solely own. Indexed documents take
    ``extraction_method``, ``quality_gate_overridden``, ``extraction_source``, ``frecency_score`` and the
    catalog ``author`` from chunks they SOLELY own (a chunk two documents share keeps its last writer's
    value, so a document whose chunks are all shared is skipped and counted), and the derived keys
    ``source_format``, ``tags`` and ``category`` from the entry itself.

    The dry run (default) prints per key how many documents were examined, already held it, were skipped
    as shared, would be stamped, or are residue (with the reasons under ``--json``). A run that examines
    no document exits non-zero (a resume past the last document is the one exception: it says so, exits 0).

    LOAD ON A SHARED SERVICE. Every pass, dry run or apply, lists the whole catalog and reads the chunk rows
    of most documents again: the keys are filled only where absent, so a dry run and the apply that follows
    each cost the full read. A round of ``--batch-size`` documents sends one manifest request, one owner
    request, ceil(chunks/300) store-get requests per collection it touches (chunk text included) and one
    write; the count depends on the chunks the round touches, not on a fixed number. ``--max-rps`` is the
    guard: it holds EVERY request to that rate, retries and fallbacks included; ``--pause-ms`` only spaces
    the rounds. For a cloud catalog start with ``--max-rps 2``. Keep the catalog's other writers (indexers,
    hooks, ``nx store put``) quiet for the whole of every pass, not only the apply: the write merges a
    key into ``meta`` and overwrites one a writer set after the listing was taken.

    THE FIRST-SIGN LISTING (nexus-6pfgb.65, RDR-236 Phase 4 Step 1) runs only under ``--list-ttl-first-sign``
    (or ``--list-ttl-first-sign-only``), on a dry run; both are refused with ``--apply``. It lists, in the
    summary and under ``ttl_first_sign_only`` in ``--json``, the notes whose expiry Step 1 changes: a note with
    a catalog TTL that ``note_ttl.catalog_ttl_stale`` held by its first sign alone, because a row's TTL differs
    from the catalog's while the row's ``indexed_at`` is not later than ``put_at`` plus five minutes (a pre-.18
    import's permanent row, or a note whose owner changed its TTL with a new-client re-put), or because the
    row has no ``indexed_at`` at all (reason ``first sign, no indexed_at``). Each says when it expires and
    whether it has already lapsed: a lapsed one is what the first sweep after Step 1 ships would retire. Step
    1 stops holding them; the catalog's TTL is the authority. Read-only: it stamps nothing and changes no
    expiry decision.

    LISTING LOAD. The listing reads what the stamping plan does not: for every note with a ``put_at`` and a
    catalog TTL, its manifest (one request per ``--batch-size`` documents) and its chunk rows
    (ceil(chunks/300) store-get requests per collection a round touches, chunk text included), on top of the
    pass's own reads. ``--list-ttl-first-sign-only`` makes only those reads, after the catalog listing: no
    owner lookups, no document rows, no per-key counts. When any keyed note could not be read the list is
    incomplete and the command exits 1.

    One progress line per round (about 300 lines for a long run) goes to stderr, so ``--json`` on stdout is
    unchanged: requests sent, the rate since the start and over the last 10 rounds, failed lookups and
    writes. Ctrl-C or SIGTERM stops after the round it is in, prints the report of the finished rounds and
    the exact command to resume (``--start-after``); the verb is fill-only, so running it again is safe. A
    second signal aborts the round at once: its counts are lost and its write may already have landed.
    ``--stop-after-failed-rounds`` stops the same way after that many failing rounds in a row.

    Exit codes: 0 finished; 1 a write failed (even when also interrupted), the run stopped on failed rounds,
    or the first-sign listing could not read every keyed note; 130 stopped by Ctrl-C; 143 stopped by SIGTERM;
    2 a usage error.
    """
    if apply and (list_first_sign or list_only):
        raise click.UsageError("--list-ttl-first-sign and --list-ttl-first-sign-only are dry-run options: they cannot be combined with --apply.")
    import signal  # noqa: PLC0415 — command-local

    from nexus.catalog.meta_backfill import (  # noqa: PLC0415 — command-local
        STOP_FAILED_ROUNDS, ResumePointError, VacuousRunError, backfill_meta, stop_after_current_batch,
    )
    from nexus.commands import catalog as _cat_cmd  # noqa: PLC0415 — module-routed helper access keeps import acyclic + monkeypatch-visible
    from nexus.db import make_t3  # noqa: PLC0415 — command-local
    from nexus.db import request_gate  # noqa: PLC0415 — command-local

    gate = request_gate.RequestGate(max_rps)
    writer = None
    try:
        with request_gate.paced(gate), stop_after_current_batch() as stop:
            reader = _cat_cmd._get_catalog()
            writer = _cat_cmd._get_catalog_writer() if apply else None
            report = backfill_meta(
                reader, make_t3(), writer, apply=apply, batch=batch_size, pause_ms=pause_ms,
                progress=lambda p: click.echo(p.line(), err=True), should_stop=stop,
                requests=lambda: gate.count, stop_after_failed_rounds=stop_after_failed_rounds,
                start_after=start_after, list_first_sign=list_first_sign, list_only=list_only,
            )
    except (VacuousRunError, ResumePointError) as exc:
        raise click.ClickException(str(exc)) from exc
    finally:
        if writer is not None:
            writer.close()

    if as_json:
        click.echo(json.dumps(report.as_dict(), indent=2, default=str))
    else:
        verb = "Stamped" if apply else "Would stamp"
        if report.list_only:
            click.echo(
                f"Examined {report.documents_examined} documents ({report.documents_notes} notes); "
                "listing only, no stamping plan was made."
            )
        else:
            click.echo(
                f"Examined {report.documents_examined} documents ({report.documents_notes} notes): "
                f"{report.documents_complete} complete, {report.documents_all_shared} with only shared chunks, "
                f"{verb.lower()} {report.documents_to_stamp}."
            )
            click.echo(
                f"  note.* keys cover the {report.documents_notes} note(s); "
                f"doc.* keys cover the {report.documents_examined - report.documents_notes} non-note document(s)."
            )
        for name, st in sorted(report.keys.items()):
            why = f" ({', '.join(f'{r} {n}' for r, n in sorted(st.residue.items()))})" if st.residue else ""
            click.echo(
                f"  {name}: examined {st.examined}, held {st.present}, skipped (shared) {st.shared}, "
                f"{'stamped' if apply else 'would stamp'} {st.stamped}, residue {sum(st.residue.values())}{why}"
            )
        click.echo(f"  {report.repo_owners} repository owner(s) seen for the repo-collection tags rule (nexus-6pfgb.43)"
                   + ("; the owner or collection listing FAILED, so the rule was off." if report.repo_listing_failed else "."))
        if report.tags_legacy_strings_left:
            click.echo(f"  {report.tags_legacy_strings_left} legacy tags strings left for the array rung (nexus-6pfgb.38).")
        if report.lookups_failed:
            click.echo(f"  {report.lookups_failed} documents had an owner or row lookup fail; their row-sourced keys were withheld.")
        if report.ttl_first_sign_checked:
            held = report.ttl_first_sign_notes
            click.echo(
                f"  {len(held)} note(s) held today by catalog_ttl_stale's first sign alone (the row's TTL differs from the "
                "catalog's while the row's indexed_at is not later than put_at plus five minutes, or the row has no "
                "indexed_at: a pre-.18 import's permanent row, or a note whose owner changed its TTL with a new-client "
                "re-put). RDR-236 Step 1 stops holding them; the catalog's TTL is the authority."
            )
            for n in held:
                click.echo(
                    f"    {n['tumbler']}  {n['title']!r}  {n['collection']}: catalog ttl_days {n['catalog_ttl_days']} "
                    f"put_at {n['put_at']}; row ttl_days {n['row_ttl_days']} indexed_at {n['row_indexed_at']} "
                    f"({n['rows']} row(s)); {n['reason']}; expires {n['expires_at']}, "
                    f"{'lapsed already' if n['lapsed_now'] else 'not lapsed yet'}"
                )
            if report.ttl_first_sign_unread:
                click.echo(
                    f"  {report.ttl_first_sign_unread} keyed note(s) could not be read (manifest or row read failed): "
                    "the list is incomplete."
                )
        if apply:
            click.echo(f"{verb} {report.documents_written}/{report.documents_to_stamp} documents.")
        elif report.list_only:
            click.echo("Listing only: nothing written.")
        else:
            click.echo("Dry run: nothing written. Re-run with --apply.")
    if as_json and report.ttl_first_sign_unread:
        click.echo(
            f"{report.ttl_first_sign_unread} keyed note(s) could not be read (manifest or row read failed): "
            "the ttl_first_sign_only list is incomplete.", err=True,
        )
    if start_after and not report.batches_total:
        click.echo(f"Nothing after {start_after}: it is the last document of the listing, so an earlier pass reached the end.", err=True)
    if report.interrupted:
        resume = ["nx catalog backfill-meta"] + (["--apply"] if apply else []) + (["--json"] if as_json else []) + (
            ["--list-ttl-first-sign-only"] if list_only else ["--list-ttl-first-sign"] if list_first_sign else []) + [
            f"--batch-size {batch_size}", f"--pause-ms {pause_ms}",
            *([f"--max-rps {max_rps:g}"] if max_rps is not None else []),
            f"--stop-after-failed-rounds {stop_after_failed_rounds}",
            *([f"--start-after {report.resume_after}"] if report.resume_after else []),
        ]
        if report.stop_reason == STOP_FAILED_ROUNDS:
            click.echo(
                f"Stopped after {report.batches_done} of {report.batches_total} batches: {stop_after_failed_rounds} rounds in a row "
                f"had a failed lookup or write ({report.lookups_failed} lookups failed, {report.writes_failed} writes failed in all). "
                "Check the service, then resume (the verb is fill-only).",
                err=True,
            )
        else:
            name = signal.Signals(stop.signum or signal.SIGINT).name
            click.echo(
                f"Interrupted by {name} after {report.batches_done} of {report.batches_total} batches "
                f"({report.documents_examined} documents examined); the report above covers the finished batches only.",
                err=True,
            )
            if report.cut_batch:
                click.echo(
                    f"Batch {report.cut_batch} was cut by a second interrupt: it is NOT in the report above, and its writes "
                    "may already have landed. The verb is fill-only, so redoing it is safe.",
                    err=True,
                )
        click.echo("Resume with: " + " ".join(resume), err=True)
        if report.writes_failed:
            click.echo(f"{report.writes_failed} writes failed in the finished batches: exit 1, which outranks the interrupt's.", err=True)
        if report.writes_failed or report.ttl_first_sign_unread or report.stop_reason == STOP_FAILED_ROUNDS:
            raise click.exceptions.Exit(1)    # a failure outranks the interrupt: 130 must not hide it
        raise click.exceptions.Exit(128 + int(stop.signum or signal.SIGINT))
    if report.writes_failed or report.ttl_first_sign_unread:
        raise click.exceptions.Exit(1)


def register(group: click.Group) -> None:
    """Attach the live backfill verb(s) to the shared ``catalog`` group."""
    group.add_command(backfill_source_uri_cmd)
    group.add_command(backfill_etags_cmd)
    group.add_command(backfill_meta_cmd)
