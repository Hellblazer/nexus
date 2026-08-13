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
3. **VALIDATE** — local mode: the two octet CHECKs (RDR-191: the unified
   ``nexus.chunks`` content check plus the ``catalog_document_chunks``
   pointer/manifest check) are validated via the ADMIN connection (table
   owner; VALIDATE scans all rows RLS-exempt — deliberately NOT a Liquibase
   boot changeset, which would crash-loop un-rekeyed stores, and NOT the svc
   role, which cannot VALIDATE).
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

from nexus.db.chash_tables import ConformanceProbe, ProbeState
from nexus.db.t2.rekey_client import RekeyJobLostError
from nexus.upgrade_ladder.protocol import (
    ConvergeOutcome,
    ConvergeResult,
    ProgressReporter,
    RungStatus,
)

_log = structlog.get_logger(__name__)

RUNG_CHASH_REKEY = "chash-rekey"

#: The octet CHECKs the rung VALIDATEs post-rekey — MUST mirror the LIVE
#: constraint names on the unified schema (RDR-191 vectors-004; pinned by
#: test against the changeset XML).
#:
#: DERIVATION (nexus-o8dil.15 — do not hand-edit this count by analogy):
#: RDR-180 (rdr180-001) started at FIVE octet CHECKs: three per-dim CONTENT
#: tables (chunks_384/768/1024), one POINTER/manifest table
#: (catalog_document_chunks), and one router table (chash_index). RDR-187
#: dropped chash_index's table and its CHECK (nexus-piwya.9), leaving FOUR.
#: RDR-191 (vectors-004-unify-chunks.xml) drops chunks_384/768/1024 and
#: replaces them with ONE unified `nexus.chunks` table carrying ONE octet
#: CHECK (`chunks_chash_octet_check`, three nullable typed embedding
#: columns, one shared `chash` column) — collapsing the three CONTENT
#: entries into one. The POINTER entry (catalog_document_chunks) is a
#: separate table, untouched by vectors-004, and survives unchanged.
#: So: 4 entries -> (3 CONTENT collapse to 1) + (1 POINTER unchanged) = 2.
OCTET_CHECKS: tuple[tuple[str, str], ...] = (
    ("nexus.chunks", "chunks_chash_octet_check"),
    ("nexus.catalog_document_chunks", "catalog_document_chunks_chash_octet_check"),
    # ("nexus.chash_index", ...) REMOVED — RDR-187/nexus-piwya.9 (.9 critique
    # Critical 1): the router table is dropped by the paired engine's
    # rdr187-2 changeset. Left in place, this rung would VALIDATE against a
    # missing relation on EVERY nx upgrade forever (RuntimeError from
    # run_admin_sql), _pointer_debt would silently degrade to unknowable,
    # and _validated_probe could never count to len(OCTET_CHECKS) again —
    # permanently un-converged.
    # ("nexus.chunks_384"/"_768"/"_1024", ...) REMOVED — RDR-191
    # vectors-004-unify-chunks.xml drops all three tables (and their three
    # per-dim CHECKs) CASCADE, in the same transaction that creates
    # `nexus.chunks` with the single unified `chunks_chash_octet_check`
    # above. Boxes that converged pre-unify stay converged: the marker is
    # "every entry in THIS tuple convalidated", and the tuple itself moved
    # with the schema, exactly as it did for the RDR-187 chash_index drop.
)


#: EXPLICIT MEMBERSHIP, never a positional slice of OCTET_CHECKS (nexus-
#: o8dil.15 CRITICAL fix): at 4 entries, `OCTET_CHECKS[:3]` happened to be
#: the three CONTENT tables and `[3:]` happened to be the one POINTER
#: table — but that was accidental positional alignment, not a contract.
#: When the tuple collapsed to 2 entries, `[:3]` would have silently
#: yielded BOTH (folding the pointer/manifest amnesty logic below into an
#: unconditional VALIDATE that can fail on legitimately-dangling manifest
#: rows) and `[3:]` would have silently yielded the EMPTY tuple (making
#: `_pointer_debt`'s loop iterate nothing, permanently). Neither slice
#: would have raised. Naming each check explicitly makes a table SWAPPED
#: between the two constants a `KeyError`/set-membership failure at
#: import/test time, not a silent product-behavior change.
#:
#: The unified `nexus.chunks` table. Its octet CHECK is the RDR-180/191
#: contract — every chunk's identity IS its 32-byte digest — and a clean
#: rekey always leaves it conformant, so this VALIDATEs unconditionally.
CONTENT_OCTET_CHECKS: tuple[tuple[str, str], ...] = tuple(
    (table, name) for table, name in OCTET_CHECKS if table == "nexus.chunks"
)

#: The POINTER table (the manifest — since RDR-187 dropped the chash_index
#: router, the only one left). A lived-in store can carry orphan pointers
#: whose content stopped existing long before the cutover (production
#: 2026-07-20: 292,230 chash_index — died with the table — + 426
#: catalog_document_chunks, none with a chash_alias entry; the manifest's
#: are nexus-uu4ue's remaining scope). VALIDATE is table-grain, so it
#: cannot succeed while they exist — that is arithmetic, not judgement — and failing
#: the whole upgrade over pre-existing debt would strand every such install.
POINTER_OCTET_CHECKS: tuple[tuple[str, str], ...] = tuple(
    (table, name)
    for table, name in OCTET_CHECKS
    if table == "nexus.catalog_document_chunks"
)

# Non-vacuity guard on the module's own partition (nexus-o8dil.15 acceptance:
# "the test FAILS if the arms are swapped or if one is empty"). This is the
# import-time half of that guard — a NEW table added to OCTET_CHECKS without
# being routed into either CONTENT_ or POINTER_OCTET_CHECKS (or a table
# routed into BOTH by a typo'd predicate) fails LOUD here rather than
# silently shrinking whichever arm lost it. The test-level half lives in
# tests/upgrade/test_chash_rekey_rung.py::TestOctetCheckPartition.
if not CONTENT_OCTET_CHECKS or not POINTER_OCTET_CHECKS:
    raise RuntimeError(
        "chash_rekey OCTET_CHECKS partition is degenerate: "
        f"CONTENT={CONTENT_OCTET_CHECKS!r} POINTER={POINTER_OCTET_CHECKS!r} "
        "— every entry in OCTET_CHECKS must route into exactly one arm"
    )
if set(CONTENT_OCTET_CHECKS) & set(POINTER_OCTET_CHECKS):
    raise RuntimeError(
        "chash_rekey OCTET_CHECKS partition overlaps between CONTENT and "
        "POINTER — a table cannot be both"
    )
if len(CONTENT_OCTET_CHECKS) + len(POINTER_OCTET_CHECKS) != len(OCTET_CHECKS):
    raise RuntimeError(
        "chash_rekey OCTET_CHECKS partition drops entries: "
        f"{len(CONTENT_OCTET_CHECKS)} + {len(POINTER_OCTET_CHECKS)} != "
        f"{len(OCTET_CHECKS)}"
    )


def probe_from_diagnostic_output(
    statements: tuple[str, ...],
    outputs: tuple[str, ...] | list[str],
) -> ConformanceProbe:
    """Turn raw ``run_diagnostic_sql`` output into a :class:`ConformanceProbe`.

    NON-VACUITY IS THE WHOLE POINT (nexus-hdumg). ``sum(int(c) for c in
    outputs)`` is 0 for an EMPTY sequence — the arithmetic identity that
    silently converts "I scanned no tables" into "no non-conformant rows".
    RDR-187 already narrowed ``POISON_CHASH_TABLES`` from five entries to
    four; a further narrowing (or a partial psql result) must fail the
    probe, never pass it. Likewise an empty output line is psql's rendering
    of a NULL aggregate — a stale view generation carrying no row for the
    filtered table — which is "unknown", never a count (the
    ``debt_chash_conformance_statements`` docstring says so in prose; this
    enforces it).
    """
    if not statements:
        return ConformanceProbe.failed(
            "the conformance probe issued NO statements — it scanned nothing, "
            "which is not a clean store"
        )
    if len(outputs) != len(statements):
        return ConformanceProbe.failed(
            f"the conformance probe returned {len(outputs)} result(s) for "
            f"{len(statements)} statement(s) — a partial scan is not a clean store"
        )
    blank = [s for s, o in zip(statements, outputs) if not o.strip()]
    if blank:
        return ConformanceProbe.failed(
            "the conformance probe returned a NULL aggregate (psql empty line) "
            f"for {len(blank)} statement(s) — the counted relation carries no "
            f"row for that table (stale diag view generation). First: {blank[0]}"
        )
    try:
        return ConformanceProbe.measured(sum(int(o) for o in outputs))
    except ValueError as exc:
        return ConformanceProbe.failed(f"non-numeric conformance output: {exc}")


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
    - ``detect_probe_fn() -> ConformanceProbe`` — READ-ONLY count of
      width-non-conformant poison rows (the diag path) as a TRI-STATE
      (nexus-hdumg): MEASURED(count) / UNAVAILABLE(reason — no diag path on
      this box) / FAILED(reason — the diagnostic was attempted and could
      not execute). It used to return ``int | None``, and ``None`` fed a
      ``verify()`` that fell back to the engine's own rekey envelope — so a
      diagnostic that could not run was indistinguishable from a clean
      store and the rung reported "converged and verified". Absence of a
      non-conformance signal is NOT a conformance signal.
    - ``validated_probe_fn() -> bool | None`` — READ-ONLY: are all of
      ``OCTET_CHECKS`` convalidated (RDR-191: two entries — the unified
      ``nexus.chunks`` content check and the manifest pointer check)? This
      is the DATA-SIDE completion marker
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
        detect_probe_fn: Callable[[], ConformanceProbe] | None = None,
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
        self._detect_probe_fn = (
            detect_probe_fn
            if detect_probe_fn is not None
            else (lambda: ConformanceProbe.unavailable("no conformance probe wired"))
        )
        self._validated_probe_fn = validated_probe_fn if validated_probe_fn is not None else (lambda: None)
        self._applicable_fn = applicable_fn if applicable_fn is not None else (lambda: True)
        if orphan_policy not in ("drop", "synthesize"):
            raise ValueError(
                f"orphan_policy must be 'drop' or 'synthesize', got {orphan_policy!r}"
            )
        self._orphan_policy = orphan_policy
        self._last_counts: dict[str, Any] | None = None
        #: Populated by a REFUSING :meth:`verify` so the runner can put the
        #: reason in front of the operator (nexus-hdumg). Cleared on success.
        self._verify_detail: str = ""

    # ── Rung protocol ────────────────────────────────────────────────────────

    def detect(self) -> RungStatus:
        """READ-ONLY. The RUNNER consults the completion ledger, but the
        read-only surfaces (``nx doctor``'s pending sweep, ``nx upgrade
        --dry-run``, ``pending_data_rung_callout``) call this RAW — so
        convergence must be visible from the DATA (nexus-p78a0): all of
        ``OCTET_CHECKS`` convalidated is the unforgeable completion marker
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
        if probe.unmeasured:
            return RungStatus(applicable=True, converged=False, pending_detail=(
                f"conformance {probe.state.value} ({probe.reason}) — the "
                "idempotent rekey converges the data, but this rung cannot "
                "record completion until the diagnostic can be measured"
            ))
        if probe.count == 0:
            return RungStatus(applicable=True, converged=False, pending_detail=(
                "no width-non-conformant poison rows counted — rekey run "
                "needed once to build the alias map and validate the checks"
            ))
        return RungStatus(applicable=True, converged=False, pending_detail=(
            f"{probe.count} width-non-conformant poison row(s) pending rekey"
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
                if outstanding and debt_before is None:
                    # nexus-hdumg sibling sweep, same class as the verify fix:
                    # the amnesty's entire justification is "this debt
                    # PRE-EXISTED". With no BEFORE measurement that claim has
                    # no evidence — and note the growth check above is itself
                    # skipped when debt_before is None, so orphans this rekey
                    # CREATED would be waved through as pre-existing. Absent
                    # evidence must not grant an exemption: validate every
                    # check and let VALIDATE fail loud (the next run, with a working
                    # probe, grants the amnesty properly).
                    _log.warning(
                        "chash_rekey_debt_amnesty_refused_no_baseline",
                        rung=self.name,
                        outstanding=outstanding,
                        note="pre-rekey pointer-debt baseline was unmeasurable; "
                             "refusing to grandfather debt on absent evidence",
                    )
                    outstanding = {}
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
        """READ-ONLY: the diag probe must MEASURE zero. Nothing else passes.

        nexus-hdumg — the vacuous-verification fix. This used to read::

            probe = self._detect_probe_fn()          # int | None
            if probe is not None:
                return probe == 0
            counts = self._last_counts               # the ENGINE's own echo
            return counts is not None and int(counts.get("residual_mismatched", -1)) == 0

        so a diagnostic that could not execute (``None``) fell through to
        the rekey endpoint's self-reported envelope, and the rung reported
        "converged and verified" off a conformance query that never ran
        (observed in the engine-service-v0.1.69 deploy rehearsal, where
        ``nexus.diag_chash_conformance`` was absent). The envelope is
        evidence that the WORK ran — it is not an independent measurement
        of the resulting store, and RDR-182's own contract is that an
        unavailable diagnostic degrades to an explicit unavailable note,
        never a silent all-clean.

        The refusal is deliberately the ladder's EXISTING failure vocabulary
        rather than a new state: a False here is ``RungOutcome.VERIFY_FAILED``
        — nothing recorded, the derived position stays put (the RDR-142
        guard), the walk stops, and ``nx upgrade`` exits non-zero naming the
        reason. Managed installs never reach here at all
        (``applicable_fn`` skips them).

        THE TWO REMEDIES ARE NOT EQUALLY REACHABLE — do not state otherwise
        (nexus-ueshf; an earlier draft of this docstring claimed both "already
        run on this same upgrade path", which is false for the role):

        - VIEW (``reprovision_diag_view_best_effort``): runs inside THIS
          rung's own ``converge``, so it is automatic on the pending path —
          the path that produces a first-run stop. It does NOT run on the
          crash-resume path, where ``detect`` reports converged and the
          runner skips ``converge`` entirely.
        - ROLE (``backfill_diag_role_best_effort``): reachable ONLY through
          the engine-convergence finisher's poison-probe leg
          (``upgrade_finish`` ~L1159), behind TWO gates. (1) That finisher
          returns early when the engine is already converged (its
          ``status.converged and integrity.ok`` branch) and never reaches the
          probe — so on a steady-state box it never runs, on ANY caller.
          (2) From the precondition stage it additionally needs
          ``allow_engine_install`` (``preconditions.py`` L377), which is
          ``not auto_mode and not skip_t3`` — i.e. it is OFF under
          ``nx upgrade --auto``, the hook-driven form. A box with a missing
          diag ROLE therefore gets NO automatic role remedy, which is why the
          UNAVAILABLE message below names ``nx init --service`` as the
          escalation and says outright that re-running ``nx upgrade`` will not
          create the role. (Symbol names for the finisher are deliberately not
          spelled here: the RDR-185 P3 engine-trigger census in
          ``tests/upgrade/test_gap4_two_mechanisms.py`` is a text scan that
          cannot tell a prose mention from a third converge trigger, and this
          rung must not be allowlisted as a caller — it is not one.)

        The rekey is idempotent, so once the diagnostic is restored the next
        run converges — the stop costs a re-run, never the store.
        """
        probe = self._detect_probe_fn()
        if probe.conformant:
            self._verify_detail = ""
            return True
        if probe.state is ProbeState.MEASURED:
            self._verify_detail = (
                f"{probe.count} width-non-conformant chash row(s) remain after "
                "the rekey — completion NOT recorded"
            )
            return False
        # The escalation differs by arm, and leading with the ineffective
        # retry is a real UX defect for the worst-case user (nexus-ueshf):
        # UNAVAILABLE means the diag ROLE is missing, and `nx upgrade` alone
        # will NOT create it — the automatic role backfill is gated behind an
        # engine that is BOTH applicable-and-unconverged AND (from the
        # precondition stage) `allow_engine_install`, which `--auto` turns
        # off. Name the remedy that actually works, FIRST.
        if probe.state is ProbeState.UNAVAILABLE:
            remedy = (
                "Run `nx init --service` — it provisions the nexus_diag role "
                "(and the nexus.diag_chash_conformance view) directly. Do NOT "
                "just re-run `nx upgrade`: the automatic role backfill is "
                "gated behind engine convergence and is disabled entirely "
                "under `--auto`, so on a converged box it will never run and "
                "the ladder will stop here again."
            )
        else:
            remedy = (
                "The diagnostic path exists but could not execute. Check the "
                "local service PG is up and that nexus_diag can read either "
                "nexus.diag_chash_conformance or the chunk tables, then "
                "re-run `nx upgrade`; `nx init --service` re-provisions both "
                "the role and the view if the grants have drifted."
            )
        self._verify_detail = (
            f"chash conformance could not be verified: the diagnostic is "
            f"{probe.state.value} ({probe.reason}). This is NOT a clean-store "
            "verdict — the rekey may well have succeeded, but nothing measured "
            f"it, so no completion is recorded. {remedy} The rekey is "
            "idempotent, so a re-run after the fix converges."
        )
        _log.warning(
            "chash_rekey_verify_unmeasurable",
            rung=self.name,
            probe_state=probe.state.value,
            reason=probe.reason,
        )
        return False

    def verify_detail(self) -> str:
        """Operator-facing reason the last :meth:`verify` refused (``""`` when
        it passed). OPTIONAL rung surface the runner consults so an
        unmeasurable store's reason reaches the CLI instead of dying in a log
        line (RDR-182: "an explicit unavailable note")."""
        return self._verify_detail


def default_chash_rekey_rung() -> "ChashRekeyRung":
    """Production wiring: engine rekey via ``HttpRekeyClient`` (the P0e
    surviving rekey surface, ``nexus.db.t2.rekey_client``); local-mode
    VALIDATE + diag probe + view re-provision through the admin/diag
    connections; managed mode degrades each to its honest None/no-op."""

    def _rekey(orphan_policy: str) -> dict[str, Any]:
        from nexus.db.t2.rekey_client import HttpRekeyClient  # noqa: PLC0415 — deferred import cost

        with HttpRekeyClient() as store:
            return store.rekey(orphan_policy)

    def _detect_probe() -> ConformanceProbe:
        """The READ-ONLY conformance measurement, as a tri-state.

        nexus-hdumg: this used to be a bare ``try/except -> None`` around
        both legs, which collapsed "no diag role here", "the view is
        absent", "psql exited 127", and "the lint refused the statement"
        into one indistinguishable ``None`` that ``verify()`` then read as
        good enough. The three arms below are the same tri-state
        ``health._check_migration_state`` has carried since P2.1 — imported
        here rather than reinvented.
        """
        from nexus.db.chash_tables import (  # noqa: PLC0415 — deferred
            chash_conformance_statements,
            legacy_chash_conformance_statements,
        )
        from nexus.db.diag_connection import resolve_diag_credentials, run_diagnostic_sql  # noqa: PLC0415 — deferred
        from nexus.remediation.sql_lint import DiagnosticSqlViolation  # noqa: PLC0415 — deferred

        try:
            creds = resolve_diag_credentials(None)
        except Exception as exc:  # noqa: BLE001 — an unreadable credential file is FAILED, never clean
            return ConformanceProbe.failed(f"diag credential resolution raised: {exc}")
        if creds is None:
            return ConformanceProbe.unavailable(
                "no nexus_diag credentials (pre-P2.1 install or no local "
                "service PG) — `nx init --service` backfills the diagnostic role"
            )

        def _run(statements: tuple[str, ...]) -> ConformanceProbe:
            """One leg. A LINT refusal PROPAGATES (it is a product defect, not
            engine-generation skew — health.py re-raises for the same reason,
            and the rung's old bare ``except Exception`` retried the legacy
            statements and mislabeled it as a stale-view fallback). Every
            other failure becomes a FAILED probe the caller may retry on the
            other leg."""
            try:
                return probe_from_diagnostic_output(
                    statements, run_diagnostic_sql(statements, creds)
                )
            except DiagnosticSqlViolation:
                raise
            except Exception as exc:  # noqa: BLE001 — a probe that could not run is FAILED, never clean
                return ConformanceProbe.failed(f"diagnostic statement failed: {exc}")

        try:
            view_probe = _run(chash_conformance_statements())
        except DiagnosticSqlViolation as exc:
            return ConformanceProbe.failed(f"diagnostic lint refused the statement: {exc}")
        if view_probe.state is ProbeState.MEASURED:
            return view_probe

        # LOUD, like health.py's chash_probe_view_fallback_legacy. The view is
        # legitimately absent between an engine changeset that DROPs it
        # (rdr180-001 / rdr187-001) and the next client-side re-provision, and
        # on a fresh install before Liquibase has created the chash tables —
        # so the underlying `diag_sql_failed` WARNING is EXPECTED there and
        # must be readable AS a fallback rather than as the unexplained error
        # it looks like on its own (nexus-hdumg Q1). A view that RAN but
        # produced a NULL aggregate (a deployed generation with no row for a
        # filtered table_name) takes this same path — same stale-view cause,
        # same working fallback.
        _log.warning(
            "chash_rekey_probe_view_fallback_legacy",
            rung=RUNG_CHASH_REKEY,
            error=view_probe.reason[:200],
            note="view-path conformance probe produced no measurement — "
                 "falling back to legacy direct-table counts (view absent on "
                 "pre-A6 engines, between a DROP-ing changeset and the next "
                 "client re-provision, a view/owner grant gap, or a stale "
                 "view generation). Any preceding diag_sql_failed is this "
                 "expected fallback, not a store defect.",
        )
        try:
            legacy_probe = _run(legacy_chash_conformance_statements())
        except DiagnosticSqlViolation as exc:
            return ConformanceProbe.failed(f"diagnostic lint refused the statement: {exc}")
        if legacy_probe.state is ProbeState.MEASURED:
            return legacy_probe
        return ConformanceProbe.failed(
            f"view path: {view_probe.reason[:120]} | legacy direct-table "
            f"fallback: {legacy_probe.reason[:120]}"
        )

    def _pointer_debt() -> dict[str, int] | None:
        """Per-table non-conformant counts for the POINTER table(s)
        (nexus-noa8d; RDR-191: one table, ``catalog_document_chunks`` — the
        manifest — since the RDR-187 router drop). Read-only, via the same
        diag choke point as the poison probe. ``None`` = unknowable (managed
        mode / no diag creds), which leaves the pre-existing all-of-
        ``OCTET_CHECKS`` VALIDATE behaviour untouched rather than guessing a
        store is clean."""
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
        except Exception:  # noqa: BLE001 — unknowable ≠ failure; all-of-OCTET_CHECKS behaviour stands
            return None

    def _validate(checks: tuple[tuple[str, str], ...] = OCTET_CHECKS) -> bool | None:
        from nexus.db.admin_sql import run_admin_sql  # noqa: PLC0415 — deferred

        return run_admin_sql(validate_statements(checks))

    def _validated_probe() -> bool | None:
        # The data-side completion marker (see the class docstring): every
        # octet CHECK in OCTET_CHECKS convalidated — TWO of them since
        # RDR-191 (vectors-004-unify-chunks.xml) collapsed the three per-dim
        # CONTENT checks into the one unified `nexus.chunks` check, on top
        # of the RDR-187 chash_index drop that took it from five to four
        # (see OCTET_CHECKS's own derivation comment). The compare below has
        # always been len(OCTET_CHECKS), so this function needed no change
        # beyond the constant's own definition moving. pg_constraint is a
        # metadata target under the diag lint, so this rides the same
        # read-only choke point as the poison probe.
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
        except Exception as exc:  # noqa: BLE001 — unreadable creds = not actionable here
            # nexus-hdumg sibling sweep. A resolved None is the honest
            # managed-mode signal; a RAISE is "we could not tell", and
            # returning False for it makes the rung report
            # SKIPPED_NOT_APPLICABLE — which the run report counts toward
            # `converged`. Kept as False (a hard failure here would brick
            # every box with a transiently unreadable credential file, and
            # managed installs legitimately have none), but no longer
            # SILENT: an operator reading a converged ladder on a local box
            # needs this line to explain why the rekey rung never ran.
            _log.warning(
                "chash_rekey_actionability_unknown",
                rung=RUNG_CHASH_REKEY,
                error=str(exc)[:200],
                note="admin-credential resolution raised — the rung will "
                     "detect-and-SKIP as not-applicable. On a local install "
                     "that is a false skip, not a managed-mode skip.",
            )
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
    import nexus.migration.state as mig_state  # noqa: PLC0415 — deferred, keeps rung import light

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
