# SPDX-License-Identifier: AGPL-3.0-or-later
# Copyright (c) 2026 Hal Hildebrand. All rights reserved.
"""``nx catalog owners --execute move`` (RDR-254, plan item 254-P2-S4): keep a document's or a repo owner's identity
when its path moves.

Two targets, one report. ``--document <tumbler> --new-path <file>`` re-points a curator document at the file's new
path and absorbs a live row already registered there (the GH #1571 state: the file was indexed again from its new
path, so a second document holds the same content). ``--owner <prefix> --new-root <dir>`` re-roots a repo owner at a
moved repository, folding an owner already registered at the new root into it and rewriting its documents' URIs.

Double-gated like the other ``nx catalog owners`` mutation arms: a dry run by default, and a write only with
``--no-dry-run --confirm``. The dry run runs the engine's own transaction and rolls it back, so its counts are the
counts the write will report; its last line is the exact write command.

The order is the contract: the options are checked and the new path is read from disk first (no network), then
:func:`~nexus.catalog.engine_writes.floors.require_catalog_move` asks the engine whether it has the routes, and only
then is a move route called. An engine that cannot confirm the routes is refused with nothing sent.

SPELLING. A document's new identity is computed the way a fresh curator registration computes it
(:func:`nexus.doc_indexer._curator_identity`: ``Path.resolve()``, and the raw ``file://<abspath>``, never
``Path.as_uri()``'s percent-encoded form), so the row the move produces is the row a later ``nx index`` of that file
finds. An owner's new root is the MAIN repository's path and its hash (:func:`nexus.repo_identity._repo_identity_with_main`),
so a worktree of the repository names the repository, as ``nx index repo`` does."""
from __future__ import annotations

import shlex
from pathlib import Path
from typing import Any

import click
import httpx

from nexus.commands.catalog_cmds.owners import _inside_git_work_tree, _report_only_notice

#: ``(report key, label)``, in print order. ``absorbed`` rows are TRASHED (their content, links and aspect rows move
#: onto the surviving row), and ``trashed`` counts only the rows of a folded owner that had no counterpart.
_COUNT_ROWS: tuple[tuple[str, str], ...] = (
    ("repointed", "repointed"),
    ("absorbed", "absorbed (trashed)"),
    ("trashed", "trashed (folded owner's rows with no counterpart)"),
    ("skipped", "skipped"),
    ("unchanged", "unchanged"),
    ("aspect_rows_rewritten", "aspect rows rewritten"),
    ("aspect_rows_deleted", "aspect rows deleted"),
    ("highlight_rows_rewritten", "highlight rows rewritten"),
    ("highlight_rows_deleted", "highlight rows deleted"),
    ("links_remapped", "links remapped"),
    ("links_collapsed", "links collapsed"),
    ("links_dropped", "links dropped"),
    ("owners_folded", "owners folded"),
)


def check_usage(action: str | None, document: str | None, new_path: str | None, owner: str | None,
                new_root: str | None, do_census: bool) -> None:
    """Refuse a malformed combination before anything is read. ``--document``, ``--new-path`` and ``--new-root`` belong
    to ``--execute move`` alone, and a move names exactly one target with the new location that fits it."""
    if action != "move":
        stray = [f for f, v in (("--document", document), ("--new-path", new_path), ("--new-root", new_root)) if v]
        if stray:
            raise click.ClickException(f"{', '.join(stray)} only apply to --execute move.")
        return
    if do_census:
        raise click.ClickException("--execute move does not take --census: it names its target.")
    if bool(document) == bool(owner):
        raise click.ClickException("--execute move needs exactly one of --document <tumbler> or --owner <prefix>.")
    if document and (not new_path or new_root):
        raise click.ClickException("--execute move --document needs --new-path <file> (and no --new-root).")
    if owner and (not new_root or new_path):
        raise click.ClickException("--execute move --owner needs --new-root <directory> (and no --new-path).")


def _document_target(document: str, new_path: str) -> dict[str, Any]:
    from nexus.catalog.tumbler import Tumbler  # noqa: PLC0415 — command-local import
    from nexus.doc_indexer import _curator_identity  # noqa: PLC0415 — deferred: the indexer module is heavy

    try:
        tumbler = Tumbler.parse(document)
    except ValueError as exc:
        raise click.ClickException(f"--document must be a document tumbler such as 1.2.3, got {document!r}: {exc}") from exc
    path = Path(new_path)
    if not path.is_file():
        raise click.ClickException(f"--new-path {new_path!r} is not an existing file. Nothing was written.")
    file_path, _legacy, source_uri = _curator_identity(path, path.parent)
    return {"tumbler": str(tumbler), "new_file_path": file_path, "new_source_uri": source_uri}


def _owner_target(owner: str, new_root: str) -> dict[str, Any]:
    from nexus.repo_identity import _repo_identity_with_main  # noqa: PLC0415 — command-local import

    root = Path(new_root)
    if not root.is_dir():
        raise click.ClickException(f"--new-root {new_root!r} is not an existing directory. Nothing was written.")
    if not _inside_git_work_tree(root):
        raise click.ClickException(
            f"--new-root {new_root!r} is not inside a git work tree: a repo owner's root is a repository. "
            f"Nothing was written."
        )
    _name, repo_hash, main_repo = _repo_identity_with_main(root)
    return {"owner": owner.strip(), "new_root": str(main_repo), "new_repo_hash": repo_hash}


def _refusal(exc: httpx.HTTPStatusError) -> click.ClickException:
    status = exc.response.status_code
    try:
        text = exc.response.json().get("error") or exc.response.text
    except ValueError:
        text = exc.response.text
    what = "refused the move" if status == 409 else f"rejected the request ({status})"
    return click.ClickException(f"The engine {what}: {text}. Nothing was written.")


def _echo_report(heading: str, report: dict[str, Any], *, will_act: bool) -> None:
    click.echo(f"{heading} ({'write' if will_act else 'dry run'})")
    for key, label in _COUNT_ROWS:
        if key in report:
            click.echo(f"  {label}: {report[key]}")
    absorbed = report.get("absorbed_tumblers") or []
    if absorbed:
        click.echo(f"  absorbed documents ({len(absorbed)}, now trashed):")
        for t in absorbed:
            click.echo(f"    {t}")


def run_move(*, document: str | None, new_path: str | None, owner: str | None, new_root: str | None,
             dry_run: bool, confirm: bool) -> None:
    """Run the move arm. See the module docstring for the order of the steps."""
    if document:
        target = _document_target(document, str(new_path))
        form = (f"nx catalog owners --execute move --document {shlex.quote(document)} "
                f"--new-path {shlex.quote(str(new_path))} --no-dry-run --confirm")
        heading = f"Move document {target['tumbler']} to {target['new_file_path']}"
    else:
        target = _owner_target(str(owner), str(new_root))
        form = (f"nx catalog owners --execute move --owner {shlex.quote(str(owner))} "
                f"--new-root {shlex.quote(str(new_root))} --no-dry-run --confirm")
        heading = f"Move owner {target['owner']} to {target['new_root']} (hash {target['new_repo_hash']})"

    from nexus.catalog.engine_writes.floors import CatalogMoveUnsupportedError, require_catalog_move  # noqa: PLC0415
    from nexus.commands import catalog as _cat_cmd  # noqa: PLC0415 — module-routed helper access keeps import acyclic + monkeypatch-visible

    try:
        require_catalog_move(_cat_cmd._get_catalog(), what="nx catalog owners --execute move")
    except CatalogMoveUnsupportedError as exc:
        raise click.ClickException(str(exc)) from exc

    will_act = _report_only_notice(dry_run, confirm)
    writer = _cat_cmd._get_catalog_writer()
    try:
        if document:
            report = writer.move_document(
                target["tumbler"], new_source_uri=target["new_source_uri"], new_file_path=target["new_file_path"],
                dry_run=not will_act)
        else:
            report = writer.move_owner(
                target["owner"], new_root=target["new_root"], new_repo_hash=target["new_repo_hash"],
                dry_run=not will_act)
    except httpx.HTTPStatusError as exc:
        raise _refusal(exc) from exc
    finally:
        writer.close()

    _echo_report(heading, report, will_act=will_act)
    if will_act:
        click.echo("Done.")
    else:
        click.echo("(dry-run: no catalog writes performed.)")
        click.echo(form)
