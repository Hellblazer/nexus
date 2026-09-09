# SPDX-License-Identifier: AGPL-3.0-or-later
"""Per-result context annotations for the search and query renders.

A retrieved chunk carries when it was indexed, when its source was
published, and when it expires; the catalog row carries the document's
index state. None of that reached the reader or the model: a result was a
distance, a title and a snippet. This module turns what is already stored
into one short line per result, and one instruction line per response,
so a reader can weigh age and status without a second lookup.

Pure functions, fixed-clock friendly: every age is computed against the
``now`` the caller passes. No I/O.
"""
from __future__ import annotations

from collections.abc import Mapping
from datetime import UTC, datetime
from typing import Any

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
    "Some inputs open with a [source: ...] line naming the document, its "
    "collection and its dates. That line is provenance, not evidence: never "
    "quote it as support. Indexed dates are when nexus last indexed the "
    "source, not when it was written; a published year is the document's "
    "own date. When sources disagree, prefer the more recently published "
    "one, and cite the source and its date."
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

    ``metadata`` is the chunk's stored metadata (``indexed_at``,
    ``ttl_days``, ``bib_year``). ``index_state`` and ``year`` come from
    the catalog row when the caller has one (the ``query`` tool's
    catalog-routed path); a chunk-only caller leaves them ``None``.

    Tokens, each present only when its fact is stored:

    - ``indexed YYYY-MM-DD (Nd ago)``
    - ``published YYYY`` from ``bib_year`` or the catalog ``year``
    - ``expires in Nd`` from ``indexed_at + ttl_days`` (``ttl_days`` 0 or
      absent is permanent and says nothing); a lapsed TTL reads ``expired``
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
    ttl = metadata.get("ttl_days")
    try:
        ttl_days = int(ttl) if ttl is not None else 0
    except (TypeError, ValueError):
        ttl_days = 0
    if indexed is not None and ttl_days > 0:
        remaining = ttl_days - (now - indexed).days
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


def source_note(
    metadata: Mapping[str, Any],
    *,
    collection: str = "",
    now: datetime | None = None,
) -> str:
    """One line naming a hydrated chunk's document, collection and dates.

    ``<title or display path or source path> · <collection> · <annotate()
    tokens>``, with every absent part dropped. Empty when nothing is
    known, so a caller can test truthiness before prefixing.
    """
    label = (
        str(metadata.get("title") or "").strip()
        or str(metadata.get("_display_path") or metadata.get("source_path") or "").strip()
    )
    parts = [x for x in (label, collection.strip()) if x]
    parts.extend(annotate(metadata, now=now or datetime.now(UTC)))
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
