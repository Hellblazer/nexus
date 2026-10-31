# SPDX-License-Identifier: AGPL-3.0-or-later
"""Per-result context annotations for the search and query renders.

A retrieved chunk's owning documents know when they were indexed, when the
source was published, and when it expires; the catalog row carries the
document's index state. None of that reached the reader or the model: a result
was a distance, a title and a snippet. This module turns those facts into one
short line per result, and one instruction line per response, so a reader can
weigh age and status without a second lookup.

Pure functions, fixed-clock friendly: every age is computed against the
``now`` the caller passes. No I/O.

Document-level facts (title, indexed date, year, expiry) come from the
catalog document(s) that own a chunk, never from the T3 chunk row: one row
exists per chash, so a chunk two documents share carries the last writer's
values (nexus-6pfgb, census row 1). :func:`entry_facts` reads them off one
catalog entry; :func:`agreed_facts` keeps a field only when every owner gives
the same value, so a field the owners disagree on, or one that no owner
resolves, is left out rather than filled from the row.
"""
from __future__ import annotations

import json
import math
from collections.abc import Mapping, Sequence
from datetime import UTC, datetime
from typing import Any

from nexus.catalog.note_ttl import note_ttl_days
from nexus.metadata_schema import chunk_ttl_days

#: The response-level instruction, first line of every text render. It
#: states only what the reader can act on from the lines below: the ranking
#: itself is vector similarity with boosts that apply only where configured
#: (hybrid scoring is off by default; the quality boost needs a citation
#: count), so the line makes no claim about how results were ranked.
READER_INSTRUCTION: str = (
    "When results disagree, prefer the newer source, and a record of record "
    "(an accepted RDR, a published paper, a release note) over a working "
    "note; cite the source and its date. Indexed dates are when nexus last "
    "indexed the source, not when it was written."
)

#: The instruction an operator prompt carries when its inputs may open
#: with a ``[source: ...]`` line (see :func:`with_source_note`). Same rule
#: as :data:`READER_INSTRUCTION`, phrased for the model that reads the
#: hydrated chunks rather than the person reading a render.
SOURCE_INSTRUCTION: str = (
    "Some inputs open with a [source: ...] line. It names the collection "
    "and, when the documents holding the text are known, their titles, "
    "plus the dates those documents agree on; a date the documents disagree "
    "on is left out. That line is provenance, not evidence: never quote it "
    "as support. Indexed dates are when nexus last indexed the "
    "source, not when it was written; a published year is the document's "
    "own date. When sources disagree, prefer the more recently published "
    "one, and cite the source and its date when the line gives one."
)

#: The marker :func:`with_source_note` writes; :func:`source_clause` keys
#: on it so a prompt carries :data:`SOURCE_INSTRUCTION` only when some
#: input actually opens with a source line.
SOURCE_MARKER: str = "[source: "

#: Index states worth naming; ``complete`` (and the pre-fence NULL) is the
#: normal case and stays silent.
_QUIET_INDEX_STATES: frozenset[str] = frozenset({"", "complete"})


def _parse_iso(value: Any) -> datetime | None:
    """Best-effort ISO-8601 parse; naive stamps are read as UTC."""
    if not isinstance(value, str) or not value:
        return None
    text = value[:-1] + "+00:00" if value.endswith("Z") else value
    try:
        parsed = datetime.fromisoformat(text)
    except ValueError:
        return None
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=UTC)
    return parsed


def _age_days(stamp: datetime, now: datetime) -> int:
    return max(0, (now - stamp).days)


def annotate(
    metadata: Mapping[str, Any],
    *,
    now: datetime,
    index_state: str | None = None,
    year: int | None = None,
) -> list[str]:
    """Return the annotation tokens for one result, in display order.

    ``metadata`` holds the facts to render (``indexed_at``, ``ttl_days``,
    ``bib_year``): the owning documents' agreed facts
    (:func:`agreed_facts`), never the shared chunk row's, which is the last
    writer's. ``index_state`` and ``year`` come from the catalog row when the
    caller has one (the ``query`` tool's catalog-routed path); a caller
    without one leaves them ``None``.

    Tokens, each present only when its fact is stored:

    - ``indexed YYYY-MM-DD (Nd ago)``
    - ``published YYYY`` from ``bib_year`` or the catalog ``year``
    - ``expires in Nd`` from ``expires_from + ttl_days`` (the note's own put
      time, :func:`entry_facts`; ``indexed_at`` for an entry that has none;
      ``ttl_days`` 0 or absent is permanent and says nothing); a lapsed TTL
      reads ``expired``
    - ``index_state: <state>`` for any state other than ``complete``

    The contradiction flag is NOT repeated here: the render already prints
    it on the title line, and one flag per result is enough.
    """
    tokens: list[str] = []
    indexed = _parse_iso(metadata.get("indexed_at"))
    if indexed is not None:
        tokens.append(f"indexed {indexed.date().isoformat()} ({_age_days(indexed, now)}d ago)")
    published = metadata.get("bib_year") or year
    try:
        published_year = int(published) if published else 0
    except (TypeError, ValueError):
        published_year = 0
    if published_year > 0:
        tokens.append(f"published {published_year}")
    # A row's ttl_days is whatever a writer stored: a bool, a string, NaN or a value past timedelta's
    # range is permanent here, as in the expiry sweep (nexus-njn1a).
    ttl_days = int(chunk_ttl_days(metadata.get("ttl_days")) or 0)
    clock = _parse_iso(metadata.get("expires_from")) or indexed
    if clock is not None and ttl_days > 0:
        remaining = ttl_days - (now - clock).days
        tokens.append(f"expires in {remaining}d" if remaining > 0 else "expired")
    if index_state and index_state not in _QUIET_INDEX_STATES:
        tokens.append(f"index_state: {index_state}")
    return tokens


def annotation_line(
    metadata: Mapping[str, Any],
    *,
    now: datetime | None = None,
    index_state: str | None = None,
    year: int | None = None,
) -> str:
    """The tokens of :func:`annotate` joined for display, or ``""``."""
    tokens = annotate(
        metadata, now=now or datetime.now(UTC), index_state=index_state, year=year,
    )
    return " · ".join(tokens)


#: Metadata key a search hit carries its owners' agreed facts under
#: (:func:`agreed_facts` over the live documents holding its chunk). Set by
#: ``search_engine._attach_display_paths``; a working key like ``_owner_doc_ids``,
#: not a result field, so ``format_json`` leaves it out.
OWNER_FACTS_KEY: str = "_owner_facts"

#: Metadata key a search hit carries its ATTRIBUTED owner's frecency under (RDR-236 Phase 1,
#: nexus-6pfgb.9): the catalog ``meta["frecency_score"]`` of the document the hit is attributed
#: to, set by ``search_engine._attach_display_paths`` from the owner lookup the page already
#: makes. A working key like :data:`OWNER_FACTS_KEY`; absent when the owner states none.
OWNER_FRECENCY_KEY: str = "_owner_frecency"

#: Metadata key a search hit carries its owners' own ``source_agent`` values under (RDR-236 Phase 2,
#: nexus-6pfgb.17): a sorted list of the agents the catalog entries of the hit's live owners
#: state in ``meta["source_agent"]``, set by ``search_engine._attach_display_paths`` from the owner
#: lookup the page already makes. Absent when no live owner states one. A working key like
#: :data:`OWNER_FACTS_KEY`, read only through ``search_engine._row_agents``.
OWNER_AGENTS_KEY: str = "_owner_agents"
#: Metadata key a search hit carries the bibliography of its ATTRIBUTED owner under (RDR-236 Phase 2,
#: nexus-6pfgb.12): ``{"bib_year", "bib_authors", "bib_venue", "bib_citation_count"}``, each present only
#: when the owning document's catalog entry states it, taken from the owner lookup the page already makes.
#: ``query``'s document card reads it; the chunk row's ``bib_*`` keys are the last writer's and are not.
#: Absent when the attributed document states none (or none resolved). A working key like
#: :data:`OWNER_FACTS_KEY`.
OWNER_BIB_KEY: str = "_owner_bib"

#: Metadata key a search hit carries the row count of its ATTRIBUTED owner's manifest under (RDR-236 Phase 2
#: review, nexus-6pfgb.12): the chash-positions route's (or the manifest's) count for the document the hit is
#: attributed to, stamped unconditionally by ``search_engine._attach_from_chash_positions`` and
#: ``_attach_doc_ids_from_catalog``. ``query``'s document card reads it first and the legacy ``chunk_count``
#: row key only when it is absent (a hit the catalog did not resolve): a pre-Phase-3 row still carries the
#: last writer's ``chunk_count``, which the plain ``setdefault`` stamp leaves in place for the scorer.
#: A working key like :data:`OWNER_FACTS_KEY`, never stored.
OWNER_CHUNK_COUNT_KEY: str = "_owner_chunk_count"

#: Metadata key a search hit carries what the quality boost reads under (nexus-6pfgb.12):
#: ``{"bib_citation_count", "bib_year"}`` of the attributed owner, or, for a hit whose ``doc_id`` the lookup
#: CHOSE (:data:`DOC_ID_INFERRED_KEY`), of the live owner with the most citations (the choice is arbitrary,
#: so an enriched owner is not penalised, as for :data:`OWNER_FRECENCY_KEY`). Absent when no owner states a
#: citation count; ``apply_quality_boost`` then boosts nothing. A working key like :data:`OWNER_FACTS_KEY`.
OWNER_QUALITY_KEY: str = "_owner_quality"

#: Metadata key a search hit carries the absolute local path of its attributed document under
#: (nexus-6pfgb.49): the ``file://`` ``source_uri`` of the catalog entry the page's one owner lookup
#: already resolved. ``_display_path`` is the catalog ``file_path``, often repo-relative; a reader that
#: must OPEN the file (the line number of a prose hit) tries this next, never the chunk row's
#: ``source_path``, which is the last writer's file. Absent when the document has no local ``file://``
#: source. A working key like :data:`OWNER_FACTS_KEY`, never stored.
DISPLAY_SOURCE_PATH_KEY: str = "_display_source_path"

#: Working key set to ``True`` on a hit whose ``doc_id`` the owner lookup CHOSE (the chunk row
#: named none; the engine's order picked the first holder, or, on the manifest fallback, a holder homed in the
#: hit's own collection, else the smallest ``doc_id`` in text order: nexus-6pfgb.52). Such a choice is arbitrary, so the
#: hit's frecency is the best of its live owners', not the chosen one's (RDR-236 Phase 1 review,
#: nexus-6pfgb.9). Never set when the row carried its own ``doc_id``.
DOC_ID_INFERRED_KEY: str = "_doc_id_inferred"

#: The most owners of one chunk resolved for its title and facts. A chunk many
#: documents share (boilerplate) would otherwise make every read resolve all of
#: them; past the cap the owners cannot be shown to agree, so a reader states
#: no facts for that chunk.
OWNER_RESOLVE_CAP: int = 10

#: Facts compared by value across owners. ``indexed_at`` compares as an
#: instant, so two spellings of one stamp agree.
_FACT_KEYS: tuple[str, ...] = ("title", "file_path", "indexed_at", "bib_year", "ttl_days", "expires_from")


def meta_frecency_score(meta: Any) -> float | None:
    """The document's catalog frecency (``meta["frecency_score"]``), or ``None`` when it states
    none: absent, not a number, a bool or not finite (a stored string or NaN is not a score)."""
    if not isinstance(meta, Mapping):
        return None
    value = meta.get("frecency_score")
    if isinstance(value, bool) or not isinstance(value, (int, float)) or not math.isfinite(value):
        return None
    return float(value)


def entry_facts(entry: Any) -> dict[str, Any]:
    """The provenance facts one catalog entry states, each present only when stored (the document's
    other bibliography, ``bib_authors`` / ``bib_venue`` / ``bib_citation_count``, rides along for the
    card and the boost, nexus-6pfgb.12).

    ``title``, ``file_path``, ``indexed_at`` (the document's own index time),
    ``bib_year`` (the enriched year, else the catalog ``year``) and ``ttl_days``
    (the entry's ``meta["ttl_days"]``, which exists once the notes catalog-meta
    writer has stamped it; an entry without one has no expiry to show) and, for
    a note whose ``meta`` records its put time, ``expires_from``: the stamp the
    TTL counts from, the same one ``nx store expire`` uses
    (:mod:`nexus.catalog.note_ttl`), so the display and the sweep agree. An
    entry without one counts from ``indexed_at``. Reads attributes with
    defaults, so a hand-built stub entry works.
    """
    facts: dict[str, Any] = {}
    for key in ("title", "file_path", "indexed_at"):
        value = str(getattr(entry, key, "") or "").strip()
        if value:
            facts[key] = value
    try:
        year = int(getattr(entry, "bib_year", 0) or getattr(entry, "year", 0) or 0)
    except (TypeError, ValueError):
        year = 0
    if year > 0:
        facts["bib_year"] = year
    # nexus-6pfgb.12: the rest of the document's own bibliography, for ``query``'s card and the quality
    # boost. Not provenance facts (``agreed_facts`` compares only ``_FACT_KEYS``), so they never reach
    # an age line; each present only when stored.
    authors = str(getattr(entry, "bib_authors", "") or getattr(entry, "author", "") or "").strip()
    if authors:
        facts["bib_authors"] = authors
    venue = str(getattr(entry, "bib_venue", "") or "").strip()
    if venue:
        facts["bib_venue"] = venue
    try:
        cites = int(getattr(entry, "bib_citation_count", 0) or 0)
    except (TypeError, ValueError):
        cites = 0
    if cites > 0:
        facts["bib_citation_count"] = cites
    meta = getattr(entry, "meta", None)
    if isinstance(meta, Mapping) and meta.get("ttl_days") is not None:
        # the note's own TTL as the expiry sweep reads it: a bool, NaN or out-of-range value is permanent
        if days := note_ttl_days(meta):
            facts["ttl_days"] = days
    if isinstance(meta, Mapping) and facts.get("ttl_days", 0) > 0 and _parse_iso(meta.get("put_at")) is not None:
        facts["expires_from"] = str(meta["put_at"])
    # RDR-236 Phase 2: the note's own writer (``meta["source_agent"]``, the notes catalog-meta home)
    # rides on the same lookup, for the contradiction flag. Like frecency it is not a provenance fact.
    if isinstance(meta, Mapping) and isinstance(agent := meta.get("source_agent"), str) and agent.strip():
        facts["source_agent"] = agent.strip()
    # RDR-236 Phase 1: the document's own frecency rides on the same lookup. It is not a provenance
    # fact (``agreed_facts`` compares only ``_FACT_KEYS``), so it never reaches a display line.
    if (frecency := meta_frecency_score(meta)) is not None:
        facts["frecency_score"] = frecency
    return facts


def _comparable(key: str, value: Any) -> Any:
    if key in ("indexed_at", "expires_from"):
        return _parse_iso(value) or value
    return value


def agreed_facts(per_owner: Sequence[Mapping[str, Any]]) -> dict[str, Any]:
    """The facts every owner states with the same value; a field any owner lacks
    or states differently is left out. No owners states nothing."""
    if not per_owner:
        return {}
    agreed: dict[str, Any] = {}
    for key in _FACT_KEYS:
        if not all(key in facts for facts in per_owner):
            continue
        values = {_comparable(key, facts[key]) for facts in per_owner}
        if len(values) == 1:
            agreed[key] = per_owner[0][key]
    # An expiry counts from a stamp: the owners' own put times when any states one,
    # else the indexed stamp. Without an agreed stamp there is nothing to count from.
    clock = "expires_from" if any("expires_from" in facts for facts in per_owner) else "indexed_at"
    if clock not in agreed:
        agreed.pop("ttl_days", None)
    return agreed


def owner_titles(titles: Sequence[str], *, limit: int = 3) -> str:
    """The title shown for the documents holding a chunk (nexus-sis0m.5).

    A chunk row carries its last writer's title, which names the wrong
    document when two share identical text, and a deleted one once that
    writer is removed. The catalog's live owners are named instead: the one
    title, or several joined, past *limit* with a count.
    """
    distinct = sorted(dict.fromkeys(t for t in titles if t))
    if len(distinct) <= limit:
        return " · ".join(distinct)
    return " · ".join(distinct[:limit]) + f" (+{len(distinct) - limit} more)"


def _owner_title_list(per_owner: Sequence[Mapping[str, Any]], *, limit: int = 3) -> str:
    """The owners' titles for a source note, quoted and joined with ", " so a title that holds
    " · " (the note's own separator) cannot read as two parts. Up to *limit* titles, then
    "+N more"; an owner with no title is counted ("N untitled"), never dropped silently."""
    distinct = sorted({str(f["title"]) for f in per_owner if f.get("title")})
    untitled = sum(1 for f in per_owner if not f.get("title"))
    if not distinct:
        return ""
    text = ", ".join(json.dumps(t, ensure_ascii=False) for t in distinct[:limit])
    extra = [f"+{len(distinct) - limit} more"] if len(distinct) > limit else []
    if untitled:
        extra.append(f"{untitled} untitled")
    return f"{text} ({', '.join(extra)})" if extra else text


def note_facts(per_owner: Sequence[Mapping[str, Any]]) -> dict[str, Any]:
    """:func:`agreed_facts` for a source note, which also names the owners.

    A title is a set of names, not a conflict: where the owners' titles differ
    the note lists them (quoted, comma-joined, past three a count, owners
    without a title counted), so the line still says what to cite and cannot
    be read as one document. Dates, year and expiry stay omitted where the
    owners disagree.
    """
    facts = agreed_facts(per_owner)
    if "title" not in facts:
        names = _owner_title_list(per_owner)
        if names:
            facts["title"] = names
    return facts


def hit_annotation_line(metadata: Mapping[str, Any], *, now: datetime | None = None) -> str:
    """The age/expiry line of one search hit, from its owners' agreed facts
    (:data:`OWNER_FACTS_KEY`). The chunk row's own ``indexed_at``, ``bib_year`` and
    ``ttl_days`` are the last writer's and are never read; a hit with no resolved
    owners, or owners that disagree, prints nothing for the fields in question."""
    return annotation_line(metadata.get(OWNER_FACTS_KEY) or {}, now=now)


def source_note(
    facts: Mapping[str, Any],
    *,
    collection: str = "",
    now: datetime | None = None,
) -> str:
    """One line naming a hydrated chunk's document, collection and dates.

    ``<title or file path> · <collection> · <annotate() tokens>``, with every
    absent part dropped. *facts* are the chunk's owners' agreed facts
    (:func:`agreed_facts`), never the chunk row. Empty when nothing is known, so
    a caller can test truthiness before prefixing.
    """
    label = str(facts.get("title") or facts.get("file_path") or "").strip()
    parts = [x for x in (label, collection.strip()) if x]
    parts.extend(annotate(facts, now=now or datetime.now(UTC)))
    return " · ".join(parts)


def with_source_note(content: str, note: str) -> str:
    """Prefix ``content`` with ``[source: <note>]`` on its own line.

    Empty content or an empty note returns ``content`` unchanged, so a
    chunk this cannot describe is never altered.
    """
    if not content or not note:
        return content
    return f"[source: {note}]\n{content}"


def source_clause(payload: str) -> str:
    """The instruction to append to an operator prompt whose *payload*
    carries at least one source line, else ``""``. A dispatch with no
    marked input pays nothing and reads exactly as before."""
    if SOURCE_MARKER in (payload or ""):
        return f"\n\n{SOURCE_INSTRUCTION}"
    return ""
