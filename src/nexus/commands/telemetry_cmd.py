# SPDX-License-Identifier: AGPL-3.0-or-later
# Copyright (c) 2026 Hal Hildebrand. All rights reserved.
"""``nx telemetry baseline`` — the shakedown playbook's §4.5 fixed-shape
telemetry baseline snapshot (nexus-v0x32).

Playbook §4.5 (T2 ``nexus/shakedown-playbook`` [21398]) makes this snapshot
mandatory every shakedown and it is the data-collection precondition for
the longitudinal telemetry study (nexus-1h112). Until this command existed
it was hand-assembled at 2026-08-04, -11, -19, and -27 by composing several
existing readers by hand each time — this reproduces that composition as
one command, in the exact fixed shape the 2026-08-27 capture (T2 [23596]
§ TELEMETRY BASELINE) established, so successive runs are DIFFABLE rather
than re-derived:

  "SAME QUERIES, SAME BUCKETS, EVERY TIME. A baseline whose shape changes
  between runs is not a baseline."

This command does not compute anything new — it composes seven existing
readers (plus one brand-new engine route, ``GET /v1/telemetry/relevance/
stats``, added alongside it for exactly this baseline):

1. nx_answer runs           — ``HttpTelemetryStore.query_nx_answer_runs``
                               (``nx answer-runs``'s own reader). Carries
                               ``oldest_created_at``/``newest_created_at``
                               (fix round 1, nexus-v0x32): this pair IS the
                               instrument behind the 08-27 capture's
                               headline "zero rows since <ts>" finding —
                               dropping it after fetching it silently
                               disabled that detector.
2. Tier writes               — ``HttpTelemetryStore.query_tier_writes``
                               (``nx tier-status``'s own reader).
3. relevance_log              — ``HttpTelemetryStore.get_relevance_stats``
                               (NEW, this bead) for count/oldest/newest —
                               THE substrate-direct telemetry figure in
                               this baseline (a server-side SQL
                               count/min/max, not a client aggregation) —
                               plus ``get_retention_markers`` (existing,
                               nexus-24p05) for the cumulative-deletes
                               retention marker.
4. search_telemetry global    — looping ``HttpTelemetryStore.
                               query_collection_stats`` over every T3
                               collection (``make_t3().list_collections()``)
                               and summing ``row_count`` — the 2026-08-27
                               capture's "no global reader" gap, closed
                               with NO NEW CODE by composing two readers
                               that already existed (``nx collection
                               health`` does the identical enumeration).
                               Structurally a LOWER BOUND (fix round 1):
                               a collection absent from ``list_collections()``
                               is invisible independent of any per-call
                               error, so the caveat renders unconditionally,
                               not only when ``errors > 0``. Carries
                               per-collection ``zero_hit_rate`` (already
                               returned by every ``query_collection_stats``
                               call the loop makes — no extra round trip).
5. Drop meter                 — ``nexus.dropped_writes.count_drops``.
6. Consent                    — the literal retirement row (nexus-lqqb2,
                               2026-08-28): the consent-audit writer family
                               was deleted as dead wire in the SAME session
                               this bead was designed in. Never omitted,
                               never rendered UNAVAILABLE, never wrapped in
                               a window — retirement is a known, permanent
                               fact, not a windowed read.
7. Substrate cross-check      — ``catalog_stats.doc_count`` via a catalog
                               reader's ``stats()`` (the same figure
                               reconcile-stale's own "Substrate anchor"
                               uses). Fix round 1: relabeled explicitly as
                               CONTEXT, not a telemetry anchor — it is a
                               catalog metric (count(*) over
                               catalog_documents) unrelated to any
                               telemetry aggregate in this block; figure 3
                               (relevance_log's count) is the baseline's
                               actual substrate-direct telemetry figure.
                               Kept because it still satisfies playbook
                               §4.6's letter (one engine-side SQL count),
                               now under an honest label.

RULES (playbook §4.5, followed exactly):

- A figure that cannot be captured renders as the literal string
  ``"UNAVAILABLE: <reason>"`` in place of its normal value — never omitted,
  never a fabricated 0/null indistinguishable from a genuine empty result.
  ``--json`` keeps every key present regardless; only the VALUE changes
  shape (int/dict -> str) on failure.
- Bucket edges/names for nx_answer_runs are ``answer_runs.py``'s own
  ``_BUCKET_ORDER`` — one definition, imported here, never re-derived.
- WINDOW SCOPING (fix round 1, nexus-v0x32): ``--since`` applies ONLY to
  nx_answer_runs and tier_writes — every other figure is always
  whole-tenant/all-time. Every figure (except the consent literal, which
  is not a windowed read at all) carries its OWN ``"window"`` key in
  ``--json`` — ``{"since": <iso>}`` when scoped, the literal string
  ``"all-time"`` otherwise — and the text form prints the window on every
  line. No figure may imply a window it does not honour. There is no
  single top-level ``since`` key any more; only ``captured_at``.
- The text form is one line per figure (diffable); ``--json`` is the same
  data as a nested dict, plus ``captured_at``.
"""
from __future__ import annotations

import json as _json
from datetime import UTC, datetime
from typing import Any

import click
import structlog

_log = structlog.get_logger(__name__)

#: nexus-lqqb2 (2026-08-28): the consent-audit telemetry writer family
#: (record_consent/list_consents, both routes+handlers) was deleted as dead
#: wire in the same session this bead was designed. This is a PERMANENT,
#: known fact recorded every run — never UNAVAILABLE, never omitted, never
#: windowed. The exact literal the design of record specifies; do not reword.
_CONSENT_RETIRED = "RETIRED (nexus-lqqb2, 2026-08-28)"

#: search_telemetry's per-collection reader (``query_collection_stats``) is
#: windowed by ``days``; the §4.5 "global count" figure wants effectively
#: ALL recorded rows, not a rolling window, so this horizon is large enough
#: to be a de facto "since table inception" cutoff (~100 years) without
#: requiring a second, unwindowed engine route.
_SEARCH_TELEMETRY_ALL_TIME_DAYS = 36_500

#: Fix round 1: how many worst (highest) per-collection zero_hit_rate
#: entries the text form prints, matching the 08-27 capture's own
#: vocabulary ("zero_hit_rate 0.524 knowledge__dt-papers, 0.325
#: knowledge__knowledge" — exactly two).
_ZERO_HIT_RATE_WORST_N = 2

#: RDR-200 consumer caveat (critic-F2, T2 [23952], population sweep
#: item 6): this figure reads the engine's RAW ``hit_count``/
#: ``fallback_count``/``total``/``since_count`` aggregates, which have
#: ZERO visibility into the RDR-200 continuation markers by design (F6,
#: zero engine change — nothing was ever taught to the query route
#: about them). Two concrete distortions, confirmed by static reasoning
#: against the engine's own documented predicate
#: (``http_telemetry_store.py``'s hit/fallback split, ``plan_id IS NULL
#: OR plan_id==0 => fallback``): every ``nx_answer_report`` completion
#: row (``plan_id=None``, verified in the diff) is misclassified
#: server-side as an inline-planner fallback, inflating
#: ``fallback_count``; and a single logical continuation call
#: contributes TWO physical rows (handoff + report) to ``total``/
#: ``since_count`` where a normal call contributes one. Client-side
#: text only — the engine is not, and by RDR-200's own Phase 1-2
#: constraint cannot be, changed to fix this. Unconditional (this
#: figure has no per-row visibility to gate the caveat on presence/
#: absence in the current window), same "always-labeled" doctrine as
#: ``search_telemetry``'s LOWER BOUND caveat just below it.
_NX_ANSWER_RUNS_CONTINUATION_CAVEAT = (
    "hit/fallback/total/since_count above have ZERO visibility into "
    "RDR-200 continuation rows (zero engine change): every "
    "nx_answer_report completion row is misclassified server-side as "
    "an inline-planner fallback, and a paired continuation call "
    "contributes 2 physical rows for 1 logical nx_answer invocation — "
    "see `nx answer-runs` for a marker-aware four-way split instead"
)


def _unavailable(reason: str) -> str:
    return f"UNAVAILABLE: {reason}"


def _window(since: str | None) -> dict[str, str] | str:
    """The per-figure window marker (fix round 1): ``{"since": since}``
    when the figure is scoped to ``--since``, else the literal string
    ``"all-time"`` — printed/serialized on EVERY figure so a reader can
    never mistake an unscoped figure for a scoped one or vice versa."""
    return {"since": since} if since else "all-time"


def _make_store() -> tuple[Any, Exception | None]:
    """Construct ONE ``HttpTelemetryStore`` for the whole baseline capture
    (code-review suggestion, fix round 1). Construction itself performs no
    I/O — it only resolves ``NX_SERVICE_HOST``/``PORT``/``TOKEN`` — so
    sharing one instance across the five store-dependent figures changes
    nothing about per-route failure semantics; it just avoids five
    redundant reconstructions. Returns ``(store, None)`` on success or
    ``(None, exc)`` on failure, so every store-dependent figure degrades
    to the SAME honest reason instead of five independent reconstructions
    each hitting and reporting the identical error."""
    try:
        from nexus.db.t2.http_telemetry_store import HttpTelemetryStore  # noqa: PLC0415 - deferred: heavy import, keep CLI startup fast

        return HttpTelemetryStore(), None
    except Exception as exc:  # noqa: BLE001 — boundary catch; caller degrades every store-dependent figure honestly
        _log.debug("telemetry_baseline_store_construction_failed", exc_info=True)
        return None, exc


def _capture_nx_answer_runs(since: str | None, store: Any, store_error: Exception | None) -> dict[str, Any]:
    """Figure 1: nx_answer runs — total, count-since, plan-match hit vs.
    inline-planner fallback, the fixed-edge latency histogram, and the
    oldest/newest event timestamps — all read from the engine's own
    ``nx_answer_runs/query`` aggregates (never recomputed client-side).

    ``newest_created_at`` costs no extra round trip: ``rows`` (newest
    first) already comes back on the SAME ``limit=1`` call this figure
    already made; ``oldest_created_at`` is a field on that same response.
    Fix round 1 (nexus-v0x32): these two were fetched and silently
    discarded — they are the instrument behind the 08-27 capture's
    headline "zero rows since <ts>" finding.
    """
    window = _window(since)
    try:
        if store is None:
            # Unify the "no store" and "store call failed" paths below into
            # one except block; _make_store()'s invariant guarantees
            # store_error is set whenever store is None, but the fallback
            # keeps this branch safe even if that invariant is ever broken.
            raise store_error or RuntimeError("telemetry store construction failed silently")
        windowed = store.query_nx_answer_runs(since=since, limit=1)
        total_all_time = (
            windowed.get("total", 0) if since is None
            else store.query_nx_answer_runs(since=None, limit=1).get("total", 0)
        )
    except Exception as exc:  # noqa: BLE001 — boundary catch; degrade to the honest UNAVAILABLE figure, never a fabricated 0
        _log.debug("telemetry_baseline_nx_answer_runs_failed", exc_info=True)
        from nexus.db.t2.http_telemetry_store import nx_answer_runs_read_failure_message  # noqa: PLC0415 - deferred: heavy import, keep CLI startup fast

        u = _unavailable(nx_answer_runs_read_failure_message(exc))
        return {
            "window": window, "total": u, "since_count": u, "hit_count": u,
            "fallback_count": u, "latency_buckets": u,
            "oldest_created_at": u, "newest_created_at": u,
        }
    rows = windowed.get("rows") or []
    newest_created_at = rows[0].get("created_at") if rows else None
    return {
        "window": window,
        "total": total_all_time,
        "since_count": windowed.get("total", 0),
        "hit_count": windowed.get("hit_count", 0),
        "fallback_count": windowed.get("fallback_count", 0),
        "latency_buckets": windowed.get("latency_buckets") or {},
        "oldest_created_at": windowed.get("oldest_created_at") or None,
        "newest_created_at": newest_created_at,
        "continuation_caveat": _NX_ANSWER_RUNS_CONTINUATION_CAVEAT,
    }


def _capture_tier_writes(since: str | None, store: Any, store_error: Exception | None) -> dict[str, Any]:
    """Figure 2: tier writes since *since* (or all-time when ``None``) —
    by tier, by tool, by agent, plus the null-agent share as a number
    (the slice that answers "which roles actually persist findings")."""
    window = _window(since)
    try:
        if store is None:
            # Unify the "no store" and "store call failed" paths below into
            # one except block; _make_store()'s invariant guarantees
            # store_error is set whenever store is None, but the fallback
            # keeps this branch safe even if that invariant is ever broken.
            raise store_error or RuntimeError("telemetry store construction failed silently")
        rows = store.query_tier_writes(since=since)
    except Exception as exc:  # noqa: BLE001 — boundary catch; degrade to the honest UNAVAILABLE figure, never a fabricated 0
        _log.debug("telemetry_baseline_tier_writes_failed", exc_info=True)
        from nexus.db.t2.http_telemetry_store import tier_writes_read_failure_message  # noqa: PLC0415 - deferred: heavy import, keep CLI startup fast

        u = _unavailable(tier_writes_read_failure_message(exc))
        return {"window": window, "total": u, "by_tier": u, "by_tool": u, "by_agent": u, "null_agent_share": u}

    by_tier: dict[str, int] = {}
    by_tool: dict[str, int] = {}
    by_agent: dict[str, int] = {}
    total = 0
    null_agent_total = 0
    for tool, tier, agent, _project, n in rows:
        total += n
        by_tier[tier] = by_tier.get(tier, 0) + n
        by_tool[tool] = by_tool.get(tool, 0) + n
        agent_key = agent or "<none>"
        by_agent[agent_key] = by_agent.get(agent_key, 0) + n
        if not agent:
            null_agent_total += n
    null_agent_share = (null_agent_total / total) if total else None
    return {
        "window": window,
        "total": total, "by_tier": by_tier, "by_tool": by_tool,
        "by_agent": by_agent, "null_agent_share": null_agent_share,
    }


def _capture_relevance_log(store: Any, store_error: Exception | None) -> dict[str, Any]:
    """Figure 3: relevance_log row count, oldest/newest event timestamp
    (NEW ``GET /v1/telemetry/relevance/stats`` route, this bead — a
    server-side SQL ``count(*)``/``min``/``max``, THE substrate-direct
    telemetry figure in this baseline), plus the cumulative-deletes
    retention marker (existing ``nexus-24p05`` route). The two calls fail
    independently — a pre-v0x32 engine 404s on the new route but still
    answers the old retention-markers one."""
    out: dict[str, Any] = {"window": "all-time"}
    try:
        if store is None:
            # Unify the "no store" and "store call failed" paths below into
            # one except block; _make_store()'s invariant guarantees
            # store_error is set whenever store is None, but the fallback
            # keeps this branch safe even if that invariant is ever broken.
            raise store_error or RuntimeError("telemetry store construction failed silently")
        stats = store.get_relevance_stats()
        out["count"] = stats.get("count", 0)
        out["oldest"] = stats.get("oldest")
        out["newest"] = stats.get("newest")
    except Exception as exc:  # noqa: BLE001 — boundary catch; degrade to the honest UNAVAILABLE figure, never a fabricated 0
        _log.debug("telemetry_baseline_relevance_stats_failed", exc_info=True)
        from nexus.db.t2.http_telemetry_store import relevance_stats_read_failure_message  # noqa: PLC0415 - deferred: heavy import, keep CLI startup fast

        u = _unavailable(relevance_stats_read_failure_message(exc))
        out["count"] = u
        out["oldest"] = u
        out["newest"] = u
    try:
        if store is None:
            # Unify the "no store" and "store call failed" paths below into
            # one except block; _make_store()'s invariant guarantees
            # store_error is set whenever store is None, but the fallback
            # keeps this branch safe even if that invariant is ever broken.
            raise store_error or RuntimeError("telemetry store construction failed silently")
        markers = store.get_retention_markers(["nexus.relevance_log"])
        out["retention_marker"] = markers.get("nexus.relevance_log", 0)
    except Exception as exc:  # noqa: BLE001 — boundary catch; degrade to the honest UNAVAILABLE figure, never a fabricated 0
        _log.debug("telemetry_baseline_retention_markers_failed", exc_info=True)
        out["retention_marker"] = _unavailable(f"{type(exc).__name__}: {exc}")
    return out


def _capture_search_telemetry(store: Any, store_error: Exception | None) -> dict[str, Any]:
    """Figure 4: search_telemetry global row count, collections examined,
    per-collection read errors, and per-collection ``zero_hit_rate`` —
    composed from two EXISTING readers (``make_t3().list_collections()``
    + ``query_collection_stats`` per name, which already returns
    ``zero_hit_rate`` on every call — no extra round trip), the same
    enumeration ``nx collection health`` already performs.

    Structurally a LOWER BOUND (fix round 1, code-review/critic round 1):
    a collection absent from ``list_collections()``'s enumeration is
    invisible to this figure independent of whether any per-call error
    occurred — so the caveat is rendered UNCONDITIONALLY, not gated on
    ``errors > 0``. A per-collection failure is additionally counted in
    ``errors`` (not fatal to the whole figure)."""
    try:
        if store is None:
            # Unify the "no store" and "store call failed" paths below into
            # one except block; _make_store()'s invariant guarantees
            # store_error is set whenever store is None, but the fallback
            # keeps this branch safe even if that invariant is ever broken.
            raise store_error or RuntimeError("telemetry store construction failed silently")
        from nexus.db import make_t3  # noqa: PLC0415 - deferred: heavy import, keep CLI startup fast

        names = [c["name"] for c in make_t3().list_collections()]
    except Exception as exc:  # noqa: BLE001 — boundary catch; whole figure UNAVAILABLE only when enumeration itself fails
        _log.debug("telemetry_baseline_search_telemetry_enumerate_failed", exc_info=True)
        u = _unavailable(f"{type(exc).__name__}: {exc}")
        return {
            "window": "all-time", "row_count_total": u, "collections_examined": u,
            "errors": u, "lower_bound": u, "zero_hit_rate_by_collection": u,
        }

    total = 0
    errors = 0
    zero_hit_rate_by_collection: dict[str, float] = {}
    for name in names:
        try:
            stats = store.query_collection_stats(name, days=_SEARCH_TELEMETRY_ALL_TIME_DAYS)
            total += int(stats.get("row_count") or 0)
            zhr = stats.get("zero_hit_rate")
            # nexus-v0x32: the engine sends the STRING "null" (not JSON
            # null) as its zero-population sentinel, TelemetryRepository.
            # queryCollectionStats -- Map.of() cannot hold a null value.
            # Filter to numeric readings only.
            if isinstance(zhr, (int, float)):
                zero_hit_rate_by_collection[name] = float(zhr)
        except Exception:  # noqa: BLE001 — per-collection failure counted; the loop (and the figure) continues
            _log.debug("telemetry_baseline_search_telemetry_collection_failed", collection=name, exc_info=True)
            errors += 1
    return {
        "window": "all-time",
        "row_count_total": total,
        "collections_examined": len(names),
        "errors": errors,
        "lower_bound": True,
        "zero_hit_rate_by_collection": zero_hit_rate_by_collection,
    }


def _capture_drop_meter() -> dict[str, Any]:
    """Figure 5: the RDR-129 B4 dropped-best-effort-write meter (a local
    JSONL log; ``count_drops()`` never raises on a missing/absent file —
    still wrapped defensively so an unreadable log renders UNAVAILABLE
    rather than crashing the whole baseline)."""
    try:
        from nexus.dropped_writes import count_drops  # noqa: PLC0415 - deferred: branch-local

        summary = count_drops()
    except Exception as exc:  # noqa: BLE001 — boundary catch; degrade to the honest UNAVAILABLE figure, never a fabricated 0
        _log.debug("telemetry_baseline_drop_meter_failed", exc_info=True)
        u = _unavailable(f"{type(exc).__name__}: {exc}")
        return {"window": "all-time", "total": u, "rows": u}
    return {"window": "all-time", "total": summary.total, "rows": summary.rows}


def _capture_substrate_check() -> dict[str, Any]:
    """Figure 7: one engine-side SQL count, ``catalog_stats.doc_count`` —
    the same figure reconcile-stale's own "Substrate anchor" reads, via a
    read-facing catalog reader's ``stats()``.

    RELABELED (fix round 1, substantive-critic round 1 SIGNIFICANT-2):
    this is a CATALOG metric (count(*) over catalog_documents) unrelated
    to any telemetry aggregate in this block — it validates NEITHER
    search_telemetry's client-side sum NOR tier_writes' by-tier sum. Kept
    as CONTEXT (satisfies playbook §4.6's letter — one engine-side SQL
    count in the block), explicitly labeled as such rather than implying
    it cross-checks this baseline's own censuses. The baseline's actual
    substrate-direct TELEMETRY figure is relevance_log's count (figure 3
    — a server-side SQL count/min/max over nexus.relevance_log itself).
    """
    try:
        from nexus.catalog.factory import make_catalog_reader  # noqa: PLC0415 - deferred: heavy import, keep CLI startup fast

        cat = make_catalog_reader()
        stats = cat.stats() if cat is not None else {}
        doc_count = stats.get("doc_count") if isinstance(stats, dict) else None
    except Exception as exc:  # noqa: BLE001 — boundary catch; degrade to the honest UNAVAILABLE figure, never a fabricated 0
        _log.debug("telemetry_baseline_substrate_check_failed", exc_info=True)
        return {"window": "all-time", "catalog_doc_count": _unavailable(f"{type(exc).__name__}: {exc}")}
    if doc_count is None:
        return {"window": "all-time", "catalog_doc_count": _unavailable("engine stats carry no doc_count")}
    return {"window": "all-time", "catalog_doc_count": int(doc_count)}


def _capture_baseline(since: str | None) -> dict[str, Any]:
    """Compose all seven §4.5 figures plus the literal consent row into
    one fixed-shape payload. ONE shared ``HttpTelemetryStore`` (fix round
    1, code-review suggestion) backs every store-dependent figure."""
    store, store_error = _make_store()
    return {
        "captured_at": datetime.now(UTC).isoformat(),
        "nx_answer_runs": _capture_nx_answer_runs(since, store, store_error),
        "tier_writes": _capture_tier_writes(since, store, store_error),
        "relevance_log": _capture_relevance_log(store, store_error),
        "search_telemetry": _capture_search_telemetry(store, store_error),
        "drop_meter": _capture_drop_meter(),
        "consent": _CONSENT_RETIRED,
        "substrate_check": _capture_substrate_check(),
    }


def _fmt_window(window: Any) -> str:
    """Render a figure's ``window`` value for the text form: ``all-time``
    verbatim, or ``since <iso>`` for a scoped figure."""
    if isinstance(window, dict):
        return f"since {window.get('since')}"
    return str(window)


def _render_text(data: dict[str, Any]) -> None:
    """One line per figure — diffable against a previous run's text
    output, playbook §4.5's own diffability requirement. Every line
    prints its own window (fix round 1) so no figure can be misread as
    covering a window it does not honour."""
    from nexus.commands.answer_runs import _BUCKET_ORDER  # noqa: PLC0415 - deferred: avoid import cycle, one definition (playbook rule)

    click.echo(f"telemetry baseline, captured {data['captured_at']}:")

    nar = data["nx_answer_runs"]
    win = _fmt_window(nar["window"])
    if isinstance(nar["window"], dict):
        # Scoped: "total N (+M since <since>)" — M is the since-scoped
        # count, N is the separately-fetched all-time total.
        total_clause = f"total={nar['total']} (+{nar['since_count']} {win})"
    else:
        total_clause = f"total={nar['total']} ({win})"
    click.echo(
        f"  nx_answer runs: {total_clause}; hit={nar['hit_count']} / "
        f"fallback={nar['fallback_count']}; newest={nar['newest_created_at']}; "
        f"oldest={nar['oldest_created_at']}"
    )
    buckets = nar["latency_buckets"]
    if isinstance(buckets, dict) and buckets:
        rendered = "  ".join(f"{k}={buckets.get(k, 0)}" for k in _BUCKET_ORDER)
        click.echo(f"    latency buckets: {rendered}")
    if nar.get("continuation_caveat"):
        click.echo(f"    CAVEAT: {nar['continuation_caveat']}")

    tw = data["tier_writes"]
    click.echo(f"  tier writes ({_fmt_window(tw['window'])}): total={tw['total']}  by_tier={tw['by_tier']}")
    click.echo(f"    by_tool={tw['by_tool']}")
    click.echo(f"    by_agent={tw['by_agent']}  null_agent_share={tw['null_agent_share']}")

    rl = data["relevance_log"]
    click.echo(
        f"  relevance_log ({_fmt_window(rl['window'])}): count={rl['count']} "
        f"(server-side SQL)  oldest={rl['oldest']}  newest={rl['newest']}  "
        f"retention_marker={rl['retention_marker']}"
    )

    st = data["search_telemetry"]
    win_st = _fmt_window(st["window"])
    if isinstance(st["row_count_total"], str):
        click.echo(f"  search_telemetry ({win_st}): {st['row_count_total']}")
    else:
        click.echo(
            f"  search_telemetry ({win_st}): {st['row_count_total']} across "
            f"{st['collections_examined']} collections, LOWER BOUND "
            f"(per-collection stats; {st['errors']} collections unreadable)"
        )
        zhr = st.get("zero_hit_rate_by_collection")
        if isinstance(zhr, dict) and zhr:
            worst = sorted(zhr.items(), key=lambda kv: kv[1], reverse=True)[:_ZERO_HIT_RATE_WORST_N]
            rendered = ", ".join(f"{v:.3f} {k}" for k, v in worst)
            click.echo(f"    zero_hit_rate {rendered}")

    dm = data["drop_meter"]
    click.echo(f"  drop meter ({_fmt_window(dm['window'])}): total={dm['total']}  rows={dm['rows']}")

    click.echo(f"  consent: {data['consent']}")

    sc = data["substrate_check"]
    click.echo(
        f"  substrate check ({_fmt_window(sc['window'])}): "
        f"catalog_doc_count={sc['catalog_doc_count']} "
        "(engine SQL, context, not a telemetry anchor)"
    )


@click.group("telemetry")
def telemetry_group() -> None:
    """Telemetry baseline and diagnostics (nexus-v0x32)."""


@telemetry_group.command("baseline")
@click.option(
    "--since", "since", default=None,
    help=(
        "ISO 8601 timestamp; scopes ONLY nx_answer_runs's since_count and "
        "tier_writes to this window (both also always report their "
        "all-time figures alongside it). relevance_log, search_telemetry, "
        "the drop meter, consent, and the substrate check are always "
        "whole-tenant/all-time (playbook §4.5 does not window them) — "
        "every figure in --json carries its own \"window\" key stating "
        "which applies, and the text form prints it on every line."
    ),
)
@click.option(
    "--json", "json_out", is_flag=True, default=False,
    help="Emit structured JSON instead of the human text form.",
)
def baseline_cmd(since: str | None, json_out: bool) -> None:
    """Capture the playbook §4.5 fixed-shape telemetry baseline.

    Composes seven existing readers (nx_answer_runs, tier_writes,
    relevance_log, search_telemetry, the drop meter, a catalog
    substrate-direct cross-check) plus the literal consent-retirement row
    into one snapshot, in the exact shape the 2026-08-27 shakedown capture
    established (T2 [23596]) — so this run's output diffs cleanly against
    a previous one. A figure that cannot be read renders as
    ``"UNAVAILABLE: <reason>"`` rather than being silently omitted or
    reported as a fabricated zero. Every figure carries its own window
    (``--since``-scoped or all-time) — see ``--since``'s own help text.
    """
    # nexus-spbay (code-review Important #1): validate/normalize --since
    # BEFORE any store call — a bad value must fail as a usage error
    # naming the value, never surface through a reader's UNAVAILABLE arm
    # as a phantom service failure (and never reach an old engine, whose
    # parser turned any unparseable since into `since now()` = zeros).
    if since:
        from nexus.db.t2.http_telemetry_store import normalize_since_filter  # noqa: PLC0415 - deferred: heavy import, keep CLI startup fast

        try:
            since = normalize_since_filter(since)
        except ValueError as exc:
            raise click.BadParameter(str(exc), param_hint="--since") from exc
    data = _capture_baseline(since)
    if json_out:
        click.echo(_json.dumps(data, indent=2))
        return
    _render_text(data)
