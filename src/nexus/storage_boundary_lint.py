# SPDX-License-Identifier: AGPL-3.0-or-later
"""RDR-120 P0.A storage-boundary lint — terminal shape (RDR-186 P4).

AST-scan that catches direct storage opens outside the allowed
daemon-internal substrate. The lint protects the boundary that
RDR-120's daemon design enforces: every consumer must go through the
``T2Database`` / ``T3Database`` facades (which route to the engine over
HTTP since RDR-158 P4).

Banlist (configurable via :data:`BANLIST`):

* ``sqlite3.connect(...)`` plus any aliased form (``import sqlite3 as
  X; X.connect(...)``). Alias resolution is per-file.
* ``voyageai.Client(...)`` (RDR-152 Seam B).

The three ``chromadb.*Client(...)`` entries were REMOVED at RDR-155 P4b P3:
chromadb left pyproject, so those calls cannot resolve at all and the ban was
redundant with reality. The resurrection tripwire is stronger elsewhere —
``tests/test_rdr155_p4b_deletion_gate.py`` bans any chromadb IMPORT anywhere in
the package, not just three call shapes.

Allowlist model (RDR-186 P4, bead nexus-146xx.18 — the census-to-zero
ratchet):

* The per-line ``epsilon-allow`` escape token is RETIRED. Any site could
  self-grant an exemption by writing a comment with a >=8-char reason;
  that machinery is gone. A comment grants NOTHING any more.
* Surviving legitimate sites are enumerated in EXPLICIT NAMED allowlists —
  per-file exact counts with a stated reason next to each entry
  (:data:`SQLITE_CONNECT_ALLOWLIST`, :data:`VOYAGEAI_CLIENT_ALLOWLIST`,
  :data:`T2DATABASE_CONSTRUCTION_ALLOWLIST`,
  :data:`T2_RAW_HANDLE_ALLOWLIST`). Growing an entry (or adding one)
  requires editing THIS file, which is review surface — an explicit Hal
  decision recorded on a bead, never a code comment.
* Surviving sites carry documentation-only comments for greppability:
  ``frozen-source-read`` marks a read-only diagnostic against the frozen
  SQLite migration source; ``boundary-allow`` marks every other named
  survivor. Neither token is parsed by this lint — the named allowlists
  above are the sole enforcement.
* The ``sqlite3.connect`` arm honours NO path-prefix allowlist: SQLite is
  deleted as a storage substrate (NO-SQLITE directive, Hal 2026-07-18; T2
  ``nexus/directive-no-sqlite-pg-everywhere``), so a new connect anywhere —
  including ``db/`` — is a hard violation. The named allowlist holds the
  two read-only frozen-migration-source diagnostics, nothing else
  (down from three at nexus-ay18d — health.py's write-shaped probe was
  ported off SQLite entirely, not just relabelled).
* ``voyageai.Client`` keeps the path-prefix allowlist (``db/`` legitimately
  owns the Voyage EF + T3 embed path) plus the named allowlist for the
  three legacy non-service Phase-4 deletion targets outside it.

Output: a :class:`LintResult` with a structured violation list plus
the catalog-allowlist call-site count metric retained for the RDR-120
phase-boundary forcing function.

Modeled on the AST-walk + offender-aggregation pattern in
``tests/test_no_direct_catalog_writes_outside_projector.py`` (RDR-101
ε-lint precedent).
"""
from __future__ import annotations

import ast
import pathlib
from dataclasses import dataclass, field
from typing import Iterable, Mapping


#: Path-prefix allowlist relative to repo root (POSIX-style). Files
#: under any of these prefixes are exempt from the ``voyageai.Client``
#: arm of the banlist. NOT honoured by the ``sqlite3.connect`` arm
#: (RDR-186 P4: SQLite is deleted as a substrate; the named
#: :data:`SQLITE_CONNECT_ALLOWLIST` is the only escape).
DEFAULT_ALLOWLIST_PREFIXES: tuple[str, ...] = (
    "src/nexus/db/",
)


#: The phase-allowlist that's active P0 through P4 and removed at P5.
#: Counted separately so the phase-boundary forcing function can assert
#: monotonic non-increase across phases.
CATALOG_PHASE_ALLOWLIST_PREFIX: str = "src/nexus/catalog/"


#: RDR-128 (RF-5) + RDR-120 P4.B (nexus-vyqah): class names whose
#: *direct construction* outside daemon-internal code is a
#: single-writer / single-client-contention offender.
#:
#: - ``T2Database(...)`` outside ``db/`` bypasses the facade-owning
#:   substrate (historically: eight SQLite connections contending on
#:   memory.db's one WAL writer lock; today the facade routes to the
#:   engine over HTTP, and the ban survives so consumers cannot grow
#:   ad-hoc construction sites outside the named allowlist).
#: - ``T3Database(...)`` constructed outside the substrate is the
#:   detectable consumer-side boundary. Since RDR-155 P4a.2
#:   (nexus-1k8s1) T3Database no longer opens Chroma clients itself
#:   (it raises without an injected ``_client``); consumers call
#:   ``make_t3()``, which returns the pgvector-service-backed
#:   ``HttpVectorClient``. The construction ban survives so consumer
#:   code cannot wrap raw vector clients ad hoc.
#:
#: RDR-186 P4: an un-allowlisted construction outside
#: :data:`T2DATABASE_CONSTRUCTION_ALLOWLIST` is a hard violation. The
#: metric field name (``t2database_constructions``) is historical (T2
#: came first); it counts both T2Database and T3Database named
#: constructions.
BANNED_CONSTRUCTORS: tuple[str, ...] = ("T2Database", "T3Database")


#: Prefixes allowed to construct ``T2Database`` / ``T3Database`` directly:
#: the substrate that defines them (``db/``).
#:
#: ``daemon/`` was here as "the daemon that runs them". It ran nothing after
#: nexus-i711w Stage 2 sub-stage B deleted t2_daemon.py, and no daemon/ file
#: constructs a T2Database any more, so the prefix is dropped rather than left
#: standing as unused permission.
T2DATABASE_CONSTRUCTION_ALLOWLIST_PREFIXES: tuple[str, ...] = (
    "src/nexus/db/",
)


#: RDR-146 P0.1 (nexus-5p2ci.1): the catalog client-cutover boundary.
#: ``Catalog(...)`` constructed in consumer code opens a direct
#: ``.catalog.db`` write handle that bypasses the T2 daemon, the
#: GH #1046 starvation root cause. Counted baseline, enforced at 0
#: since RDR-146 P1.2 (see :data:`CATALOG_CONSTRUCTION_BASELINE`).
CATALOG_BANNED_CONSTRUCTORS: tuple[str, ...] = ("Catalog",)


#: Prefixes allowed to construct ``Catalog`` directly: the module that
#: defines it (``catalog/``) and the substrate that runs it (``db/``).
#:
#: ``daemon/`` dropped with t2_daemon.py (nexus-i711w Stage 2 sub-stage B):
#: no daemon/ file constructs a Catalog any more.
CATALOG_CONSTRUCTION_ALLOWLIST_PREFIXES: tuple[str, ...] = (
    "src/nexus/db/",
    "src/nexus/catalog/",
)


#: nexus-qnp5s: allowlist for ``._db`` attribute accesses.
#: Only the catalog module itself may access ``._db`` internally. All consumer
#: code must call the public API (curator_owner_tumbler_by_name,
#: chunk_counts_for_docs, links_from_batch, etc.) which works on
#: HttpCatalogClient.
CATALOG_DB_ACCESS_ALLOWLIST_PREFIXES: tuple[str, ...] = (
    "src/nexus/catalog/",
)

#: nexus-qnp5s: baseline for ``._db`` accesses outside the catalog module.
#: History: seeded at 46 -> 44 (mcp/catalog dead branch + daemon/ scoping)
#: -> 3 (nexus-xnz0o: commands/ consumers migrated to the public API)
#: -> 1 (nexus-3cwnx: coverage analytics on the service API) -> 0
#: (nexus-i711w terminal deletion). ENFORCED at 0: the catalog handle is
#: an HttpCatalogClient whose ``._db`` property raises; any new ``._db``
#: reach outside catalog/ is a hard violation. RDR-186 P4 retired the
#: per-line escape token on this arm; there is no escape.
CATALOG_DB_ACCESS_BASELINE: int = 0


#: RDR-146 catalog-construction floor. P0.1 seeded 49; P1.2 (nexus-5p2ci.21)
#: completed the atomic cutover onto ``make_catalog_reader`` /
#: ``make_catalog_writer``, so the floor is 0 and ENFORCED. Any new
#: consumer-side bare construction is a hard violation; route it through
#: the factory instead.
CATALOG_CONSTRUCTION_BASELINE: int = 0


#: GH #1373 (nx store export crashed reaching ``T3Database``'s
#: backend-private ``._client_for(...)`` from ``exporter.py`` /
#: ``manifest_backfill.py``): ``HttpVectorClient`` -- production's
#: ``make_t3()`` return value in BOTH local and cloud mode since RDR-155
#: P4a.2 -- has no ``_client_for`` method at all, so any consumer-side
#: reach raises ``AttributeError`` at runtime. ``db/`` is the sole
#: legitimate owner. Enforced HARD at baseline 0.
CLIENT_FOR_ALLOWLIST_PREFIXES: tuple[str, ...] = (
    "src/nexus/db/",
)

#: GH #1374 sibling defect class: consumer code reaching ``Catalog``'s
#: backend-private ``._dir`` attribute (the on-disk catalog directory).
#: ``HttpCatalogClient`` (the catalog handle in every mode) has no ``._dir``.
CATALOG_DIR_ACCESS_ALLOWLIST_PREFIXES: tuple[str, ...] = (
    "src/nexus/catalog/",
    "src/nexus/daemon/",
)

#: Seeded at 1 (commands/catalog_cmds/backups.py:85); ENFORCED at 0 since
#: the nexus-i711w terminal deletion — backups.py died with the local
#: catalog, and no ``._dir`` reach remains outside the allowlist.
CATALOG_DIR_ACCESS_BASELINE: int = 0


#: nexus-9613q.1 (Part 2 of nexus-pyzk7): T2 store handles whose ``.conn`` /
#: ``._lock`` are raw SQLite-only attributes. Each resolves to an Http*Store
#: with no such attribute, so a consumer that reaches ``<x>.<store>.conn``
#: / ``._lock`` breaks — silently when wrapped in ``try/except`` (the
#: telemetry silent-loss class pyzk7 fixed), loudly otherwise.
T2_STORE_HANDLE_NAMES: frozenset[str] = frozenset({
    "taxonomy",
    "document_aspects",
    "telemetry",
    "memory",
    "plans",
    "chash_index",
    "aspect_queue",
    "document_highlights",
})

#: The raw SQLite-only attributes guarded on a T2 store handle.
T2_RAW_HANDLE_ATTRS: frozenset[str] = frozenset({"conn", "_lock"})

#: db/ defined the SQLite stores; daemon/ was the legitimate single writer.
#: Both legitimately reached ``.conn`` / ``._lock`` on a concrete store.
T2_RAW_HANDLE_ACCESS_ALLOWLIST_PREFIXES: tuple[str, ...] = (
    "src/nexus/db/",
    "src/nexus/daemon/",
)

#: nexus-9613q: baseline for un-allowlisted ``<x>.<t2_store>.conn|._lock``
#: accesses outside db/ + daemon/. ENFORCED at 0 — since RDR-158 P3
#: (nexus-7bomn) collapsed the last guarded consumer reaches with the
#: =sqlite opt-out, the population is zero repo-wide beyond the named
#: :data:`T2_RAW_HANDLE_ALLOWLIST` survivors (the Http stores' guard
#: mixin raises AttributeError at runtime for whatever the static lint
#: misses).
T2_RAW_HANDLE_BASELINE: int = 0


#: Banned call sites. Each entry is ``(module, attribute)`` matched
#: against AST ``Attribute(value=Name(id=module), attr=attribute)``
#: nodes inside ``Call`` nodes. Alias resolution maps the alias back
#: to the canonical module name before matching.
#:
#: RDR-152 Seam B (nexus-gmiaf.22): ``voyageai.Client`` is a structural
#: tripwire for the INDEXER surface. After the P3.3 cutover, embedding
#: moved to the JVM (nexus-service) — any new direct
#: ``voyageai.Client(...)`` in the indexer / client write surface is a
#: regression. Survivors are enumerated in
#: :data:`VOYAGEAI_CLIENT_ALLOWLIST`.
BANLIST: tuple[tuple[str, str], ...] = (
    ("sqlite3", "connect"),
    ("voyageai", "Client"),
)


# ── Named site allowlists (RDR-186 P4 — the terminal escape model) ──────────
#
# Keys are repo-relative POSIX paths; values are EXACT maximum site counts.
# A site beyond a file's count — or in a file not listed — is a hard
# violation. Tests additionally assert the LIVE totals equal these sums,
# so a deleted survivor forces the entry DOWN (exact-ledger discipline:
# a stale entry is a lie about the debt).

#: The only ``sqlite3.connect`` sites allowed anywhere in ``src/``:
#: diagnostics against the frozen SQLite migration source (RDR-176 Gap 2 —
#: a downgrade must find the local ``.db`` files intact, and these probes
#: must work with no engine running). Both survivors are READ-ONLY
#: (``mode=ro`` URIs). SQLite as a storage SUBSTRATE is deleted (RDR-158 P4
#: / RDR-186); a new connect is a hard violation, not a number to bump.
#:
#: health.py's entry (the PRAGMA integrity_check + FTS5 write-shaped probe)
#: was REMOVED at nexus-ay18d: the check it backed (``_check_t2_integrity``)
#: validated a fossil on a migrated box and passed vacuously on a fresh
#: PG-only box (the file never exists in that install shape). It was ported
#: to :func:`nexus.health.probe_t2_schema_fingerprint`, which asks the
#: engine's existing ``GET /version`` for the applied Liquibase changelog
#: fingerprint instead of opening the frozen file — no ``sqlite3.connect``
#: left in health.py. The ratchet moves 3 -> 2 (down, per the exact-ledger
#: discipline below); it must never move back up without a fresh Hal
#: decision recorded on a bead.
#:
#: GRANULARITY (zero-review Sig-2, stated plainly): these allowlists budget
#: per-FILE counts, not per-site identities — swapping a file's legitimate
#: site for a different connect at another line, holding the count, is not
#: detected mechanically; the reviewed module edit this dict requires is
#: the control. Same strength as the census it replaced, minus the
#: self-service per-line escape.
SQLITE_CONNECT_ALLOWLIST: dict[str, int] = {
    # mode=ro URI probe of a legacy source db (schema presence sniff).
    "src/nexus/db/__init__.py": 1,
    # nx doctor read-only (mode=ro URI) inspection of the frozen
    # migration source.
    "src/nexus/commands/doctor.py": 1,
}

#: ``voyageai.Client`` sites outside ``db/``: the RDR-152 Seam B Phase-4
#: deletion targets. RETIRED EMPTY at nexus-sghyo (2026-08-06, Hal
#: determination 2026-07-28: "we do no embedding on the client"): the
#: three named legacy sites (indexer.py's non-service embed path,
#: doc_indexer.py's ``_embed_with_fallback``, commands/collection.py's
#: re-embed CLI utility) were all deleted with the code they allowlisted.
#: A new entry here requires a fresh Hal decision, not a code comment.
VOYAGEAI_CLIENT_ALLOWLIST: dict[str, int] = {}

#: Direct ``T2Database(...)`` / ``T3Database(...)`` construction sites
#: outside ``db/`` — the RDR-128 P3 documented-irreducible survivor set,
#: formerly annotated per line. Every entry routes to the engine over
#: HTTP (the facade's only backend since RDR-158 P4); the reasons live
#: as ``boundary-allow`` comments at each site.
T2DATABASE_CONSTRUCTION_ALLOWLIST: dict[str, int] = {
    "src/nexus/collection_health.py": 1,   # read-only telemetry-stats open
    "src/nexus/context.py": 1,             # read-only T2 access
    "src/nexus/mcp_infra.py": 2,           # aspect_worker persist + service singleton
    "src/nexus/commands/_helpers.py": 1,   # t2_handle service-routing construction
    "src/nexus/commands/aspects.py": 1,    # requeue-failed read-only inspection
    "src/nexus/commands/catalog.py": 1,    # one-shot catalog-setup plan-seed loader
    "src/nexus/commands/catalog_cmds/report.py": 1,  # read-only T2 access
    "src/nexus/commands/doc.py": 3,        # read-only T2 access
    "src/nexus/commands/enrich.py": 8,     # read-only T2 access (+ routed writes)
    "src/nexus/commands/index.py": 2,      # read-only probes; writes via t2_index_write
    "src/nexus/commands/rdr.py": 1,        # short-lived read-only preamble CLI
    "src/nexus/commands/search_cmd.py": 1, # read-only T2 access
    "src/nexus/commands/taxonomy_cmd.py": 1,  # taxonomy CLI factory
}

#: ``<x>.<t2_store>.conn|._lock`` raw-handle reaches outside db/ + daemon/:
#: the one hasattr-guarded legacy branch (nexus-pyzk7 Part 3) that skips
#: itself when the handle is an Http store.
T2_RAW_HANDLE_ALLOWLIST: dict[str, int] = {
    "src/nexus/commands/catalog_cmds/report.py": 2,
}


@dataclass(frozen=True)
class Violation:
    """A single banned call site outside the allowlist."""

    file: str  # repo-relative POSIX path
    line: int
    symbol: str  # canonical "module.attr" name


@dataclass
class LintResult:
    """Aggregate result of a lint run."""

    violations: list[Violation] = field(default_factory=list)
    catalog_allowlist_count: int = 0
    #: Direct ``T2Database(...)`` / ``T3Database(...)`` constructions outside
    #: the construction-allowlist (db/) that are covered by the named
    #: :data:`T2DATABASE_CONSTRUCTION_ALLOWLIST`. Un-allowlisted
    #: constructions are hard violations (see ``violations``). Counts
    #: SYNTACTIC construction sites: a local wrapper like
    #: commands/taxonomy_cmd.py's ``_T2Database`` is counted once (at its
    #: ``return T2Database(...)`` body), not at each call site — the
    #: wrapper body is the boundary.
    t2database_constructions: int = 0
    #: ``sqlite3.connect`` sites covered by the named
    #: :data:`SQLITE_CONNECT_ALLOWLIST` — the read-only
    #: frozen-migration-source diagnostics. (Formerly
    #: ``epsilon_allow_connects``; renamed when the per-line escape token
    #: retired at RDR-186 P4.)
    sqlite_allowlisted_connects: int = 0
    #: ``voyageai.Client`` sites covered by the named
    #: :data:`VOYAGEAI_CLIENT_ALLOWLIST` — the Phase-4 deletion targets.
    #: (Formerly ``voyageai_epsilon_allow_count``.)
    voyageai_allowlisted_count: int = 0
    #: RDR-146 P0.1: ``Catalog(...)`` construction sites in consumer code
    #: (outside :data:`CATALOG_CONSTRUCTION_ALLOWLIST_PREFIXES`). Enforced
    #: at 0 via :data:`CATALOG_CONSTRUCTION_BASELINE`.
    catalog_constructions: int = 0
    #: nexus-qnp5s: ``._db`` attribute accesses outside ``src/nexus/catalog/``.
    #: The acceptance test asserts ``<= CATALOG_DB_ACCESS_BASELINE`` (0).
    catalog_db_accesses: int = 0
    #: nexus-9613q.1: ``<x>.<t2_store>.conn|._lock`` accesses outside
    #: db/ + daemon/ beyond the named :data:`T2_RAW_HANDLE_ALLOWLIST`.
    #: The acceptance test asserts ``<= T2_RAW_HANDLE_BASELINE`` (0).
    t2_raw_handle_accesses: int = 0
    #: The concrete sites backing ``t2_raw_handle_accesses`` (diagnostics).
    t2_raw_handle_access_sites: list[Violation] = field(default_factory=list)
    #: GH #1374 sibling class: ``Catalog._dir`` attribute accesses outside
    #: catalog/ + daemon/. The acceptance test asserts
    #: ``<= CATALOG_DIR_ACCESS_BASELINE`` (0).
    catalog_dir_accesses: int = 0

    @property
    def total_violations(self) -> int:
        return len(self.violations)

    def as_metric_dict(self) -> dict[str, int]:
        """Shape suitable for structlog or T2 metric storage."""
        return {
            "violations": self.total_violations,
            "catalog_allowlist_count": self.catalog_allowlist_count,
            "t2database_constructions": self.t2database_constructions,
            "sqlite_allowlisted_connects": self.sqlite_allowlisted_connects,
            "voyageai_allowlisted_count": self.voyageai_allowlisted_count,
            "catalog_constructions": self.catalog_constructions,
            "catalog_db_accesses": self.catalog_db_accesses,
            "t2_raw_handle_accesses": self.t2_raw_handle_accesses,
            "catalog_dir_accesses": self.catalog_dir_accesses,
        }


@dataclass
class FileScan:
    """Per-file scan result feeding :func:`scan_repo`'s aggregation."""

    #: ``sqlite3.connect`` sites beyond the file's named-allowlist budget
    #: (always hard violations — no path prefix applies to this arm).
    sqlite_connect_violations: list[Violation] = field(default_factory=list)
    #: Count of ``sqlite3.connect`` sites within the named-allowlist budget.
    sqlite_allowlisted_connects: int = 0
    #: ``voyageai.Client`` sites beyond the file's named-allowlist budget
    #: (hard violations outside the path-prefix allowlist).
    voyageai_violations: list[Violation] = field(default_factory=list)
    #: Count of ``voyageai.Client`` sites within the named-allowlist budget.
    voyageai_allowlisted_count: int = 0
    #: ``T2Database(...)`` / ``T3Database(...)`` construction sites within
    #: the file's named-allowlist budget.
    t2database_constructions_allowlisted: list[Violation] = field(default_factory=list)
    #: Construction sites beyond the budget. Promoted to hard violations in
    #: :func:`scan_repo` when the file is outside the construction-allowlist.
    t2database_constructions_excess: list[Violation] = field(default_factory=list)
    #: RDR-146 P0.1: ``Catalog(...)`` construction sites in this file.
    catalog_constructions: list[Violation] = field(default_factory=list)
    #: nexus-qnp5s: ``._db`` attribute accesses in this file.
    catalog_db_accesses: list[Violation] = field(default_factory=list)
    #: nexus-9613q.1: raw-handle accesses beyond the named-allowlist budget.
    t2_raw_handle_accesses: list[Violation] = field(default_factory=list)
    #: GH #1373: ``._client_for(...)`` calls in this file (backend-private
    #: T3Database method). Always a hard violation outside db/.
    client_for_calls: list[Violation] = field(default_factory=list)
    #: GH #1374 sibling class: ``._dir`` attribute accesses in this file.
    catalog_dir_accesses: list[Violation] = field(default_factory=list)

    @property
    def violations(self) -> list[Violation]:
        """Banlist-arm hard-violation candidates (sqlite + voyageai)."""
        return [*self.sqlite_connect_violations, *self.voyageai_violations]


# ---------------------------------------------------------------------------
# Per-file AST scan
# ---------------------------------------------------------------------------


def _collect_module_aliases(tree: ast.AST) -> dict[str, str]:
    """Return ``{alias_name: canonical_module}`` for matched bare imports.

    ``import sqlite3 as _sqlite3`` -> ``{"_sqlite3": "sqlite3"}``.
    ``import sqlite3`` -> ``{"sqlite3": "sqlite3"}`` (identity).
    Submodules are ignored — we match by top-level name only because
    that's what shows up as ``Name`` in the AST.
    """
    aliases: dict[str, str] = {}
    canonical_modules = {module for module, _ in BANLIST}
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            for alias in node.names:
                if alias.name in canonical_modules:
                    bound = alias.asname or alias.name
                    aliases[bound] = alias.name
    return aliases


def _collect_constructor_aliases(
    tree: ast.AST, names: Iterable[str] = BANNED_CONSTRUCTORS
) -> dict[str, str]:
    """Return ``{bound_name: canonical_class}`` for ``from`` imports of a
    tracked constructor.

    ``from nexus.db.t2 import T2Database`` -> ``{"T2Database": "T2Database"}``.
    ``from nexus.db.t2 import T2Database as DB`` -> ``{"DB": "T2Database"}``.
    ``from nexus.catalog import Catalog as _Catalog`` -> ``{"_Catalog": "Catalog"}``.
    """
    aliases: dict[str, str] = {}
    tracked = set(names)
    for node in ast.walk(tree):
        if isinstance(node, ast.ImportFrom):
            for alias in node.names:
                if alias.name in tracked:
                    aliases[alias.asname or alias.name] = alias.name
    return aliases


def _split_by_budget(
    sites: list[Violation], budget: int
) -> tuple[list[Violation], list[Violation]]:
    """Split *sites* into (allowlisted, excess) by line order.

    The first *budget* sites (ascending line number) are within the named
    allowance; everything beyond is a violation. Deterministic: within a
    file, sites are fungible at this granularity (the same per-file-count
    contract the DDL census used).
    """
    ordered = sorted(sites, key=lambda v: v.line)
    return ordered[:budget], ordered[budget:]


def _scan_file_full(
    path: pathlib.Path,
    repo_root: pathlib.Path,
    *,
    sqlite_connect_allowlist: Mapping[str, int] | None = None,
    voyageai_client_allowlist: Mapping[str, int] | None = None,
    t2database_construction_allowlist: Mapping[str, int] | None = None,
    t2_raw_handle_allowlist: Mapping[str, int] | None = None,
) -> FileScan:
    """Single-pass AST scan returning hard-violation candidates plus the
    named-allowlist populations (RDR-186 P4 terminal shape)."""
    if sqlite_connect_allowlist is None:
        sqlite_connect_allowlist = SQLITE_CONNECT_ALLOWLIST
    if voyageai_client_allowlist is None:
        voyageai_client_allowlist = VOYAGEAI_CLIENT_ALLOWLIST
    if t2database_construction_allowlist is None:
        t2database_construction_allowlist = T2DATABASE_CONSTRUCTION_ALLOWLIST
    if t2_raw_handle_allowlist is None:
        t2_raw_handle_allowlist = T2_RAW_HANDLE_ALLOWLIST

    scan = FileScan()
    try:
        source = path.read_text(encoding="utf-8")
    except (OSError, UnicodeDecodeError):
        return scan
    try:
        tree = ast.parse(source)
    except SyntaxError:
        return scan

    aliases = _collect_module_aliases(tree)
    constructor_aliases = _collect_constructor_aliases(tree)
    catalog_aliases = _collect_constructor_aliases(tree, CATALOG_BANNED_CONSTRUCTORS)

    banlist_map = {module: {attr for _, attr in BANLIST if _ == module}
                   for module, _ in BANLIST}

    def _rel() -> str:
        try:
            return path.relative_to(repo_root).as_posix()
        except ValueError:
            return str(path)

    rel = _rel()

    sqlite_sites: list[Violation] = []
    voyageai_sites: list[Violation] = []
    construction_sites: list[Violation] = []
    raw_handle_sites: list[Violation] = []

    for node in ast.walk(tree):
        if not isinstance(node, ast.Call):
            continue
        func = node.func
        line = node.lineno

        # ── Banned module.attr calls: sqlite3.connect, voyageai.Client ──
        if isinstance(func, ast.Attribute) and isinstance(func.value, ast.Name):
            canonical = aliases.get(func.value.id)
            if canonical is not None and func.attr in banlist_map.get(
                canonical, set()
            ):
                v = Violation(file=rel, line=line, symbol=f"{canonical}.{func.attr}")
                if (canonical, func.attr) == ("sqlite3", "connect"):
                    sqlite_sites.append(v)
                else:
                    voyageai_sites.append(v)
                continue

        # ── Banned constructor calls: T2Database(...) / T3Database(...) ──
        ctor: str | None = None
        if isinstance(func, ast.Name):
            ctor = constructor_aliases.get(func.id)
        elif isinstance(func, ast.Attribute) and func.attr in BANNED_CONSTRUCTORS:
            ctor = func.attr
        if ctor is not None:
            construction_sites.append(Violation(file=rel, line=line, symbol=ctor))
            continue

        # ── ._client_for(...) calls: GH #1373, backend-private method ──
        if isinstance(func, ast.Attribute) and func.attr == "_client_for":
            scan.client_for_calls.append(
                Violation(file=rel, line=line, symbol="_client_for")
            )
            continue

        # ── Catalog(...) construction: RDR-146 P0.1 baseline ──
        cat_ctor: str | None = None
        if isinstance(func, ast.Name):
            cat_ctor = catalog_aliases.get(func.id)
        elif isinstance(func, ast.Attribute) and func.attr in CATALOG_BANNED_CONSTRUCTORS:
            cat_ctor = func.attr
        if cat_ctor is not None:
            scan.catalog_constructions.append(
                Violation(file=rel, line=line, symbol=cat_ctor)
            )

    # ── nexus-qnp5s: ._db attribute access scan (not Call-scoped) ──
    # Walk all Attribute nodes (not just Call func nodes) to catch
    # any ``something._db`` access regardless of whether it is called.
    for node in ast.walk(tree):
        if (
            isinstance(node, ast.Attribute)
            and node.attr == "_db"
        ):
            # Only flag outside catalog/ — the allowlist is applied by scan_repo.
            scan.catalog_db_accesses.append(
                Violation(file=rel, line=node.lineno, symbol="catalog._db")
            )

    # ── GH #1374: Catalog._dir attribute access scan (not Call-scoped) ──
    # Same shape as the ._db scan above: ``._dir`` is Catalog's private
    # on-disk directory attribute; HttpCatalogClient has no such attribute.
    for node in ast.walk(tree):
        if (
            isinstance(node, ast.Attribute)
            and node.attr == "_dir"
        ):
            # Only flag outside catalog/ + daemon/ — allowlist applied by scan_repo.
            scan.catalog_dir_accesses.append(
                Violation(file=rel, line=node.lineno, symbol="catalog._dir")
            )

    # ── nexus-9613q.1: T2 raw-handle .conn/._lock access scan ──
    # Match ``<x>.<t2_store>.conn`` / ``<x>.<t2_store>._lock`` precisely:
    # an Attribute whose attr is conn/_lock AND whose value is itself an
    # Attribute whose attr is a known T2 store name. This is tight enough to
    # avoid the generic ``.conn`` false positive (e.g. ``client.pool.conn``).
    #
    # KNOWN BLIND SPOT (nexus-9613q review M3): this matches only the literal
    # two-level chain. It does NOT catch an aliased access (``s = db.taxonomy;
    # s.conn``), a parameter-threaded access (``def f(store): store.conn``), or
    # ``getattr(db.taxonomy, "conn")``. Resolving those needs dataflow, which
    # the storage-boundary lints deliberately avoid (cf. the taxonomy-WRITE
    # lint's same documented non-goal). The baseline=0 therefore guards the
    # idiomatic chain form; a deliberately-obfuscated alias can still evade it.
    # The fail-loud RawHandleGuardMixin (nexus-9613q.2) is the runtime backstop
    # for whatever the static lint misses.
    for node in ast.walk(tree):
        if (
            isinstance(node, ast.Attribute)
            and node.attr in T2_RAW_HANDLE_ATTRS
            and isinstance(node.value, ast.Attribute)
            and node.value.attr in T2_STORE_HANDLE_NAMES
        ):
            raw_handle_sites.append(
                Violation(
                    file=rel,
                    line=node.lineno,
                    symbol=f"{node.value.attr}.{node.attr}",
                )
            )

    # ── Apply the named per-file budgets (RDR-186 P4) ──
    allowed, excess = _split_by_budget(
        sqlite_sites, sqlite_connect_allowlist.get(rel, 0)
    )
    scan.sqlite_allowlisted_connects = len(allowed)
    scan.sqlite_connect_violations = excess

    allowed, excess = _split_by_budget(
        voyageai_sites, voyageai_client_allowlist.get(rel, 0)
    )
    scan.voyageai_allowlisted_count = len(allowed)
    scan.voyageai_violations = excess

    allowed, excess = _split_by_budget(
        construction_sites, t2database_construction_allowlist.get(rel, 0)
    )
    scan.t2database_constructions_allowlisted = allowed
    scan.t2database_constructions_excess = excess

    _, excess = _split_by_budget(
        raw_handle_sites, t2_raw_handle_allowlist.get(rel, 0)
    )
    scan.t2_raw_handle_accesses = excess

    return scan


def scan_file(
    path: pathlib.Path,
    repo_root: pathlib.Path,
) -> list[Violation]:
    """Scan a single Python file for banned call sites (hard violations).

    Backward-compatible thin wrapper over :func:`_scan_file_full`; callers
    that need the named-allowlist populations use :func:`_scan_file_full`
    (or read them off the aggregated :class:`LintResult`).
    """
    return _scan_file_full(path, repo_root).violations


# ---------------------------------------------------------------------------
# Repo-wide scan
# ---------------------------------------------------------------------------


def _is_allowlisted(file_path: str, allowlist_prefixes: Iterable[str]) -> bool:
    return any(file_path.startswith(prefix) for prefix in allowlist_prefixes)


def _iter_py_files(repo_root: pathlib.Path) -> Iterable[pathlib.Path]:
    src = repo_root / "src" / "nexus"
    if not src.is_dir():
        return []
    return (p for p in src.rglob("*.py") if p.is_file())


def scan_repo(
    repo_root: pathlib.Path,
    allowlist_prefixes: Iterable[str] | None = None,
    extra_files: Iterable[pathlib.Path] | None = None,
    construction_allowlist_prefixes: Iterable[str] | None = None,
    catalog_construction_allowlist_prefixes: Iterable[str] | None = None,
    catalog_db_access_allowlist_prefixes: Iterable[str] | None = None,
    t2_raw_handle_access_allowlist_prefixes: Iterable[str] | None = None,
    client_for_allowlist_prefixes: Iterable[str] | None = None,
    catalog_dir_access_allowlist_prefixes: Iterable[str] | None = None,
    sqlite_connect_allowlist: Mapping[str, int] | None = None,
    voyageai_client_allowlist: Mapping[str, int] | None = None,
    t2database_construction_allowlist: Mapping[str, int] | None = None,
    t2_raw_handle_allowlist: Mapping[str, int] | None = None,
) -> LintResult:
    """Scan the repo for banned call sites and the named-allowlist
    populations.

    ``allowlist_prefixes`` defaults to :data:`DEFAULT_ALLOWLIST_PREFIXES`
    plus the catalog phase-allowlist; it scopes the hard
    ``voyageai.Client`` violations ONLY. The ``sqlite3.connect`` arm
    ignores it (RDR-186 P4): the named
    :data:`SQLITE_CONNECT_ALLOWLIST` is the sole escape, everywhere.

    ``construction_allowlist_prefixes`` defaults to
    :data:`T2DATABASE_CONSTRUCTION_ALLOWLIST_PREFIXES` and scopes the
    ``T2Database(...)`` construction count (db/ defines it).

    The four ``*_allowlist`` mappings default to the module-level named
    allowlists; tests inject substitutes (keyed by ``str(path)`` for
    ``extra_files``) to exercise the budget mechanism.

    ``extra_files`` is a list of additional files (typically test
    fixtures outside the repo) to scan and report against; they are never
    allowlisted by path prefix.
    """
    repo_root = repo_root.resolve()
    if allowlist_prefixes is None:
        allowlist_prefixes = (
            *DEFAULT_ALLOWLIST_PREFIXES,
            CATALOG_PHASE_ALLOWLIST_PREFIX,
        )
    else:
        allowlist_prefixes = tuple(allowlist_prefixes)
    if construction_allowlist_prefixes is None:
        construction_allowlist_prefixes = T2DATABASE_CONSTRUCTION_ALLOWLIST_PREFIXES
    else:
        construction_allowlist_prefixes = tuple(construction_allowlist_prefixes)
    if catalog_construction_allowlist_prefixes is None:
        catalog_construction_allowlist_prefixes = (
            CATALOG_CONSTRUCTION_ALLOWLIST_PREFIXES
        )
    else:
        catalog_construction_allowlist_prefixes = tuple(
            catalog_construction_allowlist_prefixes
        )
    if catalog_db_access_allowlist_prefixes is None:
        catalog_db_access_allowlist_prefixes = CATALOG_DB_ACCESS_ALLOWLIST_PREFIXES
    else:
        catalog_db_access_allowlist_prefixes = tuple(catalog_db_access_allowlist_prefixes)
    if t2_raw_handle_access_allowlist_prefixes is None:
        t2_raw_handle_access_allowlist_prefixes = T2_RAW_HANDLE_ACCESS_ALLOWLIST_PREFIXES
    else:
        t2_raw_handle_access_allowlist_prefixes = tuple(
            t2_raw_handle_access_allowlist_prefixes
        )
    if client_for_allowlist_prefixes is None:
        client_for_allowlist_prefixes = CLIENT_FOR_ALLOWLIST_PREFIXES
    else:
        client_for_allowlist_prefixes = tuple(client_for_allowlist_prefixes)
    if catalog_dir_access_allowlist_prefixes is None:
        catalog_dir_access_allowlist_prefixes = CATALOG_DIR_ACCESS_ALLOWLIST_PREFIXES
    else:
        catalog_dir_access_allowlist_prefixes = tuple(catalog_dir_access_allowlist_prefixes)

    named_allowlists = {
        "sqlite_connect_allowlist": sqlite_connect_allowlist,
        "voyageai_client_allowlist": voyageai_client_allowlist,
        "t2database_construction_allowlist": t2database_construction_allowlist,
        "t2_raw_handle_allowlist": t2_raw_handle_allowlist,
    }

    result = LintResult()

    # In-tree scan with allowlist filters.
    for py in _iter_py_files(repo_root):
        rel = py.relative_to(repo_root).as_posix()
        scan = _scan_file_full(py, repo_root, **named_allowlists)

        # ── sqlite3.connect: named allowlist ONLY (RDR-186 P4). A connect
        # beyond the named budget is a hard violation regardless of path —
        # SQLite is deleted as a storage substrate.
        result.violations.extend(scan.sqlite_connect_violations)
        result.sqlite_allowlisted_connects += scan.sqlite_allowlisted_connects

        # ── voyageai.Client: path-prefix allowlist still applies (db/ owns
        # the Voyage EF + T3 embed path); the named allowlist covers the
        # legacy Phase-4 deletion targets outside it.
        if _is_allowlisted(rel, allowlist_prefixes):
            if rel.startswith(CATALOG_PHASE_ALLOWLIST_PREFIX):
                result.catalog_allowlist_count += len(scan.voyageai_violations)
        else:
            result.violations.extend(scan.voyageai_violations)
            result.voyageai_allowlisted_count += scan.voyageai_allowlisted_count

        # ── T2Database/T3Database constructions: scoped by the construction
        # allowlist (db/). Named-allowlisted sites -> metric; excess -> hard
        # violation.
        if not _is_allowlisted(rel, construction_allowlist_prefixes):
            result.t2database_constructions += len(
                scan.t2database_constructions_allowlisted
            )
            result.violations.extend(scan.t2database_constructions_excess)

        # RDR-146 P0.1 (catalog constructions): counted baseline outside
        # the catalog construction-allowlist (catalog/ + db/), enforced at 0.
        if not _is_allowlisted(rel, catalog_construction_allowlist_prefixes):
            result.catalog_constructions += len(scan.catalog_constructions)

        # nexus-qnp5s: catalog._db accesses — counted baseline outside
        # catalog/, enforced at 0 via CATALOG_DB_ACCESS_BASELINE.
        if not _is_allowlisted(rel, catalog_db_access_allowlist_prefixes):
            result.catalog_db_accesses += len(scan.catalog_db_accesses)

        # GH #1373: ._client_for(...) calls — HARD violation outside db/
        # (no legitimate consumer-side use; baseline 0).
        if not _is_allowlisted(rel, client_for_allowlist_prefixes):
            result.violations.extend(scan.client_for_calls)

        # GH #1374 sibling class: Catalog._dir accesses — counted baseline
        # outside catalog/ + daemon/, enforced at 0.
        if not _is_allowlisted(rel, catalog_dir_access_allowlist_prefixes):
            result.catalog_dir_accesses += len(scan.catalog_dir_accesses)

        # nexus-9613q.1: T2 raw-handle .conn/._lock accesses beyond the
        # named allowlist — counted baseline outside db/ + daemon/.
        if not _is_allowlisted(rel, t2_raw_handle_access_allowlist_prefixes):
            result.t2_raw_handle_accesses += len(scan.t2_raw_handle_accesses)
            result.t2_raw_handle_access_sites.extend(scan.t2_raw_handle_accesses)

    # Extra files: always scanned, never allowlisted by path prefix.
    if extra_files:
        for extra in extra_files:
            scan = _scan_file_full(
                pathlib.Path(extra), repo_root, **named_allowlists
            )
            result.violations.extend(scan.sqlite_connect_violations)
            result.sqlite_allowlisted_connects += scan.sqlite_allowlisted_connects
            result.violations.extend(scan.voyageai_violations)
            result.voyageai_allowlisted_count += scan.voyageai_allowlisted_count
            result.t2database_constructions += len(
                scan.t2database_constructions_allowlisted
            )
            result.violations.extend(scan.t2database_constructions_excess)
            result.catalog_constructions += len(scan.catalog_constructions)
            result.catalog_db_accesses += len(scan.catalog_db_accesses)
            result.t2_raw_handle_accesses += len(scan.t2_raw_handle_accesses)
            result.t2_raw_handle_access_sites.extend(scan.t2_raw_handle_accesses)
            result.violations.extend(scan.client_for_calls)
            result.catalog_dir_accesses += len(scan.catalog_dir_accesses)

    return result
