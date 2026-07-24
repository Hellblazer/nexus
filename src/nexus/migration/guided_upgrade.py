# SPDX-License-Identifier: AGPL-3.0-or-later
# Copyright (c) 2026 Hal Hildebrand. All rights reserved.
"""``nx guided-upgrade`` Stage-2 logic — provision + version-pin + health-gate
the engine-service, then hand off to the existing ``nx migrate-to-service``.

RDR-002. conexus owns the design; this module is the engine-side host. The
detect / migrate / validate / unlock / rollback machinery already exists
(:mod:`nexus.migration.detection`, :func:`nexus.migration.driver.run_guided_upgrade`,
``nx migrate-to-service``) and is REUSED, never rebuilt. This module adds only
the new pre-flight + provisioning + readiness-contract pieces.

ez5.2 (this commit): :func:`detect_pending_migration` — the pre-flight a
command runs BEFORE provisioning a service, so a fresh user short-circuits to
a no-op instead of standing up a service for an empty footprint.
"""

from __future__ import annotations

import sqlite3
from collections.abc import Iterable
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from typing import Any, Callable

import structlog

from nexus.migration.detection import (
    DetectionReport,
    classify_collections,
    close_read_client,
    open_read_legs,
    voyage_key_available,
)
from nexus.migration.etl_registry import LADDER_ORDER
from nexus.migration.migration_report import load_report

_log = structlog.get_logger(__name__)


@dataclass(frozen=True)
class PreflightDetection:
    """The verdict of the pre-provision detection step.

    ``needs_migration`` is the single gate the command branches on: True iff
    at least one data-bearing legacy Chroma collection exists. A fresh user
    (no legs, or only empty collections) yields ``False`` and the command must
    no-op WITHOUT provisioning a service.
    """

    report: DetectionReport
    needs_migration: bool

    @property
    def data_bearing_count(self) -> int:
        """Number of non-empty collections across all detected legs."""
        return sum(1 for c in self.report.classifications if c.has_data)

    @property
    def classified_unsupported_count(self) -> int:
        """Number of collections classified ``unsupported`` by detection.

        This is the RAW classification count — it INCLUDES legacy minilm-384
        collections that RDR-162 auto-remaps (re-embeds into a bge-768 target)
        rather than blocks. It is therefore NOT the count of genuinely-blocked
        collections; a consumer needing the blocked set must filter
        ``report.unsupported`` by :func:`cross_model_remappable`. Kept as a
        coarse informational signal only.
        """
        return len(self.report.unsupported)

    @property
    def total_count(self) -> int:
        """Total classified collections (data-bearing or not)."""
        return len(self.report.classifications)


def legacy_footprint_pending() -> bool:
    """CHEAP gate for the substrate-migration bridge (nexus-0rwwv):
    ``NX_MIGRATION_NOTICE!=0`` AND the legacy local-Chroma directory exists
    AND no REAL service signal is present. File-level checks only — NEVER
    opens the store (safe on hot paths: endpoint-failure remedies, the
    SessionStart hook), so it cannot violate the WAL single-opener
    discipline or the rollback-source immutability.

    "Already migrated" is decided by REAL-SERVICE EVIDENCE, deliberately
    NOT by ``storage_backend_for()`` (critique CRITICAL, 2026-07-08):
    SERVICE is the RDR-152 hard ROUTING default for every install — a
    vanilla 5.x upgrader who never exported ``NX_STORAGE_BACKEND`` reads
    SERVICE precisely because nothing was ever configured, which is the
    opposite of "migrated." The signals that are actually true only when
    a service exists for this install:

    * a configured ``service_url`` credential (env or config.yml — the
      managed/cloud deployment shape), or
    * the ``pg_credentials`` provisioning artifact (written once by
      ``nx init`` / ``nx guided-upgrade`` when the local PG is created), or
    * a live supervisor lease (``discover_lease``).

    Post-migration the Chroma directory legitimately persists
    (copy-not-move), which is why the directory check alone cannot carry
    the verdict. Residual accepted shapes: a mid-migration crash counts as
    "not pending" here (pg_credentials already exists) — that state owns
    its own recovery surface (the guided-upgrade failure text +
    ``nx migration --clear-state``); a deliberate stay-local user keeps
    seeing the pointer (the ``=sqlite`` opt-out retires at RDR-158 P3); a
    once-provisioned install whose PG cluster was deleted but whose
    ``pg_credentials`` file survives reads "not pending" — a
    repair/troubleshooting shape, not the never-migrated case this
    targets (critique residual 1, accepted).
    """
    import os  # noqa: PLC0415 — stdlib, kept helper-local

    if os.environ.get("NX_MIGRATION_NOTICE") == "0":
        return False

    try:
        from nexus.migration.detection import resolve_default_local_leg  # noqa: PLC0415 — deferred import — the bridge dies at RDR-155 P4b

        if not Path(resolve_default_local_leg()).is_dir():
            return False

        # Real-service evidence, cheapest first. Mirrors ALL THREE tiers
        # the real endpoint resolvers accept as "configured": the
        # service_url credential, the explicit HOST/PORT env pair, and
        # the lease (cre review, Low residual — an operator who exported
        # NX_SERVICE_HOST/PORT deliberately told nexus where their data
        # lives and must not be nagged).
        if os.environ.get("NX_SERVICE_HOST", "").strip() or os.environ.get(
            "NX_SERVICE_PORT", ""
        ).strip():
            return False

        from nexus.config import get_credential, nexus_config_dir  # noqa: PLC0415 — deferred import — keep hot paths cheap

        if (get_credential("service_url") or "").strip():
            return False

        from nexus.db.pg_provision import CREDENTIALS_FILENAME  # noqa: PLC0415 — deferred import — keep hot paths cheap

        if (nexus_config_dir() / CREDENTIALS_FILENAME).exists():
            return False

        from nexus.db.service_endpoint import discover_lease  # noqa: PLC0415 — deferred import — keep hot paths cheap

        lease_url, _lease_token = discover_lease()
        if lease_url is not None:
            return False

        return True
    except Exception:  # noqa: BLE001 — best-effort gate; never break the caller
        _log.debug("legacy_footprint_gate_failed", exc_info=True)
        return False


def endpoint_failure_migration_hint() -> str:
    """One-sentence addendum for endpoint-resolution failures (nexus-0rwwv).

    The exact wall an un-migrated 5.x -> 6.x user hits: T3 serving routes
    through the service (RDR-155 P4a), the service is not provisioned, and
    the stock error's remedy ("start the supervisor") is WRONG for them —
    they need the one-time migration. Cheap gate only; empty string when
    the install does not look like a pending legacy footprint (fresh
    installs and already-migrated installs keep the stock message).

    RDR-185 P4.2 (nexus-n7u38.29): the remedy is now ``nx upgrade`` — the
    single trigger converges the provisioning precondition and the substrate
    rung in one walk. Unlike the retired bridge notice, this hint is NOT a
    duplicate report: it fires on an ERROR path, where the user has hit the
    wall with no walk in flight and the stock remedy ("start the supervisor")
    is actively wrong for them. Naming the remedy at the wall is the same
    pattern as rollback at ``migrate_cmd``'s block path.
    """
    if not legacy_footprint_pending():
        return ""
    return (
        " NOTE: this install has a legacy local store awaiting the ONE-TIME "
        "storage migration — if you recently upgraded from a 5.x install, "
        "run `nx upgrade` (provisions the service AND migrates your data, "
        "showing a cost preview before anything bills) instead of starting "
        "the service by hand."
    )


def pending_migration_notice() -> str | None:
    """Best-effort bridge pointer from the routine commands to the cutover
    (nexus-0rwwv).

    RETIRED as a user surface — RDR-185 P4.2 (nexus-n7u38.29) removed BOTH
    call sites (``nx upgrade``, ``nx doctor``). The bridge existed because
    the routine upgrade and the one-time cutover were two commands with
    nothing between them; the ladder makes them one walk, so this pointer
    became a duplicate report of a state the ladder already reports, naming
    a verb P4.1 demoted out of ``--help``. Left in place, callable and
    tested, exactly like the demoted verbs it pointed at: this whole module
    dies at RDR-155 P4b, which is a standing blocker.

    Historical contract, unchanged below this line.

    Two upgrade commands, no bridge: a local-mode user with a pending
    Chroma -> pgvector cutover ran ``nx upgrade``, saw "migrations
    complete", and got zero pointer to ``nx guided-upgrade``. This is the
    shared probe ``nx upgrade`` (interactive mode only) and ``nx doctor``
    (default health path) append. Auto-DETECT without auto-EXECUTE: the
    notice points at the cutover, which keeps its own consent gate
    (``--yes``, cost preview).

    Returns ``None`` — silently — whenever there is nothing to say or it
    cannot safely find out: installs with real-service evidence (already migrated/provisioned; the
    probe is skipped entirely, not just suppressed), fresh installs (no
    legs), empty footprints, any probe failure, or the ``NX_MIGRATION_NOTICE=0``
    kill switch (the test suite pins it: an isolated-config test box reads
    as SQLITE mode while the XDG chroma default may resolve to a REAL
    store — the immutable rollback source — which unit tests must never
    open).

    Lives in this module deliberately: the whole bridge dies with the
    migration module at RDR-155 P4b.
    """
    if not legacy_footprint_pending():
        return None

    try:
        detection = detect_pending_migration_memoized()
    except Exception:  # noqa: BLE001 — best-effort probe; a broken store must not break nx upgrade/doctor
        _log.debug("pending_migration_notice_probe_failed", exc_info=True)
        return None
    if not detection.needs_migration:
        return None

    n = detection.data_bearing_count
    return (
        f"A one-time storage migration is pending: {n} data-bearing Chroma "
        "collection(s) can move to the PostgreSQL service substrate.\n"
        "Run: nx guided-upgrade   (interactive; shows a cost preview before "
        "migrating anything)"
    )


def detect_pending_migration(
    *,
    local_path: str | Path | None = None,
    voyage_key_present: bool | None = None,
    open_legs: Callable[[str | Path | None], tuple[Any, Any]] | None = None,
    close_leg: Callable[[Any], None] | None = None,
) -> PreflightDetection:
    """Detect whether a pre-RDR-160 Chroma footprint exists to migrate.

    Opens the local + cloud read legs, classifies the footprint via the
    existing :func:`classify_collections`, then CLOSES the legs before
    returning — the WAL local leg is a single-opener and the downstream ETL
    must be the sole opener (same invariant the driver enforces).

    ``open_legs`` / ``close_leg`` are injection seams for tests; production
    uses :func:`open_read_legs` and :func:`_close_quietly`. ``voyage_key_present``
    defaults to the deployment-mode probe.
    """
    key_present = (
        voyage_key_available() if voyage_key_present is None else voyage_key_present
    )
    _open = open_legs if open_legs is not None else open_read_legs
    _close = close_leg if close_leg is not None else close_read_client

    _skipped: dict = {}
    try:
        local, cloud = _open(local_path, skipped_out=_skipped)
    except TypeError:
        # Injected open_legs doubles predate the skipped_out kwarg — the
        # structured skip note is best-effort for them.
        local, cloud = _open(local_path)
    try:
        report = classify_collections(
            local_client=local,
            cloud_client=cloud,
            voyage_key_present=key_present,
            cloud_leg_skipped_reason=_skipped.get("cloud"),
        )
    finally:
        # Close only the legs that were actually opened — an absent leg is
        # never dispatched to ``_close`` (so injected close hooks need not
        # tolerate ``None``).
        for client in (local, cloud):
            if client is not None:
                _close(client)

    needs = len(report.legs_with_data) > 0
    _log.info(
        "guided_upgrade_preflight",
        needs_migration=needs,
        total=len(report.classifications),
        data_bearing=sum(1 for c in report.classifications if c.has_data),
        unsupported=len(report.unsupported),
    )
    return PreflightDetection(report=report, needs_migration=needs)


#: Process-local memo for :func:`detect_pending_migration_memoized`:
#: (producer function object, monotonic timestamp, result).
_detection_memo: tuple[object, float, PreflightDetection] | None = None
_DETECTION_MEMO_TTL_S: float = 60.0


def detect_pending_migration_memoized() -> PreflightDetection:
    """Single-probe memo over :func:`detect_pending_migration`.

    RDR-185 P1 critique (High): a plain ``nx doctor`` on a Chroma-mode
    install paid the read-leg classification TWICE back to back — once for
    the ladder's legacy-id census (``upgrade_ladder.census``) and once for
    the bridge notice (:func:`pending_migration_notice`). Both now share
    this memo. The entry is keyed by the producer FUNCTION OBJECT (held by
    reference, compared with ``is``): a test that monkeypatches
    ``detect_pending_migration`` produces a different object and always
    misses a foreign entry — no cross-test leakage, no stale patched
    results. The short TTL bounds staleness in long-lived processes
    (doctor CLI runs are one-shot; MCP daemons calling health checks
    repeatedly re-probe at most once a minute — migration-pending state
    changes on human timescales).
    """
    global _detection_memo
    import time  # noqa: PLC0415 — stdlib, kept helper-local

    producer = detect_pending_migration
    now = time.monotonic()
    if (
        _detection_memo is not None
        and _detection_memo[0] is producer
        and now - _detection_memo[1] < _DETECTION_MEMO_TTL_S
    ):
        return _detection_memo[2]
    result = producer()
    _detection_memo = (producer, now, result)
    return result


# ── RDR-178 Gap 7 (nexus-1sx01): already-migrated detection ────────────────
#
# The 2026-07-01 production incident: a guided-upgrade re-run re-shipped
# 158k catalog rows to patch a 270-row hole because nothing distinguished
# "source data present" (detect_pending_migration's Chroma-only signal,
# which stays True forever post-success because copy-not-move retains
# Chroma) from "already migrated". This section closes that gap for the
# T2 SQLite side ONLY — the side the incident actually hit, and the side
# with a durable evidence trail (the RDR-153 migration-report artifacts
# under ``<config>/migration-reports/``). It does NOT attempt "already
# migrated" detection for the T3 Chroma legs (that is the migrate-leg
# delta-shipping mode, nexus-s3dd4, Wave 2 — explicitly out of scope here)
# and does NOT invent a new durable marker: the existing report artifacts
# ARE the positive signal the original bead asked for.

#: (table, timestamp-column) freshness probe per RDR-152 ladder store — the
#: SINGLE anchor table each store's ETL writes first/primarily. Used only to
#: detect "have there been local writes since the report we would otherwise
#: trust completed"; a store whose newer writes land ONLY in a secondary
#: table (e.g. ``taxonomy.topic_assignments`` rather than ``topics``) is not
#: caught by this coarse probe. That is an accepted MVP limitation — the
#: probe is used only to decide whether to SKIP re-shipping, never to
#: confirm correctness, and a false skip is still safe (copy-not-move — the
#: source is never at risk, only re-verified less eagerly).
FRESHNESS_PROBES: dict[str, tuple[tuple[str, str], ...]] = {
    "memory": (("memory", "timestamp"),),
    "plans": (("plans", "created_at"),),
    "telemetry": (("search_telemetry", "ts"),),
    "taxonomy": (("topics", "created_at"),),
    # nexus-g8r2h critic Critical (critique [21092]): document_highlights
    # rides the "aspects" ETL slot (aspects_etl.migrate_highlights) but the
    # probe never anchored on it — a highlights-only local write (the
    # pre-g8r2h service-mode writer bug's stranded window) read as "no newer
    # local writes": a CONFIDENTLY FALSE clean, worse than no signal. Every
    # table an ETL slot ships must be probed; multi-probe slots confirm
    # freshness only when EVERY probe answers "no newer writes".
    "aspects": (
        ("document_aspects", "extracted_at"),
        ("document_highlights", "ingested_at"),
    ),
    # "chash" probe removed (RDR-187/nexus-piwya.9 sweep): unreachable since
    # .10 dropped the store from LADDER_ORDER; detect_already_migrated never
    # consults it.
    "catalog": (("documents", "indexed_at"),),
    "aspects_queue": (("aspect_extraction_queue", "enqueued_at"),),
}


@dataclass(frozen=True)
class StoreMigrationStatus:
    """One T2 store's already-migrated verdict for one pre-flight.

    ``line`` is the human-readable evidence trail rendered to the operator
    (e.g. ``"memory: already migrated 2026-07-01T12:00:00+00:00, no newer
    local writes"``) — never silent, so a skip is always auditable.
    """

    store: str
    skip: bool
    line: str


@dataclass(frozen=True)
class AlreadyMigratedPlan:
    """The per-store skip/run breakdown from :func:`detect_already_migrated`."""

    statuses: tuple[StoreMigrationStatus, ...]

    @property
    def skip_stores(self) -> frozenset[str]:
        return frozenset(s.store for s in self.statuses if s.skip)

    @property
    def run_stores(self) -> frozenset[str]:
        return frozenset(s.store for s in self.statuses if not s.skip)

    @property
    def all_skipped(self) -> bool:
        """True iff every EVALUATED store is covered.

        An empty plan (no statuses at all) is deliberately NOT
        ``all_skipped`` — "nothing was evaluated" must never read as
        "everything is a no-op".
        """
        return bool(self.statuses) and not self.run_stores

    def summary_lines(self) -> list[str]:
        return [s.line for s in self.statuses]


def _default_reports_dir() -> Path:
    from nexus.config import nexus_config_dir  # noqa: PLC0415 — deferred import — avoids import-time cost / circular deps

    return nexus_config_dir() / "migration-reports"


def _load_reports(dir_path: Path) -> list[dict[str, Any]]:
    """Load every ``*.json`` report under *dir_path*.

    A corrupt / unparseable / non-object file is logged and skipped — a
    durable evidence trail must degrade gracefully, never crash the
    pre-flight (mirrors :func:`detect_pending_migration`'s own
    fail-loud-only-on-real-errors posture, but a bad artifact on disk is
    evidence noise, not a real error).
    """
    if not dir_path.is_dir():
        return []
    reports: list[dict[str, Any]] = []
    for f in sorted(dir_path.glob("*.json")):
        try:
            reports.append(load_report(f))
        except (OSError, ValueError) as exc:
            _log.warning(
                "guided_upgrade_report_unreadable", path=str(f), error=str(exc)
            )
    return reports


def _store_failed_count(report: dict[str, Any], store: str) -> int | None:
    """Sum ``failed`` across *store*'s tables in *report*; ``None`` when the
    store is not present in this report at all (distinct from a present
    store with zero failures)."""
    for entry in report.get("stores", []) or []:
        if entry.get("store") == store:
            return sum(int(t.get("failed", 0)) for t in entry.get("tables", []) or [])
    return None


def _parse_ts(value: Any) -> datetime | None:
    """Parse an ISO-8601 timestamp (``Z`` or ``+00:00`` suffix); ``None`` on
    anything unparseable — never raises (both report and SQLite timestamps
    are foreign input here)."""
    if not isinstance(value, str) or not value:
        return None
    try:
        return datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError:
        return None


def _has_newer_local_writes(
    sqlite_path: str | Path, probe: tuple[str, str], report_completed_at: str,
) -> bool | None:
    """True iff *probe*'s anchor table has a row newer than
    *report_completed_at*; False if confirmed not; None if freshness cannot
    be evaluated (missing file/table/column, or an unparseable timestamp on
    either side).

    ``None`` is the CONSERVATIVE-TOWARD-TRUST branch by design: the caller
    (:func:`_decide_store`) treats "cannot evaluate" the same as "no probe
    configured" — report-presence-alone, per the RDR-178 Gap 7 spec's
    explicit fallback. *table*/*column* come from the fixed internal
    :data:`FRESHNESS_PROBES` map only, never external input.
    """
    report_dt = _parse_ts(report_completed_at)
    if report_dt is None:
        return None
    path = Path(sqlite_path)
    if not path.exists():
        return None
    table, column = probe
    try:
        conn = sqlite3.connect(f"file:{path}?mode=ro", uri=True)  # epsilon-allow: Gap-7 already-migrated freshness probe — read-only (mode=ro URI) on the frozen migration source, never T2Database
    except sqlite3.OperationalError:
        return None
    try:
        cur = conn.execute(f"SELECT MAX({column}) FROM {table}")  # noqa: S608 — table/column from the fixed internal FRESHNESS_PROBES map
        row = cur.fetchone()
    except sqlite3.Error:
        return None
    finally:
        conn.close()
    if not row or row[0] is None:
        return False
    local_dt = _parse_ts(str(row[0]))
    if local_dt is None:
        return None
    return local_dt > report_dt


def _decide_store(
    store: str, reports: list[dict[str, Any]], *, sqlite_path: str | Path,
) -> StoreMigrationStatus:
    candidates = [r for r in reports if _store_failed_count(r, store) is not None]
    if not candidates:
        return StoreMigrationStatus(
            store, False, f"{store}: no migration report found — will migrate"
        )

    latest = max(candidates, key=lambda r: str(r.get("completed_at") or ""))
    completed_at = str(latest.get("completed_at") or "")

    failed = _store_failed_count(latest, store) or 0
    if failed > 0:
        return StoreMigrationStatus(
            store, False,
            f"{store}: latest report ({completed_at}) shows {failed} failed "
            "row(s) — will migrate",
        )

    # A single-store report (``nx storage migrate <store>``) never carries a
    # top-level "verification" key (RDR-153 §schema); its ABSENCE is judged
    # on the store's own failed count alone (the "per-store success entries"
    # fallback). Present but not "verified" (mismatch / indeterminate)
    # taints the WHOLE report — the report records one verdict per run, not
    # per store (nexus-r0esi: an unverifiable run is never a pass).
    verification = latest.get("verification")
    if verification is not None and verification != "verified":
        return StoreMigrationStatus(
            store, False,
            f"{store}: latest report ({completed_at}) verification="
            f"{verification!r} — will migrate",
        )

    probes = FRESHNESS_PROBES.get(store) or ()
    freshness_confirmed = False
    if probes and completed_at:
        answers = [
            _has_newer_local_writes(sqlite_path, probe, completed_at)
            for probe in probes
        ]
        if any(a is True for a in answers):
            return StoreMigrationStatus(
                store, False,
                f"{store}: local writes newer than the report ({completed_at}) "
                "— will migrate",
            )
        # Confirmed only when EVERY probe positively answered "no newer
        # writes" — a missing/unreadable table (None) degrades to
        # trust-the-report, never to a confident clean (nexus-g8r2h fold).
        freshness_confirmed = bool(answers) and all(a is False for a in answers)

    if freshness_confirmed:
        line = f"{store}: already migrated {completed_at}, no newer local writes"
    else:
        line = (
            f"{store}: already migrated {completed_at} (no local freshness "
            "signal for this store — trusting the report)"
        )
    return StoreMigrationStatus(store, True, line)


def detect_already_migrated(
    *,
    sqlite_path: str | Path,
    reports_dir: str | Path | None = None,
    stores: Iterable[str] = LADDER_ORDER,
    force: bool = False,
) -> AlreadyMigratedPlan:
    """RDR-178 Gap 7: per-T2-store already-migrated detection.

    For each of *stores* (default: the full RDR-152 ladder), finds the
    NEWEST migration-report artifact under *reports_dir* (default
    ``<config>/migration-reports``) that mentions the store, and marks it
    SKIP when that report shows zero failed rows for the store, its
    run-level ``verification`` (if present) is ``"verified"``, and the
    store's anchor table (:data:`FRESHNESS_PROBES`) has no row newer than
    the report's ``completed_at`` (or has no configured/evaluable probe, in
    which case report-presence alone is trusted).

    ``force=True`` bypasses ALL of the above — no report is read, no SQLite
    connection is opened — and marks every store to run unconditionally.
    This is the escape hatch (this module had no prior ``--force``-style
    convention to extend, so this establishes it).

    Cheap and side-effect-free: reads local JSON files + issues bounded
    ``SELECT MAX(...)`` probes against the T2 SQLite source. No network
    call — this is precisely the "cheap...signal the module already has
    access to" the WORK spec calls for, reusing the RDR-153 report
    artifacts rather than inventing a second durable marker.
    """
    store_tuple = tuple(stores)
    if force:
        return AlreadyMigratedPlan(
            statuses=tuple(
                StoreMigrationStatus(
                    s, False, f"{s}: --force — migrating unconditionally"
                )
                for s in store_tuple
            )
        )

    dir_path = Path(reports_dir) if reports_dir is not None else _default_reports_dir()
    reports = _load_reports(dir_path)
    statuses = tuple(
        _decide_store(s, reports, sqlite_path=sqlite_path) for s in store_tuple
    )
    plan = AlreadyMigratedPlan(statuses=statuses)
    _log.info(
        "guided_upgrade_already_migrated_detect",
        skip=sorted(plan.skip_stores),
        run=sorted(plan.run_stores),
    )
    return plan


# ── ez5.4 seam + ez5.7: readiness contract ─────────────────────────────────


# ── nexus-8o9pm: voyage-capability pre-flight ──────────────────────────────


def footprint_has_voyage_collections(report: "DetectionReport") -> bool:
    """True iff the footprint has a data-bearing GENUINE-voyage collection.

    Voyage collections are NEVER cross-model-remapped to bge (RDR-162:
    re-embedding voyage text into bge silently changes recall), so a voyage
    collection can only migrate into a voyage-capable target — otherwise the
    migration blocks it. Empty/non-conformant/bge/minilm collections do not
    trigger the capability gate.

    Keys on the canonical :data:`detection._VOYAGE_MODELS` set (the same
    membership the classifier and ``cross_model_remappable`` use), NOT a
    ``startswith('voyage')`` prefix: a non-canonical ``voyage-*`` name is an
    unrecognized model (the classifier already gives it the re-index diagnostic),
    not a voyage-capability problem, so it must not mis-fire this gate.

    nexus-119p9 (GH #1381, second bug): a ``voyage-*``-NAMED collection whose
    stored vector MEASURED as local bge/ONNX (``measured_dim == _ONNX_DIM``,
    nexus-nb7hr) is a pre-RDR-109 mislabel, not real voyage data — it is
    ``cross_model_remappable`` (re-embedded locally at no cost, no Voyage key
    needed) and MUST NOT trip this gate; doing so SystemExits a bge-only
    target before the migration's auto-remap ever runs. So this predicate
    excludes any classification :func:`cross_model_remappable` accepts —
    the same measured-dim ground truth the orchestrator (:mod:`driver`) uses
    to build ``target_names``. A voyage-named collection with an unprobeable
    ``measured_dim`` (``None`` — empty or not probed) stays FAIL-CLOSED: it is
    NOT remappable (``cross_model_remappable`` requires a proven 768-dim
    measurement), so it still trips the gate — real voyage data would block
    mid-run otherwise.
    """
    from nexus.migration.detection import (  # noqa: PLC0415 — circular-dep avoidance (nexus.migration.detection)
        _VOYAGE_MODELS,
        cross_model_remappable,
    )

    return any(
        c.has_data and c.model in _VOYAGE_MODELS and not cross_model_remappable(c)
        for c in report.classifications
    )


# ── RDR-155 P4b P0e rehome: provisioning family moved to upgrade_ladder ──
# The provision → health-gate → version-pin → discoverability-gate family
# now LIVES in ``nexus.upgrade_ladder.provisioning`` (the surviving home —
# see that module's docstring and T2 ``nexus/p4b-sqlite-partition-2026-07-23``).
# The imports below are thin re-export SHIMS so this module's dying
# consumers (guided_upgrade_cmd, vector_etl's ingest-cloud probe, the
# pre-repoint test suites) stay untouched; they die with this file at P2.
# Surviving consumers (upgrade_ladder/preconditions.py, engine_version's
# docs) reference ``nexus.upgrade_ladder.provisioning`` directly.
from nexus.upgrade_ladder.provisioning import (  # noqa: F401,E402 — re-export shims for dying consumers
    HealthGateResult,
    ProvisionResult,
    ServiceReadiness,
    VersionPinOutcome,
    VoyageCapabilityOutcome,
    _default_discover_gate,
    _default_serve,
    _transport_error_types,
    establish_verified_service,
    provision_and_serve,
    verify_service_version,
    verify_voyage_capability,
    wait_for_service_health,
)
