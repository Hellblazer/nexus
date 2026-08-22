# SPDX-License-Identifier: AGPL-3.0-or-later
# Copyright (c) 2026 Hal Hildebrand. All rights reserved.
"""Plan matcher — RDR-078 P1 (SC-1, SC-11, SC-12).

Two paths, same output shape:

  * **T1 cosine path** — a :class:`PlanCache` queries the
    ``plans__session`` ChromaDB collection, returns ``(plan_id,
    distance)`` pairs. We convert distance → cosine similarity
    (``1 - distance``), look up the row in :class:`~nexus.db.t2.http_http_plan_library.HttpPlanLibrary`, and return :class:`~nexus.plans.match.
    Match` objects with ``confidence`` set.
  * **FTS5 fallback** — when no cache is provided or the cache has no
    hits, fall back to ``HttpPlanLibrary.search_plans`` (keyword match
    over the stored descriptions). Matches carry ``confidence=None``
    as the sentinel.

Post-filter: the caller may pin a ``dimensions`` dict. Plans whose
``dimensions ⊇ filter.dimensions`` pass; others drop. ``min_confidence``
rejects below-threshold cosine hits, but FTS5 hits
(``confidence=None``) pass through — the sentinel is an implicit
"skill-level gate decides".

Side effect: every returned plan increments
``plans.match_count`` (and ``match_conf_sum`` when scored) via
:meth:`HttpPlanLibrary.increment_match_metrics`. SC-12.
"""
from __future__ import annotations

from typing import Any, Protocol

import structlog

from dataclasses import replace
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    # Annotation-only (PEP 563 lazy): the runtime argument is whatever
    # the caller passes — production passes HttpPlanLibrary, the only
    # plan library left after nexus-i711w Stage 2 sub-stage A3 deleted
    # the SQLite PlanLibrary.
    from nexus.db.t2.http_plan_library import HttpPlanLibrary
from nexus.plans.match import Match, _decode_json_dict
from nexus.plans.schema import unsatisfiable_typed_binding
from nexus.plans.scope import _SCOPE_AGNOSTIC_SENTINELS, _normalize_scope_string

__all__ = ["PlanCache", "plan_match"]

_log = structlog.get_logger()


# ── Scope-aware re-ranking (RDR-091 Phase 2b + 2c) ─────────────────────────
#
# Score formula (RDR-091 §Proposed Solution → Score formula):
#     final_score = base_confidence * (1 + _SCOPE_FIT_WEIGHT * scope_fit)
# scope_fit is 0.0 (agnostic plan) or 1.0 (scope prefix match).
#
# 0.15 picked by inspection against 5 fixture pairs in test_plan_match.py
# (Phase 2c, nexus-svcg). The motivating failure case (RDR §Problem
# Statement, generic agnostic at 0.82 vs specialized matching at 0.79)
# breaks even at weight ≈ 0.038; 0.15 leaves comfortable margin
# (specialized 0.79 * 1.15 = 0.9085 beats generic 0.82) without so
# much boost that a genuinely higher-cosine agnostic plan gets drowned.
_SCOPE_FIT_WEIGHT: float = 0.15

# RDR-090 P1.3 (nexus-zgko): plans saved by the inline-planner via the
# RDR-084 plan-grow path inherit the originating question's match-text.
# That match-text is signal for paraphrases of the same question but
# noise for any unrelated question that shares scaffolding (e.g.
# 'which RDR…'). The spike found plan #67 (saved from Q1 'Which RDR
# introduced catalog tumblers?') firing at confidence 0.476 on
# unrelated Q3 (taxonomy/BERTopic) and Q4 (hooks comparative). We
# require grown plans to clear a higher floor — paraphrase-quality
# matches still fire; loose prefix-overlap drops.
#
# 0.55 sits a clear margin above the observed 0.476 leakage and just
# above the precision-first 0.50 calibration documented in RDR-079 P5
# (precision 0.90 / recall 0.19 at 0.50).
_GROWN_PLAN_MIN_CONFIDENCE: float = 0.55


#: nexus-vtp8h: a plan whose recorded runs are ALL failures at/past this
#: count (with zero successes) is skipped by the matcher — re-matching it
#: just re-crashes the runner (the plan-138 class). One success ever
#: exempts a plan permanently (real plans can fail transiently).
_ALWAYS_FAILING_MIN_FAILURES: int = 3


def _normalize_for_exact_match(text: str) -> str:
    """Casefold + whitespace-collapse for verbatim-repeat detection.

    nexus-h33x8.6 a2. Deliberately NOT a fuzzy/paraphrase comparison —
    "trivially normalized" per the fix direction means case and
    whitespace only. Anything beyond that (word order, synonyms,
    stemming) is left to the cosine path and its unchanged 0.40 gate.
    """
    return " ".join((text or "").casefold().split())


def _is_verbatim_repeat(row: dict, normalized_intent: str) -> bool:
    """True when *row*'s stored ``query`` verbatim-repeats *intent*.

    nexus-h33x8.6 a2 (T2 nexus/nx-answer-capability-analysis-2026-08-19):
    a plan's ``query`` column already carries the original question
    verbatim — for a grown plan because ``nx_answer``'s plan-grow step
    passes ``query=question``, for any other plan because promote.py's
    own gate requires a non-degenerate description. A verbatim repeat of
    that text is unambiguous signal a cosine-similarity threshold should
    never be needed to establish; the caller is quite literally asking
    the question that produced this plan.
    """
    if not normalized_intent:
        return False
    return _normalize_for_exact_match(row.get("query") or "") == normalized_intent


def _is_always_failing(row: dict) -> bool:
    """True when the plan's run record is all-failure (see constant above)."""
    try:
        failures = int(row.get("failure_count") or 0)
        successes = int(row.get("success_count") or 0)
    except (TypeError, ValueError):
        return False
    return successes == 0 and failures >= _ALWAYS_FAILING_MIN_FAILURES


#: Comma-token that marks a plan as CATEGORY-level: a hand-authored
#: template describing a kind of work ("research any concept"), as
#: opposed to an INSTANCE-level grown plan that memoizes one question.
#:
#: Matched as a comma token, like :func:`_is_grown_plan_tags`, not as a
#: naive substring. Deliberately NOT keyed on ``scope == "global"``:
#: plenty of test fixtures and user plans are global without being
#: templates, and a scope predicate would silently start firing inside
#: existing below-floor assertions.
#:
#: This is the best discriminator available today. When a durable
#: ``origin`` column lands (nexus-7bwe), this predicate moves to it.
_CATEGORY_PLAN_TAG = "builtin-template"


def _is_category_plan(tags: str | None) -> bool:
    """True when *tags* marks a category-level (builtin template) plan."""
    if not tags:
        return False
    return _CATEGORY_PLAN_TAG in [t.strip() for t in tags.split(",")]


def _plan_verb(row: dict) -> Any:
    """The plan's verb, preferring the canonical ``dimensions`` map.

    The ``verb`` column is a denormalised copy the library writes
    alongside ``dimensions``. Every other verb comparison in this module
    reads the dimensions map (via ``Match.dimensions``), so this reads it
    too and falls back to the column only when the map has no verb —
    otherwise a row whose copies had drifted would route on one verb and
    be dimension-filtered on the other.
    """
    dims = _decode_json_dict(row.get("dimensions"))
    return dims.get("verb") or row.get("verb")


def _is_grown_plan_tags(tags: str | None) -> bool:
    """Return True when *tags* identifies a grown plan.

    The plan-grow save path at ``nx_answer`` writes ``ad-hoc,grown``;
    we match the comma-token presence so future tag additions don't
    silently break the detection (RDR-090 P1.3 / nexus-zgko).
    """
    if not tags:
        return False
    return "grown" in [t.strip() for t in tags.split(",")]


def _is_unanchored_grown(row: dict) -> bool:
    """Return True for a grown plan with NO scope anchor.

    nexus-mz5tv: a grown plan inherits its origin question's match-text,
    so it is NOT genuinely scope-agnostic — but ``_scope_fit`` treats an
    empty ``scope_tags`` as agnostic (neutral, always kept). When such a
    plan ALSO has a blank ``project`` column (the #1069/#1099 project
    fallback then has nothing to scope on), it has zero provenance and
    becomes a cross-project attractor: it competes globally at the grown
    floor against scoped questions in unrelated projects.

    "Unanchored" = grown tag AND empty ``scope_tags`` AND blank
    ``project``. The caller drops such a plan when an explicit
    ``scope_preference`` is supplied (a scoped caller should not match a
    plan with no scope provenance). A grown plan carrying either anchor
    is unaffected, and with no caller scope nothing is dropped (the
    legitimate corpus:all → corpus:all case).
    """
    if not _is_grown_plan_tags(row.get("tags")):
        return False
    if (row.get("scope_tags") or "").strip():
        return False
    return not (row.get("project") or "").strip()


def _unmet_typed_binding(
    m: Match, available: frozenset[str] | None,
) -> str | None:
    """Name the typed binding that makes *m* unrunnable, else ``None``.

    ``available is None`` means the caller did not declare what it can
    supply, so no filtering happens — this keeps every pre-nexus-0yrjr
    caller (and ``plan_search``) behaving exactly as before.
    """
    if available is None:
        return None
    return unsatisfiable_typed_binding(
        required=m.required_bindings,
        defaults=m.default_bindings,
        available=available,
        # nexus-7y4v0: pass the plan so a binding that FLOWS INTO a typed
        # slot counts as typed even when its name does not look it.
        plan_json=m.plan_json,
    )


def _log_t1_drops(
    *,
    hits: list,
    scope_conflict_drops: int,
    always_failing_drops: int,
    scope_pref: str,
) -> None:
    """Report the T1 path's scope-conflict and always-failing drops.

    nexus-czoeu. Both records existed before but were emitted only after
    the ``if scored:`` early return, so they fired only when EVERY
    candidate was dropped. RDR-091's code-review finding I-4 added the
    scope record specifically so "operators can see when
    scope_preference is silently degrading matches" — which it could not
    do in the case where a degraded match is actually returned.

    INFO, not DEBUG, for the same reason as
    :func:`_log_binding_drops`: ``_resolve_level``
    (``logging_setup.py:36``) defaults MCP to INFO and CLI to WARNING,
    so the original DEBUG records never reached a log in normal
    operation — they were unobservable on the fallthrough path too.
    """
    if hits and scope_conflict_drops > 0:
        _log.info(
            "plan_match_scope_conflict_fallthrough",
            t1_hits=len(hits),
            dropped=scope_conflict_drops,
            scope_pref=scope_pref,
        )
    if always_failing_drops:
        _log.info(
            "plan_match_always_failing_dropped",
            dropped=always_failing_drops,
            remedy="nx plan hygiene --apply retires these durably",
        )


def _log_category_route(
    match: Match, *, raw_confidence: float, verb: str | None,
) -> None:
    """Record that a plan was admitted by dimension rather than by cosine.

    INFO for the same reason as the sibling drop records: the CLI
    defaults to WARNING and MCP to INFO, so DEBUG would be unobservable
    exactly where it matters. This is the one line that distinguishes
    "nx_answer matched a builtin" from "nx_answer matched anything", and
    with builtins having been selected once in four months, the
    difference is worth a log line.
    """
    _log.info(
        "plan_match_category_routed",
        plan_id=match.plan_id,
        name=match.name,
        verb=verb,
        raw_confidence=round(raw_confidence, 4),
    )


def _log_binding_drops(drops: list[tuple[int, str]]) -> None:
    """Surface unrunnable-plan drops.

    A candidate vanishing from the pool with no record is the same defect
    class as a retrieval silently widening: the caller sees a plausible
    result and no signal that the shape of the search changed.

    Emitted at INFO, not DEBUG, deliberately. ``_resolve_level``
    (``logging_setup.py:36``) defaults MCP mode to INFO and CLI to
    WARNING, so a DEBUG record here would never reach a log in practice
    and the drop would be observable only in theory. It fires at most
    once per ``plan_match`` call, and only when something was actually
    dropped.
    """
    if not drops:
        return
    # Drain: plan_match can traverse the T1 path and then fall through to
    # FTS5, and both call this on the way out. Without clearing, a single
    # drop is reported twice and an operator counting occurrences
    # over-reports.
    snapshot = list(drops)
    drops.clear()
    _log.info(
        "plan_match_unrunnable_binding_dropped",
        dropped=len(snapshot),
        detail=[{"plan_id": pid, "binding": b} for pid, b in snapshot],
        remedy=(
            "the plan is not necessarily wrong — a plan requiring a typed "
            "binding is simply not offerable to a caller that cannot "
            "supply one; pass it explicitly to use that plan"
        ),
    )


def _scope_fit(plan_scope_tags: str, normalized_scope_pref: str) -> float | None:
    """Return scope-fit in ``{0.0, 1.0}`` or ``None`` for a conflict.

    Semantics (RDR-091 §Proposed Solution → scope-fit, tightened by
    RDR-090 P1.2 / nexus-smhc):

      * empty caller scope → always ``0.0`` (no preference; no filter)
      * empty plan scope_tags (agnostic plan) → ``0.0`` (neutral; stays)
      * **direction 1** (caller is at least as specific as the plan
        tag): ``scope.startswith(tag)`` → ``1.0``. Bare-family plans
        (``rdr__``) serve narrower callers (``rdr__nexus``).
      * **direction 2** (plan is more specific than the caller):
        ``tag.startswith(scope)`` is admitted ONLY when the caller's
        scope is itself a canonical family-prefix (ends with ``__``).
        Without the ``__`` boundary marker, a bare prefix like ``rdr``
        would silently widen to ``rdr__arcaneum`` and pull
        cross-project plans into the candidate set — the plan #52
        leakage documented in the RDR-090 spike.
      * otherwise → ``None`` (conflict; caller filters out)

    Prefix matching is **case-insensitive**. Real collection names in
    nexus carry mixed case (e.g. ``code__Delos-5af9bfe0`` alongside
    ``knowledge__delos``), but ChromaDB's naming convention doesn't use
    case to disambiguate identity. A caller passing the conventional
    lowercase scope should match plans tagged with the mixed-case form
    found in the wild (nexus-yi7m). Stored values preserve original case
    so authoring output (``plan_search``) still shows the real name.
    """
    if not normalized_scope_pref:
        return 0.0
    if not plan_scope_tags:
        return 0.0
    scope_lower = normalized_scope_pref.lower()
    scope_is_family_prefix = scope_lower.endswith("__")
    tags = [t for t in plan_scope_tags.split(",") if t]
    for tag in tags:
        tag_lower = tag.lower()
        # Direction 1: caller is more-or-equally specific than plan tag.
        if scope_lower.startswith(tag_lower):
            return 1.0
        # Direction 2: plan is more specific than caller — only admit
        # when caller's scope ends with the family-prefix boundary.
        # Without this guard, bare 'rdr' (no '__') silently widens to
        # admit specific cross-project plans like 'rdr__arcaneum'.
        if scope_is_family_prefix and tag_lower.startswith(scope_lower):
            return 1.0
    return None


def _specificity(plan_scope_tags: str) -> int:
    """Tie-break key: fewer scope_tags means a more specific plan.

    Returned as a negative count so higher values sort first under
    Python's ``reverse=True`` sort.
    """
    return -len([t for t in plan_scope_tags.split(",") if t])


class PlanCache(Protocol):
    """Minimal interface for the T1 ``plans__session`` cosine cache.

    Implemented by :mod:`nexus.plans.session_cache` (Phase 2 of this
    bead) and stubbed in tests. Kept narrow so the matcher can be
    exercised without a real ChromaDB HTTP server.
    """

    @property
    def is_available(self) -> bool: ...

    def query(self, intent: str, n: int) -> list[tuple[int, float]]:
        """Return ``(plan_id, distance)`` pairs ordered closest-first."""
        ...


#: nexus-qi8t: verb-synonym equivalence classes for the dimension
#: filter. The historical strict-equality gate at :func:`_superset`
#: rejected high-confidence cosine matches whenever the caller's
#: inferred verb (``query``) and the plan's declared verb
#: (``research``) used different vocabulary for what is semantically
#: the same intent. Live repro 2026-05-06: ``nx_answer`` with
#: question 'Find papers by Grossberg about ART resonance' and
#: ``dimensions={"verb":"research"}`` did NOT match the builtin
#: ``find-by-author`` plan (verb=research) because the inline planner
#: classified the question as ``verb=query`` and the strict filter
#: dropped the cosine hit before it reached scoring.
#:
#: Each class lists verb names that should be treated as
#: interchangeable for filter purposes; verbs not in any class only
#: match themselves (strict-equality preserved). Classes are
#: deliberately conservative -- ``debug`` (failure investigation),
#: ``document`` (gap-finding), and ``plan-*`` (plan lifecycle ops)
#: all stay isolated since swapping them with a research-verb plan
#: would produce wrong answers.
_VERB_SYNONYMS: tuple[frozenset[str], ...] = (
    # Information-retrieval intents -- "tell me about X", "how does X
    # work", "find papers by Y". The user's question shape rarely
    # disambiguates between these and the plan-author's verb choice
    # is similarly fuzzy in practice.
    frozenset({"query", "research", "lookup"}),
    # Critique / synthesis intents -- "what tradeoffs", "compare X
    # across projects", "review this design".
    frozenset({"analyze", "review", "compare"}),
)


def _verbs_compatible(plan_verb: Any, filter_verb: Any) -> bool:
    """Return True when *plan_verb* and *filter_verb* should be
    treated as equivalent under the synonym map. Falls back to
    strict equality when either verb is empty or neither appears in
    any class.
    """
    if plan_verb == filter_verb:
        return True
    if not plan_verb or not filter_verb:
        return False
    for klass in _VERB_SYNONYMS:
        if plan_verb in klass and filter_verb in klass:
            return True
    return False


def _superset(plan_dims: dict[str, Any], filter_dims: dict[str, Any]) -> bool:
    """Return True when ``plan_dims ⊇ filter_dims`` (nexus-qi8t).

    The ``verb`` dimension uses the synonym-class equality at
    :func:`_verbs_compatible`; other dimensions (scope, strategy,
    taxonomy_domain, ...) keep strict equality so cross-domain plans
    cannot accidentally match.
    """
    for k, v in filter_dims.items():
        plan_v = plan_dims.get(k)
        if k == "verb":
            if not _verbs_compatible(plan_v, v):
                return False
        elif plan_v != v:
            return False
    return True


def plan_match(
    intent: str,
    *,
    library: HttpPlanLibrary,
    cache: PlanCache | None = None,
    dimensions: dict[str, Any] | None = None,
    scope_preference: str = "",
    context: dict[str, Any] | None = None,
    # RDR-079 P5 calibration (docs/rdr/rdr-079-calibration.md) picked
    # 0.40 as the F1-optimal threshold for the bundled MiniLM T1 cache.
    # Callers that need precision-first behavior override explicitly
    # (0.50 → precision 0.90 at the cost of recall 0.19).
    min_confidence: float = 0.40,
    n: int = 5,
    project: str = "",
    available_bindings: frozenset[str] | None = None,
    category_verb: str | None = None,
) -> list[Match]:
    """Return plans ranked for *intent*.

    See module docstring for the two-path contract. ``scope_preference``
    is the RDR-091 Phase 2b filter + re-ranker (was a no-op prior):

      * A non-empty *scope_preference* is normalized (hash-suffix and
        glob stripped) before comparison against each plan's
        ``scope_tags``.
      * **Scope-conflict filter**: a plan whose ``scope_tags`` is
        non-empty and none of whose tags prefix-match the caller scope
        (in either direction) is dropped from the candidate pool. Agnostic
        plans (``scope_tags == ''``) are kept with neutral weight.
      * **Scope-fit boost**: matching plans receive a small
        multiplicative boost per the RDR-091 score formula
        ``adjusted = confidence * (1 + _SCOPE_FIT_WEIGHT * scope_fit)``,
        used only for ranking. ``Match.confidence`` still carries the
        raw cosine, so ``min_confidence`` and downstream thresholds
        are unchanged.
      * **Specificity tie-break**: at equal adjusted score, the plan
        with fewer scope_tags (more specific scope) ranks first.

    ``context`` is still accepted for forward compatibility and currently
    unused. Every returned plan has its ``match_count`` bumped and, when
    the confidence is numeric, ``match_conf_sum`` accumulates.

    Always returns matches sorted descending by the ranking key
    (scope-adjusted for T1, FTS5 order preserved for the fallback).
    Zero-candidate outcome (e.g. every candidate conflicts) returns
    ``[]``; upstream ``nx_answer`` falls through to the inline planner.

    **Sentinel**: an FTS5-fallback match sets ``Match.confidence = None``.
    The ``plan_match`` MCP tool renders this as ``confidence=fts5`` in
    its string output. Callers MUST treat ``confidence=None`` (or the
    rendered ``fts5`` string) as a match that clears the gate — the
    ``min_confidence`` parameter does not apply to FTS5 hits.

    ``category_verb`` enables the CATEGORY ROUTE (T2
    design-dimension-routed-category-plans-2026-08-21). Cosine ranks
    plans by topical overlap, but a category-level plan is topic-free by
    construction, so it cannot clear an absolute floor against a
    topic-bearing question — measured with zero competition, 3 of 5
    verb-shaped probes put the intended builtin below 0.40 and
    ``debug-default`` died at RANK 1. No floor setting and no
    description rewrite fixes that; both were tried and measured.

    When *category_verb* is supplied and the cosine path scores nothing,
    the builtin templates whose verb is compatible with it are
    reconsidered with the floor — and only the floor — lifted. Every
    correctness gate still applies. Selection within that pool is by raw
    cosine, which retains RELATIVE discriminating power inside a small
    same-verb pool even though it has no ABSOLUTE power across the
    library; that is what lets a discovery-shaped question reach
    ``document-discovery`` rather than ``research-default``.

    The route can only turn a MISS into a hit. It never fires when the
    cosine path produced a candidate, so it cannot change any call that
    already matched. The returned Match carries ``confidence=None`` —
    the same sentinel the FTS path uses, and for the same reason: the
    cosine number is not what admitted it.

    *category_verb* is deliberately a separate parameter rather than a
    ``dimensions["verb"]`` entry. ``dimensions`` is a hard filter over
    ALL candidates including grown plans, whose verbs only ever come out
    as ``research`` or ``analyze``; routing a derived ``debug`` through
    it would drop every grown plan, and grown plans are currently the
    entire live hit rate.
    """
    filter_dims = dimensions or {}
    # nexus-h33x8.6 a2: precompute once — every candidate on the T1 path
    # is checked against this for a verbatim-repeat override.
    normalized_intent = _normalize_for_exact_match(intent)
    scope_pref = _normalize_scope_string(scope_preference) if scope_preference else ""
    # nexus-mz5tv: a "real" scope preference excludes the corpus:all
    # sentinel ("all"). A corpus:all caller has NO project preference, so it
    # must NOT trigger the unanchored-grown drop below — that would lose the
    # legitimate corpus:all -> corpus:all match the drop is designed to keep.
    scope_pref_is_real = bool(scope_pref) and scope_pref.lower() not in _SCOPE_AGNOSTIC_SENTINELS

    # T1 cosine path when cache available + has hits.
    # Over-fetch covers (a) dimension post-filter attrition, (b) min_confidence
    # threshold attrition, and (c) RDR-091 scope-conflict attrition. A fixed
    # floor (n * 2) avoids under-delivery when filter_dims is empty. Cost is
    # bounded by cache size (session-scoped).
    _over = max(n * 2, n + len(filter_dims) * 2)
    if scope_pref:
        _over += n  # extra budget for potential conflict drops
    if available_bindings is not None:
        _over += n  # and for unrunnable-plan drops (nexus-0yrjr)
    # (plan_id, binding) for every candidate dropped as unrunnable; drained
    # by _log_binding_drops on each return path so the pool never shrinks
    # silently.
    binding_drops: list[tuple[int, str]] = []
    if cache is not None and cache.is_available:
        hits = cache.query(intent, _over)
        # Gather all admissible candidates with (adjusted_score, specificity).
        scored: list[tuple[float, int, Match]] = []
        # Category-route candidates: (raw_cosine, is_default, plan_id, match).
        # Populated only when `category_verb` is set; consumed after the
        # cosine path has been given its chance and produced nothing.
        category_pool: list[tuple[float, bool, int, Match]] = []
        scope_conflict_drops = 0
        always_failing_drops = 0
        # nexus-h33x8.6 a2 fix-pass (substantive-critic SIGNIFICANT #2,
        # T2 substantive-critique-nexus-h33x8.6-a4-a2-2026-08-19): the
        # RAW cosine confidence per candidate, captured BEFORE the
        # verbatim-repeat override below. increment_match_metrics must
        # record this, not the synthetic 1.0 — match_conf_sum is a
        # PERMANENT aggregate RDR-196's future confidence-band plan
        # selection reads as genuine embedding-quality signal, and the
        # forced 1.0 is a gating decision, not a quality measurement.
        raw_confidence_by_plan_id: dict[int, float] = {}
        for plan_id, distance in hits:
            row = library.get_plan(plan_id)
            if row is None:
                # Search review I-4: the plan was deleted from T2 but the
                # T1 cache still carries its embedding. Evict it now so
                # the stale row stops skewing future top-N fetches.
                try:
                    cache.remove(plan_id)
                except Exception:  # noqa: BLE001 — best-effort cache eviction; logged at debug
                    # nexus-8g79.8: a recurring removal failure causes
                    # the stale row to be re-evicted on every match
                    # call. DEBUG-with-exc_info + plan_id so the
                    # repeat-offender is identifiable.
                    import structlog  # noqa: PLC0415 — deferred import — optional/heavy dependency, branch-local
                    structlog.get_logger(__name__).debug(
                        "plan_cache_eviction_failed",
                        plan_id=plan_id, exc_info=True,
                    )
                continue
            confidence = max(0.0, 1.0 - float(distance))
            raw_confidence_by_plan_id[plan_id] = confidence
            # nexus-h33x8.6 a2: a verbatim (casefold + whitespace-collapse)
            # repeat of the plan's own recorded question forces a
            # guaranteed hit, REGARDLESS of the raw cosine score. This
            # runs BEFORE the min_confidence gate deliberately — the
            # hybrid match-text embedding (query + verb + name + scope,
            # see nexus.plans.match_text) costs a little cosine even on an
            # exact repeat.
            #
            # CORRECTION (nexus-7g0rg): this comment used to assert that
            # the dilution "is exactly what let an unrelated higher-scoring
            # candidate win" on run 382. That was an inference, and it was
            # MEASURED FALSE — dilution across all 21 live plans averaged
            # -0.034, and the plan in question went 1.0000 -> 0.9400, still
            # rank 1 by a 0.70 margin over the runner-up. It cleared every
            # floor comfortably. The actual cause was the unanchored-grown
            # drop below, which is why this override now bypasses that too.
            #
            # Bypasses the CONFIDENCE floors (via confidence=1.0) and the
            # unanchored-grown drop. Deliberately does NOT bypass the
            # always-failing skip, the dimension filter, the unmet-binding
            # gate, or a genuine scope CONFLICT — those are correctness
            # gates about whether the plan can run at all, not about
            # whether the caller means this plan.
            is_verbatim = _is_verbatim_repeat(row, normalized_intent)
            if is_verbatim:
                confidence = 1.0
            # The category route (see docstring) lifts the floor, and ONLY
            # the floor, for builtin templates whose verb the caller
            # derived. Such a candidate keeps walking every gate below;
            # it is diverted into category_pool at the end rather than
            # into `scored`, so it can never outrank a real cosine hit.
            below_floor = confidence < min_confidence
            routable = (
                below_floor
                and category_verb is not None
                and _is_category_plan(row.get("tags"))
                and _verbs_compatible(_plan_verb(row), category_verb)
            )
            if below_floor and not routable:
                continue
            if _is_always_failing(row):
                # nexus-vtp8h: every recorded run failed — skip rather than
                # hand the runner a plan that only ever crashes. Attrition is
                # counted, not over-fetch-compensated (reviewer Medium,
                # recorded as accepted): `nx plan hygiene` retires these
                # durably, so cosine-hit domination by always-failing rows is
                # a transient pre-hygiene state, and the counter below makes
                # it observable rather than silently under-delivering n.
                always_failing_drops += 1
                continue
            # RDR-090 P1.3: grown plans need a higher cosine floor so
            # broad scaffolding ('which RDR…') in their match-text
            # cannot fire against unrelated questions. Caller floor
            # always wins when stricter.
            if _is_grown_plan_tags(row.get("tags")):
                grown_floor = max(min_confidence, _GROWN_PLAN_MIN_CONFIDENCE)
                if confidence < grown_floor:
                    continue
            m = Match.from_plan_row(row, confidence=confidence)
            if filter_dims and not _superset(m.dimensions, filter_dims):
                continue
            # nexus-mz5tv: a scoped caller must not match an unanchored
            # grown plan (no scope_tags AND no project) — it has no
            # provenance and would otherwise compete globally at the
            # grown floor inside the unrelated-same-domain band.
            # nexus-7g0rg: a byte-identical repeat of the question that
            # GREW this plan carries its own provenance — that is what the
            # anchor requirement is trying to establish, so demanding a
            # scope tag as well is asking for a weaker proof of a fact
            # already in hand. Measured live: a grown plan at cosine 0.9400
            # was dropped here while every other candidate scored <= 0.240,
            # so the caller paid the inline planner for a question the
            # library had already answered.
            if scope_pref_is_real and not is_verbatim and _is_unanchored_grown(row):
                scope_conflict_drops += 1
                continue
            fit = _scope_fit(m.scope_tags, scope_pref)
            if fit is None:
                scope_conflict_drops += 1
                continue  # scope conflict — drop from pool
            # nexus-0yrjr: never offer a plan the caller cannot run. A
            # typed binding (content_type, author, ...) has an enumerated
            # or numeric domain and cannot be inferred from the question,
            # so a plan requiring one the caller has not supplied is
            # unrunnable — offering it is what produced a zero-result
            # answer from an otherwise-correct type-scoped plan.
            unmet = _unmet_typed_binding(m, available_bindings)
            if unmet is not None:
                binding_drops.append((m.plan_id, unmet))
                continue
            if routable:
                # Below the floor, but a category plan the caller's verb
                # asked for. Held aside — never mixed into `scored`, so it
                # cannot displace or outrank a genuine cosine hit.
                category_pool.append(
                    (confidence, m.name == "default", m.plan_id, m)
                )
                continue
            adjusted = confidence * (1.0 + _SCOPE_FIT_WEIGHT * fit)
            scored.append((adjusted, _specificity(m.scope_tags), m))

        # nexus-czoeu: report EVERY drop reason before either exit, not
        # only on the fallthrough. All three counters previously logged
        # after the early return below, so a drop was invisible whenever
        # a runnable sibling won — the common case, and the one where a
        # degraded (rather than absent) match is what an operator needs
        # to understand. binding_drops was fixed first (nexus-eedj4);
        # these two carried the identical gap it was copied from.
        _log_t1_drops(
            hits=hits,
            scope_conflict_drops=scope_conflict_drops,
            always_failing_drops=always_failing_drops,
            scope_pref=scope_pref,
        )
        _log_binding_drops(binding_drops)

        if scored:
            scored.sort(key=lambda x: (x[0], x[1]), reverse=True)
            matches = [m for _, _, m in scored[:n]]
            for m in matches:
                # a2 fix-pass: record the RAW cosine score (pre-
                # verbatim-override) to match_conf_sum, not m.confidence
                # (which stays 1.0 for gating on the returned Match —
                # unchanged). Falls back to m.confidence only if the
                # dict lookup somehow misses (defensive; every scored
                # candidate's plan_id is populated in the same loop
                # iteration that produced it, so this should not fire).
                library.increment_match_metrics(
                    m.plan_id,
                    confidence=raw_confidence_by_plan_id.get(m.plan_id, m.confidence),
                )
            return matches

        if category_pool:
            # Cosine scored nothing. Route by dimension instead: among the
            # builtin templates the caller's verb selects, take the best
            # RELATIVE cosine. Absolute cosine is meaningless here — the
            # whole pool sits below the floor by construction — but the
            # ordering inside a same-verb pool still tracks which template
            # fits the question, which is what separates a discovery-shaped
            # question from a synthesis-shaped one.
            category_pool.sort(reverse=True)
            raw, _, _, best = category_pool[0]
            routed = replace(best, confidence=None)
            # confidence=None is the existing "admitted, but not by a cosine
            # score" sentinel (the FTS path sets it for the same reason).
            # Downstream, _nx_answer_match_is_hit passes None unconditionally
            # and choose_within_band returns matches[0] unchanged.
            _log_category_route(routed, raw_confidence=raw, verb=category_verb)
            # Record the RAW cosine, never the sentinel — match_conf_sum is a
            # permanent aggregate and must keep measuring embedding quality,
            # not gating decisions (the same rule the verbatim-repeat
            # override follows).
            library.increment_match_metrics(routed.plan_id, confidence=raw)
            return [routed]

    # FTS5 fallback: either cache unavailable or T1 returned no hits.
    # Over-fetch so the dimension post-filter doesn't starve the caller.
    _fts_over = max(n * 2, n + len(filter_dims) * 3)
    if scope_pref:
        _fts_over += n
    if available_bindings is not None:
        # Symmetry with the T1 budget above: every other drop reason on
        # this path carries compensating headroom, so binding drops must
        # too or a caller asking for n silently receives fewer.
        _fts_over += n
    rows = library.search_plans(intent, limit=_fts_over, project=project)
    matches: list[Match] = []
    for row in rows:
        if _is_always_failing(row):
            # nexus-vtp8h: same skip on the FTS5 path.
            continue
        m = Match.from_plan_row(row, confidence=None)
        if filter_dims and not _superset(m.dimensions, filter_dims):
            continue
        # nexus-mz5tv: same unanchored-grown drop on the FTS5 path — an
        # empty-project grown plan otherwise passes search_plans' project
        # filter and _scope_fit's agnostic-neutral keep.
        if scope_pref_is_real and _is_unanchored_grown(row):
            continue
        if _scope_fit(m.scope_tags, scope_pref) is None:
            continue  # scope conflict on the fallback path too
        # nexus-0yrjr: same unrunnable-plan drop on the FTS5 path.
        unmet = _unmet_typed_binding(m, available_bindings)
        if unmet is not None:
            binding_drops.append((m.plan_id, unmet))
            continue
        matches.append(m)
        if len(matches) >= n:
            break

    _log_binding_drops(binding_drops)
    for m in matches:
        library.increment_match_metrics(m.plan_id, confidence=None)
    return matches
