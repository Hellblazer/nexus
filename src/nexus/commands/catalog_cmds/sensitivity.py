# SPDX-License-Identifier: AGPL-3.0-or-later
# Copyright (c) 2026 Hal Hildebrand. All rights reserved.
"""``nx catalog sensitive`` (RDR-243 Phase 1 Step 3d, nexus-rfvyy.6.11).

Sets or clears the one-way document sensitivity flag after the document was written, through the audited
``POST /v1/catalog/sensitivity`` (``HttpCatalogClient.set_sensitivity``). The flag is the only thing this
verb moves; ``nx catalog list --sensitive`` is the matching read.

Order of refusals, each before the next costs anything:

1. Usage (no network): one target form, ``--clear`` carries a non-blank ``--reason``.
2. The capability probe (``require_catalog_sensitivity``): an engine without the column would drop the flag
   silently, so the verb refuses before it resolves a target or calls the route.
3. Target resolution. Zero matches is an error, not a success.
4. The route, in batches of at most ``_BATCH`` (the engine caps a request at 1000 tumblers).

Target forms (decisions recorded on nexus-rfvyy.6.11):

* ``TUMBLER`` is a dotted tumbler only, never a title: a privacy verb does not guess which document a title
  meant. It excludes every other option.
* ``--collection C`` is one server-filtered ``/list`` call.
* ``--path GLOB`` matches ``fnmatch`` against each entry's ``file_path`` (``*`` crosses ``/``). The engine has
  no glob, so it reads the catalog; ``--collection`` or ``--owner`` narrows the read first. Unnarrowed it walks
  every document, which RDR-241 allows because a person typed a command whose job is to read.
* ``--owner`` is accepted only with ``--path``.

There is no MCP tool for clearing, by design (RDR-243 Q5).
"""
from __future__ import annotations

import fnmatch
import json

import click

#: The route refuses a request of more than 1000 tumblers (``CatalogHandler.MAX_BATCH_DOC_IDS``); stay under it.
_BATCH: int = 500


def _check_usage(tumbler: str | None, collection: str, path: str, owner: str, clear: bool, reason: str) -> None:
    """Every refusal that needs no network. Raises ``click.UsageError``."""
    if tumbler is not None:
        if collection or path or owner:
            raise click.UsageError("TUMBLER cannot be combined with --collection, --path or --owner.")
    elif not (collection or path):
        raise click.UsageError("name what to flag: a TUMBLER, --collection C, or --path GLOB.")
    if owner and not path:
        raise click.UsageError("--owner narrows --path; pass --path GLOB with it.")
    if clear and not reason.strip():
        raise click.UsageError("--clear requires --reason TEXT (the reason is written to the audit table).")


def _targets(cat, tumbler: str | None, collection: str, path: str, owner: str) -> list[str]:
    """The tumblers to send, in a stable order and without repeats. Raises ``ClickException`` on zero."""
    from nexus.catalog.tumbler import Tumbler  # noqa: PLC0415 — command-local import
    from nexus.commands import catalog as _cat_cmd  # noqa: PLC0415 — module-routed helper access keeps import acyclic

    if tumbler is not None:
        try:
            parsed = Tumbler.parse(tumbler)
        except (ValueError, TypeError) as exc:
            raise click.BadParameter(f"{tumbler!r} is not a dotted tumbler (e.g. 1.2.3)", param_hint="TUMBLER") from exc
        if cat.resolve(parsed, follow_alias=True) is None:
            raise click.ClickException(f"Not found: {tumbler}")
        return [str(parsed)]

    owner_tumbler = _cat_cmd._resolve_owner_option(cat, owner) if owner else None
    if collection:
        entries = cat.list_by_collection(collection)
        if owner_tumbler is not None:
            entries = [e for e in entries if str(e.tumbler).startswith(f"{owner_tumbler}.")]
    elif owner_tumbler is not None:
        entries = cat.by_owner(owner_tumbler)
    else:
        entries = cat.all_documents(limit=0)
    if path:
        entries = [e for e in entries if e.file_path and fnmatch.fnmatchcase(e.file_path, path)]
    found = list(dict.fromkeys(str(e.tumbler) for e in entries))
    if not found:
        wanted = " ".join(
            p for p in (
                f"--collection {collection}" if collection else "",
                f"--path {path}" if path else "",
                f"--owner {owner}" if owner else "",
            ) if p
        )
        raise click.ClickException(f"No document matches {wanted}. Nothing was changed.")
    return found


@click.command("sensitive")
@click.argument("tumbler", required=False)
@click.option("--collection", "collection", default="", metavar="C", help="Every document in this physical collection.")
@click.option("--path", "path", default="", metavar="GLOB",
              help="Every document whose file path matches this glob (fnmatch; * crosses /). "
                   "Narrow with --collection or --owner to avoid reading the whole catalog.")
@click.option("--owner", "owner", default="", metavar="OWNER", help="With --path: only this owner's documents (tumbler or name).")
@click.option("--clear", "clear", is_flag=True, help="Clear the flag instead of setting it. Needs --reason.")
@click.option("--reason", "reason", default="", metavar="TEXT", help="Why. Required with --clear; written to the audit table.")
@click.option("--json", "as_json", is_flag=True)
def sensitive_cmd(
    tumbler: str | None, collection: str, path: str, owner: str, clear: bool, reason: str, as_json: bool,
) -> None:
    """Flag documents sensitive, or clear the flag (RDR-243).

    A sensitive document's content is kept away from the LLM-bound surfaces the privacy policy lists. The
    flag is one-way in every write path except this verb: setting needs no reason, clearing needs one, and
    every change writes an audit row. List the flagged documents with `nx catalog list --sensitive`.

    \b
    nx catalog sensitive 1.2.3
    nx catalog sensitive --collection knowledge__hr
    nx catalog sensitive --path 'docs/private/*' --owner 1.4
    nx catalog sensitive 1.2.3 --clear --reason "published 2026-11-05"
    """
    from nexus.catalog.http_catalog_client import (  # noqa: PLC0415 — command-local import
        CatalogSensitivityUnsupportedError,
        require_catalog_sensitivity,
    )
    from nexus.commands import catalog as _cat_cmd  # noqa: PLC0415 — module-routed helper access keeps import acyclic

    _check_usage(tumbler, collection, path, owner, clear, reason)
    cat = _cat_cmd._get_catalog()
    try:
        require_catalog_sensitivity(cat, what="nx catalog sensitive")
    except CatalogSensitivityUnsupportedError as exc:
        raise click.ClickException(str(exc)) from exc
    targets = _targets(cat, tumbler, collection, path, owner)

    flag = not clear
    documents: list[dict] = []
    writer = _cat_cmd._get_catalog_writer()
    try:
        for start in range(0, len(targets), _BATCH):
            batch = targets[start:start + _BATCH]
            try:
                out = writer.set_sensitivity(batch, flag, reason=reason.strip())
            except Exception as exc:  # noqa: BLE001 — name how far the run got; re-running finishes it (both directions are idempotent)
                raise click.ClickException(
                    f"{exc} ({len(documents)} of {len(targets)} document(s) changed before this; "
                    f"re-run the same command to finish)."
                ) from exc
            documents.extend(
                {"tumbler": t, "changed": bool(u)} for t, u in zip(out["targets"], out["updated"])
            )
    finally:
        writer.close()

    changed = sum(1 for d in documents if d["changed"])
    # RDR-243 Phase 3 fix r1 (nexus-rfvyy.8): a topic built before the flag keeps a label computed from the document,
    # and the Knowledge Map and the MCP cluster headers print it. Replace it now; `discover --force` rebuilds it.
    relabelled: list[int] | None = None
    topic_error = ""
    if flag and changed:
        from nexus import sensitive_topics  # noqa: PLC0415 - command-local import

        try:
            relabelled = sensitive_topics.withhold_after_flag(cat, [d["tumbler"] for d in documents if d["changed"]])
        except Exception as exc:  # noqa: BLE001 - the flag is set; say the topics were not relabelled and how to finish
            topic_error = f"{type(exc).__name__}: {exc}"[:300]
    if as_json:
        out = {"sensitive": flag, "matched": len(documents), "changed": changed, "documents": documents}
        if relabelled is not None:
            out["topics_relabelled"] = relabelled
        if topic_error:
            out["topics_error"] = topic_error
        click.echo(json.dumps(out, indent=2))
        return
    already = len(documents) - changed
    verb = "Flagged sensitive" if flag else "Cleared"
    suffix = f" ({already} already in that state)" if already else ""
    click.echo(f"{verb}: {changed} of {len(documents)} document(s) changed{suffix}.")
    for d in documents:
        if d["changed"]:
            click.echo(f"  {d['tumbler']}")
    if relabelled:
        from nexus.sensitive_topics import REDISCOVER_HINT  # noqa: PLC0415 - command-local import

        click.echo(f"{len(relabelled)} topic(s) hold chunks of these documents. {REDISCOVER_HINT}")
    if topic_error:
        click.echo(
            f"Warning: the topics holding these documents could not be relabelled ({topic_error}). A topic built "
            "before the flag may still print a label computed from them: run `nx taxonomy discover --force "
            "<collection>`.",
            err=True,
        )


def register(group: click.Group) -> None:
    """Attach ``sensitive`` to the shared ``catalog`` group."""
    group.add_command(sensitive_cmd)
