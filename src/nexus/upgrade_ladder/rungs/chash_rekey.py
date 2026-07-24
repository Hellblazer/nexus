# SPDX-License-Identifier: AGPL-3.0-or-later
# Copyright (c) 2026 Hal Hildebrand. All rights reserved.
"""RDR-180 Item6 (nexus-jxizy.6): the chash-rekey rung — the freeze-gated
per-store cutover from legacy half-digest keys to the full 32-byte chash.

Choreography (T2 ``nexus_rdr/180-engine-cohort-design`` + amendments):
the RDR-180 engine generation (bytea columns, NOT VALID octet CHECKs,
``/v1/remap/rekey``) is installed by the ENGINE precondition before this
rung walks; this rung then, under the writer freeze:

1. **Freeze** — the RDR-159 ``migration.state`` sentinel suspends aspect
   workers / ``nx index`` cross-process and degrades MCP writers loud.
   The pre-existing sentinel state is snapshotted and RESTORED afterwards
   (an already-migrated store keeps its ``migrated`` fact).
2. **Rekey** — ``POST /v1/remap/rekey`` (idempotent, per-tenant, one
   transaction engine-side): digest-mismatch predicate, alias build,
   Item8 disposition, two-phase collapse, full cascade, in-transaction
   verification scans. The response counts are the audit envelope.
3. **VALIDATE** — local mode: the five octet CHECKs are validated via the
   ADMIN connection (table owner; VALIDATE scans all rows RLS-exempt —
   deliberately NOT a Liquibase boot changeset, which would crash-loop
   un-rekeyed stores, and NOT the svc role, which cannot VALIDATE).
   Managed mode: validation is the operator's deploy-choreography step
   (Hal-relay surface) — converge reports DEFERRED-shaped detail rather
   than pretending.
4. **Re-provision** — local mode: the diag counts view is recreated
   (rdr180-001 dropped it; the era-safe predicate regenerates from
   ``chash_tables``).

``converge`` raises on a non-zero engine-side residual (the endpoint's
own in-transaction scan) — never a silent partial cutover (RDR-180
Failure Modes: no dual-width window within a tenant).
"""
from __future__ import annotations

from collections.abc import Callable
from typing import Any

import structlog

from nexus.db.t2.rekey_client import RekeyJobLostError
from nexus.upgrade_ladder.protocol import (
    ConvergeOutcome,
    ConvergeResult,
    ProgressReporter,
    RungStatus,
)

_log = structlog.get_logger(__name__)

RUNG_CHASH_REKEY = "chash-rekey"

#: The five octet CHECKs the rung VALIDATEs post-rekey — MUST mirror the
#: rdr180-001 changeset's constraint names (pinned by test against the XML).
OCTET_CHECKS: tuple[tuple[str, str], ...] = (
    ("nexus.chunks_384", "chunks_384_chash_octet_check"),
    ("nexus.chunks_768", "chunks_768_chash_octet_check"),
    ("nexus.chunks_1024", "chunks_1024_chash_octet_check"),
    ("nexus.catalog_document_chunks", "catalog_document_chunks_chash_octet_check"),
    # ("nexus.chash_index", ...) REMOVED — RDR-187/nexus-piwya.9 (.9 critique
    # Critical 1): the router table is dropped by the paired engine's
    # rdr187-2 changeset. Left in place, this rung would VALIDATE against a
    # missing relation on EVERY nx upgrade forever (RuntimeError from
    # run_admin_sql), _pointer_debt would silently degrade to unknowable,
    # and _validated_probe could never count to five again — permanently
    # un-converged. Boxes that converged pre-drop stay converged: the probe
    # counts THESE four names, and all four survive the drop.
)


#: The three CONTENT tables. Their octet CHECK is the RDR-180 contract —
#: every chunk's identity IS its 32-byte digest — and a clean rekey always
#: leaves them conformant, so these VALIDATE unconditionally.
CONTENT_OCTET_CHECKS: tuple[tuple[str, str], ...] = OCTET_CHECKS[:3]

#: The POINTER table (the manifest — since RDR-187 dropped the chash_index
#: router, the only one left). A lived-in store can carry orphan pointers
#: whose content stopped existing long before the cutover (production
#: 2026-07-20: 292,230 chash_index — died with the table — + 426
#: catalog_document_chunks, none with a chash_alias entry; the manifest's
#: are nexus-uu4ue's remaining scope). VALIDATE is table-grain, so it
#: cannot succeed while they exist — that is arithmetic, not judgement — and failing
#: the whole upgrade over pre-existing debt would strand every such install.
POINTER_OCTET_CHECKS: tuple[tuple[str, str], ...] = OCTET_CHECKS[3:]


def validate_statements(
    checks: tuple[tuple[str, str], ...] = OCTET_CHECKS,
) -> tuple[str, ...]:
    """The admin-connection VALIDATE statements (SHARE UPDATE EXCLUSIVE —
    online, no write block)."""
    return tuple(
        f"ALTER TABLE {table} VALIDATE CONSTRAINT {name}"
        for table, name in checks
    )


class ChashRekeyRung:
    """Freeze → rekey → validate → re-provision, behind injected seams.

    Constructor seams (all injectable for tests):

    - ``rekey_fn(orphan_policy) -> dict`` — drives ``POST /v1/remap/rekey``
      and returns the counts envelope. REQUIRED.
    - ``validate_fn() -> bool | None`` — run the VALIDATE statements via
      the local admin connection. ``None`` means "not possible here"
      (managed mode) — surfaced in the converge detail, never silent.
    - ``reprovision_fn() -> None`` — recreate the diag counts view
      (local); no-op default for managed.
    - ``freeze_fn() -> Callable[[], None]`` — enter the writer freeze,
      returning the restore callable. Defaults to the RDR-159 sentinel
      snapshot/restore.
    - ``detect_probe_fn() -> int | None`` — READ-ONLY count of
      width-non-conformant poison rows (the diag path), ``None`` when
      unknowable (managed / no diag creds). Drives ``detect`` only; the
      idempotent rekey is the actual convergence mechanism.
    - ``validated_probe_fn() -> bool | None`` — READ-ONLY: are all five
      octet CHECKs convalidated? This is the DATA-SIDE completion marker
      (nexus-p78a0): boot never VALIDATEs, only this rung's own VALIDATE
      step (or the managed operator's choreography) does — so ``True``
      means the rekey provably completed. Without it, the raw ``detect()``
      surfaces (``nx doctor``, ``nx upgrade --dry-run``, the transition
      callout — none of which open the completion ledger) would report a
      rekeyed store as pending forever. ``None`` = unknowable → pending.
    """

    name = RUNG_CHASH_REKEY

    def __init__(
        self,
        *,
        rekey_fn: Callable[[str], dict[str, Any]],
        validate_fn: Callable[..., bool | None] | None = None,
        pointer_debt_fn: Callable[[], dict[str, int] | None] | None = None,
        reprovision_fn: Callable[[], None] | None = None,
        freeze_fn: Callable[[], Callable[[], None]] | None = None,
        detect_probe_fn: Callable[[], int | None] | None = None,
        validated_probe_fn: Callable[[], bool | None] | None = None,
        applicable_fn: Callable[[], bool] | None = None,
        orphan_policy: str = "drop",
    ) -> None:
        self._rekey_fn = rekey_fn
        self._validate_fn = validate_fn if validate_fn is not None else (lambda _checks=None: None)
        self._pointer_debt_fn = (
            pointer_debt_fn if pointer_debt_fn is not None else (lambda: None)
        )
        self._reprovision_fn = reprovision_fn if reprovision_fn is not None else (lambda: None)
        self._freeze_fn = freeze_fn if freeze_fn is not None else _sentinel_freeze
        self._detect_probe_fn = detect_probe_fn if detect_probe_fn is not None else (lambda: None)
        self._validated_probe_fn = validated_probe_fn if validated_probe_fn is not None else (lambda: None)
        self._applicable_fn = applicable_fn if applicable_fn is not None else (lambda: True)
        if orphan_policy not in ("drop", "synthesize"):
            raise ValueError(
                f"orphan_policy must be 'drop' or 'synthesize', got {orphan_policy!r}"
            )
        self._orphan_policy = orphan_policy
        self._last_counts: dict[str, Any] | None = None

    # ── Rung protocol ────────────────────────────────────────────────────────

    def detect(self) -> RungStatus:
        """READ-ONLY. The RUNNER consults the completion ledger, but the
        read-only surfaces (``nx doctor``'s pending sweep, ``nx upgrade
        --dry-run``, ``pending_data_rung_callout``) call this RAW — so
        convergence must be visible from the DATA (nexus-p78a0): all five
        octet CHECKs convalidated is the unforgeable completion marker
        (only this rung's VALIDATE, or the managed operator's, sets it).
        Below that: a countable-zero probe (local diag path) reports
        converged-pending-verify; unknown (managed) reports applies — the
        idempotent rekey settles it."""
        if not self._applicable_fn():
            # Managed-cloud install: this box does not own the store's admin
            # path — the cloud-side rekey is the operator's deploy
            # choreography (design record: T2 nexus_rdr/180-engine-cohort-
            # design), so `nx upgrade` is NOT the remedy here and doctor
            # must not report a pending rung it cannot act on (the same
            # detect-and-skip shape as the t2-schema rung in service mode).
            return RungStatus(applicable=False, converged=False, pending_detail=(
                "chash rekey is the operator's cloud deploy choreography on "
                "managed installs — nothing for this box to converge"
            ))
        if self._validated_probe_fn():
            # Validated checks can only exist AFTER a clean rekey (converge
            # raises before VALIDATE on any residual; VALIDATE itself scans
            # every row) — the store has provably converged, regardless of
            # what any ledger says.
            return RungStatus(applicable=True, converged=True)
        probe = self._detect_probe_fn()
        if probe == 0:
            return RungStatus(applicable=True, converged=False, pending_detail=(
                "no width-non-conformant poison rows counted — rekey run "
                "needed once to build the alias map and validate the checks"
            ))
        if probe is None:
            return RungStatus(applicable=True, converged=False, pending_detail=(
                "conformance unknowable here (no diag path) — the "
                "idempotent rekey converges it"
            ))
        return RungStatus(applicable=True, converged=False, pending_detail=(
            f"{probe} width-non-conformant poison row(s) pending rekey"
        ))

    def _rekey_with_restart_retry(self, report: ProgressReporter) -> dict[str, Any]:
        """Drive the rekey, retrying ONCE if the engine restarted under it.

        nexus-sfgqi. ``HttpRekeyClient.rekey`` distinguishes three outcomes, but
        the ladder runner wraps ``converge()`` in a blanket
        ``except Exception -> RungOutcome.FAILED``, so a could-not-tell landed
        as a hard rung failure identical to a genuine one. For
        ``RekeyJobLostError`` specifically that is the wrong answer available:
        the rekey is idempotent, so the correct response is to run it again —
        over an already-rekeyed store it reports all-zero counts and converges,
        and over a rolled-back one it does the work.

        Deliberately narrow. Only the engine-restarted case retries, and only
        once. A timeout is NOT retried: the original transaction may still be
        in flight, and starting a second rekey against a live one would queue
        behind the per-tenant advisory lock rather than resolve anything. A
        genuine failure is not retried at all. Anything the retry does not
        cover still propagates, so the runner's FAILED remains the default.
        """
        try:
            return self._rekey_fn(self._orphan_policy)
        except RekeyJobLostError as exc:
            report.emit(
                "chash_rekey_lost_to_restart_retrying",
                rung=self.name,
                detail=str(exc),
            )
            return self._rekey_fn(self._orphan_policy)

    def converge(self, report: ProgressReporter) -> ConvergeResult:
        report.emit("chash_rekey_freeze", rung=self.name)
        restore = self._freeze_fn()
        try:
            # Ceiling baseline: pointer debt as it stood BEFORE this rekey.
            debt_before = self._pointer_debt_fn()
            report.emit("chash_rekey_rekey", rung=self.name, orphan_policy=self._orphan_policy)
            counts = self._rekey_with_restart_retry(report)
            self._last_counts = counts
            residual = int(counts.get("residual_mismatched", -1))
            dangling = int(counts.get("dangling_manifest", -1))
            if residual != 0:
                raise RuntimeError(
                    f"rekey left {residual} mismatched content row(s) — "
                    "refusing to record completion (no dual-width window)"
                )
            report.emit("chash_rekey_validate", rung=self.name)
            # nexus-noa8d: pointer-table debt decides WHICH checks validate.
            # THE CEILING (never a table exemption): debt measured BEFORE the
            # rekey is grandfathered; any growth is damage this run caused and
            # fails loud. A blanket "these two tables do not gate" would wave a
            # future rekey's brand-new orphans through as merely observed —
            # the silent-scope-reduction shape wearing a different hat.
            #
            # WHY THE CEILING IS RUN-LOCAL rather than persisted across runs:
            # the only cross-run store available to a rung is the completion
            # record, whose `detail` field protocol.py documents as
            # "observability-only and accepted lossy (RF-186-2)". A gate that
            # decides whether a constraint may be skipped is load-bearing, and
            # load-bearing state must not ride a field its own contract says
            # may be lost. Measuring before-versus-after within the run needs
            # no storage and catches the same class: orphans this rekey
            # created. Pre-existing debt is, by construction, what remains.
            debt_after = self._pointer_debt_fn()
            debt_note = ""
            if debt_after is None:
                checks = OCTET_CHECKS  # unknowable (managed): unchanged behaviour
            else:
                if debt_before is not None:
                    grown = {
                        t: (n, debt_before.get(t, 0))
                        for t, n in debt_after.items()
                        if n > debt_before.get(t, 0)
                    }
                    if grown:
                        raise RuntimeError(
                            "rekey created NEW non-conformant pointer rows — "
                            + ", ".join(
                                f"{t}: {before} -> {after}"
                                for t, (after, before) in grown.items()
                            )
                            + " — the amnesty covers PRE-EXISTING debt only; "
                            "refusing to record completion"
                        )
                outstanding = {t: n for t, n in debt_after.items() if n > 0}
                if outstanding:
                    checks = CONTENT_OCTET_CHECKS
                    debt_note = (
                        " | pointer-table CHECKs left NOT VALID over "
                        + ", ".join(f"{t}={n}" for t, n in sorted(outstanding.items()))
                        + " PRE-EXISTING orphan pointer(s) (content absent before this "
                        "rekey). Chunk identity IS enforced; new writes to the pointer "
                        "tables are enforced too (NOT VALID gates existing rows only). "
                        "Clean the orphans, then re-run to validate the remaining two."
                    )
                else:
                    checks = OCTET_CHECKS
            validated = self._validate_fn(checks)
            report.emit("chash_rekey_reprovision", rung=self.name)
            self._reprovision_fn()
            detail = (
                f"rekeyed={counts.get('rehashed', 0)} "
                f"aliased={counts.get('alias_rows', 0)} "
                f"collapsed={counts.get('collapsed_duplicates', 0)} "
                f"refs={counts.get('reference_only_resolved', 0)} "
                f"orphans_dropped={counts.get('orphans_dropped', 0)} "
                f"orphans_synthesized={counts.get('orphans_synthesized', 0)} "
                f"dangling_manifest={dangling} "
                f"validated={'yes' if validated else 'operator-step (managed)' if validated is None else 'FAILED'}"
                + debt_note
            )
            # rdr180-17 / F2: the engine ANALYZEs chash_alias inside the rekey
            # transaction so the planner can see the alias rows it just wrote.
            # Postgres silently SKIPS that for a role without MAINTAIN (PG17+),
            # so the engine reports whether it took effect. Surface a FALSE —
            # the rekey is still correct, but a multi-tenant store planned it
            # blind, which cost 101 minutes vs 461 seconds in production.
            if counts.get("alias_stats_refreshed") is False:
                detail += (
                    " | NOTE: alias planner statistics were NOT refreshed "
                    "(engine lacks MAINTAIN on nexus.chash_alias, or PostgreSQL "
                    "predates 17) — correct, but a multi-tenant rekey may be slow"
                )
            _log.info(
                "chash_rekey_converged",
                counts=counts,
                validated=validated,
                orphan_policy=self._orphan_policy,
            )
            if validated is False:
                raise RuntimeError(
                    "octet CHECK VALIDATE failed after a clean rekey — "
                    "investigate before re-running (constraint names: "
                    + ", ".join(n for _, n in checks)
                )
            return ConvergeResult(outcome=ConvergeOutcome.COMPLETED, detail=detail)
        finally:
            # critic-180-cohort finding 3: a restore() failure must NEVER
            # mask the root-cause rekey exception (Python replaces the
            # propagating exception with the finally's). Log-and-suppress:
            # a stuck sentinel fails SAFE (writers stay frozen, doctor
            # shows migrating) and is trivially operator-clearable.
            try:
                restore()
            except Exception:  # noqa: BLE001 — deliberate: root cause wins
                _log.error("chash_rekey_freeze_restore_failed", exc_info=True)

    def verify(self) -> bool:
        """READ-ONLY where possible: the diag probe must count zero. Where
        no probe exists (managed), the recorded converge envelope's zero
        residual is the evidence — converge already raised on non-zero."""
        probe = self._detect_probe_fn()
        if probe is not None:
            return probe == 0
        counts = self._last_counts
        return counts is not None and int(counts.get("residual_mismatched", -1)) == 0


def default_chash_rekey_rung() -> "ChashRekeyRung":
    """Production wiring: engine rekey via ``HttpRekeyClient`` (the P0e
    surviving rekey surface, ``nexus.db.t2.rekey_client``); local-mode
    VALIDATE + diag probe + view re-provision through the admin/diag
    connections; managed mode degrades each to its honest None/no-op."""

    def _rekey(orphan_policy: str) -> dict[str, Any]:
        from nexus.db.t2.rekey_client import HttpRekeyClient  # noqa: PLC0415 — deferred import cost

        with HttpRekeyClient() as store:
            return store.rekey(orphan_policy)

    def _detect_probe() -> int | None:
        try:
            from nexus.db.chash_tables import chash_conformance_statements, legacy_chash_conformance_statements  # noqa: PLC0415 — deferred
            from nexus.db.diag_connection import resolve_diag_credentials, run_diagnostic_sql  # noqa: PLC0415 — deferred

            creds = resolve_diag_credentials(None)
            if creds is None:
                return None
            try:
                counts = run_diagnostic_sql(chash_conformance_statements(), creds)
            except Exception:  # noqa: BLE001 — stale/absent view: fall back to direct counts
                counts = run_diagnostic_sql(legacy_chash_conformance_statements(), creds)
            return sum(int(c) for c in counts)
        except Exception:  # noqa: BLE001 — probe unknowable ≠ rung failure; converge is idempotent
            return None

    def _pointer_debt() -> dict[str, int] | None:
        """Per-table non-conformant counts for the two POINTER tables
        (nexus-noa8d). Read-only, via the same diag choke point as the poison
        probe. ``None`` = unknowable (managed mode / no diag creds), which
        leaves the pre-existing all-five VALIDATE behaviour untouched rather
        than guessing a store is clean."""
        try:
            from nexus.db.diag_connection import resolve_diag_credentials, run_diagnostic_sql  # noqa: PLC0415 — deferred

            creds = resolve_diag_credentials(None)
            if creds is None:
                return None
            out: dict[str, int] = {}
            for table, _name in POINTER_OCTET_CHECKS:
                rows = run_diagnostic_sql(
                    (
                        f"SELECT count(*) FROM {table} "
                        f"WHERE octet_length(chash) <> 32",
                    ),
                    creds,
                )
                out[table] = sum(int(r) for r in rows)
            return out
        except Exception:  # noqa: BLE001 — unknowable ≠ failure; all-five behaviour stands
            return None

    def _validate(checks: tuple[tuple[str, str], ...] = OCTET_CHECKS) -> bool | None:
        from nexus.db.admin_sql import run_admin_sql  # noqa: PLC0415 — deferred

        return run_admin_sql(validate_statements(checks))

    def _validated_probe() -> bool | None:
        # The data-side completion marker (see the class docstring): all
        # five octet CHECKs convalidated. pg_constraint is a metadata
        # target under the diag lint, so this rides the same read-only
        # choke point as the poison probe.
        try:
            from nexus.db.diag_connection import resolve_diag_credentials, run_diagnostic_sql  # noqa: PLC0415 — deferred

            creds = resolve_diag_credentials(None)
            if creds is None:
                return None
            names = ", ".join(f"'{n}'" for _, n in OCTET_CHECKS)
            # Schema-qualified via conrelid (critic S2): a same-named
            # constraint in another schema must not inflate the count.
            # (Miscount fails SAFE — not-converged — but exactness is
            # cheap.)
            counts = run_diagnostic_sql(
                (
                    "SELECT count(*) FROM pg_catalog.pg_constraint c "
                    "JOIN pg_catalog.pg_class t ON t.oid = c.conrelid "
                    "JOIN pg_catalog.pg_namespace n ON n.oid = t.relnamespace "
                    f"WHERE n.nspname = 'nexus' AND c.conname IN ({names}) "
                    "AND c.convalidated",
                ),
                creds,
            )
            return int(counts[0]) == len(OCTET_CHECKS)
        except Exception:  # noqa: BLE001 — probe unknowable ≠ converged; detect degrades to pending
            return None

    def _reprovision() -> None:
        from nexus.db.pg_provision import reprovision_diag_view_best_effort  # noqa: PLC0415 — deferred

        reprovision_diag_view_best_effort()

    def _locally_actionable() -> bool:
        # This box owns the rekey choreography only when it holds the local
        # admin path (pg_credentials — the bundled/local-service install
        # class). Managed-cloud clients skip: the operator drives the
        # cloud-side rekey at engine deploy.
        try:
            from nexus.db.admin_sql import resolve_admin_credentials  # noqa: PLC0415 — deferred

            return resolve_admin_credentials(None) is not None
        except Exception:  # noqa: BLE001 — unreadable creds = not actionable here
            return False

    return ChashRekeyRung(
        rekey_fn=_rekey,
        validate_fn=_validate,
        pointer_debt_fn=_pointer_debt,
        reprovision_fn=_reprovision,
        detect_probe_fn=_detect_probe,
        validated_probe_fn=_validated_probe,
        applicable_fn=_locally_actionable,
    )


def _sentinel_freeze() -> Callable[[], None]:
    """Enter the RDR-159 writer freeze, snapshotting whatever sentinel state
    exists so an already-migrated store's ``migrated`` fact survives."""
    from nexus.migration import state as mig_state  # noqa: PLC0415 — deferred, keeps rung import light

    prior = mig_state.read_state()
    mig_state.write_state(mig_state.MigrationState(
        phase=mig_state.MIGRATING,
        started_at=mig_state._utc_now_iso(),  # noqa: SLF001 — module-internal helper, same package family
        collections_total=0,
        collections_done=0,
    ))

    def _restore() -> None:
        if prior is not None:
            mig_state.write_state(prior)
        else:
            mig_state.clear_state()

    return _restore
