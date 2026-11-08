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

* **No merge without ``--merge``.** A row whose target URI a live document already holds (a twin), or that
  would rewrite onto the same URI as another stale row, is listed and left alone by the plain verb.
  ``--merge`` is the merge pass (Sam's ruling "Chunks decide", RDR-231 Decision 6): the survivor is the row that
  holds the chunks, because ``POST /v1/catalog/merge`` moves neither chunks nor aspects nor highlights. The rule
  is :mod:`nexus.catalog.note_uri_merge`; the loop and its ledger are below.
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
from nexus.catalog.note_uri_merge import Decision, Plan, plan as _plan_merges

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
            "Merge candidates are listed and not merged here: --merge merges them (the row that holds the chunks "
            "survives; POST /v1/catalog/merge moves neither chunks, aspects nor highlights)."
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


# --------------------------------------------------------------------------------------------------------------
# --merge: the merge pass (Sam's ruling "Chunks decide", ask bivz324-merge-winner)
# --------------------------------------------------------------------------------------------------------------

#: The most merge entries the engine will list in one report (its ``sample`` cap): one round's worth.
_MERGE_REPORT_ENTRIES: int = 500
#: A hard bound on rounds. Each round must try at least one group never tried before, so this only matters for a
#: tenant with more than this many groups; the census after says what is left.
_MAX_MERGE_ROUNDS: int = 200
_WINDOW_NOTE = (
    "When a stale row wins, the twin is merged into it and the stale row is rewritten in a second request. "
    "The window between the two is real: the row still holds its old URI and nothing holds the new one, so a "
    "re-put under the new collection inside it mints a third row. It is one request long."
)


class _RunStopped(Exception):
    """An engine error that is not a refusal: the pass stops, with the ledger written."""


def _decision_dict(d: Decision) -> dict:
    return {
        "winner": d.winner.tumbler,
        "losers": [c.tumbler for c in d.losers],
        "rule": d.rule,
        "target_uri": d.target_uri,
        "rewrite_needed": d.rewrite_needed,
        "members": [
            {
                "tumbler": c.tumbler,
                "chunk_count": c.chunk_count,
                "indexed_at": c.indexed_at,
                "twin": c.is_twin,
                "source_uri": c.uri,
            }
            for c in d.members
        ],
    }


def _describe(c: Any) -> str:
    return f"{c.tumbler} ({'twin' if c.is_twin else 'stale'}, {c.chunk_count} chunk(s))"


def _print_plan(p: Plan, report: dict) -> None:
    echo = click.echo
    for d in p.decisions:
        echo(f"  {d.target_uri or 'group'}")
        echo(
            f"    {d.winner.tumbler} wins: {d.rule}  [{', '.join(_describe(c) for c in d.members)}]"
        )
        for i, loser in enumerate(d.losers):
            tail = (
                f", then rewrite {d.winner.tumbler}"
                if d.rewrite_needed and i == len(d.losers) - 1
                else ""
            )
            echo(f"    merge {loser.tumbler} into {d.winner.tumbler}{tail}")
    for sk in p.skipped:
        echo(f"  skipped {sk.get('tumbler')}: {sk.get('why')}")
    listed = len(report["sample"]["merge"])
    if report["merge"] > listed:
        echo(
            f"  {report['merge'] - listed} more merge candidate(s) are not in this report: --apply works in rounds, "
            "and the pairs it merges make room for the rest."
        )


def _execute_decision(writer: Any, d: Decision, *, round_no: int, record: Any) -> dict:
    """Merge every loser of ``d`` into its winner, one request each, then rewrite a stale winner. A 409 refusal
    (self, not found, already aliased, cycle) is reported for that pair and the group goes on; any other engine
    error is recorded and raises :class:`_RunStopped`. ``record`` writes one ledger line, durably, before the
    next request is made. There is no ``--force``: the rule never folds a chunk holder into a chunkless winner,
    and a pair whose data says otherwise (a stale census) is refused here without a call."""
    pairs: list[dict] = []
    all_merged = True
    for loser in d.losers:
        line = {
            "type": "merge",
            "round": round_no,
            "winner": d.winner.tumbler,
            "loser": loser.tumbler,
            "loser_uri_before": loser.uri,
            "loser_uri_after": loser.uri,
            "rule": d.rule,
            "rewrite_needed": d.rewrite_needed,
        }
        if loser.chunk_count > 0 and d.winner.chunk_count == 0:
            line.update(
                outcome="refused",
                detail=f"chunk guard: {loser.tumbler} holds {loser.chunk_count} chunk(s) and the winner "
                f"{d.winner.tumbler} holds none; a merge moves no chunks, so none is made",
            )
        else:
            try:
                result = writer.merge_documents(loser.tumbler, d.winner.tumbler)
                line.update(
                    outcome="merged",
                    loser_uri_after="",
                    detail="",
                    links_remapped=int((result or {}).get("links_remapped", 0) or 0),
                    links_collapsed=int((result or {}).get("links_collapsed", 0) or 0),
                    links_dropped=int((result or {}).get("links_dropped", 0) or 0),
                )
            except httpx.HTTPStatusError as exc:
                if exc.response is not None and exc.response.status_code == 409:
                    line.update(outcome="refused", detail=_refusal_text(exc))
                else:
                    line.update(
                        outcome="error",
                        loser_uri_after=None,
                        detail=f"engine error: {exc}",
                    )
                    record(line)
                    raise _RunStopped(
                        f"merge {loser.tumbler} into {d.winner.tumbler} failed: {exc}"
                    ) from exc
            except httpx.TimeoutException as exc:
                line.update(
                    outcome="error",
                    loser_uri_after=None,
                    detail=f"timeout: the merge may have happened ({type(exc).__name__})",
                )
                record(line)
                raise _RunStopped(
                    f"merge {loser.tumbler} into {d.winner.tumbler} timed out; it may have happened. "
                    "Run the census again."
                ) from exc
        record(line)
        pairs.append(line)
        all_merged = all_merged and line["outcome"] == "merged"

    rewrite: dict | None = None
    if d.rewrite_needed:
        rewrite = {
            "type": "rewrite-result",
            "round": round_no,
            "winner": d.winner.tumbler,
            "followed": False,
            "detail": "",
        }
        if not all_merged:
            rewrite["detail"] = (
                "not rewritten: a merge into it was refused, so it still has a sibling"
            )
        else:
            try:
                step = _check_report(
                    writer.repair_stale_note_uris(
                        apply=True, limit=1, sample=0, tumblers=[d.winner.tumbler]
                    )
                )
            except httpx.HTTPStatusError as exc:
                if exc.response is not None and exc.response.status_code == 409:
                    rewrite["detail"] = f"rewrite refused: {_refusal_text(exc)}"
                else:
                    rewrite["detail"] = f"engine error: {exc}"
                    record(rewrite)
                    raise _RunStopped(
                        f"rewrite of {d.winner.tumbler} failed: {exc}"
                    ) from exc
            except httpx.TimeoutException as exc:
                rewrite["detail"] = (
                    f"timeout: the rewrite may have happened ({type(exc).__name__})"
                )
                record(rewrite)
                raise _RunStopped(
                    f"rewrite of {d.winner.tumbler} timed out; it may have happened. Run the census again."
                ) from exc
            else:
                if step.get("rewritten") == 1:
                    for entry in _check_ledger(step):
                        record(
                            {
                                "type": "rewrite",
                                "round": round_no,
                                "winner": d.winner.tumbler,
                                **entry,
                            }
                        )
                    rewrite["followed"] = True
                else:
                    rewrite["detail"] = (
                        "not a rewrite candidate now (a sibling or twin still holds its target, or it changed): "
                        "left for the next census"
                    )
        record(rewrite)
    return {"pairs": pairs, "rewrite": rewrite}


def _run_merge(
    writer: Any, *, apply: bool, as_json: bool, ledger_path: str | None
) -> int:
    """The ``--merge`` verb body. Returns the exit status."""
    before = _call(
        writer, {"apply": False, "limit": 1, "sample": _MERGE_REPORT_ENTRIES}
    )
    first = _plan_merges(before["sample"]["merge"])
    if not apply:
        if as_json:
            click.echo(
                json.dumps(
                    {
                        "census": before,
                        "plan": [_decision_dict(d) for d in first.decisions],
                        "skipped": first.skipped,
                    },
                    indent=2,
                )
            )
        else:
            _print_census(before)
            click.echo("Merge plan (the rule: chunks decide; ties go to the twin):")
            _print_plan(first, before)
            if not first.decisions:
                click.echo("Nothing to merge.")
            click.echo(
                "Dry run: nothing written. Re-run with --merge --apply to perform it."
            )
        return 0

    if not as_json:
        _print_census(before)
    if not first.decisions:
        if as_json:
            click.echo(
                json.dumps(
                    {
                        "before": before,
                        "after": before,
                        "pairs": [],
                        "merged": 0,
                        "refused": 0,
                        "errors": 0,
                        "rewrites_followed": 0,
                        "rounds": 0,
                        "ledger": None,
                        "skipped": first.skipped,
                    },
                    indent=2,
                )
            )
        else:
            _print_plan(first, before)
            click.echo("Nothing to merge.")
        return 0

    path = ledger_path or _default_ledger_path()
    try:
        # Owner-only, on Windows too: the ledger names every merged note's titles.
        fd = open_private(path, os.O_WRONLY | os.O_CREAT | os.O_APPEND)
        fh = os.fdopen(fd, "a", encoding="utf-8", newline="")  # noqa: SIM115 — closed in the finally below
    except OSError as exc:
        raise click.ClickException(
            f"cannot open the ledger {path!r} ({exc}). Nothing was written."
        ) from exc

    def record(line: dict) -> None:
        fh.write(json.dumps(line) + "\n")
        fh.flush()
        os.fsync(fh.fileno())

    if not as_json:
        click.echo(f"Ledger: {path}")
    pairs: list[dict] = []
    rewrites: list[dict] = []
    attempted: set[frozenset[str]] = set()
    report, current_plan, rounds, stopped, skipped = (
        before,
        first,
        0,
        None,
        list(first.skipped),
    )
    try:
        while rounds < _MAX_MERGE_ROUNDS:
            touched: set[str] = set()
            todo: list[Decision] = []
            for d in current_plan.decisions:
                key = frozenset(c.tumbler for c in d.members)
                if key in attempted or touched & key:
                    continue
                todo.append(d)
                touched |= key
            if not todo:
                break
            rounds += 1
            for d in todo:
                attempted.add(frozenset(c.tumbler for c in d.members))
                outcome = _execute_decision(writer, d, round_no=rounds, record=record)
                for p in outcome["pairs"]:
                    pairs.append(p)
                    if not as_json:
                        if p["outcome"] == "merged":
                            click.echo(
                                f"  merged   {p['loser']} into {p['winner']}  [{p['rule']}]"
                            )
                        elif p["outcome"] == "refused":
                            click.echo(
                                f"  REFUSED  {p['loser']} into {p['winner']}: {p['detail']}"
                            )
                if outcome["rewrite"] is not None:
                    rewrites.append(outcome["rewrite"])
                    if not as_json:
                        r = outcome["rewrite"]
                        click.echo(
                            f"  rewrote  {r['winner']}"
                            if r["followed"]
                            else f"  not rewritten  {r['winner']}: {r['detail']}"
                        )
            report = _call(
                writer, {"apply": False, "limit": 1, "sample": _MERGE_REPORT_ENTRIES}
            )
            current_plan = _plan_merges(report["sample"]["merge"])
            skipped = current_plan.skipped
    except _RunStopped as exc:
        stopped = str(exc)
    finally:
        fh.close()

    merged = sum(1 for p in pairs if p["outcome"] == "merged")
    refused = sum(1 for p in pairs if p["outcome"] == "refused")
    errors = sum(1 for p in pairs if p["outcome"] == "error") + (
        1 if stopped and not any(p["outcome"] == "error" for p in pairs) else 0
    )
    followed = sum(1 for r in rewrites if r["followed"])
    if stopped is None:
        after = report
    else:
        try:
            after = _call(
                writer, {"apply": False, "limit": 1, "sample": _MERGE_REPORT_ENTRIES}
            )
        except click.ClickException:
            after = report
    if as_json:
        click.echo(
            json.dumps(
                {
                    "before": before,
                    "after": after,
                    "pairs": pairs,
                    "rewrites": rewrites,
                    "merged": merged,
                    "refused": refused,
                    "errors": errors,
                    "rewrites_followed": followed,
                    "rounds": rounds,
                    "ledger": path,
                    "skipped": skipped,
                    "stopped": stopped,
                },
                indent=2,
            )
        )
    else:
        click.echo(
            f"{merged} merged, {refused} refused, {errors} error(s); {followed} rewrite(s) followed. Ledger: {path}"
        )
        if any(r["followed"] or r["detail"] for r in rewrites):
            click.echo(_WINDOW_NOTE)
        if refused:
            click.echo(
                "Refused pairs stay in the census; read the reasons above (the ledger has them too)."
            )
        click.echo("Census after:")
        _print_census(after)
    if stopped:
        click.echo(f"Stopped: {stopped}", err=True)
        return 1
    return 0


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
    "--merge",
    "merge_pass",
    is_flag=True,
    help="The merge pass for the merge candidates (a live twin, or siblings): chunks decide who survives. "
    "Dry run by default (each group's winner and why); with --apply it merges the losers into the winner "
    "and rewrites a stale winner, writing the ledger. Plain rewrite candidates are left to --apply alone.",
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
    apply: bool,
    as_json: bool,
    sample: int,
    batch_size: int,
    merge_pass: bool,
    ledger_path: str | None,
) -> None:
    """Repair note URIs that name a collection the note no longer lives in (nexus-bivz3.24).

    \b
    Always prints the census first; writes only with --apply.
      rewrite candidates  no live note holds nx-note://<its collection>/<title>: the URI is rewritten, in the
                          catalog row, its aspect row and its highlight row, in one transaction
      merge candidates    a live twin holds the target (or two stale rows share it): listed; merged only by --merge
      refused             blank or file-routed collection, or no title part: listed, never rewritten
      tombstoned          counted, never rewritten (a restored one shows up in the next census)
      nxexp:// copies     census only: their target segment goes stale with every rename or re-home, and a
                          re-import into the new collection then mints another copy; not repaired here
    A twin that appears while --apply runs is a 409 that names it: nothing is written for that batch, and the
    row is a merge candidate on the next census. A chain (a stale row's target held by another stale row) takes
    more than one pass; --apply repeats until none are left or a batch makes no progress.

    \b
    --merge is the merge pass, chunks decide: the twin wins unless the stale row holds chunks and the twin holds
    none (or both hold chunks and the stale row is newer); a stale winner is rewritten after the merge, with a
    one-request window between the two. Dry run by default (each group's winner and why); --merge --apply
    performs it, a refused pair is reported and the run goes on, and the ledger records every pair.

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
    if merge_pass:
        try:
            status = _run_merge(
                writer, apply=apply, as_json=as_json, ledger_path=ledger_path
            )
        finally:
            writer.close()
        if status:
            raise click.exceptions.Exit(status)
        return
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
