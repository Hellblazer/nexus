# SPDX-License-Identifier: AGPL-3.0-or-later
"""Retroactive target-name collision audit (nexus-p9vqa).

The nexus-5b9v0 pre-flight guard
(:func:`nexus.migration.driver._assert_no_target_name_collisions`) BLOCKS a
fresh guided-upgrade run when two or more distinct, data-bearing source
collections would resolve to the same final pgvector target name. It does
nothing for a store that already migrated through a run that predates the
guard: the overlapping-chash variant of that bug class is a SILENT merge —
no count mismatch, no error — so a fleet box that hit it holds a corrupted
pgvector target right now with nothing surfacing it.

This module re-derives what that historical run would have done and probes
what the target actually holds:

1. Re-run the SAME classification the migration ran
   (:func:`~nexus.migration.detection.classify_collections`) against the
   retained Chroma source (copy-not-move — RDR-159's rollback design keeps
   the source intact through the deprecation window; this audit is only
   possible while it survives, so this module dies with RDR-155 P4b).
2. Rebuild the historical target-name map and collision groups with the
   guard's OWN extracted logic
   (:func:`~nexus.migration.driver.build_cross_model_target_names` /
   :func:`~nexus.migration.driver.group_colliding_targets`) — never a
   reimplementation that could drift.
3. For each would-have-collided target, probe the live pgvector collection:
   per colliding source, enumerate its Chroma ids (the ETL is id-stable —
   ``vector_etl`` probes the target with raw source ids, so the audit does
   too) and ask the service which are present
   (:meth:`HttpVectorClient.existing_ids`).
4. Report a verdict per target for operator remediation.

Verdicts (exact, count-based — no fuzzy thresholds):

- ``merged`` — two or more colliding sources are FULLY present in the
  target: the silent-merge signature. Remediation: remove/re-index the
  stale duplicate source, drop the merged target, re-migrate cleanly (the
  5b9v0 guard now blocks the collision up front).
- ``single-source`` — exactly one source fully present, the rest not: the
  collision never merged (the loud ``vector_etl_count_mismatch`` variant
  was likely hit and the run abandoned/retried) but the OTHER sources'
  data is absent from the target — an unmigrated remainder, not a clean
  state.
- ``never-materialized`` — the target collection does not exist: the
  colliding run never wrote (or was rolled back). Nothing merged; the
  collision is latent and the guard will block the next attempt.
- ``partial`` — anything else (some rows of several sources present):
  operator investigates with the per-source numbers in hand.
- ``indeterminate`` — the target demonstrably holds rows but not a single
  probed source id resolved. ``existing_ids`` swallows transport failures
  into the empty set (its documented contract), which is indistinguishable
  from "genuinely absent" per call — mirroring ``vector_etl``'s
  never-blind-fill check (nexus-r0esi), a whole-collection zero-resolution
  against a non-empty target is reported as an anomaly, never as evidence
  of anything. Same collection-level-only granularity caveat as that
  check: a probe degrading on one page mid-collection is not caught.

The audit is strictly READ-ONLY on both stores: Chroma legs are opened with
the same read clients the migration's detection phase uses, and the only
service calls are ``collection_exists`` / ``count`` / ``existing_ids``.

HISTORICAL-CLASSIFICATION DRIFT (nexus-772h2, substantive-critic Critical on
the first cut of this module): classification is ``voyage_key_present``-
dependent — with a key present a voyage-NAMED collection classifies
"supported" and its measured-dim probe (the nb7hr mislabel detector) never
runs, so it is never remap-classified and never enters a collision group.
A store that silently merged in LOCAL mode (no key at migration time) and is
audited TODAY, after a Voyage key was added, would classify clean under
today's environment — a false "clean" from the exact tool meant to catch the
merge. The audit therefore classifies under BOTH worlds
(``voyage_key_present`` False and True) by default and probes the UNION of
their collision groups. This is sound because verdicts are evidence-based
(what the live target actually holds): probing an extra candidate target can
never fabricate a ``merged`` verdict, but skipping a world can fabricate a
clean. Each finding carries the world(s) whose historical map produced it;
pass ``voyage_key_present`` explicitly (CLI ``--assume-voyage-key`` /
``--assume-no-voyage-key``) to audit a single known history.

Two more honesty caveats: (1) the retained Chroma source may have gained
collections AFTER the historical run — a collision group derived today can
therefore include a pairing no historical run ever attempted; the probe
verdict, not the grouping, is the evidence (such a target typically reads
``never-materialized`` or ``single-source``). (2) ``existing_ids`` swallows
per-page transport failures into the empty set; before any verdict that
hinges on absence, the probe RE-CHECKS the missing ids once (a transient
one-page degradation heals on retry), and a target with rows but ZERO
resolutions across every source is reported ``indeterminate``, never clean.
A degradation that persists across both passes on a subset of pages while
``count()`` succeeds remains undetectable at this layer — same accepted
granularity as ``vector_etl``'s never-blind-fill check (nexus-r0esi).
"""
from __future__ import annotations

from dataclasses import dataclass, replace
from pathlib import Path
from typing import Any, Callable

import structlog

from nexus.db.chroma_quotas import QUOTAS
from nexus.migration.detection import (
    CollectionClassification,
    classify_collections,
    close_read_client,
)
from nexus.migration.driver import (
    build_cross_model_target_names,
    group_colliding_targets,
)

_log = structlog.get_logger(__name__)

#: Verdict constants (module-level so the CLI and tests never string-drift).
MERGED = "merged"
SINGLE_SOURCE = "single-source"
NEVER_MATERIALIZED = "never-materialized"
PARTIAL = "partial"
INDETERMINATE = "indeterminate"

#: World tags: which historical ``voyage_key_present`` assumption produced a
#: collision group (see module docstring, nexus-772h2).
WORLD_NO_VOYAGE_KEY = "no-voyage-key"
WORLD_VOYAGE_KEY = "voyage-key"


@dataclass(frozen=True)
class SourceProbe:
    """One colliding source collection's presence in the shared target."""

    classification: CollectionClassification
    #: ids enumerated from the Chroma source (0 when the target never
    #: materialized and probing was skipped).
    probed_ids: int
    #: how many of those ids the target reports present.
    present_in_target: int

    @property
    def missing_from_target(self) -> int:
        return self.probed_ids - self.present_in_target

    @property
    def fully_present(self) -> bool:
        return self.probed_ids > 0 and self.present_in_target == self.probed_ids


@dataclass(frozen=True)
class TargetFinding:
    """Audit result for one would-have-collided target collection."""

    target: str
    target_exists: bool
    target_count: int
    #: size of the union of all colliding sources' id sets — what a full
    #: silent merge of every source would have written.
    union_source_ids: int
    sources: tuple[SourceProbe, ...]
    verdict: str
    detail: str
    #: historical ``voyage_key_present`` world(s) whose target map produced
    #: this collision group (nexus-772h2); empty only in direct
    #: :func:`audit_collision_groups` calls that pass no world map.
    worlds: tuple[str, ...] = ()


@dataclass(frozen=True)
class CollisionAuditReport:
    """The full retroactive audit outcome."""

    findings: tuple[TargetFinding, ...]
    #: which legs the caller asked to audit ("both" | "local" | "cloud").
    requested_legs: str = "both"
    #: which legs actually opened and were classified — a "clean" verdict
    #: only speaks for these (nexus-ovbmb: partial scope must be LOUD).
    audited_legs: tuple[str, ...] = ()

    @property
    def partial_scope(self) -> bool:
        """True when the audit did not cover both source legs."""
        return set(self.audited_legs) != {"local", "cloud"}

    @property
    def clean(self) -> bool:
        """No target-name collision groups exist for this store at all."""
        return not self.findings

    @property
    def merged_targets(self) -> tuple[TargetFinding, ...]:
        return tuple(f for f in self.findings if f.verdict == MERGED)

    @property
    def indeterminate_targets(self) -> tuple[TargetFinding, ...]:
        return tuple(f for f in self.findings if f.verdict == INDETERMINATE)


def _iter_source_id_pages(read_client: Any, collection: str) -> Any:
    """Yield the source collection's ids in quota-sized pages, ids only.

    Deliberately NOT :func:`~nexus.migration.chroma_read.iter_collection_chunks`
    — that fetches documents+metadatas for the ETL's re-embed; an id
    membership audit needs neither, and ``include=[]`` keeps the ChromaCloud
    leg's read cost minimal. Same ``QUOTAS.MAX_QUERY_RESULTS`` paging.
    """
    page = QUOTAS.MAX_QUERY_RESULTS
    col = read_client.get_collection(collection)
    offset = 0
    while True:
        batch = col.get(include=[], limit=page, offset=offset)
        ids = batch.get("ids") or []
        if not ids:
            return
        yield list(ids)
        if len(ids) < page:  # short page == final page; skip the empty round trip
            return
        offset += len(ids)


def _probe_source(
    read_client: Any, vector_client: Any, source: str, target: str
) -> tuple[int, int, set[str]]:
    """Return ``(probed, present, source_id_set)`` for one source vs target.

    Absence is verdict-bearing (it can flip ``merged`` to ``single-source``/
    ``partial``), so ids that read absent on the first pass are RE-CHECKED
    once before they count as missing (substantive-critic Significant on the
    first cut). A transient one-page degradation heals here; only ids absent
    on BOTH passes count against the source.

    nexus-ou4tb: that healing used to rest on ``existing_ids`` silently
    swallowing per-page transport failures into the empty set. It no longer
    does — a silent empty there meant "all of these are MISSING" to callers
    like ``nx catalog verify``, which reported every expected document as a
    ghost whenever the service was degraded. The tolerance this function
    genuinely wants is now EXPLICIT and local: the first pass catches
    per-page failures itself, counts them, and lets the existing re-check
    heal them. The second pass does NOT catch — an id that could not be
    probed twice is a could-not-tell, and returning it as "present" or
    "missing" would both be guesses.

    The re-check is guarded too, but for a different reason than the first
    pass. If BOTH passes fail, the probe resolves nothing — and the audit
    already has the right answer for that: a non-empty target with zero
    resolved source ids reads as INDETERMINATE, the never-blind-fill mirror
    (nexus-r0esi), "an anomaly, never evidence of anything". Raising there
    would have destroyed a designed safety verdict and replaced an honest
    could-not-tell with a crash.

    So this is the one place in the vector-client surface where degrading is
    legitimate, and it satisfies the bead's clause exactly: the return is
    distinguishable from empty (INDETERMINATE, not "absent"), and it is
    counted rather than silent via ``event=collision_audit_page_degraded``.
    """
    probed = 0
    degraded_pages = 0
    all_ids: set[str] = set()
    present_ids: set[str] = set()
    for ids in _iter_source_id_pages(read_client, source):
        probed += len(ids)
        all_ids.update(ids)
        try:
            present_ids.update(vector_client.existing_ids(target, ids))
        except Exception as exc:  # noqa: BLE001 — transient per-page failure; the re-check below is the heal
            degraded_pages += 1
            _log.warning(
                "collision_audit_page_degraded",
                source=source, target=target, page_ids=len(ids),
                degraded_pages=degraded_pages, error=str(exc),
            )
    missing = sorted(all_ids - present_ids)
    if missing:
        try:
            present_ids.update(vector_client.existing_ids(target, missing))
        except Exception as exc:  # noqa: BLE001 — heal failed too; INDETERMINATE is the designed answer
            degraded_pages += 1
            _log.warning(
                "collision_audit_page_degraded",
                source=source, target=target, page_ids=len(missing),
                degraded_pages=degraded_pages, phase="recheck", error=str(exc),
            )
    if degraded_pages:
        _log.warning(
            "collision_audit_probe_degraded",
            source=source, target=target, degraded_pages=degraded_pages,
        )
    return probed, len(present_ids & all_ids), all_ids


def audit_collision_groups(
    collisions: dict[str, list[CollectionClassification]],
    *,
    vector_client: Any,
    clients_by_leg: dict[str, Any],
    worlds_by_target: dict[str, tuple[str, ...]] | None = None,
    on_progress: Callable[[str], None] | None = None,
) -> CollisionAuditReport:
    """Probe each collision group's live target — the pure audit core.

    ``clients_by_leg`` maps ``"local"``/``"cloud"`` to an OPEN Chroma read
    client; a colliding source whose leg has no client is a hard error (the
    caller opened the legs from the same detection that produced the
    classifications, so a missing leg means the store changed underneath
    the audit — fail loud, never skip). ``worlds_by_target`` annotates each
    finding with the historical assumption(s) that produced its group
    (nexus-772h2).
    """
    world_map = worlds_by_target or {}
    findings: list[TargetFinding] = []
    for target in sorted(collisions):
        sources = collisions[target]
        worlds = world_map.get(target, ())
        if on_progress is not None:
            on_progress(target)

        exists = bool(vector_client.collection_exists(target))
        if not exists:
            findings.append(
                TargetFinding(
                    target=target,
                    target_exists=False,
                    target_count=0,
                    union_source_ids=0,
                    sources=tuple(
                        SourceProbe(c, probed_ids=0, present_in_target=0)
                        for c in sources
                    ),
                    verdict=NEVER_MATERIALIZED,
                    worlds=worlds,
                    detail=(
                        "target collection does not exist on the service — the "
                        "colliding run never wrote (or was rolled back); the "
                        "nexus-5b9v0 guard will block the next attempt until the "
                        "duplicate source is removed/re-indexed"
                    ),
                )
            )
            continue

        # count() propagates service errors (unlike existing_ids, which
        # swallows them) — an unreachable target fails the audit loudly here
        # rather than reading as "empty, nothing present" below.
        target_count = int(vector_client.count(target))

        probes: list[SourceProbe] = []
        union_ids: set[str] = set()
        for c in sources:
            read_client = clients_by_leg.get(c.leg)
            if read_client is None:
                raise RuntimeError(
                    f"collision audit: no open read client for leg {c.leg!r} "
                    f"(source {c.collection!r}) — the Chroma footprint changed "
                    "between classification and probing; re-run the audit"
                )
            probed, present, ids = _probe_source(
                read_client, vector_client, c.collection, target
            )
            union_ids.update(ids)
            probes.append(
                SourceProbe(c, probed_ids=probed, present_in_target=present)
            )

        fully = [p for p in probes if p.fully_present]
        total_present = sum(p.present_in_target for p in probes)

        if target_count > 0 and total_present == 0:
            verdict = INDETERMINATE
            detail = (
                f"target holds {target_count} row(s) but not one of the "
                f"{sum(p.probed_ids for p in probes)} probed source id(s) "
                "resolved — either the presence probe degraded (existing_ids "
                "swallows transport failures into the empty set) or the "
                "target was populated by something other than these sources; "
                "re-run, and if it persists inspect the target directly"
            )
        elif len(fully) >= 2:
            verdict = MERGED
            merged_names = ", ".join(
                repr(p.classification.collection) for p in fully
            )
            verdict_count = (
                f"target holds {target_count} row(s); the union of all "
                f"colliding sources' ids is {len(union_ids)}"
            )
            detail = (
                f"SILENT MERGE CONFIRMED: {merged_names} are all fully "
                f"present in {target!r} ({verdict_count}). Remediate: "
                "remove or re-index the stale/duplicate source collection(s), "
                "drop the merged target, and re-migrate — the nexus-5b9v0 "
                "guard now blocks this collision before any write"
            )
        elif len(fully) == 1:
            missing = [p for p in probes if not p.fully_present]
            missing_names = ", ".join(
                f"{p.classification.collection!r} "
                f"({p.missing_from_target}/{p.probed_ids} absent)"
                for p in missing
            )
            verdict = SINGLE_SOURCE
            detail = (
                f"only {fully[0].classification.collection!r} is fully present "
                f"in {target!r}; {missing_names} — the collision never merged, "
                "but that remainder is UNMIGRATED. Remediate the duplicate "
                "source, then re-run the migration for the remainder"
            )
        else:
            verdict = PARTIAL
            detail = (
                "the target exists but holds no rows — created and never "
                "written (or emptied); nothing merged, and every colliding "
                "source is unmigrated into it"
                if target_count == 0
                else (
                    "no colliding source is fully present in the target — "
                    "partial writes (an interrupted or retried run); use the "
                    "per-source present/missing counts to decide whether to "
                    "drop and re-migrate the target"
                )
            )

        findings.append(
            TargetFinding(
                target=target,
                target_exists=True,
                target_count=target_count,
                union_source_ids=len(union_ids),
                sources=tuple(probes),
                verdict=verdict,
                detail=detail,
                worlds=worlds,
            )
        )

    report = CollisionAuditReport(findings=tuple(findings))
    _log.info(
        "collision_audit_complete",
        targets_flagged=len(report.findings),
        merged=len(report.merged_targets),
        indeterminate=len(report.indeterminate_targets),
    )
    return report


def _open_audit_legs(
    local_path: str | Path | None, legs: str
) -> tuple[Any | None, Any | None]:
    """Open the requested Chroma source legs, wrapping open failures into
    actionable RuntimeErrors (nexus-ovbmb: the dogfood run hit a rejected
    ChromaCloud credential surfacing as a raw chromadb traceback).

    Absent-leg sentinels keep :func:`~nexus.migration.detection.open_read_legs`'s
    semantics (missing local store / unconfigured cloud leg -> ``None``); any
    OTHER failure — credentials present but rejected, a corrupt store — is a
    hard, clean error naming the leg and the ``--legs`` remedy. Zero opened
    legs is a hard error too: a box whose retained source was deleted must
    never read as "clean" (the audit needs the copy-not-move source).
    """
    from nexus.migration.chroma_read import (  # noqa: PLC0415 — circular-dep avoidance (nexus.migration.chroma_read)
        open_cloud_read_client,
        open_local_read_client,
    )

    if legs not in ("both", "local", "cloud"):
        raise RuntimeError(f"unknown legs selector {legs!r} (both|local|cloud)")

    local: Any | None = None
    cloud: Any | None = None
    if legs in ("both", "local"):
        try:
            local = open_local_read_client(local_path)
        except FileNotFoundError:
            local = None  # absent leg, same sentinel as open_read_legs
        except Exception as exc:
            raise RuntimeError(
                f"local Chroma read leg failed to open: {exc} — the retained "
                "local source exists but is unreadable; the audit cannot "
                "proceed against it. To audit only the cloud leg (LOUDLY "
                "partial): nx migration-audit --legs cloud"
            ) from exc
    if legs in ("both", "cloud"):
        try:
            cloud = open_cloud_read_client()
        except RuntimeError:
            cloud = None  # half-configured / unconfigured = absent
        except Exception as exc:
            # The local leg opened first — close it before raising, or the
            # handle leaks past chroma_read's single-opener discipline on
            # exactly the failure path this function exists to handle
            # (code-review High, nexus-ovbmb).
            close_read_client(local)
            raise RuntimeError(
                f"cloud Chroma read leg failed to open: {exc} — credentials "
                "are configured but were rejected (a retired/rotated "
                "ChromaCloud account?). If that leg's source is permanently "
                "gone, audit the surviving leg explicitly (LOUDLY partial): "
                "nx migration-audit --legs local. Note: a gone cloud leg also "
                "means it no longer exists as a rollback source, independent "
                "of the RDR-155 deprecation window."
            ) from exc
    if local is None and cloud is None:
        raise RuntimeError(
            f"no Chroma source leg found (requested: {legs}) — the audit "
            "reads the retained copy-not-move migration source; if it was "
            "deleted, the audit is impossible and NOTHING can be concluded "
            "(this is deliberately not reported as 'clean')"
        )
    return local, cloud


def audit_target_collisions(
    *,
    vector_client: Any,
    local_path: str | Path | None = None,
    voyage_key_present: bool | None = None,
    legs: str = "both",
    on_progress: Callable[[str], None] | None = None,
) -> CollisionAuditReport:
    """Classify the retained Chroma source, rebuild the historical
    target-name map, and probe every would-have-collided pgvector target.

    ``voyage_key_present=None`` (the default) audits BOTH historical worlds
    and probes the union of their collision groups — the drift-proof default
    (nexus-772h2, see module docstring). Pass ``True``/``False`` explicitly
    only when the migration's historical key state is known.

    Read-only end to end. The read legs stay open through probing (unlike
    :func:`~nexus.migration.driver.run_guided_upgrade`, which closes them
    before the ETL takes single-opener ownership — no ETL runs here) and
    are closed before returning.
    """
    if voyage_key_present is None:
        # World order matters for source dedupe below: the no-key world
        # classifies with measured_dim probed for every suspicious
        # collection (richer diagnostics), so it wins the first-seen slot.
        worlds: tuple[tuple[bool, str], ...] = (
            (False, WORLD_NO_VOYAGE_KEY),
            (True, WORLD_VOYAGE_KEY),
        )
    else:
        worlds = (
            (
                voyage_key_present,
                WORLD_VOYAGE_KEY if voyage_key_present else WORLD_NO_VOYAGE_KEY,
            ),
        )

    local, cloud = _open_audit_legs(local_path, legs)
    audited = tuple(
        name for name, client in (("local", local), ("cloud", cloud))
        if client is not None
    )
    try:
        # Union of every world's collision groups, sources deduped by
        # (collection, leg) — first-seen (no-key world) classification kept.
        union: dict[str, dict[tuple[str, str], CollectionClassification]] = {}
        worlds_by_target: dict[str, tuple[str, ...]] = {}
        classified_total = 0
        for key_present, tag in worlds:
            detection = classify_collections(
                local_client=local,
                cloud_client=cloud,
                voyage_key_present=key_present,
            )
            classified_total = len(detection.classifications)
            target_names = build_cross_model_target_names(
                detection, voyage_key_present=key_present
            )
            for target, sources in group_colliding_targets(
                detection.classifications, target_names
            ).items():
                bucket = union.setdefault(target, {})
                for c in sources:
                    bucket.setdefault((c.collection, c.leg), c)
                worlds_by_target[target] = (*worlds_by_target.get(target, ()), tag)

        # group_colliding_targets only ever yields len>=2 groups, so every
        # union bucket already holds >= 2 sources; the filter is a pure
        # safeguard against that invariant changing upstream.
        collisions = {
            target: list(bucket.values())
            for target, bucket in union.items()
            if len(bucket) > 1
        }
        if not collisions:
            _log.info(
                "collision_audit_clean",
                classified=classified_total,
                worlds=[tag for _, tag in worlds],
                legs=audited,
            )
            return CollisionAuditReport(
                findings=(), requested_legs=legs, audited_legs=audited
            )

        clients_by_leg: dict[str, Any] = {}
        if local is not None:
            clients_by_leg["local"] = local
        if cloud is not None:
            clients_by_leg["cloud"] = cloud

        probed = audit_collision_groups(
            collisions,
            vector_client=vector_client,
            clients_by_leg=clients_by_leg,
            worlds_by_target={
                t: w for t, w in worlds_by_target.items() if t in collisions
            },
            on_progress=on_progress,
        )
        return replace(probed, requested_legs=legs, audited_legs=audited)
    finally:
        for client in (local, cloud):
            close_read_client(client)
