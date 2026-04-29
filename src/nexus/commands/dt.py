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

import re
import subprocess
from pathlib import Path

import click
import structlog

import nexus.devonthink as dt_mod
from nexus.devonthink import DTNotAvailableError, _is_darwin

_log = structlog.get_logger(__name__)


_SUPPORTED_EXTS: frozenset[str] = frozenset({".pdf", ".md"})


def _index_record(
    uuid: str,
    path: str,
    *,
    collection: str | None,
    corpus: str,
    dry_run: bool,
) -> bool:
    """Dispatch a single supported ``(uuid, path)`` to the right indexer.

    The caller (``index_cmd``) is responsible for filtering unsupported
    extensions before calling this function — that lets tests and the
    summary line see the skip count without having to introspect the
    dispatcher's internals.

    After the indexer registers the catalog entry (with the resolved
    ``file://`` source_uri it sees), this function stamps the DT
    identity onto the entry: ``source_uri = x-devonthink-item://<UUID>``
    and ``meta.devonthink_uri`` set to the same value. RDR-099 AC-1
    requires this — the catalog identity must be stable across DT
    relocations, and the file path returned by osascript at index time
    is not (DT moves files inside Files.noindex/ on its own schedule).

    Returns the stamp's success status (``True`` when the catalog entry
    now carries the DT identity, ``False`` otherwise) so the caller can
    surface stamp misses in the summary line. Indexing itself is
    treated as a precondition: an indexer exception will propagate.

    Tests monkeypatch this single function rather than the heavyweight
    ``doc_indexer`` machinery so the CLI surface is exercised
    independently of Voyage credentials and Chroma clients.
    """
    if dry_run:
        # Dry-run is handled in the command body before this function
        # is reached. If a caller invokes us with dry_run=True anyway,
        # treat it as a no-op rather than a silent indexing run.
        return True

    from nexus.doc_indexer import index_markdown, index_pdf  # noqa: PLC0415

    file_path = Path(path)
    ext = file_path.suffix.lower()
    if ext == ".pdf":
        index_pdf(file_path, corpus=corpus, collection_name=collection)
    else:  # .md — extension filtering happens in index_cmd
        index_markdown(file_path, corpus=corpus, collection_name=collection)

    return _stamp_dt_uri_on_entry(file_path, uuid)


def _stamp_dt_uri_on_entry(file_path: Path, uuid: str) -> bool:
    """Set ``source_uri`` and ``meta.devonthink_uri`` on the catalog
    entry that was just indexed for ``file_path``.

    The indexer registers the entry with the resolved local path as
    ``source_uri`` (``file://...``); this is fine for non-DT ingest but
    breaks RDR-099 AC-1, where the catalog identity must survive DT
    moving the underlying file inside its ``Files.noindex/`` tree.
    Looking up the entry by ``file_path`` immediately after the indexer
    call is reliable because no other registrar runs between the two.

    Returns ``True`` when the entry now carries the DT identity,
    ``False`` on any miss (uninitialized catalog, no matching row,
    SQLite exception). Failures are logged and surfaced in the dt
    index summary line by the caller; the function does not raise so
    a stamp miss leaves a recoverable ``file://`` entry rather than
    aborting the whole batch. ``nx catalog update --source-uri`` can
    recover after the fact.
    """
    from nexus.catalog import resolve_tumbler  # noqa: PLC0415
    from nexus.catalog.catalog import Catalog  # noqa: PLC0415
    from nexus.config import catalog_path  # noqa: PLC0415

    dt_uri = f"x-devonthink-item://{uuid}"
    cat_path = catalog_path()
    if not Catalog.is_initialized(cat_path):
        _log.warning(
            "dt_stamp_skipped_uninitialized_catalog",
            file_path=str(file_path),
            uuid=uuid,
        )
        return False

    cat = Catalog(cat_path, cat_path / ".catalog.db")
    try:
        # Globally find the entry by file_path — no owner constraint
        # because we don't know it from here. ``documents`` is keyed
        # by tumbler primary key plus a unique (file_path) row per
        # indexed file, so this returns one row.
        row = cat._db.execute(
            "SELECT tumbler FROM documents WHERE file_path = ? LIMIT 1",
            (str(file_path),),
        ).fetchone()
        if row is None:
            _log.warning(
                "dt_stamp_no_entry_found",
                file_path=str(file_path),
                uuid=uuid,
            )
            return False

        from nexus.catalog.tumbler import Tumbler  # noqa: PLC0415

        tumbler = Tumbler.parse(row[0])
        cat.update(
            tumbler,
            source_uri=dt_uri,
            meta={"devonthink_uri": dt_uri},
        )
        _log.debug(
            "dt_stamp_applied",
            tumbler=str(tumbler),
            uuid=uuid,
            dt_uri=dt_uri,
        )
        return True
    except Exception as e:
        _log.warning(
            "dt_stamp_failed",
            file_path=str(file_path),
            uuid=uuid,
            error=str(e),
        )
        return False
    finally:
        cat._db.close()


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
    stamp_failed = 0
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
        stamped = _index_record(
            uuid,
            path,
            collection=collection,
            corpus=corpus,
            dry_run=False,
        )
        indexed += 1
        if not stamped:
            stamp_failed += 1
    summary = f"Indexed {indexed} record(s) ({skipped} skipped"
    if stamp_failed:
        # Stamp failure leaves the entry recoverable via
        # 'nx catalog update --source-uri x-devonthink-item://<UUID>'
        # — flag it so the operator knows the round-trip is broken
        # for those records.
        summary += f", {stamp_failed} DT-URI stamp-failed"
    summary += ")."
    click.echo(summary)
    if stamp_failed:
        click.echo(
            "Some records were indexed but their catalog entry still "
            "carries source_uri=file://… instead of x-devonthink-item://"
            "<UUID>. Inspect ~/Library/Logs (or your structlog sink) "
            "for 'dt_stamp_failed' events and recover with "
            "'nx catalog update <tumbler> --source-uri x-devonthink-item://<UUID>'.",
        )


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


# ── nx dt open ───────────────────────────────────────────────────────────────


# DT records use canonical 8-4-4-4-12 hex UUIDs; tumblers are
# dot-separated decimal numbers (e.g. ``1.2.3``). The two shapes are
# disjoint — UUIDs have hyphens, tumblers have dots — so a single regex
# pair classifies the argument unambiguously.
_UUID_RE = re.compile(
    r"^[0-9A-Fa-f]{8}-[0-9A-Fa-f]{4}-[0-9A-Fa-f]{4}-"
    r"[0-9A-Fa-f]{4}-[0-9A-Fa-f]{12}$",
)
_TUMBLER_RE = re.compile(r"^\d+(\.\d+)+$")


def _select_dt_uri_from_entry(entry: object) -> str | None:
    """Pick the ``x-devonthink-item://`` URI off a catalog entry.

    Pure function over an entry-shaped object (anything exposing
    ``meta`` and ``source_uri``). Resolution order mirrors the
    substrate at ``catalog._resolve_via_devonthink``:

    1. ``meta.devonthink_uri`` if it starts with ``x-devonthink-item://``
       (the canonical reverse-lookup recorded on entries that came in
       via DEVONthink, e.g. anything indexed via ``nx dt index``).
    2. ``source_uri`` if it starts with ``x-devonthink-item://``
       (entries registered with a DT identity from the start).
    3. ``None`` otherwise — caller decides how to surface this.

    Extracted from :func:`_resolve_dt_uri_from_tumbler` so the
    selection rule is unit-testable without standing up a Catalog
    fixture.
    """
    meta = getattr(entry, "meta", {}) or {}
    if isinstance(meta, dict):
        dt_uri = meta.get("devonthink_uri", "")
        if isinstance(dt_uri, str) and dt_uri.startswith(
            "x-devonthink-item://",
        ):
            return dt_uri
    source_uri = getattr(entry, "source_uri", "")
    if isinstance(source_uri, str) and source_uri.startswith(
        "x-devonthink-item://",
    ):
        return source_uri
    return None


def _resolve_dt_uri_from_tumbler(tumbler: str) -> str | None:
    """Return the ``x-devonthink-item://`` URI for a tumbler, or
    ``None`` when the entry exists but carries no DT URI.

    Catalog plumbing only — the URI-selection rule lives in
    :func:`_select_dt_uri_from_entry`.

    Raises:
        click.ClickException: when the tumbler doesn't resolve to any
            catalog entry (caller surfaces this as a non-zero exit).
    """
    from nexus.catalog import resolve_tumbler  # noqa: PLC0415
    from nexus.catalog.catalog import Catalog  # noqa: PLC0415
    from nexus.config import catalog_path  # noqa: PLC0415

    path = catalog_path()
    if not Catalog.is_initialized(path):
        raise click.ClickException(
            "Catalog not initialized. Run 'nx catalog setup' first.",
        )
    cat = Catalog(path, path / ".catalog.db")
    try:
        t, err = resolve_tumbler(cat, tumbler)
        if err:
            raise click.ClickException(f"tumbler not found: {tumbler}")
        entry = cat.resolve(t)
        if entry is None:
            raise click.ClickException(f"tumbler not found: {tumbler}")
        return _select_dt_uri_from_entry(entry)
    finally:
        # CatalogDB owns the SQLite connection + WAL lock; close it
        # explicitly so back-to-back CliRunner invocations (and any
        # future in-process callers) don't leak the write lock until
        # GC. Existing nx catalog commands rely on process-exit cleanup
        # which is fine for one-shot CLI but not for in-process reuse.
        cat._db.close()


@dt.command("open")
@click.argument("tumbler_or_uuid")
def open_cmd(tumbler_or_uuid: str) -> None:
    """Open a record in DEVONthink by tumbler or UUID.

    A UUID-shaped argument (``8-4-4-4-12`` hex) is converted directly
    to ``x-devonthink-item://<UUID>`` — no catalog hit, no osascript.
    A tumbler (e.g. ``1.2.3``) is resolved through the catalog,
    preferring ``meta.devonthink_uri`` and falling back to
    ``source_uri`` when the entry was registered with a DT identity.
    """
    # Platform gate fires before any branch-specific work so non-darwin
    # users get the documented "macOS-only" message regardless of
    # argument shape. Previously the tumbler branch would open the
    # catalog and resolve the tumbler before checking platform, leaking
    # catalog errors (uninitialized, not-found) ahead of the real
    # diagnostic.
    if not _is_darwin():
        raise click.ClickException(
            "DEVONthink integration is macOS-only",
        )

    if _UUID_RE.match(tumbler_or_uuid):
        uri = f"x-devonthink-item://{tumbler_or_uuid}"
    elif _TUMBLER_RE.match(tumbler_or_uuid):
        uri = _resolve_dt_uri_from_tumbler(tumbler_or_uuid)
        if uri is None:
            raise click.ClickException(
                f"no DEVONthink URI for tumbler {tumbler_or_uuid}",
            )
    else:
        raise click.ClickException(
            "argument is neither a tumbler (e.g. 1.2.3) nor a UUID "
            "(e.g. 8EDC855D-213F-40AD-A9CF-9543CC76476B).",
        )

    subprocess.run(["open", uri], check=True)  # noqa: S603,S607


# ── DT-side AppleScript installer (nexus-tv5u) ────────────────────────────────


# Manifest mapping each shipped .applescript file to the DT subdirs it
# installs into. The actual files travel as wheel package data via
# ``[tool.hatch.build.targets.wheel.force-include]`` ("dt/scripts" ->
# "nexus/_resources/dt-scripts"); editable installs resolve the same
# path through the ``src/nexus/_resources/dt-scripts`` symlink. Adding
# a new script: drop it into ``dt/scripts/`` and add a manifest entry.
_DT_SCRIPT_MANIFEST: dict[str, tuple[str, ...]] = {
    "Index Selection in nx.applescript": ("Toolbar", "Menu"),
    "Index Selection in nx (Knowledge).applescript": ("Menu",),
    "Index Current Group in nx.applescript": ("Toolbar", "Menu"),
}

# DT4's bundle identifier. DT3 lives under ``com.devon-technologies.think3``;
# we deliberately target DT4 only because that's where the ``nx dt`` CLI
# was developed and exercised.
_DT_APP_SCRIPTS_SUBDIR = "com.devon-technologies.think"


def _default_app_scripts_dir() -> Path:
    """Default ``--app-scripts-dir`` location for installed scripts.

    DT4 watches subdirectories of ``~/Library/Application Scripts/
    com.devon-technologies.think/`` (Toolbar, Menu, Contextual Menu,
    Smart Rules, Reminders). The user must restart DT for a freshly-
    installed Toolbar script to be draggable in "View > Customize
    Toolbar…"; Menu items are picked up on next menu open.
    """
    return Path.home() / "Library" / "Application Scripts" / _DT_APP_SCRIPTS_SUBDIR


def _resolve_dt_script_source_dir() -> Path:
    """Resolve the package-data directory containing the shipped
    ``.applescript`` source files.

    Editable installs see ``src/nexus/_resources/dt-scripts`` (symlink
    to ``dt/scripts``). Wheel installs see the force-included copy
    inside the installed ``nexus/_resources/dt-scripts``. Both resolve
    via :func:`importlib.resources.files`.
    """
    from importlib.resources import as_file, files

    resource = files("nexus") / "_resources" / "dt-scripts"
    with as_file(resource) as resolved:
        return Path(resolved)


@dt.command("install-scripts")
@click.option(
    "--target",
    type=click.Choice(["toolbar", "menu", "all"], case_sensitive=False),
    default="all",
    show_default=True,
    help=(
        "Which DT script slot to install into. ``toolbar`` installs "
        "scripts into the Toolbar/ subdir (drag to add as toolbar "
        "buttons); ``menu`` installs into Menu/ (DT's Scripts menu, "
        "left of Help); ``all`` does both."
    ),
)
@click.option(
    "--uninstall",
    is_flag=True,
    help="Remove installed scripts instead of installing.",
)
@click.option(
    "--force",
    is_flag=True,
    help="Overwrite existing files without prompting.",
)
@click.option(
    "--dry-run",
    is_flag=True,
    help="Show what would happen without writing or deleting.",
)
@click.option(
    "--app-scripts-dir",
    type=click.Path(file_okay=False, path_type=Path),
    default=None,
    help=(
        "Override the DEVONthink Application Scripts directory. "
        "Defaults to ~/Library/Application Scripts/"
        "com.devon-technologies.think. Used by tests; rarely needed "
        "in practice."
    ),
)
def install_scripts_cmd(
    target: str,
    uninstall: bool,
    force: bool,
    dry_run: bool,
    app_scripts_dir: Path | None,
) -> None:
    """Install (or remove) DT-side AppleScripts that wrap ``nx dt index``.

    Drops one or more ``.applescript`` files into DEVONthink's
    Application Scripts subdirectories so the actions appear as
    toolbar buttons (Toolbar/) or in DT's own Scripts menu (Menu/).
    The scripts call back into ``nx dt index`` via ``do shell
    script``; this verb is purely the file-copying installer.

    Restart DEVONthink to make a newly-installed Toolbar script
    draggable in "View > Customize Toolbar…". Menu items are picked
    up on the next menu open.
    """
    if not _is_darwin():
        raise click.ClickException("DEVONthink is macOS-only")

    base = app_scripts_dir if app_scripts_dir is not None else _default_app_scripts_dir()

    targets_filter: set[str] = (
        {"Toolbar", "Menu"} if target == "all"
        else {target.capitalize()}
    )

    if uninstall:
        _uninstall_scripts(base, targets_filter, dry_run=dry_run)
        return

    src_dir = _resolve_dt_script_source_dir()
    _install_scripts(
        src_dir,
        base,
        targets_filter,
        force=force,
        dry_run=dry_run,
    )


def _install_scripts(
    src_dir: Path,
    base: Path,
    targets_filter: set[str],
    *,
    force: bool,
    dry_run: bool,
) -> None:
    """Copy each manifest entry into every applicable DT subdir."""
    written = 0
    skipped = 0
    for filename, manifest_targets in _DT_SCRIPT_MANIFEST.items():
        applicable = set(manifest_targets) & targets_filter
        if not applicable:
            continue
        source = src_dir / filename
        if not source.exists():
            raise click.ClickException(
                f"package-data file missing for manifest entry: {filename}",
            )
        for subdir in sorted(applicable):
            dest_dir = base / subdir
            dest = dest_dir / filename
            if dry_run:
                click.echo(f"would install: {dest}")
                continue

            if dest.exists() and not force:
                if not click.confirm(
                    f"{dest} already exists. Overwrite?",
                    default=False,
                ):
                    click.echo(f"skipped: {dest}")
                    skipped += 1
                    continue

            dest_dir.mkdir(parents=True, exist_ok=True)
            dest.write_bytes(source.read_bytes())
            click.echo(f"installed: {dest}")
            written += 1

    if dry_run:
        return

    click.echo("")
    click.echo(f"Done: {written} installed, {skipped} skipped.")
    if written:
        click.echo(
            "Restart DEVONthink to pick up Toolbar scripts in the "
            "'Customize Toolbar…' sheet. Menu items appear on next "
            "menu open.",
        )


def _uninstall_scripts(
    base: Path,
    targets_filter: set[str],
    *,
    dry_run: bool,
) -> None:
    """Remove every manifest entry from every applicable DT subdir.

    Idempotent on missing files: a clean tree returns success with a
    "0 removed" line so the caller can run uninstall freely without
    pre-checking.
    """
    removed = 0
    for filename, manifest_targets in _DT_SCRIPT_MANIFEST.items():
        applicable = set(manifest_targets) & targets_filter
        for subdir in sorted(applicable):
            dest = base / subdir / filename
            if not dest.exists():
                continue
            if dry_run:
                click.echo(f"would remove: {dest}")
                continue
            dest.unlink()
            click.echo(f"removed: {dest}")
            removed += 1

    if dry_run:
        return

    click.echo("")
    click.echo(f"Done: {removed} removed.")
