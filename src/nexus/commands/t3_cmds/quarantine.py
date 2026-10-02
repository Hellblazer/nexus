# SPDX-License-Identifier: AGPL-3.0-or-later
# Copyright (c) 2026 Hal Hildebrand. All rights reserved.
"""``nx t3 quarantine`` — operate on the engine's ``quarantine-*`` collections.

``nx t3 quarantine restore`` (RDR-192 Step 9 Day-2, beads nexus-2x9xa and nexus-wbfpw.49, Sam's
rulings 2026-10-01) moves chunks from a collection's ``quarantine-`` sibling back to the
collection, through the engine route ``POST /v1/vectors/gc/quarantine-restore``
(``nexus.quarantine_restore_chunks``, changeset ``vectors-025``).

The engine finds the quarantine collection itself (nexus-wbfpw.55): the verb names none. The reaper
names its sibling from the collection's NAME while the client's own move names one from the catalog ROW,
and catalog-044 rewrote the row's owner on repo collections, so a name derived here reaches nothing the
reaper moved. The engine looks at the reaper's name and at every quarantine collection holding chunks
tagged with this origin.

Why a verb of its own: until now the only way out of quarantine was
``gc_restore_rereferenced``, which restores a chunk only when the origin's manifest
names it again. A chunk the reaper moved WRONGLY has no manifest row by definition, so
the only recovery was hand SQL from the ``gc_audit`` chash list.

Why ``--reattach`` (the default): since RDR-192 Phase 2 a chunk with no live owning manifest
row is hidden from search and get, so a restore that moved bytes only would hand back text
nothing shows. When a restored chunk's metadata names a document that is still live, the
engine writes that document's manifest row too and the chunk is visible again. A chunk it
cannot attach is still restored, and the output says plainly that it stays hidden and what to
do about it.

The chunks are named in one of three ways, because the places that record what was
quarantined differ in completeness: explicit ``--chash`` values, a ``--audit-id`` (the
chash list of a ``reaper_quarantine`` row; a ``gc_quarantine_orphans`` row lists only a
sample and the engine refuses it), or a ``--quarantined-since`` / ``--quarantined-before``
window over the sibling itself, which covers every quarantine however it was made.
"""
from __future__ import annotations

import json
import re
import shlex
import sys
from datetime import UTC, datetime
from typing import Any, NoReturn

import click

from nexus.db.engine_reasons import QUARANTINE_RESTORE_BUSY_REASON

#: Most chashes one engine call takes (``VectorHandler.MAX_QUARANTINE_RESTORE_PER_CALL``).
_BATCH = 1000

#: Exit codes. 1 is a restore that left a requested chunk unrestored for a reason the operator
#: should look at (missing, or an embedding-width conflict); a dry run exits 1 for the same
#: reasons, since a real run would leave them too. 3 is a restore that brought every requested
#: chunk back but left at least one HIDDEN from search and get (no live owner row names it, or
#: reattach refused or was switched off): the bytes are in the collection and nothing shows them,
#: which is the round-1 defect a script must be able to see. 1 wins when both hold. 4 and 5
#: mirror ``nx t3 census-manifest-less``: the engine predates the route, or answered with an
#: error. 6 is the engine's typed retryable ``quarantine_restore_busy``: a lock or statement bound
#: tripped and the same command may be run again. Each quarantine sibling restores in its own
#: transaction, so "nothing moved" holds only when the trip was on the first one: the engine says
#: so (``nothing_moved``) and names the audit rows an earlier sibling had already written.
EXIT_UNRESTORED = 1
EXIT_HIDDEN = 3
EXIT_NO_ROUTE = 4
EXIT_ENGINE_ERROR = 5
EXIT_BUSY = 6

#: Rows printed in the table before the rest are summarised (``--json`` carries every row).
_TABLE_ROWS = 100

#: Re-put recipes printed under the table before the rest are summarised.
_RECIPE_ROWS = 20

_CHASH_RE = re.compile(r"^[0-9a-f]{64}$")

_OUTCOMES = ("restored", "would_restore", "present", "dim_conflict", "missing")


def _make_t3():
    """The T3 client (``HttpVectorClient`` in every mode). Patched in tests."""
    from nexus.db import make_t3  # noqa: PLC0415 — command-local import deferred to avoid CLI startup cost (nexus.db)

    return make_t3()


def _content_type(origin: str) -> str:
    """The origin's content type (``knowledge``, ``docs``, ``code``, ``rdr``) from its catalog row, never from
    its name (RDR-204). ``""`` when the row cannot be read, which the guidance treats as unknown. Patched in tests."""
    from nexus.corpus import CollectionNotRegisteredError, collection_content_type  # noqa: PLC0415 — command-local import deferred to avoid CLI startup cost (nexus.corpus)

    try:
        return collection_content_type(origin)
    except CollectionNotRegisteredError:
        return ""


def _instant(option: str, raw: str | None) -> str | None:
    """*raw* as the ``YYYY-MM-DDTHH:MM:SSZ`` the engine parses. A date or a datetime; a naive
    value is UTC."""
    if raw is None:
        return None
    try:
        parsed = datetime.fromisoformat(raw)
    except ValueError as exc:
        raise click.BadParameter(
            f"{raw!r} is not an ISO-8601 date or datetime (for example 2026-09-01 or 2026-09-01T12:00:00Z)",
            param_hint=option,
        ) from exc
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=UTC)
    return parsed.astimezone(UTC).strftime("%Y-%m-%dT%H:%M:%SZ")


def _parse_instant(value: str) -> datetime:
    """An engine timestamp (``2026-10-31T12:00:00Z`` or with fractional seconds) as an aware datetime,
    so two of them compare by time and not by how many fractional digits the string happens to carry."""
    return datetime.fromisoformat(value.replace("Z", "+00:00"))


def _actor() -> str:
    try:
        import getpass  # noqa: PLC0415 — only this verb needs it

        return f"nx-cli quarantine-restore ({getpass.getuser()})"
    except Exception:  # noqa: BLE001 — a missing login name must not stop a restore
        return "nx-cli quarantine-restore"


def _fail(message: str, code: int) -> NoReturn:
    click.echo(message, err=True)
    sys.exit(code)


def _pages(client: Any, origin: str, *, chashes: list[str], audit_id: int | None,
           since: str | None, before: str | None, dry_run: bool, reattach: bool, actor: str):
    """Yield the engine's answer page by page for whichever source was named.

    No quarantine collection is named: the engine finds it (nexus-wbfpw.55). One derived here from the
    origin's catalog row is not where the reaper put the chunks once catalog-044 rewrote the row's owner.
    """
    common = {"dry_run": dry_run, "reattach": reattach, "actor": actor}
    if chashes:
        for start in range(0, len(chashes), _BATCH):
            yield client.gc_quarantine_restore(origin, chashes=chashes[start:start + _BATCH], **common)
    elif audit_id is not None:
        offset = 0
        while True:
            page = client.gc_quarantine_restore(
                origin, audit_id=audit_id, offset=offset, limit=_BATCH, **common)
            yield page
            nxt = (page.get("source") or {}).get("next_offset")
            if nxt is None:
                return
            offset = int(nxt)
    else:
        after: str | None = None
        while True:
            page = client.gc_quarantine_restore(
                origin, quarantined_since=since, quarantined_before=before,
                after_chash=after, limit=_BATCH, **common)
            yield page
            after = page.get("next_after")
            if not after:
                return


def _tally(rows: list[dict]) -> dict[str, int]:
    """What the reattach step did across *rows*, counted from the rows themselves."""
    return {
        "attached": sum(1 for r in rows if r.get("attached")),
        "would_attach": sum(1 for r in rows if r.get("reattach") == "attach" and not r.get("attached")),
        "superseded": sum(1 for r in rows if r.get("reattach") == "superseded"),
        "no_live_owner": sum(1 for r in rows if r.get("reattach") == "no_live_owner"),
        "no_position": sum(1 for r in rows if r.get("reattach") == "no_position"),
    }


def _hidden(rows: list[dict], reattach: bool) -> list[dict]:
    """Rows of chunks that are in (or, on a dry run, would be in) the collection but that no live owner row
    names after this call, so search and get still do not return them. A chunk that already had a manifest
    row (``owned``) or that this call attached (or, with reattach on, would attach) is visible; a missing or
    width-conflicting one is not here at all."""
    out = []
    for r in rows:
        if r.get("outcome") not in ("restored", "would_restore", "present") or r.get("attached"):
            continue
        verdict = r.get("reattach")
        if verdict == "owned" or (verdict == "attach" and reattach):
            continue
        out.append(r)
    return out


#: Why a superseded chunk was refused, grouped by what the operator should do about it.
_AMBIGUOUS = frozenset({"rival", "race"})


def _hidden_kind(row: dict, content_type: str) -> str:
    """The guidance class of a hidden chunk: one of ``not_attached`` (reattach was off), ``moved_on``,
    ``indexing``, ``failed``, ``other_collection``, ``ambiguous``, ``no_key_note`` (a ``knowledge`` chunk no document
    names: a re-put can bring it back), ``no_key_file`` (a chunk of an indexed file, which carries no key
    at all: the file is what to re-index) or ``no_key_unknown`` (the collection's content type could not be
    read, so the guidance names both remedies)."""
    verdict = row.get("reattach")
    why = row.get("reason")
    if verdict == "attach":
        return "not_attached"
    if verdict == "superseded":
        if why == "indexing":
            return "indexing"
        if why == "failed":
            return "failed"
        if why == "other_collection":
            return "other_collection"
        if why in _AMBIGUOUS:
            return "ambiguous"
        return "moved_on"
    if content_type == "knowledge":
        return "no_key_note"
    return "no_key_file" if content_type else "no_key_unknown"


def _row_note(row: dict, dry_run: bool, content_type: str = "") -> str:
    """The NOTE column: what happened to the chunk's visibility, in words."""
    outcome = row["outcome"]
    verdict = row.get("reattach")
    owner = row.get("owner")
    title = row.get("owner_title")
    pos = row.get("position")
    who = f"{title!r} ({owner})" if title else (owner or "its owner")
    parts: list[str] = []
    if outcome == "present":
        parts.append("already in the collection; left alone")
    elif outcome == "dim_conflict":
        return "collection row has another embedding width; left alone"
    elif outcome == "missing":
        return "not in the quarantine collection"
    if row.get("attached"):
        parts.append(f"attached to {who} at position {pos}")
    elif verdict == "attach":
        parts.append(f"would attach to {who} at position {pos}" if dry_run
                     else f"NOT attached (--no-reattach); a run without it attaches to {who} at position {pos}")
    elif verdict == "owned":
        parts.append("already has a manifest row")
    elif verdict == "superseded":
        kind = _hidden_kind(row, content_type)
        if kind == "indexing":
            parts.append(f"{who} is mid index run: bytes only, hidden; run again when it finishes")
        elif kind == "failed":
            parts.append(f"{who}'s last index run failed, so its manifest is partial: bytes only, hidden; "
                         "re-index it")
        elif kind == "other_collection":
            parts.append(f"{who}'s manifest rows sit under another collection: bytes only, hidden")
        elif kind == "ambiguous":
            parts.append(f"another chunk claims position {pos} of {who}: version ambiguous, nothing attached; "
                         "bytes only, hidden")
        else:
            parts.append(f"{who}'s current text is live: bytes only, hidden")
    elif verdict == "no_position":
        parts.append(f"{who} is multi-chunk and this chunk records no position: bytes only, hidden from search and get")
    elif verdict == "no_live_owner":
        parts.append("no live owner names it: bytes only, hidden from search and get")
    if row.get("outcome") == "restored" and row.get("no_manifest") and row.get("reapable_after"):
        parts.append(f"reapable again {row['reapable_after']}")
    return "; ".join(parts)


def _recipes(collection: str, hidden: list[dict]) -> list[str]:
    """One re-put command per distinct owner document (or, with none named, per chunk title). Only for a
    ``knowledge`` collection, where a note is a document of its own and ``nx store put`` under its title is
    the way to give the text an owner; a file collection's chunks are re-made by indexing the file."""
    seen: dict[tuple[str | None, str | None], int] = {}
    for r in hidden:
        key = (r.get("owner"), r.get("owner_title") or r.get("chunk_title"))
        seen[key] = seen.get(key, 0) + 1
    lines: list[str] = []
    for (owner, title), n in list(seen.items())[:_RECIPE_ROWS]:
        if title:
            cmd = f"nx store put - --collection {shlex.quote(collection)} --title {shlex.quote(title)}"
            tail = f"   # owner {owner}, {n} chunk{'s' if n != 1 else ''}" if owner else (
                f"   # no live owner named; the chunk's own title, {n} chunk{'s' if n != 1 else ''}")
            lines.append(f"  {cmd}{tail}")
        else:
            lines.append(f"  (owner {owner or 'none named'}, no title recorded: {n} chunk{'s' if n != 1 else ''})")
    if len(seen) > _RECIPE_ROWS:
        lines.append(f"  ... {len(seen) - _RECIPE_ROWS} more (use --json for every row).")
    return lines


def _owners(rows: list[dict]) -> list[dict]:
    """Per owner document, the chunks this call attached and how many of the document's registered chunks now
    have a manifest row (the engine's count after the page that attached them; the last page wins)."""
    seen: dict[str, dict] = {}
    for r in rows:
        owner = r.get("owner")
        if not r.get("attached") or not owner:
            continue
        entry = seen.setdefault(owner, {"owner": owner, "title": r.get("owner_title"), "attached": 0,
                                        "manifest_rows": None, "chunk_count": None})
        entry["attached"] += 1
        if r.get("owner_rows") is not None:
            entry["manifest_rows"] = r["owner_rows"]
        if r.get("owner_chunks") is not None:
            entry["chunk_count"] = r["owner_chunks"]
    return list(seen.values())


def _partial(owners: list[dict]) -> list[dict]:
    """The owners whose manifest still has fewer rows than the chunks they register."""
    return [o for o in owners
            if o["manifest_rows"] is not None and o["chunk_count"] and o["manifest_rows"] < o["chunk_count"]]


def _render_text(origin: str, siblings: list[str], dry_run: bool, reattach: bool, rows: list[dict],
                 totals: dict[str, int], audit_ids: list[int], source: dict | None, earliest: str | None,
                 content_type: str = "") -> None:
    # The engine says where it looked; when no quarantine collection holds anything of this origin it says none.
    where = ", ".join(siblings) if siblings else "no quarantine collection (none holds chunks of this collection)"
    if dry_run:
        click.echo(f"Dry run: nothing moved. Would restore from {where} into {origin}.")
    else:
        click.echo(f"Restore from {where} into {origin}.")
    if source:
        click.echo(f"Source: gc_audit {source.get('audit_id')} ({source.get('operation')}, "
                   f"{source.get('chash_count')} chashes).")
    if rows:
        click.echo("")
        click.echo(f"{'CHASH':<64}  {'OUTCOME':<13}  NOTE")
        for row in rows[:_TABLE_ROWS]:
            click.echo(f"{row['chash']}  {row['outcome']:<13}  {_row_note(row, dry_run, content_type)}".rstrip())
        if len(rows) > _TABLE_ROWS:
            click.echo(f"... {len(rows) - _TABLE_ROWS} more rows (use --json for every row).")
        click.echo("")
    lead = "would restore" if dry_run else "restored"
    n = totals["would_restore"] if dry_run else totals["restored"]
    click.echo(f"{lead} {n}, present {totals['present']}, dim_conflict {totals['dim_conflict']}, "
               f"missing {totals['missing']}")
    tally = _tally(rows)
    if not reattach:
        click.echo("reattach: off (--no-reattach). Chunks that name a live document are not attached; "
                   f"a run without the flag would attach {tally['would_attach']}.")
    else:
        verb = "would attach" if dry_run else "attached"
        click.echo(f"reattach: {verb} {tally['would_attach'] if dry_run else tally['attached']}, "
                   f"superseded {tally['superseded']}, no live owner {tally['no_live_owner']}, "
                   f"no position {tally['no_position']}")
    if audit_ids:
        click.echo("gc_audit " + ", ".join(str(a) for a in audit_ids)
                   + "  (nx catalog gc-audit list --operation quarantine_restore)")
    owners = _owners(rows)
    for o in _partial(owners):
        label = f"{o['title']!r} ({o['owner']})" if o.get("title") else o["owner"]
        click.echo(f"{label}: {o['manifest_rows']} of {o['chunk_count']} attached: restore the rest "
                   "(name them with --chash, or the same window or audit id) or re-index it.")
    hidden = _hidden(rows, reattach)
    if hidden:
        verb = "would stay" if dry_run else "stay"
        click.echo(
            f"\n{len(hidden)} chunk{'s' if len(hidden) != 1 else ''} {verb} HIDDEN from search and get: "
            "no live owner row names them, so the engine does not return them, though their text is back in the "
            "collection.")
        groups: dict[str, list[dict]] = {}
        for r in hidden:
            groups.setdefault(_hidden_kind(r, content_type), []).append(r)
        for kind, members in groups.items():
            n = len(members)
            what = f"{n} chunk{'s' if n != 1 else ''}"
            if kind == "moved_on":
                click.echo(f"\n{what}: the document's current text is live; nothing to do unless you need the old text.")
            elif kind == "indexing":
                click.echo(f"\n{what}: the document is in the middle of an index run. Run the same command again "
                           "when it has finished, if you still want the chunk back.")
            elif kind == "failed":
                click.echo(f"\n{what}: the document's last index run failed, so its manifest is partial and nothing "
                           "was attached to it. Re-index the document (`nx index repo --force`, or the matching "
                           "verb: `nx index pdf`, `nx index md`, `nx index rdr`), then run the same command again "
                           "if you still want the chunk back.")
            elif kind == "other_collection":
                click.echo(f"\n{what}: the document's manifest rows sit under another collection, where its current "
                           "text is live. Nothing to do unless you need the old text here.")
            elif kind == "ambiguous":
                click.echo(f"\n{what}: more than one stored chunk (here or in the quarantine collection) claims the "
                           "same position of the same document, so none was attached and the version is yours to "
                           "pick. Compare the text with `nx store get CHASH`, then re-index the document or re-put "
                           "the note.")
            elif kind == "not_attached":
                click.echo(f"\n{what}: not attached because of --no-reattach. Run the command again without it.")
            elif kind == "no_key_note":
                click.echo(f"\n{what}: no live document names them. To make one visible, re-put your own copy of its "
                           "note under the same title:")
                for line in _recipes(origin, members):
                    click.echo(line)
            elif kind == "no_key_unknown":
                click.echo(f"\n{what}: no live document names them. If this collection holds notes, re-put your own "
                           "copy of each under the same title (`nx store put - --collection C --title 'T'`); if it "
                           "holds indexed files, re-index the owning file (`nx index repo --force`).")
            else:
                click.echo(f"\n{what}: the chunks of an indexed file carry no key naming their document, so the "
                           "engine cannot tell which one they belong to. The owning file is probably still indexed "
                           "but its chunks carry no key; re-index it with `nx index repo --force` (or the matching "
                           "verb: `nx index pdf`, `nx index md`, `nx index rdr`).")
        if earliest:
            click.echo(
                f"\nUntil then the chunks are also eligible for the engine reaper again on or after {earliest} "
                "(30 days after the restore); an owner row by then keeps them.")


def _audit_ids_of(exc: Exception) -> list[int]:
    """The ``quarantine_restore`` audit rows the engine's busy 503 says an earlier sibling had already written."""
    body = getattr(exc, "engine_body", None)
    ids = body.get("audit_ids") if isinstance(body, dict) else None
    return [int(a) for a in ids if isinstance(a, int) and not isinstance(a, bool)] if isinstance(ids, list) else []


def _error_for(exc: Exception, pages_done: int) -> tuple[str, int]:
    """The operator's message and exit code for a failure of the engine call, never a traceback."""
    from nexus.db.http_vector_client import VectorServiceError  # noqa: PLC0415 — command-local import deferred to avoid CLI startup cost (nexus.db.http_vector_client)

    after = (f" Pages before it are committed and are in the report above ({pages_done} page"
             f"{'s' if pages_done != 1 else ''}); run the same command again to continue."
             if pages_done else "")
    if not isinstance(exc, VectorServiceError):
        # Not an engine answer (a bug, a broken pipe, a decode error): still no traceback over the report of the
        # pages that did commit, and the type is named so the failure is not a mystery.
        return (f"quarantine restore failed unexpectedly ({type(exc).__name__}: {exc}).{after}"), EXIT_ENGINE_ERROR
    if exc.code == 404:
        return ("This engine does not carry the quarantine-restore route (RDR-192 Step 9, bead "
                "nexus-2x9xa): the connected engine predates it. Upgrade the engine (compare its "
                "version against REQUIRED_ENGINE_VERSION in src/nexus/engine_version.py)."), EXIT_NO_ROUTE
    if exc.code == 503 and exc.reason == QUARANTINE_RESTORE_BUSY_REASON:
        engine = exc.engine_body if isinstance(exc.engine_body, dict) else {}
        committed = engine.get("nothing_moved") is False
        if committed:
            audit = _audit_ids_of(exc)
            moved = engine.get("moved_chashes")
            n = len(moved) if isinstance(moved, list) else 0
            rows = f" (gc_audit {', '.join(str(a) for a in audit)})" if audit else ""
            return (f"quarantine restore: the engine was busy ({exc}). Part of this call was already committed: "
                    f"{n} chunk{'s' if n != 1 else ''} restored or attached by an earlier quarantine sibling{rows}; "
                    "the sibling that was busy and any after it were not touched. Wait a few seconds and run the "
                    "same command again: what is done reads present, the rest is restored."
                    f"{after}"), EXIT_BUSY
        return (f"quarantine restore: the engine was busy ({exc}). That call rolled back: nothing moved, was attached "
                f"or audited (no earlier quarantine sibling had committed). Wait a few seconds and run the same "
                f"command again.{after}"), EXIT_BUSY
    # The engine names its request fields; the operator types flags.
    said = str(exc).replace("quarantined_since / quarantined_before", "--quarantined-since / --quarantined-before")
    return f"quarantine restore refused by the engine: {said}{after}", EXIT_ENGINE_ERROR


@click.group("quarantine")
def quarantine_group() -> None:
    """Operate on ``quarantine-*`` collections (RDR-192)."""


@quarantine_group.command("restore")
@click.option("--collection", "-c", "collection", required=True, metavar="NAME",
              help="The collection the chunks were quarantined FROM (not the quarantine- sibling).")
@click.option("--chash", "chashes", multiple=True, metavar="HEX",
              help="A chunk to restore, 64 lowercase hex characters; repeatable.")
@click.option("--audit-id", "audit_id", type=int, default=None, metavar="N",
              help="Restore the chashes a gc_audit row lists (find it with "
              "`nx catalog gc-audit list --operation reaper_quarantine`). A gc_quarantine_orphans row "
              "lists only a sample and is refused: use the window options for those.")
@click.option("--quarantined-since", "since", default=None, metavar="WHEN",
              help="Restore the chunks quarantined from this collection at or after WHEN "
              "(ISO-8601 date or datetime, UTC when naive).")
@click.option("--quarantined-before", "before", default=None, metavar="WHEN",
              help="Restore the chunks quarantined from this collection before WHEN.")
@click.option("--reattach/--no-reattach", "reattach", default=True,
              help="Also write the owner document's manifest row for a chunk whose metadata names a document "
              "that is still live, so the chunk is visible to search and get again (the default). "
              "--no-reattach moves the bytes only.")
@click.option("--dry-run", is_flag=True, default=False,
              help="Report what would be restored and attached; move nothing and write no audit row.")
@click.option("--json", "as_json", is_flag=True, default=False, help="Emit one JSON document.")
def restore_cmd(collection: str, chashes: tuple[str, ...], audit_id: int | None, since: str | None,
                before: str | None, reattach: bool, dry_run: bool, as_json: bool) -> None:
    """Restore chunks from a collection's quarantine sibling.

    \b
    Moves the chunks back in one engine statement, under the exclusive sweep
    gate, with no manifest row required: this is the way back for a chunk the
    engine reaper quarantined wrongly (a manifest defect), which has no manifest
    row for ``gc_restore_rereferenced`` to find. Name the chunks one of three ways:
      --chash HEX ...                           explicit chashes
      --audit-id N                              the chash list of a gc_audit row
      --quarantined-since / --quarantined-before   a window over the sibling

    \b
    A chunk with no live owning manifest row is hidden from search and get, so
    moving the bytes is not enough. --reattach (the default) also writes the
    manifest row when the chunk's metadata names a document that is still live in
    the collection, at the chunk's own position. It reaches chunks that name
    their document in their own metadata (an older legacy chunk, or a note put
    with `nx store put`) and single-chunk legacy notes; a chunk cut from an
    indexed file names nothing, comes back as bytes, and needs the file
    re-indexed. It writes nothing, and says `superseded` with the reason, when
    the document's current text is live: its manifest already holds a chunk at
    that position, it is stamped complete, the chunk was cut from another
    content hash, or the position is past the end of what it registers; or when
    another stored chunk (in the collection or in quarantine) claims the same
    position, so the version is ambiguous; or the document is mid index run. The
    manifest is never changed to make room. A chunk it cannot attach is still
    restored and stays HIDDEN; the output says what to do about it (for a
    knowledge__ collection, the `nx store put` that re-puts the note under its
    title; for a file collection, re-index the file). A chunk the collection
    already holds is left alone and reported present (never overwritten), and a
    present chunk with no manifest row is reattached the same way, so a restore
    made with --no-reattach can be finished by running it again. A chash that is
    nowhere reports missing. Every restore or attach writes one gc_audit row
    (operation quarantine_restore) with the full chash list. When some but not
    all of a multi-chunk document's chunks have a row, the output says
    "M of N attached". Use --dry-run first: it reports the same verdicts.

    \b
    Exit codes:
      0  every requested chunk was restored or was already present, and is
         visible to search and get (attached, or already owned).
      1  a requested chunk is missing from quarantine, or its embedding width
         conflicts with the collection's row; a dry run exits 1 for the same
         reasons. Wins over 3.
      2  a bad option (nothing was sent).
      3  every requested chunk is back but at least one stays HIDDEN from search
         and get (no live owner names it, reattach refused it, or --no-reattach
         left it); a dry run exits 3 when a real run would leave it hidden. The
         JSON document carries the count as "hidden".
      4  the connected engine predates the restore route; upgrade it.
      5  the engine refused the request or failed (the message says why).
      6  the engine was busy (a manifest writer held a lock): the statement that
         was busy rolled back; run the same command again. Each quarantine
         collection is restored in its own transaction, so the message says whether
         an earlier one had already committed (and its gc_audit ids); a chunk that
         was restored reads present on the rerun.
      A failure after the first page still prints the report of the pages already
      committed, with their audit ids, before exiting 4, 5 or 6.

    \b
    Examples:
      nx t3 quarantine restore -c knowledge__x__voyage-context-3__v1 --chash 3f...a9 --dry-run
      nx t3 quarantine restore -c knowledge__x__voyage-context-3__v1 --audit-id 4125
      nx t3 quarantine restore -c docs__x__voyage-context-3__v1 --quarantined-since 2026-09-28
    """
    named = [bool(chashes), audit_id is not None, since is not None or before is not None]
    if sum(named) != 1:
        raise click.UsageError(
            "Name exactly one source: --chash, --audit-id, or --quarantined-since / --quarantined-before.")
    normalised: list[str] = []
    for value in chashes:
        if not _CHASH_RE.match(value):
            raise click.BadParameter(f"{value!r} is not 64 lowercase hex characters", param_hint="--chash")
        if value not in normalised:
            normalised.append(value)
    since_iso = _instant("--quarantined-since", since)
    before_iso = _instant("--quarantined-before", before)
    if since_iso and before_iso and since_iso >= before_iso:
        raise click.UsageError("--quarantined-since must be earlier than --quarantined-before.")

    client = _make_t3()
    rows: list[dict] = []
    totals = dict.fromkeys(_OUTCOMES, 0)
    audit_ids: list[int] = []
    siblings: list[str] = []
    source: dict | None = None
    pages_done = 0
    failure: tuple[str, int] | None = None
    busy_detail: dict[str, Any] = {}
    try:
        for page in _pages(client, collection, chashes=normalised, audit_id=audit_id,
                           since=since_iso, before=before_iso, dry_run=dry_run, reattach=reattach,
                           actor=_actor()):
            rows.extend(page.get("rows") or [])
            for outcome in _OUTCOMES:
                totals[outcome] += int(page.get(outcome) or 0)
            # One audit row per quarantine collection that moved something; an engine that predates the list
            # reports the single ``audit_id``.
            page_audits = page.get("audit_ids")
            if page_audits is None:
                page_audits = [page["audit_id"]] if page.get("audit_id") is not None else []
            audit_ids.extend(int(a) for a in page_audits)
            for name in page.get("quarantine_collections") or ([page["quarantine_collection"]]
                                                               if page.get("quarantine_collection") else []):
                if name not in siblings:
                    siblings.append(name)
            if page.get("source"):
                source = {**(source or {}), **page["source"]}
            pages_done += 1
    except Exception as exc:  # noqa: BLE001 — every engine failure is reported with what was committed before it
        failure = _error_for(exc, pages_done)
        # A busy trip on a later quarantine sibling leaves the earlier ones committed: their audit rows are part of
        # what this run did, so the report carries them beside the pages that completed.
        audit_ids.extend(a for a in _audit_ids_of(exc) if a not in audit_ids)
        busy_body = getattr(exc, "engine_body", None)
        if isinstance(busy_body, dict) and getattr(exc, "reason", None) == QUARANTINE_RESTORE_BUSY_REASON:
            busy_detail = {"nothing_moved": busy_body.get("nothing_moved"),
                           "moved_chashes": busy_body.get("moved_chashes") or []}

    reapable = sorted(((_parse_instant(r["reapable_after"]), r["reapable_after"]) for r in rows
                       if r.get("outcome") == "restored" and r.get("no_manifest") and r.get("reapable_after")),
                      key=lambda pair: pair[0])
    earliest = reapable[0][1] if reapable else None
    unrestored = totals["missing"] + totals["dim_conflict"]
    hidden_rows = _hidden(rows, reattach)
    owners = _owners(rows)

    if as_json:
        doc: dict[str, Any] = {
            "origin_collection": collection,
            "quarantine_collection": siblings[0] if siblings else None,
            "quarantine_collections": siblings,
            "dry_run": dry_run,
            "reattach": reattach,
            "source": source,
            "totals": totals,
            "reattach_totals": _tally(rows),
            "hidden": len(hidden_rows),
            "owners": owners,
            "partial_owners": _partial(owners),
            "audit_ids": audit_ids,
            "reapable_again_after": earliest,
            "rows": rows,
        }
        if failure:
            doc["error"] = {"message": failure[0], "exit_code": failure[1], "pages_committed": pages_done,
                            **busy_detail}
        click.echo(json.dumps(doc, indent=2))
    else:
        if rows or not failure:
            _render_text(collection, siblings, dry_run, reattach, rows, totals, audit_ids, source, earliest,
                         _content_type(collection) if hidden_rows else "")
    if failure:
        click.echo(failure[0], err=True)
        sys.exit(failure[1])
    if unrestored:
        sys.exit(EXIT_UNRESTORED)
    if hidden_rows:
        sys.exit(EXIT_HIDDEN)
