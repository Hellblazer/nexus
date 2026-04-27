# SPDX-License-Identifier: AGPL-3.0-or-later
# Copyright (c) 2026 Hal Hildebrand. All rights reserved.
"""SQL fast path for analytics operators (RDR-089 deferred Open Question).

The §D.4 analytics quartet (``operator_filter`` / ``operator_groupby`` /
``operator_aggregate``) on RDR-088 + RDR-093 dispatch to the Claude CLI
per call. When the inputs are documents that have already had aspects
extracted into T2 ``document_aspects`` (RDR-089), the same query can
run as a SQLite SELECT and complete in milliseconds. This module is
the substrate.

Design choices:

- **Three execution modes via ``source`` parameter**: ``"auto"``
  attempts SQL first and falls back to the LLM path when SQL is not
  applicable. ``"aspects"`` forces SQL and errors when input shape
  precludes it. ``"llm"`` skips SQL and dispatches to Claude as
  before. Default for the three operators is ``"auto"``.

- **Two routing knobs**: callers set ``aspect_field=<column>`` to
  pin the SQL column explicitly. When unset, ``_infer_aspect_field``
  parses the criterion / key / reducer for keyword cues. Inference
  is intentionally narrow — when in doubt, return ``None`` and let
  the operator fall back to LLM.

- **Items contract**: SQL path requires every input item to carry
  ``collection`` and ``source_path`` fields (or the runner-canonical
  ``physical_collection`` synonym). Items missing these short-circuit
  to LLM regardless of mode. The operator-level callers must adapt
  their input shape; the planner-side auto-resolution that the RDR
  defers to a separate RDR is what would make this transparent.

- **Consistency model**: when an item's aspect row is missing from
  T2 (queue-pending or extraction failed), the SQL path treats the
  item as not-matching for filter, ``unassigned`` for groupby, and
  excluded from aggregate counts. The LLM path is the back-pressure
  fallback: callers who need eventual consistency can re-run with
  ``source="auto"`` after the queue drains, or pass ``source="llm"``
  to bypass T2 entirely.

- **No new schema**: SQL queries reuse existing ``document_aspects``
  columns. Array fields (``experimental_datasets``,
  ``experimental_baselines``) are stored as JSON TEXT and matched
  with ``LIKE '%"<token>"%'`` for membership; ``extras`` is matched
  via ``json_extract``.

The module returns dicts that match each operator's existing schema
verbatim, so the operator's callers see no shape change beyond the
new optional parameters.
"""
from __future__ import annotations

import json
import re
from typing import Any

import structlog

_log = structlog.get_logger(__name__)


# ── Aspect column registry ──────────────────────────────────────────────────

# Scalar TEXT columns: simple LIKE filter. (column, type) where type
# in {"scalar_text", "json_array", "json_object", "scalar_real"}.
_ASPECT_COLUMN_TYPES = {
    "problem_formulation": "scalar_text",
    "proposed_method": "scalar_text",
    "experimental_datasets": "json_array",
    "experimental_baselines": "json_array",
    "experimental_results": "scalar_text",
    "extras": "json_object",
    "confidence": "scalar_real",
}

# Heuristic keyword → column mapping for ``--aspect_field`` inference.
# Order matters: the first matching keyword wins. Narrow matches first
# (longer / more specific) so we don't claim a generic "method" hit
# when the criterion mentioned "experimental method" specifically.
_INFERENCE_RULES: list[tuple[re.Pattern, str]] = [
    (re.compile(r"\b(dataset|datasets|trained on|tested on)\b", re.I), "experimental_datasets"),
    (re.compile(r"\b(baseline|baselines|compared (with|against|to)|vs\.?)\b", re.I), "experimental_baselines"),
    (re.compile(r"\b(result|results|achieves?|achieved|reports?|reported|throughput|accuracy|f1|recall|precision)\b", re.I), "experimental_results"),
    (re.compile(r"\b(method|approach|technique|algorithm|propose[ds]?|introduce[ds]?|implements?)\b", re.I), "proposed_method"),
    (re.compile(r"\b(problem|challenge|addresses?|targets?|focuses on)\b", re.I), "problem_formulation"),
    (re.compile(r"\b(venue|conference|journal)\b", re.I), "extras.venue"),
    (re.compile(r"\b(year|published in|publication year)\b", re.I), "extras.year"),
]


def _infer_aspect_field(text: str) -> str:
    """Return the most likely aspect field for ``text``, or '' if the
    text contains no recognised cue. Sufficient for the first SQL
    fast path; richer planner-side inference is deferred."""
    for pattern, field in _INFERENCE_RULES:
        if pattern.search(text):
            return field
    return ""


# ── Items shape helpers ─────────────────────────────────────────────────────


def _parse_items(items: str) -> list[dict] | None:
    """Decode the operator's ``items`` argument. Operators accept
    JSON arrays of dicts or plain text. Only the JSON array shape is
    eligible for the SQL path; plain text returns None."""
    try:
        parsed = json.loads(items)
    except (ValueError, TypeError):
        return None
    if not isinstance(parsed, list):
        return None
    if not parsed:
        return []
    if not all(isinstance(x, dict) for x in parsed):
        return None
    return parsed


def _resolve_identity(item: dict) -> tuple[str, str] | None:
    """Return (collection, source_path) tuple if the item carries them,
    else None. Accepts either ``collection`` or
    ``physical_collection`` (the runner's canonical key when items
    come from ``query`` results)."""
    collection = item.get("collection") or item.get("physical_collection")
    source_path = item.get("source_path")
    if collection and source_path:
        return collection, source_path
    return None


def _resolve_all(items: list[dict]) -> list[tuple[str, str]] | None:
    """Resolve every item to (collection, source_path), or return None
    if any item lacks the identity fields."""
    out: list[tuple[str, str]] = []
    for item in items:
        ident = _resolve_identity(item)
        if ident is None:
            return None
        out.append(ident)
    return out


# ── SQL builders ────────────────────────────────────────────────────────────


def _build_filter_predicate(field: str, query: str) -> tuple[str, list]:
    """Return (sql_fragment, params) for a single-column filter.

    Handles ``extras.<key>`` syntax via ``json_extract``. Array
    columns match by JSON-token substring (``LIKE '%"<query>"%'``)
    so ``"TPC-C"`` matches ``["TPC-C", "YCSB"]`` exactly.

    Raises ``ValueError`` for unknown columns (caller should treat as
    "SQL not applicable" and fall back).
    """
    if field.startswith("extras."):
        key = field[len("extras."):]
        return (
            "json_extract(extras, ?) LIKE ?",
            [f"$.{key}", f"%{query}%"],
        )
    col_type = _ASPECT_COLUMN_TYPES.get(field)
    if col_type is None:
        raise ValueError(f"unknown aspect field: {field!r}")
    if col_type == "scalar_text":
        return f"{field} LIKE ?", [f"%{query}%"]
    if col_type == "json_array":
        # Match a token inside the JSON array. The serialised array
        # is e.g. '["TPC-C", "YCSB"]'; match on the quoted token to
        # avoid false positives from substring overlap with the
        # scalar text columns.
        return f"{field} LIKE ?", [f'%"{query}"%']
    if col_type == "json_object":
        return f"{field} LIKE ?", [f"%{query}%"]
    if col_type == "scalar_real":
        # Numeric criterion: caller should pre-format their query
        # as e.g. ">0.7"; for the simple LIKE substrate we treat
        # this as untenable.
        raise ValueError(f"scalar_real column {field!r} not supported by simple LIKE")
    raise ValueError(f"unhandled column type: {col_type}")


# ── Operator entry points ───────────────────────────────────────────────────


_VALID_SOURCES = ("auto", "aspects", "llm")


def _validate_source(source: str) -> None:
    """Reject silent typos like ``"LLM"`` or ``"auto "``. Without this
    guard the unrecognised value falls through to the SQL path and the
    caller silently loses the LLM fallback they thought they were
    invoking.
    """
    if source not in _VALID_SOURCES:
        raise ValueError(
            f"source must be one of {_VALID_SOURCES}; got {source!r}",
        )


def try_filter(
    items: str,
    criterion: str,
    *,
    source: str,
    aspect_field: str,
) -> dict | None:
    """SQL fast path for ``operator_filter``.

    Returns a dict matching the operator's schema
    (``{items, rationale}``) on success, or ``None`` to signal the
    caller should fall back to LLM dispatch.

    Source modes:

    * ``"llm"``: returns ``None`` immediately — caller bypasses SQL.
    * ``"auto"``: attempts SQL; returns ``None`` when prerequisites
      (parseable items with identity, resolvable aspect column,
      populated T2 rows) fail — caller falls back to LLM.
    * ``"aspects"``: attempts SQL; returns a result with empty
      ``items`` and a rationale entry per input documenting the
      reason when prerequisites fail (no LLM fallback). Used when
      the caller specifically wants the SQL semantic and does not
      want a silent re-route to the LLM substrate.
    """
    _validate_source(source)
    if source == "llm":
        return None

    parsed = _parse_items(items)
    if parsed is None:
        return _aspects_only_or_none(
            source, "items not a JSON array of dicts",
        )
    if not parsed:
        return {"items": [], "rationale": []}

    idents = _resolve_all(parsed)
    if idents is None:
        return _aspects_only_or_none(
            source, "items lack collection / source_path identity",
        )

    field = aspect_field or _infer_aspect_field(criterion)
    if not field:
        return _aspects_only_or_none(
            source,
            f"could not infer aspect field from criterion {criterion!r}; "
            f"pass aspect_field=<column> explicitly",
        )

    # Strip the heuristic stop-words off the criterion so the SQL
    # LIKE matches the topical token. Pragmatic: take the longest
    # alphanumeric token from the criterion as the search term.
    # Both inferred and explicit `aspect_field` paths must tokenize:
    # passing the full natural-language criterion as a LIKE pattern
    # silently zero-matches against stored values.
    search_token = _extract_search_token(criterion, field)
    if not search_token:
        return _aspects_only_or_none(
            source,
            f"no search token derivable from criterion {criterion!r}; "
            f"the SQL path requires a substring to LIKE-match",
        )

    try:
        keep, rationale = _query_filter(idents, field, search_token)
    except ValueError as exc:
        return _aspects_only_or_none(source, str(exc))

    # Stitch the kept set back to original items by (coll, src).
    keep_set = set(keep)
    out_items = [
        item for item in parsed
        if _resolve_identity(item) in keep_set
    ]
    return {"items": out_items, "rationale": rationale}


def try_groupby(
    items: str,
    key: str,
    *,
    source: str,
    aspect_field: str,
) -> dict | None:
    """SQL fast path for ``operator_groupby``.

    Returns ``{groups: [{key_value, items}]}`` on success, or
    ``None`` to signal LLM fallback (mode ``"auto"`` only).

    Group cardinality semantics: scalar-column groupby is direct
    (one group per unique value — typically high cardinality for
    free-text columns). JSON-object groupby uses ``json_extract``
    on the ``extras.<key>`` form.

    JSON-array semantics — invariant divergence (substantive
    critic finding): the LLM path's ``operator_groupby`` contract
    requires every input item to appear in EXACTLY ONE group
    (RDR-093 §C-1 inline-items contract). The SQL path on a JSON-
    array column would naturally unroll a multi-value item across
    multiple groups (a paper with ``["TPC-C", "YCSB"]`` would join
    both groups), violating the invariant. Downstream consumers
    that count items across groups, deduplicate, or pipe through
    ``operator_aggregate`` with a ``count`` reducer would silently
    receive double-counted results.

    Resolution: under ``source="auto"`` the SQL path REJECTS
    JSON-array fields and returns ``None`` so the operator falls
    back to LLM (which respects the one-group invariant). Under
    ``source="aspects"`` the SQL path emits a stub group with the
    rejection reason. Callers who specifically want unrolled
    multi-membership semantics must pass ``source="llm"`` and
    rely on LLM partitioning, OR construct a different operator
    (the unroll behaviour does not belong in groupby).

    Items whose aspect row is missing land in
    ``key_value="unassigned"`` (matching the LLM path's fallback
    semantic).
    """
    _validate_source(source)
    if source == "llm":
        return None

    parsed = _parse_items(items)
    if parsed is None:
        return _aspects_only_or_none_grouped(
            source, "items not a JSON array of dicts",
        )
    if not parsed:
        return {"groups": []}

    idents = _resolve_all(parsed)
    if idents is None:
        return _aspects_only_or_none_grouped(
            source, "items lack collection / source_path identity",
        )

    field = aspect_field or _infer_aspect_field(key)
    if not field:
        return _aspects_only_or_none_grouped(
            source,
            f"could not infer aspect field from key {key!r}; "
            f"pass aspect_field=<column> explicitly",
        )

    # JSON-array fields would violate the LLM path's one-group-per-
    # item invariant if unrolled. Reject so the operator falls back
    # to LLM semantics in auto mode, or stubs the divergence in
    # aspects mode. Detection mirrors _ASPECT_COLUMN_TYPES.
    is_json_array = (
        not field.startswith("extras.")
        and _ASPECT_COLUMN_TYPES.get(field) == "json_array"
    )
    if is_json_array:
        return _aspects_only_or_none_grouped(
            source,
            f"groupby on json_array column {field!r} would unroll "
            f"each item across multiple groups, violating the LLM "
            f"path's one-group-per-item contract; falling back to "
            f"LLM (source='auto') or pass source='llm' explicitly",
        )

    try:
        groups_raw = _query_groupby(idents, field)
    except ValueError as exc:
        return _aspects_only_or_none_grouped(source, str(exc))

    # Map identity → original item dict (preserve runtime fields).
    by_identity = {
        _resolve_identity(item): item for item in parsed
    }
    groups = []
    for key_value, identities in groups_raw.items():
        groups.append({
            "key_value": key_value,
            "items": [by_identity[i] for i in identities if i in by_identity],
        })
    return {"groups": groups}


def try_aggregate(
    groups: str,
    reducer: str,
    *,
    source: str,
    aspect_field: str,
) -> dict | None:
    """SQL fast path for ``operator_aggregate``.

    Returns ``{aggregates: [{key_value, summary}]}`` on success.
    Recognised reducers: ``count`` (returns ``"<N> item(s)"`` as the
    summary), ``count distinct``, ``avg confidence`` /
    ``mean confidence``, ``max confidence``, ``min confidence``.
    Anything else returns ``None`` (LLM fallback) under
    ``source="auto"`` or an aspects-only stub under
    ``source="aspects"``.

    Input ``groups`` is the JSON shape produced by
    ``operator_groupby`` (whether SQL or LLM path): a list of
    ``{key_value, items}`` dicts where ``items`` is a list of dicts.
    """
    _validate_source(source)
    if source == "llm":
        return None

    try:
        parsed_groups = json.loads(groups)
    except (ValueError, TypeError):
        return _aspects_only_or_none_aggregated(
            source, "groups not valid JSON",
        )
    if not isinstance(parsed_groups, list):
        return _aspects_only_or_none_aggregated(
            source, "groups not a JSON array",
        )

    reducer_kind = _classify_reducer(reducer)
    if reducer_kind is None:
        return _aspects_only_or_none_aggregated(
            source,
            f"reducer {reducer!r} not recognised by SQL fast path; "
            f"supported: count, count distinct, "
            f"avg/min/max confidence",
        )

    aggregates = []
    for group in parsed_groups:
        if not isinstance(group, dict):
            continue
        key_value = group.get("key_value", "unassigned")
        items = group.get("items") or []

        if reducer_kind == "count":
            summary = f"{len(items)} item(s)"
        elif reducer_kind == "count_distinct":
            # Prefer ``id`` field for dedup; fall back to (collection,
            # source_path) tuple when ``id`` is absent on every item
            # in the group. Items from operator_groupby's SQL path
            # carry collection + source_path but may not have id; the
            # fallback prevents a silent 0 (which would be wrong but
            # plausible-looking).
            ids = {
                item.get("id") for item in items if isinstance(item, dict)
            }
            ids.discard(None)
            if ids:
                summary = f"{len(ids)} distinct item(s)"
            else:
                identities = {
                    (item.get("collection") or item.get("physical_collection"),
                     item.get("source_path"))
                    for item in items if isinstance(item, dict)
                }
                # Drop tuples where both halves are None (truly
                # identity-less items).
                identities = {
                    t for t in identities
                    if t != (None, None)
                }
                if identities:
                    summary = (
                        f"{len(identities)} distinct item(s) "
                        f"(deduped by (collection, source_path); "
                        f"id field absent)"
                    )
                else:
                    summary = (
                        f"{len(items)} item(s) (no id or identity "
                        f"field; cannot dedup)"
                    )
        elif reducer_kind in ("avg_confidence", "max_confidence", "min_confidence"):
            idents = [_resolve_identity(item) for item in items if isinstance(item, dict)]
            idents = [i for i in idents if i is not None]
            if not idents:
                summary = "no items with identity for confidence aggregate"
            else:
                value = _query_confidence_aggregate(idents, reducer_kind)
                if value is None:
                    summary = "no aspect rows found for items"
                else:
                    op = reducer_kind.split("_")[0]
                    summary = f"{op}(confidence) = {value:.3f}"
        else:
            continue

        aggregates.append({
            "key_value": key_value,
            "summary": summary,
        })
    return {"aggregates": aggregates}


# ── SQL execution ───────────────────────────────────────────────────────────


def _query_filter(
    idents: list[tuple[str, str]], field: str, query: str,
) -> tuple[list[tuple[str, str]], list[dict]]:
    """Execute the filter SQL. Return (kept_idents, rationale_entries)."""
    from nexus.commands._helpers import default_db_path
    from nexus.db.t2 import T2Database

    pred_sql, pred_params = _build_filter_predicate(field, query)

    # SELECT in batches of 300 (ChromaDB-quota-equivalent SQLite param
    # cap) to handle large item lists. SQLite's default param limit
    # is 999 with two params per item (collection, source_path), so
    # batching at 300 leaves plenty of headroom.
    # RDR-096 P2.3 dual-read: ``COALESCE(source_uri, 'file://' ||
    # source_path) AS source_identity`` projects the URI form so the
    # rationale entries can surface URIs alongside the source_path
    # ``id`` (kept for back-compat). Rows whose source_uri escaped
    # backfill (empty source_path) get the file://-shaped fallback
    # via the COALESCE second arm.
    #
    # Rationale-shape contract (P2.3):
    # * ``id`` — source_path string (back-compat with pre-P2.3 callers)
    # * ``source_uri`` — populated for rows the SQL fast path
    #   matched; ``""`` when no row exists (non-match, queue pending,
    #   or aspects-only meta entry). Never ``None``: the empty-string
    #   sentinel keeps the field type consistent for downstream
    #   consumers that don't distinguish missing-vs-pending.
    # * ``reason`` — human-readable explanation.
    keep: list[tuple[str, str]] = []
    matches: dict[tuple[str, str], bool] = {}
    uris: dict[tuple[str, str], str] = {}
    with T2Database(default_db_path()) as db:
        conn = db.document_aspects.conn
        for chunk_start in range(0, len(idents), 300):
            batch = idents[chunk_start:chunk_start + 300]
            placeholders = ",".join(["(?, ?)"] * len(batch))
            params: list[Any] = []
            for c, sp in batch:
                params.extend([c, sp])
            sql = (
                f"SELECT collection, source_path, "
                f"       COALESCE(source_uri, 'file://' || source_path) AS source_identity "
                f"FROM document_aspects "
                f"WHERE (collection, source_path) IN ({placeholders}) "
                f"  AND {pred_sql}"
            )
            params.extend(pred_params)
            for c, sp, uri in conn.execute(sql, params).fetchall():
                matches[(c, sp)] = True
                uris[(c, sp)] = uri

    rationale = []
    for c, sp in idents:
        if matches.get((c, sp)):
            keep.append((c, sp))
            rationale.append({
                "id": sp,
                "source_uri": uris.get((c, sp), ""),
                "reason": f"{field} matches '{query}' (SQL fast path)",
            })
        else:
            rationale.append({
                "id": sp,
                "source_uri": "",
                "reason": (
                    f"{field} does not match '{query}' "
                    f"(or aspect row absent — queue may be pending)"
                ),
            })
    return keep, rationale


def _query_groupby(
    idents: list[tuple[str, str]], field: str,
) -> dict[str, list[tuple[str, str]]]:
    """Execute the groupby SQL. Return {key_value: [idents]}."""
    from nexus.commands._helpers import default_db_path
    from nexus.db.t2 import T2Database

    if field.startswith("extras."):
        extras_key = field[len("extras."):]
        select_expr = "json_extract(extras, ?)"
        select_params: list[Any] = [f"$.{extras_key}"]
        col_type = "scalar_text"
    else:
        col_type = _ASPECT_COLUMN_TYPES.get(field)
        if col_type is None:
            raise ValueError(f"unknown aspect field: {field!r}")
        select_expr = field
        select_params = []

    # P2.3 note: ``_query_filter`` widens its SELECT with a COALESCE
    # source_identity column so rationale entries can surface URIs.
    # ``_query_groupby`` does not — its return shape (``{key:
    # [(collection, source_path)]}``) doesn't currently expose
    # identity to callers, and projecting an unused column would be
    # dead overhead. When a future bead adds URI to groupby output,
    # widen the SELECT here and update the unpacking.
    groups: dict[str, list[tuple[str, str]]] = {}
    fetched: dict[tuple[str, str], Any] = {}

    with T2Database(default_db_path()) as db:
        conn = db.document_aspects.conn
        for chunk_start in range(0, len(idents), 300):
            batch = idents[chunk_start:chunk_start + 300]
            placeholders = ",".join(["(?, ?)"] * len(batch))
            params: list[Any] = list(select_params)
            for c, sp in batch:
                params.extend([c, sp])
            sql = (
                f"SELECT collection, source_path, {select_expr} "
                f"FROM document_aspects "
                f"WHERE (collection, source_path) IN ({placeholders})"
            )
            for c, sp, value in conn.execute(sql, params).fetchall():
                fetched[(c, sp)] = value

    for ident in idents:
        value = fetched.get(ident)
        if value is None:
            groups.setdefault("unassigned", []).append(ident)
            continue
        if col_type == "json_array":
            try:
                tokens = json.loads(value)
            except (ValueError, TypeError):
                tokens = []
            if not tokens:
                groups.setdefault("unassigned", []).append(ident)
            else:
                for token in tokens:
                    groups.setdefault(str(token), []).append(ident)
        else:
            groups.setdefault(str(value), []).append(ident)
    return groups


def _query_confidence_aggregate(
    idents: list[tuple[str, str]], reducer_kind: str,
) -> float | None:
    """Run the SQL aggregate (AVG / MIN / MAX) over confidence.

    Paginates over the input identities at 300-id batches (SQLite
    parameter cap). MIN / MAX accumulate by Python folding (still
    O(N) wall-clock but exact). AVG accumulates the sum and count
    across batches and divides at the end (single pass, exact
    arithmetic on floats).
    """
    from nexus.commands._helpers import default_db_path
    from nexus.db.t2 import T2Database

    op_map = {
        "avg_confidence": "AVG",
        "max_confidence": "MAX",
        "min_confidence": "MIN",
    }
    if reducer_kind not in op_map:
        return None

    sum_acc = 0.0
    count_acc = 0
    min_acc: float | None = None
    max_acc: float | None = None

    with T2Database(default_db_path()) as db:
        conn = db.document_aspects.conn
        for chunk_start in range(0, len(idents), 300):
            batch = idents[chunk_start:chunk_start + 300]
            placeholders = ",".join(["(?, ?)"] * len(batch))
            params: list[Any] = []
            for c, sp in batch:
                params.extend([c, sp])
            sql = (
                f"SELECT confidence FROM document_aspects "
                f"WHERE (collection, source_path) IN ({placeholders}) "
                f"  AND confidence IS NOT NULL"
            )
            for (value,) in conn.execute(sql, params).fetchall():
                if value is None:
                    continue
                v = float(value)
                sum_acc += v
                count_acc += 1
                min_acc = v if min_acc is None else min(min_acc, v)
                max_acc = v if max_acc is None else max(max_acc, v)

    if count_acc == 0:
        return None
    if reducer_kind == "avg_confidence":
        return sum_acc / count_acc
    if reducer_kind == "min_confidence":
        return min_acc
    return max_acc  # max_confidence


# ── Helpers ─────────────────────────────────────────────────────────────────


_REDUCER_PATTERNS: list[tuple[re.Pattern, str]] = [
    (re.compile(r"\bcount\s+distinct\b", re.I), "count_distinct"),
    (re.compile(r"\bcount\b", re.I), "count"),
    (re.compile(r"\bavg(?:erage)?\s+confidence\b", re.I), "avg_confidence"),
    (re.compile(r"\bmean\s+confidence\b", re.I), "avg_confidence"),
    (re.compile(r"\bmax(?:imum)?\s+confidence\b", re.I), "max_confidence"),
    (re.compile(r"\bmin(?:imum)?\s+confidence\b", re.I), "min_confidence"),
]


def _classify_reducer(reducer: str) -> str | None:
    for pattern, kind in _REDUCER_PATTERNS:
        if pattern.search(reducer):
            return kind
    return None


def _extract_search_token(criterion: str, field: str) -> str:
    """Pull the topical token out of a natural-language criterion.

    Pragmatic: strip the trigger keywords that matched the inference
    rule for ``field``, then return the longest remaining token. If
    no clean token survives, fall back to the original criterion's
    first non-stop alphanumeric word.
    """
    stripped = criterion
    for pattern, target in _INFERENCE_RULES:
        if target == field:
            stripped = pattern.sub("", stripped)
    # Remove common stop-words and punctuation; pick the longest
    # remaining alphanumeric (or hyphenated) token.
    tokens = re.findall(r"[A-Za-z0-9][\w\-]*", stripped)
    stop = {
        "a", "an", "the", "and", "or", "with", "of", "to", "for",
        "in", "on", "from", "by", "is", "are", "at", "this", "that",
    }
    tokens = [t for t in tokens if t.lower() not in stop and len(t) > 1]
    if not tokens:
        return ""
    return max(tokens, key=len)


def _aspects_only_or_none(source: str, reason: str) -> dict | None:
    """Branch on ``source`` for the filter operator: ``"aspects"`` returns
    an empty result with an explanatory rationale; ``"auto"`` returns
    ``None`` so the caller falls back to LLM."""
    if source == "auto":
        _log.debug("aspect_sql_filter_falls_back_to_llm", reason=reason)
        return None
    return {
        "items": [],
        # ``source_uri: ""`` matches the rationale-shape contract:
        # always present on entries, empty string when no document
        # row exists to project from. ``"_meta"`` rationale rows
        # describe the operator-call outcome, not a document.
        "rationale": [{"id": "_meta", "source_uri": "", "reason": f"aspects-only: {reason}"}],
    }


def _aspects_only_or_none_grouped(source: str, reason: str) -> dict | None:
    if source == "auto":
        _log.debug("aspect_sql_groupby_falls_back_to_llm", reason=reason)
        return None
    return {
        "groups": [{"key_value": "_meta", "items": [], "_reason": reason}],
    }


def _aspects_only_or_none_aggregated(source: str, reason: str) -> dict | None:
    if source == "auto":
        _log.debug("aspect_sql_aggregate_falls_back_to_llm", reason=reason)
        return None
    return {
        "aggregates": [{
            "key_value": "_meta",
            "summary": f"aspects-only: {reason}",
        }],
    }
