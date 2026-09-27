# SPDX-License-Identifier: AGPL-3.0-or-later
# Copyright (c) 2026 Hal Hildebrand. All rights reserved.
"""GH #896 (the in-place converter half; render/validate landed under
nexus-sevlu): ``nx catalog footnotes <file.md>`` — convert
``[label](nx://catalog/<tumbler>)`` markdown links IN PLACE in the
source file into stable GFM footnote markers, and back.

Built entirely on :mod:`nexus.doc.catalog_links`'s scan+resolve+link-
safety machinery — this module contributes NO second way to find a
``nx://catalog/`` link, resolve a tumbler, or decide what is safe to
emit as a link target; :func:`nexus.doc.catalog_links.scan_catalog_links`,
:func:`~nexus.doc.catalog_links.resolve_catalog_links` and
:func:`~nexus.doc.catalog_links.safe_link_target` are the single source
of truth for all three, shared with ``nx doc render``/``nx doc validate``.
It also delegates ALL fenced-code / inline-code-span / indented-code-
block exclusion to the shared scanner (:mod:`nexus.doc._common`), via
``scan_catalog_links``/``iter_plain_lines`` — no second masking pass
lives here either.

Marker form and why (code-review round, mid-line-citation exactness)
----------------------------------------------------------------------

A citation converts to ``[label][^tumbler-<slug>]`` — the label STAYS
bracketed; only the ``(nx://catalog/<tumbler>)`` target is replaced by
the footnote reference, appended immediately after the closing ``]``.
This is deliberately NOT ``label[^tumbler-<slug>]`` (label unbracketed):
without a bracket delimiter, reversing "As noted in the paper
[x](nx://catalog/1.1.1), it works." must guess where the label begins
against arbitrary preceding prose, and a guess is wrong exactly when
the citation is not alone at the start of its line (the common case).
With the label re-bracketed, the reverse regex needs no guess at all —
``\\[label\\]\\[\\^slug\\]`` is unambiguous regardless of what precedes
or follows it on the line, how many citations share the line, or
whether the label itself contains brackets that the ORIGINAL scan
already delimited correctly.

GFM validity: ``[^ref]`` is recognized by GitHub's footnote extension
as its OWN inline token — a caret immediately inside brackets — not as
part of ordinary reference-link resolution, and a footnote DEFINITION
(``[^ref]: ...``) is registered in the footnote namespace, not the
plain link-reference namespace. So ``[label][^slug]`` is never resolved
as a reference-style link against some accidental same-named
definition (none exists in the footnote namespace); the footnote
extension picks up ``[^slug]`` on its own inline scan regardless of
what precedes it. The visible result is the bracketed label followed
by a footnote marker/superscript — a legitimate, if slightly more
literal-looking, rendering than a bare unbracketed label, traded
deliberately for exact reversibility.

Design
------

Forward (:func:`convert_links_to_footnotes`):

1. Split off a trailing ``## Footnotes`` section this converter itself
   wrote on a prior run (every non-blank line matches this converter's
   own definition-line shape — a section that doesn't is left alone by
   REFUSING outright, via :class:`FootnoteSectionConflict`, rather than
   risking corrupting unrelated hand-written footnotes).
2. Parse that section's ``slug -> tumbler`` map — this is what keeps
   markers STABLE across runs: a tumbler already carrying a marker
   keeps the same slug forever, even as its catalog data drifts.
3. Scan the remaining body (fenced/inline-code/indented-code aware, via
   :func:`~nexus.doc.catalog_links.scan_catalog_links`) for raw
   ``nx://catalog/`` links and derive a fresh, deterministic,
   collision-handled slug for any tumbler not already mapped (skipped
   entirely under ``refresh_only`` — see below).
4. Resolve every referenced tumbler (old markers + new links) in ONE
   batch via :func:`~nexus.doc.catalog_links.resolve_catalog_links`.
5. Rewrite the body: a raw link whose tumbler resolves becomes
   ``[label][^slug]``; one that does NOT resolve is left as a link,
   verbatim, and reported (never silently dropped). The CALLER (the
   CLI) decides what "reported" means for the actual file write — this
   function always returns the full picture (what changed, what's
   still dangling), never partially withholds either.
6. Rebuild the footnote section from the resolved data — this is the
   ONLY place that changes on catalog drift; markers in the body are
   never touched once assigned. An existing marker whose tumbler has
   itself gone dangling since the last run gets an "unresolved"
   footnote body (marker text is left alone; there is nothing safe to
   rewrite it to).

``refresh_only=True`` (GH #896 ask 1's ``--refresh``): steps 3-4 above
consider ONLY tumblers that already carry a marker from a prior run —
a brand-new raw link is left completely untouched (not converted, not
resolved, not reported as dangling: it was never attempted). Only
existing footnote BODIES refresh against current catalog state.

``style`` (``"long"`` default or ``"short"``, GH #896 ask 1's
``--style``): a footnote body's level of detail. ``"short"`` is title +
tumbler id only; ``"long"`` adds content type, indexed date, a link (or
the safe-repo-relative label), a merge note, and outbound links.

Line-ending / trailing-newline handling (code-review round): the file's
own CRLF-vs-LF convention is detected once and restored on output — all
internal logic works in LF-normalized space, converted back to CRLF at
the very end when the original used it (a file mixing both conventions
is normalized to whichever this detects, a documented simplification,
not silent data loss). A file with NO trailing newline keeps having
none when nothing needed to change (the idempotent / all-dangling
paths); appending a NEW ``## Footnotes`` section always ends with
exactly one trailing newline, since that section is new content being
added, not a preserved absence.

Reverse (:func:`convert_footnotes_to_links`) needs no catalog access at
all: the footnote body already carries the tumbler in backticks, so a
marker is expanded back to ``[label](nx://catalog/<tumbler>)`` purely
from the file's own footnote section, via the SAME bracketed-label
regex the forward direction writes — no boundary-guessing, exact
regardless of what precedes/follows a citation on its line.
"""
from __future__ import annotations

import re
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from nexus.doc._common import iter_plain_lines
from nexus.doc.catalog_links import (
    CatalogLinkResolutionError,
    resolve_catalog_links,
    scan_catalog_links,
)

__all__ = [
    "ConversionResult",
    "DanglingRef",
    "FootnoteSectionConflict",
    "FOOTNOTES_HEADING",
    "convert_links_to_footnotes",
    "convert_footnotes_to_links",
]

FOOTNOTES_HEADING = "## Footnotes"

_SLUG_PREFIX = "tumbler-"
_SLUG_BODY_RE = r"[a-z0-9]+(?:-[a-z0-9]+)*"
_MARKER_RE = re.compile(r"\[\^(" + _SLUG_PREFIX + _SLUG_BODY_RE + r")\]")
_DEF_RE = re.compile(
    r"^\[\^(" + _SLUG_PREFIX + _SLUG_BODY_RE + r")\]:\s*nx catalog tumbler "
    r"`(\d+(?:\.\d+)+)`"
)
#: Forward/reverse marker shape: a bracketed label immediately followed
#: by the footnote reference — see the module docstring for why the
#: label stays bracketed (exact reversal regardless of surrounding text).
_REVERSE_RE = re.compile(
    r"\[(?P<label>[^\]\n]*)\]\[\^(?P<slug>" + _SLUG_PREFIX + _SLUG_BODY_RE + r")\]"
)
_SLUG_STRIP_RE = re.compile(r"[^a-z0-9]+")

#: Cap on the derived slug's base so a long title doesn't produce an
#: unwieldy marker; collision suffixing (``-2``, ``-3``, ...) still
#: applies on top of the truncated form.
_SLUG_MAX_LEN = 40


class FootnoteSectionConflict(Exception):
    """Raised when the file already carries a ``## Footnotes`` heading
    whose body does not exclusively match this converter's own
    definition-line format. Editing it could destroy footnotes this
    tool never wrote, so the converter refuses rather than guess."""


@dataclass(slots=True)
class DanglingRef:
    """One tumbler (forward mode) or slug (reverse mode, when a marker
    names a slug with no matching definition) that could not be
    resolved — reported, never silently dropped."""

    lineno: int
    tumbler: str


@dataclass(slots=True)
class ConversionResult:
    text: str
    changed: bool
    dangling: list[DanglingRef] = field(default_factory=list)


def _slugify(text: str) -> str:
    s = _SLUG_STRIP_RE.sub("-", text.strip().lower()).strip("-")
    s = s[:_SLUG_MAX_LEN].rstrip("-")
    return s or "x"


def _derive_slug(used: set[str], tumbler: str, title: str) -> str:
    base = _slugify(title) if title else tumbler.replace(".", "-")
    candidate = f"{_SLUG_PREFIX}{base}"
    if candidate not in used:
        return candidate
    i = 2
    while f"{candidate}-{i}" in used:
        i += 1
    return f"{candidate}-{i}"


def _split_footnotes_section(text: str) -> tuple[str, list[tuple[str, str]]]:
    """Return ``(body, existing_defs)`` — *existing_defs* is a list of
    ``(slug, tumbler)`` pairs parsed from a trailing ``## Footnotes``
    section this converter itself wrote. No such heading -> the WHOLE
    *text* is the body and *existing_defs* is empty.

    Raises :class:`FootnoteSectionConflict` if the heading is present
    but any non-blank line under it does not match this converter's own
    definition format — a foreign/mixed section is out of scope, and
    touching it risks destroying footnotes this tool never wrote.
    """
    lines = text.split("\n")
    for i, line in enumerate(lines):
        if line.strip() != FOOTNOTES_HEADING:
            continue
        rest = lines[i + 1:]
        non_blank = [ln for ln in rest if ln.strip()]
        # NOT rstripped: the body's own trailing-newline count (typically
        # one real newline plus the blank separator line right before the
        # heading) must round-trip exactly for convert_footnotes_to_links,
        # which returns this body verbatim with no re-assembly step of
        # its own. The forward direction's OWN re-assembly (in
        # _convert_links_to_footnotes_lf) already does its own
        # rstrip("\n") when it rebuilds the "body + blank line + heading"
        # separator, so leaving this unstripped changes nothing there.
        body = "\n".join(lines[:i])
        if not non_blank:
            return body, []
        defs: list[tuple[str, str]] = []
        for ln in non_blank:
            m = _DEF_RE.match(ln)
            if not m:
                raise FootnoteSectionConflict(
                    f"'{FOOTNOTES_HEADING}' section contains a line this "
                    f"converter did not write: {ln!r}. Refusing to touch it "
                    f"— resolve the conflict by hand first."
                )
            defs.append((m.group(1), m.group(2)))
        return body, defs
    return text, []


def _scan_markers(body: str) -> list[tuple[int, int, str]]:
    """``(lineno, col, slug)`` for every ``[^tumbler-<slug>]`` marker.

    Delegates fenced/inline-code/indented-code exclusion entirely to
    :func:`~nexus.doc._common.iter_plain_lines` (ONE call over the
    whole body, so its indented-code-block state tracks correctly
    across lines — a per-line call here would reset that state on
    every line and misclassify almost every 4-space-indented line as
    "start of a new block").
    """
    out: list[tuple[int, int, str]] = []
    for lineno, line in iter_plain_lines(body):
        for mm in _MARKER_RE.finditer(line):
            out.append((lineno, mm.start() + 1, mm.group(1)))
    return out


def _rewrite_body(
    body: str,
    tumbler_to_slug: dict[str, str],
    entries: dict[str, Any],
    dangling_out: list[DanglingRef],
    *,
    eligible_tumblers: set[str] | None = None,
) -> str:
    """Replace every RAW ``nx://catalog/`` link occurrence with
    ``[label][^slug]`` when its tumbler resolves AND has an assigned
    slug; leave it exactly as-is otherwise. Fenced/inline-code/
    indented-code content is never touched (via ``scan_catalog_links``,
    called ONCE over the whole *body* so its scanner state — indented-
    code-block tracking especially — is correct across lines).

    *eligible_tumblers*, when not ``None`` (``--refresh`` mode): a raw
    link whose tumbler is OUTSIDE this set is left untouched AND NOT
    reported as dangling — it was never attempted, not resolved-and-
    failed.
    """
    links_by_line: dict[int, list] = {}
    for link in scan_catalog_links(body):
        links_by_line.setdefault(link.lineno, []).append(link)

    out_lines: list[str] = []
    for lineno, raw_line in enumerate(body.split("\n"), 1):
        links = links_by_line.get(lineno)
        if not links:
            out_lines.append(raw_line)
            continue

        pieces: list[str] = []
        cursor = 0
        for link in links:
            start = link.col - 1
            original = f"[{link.display}](nx://catalog/{link.tumbler})"
            end = start + len(original)
            pieces.append(raw_line[cursor:start])

            if eligible_tumblers is not None and link.tumbler not in eligible_tumblers:
                pieces.append(original)  # --refresh: never attempted, never reported
            else:
                slug = tumbler_to_slug.get(link.tumbler)
                entry = entries.get(link.tumbler)
                if slug is not None and entry is not None:
                    pieces.append(f"[{link.display}][^{slug}]")
                else:
                    pieces.append(original)
                    dangling_out.append(DanglingRef(lineno=lineno, tumbler=link.tumbler))
            cursor = end
        pieces.append(raw_line[cursor:])
        out_lines.append("".join(pieces))
    return "\n".join(out_lines)


def _format_def(
    slug: str,
    tumbler: str,
    entry: Any,
    merged_into: str | None,
    links_from: list,
    *,
    base_dir: Path | None = None,
    repo_root: str = "",
    style: str = "long",
) -> str:
    if entry is None:
        return (
            f"[^{slug}]: nx catalog tumbler `{tumbler}` — unresolved "
            f"(no longer in the catalog)."
        )
    title = entry.title or tumbler
    if style == "short":
        return f'[^{slug}]: nx catalog tumbler `{tumbler}`. Title: "{title}".'

    from nexus.doc.catalog_links import safe_link_target  # noqa: PLC0415 — deferred to avoid an unused import when only reverse conversion runs

    content_type = entry.content_type or "unknown"
    parts = [
        f"[^{slug}]: nx catalog tumbler `{tumbler}`.",
        f' Title: "{title}".',
        f" Content type: {content_type}.",
    ]
    indexed_at = getattr(entry, "indexed_at", "") or ""
    if indexed_at:
        parts.append(f" Indexed {indexed_at}.")
    if merged_into:
        parts.append(f" Merged into `nx://catalog/{merged_into}`.")
    target = safe_link_target(entry, base_dir=base_dir, repo_root=repo_root or None)
    if target is not None:
        if target.is_link:
            parts.append(f" [{target.text}]({target.text}).")
        else:
            parts.append(f" (repo-relative) `{target.text}`.")
    if links_from:
        rendered = ", ".join(
            f"`{lnk.link_type} -> {lnk.to_tumbler}`" for lnk in links_from
        )
        parts.append(f" Outbound links: {rendered}.")
    return "".join(parts)


def _convert_links_to_footnotes_lf(
    text: str,
    get_reader,
    *,
    base_dir: Path | None,
    refresh_only: bool,
    style: str,
) -> ConversionResult:
    """:func:`convert_links_to_footnotes`'s implementation, operating
    entirely in LF-normalized space — see that function for the CRLF
    wrapper around this."""
    body, existing_defs = _split_footnotes_section(text)
    tumbler_to_slug: dict[str, str] = {t: s for s, t in existing_defs}
    used_slugs: set[str] = {s for s, _t in existing_defs}
    slug_to_tumbler_existing: dict[str, str] = dict(existing_defs)
    already_known_tumblers: set[str] = set(tumbler_to_slug)

    raw_links = scan_catalog_links(body)
    markers = _scan_markers(body)

    marker_tumblers_present = {
        slug_to_tumbler_existing[slug]
        for (_ln, _col, slug) in markers
        if slug in slug_to_tumbler_existing
    }
    raw_tumblers_seen: set[str] = set()
    raw_tumblers_in_order: list[str] = []
    for link in raw_links:
        if link.tumbler in tumbler_to_slug or link.tumbler in raw_tumblers_seen:
            continue
        raw_tumblers_seen.add(link.tumbler)
        raw_tumblers_in_order.append(link.tumbler)

    if refresh_only:
        all_tumblers = sorted(marker_tumblers_present)
    else:
        all_tumblers = sorted(marker_tumblers_present | {lnk.tumbler for lnk in raw_links})
    if not all_tumblers:
        return ConversionResult(text=text, changed=False, dangling=[])

    try:
        reader = get_reader()
    except CatalogLinkResolutionError:
        raise
    except Exception as exc:  # noqa: BLE001 — normalized into a typed error below
        raise CatalogLinkResolutionError(f"cannot open catalog reader: {exc}") from exc
    entries, _owner_names, merged_into = resolve_catalog_links(all_tumblers, reader)
    from nexus.doc.catalog_links import owner_repo_roots_for  # noqa: PLC0415 — deferred: only needed on the resolved-links path

    owner_repo_roots = owner_repo_roots_for(entries, reader)

    if not refresh_only:
        for tumbler in raw_tumblers_in_order:
            entry = entries.get(tumbler)
            if entry is None:
                continue  # dangling new link — no slug, stays a link (see _rewrite_body)
            slug = _derive_slug(used_slugs, tumbler, entry.title or "")
            used_slugs.add(slug)
            tumbler_to_slug[tumbler] = slug

    dangling: list[DanglingRef] = []
    new_body = _rewrite_body(
        body, tumbler_to_slug, entries, dangling,
        eligible_tumblers=already_known_tumblers if refresh_only else None,
    )

    # Final marker set actually present in the rewritten body, in
    # first-appearance order — this is what the footnote section
    # reflects; a marker whose text nobody touched (pre-existing) and a
    # freshly-created one are treated identically here.
    final_markers = _scan_markers(new_body)
    ordered_slugs: list[str] = []
    seen_slugs: set[str] = set()
    slug_first_line: dict[str, int] = {}
    for (lineno, _col, slug) in final_markers:
        if slug not in seen_slugs:
            seen_slugs.add(slug)
            ordered_slugs.append(slug)
            slug_first_line[slug] = lineno

    if not ordered_slugs:
        # Nothing structurally changed (no footnote section is or was
        # needed) — return new_body AS-IS: it reconstructs `body`
        # exactly (split/join is an identity when nothing substituted),
        # which preserves the file's trailing-newline state precisely.
        dangling.sort(key=lambda d: d.lineno)
        return ConversionResult(text=new_body, changed=new_body != text, dangling=dangling)

    slug_to_tumbler = {s: t for t, s in tumbler_to_slug.items()}
    defs_lines: list[str] = []
    for slug in ordered_slugs:
        tumbler = slug_to_tumbler.get(slug)
        if tumbler is None:
            # A marker whose def we can't recover (hand-typed/corrupted,
            # not something this run assigned) — nothing to rebuild it
            # from; leave it out of the section.
            continue
        entry = entries.get(tumbler)
        links_from: list = []
        if entry is not None:
            try:
                links_from = reader.links_from(entry.tumbler)
            except Exception:  # noqa: BLE001 — best-effort, cosmetic
                links_from = []
        else:
            dangling.append(DanglingRef(lineno=slug_first_line[slug], tumbler=tumbler))
        owner_prefix = str(entry.tumbler.owner_address()) if entry is not None else ""
        defs_lines.append(_format_def(
            slug, tumbler, entry, merged_into.get(tumbler), links_from,
            base_dir=base_dir, repo_root=owner_repo_roots.get(owner_prefix, ""),
            style=style,
        ))

    dangling.sort(key=lambda d: d.lineno)
    # Exactly ONE "\n" is added between the body and the heading, never
    # an unconditional rstrip-then-force-two -- that would destroy the
    # one piece of information that makes _split_footnotes_section's
    # later reconstruction exact: whether the body's OWN last line
    # already ended in a newline. If it did (the common case), this ONE
    # added "\n" reads as the conventional blank line before a heading;
    # if it did not (the file had no trailing newline at all), this ONE
    # "\n" is the only separator -- the heading follows directly, no
    # blank line -- and reversing (stripping the section back off)
    # reconstructs the body with NO trailing newline, exactly as it was
    # (code-review round: "no trailing newline, preserve its absence").
    final_text = (
        new_body + "\n" + FOOTNOTES_HEADING + "\n\n"
        + "\n\n".join(defs_lines) + "\n"
    )
    return ConversionResult(text=final_text, changed=final_text != text, dangling=dangling)


def convert_links_to_footnotes(
    text: str,
    get_reader,
    *,
    base_dir: Path | None = None,
    refresh_only: bool = False,
    style: str = "long",
) -> ConversionResult:
    """Forward conversion: ``[label](nx://catalog/T)`` ->
    ``[label][^slug]`` plus a trailing ``## Footnotes`` section,
    idempotent across runs.

    *get_reader* is called at most once, and ONLY when *text* actually
    references at least one tumbler (a raw link or a marker with a
    recoverable definition) — mirrors
    :func:`~nexus.doc.catalog_links.scan_and_resolve_catalog_links`'s own
    "no links, no catalog call" contract.

    *base_dir* (typically the file's own directory — footnotes are
    appended IN PLACE, so the citing and cited-from location are the
    same file) is passed to
    :func:`~nexus.doc.catalog_links.safe_link_target` via
    :func:`~nexus.doc.catalog_links.owner_repo_roots_for` so a safe,
    repo-relative ``file_path`` becomes a WORKING relative link rather
    than an unresolvable label; omit it to get the plain-text
    ``(repo-relative)`` fallback unconditionally.

    *refresh_only* (GH #896 ``--refresh``): only refresh EXISTING
    footnote bodies against current catalog state; a brand-new raw
    link is left completely untouched (see the module docstring).

    *style* (``"long"``/``"short"``, GH #896 ``--style``): footnote
    body verbosity — see the module docstring.

    Raises :class:`~nexus.doc.catalog_links.CatalogLinkResolutionError`
    on a degraded catalog service (never silently downgraded to "every
    citation is dangling") and :class:`FootnoteSectionConflict` on a
    foreign ``## Footnotes`` section.

    CRLF files (detected once, over the whole input) get CRLF output;
    a file with no trailing newline keeps having none unless a
    ``## Footnotes`` section is newly appended (see the module
    docstring's "Line-ending" section).
    """
    uses_crlf = "\r\n" in text
    normalized = text.replace("\r\n", "\n") if uses_crlf else text
    result = _convert_links_to_footnotes_lf(
        normalized, get_reader, base_dir=base_dir, refresh_only=refresh_only, style=style,
    )
    if not uses_crlf:
        return result
    final_text = result.text.replace("\n", "\r\n")
    return ConversionResult(text=final_text, changed=final_text != text, dangling=result.dangling)


def _convert_footnotes_to_links_lf(text: str) -> ConversionResult:
    """:func:`convert_footnotes_to_links`'s implementation, operating
    entirely in LF-normalized space."""
    body, existing_defs = _split_footnotes_section(text)
    if not existing_defs:
        return ConversionResult(text=text, changed=False, dangling=[])
    slug_to_tumbler = dict(existing_defs)

    # One whole-body pass for fence/inline-code/indented-code state
    # (see _scan_markers's docstring for why NOT per-line); a line
    # absent from this map is fenced/indented-code and copied verbatim.
    plain = dict(iter_plain_lines(body))

    dangling: list[DanglingRef] = []
    out_lines: list[str] = []
    for lineno, raw_line in enumerate(body.split("\n"), 1):
        masked_line = plain.get(lineno)
        if masked_line is None:
            out_lines.append(raw_line)
            continue

        pieces: list[str] = []
        cursor = 0
        for mm in _REVERSE_RE.finditer(raw_line):
            start, end = mm.span()
            pieces.append(raw_line[cursor:start])
            if masked_line[start:end] != raw_line[start:end]:
                # Inside an inline code span (e.g. a docs example
                # quoting the marker syntax) -- never touched.
                pieces.append(raw_line[start:end])
            else:
                slug = mm.group("slug")
                label = mm.group("label")
                tumbler = slug_to_tumbler.get(slug)
                if tumbler is None:
                    pieces.append(mm.group(0))
                    dangling.append(DanglingRef(lineno=lineno, tumbler=slug))
                else:
                    pieces.append(f"[{label}](nx://catalog/{tumbler})")
            cursor = end
        pieces.append(raw_line[cursor:])
        out_lines.append("".join(pieces))

    new_body = "\n".join(out_lines)
    dangling.sort(key=lambda d: d.lineno)
    return ConversionResult(text=new_body, changed=new_body != text, dangling=dangling)


def convert_footnotes_to_links(text: str) -> ConversionResult:
    """Reverse conversion: ``[label][^slug]`` -> ``[label](nx://catalog/T)``,
    dropping the ``## Footnotes`` section entirely (every definition it
    held is, by construction, consumed back into a link). No catalog
    access — the tumbler is recovered from the footnote body itself.

    Exact for every citation, regardless of what precedes or follows it
    on its line, or how many citations share a line — see the module
    docstring's "Marker form and why" section. CRLF/trailing-newline
    handling matches :func:`convert_links_to_footnotes`.
    """
    uses_crlf = "\r\n" in text
    normalized = text.replace("\r\n", "\n") if uses_crlf else text
    result = _convert_footnotes_to_links_lf(normalized)
    if not uses_crlf:
        return result
    final_text = result.text.replace("\n", "\r\n")
    return ConversionResult(text=final_text, changed=final_text != text, dangling=result.dangling)
