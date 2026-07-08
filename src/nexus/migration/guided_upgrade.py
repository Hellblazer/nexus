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
    """
    if not legacy_footprint_pending():
        return ""
    return (
        " NOTE: this install has a legacy local store awaiting the ONE-TIME "
        "storage migration — if you recently upgraded from a 5.x install, "
        "run `nx guided-upgrade` (provisions the service AND migrates your "
        "data; shows a cost preview first) instead of starting the service "
        "by hand."
    )


def pending_migration_notice() -> str | None:
    """Best-effort bridge pointer from the routine commands to the cutover
    (nexus-0rwwv).

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
        detection = detect_pending_migration()
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

    local, cloud = _open(local_path)
    try:
        report = classify_collections(
            local_client=local,
            cloud_client=cloud,
            voyage_key_present=key_present,
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
FRESHNESS_PROBES: dict[str, tuple[str, str]] = {
    "memory": ("memory", "timestamp"),
    "plans": ("plans", "created_at"),
    "telemetry": ("search_telemetry", "ts"),
    "taxonomy": ("topics", "created_at"),
    "aspects": ("document_aspects", "extracted_at"),
    "chash": ("chash_index", "created_at"),
    "catalog": ("documents", "indexed_at"),
    "aspects_queue": ("aspect_extraction_queue", "enqueued_at"),
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

    probe = FRESHNESS_PROBES.get(store)
    freshness_confirmed = False
    if probe is not None and completed_at:
        newer = _has_newer_local_writes(sqlite_path, probe, completed_at)
        if newer is True:
            return StoreMigrationStatus(
                store, False,
                f"{store}: local writes newer than the report ({completed_at}) "
                "— will migrate",
            )
        freshness_confirmed = newer is False

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


@dataclass(frozen=True)
class VersionPinOutcome:
    """Result of the engine-service version-pin check (ez5.4 seam).

    ``ok`` is True only when the running service is at or above the required
    release (>= v0.1.5). ``reason`` carries the remedy when not.
    """

    ok: bool
    reason: str | None


#: Minimum engine-service release the guided upgrade will hand off to (RDR-002).
#: Bumped (0,1,5)->(0,1,8) for nexus-x2g1z (2026-06-24): engine-service-v0.1.8
#: is the current managed/native release; conexus relay [4566] confirmed the
#: managed service reports release_version on /version. The managed cloud gate
#: keeps its own floor (``managed_endpoint.MIN_MANAGED_RELEASE_VERSION``); this
#: constant is the native-binary floor enforced by ``verify_service_version``
#: and the 3rq00 parity test.
REQUIRED_RELEASE_VERSION: tuple[int, int, int] = (0, 1, 34)


def _parse_semver(raw: str | None) -> tuple[int, int, int] | None:
    """Parse ``X.Y.Z`` (optional leading ``v``) to a tuple, else ``None``.

    Fail-closed by construction: a blank, ``SNAPSHOT``/``dev``-qualified, or
    unparseable value returns ``None`` so the caller refuses. Trailing
    pre-release/build qualifiers (``-rc1``, ``+meta``) are rejected rather than
    silently accepted — a dev build is not a release.
    """
    if not raw:
        return None
    s = raw.strip()
    if not s:
        return None
    if s[:1] in ("v", "V"):
        s = s[1:]
    lower = s.lower()
    if "snapshot" in lower or "dev" in lower:
        return None
    parts = s.split(".")
    if len(parts) != 3:
        return None
    try:
        major, minor, patch = (int(p) for p in parts)
    except ValueError:
        return None
    if major < 0 or minor < 0 or patch < 0:
        return None
    return (major, minor, patch)


def verify_service_version(
    service_url: str,
    *,
    required: tuple[int, int, int] = REQUIRED_RELEASE_VERSION,
    http_get: Callable[[str, float], Any] | None = None,
    timeout_s: float = 5.0,
) -> VersionPinOutcome:
    """RDR-002 ez5.4 version-pin: assert ``release_version >= required``.

    GETs ``{service_url}/version`` and pins on the dedicated ``release_version``
    field (the RDR-002 contract; ``app_version`` is the dev coordinate and is
    NOT used). FAIL-CLOSED on every uncertain outcome: transport error, non-200,
    a missing / null / blank / dev / SNAPSHOT ``release_version`` (an engine
    predating the field is by definition older than the required release), an
    unparseable version, or a version below ``required``. ``http_get`` is an
    injection seam for tests.
    """
    req = ".".join(str(n) for n in required)
    if http_get is not None:
        _get = http_get
    else:

        def _get(url: str, timeout: float) -> Any:
            import httpx  # noqa: PLC0415 — optional/heavy dependency deferred (httpx)

            return httpx.get(url, timeout=timeout)

    url = service_url.rstrip("/") + "/version"
    try:
        resp = _get(url, timeout_s)
    except Exception as exc:  # noqa: BLE001 — any probe failure is fail-closed
        return VersionPinOutcome(
            ok=False, reason=f"could not reach {url} to verify version: {exc}"
        )
    if getattr(resp, "status_code", None) != 200:
        return VersionPinOutcome(
            ok=False,
            reason=f"{url} returned HTTP {getattr(resp, 'status_code', '?')}",
        )
    try:
        body = resp.json()
    except Exception:  # noqa: BLE001 — non-JSON body cannot confirm a version
        body = {}
    raw = body.get("release_version")
    parsed = _parse_semver(raw if isinstance(raw, str) else None)
    if parsed is None:
        return VersionPinOutcome(
            ok=False,
            reason=(
                f"engine-service reported no usable release_version "
                f"(got {raw!r}); a dev/unstamped or pre-RDR-002 build is older "
                f"than the required v{req} — refusing to proceed"
            ),
        )
    if parsed < required:
        got = ".".join(str(n) for n in parsed)
        return VersionPinOutcome(
            ok=False,
            reason=f"engine-service v{got} < required v{req} — upgrade the service",
        )
    return VersionPinOutcome(ok=True, reason=None)


# ── nexus-8o9pm: voyage-capability pre-flight ──────────────────────────────


def footprint_has_voyage_collections(report: "DetectionReport") -> bool:
    """True iff the footprint has a data-bearing voyage-model collection.

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
    """
    from nexus.migration.detection import _VOYAGE_MODELS  # noqa: PLC0415 — circular-dep avoidance (nexus.migration.detection)

    return any(
        c.has_data and c.model in _VOYAGE_MODELS for c in report.classifications
    )


@dataclass(frozen=True)
class VoyageCapabilityOutcome:
    """Whether the target service can serve voyage-model collections."""

    ok: bool
    reason: str | None


def verify_voyage_capability(
    service_url: str,
    *,
    http_get: Callable[[str, float], Any] | None = None,
    timeout_s: float = 5.0,
) -> VoyageCapabilityOutcome:
    """Assert the target service embeds with a voyage model (its actual capability).

    GETs ``{service_url}/version`` and checks ``embedding_models`` for any
    ``voyage-*`` token — the AUTHORITATIVE server-side signal (not the client's
    voyage-key probe, which is wrong in service mode). FAIL-CLOSED on transport
    error, non-200, or a missing/empty/voyage-absent ``embedding_models``: if we
    cannot confirm voyage capability, the voyage collections would block, so the
    caller must surface that early.
    """
    if http_get is not None:
        _get = http_get
    else:

        def _get(url: str, timeout: float) -> Any:
            import httpx  # noqa: PLC0415 — optional/heavy dependency deferred (httpx)

            return httpx.get(url, timeout=timeout)

    url = service_url.rstrip("/") + "/version"
    try:
        resp = _get(url, timeout_s)
    except Exception as exc:  # noqa: BLE001 — any probe failure is fail-closed
        return VoyageCapabilityOutcome(
            ok=False, reason=f"could not reach {url} to check voyage capability: {exc}"
        )
    if getattr(resp, "status_code", None) != 200:
        return VoyageCapabilityOutcome(
            ok=False, reason=f"{url} returned HTTP {getattr(resp, 'status_code', '?')}"
        )
    try:
        body = resp.json()
    except Exception:  # noqa: BLE001 — fallback path; safe default ({}) returned when response body is not JSON
        body = {}
    models = body.get("embedding_models") or []
    if any(isinstance(m, str) and m.startswith("voyage") for m in models):
        return VoyageCapabilityOutcome(ok=True, reason=None)
    return VoyageCapabilityOutcome(
        ok=False,
        reason=(
            f"target service embeds with {list(models)} and cannot serve voyage "
            "collections (voyage vectors are not re-embeddable into bge without "
            "changing recall)"
        ),
    )


@dataclass(frozen=True)
class ServiceReadiness:
    """Outcome of the Stage 2->3 readiness contract.

    ``service_url`` is the VERIFIED endpoint and is set ONLY when ``ready`` is
    True (health-ready AND version-pinned). On any failure it is ``None`` and
    ``reason`` carries the remedy — the caller (ez5.10) hard-fails and never
    hands a not-ready service to ``migrate-to-service``.
    """

    ready: bool
    service_url: str | None
    reason: str | None
    version_ok: bool
    provision: "ProvisionResult | None"
    health: "HealthGateResult | None"


def _default_discover_gate() -> bool:
    """Confirm a LIVE, discoverable ``storage_service`` lease exists.

    nexus-f9y78: ``/health`` hits the service process directly, so it stays 200
    even when the inline-provisioned supervisor died (e.g. OOM-killed) leaving an
    orphaned-but-serving JVM whose lease aged out (15s TTL). Every env-unpinned
    consumer then resolves the endpoint via lease discovery and races expiry.

    This is a pure DISCOVERABILITY CHECK against the PG-arch canonical resolver
    (``service_endpoint.discover_lease`` — the same path the downstream migration
    legs / T2 / T3 / catalog consumers use): a missing lease means the supervisor
    is gone and downstream discovery would fail, so readiness fails fast. It does
    NOT re-spawn — routing a dead-lease-but-live-JVM case through
    ``ensure_storage_supervisor`` would spawn a SECOND JVM alongside the orphaned
    one (``discover()`` returns ``None`` so the dead-pid guard never fires),
    worsening the OOM that caused the bug.

    On LINUX the orphan scenario is now closed at the source: the JVM is armed
    with PR_SET_PDEATHSIG (storage_service_daemon, nexus-03bcg), so a dead
    supervisor leaves no orphaned-but-serving JVM. This gate remains a correct
    belt-and-suspenders (and covers macOS/non-Linux, where the orphan can still
    linger). Any remaining resolver-layer heal for the macOS-without-autostart
    path is tracked under nexus-03bcg.
    """
    from nexus.db import service_endpoint  # noqa: PLC0415 — deferred import — heavy dep loaded only on this path

    base_url, _token = service_endpoint.discover_lease()
    return base_url is not None


def establish_verified_service(
    *,
    timeout_s: float = 30.0,
    interval_s: float = 1.0,
    provision: Callable[[], "ProvisionResult"] | None = None,
    health_gate: Callable[..., "HealthGateResult"] | None = None,
    verify_version: Callable[[str], VersionPinOutcome] | None = None,
    discover_gate: Callable[[], bool] | None = None,
) -> ServiceReadiness:
    """Provision -> health-gate -> version-pin -> discoverability-gate; emit a
    verified url iff all pass.

    Order: stand up the service (ez5.6), then BOUNDED health-gate it (ez5.5 —
    a not-ready service short-circuits before the version probe), then version-
    pin it (ez5.4 seam), then confirm a LIVE, DISCOVERABLE lease (nexus-f9y78 —
    ``/health`` alone passes on an orphaned JVM whose supervisor died and whose
    lease aged out). The verified ``service_url`` is emitted ONLY when the
    service is health-ready AND version-pinned AND its lease is discoverable.

    All steps are injection seams for tests; ``verify_version`` defaults to the
    fail-closed placeholder until ez5.4 lands.
    """
    _provision = provision if provision is not None else provision_and_serve
    _health = health_gate if health_gate is not None else wait_for_service_health
    _verify = verify_version if verify_version is not None else verify_service_version
    _discover = discover_gate if discover_gate is not None else _default_discover_gate

    prov = _provision()

    health = _health(
        service_url=prov.service_url, timeout_s=timeout_s, interval_s=interval_s
    )
    if not health.ready:
        reason = (
            f"storage service at {prov.service_url} did not become healthy "
            f"within {timeout_s:.0f}s "
            f"(last status={health.last_status}, error={health.last_error})"
        )
        _log.warning("guided_upgrade_not_ready", stage="health", reason=reason)
        return ServiceReadiness(
            ready=False, service_url=None, reason=reason,
            version_ok=False, provision=prov, health=health,
        )

    pin = _verify(prov.service_url)
    if not pin.ok:
        _log.warning("guided_upgrade_not_ready", stage="version", reason=pin.reason)
        return ServiceReadiness(
            ready=False, service_url=None, reason=pin.reason,
            version_ok=False, provision=prov, health=health,
        )

    if not _discover():
        # health + version passed, but the lease is not discoverable — the inline
        # supervisor likely died (OOM) leaving an orphaned /health-green service.
        # Emitting the url here would hand downstream legs an endpoint that
        # env-unpinned consumers cannot discover (they race the 15s TTL). Fail
        # closed. (nexus-f9y78)
        reason = (
            f"storage service at {prov.service_url} is health-ready and "
            "version-pinned but its lease is NOT discoverable — the inline "
            "supervisor likely died (e.g. OOM-killed) leaving an orphaned "
            "service; consumers that resolve the endpoint via lease discovery "
            "would race expiry. On a memory-constrained host, set "
            "NX_SERVICE_MAX_HEAP (e.g. 1g) to cap the service heap and reduce "
            "OOM risk, then re-run."
        )
        _log.warning("guided_upgrade_not_ready", stage="discover", reason=reason)
        return ServiceReadiness(
            ready=False, service_url=None, reason=reason,
            version_ok=True, provision=prov, health=health,
        )

    _log.info("guided_upgrade_service_verified", service_url=prov.service_url)
    return ServiceReadiness(
        ready=True, service_url=prov.service_url, reason=None,
        version_ok=True, provision=prov, health=health,
    )


# ── ez5.6: provision-and-serve sequence ────────────────────────────────────


@dataclass(frozen=True)
class ProvisionResult:
    """The serving endpoint after the Stage-2 provision+serve sequence.

    ``service_url`` is the UNVERIFIED endpoint (a lease exists, but the service
    has not yet been version-pinned or health-gated). ez5.7 only emits it as a
    VERIFIED url after the pin (ez5.4) and the health-gate (ez5.5) pass.
    """

    service_url: str
    host: str
    port: int
    pid: int | None
    generation: int | None


def _default_serve() -> Any:
    from nexus.commands.init import provision_and_start_service  # noqa: PLC0415 — circular-dep avoidance (nexus.commands.init)

    return provision_and_start_service()


def provision_and_serve(
    *,
    serve: Callable[[], Any] | None = None,
) -> ProvisionResult:
    """Provision + serve the local storage service, returning its endpoint.

    Reuses the FULL ``nx init --service`` sequence (no fork): provision PG, lock
    the embedder + fetch the bge-768 ONNX the service reads, acquire the native
    binary, and start the persistent supervisor — via
    ``init.provision_and_start_service``. (Calling only the PG-provision + start
    steps and skipping the embedder/model fetch is what crashed the service on a
    missing bge ONNX — RDR-002 ez5.13.) The returned ``service_url`` is
    UNVERIFIED — the caller (ez5.7) version-pins + health-gates it first.

    Raises ``RuntimeError`` when the serve step yields no lease — the guided
    upgrade's default provision path is LOCAL-mode only (cloud mode has no local
    service to migrate into; cloud users gate an existing service via
    ``--service-url``). ``serve`` is an injection seam for tests.
    """
    _serve = serve if serve is not None else _default_serve

    lease = _serve()
    if lease is None:
        raise RuntimeError(
            "guided-upgrade provisioning requires a LOCAL service, but the "
            "deployment is in cloud mode (no local service to migrate into) — "
            "point --service-url at the managed service instead"
        )

    endpoint = getattr(lease, "endpoint", None) or {}
    host = endpoint.get("host")
    port = endpoint.get("port")
    # `is None` (not falsiness): port 0 is a valid OS-assigned ephemeral port
    # (code-review M2) — only a genuinely absent host/port is malformed.
    if host is None or port is None or host == "":
        raise RuntimeError(
            "storage service started but its lease endpoint is missing host/port "
            f"(endpoint={endpoint!r}) — cannot derive a service_url"
        )
    result = ProvisionResult(
        service_url=f"http://{host}:{port}",
        host=str(host),
        port=int(port),
        pid=endpoint.get("pid"),
        generation=getattr(lease, "generation", None),
    )
    _log.info(
        "guided_upgrade_provisioned",
        service_url=result.service_url,
        generation=result.generation,
    )
    return result


# ── ez5.5: bounded health-gate ─────────────────────────────────────────────


@dataclass(frozen=True)
class HealthGateResult:
    """Outcome of the bounded wait for engine-service readiness.

    ``ready`` is the gate the handoff (ez5.7) branches on: the command must
    NEVER call ``migrate-to-service`` unless ``ready`` is True. The diagnostic
    fields back the hard-fail remedy message on a not-ready service.
    """

    ready: bool
    attempts: int
    last_status: int | None
    last_error: str | None
    waited_s: float


def _transport_error_types() -> tuple[type[BaseException], ...]:
    """The connection/timeout errors a poll attempt may raise and retry on.

    ``OSError`` (covers ``ConnectionError``) plus httpx's transport errors when
    httpx is importable. Anything outside this set is a real bug and propagates
    loud — the gate never swallows unexpected failures.
    """
    types: list[type[BaseException]] = [OSError]
    try:
        import httpx  # noqa: PLC0415 — optional/heavy dependency deferred (httpx)

        types.extend([httpx.ConnectError, httpx.TimeoutException])
    except Exception:  # noqa: BLE001 — httpx optional at probe-build time
        pass
    return tuple(types)


def wait_for_service_health(
    *,
    service_url: str,
    timeout_s: float = 30.0,
    interval_s: float = 1.0,
    http_get: Callable[[str, float], Any] | None = None,
    sleep: Callable[[float], None] | None = None,
    clock: Callable[[], float] | None = None,
) -> HealthGateResult:
    """Poll ``GET {service_url}/health`` until ready, BOUNDED by ``timeout_s``.

    Ready == HTTP 200 AND ``body["db"] == "up"`` (the ez5.1 pinned /health
    contract). Always makes at least one attempt; never sleeps past the
    deadline; returns ``ready=False`` with the last status/error when the
    service does not come up in time — the caller hard-fails with a remedy
    (ez5.7), it does NOT wait forever.

    ``http_get`` / ``sleep`` / ``clock`` are injection seams for deterministic
    tests; production uses ``httpx.get`` / ``time.sleep`` / ``time.monotonic``.
    """
    if timeout_s < 0:
        raise ValueError(f"timeout_s must be non-negative, got {timeout_s}")
    if interval_s <= 0:
        # A non-positive interval never advances an injected clock (and busy-loops
        # a real one), so the bounded-poll guarantee would not hold (code-review M1).
        raise ValueError(f"interval_s must be positive, got {interval_s}")

    import time  # noqa: PLC0415 — stdlib deferred to call site (time)

    _sleep = sleep if sleep is not None else time.sleep
    _clock = clock if clock is not None else time.monotonic
    if http_get is not None:
        _get = http_get
    else:

        def _get(url: str, timeout: float) -> Any:
            import httpx  # noqa: PLC0415 — optional/heavy dependency deferred (httpx)

            return httpx.get(url, timeout=timeout)

    url = service_url.rstrip("/") + "/health"
    req_timeout = max(0.1, min(interval_s, 5.0)) if interval_s > 0 else 1.0
    caught = _transport_error_types()

    start = _clock()
    attempts = 0
    last_status: int | None = None
    last_error: str | None = None

    while True:
        try:
            resp = _get(url, req_timeout)
            attempts += 1
            last_status = resp.status_code
            try:
                body = resp.json()
            except Exception:  # noqa: BLE001 — a non-JSON body is just "not ready"
                body = {}
            if resp.status_code == 200 and body.get("db") == "up":
                return HealthGateResult(
                    ready=True,
                    attempts=attempts,
                    last_status=last_status,
                    last_error=None,
                    waited_s=_clock() - start,
                )
            detail = body.get("detail")
            last_error = detail or (
                f"status={body.get('status')!r} db={body.get('db')!r}"
            )
        except caught as exc:
            attempts += 1
            last_error = str(exc)

        elapsed = _clock() - start
        if elapsed + interval_s >= timeout_s:
            _log.warning(
                "guided_upgrade_health_gate_timeout",
                url=url,
                attempts=attempts,
                last_status=last_status,
                last_error=last_error,
                waited_s=elapsed,
            )
            return HealthGateResult(
                ready=False,
                attempts=attempts,
                last_status=last_status,
                last_error=last_error,
                waited_s=elapsed,
            )
        _sleep(interval_s)
