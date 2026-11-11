# SPDX-License-Identifier: AGPL-3.0-or-later
"""The wire contract of ``nx doctor``'s bounded engine routes (nexus-f3yqd.6): the 0.1.189 stub-metadata and chash
conformance bodies, and the 0.1.190 census routes (``GET /v1/catalog/index-run/census``, ``GET
/v1/catalog/owners/next_seq_drift``). Constants, body validators and readers, kept out of the over-cap
``http_catalog_client`` module, whose methods call them."""
from __future__ import annotations

from collections.abc import Callable
from typing import Any

import structlog

_log = structlog.get_logger(__name__)

#: The index-run census read's client timeout (engine 0.1.190, nexus-f3yqd.6). The engine runs its three buckets in
#: series, each under the smaller of its own 10 s bound and what is left of one 25 s budget for the call
#: (``IndexRunCensusReads.CALL_BUDGET_MS``), so it answers inside about 25 s, a bucket out of time as ``timed_out``.
#: This is above that and under the public edge's roughly 30 s cut. A cut, by the edge or by this timeout, is an error
#: to the caller, and the doctor then reports all three rows not checked.
INDEX_RUN_CENSUS_TIMEOUT_S = 28.0
#: The next_seq drift read's client timeout. The engine checks a call-level 10 s deadline before each owner probe and
#: re-arms a 10 s ``statement_timeout`` for each one, so its worst case is about 20 s; this is above that and under
#: the public edge's cut. A cut is an error to the caller: the drift row is then reported not checked.
NEXT_SEQ_DRIFT_TIMEOUT_S = 25.0
#: The census the client reads is the one for these constants, which the engine echoes (``threshold_s``, ``floor``):
#: a 6 h stale-fence threshold and the release of full producer coverage. An engine that echoes others computes a
#: different census than this client's rules assume, so its answer is read as "cannot say".
INDEX_RUN_STALE_THRESHOLD_S = 6 * 3600
INDEX_RUN_FENCE_FLOOR_ISO = "2026-08-07T16:00:35Z"
#: :meth:`HttpCatalogClient.index_run_census`'s answer when the engine echoed a well-formed ``threshold_s`` or ``floor``
#: that is not this client's: the engine and the client are from different releases, and the doctor reports the rows
#: not checked, with that reason, instead of "did not answer".
INDEX_RUN_CENSUS_RULES_DIFFER = object()
#: The most documents one bucket of the census lists or counts, and the most examples the drift read lists: the
#: engine's ``IndexRunCensusReads.MAX_LIMIT`` and ``NextSeqDriftReads.MAX_LIMIT``. A caller that wants the whole
#: answer asks for this much; a ``capped`` answer is then a floor, never a count.
INDEX_RUN_CENSUS_MAX_LIMIT = 10_000


def _is_count(v: object) -> bool:
    return isinstance(v, int) and not isinstance(v, bool) and v >= 0


def _is_text_or_null(v: object) -> bool:
    return v is None or isinstance(v, str)


def _is_instant(v: object) -> bool:
    """A string the doctor can read as a time (the engine writes ISO-8601 instants, ``Z`` suffix)."""
    if not isinstance(v, str) or not v:
        return False
    from datetime import datetime  # noqa: PLC0415 — only these validators parse a time

    try:
        datetime.fromisoformat(v.replace("Z", "+00:00"))
    except ValueError:
        return False
    return True


def _instant(v: str) -> Any:
    from datetime import datetime  # noqa: PLC0415 — only these validators parse a time

    return datetime.fromisoformat(v.replace("Z", "+00:00"))


def _rows_ok(rows: object, ok: Any) -> bool:
    return isinstance(rows, list) and all(isinstance(r, dict) and ok(r) for r in rows)


def _valid_stale_bucket(b: dict) -> bool:
    return (_is_count(b.get("count")) and isinstance(b.get("capped"), bool) and _is_count(b.get("notes"))
            and _rows_ok(b.get("examples"), lambda e: (
                isinstance(e.get("tumbler"), str) and _is_text_or_null(e.get("source_uri"))
                and _is_count(e.get("age_s")) and isinstance(e.get("note"), bool)
                and _is_text_or_null(e.get("started_at"))))
            and len(b["examples"]) <= b["count"])


def _valid_fence_bucket(b: dict) -> bool:
    anchor, post = b.get("anchor"), b.get("post_anchor")
    if not (isinstance(anchor, dict) and isinstance(post, dict)):
        return False
    kind = anchor.get("kind")
    if kind not in (None, "proven", "earliest") or not (_is_instant(anchor.get("at")) if kind else anchor.get("at") is None):
        return False
    return (all(_is_count(b.get(k)) for k in ("checked", "reported_null", "undated_reported_null", "floor_candidates"))
            and _is_count(post.get("count")) and isinstance(post.get("capped"), bool)
            and _rows_ok(post.get("examples"), lambda e: (
                isinstance(e.get("tumbler"), str) and _is_text_or_null(e.get("source_uri"))
                and _is_text_or_null(e.get("file_path")) and _is_instant(e.get("indexed_at"))))
            and len(post["examples"]) <= post["count"])


def _valid_failed_hidden_bucket(b: dict) -> bool:
    failed = b.get("failed")
    return (isinstance(failed, dict) and _is_count(failed.get("count")) and isinstance(failed.get("capped"), bool)
            and _is_count(b.get("count")) and isinstance(b.get("capped"), bool)
            and b["count"] <= failed["count"]
            and _rows_ok(b.get("examples"), lambda e: (
                isinstance(e.get("tumbler"), str) and _is_text_or_null(e.get("source_uri"))
                and _is_count(e.get("chunk_count")) and _is_count(e.get("chunks_stored"))))
            and len(b["examples"]) <= b["count"])


def _census_rules_differ(body: object) -> bool:
    """True when *body* echoes a well-formed ``threshold_s`` and ``floor`` and either is not this client's. A body
    that echoes neither, or a malformed one, is not this case (it is malformed)."""
    if not isinstance(body, dict):
        return False
    threshold, floor = body.get("threshold_s"), body.get("floor")
    if not (_is_count(threshold) and _is_instant(floor)):
        return False
    return threshold != INDEX_RUN_STALE_THRESHOLD_S or _instant(floor) != _instant(INDEX_RUN_FENCE_FLOOR_ISO)


def _valid_index_run_census(body: object) -> dict | None:
    """The census body, or ``None`` when it is not the documented shape. A bucket is the engine's object or exactly
    ``{"timed_out": true}``; one malformed bucket makes the whole body malformed (a half-read census is not read). The
    echoed ``threshold_s`` and ``floor`` must be this client's own (:data:`INDEX_RUN_STALE_THRESHOLD_S`,
    :data:`INDEX_RUN_FENCE_FLOOR_ISO`)."""
    if not isinstance(body, dict):
        return None
    threshold, floor = body.get("threshold_s"), body.get("floor")
    if not (_is_count(threshold) and threshold == INDEX_RUN_STALE_THRESHOLD_S and _is_instant(floor)
            and _instant(floor) == _instant(INDEX_RUN_FENCE_FLOOR_ISO)):
        return None
    census: dict = {}
    for name, valid in (("stale", _valid_stale_bucket), ("fence", _valid_fence_bucket),
                        ("failed_hidden", _valid_failed_hidden_bucket)):
        bucket = body.get(name)
        if not isinstance(bucket, dict):
            return None
        if bucket == {"timed_out": True}:
            census[name] = {"timed_out": True}
        elif "timed_out" in bucket or not valid(bucket):
            return None
        else:
            census[name] = bucket
    return census


def _valid_next_seq_drift(body: object) -> dict | None:
    if not isinstance(body, dict):
        return None
    examples = body.get("examples")
    if not (_is_count(body.get("checked")) and _is_count(body.get("drifted")) and isinstance(body.get("capped"), bool)
            and _rows_ok(examples, lambda e: (
                isinstance(e.get("owner"), str) and bool(e["owner"]) and _is_count(e.get("next_seq"))
                and _is_count(e.get("highest_child_seq")) and e["highest_child_seq"] > e["next_seq"]))):
        return None
    if len(examples) > body["drifted"] or body["drifted"] > body["checked"]:
        return None
    return {"checked": body["checked"], "drifted": body["drifted"], "capped": body["capped"], "examples": examples}


def read_index_run_census(http_get: Callable[..., Any], flags: dict, limit: int = INDEX_RUN_CENSUS_MAX_LIMIT) -> dict | object | None:
    """The engine's bounded census of the index-run fence (``GET /v1/catalog/index-run/census``, engine 0.1.190,
    nexus-f3yqd.6): ``{"stale": ..., "fence": ..., "failed_hidden": ...}``, each bucket either the engine's object
    (see ``IndexRunCensusReads``) or ``{"timed_out": True}`` when that bucket could not be read in time (the
    others still answer). A bucket's ``capped`` means more exist than *limit*, so its count reads "at least".

    ``None`` means "cannot say", never "none found": the engine does not advertise
    ``catalog_index_run_census_supported`` (no request is sent then), the route answers 403, 404, 503 or any
    other error, the request fails, or the body is not the shape the engine documents. A body whose echoed
    ``threshold_s`` or ``floor`` is well formed but not this client's answers :data:`INDEX_RUN_CENSUS_RULES_DIFFER`:
    also "cannot say", with its reason.
    """
    if flags.get("catalog_index_run_census_supported") is not True:
        return None
    try:
        body = http_get("/index-run/census", idempotent=False, timeout=INDEX_RUN_CENSUS_TIMEOUT_S, limit=limit)
    except Exception as exc:  # noqa: BLE001 — best-effort read of an optional route; the caller reports not checked
        _log.info("http_catalog_client.index_run_census_unavailable", error=str(exc))
        return None
    if _census_rules_differ(body):
        _log.warning("http_catalog_client.index_run_census_rules_differ")
        return INDEX_RUN_CENSUS_RULES_DIFFER
    census = _valid_index_run_census(body)
    if census is None:
        _log.warning("http_catalog_client.index_run_census_malformed")
    return census


def read_next_seq_drift(http_get: Callable[..., Any], flags: dict, limit: int = INDEX_RUN_CENSUS_MAX_LIMIT) -> dict | None:
    """The owners whose ``next_seq`` trails their highest child, read without flooring them (``GET
    /v1/catalog/owners/next_seq_drift``, engine 0.1.190, nexus-f3yqd.6): ``{"checked", "drifted", "capped",
    "examples": [{"owner", "next_seq", "highest_child_seq"}]}``. ``drifted`` counts every drifted owner;
    ``examples`` lists at most *limit* of them and ``capped`` says it holds fewer than ``drifted``.

    ``None`` means "cannot say": no ``catalog_next_seq_drift_read_supported`` flag (no request is sent), a 403, 404
    or 503, any other error, or a malformed body.
    """
    if flags.get("catalog_next_seq_drift_read_supported") is not True:
        return None
    try:
        body = http_get("/owners/next_seq_drift", idempotent=False, timeout=NEXT_SEQ_DRIFT_TIMEOUT_S, limit=limit)
    except Exception as exc:  # noqa: BLE001 — best-effort read of an optional route; the caller reports not checked
        _log.info("http_catalog_client.next_seq_drift_unavailable", error=str(exc))
        return None
    drift = _valid_next_seq_drift(body)
    if drift is None:
        _log.warning("http_catalog_client.next_seq_drift_malformed")
    return drift


def chash_body_validated(body: object) -> bool | None:
    """:meth:`HttpCatalogClient.chash_constraints_validated`'s reading of a ``GET /v1/schema/chash-conformance`` body:
    ``True`` only when both poison tables are listed and every constraint is present, validated and of the expected
    definition with ``all_validated`` true; ``False`` for any other well-formed answer; ``None`` for a malformed one."""
    if not isinstance(body, dict) or not isinstance(body.get("all_validated"), bool) \
            or not isinstance(body.get("constraints"), list):
        return None
    constraints = body["constraints"]
    if not all(isinstance(c, dict) and isinstance(c.get("table"), str) for c in constraints):
        return None
    from nexus.db.chash_tables import POISON_CHASH_TABLES  # noqa: PLC0415 — deferred to avoid circular import

    named = {c.get("table") for c in constraints}
    return (body["all_validated"] is True and {t.table for t in POISON_CHASH_TABLES} <= named
            and all(c.get("present") is True and c.get("validated") is True and c.get("definition_matches") is True
                    for c in constraints))


def parse_stub_metadata(result: object) -> list[dict] | None:
    """:meth:`HttpCatalogClient.stub_metadata`'s reading of a ``GET /v1/catalog/stub-metadata`` body: the rows, each a
    count or ``{"collection", "timed_out": True}``, or ``None`` when any row is malformed."""
    collections = result.get("collections") if isinstance(result, dict) else None

    def _row_ok(r: object) -> bool:
        if not isinstance(r, dict):
            return False
        if r.get("timed_out") is True:
            return isinstance(r.get("collection"), str)
        n = r.get("stub_chunks")
        return (isinstance(r.get("collection"), str) and not isinstance(n, bool) and isinstance(n, int) and n >= 0
                and isinstance(r.get("capped"), bool) and isinstance(r.get("examples"), list)
                and all(isinstance(e, str) for e in r["examples"]))

    if not isinstance(collections, list) or not all(_row_ok(r) for r in collections):
        _log.warning("http_catalog_client.stub_metadata_malformed")
        return None
    return [{"collection": r["collection"], "timed_out": True} if r.get("timed_out") is True
            else {"collection": r["collection"], "stub_chunks": r["stub_chunks"], "capped": r["capped"],
                  "examples": list(r["examples"])} for r in collections]
