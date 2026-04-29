# SPDX-License-Identifier: AGPL-3.0-or-later
# Copyright (c) 2026 Hal Hildebrand. All rights reserved.
"""``nx dt`` — DEVONthink integration verbs (RDR-099 P2).

Glue between the macOS-only :mod:`nexus.devonthink` selectors and the
existing ``nx index pdf`` / ``nx index md`` ingest paths. The operator
picks records in DT (selection / tag / group / smart group / UUID) and
``nx dt index`` walks each ``(uuid, path)`` pair into the right indexer
by file extension.

Mutual exclusion is enforced at the Click layer — exactly one selector
flag must be supplied. ``--uuid`` accepts ``multiple=True`` so batch
ingest of a known UUID list (e.g. from a smart-rule) doesn't require
shell-side fan-out.

Per-record dispatch lives in :func:`_index_record`. Tests monkeypatch
this single function rather than the heavyweight ``doc_indexer``
machinery, so the CLI surface (flag wiring, mutual-exclusion, dry-run,
error mapping) is exercised independently of the indexer internals.
"""
from __future__ import annotations

from pathlib import Path

import click
import structlog

import nexus.devonthink as dt_mod
from nexus.devonthink import DTNotAvailableError

_log = structlog.get_logger(__name__)


_SUPPORTED_EXTS: frozenset[str] = frozenset({".pdf", ".md"})


def _index_record(
    uuid: str,
    path: str,
    *,
    collection: str | None,
    corpus: str,
    dry_run: bool,
) -> None:
    """Dispatch a single supported ``(uuid, path)`` to the right indexer.

    The caller (``index_cmd``) is responsible for filtering unsupported
    extensions before calling this function — that lets tests and the
    summary line see the skip count without having to introspect the
    dispatcher's internals.

    Tests monkeypatch this single function rather than the heavyweight
    ``doc_indexer`` machinery so the CLI surface is exercised
    independently of Voyage credentials and Chroma clients.
    """
    if dry_run:
        # Dry-run is handled in the command body before this function
        # is reached. If a caller invokes us with dry_run=True anyway,
        # treat it as a no-op rather than a silent indexing run.
        return

    from nexus.doc_indexer import index_markdown, index_pdf  # noqa: PLC0415

    file_path = Path(path)
    ext = file_path.suffix.lower()
    if ext == ".pdf":
        index_pdf(file_path, corpus=corpus, collection_name=collection)
    else:  # .md — extension filtering happens in index_cmd
        index_markdown(file_path, corpus=corpus)


@click.group("dt")
def dt() -> None:
    """DEVONthink integration verbs (macOS only).

    Subcommands wrap DEVONthink so DT-side selections (or smart groups,
    tags, groups) flow into Nexus indexing without manual UUID/path
    copying. Requires DEVONthink to be running for selectors that read
    live application state.
    """


@dt.command("index")
@click.option(
    "--selection",
    "use_selection",
    is_flag=True,
    default=False,
    help="Index records currently selected in DEVONthink's UI.",
)
@click.option(
    "--tag",
    default=None,
    help="Index every record carrying this tag (use --database to scope).",
)
@click.option(
    "--group",
    "group_path",
    default=None,
    help="Index every record under this group path (recursive). "
    "Use --database to scope to one library.",
)
@click.option(
    "--smart-group",
    "smart_group",
    default=None,
    help="Execute the named smart group's query and index its results. "
    "Honours the smart group's own scope and exclude-subgroups flag.",
)
@click.option(
    "--uuid",
    "uuids",
    multiple=True,
    default=(),
    help="Index a single record by UUID. Repeat for batch ingest.",
)
@click.option(
    "--database",
    default=None,
    help="Limit selectors to one DEVONthink database. Default: all open libraries.",
)
@click.option(
    "--collection",
    default=None,
    help="T3 collection override (e.g. knowledge__papers). Forwarded to the underlying indexer.",
)
@click.option(
    "--corpus",
    default="default",
    show_default=True,
    help="Corpus name for docs__ collection (used when --collection is not set).",
)
@click.option(
    "--dry-run",
    is_flag=True,
    default=False,
    help="Print the records that would be indexed; make no T3 writes.",
)
def index_cmd(
    use_selection: bool,
    tag: str | None,
    group_path: str | None,
    smart_group: str | None,
    uuids: tuple[str, ...],
    database: str | None,
    collection: str | None,
    corpus: str,
    dry_run: bool,
) -> None:
    """Index DEVONthink records into Nexus.

    Exactly one selector flag must be provided: ``--selection``,
    ``--tag``, ``--group``, ``--smart-group``, or one or more ``--uuid``.
    """
    selectors_used = sum([
        use_selection,
        tag is not None,
        group_path is not None,
        smart_group is not None,
        bool(uuids),
    ])
    if selectors_used == 0:
        raise click.UsageError(
            "Provide exactly one selector: --selection, --tag, --group, "
            "--smart-group, or --uuid (one or more).",
        )
    if selectors_used > 1:
        raise click.UsageError(
            "Selectors are mutually exclusive: pick one of --selection, "
            "--tag, --group, --smart-group, or --uuid.",
        )

    try:
        records = _gather_records(
            use_selection=use_selection,
            tag=tag,
            group_path=group_path,
            smart_group=smart_group,
            uuids=uuids,
            database=database,
        )
    except DTNotAvailableError as e:
        raise click.ClickException(str(e)) from e

    if not records:
        click.echo("No records found.")
        return

    if dry_run:
        click.echo(f"Would index {len(records)} record(s):")
        for uuid, path in records:
            click.echo(f"  {uuid}\t{path}")
        return

    indexed = 0
    skipped = 0
    for uuid, path in records:
        ext = Path(path).suffix.lower()
        if ext not in _SUPPORTED_EXTS:
            _log.warning(
                "dt_skip_unsupported_extension",
                uuid=uuid,
                path=path,
                ext=ext,
            )
            skipped += 1
            continue
        _index_record(
            uuid,
            path,
            collection=collection,
            corpus=corpus,
            dry_run=False,
        )
        indexed += 1
    click.echo(f"Indexed {indexed} record(s) ({skipped} skipped).")


def _gather_records(
    *,
    use_selection: bool,
    tag: str | None,
    group_path: str | None,
    smart_group: str | None,
    uuids: tuple[str, ...],
    database: str | None,
) -> list[tuple[str, str]]:
    """Resolve the chosen selector to ``[(uuid, path), ...]``.

    Mutual exclusion is enforced upstream — exactly one branch fires.
    Selectors are accessed via the :mod:`nexus.devonthink` module
    (rather than ``from nexus.devonthink import _dt_selection``) so
    tests can monkeypatch the module attributes.
    """
    if use_selection:
        return dt_mod._dt_selection()
    if tag is not None:
        return dt_mod._dt_tag_records(tag, database=database)
    if group_path is not None:
        return dt_mod._dt_group_records(group_path, database=database)
    if smart_group is not None:
        return dt_mod._dt_smart_group_records(smart_group, database=database)
    # uuids — one resolver call per UUID, results merged.
    out: list[tuple[str, str]] = []
    for u in uuids:
        out.extend(dt_mod._dt_uuid_record(u))
    return out
