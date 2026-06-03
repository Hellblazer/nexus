# SPDX-License-Identifier: AGPL-3.0-or-later
"""Link-graph operations for the catalog (nexus-mbm extraction 3/5).

Owns the link-table SQL surface and the BFS traversal helpers.
Composed into ``Catalog`` as ``self._links`` (T2Database-style
facade pattern). Catalog's public ``link`` / ``unlink`` /
``links_from`` / ... methods are thin one-line delegates so the
existing public API is unchanged.

Call shapes preserved verbatim for public-API stability:

- :meth:`_LinkOps.link` / :meth:`_LinkOps.link_if_absent` —
  create-or-merge with span validation + dangling-link guard.
- :meth:`_LinkOps.unlink` / :meth:`_LinkOps.bulk_unlink` — single
  and filtered bulk deletion with JSONL tombstones.
- :meth:`_LinkOps.links_from` / :meth:`_LinkOps.links_to` /
  :meth:`_LinkOps.link_query` — directional + composable queries.
- :meth:`_LinkOps.validate_link` — endpoint + duplicate check.
- :meth:`_LinkOps.link_audit` — orphan / duplicate / stale-span /
  stale-chash inventory.
- :meth:`_LinkOps.graph` / :meth:`_LinkOps.graph_many` — BFS
  traversal capped at ``_MAX_GRAPH_DEPTH=10`` and
  ``_MAX_GRAPH_NODES=500``.

The class holds a single ``_cat`` reference back to the parent
``Catalog`` so the SQL connection, JSONL append helper, locks,
and projector all flow through one wire. No separate state
duplication — every operation reads ``self._cat.<...>`` so
single-instance invariants (lock, transaction) are preserved.
"""
from __future__ import annotations

import json
import re
from collections import deque  # kept for any external importers; unused here after nexus-5p2ci.17
from datetime import UTC, datetime
from typing import TYPE_CHECKING

import structlog

# nexus-mbm: ``catalog`` is fully loaded by the time this module is
# imported (the import lives inside ``Catalog.__init__``). Reference
# the patchable module-level helpers (``_cat_mod._SPAN_PATTERN``,
# ``_cat_mod._make_event``) and the graph caps through the module object so
# tests that ``monkeypatch.setattr("nexus.catalog.catalog._FOO",
# ...)`` propagate here. Direct ``from … import _FOO`` would bind to
# the original value at load time and silently defeat the patch.
# Type / dataclass imports (``LinkRecord``, ``_LinkCreatedPayload``,
# ``_LinkDeletedPayload``) stay direct because they are not patched.
from nexus.catalog import catalog as _cat_mod

if TYPE_CHECKING:
    from chromadb.api import ClientAPI

    from nexus.catalog.catalog import Catalog, CatalogLink
    from nexus.catalog.tumbler import Tumbler

_log = structlog.get_logger(__name__)


_MAX_GRAPH_DEPTH: int = 10
_MAX_GRAPH_NODES: int = 500


# nexus-6ppk: link types whose precision is too low for default
# graph traversal. ``implements-heuristic`` is auto-emitted by
# ``index_hook`` whenever a code chunk's auto-extracted symbols match
# an RDR's terminology heuristic; high-traffic generic-infrastructure
# RDRs (rdr-063 t2-domain-split, AST chunking, T1 scratch) accumulate
# 500-660 inbound heuristic edges each, drowning the ~6% of hand-
# curated cites/relates/contains. The 2026-05-08 prod probe found
# 15,490 ``implements-heuristic`` edges (66% of 23,582 total); the
# ``_MAX_GRAPH_NODES`` cap fires correctly but the resulting first-
# 500 set is mostly noise.
#
# Default-exclude from graph traversal; callers wanting the noise
# (auditing / debugging) opt back in via ``include_heuristic=True``.
_HEURISTIC_LINK_TYPES: frozenset[str] = frozenset({
    "implements-heuristic",
})


def _filter_link_types(
    link_types: list[str] | None,
    link_type: str,
    *,
    include_heuristic: bool,
) -> list[str] | None:
    """nexus-6ppk: compute the effective link-type filter for graph
    traversal. When the caller did not specify any types AND
    ``include_heuristic`` is False, returns the full known set minus
    heuristic types so the BFS skips them. Returns the caller's
    explicit list (or single-type list) unmodified when types are
    given. Returns None (no filter) when ``include_heuristic`` is
    True and no types were specified.
    """
    if link_types or link_type:
        # Explicit caller filter wins; trust the caller knows whether
        # they want heuristic edges in the result.
        return link_types or [link_type]
    if include_heuristic:
        # Caller asked for everything explicitly.
        return None
    # Default: known meaningful types except the heuristic class.
    return [
        "cites", "implements", "relates", "contains",
        "supersedes", "describes", "quotes", "comments",
        "formalizes", "same-as",
    ]


class _LinkOps:
    """Composed into ``Catalog`` as ``self._links``.

    Methods access catalog state via ``self._cat.<attr>`` —
    ``_db`` for SQL, ``_acquire_lock`` / ``_release_lock`` for the
    catalog directory flock, ``_event_sourced_enabled`` /
    ``_projector`` / ``_write_to_event_log`` /
    ``_emit_shadow_event`` / ``_append_jsonl`` for the dual-write
    machinery, and ``resolve`` / ``resolve_span`` for endpoint +
    chash validation.
    """

    def __init__(self, catalog: "Catalog") -> None:
        self._cat = catalog

    # ── Internal helpers ────────────────────────────────────────────────────

    def _row_to_link(self, row: tuple) -> "CatalogLink":
        from nexus.catalog.catalog import CatalogLink
        from nexus.catalog.tumbler import Tumbler
        return CatalogLink(
            from_tumbler=Tumbler.parse(row[0]),
            to_tumbler=Tumbler.parse(row[1]),
            link_type=row[2],
            from_span=row[3] or "",
            to_span=row[4] or "",
            created_by=row[5],
            created_at=row[6] or "",
            meta=json.loads(row[7]) if row[7] else {},
        )

    def _link_unlocked(
        self,
        from_t: "Tumbler",
        to_t: "Tumbler",
        link_type: str,
        created_by: str,
        from_span: str,
        to_span: str,
        meta: dict,
        *,
        allow_dangling: bool = False,
    ) -> bool:
        """Core link logic — caller must hold the lock.

        Returns ``True`` if a new row was inserted, ``False`` if an
        existing row was merged (metadata + co_discovered_by fold).
        """
        from nexus.catalog.catalog import LinkRecord, _LinkCreatedPayload
        cat = self._cat

        # Validate span format (Xanadu transclusion addressing)
        for span, label in [(from_span, "from_span"), (to_span, "to_span")]:
            if not _cat_mod._SPAN_PATTERN.match(span):
                raise ValueError(
                    f"invalid {label}: {span!r} — use 'line_start-line_end', "
                    f"'chunk_idx:char_start-char_end', 'chash:<sha256hex>', "
                    f"'chash:<start>-<end>:<sha256hex>', or '' for whole document"
                )
        if not allow_dangling:
            errors = []
            from_entry = cat.resolve(from_t)
            to_entry = cat.resolve(to_t)
            if from_entry is None:
                errors.append(f"from_tumbler {from_t} not found")
            if to_entry is None:
                errors.append(f"to_tumbler {to_t} not found")
            if errors:
                raise ValueError(f"dangling link: {'; '.join(errors)}")
            for span, entry, label in [
                (from_span, from_entry, "from_span"),
                (to_span, to_entry, "to_span"),
            ]:
                if span.startswith("chash:") and entry and entry.physical_collection:
                    try:
                        from nexus.db import make_t3
                        t3 = make_t3()
                        result = cat.resolve_span(
                            span, entry.physical_collection, t3._client,
                        )
                        if result is None:
                            errors.append(
                                f"{label} {span!r} does not resolve in "
                                f"collection {entry.physical_collection}"
                            )
                    except Exception:
                        pass  # T3 unavailable — skip validation
            if errors:
                raise ValueError(f"unresolvable span: {'; '.join(errors)}")
        now = datetime.now(UTC).isoformat()
        row = cat._db.execute(
            "SELECT id, created_by, metadata, created_at FROM links "
            "WHERE from_tumbler=? AND to_tumbler=? AND link_type=?",
            (str(from_t), str(to_t), link_type),
        ).fetchone()

        if row is not None:
            existing_meta = json.loads(row[2]) if row[2] else {}
            existing_meta.update(meta)
            co = existing_meta.get("co_discovered_by", [])
            if created_by != row[1] and created_by not in co:
                co.append(created_by)
            existing_meta["co_discovered_by"] = co
            rec = LinkRecord(
                from_t=str(from_t), to_t=str(to_t), link_type=link_type,
                from_span=from_span, to_span=to_span,
                created_by=row[1], created_at=row[3] or now,
                meta=existing_meta,
            )
            event = _cat_mod._make_event(
                _LinkCreatedPayload(
                    from_doc=str(from_t),
                    to_doc=str(to_t),
                    link_type=link_type,
                    creator=row[1],
                    from_span=from_span,
                    to_span=to_span,
                    created_at=row[3] or now,
                    meta=dict(existing_meta),
                ),
                v=0,
            )
            if cat._event_sourced_enabled:
                # Event-sourced merge: emit the LinkCreated carrying
                # the FINAL merged metadata first, then let the
                # projector's INSERT OR REPLACE overwrite the prior
                # SQLite row with the merged state.
                cat._write_to_event_log(event)
                cat._projector.apply(event)
                cat._db.commit()
                cat._append_jsonl(cat._links_path, rec.__dict__)
            else:
                cat._db.execute(
                    "UPDATE links SET from_span=?, to_span=?, metadata=? "
                    "WHERE id=?",
                    (from_span, to_span, json.dumps(existing_meta), row[0]),
                )
                cat._append_jsonl(cat._links_path, rec.__dict__)
                cat._db.commit()
                cat._emit_shadow_event(event)
            return False
        else:
            combined_meta = dict(meta)
            rec = LinkRecord(
                from_t=str(from_t), to_t=str(to_t), link_type=link_type,
                from_span=from_span, to_span=to_span,
                created_by=created_by, created_at=now, meta=combined_meta,
            )
            event = _cat_mod._make_event(
                _LinkCreatedPayload(
                    from_doc=str(from_t),
                    to_doc=str(to_t),
                    link_type=link_type,
                    creator=created_by,
                    from_span=from_span,
                    to_span=to_span,
                    created_at=now,
                    meta=dict(combined_meta),
                ),
                v=0,
            )
            if cat._event_sourced_enabled:
                cat._write_to_event_log(event)
                cat._projector.apply(event)
                cat._db.commit()
                cat._append_jsonl(cat._links_path, rec.__dict__)
            else:
                cat._db.execute(
                    "INSERT OR IGNORE INTO links "
                    "(from_tumbler, to_tumbler, link_type, from_span, "
                    "to_span, created_by, created_at, metadata) "
                    "VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
                    (str(from_t), str(to_t), link_type, from_span, to_span,
                     created_by, now, json.dumps(combined_meta)),
                )
                cat._append_jsonl(cat._links_path, rec.__dict__)
                cat._db.commit()
                cat._emit_shadow_event(event)
            return True

    # ── Public surface (called via Catalog delegates) ───────────────────────

    def link(
        self,
        from_t: "Tumbler",
        to_t: "Tumbler",
        link_type: str,
        created_by: str,
        *,
        from_span: str = "",
        to_span: str = "",
        allow_dangling: bool = False,
        **meta: object,
    ) -> bool:
        """Create or merge a link. ``True`` if new, ``False`` if merged."""
        cat = self._cat
        dir_fd = cat._acquire_lock()
        try:
            return self._link_unlocked(
                from_t, to_t, link_type, created_by,
                from_span, to_span, dict(meta),
                allow_dangling=allow_dangling,
            )
        finally:
            cat._release_lock(dir_fd)

    def link_if_absent(
        self,
        from_t: "Tumbler",
        to_t: "Tumbler",
        link_type: str,
        created_by: str,
        *,
        from_span: str = "",
        to_span: str = "",
        allow_dangling: bool = False,
        **meta: object,
    ) -> bool:
        """Insert-or-skip link via the UNIQUE constraint.

        Returns ``True`` when the row was created, ``False`` when an
        identical row already existed. No metadata merge, no
        ``co_discovered_by`` fold, no JSONL append on the skip path.
        Raises ``ValueError`` on dangling endpoints (unless
        ``allow_dangling=True``) or malformed spans.
        """
        from nexus.catalog.catalog import LinkRecord, _LinkCreatedPayload
        cat = self._cat

        for span, label in [(from_span, "from_span"), (to_span, "to_span")]:
            if not _cat_mod._SPAN_PATTERN.match(span):
                raise ValueError(
                    f"invalid {label}: {span!r} — use 'line_start-line_end', "
                    f"'chunk_idx:char_start-char_end', 'chash:<sha256hex>', "
                    f"'chash:<start>-<end>:<sha256hex>', or '' for whole document"
                )
        dir_fd = cat._acquire_lock()
        try:
            row = cat._db.execute(
                "SELECT id FROM links WHERE from_tumbler=? AND to_tumbler=? "
                "AND link_type=?",
                (str(from_t), str(to_t), link_type),
            ).fetchone()
            if row is not None:
                return False
            if not allow_dangling:
                errors = []
                from_entry = cat.resolve(from_t)
                to_entry = cat.resolve(to_t)
                if from_entry is None:
                    errors.append(f"from_tumbler {from_t} not found")
                if to_entry is None:
                    errors.append(f"to_tumbler {to_t} not found")
                if errors:
                    raise ValueError(f"dangling link: {'; '.join(errors)}")
                for span, entry, label in [
                    (from_span, from_entry, "from_span"),
                    (to_span, to_entry, "to_span"),
                ]:
                    if span.startswith("chash:") and entry and entry.physical_collection:
                        try:
                            from nexus.db import make_t3
                            t3 = make_t3()
                            result = cat.resolve_span(
                                span, entry.physical_collection, t3._client,
                            )
                            if result is None:
                                errors.append(
                                    f"{label} {span!r} does not resolve in "
                                    f"collection {entry.physical_collection}"
                                )
                        except Exception:
                            pass
                if errors:
                    raise ValueError(f"unresolvable span: {'; '.join(errors)}")
            now = datetime.now(UTC).isoformat()
            combined_meta = dict(meta)
            rec = LinkRecord(
                from_t=str(from_t), to_t=str(to_t), link_type=link_type,
                from_span=from_span, to_span=to_span,
                created_by=created_by, created_at=now, meta=combined_meta,
            )
            event = _cat_mod._make_event(
                _LinkCreatedPayload(
                    from_doc=str(from_t),
                    to_doc=str(to_t),
                    link_type=link_type,
                    creator=created_by,
                    from_span=from_span,
                    to_span=to_span,
                    created_at=now,
                    meta=dict(combined_meta),
                ),
                v=0,
            )
            if cat._event_sourced_enabled:
                cat._write_to_event_log(event)
                cat._projector.apply(event)
                cat._db.commit()
                cat._append_jsonl(cat._links_path, rec.__dict__)
            else:
                cat._db.execute(
                    "INSERT OR IGNORE INTO links "
                    "(from_tumbler, to_tumbler, link_type, from_span, "
                    "to_span, created_by, created_at, metadata) "
                    "VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
                    (str(from_t), str(to_t), link_type, from_span, to_span,
                     created_by, now, json.dumps(combined_meta)),
                )
                cat._append_jsonl(cat._links_path, rec.__dict__)
                cat._db.commit()
                cat._emit_shadow_event(event)
            return True
        finally:
            cat._release_lock(dir_fd)

    def unlink(
        self,
        from_t: "Tumbler",
        to_t: "Tumbler",
        link_type: str = "",
    ) -> int:
        """Delete one or all links between *from_t* and *to_t*.

        ``link_type=""`` removes all link types between the pair.
        Returns the count removed. Tombstones are appended to the
        JSONL audit trail before SQLite commits.
        """
        from nexus.catalog.catalog import _LinkDeletedPayload
        cat = self._cat

        dir_fd = cat._acquire_lock()
        try:
            if link_type:
                rows = cat._db.execute(
                    "SELECT id, link_type, created_by FROM links "
                    "WHERE from_tumbler = ? AND to_tumbler = ? AND link_type = ?",
                    (str(from_t), str(to_t), link_type),
                ).fetchall()
            else:
                rows = cat._db.execute(
                    "SELECT id, link_type, created_by FROM links "
                    "WHERE from_tumbler = ? AND to_tumbler = ?",
                    (str(from_t), str(to_t)),
                ).fetchall()

            for row_id, lt, original_created_by in rows:
                full = cat._db.execute(
                    "SELECT from_span, to_span, metadata FROM links WHERE id = ?",
                    (row_id,),
                ).fetchone()
                tombstone = {
                    "from_t": str(from_t),
                    "to_t": str(to_t),
                    "link_type": lt,
                    "_deleted": True,
                    "from_span": full[0] or "" if full else "",
                    "to_span": full[1] or "" if full else "",
                    "created_by": original_created_by,
                    "created_at": datetime.now(UTC).isoformat(),
                    "meta": json.loads(full[2]) if full and full[2] else {},
                }
                event = _cat_mod._make_event(
                    _LinkDeletedPayload(
                        from_doc=str(from_t),
                        to_doc=str(to_t),
                        link_type=lt,
                        reason="catalog.unlink",
                    ),
                    v=0,
                )
                if cat._event_sourced_enabled:
                    cat._write_to_event_log(event)
                    cat._projector.apply(event)
                    cat._append_jsonl(cat._links_path, tombstone)
                else:
                    cat._append_jsonl(cat._links_path, tombstone)
                    cat._db.execute(
                        "DELETE FROM links WHERE id = ?", (row_id,),
                    )

            cat._db.commit()
            # Shadow-emit one LinkDeleted per removed row AFTER
            # db.commit() so a process crash between the DELETE and
            # the commit cannot leave events.jsonl claiming a
            # deletion SQLite has not yet committed. Skipped when
            # event-sourced is on — the per-row loop above already
            # emitted + applied.
            if not cat._event_sourced_enabled:
                for row_id, lt, original_created_by in rows:
                    cat._emit_shadow_event(_cat_mod._make_event(
                        _LinkDeletedPayload(
                            from_doc=str(from_t),
                            to_doc=str(to_t),
                            link_type=lt,
                            reason="catalog.unlink",
                        ),
                        v=0,
                    ))
            return len(rows)
        finally:
            cat._release_lock(dir_fd)

    def links_from(
        self,
        tumbler: "Tumbler",
        link_type: str = "",
        link_types: list[str] | None = None,
    ) -> list["CatalogLink"]:
        """All outbound links from *tumbler*."""
        cat = self._cat
        sql = (
            "SELECT from_tumbler, to_tumbler, link_type, from_span, to_span, "
            "created_by, created_at, metadata FROM links WHERE from_tumbler = ?"
        )
        params: list[str] = [str(tumbler)]
        effective = link_types or ([link_type] if link_type else [])
        if len(effective) == 1:
            sql += " AND link_type = ?"
            params.append(effective[0])
        elif len(effective) > 1:
            placeholders = ",".join("?" * len(effective))
            sql += f" AND link_type IN ({placeholders})"
            params.extend(effective)
        return [self._row_to_link(r) for r in cat._db.execute(sql, params).fetchall()]

    def links_to(
        self,
        tumbler: "Tumbler",
        link_type: str = "",
        link_types: list[str] | None = None,
    ) -> list["CatalogLink"]:
        """All inbound links to *tumbler*."""
        cat = self._cat
        sql = (
            "SELECT from_tumbler, to_tumbler, link_type, from_span, to_span, "
            "created_by, created_at, metadata FROM links WHERE to_tumbler = ?"
        )
        params: list[str] = [str(tumbler)]
        effective = link_types or ([link_type] if link_type else [])
        if len(effective) == 1:
            sql += " AND link_type = ?"
            params.append(effective[0])
        elif len(effective) > 1:
            placeholders = ",".join("?" * len(effective))
            sql += f" AND link_type IN ({placeholders})"
            params.extend(effective)
        return [self._row_to_link(r) for r in cat._db.execute(sql, params).fetchall()]

    def link_query(
        self,
        from_t: str = "",
        to_t: str = "",
        link_type: str = "",
        created_by: str = "",
        direction: str = "both",
        tumbler: str = "",
        created_at_before: str = "",
        limit: int = 100,
        offset: int = 0,
    ) -> list["CatalogLink"]:
        """Composable link filter. ``limit=0`` means unlimited."""
        cat = self._cat
        conditions: list[str] = []
        params: list[str | int] = []

        if tumbler:
            if direction == "out":
                conditions.append("from_tumbler = ?")
                params.append(tumbler)
            elif direction == "in":
                conditions.append("to_tumbler = ?")
                params.append(tumbler)
            else:
                conditions.append("(from_tumbler = ? OR to_tumbler = ?)")
                params.extend([tumbler, tumbler])
        if from_t:
            conditions.append("from_tumbler = ?")
            params.append(from_t)
        if to_t:
            conditions.append("to_tumbler = ?")
            params.append(to_t)
        if link_type:
            conditions.append("link_type = ?")
            params.append(link_type)
        if created_by:
            conditions.append("created_by = ?")
            params.append(created_by)
        if created_at_before:
            conditions.append("created_at != '' AND created_at < ?")
            params.append(created_at_before)

        sql = (
            "SELECT from_tumbler, to_tumbler, link_type, from_span, to_span, "
            "created_by, created_at, metadata FROM links"
        )
        if conditions:
            sql += " WHERE " + " AND ".join(conditions)
        sql += " LIMIT ? OFFSET ?"
        params.extend([limit if limit > 0 else -1, offset])

        return [self._row_to_link(r) for r in cat._db.execute(sql, params).fetchall()]

    def bulk_unlink(
        self,
        from_t: str = "",
        to_t: str = "",
        link_type: str = "",
        created_by: str = "",
        created_at_before: str = "",
        dry_run: bool = False,
    ) -> int:
        """Filtered bulk delete with JSONL tombstones.

        Requires at least one filter (or ``dry_run=True``). Returns
        the count removed (or, with ``dry_run=True``, the count
        that *would* be removed).
        """
        from nexus.catalog.catalog import _LinkDeletedPayload
        cat = self._cat

        has_filter = any((
            from_t, to_t, link_type, created_by, created_at_before,
        ))
        if not has_filter and not dry_run:
            raise ValueError(
                "bulk_unlink requires at least one filter (or dry_run=True)"
            )

        dir_fd = cat._acquire_lock()
        try:
            matching = self.link_query(
                from_t=from_t, to_t=to_t, link_type=link_type,
                created_by=created_by, created_at_before=created_at_before,
                limit=0,
            )

            if dry_run:
                return len(matching)

            for lnk in matching:
                tombstone = {
                    "from_t": str(lnk.from_tumbler),
                    "to_t": str(lnk.to_tumbler),
                    "link_type": lnk.link_type, "_deleted": True,
                    "from_span": lnk.from_span, "to_span": lnk.to_span,
                    "created_by": lnk.created_by,
                    "created_at": datetime.now(UTC).isoformat(),
                    "meta": lnk.meta,
                }
                event = _cat_mod._make_event(
                    _LinkDeletedPayload(
                        from_doc=str(lnk.from_tumbler),
                        to_doc=str(lnk.to_tumbler),
                        link_type=lnk.link_type,
                        reason="catalog.bulk_unlink",
                    ),
                    v=0,
                )
                if cat._event_sourced_enabled:
                    cat._write_to_event_log(event)
                    cat._projector.apply(event)
                    cat._append_jsonl(cat._links_path, tombstone)
                else:
                    cat._append_jsonl(cat._links_path, tombstone)
                    cat._db.execute(
                        "DELETE FROM links WHERE from_tumbler=? AND "
                        "to_tumbler=? AND link_type=?",
                        (
                            str(lnk.from_tumbler), str(lnk.to_tumbler),
                            lnk.link_type,
                        ),
                    )
            cat._db.commit()
            if not cat._event_sourced_enabled:
                for lnk in matching:
                    cat._emit_shadow_event(_cat_mod._make_event(
                        _LinkDeletedPayload(
                            from_doc=str(lnk.from_tumbler),
                            to_doc=str(lnk.to_tumbler),
                            link_type=lnk.link_type,
                            reason="catalog.bulk_unlink",
                        ),
                        v=0,
                    ))
            return len(matching)
        finally:
            cat._release_lock(dir_fd)

    def validate_link(
        self,
        from_t: "Tumbler",
        to_t: "Tumbler",
        link_type: str,
    ) -> list[str]:
        """Return a list of validation errors (empty = link is valid)."""
        cat = self._cat
        errors: list[str] = []
        if cat.resolve(from_t) is None:
            errors.append(
                f"from_tumbler {from_t} not found in documents",
            )
        if cat.resolve(to_t) is None:
            errors.append(
                f"to_tumbler {to_t} not found in documents",
            )
        row = cat._db.execute(
            "SELECT id FROM links WHERE from_tumbler=? AND to_tumbler=? "
            "AND link_type=?",
            (str(from_t), str(to_t), link_type),
        ).fetchone()
        if row is not None:
            errors.append(
                f"duplicate: link ({from_t}, {to_t}, {link_type!r}) "
                f"already exists",
            )
        return errors

    def link_audit(self, *, t3: "ClientAPI | None" = None) -> dict:
        """Audit the links table.

        When ``t3`` is provided, verifies each ``chash:`` span
        resolves to a chunk in the corresponding ChromaDB
        collection. Pass a raw ``ClientAPI`` (production callers
        use ``t3_db._client``; tests pass an ``EphemeralClient``).
        Returns a dict with ``total``, ``by_type``, ``by_creator``,
        ``orphaned``, ``duplicates``, ``stale_spans``, and
        ``stale_chash`` lists plus their counts.
        """
        from nexus.catalog.tumbler import Tumbler
        cat = self._cat

        total = cat._db.execute(
            "SELECT count(*) FROM links",
        ).fetchone()[0]
        by_type = dict(
            cat._db.execute(
                "SELECT link_type, count(*) FROM links GROUP BY link_type",
            ).fetchall(),
        )
        by_creator = dict(
            cat._db.execute(
                "SELECT created_by, count(*) FROM links GROUP BY created_by",
            ).fetchall(),
        )
        orphan_rows = cat._db.execute(
            "SELECT from_tumbler, to_tumbler, link_type FROM links l "
            "WHERE NOT EXISTS (SELECT 1 FROM documents d "
            "                  WHERE d.tumbler = l.from_tumbler) "
            "   OR NOT EXISTS (SELECT 1 FROM documents d "
            "                  WHERE d.tumbler = l.to_tumbler)",
        ).fetchall()
        orphaned = [
            {"from": r[0], "to": r[1], "type": r[2]} for r in orphan_rows
        ]
        dup_rows = cat._db.execute(
            "SELECT from_tumbler, to_tumbler, link_type, count(*) AS cnt "
            "FROM links GROUP BY from_tumbler, to_tumbler, link_type "
            "HAVING cnt > 1",
        ).fetchall()
        duplicates = [
            {"from": r[0], "to": r[1], "type": r[2], "count": r[3]}
            for r in dup_rows
        ]
        # Stale spans: positional spans pointing to documents
        # re-indexed after link creation. Content-hash spans
        # (chash:) are excluded — they survive re-indexing by
        # design (RDR-053). Stale chash spans are detected
        # separately via T3 verification below. Checks both
        # from_span (joined on from_tumbler) and to_span (joined
        # on to_tumbler). datetime() wraps ensure correct
        # comparison regardless of ISO-8601 padding.
        stale_span_rows = cat._db.execute(
            "SELECT l.from_tumbler, l.to_tumbler, l.link_type, l.created_at, "
            "       d.indexed_at, 'from' AS side "
            "FROM links l "
            "JOIN documents d ON d.tumbler = l.from_tumbler "
            "WHERE (l.from_span IS NOT NULL AND l.from_span != '') "
            "  AND l.from_span NOT LIKE 'chash:%' "
            "  AND datetime(l.created_at) < datetime(d.indexed_at) "
            "UNION ALL "
            "SELECT l.from_tumbler, l.to_tumbler, l.link_type, l.created_at, "
            "       d.indexed_at, 'to' AS side "
            "FROM links l "
            "JOIN documents d ON d.tumbler = l.to_tumbler "
            "WHERE (l.to_span IS NOT NULL AND l.to_span != '') "
            "  AND l.to_span NOT LIKE 'chash:%' "
            "  AND datetime(l.created_at) < datetime(d.indexed_at)",
        ).fetchall()
        stale_spans = [
            {
                "from": r[0], "to": r[1], "type": r[2],
                "link_created": r[3], "doc_reindexed": r[4], "side": r[5],
            }
            for r in stale_span_rows
        ]
        stale_chash: list[dict] = []
        if t3 is not None:
            chash_rows = cat._db.execute(
                "SELECT from_tumbler, to_tumbler, link_type, from_span, to_span "
                "FROM links WHERE from_span LIKE 'chash:%' "
                "OR to_span LIKE 'chash:%'",
            ).fetchall()
            for row in chash_rows:
                from_t, to_t, lt, from_span, to_span = row
                for span, tumbler_str in [
                    (from_span, from_t), (to_span, to_t),
                ]:
                    if not span.startswith("chash:"):
                        continue
                    body = span[len("chash:"):]
                    m_range = re.match(r"^([0-9a-f]{64}):\d+-\d+$", body)
                    chunk_hash = m_range.group(1) if m_range else body
                    entry = cat.resolve(Tumbler.parse(tumbler_str))
                    if entry is None:
                        stale_chash.append({
                            "from": from_t, "to": to_t, "type": lt,
                            "span": span, "reason": "document_deleted",
                        })
                        continue
                    try:
                        col = t3.get_collection(entry.physical_collection)
                        result = col.get(
                            where={"chunk_text_hash": chunk_hash}, include=[],
                        )
                        if not result["ids"]:
                            stale_chash.append({
                                "from": from_t, "to": to_t, "type": lt,
                                "span": span, "reason": "missing",
                            })
                    except Exception as exc:
                        _log.warning(
                            "link_audit_chash_error",
                            tumbler=tumbler_str, span=span,
                            exc_info=True,
                        )
                        stale_chash.append({
                            "from": from_t, "to": to_t, "type": lt,
                            "span": span, "reason": "error",
                            "error": type(exc).__name__,
                        })

        return {
            "total": total,
            "by_type": by_type,
            "by_creator": by_creator,
            "orphaned": orphaned,
            "orphaned_count": len(orphaned),
            "duplicates": duplicates,
            "duplicate_count": len(duplicates),
            "stale_spans": stale_spans,
            "stale_span_count": len(stale_spans),
            "stale_chash": stale_chash,
            "stale_chash_count": len(stale_chash),
        }

    def graph(
        self,
        tumbler: "Tumbler",
        depth: int = 1,
        direction: str = "both",
        link_type: str = "",
        link_types: list[str] | None = None,
        include_heuristic: bool = False,
    ) -> dict:
        """WITH RECURSIVE traversal to *depth*. Capped at
        ``_MAX_GRAPH_DEPTH`` (10) and ``_MAX_GRAPH_NODES`` (500).
        Returns ``{"nodes": [...], "edges": [...]}``.
        ``link_types`` (list) takes precedence over ``link_type``
        (single).

        Caps are read from the ``Catalog`` class attribute
        (``cat._MAX_GRAPH_DEPTH`` / ``_MAX_GRAPH_NODES``) so tests
        that ``patch.object(type(cat), "_MAX_GRAPH_NODES", N)``
        intercept the value used here.

        nexus-6ppk / nexus-5p2ci.17: replaced the Python ``deque``
        BFS (O(N) round-trips — 2 SQL queries per node) with a
        single bounded SQLite ``WITH RECURSIVE`` query that emits
        both node rows and edge rows in one round-trip.  Semantics
        preserved verbatim:

        - Seed always in node set.
        - Depth cap applied before ``_MAX_GRAPH_NODES`` LIMIT so
          lowest-depth nodes survive truncation (ORDER BY min_depth,
          tumbler makes truncation deterministic).
        - ``link_types`` / ``link_type`` / ``include_heuristic``
          resolved via ``_filter_link_types`` (unchanged).
        - Edges from leaf nodes (BFS depth == requested depth) are
          NOT returned; only edges whose "processing" side
          (from_tumbler for out/both, to_tumbler for in/both) is a
          non-leaf are included — matching the original BFS contract.
        - No dangling edges: edge endpoints are always in the
          surviving node set.
        - ``graph_node_limit`` warning fires when len(nodes) >=
          max_nodes (same condition as the former BFS).

        Cycle-safety: SQLite UNION (not UNION ALL) deduplicates
        (tumbler, depth) pairs.  Because depth strictly increases
        each recursive step, cycles generate rows at increasing
        depths until ``r.depth < ?`` (the depth cap) is false for
        every row in the working table.  The outer GROUP BY +
        MIN(depth) collapses to the BFS-minimum depth per tumbler.
        """
        from nexus.catalog.tumbler import Tumbler
        cat = self._cat
        max_depth = getattr(cat, "_MAX_GRAPH_DEPTH", _MAX_GRAPH_DEPTH)
        max_nodes = getattr(cat, "_MAX_GRAPH_NODES", _MAX_GRAPH_NODES)

        depth = min(depth, max_depth)
        effective_types: list[str] = _filter_link_types(
            link_types, link_type,
            include_heuristic=include_heuristic,
        ) or []
        seed_str = str(tumbler)

        # ── Link-type filter fragment (shared by recursive arms + edge query) ──
        if effective_types:
            lt_ph = ",".join("?" * len(effective_types))
            lt_filter = f" AND l.link_type IN ({lt_ph})"
        else:
            lt_filter = ""

        # ── Recursive arms: expand outbound / inbound / both ─────────────────
        # Each arm: SELECT neighbor_tumbler, r.depth + 1
        #           FROM reachable r JOIN links l ON <direction condition>
        #           WHERE r.depth < ? <link_type_filter>
        #
        # UNION (not UNION ALL) deduplicates (tumbler, depth) pairs so the
        # same (tumbler, depth) combination is never re-expanded, bounding the
        # working table to O(max_depth * |V|) rows even in cyclic graphs.
        arms_sql: list[str] = []
        if direction in ("out", "both"):
            arms_sql.append(
                "SELECT l.to_tumbler, r.depth + 1"
                " FROM reachable r JOIN links l ON l.from_tumbler = r.tumbler"
                f" WHERE r.depth < ?{lt_filter}"
            )
        if direction in ("in", "both"):
            arms_sql.append(
                "SELECT l.from_tumbler, r.depth + 1"
                " FROM reachable r JOIN links l ON l.to_tumbler = r.tumbler"
                f" WHERE r.depth < ?{lt_filter}"
            )

        if not arms_sql:
            # Unrecognised direction: return seed node only, no edges.
            node = cat.resolve(Tumbler.parse(seed_str))
            return {"nodes": [node] if node is not None else [], "edges": []}

        recursive_body = " UNION ".join(arms_sql)

        # ── Direction condition for the edge query ────────────────────────────
        # An edge A→B appears in the BFS result when the "processing" side
        # is a non-leaf (min_depth < requested depth):
        #   direction="out"  → from_tumbler (sf) must be non-leaf
        #   direction="in"   → to_tumbler   (st) must be non-leaf
        #   direction="both" → either end non-leaf (fetched from either side)
        if direction == "out":
            dir_cond = "sf.min_depth < ?"
            dir_params: list = [depth]
        elif direction == "in":
            dir_cond = "st.min_depth < ?"
            dir_params = [depth]
        else:  # both
            dir_cond = "(sf.min_depth < ? OR st.min_depth < ?)"
            dir_params = [depth, depth]

        # Edge link-type filter (same effective_types, applied to the
        # links join in the edge sub-query).
        if effective_types:
            lt_ph2 = ",".join("?" * len(effective_types))
            edge_lt_filter = f" AND l.link_type IN ({lt_ph2})"
        else:
            edge_lt_filter = ""

        # ── Combined query: node rows (rtype='N') + edge rows (rtype='E') ─────
        #
        # Both result sets share the same ``surviving`` CTE (the capped,
        # ordered node set) so we need only one SQL round-trip.
        #
        # Node rows:  9 columns — ('N', tumbler, min_depth_str, 6×NULL)
        # Edge rows:  9 columns — ('E', from, to, type, from_span,
        #                           to_span, created_by, created_at, metadata)
        #
        # Edge deduplication: GROUP BY (from_tumbler, to_tumbler, link_type).
        # The links table has a unique (from, to, type) constraint enforced at
        # upsert time, so GROUP BY collapses the "direction=both fetches same
        # edge twice" case without affecting per-column values.
        sql = f"""
WITH RECURSIVE reachable(tumbler, depth) AS (
    SELECT ?, 0
    UNION
    {recursive_body}
),
surviving AS (
    SELECT tumbler, MIN(depth) AS min_depth
    FROM   reachable
    GROUP  BY tumbler
    ORDER  BY min_depth, tumbler
    LIMIT  ?
)
SELECT 'N' AS rtype,
       tumbler        AS c1,
       CAST(min_depth AS TEXT) AS c2,
       NULL AS c3, NULL AS c4, NULL AS c5, NULL AS c6, NULL AS c7, NULL AS c8
FROM   surviving
UNION ALL
SELECT 'E',
       l.from_tumbler, l.to_tumbler, l.link_type,
       l.from_span, l.to_span,
       l.created_by, l.created_at, l.metadata
FROM   links l
JOIN   surviving sf ON l.from_tumbler = sf.tumbler
JOIN   surviving st ON l.to_tumbler   = st.tumbler
WHERE  {dir_cond}{edge_lt_filter}
GROUP  BY l.from_tumbler, l.to_tumbler, l.link_type
"""

        # ── Parameter list ────────────────────────────────────────────────────
        # Order: seed | (depth, *lt_params) × len(arms) | max_nodes |
        #        *dir_params | *lt_params_for_edge_filter
        params: list = [seed_str]
        for _ in arms_sql:
            params.append(depth)           # WHERE r.depth < ?
            params.extend(effective_types)  # link_type IN (...)
        params.append(max_nodes)           # LIMIT ?
        params.extend(dir_params)          # direction condition
        params.extend(effective_types)     # edge link-type filter

        rows = cat._db.execute(sql, params).fetchall()

        # ── Split node rows / edge rows ───────────────────────────────────────
        node_rows: list[tuple[str, int]] = []
        edge_rows: list[tuple] = []
        for r in rows:
            if r[0] == "N":
                node_rows.append((r[1], int(r[2])))
            else:
                edge_rows.append(r)

        # ── Node cap warning (mirrors original BFS check) ─────────────────────
        if len(node_rows) >= max_nodes:
            _log.warning(
                "graph_node_limit",
                tumbler=str(tumbler), visited=len(node_rows),
            )

        # ── Resolve nodes (filter deleted / dangling) ─────────────────────────
        nodes = [cat.resolve(Tumbler.parse(t)) for t, _ in node_rows]
        nodes = [n for n in nodes if n is not None]

        # ── Build edges via _row_to_link ──────────────────────────────────────
        # Edge row layout (indices 1-8 of the query row):
        #   [1] from_tumbler  [2] to_tumbler  [3] link_type
        #   [4] from_span     [5] to_span
        #   [6] created_by    [7] created_at  [8] metadata
        # _row_to_link expects a tuple (from, to, type, from_span, to_span,
        #   created_by, created_at, metadata) — exactly r[1:9].
        all_edges = [self._row_to_link(r[1:9]) for r in edge_rows]

        return {"nodes": nodes, "edges": all_edges}

    def graph_many(
        self,
        seeds: list["Tumbler"],
        depth: int = 1,
        direction: str = "both",
        link_type: str = "",
        link_types: list[str] | None = None,
        include_heuristic: bool = False,
    ) -> dict:
        """BFS from multiple seeds. Thin wrapper over :meth:`graph`
        that dedupes nodes by ``str(tumbler)`` and edges by
        ``(from, to, link_type)``. Drops edges whose endpoints were
        excluded by the node cap so callers iterating
        nodes-then-edges never see dangling references.

        Per-seed traversal goes through ``self._cat.graph`` (not
        ``self.graph`` directly) so tests that ``patch.object(cat,
        "graph", ...)`` intercept the recursive call.
        """
        max_nodes = getattr(
            self._cat, "_MAX_GRAPH_NODES", _MAX_GRAPH_NODES,
        )
        merged_nodes: dict[str, object] = {}
        merged_edges: dict[tuple[str, str, str], object] = {}

        for seed in seeds:
            if len(merged_nodes) >= max_nodes:
                _log.warning(
                    "graph_many_node_limit", visited=len(merged_nodes),
                )
                break
            result = self._cat.graph(
                seed, depth=depth, direction=direction,
                link_type=link_type, link_types=link_types,
                include_heuristic=include_heuristic,
            )
            for node in result.get("nodes") or []:
                if len(merged_nodes) >= max_nodes:
                    _log.debug(
                        "graph_many_node_limit_mid_seed",
                        visited=len(merged_nodes),
                    )
                    break
                key = (
                    str(node.tumbler) if hasattr(node, "tumbler")
                    else str(node)
                )
                if key not in merged_nodes:
                    merged_nodes[key] = node
            for edge in result.get("edges") or []:
                from_key = str(edge.from_tumbler)
                to_key = str(edge.to_tumbler)
                if from_key not in merged_nodes or to_key not in merged_nodes:
                    continue
                edge_key = (from_key, to_key, edge.link_type)
                if edge_key not in merged_edges:
                    merged_edges[edge_key] = edge

        return {
            "nodes": list(merged_nodes.values()),
            "edges": list(merged_edges.values()),
        }
