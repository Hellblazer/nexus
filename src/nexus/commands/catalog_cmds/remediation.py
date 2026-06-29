# SPDX-License-Identifier: AGPL-3.0-or-later
# Copyright (c) 2026 Hal Hildebrand. All rights reserved.
"""Path-remediation commands for the ``nx catalog`` group (nexus-whh61.4).

Carved out of ``commands.catalog``: ``remediate-paths`` (repair catalog
entries whose ``file_path`` is a bare basename or has gone missing) and
``prune-stale`` (drop entries whose absolute/owner-relative ``file_path`` is
missing on disk), together with the six private helpers they share
(``_build_basename_index``, ``_entry_needs_remediation``,
``_resolve_via_devonthink``, ``_resolve_candidate``, ``_rdr_prefix_of``,
``_build_rdr_prefix_index``) and the ``_REMEDIATE_DEFAULT_EXTENSIONS`` /
``_RDR_PREFIX_RE`` module constants. Those helpers are used ONLY by these two
commands, so they move here rather than staying in the god module; the two
test modules that import them by symbol were repointed to this module.

Behaviour-preserving; ``register`` attaches both commands to the shared
``catalog`` group. ``_get_catalog`` / ``_get_catalog_writer`` are reached
through the ``nexus.commands.catalog`` module object inside each command body
— keeping imports acyclic and preserving the
``patch("nexus.commands.catalog._get_catalog", …)`` test seam.
"""
from __future__ import annotations

import re as _re
from pathlib import Path

import click

from nexus.catalog.tumbler import Tumbler

# Default file extensions the remediator considers candidates. Mirrors the
# set of types the catalog tracks: PDFs (papers / docs__), markdown (RDR /
# docs__ prose). Code files are excluded by design — code ingest stores
# absolute paths from a registered repo root, not loose basenames.
_REMEDIATE_DEFAULT_EXTENSIONS: frozenset[str] = frozenset({
    ".pdf", ".md", ".markdown",
})

# nexus-zg4c: RDR-prefix matcher. RDRs are renamed end-to-end occasionally
# (rdr-066-enrichment-time → rdr-066-composition-smoke) but their numeric
# id is the durable handle. ``rdr-NNN-`` is the contract: digits, then a
# dash, then the slug. Three or more digits accommodates the eventual
# four-digit RDRs without rewriting the regex.
_RDR_PREFIX_RE = _re.compile(r"^(rdr-\d{3,}-)")


def _build_basename_index(
    source_dir: Path,
    extensions: frozenset[str] | None = _REMEDIATE_DEFAULT_EXTENSIONS,
) -> dict[str, list[Path]]:
    """Walk *source_dir* and return ``{basename: [absolute_path, ...]}``.

    Symlinks are followed; hidden directories (``.git``, ``.venv``) are
    pruned because they don't carry curated source documents and they
    would dominate the walk on large repos. ``extensions=None`` matches
    every file regardless of suffix (used by ``--extensions *``).
    """
    import os as _os  # noqa: PLC0415 — deferred import; rare/branch-local path or circular-dep / startup-cost avoidance
    index: dict[str, list[Path]] = {}
    for root, dirs, files in _os.walk(
        str(source_dir.resolve()), followlinks=True,
    ):
        # Prune hidden dirs in-place so os.walk doesn't descend into them.
        dirs[:] = [d for d in dirs if not d.startswith(".")]
        root_path = Path(root)
        for fname in files:
            if extensions is not None and Path(fname).suffix.lower() not in extensions:
                continue
            index.setdefault(fname, []).append(root_path / fname)
    return index


def _entry_needs_remediation(entry: object) -> tuple[bool, str]:
    """Return ``(needs_fix, reason)`` for a catalog entry.

    Reasons:
    * ``"basename"`` — file_path has no slash; resolves against cwd.
    * ``"missing"`` — file_path is absolute but does not exist on disk.
    * ``""`` — file_path is fine.

    Empty file_path entries (MCP-stored knowledge with no source file)
    are not remediable here — they return ``(False, "no-file-path")``.
    """
    fp = getattr(entry, "file_path", "") or ""
    if not fp:
        return (False, "no-file-path")
    if "/" not in fp:
        return (True, "basename")
    if not Path(fp).exists():
        return (True, "missing")
    return (False, "")


def _resolve_via_devonthink(entry: object) -> Path | None:
    """If ``entry.meta`` carries a ``devonthink_uri``, ask DEVONthink for
    the current filesystem path and return it when the file exists on
    disk. Returns ``None`` when no DT URI is recorded, when the platform
    isn't macOS, when osascript fails, or when DT reports a path that
    doesn't actually exist (a sign the resolver returned a stale cache).

    This is the companion path to making ``x-devonthink-item://`` a
    canonical source URI (nexus-bqda): even with a ``file://`` source URI
    we can still recover from DT relocations using the meta we already
    record on entries that came in via DEVONthink.
    """
    import sys  # noqa: PLC0415  — stdlib deferred to call site (sys)

    if sys.platform != "darwin":
        return None
    meta = getattr(entry, "meta", {}) or {}
    dt_uri = meta.get("devonthink_uri", "") if isinstance(meta, dict) else ""
    if not dt_uri or not dt_uri.startswith("x-devonthink-item://"):
        return None
    uuid = dt_uri[len("x-devonthink-item://"):]
    if not uuid:
        return None
    from nexus.aspect_readers import _devonthink_resolver_default  # noqa: PLC0415  — command-local import (nexus.aspect_readers)
    path, _detail = _devonthink_resolver_default(uuid)
    if path is None:
        return None
    p = Path(path)
    if not p.exists():
        return None
    return p


def _resolve_candidate(
    entry: object,
    candidates: list[Path],
    *,
    prefer_deepest: bool = False,
) -> tuple[Path | None, str]:
    """Pick a single candidate path for *entry*, or ``None``.

    Returns ``(path, note)`` where *note* explains the choice:
      * ``"unique"`` — exactly one candidate
      * ``"deepest"`` — multiple, picked the longest path
      * ``"ambiguous"`` — multiple and no resolution strategy applied
      * ``"none"`` — no candidates
    """
    if not candidates:
        return (None, "none")
    if len(candidates) == 1:
        return (candidates[0], "unique")
    if prefer_deepest:
        return (max(candidates, key=lambda p: len(str(p))), "deepest")
    return (None, "ambiguous")


def _rdr_prefix_of(file_path: str) -> str:
    """Return the ``rdr-NNN-`` prefix of *file_path*'s basename, or ``""``.

    Empty when *file_path* is empty, has no ``rdr-NNN-`` basename, or the
    digit run is shorter than three (which would match release tag
    artifacts like ``rdr-1-`` from migration scripts).
    """
    if not file_path:
        return ""
    basename = Path(file_path).name
    match = _RDR_PREFIX_RE.match(basename)
    return match.group(1) if match else ""


def _build_rdr_prefix_index(
    source_dir: Path,
) -> dict[str, list[Path]]:
    """Walk *source_dir* and return ``{rdr_prefix: [absolute_path, ...]}``.

    Only ``.md`` / ``.markdown`` files participate — RDRs are markdown.
    The prefix index lives alongside the basename index so the two-step
    lookup in ``--rdr-prefix-mode`` (basename first, prefix second) only
    walks the source tree once.
    """
    import os as _os  # noqa: PLC0415 — deferred import; rare/branch-local path or circular-dep / startup-cost avoidance
    index: dict[str, list[Path]] = {}
    for root, dirs, files in _os.walk(
        str(source_dir.resolve()), followlinks=True,
    ):
        dirs[:] = [d for d in dirs if not d.startswith(".")]
        root_path = Path(root)
        for fname in files:
            if Path(fname).suffix.lower() not in (".md", ".markdown"):
                continue
            prefix = _rdr_prefix_of(fname)
            if not prefix:
                continue
            index.setdefault(prefix, []).append(root_path / fname)
    return index


@click.command("remediate-paths")
@click.argument(
    "source_dir",
    type=click.Path(exists=True, file_okay=False, path_type=Path),
)
@click.option(
    "--dry-run", is_flag=True,
    help="Show the transition table without writing.",
)
@click.option(
    "--collection", default="",
    help="Limit remediation to entries in this physical collection.",
)
@click.option(
    "--owner", default="",
    help="Limit remediation to entries under this owner tumbler prefix.",
)
@click.option(
    "--prefer-deepest", is_flag=True,
    help="When multiple candidates share a basename, pick the deepest path "
         "(longest absolute path string). Default: skip ambiguous entries.",
)
@click.option(
    "--mark-missing", is_flag=True,
    help="For entries with no candidate found in SOURCE_DIR, set "
         "meta.status='missing' so 'nx catalog gc' can sweep them.",
)
@click.option(
    "--extensions", default="",
    help="Comma-separated extensions to scan (default: .pdf,.md,.markdown). "
         "Use '*' to scan every file regardless of extension.",
)
@click.option(
    "--rdr-prefix-mode", is_flag=True,
    help="When basename match fails for an RDR file, fall back to matching "
         "by ``rdr-NNN-`` prefix. Catches RDRs renamed end-to-end "
         "(e.g. rdr-066-enrichment-time.md → rdr-066-composition-smoke.md).",
)
def remediate_paths_cmd(
    source_dir: Path,
    dry_run: bool,
    collection: str,
    owner: str,
    prefer_deepest: bool,
    mark_missing: bool,
    extensions: str,
    rdr_prefix_mode: bool,
) -> None:
    """Repair catalog entries whose file_path is a basename or has gone missing.

    Walks SOURCE_DIR and matches catalog entries by basename. For each
    remediable entry, updates file_path to an absolute path under
    SOURCE_DIR. Use this after moving PDFs from ~/Downloads into a
    git-backed papers archive, or any time the original ingest paths
    no longer exist on disk.

    \b
    Examples:
      nx catalog remediate-paths ~/papers-archive --dry-run
      nx catalog remediate-paths ~/papers --collection knowledge__hybridrag
      nx catalog remediate-paths ~/papers --prefer-deepest --mark-missing

    Strategy:
      * Catalog entries with file_path = basename only (no slash) → look up
        by basename, update on unique match.
      * Catalog entries with file_path = absolute path that doesn't exist
        on disk → same lookup; treat as moved/recovered.
      * Multiple basename matches in SOURCE_DIR → ambiguous, skip
        (use --prefer-deepest to break ties by path length).
      * No basename match → leave alone, optionally mark with --mark-missing.

    Idempotent: re-running on the same SOURCE_DIR is a no-op once entries
    are resolved.
    """
    from nexus.commands import catalog as _cat_cmd  # noqa: PLC0415 — module-routed helper access keeps import acyclic + monkeypatch-visible

    ext_filter: frozenset[str] | None
    if extensions == "*":
        ext_filter = None  # match every file
    elif extensions:
        ext_filter = frozenset(
            e if e.startswith(".") else f".{e}"
            for e in (s.strip().lower() for s in extensions.split(","))
            if e
        )
    else:
        ext_filter = _REMEDIATE_DEFAULT_EXTENSIONS

    cat = _cat_cmd._get_catalog()
    writer = _cat_cmd._get_catalog_writer()

    click.echo(f"Scanning {source_dir.resolve()}…")
    index = _build_basename_index(source_dir, ext_filter)
    click.echo(f"Indexed {sum(len(v) for v in index.values())} files "
               f"({len(index)} unique basenames).")

    # Build the RDR prefix index up front so the resolution loop is one
    # walk-cost: avoids scanning source_dir twice when --rdr-prefix-mode is on.
    prefix_index: dict[str, list[Path]] = (
        _build_rdr_prefix_index(source_dir) if rdr_prefix_mode else {}
    )
    if rdr_prefix_mode:
        click.echo(
            f"Indexed {sum(len(v) for v in prefix_index.values())} RDR file(s) "
            f"({len(prefix_index)} unique prefixes)."
        )

    # Select entries to consider.
    entries: list = []
    if owner:
        entries = cat.by_owner(Tumbler.parse(owner))
    elif collection:
        # CatalogTaxonomy doesn't expose by_physical_collection directly;
        # walk all_documents and filter.
        entries = [
            e for e in cat.all_documents()
            if e.physical_collection == collection
        ]
    else:
        entries = cat.all_documents()

    if not entries:
        click.echo("No catalog entries to consider.")
        return

    # Categorise.
    transitions: list[tuple[object, str, str, Path | None, str]] = []
    skipped_ok = 0
    skipped_no_file_path = 0
    n_devonthink = 0
    for entry in entries:
        needs, reason = _entry_needs_remediation(entry)
        if not needs:
            if reason == "no-file-path":
                skipped_no_file_path += 1
            else:
                skipped_ok += 1
            continue
        basename = Path(entry.file_path).name
        # nexus-srck: try DEVONthink resolution before basename scan.
        # When meta carries devonthink_uri and DT reports an existing
        # path, that's authoritative — no point ranking basename matches
        # against a SOURCE_DIR walk that wouldn't include DT's
        # Files.noindex tree anyway.
        dt_path = _resolve_via_devonthink(entry)
        if dt_path is not None:
            n_devonthink += 1
            transitions.append((entry, reason, "devonthink", dt_path, basename))
            continue
        candidates = index.get(basename, [])
        chosen, note = _resolve_candidate(
            entry, candidates, prefer_deepest=prefer_deepest,
        )
        # Fallback: same-RDR-prefix replacement. Only fires when basename
        # match found nothing, the entry's basename has a usable rdr-NNN-
        # prefix, and --rdr-prefix-mode was requested.
        if (
            chosen is None
            and note == "none"
            and rdr_prefix_mode
        ):
            prefix = _rdr_prefix_of(basename)
            if prefix:
                prefix_candidates = prefix_index.get(prefix, [])
                chosen, note = _resolve_candidate(
                    entry, prefix_candidates, prefer_deepest=prefer_deepest,
                )
                # Annotate the note so the table tells the operator the
                # match came from the RDR-prefix path, not the basename
                # path (relevant when sorting which renames you've shipped).
                if chosen is not None and note in ("unique", "deepest"):
                    note = f"rdr-prefix:{note}"
        transitions.append((entry, reason, note, chosen, basename))

    # Report.
    n_total = len(transitions)
    n_resolved = sum(1 for _, _, _, p, _ in transitions if p is not None)
    n_ambiguous = sum(1 for _, _, n, _, _ in transitions if n == "ambiguous")
    n_missing = sum(1 for _, _, n, _, _ in transitions if n == "none")

    click.echo(
        f"\n{n_total} entries need remediation "
        f"(skipped {skipped_ok} already-good, {skipped_no_file_path} no-file-path):"
    )
    click.echo(f"  {n_resolved:4d} resolvable")
    if n_devonthink:
        click.echo(f"    of which {n_devonthink:4d} via DEVONthink")
    click.echo(f"  {n_ambiguous:4d} ambiguous (multiple basename matches)")
    click.echo(f"  {n_missing:4d} no candidate found in SOURCE_DIR")

    if not transitions:
        return

    # Show first ~20 transitions for visibility.
    click.echo("\nSample (first 20):")
    for entry, why, note, chosen, basename in transitions[:20]:
        old = entry.file_path or "(empty)"
        new = str(chosen) if chosen else f"<{note}>"
        click.echo(f"  [{why:8s}] {entry.tumbler}  {basename}\n    {old}\n  → {new}")

    if dry_run:
        click.echo("\n(dry-run — no catalog writes performed.)")
        return

    # Apply.
    n_updated = 0
    n_marked = 0
    for entry, _why, _note, chosen, _basename in transitions:
        if chosen is not None:
            writer.update(entry.tumbler, file_path=str(chosen))
            n_updated += 1
        elif mark_missing:
            writer.update(entry.tumbler, meta={"status": "missing"})
            n_marked += 1

    click.echo(
        f"\nDone: updated {n_updated} file_paths"
        + (f", marked {n_marked} as missing" if mark_missing else "")
        + "."
    )


@click.command("prune-stale")
@click.option(
    "--collection", default="",
    help="Limit prune to entries in this physical_collection.",
)
@click.option(
    "--owner", default="",
    help="Limit prune to entries under this owner tumbler prefix.",
)
@click.option(
    "--source-dir", "source_dir_opt", default="",
    type=click.Path(exists=False, file_okay=False, path_type=Path),
    help="Optional source directory to consult for RDR-prefix replacements; "
         "when set, entries whose ``rdr-NNN-`` prefix matches a file under "
         "SOURCE_DIR are skipped (preferring rename-aware remediation over "
         "destructive prune). Use --no-rdr-prefix-skip to disable that check.",
)
@click.option(
    "--rdr-prefix-skip/--no-rdr-prefix-skip",
    default=True,
    help="When --source-dir is set, skip entries whose RDR-prefix has a "
         "plausible replacement on disk. On by default.",
)
@click.option(
    "--dry-run/--no-dry-run", default=True,
    help="Report-only (default). Use --no-dry-run to perform deletions.",
)
@click.option(
    "--confirm", is_flag=True, default=False,
    help="Required alongside --no-dry-run to actually delete catalog rows.",
)
def prune_stale_cmd(
    collection: str,
    owner: str,
    source_dir_opt: Path,
    rdr_prefix_skip: bool,
    dry_run: bool,
    confirm: bool,
) -> None:
    """Drop catalog entries whose ``file_path`` is absolute and missing on disk.

    Catalog-side counterpart to ``nx t3 prune-stale`` (#349). Pairs
    naturally with ``nx catalog remediate-paths --rdr-prefix-mode``: run
    the remediator first to repair what's recoverable, then prune the
    rest.

    \b
    Default is read-only (--dry-run is on). To actually delete:
      nx catalog prune-stale --no-dry-run --confirm

    \b
    Examples:
      nx catalog prune-stale                                 # report all
      nx catalog prune-stale -c rdr__nexus-571b8edd          # one collection
      nx catalog prune-stale --source-dir docs/rdr           # honour rename hints
      nx catalog prune-stale --no-dry-run --confirm          # actually delete

    \b
    Skip rules — these are never deleted:
      * Empty file_path (MCP-stored entries with no source file).
      * Basename-only file_path (no ``/``) — remediable, not stale.
      * file_path that exists on disk.
      * RDR entries whose ``rdr-NNN-`` prefix matches a file under
        --source-dir, when --rdr-prefix-skip is on (default).
    """
    from nexus.commands import catalog as _cat_cmd  # noqa: PLC0415 — module-routed helper access keeps import acyclic + monkeypatch-visible

    will_delete = (not dry_run) and confirm
    if (not dry_run) and not confirm:
        click.echo(
            "--no-dry-run alone is treated as report-only. "
            "Add --confirm to actually delete catalog rows."
        )
        will_delete = False

    cat = _cat_cmd._get_catalog()
    writer = _cat_cmd._get_catalog_writer()

    # Build the RDR-prefix index lazily — only when both --source-dir is
    # set and --rdr-prefix-skip is on. Skipping the walk on the no-source
    # path is important; nx catalog prune-stale with no args should be
    # fast enough to run in a CI loop.
    prefix_index: dict[str, list[Path]] = {}
    if source_dir_opt and rdr_prefix_skip and source_dir_opt.exists():
        prefix_index = _build_rdr_prefix_index(source_dir_opt)

    # Select candidate entries.
    if owner:
        entries = cat.by_owner(Tumbler.parse(owner))
    elif collection:
        entries = [
            e for e in cat.all_documents()
            if e.physical_collection == collection
        ]
    else:
        entries = cat.all_documents()

    # nexus-6ims: relative file_paths must resolve against the owner's
    # repo_root (RDR-060), not against the running process's cwd. Pre-fix
    # logic used ``Path(fp).exists()`` directly, which caught absolute
    # paths fine but mass-misclassified relative paths whenever the
    # operator ran the verb from a different repo (verified 2026-05-08:
    # 11,766 valid entries reported as stale because cwd was nexus, not
    # the entry's owning repo).
    # nexus-xnz0o: use owners_with_roots() (uniform API).
    owner_roots = cat.owners_with_roots()

    stale: list = []  # entries to delete
    skipped_replacement: list = []  # entries with RDR-prefix replacement
    skipped_no_root: list = []  # owner has no repo_root — can't verify
    for entry in entries:
        fp = entry.file_path or ""
        if not fp:  # MCP-stored, no source file
            continue
        if "/" not in fp:  # basename-only — remediable
            continue

        if fp.startswith("/"):
            resolved = Path(fp)
        else:
            # Relative path — anchor at owner.repo_root.
            t_str = str(entry.tumbler)
            parts = t_str.split(".")
            owner_id = ".".join(parts[:2]) if len(parts) >= 2 else ""
            root = owner_roots.get(owner_id, "")
            if not root:
                # Owner has no repo_root (registered before RDR-060
                # added the column). Cannot verify presence; refuse to
                # delete — operator must repair owner.repo_root first
                # via ``nx catalog dedupe-owners`` or manual update.
                skipped_no_root.append(entry)
                continue
            resolved = Path(root) / fp

        if resolved.exists():  # live, not stale
            continue

        # Stale candidate. If a same-prefix replacement exists, prefer
        # remediation: skip prune.
        if prefix_index:
            prefix = _rdr_prefix_of(fp)
            if prefix and prefix_index.get(prefix):
                skipped_replacement.append(entry)
                continue
        stale.append(entry)

    n_stale = len(stale)
    n_skipped = len(skipped_replacement)
    n_no_root = len(skipped_no_root)
    parts_msg = []
    if n_skipped:
        parts_msg.append(f"skipped {n_skipped} with same-prefix replacement")
    if n_no_root:
        parts_msg.append(
            f"skipped {n_no_root} relative-path entries whose owner has "
            f"no repo_root (cannot verify)"
        )
    suffix = f" ({'; '.join(parts_msg)})" if parts_msg else ""
    click.echo(
        f"{n_stale} stale entr{'y' if n_stale == 1 else 'ies'}{suffix}."
    )

    if n_stale:
        click.echo("\nSample (first 20):")
        for entry in stale[:20]:
            click.echo(
                f"  {entry.tumbler}  [{entry.physical_collection or '-'}]  "
                f"{entry.file_path}"
            )

    if not will_delete:
        if dry_run:
            click.echo("\n(dry-run — no catalog writes performed.)")
        return

    # Backup snapshot before delete (RDR-106 Option A).
    from nexus.catalog.catalog_backup import snapshot_documents  # noqa: PLC0415 — deferred import; rare/branch-local path or circular-dep / startup-cost avoidance
    backup_path = snapshot_documents(
        cat,
        [str(e.tumbler) for e in stale],
        verb="prune-stale",
        reason="absolute path missing OR relative path missing under owner.repo_root",
        args={
            "collection": collection, "owner": owner,
            "source_dir": str(source_dir_opt) if source_dir_opt else "",
            "rdr_prefix_skip": rdr_prefix_skip,
        },
    )
    if backup_path:
        click.echo(
            f"\nBackup snapshot written: {backup_path}"
            f"\n  Restore with: nx catalog undelete {backup_path.name}"
        )

    n_deleted = 0
    for entry in stale:
        if writer.delete_document(entry.tumbler):
            n_deleted += 1

    click.echo(f"\nDone: deleted {n_deleted} catalog entr"
               f"{'y' if n_deleted == 1 else 'ies'}.")


def register(group: click.Group) -> None:
    """Attach the path-remediation commands to the shared ``catalog`` group."""
    group.add_command(remediate_paths_cmd)
    group.add_command(prune_stale_cmd)
