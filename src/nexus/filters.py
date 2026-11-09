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
_RANGE_OPS = frozenset({"$gte", "$lte", "$gt", "$lt"})


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


def _coerce_range_operand(key: str, raw: str, *, strict: bool) -> str | int | float:
    """Operand typing for RANGE operators (nexus-gnrow, critique of 4l80g).

    The service bridge is operand-typed: a numeric operand compares
    numerically (jsonb_typeof-guarded), a string operand compares LEXICALLY
    ('9' > '10'). With the old 8-field ``NUMERIC_FIELDS``-only coercion, a
    range query on any OTHER field shipped its numeric-looking value as a
    string and silently took the lexical path — plausible-looking, silently
    WRONG results (strictly worse than the pre-4l80g loud 400).

    Rule: a range operator with an unambiguous numeric literal expresses
    numeric intent for ANY field. Quote the value (``field>='2026-01-01'``)
    to force the lexical/string compare (correct for ISO dates and other
    ordered strings). Non-numeric literals stay strings, quoted or not.
    """
    if len(raw) >= 2 and raw[0] == raw[-1] and raw[0] in ("'", '"'):
        return raw[1:-1]
    coerced = coerce_value(key, raw, strict=strict)
    if isinstance(coerced, (int, float)):
        return coerced
    try:
        return int(raw)
    except ValueError:
        try:
            return float(raw)
        except ValueError:
            return raw


def parse_where(
    pairs: list[str] | tuple[str, ...],
    *,
    strict: bool = False,
) -> dict | None:
    """Parse ``KEY{op}VALUE`` strings into a ChromaDB where dict.

    Supported operators: ``=``, ``>=``, ``<=``, ``>``, ``<``, ``!=``.
    Known numeric fields are auto-coerced to int/float.

    Multiple pairs on distinct keys merge into one flat dict: every key in it
    must hold, and the engine ANDs the fields of one dict. It refuses a
    ``$and`` key outright, so this never returns one (nexus-3ceda.11: a
    two-clause filter with an operator, ``content_type=pdf,bib_year>=2020``,
    used to become ``$and`` and failed every collection with an HTTP 400).
    Returns ``None`` when *pairs* is empty.

    Raises ``ValueError`` on invalid format, empty values, or a repeated key.
    A repeated equality key (``k=a`` + ``k=b``) is never a legitimate AND: no
    record can equal two different values, so scope "any of several exact
    values" as two queries unioned client-side (single-key ``$in`` support
    tracked as nexus-4gzc8). A repeated key with an operator (a range on one
    field, ``bib_year>=2020`` + ``bib_year<=2024``, or ``k!=a`` + ``k!=b``) has
    no engine form yet: the engine holds one condition per field.
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
        if raw_value[0] in "=<>!":
            # nexus-zdzm5: "score>>1" parsed as score > ">1", a well-formed
            # filter that matches nothing, and search answered "No results."
            raise ValueError(
                f"Invalid where operator in {pair!r}: the value starts with "
                f"{raw_value[0]!r}. Operators are =, >=, <=, >, <, !=."
            )
        chroma_op = _OP_MAP[op_str]
        if chroma_op in _RANGE_OPS:
            value = _coerce_range_operand(key, raw_value, strict=strict)
        else:
            value = coerce_value(key, raw_value, strict=strict)
        if chroma_op is None:
            parts.append({key: value})
        else:
            parts.append({key: {chroma_op: value}})
    if not parts:
        return None
    # nexus-1oguj review / nexus-4gzc8: a naive dict.update() here silently
    # overwrites on a repeated key — `--where k=a --where k=b` used to land on
    # k=b with zero signal that k=a was ever requested. Fail loud instead.
    merged: dict = {}
    for p in parts:
        for key, value in p.items():
            if key not in merged:
                merged[key] = value
                continue
            if isinstance(value, dict) or isinstance(merged[key], dict):
                both = f"{_clause_text(key, merged[key])} and {_clause_text(key, value)}"
                if _is_range(value) and _is_range(merged[key]):
                    raise ValueError(
                        f"--where has two conditions on {key!r} ({both}): a range on "
                        "one field is not supported yet, because the engine takes one "
                        "condition per field. Filter on one bound and drop the results "
                        "outside the other yourself."
                    )
                raise ValueError(
                    f"--where has two conditions on {key!r} ({both}): the engine "
                    "takes one condition per field. Keep the one that narrows the "
                    "results most and drop the rest yourself."
                )
            if merged[key] != value:
                raise ValueError(
                    f"duplicate --where key {key!r} with different "
                    f"values: {merged[key]!r} and {value!r}. "
                    "A single equality filter can only match one "
                    "value per key — this is not an AND (no record "
                    "can equal both). To scope 'any of several exact "
                    f"values for {key!r}', run two queries (one per "
                    "value) and union the results client-side; "
                    "single-key $in support is tracked as nexus-4gzc8."
                )
            raise ValueError(
                f"duplicate --where key {key!r} repeated with "
                f"the same value {value!r} — remove the "
                "redundant --where clause."
            )
    return merged


_OP_TEXT: dict[str, str] = {op: text for text, op in _OP_MAP.items() if op is not None}


def _is_range(value: object) -> bool:
    return isinstance(value, dict) and set(value) <= _RANGE_OPS


def _clause_text(key: str, value: object) -> str:
    """``bib_year>=2020`` for ``{"$gte": 2020}``: the clause as the user typed it."""
    if isinstance(value, dict):
        ((op, operand),) = value.items()
        return f"{key}{_OP_TEXT.get(op, op)}{operand}"
    return f"{key}={value}"


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
