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

from nexus.commands.catalog_cmds.owners import _inside_git_work_tree

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


def _body_text(exc: httpx.HTTPStatusError) -> tuple[str, str]:
    """``(error message, field detail)`` of an engine error body; a body that is not a JSON object gives its text."""
    response = exc.response
    try:
        body = response.json()
    except ValueError:
        body = None
    if not isinstance(body, dict):
        return (response.text or "").strip(), ""
    message = str(body.get("error") or response.text or "").strip()
    field = body.get("field")
    detail = f" (field {field!s}: {body['value']!s})" if field and "value" in body else (f" (field {field!s})" if field else "")
    return message, detail


def _failure(exc: httpx.HTTPStatusError, *, will_act: bool) -> click.ClickException:
    """What a failed route call says it knows. A 400 or 409 is the engine refusing before it writes (or rolling back),
    so nothing was written. Any other status (a 5xx, a gateway or edge answer) leaves the outcome unknown."""
    status = exc.response.status_code
    message, detail = _body_text(exc)
    if status in (400, 409):
        what = "refused the move" if status == 409 else f"rejected the request ({status})"
        again = " Run it again." if "integrity constraint violation" in message else ""
        return click.ClickException(f"The engine {what}: {message}{detail}. Nothing was written.{again}")
    if will_act:
        return click.ClickException(
            f"The move's outcome is unknown: {exc}. Re-run the same command without --no-dry-run --confirm "
            f"(a dry run) to check what is left to move; re-running is safe."
        )
    return click.ClickException(f"The dry run failed: {exc}. A dry run writes nothing; re-running it is safe.")


def _transport_failure(exc: httpx.TransportError, *, will_act: bool) -> click.ClickException:
    """A timeout or lost connection: the engine may or may not have finished. ``nx catalog``'s precedent
    (``_service_timeout_error``): say it may have completed, and that running again is safe."""
    if will_act:
        return click.ClickException(
            f"the nexus service did not answer the move ({type(exc).__name__}). The move may have completed "
            f"server-side; re-run the same command without --no-dry-run --confirm (a dry run) to see; "
            f"re-running is safe."
        )
    return click.ClickException(
        f"the nexus service did not answer the dry run ({type(exc).__name__}). A dry run writes nothing; "
        f"re-running it is safe."
    )


def _echo_report(heading: str, report: dict[str, Any], *, will_act: bool, owner_move: bool) -> None:
    click.echo(f"{heading} ({'write' if will_act else 'dry run'})")
    for key, label in _COUNT_ROWS:
        if key == "trashed" and not owner_move:
            continue  # only a folded owner's rows can be trashed outright; a document move trashes through "absorbed"
        if key in report:
            click.echo(f"  {label}: {report[key]}")
    absorbed = report.get("absorbed_tumblers") or []
    if absorbed:
        fate = "now trashed" if will_act else "would be trashed"
        click.echo(f"  absorbed documents ({len(absorbed)}, {fate}):")
        for t in absorbed:
            click.echo(f"    {t}")


def run_move(*, document: str | None, new_path: str | None, owner: str | None, new_root: str | None,
             dry_run: bool, confirm: bool) -> None:
    """Run the move arm. See the module docstring for the order of the steps."""
    if document:
        target = _document_target(document, str(new_path))
        form = (f"nx catalog owners --execute move --document {shlex.quote(target['tumbler'])} "
                f"--new-path {shlex.quote(target['new_file_path'])} --no-dry-run --confirm")
        heading = f"Move document {target['tumbler']} to {target['new_file_path']}"
    else:
        target = _owner_target(str(owner), str(new_root))
        form = (f"nx catalog owners --execute move --owner {shlex.quote(target['owner'])} "
                f"--new-root {shlex.quote(target['new_root'])} --no-dry-run --confirm")
        heading = f"Move owner {target['owner']} to {target['new_root']} (hash {target['new_repo_hash']})"

    from nexus.catalog.engine_writes.floors import CatalogMoveUnsupportedError, require_catalog_move  # noqa: PLC0415
    from nexus.commands import catalog as _cat_cmd  # noqa: PLC0415 — module-routed helper access keeps import acyclic + monkeypatch-visible

    try:
        require_catalog_move(_cat_cmd._get_catalog(), what="nx catalog owners --execute move")
    except CatalogMoveUnsupportedError as exc:
        raise click.ClickException(str(exc)) from exc

    will_act = not dry_run and confirm
    if not dry_run and not confirm:
        click.echo("--no-dry-run alone is treated as report-only. Add --confirm to actually move.")
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
        raise _failure(exc, will_act=will_act) from exc
    except httpx.TransportError as exc:  # a timeout is one: the write is never resent, so say what is unknown
        raise _transport_failure(exc, will_act=will_act) from exc
    finally:
        writer.close()

    if report.get("dry_run") is not (not will_act):
        raise click.ClickException(
            f"the engine answered dry_run={report.get('dry_run')!r} to a request that asked for "
            f"dry_run={not will_act}; refusing to report a move it may not have made. Re-run the command "
            f"without --no-dry-run --confirm (a dry run) to see the state."
        )
    _echo_report(heading, report, will_act=will_act, owner_move=bool(owner))
    if will_act:
        click.echo("Done.")
    else:
        click.echo("(dry-run: no catalog writes performed.)")
        click.echo(form)
