# SPDX-License-Identifier: AGPL-3.0-or-later
"""Shared where-filter parsing for ChromaDB metadata queries.

Used by both MCP tools and CLI commands.
"""
from __future__ import annotations

import re

NUMERIC_FIELDS = frozenset({
    "bib_year", "bib_citation_count", "page_number", "page_count",
    "chunk_index", "chunk_count", "chunk_start_char", "chunk_end_char",
})

_WHERE_RE = re.compile(r"^([A-Za-z_][A-Za-z0-9_]*)(>=|<=|!=|>|<|=)(.*)$")
_OP_MAP: dict[str, str | None] = {
    ">=": "$gte", "<=": "$lte", "!=": "$ne",
    ">": "$gt", "<": "$lt", "=": None,
}


def coerce_value(key: str, value: str, *, strict: bool = False) -> str | int | float:
    """Coerce *value* to int/float for known numeric metadata fields.

    When *strict* is True, raises ``ValueError`` if a numeric field
    has a non-numeric value. Otherwise returns the original string.
    """
    if key in NUMERIC_FIELDS:
        try:
            return int(value)
        except ValueError:
            try:
                return float(value)
            except ValueError:
                if strict:
                    raise ValueError(
                        f"field {key!r} requires a numeric value, got {value!r}"
                    )
    return value


def parse_where(
    pairs: list[str] | tuple[str, ...],
    *,
    strict: bool = False,
) -> dict | None:
    """Parse ``KEY{op}VALUE`` strings into a ChromaDB where dict.

    Supported operators: ``=``, ``>=``, ``<=``, ``>``, ``<``, ``!=``.
    Known numeric fields are auto-coerced to int/float.

    Multiple pairs are ANDed via ``$and`` when operator nesting is present,
    or merged into a flat dict for simple equality filters.
    Returns ``None`` when *pairs* is empty.

    Raises ``ValueError`` on invalid format or empty values.
    """
    if not pairs:
        return None
    parts: list[dict] = []
    for pair in pairs:
        pair = pair.strip()
        if not pair:
            continue
        m = _WHERE_RE.match(pair)
        if not m:
            raise ValueError(f"Invalid where format: {pair!r}. Use KEY=VALUE or KEY>=VALUE")
        key, op_str, raw_value = m.group(1), m.group(2), m.group(3)
        if not raw_value:
            raise ValueError(f"empty value in where clause: {pair!r}")
        value = coerce_value(key, raw_value, strict=strict)
        chroma_op = _OP_MAP[op_str]
        if chroma_op is None:
            parts.append({key: value})
        else:
            parts.append({key: {chroma_op: value}})
    if not parts:
        return None
    if len(parts) == 1:
        return parts[0]
    if all(not isinstance(v, dict) for p in parts for v in p.values()):
        merged: dict = {}
        for p in parts:
            merged.update(p)
        return merged
    return {"$and": parts}


def parse_where_str(where_str: str) -> dict | None:
    """Parse a comma-separated where string. Convenience for MCP tools."""
    if not where_str.strip():
        return None
    return parse_where(where_str.split(","))


# ── Query sanitizer (RDR-071) ────────────────────────────────────────────────
#
# 4-step cascade to extract search intent from agent queries that may
# have system prompts, chain-of-thought, or tool preambles prepended.
# Ported from mempalace/query_sanitizer.py, adapted to return str.

import structlog

_sanitizer_log = structlog.get_logger("nexus.query_sanitizer")

MAX_QUERY_LENGTH = 500  # Above this, system prompt almost certainly dominates
SAFE_QUERY_LENGTH = 200  # Below this, query is almost certainly clean
MIN_QUERY_LENGTH = 10  # Extracted result shorter than this = extraction failed

# Sentence splitter: split on . ! ? (including fullwidth) and newlines
_SENTENCE_SPLIT = re.compile(r"[.!?。！？\n]+")

# Question detector: ends with ? or fullwidth ？
_QUESTION_MARK = re.compile(r'[?？]\s*["\']?\s*$')


def sanitize_query(raw_query: str) -> str:
    """Extract search intent from a potentially contaminated query.

    AI agents sometimes prepend system prompts (2000+ chars) to search
    queries. The embedding model represents the concatenated string as a
    single vector where the prompt overwhelms the question, causing
    near-total retrieval failure on MiniLM (5x distance inflation).

    4-step cascade:
    1. Passthrough (<= 200 chars): no action needed
    2. Question extraction: find last sentence ending with ?
    3. Tail sentence: last meaningful sentence
    4. Tail truncation: last 500 chars (fallback)

    Returns the sanitized query string.
    """
    if not raw_query or not raw_query.strip():
        return ""

    raw_query = raw_query.strip()
    original_length = len(raw_query)

    # Step 1: Short query passthrough
    if original_length <= SAFE_QUERY_LENGTH:
        return raw_query

    # Step 2: Question extraction
    all_segments = [s.strip() for s in raw_query.split("\n") if s.strip()]

    question_sentences: list[str] = []
    for seg in reversed(all_segments):
        if _QUESTION_MARK.search(seg):
            question_sentences.append(seg)

    if not question_sentences:
        sentences = [s.strip() for s in _SENTENCE_SPLIT.split(raw_query) if s.strip()]
        for sent in reversed(sentences):
            if "?" in sent or "？" in sent:
                question_sentences.append(sent)

    if question_sentences:
        candidate = question_sentences[0].strip()
        if len(candidate) >= MIN_QUERY_LENGTH:
            if len(candidate) > MAX_QUERY_LENGTH:
                candidate = candidate[-MAX_QUERY_LENGTH:]
            _sanitizer_log.debug(
                "query_sanitized",
                method="question_extraction",
                original_length=original_length,
                clean_length=len(candidate),
            )
            return candidate

    # Step 3: Tail sentence extraction
    for seg in reversed(all_segments):
        seg = seg.strip()
        if len(seg) >= MIN_QUERY_LENGTH:
            candidate = seg
            if len(candidate) > MAX_QUERY_LENGTH:
                candidate = candidate[-MAX_QUERY_LENGTH:]
            _sanitizer_log.debug(
                "query_sanitized",
                method="tail_sentence",
                original_length=original_length,
                clean_length=len(candidate),
            )
            return candidate

    # Step 4: Tail truncation (fallback)
    candidate = raw_query[-MAX_QUERY_LENGTH:].strip()
    _sanitizer_log.debug(
        "query_sanitized",
        method="tail_truncation",
        original_length=original_length,
        clean_length=len(candidate),
    )
    return candidate
