# SPDX-License-Identifier: Apache-2.0
"""Core tuple-space API: out, read, take, ack, nack, list_subspaces,
subspace_schema, subspace_stats (RDR-110 P1.4, nexus-8q4v).

All functions accept injected dependencies (conn, index, registry) so
callers control the storage backend.  No global singletons here; the MCP
tool layer (src/nexus/mcp/core.py) wires singletons to these functions.

**Modes**:

- ``semantic`` (default): chroma top-K -> floor/margin gate -> ID list -> CAS.
- ``exact`` (e.g. ``locks/<resource>``): SQL-only candidate selection on
  ``schema.take.match_keys``; bypasses chroma entirely.

**CAS atomicity** (honker RF-9): ``take`` uses a single-statement
``UPDATE ... WHERE id = (SELECT ... LIMIT 1) RETURNING id`` under SQLite's
single-writer lock.  Two concurrent claimants cannot both succeed on the same
row because only one UPDATE wins the write lock; the loser's subquery finds no
eligible row (claim_state already set) and returns nothing.

**Idempotent retake** by the same claimant is a two-statement read-then-update
per RDR-110 design note.  The CAS pattern conflates same-claimant retake with
foreign-claimant contention, so same-claimant retake is handled separately.

**block=True is feature-flagged OFF in Phase 1** (overlayfs unsafe, RDR-112
§A2).  The parameter is accepted but raises ``BlockingNotSupported`` in
direct-mode.  The daemon-mode block path lands in Phase 3.

**embed_from** validation happens on the first ``out`` against a subspace.
Valid forms: ``"content"``, ``"match_text"``, or ``"dimensions:<key>"`` where
``<key>`` exists in the schema's ``dimensions`` block.

**unixepoch() note** (store.py doc): any query touching ``idx_tuples_avail``
must use ``unixepoch()`` inline, NOT a bound ``?``, for the partial-index
predicate ``claim_expires_at < unixepoch()``.  The ``_select_candidates_sql``
helper follows this rule.
"""

from __future__ import annotations

import hashlib
import json
import sqlite3
import time
from typing import Any, Callable, Optional

import structlog

from nexus.tuplespace.index import TupleIndex
from nexus.tuplespace.registry import (
    Registry,
    SubspaceSchema,
    UnknownSubspaceError,
)

_log = structlog.get_logger(__name__)


# ---------------------------------------------------------------------------
# Errors
# ---------------------------------------------------------------------------


class SubspaceSchemaError(ValueError):
    """Raised when dimensions or embed_from fail schema validation."""


class TakeDisabledError(RuntimeError):
    """Raised when take() is called on a subspace with take.enabled=False."""


class InvalidTimeoutError(ValueError):
    """Raised when timeout_seconds exceeds the MCP transport budget cap."""


class BlockingNotSupported(NotImplementedError):
    """Raised when block=True is requested in direct-mode (Phase 1 feature-flag)."""


class ClaimOwnershipError(PermissionError):
    """Raised when ack/nack is called by a claimant that does not own the claim."""


class ClaimNotFoundError(LookupError):
    """Raised when ack/nack is called with a claim_id that does not exist."""


# ---------------------------------------------------------------------------
# Test-injection hook (used in test_api.py CAS race test)
# ---------------------------------------------------------------------------

#: If set, called by ``take`` immediately after candidate selection and before
#: the CAS UPDATE.  Used in tests to synchronise concurrent threads at the
#: race window.  Must be ``None`` in production.
_take_pre_update_hook: Optional[Callable[[], None]] = None


# ---------------------------------------------------------------------------
# ID computation
# ---------------------------------------------------------------------------


def _tuple_id(
    subspace: str,
    content: str,
    dimensions: dict[str, Any],
    match_text: str,
) -> str:
    """Compute a stable 32-hex chash for a tuple.

    The ID includes subspace, content, dimensions (sorted), and match_text so
    that logically distinct tuples in the same subspace with identical content
    but different dimensions produce distinct IDs.

    Args:
        subspace: Concrete subspace string (e.g. ``"tasks/nexus"``).
        content: Tuple body text.
        dimensions: Validated dimensions dict.
        match_text: Embedding override text, or empty string if not provided.

    Returns:
        32-character lowercase hex string (sha256 prefix).
    """
    canonical = json.dumps(
        {
            "subspace": subspace,
            "content": content,
            "dimensions": dimensions,
            "match_text": match_text,
        },
        sort_keys=True,
        ensure_ascii=True,
    )
    return hashlib.sha256(canonical.encode()).hexdigest()[:32]


def _claim_id(claimant: str, tuple_id: str, now: float) -> str:
    """Compute a stable claim ID from claimant + tuple_id + timestamp."""
    canonical = f"{claimant}:{tuple_id}:{now}"
    return hashlib.sha256(canonical.encode()).hexdigest()[:32]


# ---------------------------------------------------------------------------
# Validation helpers
# ---------------------------------------------------------------------------


def _validate_embed_from(embed_from: str, dimensions: dict[str, Any]) -> None:
    """Validate an embed_from value against valid forms.

    Valid forms:
    - ``"content"``
    - ``"match_text"``
    - ``"dimensions:<key>"`` where ``<key>`` is non-empty and exists in
      *dimensions*.

    Args:
        embed_from: The embed_from string from the subspace schema.
        dimensions: The dimensions dict (schema dimensions block keys).

    Raises:
        SubspaceSchemaError: If the value is not one of the valid forms.
    """
    if embed_from == "content":
        return
    if embed_from == "match_text":
        return
    if embed_from.startswith("dimensions:"):
        key = embed_from[len("dimensions:"):]
        if not key:
            raise SubspaceSchemaError(
                f"embed_from={embed_from!r} has empty key after 'dimensions:'; "
                "must be 'dimensions:<key>' where <key> exists in the schema dimensions block"
            )
        if key not in dimensions:
            raise SubspaceSchemaError(
                f"embed_from={embed_from!r}: key {key!r} not found in schema dimensions "
                f"(available: {sorted(dimensions)})"
            )
        return
    raise SubspaceSchemaError(
        f"embed_from={embed_from!r} is not a valid form; "
        "valid forms: 'content', 'match_text', 'dimensions:<key>'"
    )


def _resolve_embed_text(
    embed_from: str,
    content: str,
    match_text: Optional[str],
    dimensions: dict[str, Any],
) -> str:
    """Resolve the embed text based on the schema's embed_from setting.

    Args:
        embed_from: Schema embed_from string (already validated).
        content: Tuple body text.
        match_text: Caller-supplied override (may be None).
        dimensions: Validated dimensions dict (already validated values).

    Returns:
        The text to embed in ChromaDB.
    """
    if embed_from == "content":
        return content
    if embed_from == "match_text":
        return match_text or content
    if embed_from.startswith("dimensions:"):
        key = embed_from[len("dimensions:"):]
        return str(dimensions[key])
    # Should not reach here after _validate_embed_from, but be defensive
    return content


def _validate_dimensions(
    schema: SubspaceSchema, dimensions: dict[str, Any]
) -> None:
    """Validate *dimensions* against the schema's declared dimension constraints.

    Checks:
    - Required dimensions are present and non-empty.
    - Enum-typed dimensions have a value in the declared ``values`` list.

    Args:
        schema: The subspace schema declaring dimension constraints.
        dimensions: The caller-supplied dimensions dict.

    Raises:
        SubspaceSchemaError: If any constraint is violated.
    """
    for dim_name, dim_spec in schema.dimensions.items():
        required = dim_spec.get("required", False)
        value = dimensions.get(dim_name)

        if required and (value is None or value == ""):
            raise SubspaceSchemaError(
                f"dimension {dim_name!r} is required but missing or empty "
                f"(subspace schema: {schema.name!r})"
            )

        if value is not None and dim_spec.get("type") == "enum":
            allowed = dim_spec.get("values", [])
            if value not in allowed:
                raise SubspaceSchemaError(
                    f"dimension {dim_name!r} value {value!r} is not in "
                    f"allowed values {allowed!r} for schema {schema.name!r}"
                )


# ---------------------------------------------------------------------------
# SQL helpers
# ---------------------------------------------------------------------------


def _select_candidates_sql(
    subspace: str,
    extra_where_fragments: list[str],
    limit: int = 1,
) -> tuple[str, list[Any]]:
    """Build the available-candidate SELECT for exact-mode take.

    Uses ``unixepoch()`` inline (not a bound ?) per the idx_tuples_avail
    partial-index contract in store.py.

    Args:
        subspace: Concrete subspace string.
        extra_where_fragments: List of SQL fragments to AND into the WHERE
            clause.  Each fragment must use only bound parameters appended
            to the returned params list.
        limit: Row limit (default 1 for take).

    Returns:
        (sql_string, params_list) tuple.
    """
    base_where = [
        "subspace = ?",
        "consumed_at IS NULL",
        "(claim_state IS NULL OR claim_expires_at < unixepoch())",
    ]
    all_where = base_where + extra_where_fragments
    sql = (
        f"SELECT id FROM tuples WHERE {' AND '.join(all_where)} "
        f"ORDER BY created_at LIMIT {limit}"
    )
    return sql, [subspace]


def _post_filter_ids(
    conn: sqlite3.Connection,
    candidate_ids: list[str],
) -> list[str]:
    """Post-filter chroma candidate IDs against the SQL claim/consumed state.

    Drops tuples that are consumed or actively claimed.  Uses ``unixepoch()``
    inline per the idx_tuples_avail partial-index contract.

    Args:
        conn: Open SQLite connection.
        candidate_ids: List of tuple IDs from the chroma query.

    Returns:
        Subset of *candidate_ids* that are still available.
    """
    if not candidate_ids:
        return []
    placeholders = ",".join("?" * len(candidate_ids))
    rows = conn.execute(
        f"SELECT id FROM tuples "
        f"WHERE id IN ({placeholders}) "
        f"  AND consumed_at IS NULL "
        f"  AND (claim_state IS NULL OR claim_expires_at < unixepoch())",
        candidate_ids,
    ).fetchall()
    available = {r[0] for r in rows}
    # Preserve chroma ordering
    return [cid for cid in candidate_ids if cid in available]


def _load_tuple_row(conn: sqlite3.Connection, tuple_id: str) -> dict[str, Any]:
    """Load a full tuple row as a dict from SQLite."""
    conn.row_factory = sqlite3.Row
    row = conn.execute("SELECT * FROM tuples WHERE id = ?", (tuple_id,)).fetchone()
    if row is None:
        return {}
    result = dict(row)
    # Parse dimensions_json for callers
    try:
        result["dimensions"] = json.loads(result.get("dimensions_json", "{}"))
    except (json.JSONDecodeError, TypeError):
        result["dimensions"] = {}
    return result


# ---------------------------------------------------------------------------
# Public API
# ---------------------------------------------------------------------------


def out(
    *,
    conn: sqlite3.Connection,
    index: TupleIndex,
    registry: Registry,
    subspace: str,
    content: str,
    dimensions: dict[str, Any],
    match_text: Optional[str] = None,
    ttl_seconds: Optional[float] = None,
) -> str:
    """Post (upsert) a tuple into the tuple space.

    The tuple_id is computed deterministically from (subspace, content,
    dimensions, match_text), so calling ``out`` twice with the same
    arguments is a no-op and returns the same ID.

    Args:
        conn: Open SQLite connection to tuples.db.
        index: ``TupleIndex`` wrapping ChromaDB collections.
        registry: Loaded ``Registry`` of subspace schemas.
        subspace: Concrete subspace string (e.g. ``"tasks/nexus"``).
        content: Tuple body text.
        dimensions: Dict of dimension values; validated against schema.
        match_text: Optional override for the embedding source.  Required when
            ``embed_from="match_text"`` in the schema.
        ttl_seconds: Optional TTL in seconds from now.  ``None`` means no expiry.

    Returns:
        The 32-hex tuple_id.

    Raises:
        UnknownSubspaceError: Subspace does not match any registered template.
        SubspaceSchemaError: Dimension values fail schema validation, or
            embed_from form is invalid.
    """
    schema = registry.get_schema_for(subspace)
    _validate_embed_from(schema.embed_from, schema.dimensions)
    _validate_dimensions(schema, dimensions)

    mt = match_text or ""
    tid = _tuple_id(subspace, content, dimensions, mt)
    embed_text = _resolve_embed_text(schema.embed_from, content, match_text, dimensions)
    dims_json = json.dumps(dimensions, sort_keys=True)
    now = time.time()
    # Resolve effective TTL: explicit ttl_seconds wins; otherwise fall back
    # to the subspace schema's retention_seconds (nexus-kk9h, RDR-111).
    # retention_seconds == 0 means "no expiry" — leave expires_at NULL so
    # the retention sweeper skips the row.
    effective_ttl: Optional[float] = ttl_seconds
    if effective_ttl is None:
        ret = getattr(schema, "retention_seconds", 0) or 0
        if ret > 0:
            effective_ttl = float(ret)
    expires_at = now + effective_ttl if effective_ttl is not None else None

    # Upsert into SQLite (INSERT OR IGNORE for idempotency — same tid = same content)
    conn.execute(
        "INSERT OR IGNORE INTO tuples "
        "(id, subspace, template_name, content, dimensions_json, embed_text, "
        " match_text, created_at, expires_at) "
        "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)",
        (tid, subspace, schema.name, content, dims_json, embed_text, mt or None, now, expires_at),
    )
    conn.commit()

    # Upsert into ChromaDB (idempotent by design)
    meta: dict[str, Any] = {"subspace": subspace}
    for k, v in dimensions.items():
        # ChromaDB metadata values must be str/int/float/bool
        meta[k] = v if isinstance(v, (str, int, float, bool)) else str(v)

    index.out(
        template_name=schema.name,
        subspace=subspace,
        tuple_id=tid,
        payload=embed_text,
        metadata=meta,
    )

    _log.debug("tuplespace_out_complete", subspace=subspace, tuple_id=tid)
    return tid


def read(
    *,
    conn: sqlite3.Connection,
    index: TupleIndex,
    registry: Registry,
    subspace: str,
    query: str,
    where: Optional[dict[str, Any]] = None,
    floor: Optional[float] = None,
    n: Optional[int] = None,
) -> list[dict[str, Any]]:
    """Read tuples semantically from a subspace (non-destructive).

    Returns available (non-consumed, non-claimed) tuples matching *query*
    above the configured *floor*, up to *n* results.

    Args:
        conn: Open SQLite connection to tuples.db.
        index: ``TupleIndex`` wrapping ChromaDB collections.
        registry: Loaded ``Registry`` of subspace schemas.
        subspace: Concrete subspace string.
        query: Semantic query text.
        where: Optional ChromaDB metadata filter dict.
        floor: Minimum similarity threshold (overrides schema default).
        n: Maximum results (overrides schema default).

    Returns:
        List of tuple dicts with keys ``id``, ``content``, ``dimensions``,
        ``subspace``, ``created_at``, ``embed_text``, ``match_text``.
        Empty list if no matches.

    Raises:
        UnknownSubspaceError: Subspace does not match any registered template.
    """
    schema = registry.get_schema_for(subspace)
    effective_floor = floor if floor is not None else schema.read["default_floor"]
    effective_n = n if n is not None else schema.read["default_n"]

    # Clamp n_results to chroma quota maximum
    from nexus.db.chroma_quotas import QUOTAS
    effective_n = min(effective_n, QUOTAS.MAX_QUERY_RESULTS)

    chroma_results = index.read(
        template_name=schema.name,
        subspace=subspace,
        query=query,
        where=where,
        n_results=effective_n,
    )

    # Apply floor filter
    candidate_ids: list[str] = [
        r["id"]
        for r in chroma_results
        if (1.0 - r["distance"]) >= effective_floor
    ]

    # Post-filter against SQL availability (consumed_at / claim_state)
    available_ids = _post_filter_ids(conn, candidate_ids)

    if not available_ids:
        return []

    # Load full rows from SQLite
    placeholders = ",".join("?" * len(available_ids))
    conn.row_factory = sqlite3.Row
    rows = conn.execute(
        f"SELECT * FROM tuples WHERE id IN ({placeholders})",
        available_ids,
    ).fetchall()

    id_to_row = {r["id"]: dict(r) for r in rows}

    result = []
    for tid in available_ids:
        row = id_to_row.get(tid)
        if row is None:
            continue
        try:
            row["dimensions"] = json.loads(row.get("dimensions_json", "{}"))
        except (json.JSONDecodeError, TypeError):
            row["dimensions"] = {}
        result.append(row)

    return result


def take(
    *,
    conn: sqlite3.Connection,
    index: TupleIndex,
    registry: Registry,
    subspace: str,
    query: str,
    claimant: str,
    where: Optional[dict[str, Any]] = None,
    floor: Optional[float] = None,
    lease_seconds: Optional[float] = None,
    block: bool = False,
    timeout_seconds: Optional[float] = None,
) -> Optional[tuple[dict[str, Any], str]]:
    """Atomically claim a tuple from a subspace (destructive read).

    Uses a single-statement CAS ``UPDATE ... WHERE id = (SELECT ... LIMIT 1)
    RETURNING id`` for atomicity under SQLite's single-writer lock.  Two
    concurrent claimants cannot both succeed on the same row.

    **Idempotent retake by same claimant**: if the same *claimant* already
    holds an active claim (not expired) on a candidate, returns the existing
    claim_id without a new UPDATE.

    Args:
        conn: Open SQLite connection to tuples.db.
        index: ``TupleIndex`` wrapping ChromaDB collections.
        registry: Loaded ``Registry`` of subspace schemas.
        subspace: Concrete subspace string.
        query: Semantic query (unused in exact mode).
        claimant: Unique identifier for the claiming agent.
        where: Optional filter dict.  In ``exact`` mode, must contain all
            ``match_keys`` declared in the schema.
        floor: Minimum similarity threshold (overrides schema default).
        lease_seconds: Lease duration in seconds (overrides schema default).
        block: Must be False in Phase 1 (raises ``BlockingNotSupported``).
        timeout_seconds: Maximum wait duration (only meaningful when block=True;
            capped at 30 s per MCP transport budget).

    Returns:
        ``(tuple_dict, claim_id)`` if successful, ``None`` if no candidate
        was available.

    Raises:
        UnknownSubspaceError: Subspace does not match any registered template.
        TakeDisabledError: The schema declares ``take.enabled=False``.
        BlockingNotSupported: *block=True* in Phase 1 direct mode.
        SubspaceSchemaError: Exact-mode *where* is missing a required
            ``match_key``.
        InvalidTimeoutError: *timeout_seconds* > 30.
    """
    if block:
        raise BlockingNotSupported(
            "block=True is not supported in Phase 1 direct-mode (RDR-112 §A2). "
            "The daemon-mode block path lands in Phase 3."
        )

    if timeout_seconds is not None and timeout_seconds > 30:
        raise InvalidTimeoutError(
            f"timeout_seconds={timeout_seconds} exceeds the MCP transport budget cap of 30 s "
            "(RDR-110 §Technical Design)"
        )

    schema = registry.get_schema_for(subspace)

    if not schema.take.get("enabled", False):
        raise TakeDisabledError(
            f"take is disabled for subspace {subspace!r} "
            f"(schema: {schema.name!r})"
        )

    mode = schema.take.get("mode", "semantic")
    effective_floor = floor if floor is not None else schema.take.get("floor", 0.0)
    effective_margin = schema.take.get("margin", 0.0)
    effective_lease = lease_seconds if lease_seconds is not None else schema.take.get(
        "default_lease_seconds", 60
    )

    # Candidate selection
    top_ids: list[str] = []

    if mode == "exact":
        # Bypass chroma; select purely on match_keys + SQL availability.
        match_keys: list[str] = schema.take.get("match_keys", [])
        for k in match_keys:
            if not where or k not in where:
                raise SubspaceSchemaError(
                    f"take mode=exact requires match_key {k!r} in where; "
                    f"schema: {schema.name!r}, match_keys={match_keys!r}"
                )

        # Dimensions are stored as JSON in dimensions_json; use json_extract.
        # Column names (match_keys) come from the schema, never from the caller,
        # so interpolating them into the fragment is safe.
        fragments = [f"json_extract(dimensions_json, '$.{k}') = ?" for k in match_keys]
        match_values = [where[k] for k in match_keys]
        sql, base_params = _select_candidates_sql(subspace, fragments, limit=1)
        params = base_params + match_values
        rows = conn.execute(sql, params).fetchall()
        top_ids = [r[0] for r in rows]

    else:
        # Semantic mode: chroma top-K -> floor -> margin -> candidate IDs
        from nexus.db.chroma_quotas import QUOTAS
        n_query = min(max(5, schema.read.get("default_n", 1)), QUOTAS.MAX_QUERY_RESULTS)

        chroma_results = index.read(
            template_name=schema.name,
            subspace=subspace,
            query=query,
            where=where,
            n_results=n_query,
        )

        if chroma_results:
            best = chroma_results[0]
            best_similarity = 1.0 - best["distance"]

            if best_similarity >= effective_floor:
                # Margin check: only need margin if there's a second candidate
                if len(chroma_results) == 1:
                    margin_ok = True
                else:
                    second_similarity = 1.0 - chroma_results[1]["distance"]
                    margin_ok = (best_similarity - second_similarity) >= effective_margin

                if margin_ok:
                    top_ids = [
                        r["id"]
                        for r in chroma_results
                        if (1.0 - r["distance"]) >= effective_floor
                    ]

    # Synchronisation point for CAS race test (no-op in production)
    if _take_pre_update_hook is not None:
        _take_pre_update_hook()

    if not top_ids:
        return None

    now = time.time()
    expires_at = now + effective_lease

    # --- Idempotent retake by same claimant (two-statement read-then-update) ---
    # The CAS pattern below would see claim_state='claimed' AND claim_expires_at>now
    # for the same claimant's live claim and return no row — indistinguishable from
    # a foreign-claimant loss.  Handle it explicitly first.
    placeholders = ",".join("?" * len(top_ids))
    existing = conn.execute(
        f"SELECT id, claim_id FROM tuples "
        f"WHERE id IN ({placeholders}) "
        f"  AND claim_state = 'claimed' "
        f"  AND claimant = ? "
        f"  AND claim_expires_at > ?",
        top_ids + [claimant, now],
    ).fetchone()

    if existing is not None:
        # Same claimant still holds the lease — return existing claim
        t_dict = _load_tuple_row(conn, existing[0])
        _log.debug(
            "tuplespace_take_idempotent_retake",
            subspace=subspace,
            claimant=claimant,
            tuple_id=existing[0],
        )
        return t_dict, existing[1]

    # --- Single-statement CAS ---
    # Portable form: LIMIT 1 in the inner SELECT (universally supported).
    # LIMIT directly in UPDATE requires SQLITE_ENABLE_UPDATE_DELETE_LIMIT
    # which CPython's stdlib sqlite3 does NOT set.
    cid = _claim_id(claimant, top_ids[0], now)
    row = conn.execute(
        f"UPDATE tuples "
        f"SET claim_state='claimed', claimant=?, claim_id=?, claim_expires_at=? "
        f"WHERE id = ( "
        f"    SELECT id FROM tuples "
        f"    WHERE id IN ({placeholders}) "
        f"      AND consumed_at IS NULL "
        f"      AND (claim_state IS NULL OR claim_expires_at < unixepoch()) "
        f"    ORDER BY created_at "
        f"    LIMIT 1 "
        f") "
        f"RETURNING id",
        [claimant, cid, expires_at] + top_ids,
    ).fetchone()

    if row is None:
        # All candidates raced away — no winner
        return None

    conn.execute(
        "INSERT INTO tuple_claim_log (tuple_id, subspace, claim_id, claimant, transition, at) "
        "VALUES (?, ?, ?, ?, 'claim', ?)",
        (row[0], subspace, cid, claimant, now),
    )
    conn.commit()

    t_dict = _load_tuple_row(conn, row[0])
    _log.debug(
        "tuplespace_take_claimed",
        subspace=subspace,
        claimant=claimant,
        tuple_id=row[0],
        claim_id=cid,
    )
    return t_dict, cid


def ack(
    *,
    conn: sqlite3.Connection,
    claim_id: str,
    claimant: str,
) -> None:
    """Acknowledge a claim: mark the tuple consumed and log the transition.

    Args:
        conn: Open SQLite connection to tuples.db.
        claim_id: The claim ID returned by ``take``.
        claimant: The claiming agent; must match the original claimant.

    Raises:
        ClaimNotFoundError: No claim with *claim_id* exists.
        ClaimOwnershipError: *claimant* does not own the claim.
    """
    row = conn.execute(
        "SELECT id, claimant, subspace FROM tuples WHERE claim_id = ? AND claim_state = 'claimed'",
        (claim_id,),
    ).fetchone()

    if row is None:
        raise ClaimNotFoundError(
            f"No active claim found for claim_id={claim_id!r}"
        )

    if row[1] != claimant:
        raise ClaimOwnershipError(
            f"Claim {claim_id!r} is owned by {row[1]!r}, not {claimant!r}"
        )

    now = time.time()
    conn.execute(
        "UPDATE tuples "
        "SET consumed_at=?, consumed_by=?, claim_state='acked' "
        "WHERE claim_id=?",
        (now, claimant, claim_id),
    )
    conn.execute(
        "INSERT INTO tuple_claim_log (tuple_id, subspace, claim_id, claimant, transition, at) "
        "VALUES (?, ?, ?, ?, 'ack', ?)",
        (row[0], row[2], claim_id, claimant, now),
    )
    conn.commit()
    _log.debug("tuplespace_ack", tuple_id=row[0], claim_id=claim_id, claimant=claimant)


def nack(
    *,
    conn: sqlite3.Connection,
    claim_id: str,
    claimant: str,
) -> None:
    """Negative-acknowledge a claim: release it back to available and log.

    Args:
        conn: Open SQLite connection to tuples.db.
        claim_id: The claim ID returned by ``take``.
        claimant: The claiming agent; must match the original claimant.

    Raises:
        ClaimNotFoundError: No active claim with *claim_id* exists.
        ClaimOwnershipError: *claimant* does not own the claim.
    """
    row = conn.execute(
        "SELECT id, claimant, subspace FROM tuples WHERE claim_id = ? AND claim_state = 'claimed'",
        (claim_id,),
    ).fetchone()

    if row is None:
        raise ClaimNotFoundError(
            f"No active claim found for claim_id={claim_id!r}"
        )

    if row[1] != claimant:
        raise ClaimOwnershipError(
            f"Claim {claim_id!r} is owned by {row[1]!r}, not {claimant!r}"
        )

    now = time.time()
    conn.execute(
        "UPDATE tuples "
        "SET claim_state=NULL, claimant=NULL, claim_id=NULL, claim_expires_at=NULL "
        "WHERE claim_id=?",
        (claim_id,),
    )
    conn.execute(
        "INSERT INTO tuple_claim_log (tuple_id, subspace, claim_id, claimant, transition, at) "
        "VALUES (?, ?, ?, ?, 'nack', ?)",
        (row[0], row[2], claim_id, claimant, now),
    )
    conn.commit()
    _log.debug("tuplespace_nack", tuple_id=row[0], claim_id=claim_id, claimant=claimant)


def list_subspaces(*, registry: Registry) -> list[str]:
    """Return all registered subspace template names.

    Args:
        registry: Loaded ``Registry`` of subspace schemas.

    Returns:
        List of template name strings (e.g. ``["tasks/<project>", "locks/<resource>"]``).
    """
    return [s.name for s in registry.schemas()]


def subspace_schema(*, registry: Registry, subspace: str) -> dict[str, Any]:
    """Return the schema dict for a subspace.

    Args:
        registry: Loaded ``Registry`` of subspace schemas.
        subspace: Concrete or template subspace string.

    Returns:
        Dict with keys: name, tier, content_type, embed_from, dimensions,
        take, read, tiers, retention_seconds.

    Raises:
        UnknownSubspaceError: Subspace does not match any registered template.
    """
    schema = registry.get_schema_for(subspace)
    return {
        "name": schema.name,
        "tier": schema.tier,
        "content_type": schema.content_type,
        "embed_from": schema.embed_from,
        "dimensions": schema.dimensions,
        "take": dict(schema.take),
        "read": dict(schema.read),
        "tiers": schema.tiers,
        "retention_seconds": schema.retention_seconds,
    }


def subspace_stats(*, conn: sqlite3.Connection, subspace: str) -> dict[str, Any]:
    """Return aggregate counts for a concrete subspace.

    Args:
        conn: Open SQLite connection to tuples.db.
        subspace: Concrete subspace string (e.g. ``"tasks/nexus"``).

    Returns:
        Dict with keys: total, available, claimed, consumed.
    """
    row = conn.execute(
        "SELECT "
        "  COUNT(*) as total, "
        "  SUM(CASE WHEN consumed_at IS NULL AND (claim_state IS NULL OR claim_expires_at < unixepoch()) THEN 1 ELSE 0 END) as available, "
        "  SUM(CASE WHEN claim_state='claimed' AND claim_expires_at >= unixepoch() THEN 1 ELSE 0 END) as claimed, "
        "  SUM(CASE WHEN consumed_at IS NOT NULL THEN 1 ELSE 0 END) as consumed "
        "FROM tuples WHERE subspace = ?",
        (subspace,),
    ).fetchone()

    if row is None:
        return {"total": 0, "available": 0, "claimed": 0, "consumed": 0}

    return {
        "total": row[0] or 0,
        "available": row[1] or 0,
        "claimed": row[2] or 0,
        "consumed": row[3] or 0,
    }
