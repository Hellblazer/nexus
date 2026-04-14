---
title: "RDR-076: Idempotent Upgrade Mechanism"
status: accepted
type: architecture
priority: P1
created: 2026-04-13
accepted_date: 2026-04-14
reviewed-by: self
---

# RDR-076: Idempotent Upgrade Mechanism

Nexus has a database, config files, a plugin, and a CLI — but no coherent upgrade path. Schema migrations are ad-hoc `ALTER TABLE` calls scattered across four domain stores. There's no version tracking, no `nx upgrade` command, and no way for the plugin to know it's running against a newer or older CLI. Every new feature that touches T2 schema or T3 metadata must invent its own migration. This doesn't scale.

## Problem

### Current state: ad-hoc migrations, no coordination

Each T2 domain store independently detects and applies schema changes on first use:

| Store | Migrations | Detection method |
|---|---|---|
| MemoryStore | FTS rebuild, access tracking columns | `PRAGMA table_info()` |
| PlanLibrary | project column, ttl column | `sqlite_master` + `PRAGMA table_info()` |
| CatalogTaxonomy | topics tables, assigned_by column, review columns | `sqlite_master` + `PRAGMA table_info()` |
| Telemetry | None | `CREATE TABLE IF NOT EXISTS` |

**Problems with this approach:**

1. **No version tracking**: Migrations detect state by probing columns. No record of which version last touched the database. Can't distinguish "never migrated" from "partially migrated".
2. **No ordering guarantee**: Migrations run on first access to each store, not in a defined sequence. Cross-store dependencies can't be expressed.
3. **No rollback**: `ALTER TABLE ADD COLUMN` is irreversible in SQLite. Partial failures leave the database in an unknown state.
4. **No `nx upgrade` command**: Users who upgrade the CLI have no explicit step to run. Migrations fire implicitly and silently.
5. **No plugin-CLI version coordination**: Plugin (`plugin.json` version) and CLI (`pyproject.toml` version) can diverge. Plugin 4.1.2 might call MCP tools that assume CLI 4.2.0 schema. No compatibility check.
6. **T3 has no migration story at all**: ChromaDB collections have `pipeline_version` metadata (RDR-029) for reindex detection, but no general-purpose migration framework.
7. **Config evolution is implicit**: `.nexus.yml` has no version field. New config sections use hardcoded defaults when absent — which works until a default changes.
8. **`nx doctor` doesn't check schemas**: It checks T3 old-layout migration and HNSW tuning, but never validates T2 table schemas.

### What triggers this now

RDR-075 (cross-collection topic projection) needs to retroactively project existing collections on upgrade. RDR-070 (taxonomy) already added 3 migrations to CatalogTaxonomy. Every feature that touches schema will keep adding one-off migrations unless we build the infrastructure once.

## Proposed Design

### Core principle: leverage the existing version, don't reinvent it

The CLI version (`importlib.metadata.version("conexus")`) and the plugin version (`plugin.json`) are already synchronized on every release. This **is** the version. The upgrade mechanism compares the running version against the last-seen version stored in T2, and runs migrations introduced between those two versions.

No separate version tracking file. No parallel numbering scheme. One version, one source of truth.

### Version-gated migration registry

The registry covers **T2 schema migrations only** — `ALTER TABLE`, FTS rebuilds, new tables. T3 operations (ChromaDB backfills, projection, re-indexing) are a separate execution path in `nx upgrade` (see below) because they require a ChromaDB client, not a `sqlite3.Connection`.

```python
# src/nexus/db/migrations.py

@dataclass
class Migration:
    introduced: str   # package version that introduced this migration
    name: str         # human-readable description
    fn: Callable[[sqlite3.Connection], None]  # idempotent, module-level function

# Version tags are historically accurate per git tag archaeology.
# New tables (CREATE TABLE IF NOT EXISTS) belong in base schema, not here.
# Only ALTER TABLE / FTS rebuild / data transforms need registry entries.
MIGRATIONS: list[Migration] = [
    # Tags verified via `git tag --contains <commit>` for each migration commit
    Migration("1.10.0", "Memory FTS rebuild with title",  migrate_memory_fts),
    Migration("2.8.0",  "Add plan project column",        migrate_plan_project),
    Migration("3.7.0",  "Add memory access tracking",     migrate_access_tracking),
    Migration("3.7.0",  "Add topics tables",              migrate_topics),
    Migration("3.8.0",  "Add plan ttl column",            migrate_plan_ttl),
    Migration("4.0.0",  "Add assigned_by column",         migrate_assigned_by),
    Migration("4.0.0",  "Add review columns",             migrate_review_columns),
    # Future migrations append here — verify tag with `git tag --contains <commit>`
]
```

**Migration function extraction**: Existing `_migrate_*_if_needed()` are instance methods on domain stores (access `self.conn`). For the registry, extract them as **module-level private functions** in `src/nexus/db/migrations.py`, each accepting `conn: sqlite3.Connection`. Domain stores then delegate to these module-level functions, preserving the per-domain `_migrated_lock` guard for standalone-store backward compatibility:

```python
# src/nexus/db/migrations.py
def migrate_access_tracking(conn: sqlite3.Connection) -> None:
    cols = {r[1] for r in conn.execute("PRAGMA table_info(memory)").fetchall()}
    if "access_count" in cols:
        return
    conn.execute("ALTER TABLE memory ADD COLUMN access_count INTEGER DEFAULT 0 NOT NULL")
    conn.execute("ALTER TABLE memory ADD COLUMN last_accessed TEXT DEFAULT ''")
    conn.commit()

# src/nexus/db/t2/memory_store.py (unchanged interface, delegates)
def _migrate_access_tracking_if_needed(self) -> None:
    from nexus.db.migrations import migrate_access_tracking
    migrate_access_tracking(self.conn)
```

Version comparison uses `tuple(int(x) for x in ver.split("."))` — no `packaging` dependency needed. Python 3.12 stdlib is sufficient.

**Version tracking table** (in T2 database):
```sql
CREATE TABLE IF NOT EXISTS _nexus_version (
    key     TEXT PRIMARY KEY,
    value   TEXT NOT NULL
);
-- Single row: key='cli_version', value='4.1.2'
```

**Bootstrapping for existing installs**: When `_nexus_version` table is absent and the database already has tables (detected via `SELECT name FROM sqlite_master WHERE type='table' AND name='memory'`), this is an existing install upgrading to the first version with the registry. Seed `_nexus_version` to `PRE_REGISTRY_VERSION` (a constant set to the last release before the registry shipped, e.g., `"4.1.2"`). This prevents spurious re-running of retroactive migrations that already applied. Fresh installs (no existing tables) start at `"0.0.0"` — all migrations run, creating tables and columns from scratch.

**Runner**: `apply_pending(conn, current_version)`:
1. **Create base tables**: Execute all domain base-schema SQL (`_MEMORY_SCHEMA_SQL`, `_PLAN_SCHEMA_SQL`, `_TAXONOMY_SCHEMA_SQL`, `_TELEMETRY_SCHEMA_SQL`) on the transient connection via `CREATE TABLE IF NOT EXISTS`. This ensures migration functions can safely `PRAGMA table_info()` on tables that exist — critical for fresh installs where no store has constructed yet
2. Create `_nexus_version` table if absent
3. **Bootstrap check**: Read `SELECT value FROM _nexus_version WHERE key='cli_version'`. If no row exists AND `memory` table has data (not just created above) → insert `PRE_REGISTRY_VERSION`. If no row exists AND `memory` table is empty → insert `"0.0.0"`. Edge case: empty `_nexus_version` table (deleted row) treated as `"0.0.0"` — safe because all migrations are idempotent
4. Read last-seen version from `_nexus_version`
5. Filter migrations where `introduced > last_seen AND introduced <= current_version`. Version comparison via `_parse_version(ver)` which returns `tuple[int, ...]` — wraps `tuple(int(x) for x in ver.split("."))` with a `try/except ValueError` that falls back to `(0, 0, 0)` for pre-release strings like `"1.0.0rc1"`
6. Execute each (idempotent — `PRAGMA table_info()` guards stay as safety nets inside each function)
7. Update `_nexus_version` to `current_version`

**Process-level fast path**: A new module-level `_upgrade_done: set[str]` in `src/nexus/db/migrations.py` (alongside the `MIGRATIONS` list) is checked **before** opening any connection. If `path_key` is already in the set, skip entirely — no DB read, no overhead. This makes repeated `t2_ctx()` calls in MCP tool handlers (15+ call sites) cost one set lookup, not a DB read. This is distinct from the per-domain `_migrated_paths` sets in each store — those continue to guard standalone store construction.

### `nx upgrade` command

```bash
nx upgrade [--dry-run] [--force] [--auto]
```

**Flags:**
- `--dry-run`: List pending migrations and T3 checks without executing. Exit 0.
- `--force`: Reset version check to `"0.0.0"` and re-run all migrations. Does NOT bypass per-migration idempotency guards (`PRAGMA table_info()` checks still run inside each function) — only the version gate is reset.
- `--auto`: Quiet mode for hook invocation. Run pending T2 migrations only (T3 upgrade steps skipped — cloud connections can exceed hook timeout). Emit structured log only (no interactive output), exit 0 always (even on error — errors logged, not raised). This is what the SessionStart hook calls.

**Execution steps:**

1. Read current CLI version from `importlib.metadata.version("conexus")`
2. Read last-seen version from T2 `_nexus_version` table (with bootstrapping)
3. **T2 schema migrations**: Filter and run pending migrations from `MIGRATIONS` registry
4. **T3 operations** (separate typed interface, requires ChromaDB client):
   - Pipeline version checks (stale collection detection)
   - Data backfills (e.g., RDR-075 cross-collection projection — requires `T3Database`)
   - New collection metadata
5. Validate config (warn about deprecated keys)
6. Update `_nexus_version` to current
7. Report what changed (unless `--auto`)

T3 operations are executed via a separate `T3UpgradeStep` interface:
```python
@dataclass
class T3UpgradeStep:
    introduced: str
    name: str
    fn: Callable[[T3Database, CatalogTaxonomy], None]

T3_UPGRADES: list[T3UpgradeStep] = [
    T3UpgradeStep("4.2.0", "Backfill cross-collection projection", backfill_projection),
]
```

This separation keeps T2 migrations pure `sqlite3.Connection` and T3 operations properly typed with their required clients.

### Auto-upgrade on startup

**Deliverable**: Insert `nx upgrade --auto` as the **first** hook in the existing SessionStart matcher in `hooks.json`, before `nx hook session-start`:

```json
{
  "matcher": "startup|resume|clear|compact",
  "hooks": [
    {"type": "command", "command": "nx upgrade --auto", "timeout": 30},
    {"type": "command", "command": "nx hook session-start", "timeout": 10},
    ...
  ]
}
```

Timeout is 30 seconds (all values in hooks.json are seconds). `--auto` mode runs T2 migrations only — T3 upgrade steps are **skipped** in `--auto` mode because T3 cloud connections can exceed the hook timeout. T3 upgrades run via explicit `nx upgrade` invocation. The `--auto` flag ensures errors are logged but never block session startup.

### Plugin-CLI version enforcement

On MCP server startup (`core.py:main()`, `catalog.py:main()`), before `mcp.run()`:

```python
from importlib.metadata import version
cli_ver = version("conexus")  # e.g. "4.2.0"
```

Compare `cli_ver` against `_nexus_version` stored value. If they diverge by more than a patch version (i.e., someone rolled back the CLI without the hook running), emit a structured `structlog` warning. Note: `$CLAUDE_PLUGIN_ROOT` is not available in MCP subprocess context, so reading `plugin.json` directly is not feasible. Since CLI and plugin versions are always synchronized on release, the CLI version comparison against the stored version is sufficient.

### Migration connection lifecycle

`T2Database` is a pure facade with no connection of its own. The migration path uses a **transient connection**:

1. `T2Database.__init__()` checks `_upgrade_done` set in `migrations.py` (in-memory, process-level) — if `path_key` present, skip entirely (zero DB overhead on repeated `t2_ctx()` calls)
2. If not in set: open a transient `sqlite3.Connection` to the database file
3. Run `apply_pending(conn, current_version)` — base table creation, bootstrapping, migration execution, version update
4. Close the transient connection
5. Add `path_key` to `_upgrade_done`
6. Construct domain stores normally (each opens its own connection — base tables already exist from step 3)

This avoids five concurrent WAL connections and makes the fast path (all migrations applied, within one process) a pure set lookup.

### `nx doctor --check-schema`

Add schema validation to doctor:
- Compare actual T2 table schemas against expected schemas
- Check `_nexus_version` against CLI version — report pending migrations
- Suggest `nx upgrade` if anything is out of date

## Research Findings

### RF-1: Per-database-file or global migrations? — ANSWERED: Per-file, but effectively global

**Status**: Answered (codebase evidence)

All four domain stores open independent connections to the **same single SQLite file**: `~/.config/nexus/memory.db` (`_helpers.py:6–8`). The `T2Database` facade passes the same `path` to all four constructors (`t2/__init__.py:99–113`). Each store has its own `_migrated_paths: set[str]` keyed by canonical path, so migrations are per-database-file per-domain.

**Recommendation**: Since there's only one database file in practice, the `_nexus_version` table goes in that file. The migration registry is global (one ordered list), not per-domain. Each migration function is a module-level function receiving `conn` and can touch any table. `T2Database.__init__()` opens a transient connection, calls `apply_pending()`, closes it, then constructs domain stores.

### RF-2: How does the plugin detect CLI version at startup? — ANSWERED: Two paths available

**Status**: Answered (codebase evidence)

1. **Python path**: `importlib.metadata.version("conexus")` works from any Python code where the `conexus` package is installed. MCP servers run as subprocesses via entry points (`nx-mcp`, `nx-mcp-catalog` in `pyproject.toml:72–75`), so the package is always importable.

2. **Plugin hook path**: The `SessionStart` hook (`nx/hooks/scripts/session_start_hook.py`) already calls `nx` CLI commands (e.g., `nx memory list`). It could call `nx --version` and compare against `plugin.json` version.

3. **MCP server startup**: `core.py:1151` and `catalog.py:575` both have `main()` functions called before `mcp.run()` — ideal injection point for version check.

**Recommendation**: Check version in MCP `main()` before `mcp.run()`. This catches version mismatch at the earliest point where both plugin and CLI versions are knowable. Use `importlib.metadata.version("conexus")` — no subprocess needed.

### RF-3: Auto-run on first invocation or require explicit `nx upgrade`? — ANSWERED: Auto-run with notification

**Status**: Design recommendation (precedent analysis)

**Existing precedent**: T2 schema migrations already auto-run on first access — users have never needed to run an explicit upgrade command. The `_migrated_paths` pattern is invisible to users. Breaking this expectation by requiring `nx upgrade` would be a regression.

**Existing auto-run infrastructure**: The `SessionStart` hook (`hooks.json:2–28`) runs on `startup|resume|clear|compact`. It already executes `nx hook session-start` and outputs context. Adding `nx upgrade --auto` as a pre-flight step is natural.

**Recommendation**: Auto-run pending migrations on first CLI invocation (preserve current invisible behavior). **Also** provide explicit `nx upgrade` for: `--dry-run` to preview, `--force` to repair, and visibility into what was applied. The SessionStart hook runs `nx upgrade --auto` (quiet mode: structured log, no interactive output, exit 0 always) and surfaces applied migrations to the user as a one-line notification. `--auto` is specified in the command section alongside `--dry-run` and `--force`.

### RF-4: Any NOT NULL without DEFAULT in ALTER TABLE? — ANSWERED: None, all correct

**Status**: Verified (exhaustive audit)

Every `ALTER TABLE ADD COLUMN` in the codebase correctly includes `DEFAULT` for `NOT NULL` columns:

| Migration | Statement | Correct? |
|---|---|---|
| MemoryStore: access_count | `ADD COLUMN access_count INTEGER DEFAULT 0 NOT NULL` | Yes |
| MemoryStore: last_accessed | `ADD COLUMN last_accessed TEXT DEFAULT ''` | Yes (nullable) |
| PlanLibrary: project | `ADD COLUMN project TEXT NOT NULL DEFAULT ''` | Yes |
| PlanLibrary: ttl | `ADD COLUMN ttl INTEGER` | Yes (nullable) |
| CatalogTaxonomy: assigned_by | `ADD COLUMN assigned_by TEXT NOT NULL DEFAULT 'hdbscan'` | Yes |
| CatalogTaxonomy: review_status | `ADD COLUMN review_status TEXT NOT NULL DEFAULT 'pending'` | Yes |
| CatalogTaxonomy: terms | `ADD COLUMN terms TEXT` | Yes (nullable) |

No violations. The migration registry can safely wrap all existing migrations without modification.

### RF-5: Can we consolidate `_init_schema()` into one `apply_pending_migrations()` call? — ANSWERED: Yes, with one constraint

**Status**: Answered (architecture analysis)

**Current flow**: `T2Database.__init__()` constructs four stores sequentially (`t2/__init__.py:108–113`). Each store's constructor calls `_init_schema()` which: (1) creates base tables via `CREATE TABLE IF NOT EXISTS`, (2) runs migrations under `_migrated_lock`.

**Consolidation approach**:
1. `T2Database.__init__()` checks `_migrated_paths` set first (zero-cost fast path)
2. If not in set: open transient connection, run `apply_pending(conn, current_version)`, close connection
3. Construct domain stores normally — each creates base tables via `CREATE TABLE IF NOT EXISTS`
4. Domain stores' `_init_schema()` still call their `_migrate_*_if_needed()` methods, which now delegate to the same module-level functions the registry uses — idempotent either way

**Constraint**: CatalogTaxonomy takes a `MemoryStore` reference (`t2/__init__.py:112`). The transient connection's `apply_pending` step 1 executes all base-schema SQL (including memory tables) via `CREATE TABLE IF NOT EXISTS` before running any ALTER TABLE migrations, satisfying this dependency.

**Backward compatibility**: If someone constructs a domain store standalone (outside `T2Database`), the `_migrate_*_if_needed()` delegation to module-level functions still works — they check column existence via `PRAGMA table_info()` regardless of whether the registry already ran.

### RF-6: We already sync plugin and CLI versions — leverage, don't reinvent — ANSWERED

**Status**: Answered (design simplification)

The release process already synchronizes `pyproject.toml` version, `plugin.json` version, and `marketplace.json` version. `importlib.metadata.version("conexus")` returns the CLI version at runtime. This is the only version that matters.

**What we already have**:
- `pyproject.toml:7` — source of truth (`version = "4.1.2"`)
- `plugin.json:3` — synced on release (`"version": "4.1.2"`)
- `importlib.metadata.version("conexus")` — runtime access from any Python code
- `@click.version_option(package_name="conexus")` — CLI `nx --version`
- `PIPELINE_VERSION = "4"` in `indexer.py` — T3 reindex detection

**What the upgrade mechanism adds**: One `_nexus_version` table in T2 with a single row recording the last-seen CLI version. On startup, compare `importlib.metadata.version("conexus")` against stored value. If newer: run version-gated migrations. If same: no-op. If older (downgrade): warn but don't block.

**What we DON'T add**: No `.cli_version` file. No separate version numbering. No config `version` field. The package version drives everything.

### RF-7: Proven patterns to build on — ANSWERED

**Status**: Answered (pattern audit)

These patterns have survived real usage and should be the foundation of the upgrade mechanism:

| Pattern | Where | Why it's proven |
|---|---|---|
| Per-domain `_migrated_paths` + `_migrated_lock` | All T2 stores | Prevents double ALTER TABLE; tested via `test_t2_concurrency.py` (8-thread concurrent writes) |
| `PRAGMA table_info()` guards | Every migration function | Idempotent re-runs guaranteed — columns checked before ALTER |
| WAL + `busy_timeout=5000` | All T2 stores | Cross-domain writes serialize at SQLite layer, no Python-level deadlocks |
| Atomic credential writes | `config.py:442–469` | `tempfile.mkstemp()` → `os.chmod(0o600)` → `os.replace()` — POSIX atomic |
| Deep-merge config | `config.py:472–506` | Unknown keys silently ignored — forward-compatible by default |
| Catalog JSONL truth | `catalog.py` | SQLite is cache, JSONL is truth. `_ensure_consistent()` rebuilds from source |
| Retry with backoff | `retry.py:53–78` | Exponential 2s→30s, max 5 attempts, only retries known transient errors |
| PPID-chain session adoption | `hooks.py:91–93` | Multi-agent trees share T1 server, disjoint windows isolated |

**Design implication**: The migration registry should use the same `_migrated_lock` single-execution guard, the same `PRAGMA table_info()` idempotency checks, and the same atomic write pattern for version persistence.

### RF-8: Fragile patterns the upgrade mechanism should address — ANSWERED

**Status**: Answered (gap analysis)

| Gap | Impact | Fix in RDR-076 |
|---|---|---|
| `_migrated_paths` is in-process memory | Lost on crash, migrations re-run (safe but slow, no audit trail) | `_nexus_version` table persists state |
| T2 schema not validated by `nx doctor` | Silent failures from missing columns | `nx doctor --check-schema` + `nx upgrade --dry-run` |
| Constructor order dependency (CatalogTaxonomy needs MemoryStore) | Only documented by comment, not enforced | Migration registry runs before store construction |
| Inline post-store hooks, no registry | `taxonomy_assign_hook` + `catalog_auto_link` called manually, silent failures | Out of scope for RDR-076 but noted |
| No plugin-CLI version enforcement | MCP servers accept any version mismatch silently | Version check in MCP `main()` before `mcp.run()` |
| `nx doctor --fix` destructive without preview | No dry-run for HNSW tuning, checkpoint/pipeline cleanup | `nx upgrade --dry-run` sets the standard |

### RF-9: Three orthogonal version axes — don't conflate them — ANSWERED

**Status**: Answered (version audit)

The project has three independent version axes. The upgrade mechanism touches only the first:

| Axis | Constant | What it tracks | When it changes | Upgrade action |
|---|---|---|---|---|
| **Package version** | `pyproject.toml` version | Features, schema, API surface | Every release | T2 schema migrations, data backfills |
| **Pipeline version** | `PIPELINE_VERSION = "4"` in `indexer.py` | Chunking/embedding algorithm | When embeddings change | Re-index (`--force-stale`), not schema migration |
| **Export format** | `FORMAT_VERSION = 1` in `exporter.py` | `.nxexp` file format | When export schema changes | Import compatibility check |

**Key insight**: `PIPELINE_VERSION` and `FORMAT_VERSION` are already well-managed and orthogonal. The upgrade mechanism should NOT subsume them — they serve different purposes (re-indexing vs schema migration vs import compatibility). The `_nexus_version` table tracks only the package version axis.

**Release process already handles synchronization** (`docs/contributing.md:157–298`): 5 files updated in lockstep on every release (`pyproject.toml`, `uv.lock`, both CHANGELOGs, `marketplace.json`, both `plugin.json` files). The upgrade mechanism rides this existing discipline — no new release steps needed.

## Success Criteria

- SC-1: `_nexus_version` table tracks last-seen CLI version (single row, package version string)
- SC-2: Migrations tagged with historically accurate `introduced` version — run when CLI version exceeds last-seen
- SC-3: `nx upgrade` runs pending migrations, reports results. `--dry-run` previews. `--force` resets version gate (not per-migration guards). `--auto` for quiet hook mode
- SC-4: `nx doctor --check-schema` validates T2 schemas and reports version delta
- SC-5: CLI version divergence from stored version produces structured warning on MCP startup
- SC-6: Migration bodies extracted to module-level functions; domain stores delegate to them, preserving `_migrated_lock` for standalone backward compatibility
- SC-7: New T2 features add one `Migration("x.y.z", ...)` entry. New T3 features add one `T3UpgradeStep`. No new patterns needed
- SC-8: `hooks.json` SessionStart chain includes `nx upgrade --auto` as first command; applied migrations surfaced to user
- SC-9: Existing installs bootstrapped to `PRE_REGISTRY_VERSION` (not `"0.0.0"`) — no spurious migration notifications on first upgrade

## Technical Notes

- **One version**: `importlib.metadata.version("conexus")` is the single source of truth — no parallel version schemes. Version comparison via `_parse_version(ver)` helper: `tuple(int(x) for x in ver.split("."))` with `try/except ValueError` fallback to `(0, 0, 0)` for pre-release strings (e.g., `"1.0.0rc1"`). No `packaging` dependency needed
- **T2 registry vs T3 operations**: The `MIGRATIONS` registry is strictly `Callable[[sqlite3.Connection], None]`. T3 backfills (ChromaDB client needed) go in the separate `T3_UPGRADES` list with `Callable[[T3Database, CatalogTaxonomy], None]`. These are two typed interfaces, not a union
- **New tables vs ALTER TABLE**: New tables (`topic_links`, `taxonomy_meta`, etc.) belong in base schema via `CREATE TABLE IF NOT EXISTS` in `_init_schema()`. Only `ALTER TABLE` / FTS rebuild / data transforms need registry entries
- **FTS rebuild is destructive-but-idempotent**: `migrate_memory_fts` drops and recreates the FTS5 virtual table. Unlike column additions, this is a heavier operation. The `PRAGMA table_info()` check for the `title` column in FTS schema guards re-runs — the registry runner does not bypass this
- **`--force` semantics**: Resets the version gate to `"0.0.0"` so all migrations are eligible. Does NOT bypass per-migration idempotency guards (`PRAGMA table_info()` still runs). This means `--force` is safe to run at any time
- SQLite has no transactional DDL for `ALTER TABLE ADD COLUMN` — each migration must be its own transaction
- `_migrated_paths` + `_migrated_lock` pattern (RDR-063) prevents concurrent migration execution per database path — the new registry adds `_upgrade_done: set[str]` in `migrations.py` for the T2Database-level fast path, distinct from per-domain sets in each store
- T3 `PIPELINE_VERSION` in `indexer.py` handles T3 reindex detection separately — orthogonal to the schema migration registry
- Config evolution remains implicit (new keys with defaults) — no config version field needed since config is always forward-compatible
- Related: RDR-029 (pipeline versioning), RDR-063 (T2 domain split), RDR-075 (projection backfill)
