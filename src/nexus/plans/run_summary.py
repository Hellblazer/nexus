# SPDX-License-Identifier: AGPL-3.0-or-later
"""The nx_answer run summary (RDR-232 Phase 3, nexus-jypmj.3.5): what one run records about its evidence
coverage, and how the client knows whether it was kept.

The summary is one JSON object stored in ``nx_answer_runs.run_summary`` (engine half: nexus-jypmj.3.4).
:func:`build_run_summary` makes it from values the run already holds; :class:`RunSummaryRecorder` is the
per-call object ``nx_answer`` threads to every telemetry write, which decides whether the engine can take the
field, reads it back, and tells the envelope what happened.

**Schema, version 1.** Top level: ``version``, ``kind`` (``nx_answer`` or ``continuation_report``),
``outcome``, ``trace``, the four coverage counts ``coverage_complete`` / ``coverage_partial`` /
``coverage_unavailable`` / ``coverage_unassessed`` (one of them is 1 for a run that reached its terminal
reduction, so a reader can sum them across runs; all four are 0 for a run that never got there),
``coverage_state`` (the state name, or ``none``), ``coverage`` (the ledger's counts, no text), ``request``,
``eligibility``, ``streams``, ``scope``, ``repair`` and ``run`` (plan id, step count, latency, cost). A block
whose source the run never had is ``None``. A summary that had to be cut to fit the size cap carries
``truncated``, the names of the blocks it dropped. Additive keys keep the version; a change in meaning bumps it.

**Redaction.** The question never appears. ``trace=False`` also drops facet text, source hints, the topic
sentence and every source label; ids, counts and closed names stay. The summary is built from the same
objects the envelope reports and takes the same ``trace`` the run log does.

**Persistence.** The engine silently discards a ``run_summary`` it does not know, so HTTP 200 proves nothing.
The state is one of ``persisted`` (read back, same version and digest), ``dropped`` (written, but the row came
back without it or with another digest, or the write failed), ``unsupported`` (the engine does not advertise
``nx_answer_run_summary_supported``, so nothing was sent), ``unverified`` (sent, readback not asked for) and
``not_recorded`` (no write happened). A ``structured`` call asks for the readback; a text call does not.

**Digest.** SHA-256 over :func:`canonical_json`: object keys sorted recursively, compact separators, UTF-8
not escaped, and one number rule. PostgreSQL ``jsonb`` re-orders keys and returns numbers as the engine
parsed them (a float comes back BigDecimal-parsed, an integral ``3.0`` may come back ``3``), so the digest is
over the parsed value, never the bytes. The rule: ``bool`` is its own type; an integral number (``3``, ``3.0``,
``-0.0``) with magnitude below 2**53 is the integer; any other finite float is itself; NaN and the
infinities are refused. The builder never emits a number it could not recover, and the cost it records is
rounded to 6 places so a decimal round trip is exact.
"""
from __future__ import annotations

import copy
import hashlib
import json
import math
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass
from datetime import datetime
from typing import Any, Literal

import structlog

_log = structlog.get_logger(__name__)

__all__ = [
    "ENGINE_RUN_SUMMARY_LIMIT_BYTES",
    "RUN_SUMMARY_MAX_BYTES",
    "RUN_SUMMARY_VERSION",
    "PersistenceState",
    "RunSummaryInputs",
    "RunSummaryRecorder",
    "build_run_summary",
    "canonical_json",
    "confirm_persistence",
    "fit_summary",
    "summary_digest",
]

RUN_SUMMARY_VERSION = 1

#: The engine's own cap (``TelemetryHandler.MAX_RUN_SUMMARY_BYTES``): a summary over it is a 400 that loses
#: the whole run record.
ENGINE_RUN_SUMMARY_LIMIT_BYTES = 65_536
#: What the client allows itself: 8 KiB below the engine cap, for the difference between this serialization
#: and Jackson's (number formatting, escapes).
RUN_SUMMARY_MAX_BYTES = 57_344

PersistenceState = Literal["persisted", "dropped", "unsupported", "unverified", "not_recorded"]

#: Facets and hints listed in the summary, and the text kept per facet. The counts are always the whole.
_MAX_FACETS_LISTED = 24
_MAX_HINTS_LISTED = 20
_MAX_TEXT_CHARS = 200
#: Integral numbers below this are integers under the canonical number rule (the range a double holds exactly).
_EXACT_INT_LIMIT = 2**53

_COVERAGE_STATES = ("complete", "partial", "unavailable", "unassessed")

#: ``unsupported`` is the normal answer on every engine that predates 0.1.167, so its warning is given once
#: per process rather than once per call (the envelope says it on every structured call). Tests reset it.
_unsupported_warned = False

#: Optional blocks, cheapest to lose first, for :func:`fit_summary`.
_CUT_LADDER: tuple[str, ...] = (
    "request.source_hints", "request.facets", "streams", "eligibility", "request", "coverage",
)
#: What survives the last-resort cut.
_KEEP_ALWAYS = (
    "version", "kind", "outcome", "trace", "coverage_complete", "coverage_partial", "coverage_unavailable",
    "coverage_unassessed", "coverage_state", "run",
)


# ── canonical form and digest ────────────────────────────────────────────────────────────────────────────


def _canon(value: Any) -> Any:
    if value is None or isinstance(value, (str, bool)):
        return value
    if isinstance(value, int):
        return value
    if isinstance(value, float):
        if not math.isfinite(value):
            raise ValueError(f"run summary numbers must be finite, got {value!r}")
        if value.is_integer() and abs(value) < _EXACT_INT_LIMIT:
            return int(value)
        return value
    if isinstance(value, Mapping):
        out: dict[str, Any] = {}
        for key in value:
            if not isinstance(key, str):
                raise ValueError(f"run summary object keys must be strings, got {key!r}")
        for key in sorted(value):
            out[key] = _canon(value[key])
        return out
    if isinstance(value, (list, tuple)):
        return [_canon(v) for v in value]
    raise ValueError(f"run summary values must be JSON plain, got {type(value).__name__}")


def canonical_json(value: Any) -> str:
    """The canonical JSON text of *value* (see the module docstring for the number rule)."""
    return json.dumps(_canon(value), sort_keys=True, separators=(",", ":"), ensure_ascii=False, allow_nan=False)


def summary_digest(value: Any) -> str:
    """``sha256:<hex>`` over the canonical JSON of *value*."""
    return "sha256:" + hashlib.sha256(canonical_json(value).encode("utf-8")).hexdigest()


def _size(value: Any) -> int:
    return len(canonical_json(value).encode("utf-8"))


# ── building ─────────────────────────────────────────────────────────────────────────────────────────────


@dataclass(frozen=True)
class RunSummaryInputs:
    """The run's live state at the moment a record is written: the request's derived coverage, the
    eligibility account (``EligibilityFilter.to_wire()``), the evidence diagnostics and the scope diagnostics."""

    request: Any = None
    eligibility: Mapping[str, Any] | None = None
    diagnostics: Mapping[str, Any] | None = None
    scope_diagnostics: Sequence[Mapping[str, Any]] = ()


def _num(value: Any, *, places: int | None = None) -> int | float | None:
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        return None
    if isinstance(value, float):
        if not math.isfinite(value):
            return None
        return round(value, places) if places is not None else value
    return value


def _count(value: Any) -> int:
    n = _num(value)
    return int(n) if n is not None else 0


def _text(value: Any) -> str:
    return str(value).replace("\x00", "")[:_MAX_TEXT_CHARS]


def _coverage_block(coverage: Any) -> dict[str, Any] | None:
    if coverage is None:
        return None
    block = dict(coverage.counts())
    assessment = coverage.ledger.assessment
    block["assessment_latency_ms"] = assessment.latency_ms
    block["assessment_skipped_items"] = assessment.skipped_items
    return block


def _request_block(request: Any, trace: bool) -> dict[str, Any] | None:
    if request is None:
        return None
    facets = list(request.facets)
    hints = list(request.source_hints)
    listed: list[dict[str, Any]] = []
    for f in facets[:_MAX_FACETS_LISTED]:
        item: dict[str, Any] = {"id": f.id, "origin": f.origin, "topics": list(f.topics)}
        if trace:
            # A whole_question facet's text is the question itself, which the summary never carries
            # (nexus-jypmj.3.16); the row's own question column holds it on a traced run.
            if f.origin != "whole_question":
                item["text"] = _text(f.text)
            item["source_hints"] = [_text(h) for h in f.source_hints[:_MAX_HINTS_LISTED]]
        listed.append(item)
    block: dict[str, Any] = {
        "answer_kind": request.answer_kind,
        "kind_decision": request.kind_decision,
        "facet_count": len(facets),
        "facets": listed,
        "constraint_count": len(request.constraints),
        "constraint_kinds": sorted({c.kind for c in request.constraints}),
        "named_source_count": len(request.named_sources),
        "source_origin": request.source_origin,
        "source_hint_count": len(hints),
        "facets_certain": bool(request.facets_certain),
        "wants_lifecycle_stream": bool(request.wants_lifecycle_stream),
        "extraction": {
            "outcome": request.extraction.outcome, "calls": request.extraction.calls,
            "latency_ms": request.extraction.latency_ms,
            "cost_usd": _num(request.extraction.cost_usd, places=6),
        },
        "derive_ms": request.derive_ms,
    }
    if trace:
        block["source_hints"] = [_text(h.text) for h in hints[:_MAX_HINTS_LISTED]]
    return block


def _eligibility_block(wire: Mapping[str, Any] | None) -> dict[str, Any] | None:
    if wire is None:
        return None
    branches = wire.get("branches") or {}
    returned = wire.get("returned")
    return {
        "request_kind": wire.get("request_kind"),
        "enforced": bool(wire.get("enforced")),
        "shadow_reason": wire.get("shadow_reason"),
        "excluded_total": _count(wire.get("excluded_total")),
        "branches": {
            name: {"examined": _count(b.get("examined")), "excluded": _count(b.get("excluded"))}
            for name, b in branches.items()
        },
        "returned": dict(returned) if isinstance(returned, Mapping) else None,
        "fallback_reason": wire.get("fallback_reason"),
    }


def _streams_block(diagnostics: Mapping[str, Any] | None) -> dict[str, Any] | None:
    streams = (diagnostics or {}).get("streams") if diagnostics else None
    if not isinstance(streams, Mapping):
        return None
    out: dict[str, Any] = {}
    for name, s in streams.items():
        if not isinstance(s, Mapping):
            continue
        entry: dict[str, Any] = {"references": _count(s.get("references"))}
        for key in ("in_scope", "out_of_scope"):
            if key in s:
                entry[key] = _num(s[key])  # None where the scope check did not run
        out[name] = entry
    return out


def build_run_summary(
    inputs: RunSummaryInputs, *, outcome: str, plan_id: int | None, step_count: int, duration_ms: int,
    cost_usd: float | None, trace: bool, coverage: Any, terminal_reached: bool, kind: str = "nx_answer",
) -> dict[str, Any]:
    """The version-1 summary for one run. *coverage* is the run's terminal ``CoverageResult`` or ``None``;
    *terminal_reached* says the run got as far as its terminal reduction, which decides whether a missing
    coverage is ``unassessed`` (reached, not assessed) or ``none`` (never reached). Always JSON plain and
    under :data:`RUN_SUMMARY_MAX_BYTES`."""
    state = coverage.state if coverage is not None else ("unassessed" if terminal_reached else "none")
    attempts = max(coverage.ledger.rounds - 1, 0) if coverage is not None else 0
    conflicts = sum(1 for d in inputs.scope_diagnostics if isinstance(d, Mapping) and d.get("outcome") == "conflict")
    summary: dict[str, Any] = {
        "version": RUN_SUMMARY_VERSION,
        "kind": kind,
        "outcome": outcome,
        "trace": bool(trace),
        **{f"coverage_{s}": int(state == s) for s in _COVERAGE_STATES},
        "coverage_state": state,
        "coverage": _coverage_block(coverage),
        "request": _request_block(inputs.request, trace),
        "eligibility": _eligibility_block(inputs.eligibility),
        "streams": _streams_block(inputs.diagnostics),
        "scope": {"conflicts": conflicts, "diagnosed_steps": len(inputs.scope_diagnostics)},
        "repair": {
            "attempts": attempts,
            "facets_repairable": len(coverage.repair_facet_ids) if coverage is not None else 0,
        },
        "run": {
            "plan_id": plan_id, "step_count": step_count, "duration_ms": duration_ms,
            "cost_usd": _num(cost_usd, places=6),
        },
    }
    return fit_summary(summary)


def _drop(summary: dict[str, Any], path: str) -> bool:
    head, _, tail = path.partition(".")
    block = summary.get(head)
    if not tail:
        if summary.get(head) is None:
            return False
        summary[head] = None
        return True
    if not isinstance(block, dict) or tail not in block:
        return False
    if tail == "facets":
        # The ids are the part of the list worth keeping; the text and hints go.
        block[tail] = [{"id": f.get("id")} for f in block[tail] if isinstance(f, dict)]
        return True
    del block[tail]
    return True


def fit_summary(summary: dict[str, Any], max_bytes: int = RUN_SUMMARY_MAX_BYTES) -> dict[str, Any]:
    """*summary* if it fits *max_bytes* (UTF-8 bytes of its canonical JSON), else a copy with optional blocks
    cut, cheapest first, and ``truncated`` naming them. Never raises on size: the last resort keeps only the
    always-kept scalars."""
    if _size(summary) <= max_bytes:
        return summary
    out = copy.deepcopy(summary)
    cut: list[str] = []
    for path in _CUT_LADDER:
        if not _drop(out, path):
            continue
        cut.append(path)
        out["truncated"] = list(cut)
        if _size(out) <= max_bytes:
            return out
    minimal = {k: out[k] for k in _KEEP_ALWAYS if k in out}
    minimal["truncated"] = [*cut, "*"]
    if _size(minimal) > max_bytes:
        minimal = {k: minimal[k] for k in ("version", "kind", "outcome") if k in minimal}
        minimal["truncated"] = ["all"]
    return minimal


# ── persistence ──────────────────────────────────────────────────────────────────────────────────────────


def _same_instant(a: Any, b: Any) -> bool:
    try:
        return datetime.fromisoformat(str(a).replace("Z", "+00:00")) == datetime.fromisoformat(str(b).replace("Z", "+00:00"))
    except ValueError:
        return False


def _telemetry_supports(telemetry: Any) -> bool:
    probe = getattr(telemetry, "_supports_nx_answer_run_summary", None)
    if not callable(probe):
        return False
    try:
        return probe() is True
    except Exception:  # noqa: BLE001 — a probe that raises reads as unsupported, the safe direction
        return False


def confirm_persistence(
    telemetry: Any, summary: Mapping[str, Any], *, question: str, created_at: str,
    final_text: str | None = None, duration_ms: int | None = None, step_count: int | None = None,
) -> tuple[PersistenceState, str, str | None]:
    """Read the run row back and compare: ``(state, detail, persisted_coverage_state)``.

    ``persisted`` only when a row of this run exists and carries a summary of the same version and digest.
    A row that came back without a summary, a different one, or no row at all is ``dropped``, and the store's
    cached ``/version`` is refreshed (an engine that lost the capability under a running client is how a
    cached flag goes stale). A readback that itself failed proves nothing either way: ``unverified``.
    The engine reports ``created_at`` to the second, so the row is found by question and second plus the
    run's text, duration and step count; when two rows still fit, the run is persisted if either holds its summary.
    """
    reader = getattr(telemetry, "_read_nx_answer_run_summary", None)
    if not callable(reader):
        return "unverified", "readback_unavailable", None
    try:
        found, candidates = reader(
            question=question, created_at=created_at, final_text=final_text, duration_ms=duration_ms,
            step_count=step_count,
        )
    except Exception as exc:  # noqa: BLE001 — telemetry never fails a retrieval
        _log.warning("nx_answer_run_summary_readback_failed", error=type(exc).__name__)
        return "unverified", f"readback_failed:{type(exc).__name__}", None
    persisted_state: str | None = None
    state: PersistenceState
    if not found:
        state, detail = "dropped", "row_not_found"
    else:
        want = summary_digest(summary)
        stored = [c for c in candidates if isinstance(c, Mapping)]
        for c in stored:
            try:
                if c.get("version") == summary.get("version") and summary_digest(c) == want:
                    return "persisted", "digest_match", str(c.get("coverage_state"))
            except ValueError:
                continue
        if not stored:
            state, detail = "dropped", "summary_missing"
        elif stored[0].get("version") != summary.get("version"):
            state, detail = "dropped", "version_mismatch"
        else:
            state, detail = "dropped", "digest_mismatch"
    refreshed = None
    refresh = getattr(telemetry, "_refresh_version_capabilities", None)
    if callable(refresh):
        try:
            refreshed = refresh()
        except Exception:  # noqa: BLE001 — best effort
            refreshed = None
    _log.warning("nx_answer_run_summary_dropped", detail=detail, capability_refreshed=refreshed)
    return state, detail, persisted_state


class RunSummaryRecorder:
    """The per-call summary state ``nx_answer`` threads to its telemetry writes.

    One per call. ``inputs`` is a callable returning the run's :class:`RunSummaryInputs` at the moment a
    record is written (the request, eligibility and diagnostics change as the call proceeds). The write sites
    call :meth:`prepare` before the write and :meth:`settle` after it; the envelope reads :meth:`to_wire`.
    """

    def __init__(
        self, *, trace: bool, readback: bool, inputs: Callable[[], RunSummaryInputs] | None = None,
        kind: str = "nx_answer",
    ) -> None:
        self.trace = trace
        self.readback = readback
        self.kind = kind
        self._inputs = inputs
        self._coverage: Any = None
        self._terminal = False
        self.persistence: PersistenceState = "not_recorded"
        self.detail = ""
        self.digest: str | None = None
        self.persisted_coverage_state: str | None = None
        self.summary: dict[str, Any] | None = None

    def observe_plan_result(self, plan_result: Any) -> None:
        """Note whether the run reached its terminal reduction, and take its coverage assessment, if any.

        A run the budget or deadline cut before the terminal reduction (``budget_exhausted_at_step`` set, no
        assessment) did not reach it: its counts are all 0 and its state is ``none``, not ``unassessed``. A cut
        after the boundary ran (an assessment exists) did reach it."""
        from nexus.plans.coverage_ledger import CoverageResult  # noqa: PLC0415 — deferred; the ledger module is heavy and the store never needs it

        coverage = getattr(plan_result, "coverage", None)
        # An isinstance, not a duck check: a test double's unconfigured attribute is a truthy stand-in.
        self._coverage = coverage if isinstance(coverage, CoverageResult) else None
        cut = isinstance(getattr(plan_result, "budget_exhausted_at_step", None), int)
        self._terminal = self._coverage is not None or not cut

    def observe_final_coverage(self, coverage: Any) -> None:
        """Replace the assessment with the same result at the state the call RETURNED.

        The boundary decides the state before generation; two rows of RDR-232 section 4's table depend on what
        generation returned (a ``partial`` or ``complete`` ledger whose prose is no usable answer is
        ``unavailable``; nexus-jypmj.3.3), and the summary must count the state the caller saw. A value that is
        not a ``CoverageResult`` is ignored.
        """
        from nexus.plans.coverage_ledger import CoverageResult  # noqa: PLC0415 — deferred, as in observe_plan_result

        if isinstance(coverage, CoverageResult):
            self._coverage = coverage

    @property
    def returned_coverage_state(self) -> str:
        if self._coverage is not None:
            return str(self._coverage.state)
        return "unassessed" if self._terminal else "none"

    def coverage_wire(self, *, enabled: bool, facets: int) -> dict[str, Any]:
        """The envelope's top-level ``coverage`` object (RDR-232 section 4), always a dict.

        The assessed run's :meth:`CoverageResult.to_wire` at the state :meth:`observe_final_coverage` last
        recorded, so it cannot differ from :attr:`returned_coverage_state`; redacted when the call is untraced.
        A run with no assessment gets the same keys with state ``unassessed`` and the reason it was not
        assessed: ``switch_off`` (*enabled* is false), ``no_facets`` (*facets* is 0), ``terminal_reduction_not_reached``
        (an error before it, the single-query fast path) or ``boundary_not_run`` (it was reached, and the runner
        returned no assessment).
        """
        from nexus.plans.coverage_ledger import CoverageLedger, CoverageResult  # noqa: PLC0415 — deferred, as in observe_plan_result

        if self._coverage is not None:
            return self._coverage.to_wire(redact=not self.trace)
        reason = (
            "switch_off" if not enabled else "no_facets" if facets <= 0
            else "terminal_reduction_not_reached" if not self._terminal else "boundary_not_run"
        )
        return CoverageResult("unassessed", reason, False, CoverageLedger()).to_wire(redact=not self.trace)

    def _set(self, state: PersistenceState, detail: str) -> None:
        self.persistence = state
        self.detail = detail

    def prepare(
        self, telemetry: Any, *, outcome: str, plan_id: int | None, step_count: int, duration_ms: int,
        cost_usd: float | None,
    ) -> dict[str, Any] | None:
        """The summary to put on the write, or ``None``: the engine cannot take it (``unsupported``, logged)
        or it could not be built (``not_recorded``). Never raises."""
        if not _telemetry_supports(telemetry):
            self._set("unsupported", "engine does not advertise nx_answer_run_summary_supported")
            global _unsupported_warned  # noqa: PLW0603 — once-per-process latch, see its declaration
            if not _unsupported_warned:
                _unsupported_warned = True
                _log.warning(
                    "nx_answer_run_summary_unsupported_by_engine",
                    consequence="run rows are recorded without a run summary until the engine is upgraded "
                                "(logged once per process; the structured envelope reports it on every call)",
                )
            return None
        try:
            inputs = self._inputs() if self._inputs is not None else RunSummaryInputs()
            summary = build_run_summary(
                inputs, outcome=outcome, plan_id=plan_id, step_count=step_count, duration_ms=duration_ms,
                cost_usd=cost_usd, trace=self.trace, coverage=self._coverage, terminal_reached=self._terminal,
                kind=self.kind,
            )
            self.digest = summary_digest(summary)
        except Exception as exc:  # noqa: BLE001 — a summary that cannot be built is dropped, never a retrieval failure
            _log.warning("nx_answer_run_summary_build_failed", error=type(exc).__name__)
            self._set("not_recorded", f"build_failed:{type(exc).__name__}")
            return None
        self.summary = summary
        return summary

    def settle(
        self, telemetry: Any, summary: Mapping[str, Any] | None, *, question: str, created_at: str | None,
        sent: bool, error: BaseException | None = None, final_text: str | None = None,
        duration_ms: int | None = None, step_count: int | None = None,
    ) -> None:
        """Record what the write did. *sent* is whether the summary went on the wire; *error* the write's
        failure, if it failed. A structured call then reads the row back, finding it by question and second
        plus *final_text*, *duration_ms* and *step_count* as written. The write methods return nothing (the
        store's frozen return shape), so *sent* is "a summary was prepared and handed to the store"; a store
        that then declined it (a capability flip inside the interval, a size it could not take) shows as
        ``dropped`` on readback."""
        try:
            self._settle(
                telemetry, summary, question=question, created_at=created_at, sent=sent, error=error,
                match={"final_text": final_text, "duration_ms": duration_ms, "step_count": step_count},
            )
        except Exception as exc:  # noqa: BLE001 — telemetry bookkeeping never fails a retrieval
            _log.warning("nx_answer_run_summary_settle_failed", error=type(exc).__name__)
            self._set("unverified", f"settle_failed:{type(exc).__name__}")

    def _settle(
        self, telemetry: Any, summary: Mapping[str, Any] | None, *, question: str, created_at: str | None,
        sent: bool, error: BaseException | None, match: Mapping[str, Any],
    ) -> None:
        if summary is None:
            return  # prepare() already set the state
        if error is not None:
            self._set("dropped", f"write_failed:{type(error).__name__}")
            return
        if not sent:
            self._set("unsupported", "store did not send the run summary")
            return
        if not self.readback or created_at is None:
            self._set("unverified", "readback_not_requested")
            return
        state, detail, persisted_state = confirm_persistence(
            telemetry, summary, question=question, created_at=created_at, **match,
        )
        self._set(state, detail)
        self.persisted_coverage_state = persisted_state

    def to_wire(self) -> dict[str, Any]:
        """The envelope's ``run_summary`` object: the returned and the persisted coverage states apart."""
        return {
            "version": RUN_SUMMARY_VERSION,
            "persistence": self.persistence,
            "detail": self.detail,
            "returned_coverage_state": self.returned_coverage_state,
            "persisted_coverage_state": self.persisted_coverage_state,
            "digest": self.digest,
        }
