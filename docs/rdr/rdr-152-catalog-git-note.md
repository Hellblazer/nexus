# RDR-152 Decision Record: Catalog git-backing is DROPPED (PG-only)

> Companion to RDR-152 (Postgres + Java Storage Service). Resolves the
> `catalog_git` open question flagged at RDR-152 §Implementation Plan
> Phase 2 step 7 (lines 689–695) BEFORE the `.18` catalog migration bead.
> Bead: `nexus-gmiaf.17` (P2.7a). No implementation — decision record,
> dependency/removal map, capabilities-lost ledger, and `.18`/`.24`
> implications.

## 1. DECISION

**The catalog's git-backed JSONL authority is DROPPED. Postgres becomes the
sole authority for the catalog.** (Option C from the planning prompt — decided
by the user; full decision in T2 `152-catalog-git-DECISION`.)

After RDR-152 there is no JSONL authority, no SQLite projection-cache, and no
git layer for the catalog. `register` / `update` / `link` / `unlink` /
`set_alias` / `dedupe` write directly to the Postgres `catalog` schema (owned by
the Java storage service); reads query Postgres. The on-disk git repo, the
JSONL files, the projection-rebuild machinery, and the clone/pull/push remote
plumbing are all removed.

This matches RDR-152's core thesis: the service owns all storage; clients carry
zero storage libraries. Keeping git would have forced either the Python client
to keep shelling out to `git` (a storage responsibility the migration is
removing from clients) or the Java service to embed a git library and maintain
an on-disk JSONL mirror in parallel with Postgres (the dual-authority bug class
RDR-152 exists to dissolve).

**Why the loss is bounded — the key finding:** git is NOT the catalog's
authority today, and it is NOT a live-on-write dependency (§2). RDR-101 already
moved canonical state to the `events.jsonl` event stream (SQLite is a
projection); RDR-120 P5.A.2 moved the relational side into T2 as a projection.
Git merely wraps the event-log file and commits on a manual/hook trigger.
Dropping git removes nothing currently authoritative — authority is the event
stream, which RDR-152 relocates into PG regardless.

---

## 2. Why git is already vestigial (the basis for cheap removal)

The RDR-049 framing ("git-backed JSONL is authority, SQLite is the cache") has
been overtaken by two later RDRs, and the git commit is not on the write path.

### 2.1 Authority already moved to the event stream → relational projection
- **RDR-101** made `events.jsonl` **canonical**. Per `catalog/AGENTS.md` (Key
  invariants) and `event_log.py`: every mutation flows through
  `Catalog.register/update/link/unlink/set_alias/dedupe`, which emits an event
  into `events.jsonl` first, then **projects** to the relational store. The
  legacy per-class files (`owners/documents/links.jsonl`) "are still written for
  the cutover window but are **no longer canonical**."
- **RDR-120 P5.A.2** moved the catalog SQLite layer into T2; `catalog_db.py` is a
  re-export shim (`CatalogDB = nexus.db.t2.catalog.CatalogStore`). The relational
  side is already a projection, not an independent store.

Authority chain today: **`events.jsonl` (canonical) → projector → relational
projection.** Git wraps `events.jsonl`; it is not the authority.

### 2.2 Git-backing is manual/batch, never live-on-write
- **Mutations do not commit.** `catalog_writes.py` / `catalog_links.py`
  (`register`, `update`, `link`, …) append to the event log + project under a
  directory flock. None call `commit_and_push`.
- **Only two git entry points, both manual verbs:** `nx catalog sync` →
  `Catalog.sync()` → `_git.commit_and_push()` (`commands/catalog.py:1623`,
  `catalog.py:827`); `nx catalog pull` → `Catalog.pull()` →
  `_git.pull_origin_if_remote()` + `rebuild()` (`commands/catalog.py:1732`,
  `catalog.py:844`).
- **One automatic trigger:** the session-close Stop hook
  (`conexus/hooks/scripts/stop_verification_hook.sh:59`) runs
  `nx catalog sync -m "auto-sync at session close"` best-effort. No daemon timer,
  no MCP-lifespan commit, no per-write commit.

### 2.3 Remote sync / multi-machine is optional and OFF by default
- `nx catalog setup` tells the user *"Catalog is local-only — add a git remote
  for durability"* (`commands/catalog.py:333`). The default catalog has **no
  remote**.
- `clone_catalog` runs only on `Catalog.init(remote=...)`;
  `pull_origin_if_remote` is a no-op without a remote (`catalog_git.py:173`).

### 2.4 Disaster recovery does not depend on git
- **Accidental-delete DR** is `catalog_backup.py` (RDR-106): destructive verbs
  snapshot rows to `.deleted-backups/*.jsonl` (gitignored, per-machine) before
  deleting; `nx catalog undelete` restores via the event-sourced API.
  **Independent of git.**
- **Corrupt-projection DR** is `rebuild()` replaying the event log → SQLite. Git
  is not in that loop.

---

## 3. Dependency / removal map

Two facts drive the map: git commit/push is **off the write path** (so removing
it is a no-op for write correctness), and the JSONL/event-log files **are** the
authority-and-projection-source today (so PG must replace BOTH the authority AND
the projection — there is no source/projection split left after migration).

### 3.1 DELETED at Phase 4 (`.24/.25`, daemon/SQLite decommission)

| Artifact | Why it goes |
|---|---|
| `catalog/catalog_git.py` (entire module) | `run_git`, `ensure_git_identity`, `clone_catalog`, `init_repo`, `add_remote_origin_if_missing`, `commit_and_push`, `pull_origin_if_remote`. No caller once `sync`/`pull` go. |
| `Catalog.sync()` / `Catalog.pull()` (`catalog.py:827`, `:844`) | Sole callers of the git plumbing. |
| `nx catalog sync` / `nx catalog pull` verbs (`commands/catalog.py:1617`, `:1727`) | Their bodies are git commit-push / clone-rebuild. |
| `--remote` on `nx catalog init`/`setup` (`commands/catalog.py:231`, `:242`) + the `Catalog.init(remote=...)` clone branch (`catalog.py:786`) | Git-remote onboarding/restore; no PG client equivalent. |
| Session-close auto-sync line (`stop_verification_hook.sh:59`) | No git to commit. |
| JSONL **write** machinery: `_append_jsonl` + the legacy dual-write branches in `catalog_writes.py` / `catalog_links.py` (`cat._append_jsonl(cat._documents_path/_links_path, …)`) | PG is authority; nothing appends JSONL. |
| `event_log.py` JSONL writer + `_write_to_event_log` + the `events.jsonl` on-disk format | The event log becomes a PG `catalog` table (§3.3). The on-disk JSONL log is removed. |
| `catalog_sync.py` rebuild machinery: `_ensure_consistent` five-way dispatch, offset/header-hash checkpoint, `read_documents/read_links/read_owners` replay, the `.gitignore`d `.catalog.db` regeneration, `defrag`/`compact`, `_should_compact`/`_defrag_unlocked` | No JSONL to project from, no SQLite cache to rebuild. Reads hit PG. |
| Catalog directory flock (`_acquire_lock`/`_release_lock`) **as a git/JSONL-append guard** | Concurrency moves to PG (MVCC + RLS). Verify no non-git caller remains before deletion. |
| Docs: `docs/catalog.md` §"Storage layout"/§"Durability and remote sync" (l.276–349), `getting-started.md:201-208`, `cli-reference.md:519-572` | Document removed verbs/workflows. |

### 3.2 `catalog_backup.py` — KEEP, REPOINT to PG (not a git/JSONL export)

`catalog_backup.py` is **independent of git and of JSONL authority**. It is the
RDR-106 backup-before-delete safety net: destructive verbs snapshot
about-to-be-deleted rows to out-of-tree `.deleted-backups/*.jsonl` (per-machine,
gitignored); `nx catalog undelete` re-registers them.

- It is **NOT** the git/JSONL export → do **NOT** delete it.
- **Repoint at `.18`:** `snapshot_documents`/`snapshot_links` read via
  `catalog._db.execute(SELECT …)` → must read from PG.
  `restore_documents` writes via `_write_to_event_log` + `_projector.apply` under
  the directory flock → must re-route to the PG write path (emit-event-to-PG; no
  flock, no JSONL). The backup *files* can remain local JSONL artifacts (a
  convenient portable dump format); only the read-source and restore-sink change.
- Net: keep the capability, swap the two storage seams.

### 3.3 What `.18` (catalog → PG) MUST do

1. **PG is authority from line one of `.18`.** No JSONL-authority/PG-projection
   split, no transitional dual-write to JSONL, no `catalog_git` on the write
   path. Migration target: `documents` (tumbler tree), `links` graph, `spans`,
   `document_chunks` manifest (RDR-108) — in the PG `catalog` schema,
   tenant-scoped via RDR-152's `owner_id` sub-scope + RLS from the first
   changeset.
2. **One-shot seed, not ongoing sync.** Read the current event log (or the live
   SQLite projection — equivalent under RDR-101 replay-equality) **once** to seed
   PG. After seed, the JSONL/git repo is dead state; no further reads. Apply the
   per-store **write-quiesce → import → flip** cutover (bounded quiesce window,
   not silent loss).
3. **Event log → PG `catalog` event table.** Preserve RDR-101's event-sourced
   invariant *in form*: an append-only PG event table is canonical, the
   document/link/span tables are projected from it inside the service (port the
   projector to read PG rows instead of `events.jsonl`). Keeps mutation history +
   replay without git.
4. **Tighten soft FKs.** `topic_links` (taxonomy) → catalog and other soft refs
   become real cross-schema FKs once catalog lands (RDR-152 step 7).
5. **Catalog FTS:** the SQLite-FTS5 → Postgres `tsvector`/GIN contract applies;
   the §Phase-2 FTS parity gate (top-K set equality + Spearman ≥ 0.90) covers
   catalog title/text search at the `.18` gate.

### 3.4 What `.24/.25` (Phase 4 decommission) MUST do
Physically delete every artifact in §3.1 once the flag is removed and rollback
is no longer offered, and update the three doc files.

---

## 4. Capabilities LOST (on record; user has accepted)

| Capability (RDR-049) | Status under PG-only | Replacement |
|---|---|---|
| Git-versioned **history / diff / revert** of the catalog (`git log`/`diff`, "roll back a bad change with git tools" — `docs/catalog.md:290`) | LOST as git | PARTIAL — the RDR-101 event log moves to a PG append-only `catalog` event table → full mutation history + replay survives. Lost: the **git UX** (`git log`/`git diff`/`git revert`, human-diffable JSONL). History becomes a SQL query over the event table, not a git command. |
| **Disaster recovery via git remote** ("survives disk loss" — `getting-started.md:201`, `docs/catalog.md:323`) | LOST as git-remote DR | REPLACED — PG backups / `pg_dump` logical export. RDR-152 already owns PG durability for all eleven T2 stores; catalog folds into the same regime. Arguably better (one backup story, not git-per-store). |
| Catalog **audit / provenance archaeology** | PRESERVED | PG `catalog` event table (canonical, append-only) + `link_query` (all-links-incl-orphans) view. |
| **Accidental-delete recovery** | PRESERVED | `catalog_backup.py` repointed to PG (§3.2). |
| **Multi-machine / new-machine sync via `git clone`** (`nx catalog setup --remote <url>` clones the full registry "instantly" — `docs/catalog.md:347`, `cli-reference.md:524`) | LOST | **SEE LOAD-BEARING FLAG (§4.1).** No like-for-like client-side replacement under a *local* PG model. |

### 4.1 Genuinely load-bearing lost capability — FLAGGED for user

**Multi-machine catalog sharing / new-machine restore via `git clone`.** The
docs actively instruct cloud-mode users to add a git remote and document
`nx catalog setup --remote <url>` as the new-machine restore path that "restores
your tumblers, links, and full document registry instantly"
(`docs/catalog.md:347`, `getting-started.md:208`, `cli-reference.md:524`). This
is the one removed capability with **no automatic equivalent** under a *local*
PG-only model.

- **Likely fine (and probably superseded):** RDR-152 centralizes storage in one
  Java service backed by one Postgres. If that Postgres is a **shared / networked
  endpoint** (the natural deployment for "service owns storage, thin clients"),
  every machine that connects sees the same catalog automatically — strictly
  better than the git-clone-snapshot model. "New-machine restore" reduces to
  "point the client at the service." Under that deployment the git-clone loss is
  **not** load-bearing; it is superseded.
- **The risk window:** if RDR-152's PG is deployed **per-machine / local-only**
  (each machine its own Postgres — and RDR-152 §Technical Design's honest v1
  framing is "nx-managed local Postgres, single workspace"), then there is
  genuinely no cross-machine catalog sharing or restore anymore — a real
  regression for the documented cloud-mode multi-machine workflow.

**The one thing for the user to confirm:** does RDR-152's Postgres deployment
give multiple machines a shared DB endpoint? If yes → replaced-and-improved,
drop git freely. If local-only → the user is knowingly accepting the loss of
cross-machine catalog sync/restore (acceptable if no current workflow has two
machines sharing one catalog, but it should be a conscious "yes"). The prior T2
decision `152-cloud-locality-scope` (cloud-move locality, 2026-06-06/07) is the
governing context; if it already pins the deployment-locality answer, this flag
is resolved there.

---

## 5. Reversibility

This note is a **reversible record, not a load-bearing lock — with one caveat.**

- The **direction** (PG-only, git dropped) is a firm user decision.
- **Mechanism details are advisory** and `.18`/`.24` may adjust them: the exact
  shape of the PG event table, whether backup files stay JSONL or move to
  `COPY`, deletion vs. repoint ordering of verbs.
- **The one item NOT freely reversible later:** the multi-machine-sync loss in
  the local-PG deployment case. Re-adding a git mirror *after* the JSONL write
  path is deleted is a non-trivial rebuild, not a flag flip — so the §4.1
  deployment-locality question should be settled **before `.18` quiesces the
  catalog**, per the orchestrator's stated "surface to user before `.18`" step.

---

## 6. Actionable output (the deliverable)

- **`.18`:** PG authority from line one; one-shot seed from the current event log
  /projection; event log → PG `catalog` event table; NO JSONL write, NO
  `catalog_git`, NO projection-rebuild machinery; repoint `catalog_backup.py`
  read-source + restore-sink to PG (keep the verb); tighten soft FKs; catalog FTS
  under the Phase-2 parity gate.
- **`.24/.25`:** physically delete `catalog_git.py`, `Catalog.sync`/`pull`,
  `nx catalog sync`/`pull`, `--remote` onboarding, `event_log.py` JSONL writer +
  `_append_jsonl` dual-write, `catalog_sync.py` rebuild/defrag/compact, the
  directory flock (verify no non-git callers), the session-close auto-sync hook
  line; update `docs/catalog.md` / `getting-started.md` / `cli-reference.md`.
- **One user confirmation outstanding:** multi-machine catalog sharing/restore is
  the sole load-bearing lost capability; its fate hinges on whether RDR-152's
  Postgres is a shared endpoint (replaced-and-improved) or local-only
  (knowingly-accepted regression).

---

## References

- RDR-152 §Implementation Plan Phase 2 step 7 (the open question, l.689–695);
  §Technical Design v1-tenancy/local-Postgres framing.
- RDR-049 (`docs/rdr/rdr-049-git-backed-catalog.md`) — original git-backing
  rationale (superseded on the authority claim by RDR-101/120).
- RDR-101 — event log canonical, SQLite as projection (`catalog/AGENTS.md`;
  `catalog/event_log.py`).
- RDR-120 P5.A.2 — catalog SQLite moved to T2 (`catalog/catalog_db.py` shim).
- RDR-106 Option A — `catalog/catalog_backup.py` delete-recovery (git-independent).
- Code: `catalog/catalog_git.py`; `catalog/catalog.py:775–847` (init/sync/pull);
  `commands/catalog.py:231–340,1617–1735` (setup/sync/pull verbs);
  `conexus/hooks/scripts/stop_verification_hook.sh:59` (session-close auto-sync);
  `catalog/catalog_writes.py` + `catalog/catalog_links.py` (`_append_jsonl`
  dual-write); `catalog/catalog_sync.py` (rebuild/defrag/compact);
  `catalog/catalog_backup.py` (RDR-106 delete-recovery).
- Docs documenting the lost workflows: `docs/catalog.md:276–349`,
  `getting-started.md:201–208`, `cli-reference.md:519–572`.
