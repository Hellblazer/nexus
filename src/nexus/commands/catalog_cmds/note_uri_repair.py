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
* **No tombstoned rows.** Counted, not rewritten: only live rows can mint a duplicate. A restore makes a stale
  tombstone live again, so it appears in the next census as a rewrite or merge candidate.
* **No ``nxexp://`` rows (scope, and a risk that grows).** An export copy's inner collection names the SOURCE of
  the export by design, so it is not stale; the census reports the copies whose *target* segment is no longer the
  collection they live in. ``NoteUri.moveTo`` skips ``nxexp://`` values, so every rename and re-home leaves its
  copies' target segment behind and that count grows with each one; a re-import into the new collection then
  misses the copy and mints another. Repairing it needs its own rule and is a follow-on, not part of this verb.

``--apply`` keeps a LEDGER: one JSON line per row changed, in all three tables (``catalog_documents`` keyed by
tumbler, ``document_aspects`` and ``document_highlights`` keyed by row id), with the old and the new URI. It is the
only undo record: nothing else keeps the old value. It is written and flushed after every batch, before the next
one starts, to ``--ledger PATH`` (default a timestamped ``nx-note-uri-repair-<UTC>.jsonl`` in the working
directory; an existing file is appended to, and a new one is created owner-only). A response lost to a timeout
may leave one batch rewritten with no ledger lines: the engine keeps no other record of the old URIs, so that
batch cannot be undone from the ledger. A later census shows its rows as already in their own collection, so
re-running is safe.
"""

from __future__ import annotations

import datetime
import json
import os
from typing import Any

import click
import httpx

from nexus._winsec import open_private

#: The counts a report must carry, as integers, before anything is written on its strength.
_COUNT_KEYS: tuple[str, ...] = (
    "stale_live",
    "rewrite",
    "merge",
    "merge_twin",
    "merge_sibling",
    "refused",
    "refused_file_routed_collection",
    "refused_malformed",
    "tombstoned",
    "export_copies",
    "export_copies_target_moved",
)
_LEDGER_KEYS: tuple[str, ...] = ("table", "key", "old_uri", "new_uri")

# Extra batches beyond ceil(candidates / batch size): each link of a chain (stale A's target held by stale B)
# needs one more pass, so the cap allows chains up to this deep before it calls the rewrite unsettled.
_CHAIN_SLACK: int = 10
_SAMPLE_KEYS: tuple[str, ...] = (
    "rewrite",
    "merge",
    "refused",
    "tombstoned",
    "export_target_moved",
)


def _check_report(report: Any) -> dict:
    """The engine's report, or a ClickException naming what is wrong with it (nothing is written on a bad one)."""
    if not isinstance(report, dict):
        raise click.ClickException(
            f"unexpected report from the engine (not an object): {report!r}"
        )
    for key in _COUNT_KEYS:
        value = report.get(key)
        if isinstance(value, bool) or not isinstance(value, int):
            raise click.ClickException(
                f"unexpected report from the engine: {key!r} is {value!r}, not an integer. Nothing was written."
            )
    sample = report.get("sample")
    if not isinstance(sample, dict) or any(
        not isinstance(sample.get(k), list) for k in _SAMPLE_KEYS
    ):
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
        err = _cat_cmd._service_timeout_error("repair-note-uris", exc)
        if verb_args.get("apply"):
            err = click.ClickException(
                f"{err.format_message()}\nThe batch in flight may have been rewritten with no ledger lines: the "
                "engine keeps no other record of its old URIs, so it cannot be undone from the ledger. Run the "
                "census again before re-running --apply."
            )
        raise err from exc


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
        echo(
            f"  rewrite  {row.get('tumbler')}  {row.get('source_uri')}  ->  {row.get('new_uri')}"
        )
    for row in sample["merge"]:
        echo(
            f"  merge?   {row.get('tumbler')}  {row.get('source_uri')}  ->  {row.get('new_uri')}"
            f"  (twin {row.get('twin')}, {row.get('reason')}; chunks {row.get('chunk_count', '?')} vs "
            f"{row.get('twin_chunk_count', '?')})"
        )
    for row in sample["refused"]:
        echo(
            f"  refused  {row.get('tumbler')}  {row.get('source_uri')}  [{row.get('reason')}]"
        )
    shown = sum(len(sample[k]) for k in _SAMPLE_KEYS)
    listed = (
        report["rewrite"]
        + report["merge"]
        + report["refused"]
        + report["tombstoned"]
        + report["export_copies_target_moved"]
    )
    if shown < listed:
        echo(f"  ... {listed - shown} more (raise --sample, or read --json)")
    if report["merge"]:
        echo(
            "Merge candidates are listed and not merged: which row wins (the stale-URI row or its twin) is a "
            "design decision, and POST /v1/catalog/merge moves neither chunks, aspects nor highlights."
        )


def _check_ledger(step: dict) -> list[dict]:
    """The step's ledger, or a ClickException: rows were rewritten, so a response without a usable ledger is stopped on."""
    ledger = step.get("ledger")
    if not isinstance(ledger, list) or any(
        not isinstance(e, dict)
        or any(not isinstance(e.get(k), str) for k in _LEDGER_KEYS)
        for e in ledger
    ):
        raise click.ClickException(
            "unexpected report from the engine: apply returned rewritten rows without a usable ledger "
            "(a list of {table, key, old_uri, new_uri})."
        )
    catalog_rows = sum(1 for e in ledger if e["table"] == "catalog_documents")
    if catalog_rows != step["rewritten"]:
        raise click.ClickException(
            f"unexpected report from the engine: apply rewrote {step['rewritten']} catalog row(s) but the ledger "
            f"names {catalog_rows}."
        )
    return ledger


def _default_ledger_path() -> str:
    stamp = datetime.datetime.now(datetime.timezone.utc).strftime("%Y%m%dT%H%M%SZ")
    return f"nx-note-uri-repair-{stamp}.jsonl"


@click.command(name="repair-note-uris")
@click.option(
    "--apply", is_flag=True, help="Perform the rewrite (default: dry-run census only)."
)
@click.option(
    "--json",
    "as_json",
    is_flag=True,
    help="Emit JSON instead of the human-readable report.",
)
@click.option(
    "--sample",
    type=click.IntRange(0, 500),
    default=20,
    show_default=True,
    help="Example rows listed per class. The counts are never capped.",
)
@click.option(
    "--batch-size",
    "batch_size",
    type=click.IntRange(1, 5000),
    default=1000,
    show_default=True,
    help="Rewrite candidates ONE engine transaction rewrites; --apply repeats until none are left.",
)
@click.option(
    "--ledger",
    "ledger_path",
    type=click.Path(dir_okay=False, writable=True),
    default=None,
    help="With --apply: where the undo ledger (one JSON line per row changed) goes. Default: "
    "nx-note-uri-repair-<UTC timestamp>.jsonl in the working directory. An existing file is appended to.",
)
def repair_note_uris_cmd(
    apply: bool, as_json: bool, sample: int, batch_size: int, ledger_path: str | None
) -> None:
    """Repair note URIs that name a collection the note no longer lives in (nexus-bivz3.24).

    \b
    Always prints the census first; writes only with --apply.
      rewrite candidates  no live note holds nx-note://<its collection>/<title>: the URI is rewritten, in the
                          catalog row, its aspect row and its highlight row, in one transaction
      merge candidates    a live twin holds the target (or two stale rows share it): listed, NOT merged
      refused             blank or file-routed collection, or no title part: listed, never rewritten
      tombstoned          counted, never rewritten (a restored one shows up in the next census)
      nxexp:// copies     census only: their target segment goes stale with every rename or re-home, and a
                          re-import into the new collection then mints another copy; not repaired here
    A twin that appears while --apply runs is a 409 that names it: nothing is written for that batch, and the
    row is a merge candidate on the next census. A chain (a stale row's target held by another stale row) takes
    more than one pass; --apply repeats until none are left or a batch makes no progress.

    \b
    --apply writes a LEDGER (--ledger PATH): every row changed, in all three tables, with its old and new URI.
    It is the only undo record. Run the dry run first, and a census against a cloud tenant only with the
    operator's go.

    \b
    Examples:
      nx catalog repair-note-uris                # census
      nx catalog repair-note-uris --json         # census as JSON
      nx catalog repair-note-uris --apply        # rewrite every rewrite candidate, ledger in the working directory
    """
    from nexus.commands import catalog as _cat_cmd  # noqa: PLC0415 — module-routed helper access keeps import acyclic + monkeypatch-visible

    if ledger_path is not None and not apply:
        raise click.UsageError("--ledger only applies with --apply.")
    writer = _cat_cmd._get_catalog_writer()
    ledger_fh = None
    try:
        before = _call(writer, {"apply": False, "limit": batch_size, "sample": sample})
        if not apply:
            if as_json:
                click.echo(json.dumps(before, indent=2))
            else:
                _print_census(before)
                click.echo(
                    "Dry run: nothing written. Re-run with --apply to rewrite the rewrite candidates."
                )
            return

        if not as_json:
            _print_census(before)
        rewritten = 0
        batches = 0
        stalled = False
        capped = False
        after = before
        path: str | None = None
        if before["rewrite"] == 0:
            if not as_json:
                click.echo("Nothing to rewrite.")
        else:
            path = ledger_path or _default_ledger_path()
            try:
                # Owner-only, on Windows too: the ledger names every rewritten note's title.
                fd = open_private(path, os.O_WRONLY | os.O_CREAT | os.O_APPEND)
                ledger_fh = os.fdopen(fd, "a", encoding="utf-8", newline="")  # noqa: SIM115 — closed in the finally below
            except OSError as exc:
                raise click.ClickException(
                    f"cannot open the ledger {path!r} ({exc}). Nothing was written."
                ) from exc
            if not as_json:
                click.echo(f"Ledger: {path}")
            # A chain (stale A's target held by stale B) needs a pass after B has moved, so the loop re-censuses
            # and goes round again while rewrite candidates remain; the cap keeps a rewrite that never settles
            # from looping.
            cap = -(-before["rewrite"] // batch_size) + _CHAIN_SLACK
            while True:
                while True:
                    if batches >= cap:
                        capped = True
                        break
                    try:
                        step = _call(
                            writer, {"apply": True, "limit": batch_size, "sample": 0}
                        )
                    except httpx.HTTPStatusError as exc:
                        if exc.response is not None and exc.response.status_code == 409:
                            raise click.ClickException(
                                f"{_refusal_text(exc)}\n{rewritten} note URI(s) were rewritten before it (ledger: "
                                f"{path}); that batch wrote nothing. Run the census again: the row is a merge "
                                "candidate now."
                            ) from exc
                        raise
                    done = step.get("rewritten")
                    if (
                        isinstance(done, bool)
                        or not isinstance(done, int)
                        or not isinstance(step.get("remaining"), int)
                    ):
                        raise click.ClickException(
                            f"unexpected report from the engine: apply returned rewritten={done!r}, "
                            f"remaining={step.get('remaining')!r}."
                        )
                    batches += 1
                    rewritten += done
                    if done:
                        try:
                            entries = _check_ledger(step)
                        except click.ClickException as exc:
                            # That batch DID commit: keep whatever the engine sent, so nothing it said is lost.
                            rejected = f"{path}.rejected"
                            with open(rejected, "a", encoding="utf-8") as bad:
                                bad.write(
                                    json.dumps(
                                        {"batch": batches, "step": step}, default=str
                                    )
                                    + "\n"
                                )
                            raise click.ClickException(
                                f"{exc.format_message()} That batch was rewritten ({done} catalog row(s)); the raw "
                                f"engine report is saved to {rejected}. Stopping."
                            ) from exc
                        for entry in entries:
                            ledger_fh.write(
                                json.dumps({"batch": batches, **entry}) + "\n"
                            )
                        ledger_fh.flush()
                        os.fsync(ledger_fh.fileno())
                    if step["remaining"] == 0:
                        break
                    if done == 0:
                        stalled = True
                        break
                after = _call(
                    writer, {"apply": False, "limit": batch_size, "sample": sample}
                )
                if stalled or capped or after["rewrite"] == 0:
                    break
    finally:
        if ledger_fh is not None:
            ledger_fh.close()
        writer.close()

    if as_json:
        click.echo(
            json.dumps(
                {
                    "before": before,
                    "rewritten": rewritten,
                    "batches": batches,
                    "ledger": path,
                    "after": after,
                },
                indent=2,
            )
        )
    else:
        click.echo(
            f"Rewrote {rewritten} note URI(s) in {batches} batch(es)."
            + (f" Ledger: {path}" if path and rewritten else "")
        )
        if batches:
            click.echo("Census after:")
            _print_census(after)
    if stalled:
        click.echo(
            "The last batch made no progress while rewrite candidates remain: stopping. Run the census again.",
            err=True,
        )
        raise click.exceptions.Exit(1)
    if capped:
        click.echo(
            f"Stopped after {batches} batches (the cap) with rewrite candidates still listed: something keeps "
            "producing them. Run the census again and read the samples.",
            err=True,
        )
        raise click.exceptions.Exit(1)


def register(group: click.Group) -> None:
    """Attach ``repair-note-uris`` to the shared ``catalog`` group."""
    group.add_command(repair_note_uris_cmd)
