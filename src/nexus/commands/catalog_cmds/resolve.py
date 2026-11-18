# SPDX-License-Identifier: AGPL-3.0-or-later
"""``nx catalog resolve``: a tumbler, owner or corpus to physical T3 collection names (GH #1668).

The same function as the MCP tool ``resolve`` (``nexus.catalog.lookup.resolve_collections``).
The catalog comes through the module object (``_cat_cmd._get_catalog()``), so
this module stays acyclic with ``nexus.commands.catalog`` and the
``patch("nexus.commands.catalog._get_catalog", ...)`` test seam works.
"""

from __future__ import annotations

import json

import click


@click.command("resolve")
@click.argument("tumbler_arg", metavar="[TUMBLER]", required=False)
@click.option("--tumbler", "tumbler_opt", default="", help="Document tumbler (dotted, e.g. 1.2.3) or source URI. Same as the positional argument.")
@click.option("--owner", default="", help="Owner tumbler (e.g. 1.2) or registered owner name.")
@click.option(
    "--corpus", default="",
    help="A collection content type (code, docs, rdr, knowledge): its live collections; "
    "or a free-form document corpus label, matched exactly.",
)
@click.option("--json", "as_json", is_flag=True, help="Print the collection names as a JSON array.")
def resolve_cmd(tumbler_arg: str | None, tumbler_opt: str, owner: str, corpus: str, as_json: bool) -> None:
    """Resolve a tumbler, owner or corpus to its physical T3 collection names.

    Prints one collection name per line (sorted, distinct); pass the names to
    `nx search --corpus`. Give any of TUMBLER, --owner and --corpus; the result
    is the union.

    \b
    --corpus takes either:
      a content type   code, docs, rdr or knowledge: every live collection of
                       that type (empty when none is live)
      a corpus label   the free-form `corpus` label of documents, matched exactly
    Any other value is an error (exit 1) that lists the valid content types.
    """
    if tumbler_arg and tumbler_opt and tumbler_arg != tumbler_opt:
        raise click.UsageError("give the tumbler once: positional TUMBLER and --tumbler differ.")
    tumbler = tumbler_arg or tumbler_opt
    if not (tumbler or owner or corpus):
        raise click.UsageError("give a TUMBLER, --owner or --corpus.")

    import httpx  # noqa: PLC0415 — keeps CLI startup light

    from nexus.catalog.lookup.resolve_collections import resolve_collections  # noqa: PLC0415 — keeps CLI startup light
    from nexus.commands import catalog as _cat_cmd  # noqa: PLC0415 — module-routed helper access keeps import acyclic + monkeypatch-visible

    cat = _cat_cmd._get_catalog()
    try:
        names = resolve_collections(cat, tumbler=tumbler, owner=owner, corpus=corpus)
    except ValueError as exc:
        raise click.ClickException(str(exc)) from exc
    except httpx.HTTPError as exc:
        raise click.ClickException(f"catalog lookup failed: {exc}") from exc
    if as_json:
        click.echo(json.dumps(names))
    else:
        for name in names:
            click.echo(name)


def register(group: click.Group) -> None:
    """Attach ``resolve`` to the shared ``catalog`` group."""
    group.add_command(resolve_cmd)
