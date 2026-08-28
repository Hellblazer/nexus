# SPDX-License-Identifier: AGPL-3.0-or-later
# Copyright (c) 2026 Hal Hildebrand. All rights reserved.

"""Auto-generate typed links in the catalog from metadata cross-matching."""

from __future__ import annotations

import re
from dataclasses import dataclass
from pathlib import Path

import structlog

from nexus.catalog.types import CatalogEntry
from nexus.catalog.catalog_protocol import CatalogReader, CatalogWriter
from nexus.catalog.tumbler import Tumbler

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
