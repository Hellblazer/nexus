# SPDX-License-Identifier: AGPL-3.0-or-later
"""Owner registration + owner-table queries (nexus-kgyoz extraction).

Owns the owner write path (``register_owner`` /
``ensure_owner_for_repo`` / ``set_owner_head_hash``) and the
owner-table read surface (``list_owners`` / ``list_owners_by_type``
/ ``get_owner_by_prefix`` / ``owners_with_roots`` /
``curator_owner_tumbler_by_name``).

Composed onto ``Catalog`` as ``self._owners`` (T2Database-style
facade pattern, mirroring ``catalog_links._LinkOps`` /
``catalog_docs._DocumentOps`` / ``catalog_sync._SyncOps``). The
public ``Catalog.register_owner`` / ``ensure_owner_for_repo`` /
``set_owner_head_hash`` / ``list_owners`` / ... methods are thin
one-line delegates so the existing public API is unchanged.

The class holds a single ``_cat`` reference back to the parent
``Catalog`` so the SQL connection, JSONL append helper, locks,
event log, and projector all flow through one wire. No separate
state duplication — every operation reads ``self._cat.<...>`` so
the within-process owner-register lock and the directory flock
both stay single-instance.

Note: ``owner_for_repo`` / ``owner_tumblers_by_name`` are
owner-table queries that deliberately remain in ``_DocumentOps``
because ``register_document`` uses them directly via the facade;
moving them here would introduce a ``_docs`` -> ``_owners``
cross-_Ops dependency that the facade-routing architecture avoids
(nexus-kgyoz deferred — a natural clean-up once the indexer and
commands decomposition PRs land).
"""
from __future__ import annotations

import sqlite3
from pathlib import Path
from typing import TYPE_CHECKING

import structlog

# nexus-kgyoz: ``catalog`` is fully loaded by the time this module is
# imported (the import lives inside ``Catalog.__init__``). Reference
# the patchable module-level helpers (``_cat_mod._make_event``,
# ``_cat_mod._OwnerRegisteredPayload``) through the module object so
# tests that ``monkeypatch.setattr("nexus.catalog.catalog._FOO",
# ...)`` propagate here. Direct ``from … import _FOO`` would bind to
# the original value at load time and silently defeat the patch.
# The ``tumbler`` dataclasses/readers (``OwnerRecord``, ``Tumbler``,
# ``read_owners``) stay direct because they are not patched.
from nexus.catalog import catalog as _cat_mod
from nexus.catalog.tumbler import OwnerRecord, Tumbler, read_owners

if TYPE_CHECKING:
    from nexus.catalog.catalog import Catalog

_log = structlog.get_logger(__name__)


class _OwnerOps:
    """Composed onto ``Catalog`` as ``self._owners``.

    Methods read catalog state via ``self._cat.<attr>`` —
    ``_db`` for SQL, ``_owners_path`` for the canonical owners
    JSONL, ``_owner_register_lock`` / ``_acquire_lock`` /
    ``_release_lock`` for the check-then-register critical section,
    and ``_event_sourced_enabled`` / ``_projector`` /
    ``_write_to_event_log`` / ``_append_jsonl`` / ``_emit_shadow_event``
    for the dual write path.
    """

    def __init__(self, catalog: "Catalog") -> None:
        self._cat = catalog

    def register_owner(
        self, name: str, owner_type: str, *, repo_hash: str = "", description: str = "", repo_root: str = ""
    ) -> Tumbler:
        cat = self._cat
        # nexus-zbne (part of nexus-b34f): owner_type="repo" without a
        # repo_hash is the pathway that produced 83 orphan owners in the
        # live catalog — callers skipped ``owner_for_repo(repo_hash)`` and
        # fell straight through to register_owner(), accumulating one
        # alias per (repo_root, indexing-run) pair. Refuse the call so the
        # invariant is enforced at the API boundary: every repo owner
        # must be keyed by a stable hash that ``owner_for_repo`` can find.
        if owner_type == "repo" and not repo_hash.strip():
            raise ValueError(
                "register_owner(owner_type='repo') requires a non-empty repo_hash. "
                "Use Catalog.owner_for_repo(repo_hash) to look up an existing owner "
                "before falling through to register_owner()."
            )
        if repo_root and not Path(repo_root).is_absolute():
            raise ValueError(f"repo_root must be an absolute path: {repo_root!r}")
        # RDR-137 followup CRITICAL-5 (nexus-43qgm.5): threading lock
        # (within-process) wraps the flock (cross-process) so the
        # check-then-register critical section is atomic against BOTH
        # sibling threads and sibling processes. Lock order is always
        # threading-then-flock to avoid deadlock.
        with cat._owner_register_lock:
            dir_fd = cat._acquire_lock()
            try:
                # RDR-137 followup CRITICAL-5: re-check inside both
                # locks. ensure_owner_for_repo's owner_for_repo() runs
                # OUTSIDE this critical section; a concurrent caller may
                # have registered the same repo_hash in the meantime.
                # The projector's INSERT OR REPLACE would silently
                # replace the first owner's row (deleting its tumbler)
                # rather than raise IntegrityError, so the re-check —
                # not the UNIQUE-index error path — is what guarantees
                # a single stable owner per repo_hash. Curator owners
                # (no repo_hash) are intentionally exempt: name
                # collisions across owner_types are allowed.
                if owner_type == "repo" and repo_hash.strip():
                    existing = cat._docs.owner_for_repo(repo_hash)
                    if existing is not None:
                        return existing
                # Compute next owner number. Under event-sourced mode the
                # events.jsonl is canonical and SQLite is its projection,
                # which means SQLite is consistent with all committed
                # events even after a crash that lost the JSONL append
                # (events.jsonl is written FIRST, SQLite committed second,
                # JSONL appended last). Reading the high-water-mark from
                # JSONL would re-allocate a colliding tumbler in that
                # crash window. Under legacy mode JSONL is canonical, so
                # read from JSONL.
                if cat._event_sourced_enabled:
                    row = cat._db.execute(
                        "SELECT COALESCE(MAX(CAST(SUBSTR(tumbler_prefix, "
                        "INSTR(tumbler_prefix, '.') + 1) AS INTEGER)), 0) "
                        "FROM owners WHERE tumbler_prefix LIKE '1.%'"
                    ).fetchone()
                    next_num = (row[0] or 0) + 1
                else:
                    owners = read_owners(cat._owners_path) if cat._owners_path.exists() else {}
                    next_num = max(
                        (Tumbler.parse(k).owner for k in owners), default=0
                    ) + 1
                prefix = f"1.{next_num}"
                rec = OwnerRecord(
                    owner=prefix,
                    name=name,
                    owner_type=owner_type,
                    repo_hash=repo_hash,
                    description=description,
                    repo_root=repo_root,
                )
                event = _cat_mod._make_event(
                    _cat_mod._OwnerRegisteredPayload(
                        owner_id=prefix,
                        name=name,
                        owner_type=owner_type,
                        repo_root=repo_root,
                        repo_hash=repo_hash,
                        description=description,
                    ),
                    v=0,
                )
                if cat._event_sourced_enabled:
                    # Event-sourced path: events.jsonl first, projector
                    # writes SQLite, legacy JSONL last for back-compat.
                    cat._write_to_event_log(event)
                    cat._projector.apply(event)
                    cat._db.commit()
                    cat._append_jsonl(cat._owners_path, rec.__dict__)
                else:
                    cat._append_jsonl(cat._owners_path, rec.__dict__)
                    # RDR-137 followup CRITICAL-1: COALESCE-preserve head_hash
                    # so the legacy non-event-sourced re-register path doesn't
                    # wipe the column (set_owner_head_hash writes are epsilon-
                    # allowed and live only in SQLite + the JSONL replay layer).
                    cat._db.execute(
                        "INSERT OR REPLACE INTO owners "
                        "(tumbler_prefix, name, owner_type, repo_hash, description, repo_root, head_hash) "
                        "VALUES (?, ?, ?, ?, ?, ?, "
                        "COALESCE((SELECT head_hash FROM owners WHERE name = ? AND owner_type = ?), ''))",
                        (prefix, name, owner_type, repo_hash, description, repo_root, name, owner_type),
                    )
                    cat._db.commit()
                    cat._emit_shadow_event(event)
                return Tumbler.parse(prefix)
            finally:
                cat._release_lock(dir_fd)

    def curator_owner_tumbler_by_name(self, name: str) -> "Tumbler | None":
        """Return the tumbler of the *curator*-type owner with this name, or None.

        The ``(name, owner_type)`` UNIQUE constraint guarantees at most one
        curator owner per name.  Returns ``None`` when no curator owner exists.

        nexus-qnp5s: mirrors the same method on HttpCatalogClient so all
        callers can use a uniform public-API call instead of raw ``_db.execute``.
        """
        cat = self._cat
        row = cat._db.execute(
            "SELECT tumbler_prefix FROM owners WHERE name = ? AND owner_type = 'curator'",
            (name,),
        ).fetchone()
        return Tumbler.parse(row[0]) if row else None

    def ensure_owner_for_repo(
        self, repo: Path, *, repo_name: str = "", description: str = "",
    ) -> Tumbler:
        """Look up or register the owner for ``repo``.

        RDR-103 Phase 4: extracts the owner-registration step from
        :func:`nexus.indexer._catalog_hook` so callers that need the
        owner BEFORE the indexer's hook fires (e.g. ``nx index repo``
        registering the registry entry) can mint it up front. Lookup
        is keyed by ``_repo_identity(repo)`` for stability across
        worktrees.

        Idempotent: existing owners are returned without re-registering.
        ``repo_name`` defaults to the basename returned by
        :func:`nexus.registry._repo_identity`; ``description`` defaults
        to ``"Git repository: {repo_name}"``.
        """
        cat = self._cat
        from nexus.repo_identity import _repo_identity_with_main  # noqa: PLC0415  — circular-dep avoidance (nexus.repo_identity)

        # nexus-zr2ie (RDR-137 gate critique 2026-05-28): use the
        # 3-tuple variant so ``repo_root`` is the canonical main-repo
        # path even when *repo* is a worktree. Pre-fix this wrote
        # ``str(repo)`` and contaminated the catalog on first-run-
        # from-worktree indexing; after worktree deletion the stored
        # path was broken for every relative-path document.
        derived_name, repo_hash, main_repo = _repo_identity_with_main(repo)
        existing = cat.owner_for_repo(repo_hash)
        if existing is not None:
            return existing
        try:
            return cat.register_owner(
                name=repo_name or derived_name,
                owner_type="repo",
                repo_hash=repo_hash,
                repo_root=str(main_repo),
                description=description or f"Git repository: {repo_name or derived_name}",
            )
        except sqlite3.IntegrityError:
            # RDR-137 followup CRITICAL-5 (nexus-43qgm.5): partial
            # UNIQUE on owners.repo_hash trips when two concurrent
            # ensure_owner_for_repo calls both miss the lookup and
            # both attempt to register. The losing thread re-lookups
            # to return the winner's tumbler. Without the catch, the
            # second thread would crash on the duplicate-key error.
            existing = cat.owner_for_repo(repo_hash)
            if existing is not None:
                return existing
            raise

    def set_owner_head_hash(
        self, owner: "Tumbler | str", head_hash: str,
    ) -> int:
        """Persist *head_hash* on the owner row. Returns rowcount.

        RDR-137 Phase 3.8 (nexus-tts0d.13): per-repo git HEAD identity
        moves from ``~/.config/nexus/repos.json`` into the
        ``owners.head_hash`` column (Phase 1.5b, ``nexus-tts0d.2``).
        The indexer calls this after a successful full-index run so the
        next staleness check can compare current HEAD against the
        recorded value.

        RDR-137 followup CRITICAL-1 (nexus-43qgm.1): also appends a
        fresh OwnerRecord to ``owners.jsonl`` so the value survives a
        rebuild from JSONL. The pre-fix path wrote only to SQLite; the
        next rebuild silently wiped the column.

        RDR-137 followup SIG-9 (nexus-43qgm.9): returns ``cursor.rowcount``
        so callers can detect a no-match (e.g. owner concurrently
        deleted between owner_for_repo lookup and set call).

        Direct write rather than event-sourced because head_hash is a
        pure derived signal (one query on the source git tree); no
        replay-equality concerns. See ``§A8-exempt content writes`` at
        the top of :mod:`nexus.db.t2.catalog`.
        """
        cat = self._cat
        owner_str = str(owner)
        cur = cat._db.execute(  # epsilon-allow: derived staleness signal — not an event; the JSONL append below is for rebuild-survival (RDR-137 P3.8 + nexus-43qgm.1), not replay-equality
            "UPDATE owners SET head_hash = ? WHERE tumbler_prefix = ?",
            (head_hash, owner_str),
        )
        cat._db.commit()
        if cur.rowcount > 0:
            # Append a snapshot OwnerRecord to JSONL so rebuild
            # preserves the value (the catalog's rebuild path replays
            # owners.jsonl as last-wins; without this append the most
            # recent head_hash would be lost on the next rebuild).
            #
            # CRITICAL: preserve next_seq from the most-recent existing
            # OwnerRecord. next_seq is JSONL-only state (not in the
            # SQLite owners table); ``register`` reads owners.jsonl
            # last-wins to compute the next document number. If this
            # snapshot defaults next_seq=1 (the dataclass default), the
            # next register() will allocate tumblers starting from 1
            # and reuse already-allocated document slots — REGRESSION
            # uncovered by test_tumblers_stable_across_delete_compact_reindex
            # in the RDR-137 follow-up CI run.
            row = cat._db.execute(
                "SELECT name, owner_type, repo_hash, description, repo_root, head_hash "
                "FROM owners WHERE tumbler_prefix = ?",
                (owner_str,),
            ).fetchone()
            if row is not None:
                # Read current JSONL state to recover next_seq.
                if cat._owners_path.exists():
                    existing = read_owners(cat._owners_path).get(owner_str)
                    preserved_next_seq = (
                        existing.next_seq if existing else 1
                    )
                else:
                    preserved_next_seq = 1
                rec = OwnerRecord(
                    owner=owner_str,
                    name=row[0],
                    owner_type=row[1],
                    repo_hash=row[2] or "",
                    description=row[3] or "",
                    repo_root=row[4] or "",
                    next_seq=preserved_next_seq,
                    head_hash=row[5] or "",
                )
                cat._append_jsonl(cat._owners_path, rec.__dict__)
        return cur.rowcount

    def get_owner_by_prefix(self, tumbler_prefix: str) -> dict | None:
        """Return full owner dict for the given tumbler_prefix, or None.

        nexus-qnp5s: mirrors HttpCatalogClient.get_owner_by_prefix() so
        repos.py head_hash lookup can use a uniform call on both backends.
        """
        cat = self._cat
        row = cat._db.execute(
            "SELECT tumbler_prefix, name, owner_type, repo_hash, "
            "description, repo_root, head_hash "
            "FROM owners WHERE tumbler_prefix = ?",
            (tumbler_prefix,),
        ).fetchone()
        if not row:
            return None
        return {
            "tumbler_prefix": row[0],
            "name": row[1],
            "owner_type": row[2],
            "repo_hash": row[3],
            "description": row[4],
            "repo_root": row[5],
            "head_hash": row[6],
        }

    def list_owners_by_type(self, owner_type: str) -> list[dict]:
        """Return all owners with the given owner_type as a list of dicts.

        nexus-qnp5s: mirrors HttpCatalogClient.list_owners_by_type() so
        repos.py repo-root iteration can use a uniform call on both backends.
        """
        cat = self._cat
        rows = cat._db.execute(
            "SELECT tumbler_prefix, name, owner_type, repo_hash, "
            "description, repo_root, head_hash "
            "FROM owners WHERE owner_type = ?",
            (owner_type,),
        ).fetchall()
        return [
            {
                "tumbler_prefix": r[0],
                "name": r[1],
                "owner_type": r[2],
                "repo_hash": r[3],
                "description": r[4],
                "repo_root": r[5],
                "head_hash": r[6],
            }
            for r in rows
        ]

    def list_owners(self) -> list[dict]:
        """Return all owners for this catalog.

        Backs commands/catalog.py owners_cmd.  Mirrors HttpCatalogClient.list_owners()
        (nexus-xnz0o).  Returns list of dicts with keys: tumbler_prefix, name,
        owner_type, repo_hash, description, repo_root, head_hash.
        """
        cat = self._cat
        rows = cat._db.execute(
            "SELECT tumbler_prefix, name, owner_type, repo_hash, "
            "description, repo_root, head_hash FROM owners"
        ).fetchall()
        return [
            {
                "tumbler_prefix": r[0],
                "name": r[1],
                "owner_type": r[2],
                "repo_hash": r[3],
                "description": r[4],
                "repo_root": r[5] or "",
                "head_hash": r[6],
            }
            for r in rows
        ]

    def owners_with_roots(self) -> dict[str, str]:
        """Return {tumbler_prefix: repo_root} for owners with non-empty repo_root.

        Backs commands/catalog.py prune_stale_cmd.
        Mirrors HttpCatalogClient.owners_with_roots() (nexus-xnz0o).
        """
        cat = self._cat
        rows = cat._db.execute(
            "SELECT tumbler_prefix, repo_root FROM owners WHERE repo_root != ''"
        ).fetchall()
        return {r[0]: r[1] for r in rows}
