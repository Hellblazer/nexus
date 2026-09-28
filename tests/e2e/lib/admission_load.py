# SPDX-License-Identifier: AGPL-3.0-or-later
# Copyright (c) 2026 Hal Hildebrand. All rights reserved.
"""Driver for ``tests/e2e/admission-load-gate.sh`` (nexus-u2mlh.9).

Proves that the cloud stack sheds concurrent CCE embed load with fast
refused 503s (``X-Nexus-Deadline-Outcome: refused``) and never with a
proxy cut, measured on THIS run's own responses through the public edge.

What it does NOT prove, since conexus-vtlr (2026-09-27): that the ENGINE's
admission control fires. The edge now admits about 16 embed bodies at a
time and refuses the rest itself; at that concurrency the engine's 12
threads never queue past its own refusal threshold, so through the public
edge the engine's admission is a backstop the gate cannot reach. Engine
admission is proven by its engine tests and by the 2026-09-26 128-way run
(admission_refusals_total 0 -> 104, T2
nexus/u2mlh-admission-load-gate-run-2026-09-26), before the edge
refused. An engine refusal in a step is still reported, as a signal that
the edge let through more than the engine could hold. Close check for
nexus-u2mlh.10 and the epic nexus-u2mlh.

Assumptions and scope, read before changing the pass criterion or the
network layer:

* **PASS criterion (nexus-u2mlh.10, 2026-09-27; was fix round [27132]
  C1/C2):** a step whose OWN raw responses include at least one 503
  carrying ``X-Nexus-Deadline-Outcome: refused`` and no proxy timeout
  (:data:`TIMEOUT_STATUSES`); any timeout in the ramp fails the run. The
  engine counter no longer has to move: since conexus-vtlr the edge
  refuses on saturation with the same header, often before the engine
  does, and each step reports the engine/edge split. See
  :func:`evaluate_step`. ``deadline_aborts_total`` is
  a genuinely distinct counter at a genuinely distinct call site
  (``CceEmbedder.deadlineAbort`` vs. ``CceEmbedder.admit``'s ``refuse``)
  and is nexus-u2mlh.3's mechanism, already closed; it is reported
  per-step for corroboration only and never satisfies this gate on its
  own. A step where ``admission_refusals_total`` moved but no ``refused``
  503 appeared in this run's own responses is reported as UNATTRIBUTABLE
  and fails the whole run immediately (further steps could not make the
  attribution problem go away).

* **Single-engine-JVM assumption.** The counters this gate reads
  (``GET /v1/status``'s ``embedder_activity``) are PROCESS-GLOBAL, held
  by one in-memory ``ActivityTracker`` in one engine JVM (RDR-205's
  2026-09-09 research answer: "one engine JVM with stop-then-start
  deploys"). Before/after snapshots are only meaningful because there is
  exactly one JVM behind the edge today. If the deploy topology ever adds
  a second replica, per-JVM counters make this gate's before/after diff
  meaningless with no warning from the script itself — this comment is
  that warning, since nothing here can detect a topology change on its
  own.

* **The throwaway subject is a documented exception to docs/collections.md
  Rule 1.** Rule 1 explicitly excludes "a session or task" as a valid
  knowledge-collection subject; ``u2mlh-load-<nonce>`` (see
  :func:`load_collection_subject`) is exactly that shape. This is
  deliberate: the collection is created and destroyed within one run
  (registered right before the ramp, deleted in :func:`run_gate`'s own
  ``finally`` on every exit path this process can control — see the
  Interrupts note below for what it cannot control), never a durable
  subject a human would browse, so Rule 1's own rationale ("a subject a
  reader would browse for a long time") does not apply. It is visible via
  the normal collection-listing routes to any other user of the shared
  install for the run's duration, which is why :func:`find_orphan_collections`
  reports (never deletes) any PRIOR run's leftover collections under the
  same prefix at startup.

* **The client's RateLimitBrake / Retry-After handling is deliberately
  NOT exercised here.** This driver speaks raw, un-retried HTTP — see the
  point below on why — so ``nexus.rate_brake.RateLimitBrake`` and
  ``nexus.retry``'s Retry-After floor never engage on this path. That
  half of nexus-u2mlh.2's original scope (the CLIENT honouring
  Retry-After) is covered at the unit level only, by
  ``tests/test_vector_retry.py::test_admission_refusal_503_retry_after_floors_the_shared_brake``,
  never by this or any other E2E gate.

* **conexus-26b5 is LIVE** (since 2026-09-24 19:37Z): the public edge
  clamps ``X-Nexus-Request-Deadline-Ms`` to 50000 on
  ``/v1/vectors/upsert-chunks`` (and ``write_many``/``store-put``)
  regardless of what this driver sends. Admission is therefore live and
  reachable through the edge, not inert — a run that sees neither counter
  move is evidence about admission control, not about whether the clamp
  itself has shipped.

* **Cost.** The full worst-case ramp (32+64+128+256 = 480 documents at the
  default 12KB target) sends roughly 5.9MB of text, on the order of
  1.3-1.4M tokens, all billed to Voyage — before a PASS can even be
  declared if the ramp never stops early. The wall-clock cap below bounds
  TIME, not COST; a run that reaches the cap without a pass has still
  spent up to that much.

Two design choices worth reading before changing the network layer:

1. **Raw HTTP, never ``HttpVectorClient.upsert_chunks()``.** That client's
   own layered retry (``nexus.retry._vector_with_retry`` on top of
   ``http_vector_client._request``'s inner gateway retry) transparently
   retries a plain 503 refusal up to three times *inside* one call before
   any of it reaches caller code, and even the one 503 shape that DOES
   propagate immediately (a deadline abort, ``X-Nexus-Deadline-Outcome:
   aborted``) loses its headers by the time ``VectorServiceError`` reaches
   the caller (it carries only ``code``/``edge_refusal``). The evidence
   this gate exists to produce — the raw status code, ``Retry-After``, and
   ``X-Nexus-Deadline-Outcome`` per attempt — needs one un-retried POST per
   request, so :func:`post_upsert` talks to ``/v1/vectors/upsert-chunks``
   directly. The shared process-wide rate-limit brake
   (``nexus.rate_brake``) is not engaged by this path either, deliberately
   (see above).

2. **Auth reuses ``HttpVectorClient._request_once``'s own bearer
   resolution** (:func:`resolve_bearer`, calling the same
   ``get_data_token_manager().bearer_for(base_url, tenant)`` that private
   function calls) rather than sending only the static ``service_token`` —
   fix round, code-review critique [27134] I3: a box with self-minting
   configured (``mint_token`` credential, RDR-005 2a) authenticates real
   write traffic on a short-TTL minted data token, never the static token
   alone, and this driver now matches that.

3. **Collection lifecycle reuses the real write path's own functions**
   (``nexus.corpus.t3_collection_name`` / ``ensure_collection_registered``,
   ``nexus.db.collection_purge.purge_collection_cascade``) rather than
   re-deriving names or hand-rolling a delete.

Safety rails added in the fix round (substantive [27132] C3, code-review
[27134] I2): an overall wall-clock cap (default 6 minutes,
``--wall-clock-cap-s`` overridable) that stops dispatching new load and
cancels queued-but-unstarted requests once reached; a two-read idle
pre-check (``active=false`` and ``queue_depth<=0`` a few seconds apart)
before any write, so this driver never piles onto real traffic already in
flight; an upfront ``GET /v1/status`` readability check before any write,
so a scoping problem is reported as "status unreadable" rather than
spending the full ramp's cost on a run that could never have detected
movement; and a per-step transport-error-fraction check
(:func:`is_step_valid`) that marks a step's own measurement untrustworthy
rather than letting connection-pool exhaustion or transport failures read
as "no load-induced counter movement".

**Interrupts, disclosed honestly.** :func:`run_gate` catches
``BaseException`` (not just ``Exception``) so ``Ctrl-C`` mid-ramp still
writes a result and runs cleanup, and :func:`fire_step` shuts its pool
down with ``shutdown(wait=False, cancel_futures=True)`` on both a
wall-clock timeout and an interrupt. This is best-effort, not a hard
guarantee: Python's ``ThreadPoolExecutor`` cannot forcibly kill an
ALREADY-RUNNING worker thread — ``cancel_futures=True`` only discards work
that had not yet started, and any request already in flight keeps running
in the background (bounded by its own ``httpx`` timeout) even after this
function returns; the interpreter will still wait for those non-daemon
threads at process exit. A harder kill (SIGKILL/OOM) skips ``finally``
entirely, which is exactly why :func:`find_orphan_collections` exists —
report a prior run's leftover collection rather than pretend that risk is
zero.

Pure functions (no network, unit-tested in ``tests/test_admission_load
_gate.py`` with ``httpx.MockTransport`` for the network-facing ones):
counter extraction/comparison, per-step pass evaluation, ramp-stop
decision, idle-precheck logic, nonce/subject/document generation,
response summarization, transport-error validity. Everything that
resolves a live endpoint or sends a request is a thin, deliberately
un-pure wrapper around those, with an injectable client/fetch/sleep seam
so the whole orchestration in :func:`run_gate` is itself testable without
touching a real network.
"""
from __future__ import annotations

import argparse
import dataclasses
import hashlib
import json
import re
import time
import uuid
from collections.abc import Callable, Sequence
from concurrent.futures import ThreadPoolExecutor
from concurrent.futures import wait as futures_wait
from dataclasses import dataclass
from typing import Any

import httpx

#: The one embedder every CCE write in this gate targets (knowledge
#: collections embed with voyage-context-3 — see AGENTS.md's collection
#: table).
DEFAULT_EMBEDDER = "voyage-context-3"

#: Concurrency steps tried, in order, stopping at the first that passes.
#: bead nexus-u2mlh.9's own sizing note: thread_width=12 today and the
#: edge clamps the request deadline to ~50s on this route, so admission
#: refusal needs on the order of 70-200 concurrent document-sized
#: upserts — comfortably inside the 256 ceiling below.
DEFAULT_RAMP_STEPS: tuple[int, ...] = (32, 64, 128, 256)

#: Document-size bounds (bytes) — "document-sized", per the bead: large
#: enough that a single request is a real CCE batch, small enough that 256
#: of them in flight is a load test, not a memory test.
MIN_DOCUMENT_BYTES = 6 * 1024
MAX_DOCUMENT_BYTES = 24 * 1024
DEFAULT_TARGET_BYTES = 12 * 1024

#: Advisory client-declared embed budget (milliseconds), matching
#: ``HttpVectorClient``'s own ``_UPSERT_CHUNKS_DEADLINE_MS`` for this route
#: — the public edge clamps it to 50000ms regardless of what is sent
#: (conexus-26b5, live since 2026-09-24 19:37Z).
DEFAULT_REQUEST_DEADLINE_MS = 540_000

#: Per-request read timeout: comfortably above the edge's 50s deadline
#: clamp so a genuine 503 (refused fast, or aborted at the deadline) is
#: observed rather than a client-side read timeout racing it.
DEFAULT_REQUEST_TIMEOUT_S = 70.0

#: Extra connection-pool headroom above the largest ramp step (code-review
#: [27134] finding I1: httpx.Client's DEFAULT pool cap is 100 connections,
#: which silently throttled every step above it — the 256-concurrency step
#: most of all, exactly where the sizing note says the real threshold is
#: likeliest to sit).
CONNECTION_POOL_HEADROOM = 16

#: Explicit pool-acquire timeout, separate from the per-request read
#: timeout: with the pool sized to comfortably exceed every ramp step
#: (see CONNECTION_POOL_HEADROOM), genuine pool exhaustion is a bug, and
#: this makes it fail fast rather than block up to DEFAULT_REQUEST_TIMEOUT_S.
POOL_ACQUIRE_TIMEOUT_S = 15.0

#: Overall wall-clock cap on the whole ramp (fix round, substantive
#: [27132] C3 / code-review [27134] I2): the only rail that bounds how
#: long this driver can spend degrading the one shared production
#: environment, independent of how many ramp steps are configured.
DEFAULT_WALL_CLOCK_CAP_S = 360.0

#: Gap between the two idle-precheck reads.
DEFAULT_IDLE_CHECK_GAP_S = 3.0

#: A step is untrustworthy once more than this fraction of its responses
#: are transport errors (status_code == 0) — connection-pool exhaustion or
#: a genuine network blip must never read as "load without movement".
MAX_TRANSPORT_ERROR_FRACTION = 0.1

#: The one tenant every install uses today (see
#: ``http_vector_client._process_default_tenant``'s own docstring: a named
#: constant, not a real per-install lookup).
TENANT = "default"

#: Every collection this gate has ever minted starts with this prefix —
#: the marker :func:`find_orphan_collections` scans for.
ORPHAN_PREFIX = "knowledge__u2mlh-load-"

#: Subject-naming grammar, docs/collections.md Rule 3: lowercase,
#: hyphen-separated, ASCII, two-to-three words.
_SUBJECT_RE = re.compile(r"^[a-z0-9]+(?:-[a-z0-9]+){1,2}$")


class AdmissionLoadVacuousError(RuntimeError):
    """No step shed load with an observed refused 503, or the ramp saw a
    proxy cut (see :class:`AdmissionLoadCutError`)."""


class AdmissionLoadCutError(AdmissionLoadVacuousError):
    """A request was cut (502/504, or a transport-level failure such as a
    reset keep-alive connection) instead of refused: the 2026-09-24
    incident shape. Fails the run whatever else a step showed."""


class AdmissionLoadUnattributableError(RuntimeError):
    """admission_refusals_total moved during a step, but no refused 503
    appeared in this run's own responses at that step — the movement is
    not attributable to this driver's load."""


class AdmissionLoadPreflightError(RuntimeError):
    """The upfront status-readability or idle pre-check failed before any
    write was attempted."""


# ── Pure: counters + idle pre-check ─────────────────────────────────────


@dataclass(frozen=True)
class EmbedderCounters:
    admission_refusals_total: int = 0
    deadline_aborts_total: int = 0
    queue_depth: int = 0
    chunks_done_total: int = 0
    thread_width: int = 0
    active: bool = False


def snapshot_counters(status: dict[str, Any] | None, embedder: str = DEFAULT_EMBEDDER) -> EmbedderCounters:
    """Extract *embedder*'s counters from a ``GET /v1/status`` body.

    Tolerant of a missing/malformed body — returns all-zero/idle counters
    rather than raising, matching ``fetch_engine_status``'s own
    fail-closed contract.
    """
    if not isinstance(status, dict):
        return EmbedderCounters()
    activity = status.get("embedder_activity")
    if not isinstance(activity, dict):
        return EmbedderCounters()
    entry = activity.get(embedder)
    if not isinstance(entry, dict):
        return EmbedderCounters()

    def _int(key: str) -> int:
        value = entry.get(key, 0)
        return value if isinstance(value, int) else 0

    return EmbedderCounters(
        admission_refusals_total=_int("admission_refusals_total"),
        deadline_aborts_total=_int("deadline_aborts_total"),
        queue_depth=_int("queue_depth"),
        chunks_done_total=_int("chunks_done_total"),
        thread_width=_int("thread_width"),
        active=bool(entry.get("active", False)),
    )


def is_idle(counters: EmbedderCounters) -> bool:
    """True when *counters* show no in-flight activity and nothing
    queued — the bar this gate requires TWICE before it will write
    anything."""
    return (not counters.active) and counters.queue_depth <= 0


def resolve_idle_precheck(
    fetch: Callable[[], dict[str, Any] | None],
    embedder: str = DEFAULT_EMBEDDER,
    *,
    gap_s: float = DEFAULT_IDLE_CHECK_GAP_S,
    sleep: Callable[[float], None] = time.sleep,
) -> tuple[str | None, EmbedderCounters | None]:
    """The combined upfront scope + idle check, before any write.

    Returns ``(failure_reason, last_counters)``. *failure_reason* is
    ``None`` when the engine is confirmed idle across two reads *gap_s*
    apart; otherwise it names which precondition failed: "status
    unreadable" (either read returned nothing usable with these
    credentials — a scoping problem, not a load-induced signal) or
    "engine not idle" (the two reads disagree, or show activity/queue
    depth). *fetch* and *sleep* are injection seams so this is fully
    unit-testable with no real network or wall-clock wait.
    """
    first_status = fetch()
    if first_status is None:
        return (
            "status unreadable: GET /v1/status returned nothing usable with these "
            "credentials -- refusing to spend any load before confirming read scope",
            None,
        )
    first = snapshot_counters(first_status, embedder)
    sleep(gap_s)
    second_status = fetch()
    if second_status is None:
        return (
            "status unreadable: the second GET /v1/status probe (idle pre-check) "
            "returned nothing usable",
            None,
        )
    second = snapshot_counters(second_status, embedder)
    if not (is_idle(first) and is_idle(second)):
        return (
            f"engine not idle: {embedder} (active, queue_depth) = "
            f"({first.active}, {first.queue_depth}) then ({second.active}, {second.queue_depth}) "
            f"{gap_s}s apart -- refusing to pile onto real traffic",
            second,
        )
    return (None, second)


# ── Pure: per-step evaluation + ramp stop logic ─────────────────────────


@dataclass(frozen=True)
class RawResponse:
    status_code: int
    retry_after: float | None
    deadline_outcome: str | None
    elapsed_s: float
    error: str | None = None


def summarize_responses(responses: Sequence[RawResponse]) -> dict[str, Any]:
    """Pure evidence rollup over one ramp step's raw responses: counts by
    status code and by ``X-Nexus-Deadline-Outcome`` — the raw-503 evidence
    the client library's own transparent retry would otherwise hide (see
    this module's docstring, point 1)."""
    by_status: dict[str, int] = {}
    by_outcome: dict[str, int] = {}
    errors = 0
    for r in responses:
        key = str(r.status_code)
        by_status[key] = by_status.get(key, 0) + 1
        if r.deadline_outcome:
            by_outcome[r.deadline_outcome] = by_outcome.get(r.deadline_outcome, 0) + 1
        if r.error:
            errors += 1
    return {
        "count": len(responses),
        "by_status": by_status,
        "by_deadline_outcome": by_outcome,
        "transport_errors": errors,
    }


def is_step_valid(responses: Sequence[RawResponse]) -> bool:
    """False when more than :data:`MAX_TRANSPORT_ERROR_FRACTION` of a
    step's responses were transport errors (``status_code == 0``) — such
    a step's measurement cannot be trusted as real load (fix round,
    code-review [27134] I1/wall-clock note): connection-pool exhaustion or
    a network blip must never read as "load without movement". An empty
    response list is also invalid — nothing was measured at all."""
    if not responses:
        return False
    errors = sum(1 for r in responses if r.status_code == 0)
    return (errors / len(responses)) <= MAX_TRANSPORT_ERROR_FRACTION


#: Statuses that mean a proxy cut the request on a timeout instead of the
#: system shedding it with a refusal: the 2026-09-24 incident shape. The
#: 2026-09-26 run's five 504s came from the ALB's 60 s idle timeout
#: (nexus-u2mlh.10). A transport-level failure (``status_code == 0``: a
#: reset or read timeout, which is how an ALB idle cut on a reused
#: keep-alive connection surfaces) counts as a cut too. Any cut anywhere in
#: the ramp fails the gate, including in a step otherwise marked invalid.
TIMEOUT_STATUSES: frozenset[int] = frozenset({502, 504})


def count_cuts(responses: Sequence[RawResponse]) -> int:
    """Responses that were cut rather than answered: proxy timeout statuses
    plus transport-level failures (``status_code == 0``)."""
    return sum(1 for r in responses if r.status_code in TIMEOUT_STATUSES or r.status_code == 0)


@dataclass(frozen=True)
class StepVerdict:
    admission_moved: bool
    deadline_moved: bool
    observed_refused: bool
    passes: bool
    unattributable: bool
    refused_count: int = 0
    engine_refusals: int = 0
    edge_refusals: int = 0
    timeouts: int = 0
    counter_delta: int = 0


#: The verdict an INVALID step is forced to — never contributes a pass, an
#: unattributable failure, or a corroborating deadline-only note; its own
#: transport-error evidence is what the operator reads instead.
INVALID_STEP_VERDICT = StepVerdict(
    admission_moved=False, deadline_moved=False, observed_refused=False, passes=False, unattributable=False,
)


def evaluate_step(before: EmbedderCounters, after: EmbedderCounters, responses: Sequence[RawResponse]) -> StepVerdict:
    """The pass criterion: this step's own responses carry at least one
    ``X-Nexus-Deadline-Outcome: refused`` AND no proxy timeout
    (:data:`TIMEOUT_STATUSES`). The system must shed load with fast
    refusals, never with cuts.

    nexus-u2mlh.10 (2026-09-27): the edge now refuses on its own when
    saturated (conexus-vtlr), with the same header, so at many
    concurrencies the edge refuses before the engine ever does and
    ``admission_refusals_total`` does not move. Requiring engine movement
    would make the gate unpassable. Refusals are therefore split for the
    report, not required: ``engine_refusals`` is the counter delta and
    ``edge_refusals`` the observed refusals the engine did not count.
    Attribution rests on the refusals being this run's own responses.

    Kept from fix round [27132] C1/C2:
    ``deadline_aborts_total`` moving is reported (``deadline_moved``) but
    never sufficient on its own — that is nexus-u2mlh.3's mechanism, a
    different call site in ``CceEmbedder`` (``deadlineAbort`` vs.
    ``admit``'s ``refuse``), already closed. A step where admission moved
    but no refused 503 was observed is ``unattributable``: the movement
    cannot be pinned on this run's own load (e.g. concurrent activity
    elsewhere on the shared tenant) and must fail the run outright rather
    than let a later step's evidence paper over it."""
    admission_moved = after.admission_refusals_total > before.admission_refusals_total
    deadline_moved = after.deadline_aborts_total > before.deadline_aborts_total
    refused_count = sum(1 for r in responses if r.deadline_outcome == "refused")
    observed_refused = refused_count > 0
    timeouts = count_cuts(responses)
    # The counter is engine-global: another tenant's refused traffic in the
    # window can move it past this run's own observed refusals. Clamp the
    # engine share to what this run saw and report the raw delta beside it,
    # so the split always sums to refused_count.
    counter_delta = max(0, after.admission_refusals_total - before.admission_refusals_total)
    engine_refusals = min(counter_delta, refused_count)
    edge_refusals = refused_count - engine_refusals
    passes = observed_refused and timeouts == 0
    unattributable = admission_moved and not observed_refused
    return StepVerdict(
        admission_moved, deadline_moved, observed_refused, passes, unattributable,
        refused_count=refused_count, engine_refusals=engine_refusals,
        edge_refusals=edge_refusals, timeouts=timeouts, counter_delta=counter_delta,
    )


@dataclass(frozen=True)
class RampOutcome:
    tried: tuple[int, ...]
    stopped_at_step: int | None
    stopped_at_index: int | None
    unattributable_at_step: int | None
    deadline_only_steps: tuple[int, ...]
    timeout_steps: tuple[int, ...] = ()


def decide_ramp_outcome(steps: Sequence[int], verdicts: Sequence[StepVerdict]) -> RampOutcome:
    """Pure ramp-stop decision over each step's :class:`StepVerdict`.

    The first step whose ``unattributable`` is True stops the ramp
    immediately (further steps cannot fix an attribution problem). The
    first step whose ``passes`` is True stops the ramp with a PASS. Every
    step where ``deadline_moved`` fired without ``admission_moved`` is
    collected as corroborating-but-insufficient evidence. An all-``False``
    walk is the non-vacuity failure this gate must name explicitly.
    """
    if len(steps) != len(verdicts):
        raise ValueError(f"steps/verdicts length mismatch: {len(steps)} vs {len(verdicts)}")
    deadline_only: list[int] = []
    timeout_steps: list[int] = []
    for index, (step, verdict) in enumerate(zip(steps, verdicts)):
        if verdict.timeouts:
            timeout_steps.append(step)
        if verdict.unattributable:
            return RampOutcome(tuple(steps), None, None, step, tuple(deadline_only), tuple(timeout_steps))
        if verdict.passes and not timeout_steps:
            return RampOutcome(tuple(steps), step, index, None, tuple(deadline_only), tuple(timeout_steps))
        if verdict.deadline_moved and not verdict.admission_moved:
            deadline_only.append(step)
    return RampOutcome(tuple(steps), None, None, None, tuple(deadline_only), tuple(timeout_steps))


def require_pass(outcome: RampOutcome) -> None:
    """Raise the named failure when *outcome* is not a pass.

    :class:`AdmissionLoadUnattributableError` when a step's
    ``admission_refusals_total`` moved with no corroborating refused 503;
    :class:`AdmissionLoadVacuousError` (nexus-moht0 vacuous-gate doctrine:
    a sweep that found nothing to check is a failure, not a pass) when no
    step ever passed at all.
    """
    if outcome.timeout_steps:
        raise AdmissionLoadCutError(
            f"requests cut instead of refused (502/504 or transport-level failure) at "
            f"concurrency={list(outcome.timeout_steps)} -- the 2026-09-24 shape (nexus-u2mlh.10)"
        )
    if outcome.unattributable_at_step is not None:
        raise AdmissionLoadUnattributableError(
            f"admission_refusals_total moved at concurrency={outcome.unattributable_at_step} but no 503 "
            "carrying X-Nexus-Deadline-Outcome=refused was observed in this run's own responses at that "
            "step -- the movement is not attributable to this load (possible concurrent activity on the "
            "shared tenant)"
        )
    if outcome.stopped_at_step is None:
        note = ""
        if outcome.deadline_only_steps:
            note = (
                f" (deadline_aborts_total alone moved at concurrency={list(outcome.deadline_only_steps)} -- "
                "that is nexus-u2mlh.3's mechanism, already closed, and never satisfies this gate on its own)"
            )
        raise AdmissionLoadVacuousError(
            "no refused 503 (X-Nexus-Deadline-Outcome=refused) observed at any "
            f"concurrency step tried: {list(outcome.tried)}{note}"
        )


# ── Pure: nonce, subject naming, document generation ────────────────────


def parse_ramp_steps(raw: str) -> list[int]:
    """Parse a comma-separated concurrency list, e.g. ``"32,64,128,256"``.

    Raises ``ValueError`` on an empty list or one that is not strictly
    increasing (a non-increasing ramp cannot be read as "escalating load"
    and would make the stop-at-first-pass logic ambiguous).
    """
    steps = [int(part.strip()) for part in raw.split(",") if part.strip()]
    if not steps:
        raise ValueError("ramp-steps must name at least one concurrency value")
    if steps != sorted(steps) or len(set(steps)) != len(steps):
        raise ValueError(f"ramp-steps must be strictly increasing, got {steps}")
    return steps


def make_run_nonce(source: Callable[[], str] | None = None) -> str:
    """A lowercase-alnum run identifier, unique per invocation by default
    (``uuid4().hex[:10]``). *source* is an injection seam for deterministic
    tests — it must itself already return a lowercase-alnum string."""
    nonce = (source() if source is not None else uuid.uuid4().hex[:10]).lower()
    if not re.fullmatch(r"[a-z0-9]+", nonce):
        raise ValueError(f"run nonce must be lowercase alnum, got {nonce!r}")
    return nonce


def load_collection_subject(nonce: str) -> str:
    """The throwaway knowledge-collection SUBJECT for this run
    (``u2mlh-load-<nonce>``) — docs/collections.md Rule 3 shape (lowercase,
    hyphen-separated, ASCII, two-to-three words), and a documented,
    deliberate exception to Rule 1 (see this module's own docstring: a
    session/task-shaped subject that is registered and deleted within one
    run, never a durable browsing subject)."""
    subject = f"u2mlh-load-{nonce}"
    if not _SUBJECT_RE.match(subject):
        raise ValueError(f"generated subject {subject!r} does not fit docs/collections.md Rule 3's grammar")
    return subject


def generate_document(nonce: str, tag: str, target_bytes: int = DEFAULT_TARGET_BYTES) -> str:
    """A unique, document-sized text body for one load-step request.

    Content is derived from *nonce* + *tag* so no two calls — even
    concurrent workers within the same ramp step — can collide on chunk
    identity: the server's existing-chash embed-skip (RDR-181) must never
    elide this call's embed, or the ramp would silently stop generating
    load without any client-visible symptom.
    """
    if not (MIN_DOCUMENT_BYTES <= target_bytes <= MAX_DOCUMENT_BYTES):
        raise ValueError(
            f"target_bytes {target_bytes} outside the documented "
            f"[{MIN_DOCUMENT_BYTES}, {MAX_DOCUMENT_BYTES}] range"
        )
    header = f"admission-load-gate probe run={nonce} tag={tag}\n\n"
    parts: list[str] = [header]
    size = len(header.encode("utf-8"))
    n = 0
    while size < target_bytes:
        line = f"line {n} nonce={nonce} tag={tag} filler text sized for a real CCE embed batch\n"
        parts.append(line)
        size += len(line.encode("utf-8"))
        n += 1
    return "".join(parts)


def load_document_id(nonce: str, tag: str, target_bytes: int = DEFAULT_TARGET_BYTES) -> str:
    """Content-addressed chunk id for this call's own document — computed
    the same way a real indexer would (sha256 hex of the exact chunk
    text), so the id sent on the wire matches production's own identity
    convention (AGENTS.md: chunk natural id is the full 64-hex
    ``chunk_text_hash``)."""
    text = generate_document(nonce, tag, target_bytes)
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


# ── Network: endpoint, auth, collection lifecycle, raw upsert ───────────


def resolve_cloud_endpoint() -> tuple[str, str]:
    """``(base_url, static_token)`` for this box's configured service, via
    the same evidence-gated resolver ``GET /v1/status`` reads through
    (``nexus.db.http_engine_status.fetch_engine_status``)."""
    from nexus.db.service_endpoint import (  # noqa: PLC0415 — deferred: keeps this module importable with zero network deps for the pure-function tests
        resolve_service_endpoint_with_evidence_gate,
    )

    return resolve_service_endpoint_with_evidence_gate()


def resolve_bearer(base_url: str, tenant: str, static_token: str) -> str:
    """The bearer this box's real write path would actually send — reuses
    ``HttpVectorClient._request_once``'s own resolution
    (``get_data_token_manager().bearer_for``), never re-derived by hand: a
    self-minted data token when a ``mint_token`` credential is configured
    (RDR-005 2a), else *static_token* unchanged."""
    from nexus.db.data_token import get_data_token_manager  # noqa: PLC0415 — deferred, see resolve_cloud_endpoint

    data_token = get_data_token_manager().bearer_for(base_url, tenant)
    return data_token if data_token is not None else static_token


def guard_write(base_url: str) -> None:
    """Thin, monkeypatchable wrapper around
    ``nexus.db.service_endpoint.guard_production_write`` — every other
    write path in this driver (registration, delete) calls it internally
    already; this is the one explicit call for the raw upsert path that
    bypasses that internal plumbing."""
    from nexus.db.service_endpoint import guard_production_write  # noqa: PLC0415 — deferred, see resolve_cloud_endpoint

    guard_production_write(base_url)


def build_client(base_url: str, headers: dict[str, str], ramp_steps: Sequence[int]) -> httpx.Client:
    """The real client this driver posts through, sized so every ramp
    step's concurrency fits inside the connection pool (fix round,
    code-review [27134] finding I1): httpx.Client's DEFAULT
    ``max_connections=100`` silently throttled every step above it, the
    256-concurrency step worst of all — exactly the step most likely
    needed per this module's own sizing note. ``max_keepalive_connections``
    is sized identically so a step never has to renegotiate a fresh TCP
    connection mid-ramp. The pool-acquire timeout is explicit and separate
    from the per-request read timeout (:data:`POOL_ACQUIRE_TIMEOUT_S`):
    with headroom above every step, genuine pool exhaustion is a bug and
    should fail fast, not block for up to :data:`DEFAULT_REQUEST_TIMEOUT_S`.
    """
    max_conn = max(ramp_steps) + CONNECTION_POOL_HEADROOM
    limits = httpx.Limits(max_connections=max_conn, max_keepalive_connections=max_conn)
    timeout = httpx.Timeout(
        connect=10.0, read=DEFAULT_REQUEST_TIMEOUT_S, write=10.0, pool=POOL_ACQUIRE_TIMEOUT_S,
    )
    return httpx.Client(base_url=base_url.rstrip("/"), headers=headers, limits=limits, timeout=timeout)


def fetch_status(client: httpx.Client) -> dict[str, Any] | None:
    """``GET /v1/status`` on *client*, fail-closed to ``None`` on any
    error — mirrors ``nexus.db.http_engine_status.fetch_engine_status``'s
    own contract."""
    try:
        resp = client.get("/v1/status")
        resp.raise_for_status()
        body = resp.json()
    except Exception:  # noqa: BLE001 — fail-closed: a transport blip is "unprobeable", never a crash
        return None
    return body if isinstance(body, dict) else None


def resolve_collection_name(subject: str) -> str:
    """The rendered four-segment collection name this box would mint for
    *subject* — the SAME resolver every real write path uses
    (``nexus.corpus.t3_collection_name``), so the name this gate writes
    under is exactly what a normal write targeting *subject* would use.
    Pure/network-free with no live probe (``t3=None``): no grandfathering
    onto a pre-existing collection is possible or desired for a brand-new
    throwaway subject."""
    from nexus.corpus import t3_collection_name  # noqa: PLC0415 — deferred, see resolve_cloud_endpoint

    return t3_collection_name(subject, for_write=True)


def register_load_collection(name: str) -> None:
    """Register *name* the same way every write path does before its
    first chunk lands (RDR-204 Phase 1: the engine no longer
    auto-registers on first write)."""
    from nexus.corpus import ensure_collection_registered  # noqa: PLC0415 — deferred, see resolve_cloud_endpoint

    ensure_collection_registered(name)


def delete_load_collection(name: str) -> dict[str, Any]:
    """The engine's own delete-and-cascade-purge for *name* — functionally
    identical to the interactive collection-delete verb's own call,
    invoked directly so this driver never shells out to a CLI subprocess."""
    from nexus.db import make_t3  # noqa: PLC0415 — deferred, see resolve_cloud_endpoint
    from nexus.db.collection_purge import purge_collection_cascade  # noqa: PLC0415 — deferred, see resolve_cloud_endpoint

    cascade = purge_collection_cascade(make_t3(), name)
    return {"t3_absent": cascade.t3_absent, "failures": list(cascade.failures)}


def find_orphan_collections(exclude: str | None = None) -> list[str]:
    """Every LIVE collection matching this gate's own throwaway prefix
    (:data:`ORPHAN_PREFIX`), excluding *exclude* (the current run's own
    name, once resolved) — evidence of a prior run's collection a killed
    process never cleaned up. Report-only: never deletes anything this
    run did not itself create (fix round: a harder kill than
    ``finally`` can catch, e.g. SIGKILL/OOM, is a real and disclosed
    risk — see this module's Interrupts note — and the remedy is a human
    reading this report, never an automatic sweep)."""
    from nexus.db import make_t3  # noqa: PLC0415 — deferred, see resolve_cloud_endpoint

    t3 = make_t3()
    names = [row.get("name") for row in t3.list_collections() if isinstance(row, dict)]
    return sorted(n for n in names if isinstance(n, str) and n.startswith(ORPHAN_PREFIX) and n != exclude)


def post_upsert(
    client: httpx.Client,
    collection: str,
    nonce: str,
    tag: str,
    *,
    target_bytes: int = DEFAULT_TARGET_BYTES,
    deadline_ms: int = DEFAULT_REQUEST_DEADLINE_MS,
) -> RawResponse:
    """One raw, un-retried POST to ``/v1/vectors/upsert-chunks``.

    Deliberately bypasses ``HttpVectorClient.upsert_chunks`` — see this
    module's docstring, point 1, for why: that client's layered retry
    silently absorbs the exact evidence (raw status + ``Retry-After`` +
    ``X-Nexus-Deadline-Outcome`` per attempt) this gate exists to observe.
    *client* already carries ``Authorization``/``X-Nexus-Tenant``/
    ``Content-Type`` as default headers (see :func:`build_client`); only
    the per-request deadline header is added here.
    """
    text = generate_document(nonce, tag, target_bytes)
    doc_id = load_document_id(nonce, tag, target_bytes)
    body = {"collection": collection, "ids": [doc_id], "documents": [text], "metadatas": [{}]}
    headers = {"X-Nexus-Request-Deadline-Ms": str(deadline_ms)}
    t0 = time.monotonic()
    try:
        resp = client.post("/v1/vectors/upsert-chunks", content=json.dumps(body).encode("utf-8"), headers=headers)
    except Exception as exc:  # noqa: BLE001 — a transport failure is evidence for this gate, never a crash
        return RawResponse(status_code=0, retry_after=None, deadline_outcome=None, elapsed_s=time.monotonic() - t0, error=str(exc)[:200])
    elapsed = time.monotonic() - t0
    retry_after_raw = resp.headers.get("Retry-After")
    try:
        retry_after = float(retry_after_raw) if retry_after_raw is not None else None
    except ValueError:
        retry_after = None
    return RawResponse(
        status_code=resp.status_code,
        retry_after=retry_after,
        deadline_outcome=resp.headers.get("X-Nexus-Deadline-Outcome"),
        elapsed_s=elapsed,
    )


def fire_step(
    client: httpx.Client,
    collection: str,
    nonce: str,
    step_index: int,
    concurrency: int,
    *,
    target_bytes: int = DEFAULT_TARGET_BYTES,
    deadline_s: float | None = None,
) -> tuple[list[RawResponse], bool]:
    """Fire *concurrency* concurrent raw upserts (one document each) and
    collect every response. Never fail-fast on one worker's transport
    error. Returns ``(responses, hit_deadline)``.

    When *deadline_s* is given, the WHOLE STEP (not each request) is
    capped at it: on expiry, queued-but-not-yet-started futures are
    cancelled and the pool is shut down WITHOUT waiting for already-
    dispatched requests to finish (``shutdown(wait=False,
    cancel_futures=True)``) — see this module's own Interrupts note for
    the disclosed limitation (Python cannot forcibly kill a running
    thread). The same shutdown shape runs on ANY interruption of the wait
    (including ``KeyboardInterrupt``), which is then re-raised so the
    caller's own interrupt handling still sees it.
    """
    pool = ThreadPoolExecutor(max_workers=concurrency)
    futures = [
        pool.submit(post_upsert, client, collection, nonce, f"{step_index}-{i}", target_bytes=target_bytes)
        for i in range(concurrency)
    ]
    hit_deadline = False
    try:
        _done, not_done = futures_wait(futures, timeout=deadline_s)
        hit_deadline = bool(not_done)
        for f in not_done:
            f.cancel()
    except BaseException:
        for f in futures:
            f.cancel()
        pool.shutdown(wait=False, cancel_futures=True)
        raise
    pool.shutdown(wait=not hit_deadline, cancel_futures=hit_deadline)

    responses: list[RawResponse] = []
    for f in futures:
        if f.cancelled():
            responses.append(RawResponse(0, None, None, 0.0, error="cancelled: wall-clock cap reached"))
            continue
        if not f.done():
            responses.append(RawResponse(0, None, None, 0.0, error="incomplete: wall-clock cap reached"))
            continue
        try:
            responses.append(f.result())
        except Exception as exc:  # noqa: BLE001 — a worker's own unexpected exception is evidence, never a crash
            responses.append(RawResponse(0, None, None, 0.0, error=str(exc)[:200]))
    return responses, hit_deadline


# ── Orchestration ────────────────────────────────────────────────────────


def run_gate(
    *,
    dry_run: bool,
    ramp_steps: Sequence[int] = DEFAULT_RAMP_STEPS,
    embedder: str = DEFAULT_EMBEDDER,
    target_bytes: int = DEFAULT_TARGET_BYTES,
    wall_clock_cap_s: float = DEFAULT_WALL_CLOCK_CAP_S,
    nonce_source: Callable[[], str] | None = None,
    client_factory: Callable[[str, dict[str, str]], httpx.Client] | None = None,
    idle_gap_s: float = DEFAULT_IDLE_CHECK_GAP_S,
    idle_sleep: Callable[[float], None] = time.sleep,
) -> dict[str, Any]:
    """Run (or, with ``dry_run=True``, only plan) the whole ramp.

    Never raises: every failure mode — endpoint resolution, the write
    guard, the upfront status/idle pre-check, registration, the ramp
    itself, an interrupt, cleanup — is caught and folded into the
    returned ``{"passed": bool, "reason": str, ...}`` result, so the
    caller always gets a clean verdict instead of a bare traceback.
    Cleanup (the collection delete) runs in a ``finally`` whenever a
    collection name was ever resolved, regardless of how the run ended —
    including ``KeyboardInterrupt`` (see this module's Interrupts note
    for the disclosed limits of that guarantee).

    *client_factory*, given ``(base_url, headers)``, must return an
    ``httpx.Client``; defaults to :func:`build_client` sized for
    *ramp_steps*. Tests substitute a client built on
    ``httpx.MockTransport`` here to exercise this whole function with no
    real network.
    """
    nonce = make_run_nonce(nonce_source)
    subject = load_collection_subject(nonce)
    plan: dict[str, Any] = {
        "nonce": nonce,
        "subject": subject,
        "ramp_steps": list(ramp_steps),
        "embedder": embedder,
        "target_bytes": target_bytes,
        "wall_clock_cap_s": wall_clock_cap_s,
    }

    if dry_run:
        plan["collection_name"] = resolve_collection_name(subject)
        return {
            "passed": True,
            "dry_run": True,
            "reason": "dry-run: plan only, no network touched",
            "plan": plan,
        }

    name: str | None = None
    steps_tried: list[int] = []
    verdicts: list[StepVerdict] = []
    evidence: list[dict[str, Any]] = []
    invalid_step_count = 0
    status_read_failure_count = 0
    wall_clock_hit = False
    error: str | None = None
    is_preflight_failure = False
    orphan_collections: list[str] = []
    cleanup: dict[str, Any] = {"attempted": False}
    start = time.monotonic()

    try:
        base_url, static_token = resolve_cloud_endpoint()
        guard_write(base_url)
        token = resolve_bearer(base_url, TENANT, static_token)
        headers = {
            "Authorization": f"Bearer {token}",
            "X-Nexus-Tenant": TENANT,
            "Content-Type": "application/json",
        }
        factory = client_factory if client_factory is not None else (lambda url, hdrs: build_client(url, hdrs, ramp_steps))

        with factory(base_url, headers) as client:
            orphan_collections = find_orphan_collections()
            plan["orphan_collections_seen_at_start"] = orphan_collections

            preflight_reason, _ = resolve_idle_precheck(
                lambda: fetch_status(client), embedder, gap_s=idle_gap_s, sleep=idle_sleep,
            )
            if preflight_reason is not None:
                is_preflight_failure = True
                raise AdmissionLoadPreflightError(preflight_reason)

            name = resolve_collection_name(subject)
            plan["collection_name"] = name
            print(f"[admission-load] plan: {json.dumps(plan)}")

            register_load_collection(name)

            for step_index, concurrency in enumerate(ramp_steps):
                remaining = wall_clock_cap_s - (time.monotonic() - start)
                if remaining <= 0:
                    wall_clock_hit = True
                    print(
                        f"[admission-load] wall-clock cap of {wall_clock_cap_s}s reached before "
                        f"concurrency={concurrency} could start"
                    )
                    break

                before_status = fetch_status(client)
                status_read_failed = before_status is None
                before = snapshot_counters(before_status, embedder)
                t0 = time.monotonic()
                responses, hit_deadline = fire_step(
                    client, name, nonce, step_index, concurrency, target_bytes=target_bytes, deadline_s=remaining,
                )
                elapsed = time.monotonic() - t0
                after_status = fetch_status(client)
                status_read_failed = status_read_failed or after_status is None
                after = snapshot_counters(after_status, embedder)

                # fix round (T2 [27139] Important 2): snapshot_counters' own
                # fail-closed-to-zero default is right for "no embedder_activity
                # entry at all" but WRONG here -- a transiently failed before/after
                # read must never silently become a zeroed counter, or a real
                # nonzero value read back correctly on the OTHER side of the pair
                # synthesizes a phantom admission_moved=True this driver's own
                # request stream never actually produced (see this bead's T2
                # review for the exact mechanism). A failed status read makes the
                # step's measurement just as untrustworthy as a transport-error
                # storm, so it is folded into the same `valid` gate rather than
                # given its own RampOutcome field.
                valid = is_step_valid(responses) and not status_read_failed
                if not valid:
                    invalid_step_count += 1
                    if status_read_failed:
                        status_read_failure_count += 1
                verdict = (
                    evaluate_step(before, after, responses) if valid
                    else dataclasses.replace(INVALID_STEP_VERDICT, timeouts=count_cuts(responses))
                )

                steps_tried.append(concurrency)
                verdicts.append(verdict)

                step_evidence = {
                    "concurrency": concurrency,
                    "before": dataclasses.asdict(before),
                    "after": dataclasses.asdict(after),
                    "valid": valid,
                    "status_read_failed": status_read_failed,
                    "hit_wall_clock_cap": hit_deadline,
                    "admission_moved": verdict.admission_moved,
                    "deadline_moved": verdict.deadline_moved,
                    "observed_refused": verdict.observed_refused,
                    "refused_count": verdict.refused_count,
                    "engine_refusals": verdict.engine_refusals,
                    "edge_refusals": verdict.edge_refusals,
                    "timeouts": verdict.timeouts,
                    "counter_delta": verdict.counter_delta,
                    "responses": summarize_responses(responses),
                    "elapsed_s": round(elapsed, 2),
                }
                evidence.append(step_evidence)
                print(f"[admission-load] step {json.dumps(step_evidence)}")

                if hit_deadline:
                    wall_clock_hit = True
                    print(
                        f"[admission-load] wall-clock cap of {wall_clock_cap_s}s reached mid-step "
                        f"at concurrency={concurrency}"
                    )
                if verdict.passes or verdict.unattributable or verdict.timeouts or wall_clock_hit:
                    break
    except AdmissionLoadPreflightError as exc:
        error = str(exc)
    except BaseException as exc:  # noqa: BLE001 — deliberately broad: Ctrl-C (and anything else) mid-ramp must still run cleanup and write a result — see this module's Interrupts note
        error = f"{type(exc).__name__}: {exc}"
    finally:
        if name is not None:
            cleanup["attempted"] = True
            try:
                cleanup.update(delete_load_collection(name))
                cleanup["ok"] = not cleanup.get("failures")
            except BaseException as exc:  # noqa: BLE001 — a second interrupt during cleanup must not crash silently; best-effort record and move on (see this module's Interrupts note)
                cleanup["ok"] = False
                cleanup["error"] = str(exc)
        else:
            cleanup["ok"] = True
            cleanup["skipped_reason"] = "no collection name was ever resolved; nothing to clean up"

    result: dict[str, Any] = {
        "plan": plan,
        "steps": evidence,
        "cleanup": cleanup,
        "orphan_collections_seen_at_start": orphan_collections,
    }

    if error is not None:
        result["passed"] = False
        result["reason"] = error if is_preflight_failure else f"error during load: {error}"
        return result

    outcome = decide_ramp_outcome(steps_tried, verdicts)
    try:
        require_pass(outcome)
    except (AdmissionLoadVacuousError, AdmissionLoadUnattributableError) as exc:
        reason = str(exc)
        if invalid_step_count:
            causes = []
            if status_read_failure_count:
                causes.append(f"{status_read_failure_count} status read failed")
            transport_only = invalid_step_count - status_read_failure_count
            if transport_only:
                causes.append(f"{transport_only} too many transport errors")
            reason += (
                f" ({invalid_step_count} of {len(steps_tried)} step(s) tried were invalid: "
                f"{', '.join(causes)} -- see per-step evidence)"
            )
        if wall_clock_hit:
            reason += (
                f" (stopped early: wall-clock cap of {wall_clock_cap_s}s reached after "
                f"{len(steps_tried)} of {len(ramp_steps)} planned step(s))"
            )
        result["passed"] = False
        result["reason"] = reason
        return result

    if not cleanup.get("ok", False):
        result["passed"] = False
        result["reason"] = (
            f"load succeeded (stopped at concurrency={outcome.stopped_at_step}) "
            f"but cleanup did not confirm success: {cleanup}"
        )
        return result

    result["passed"] = True
    result["stopped_at_step"] = outcome.stopped_at_step
    passing = verdicts[outcome.stopped_at_index]
    engine_note = (
        "; the ENGINE refused too, so the edge let through more than it could hold"
        if passing.engine_refusals else ""
    )
    result["reason"] = (
        f"load shed by fast refusals at concurrency={outcome.stopped_at_step}: "
        f"{passing.refused_count} refused 503s ({passing.engine_refusals} engine, "
        f"{passing.edge_refusals} edge; engine counter delta {passing.counter_delta}), "
        f"zero cuts{engine_note}"
    )
    return result


# ── CLI ────────────────────────────────────────────────────────────────


def _build_arg_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    sub = parser.add_subparsers(dest="command", required=True)
    run = sub.add_parser("run", help="Run the admission-load ramp against the resolved cloud endpoint.")
    run.add_argument("--dry-run", action="store_true", help="Print the plan; touch no network.")
    run.add_argument(
        "--ramp-steps",
        default=",".join(str(s) for s in DEFAULT_RAMP_STEPS),
        help="Comma-separated concurrency steps, strictly increasing.",
    )
    run.add_argument("--embedder", default=DEFAULT_EMBEDDER)
    run.add_argument("--target-bytes", type=int, default=DEFAULT_TARGET_BYTES)
    run.add_argument(
        "--wall-clock-cap-s",
        type=float,
        default=DEFAULT_WALL_CLOCK_CAP_S,
        help="Stop dispatching new load once this many seconds have elapsed (default 360s / 6 minutes).",
    )
    run.add_argument(
        "--result-file",
        default=None,
        help="Write the JSON result here (the wrapper script reads it for the FAILED reason).",
    )
    return parser


def main(argv: list[str] | None = None) -> int:
    args = _build_arg_parser().parse_args(argv)

    try:
        ramp_steps = parse_ramp_steps(args.ramp_steps)
        if args.wall_clock_cap_s <= 0:
            raise ValueError(f"--wall-clock-cap-s must be positive, got {args.wall_clock_cap_s}")
        result = run_gate(
            dry_run=args.dry_run,
            ramp_steps=ramp_steps,
            embedder=args.embedder,
            target_bytes=args.target_bytes,
            wall_clock_cap_s=args.wall_clock_cap_s,
        )
    except Exception as exc:  # noqa: BLE001 — belt-and-suspenders: run_gate already catches its own failures; this covers a bug in the CLI plumbing around it
        result = {"passed": False, "reason": f"driver error: {exc}"}

    if args.result_file:
        with open(args.result_file, "w", encoding="utf-8") as fh:
            json.dump(result, fh, indent=2, default=str)

    verdict = "PASSED" if result.get("passed") else "FAILED"
    print(f"[admission-load] {verdict}: {result.get('reason')}")
    return 0 if result.get("passed") else 1


if __name__ == "__main__":
    raise SystemExit(main())
