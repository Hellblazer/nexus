# RDR-152 Design Note: Catalog git-backing after the Postgres migration

> Companion to RDR-152 (Postgres + Java Storage Service). Resolves the
> `catalog_git` open question flagged at RDR-152 §Implementation Plan
> Phase 2 step 7 (lines 689–695) BEFORE the `.18` catalog migration bead.
> Bead: `nexus-gmiaf.17` (P2.7a). No implementation — analysis, options,
> recommendation, and `.18` implications.

## TL;DR

**Recommendation: Option B — Postgres becomes the catalog authority; git is
demoted to a downstream, read-only EXPORT artifact (versioned history +
optional remote backup/portability).** This is the only option that both
honors RDR-152's "service owns all storage / client holds zero storage libs /
one enforceable boundary" thesis AND preserves the genuinely-valuable
git features. Option A (keep git as authority) re-admits the exact
two-storage-owner bug class RDR-152 exists to dissolve. Option C (drop git)
deletes a real-if-lightly-used feature at the hardest store's migration.

**Reversibility:** the *authority direction* (PG-authoritative, git-as-export)
is a **load-bearing lock** — it determines the catalog write path and whether a
second storage owner is re-introduced, so it must be settled before `.18`. The
*export mechanism details* (jgit vs git-shell-out, manual-only vs timer trigger,
which JSONL files to emit, whether the import/`pull` half ships in `.18` or
Phase 5) are **reversible** and can be adjusted during `.18` planning.

---

## 1. What actually depends on the current git-backing

The single most important finding reframes the whole question: **git is NOT the
catalog's authority today, and it is NOT a live-on-write dependency.** The
RDR-049 framing ("git-backed JSONL is authority, SQLite is the cache") has been
overtaken by two later RDRs.

### 1.1 Authority already moved to the event log, projected to a relational store

- **RDR-101** made `events.jsonl` the **canonical** state. Per
  `src/nexus/catalog/AGENTS.md` (Key invariants) and `event_log.py`: every
  mutation flows through `Catalog.register/update/link/unlink/set_alias/dedupe`,
  which emits an event into `events.jsonl` first and then **projects** to the
  relational store. The legacy per-class files (`owners.jsonl`,
  `documents.jsonl`, `links.jsonl`) "are still written for the cutover window
  but are **no longer canonical**."
- **RDR-120 P5.A.2** (nexus-2t7o5) moved the catalog SQLite layer into T2 as the
  eighth domain store. `src/nexus/catalog/catalog_db.py` is now just a
  re-export shim: `CatalogDB = nexus.db.t2.catalog.CatalogStore`. The relational
  side is already a T2 projection, not an independent store.

So the authority chain today is: **`events.jsonl` (canonical append-only log) →
projector → relational projection.** Git wraps the `events.jsonl` file; it is
*not* the authority itself. This is decisive for costing Options B/C: removing
git-as-authority removes nothing that is currently authoritative, because the
authority is the event stream, which RDR-152 moves into PG regardless.

### 1.2 Git-backing is a manual, batch operation — never live-on-write

Grounded in the code:

- **Mutations do not commit.** `catalog_writes.py` (`register`, `update`,
  `link`, …) append to `events.jsonl` + project under a directory flock. None of
  them call `commit_and_push`. Verified: no `commit_and_push` / `.sync(` call
  sites exist in `catalog_writes.py` or anywhere outside the two CLI verbs below.
- **The only git-commit entry points are two CLI verbs:**
  - `nx catalog sync` → `Catalog.sync()` → `_git.commit_and_push()`
    (`commands/catalog.py:1623`, `catalog.py:827–842`).
  - `nx catalog pull` → `Catalog.pull()` → `_git.pull_origin_if_remote()` +
    `rebuild()` (`commands/catalog.py:1732`, `catalog.py:844–847`).
- **No automatic trigger.** No daemon timer, no MCP lifespan hook, no
  scheduled sync, no auto-commit anywhere. `sync`/`pull`/`compact` are in the
  daemon `CATALOG_WRITE_OPS` whitelist only so that *when a user runs the verb*,
  the git+whole-JSONL maintenance routes through the single writer
  (`daemon/catalog_write_shim.py:47–74`) — not because anything fires them
  automatically.

### 1.3 Remote sync / multi-machine is optional and unconfigured by default

- `nx catalog setup` explicitly tells the user: *"Catalog is local-only — add a
  git remote for durability"* (`commands/catalog.py:333–338`). The default
  catalog has **no remote**.
- `clone_catalog` runs only on `Catalog.init(remote=...)` (new-machine
  bootstrap). `pull_origin_if_remote` is a no-op when no remote is configured
  (`catalog_git.py:173–181`).
- RDR-152's own v1 deployment is **nx-managed local Postgres, single workspace,
  one tenant in practice** (RDR-152 §Technical Design, v1-tenancy honest
  framing). Cross-machine catalog sync via git exists but is **not exercised by
  the v1 target**.

### 1.4 Disaster recovery does not depend on git

- **Accidental-delete DR** is `catalog_backup.py` (RDR-106 Option A): every
  destructive verb snapshots rows to `.deleted-backups/*.jsonl` (gitignored,
  per-machine) before deleting; `nx catalog undelete` restores via the event log.
  This is **independent of git** and stays the real delete-recovery path.
- **Corrupt-projection DR** today is `rebuild()` replaying JSONL → SQLite. Under
  RDR-152 the projection rebuilds by replaying the event stream from PG; git is
  not in that loop.

### 1.5 Net assessment of what git delivers (and its load-bearing-ness)

| Capability | Provided by git today? | Load-bearing? | Survives without live git? |
|---|---|---|---|
| Authority / source of truth | No — authority is `events.jsonl` | **No** | Yes (authority moves to PG) |
| Live-on-write durability | No — commit is manual | No | Yes |
| Audit / mutation history | Partially — git wraps the event log; the **event log itself** is the audit log | The audit *capability* is the event stream, not git | Yes (event rows in PG; export reproduces `git log`/`diff`) |
| Human-facing `git log`/`diff`/`blame` ergonomics | Yes | Low (nice-to-have) | Only via export (Option B) |
| Remote backup | Only if user configures a remote + runs `sync` | Low (opt-in, off by default) | Only via export (Option B) |
| Portability (clone on new machine) | Yes (`init --remote`) | Low (opt-in) | Via export + import/`pull` restore (Option B) |
| Delete recovery | No — that's `catalog_backup.py` | n/a | Yes (unchanged) |

**Conclusion:** git-as-authority is **not load-bearing** — authority already
sits in the event stream, which RDR-152 relocates to PG. Git's residual real
value is (1) diffable/auditable history ergonomics and (2) optional
remote backup/portability. Both are **preservable as a downstream export**
without keeping git as a live storage authority. This makes Option C's loss
bounded and Option B's preservation cheap — and makes Option A's cost
(re-introducing a second live storage owner) clearly unjustified.

---

## 2. The three options — mechanism, projection consistency, failure modes, RDR-152 alignment

### Option A — JSONL-in-git stays AUTHORITY; PG is the projection

- **Mechanism / where git runs.** Catalog writes append `events.jsonl` + `git
  commit` (authority); PG is rebuilt by replaying `events.jsonl`. Git ops run
  either in the Python client (shell-out — but the client must then own a git
  working tree + filesystem write authority, i.e. become a **second storage
  owner**, directly violating RDR-152 Gap 2 and "client holds zero storage
  libs") or in the Java service (shell-out / jgit against a working tree it owns).
- **Projection consistency.** PG must be re-projected from `events.jsonl` on
  every change; the catalog now has two write paths (file + PG) that must be kept
  convergent. Concurrency is still serialized by the **directory flock +
  git-subprocess**, i.e. the exact hand-rolled-serializer class RDR-152 dissolves.
- **Failure modes.** File/PG divergence; flock contention; git-subprocess
  latency/timeout on the hot path; two-substrate consistency bugs.
- **RDR-152 alignment.** **Direct contradiction.** RDR-152 §Decision Rationale
  (lines 461–463): "a half-migration (two storage owners) keeps the boundary
  porous and re-admits the bug class." Option A makes the catalog's true write
  serializer the filesystem+git while every *other* store uses PG MVCC. It keeps
  the catalog outside the single-authority boundary the whole RDR is built to
  establish. **Reject.**

### Option B — PG becomes AUTHORITY; git demoted to downstream EXPORT *(RECOMMENDED)*

- **Mechanism / where git runs.** Catalog writes go to PG like every other store
  (MVCC, RLS, single authority). The canonical mutation history lives as an
  append-only **`catalog.events`** table in PG (the `events.jsonl` event stream,
  relocated). A **service-side export** reads `catalog.events`, writes JSONL to
  the on-disk catalog dir, and runs `git add/commit/(push)`. The export runs in
  the **Java service** (the only component allowed to read canonical rows);
  git plumbing is **jgit** (cleanest under a native-image binary — no subprocess
  dependency) or shell-out to system git (reversible detail — see §4).
  **Trigger** = the existing manual `nx catalog sync` verb, now a thin service
  RPC ("export + commit + push now"). UX is unchanged because sync is *already*
  manual today. An optional service-internal timer can be added later.
- **Projection consistency.** PG is the single authority; the document/link/span
  tables are projected from `catalog.events` **inside PG** (same projector logic,
  reading PG instead of a file). Git is strictly **downstream and read-only with
  respect to authority** — a failed or lagging export never corrupts or blocks a
  PG write.
- **Failure modes.** Export lag (the git mirror trails PG between syncs — same
  staleness as today, where the mirror trails until someone runs `sync`); a push
  failure is non-fatal (already true today, `catalog_git.commit_and_push` logs
  and does not raise). None of these touch authority.
- **RDR-152 alignment.** **Full alignment.** PG is the sole authority and sole
  concurrency substrate; the client holds zero storage libs (sync is an RPC, not
  a client-side git shell-out); git is an export artifact, not a storage owner,
  so the two-owner bug class is not re-admitted. Preserves diffable history,
  optional remote backup, and clone-based portability.

### Option C — PG AUTHORITY; DROP git-backing entirely

- **Mechanism.** PG `pg_dump` / logical export for durability; no git layer.
- **Projection consistency.** Identical to B on the authority/projection side
  (PG-authoritative); simply omits the export.
- **Failure modes.** None new — but a **feature deletion**.
- **RDR-152 alignment.** Aligned with the thesis (single authority), and the
  simplest. **But** it deletes `git log`/`diff` history ergonomics and
  clone-based cross-machine portability. The `catalog.events` PG table still
  provides a queryable, timestamped audit log, so *audit capability* survives —
  what is lost is the human-facing git ergonomics and the opt-in remote-sync
  path. Doing this irreversibly at the **hardest** store's migration, for a user
  who may have a remote configured, risks a silent regression
  (`feedback_unused_not_useless`: dormant ≠ dead).

### Comparison table

| Dimension | A (git authority) | B (PG authority, git export) | C (PG authority, no git) |
|---|---|---|---|
| Single storage authority | No (file + PG) | **Yes (PG)** | **Yes (PG)** |
| Client holds zero storage libs | No (git tree in client/service) | **Yes (sync = RPC)** | **Yes** |
| Concurrency substrate | flock + git (the bug class) | **PG MVCC** | **PG MVCC** |
| Re-admits two-owner bug class | **Yes** | No | No |
| Diffable history (`git log`/`diff`) | Yes | **Yes (via export)** | No |
| Audit log | Yes | Yes (PG `catalog.events` + export) | Yes (PG `catalog.events`) |
| Remote backup / portability | Yes | **Yes (service push / clone-restore)** | No |
| Implementation cost in `.18` | High + wrong | Medium | Low |
| RDR-152 thesis fit | Contradicts | **Best** | Good-but-lossy |

---

## 3. Recommendation

**Adopt Option B.** Rationale:

1. **It removes the actual root cause for the catalog too.** RDR-152 exists to
   replace the flock/single-writer concurrency simulation with PG MVCC. Option A
   keeps the catalog on flock+git as its real serializer — it would leave the
   single most contended store (RDR-146 catalog-starvation forensics) outside the
   fix. B puts the catalog on the same MVCC substrate as everything else.
2. **It keeps the boundary non-porous.** The RDR's central architectural bet is
   "one storage owner, or the bug class comes back." B keeps git strictly
   downstream of authority; A makes git a co-authority (the exact anti-pattern);
   C is non-porous but lossy.
3. **The feature loss of C is real but avoidable for near-zero cost.** Because
   authority already lives in the event stream, exporting `catalog.events` →
   JSONL → git reproduces every git-specific capability that is used today
   (history/diff, remote backup, clone-portability) as a read-only artifact.
   There is no reason to delete a working feature when preserving it is a
   downstream export off a table the migration is creating anyway.
4. **UX is preserved unchanged.** `nx catalog sync` / `nx catalog pull` keep
   their meaning and signatures; they become thin RPCs. Today's users see no
   behavioral change. This also matches the RDR's "thin clients keep surfaces
   stable so MCP/CLI behavior is unchanged per store" incremental-adoption note.

This confirms the direction the RDR itself leaned (line 692–693: "git-backing is
reframed as a Postgres-native export/audit log and the on-disk git mirror becomes
optional"), now with code-grounded justification and the strengthening finding
that **git-as-authority is already vestigial** (RDR-101 + RDR-120 moved authority
to the event stream and the relational side to a projection).

---

## 4. Implications for the `.18` catalog migration

Under Option B, `.18` MUST implement:

1. **Catalog relational tables in the PG `catalog` schema** — `documents`
   (tumbler tree), `links` graph, `spans`, the `document_chunks` manifest
   (RDR-108) — each with `tenant_id` + RLS policy from its first changeset, like
   every other store's migration unit. Tighten the soft FKs from the taxonomy
   (step 4) and aspects (step 5) migrations into real cross-schema FKs once
   catalog lands (already in the RDR's ladder).
2. **`catalog.events` as the canonical, append-only event table in PG.** PG is
   authority; the document/link/span tables are **projected from
   `catalog.events`** inside the service (port the existing projector to read PG
   rows instead of `events.jsonl`). This preserves RDR-101's event-sourced
   invariant inside Postgres.
3. **A service-side git export** (the `sync` op, already whitelisted): read
   `catalog.events` (and, for back-compat consumers, optionally re-emit the
   legacy `owners/documents/links.jsonl` + `events.jsonl`), write to the on-disk
   catalog dir, `git add/commit/(push if remote)`. Runs **in the Java service**.
   Sub-decision (reversible — pick during `.18`): **jgit** (preferred; no
   subprocess, native-image-clean) vs shell-out to system git (requires the
   native-image build to permit subprocess; weigh against S0.4).
4. **Client surface unchanged:** `nx catalog sync` and `nx catalog pull` become
   thin RPCs with identical signatures. `pull`/import (clone-or-pull the git repo
   → load JSONL → `catalog.events` → project) is the **new-machine bootstrap +
   DR-from-remote** path. The export-out half is the MVP; the import-back half
   MAY defer to Phase 5 since v1 is single-machine/single-tenant (reversible).
5. **Idempotent ETL** `events.jsonl` → `catalog.events` PG rows (RDR-076
   idempotent-upgrade heritage), stamped under the default tenant. Keep the
   on-disk git working tree (it is now the export target, not deleted) and apply
   the per-store **write-quiesce/cutover** discipline the RDR mandates for
   catalog (a bounded write-quiesce window, not silent loss).
6. **Decommission alignment:** the Python `catalog_git.py` shell-out is removed
   from the client tree per Gap 2 (no storage/git libs in the Python tree); its
   logic moves into the service (jgit or service-owned git). `catalog_backup.py`
   delete-recovery is **unchanged** and stays the accidental-delete DR path
   (it operates over the public event-sourced API and is orthogonal to the
   substrate move).

`.18` MUST NOT keep flock + git-subprocess as the catalog write serializer
(that is Option A and re-admits the bug class RDR-152 dissolves).

**FTS reminder:** catalog FTS (document title/text search) is on the RDR's
SQLite-FTS5 → Postgres `tsvector`/GIN list; the §Phase-2 FTS parity contract
(top-K set equality + Spearman ≥ 0.90) applies to catalog search queries at the
`.18` gate.

---

## 5. Reversibility verdict

- **Load-bearing lock (decide before `.18`):** *PG is the catalog authority; git
  is a downstream read-only export, not a live write dependency.* This sets the
  catalog write path and prevents a second storage owner; reversing it after
  `.18` ships would require re-architecting the catalog write path. **Surface
  this to the user before `.18` starts** (the orchestrator's stated step).
- **Reversible details (`.18` planning may adjust freely):** jgit vs
  git-shell-out; export trigger (manual-only RPC now vs add a service timer
  later); whether to emit only `events.jsonl` or also the legacy per-class JSONL;
  whether the `pull`/import restore half ships in `.18` or defers to Phase 5.

---

## References

- RDR-152 §Implementation Plan Phase 2 step 7 (the open question, lines 689–695)
  and §Decision Rationale (two-owner porosity, lines 461–463).
- RDR-049 (`docs/rdr/rdr-049-git-backed-catalog.md`) — original git-backing
  rationale (now partially superseded on the authority claim).
- RDR-101 — event log canonical, SQLite as projection
  (`src/nexus/catalog/AGENTS.md` Key invariants; `catalog/event_log.py`).
- RDR-120 P5.A.2 — catalog SQLite moved to T2 (`catalog/catalog_db.py` shim).
- RDR-106 Option A — `catalog/catalog_backup.py` delete-recovery (git-independent).
- Code: `catalog/catalog_git.py`, `catalog/catalog.py:775–847` (init/sync/pull),
  `commands/catalog.py:231–340,1617–1735` (setup/sync/pull verbs),
  `daemon/catalog_write_shim.py:40–74` (sync/pull/compact whitelist rationale),
  `catalog/factory.py` (reader/writer/admin routing).
