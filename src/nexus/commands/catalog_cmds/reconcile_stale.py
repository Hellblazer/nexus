# SPDX-License-Identifier: AGPL-3.0-or-later
"""``nx catalog reconcile-stale`` (nexus-cdypx).

13,279 catalog docs (61.2% of production, 2026-08) carry ``chunk_count == 0``;
7,142 of those point at 29 T3 collections that no longer exist (benchmark-temp
/ worktree debris, nexus-wq1e4); 6,151 sit in collections that are still live.
Catalog-aware routing ranks over a corpus where 3 of 5 docs have no
retrievable content.

Default mode is a read-only CENSUS (report mode) — it never constructs a
catalog writer. Exit 0 means the report was produced; a nonzero exit (the
shared INCOMPLETE guard, same contract as ``nx catalog verify``) means a read
this classification depends on could not be trusted and none of the findings
below should be acted on — this verb's exit code is NOT itself a correctness
gate over the findings (``nx catalog verify`` is that gate).

Classes, mirroring the design memo (T1 scratch 80500d58):

  vanished_collection   physical_collection absent from T3 entirely.
                         empty_manifest -> tombstone candidate.
                         has_manifest -> diagnosis only, NEVER auto-deleted
                         (nexus-3ck2g: the engine's own tombstone path
                         deliberately leaves manifest rows behind, so a
                         non-empty manifest here is not proof of a live
                         backing collection). Rows carry ``chunk_count`` so a
                         "dishonest"-shaped doc (chunk_count > 0, empty
                         manifest) that ALSO sits in a vanished collection is
                         still visible in samples, not just the plain
                         ``dishonest`` bucket below.
  zero_count_live       chunk_count == 0, collection still exists in T3.
                         recount -> manifest is non-empty despite the
                         cached count (the wu8s1/94fxl class); resync
                         restores the COUNT only. A doc whose manifest
                         chashes are themselves missing from T3 (a Class-B
                         damaged manifest, see ``nx catalog verify``) gets
                         "fixed" to a number that still is not retrievable
                         content — run ``nx catalog verify`` after
                         ``--execute recount`` to confirm the resynced
                         count is backed by real chunks.
                         rdr145_exempt -> knowledge__* store_put notes
                         with no file_path/source_uri; legitimate, no
                         action (same predicate as
                         catalog_cmds.integrity._classify_never_chunked).
                         Population is LEGACY-ONLY going forward: since
                         nexus-sdp0u, every non-empty-title store_put /
                         memory-promote doc is registered with a synthesized
                         ``chroma://<collection>/<title>`` source_uri, so a
                         post-fix doc can no longer land here — it falls to
                         ``unresolvable_provenance``/``source_uri_only``
                         below instead. That is the deliberate resolution,
                         not a gap: manifests are written synchronously at
                         put time and a write failure surfaces loudly
                         (store.py's "stored but NOT cataloged" error), so a
                         post-fix store_put doc that is STILL chunk_count==0
                         is anomalous and "investigate" is the correct
                         disposition — "exempt" would hide it.
                         reindex_candidate -> a concrete on-disk location
                         resolves (from file_path or a file:// source_uri)
                         and the file exists there, and its content is NOT
                         verifiably unchunkable (see zero_content_by_design
                         below) — re-index is a plausible remedy.
                         zero_content_by_design -> the resolved, existing
                         file is zero-byte or binary content (same
                         ``classifier.looks_like_binary_content`` sniff the
                         producer-side registration fix uses, nexus-rqsh1)
                         — it can NEVER produce a chunk no matter how many
                         times it is re-indexed, so re-index is never the
                         correct remedy; tombstone is. An HONEST bucket,
                         not a suppression — these stay in the census until
                         actually tombstoned.
                         orphaned_path -> a concrete on-disk location
                         resolves and is CONFIRMED absent (``reason``:
                         ``file_missing`` — the file itself is gone;
                         ``owner_root_gone`` — the owning repo_root itself
                         no longer exists on disk, e.g. a deleted worktree
                         or temp checkout, nexus-wq1e4's confirmed-safe
                         population). Tombstone candidate.
                         unresolvable_provenance -> absence could NEVER be
                         confirmed from any mutation arm (``reason``:
                         ``no_repo_root`` — owner has no registered
                         repo_root, mirrors remediation.py's
                         ``skipped_no_root`` refusal; ``malformed_tumbler``
                         — the tumbler has no owner-address component;
                         ``source_uri_only`` — empty file_path but a
                         non-``file://``, non-``chroma://`` source_uri
                         (RDR-096 P3.1 schemes: ``x-devonthink-item://``,
                         ``nx-orphan-backfill://``, ``nx-scratch://``);
                         ``no_provenance`` — empty
                         file_path AND empty source_uri on a non-
                         ``knowledge__`` collection, the ``code__``
                         "unclassified" population ``catalog_cmds.integrity``
                         itself flags as investigation-needed, not a
                         disposition RDR-145 ever ruled on). Diagnosis
                         only — absence of evidence is not evidence of
                         absence; never delete-eligible, mirroring the
                         ``dishonest`` bucket below.
                         store_put_origin -> nexus-0y0gk critique fix-round:
                         a zero-chunk-count knowledge__ store_put doc
                         carrying its nexus-sdp0u synthesized
                         ``chroma://<collection>/<title>`` source_uri.
                         Formerly folded into ``unresolvable_provenance``/
                         ``source_uri_only`` (see the ``rdr145_exempt``
                         entry above for the pre-sdp0u/post-sdp0u split this
                         predicate mirrors) — pulled into its own bucket
                         because it is a RECOGNIZABLE population, not a
                         genuinely-unknown one, even though (chunk_count==0
                         here) it still needs a content re-write, not a
                         manifest-only backfill; see ``dishonest``'s
                         ``store_put_origin`` below for the FK-safe sibling
                         population. ``reason`` is always ``chroma_uri``
                         here (the ``knowledge_single_chunk_no_path``
                         sub-reason requires chunk_count==1, unreachable at
                         chunk_count==0).
  dishonest              chunk_count > 0 but the manifest is empty (the
                         wq1e4 "5 dishonest" population, e.g. tumblers
                         1.10.4386, 1.11.227-230). Diagnosis only;
                         nexus-wq1e4 explicitly forbids sweeping these
                         automatically. NOT tumbler 1.14.2 — that fixture
                         has a NON-empty 56-row manifest with the T3 chunks
                         themselves missing (wq1e4's separate Class-B
                         damaged/dangling-manifest population, out of this
                         verb's scope; see ``nx catalog verify``'s
                         ``damaged`` class).
                         nexus-0y0gk: each row now also carries an
                         ``origin`` (plus, where applicable, ``reason``/
                         ``resolved_path``/``file_path``) — FOUR values,
                         checked in this order: ``store_put_origin`` (the
                         nexus-sdp0u store_put signature — ``reason``
                         ``chroma_uri`` for a synthesized ``chroma://``
                         source_uri, or ``knowledge_single_chunk_no_path``
                         for a knowledge__ doc with chunk_count==1 and
                         NEITHER file_path nor source_uri — store_put docs
                         are single-chunk by construction, so a live T3
                         chunk very likely still exists and this class is
                         an FK-safe ``nx t3 backfill-manifest --only-gapped``
                         candidate, NOT "cannot confirm, leave it"; critique
                         fix-round, 2026-08-15: 4 of the 5 live dishonest
                         docs at the time carried exactly this signature and
                         were being lumped into ``unresolvable_provenance``);
                         then the SAME ``_resolve_provenance`` split the
                         ``zero_count_*`` buckets use — ``reindex_candidate``
                         (a concrete on-disk location resolves and exists),
                         ``orphaned_path`` (confirmed absent), or
                         ``unresolvable_provenance`` (absence could never be
                         confirmed) — so the 3n7pr triage (file-backed
                         re-index vs. store_put-origin backfill vs.
                         genuinely unknown) is mechanical instead of a
                         hand-run SQL query.

Mutation arms (``--execute {recount,tombstone-vanished,tombstone-orphaned,
tombstone-zero-content}``)
follow the ``--dry-run/--no-dry-run`` + ``--confirm`` gate nexus-tnz3
inverted catalog_cmds.maintenance.gc_cmd to: a forgotten flag reports,
it never mutates. The catalog writer is constructed lazily — only once a
run has both ``--no-dry-run`` and ``--confirm`` — so a plain report or a
report-only ``--no-dry-run`` invocation never touches the write path.
``--json`` and ``--execute`` are mutually exclusive: the mutation arms print
plain-text action reports that would corrupt the JSON stdout contract.
"""
from __future__ import annotations

import json
from pathlib import Path
from typing import TYPE_CHECKING

import click
import structlog

from nexus.catalog.tumbler import Tumbler

# Deliberate cross-module reuse of catalog_cmds.integrity's private
# predicates (design memo T1 80500d58) — single source of truth for the
# vanished-collection INCOMPLETE guard and the RDR-145 exemption predicate,
# rather than a second, driftable copy of either.
from nexus.commands.catalog_cmds.integrity import (
    _class_a_vanished_collections,
    _classify_never_chunked,
    _is_zero_content_by_design,
)

# nexus-u8n4r: the worktree/tempdir predicate moved to nexus.repo_identity
# so the index-time registration guard can share it instead of forking a
# second copy. Alias kept so existing imports (this module's own
# `_is_worktree_or_tempdir_path` call sites below, and
# `tests/test_catalog_reconcile_stale.py`'s
# `reconcile_stale_mod._is_worktree_or_tempdir_path`) keep working
# unchanged.
from nexus.repo_identity import is_worktree_or_tempdir_path

_log = structlog.get_logger(__name__)

if TYPE_CHECKING:
    from nexus.catalog.catalog_protocol import CatalogReader  # noqa: F401 — PEP 563 deferred annotation use

_ACTIONS = ("recount", "tombstone-vanished", "tombstone-orphaned", "tombstone-zero-content")

# reconcile_cmd's convention (catalog.py): actionable findings cap at 20,
# report-only/diagnosis-only findings cap at 5.
_CAP_ACTION = 20
_CAP_INFO = 5

# Progress checkpoint cadence for the (per-doc HTTP) recount mutation loop —
# opaque past a few hundred docs without it (code-review IMPORTANT).
_PROGRESS_EVERY = 100

_ORPHANED_REASONS = ("file_missing", "owner_root_gone")
_UNRESOLVABLE_REASONS = ("no_repo_root", "malformed_tumbler", "source_uri_only", "no_provenance")
_STORE_PUT_ORIGIN_REASONS = ("chroma_uri", "knowledge_single_chunk_no_path")
_STORE_PUT_URI_PREFIX = "chroma://"

# nexus-wq1e4: paths under a Claude Code worktree, or shaped like a system
# temp directory, are the "confirmed-safe" population an operator asked to
# see called out explicitly before any tombstone run (1,798 of the original
# 7,142-doc evidence set were worktree paths). Predicate itself now lives in
# nexus.repo_identity (nexus-u8n4r) — see the import above.
_is_worktree_or_tempdir_path = is_worktree_or_tempdir_path


def _owner_id_of(tumbler_str: str) -> str:
    parts = tumbler_str.split(".")
    return ".".join(parts[:2]) if len(parts) >= 2 else ""


def _resolve_provenance(entry: object, owner_roots: dict[str, str]) -> tuple[Path | None, str]:
    """Resolve *entry*'s on-disk location, or diagnose why it cannot be.

    Returns ``(resolved_path, reason)``. ``reason == ""`` means
    *resolved_path* is a concrete, cwd-independent candidate whose
    EXISTENCE the caller still has to check (nexus-6ims: an absolute path,
    or a relative one anchored at the owner's ``repo_root``, never at cwd).
    A non-empty *reason* is one of two disjoint outcomes the caller must
    route to a DIFFERENT bucket:

      * :data:`_ORPHANED_REASONS` (``file_missing`` is the caller's own
        ``.exists()`` check failing; ``owner_root_gone`` is returned here
        directly) — the evidence IS conclusive, tombstone-eligible.
      * :data:`_UNRESOLVABLE_REASONS` — absence could never be confirmed;
        diagnosis-only, mirrors ``remediation.py``'s ``skipped_no_root``
        refusal (lines ~538-544: "cannot verify" != "confirmed gone").

    nexus-6ims: cwd resolution mass-misclassified 11,766 entries when the
    operator ran the sweep from a different repo than the entry's owner —
    a relative file_path is anchored at ``owner_roots[owner_id]`` or,
    absent that, treated as unresolvable. Never falls back to cwd.
    """
    fp = entry.file_path or ""
    source_uri = getattr(entry, "source_uri", "") or ""

    if fp:
        if fp.startswith("/"):
            return Path(fp), ""
        owner_id = _owner_id_of(str(entry.tumbler))
        if not owner_id:
            return None, "malformed_tumbler"
        root = owner_roots.get(owner_id, "")
        if not root:
            return None, "no_repo_root"
        if not Path(root).exists():
            # The owner's own worktree/temp checkout is gone — a deleted
            # root can never resurrect the file inside it. This IS the
            # wq1e4 confirmed-safe population, not merely unverifiable.
            return Path(root) / fp, "owner_root_gone"
        return Path(root) / fp, ""

    if source_uri.startswith("file://"):
        # RDR-096 P3.1 (_normalize_source_uri, catalog/types.py): a
        # file:// source_uri is already an absolute path — resolvable
        # exactly like file_path, cwd-independent by construction.
        return Path(source_uri[len("file://"):]), ""

    if source_uri:
        # x-devonthink-item://, nx-orphan-backfill://, nx-scratch:// (any
        # other RDR-096 non-file scheme): the doc's provenance is a
        # non-filesystem identity. "file missing" is meaningless here —
        # there was never a resolvable path to confirm absent.
        return None, "source_uri_only"

    if not entry.physical_collection.startswith("knowledge__"):
        # code__/docs__/rdr__ docs with NEITHER file_path NOR source_uri.
        # RDR-145's "legitimate by design" exemption is scoped to
        # knowledge__ store_put notes only (_classify_never_chunked,
        # already checked by the caller before this function runs) — this
        # is exactly the code__ "unclassified" population
        # catalog_cmds.integrity's own docstring flags as investigation-
        # needed, a disposition RDR-145 never ruled on. Absence of
        # evidence is not evidence of absence.
        return None, "no_provenance"

    # A knowledge__ doc with empty file_path/source_uri reaching here means
    # _classify_never_chunked disagreed with this predicate (defensive —
    # the two are meant to stay in lockstep). Still diagnosis-only.
    return None, "no_provenance"


def _store_put_signature_reason(entry: object) -> str | None:
    """Return the store_put-origin sub-reason when *entry* matches the
    nexus-sdp0u store_put signature, else ``None`` (nexus-0y0gk critique
    fix-round).

    ``_resolve_provenance``'s catch-all ``unresolvable_provenance``/
    ``no_provenance`` outcome was lumping genuinely-dead docs together with
    a RECOGNIZABLE, likely-backfillable sub-population — 4 of the 5 live
    ``dishonest`` docs (chunk_count > 0, manifest empty) at critique time
    carried exactly this signature and are FK-safe
    ``nx t3 backfill-manifest --only-gapped`` candidates (store_put docs are
    single-chunk by construction: ``doc_id == chunk_text_hash ==
    content_hash``, so a live T3 chunk very likely still exists), not
    "cannot confirm, leave it".

    Two disjoint sub-signatures:

      chroma_uri                      source_uri is the nexus-sdp0u
                                       synthesized ``chroma://<collection>/
                                       <title>`` identity written at
                                       store_put time, regardless of
                                       chunk_count. Reachable from both the
                                       ``dishonest`` bucket and the
                                       zero_count_* triage.
      knowledge_single_chunk_no_path  chunk_count == 1, physical_collection
                                       is ``knowledge__*``, and BOTH
                                       file_path/source_uri are empty. Only
                                       reachable from the ``dishonest``
                                       bucket (chunk_count > 0) — a
                                       chunk_count==0 doc with this exact
                                       empty-path/empty-uri shape is already
                                       routed to ``rdr145_exempt`` upstream
                                       (pre-sdp0u legacy note) before
                                       reaching this function at all.

    A non-empty ``file_path`` always wins as ``reindex_candidate`` instead
    — this function returns ``None`` immediately when one is present, so a
    file-backed doc is never silently reclassified as store_put-origin.
    """
    if entry.file_path:
        return None
    source_uri = getattr(entry, "source_uri", "") or ""
    if source_uri.startswith(_STORE_PUT_URI_PREFIX):
        return "chroma_uri"
    if (
        not source_uri
        and entry.physical_collection.startswith("knowledge__")
        and entry.chunk_count == 1
    ):
        return "knowledge_single_chunk_no_path"
    return None


def _breakdown_by_collection(rows: list[dict]) -> list[dict]:
    counts: dict[str, int] = {}
    for row in rows:
        coll = row["physical_collection"]
        counts[coll] = counts.get(coll, 0) + 1
    return [{"collection": c, "count": n} for c, n in sorted(counts.items())]


def _count_by_key(rows: list[dict], key: str) -> dict[str, int]:
    counts: dict[str, int] = {}
    for row in rows:
        val = row.get(key, "")
        counts[val] = counts.get(val, 0) + 1
    return counts


def _count_by_reason(rows: list[dict]) -> dict[str, int]:
    return _count_by_key(rows, "reason")


def _resolve_dishonest_origin(entry: object, owner_roots: dict[str, str]) -> dict:
    """Provenance split for the ``dishonest`` population (chunk_count > 0,
    manifest empty) so its triage is mechanical instead of requiring a
    hand-run SQL query against the engine.

    Returns a dict merged into the row: ``origin`` is one of FOUR values —
    ``store_put_origin`` (checked FIRST, via :func:`_store_put_signature_reason`
    — the nexus-sdp0u store_put signature; critique fix-round, nexus-0y0gk:
    this is a RECOGNIZABLE, FK-safe-backfillable sub-population that must
    not be lumped into ``unresolvable_provenance``), or one of the SAME
    three ``_resolve_provenance`` outcomes the ``zero_count_*`` buckets use
    — ``reindex_candidate`` (a concrete on-disk location resolves and
    exists), ``orphaned_path`` (confirmed absent), or
    ``unresolvable_provenance`` (absence could never be confirmed). The
    latter three are the same bucket names, without the ``zero_count_``
    prefix, since this is a different source population, not a fourth
    bucket of the zero-count triage. Field shape (``resolved_path``,
    ``file_path``, ``reason``) matches the corresponding ``zero_count_*``
    rows exactly — see ``_classify``'s zero-count branch for the origin.
    """
    store_put_reason = _store_put_signature_reason(entry)
    if store_put_reason is not None:
        return {"origin": "store_put_origin", "reason": store_put_reason}
    resolved, reason = _resolve_provenance(entry, owner_roots)
    if reason in _UNRESOLVABLE_REASONS:
        return {"origin": "unresolvable_provenance", "reason": reason}
    if reason == "owner_root_gone":
        return {
            "origin": "orphaned_path",
            "file_path": entry.file_path or "",
            "resolved_path": str(resolved),
            "reason": "owner_root_gone",
        }
    if resolved is not None and resolved.exists():
        return {"origin": "reindex_candidate", "resolved_path": str(resolved)}
    return {
        "origin": "orphaned_path",
        "file_path": entry.file_path or "",
        "resolved_path": str(resolved),
        "reason": "file_missing",
    }


def _format_reason_counts(counts: dict[str, int]) -> str:
    return ", ".join(f"{reason}={n}" for reason, n in sorted(counts.items()))


def _classify(cat: "CatalogReader", t3: object) -> tuple[dict, list[str]]:
    """Build the full classification report. Returns ``(report, unreadable)``.

    ``unreadable`` non-empty means a read this classification depends on
    (T3 collection listing, or the batched manifest fetch) could not be
    trusted — the caller must refuse to act on ANY of the findings below,
    same as ``catalog verify``'s INCOMPLETE contract.
    """
    entries = [
        e for e in cat.all_documents(limit=0)
        if not e.alias_of and e.physical_collection
    ]
    total_docs = len(entries)

    report = {
        "total_docs": total_docs,
        "vanished_empty_manifest": [],
        "vanished_has_manifest": [],
        "zero_count_recount": [],
        "zero_count_rdr145_exempt": [],
        "zero_count_reindex_candidate": [],
        "zero_count_zero_content_by_design": [],
        "zero_count_orphaned_path": [],
        "zero_count_unresolvable_provenance": [],
        "zero_count_store_put_origin": [],
        "dishonest": [],
    }
    unreadable: list[str] = []
    if not entries:
        return report, unreadable

    # sj4a3 guard lives in _class_a_vanished_collections: an empty T3
    # listing against a populated (non-bypass) catalog is indistinguishable
    # from a swallowed transport error, so it folds into `unreadable`
    # instead of reporting every collection vanished.
    _, vanished_names = _class_a_vanished_collections(cat, t3, unreadable)
    if unreadable:
        return report, unreadable

    try:
        manifests = cat.get_manifests([str(e.tumbler) for e in entries])
    except Exception as exc:  # noqa: BLE001 — isolated: reported, not swallowed
        unreadable.append("catalog:get_manifests")
        _log.warning("reconcile_stale_get_manifests_failed", error=str(exc))
        return report, unreadable

    owner_roots = cat.owners_with_roots()

    for e in entries:
        tstr = str(e.tumbler)
        manifest_len = len(manifests.get(tstr, []))
        row = {
            "tumbler": tstr, "title": e.title,
            "physical_collection": e.physical_collection,
            "manifest_len": manifest_len,
            "chunk_count": e.chunk_count,
        }

        if e.physical_collection in vanished_names:
            bucket = report["vanished_empty_manifest" if manifest_len == 0 else "vanished_has_manifest"]
            bucket.append(row)
            continue

        if e.chunk_count == 0:
            if manifest_len > 0:
                report["zero_count_recount"].append(row)
            elif _classify_never_chunked(e) == "rdr145_exempt":
                report["zero_count_rdr145_exempt"].append(row)
            elif (store_put_reason := _store_put_signature_reason(e)) is not None:
                # nexus-0y0gk critique fix-round: the same store_put
                # signature check the dishonest bucket uses, applied here
                # so a post-sdp0u store_put doc that is STILL
                # chunk_count==0 (module docstring: "anomalous, investigate")
                # is labelled distinctly from a genuinely-unknown doc
                # instead of collapsing into unresolvable_provenance.
                report["zero_count_store_put_origin"].append({**row, "reason": store_put_reason})
            else:
                resolved, reason = _resolve_provenance(e, owner_roots)
                if reason in _UNRESOLVABLE_REASONS:
                    report["zero_count_unresolvable_provenance"].append({**row, "reason": reason})
                elif reason == "owner_root_gone":
                    report["zero_count_orphaned_path"].append({
                        **row,
                        "file_path": e.file_path or "",
                        "resolved_path": str(resolved),
                        "reason": "owner_root_gone",
                    })
                elif resolved is not None and resolved.exists():
                    if _is_zero_content_by_design(resolved):
                        # nexus-rqsh1: a zero-byte or binary source can
                        # NEVER produce a chunk no matter how many times it
                        # is re-indexed -- reindex_candidate implies
                        # re-index is a valid remedy, which is false here.
                        report["zero_count_zero_content_by_design"].append(
                            {**row, "resolved_path": str(resolved)},
                        )
                    else:
                        report["zero_count_reindex_candidate"].append({**row, "resolved_path": str(resolved)})
                else:
                    report["zero_count_orphaned_path"].append({
                        **row,
                        "file_path": e.file_path or "",
                        "resolved_path": str(resolved),
                        "reason": "file_missing",
                    })
            continue

        if manifest_len == 0:
            report["dishonest"].append({**row, **_resolve_dishonest_origin(e, owner_roots)})

    return report, unreadable


def _assert_empty_manifest(targets: list[dict]) -> tuple[list[dict], list[dict]]:
    """Split *targets* into (confirmed-empty-manifest, skipped-non-empty).

    Classification already guarantees every row handed to a tombstone arm
    has ``manifest_len == 0`` — this is the runtime re-check the design
    calls for (recount-class docs must never reach a tombstone arm) so a
    future classification bug degrades to "skip + report", not a silent
    delete of a doc whose manifest resurfaced.
    """
    ok = [r for r in targets if r.get("manifest_len", 0) == 0]
    skipped = [r for r in targets if r.get("manifest_len", 0) != 0]
    return ok, skipped


def _echo_sample(rows: list[dict], cap: int, label_fn) -> None:
    for row in rows[:cap]:
        click.echo(f"    {label_fn(row)}")
    if len(rows) > cap:
        click.echo(f"    ... and {len(rows) - cap} more")


def _label_vanished(row: dict) -> str:
    # nexus-cdypx fix-round (critic observation): a "dishonest"-shaped doc
    # (chunk_count > 0, empty manifest) that ALSO sits in a vanished
    # collection must stay visible as such in samples, not just blend in.
    chunk_note = f"  chunk_count={row['chunk_count']}" if row.get("chunk_count") else ""
    return f"{row['tumbler']:<14} [{row['physical_collection']}]  {row['title']}{chunk_note}"


def _label_zero_count(row: dict) -> str:
    extra = f"  -> {row['resolved_path']}" if "resolved_path" in row else ""
    reason = f"  ({row['reason']})" if "reason" in row else ""
    return f"{row['tumbler']:<14} [{row['physical_collection']}]  {row['title']}{extra}{reason}"


def _label_dishonest(row: dict) -> str:
    origin = f"  origin={row['origin']}" if "origin" in row else ""
    reason = f"  ({row['reason']})" if "reason" in row else ""
    return (
        f"{row['tumbler']:<14} [{row['physical_collection']}]  "
        f"chunk_count={row['chunk_count']}  {row['title']}{origin}{reason}"
    )


def _echo_human_report(report: dict, unreadable: list[str]) -> None:
    click.echo(f"Catalog reconcile-stale: {report['total_docs']} non-alias catalog document(s) examined.")

    vanished_empty = report["vanished_empty_manifest"]
    vanished_has = report["vanished_has_manifest"]
    if vanished_empty or vanished_has:
        click.echo(
            f"\nVanished collections ({len(vanished_empty)} tombstone candidate(s), "
            f"{len(vanished_has)} diagnosis-only — non-empty manifest, nexus-3ck2g, never auto-deleted):"
        )
        if vanished_empty:
            click.echo("  Tombstone candidates:")
            _echo_sample(vanished_empty, _CAP_ACTION, _label_vanished)
        if vanished_has:
            click.echo("  Diagnosis-only (non-empty manifest):")
            _echo_sample(vanished_has, _CAP_INFO, _label_vanished)
        click.echo("  By collection:")
        for row in _breakdown_by_collection(vanished_empty + vanished_has):
            click.echo(f"    {row['count']:5d}  {row['collection']}")

    zc_recount = report["zero_count_recount"]
    zc_exempt = report["zero_count_rdr145_exempt"]
    zc_reindex = report["zero_count_reindex_candidate"]
    zc_zero_content = report["zero_count_zero_content_by_design"]
    zc_orphaned = report["zero_count_orphaned_path"]
    zc_unresolvable = report["zero_count_unresolvable_provenance"]
    zc_store_put = report["zero_count_store_put_origin"]
    if (
        zc_recount or zc_exempt or zc_reindex or zc_zero_content
        or zc_orphaned or zc_unresolvable or zc_store_put
    ):
        click.echo(
            f"\nZero-count live documents ({len(zc_recount)} recount, "
            f"{len(zc_exempt)} RDR-145 exempt, {len(zc_reindex)} reindex candidate(s), "
            f"{len(zc_zero_content)} zero-content-by-design, "
            f"{len(zc_orphaned)} orphaned path, {len(zc_store_put)} store_put origin, "
            f"{len(zc_unresolvable)} unresolvable provenance):"
        )
        if zc_recount:
            click.echo("  Recount candidates (manifest non-empty; resync):")
            _echo_sample(zc_recount, _CAP_ACTION, _label_zero_count)
        if zc_exempt:
            click.echo("  RDR-145 exempt (store_put notes; no action):")
            _echo_sample(zc_exempt, _CAP_INFO, _label_zero_count)
        if zc_reindex:
            click.echo("  Reindex candidates (source file present):")
            _echo_sample(zc_reindex, _CAP_ACTION, _label_zero_count)
        if zc_zero_content:
            click.echo(
                "  Zero-content-by-design (zero-byte or binary source; "
                "will never chunk; producer no longer registers these; "
                "remedy is tombstone, not re-index):"
            )
            _echo_sample(zc_zero_content, _CAP_ACTION, _label_zero_count)
        if zc_orphaned:
            click.echo("  Orphaned path (confirmed absent; tombstone candidate):")
            _echo_sample(zc_orphaned, _CAP_ACTION, _label_zero_count)
            worktree_n = sum(1 for r in zc_orphaned if _is_worktree_or_tempdir_path(r.get("resolved_path", "")))
            click.echo(
                f"    {worktree_n} of {len(zc_orphaned)} orphaned-path row(s) are worktree/temp-dir "
                "paths (nexus-wq1e4 confirmed-safe signal)."
            )
            click.echo(f"    by reason: {_format_reason_counts(_count_by_reason(zc_orphaned))}")
        if zc_store_put:
            click.echo(
                "  Store_put origin (nexus-sdp0u signature; recognizable, "
                "NOT genuinely-unknown provenance — see `nx t3 backfill-manifest "
                "--dry-run --only-gapped`):"
            )
            _echo_sample(zc_store_put, _CAP_ACTION, _label_zero_count)
            click.echo(f"    by reason: {_format_reason_counts(_count_by_reason(zc_store_put))}")
        if zc_unresolvable:
            click.echo("  Unresolvable provenance (cannot confirm absence; diagnosis only, NEVER tombstoned):")
            _echo_sample(zc_unresolvable, _CAP_INFO, _label_zero_count)
            click.echo(f"    by reason: {_format_reason_counts(_count_by_reason(zc_unresolvable))}")
        click.echo("  By collection:")
        for row in _breakdown_by_collection(
            zc_recount + zc_exempt + zc_reindex + zc_zero_content
            + zc_orphaned + zc_store_put + zc_unresolvable
        ):
            click.echo(f"    {row['count']:5d}  {row['collection']}")

    dishonest = report["dishonest"]
    if dishonest:
        click.echo(f"\nDishonest documents ({len(dishonest)} — chunk_count > 0, manifest empty; diagnosis only, never auto-acted):")
        _echo_sample(dishonest, _CAP_INFO, _label_dishonest)
        click.echo(f"    by origin: {_format_reason_counts(_count_by_key(dishonest, 'origin'))}")

    click.echo("\nProposed actions:")
    click.echo(
        f"  nx catalog reconcile-stale --execute recount --no-dry-run --confirm"
        f"            # resync {len(zc_recount)} zero-count doc(s) with a non-empty manifest"
    )
    click.echo(
        f"  nx catalog reconcile-stale --execute tombstone-vanished --no-dry-run --confirm"
        f"  # tombstone {len(vanished_empty)} vanished-collection doc(s) with an empty manifest"
    )
    click.echo(
        f"  nx catalog reconcile-stale --execute tombstone-orphaned --no-dry-run --confirm"
        f"  # tombstone {len(zc_orphaned)} doc(s) whose source is confirmed gone"
    )
    click.echo(
        f"  nx catalog reconcile-stale --execute tombstone-zero-content --no-dry-run --confirm"
        f"  # tombstone {len(zc_zero_content)} doc(s) whose source will never chunk (zero-byte/binary)"
    )

    if unreadable:
        click.echo(
            f"\nWARNING: {len(unreadable)} check(s) could not be read and were SKIPPED "
            f"(not classified): {', '.join(sorted(unreadable))}",
            err=True,
        )


def _json_payload(report: dict, unreadable: list[str]) -> dict:
    zc_orphaned = report["zero_count_orphaned_path"]
    zc_unresolvable = report["zero_count_unresolvable_provenance"]
    zc_store_put = report["zero_count_store_put_origin"]
    return {
        "summary": {
            "total_docs": report["total_docs"],
            "vanished_empty_manifest": len(report["vanished_empty_manifest"]),
            "vanished_has_manifest": len(report["vanished_has_manifest"]),
            "zero_count_recount": len(report["zero_count_recount"]),
            "zero_count_rdr145_exempt": len(report["zero_count_rdr145_exempt"]),
            "zero_count_reindex_candidate": len(report["zero_count_reindex_candidate"]),
            "zero_count_zero_content_by_design": len(report["zero_count_zero_content_by_design"]),
            "zero_count_orphaned_path": len(zc_orphaned),
            "zero_count_store_put_origin": len(zc_store_put),
            "zero_count_unresolvable_provenance": len(zc_unresolvable),
            "dishonest": len(report["dishonest"]),
        },
        "vanished": {
            "empty_manifest": _breakdown_by_collection(report["vanished_empty_manifest"]),
            "has_manifest": _breakdown_by_collection(report["vanished_has_manifest"]),
        },
        "zero_count_live": {
            "recount": len(report["zero_count_recount"]),
            "recount_targets": report["zero_count_recount"],
            "rdr145_exempt": len(report["zero_count_rdr145_exempt"]),
            "reindex_candidates": report["zero_count_reindex_candidate"],
            "zero_content_by_design": report["zero_count_zero_content_by_design"],
            "orphaned_path": zc_orphaned,
            "orphaned_path_by_reason": _count_by_reason(zc_orphaned),
            "orphaned_path_worktree_count": sum(
                1 for r in zc_orphaned if _is_worktree_or_tempdir_path(r.get("resolved_path", ""))
            ),
            "store_put_origin": zc_store_put,
            "store_put_origin_by_reason": _count_by_reason(zc_store_put),
            "unresolvable_provenance": zc_unresolvable,
            "unresolvable_provenance_by_reason": _count_by_reason(zc_unresolvable),
        },
        "dishonest": report["dishonest"],
        "dishonest_by_origin": _count_by_key(report["dishonest"], "origin"),
        "incomplete": unreadable,
    }


def _raise_if_unreadable(unreadable: list[str]) -> None:
    if unreadable:
        raise click.ClickException(
            f"reconcile-stale INCOMPLETE: {len(unreadable)} check(s) could not be read "
            f"({', '.join(sorted(unreadable))}). Refusing to classify vanished collections "
            "against a possibly-swallowed T3 read — re-run once the store is healthy."
        )


def _report_only_notice(dry_run: bool, confirm: bool) -> bool:
    """Echo the report-only nudge when applicable. Returns whether to act."""
    will_act = (not dry_run) and confirm
    if (not dry_run) and not confirm:
        click.echo(
            "--no-dry-run alone is treated as report-only. "
            "Add --confirm to actually mutate the catalog."
        )
    return will_act


def _run_recount(report: dict, *, will_act: bool, dry_run: bool) -> None:
    targets = report["zero_count_recount"]
    click.echo(f"\nrecount: {len(targets)} candidate(s) (manifest non-empty despite chunk_count == 0).")
    _echo_sample(targets, _CAP_ACTION, _label_zero_count)
    if not will_act:
        if dry_run:
            click.echo("\n(dry-run — no catalog writes performed.)")
        return

    from nexus.commands import catalog as _cat_cmd  # noqa: PLC0415 — module-routed helper access keeps import acyclic + monkeypatch-visible
    writer = _cat_cmd._get_catalog_writer()
    n = 0
    failures: list[dict] = []
    try:
        total = len(targets)
        for i, row in enumerate(targets, start=1):
            try:
                writer.resync_chunk_count_cache(row["tumbler"])
                n += 1
            except Exception as exc:  # noqa: BLE001 — isolated per-doc: continue, report at end
                failures.append({"tumbler": row["tumbler"], "error": str(exc)})
                _log.warning("reconcile_stale_recount_failed", tumbler=row["tumbler"], error=str(exc))
            if i % _PROGRESS_EVERY == 0:
                click.echo(f"  ... {i}/{total} processed")
    finally:
        writer.close()

    click.echo(f"\nDone: resynced {n} document(s).")
    click.echo(
        "Note: this restores chunk_count only, not verified content — a doc whose manifest "
        "chashes are themselves missing from T3 gets \"fixed\" to a number that still isn't "
        "retrievable. Run `nx catalog verify` afterward to confirm."
    )
    _log.info("reconcile_stale_recount_recommend_verify", resynced=n, failed=len(failures))

    if failures:
        click.echo(f"\n{len(failures)} failure(s) (resync raised; doc left at its prior chunk_count):")
        _echo_sample(failures, _CAP_INFO, lambda r: f"{r['tumbler']}: {r['error']}")
        raise click.exceptions.Exit(1)


def _run_tombstone(report: dict, class_key: str, *, will_act: bool, dry_run: bool, verb: str) -> None:
    targets, invariant_skipped = _assert_empty_manifest(report[class_key])
    click.echo(f"\n{verb}: {len(targets)} candidate(s) (empty manifest).")
    _echo_sample(targets, _CAP_ACTION, _label_vanished if class_key.startswith("vanished") else _label_zero_count)
    if invariant_skipped:
        click.echo(
            f"  skipped {len(invariant_skipped)} whose manifest was non-empty "
            "(classification invariant re-check; never auto-tombstoned)."
        )
    if not will_act:
        if dry_run:
            click.echo("\n(dry-run — no catalog writes performed.)")
        return

    from nexus.commands import catalog as _cat_cmd  # noqa: PLC0415 — module-routed helper access keeps import acyclic + monkeypatch-visible
    writer = _cat_cmd._get_catalog_writer()
    try:
        tumblers = [Tumbler.parse(row["tumbler"]) for row in targets]
        # nexus-i711w: the catalog writer is service-only-always
        # (factory.py's _is_catalog_service_mode() is a hardcoded True with
        # no local-mode leg left to fall back to) — delete_many is always
        # available on the service writer, so there is no daemon-mode
        # per-doc delete_document path to preserve here.
        n_deleted = len(writer.delete_many(tumblers))
    finally:
        writer.close()
    click.echo(f"\nDone: tombstoned {n_deleted} catalog entr{'y' if n_deleted == 1 else 'ies'}.")


@click.command("reconcile-stale")
@click.option(
    "--execute", "action",
    type=click.Choice(_ACTIONS),
    default=None,
    help="Mutation arm to run after the classification report. Omit for a "
         "pure read-only census.",
)
@click.option(
    "--dry-run/--no-dry-run", default=True,
    help="Report-only (default). Use --no-dry-run (with --confirm) to mutate.",
)
@click.option(
    "--confirm", is_flag=True, default=False,
    help="Required alongside --no-dry-run and --execute to actually mutate the catalog.",
)
@click.option(
    "--json", "json_out", is_flag=True, default=False,
    help="Emit the full structured classification as JSON on stdout "
         "(diagnostics still go to stderr). Cannot be combined with --execute.",
)
def reconcile_stale_cmd(action: str | None, dry_run: bool, confirm: bool, json_out: bool) -> None:
    """Classify + optionally repair catalog docs with unreliable chunk_count/manifest state.

    nexus-cdypx: 61.2% of production catalog docs carry ``chunk_count == 0``;
    catalog-aware routing ranks over a corpus where most docs have no
    retrievable content. Default (no ``--execute``) is a read-only census —
    it constructs NO catalog writer. Exit 0 means the report was produced;
    a nonzero exit (the shared INCOMPLETE guard — see the module docstring)
    means part of this classification could not be trusted and none of the
    findings above should be acted on. This is not itself a correctness gate
    over the findings; ``nx catalog verify`` is that gate. See the module
    docstring for the full class taxonomy.

    \\b
    Mutation arms (each prints the classification report first, then its
    own target list, then acts only with --no-dry-run --confirm):
      recount               resync chunk_count for zero-count docs whose
                             manifest is actually non-empty. Restores the
                             COUNT, not verified content — re-run
                             ``nx catalog verify`` afterward.
      tombstone-vanished     delete zero-manifest docs in vanished
                             collections. Non-empty-manifest vanished docs
                             are NEVER touched by this arm (nexus-3ck2g).
      tombstone-orphaned     delete zero-count docs whose confirmed on-disk
                             location is gone (file itself missing, or the
                             owner's repo_root/worktree itself deleted).
                             Docs whose absence could not be CONFIRMED
                             (no repo_root, malformed tumbler, a non-file
                             source_uri, or no provenance at all) are never
                             in this arm's target set — see
                             ``unresolvable_provenance`` in the report.
      tombstone-zero-content delete zero-count docs whose confirmed on-disk
                             source is verifiably unchunkable by
                             construction (zero-byte, or binary content —
                             nexus-rqsh1): re-indexing can never produce a
                             chunk for these, so re-index is never the
                             correct remedy, only tombstone.

    \\b
    Examples:
      nx catalog reconcile-stale                                         # census
      nx catalog reconcile-stale --json                                  # CI-friendly
      nx catalog reconcile-stale --execute recount --no-dry-run --confirm
      nx catalog reconcile-stale --execute tombstone-vanished --no-dry-run --confirm
    """
    if json_out and action is not None:
        raise click.ClickException(
            "--json cannot be combined with --execute: the mutation arms print "
            "plain-text action reports that would corrupt the JSON stdout contract. "
            "Run --json alone for the census, then --execute separately."
        )

    from nexus.commands import catalog as _cat_cmd  # noqa: PLC0415 — module-routed helper access keeps import acyclic + monkeypatch-visible

    cat = _cat_cmd._get_catalog()
    t3 = _cat_cmd._make_t3()
    report, unreadable = _classify(cat, t3)

    if json_out:
        click.echo(json.dumps(_json_payload(report, unreadable), indent=2))
    else:
        _echo_human_report(report, unreadable)

    _raise_if_unreadable(unreadable)

    if action is None:
        return

    will_act = _report_only_notice(dry_run, confirm)

    if action == "recount":
        _run_recount(report, will_act=will_act, dry_run=dry_run)
    elif action == "tombstone-vanished":
        _run_tombstone(
            report, "vanished_empty_manifest",
            will_act=will_act, dry_run=dry_run, verb="tombstone-vanished",
        )
    elif action == "tombstone-orphaned":
        _run_tombstone(
            report, "zero_count_orphaned_path",
            will_act=will_act, dry_run=dry_run, verb="tombstone-orphaned",
        )
    elif action == "tombstone-zero-content":
        _run_tombstone(
            report, "zero_count_zero_content_by_design",
            will_act=will_act, dry_run=dry_run, verb="tombstone-zero-content",
        )


def register(group: click.Group) -> None:
    """Attach ``reconcile-stale`` to the shared ``catalog`` group."""
    group.add_command(reconcile_stale_cmd)
