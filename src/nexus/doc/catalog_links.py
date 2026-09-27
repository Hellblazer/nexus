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

import os
import re
from collections.abc import Callable
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any
from urllib.parse import unquote, urlsplit

# nexus-w715w round 2: single source of truth for which source_uri
# schemes the catalog recognizes at all (register-boundary validation,
# `_normalize_source_uri`). Reused here — never re-typed — to derive the
# narrower "safe to emit as a clickable link" subset below.
from nexus.catalog.types import _KNOWN_URI_SCHEMES
from nexus.doc._common import iter_plain_lines

__all__ = [
    "CatalogLink",
    "CatalogLinkResolutionError",
    "ResolvedCatalogLinks",
    "SafeLinkTarget",
    "scan_catalog_links",
    "scan_and_resolve_catalog_links",
    "resolve_catalog_links",
    "owner_repo_roots_for",
    "safe_link_target",
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


def owner_repo_roots_for(entries: dict[str, Any], reader: Any) -> dict[str, str]:
    """Best-effort ``owner_prefix -> repo_root`` for every resolved
    entry's owner (nexus-w715w round 2).

    Needed to turn a safe, repo-relative ``file_path`` into a WORKING
    relative link (:func:`safe_link_target`) rather than emitting the
    repo-relative string as a link that only happens to resolve when the
    citing file sits at the repo root. Deliberately a SEPARATE pass
    rather than folded into :func:`resolve_catalog_links`'s own
    owner-name loop: that function's 3-tuple return is depended on by
    several existing callers/tests, and repo_root is only needed by a
    caller that renders a link, not by every consumer of
    entries/owner_names. One extra ``get_owner_by_prefix`` per unique
    owner — cheap, and best-effort like the owner-name lookup it mirrors.
    """
    repo_roots: dict[str, str] = {}
    seen: set[str] = set()
    for entry in entries.values():
        owner_prefix = str(entry.tumbler.owner_address())
        if owner_prefix in seen:
            continue
        seen.add(owner_prefix)
        try:
            owner = reader.get_owner_by_prefix(owner_prefix)
        except Exception:  # noqa: BLE001 — owner lookup is cosmetic; best-effort only
            owner = None
        if owner and owner.get("repo_root"):
            repo_roots[owner_prefix] = owner["repo_root"]
    return repo_roots


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


#: Schemes safe to emit as a clickable link target. A strict SUBSET of
#: `_KNOWN_URI_SCHEMES` (the catalog's full register-boundary allowlist):
#: excludes ``file`` (a host-local path — the very thing this helper
#: exists to never leak), ``chroma`` (retired substrate, RDR-155 P4b —
#: nothing resolves it), ``nx-orphan-backfill`` (an internal marker with
#: nothing to follow — see `_KNOWN_URI_SCHEMES`'s own comment), and
#: ``nx-scratch`` (code-review round: T1 scratch is SESSION-scoped —
#: a ``nx-scratch://`` URI is unresolvable by anyone outside the
#: session that wrote it, including the reader of a rendered doc
#: minutes or days later, so it is not a "working link" by this
#: function's own contract; entries carrying only a scratch source get
#: the file_path fallback or no link, same as any other unsafe source).
_LINK_SAFE_SCHEMES: frozenset[str] = _KNOWN_URI_SCHEMES - {
    "file", "chroma", "nx-orphan-backfill", "nx-scratch",
}

#: A Windows drive-letter absolute path (``C:\...`` or ``C:/...``).
_DRIVE_LETTER_RE = re.compile(r"^[A-Za-z]:[\\/]")


def _looks_unsafe_relative_path(raw: str) -> bool:
    """True when *raw* is NOT safely repo-relative.

    nexus-w715w round 2: catches everything ``Path(raw).is_absolute()``
    misses because that call is HOST-OS-NATIVE — a Windows drive-letter
    path or a UNC path reads as "relative" on a POSIX host, and a
    ``~``-relative or upward-escaping (``../..``) path reads as
    "relative" everywhere despite not staying inside the repo. This
    classifier is purely CONTENT-based (string patterns), so it catches
    all these regardless of the host OS running the check:

    * POSIX absolute (``/...``) or ``~``-relative.
    * UNC (``\\\\server\\share`` or ``//server/share``).
    * Windows drive-letter absolute (``C:\\...`` / ``C:/...``).
    * Any path whose ``..`` segments, once normalized across both ``/``
      and ``\\`` separators, would climb above the path's own start.

    Code-review round: classification runs against ``unquote(raw)``,
    not *raw* itself — a percent-encoded traversal (``%2e%2e%2f``) is
    invisible to every literal check above until decoded, and a
    catalog ``file_path`` is caller-supplied (the register/update
    boundary), so this is reachable. *raw* itself is unaffected — the
    decoded form is used ONLY to decide safety; a safe value is still
    joined/displayed using its original (possibly still-encoded)
    bytes, since real filesystem paths are not URL-decoded by this
    function.
    """
    decoded = unquote(raw)
    if not decoded:
        return True
    if decoded.startswith(("/", "~")):
        return True
    if decoded.startswith("\\\\") or decoded.startswith("//"):
        return True
    if _DRIVE_LETTER_RE.match(decoded):
        return True
    depth = 0
    for part in re.split(r"[\\/]+", decoded):
        if part in ("", "."):
            continue
        if part == "..":
            depth -= 1
            if depth < 0:
                return True
        else:
            depth += 1
    return False


@dataclass(slots=True)
class SafeLinkTarget:
    """The result of :func:`safe_link_target`: either a working,
    clickable link (``is_link=True``, render as ``[text](text)``) or a
    repo-relative path that is safe to SHOW but not confirmed to
    resolve (``is_link=False`` — a repo_root/base_dir was not supplied,
    so render *text* as plain, non-clickable text, e.g. labelled
    ``(repo-relative)``)."""

    text: str
    is_link: bool


def safe_link_target(
    entry: Any, *, base_dir: Path | None = None, repo_root: str | None = None,
) -> SafeLinkTarget | None:
    """Return the link/text safe to embed in rendered output, or
    ``None`` when nothing qualifies (render title/type/owner only).

    THE ONE PLACE in this codebase that composes a link from
    ``entry.source_uri`` / ``entry.file_path`` — every caller (currently
    :func:`format_footnote` and ``nexus.doc.footnote_converter``) MUST
    route through this function rather than reading those fields
    itself; ``tests/test_catalog_link_safety_single_source_lint.py``
    enforces that mechanically.

    nexus-w715w / GH #896 review item 2 (plus round 2 hardening): NEVER
    emit a ``file://`` URI or an absolute/UNC/``~``/upward-escaping
    filesystem path — ``nx index repo`` derives ``source_uri`` as
    ``file://<abspath>`` for every registration
    (``CatalogRepository.deriveSourceUri``), so preferring it
    unconditionally leaked the indexing machine's own ``/Users/...``
    layout into rendered/shared output. Preference order:

    1. ``source_uri``, when its scheme (parsed case-insensitively via
       ``urlsplit`` — never a bare ``.startswith("file://")`` string
       check, which a differently-cased or slashless ``file:`` URI slips
       past) is in :data:`_LINK_SAFE_SCHEMES` — an explicit ALLOWLIST,
       not "anything that isn't file://". Emitted verbatim (``https://``
       and ``x-devonthink-item://`` URIs are portable by construction).
    2. ``file_path``, when :func:`_looks_unsafe_relative_path` clears it
       (repo-relative, not absolute/UNC/``~``/escaping):

       * *repo_root* and *base_dir* both supplied — resolved to the
         real on-disk location and re-expressed as
         ``os.path.relpath(repo_root/file_path, base_dir)``: a WORKING
         relative link from the citing/rendered file's own directory,
         never an absolute path (``is_link=True``).
       * either is missing — the repo-relative string is shown as
         plain, non-clickable text (``is_link=False``): honest that it
         has not been confirmed to resolve from wherever the reader is.

    Neither candidate qualifies -> ``None``.
    """
    source_uri = entry.source_uri or ""
    if source_uri:
        scheme = urlsplit(source_uri).scheme.lower()
        is_file_like = scheme == "file" or source_uri.lower().startswith("file:")
        if not is_file_like and scheme in _LINK_SAFE_SCHEMES:
            return SafeLinkTarget(text=source_uri, is_link=True)
        # Any other scheme (file://, chroma://, unknown/unparseable) —
        # never emitted as a link; fall through to the file_path leg.

    file_path = entry.file_path or ""
    if not file_path or _looks_unsafe_relative_path(file_path):
        return None

    if repo_root and base_dir is not None:
        try:
            repo_root_resolved = Path(repo_root).resolve()
            base_dir_resolved = Path(base_dir).resolve()
            # Code-review round: a working relative link is only safe to
            # emit when the citing file is actually INSIDE this entry's
            # own repo — otherwise the "relative" link is a "../../.."
            # traversal into an entirely different repository's
            # filesystem layout, which is exactly the kind of local-
            # machine-topology leak this function exists to prevent
            # (see the module docstring precedent for source_uri). Cross-
            # repo falls back to the plain, non-clickable label below,
            # same as "repo_root/base_dir unknown".
            if base_dir_resolved.is_relative_to(repo_root_resolved):
                abs_target = (repo_root_resolved / file_path).resolve()
                rel = os.path.relpath(abs_target, base_dir_resolved)
                return SafeLinkTarget(text=rel, is_link=True)
        except (OSError, ValueError):
            pass
    return SafeLinkTarget(text=file_path, is_link=False)


def format_footnote(
    link: CatalogLink,
    entry: Any,
    owner_names: dict[str, str],
    *,
    merged_into: str | None = None,
    base_dir: Path | None = None,
    repo_root: str | None = None,
) -> str:
    """Render one resolved catalog entry as a footnote line.

    ``- \\`nx://catalog/<tumbler>\\` — **<title>** (<content_type>,
    owner: <owner>) — [<link>](<link>)`` where ``<link>`` is chosen by
    :func:`safe_link_target` (never a ``file://`` URI or an absolute
    path — see its docstring) and omitted entirely when neither
    candidate qualifies; a safe-but-unresolvable repo-relative path
    renders as plain ``(repo-relative) \\`<path>\\``` text instead of a
    link. When *merged_into* is set (the cited tumbler is a merged
    duplicate — see :func:`resolve_catalog_links`), an additional
    ``— merged into \\`nx://catalog/<canonical>\\``` segment is inserted
    before the link. Pass *base_dir* (the rendered/citing file's own
    directory) and *repo_root* (the entry's owner's repo root, e.g. from
    :func:`owner_repo_roots_for`) to get a WORKING relative link rather
    than an unresolvable repo-relative label.
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
    target = safe_link_target(entry, base_dir=base_dir, repo_root=repo_root)
    if target is not None:
        if target.is_link:
            head += f" — [{target.text}]({target.text})"
        else:
            head += f" — (repo-relative) `{target.text}`"
    return head


def format_unresolved_footnote(link: CatalogLink) -> str:
    """Render a footnote line for a tumbler that no longer resolves."""
    return f"- `nx://catalog/{link.tumbler}` — [unresolved tumbler: {link.tumbler}]"
