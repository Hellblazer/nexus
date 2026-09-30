# SPDX-License-Identifier: AGPL-3.0-or-later
"""Shared helpers for CLI command modules.

nexus-8g79.10: ``default_db_path`` was promoted to ``nexus.config``
so non-CLI modules (mcp_infra, health, collection_health,
collection_audit, context, operators/aspect_sql, merge_candidates,
console/routes/health) can resolve the canonical T2 path without
importing up from this CLI helpers module. Re-exported here for
back-compat with CLI command modules that import from
``commands._helpers`` directly.

The re-export is a thin wrapper (not ``from … import``) so test
monkeypatches on ``nexus.config.default_db_path`` reach the live
binding via attribute access at call time. The ``from x import y``
form captures ``y`` at import time and silently bypasses the patch.
"""
from contextlib import contextmanager
from pathlib import Path
from typing import TYPE_CHECKING, Any, Iterator

from nexus import config as _config

if TYPE_CHECKING:
    import httpx

__all__ = [
    "T2_SHARED_CLIENT_CTX_KEY",
    "default_db_path",
    "emit_identity_drop_summary",
    "emit_retry_summary",
    "raise_identity_drop_exception",
    "raise_identity_drop_exception_for_file",
    "reset_identity_drop_collectors",
    "resolve_confirmed_write_failure_doc_ids",
    "t2_handle",
    "t2_shared_client_from_context",
]

#: nexus-m20mf P3 fold-in: the ``ctx.obj`` dict key a Click group callback
#: (``taxonomy_cmd.taxonomy``, ``index.index``, ``collection``'s reindex
#: command) stashes its one-per-invocation shared T2 ``httpx.Client``
#: under. A NEW key in that dict (``main``'s ``ctx.ensure_object(dict)`` /
#: ``ctx.obj["verbose"]`` in ``src/nexus/cli.py``), never a replacement of
#: it, so nothing that dict already carries is disturbed.
T2_SHARED_CLIENT_CTX_KEY = "_t2_shared_client"


def t2_shared_client_from_context() -> "httpx.Client | None":
    """CLI-LAYER helper: the current Click invocation's shared T2 client,
    or ``None`` outside a live Click context (a test calling a command
    function directly, or any non-CLI caller).

    Call this from a COMMAND FUNCTION's own body (the CLI layer) and pass
    the result on as an explicit ``client=`` argument -- never from inside
    a data-layer construction helper. ``T2Database``, ``_T2Database``,
    ``run_collection_postprocessing``, and ``_collections_without_topics``
    all take ``client`` as a plain, explicit parameter precisely so their
    behavior is a pure function of their arguments, not of whichever Click
    group happens to be active on the context stack (nexus-m20mf P3
    fold-in, critic finding 3 -- the prior per-module ``click.get_current_
    context()`` calls INSIDE those data-layer helpers produced exactly the
    inconsistency bug finding 2 describes: the identical function silently
    got a shared client under one Click group and none under another).
    """
    import click  # noqa: PLC0415 — deferred: keeps this module's own import-time cost light for CLI helpers that never touch Click state

    ctx = click.get_current_context(silent=True)
    if ctx is None or not isinstance(ctx.obj, dict):
        return None
    return ctx.obj.get(T2_SHARED_CLIENT_CTX_KEY)


def default_db_path() -> Path:
    """Delegate to :func:`nexus.config.default_db_path` at call time."""
    return _config.default_db_path()


@contextmanager
def t2_handle() -> Iterator[Any]:
    """Open a T2 handle for the user-facing CLI memory / plan commands.

    RDR-120 P6 follow-up (nexus-w6txl) routed these through a ``T2Client`` so
    multi-process operators (host CLI + Cowork-bridged MCP server +
    dev-container CLI) shared a single arbitrated SQLite writer rather than
    each opening its own connection and racing the WAL. The daemon that
    arbitrated them is retired (nexus-i711w Stage 2 sub-stage B); in service
    mode Postgres is the arbiter and the concern does not arise.

    Yields a service-backed ``T2Database``. Tests monkeypatch this helper to
    yield an in-process ``T2Database`` fixture; call sites use
    ``.memory.<method>`` on the yielded object either way.

    Operator/debug paths that MUST work when the daemon is offline
    (``nx upgrade``, ``nx doctor``, ``_session_end_launcher``, etc.)
    continue to construct ``T2Database(default_db_path())`` directly —
    each named in ``storage_boundary_lint.T2DATABASE_CONSTRUCTION_ALLOWLIST``
    — this helper is for the user-facing memory/plan surface only.

    Note: ``nx plan`` commands open T2 directly (named-allowlisted) and
    do NOT go through this helper — they must tolerate offline mode.
    """
    import click  # noqa: PLC0415 — deliberate function-local import: avoids click dependency at module import time

    # RDR-152 nexus-fjwxh: the Java service (PG) is the write arbiter, so the
    # SQLite single-writer T2 daemon is not in the picture — route directly to
    # a service-backed T2Database (its ``.memory`` is an HttpMemoryStore with
    # the same interface as ``T2Client.memory``). The daemon-client fail-loud
    # arm this helper carried died with the =sqlite opt-out (RDR-158 P3,
    # nexus-7bomn; its history — the vw7zk fail-loud ruling and the MCP/CLI
    # asymmetry it accepted — is in git at df0c9c25).
    import httpx  # noqa: PLC0415 — deliberate function-local import: branch-local, only used for the error taxonomy below

    from nexus.db.t2 import T2Database  # noqa: PLC0415 — deliberate function-local import: circular-dep avoidance, db package imports commands surfaces

    # T2Database routes to the HTTP service (PG arbiter), not a raw SQLite
    # writer, so the RDR-128 single-writer concern does not apply.
    #
    # nexus-00en9: two distinct service-down failure points, both of which
    # would otherwise reach Click as a raw traceback:
    #  (a) PRE-YIELD construction — HttpMemoryStore resolves its endpoint via
    #      resolve_service_config(), which raises RuntimeError fail-loud when
    #      no lease/env is discoverable (the common "service never started"
    #      case). Its message already names the operator fix.
    #  (b) POST-YIELD RPC — the endpoint resolved (a lease existed) but the
    #      service is unreachable/erroring when the actual RPC fires, raising
    #      an httpx transport or status error.
    try:
        db = T2Database(default_db_path(), run_migrations=False)  # boundary-allow: service mode routes to HTTP service, not a raw SQLite writer
    except RuntimeError as exc:
        raise click.ClickException(
            f"T2 storage service unavailable: {exc}"
        ) from exc
    try:
        yield db
    except (httpx.TransportError, httpx.HTTPStatusError) as exc:
        # Narrow to transport/status failures (unreachable/erroring service).
        # Decode/redirect/protocol httpx errors are service-side bugs that
        # should keep their traceback during go-live, not be aliased to a
        # reachability hint.
        raise click.ClickException(
            f"T2 storage service error: {exc}. "
            "Check the storage service: nx doctor"
        ) from exc
    finally:
        db.close()


# ── nexus-7f5qj: shared identity-drop / manifest-write-failure /
# completion-refusal collector wiring ───────────────────────────────────
#
# The reset-before / check-after / raise-if-any sequence around
# ``nexus.mcp_infra``'s three process-global post-store-hook failure
# collectors (``get_manifest_write_failures``, ``get_manifest_identity_
# drops``, ``get_complete_refusals``) was independently duplicated in
# ``index_repo_cmd`` (commands/index.py, nexus-tp8yk D2b's original) and
# ``dt.py``'s ``index_cmd`` (nexus-tp8yk D2). ``index_pdf_cmd`` / ``index_
# md_cmd`` needed the identical sequence a third and fourth time
# (nexus-7f5qj) — the point CLAUDE.md's "extract only when repetition is
# proven" is satisfied. Extracted here (not ``nexus.mcp_infra``, which
# owns the collectors themselves but has no ``click`` dependency, and not
# ``nexus.indexer_utils``, a non-CLI module with no existing ``click``
# import) since every caller is a Click command module and this file is
# already the established "shared helpers for CLI command modules" home
# (see the module docstring; every other command module reaches it via a
# deferred ``from nexus.commands._helpers import ...``).


def reset_identity_drop_collectors() -> None:
    """Zero the post-store-hook failure collectors for a fresh run.

    Call once at the start of an index run (or once per file, for a loop
    that wants per-file attribution — see ``index_pdf_cmd``'s ``--dir``
    batch branch) so the end-of-run/end-of-file check below reflects only
    this run's/file's problems, not leftover state from an earlier call
    in the same process.
    """
    from nexus.mcp_infra import (  # noqa: PLC0415 — deliberate function-local import: avoids a hard nexus.mcp_infra dependency at CLI startup
        reset_complete_refusals,
        reset_manifest_identity_drops,
        reset_manifest_partial_doc_skips,
        reset_manifest_write_failures,
        reset_reconciled_collections_count,
        reset_superseded_sweep_stats,
    )

    reset_manifest_write_failures()
    reset_manifest_identity_drops()
    reset_complete_refusals()
    # nexus-39upx hazard 4: same reset-before/check-after shape as the
    # three collectors above, added here (not a parallel function) so
    # every existing call site picks up sweep-skip visibility for free.
    reset_superseded_sweep_stats()
    # nexus-2t63u round 2: same shape again, for physical_collection
    # reconciliation visibility.
    reset_reconciled_collections_count()
    # nexus-gup3b: same shape again — a fresh run must not inherit a prior
    # run's per-doc continuation-flush dedup state, or a document re-indexed
    # in THIS run right after a prior run touched it would wrongly stay in
    # "already reported" (debug-only) mode and never get its own INFO line.
    reset_manifest_partial_doc_skips()


def emit_retry_summary() -> None:
    """Echo the end-of-run transient-error-backoff + rate-limit-brake
    summary (stderr). Silent when nothing retried and the shared brake was
    never tripped.

    nexus-cy9u7 S3 (extracted from ``index_repo_cmd``'s inline
    ``_emit_retry_summary``, which was the only wired-in caller pre-fix —
    `nx index pdf` and `nx index md` reset nothing and printed nothing,
    so a run throttled entirely on those paths gave the operator zero
    signal that it was slow because it was throttled, not because it was
    slow). Call :func:`nexus.retry.reset_retry_stats` once at the start of
    a run/batch, then this once at the end, so the summary reflects only
    that run's backoffs — same reset-before/emit-after shape as
    :func:`reset_identity_drop_collectors` / :func:`emit_identity_drop_summary`.

    The brake-trip check is SEPARATE from ``total_count`` because a
    manifest-write rate-limit trip can pause the shared brake without
    incrementing any of the three per-wrapper retry counters (the
    manifest-write path has never had its own accumulator) — without the
    separate check, a run throttled ONLY on the manifest-write leg would
    print nothing at all.
    """
    import click  # noqa: PLC0415 — deferred: CLI-only dependency, avoids import cost for non-CLI callers of this module
    import structlog  # noqa: PLC0415 — deferred: command-local logger binding

    from nexus.retry import get_retry_stats  # noqa: PLC0415 — deferred: avoids a hard nexus.retry dependency at CLI startup

    _log = structlog.get_logger(__name__)

    retry_stats = get_retry_stats()
    if not retry_stats["total_count"] and not retry_stats["brake_trips"]:
        return
    if retry_stats["total_count"]:
        parts: list[str] = []
        if retry_stats["voyage_count"]:
            parts.append(
                f"voyage {retry_stats['voyage_seconds']:.1f}s over "
                f"{retry_stats['voyage_count']} retries"
            )
        if retry_stats["vector_count"]:
            parts.append(
                f"chroma {retry_stats['vector_seconds']:.1f}s over "
                f"{retry_stats['vector_count']} retries"
            )
        click.echo(
            f"  Transient-error backoff: {retry_stats['total_seconds']:.1f}s total "
            f"({', '.join(parts)})",
            err=True,
        )
    if retry_stats["brake_trips"]:
        # nexus-cy9u7: the shared rate-limit brake paused every writer in
        # this process at least once — distinct from the per-attempt
        # backoff above (which counts local retries, not the shared pause
        # other workers may have also paid).
        _log.info(
            "rate_brake_run_summary",
            trips=retry_stats["brake_trips"],
            seconds_paused=retry_stats["brake_seconds"],
        )
        click.echo(
            f"  Rate-limit brake: {retry_stats['brake_trips']} pauses, "
            f"{retry_stats['brake_seconds']:.1f}s",
            err=True,
        )


def resolve_confirmed_write_failure_doc_ids() -> frozenset[str]:
    """nexus-wbfpw.29 round 6: which of this run's manifest-write-failed
    documents are ACTUALLY repaired, verified by reading the catalog
    manifest back AFTER the whole run (same-run self-heal included) has
    completed.

    Round 5's ``ManifestHealResult.confirmed_doc_ids`` compared the
    rebuilt chunk count against the document's ``chunk_count`` AT
    SELF-HEAL TIME — but a document whose manifest hook RAISED (as
    opposed to detecting and reporting its own failure) never has its
    ``chunk_count`` bumped at all, so that comparison read 0 and
    confirmed ANY rebuild, including one built from a T3 write that
    itself only partially landed this same run (round 5's own documented
    gap). This checks the only claim that actually means "this run's
    damage is undone": every chash the failing write was TRYING to put
    in the document's manifest (recorded per doc_id by
    ``mcp_infra._record_manifest_write_failure``, unioned across
    continuation-slice flushes — see
    ``mcp_infra.get_manifest_write_failure_chashes``) is present in the
    manifest now, after self-heal has had its chance.

    A doc_id with no recorded expected-chash set, an explicitly UNKNOWN
    one, or an empty one is never confirmed — a claim we cannot verify is
    a claim we do not make (Sam's locked decision: unconfirmed is not
    repaired).

    A catalog-reader failure during verification confirms NOTHING — fail
    loud rather than guess either way. The caller's existing fail-loud
    plumbing (``raise_identity_drop_exception``) then treats every write
    failure as still-failed, which is the correct outcome when the
    verification itself could not run.
    """
    from nexus.mcp_infra import (  # noqa: PLC0415 — deliberate function-local import: rare branch, only reached when a write failure was recorded
        get_manifest_write_failure_chashes,
        get_manifest_write_failures,
    )

    failed = get_manifest_write_failures()
    if not failed:
        return frozenset()

    expected = get_manifest_write_failure_chashes()
    candidates = {
        doc_id: chashes
        for doc_id in failed
        if (chashes := expected.get(doc_id)) is not None and chashes
    }
    if not candidates:
        return frozenset()

    try:
        from nexus.catalog.factory import make_catalog_reader  # noqa: PLC0415 — deliberate function-local import: heavy catalog dep, rare branch

        manifests = make_catalog_reader().get_manifests(list(candidates))
    except Exception:  # noqa: BLE001 — fail loud via the caller's existing gate: nothing is confirmed here, never a guess
        import structlog  # noqa: PLC0415 — deliberate function-local import: rare branch

        structlog.get_logger(__name__).warning(
            "manifest_write_failure_verification_read_failed", exc_info=True,
        )
        return frozenset()

    return frozenset(
        doc_id
        for doc_id, chashes in candidates.items()
        if chashes <= {row.chash for row in manifests.get(doc_id, []) if row.chash}
    )


def _resolve_manifest_failure_display_paths(doc_ids: list[str]) -> dict[str, str]:
    """Best-effort doc_id -> source path/URI lookup for the manifest
    write-failure warning (nexus-wbfpw.29 round 2, critic Significant):
    a bare catalog tumbler like ``"1.2.3"`` is not directly actionable —
    an operator needs an extra ``nx catalog show <tumbler>`` lookup to
    find the actual file to re-index. One batched ``resolve_many`` call
    resolves every failed doc_id's ``CatalogEntry`` in a single round
    trip (nexus-7lm3q's existing batch API).

    Never raises: a lookup failure (the catalog being unreachable is
    exactly one of the failure modes THIS warning fires for — see
    ``manifest_write_hook_no_catalog`` in ``mcp_infra.py``) must still
    let the bare-tumbler warning print rather than crash the run's
    end-of-run summary. Doc ids with no resolvable path/URI are simply
    absent from the returned mapping.
    """
    try:
        from nexus.mcp_infra import get_catalog  # noqa: PLC0415 — deliberate function-local import: rare branch, only reached when checked

        cat = get_catalog()
        if cat is None:
            return {}
        entries = cat.resolve_many(doc_ids)
    except Exception:  # noqa: BLE001 — best-effort: the warning must still print bare ids if path lookup fails
        import structlog  # noqa: PLC0415 — deliberate function-local import: rare branch, only reached on lookup failure

        structlog.get_logger(__name__).debug(
            "manifest_write_failure_path_lookup_failed", exc_info=True,
        )
        return {}
    paths: dict[str, str] = {}
    for doc_id, entry in entries.items():
        display = getattr(entry, "file_path", "") or getattr(entry, "source_uri", "")
        if display:
            paths[doc_id] = display
    return paths


def _emit_write_failed_warning(
    *, healed_doc_ids: "frozenset[str] | set[str]" = frozenset(),
) -> bool:
    """GH #1371: a persistent (retries-exhausted or non-retryable) catalog
    manifest-write failure previously surfaced only as a structlog
    WARNING — invisible without log capture wired up. Returns ``True`` iff
    an UNHEALED entry remains (see *healed_doc_ids* below) — a doc that
    failed but was restored is reported informationally and never trips
    the caller's fail-loud gate on its own.

    Wording copied VERBATIM from the two existing call sites this
    replaces (``index_repo_cmd``, ``dt.py``'s ``index_cmd``) — including
    the "document(s)" noun, which both callers already hardcoded
    identically despite ``dt.py`` otherwise saying "record(s)" elsewhere
    in its own summary (nexus-7f5qj: verified by direct comparison before
    extracting, NOT a new design choice — an earlier draft made the noun
    a per-caller parameter, which would have silently changed ``dt.py``'s
    output; keeping it hardcoded is what makes this a behavior-preserving
    refactor rather than a wording change).

    nexus-wbfpw.29 (RDR-192 S3b): the parenthetical doc-id list is new —
    a run failing this way must NAME the document(s) to re-index, not
    just count them (a manifest-hook EXCEPTION now lands here too, via
    ``HookRegistry.fire_batch``'s ``_record_manifest_hook_batch_exception``,
    alongside the pre-existing detected-write-failure path). Appended
    after the existing "N document(s)" phrase so the pinned substring
    checks in ``test_index_cmd.py`` / ``test_commands_helpers_identity_
    drop.py`` (which assert on the prefix, not the full line) keep
    passing unchanged.

    nexus-wbfpw.29 round 2 (critic Significant): each id also carries its
    resolved source path/URI when the catalog can supply one (a bare
    tumbler needs an extra ``nx catalog show`` lookup to be actionable) —
    see :func:`_resolve_manifest_failure_display_paths`.

    nexus-wbfpw.29 round 3 (critic Critical): ``nx index repo``'s own
    same-run manifest self-heal pass (nexus-c21fk, indexer.py) can repair
    the EXACT gap a manifest-hook exception left moments earlier in the
    SAME run — before this, the warning printed "will not appear in
    catalog-aware queries. Run 'nx catalog reconcile' to repair." for a
    document that already appears and needs no reconcile, a provably
    false claim. *healed_doc_ids* is the set of doc_ids this run's own
    self-heal pass (if any — most callers have none and pass the default
    empty set, which reproduces the exact prior behavior) already
    restored via the SAME ``atomic_manifest_replace`` path
    ``nx catalog reconcile`` uses. Failures in this set print a SEPARATE,
    informational line ("restored by self-heal ... no action needed")
    and are excluded from both the count and the parenthetical list in
    the fail-loud line below, and from this function's return value.
    """
    import click  # noqa: PLC0415 — deliberate function-local import: avoids click dependency at module import time

    from nexus.mcp_infra import get_manifest_write_failures  # noqa: PLC0415 — deliberate function-local import: rare branch, only reached when checked

    failed = get_manifest_write_failures()
    if not failed:
        return False
    healed = [d for d in failed if d in healed_doc_ids]
    still_failed = [d for d in failed if d not in healed_doc_ids]

    if healed:
        healed_paths = _resolve_manifest_failure_display_paths(healed)
        healed_text = ", ".join(
            f"{doc_id} ({healed_paths[doc_id]})" if doc_id in healed_paths else doc_id
            for doc_id in healed
        )
        click.echo(
            f"  WARNING: catalog manifest write failed for {len(healed)} "
            f"document(s) ({healed_text}) and was restored by self-heal "
            f"in this same run — no action needed.",
            err=True,
        )

    if not still_failed:
        return False
    paths = _resolve_manifest_failure_display_paths(still_failed)
    ids_text = ", ".join(
        f"{doc_id} ({paths[doc_id]})" if doc_id in paths else doc_id
        for doc_id in still_failed
    )
    click.echo(
        f"  WARNING: catalog manifest write failed for {len(still_failed)} "
        f"document(s) ({ids_text}) — they will not appear in "
        f"catalog-aware queries. Run 'nx catalog reconcile' to repair.",
        err=True,
    )
    return True


# nexus-z0o2p.20: how many not-indexed files the summary names before "and N more".
_MAX_NAMED_DROPS = 10


def _emit_identity_drops_warning() -> bool:
    """GH #1397 / nexus-94fxl: batches DROPPED for missing document
    identity never reach the write, so they are invisible to the
    write-failure count above — a clean "0 failed" hid them entirely.
    Returns ``True`` iff the collector held an entry."""
    import click  # noqa: PLC0415 — deliberate function-local import: avoids click dependency at module import time

    from nexus.mcp_infra import get_manifest_identity_drops  # noqa: PLC0415 — deliberate function-local import: rare branch, only reached when checked

    all_drops = get_manifest_identity_drops()
    if not all_drops:
        return False
    # RDR-223 (nexus-z0o2p.13): a drop with written=False was refused before any
    # write (no catalog document to own its chunks), so there is nothing stored to
    # reconcile; the two kinds get different wording.
    drops = [d for d in all_drops if d.get("written", True)]
    refused = [d for d in all_drops if not d.get("written", True)]
    if drops:
        n_chunks = sum(d["batch_size"] for d in drops)
        cols = sorted({d["collection"] for d in drops})
        click.echo(
            f"  WARNING: {len(drops)} chunk batch(es) ({n_chunks} chunks; "
            f"collection(s): {', '.join(cols)}) were indexed WITHOUT a "
            f"catalog document identity — their manifests were not "
            f"written and the documents will not appear in "
            f"catalog-aware queries. Run 'nx catalog reconcile' to "
            f"repair.",
            err=True,
        )
    if refused:
        n_chunks = sum(d["batch_size"] for d in refused)
        cols = sorted({d["collection"] for d in refused})
        chunk_text = f"{n_chunks} chunks; " if n_chunks else ""
        click.echo(
            f"  WARNING: {len(refused)} document(s) ({chunk_text}"
            f"collection(s): {', '.join(cols)}) were NOT indexed: catalog "
            f"registration returned no document identity to own their "
            f"chunks, so nothing was written. Fix the registration failure "
            f"(see the 'catalog_hook_register_failed', 'catalog_hook_failed' "
            f"or 'preflight_register_failed' log event) and re-run the index.",
            err=True,
        )
        # nexus-z0o2p.20: name the files and the reason each has no document.
        named = [f for d in refused for f in d.get("files", ())]
        for f in named[:_MAX_NAMED_DROPS]:
            click.echo(f"    not indexed: {f['file']} ({f['cause']})", err=True)
        if len(named) > _MAX_NAMED_DROPS:
            click.echo(
                f"    ... and {len(named) - _MAX_NAMED_DROPS} more", err=True,
            )
    return True


def _emit_refused_warning(*, indexed_count: int, refused_in_failed: int = 0) -> bool:
    """nexus-5xn3k.6 (RUNFENCE C4): the manifest write SUCCEEDED but the
    engine's fail-closed completion verify REFUSED the stamp —
    index_state stays 'indexing', the document is NOT fully indexed.
    Distinct from both collectors above (a refusal, not a write failure
    or an identity drop). Returns ``True`` iff the collector held an
    entry.

    *refused_in_failed* (nexus-l6tr7): how many of the collector's
    refusals the caller ALREADY bucketed into its ``failed`` list because
    the refusal propagated as ``IndexRunVerifyRefused`` (the streaming /
    incremental path) instead of returning normally with chunks (the
    flush-grain path, which IS counted in *indexed_count*).
    ``_fence_complete`` records BOTH kinds in the same process-global
    collector by design, so without this split a mixed batch printed
    "2 of the 1 indexed above" — an impossible subset. Zero (the default,
    and every caller that never propagates) keeps the original wording.
    """
    import click  # noqa: PLC0415 — deliberate function-local import: avoids click dependency at module import time

    from nexus.mcp_infra import get_complete_refusals  # noqa: PLC0415 — deliberate function-local import: rare branch, only reached when checked

    refused = get_complete_refusals()
    collected = len(refused)
    if collected == 0 and refused_in_failed <= 0:
        return False
    if refused_in_failed > collected:
        # The caller bucketed MORE refusals than the run-wide collector
        # holds. ``_fence_complete`` records every refusal before raising,
        # and the collector deduplicates by doc_id, so this is either a
        # duplicate record in one batch or recording drift — say so
        # rather than clamp into a sentence that contradicts itself
        # (substantive-critic; same event mcp_infra raises on the
        # write_many path).
        import structlog  # noqa: PLC0415 — deliberate function-local import: rare branch

        structlog.get_logger(__name__).warning(
            "complete_refused_count_mismatch",
            collector_len=collected,
            bucketed_in_failed=refused_in_failed,
            path="dt_index_footer",
            note="more refusals bucketed into failed than the run-wide "
                 "collector recorded — duplicate record or recording drift",
        )
        click.echo(
            f"  WARNING: {refused_in_failed} record(s) had completion "
            f"refused by the engine's fail-closed verify (fence left at "
            f"'indexing') and are listed under failed; the run-wide "
            f"refusal collector reported {collected} — see the "
            f"complete_refused_count_mismatch log event — NOT fully "
            f"indexed. Re-index or --force to retry.",
            err=True,
        )
        return True
    if refused_in_failed > 0:
        in_indexed = collected - refused_in_failed
        click.echo(
            f"  WARNING: {len(refused)} completion refusal(s) by the "
            f"engine's fail-closed verify (fence left at 'indexing'): "
            f"{in_indexed} of the {indexed_count} indexed above and "
            f"{refused_in_failed} listed under failed — NOT fully "
            f"indexed. Re-index or --force to retry.",
            err=True,
        )
        return True
    click.echo(
        f"  WARNING: {len(refused)} of the {indexed_count} "
        f"indexed above had completion refused by the engine's "
        f"fail-closed verify (fence left at 'indexing') — NOT "
        f"fully indexed. Re-index or --force to retry.",
        err=True,
    )
    return True


def _emit_superseded_swept_info() -> bool:
    """nexus-39upx hazard 4: chunks the in-band superseded-vector sweep
    already deleted this run — purely informational (a SUCCESSFUL
    cleanup, not a problem), so this always returns ``False`` and never
    trips the batch fail-loud gate below."""
    import click  # noqa: PLC0415 — deliberate function-local import: avoids click dependency at module import time

    from nexus.mcp_infra import get_superseded_sweep_stats  # noqa: PLC0415 — deliberate function-local import: rare branch, only reached when checked

    stats = get_superseded_sweep_stats()
    swept = stats.get("swept", 0)
    if swept:
        click.echo(
            f"  swept {swept} superseded T3 chunk(s) left behind by a "
            f"changed re-index (nexus-39upx)"
        )
    # nexus-4pj54: a multi-batch document's sweep is deferred to its
    # completion stamp. A failed or fenced run drops it (discarded); a run
    # that never reached either fence call leaves it held (pending). Either
    # way superseded rows may remain in T3. The run's own failure already
    # drives the exit code, so this stays informational.
    discarded = stats.get("deferred_discarded", 0)
    pending = stats.get("deferred_pending", 0)
    if discarded or pending:
        click.echo(
            f"  superseded-chunk sweep not run for {discarded} failed/fenced "
            f"and {pending} unfinished document(s); old T3 rows may remain "
            f"until 'nx t3 gc -c COLLECTION' (nexus-4pj54)"
        )
        pending_ids = stats.get("deferred_pending_doc_ids", [])
        if pending_ids:
            click.echo(f"    unfinished: {', '.join(pending_ids)}")
    return False


def _emit_superseded_sweep_skipped_warning() -> bool:
    """nexus-39upx hazard 4: the sweep could not verify orphanhood or
    note-safety for one or more documents this run — old/superseded T3
    rows (nexus-gtltb-class: still fully searchable, since vector search
    reads T3, not the manifest) may remain. Capability-honest: this
    collector exists so that state is never silent. Returns ``True`` iff
    any skip was recorded, so it counts toward the batch's non-zero exit
    (uqq9z/7f5qj strictness direction — a sweep that could not run is
    treated the same as any other identity-drop-class problem, not
    swallowed as housekeeping)."""
    import click  # noqa: PLC0415 — deliberate function-local import: avoids click dependency at module import time

    from nexus.mcp_infra import get_superseded_sweep_stats  # noqa: PLC0415 — deliberate function-local import: rare branch, only reached when checked

    skips = get_superseded_sweep_stats().get("skipped", [])
    if not skips:
        return False
    reasons = sorted({s["reason"] for s in skips})
    click.echo(
        f"  WARNING: superseded-chunk sweep skipped for {len(skips)} "
        f"document(s) ({', '.join(reasons)}) — old/superseded T3 rows "
        f"may still be searchable. Re-index, or run "
        f"'nx t3 gc -c COLLECTION' once the underlying issue clears.",
        err=True,
    )
    return True


_IDENTITY_DROP_CHECKS = {
    "write_failed": lambda indexed_count, **kw: _emit_write_failed_warning(
        healed_doc_ids=kw.get("healed_doc_ids", frozenset()),
    ),
    "identity_drops": lambda indexed_count, **kw: _emit_identity_drops_warning(),
    "refused": lambda indexed_count, **kw: _emit_refused_warning(
        indexed_count=indexed_count,
        refused_in_failed=kw.get("refused_in_failed", 0),
    ),
    "superseded_swept": lambda indexed_count, **kw: _emit_superseded_swept_info(),
    "superseded_sweep_skipped": lambda indexed_count, **kw: _emit_superseded_sweep_skipped_warning(),
}
# Default sequence matches index_repo_cmd's pre-extraction order (also
# used, fresh, by index_pdf_cmd / index_md_cmd — nexus-7f5qj). The two
# nexus-39upx entries are appended (not test-pinned anywhere upstream,
# same "no test pinned the print order" precedent the docstring above
# already documents for the first three).
_DEFAULT_ORDER = (
    "write_failed", "identity_drops", "refused",
    "superseded_swept", "superseded_sweep_skipped",
)


def emit_identity_drop_summary(
    *, indexed_count: int, order: tuple[str, ...] = _DEFAULT_ORDER,
    refused_in_failed: int = 0,
    healed_doc_ids: "frozenset[str] | set[str]" = frozenset(),
) -> bool:
    """Echo a WARNING line (stderr) for each populated collector since the
    last :func:`reset_identity_drop_collectors` call. Returns ``True`` if
    any of the three collectors held an entry, ``False`` if all were
    empty (the common case — silent).

    *order* controls the sequence the three WARNING lines print in —
    ``"write_failed"``, ``"identity_drops"``, ``"refused"``. Defaults to
    ``index_repo_cmd``'s pre-extraction order. ``dt.py``'s ``index_cmd``
    passes ``("refused", "write_failed", "identity_drops")`` to preserve
    ITS pre-extraction order exactly — no test pinned the print order for
    either caller, but matching it anyway keeps the "behavior-preserving
    refactor" claim exact rather than "no test noticed the difference"
    (code-review follow-up, T2 [21484]).

    *indexed_count* is only read by the completion-refusal branch
    ("N of the *indexed_count* indexed above..."); pass the count of
    files/records this run actually wrote (excluding skips), matching
    each caller's own bookkeeping.

    *refused_in_failed* (nexus-l6tr7): refusals the caller already
    bucketed into ``failed`` because they PROPAGATED as
    ``IndexRunVerifyRefused`` — see :func:`_emit_refused_warning`.

    *healed_doc_ids* (nexus-wbfpw.29 round 3): doc_ids this run's own
    same-run manifest self-heal pass already restored — forwarded only to
    the ``write_failed`` check (identity drops have no catalog entry for
    self-heal to have found in the first place; see
    :func:`_emit_write_failed_warning`). Callers with no same-run
    self-heal (every index verb except `nx index repo`) omit this and get
    the exact prior behavior.
    """
    problems_detected = False
    for key in order:
        if _IDENTITY_DROP_CHECKS[key](
            indexed_count, refused_in_failed=refused_in_failed,
            healed_doc_ids=healed_doc_ids,
        ):
            problems_detected = True
    return problems_detected


def raise_identity_drop_exception(
    *, subject: str = "document",
    healed_doc_ids: "frozenset[str] | set[str]" = frozenset(),
) -> None:
    """Raise the fail-loud ``ClickException`` for a BATCH run
    (``nx index repo`` / ``nx dt index``) that recorded manifest write
    failures, identity drops, completion refusals, and/or (nexus-39upx
    round 2 SIGNIFICANT 2) a superseded-chunk sweep skip. Call only after
    :func:`emit_identity_drop_summary` returned ``True``.

    *healed_doc_ids* (nexus-wbfpw.29 round 3): pass the SAME set given to
    :func:`emit_identity_drop_summary` — a write failure this run's own
    same-run self-heal already restored must not itself count toward
    ``write_failed`` here either, or a run with BOTH a self-healed write
    failure AND some unrelated real problem (e.g. an identity drop) would
    still wrongly list "manifest write failures" among the causes and
    point the operator at a reconcile that has nothing left to do.

    The message names ONLY the cause(s) that actually fired this run and
    the matching remedy for each — nexus-39upx round 2 (substantive-
    critique, T2 21515): the ORIGINAL wording named all three write-class
    causes and the ``nx catalog show``/re-index remedy UNCONDITIONALLY,
    which was accurate for the three write-class collectors this
    function was written for but became misleading once a fourth,
    housekeeping-only trigger (a sweep that could not verify orphan/note
    safety, no write ever failed) joined the same fail-loud gate: a run
    tripping ONLY that condition would exit non-zero pointing an operator
    at ``manifest-verify`` for a document whose manifest was never in
    question. The WARNING line ``_emit_superseded_sweep_skipped_warning``
    prints above already names the correct remedy
    (``nx t3 gc -c COLLECTION``); this exception now matches it instead
    of contradicting it.

    Reads the SAME four collectors ``emit_identity_drop_summary`` just
    printed from — nothing resets between that call and this one, so the
    state is identical. If reached with none of the four populated (the
    two-unit-test degenerate case, or any future caller that raises
    without checking first), falls back to the ORIGINAL generic wording
    verbatim — a defensive floor, not a real production path.

    For a SINGLE-FILE command where one failure means one orphaned
    document, use :func:`raise_identity_drop_exception_for_file` instead
    (nexus-7f5qj) — its wording names the file and the remedy, which a
    generic count-based message cannot.
    """
    import click  # noqa: PLC0415 — deliberate function-local import: avoids click dependency at module import time

    from nexus.mcp_infra import (  # noqa: PLC0415 — deliberate function-local import: rare branch, only reached on a fail-loud exit
        get_complete_refusals,
        get_manifest_identity_drops,
        get_manifest_write_failures,
        get_superseded_sweep_stats,
    )

    write_failed = bool(
        [d for d in get_manifest_write_failures() if d not in healed_doc_ids]
    )
    identity_dropped = bool(get_manifest_identity_drops())
    refused = bool(get_complete_refusals())
    sweep_skipped = bool(get_superseded_sweep_stats().get("skipped"))
    any_write_class = write_failed or identity_dropped or refused

    if not any_write_class and not sweep_skipped:
        raise click.ClickException(
            f"one or more {subject}s had manifest write failures, "
            f"identity drops, or completion refusals this run — see "
            f"the WARNING lines above. Run 'nx catalog show <tumbler>' to "
            f"inspect a specific {subject}'s index_state, or re-index "
            f"with --force."
        )

    causes = []
    if write_failed:
        causes.append("manifest write failures")
    if identity_dropped:
        causes.append("identity drops")
    if refused:
        causes.append("completion refusals")
    if sweep_skipped:
        causes.append("a superseded-chunk sweep skip")
    cause_text = (
        causes[0] if len(causes) == 1
        else ", ".join(causes[:-1]) + f", and {causes[-1]}"
    )

    # nexus-wbfpw.29: each cause names the remedy its own WARNING line
    # names. Manifest write failures and identity drops are both manifest
    # gaps that 'nx catalog reconcile' rebuilds from T3 (identity drops as
    # the GH #1397 ghost class: registered, chunk_count 0, content_hash
    # recorded). The old single write-class remedy ("catalog show / re-index
    # with --force") contradicted both WARNINGs. It stays for completion
    # refusals, whose WARNING says "Re-index or --force to retry", and as
    # the identity-drop fallback.
    #
    # round 4 critique (narrowed round 5): "reconcile cannot rebuild a
    # document that never registered" is NOT a blanket truth for every
    # identity-drop producer. It holds for a REGISTRATION failure
    # (indexer.py's preflight register call itself failed -- no catalog
    # row, no tumbler, nothing for reconcile's ghost path to find). It
    # does NOT hold for the OTHER identity-drop producer
    # (mcp_infra.py's manifest_write_batch_hook, fired when a chunk
    # batch's own catalog_doc_id/meta.doc_id never resolved): `nx index
    # repo` registers catalog entries UPFRONT, independently of whether a
    # later hook call correctly threads that id through, so a registered
    # entry with a resolvable content_hash MAY already exist for that
    # document -- reconcile's ghost-candidate match (content_hash +
    # physical_collection, no chunk_count requirement) can genuinely
    # repair that shape. --force stays as the fallback either way, since
    # this function cannot tell which of the two producers a given run's
    # drops came from and a document reconcile could not reach (never
    # registered, or its chunks are reported LOST) still needs it.
    remedies = []
    if write_failed and not identity_dropped:
        remedies.append("Run 'nx catalog reconcile' to repair the manifests")
    if identity_dropped:
        # nexus-z0o2p.20: a drop that wrote nothing (no document to own the
        # chunks) has no manifest for reconcile to repair.
        if all(not d.get("written", True) for d in get_manifest_identity_drops()):
            remedies.append(
                f"Fix the registration failure named above and re-run the "
                f"index; no chunk of the affected {subject}s was written"
            )
        else:
            remedies.append(
                f"Run 'nx catalog reconcile' to repair the manifests; re-index "
                f"with --force any {subject} still missing afterwards"
            )
    if refused:
        remedies.append(
            f"Run 'nx catalog show <tumbler>' to inspect a specific "
            f"{subject}'s index_state, or re-index with --force"
        )
    if sweep_skipped:
        remedies.append(
            "re-index, or run 'nx t3 gc -c COLLECTION' once the "
            "underlying sweep issue clears"
        )
    remedy_text = "; ".join(remedies) + "."

    raise click.ClickException(
        f"one or more {subject}s had {cause_text} this run — see the "
        f"WARNING lines above. {remedy_text}"
    )


def raise_identity_drop_exception_for_file(path: Path, *, chunks: int) -> None:
    """Raise the fail-loud ``ClickException`` for a SINGLE-FILE index
    command (``nx index pdf <file>`` / ``nx index md <file>``) whose
    catalog registration failed this run (nexus-7f5qj).

    For a one-file command a register failure means THIS document is
    orphaned: its chunks landed and are searchable (over-work-never-
    under-work — nothing was lost), but no catalog Document/tumbler
    exists for them, so it is invisible to every catalog-aware query.
    Distinct wording from :func:`raise_identity_drop_exception` (a batch
    run's generic count) — names the file and the remedy directly. Call
    only after :func:`emit_identity_drop_summary` returned ``True``.
    """
    import click  # noqa: PLC0415 — deliberate function-local import: avoids click dependency at module import time

    raise click.ClickException(
        f"{path} was indexed ({chunks} chunk(s) written) but its catalog "
        f"document identity failed to register this run — see the "
        f"WARNING line(s) above. The chunks are searchable but orphaned "
        f"(no tumbler, invisible to catalog-aware queries). Re-run once "
        f"the engine/catalog is reachable — the write is idempotent and "
        f"the chunks reconcile via upsert identity — or run 'nx catalog "
        f"reconcile' to repair without re-indexing."
    )
