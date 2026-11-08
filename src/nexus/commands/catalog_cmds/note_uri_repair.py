# SPDX-License-Identifier: AGPL-3.0-or-later
# Copyright (c) 2026 Hal Hildebrand. All rights reserved.
"""``nx catalog repair-note-uris`` (nexus-bivz3.24): RDR-231 critique Issue 7, the repair half.

A note moved by re-home, a collection update or a rename before RDR-231 step 4 keeps a ``source_uri`` that
names the collection it left: ``nx-note://X/t`` on a row living in ``Y``. The step-5 backfill changes only the
scheme, so the row stays stale, and a re-put under ``Y`` misses it and mints a duplicate. This verb counts those
rows and repairs the ones that are safe to repair.

Dry run by default, always prints the census, ``--apply`` to write, ``--json`` for the report (the shape of
``backfill-source-uri``). The census and the rewrite are the engine's (``POST /v1/catalog/note_uri_repair``):
one SQL census per tenant, and a rewrite of the catalog row, its aspect row and its highlight row in ONE
transaction that refuses (409, nothing written) when a live document already holds, or takes while the call
runs, the target URI.

What it does NOT do, by design:

* **No merge.** A row whose target URI a live document already holds (a twin), or that would rewrite onto the
  same URI as another stale row, is listed and left alone. ``POST /v1/catalog/merge`` moves neither chunks nor
  aspects nor highlights, so which row wins is a design choice that waits for Sam.
* **No tombstoned rows.** Counted, not rewritten: only live rows can mint a duplicate.
* **No ``nxexp://`` rows.** An export copy's inner collection names the SOURCE of the export by design, so it is
  not stale; the census reports the copies whose *target* segment is no longer the collection they live in. No
  mover rewrites an ``nxexp://`` value, so there is no rule to repair against.
"""
from __future__ import annotations

import json
from typing import Any

import click
import httpx

#: The counts a report must carry, as integers, before anything is written on its strength.
_COUNT_KEYS: tuple[str, ...] = (
    "stale_live", "rewrite", "merge", "merge_twin", "merge_sibling", "refused",
    "refused_file_routed_collection", "refused_malformed", "tombstoned", "export_copies",
    "export_copies_target_moved",
)
_SAMPLE_KEYS: tuple[str, ...] = ("rewrite", "merge", "refused", "tombstoned", "export_target_moved")


def _check_report(report: Any) -> dict:
    """The engine's report, or a ClickException naming what is wrong with it (nothing is written on a bad one)."""
    if not isinstance(report, dict):
        raise click.ClickException(f"unexpected report from the engine (not an object): {report!r}")
    for key in _COUNT_KEYS:
        value = report.get(key)
        if isinstance(value, bool) or not isinstance(value, int):
            raise click.ClickException(
                f"unexpected report from the engine: {key!r} is {value!r}, not an integer. Nothing was written."
            )
    sample = report.get("sample")
    if not isinstance(sample, dict) or any(not isinstance(sample.get(k), list) for k in _SAMPLE_KEYS):
        raise click.ClickException(
            "unexpected report from the engine: 'sample' is not an object of lists. Nothing was written."
        )
    return report


def _call(writer: Any, verb_args: dict) -> dict:
    """One engine call, with the refusals every call shares."""
    from nexus.commands import catalog as _cat_cmd  # noqa: PLC0415 — module-routed helper access keeps import acyclic + monkeypatch-visible

    try:
        return _check_report(writer.repair_stale_note_uris(**verb_args))
    except httpx.HTTPStatusError as exc:
        if exc.response is not None and exc.response.status_code == 404:
            raise click.ClickException(
                "nx catalog repair-note-uris: this engine does not yet expose POST /v1/catalog/note_uri_repair "
                "(needs the nexus-bivz3.24 engine release). Upgrade the deployed engine-service, then re-run."
            ) from exc
        raise
    except httpx.TimeoutException as exc:
        raise _cat_cmd._service_timeout_error("repair-note-uris", exc) from exc


def _refusal_text(exc: httpx.HTTPStatusError) -> str:
    try:
        body = exc.response.json()
        if isinstance(body, dict) and body.get("error"):
            return str(body["error"])
    except Exception:  # noqa: BLE001 — a body that is not JSON falls back to the status line
        pass
    return str(exc)


def _print_census(report: dict) -> None:
    echo = click.echo
    echo(f"stale note rows (live): {report['stale_live']}")
    echo(f"  rewrite candidates (no live twin): {report['rewrite']}")
    echo(
        f"  merge candidates: {report['merge']} ({report['merge_twin']} with a live twin, "
        f"{report['merge_sibling']} rewriting onto one URI with another stale row)"
    )
    echo(
        f"  refused: {report['refused']} ({report['refused_file_routed_collection']} in a file-routed collection, "
        f"{report['refused_malformed']} with no title part)"
    )
    echo(f"tombstoned stale rows (left as they are): {report['tombstoned']}")
    echo(
        f"nxexp:// export copies: {report['export_copies']}, {report['export_copies_target_moved']} whose target "
        "collection is not the one they live in (census only)"
    )
    sample = report["sample"]
    for row in sample["rewrite"]:
        echo(f"  rewrite  {row.get('tumbler')}  {row.get('source_uri')}  ->  {row.get('new_uri')}")
    for row in sample["merge"]:
        echo(
            f"  merge?   {row.get('tumbler')}  {row.get('source_uri')}  ->  {row.get('new_uri')}"
            f"  (twin {row.get('twin')}, {row.get('reason')})"
        )
    for row in sample["refused"]:
        echo(f"  refused  {row.get('tumbler')}  {row.get('source_uri')}  [{row.get('reason')}]")
    shown = sum(len(sample[k]) for k in _SAMPLE_KEYS)
    listed = (
        report["rewrite"] + report["merge"] + report["refused"] + report["tombstoned"]
        + report["export_copies_target_moved"]
    )
    if shown < listed:
        echo(f"  ... {listed - shown} more (raise --sample, or read --json)")
    if report["merge"]:
        echo(
            "Merge candidates are listed and not merged: which row wins (the stale-URI row or its twin) is a "
            "design decision, and POST /v1/catalog/merge moves neither chunks, aspects nor highlights."
        )


@click.command(name="repair-note-uris")
@click.option("--apply", is_flag=True, help="Perform the rewrite (default: dry-run census only).")
@click.option("--json", "as_json", is_flag=True, help="Emit JSON instead of the human-readable report.")
@click.option(
    "--sample", type=click.IntRange(0, 500), default=20, show_default=True,
    help="Example rows listed per class. The counts are never capped.",
)
@click.option(
    "--batch-size", "batch_size", type=click.IntRange(1, 5000), default=1000, show_default=True,
    help="Rewrite candidates ONE engine transaction rewrites; --apply repeats until none are left.",
)
def repair_note_uris_cmd(apply: bool, as_json: bool, sample: int, batch_size: int) -> None:
    """Repair note URIs that name a collection the note no longer lives in (nexus-bivz3.24).

    \b
    Always prints the census first; writes only with --apply.
      rewrite candidates  no live note holds nx-note://<its collection>/<title>: the URI is rewritten, in the
                          catalog row, its aspect row and its highlight row, in one transaction
      merge candidates    a live twin holds the target URI (or two stale rows share it): listed, NOT merged
      refused             file-routed collection, or no title part: listed, never rewritten
      tombstoned          counted, never rewritten
      nxexp:// copies     census only
    A twin that appears while --apply runs is a 409 that names it: nothing is written for that batch, and the
    row is a merge candidate on the next census. Run the dry run first, and a census against a cloud tenant only
    with the operator's go.

    \b
    Examples:
      nx catalog repair-note-uris                # census
      nx catalog repair-note-uris --json         # census as JSON
      nx catalog repair-note-uris --apply        # rewrite every rewrite candidate
    """
    from nexus.commands import catalog as _cat_cmd  # noqa: PLC0415 — module-routed helper access keeps import acyclic + monkeypatch-visible

    writer = _cat_cmd._get_catalog_writer()
    try:
        before = _call(writer, {"apply": False, "limit": batch_size, "sample": sample})
        if not apply:
            if as_json:
                click.echo(json.dumps(before, indent=2))
            else:
                _print_census(before)
                click.echo("Dry run: nothing written. Re-run with --apply to rewrite the rewrite candidates.")
            return

        if not as_json:
            _print_census(before)
        rewritten = 0
        batches = 0
        stalled = False
        after = before
        if before["rewrite"] == 0:
            if not as_json:
                click.echo("Nothing to rewrite.")
        else:
            while True:
                try:
                    step = _call(writer, {"apply": True, "limit": batch_size, "sample": 0})
                except httpx.HTTPStatusError as exc:
                    if exc.response is not None and exc.response.status_code == 409:
                        raise click.ClickException(
                            f"{_refusal_text(exc)}\n{rewritten} note URI(s) were rewritten before it; that batch "
                            "wrote nothing. Run the census again: the row is a merge candidate now."
                        ) from exc
                    raise
                done = step.get("rewritten")
                if isinstance(done, bool) or not isinstance(done, int) or not isinstance(step.get("remaining"), int):
                    raise click.ClickException(
                        f"unexpected report from the engine: apply returned rewritten={done!r}, "
                        f"remaining={step.get('remaining')!r}."
                    )
                rewritten += done
                batches += 1
                if step["remaining"] == 0:
                    break
                if done == 0:
                    stalled = True
                    break
            after = _call(writer, {"apply": False, "limit": batch_size, "sample": sample})
    finally:
        writer.close()

    if as_json:
        click.echo(json.dumps({"before": before, "rewritten": rewritten, "batches": batches, "after": after}, indent=2))
    else:
        click.echo(f"Rewrote {rewritten} note URI(s) in {batches} batch(es).")
        if batches:
            click.echo("Census after:")
            _print_census(after)
    if stalled:
        click.echo(
            "The last batch made no progress while rewrite candidates remain: stopping. Run the census again.",
            err=True,
        )
        raise click.exceptions.Exit(1)


def register(group: click.Group) -> None:
    """Attach ``repair-note-uris`` to the shared ``catalog`` group."""
    group.add_command(repair_note_uris_cmd)
