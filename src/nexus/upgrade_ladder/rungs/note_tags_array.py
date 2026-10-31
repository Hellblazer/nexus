# SPDX-License-Identifier: AGPL-3.0-or-later
# Copyright (c) 2026 Hal Hildebrand. All rights reserved.
"""RDR-236 RF-17 (nexus-6pfgb.38): convert a note's tags in its catalog entry's ``meta`` from the
comma string older clients stored to the JSON array current clients write.

WHY A RUNG. Tags are a set (RF-17): on the catalog entry they are a JSON array, filtered by
containment, never a comma string matched by equality. Every put since nexus-6pfgb.38 writes the
array (:func:`nexus.catalog.store_hook.note_catalog_meta`); notes stored by nexus-6pfgb.5 through
7.89 hold a string. The upgrade ladder (RDR-185) is the standing mechanism for a data transition that
must converge on every install, so the conversion is a rung: ``nx upgrade`` runs it once the engine is
at the pinned tag.

WHAT IT DOES.
  detect   : read-only, and the dry run: the detail names how many notes hold a string. A note is a
             catalog document with ``content_type == "knowledge"`` and no ``file_path`` in a
             ``knowledge__*`` collection (the shape :func:`~nexus.catalog.store_hook.lapsed_note_documents`
             walks); indexed files never carried ``meta.tags``. Not applicable only when no catalog is
             available. Converged when no note holds a string.
  converge : for each such note, re-read it (a put since the listing may have written the array, and
             that put wins), then ``update(meta={"tags": [...]})`` with the string split once
             (:func:`~nexus.catalog.store_hook.split_note_tags`: trimmed, de-duplicated, first-seen
             order; ``""`` becomes ``[]``). The engine MERGES ``meta`` (RF-18), so only the tags key is
             sent. An entry that already holds an array, or no ``tags`` key, is left alone, which is what
             makes a re-run a no-op. Batches of :data:`DEFAULT_BATCH_LIMIT` notes are RESUMABLE.
  verify   : positive signal only. The listing of every ``knowledge__*`` collection SUCCEEDED and shows
             no string. An unreadable catalog is False, with ``verify_detail``.

EXEMPT FROM THE FILL-ONLY RULE, BY DESIGN. The RDR-236 backfill (nexus-6pfgb.10) fills a document key
only where the entry holds none, and never rewrites a value it finds. This conversion is the stated
exception: it rewrites a stored string into an array, because the string IS the old spelling of the
same value. It runs before that backfill, which then writes arrays for the tags it fills.
"""
from __future__ import annotations

from collections.abc import Callable
from typing import Any

import structlog

from nexus.upgrade_ladder.auto_guard import INTERACTIVE_REMEDY, managed_service, refuses_unbounded_read
from nexus.upgrade_ladder.deadline import active_expired
from nexus.upgrade_ladder.protocol import (
    ConvergeOutcome,
    ConvergeResult,
    ProgressReporter,
    RungStatus,
)
from nexus.upgrade_ladder.registry import RUNG_NOTE_TAGS_ARRAY

_log = structlog.get_logger(__name__)

RUNG_NAME = RUNG_NOTE_TAGS_ARRAY

#: Notes rewritten per ``converge`` call before it returns RESUMABLE.
DEFAULT_BATCH_LIMIT = 500

_NOTE_COLLECTION_PREFIX = "knowledge__"


def _default_reader() -> Any:
    from nexus.catalog.factory import make_catalog_reader  # noqa: PLC0415 — deferred: keeps the registry import cheap

    return make_catalog_reader()


def _default_writer() -> Any:
    from nexus.catalog.factory import make_catalog_writer  # noqa: PLC0415 — deferred: keeps the registry import cheap

    return make_catalog_writer()


def _is_note(entry: Any) -> bool:
    return getattr(entry, "content_type", "") == "knowledge" and not getattr(entry, "file_path", "")


def _holds_string(entry: Any) -> bool:
    return isinstance(((getattr(entry, "meta", None) or {}).get("tags")), str)


class NoteTagsArrayRung:
    """The rung. Collaborators are injected for tests: *reader_factory* (``distinct_doc_collections``,
    ``list_by_collection``, ``resolve``, ``close``; ``None`` when no catalog is available) and
    *writer_factory* (``update``, ``close``). Each is called when needed and closed after use."""

    name = RUNG_NAME

    def __init__(
        self,
        *,
        reader_factory: Callable[[], Any] = _default_reader,
        writer_factory: Callable[[], Any] = _default_writer,
        batch_limit: int = DEFAULT_BATCH_LIMIT,
        managed_fn: Callable[[], bool] = managed_service,
    ) -> None:
        self._managed_fn = managed_fn
        self._reader_factory = reader_factory
        self._writer_factory = writer_factory
        self._batch_limit = batch_limit
        self._converted = 0
        self._verify_detail = ""
        self._record_detail = ""

    # ── reading ──────────────────────────────────────────────────────────────

    def _scan(self, reader: Any) -> tuple[int, list[tuple[str, str]]]:
        """``(notes examined, [(tumbler, string)])`` over every ``knowledge__*`` collection. Any
        failed read propagates: a partial listing is not 'no strings'."""
        examined = 0
        pending: list[tuple[str, str]] = []
        for collection in sorted(reader.distinct_doc_collections() or []):
            if not collection.startswith(_NOTE_COLLECTION_PREFIX):
                continue
            for entry in reader.list_by_collection(collection) or []:
                if not _is_note(entry) or (entry.physical_collection or collection) != collection:
                    continue
                examined += 1
                if _holds_string(entry):
                    pending.append((str(entry.tumbler), entry.meta["tags"]))
        return examined, pending

    def _read(self) -> tuple[int, list[tuple[str, str]]] | None:
        """The scan, or ``None`` when no catalog is available."""
        reader = self._reader_factory()
        if reader is None:
            return None
        try:
            return self._scan(reader)
        finally:
            _close(reader)

    # ── detect ───────────────────────────────────────────────────────────────

    def detect(self) -> RungStatus:
        if refuses_unbounded_read(self._managed_fn):
            # nexus-75w5d: the scan lists every note in every knowledge__* collection, on every call,
            # with no completion record or memo to answer from. Unattended against a managed service
            # it reads nothing; an interactive `nx upgrade` (and `nx doctor`) still scans.
            reason = (
                "unattended `nx upgrade --auto` does not list every note of a managed catalog; "
                f"{INTERACTIVE_REMEDY}"
            )
            _log.info("note_tags_rung_skipped", reason=reason)
            return RungStatus(applicable=False, converged=False, pending_detail=reason)
        scanned = self._read()
        if scanned is None:
            reason = "no catalog is available"
            _log.info("note_tags_rung_skipped", reason=reason)
            return RungStatus(applicable=False, converged=False, pending_detail=reason)
        examined, pending = scanned
        if not pending:
            return RungStatus(applicable=True, converged=True)
        return RungStatus(
            applicable=True,
            converged=False,
            pending_detail=(
                f"{len(pending)} note(s) of {examined} hold their tags as a comma string in the "
                f"catalog, to rewrite as a JSON array"
            ),
        )

    # ── converge ─────────────────────────────────────────────────────────────

    def converge(self, report: ProgressReporter) -> ConvergeResult:
        from nexus.catalog.store_hook import split_note_tags  # noqa: PLC0415 — deferred: store_hook has a heavy import graph

        reader = self._reader_factory()
        if reader is None:
            return ConvergeResult(ConvergeOutcome.DEFERRED, detail="no catalog is available")
        writer = None
        try:
            examined, pending = self._scan(reader)
            batch = pending[: self._batch_limit]
            skipped = 0
            for i, (tumbler, _stale) in enumerate(batch):
                if i and active_expired():
                    report.emit("note_tags_rung_deadline", converted=self._converted)
                    return ConvergeResult(
                        ConvergeOutcome.DEFERRED,
                        detail=(
                            f"the `nx upgrade --auto` time limit passed after {self._converted} "
                            f"note(s); the conversion resumes on the next `nx upgrade`"
                        ),
                    )
                # A put since the listing may have written the array: it wins over the stale string.
                fresh = reader.resolve(tumbler)
                if fresh is None or not _holds_string(fresh):
                    skipped += 1
                    continue
                if writer is None:
                    writer = self._writer_factory()
                writer.update(tumbler, meta={"tags": split_note_tags(fresh.meta["tags"])})
                self._converted += 1
            report.emit(
                "note_tags_rung_batch",
                converted=len(batch) - skipped, skipped=skipped, remaining=len(pending) - len(batch),
                examined=examined,
            )
        finally:
            if writer is not None:
                _close(writer)
            _close(reader)
        if len(pending) > len(batch):
            return ConvergeResult(
                ConvergeOutcome.RESUMABLE,
                detail=f"converted {self._converted} note(s); {len(pending) - len(batch)} more",
            )
        return ConvergeResult(ConvergeOutcome.COMPLETED, detail=f"converted {self._converted} note(s)")

    # ── verify ───────────────────────────────────────────────────────────────

    def verify(self) -> bool:
        """True only when the listing of every ``knowledge__*`` collection SUCCEEDED and shows no note
        holding a string. A catalog that is missing or cannot be read is not 'zero strings'
        (nexus-hdumg): it is False, with :meth:`verify_detail`."""
        self._verify_detail = ""
        try:
            scanned = self._read()
        except Exception as exc:  # noqa: BLE001 — an unreadable catalog is unknown, and unknown is not reached
            self._verify_detail = f"could not read the catalog to confirm the conversion: {exc}"
            return False
        if scanned is None:
            self._verify_detail = "cannot confirm the conversion: no catalog is available"
            return False
        examined, pending = scanned
        if pending:
            self._verify_detail = f"{len(pending)} note(s) still hold their tags as a string"
            return False
        self._record_detail = (
            f"examined {examined} note(s), none holds a string; {self._converted} converted this run"
        )
        return True

    def verify_detail(self) -> str:
        return self._verify_detail

    def record_detail(self) -> str:
        return self._record_detail


def _close(obj: Any) -> None:
    close = getattr(obj, "close", None)
    if close is not None:
        try:
            close()
        except Exception:  # noqa: BLE001 — a failed close never fails the conversion
            _log.debug("note_tags_rung_close_failed", exc_info=True)
