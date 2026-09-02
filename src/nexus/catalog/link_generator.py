# SPDX-License-Identifier: AGPL-3.0-or-later
# Copyright (c) 2026 Hal Hildebrand. All rights reserved.

"""Auto-generate typed links in the catalog from metadata cross-matching."""

from __future__ import annotations

import re
from collections import defaultdict
from collections.abc import Callable
from dataclasses import dataclass
from functools import partial
from pathlib import Path

import structlog

from nexus.catalog.types import CatalogEntry
from nexus.catalog.catalog_protocol import CatalogReader, CatalogWriter
from nexus.catalog.rdr_canonical import (
    RDR_CONTENT_TYPES,
    current_rdr_owner,
    group_rdr_candidates,
    is_in_repo,
    rdr_key_of,
    rdr_source_prefix,
    resolve_all,
)
from nexus.catalog.tumbler import Tumbler
from nexus.md_chunker import parse_frontmatter

_log = structlog.get_logger()


def _all_entries(cat: CatalogReader) -> list[CatalogEntry]:
    """Fetch all catalog entries via the public API.

    Only ``generate_citation_links`` still uses this. Bib IDs/references
    can in principle live on any content type the enrich pipeline touches
    (today: "paper", historically also seen on "pdf" — see nexus-57mk),
    and this generator runs off the hot indexing path (only after
    ``nx enrich`` or the ``nx catalog links`` CLI verb), so narrowing its
    fetch by content_type risks silently dropping a citation edge for a
    marginal win. Left as a full scan deliberately.
    """
    return cat.all_documents()


def _entries_of_type(cat: CatalogReader, content_type: str) -> list[CatalogEntry]:
    """Fetch entries of exactly one content type via a single round trip.

    ``CatalogReader.all_documents(content_type=...)`` is server-filtered
    (``HttpCatalogClient`` sends one unbounded ``/list?content_type=``
    request — see its docstring) rather than the unfiltered branch's
    page=1000 pagination loop over the WHOLE tenant catalog. The
    filepath/pdf linkers below only ever match a small, fixed set of
    content types (their own source type(s), plus "code" as the link
    target); fetching everything just to filter it client-side pays for
    documents that can never match.
    """
    return cat.all_documents(content_type=content_type)


def _no_qualifying_seed(
    new_tumblers: list[Tumbler] | None,
    new_content_types: frozenset[str] | set[str] | None,
    source_types: frozenset[str],
) -> bool:
    """True when an incremental generator can skip its fetches entirely.

    A generator only ever produces a link FROM a document whose own
    tumbler is in *new_tumblers* AND whose content_type is one of
    *source_types* (the existing ``new_set`` filter each generator
    applies AFTER fetching proves this: an entry fetched by
    ``_entries_of_type(cat, X)`` for ``X in source_types`` that isn't
    also in ``new_set`` never reaches the matching loop). So when the
    caller can name every content_type among *new_tumblers* up front
    (*new_content_types*) and none of them intersect *source_types*,
    every one of this generator's source-type fetches is guaranteed to
    filter down to an empty list — running them is provably equivalent
    to skipping them and returning 0.

    Opt-in and strictly additive: *new_content_types* is ``None`` for
    every call site that doesn't pass it (e.g. the CLI full-scan path,
    and any test exercising the pre-existing contract), in which case
    this returns ``False`` and behavior is byte-for-byte the same as
    before this helper existed. The existing ``len(new_tumblers) == 0``
    short-circuit each generator already has is a special case of this
    (empty new_tumblers trivially has no qualifying content type) but is
    left in place independently since it needs no *new_content_types*.
    """
    if new_tumblers is None or not new_tumblers:
        return False
    if new_content_types is None:
        return False
    return source_types.isdisjoint(new_content_types)


# Source content_type(s) each incremental generator requires present among
# new_tumblers before its fetches can possibly yield a link. See each
# generator's docstring for the source-vs-target reasoning.
_RDR_FILEPATH_SOURCE_TYPES = frozenset({"rdr"})
# "docs" is kept even though no current registration path feeds it through
# indexer.py's _catalog_hook (repo/doc indexing registers "prose", never
# "docs", as a content_type — "docs" only ever appears as a *collection*
# name prefix there). It IS a live content_type value elsewhere: the
# DEVONthink orphan-backfill path (catalog/orphan_backfill.py
# ``_content_type_for_collection``) infers "docs" from a ``docs__``-prefixed
# collection and can register documents with content_type=="docs" through a
# different registration flow. That flow does not currently wire into this
# generator's new_tumblers, but the OLD (pre-T0.2) unfiltered predicate did
# include "docs", and dropping it would silently narrow correctness the
# moment any caller starts passing docs-typed new_tumblers through here.
# Kept for that reason; the seed check above means it costs nothing when
# absent, which is the common case today.
_PROSE_FILEPATH_SOURCE_TYPES = frozenset({"prose", "markdown", "docs"})
_PDF_CORPUS_SOURCE_TYPES = frozenset({"pdf", "paper"})

_INCREMENTAL_SOURCE_TYPES: dict[str, frozenset[str]] = {
    "rdr": _RDR_FILEPATH_SOURCE_TYPES,
    "prose": _PROSE_FILEPATH_SOURCE_TYPES,
    "pdf": _PDF_CORPUS_SOURCE_TYPES,
    # RDR-201 P3.2 (nexus-j9z30.21): generate_rdr_dependency_links's own
    # source-type set (RDR_CONTENT_TYPES == {"rdr", "prose"}) is WIDER
    # than the filepath linker's "rdr"-only set above, so it gets its own
    # kind rather than reusing "rdr" -- reusing "rdr" would under-announce
    # (skip the phase message) for a batch whose only new entries are
    # legacy "prose"-registered RDRs, which is exactly the Finding-4
    # fragmentation shape this generator exists to catch.
    "rdr-dependency": RDR_CONTENT_TYPES,
}


def incremental_generator_applies(
    kind: str,
    new_tumblers: list[Tumbler] | None,
    new_content_types: frozenset[str] | set[str] | None,
) -> bool:
    """True when the *kind* incremental generator (``"rdr"`` / ``"prose"``
    / ``"pdf"``) can do work for this batch — the inverse of the seed
    predicate each generator applies before fetching (nexus-jg3x5).

    The indexer's catalog hook uses it to decide whether a generator gets
    its own ``[post]`` sub-phase pair: a generator with no qualifying new
    tumbler returns 0 without fetching, and announcing a phase that never
    ran is the honesty defect this exists to avoid. The contract mirrors
    the seed predicate exactly: ``new_tumblers is None`` is the full-scan
    shape, where every generator runs unconditionally, so it applies;
    an EMPTY list has nothing to link, so it does not; unknown content
    types (``None``) cannot prove a generator idle, so it applies.
    """
    if new_tumblers is None:
        return True
    if not new_tumblers:
        return False
    return not _no_qualifying_seed(
        new_tumblers, new_content_types, _INCREMENTAL_SOURCE_TYPES[kind],
    )


def generate_citation_links(cat: CatalogReader, *, writer: CatalogWriter | None = None) -> int:
    """Auto-create 'cites' links via bib ID cross-matching.

    Uses metadata already on catalog entries — no API calls.
    created_by='bib_enricher' per RF-8.

    nexus-57mk: indexes both ``bib_semantic_scholar_id`` (Semantic
    Scholar paper IDs) and ``bib_openalex_id`` (OpenAlex W-ids) so a
    catalog enriched by either backend produces cite links. The
    ``references`` list on each entry contains IDs from whichever
    backend enriched that entry; matching is exact-string against the
    same ID space, so cross-backend references (a paper enriched by
    OpenAlex referencing one enriched only by S2) won't match — that's
    the correct conservative behavior, since the two ID spaces are
    distinct and we don't have a DOI bridge yet.
    """
    # RDR-146 P1.2: reads (_all_entries -> cat.all_documents) via cat,
    # writes (link_if_absent) via writer (defaults to cat for callers that
    # pass a single full Catalog).
    w = writer if writer is not None else cat
    entries = _all_entries(cat)

    # Build index: bib ID -> tumbler. Both backends' IDs share one map
    # because their ID spaces don't collide (S2 paperIds are 40-hex
    # SHA-shaped strings; OpenAlex IDs start with 'W' followed by
    # digits). A collision would only happen if a future backend
    # introduced overlapping namespacing.
    id_to_tumbler: dict[str, Tumbler] = {}
    entries_with_refs: list[tuple[Tumbler, list[str]]] = []

    for entry in entries:
        meta = entry.meta or {}
        for id_field in ("bib_semantic_scholar_id", "bib_openalex_id"):
            bib_id = meta.get(id_field, "")
            if bib_id:
                id_to_tumbler[bib_id] = entry.tumbler
        refs = meta.get("references", [])
        if refs:
            entries_with_refs.append((entry.tumbler, refs))

    count = 0
    for from_tumbler, ref_ids in entries_with_refs:
        for ref_id in ref_ids:
            to_tumbler = id_to_tumbler.get(ref_id)
            if to_tumbler and to_tumbler != from_tumbler:
                if w.link_if_absent(from_tumbler, to_tumbler, "cites", created_by="bib_enricher"):
                    count += 1
                    _log.debug("citation_link_created", from_t=str(from_tumbler), to_t=str(to_tumbler))

    return count


_FILE_PATH_RE = re.compile(
    r"(?:src|tests|lib|pkg|cmd|internal|app)/"  # must start with a source root
    r"[\w/.-]+"                                  # path chars
    r"\.(?:py|java|go|rs|ts|tsx|js|jsx|c|cpp|h|rb|php|swift|kt|scala)"  # source extension
)


# nexus-sob9: prose-side regex. RDR file_path matching anchors on a
# source-root prefix (``src/`` etc) because RDR text is dense with
# fully-qualified paths; the anchor disambiguates against common
# prose like "the algorithm runs in O(n log n)". Prose docs use a
# wider path vocabulary (``docs/`` runbooks, ``conexus/`` plugin trees,
# ``.claude/`` profiles) that the RDR anchor list misses entirely.
# The prose regex requires AT LEAST ONE ``/`` (so a bare
# ``foo.py`` mention doesn't match) plus a recognised source
# extension. The match is then checked against catalog code
# entries by exact ``file_path`` so non-existent-in-catalog
# strings fall through silently.
_PROSE_PATH_RE = re.compile(
    r"(?:[\w.-]+/)+"                              # at least one dir segment
    r"[\w.-]+"                                    # filename
    r"\.(?:py|java|go|rs|ts|tsx|js|jsx|c|cpp|h|rb|php|swift|kt|scala|md)"
)


def generate_rdr_filepath_links(
    cat: CatalogReader,
    *,
    writer: CatalogWriter | None = None,
    new_tumblers: list[Tumbler] | None = None,
    new_content_types: frozenset[str] | set[str] | None = None,
) -> int:
    """Extract file paths from RDR content and link to matching code entries.

    Scans each RDR's file on disk for source file paths (e.g.,
    ``src/nexus/catalog/catalog.py``). Matches against catalog code entries
    by file_path. Creates ``implements`` links (RDR → code).
    created_by='filepath_extractor'.

    SOURCE content_type (the side that must be new for a link to be
    possible) is "rdr"; TARGET is "code" — an RDR can only newly link to
    code that already exists, but a run whose new_tumblers are all "code"
    produces no new RDR->code links no matter how many RDRs exist.

    When *new_tumblers* is provided, only those entries are scanned (incremental
    mode). Pass ``None`` (default) for the full-scan behavior.

    *new_content_types*, when supplied alongside *new_tumblers*, is the set
    of content_type values present among *new_tumblers*; it lets this
    generator skip its catalog fetches entirely when none of the new
    tumblers are "rdr" (see :func:`_no_qualifying_seed`). Optional and
    additive — omitting it preserves the exact pre-existing behavior.
    """
    if new_tumblers is not None and len(new_tumblers) == 0:
        return 0
    if _no_qualifying_seed(new_tumblers, new_content_types, _RDR_FILEPATH_SOURCE_TYPES):
        _log.debug(
            "rdr_filepath_links_skipped_no_seed",
            new_content_types=sorted(new_content_types) if new_content_types else [],
        )
        return 0

    rdr_entries = [e for e in _entries_of_type(cat, "rdr") if e.file_path]
    code_entries = [e for e in _entries_of_type(cat, "code") if e.file_path]

    if new_tumblers is not None:
        new_set = {str(t) for t in new_tumblers}
        rdr_entries = [e for e in rdr_entries if str(e.tumbler) in new_set]

    # Index: file_path → tumbler (code entries)
    path_to_code: dict[str, Tumbler] = {}
    for code in code_entries:
        path_to_code[code.file_path] = code.tumbler

    count = 0
    for rdr in rdr_entries:
        resolved = cat.resolve_path(rdr.tumbler)
        if resolved is None or not resolved.is_file():
            continue
        try:
            text = resolved.read_text(errors="replace")
        except OSError:
            continue

        # Find all file paths in the RDR text
        seen_targets: set[str] = set()
        for match in _FILE_PATH_RE.finditer(text):
            fpath = match.group(0)
            if fpath in seen_targets:
                continue
            seen_targets.add(fpath)
            code_tumbler = path_to_code.get(fpath)
            if code_tumbler is None:
                continue
            try:
                created = (writer if writer is not None else cat).link_if_absent(
                    rdr.tumbler, code_tumbler, "implements",
                    created_by="filepath_extractor",
                )
            except ValueError:
                continue
            if created:
                count += 1
                _log.debug(
                    "rdr_filepath_link_created",
                    rdr=str(rdr.tumbler), code=str(code_tumbler),
                    path=fpath,
                )

    return count


# ── RDR-to-RDR dependency edges (RDR-201 Phase 3.2, nexus-j9z30.21) ─────────
#
# Seeds catalog links from frontmatter that already exists on disk --
# ``supersedes``, ``superseded_by``, ``parent_rdr``, ``related_rdrs``
# (Finding 4, T2 ``nexus_rdr/201-research-2``). No new frontmatter field is
# introduced; this generator only reads what authors have already been
# writing. Both endpoints resolve through ``nexus.catalog.rdr_canonical`` --
# ``is_in_repo`` is the single admission gate and ``resolve_all`` /
# ``resolve_canonical_tumbler`` are the single resolution authority for
# turning an rdr_key (basename identity) into a canonical tumbler. This
# generator does not reimplement any part of that rule; it only adds the
# ONE thing rdr_canonical deliberately does not do -- turning a numeric
# ``RDR-NNN`` text reference into the rdr_key that rule operates on.

#: SOURCE content types this generator scans for frontmatter (mirrors
#: RDR_CONTENT_TYPES -- "rdr" is the live scheme, "prose" the legacy one
#: Finding 4 found still registered for some never-reindexed files).
_RDR_DEPENDENCY_SOURCE_TYPES = RDR_CONTENT_TYPES

#: Matches an RDR cross-reference at the START of a frontmatter value.
#: Anchoring at position 0 is deliberate and load-bearing, not cosmetic:
#: it accepts "RDR-159 (partial: ...)" (real corpus shape, rdr-185) by
#: matching the identifier and ignoring the trailing prose, but REJECTS
#: "conexus:RDR-001" (real corpus shape, rdr-155's related_rdrs) because
#: that value does not start with "RDR-" -- a foreign-repo-qualified
#: reference is never mistaken for this repo's own same-numbered RDR.
_RDR_REF_RE = re.compile(r"^RDR-(\d+)\b", re.IGNORECASE)

#: Extracts the leading number from an rdr_key (rdr_canonical's basename
#: identity, e.g. "rdr-107-t3-chunk-soft-delete"). Deliberately mirrors
#: rdr_key_of's own "rdr-" + digits + "-" anchor: a dash-less filename
#: like "rdr137-test-fixture-partition-deliverable.md" is already outside
#: rdr_key_of's domain (never grouped, never a source or a target here)
#: for the identical reason -- one convention, not a second copy of it.
_RDR_KEY_NUM_RE = re.compile(r"^rdr-(\d+)-", re.IGNORECASE)

#: Matches a frontmatter ``id:`` value that self-declares "I am RDR-NNN"
#: (as opposed to "companion-note" or no id: field at all).
_ID_SELF_DECLARATION_RE = re.compile(r"^rdr-0*(\d+)$", re.IGNORECASE)

#: field -> (catalog link_type, source_is_from). ``source_is_from=True``
#: means the edge points FROM the document carrying the field TO the
#: document it names -- the natural reading of "X supersedes Y",
#: "X's parent_rdr is Y", "X related_rdrs includes Y". ``supersedes`` and
#: ``superseded_by`` both map to the catalog's existing ``supersedes``
#: link type; ``superseded_by`` gets ``source_is_from=False`` so the
#: RESULTING edge still reads successor->predecessor regardless of which
#: of the two documents made the declaration (RDR-014's own
#: ``superseded_by: RDR-015`` produces the identical edge shape as
#: RDR-015's ``supersedes: RDR-014`` -- one relationship, one direction).
#: ``parent_rdr`` and ``related_rdrs`` map to the catalog's ``relates``
#: type -- there is no better-fitting built-in (no "parent-of"/"child-of"
#: type exists), so the softer, symmetric-in-meaning ``relates`` is the
#: correct choice for both; direction is declaring-doc -> referenced-doc,
#: same convention as the forward ``supersedes`` case, for one consistent
#: rule across all four fields.
_RDR_DEPENDENCY_FIELDS: dict[str, tuple[str, bool]] = {
    "supersedes": ("supersedes", True),
    "superseded_by": ("supersedes", False),
    "parent_rdr": ("relates", True),
    "related_rdrs": ("relates", True),
}


def _extract_rdr_ref_numbers(value: object) -> list[int]:
    """Pull every same-repo ``RDR-NNN`` reference out of one frontmatter
    value. Accepts both shapes seen in the wild -- a bare scalar
    (``supersedes: RDR-014``) and a YAML list (``related_rdrs: [RDR-053,
    RDR-106]``) -- coerced to a single-item list when scalar. A value that
    is not RDR-shaped at all (a file path, a PR reference, a foreign-repo-
    qualified id) is not malformed, just not a same-repo reference, and is
    silently skipped rather than warned about -- see :data:`_RDR_REF_RE`.
    """
    items = value if isinstance(value, list) else [value]
    numbers: list[int] = []
    for item in items:
        if not isinstance(item, str):
            continue
        m = _RDR_REF_RE.match(item.strip())
        if m:
            numbers.append(int(m.group(1)))
    return numbers


def _rdr_key_number(rdr_key: str) -> int | None:
    m = _RDR_KEY_NUM_RE.match(rdr_key)
    return int(m.group(1)) if m else None


def _reads_frontmatter(cat: CatalogReader, tumbler: Tumbler) -> dict:
    """Resolve *tumbler* to its on-disk file and parse its frontmatter.

    Returns ``{}`` on any failure (missing path, unreadable file, broken
    YAML) -- this generator treats an unreadable RDR the same way
    ``generate_rdr_filepath_links`` does (skip, never crash the whole
    run over one bad file).
    """
    resolved = cat.resolve_path(tumbler)
    if resolved is None or not resolved.is_file():
        return {}
    try:
        text = resolved.read_text(errors="replace")
    except OSError:
        return {}
    fm, _body = parse_frontmatter(text, source=str(resolved))
    return fm


def _group_self_declares_id(
    cat: CatalogReader, candidates: list[CatalogEntry], number: int,
) -> bool:
    """True when ANY candidate in *candidates* has its own frontmatter
    ``id:`` field declaring it IS ``RDR-<number>`` (as opposed to a
    companion note's ``id: companion-note`` or no ``id:`` at all). All
    candidates in one rdr_key group are registrations of the SAME on-disk
    file (Finding-4 fragmentation), so checking the first one that
    actually resolves is sufficient -- their frontmatter is identical.
    """
    for c in candidates:
        fm = _reads_frontmatter(cat, c.tumbler)
        if not fm:
            continue
        id_val = fm.get("id")
        if not isinstance(id_val, str):
            return False
        m = _ID_SELF_DECLARATION_RE.match(id_val.strip())
        return bool(m) and int(m.group(1)) == number
    return False


def _numeric_id_index(
    cat: CatalogReader, in_repo_groups: dict[str, list[CatalogEntry]],
) -> dict[int, str]:
    """Map each RDR number to the ONE rdr_key that is that number's own
    canonical document.

    Multiple rdr_key groups can share the same leading number -- a
    companion note or phase artifact living beside the RDR it documents
    (rdr_canonical's own module docstring: RDR-152 has three ``rdr-152-
    *.md`` files under this repo alone). This disambiguates via each
    group's own frontmatter ``id: RDR-<num>`` self-declaration -- the
    file that says it IS RDR-<num>, not merely adjacent to it. A number
    with only one candidate group needs no disambiguation. A number whose
    groups collide and where zero or more than one group self-declares is
    left OUT of the map entirely: never a guess. *in_repo_groups* must
    already be scoped to this repo (:func:`nexus.catalog.rdr_canonical.
    is_in_repo`) -- passing an unscoped fetch risks a same-numbered
    foreign-repo document supplying a spurious second (or a spurious
    matching) ``id:`` declaration.
    """
    by_number: dict[int, list[str]] = defaultdict(list)
    for rdr_key in in_repo_groups:
        n = _rdr_key_number(rdr_key)
        if n is not None:
            by_number[n].append(rdr_key)

    result: dict[int, str] = {}
    for number, keys in by_number.items():
        if len(keys) == 1:
            result[number] = keys[0]
            continue
        declared = [
            k for k in keys if _group_self_declares_id(cat, in_repo_groups[k], number)
        ]
        if len(declared) == 1:
            result[number] = declared[0]
        # else: genuine collision, left unresolved (logged at the call
        # site, which has the source context this function does not).
    return result


#: Above this many new tumblers the generator lists instead of probing.
#: The listing is len(RDR_CONTENT_TYPES) == 2 round trips carrying ~530 rows
#: on this repo; each probe is one single-row round trip, serialized under
#: the service catalog lock. The crossover is NOT measured (this box is a
#: production install and cannot be timed from a dev session): 4 keeps the
#: one-to-few-file `nx index md` case -- the reported regression -- at
#: fewer round trips than the listing, and anything larger takes the
#: listing as before. Measure before moving it.
_RDR_SEED_LOOKUP_CAP = 4


def _any_new_tumbler_is_rdr_keyed(cat: CatalogReader, new_tumblers: list[Tumbler]) -> bool:
    """Incremental-mode seed check, one ``resolve`` per NEW tumbler.

    The content-type check (:func:`_no_qualifying_seed`) admits this
    generator for any batch carrying ``"prose"`` -- but ``prose`` is the
    repo-wide default for ALL general markdown, so it admits essentially
    every ``nx index`` run that touches a ``.md`` file and the full
    RDR listing (~530 rows on this repo) fires on each (RDR-201 P3.2
    round-2 critique, T2 [24077]). Only a document :func:`rdr_key_of`
    recognises -- a ``rdr-*.md`` file directly under a ``rdr/`` directory
    -- can be a SOURCE of a dependency edge, so a small batch with no such
    tumbler can skip the listing exactly. Batches above
    :data:`_RDR_SEED_LOOKUP_CAP` fall through (``True``) rather than trade
    one listing for more lookups than it holds.
    """
    if len(new_tumblers) > _RDR_SEED_LOOKUP_CAP:
        return True
    for tumbler in new_tumblers:
        entry = cat.resolve(tumbler)
        if entry is not None and rdr_key_of(entry) is not None:
            return True
    return False


def rdr_resolution(
    cat: CatalogReader, current_owner: Tumbler, *, repo_source_prefix: str,
) -> tuple[dict[str, Tumbler | None], dict[int, str]]:
    """This repo's RDR records, resolved: ``(rdr_key -> canonical tumbler
    or None, RDR number -> rdr_key)``.

    The ONE place the dependency generator and ``nx rdr set-status``
    (RDR-201 P3.3, nexus-j9z30.22) both derive "which catalog tumbler IS
    RDR-<n>" from -- one listing, one in-repo admission
    (:func:`nexus.catalog.rdr_canonical.is_in_repo`), one canonical
    resolution (:func:`nexus.catalog.rdr_canonical.resolve_all`), one
    number disambiguation (:func:`_numeric_id_index`). A caller that
    re-derived any of those steps would be the second, narrower copy of
    an admission test this arc keeps finding and deleting.
    """
    entries = [
        e
        for content_type in RDR_CONTENT_TYPES
        for e in _entries_of_type(cat, content_type)
    ]
    in_repo_entries = [e for e in entries if is_in_repo(e, current_owner, repo_source_prefix)]
    in_repo_groups = group_rdr_candidates(in_repo_entries)
    resolved = resolve_all(in_repo_entries, current_owner, repo_source_prefix=repo_source_prefix)
    return resolved, _numeric_id_index(cat, in_repo_groups)


def generate_rdr_dependency_links(
    cat: CatalogReader,
    *,
    writer: CatalogWriter | None = None,
    current_owner: Tumbler,
    repo_source_prefix: str,
    new_tumblers: list[Tumbler] | None = None,
    new_content_types: frozenset[str] | set[str] | None = None,
) -> int:
    """Seed RDR-to-RDR catalog links from existing frontmatter.

    Reads ``supersedes``, ``superseded_by``, ``parent_rdr``,
    ``related_rdrs`` off each resolvable RDR document's own frontmatter
    (see :data:`_RDR_DEPENDENCY_FIELDS` for the link-type/direction
    mapping) and creates the corresponding catalog link. Both endpoints
    resolve through :func:`nexus.catalog.rdr_canonical.resolve_all` /
    :func:`nexus.catalog.rdr_canonical.resolve_canonical_tumbler` -- an
    endpoint that does not resolve (Finding-4 ambiguity, a foreign-repo
    collision, or simply no on-disk RDR at that number) creates NO edge
    and logs ``rdr_dependency_target_unresolved`` naming both the source
    and the unresolved target number (RDR-201 § Failure Modes: never a
    guess, never a silent pick).

    *current_owner* and *repo_source_prefix* are REQUIRED (no default),
    mirroring :func:`resolve_canonical_tumbler`'s own discipline -- the
    repo-scoping check this generator's number resolution depends on can
    never be silently skipped by omission. Callers derive them via
    :func:`nexus.catalog.rdr_canonical.current_rdr_owner` /
    :func:`nexus.catalog.rdr_canonical.rdr_source_prefix`.

    *new_tumblers* / *new_content_types*: same incremental-mode contract
    as the other generators in this module (see
    :func:`generate_rdr_filepath_links`) -- when supplied, only entries
    whose CANONICAL tumbler is in *new_tumblers* are scanned as sources;
    target resolution always considers the full catalog, since a newly
    indexed RDR can reference an RDR that already existed.
    """
    if new_tumblers is not None and len(new_tumblers) == 0:
        return 0
    if _no_qualifying_seed(new_tumblers, new_content_types, _RDR_DEPENDENCY_SOURCE_TYPES):
        _log.debug(
            "rdr_dependency_links_skipped_no_seed",
            new_content_types=sorted(new_content_types) if new_content_types else [],
        )
        return 0
    if new_tumblers is not None and not _any_new_tumbler_is_rdr_keyed(cat, new_tumblers):
        _log.debug("rdr_dependency_links_skipped_no_rdr_keyed_seed", new_count=len(new_tumblers))
        return 0

    resolved, number_index = rdr_resolution(
        cat, current_owner, repo_source_prefix=repo_source_prefix,
    )

    if new_tumblers is not None:
        new_set = {str(t) for t in new_tumblers}
        source_keys = [k for k, t in resolved.items() if t is not None and str(t) in new_set]
    else:
        source_keys = [k for k, t in resolved.items() if t is not None]

    w = writer if writer is not None else cat
    count = 0
    for source_key in source_keys:
        source_tumbler = resolved[source_key]
        fm = _reads_frontmatter(cat, source_tumbler)
        if not fm:
            continue
        for field, (link_type, source_is_from) in _RDR_DEPENDENCY_FIELDS.items():
            if field not in fm:
                continue
            for number in _extract_rdr_ref_numbers(fm[field]):
                target_key = number_index.get(number)
                target_tumbler = resolved.get(target_key) if target_key else None
                if target_key is None or target_tumbler is None:
                    _log.warning(
                        "rdr_dependency_target_unresolved",
                        source=source_key,
                        source_tumbler=str(source_tumbler),
                        field=field,
                        target_number=number,
                        target_key=target_key or "",
                        reason=(
                            "target_tumbler_unresolvable" if target_key
                            else "target_rdr_number_not_found_or_ambiguous"
                        ),
                    )
                    continue
                if target_key == source_key or target_tumbler == source_tumbler:
                    continue  # self-reference: no edge
                from_t, to_t = (
                    (source_tumbler, target_tumbler) if source_is_from
                    else (target_tumbler, source_tumbler)
                )
                try:
                    created = w.link_if_absent(
                        from_t, to_t, link_type,
                        created_by="rdr_dependency_extractor",
                    )
                except ValueError:
                    continue
                if created:
                    count += 1
                    _log.debug(
                        "rdr_dependency_link_created",
                        from_t=str(from_t), to_t=str(to_t), link_type=link_type,
                        field=field,
                    )
    return count


def bind_rdr_dependency_generator(
    cat: CatalogReader, repo_root: Path,
) -> tuple[str, Callable[..., int]] | None:
    """Bind :func:`generate_rdr_dependency_links`'s required
    ``current_owner`` / ``repo_source_prefix`` from *repo_root*, producing
    a ``("rdr-dependency", fn)`` tuple that slots into the SAME
    ``(cat, writer=, new_tumblers=, new_content_types=)`` calling
    convention as ``generate_rdr_filepath_links`` / ``generate_
    prose_filepath_links`` / ``generate_pdf_corpus_links`` -- the shared
    loop at both registration sites (``indexer.py``'s ``_catalog_hook``
    and ``commands/catalog_cmds/links.py``'s ``generate_links_cmd``) calls
    every generator identically; this is the ONE place that adapter is
    built, so both sites bind it the same way.

    ``current_owner`` / ``repo_source_prefix`` stay required, no-default
    parameters on the underlying generator itself (rdr_canonical's own
    discipline: the repo-scoping check its number resolution depends on
    can never be silently skipped by omission) -- this function is the
    adapter that satisfies the uniform call shape without weakening that.

    Returns ``None`` when no catalog owner is registered yet for
    *repo_root* (a catalog that has never completed an index has nothing
    to scope endpoint resolution against) -- callers skip registering the
    generator for this run rather than passing a bogus owner.
    """
    owner = current_rdr_owner(cat, repo_root)
    if owner is None:
        return None
    return (
        "rdr-dependency",
        partial(
            generate_rdr_dependency_links,
            current_owner=owner,
            repo_source_prefix=rdr_source_prefix(repo_root),
        ),
    )


def generate_prose_filepath_links(
    cat: CatalogReader,
    *,
    writer: CatalogWriter | None = None,
    new_tumblers: list[Tumbler] | None = None,
    new_content_types: frozenset[str] | set[str] | None = None,
) -> int:
    """nexus-sob9: extract file paths from prose / markdown content
    and link to matching code entries.

    Same shape as ``generate_rdr_filepath_links`` but with two
    contracts widened so prose docs (the original RDR-only filter
    excluded them) get linked to code:

    - Source-side filter: ``content_type in {"prose", "markdown",
      "docs"}`` instead of ``"rdr"``.
    - Path regex: ``_PROSE_PATH_RE`` requires at least one ``/``
      and a recognised source extension, but does NOT require a
      ``src/`` / ``tests/`` source-root anchor. ``docs/`` runbooks,
      ``conexus/`` plugin trees, and ``.claude/`` profiles all match.
      Disambiguates against bare-filename mentions in prose by
      requiring the directory segment.

    Match is then checked against catalog code entries by exact
    ``file_path`` so non-existent strings fall through silently.
    Creates ``implements`` links (prose -> code) with
    ``created_by="filepath_extractor"`` for parity with the RDR
    linker.

    SOURCE content_type(s) are ``{"prose", "markdown", "docs"}``;
    TARGET is "code" — same reasoning as the RDR linker: a run whose
    new_tumblers are all "code" produces no new links here either.

    Closes prose=0.1% catalog auto-link coverage gap from the
    2026-05-08 prod shakeout (4.29.0: 23,378 docs / 23,575 links).

    *new_content_types*: see :func:`generate_rdr_filepath_links` — same
    optional, additive fetch-skip contract, checked against
    ``{"prose", "markdown", "docs"}``.
    """
    if new_tumblers is not None and len(new_tumblers) == 0:
        return 0
    if _no_qualifying_seed(new_tumblers, new_content_types, _PROSE_FILEPATH_SOURCE_TYPES):
        _log.debug(
            "prose_filepath_links_skipped_no_seed",
            new_content_types=sorted(new_content_types) if new_content_types else [],
        )
        return 0

    prose_entries = [
        e
        for content_type in ("prose", "markdown", "docs")
        for e in _entries_of_type(cat, content_type)
        if e.file_path
    ]
    code_entries = [e for e in _entries_of_type(cat, "code") if e.file_path]

    if new_tumblers is not None:
        new_set = {str(t) for t in new_tumblers}
        prose_entries = [
            e for e in prose_entries if str(e.tumbler) in new_set
        ]

    path_to_code: dict[str, Tumbler] = {
        code.file_path: code.tumbler for code in code_entries
    }

    count = 0
    for prose in prose_entries:
        resolved = cat.resolve_path(prose.tumbler)
        if resolved is None or not resolved.is_file():
            continue
        try:
            text = resolved.read_text(errors="replace")
        except OSError:
            continue

        seen_targets: set[str] = set()
        for match in _PROSE_PATH_RE.finditer(text):
            fpath = match.group(0)
            if fpath in seen_targets:
                continue
            seen_targets.add(fpath)
            code_tumbler = path_to_code.get(fpath)
            if code_tumbler is None:
                continue
            try:
                created = (writer if writer is not None else cat).link_if_absent(
                    prose.tumbler, code_tumbler, "implements",
                    created_by="filepath_extractor",
                )
            except ValueError:
                continue
            if created:
                count += 1
                _log.debug(
                    "prose_filepath_link_created",
                    prose=str(prose.tumbler),
                    code=str(code_tumbler),
                    path=fpath,
                )

    return count


def generate_pdf_corpus_links(
    cat: CatalogReader,
    *,
    writer: CatalogWriter | None = None,
    new_tumblers: list[Tumbler] | None = None,
    new_content_types: frozenset[str] | set[str] | None = None,
) -> int:
    """nexus-sob9: link PDFs that share a content_hash via ``same-as``.

    Two PDFs in different physical_collections with the same
    ``head_hash`` are the same source paper indexed twice (e.g. a
    PDF imported into both ``knowledge__delos`` and
    ``knowledge__art-grossberg-papers``). The catalog should
    surface that fact so cross-corpus retrieval can collapse them
    to one logical document.

    Algorithm:
    1. Group catalog PDF entries (``content_type in {"pdf",
       "paper"}``) by ``head_hash`` (the catalog's stored
       file-content hash; populated at register time).
    2. For each group with >= 2 entries, create ``same-as`` links
       from every member to the lexicographically-first member
       (the canonical anchor). Avoids O(N^2) pairwise links;
       everyone links to one anchor and traversal goes through it.

    Idempotent via ``link_if_absent``. Incremental when
    ``new_tumblers`` is supplied: only the new pdf entries emit
    links FROM them; the anchor side may be a pre-existing
    tumbler (that's the desired join point).

    SOURCE and TARGET are the same content_type set here,
    ``{"pdf", "paper"}`` — unlike the filepath linkers, this generator
    only ever matches within its own fetched population (anchor +
    members are both drawn from ``pdf_entries``), so a run whose
    new_tumblers contain no pdf/paper document cannot produce a link
    regardless of how many other pdf/paper documents exist.

    Closes pdf=0% catalog auto-link coverage gap from the
    2026-05-08 prod shakeout.

    *new_content_types*: see :func:`generate_rdr_filepath_links` — same
    optional, additive fetch-skip contract, checked against
    ``{"pdf", "paper"}``.
    """
    if new_tumblers is not None and len(new_tumblers) == 0:
        return 0
    if _no_qualifying_seed(new_tumblers, new_content_types, _PDF_CORPUS_SOURCE_TYPES):
        _log.debug(
            "pdf_corpus_links_skipped_no_seed",
            new_content_types=sorted(new_content_types) if new_content_types else [],
        )
        return 0

    pdf_entries = [
        e
        for content_type in ("pdf", "paper")
        for e in _entries_of_type(cat, content_type)
        if e.head_hash
    ]

    by_hash: dict[str, list[CatalogEntry]] = {}
    for e in pdf_entries:
        by_hash.setdefault(e.head_hash, []).append(e)

    new_set: set[str] | None
    if new_tumblers is not None:
        new_set = {str(t) for t in new_tumblers}
    else:
        new_set = None

    count = 0
    for hash_value, group in by_hash.items():
        if len(group) < 2:
            continue
        anchor = min(group, key=lambda e: str(e.tumbler))
        for member in group:
            if member.tumbler == anchor.tumbler:
                continue
            if new_set is not None and str(member.tumbler) not in new_set:
                continue
            try:
                created = (writer if writer is not None else cat).link_if_absent(
                    member.tumbler, anchor.tumbler, "same-as",
                    created_by="content_hash_dedup",
                )
            except ValueError:
                continue
            if created:
                count += 1
                _log.debug(
                    "pdf_same_as_link_created",
                    member=str(member.tumbler),
                    anchor=str(anchor.tumbler),
                    head_hash=hash_value[:16],
                )

    return count




# ── Dry-run preview (nexus-glivh) ────────────────────────────────────────────
#
# `nx catalog generate-links --dry-run` previously printed "dry-run mode not
# yet supported for link preview" and previewed nothing, so the only way to
# learn what a pass would do was to let it write to a live catalog. That is
# the wrong order for a generator whose failure mode is VOLUME: the
# implements-heuristic flood (nexus-ybj1b) had to be disabled at engine
# v0.1.57 after exactly that kind of unmeasured pass.

@dataclass(frozen=True)
class ProposedLink:
    """One link a dry run would have created."""
    from_tumbler: str
    to_tumbler: str
    link_type: str
    created_by: str


class DryRunLinkWriter:
    """A ``CatalogWriter``-shaped recorder that writes nothing.

    Composes with the generators UNCHANGED: they already take a ``writer=``,
    so a dry run is the real code path with the writes swapped out. A preview
    computed by a separate path is a preview of the wrong thing.

    ``link_if_absent`` returns True only when a link is genuinely new, so a
    count means "would create" and never "would attempt". Pre-existing links
    are supplied up front via :func:`load_existing_link_keys` rather than
    probed one at a time, which would be thousands of round trips.
    """

    def __init__(self, existing: set[tuple[str, str, str]] | None = None) -> None:
        self._existing: set[tuple[str, str, str]] = existing or set()
        self._seen: set[tuple[str, str, str]] = set()
        self._proposed: list[ProposedLink] = []

    def link_if_absent(
        self, from_t: object, to_t: object, link_type: str,
        created_by: str = "", **_meta: object,
    ) -> bool:
        key = (str(from_t), str(to_t), str(link_type))
        # Dedupe within the run as well as against the catalog: a generator
        # that proposes the same edge twice must not be counted twice, or the
        # projected volume is inflated by its own repetition.
        if key in self._existing or key in self._seen:
            return False
        self._seen.add(key)
        self._proposed.append(
            ProposedLink(str(from_t), str(to_t), str(link_type), str(created_by))
        )
        return True

    # The generators only ever call link_if_absent, but a writer that silently
    # accepted a direct link() would write nothing while reporting nothing —
    # fail loud instead of previewing a lie.
    def link(self, *_args: object, **_kwargs: object) -> None:
        raise NotImplementedError(
            "DryRunLinkWriter received a direct link() call. Dry-run preview "
            "only models link_if_absent; a generator using link() would write "
            "unconditionally and its volume would not appear in this preview."
        )

    def close(self) -> None:
        return None

    @property
    def proposed(self) -> list[ProposedLink]:
        return list(self._proposed)

    def count_by_link_type(self) -> dict[str, int]:
        out: dict[str, int] = {}
        for p in self._proposed:
            out[p.link_type] = out.get(p.link_type, 0) + 1
        return out

    def count_by_creator(self) -> dict[str, int]:
        out: dict[str, int] = {}
        for p in self._proposed:
            out[p.created_by] = out.get(p.created_by, 0) + 1
        return out

    def fan_out(self) -> dict[str, int]:
        """Proposed out-degree per source document.

        The number that decides whether a pass is a fix or a flood: total
        count hides a single document proposing thousands of edges.
        """
        out: dict[str, int] = {}
        for p in self._proposed:
            out[p.from_tumbler] = out.get(p.from_tumbler, 0) + 1
        return out


def load_existing_link_keys(cat: CatalogReader, *, page: int = 200) -> set[tuple[str, str, str]]:
    """Every ``(from, to, link_type)`` already in the catalog.

    Paged; the whole link set is small enough to hold in memory (order 1e3 on
    a real corpus). Raises rather than returning a partial set, because a
    short read would silently inflate the "would create" count with links that
    already exist.
    """
    keys: set[tuple[str, str, str]] = set()
    offset = 0
    while True:
        batch = cat.link_query(limit=page, offset=offset)
        if not batch:
            break
        for link in batch:
            keys.add((str(link.from_tumbler), str(link.to_tumbler), str(link.link_type)))
        if len(batch) < page:
            break
        offset += page
    return keys
