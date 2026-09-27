# SPDX-License-Identifier: AGPL-3.0-or-later
# Copyright (c) 2026 Hal Hildebrand. All rights reserved.
"""GH #896 (render/validate half; the in-place converter is nexus-sxiay):
``nx://catalog/<tumbler>`` markdown-link resolution.

Docs cite catalog entries as markdown links using a custom URL scheme,
e.g. ``[tumbler 1.1.194](nx://catalog/1.1.194)`` — nothing resolved
that scheme. This module scans for the link shape, batch-resolves the
tumblers against the existing catalog client (``resolve_many`` /
``get_owner_by_prefix`` — no new catalog route), and formats the
render-time footnote / validate-time failure line.

A markdown LINK, not a ``{{ns:key}}`` token: the citation is already
valid markdown ``[display](scheme:...)`` syntax authors write today
(GH #896's own example), and the RDR-082 token grammar is reserved for
system-of-record / projection-derived VALUES interpolated into prose
(``{{bd:x.status}}`` expands to a string); a catalog reference is a
structural hyperlink whose target the render step annotates, exactly
the shape RDR-083's ``chash:`` citations already use — so this reuses
that scanner-plus-footnote precedent rather than growing the token
grammar a third family for the same shape.

Mirrors the ``chash:`` citation + footnote pattern (RDR-083
``nexus.doc.citations``, RDR-086 ``nexus.commands.doc._append_chash_footnotes``)
but a genuinely absent tumbler is a DIFFERENT case from a degraded
catalog service: ``resolve_many`` reports the former by simple omission
(no exception) and raises for the latter, so this module never
conflates "not found" with "couldn't check" — the same distinction the
RDR-086 nexus-ib6uy chash fix drew, for the same reason: silently
downgrading a service outage to "not found" would bake a false
dangling-reference verdict into the rendered/validated doc.
"""
from __future__ import annotations

import re
from collections.abc import Callable
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from nexus.doc._common import iter_plain_lines

__all__ = [
    "CatalogLink",
    "CatalogLinkResolutionError",
    "ResolvedCatalogLinks",
    "scan_catalog_links",
    "scan_and_resolve_catalog_links",
    "resolve_catalog_links",
    "format_footnote",
    "format_unresolved_footnote",
]


# nx://catalog/<tumbler> — tumbler is 2+ dotted non-negative integers
# (store.owner.document[.chunk], Tumbler.parse's own grammar).
_CATALOG_LINK_RE = re.compile(
    r"\[(?P<display>[^\]\n]+)\]\(nx://catalog/(?P<tumbler>\d+(?:\.\d+)+)\)"
)


class CatalogLinkResolutionError(Exception):
    """Raised when the catalog service itself failed while resolving
    ``nx://catalog/<tumbler>`` links — as opposed to a tumbler that
    simply does not exist, which :func:`resolve_catalog_links` reports
    by omission (no exception). Callers should abort rather than treat
    this as "unresolved".
    """


@dataclass(slots=True)
class CatalogLink:
    """One ``nx://catalog/<tumbler>`` markdown-link occurrence."""

    display: str
    tumbler: str
    lineno: int   # 1-based source line
    col: int       # 1-based column of the opening ``[``


def scan_catalog_links(md_text: str) -> list[CatalogLink]:
    """Return every ``nx://catalog/<tumbler>`` link, in source order,
    with NO deduplication — every occurrence, including repeats of the
    same tumbler on different lines. Callers that want one entry per
    unique tumbler (footnote emission) dedupe themselves; callers that
    must report every citing line (``nx doc validate``) must NOT.

    Fenced code blocks are skipped (mirrors ``scan_citations`` /
    ``parse_tokens``): a tutorial snippet demonstrating the syntax is
    not itself a reference.
    """
    links: list[CatalogLink] = []
    for lineno, line in iter_plain_lines(md_text):
        for m in _CATALOG_LINK_RE.finditer(line):
            links.append(CatalogLink(
                display=m.group("display"),
                tumbler=m.group("tumbler"),
                lineno=lineno,
                col=m.start() + 1,
            ))
    return links


def resolve_catalog_links(
    tumblers: list[str], reader: Any,
) -> tuple[dict[str, Any], dict[str, str], dict[str, str]]:
    """Batch-resolve unique *tumblers* via the catalog reader's
    ``resolve_many``, follow a merged duplicate to its canonical entry,
    then best-effort resolve each result's owner name.

    Returns ``(entries, owner_names, merged_into)``:

    * ``entries`` maps tumbler string -> ``CatalogEntry`` for every
      tumbler that resolved. A tumbler absent from this dict is a
      genuine miss (never registered, deleted, or a typo'd reference)
      — ``resolve_many`` reports that by omission, not an exception.
      For a tumbler whose row is itself a MERGED duplicate (its
      ``alias_of`` is set — ``CatalogRepository.mergeDocuments`` sets
      it on the duplicate but does NOT tombstone the row, so
      ``resolve_many`` returns it as "resolved" even though it is
      stale), this holds the CANONICAL entry instead, resolved in one
      additional batch call — never the stale duplicate's own data.
    * ``owner_names`` maps owner-prefix string (e.g. ``"1.1"``) ->
      human owner name, best-effort — a missing/unreadable owner
      record just omits that key; callers fall back to the prefix
      itself.
    * ``merged_into`` maps the ORIGINAL (duplicate) tumbler string ->
      the canonical tumbler string it was redirected to. Present only
      when the redirect actually resolved; if the canonical side is
      itself unresolvable (should not happen absent a race), the
      duplicate's own — still real — data is kept and this dict omits
      that tumbler.

    Raises :class:`CatalogLinkResolutionError` when a resolve call
    itself fails (service degraded / unreachable) — this must NOT be
    conflated with "no such tumbler".
    """
    unique = sorted(set(tumblers))
    if not unique:
        return {}, {}, {}
    entries: dict[str, Any] = dict(_resolve_many_or_raise(reader, unique))

    # nexus-w715w / GH #896 review item 4: CatalogEntry already exposes
    # alias_of on the wire (the engine's documentFields() selects it for
    # every documentFields()-backed route, resolve_many included) — no
    # engine change needed to see it.
    alias_targets = {
        tumbler: entry.alias_of
        for tumbler, entry in entries.items()
        if getattr(entry, "alias_of", "")
    }
    merged_into: dict[str, str] = {}
    if alias_targets:
        canon_unique = sorted(set(alias_targets.values()))
        canon_entries = _resolve_many_or_raise(reader, canon_unique)
        for tumbler, canon_tumbler in alias_targets.items():
            canon_entry = canon_entries.get(canon_tumbler)
            if canon_entry is not None:
                entries[tumbler] = canon_entry
                merged_into[tumbler] = canon_tumbler
            # else: canonical side didn't resolve — keep the duplicate's
            # own (still real) entry rather than manufacture a miss.

    owner_names: dict[str, str] = {}
    for entry in entries.values():
        owner_prefix = str(entry.tumbler.owner_address())
        if owner_prefix in owner_names:
            continue
        try:
            owner = reader.get_owner_by_prefix(owner_prefix)
        except Exception:  # noqa: BLE001 — owner label is cosmetic; best-effort only
            owner = None
        if owner and owner.get("name"):
            owner_names[owner_prefix] = owner["name"]
    return entries, owner_names, merged_into


def _resolve_many_or_raise(reader: Any, tumblers: list[str]) -> dict[str, Any]:
    try:
        return reader.resolve_many(tumblers)
    except Exception as exc:  # noqa: BLE001 — normalized into a typed error below
        raise CatalogLinkResolutionError(
            f"catalog service unavailable while resolving nx://catalog links: {exc}"
        ) from exc


@dataclass(slots=True)
class ResolvedCatalogLinks:
    """Everything ``nx doc render``/``nx doc validate`` need from one
    file's ``nx://catalog/`` links — the single scan+resolve pass both
    commands share (GH #896 review item 5) instead of each keeping its
    own copy of the loop."""

    links: list[CatalogLink] = field(default_factory=list)   # EVERY occurrence, not deduped
    entries: dict[str, Any] = field(default_factory=dict)
    owner_names: dict[str, str] = field(default_factory=dict)
    merged_into: dict[str, str] = field(default_factory=dict)


def scan_and_resolve_catalog_links(
    text: str, get_reader: Callable[[], Any],
) -> ResolvedCatalogLinks:
    """Scan *text* and batch-resolve every unique tumbler it cites.

    *get_reader* is called — and so opens a catalog client — ONLY when
    *text* actually carries a link; a document with none never touches
    the catalog. Any failure from *get_reader* itself (as well as from
    the resolve calls) is normalized into
    :class:`CatalogLinkResolutionError`, so callers have one exception
    type to catch regardless of which step failed.
    """
    links = scan_catalog_links(text)
    if not links:
        return ResolvedCatalogLinks()
    try:
        reader = get_reader()
    except CatalogLinkResolutionError:
        raise
    except Exception as exc:  # noqa: BLE001 — normalized into a typed error above
        raise CatalogLinkResolutionError(
            f"cannot open catalog reader: {exc}"
        ) from exc
    entries, owner_names, merged_into = resolve_catalog_links(
        [link.tumbler for link in links], reader,
    )
    return ResolvedCatalogLinks(
        links=links, entries=entries, owner_names=owner_names,
        merged_into=merged_into,
    )


def _safe_link_target(entry: Any) -> str | None:
    """Return a link string safe to embed in rendered output, or ``None``.

    nexus-w715w / GH #896 review item 2: NEVER emit a ``file://`` URI or
    an absolute filesystem path — ``nx index repo`` derives
    ``source_uri`` as ``file://<abspath>`` for every registration
    (``CatalogRepository.deriveSourceUri``), so preferring it
    unconditionally leaked the indexing machine's own ``/Users/...``
    layout into rendered/shared output. Preference order:

    1. ``source_uri``, when its scheme is NOT ``file://`` (``https://``,
       ``x-devonthink-item://``, ...) — those are portable by
       construction.
    2. ``file_path``, when it is a RELATIVE path — repo-relative and
       therefore portable.

    Neither candidate qualifies -> ``None`` (render title/type/owner
    only, no link segment).
    """
    source_uri = entry.source_uri or ""
    if source_uri and not source_uri.startswith("file://"):
        return source_uri
    file_path = entry.file_path or ""
    if file_path and not Path(file_path).is_absolute():
        return file_path
    return None


def format_footnote(
    link: CatalogLink,
    entry: Any,
    owner_names: dict[str, str],
    *,
    merged_into: str | None = None,
) -> str:
    """Render one resolved catalog entry as a footnote line.

    ``- \\`nx://catalog/<tumbler>\\` — **<title>** (<content_type>,
    owner: <owner>) — [<link>](<link>)`` where ``<link>`` is chosen by
    :func:`_safe_link_target` (never a ``file://`` URI or an absolute
    path — see its docstring) and omitted entirely when neither
    candidate qualifies. When *merged_into* is set (the cited tumbler
    is a merged duplicate — see :func:`resolve_catalog_links`), an
    additional ``— merged into \\`nx://catalog/<canonical>\\``` segment
    is inserted before the link.
    """
    owner_prefix = str(entry.tumbler.owner_address())
    owner_label = owner_names.get(owner_prefix, owner_prefix)
    title = entry.title or link.tumbler
    content_type = entry.content_type or "unknown"
    head = (
        f"- `nx://catalog/{link.tumbler}` — **{title}** "
        f"({content_type}, owner: {owner_label})"
    )
    if merged_into:
        head += f" — merged into `nx://catalog/{merged_into}`"
    target = _safe_link_target(entry)
    if target:
        head += f" — [{target}]({target})"
    return head


def format_unresolved_footnote(link: CatalogLink) -> str:
    """Render a footnote line for a tumbler that no longer resolves."""
    return f"- `nx://catalog/{link.tumbler}` — [unresolved tumbler: {link.tumbler}]"
