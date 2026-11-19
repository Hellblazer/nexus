# SPDX-License-Identifier: AGPL-3.0-or-later
"""``nx catalog span``: print a ``dchash:`` span for a passage of a document (RDR-171 Step 3).

The catalog comes through the module object (``_cat_cmd._get_catalog()``), so
this module stays acyclic with ``nexus.commands.catalog`` and the
``patch("nexus.commands.catalog._get_catalog", ...)`` test seam works.
"""

from __future__ import annotations

import click


@click.command("span")
@click.argument("tumbler")
@click.option(
    "--passage", default=None,
    help="Quote the passage to pin (recommended). It must occur exactly once in the document.",
)
@click.option("--start", type=int, default=None, help="First code point of the span (with --end).")
@click.option("--end", type=int, default=None, help="One past the last code point of the span (with --start).")
def span_cmd(tumbler: str, passage: str | None, start: int | None, end: int | None) -> None:
    """Print a dchash span for a passage of TUMBLER's source file.

    TUMBLER is a tumbler (1.1.5), a title or filename, or a source URI, as for
    `nx catalog link`. The span pins the passage by its offsets in the document's canonical text
    and survives re-chunking of unchanged text. Give it to `nx catalog link`
    as --from-span or --to-span.

    \b
    Two modes, exactly one per call:
      --passage TEXT          quote the passage (recommended); line ends in TEXT
                              match either LF or CRLF in the file
      --start N --end N       offsets, half-open, in Unicode code points of the
                              canonical text (not bytes, not UTF-16 units)

    The canonical text is the source file decoded as strict UTF-8, one leading
    BOM removed, CRLF and then lone CR turned into LF. A passage that occurs
    more than once, or not at all, is refused. PDFs and nx-note documents have
    no text source and are refused.
    """
    if not tumbler.strip():
        raise click.UsageError("TUMBLER is empty.")
    by_offsets = start is not None or end is not None
    if passage is not None and by_offsets:
        raise click.UsageError("--passage cannot be combined with --start/--end.")
    if passage is None and not by_offsets:
        raise click.UsageError("give --passage TEXT, or --start N with --end N.")
    if by_offsets and (start is None or end is None):
        raise click.UsageError("--start and --end go together.")

    import httpx  # noqa: PLC0415 — keeps CLI startup light

    from nexus.catalog.dchash import dchash_span, dchash_span_for_passage  # noqa: PLC0415 — keeps CLI startup light
    from nexus.commands import catalog as _cat_cmd  # noqa: PLC0415 — module-routed helper access keeps import acyclic + monkeypatch-visible

    cat = _cat_cmd._get_catalog()
    try:
        # A title or URI resolves as for `nx catalog link`; the engine's /show matches tumblers only.
        resolved = str(_cat_cmd._resolve_tumbler(cat, tumbler))
        if passage is not None:
            span = dchash_span_for_passage(cat, resolved, passage)
        else:
            assert start is not None and end is not None
            span = dchash_span(cat, resolved, start, end)
    except UnicodeDecodeError as exc:
        raise click.ClickException(
            f"{tumbler}: the source file is not valid UTF-8 ({exc.reason} at byte {exc.start})"
        ) from exc
    except ValueError as exc:
        raise click.ClickException(str(exc)) from exc
    except OSError as exc:
        raise click.ClickException(f"{tumbler}: cannot read the source file: {exc}") from exc
    except (httpx.HTTPError, RuntimeError) as exc:
        raise click.ClickException(f"{tumbler}: catalog lookup failed: {exc}") from exc
    click.echo(span)


def register(group: click.Group) -> None:
    """Attach ``span`` to the shared ``catalog`` group."""
    group.add_command(span_cmd)
