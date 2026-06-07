# SPDX-License-Identifier: AGPL-3.0-or-later
"""RDR-120 P0.A: storage-boundary lint.

AST-scan that catches direct storage opens outside the allowed
daemon-internal substrate. The lint protects the boundary that
RDR-120's daemon design enforces: only ``src/nexus/db/`` (and during
P0-P4, ``src/nexus/catalog/``) may open SQLite or chromadb clients
directly. Every other caller must go through the ``T2Database`` /
``T3Database`` facades or, post-P3 cutover, through the daemon-
backed ``T2Client`` / ``T3Client`` wrappers.

Banlist (configurable via :data:`BANLIST`):

* ``sqlite3.connect(...)`` plus any aliased form (``import sqlite3 as
  X; X.connect(...)``). Alias resolution is per-file.
* ``chromadb.PersistentClient(...)``
* ``chromadb.CloudClient(...)``
* ``chromadb.EphemeralClient(...)``

Allowlist:

* Path-prefix allowlist (``src/nexus/db/`` always; ``src/nexus/catalog/``
  P0-P4 phase-allowlisted; deleted at P5).
* Per-line ``# epsilon-allow: <reason>`` override, reason >= 8 chars.

Output: a :class:`LintResult` with a structured violation list plus
the catalog-allowlist call-site count metric for the phase-boundary
forcing function (RDR-120 §Approach catalog-allowlist non-increase).

Modeled on the AST-walk + offender-aggregation pattern in
``tests/test_no_direct_catalog_writes_outside_projector.py`` (RDR-101
ε-lint precedent).
"""
from __future__ import annotations

import ast
import pathlib
import re
from dataclasses import dataclass, field
from typing import Iterable


#: Path-prefix allowlist relative to repo root (POSIX-style). Files
#: under any of these prefixes are exempt from the lint.
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
#: - ``T2Database(...)`` outside the daemon opens eight SQLite
#:   connections that contend on memory.db's one WAL writer lock.
#: - ``T3Database(local_mode=True, ...)`` WITHOUT an injected
#:   ``_client`` opens its own ``chromadb.PersistentClient`` on the
#:   local on-disk store, the T3 analogue of the same multi-process
#:   contention (the ``chromadb.PersistentClient`` call itself lives
#:   in the allowlisted ``db/t3.py`` so the BANLIST scan cannot catch
#:   it — the consumer-side ``T3Database(...)`` construction is the
#:   detectable boundary). Consumers must call ``make_t3()`` /
#:   ``make_t3_client()`` (RDR-120 P6 made ``make_t3()`` route through
#:   the daemon in local mode) instead of constructing T3Database
#:   directly.
#:
#: P3 (nexus-sbxbe.3) flipped the lint to ENFORCE — an un-annotated
#: construction outside the construction-allowlist is a hard violation,
#: while one carrying ``# epsilon-allow: <reason>`` is a documented
#: exception (counted in ``t2database_constructions``). Mirrors the
#: ``sqlite3.connect`` treatment exactly. The metric field name is
#: historical (T2 came first); it now counts both T2Database and
#: T3Database documented constructions.
BANNED_CONSTRUCTORS: tuple[str, ...] = ("T2Database", "T3Database")


#: Prefixes allowed to construct ``T2Database`` / ``T3Database``
#: directly: the substrate that defines them (``db/``) and the daemon
#: that runs them (``daemon/`` — e.g. ``make_t3_client`` builds the
#: daemon-backed ``T3Database`` with an injected ``HttpClient``).
#: Distinct from the connect-allowlist (which includes ``catalog/``
#: P0-P4 but not ``daemon/``).
T2DATABASE_CONSTRUCTION_ALLOWLIST_PREFIXES: tuple[str, ...] = (
    "src/nexus/db/",
    "src/nexus/daemon/",
)


#: RDR-146 P0.1 (nexus-5p2ci.1): the catalog client-cutover boundary.
#: ``Catalog(...)`` constructed in consumer code opens a direct
#: ``.catalog.db`` write handle that bypasses the T2 daemon, the
#: GH #1046 starvation root cause (the catalog is already the 8th T2
#: domain store served over RPC; consumers must route writes through
#: ``T2Client.catalog`` instead of holding a local ``Catalog``).
#:
#: Unlike ``BANNED_CONSTRUCTORS``, this is a COUNTED BASELINE at P0.1,
#: NOT an enforced hard violation: ~49 consumer sites still construct
#: ``Catalog`` directly and are cut over in RDR-146 Phase 1. The metric
#: ratchets down as sites migrate (monotonic non-increase, mirroring the
#: RDR-128 P0c ``t2database_constructions`` baseline before its P3
#: enforce-flip). The end-of-P1 enforce-flip is a separate change.
CATALOG_BANNED_CONSTRUCTORS: tuple[str, ...] = ("Catalog",)


#: Prefixes allowed to construct ``Catalog`` directly: the module that
#: defines it (``catalog/``) and the substrate/daemon that run it
#: (``db/``, ``daemon/``). Every other site is a consumer that must
#: route catalog writes through the daemon client — those are the
#: cutover surface counted by ``catalog_constructions``.
CATALOG_CONSTRUCTION_ALLOWLIST_PREFIXES: tuple[str, ...] = (
    "src/nexus/db/",
    "src/nexus/daemon/",
    "src/nexus/catalog/",
)


#: RDR-146 catalog-construction floor. P0.1 seeded this at 49 (the AST
#: count of bare ``Catalog(...)`` construction sites in consumer code at
#: the start of the Phase-1 cutover). P1.2 (nexus-5p2ci.21) completed the
#: atomic cutover: every consumer site now routes catalog reads through
#: :func:`nexus.catalog.factory.make_catalog_reader` and writes through
#: :func:`make_catalog_writer` (the daemon-hosted single writer), so the
#: floor is now 0 and ENFORCED. The acceptance criterion
#: ``scan_repo(...).catalog_constructions <= CATALOG_CONSTRUCTION_BASELINE``
#: now means "no bare ``Catalog(...)`` survives outside the substrate
#: allowlist (db/, daemon/, catalog/)". Any new consumer-side bare
#: construction is a hard violation; route it through the factory instead.
CATALOG_CONSTRUCTION_BASELINE: int = 0


#: Banned call sites. Each entry is ``(module, attribute)`` matched
#: against AST ``Attribute(value=Name(id=module), attr=attribute)``
#: nodes inside ``Call`` nodes. Alias resolution maps the alias back
#: to the canonical module name before matching.
#:
#: RDR-152 Seam B (nexus-gmiaf.22): ``voyageai.Client`` is added as a
#: structural tripwire for the INDEXER surface.  After the P3.3 cutover,
#: embedding moves to the JVM (nexus-service) — any new direct
#: ``voyageai.Client(...)`` in the indexer / client write surface is a
#: regression.  The allowlist (db/, catalog/ P0-P4, daemon/) covers
#: the Phase-4 deletion targets that still hold direct Voyage calls
#: (t3.py, daemon/, catalog/).  Any new Voyage call outside those paths
#: must carry a valid ``# epsilon-allow: <reason>`` annotation.
BANLIST: tuple[tuple[str, str], ...] = (
    ("sqlite3", "connect"),
    ("chromadb", "PersistentClient"),
    ("chromadb", "CloudClient"),
    ("chromadb", "EphemeralClient"),
    ("voyageai", "Client"),
)


#: Per-line override token. The trailing reason text after the colon
#: must be at least this many characters (whitespace-stripped) for the
#: override to apply.
ALLOWLIST_TOKEN: str = "# epsilon-allow:"
ALLOWLIST_REASON_MIN_LENGTH: int = 8

_ALLOWLIST_RE = re.compile(
    r"#\s*epsilon-allow\s*:\s*(?P<reason>.+?)\s*$",
)


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
    #: RDR-128 P3 population 2 (DOCUMENTED): direct ``T2Database(...)``
    #: constructions outside the construction-allowlist (db/ + daemon/)
    #: that carry a valid ``# epsilon-allow: <reason>`` override. At P0c
    #: this counted ALL constructions (baseline metric); P3 flipped the
    #: lint to enforce, so it now counts only the ANNOTATED survivors —
    #: the documented-irreducible set, each carrying a lock-discipline
    #: justification. Un-annotated constructions are hard violations
    #: (see ``violations``), mirroring the ``sqlite3.connect`` treatment.
    #: Counts SYNTACTIC construction sites: a local wrapper like
    #: commands/taxonomy_cmd.py's ``_T2Database`` is counted once (at its
    #: ``return T2Database(...)`` body), not at each call site — the
    #: wrapper body is the boundary.
    t2database_constructions: int = 0
    #: RDR-128 P0c population 1: ``sqlite3.connect`` sites outside the
    #: connect-allowlist that carry a valid ``# epsilon-allow:`` override.
    #: The deliberate raw-connect exceptions to the substrate boundary.
    epsilon_allow_connects: int = 0
    #: RDR-146 P0.1: ``Catalog(...)`` construction sites in consumer code
    #: (outside :data:`CATALOG_CONSTRUCTION_ALLOWLIST_PREFIXES`). A COUNTED
    #: baseline at P0.1 — these are the cutover surface for the catalog
    #: client-cutover, NOT yet promoted to hard violations. Ratchets down
    #: as Phase-1 waves migrate sites onto ``T2Client.catalog``.
    catalog_constructions: int = 0

    @property
    def total_violations(self) -> int:
        return len(self.violations)

    def as_metric_dict(self) -> dict[str, int]:
        """Shape suitable for structlog or T2 metric storage."""
        return {
            "violations": self.total_violations,
            "catalog_allowlist_count": self.catalog_allowlist_count,
            "t2database_constructions": self.t2database_constructions,
            "epsilon_allow_connects": self.epsilon_allow_connects,
            "catalog_constructions": self.catalog_constructions,
        }


@dataclass
class FileScan:
    """Per-file scan result feeding :func:`scan_repo`'s aggregation."""

    violations: list[Violation] = field(default_factory=list)
    #: RDR-128 P3: ``T2Database(...)`` construction sites carrying a valid
    #: ``# epsilon-allow: <reason>`` (the documented-irreducible survivors).
    t2database_constructions_documented: list[Violation] = field(default_factory=list)
    #: RDR-128 P3: ``T2Database(...)`` construction sites WITHOUT a valid
    #: override. Promoted to hard violations in :func:`scan_repo` when the
    #: file is outside the construction-allowlist (db/ + daemon/).
    t2database_constructions_undocumented: list[Violation] = field(default_factory=list)
    #: Count of epsilon-allow'd ``sqlite3.connect`` sites in this file.
    epsilon_allow_connects: int = 0
    #: RDR-146 P0.1: ``Catalog(...)`` construction sites in this file
    #: (the catalog client-cutover surface). Scoped by the catalog
    #: construction-allowlist in :func:`scan_repo`.
    catalog_constructions: list[Violation] = field(default_factory=list)


# ---------------------------------------------------------------------------
# Per-file AST scan
# ---------------------------------------------------------------------------


def _collect_module_aliases(tree: ast.AST) -> dict[str, str]:
    """Return ``{alias_name: canonical_module}`` for matched bare imports.

    ``import sqlite3 as _sqlite3`` -> ``{"_sqlite3": "sqlite3"}``.
    ``import chromadb`` -> ``{"chromadb": "chromadb"}`` (identity).
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


def _line_has_allowlist_token(source_lines: list[str], line_no: int) -> bool:
    """Return True iff the (1-indexed) line carries an epsilon-allow tag."""
    if line_no < 1 or line_no > len(source_lines):
        return False
    line = source_lines[line_no - 1]
    if ALLOWLIST_TOKEN not in line:
        return False
    match = _ALLOWLIST_RE.search(line)
    if not match:
        return False
    reason = match.group("reason").strip()
    return len(reason) >= ALLOWLIST_REASON_MIN_LENGTH


def _scan_file_full(
    path: pathlib.Path,
    repo_root: pathlib.Path,
) -> FileScan:
    """Single-pass AST scan returning hard violations plus the two
    RDR-128 P0c baseline populations (epsilon-allow'd ``sqlite3.connect``
    sites and direct ``T2Database(...)`` constructions)."""
    scan = FileScan()
    try:
        source = path.read_text(encoding="utf-8")
    except (OSError, UnicodeDecodeError):
        return scan
    try:
        tree = ast.parse(source)
    except SyntaxError:
        return scan

    source_lines = source.splitlines()
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

    for node in ast.walk(tree):
        if not isinstance(node, ast.Call):
            continue
        func = node.func
        line = node.lineno

        # ── Banned module.attr calls: sqlite3.connect, chromadb.* ──
        if isinstance(func, ast.Attribute) and isinstance(func.value, ast.Name):
            canonical = aliases.get(func.value.id)
            if canonical is not None and func.attr in banlist_map.get(
                canonical, set()
            ):
                if _line_has_allowlist_token(source_lines, line):
                    # Deliberate, documented exception. Count raw-connect
                    # overrides as the population-1 baseline instead of
                    # silently skipping them.
                    if (canonical, func.attr) == ("sqlite3", "connect"):
                        scan.epsilon_allow_connects += 1
                else:
                    scan.violations.append(
                        Violation(
                            file=_rel(),
                            line=line,
                            symbol=f"{canonical}.{func.attr}",
                        )
                    )
                continue

        # ── Banned constructor calls: T2Database(...) ──
        ctor: str | None = None
        if isinstance(func, ast.Name):
            ctor = constructor_aliases.get(func.id)
        elif isinstance(func, ast.Attribute) and func.attr in BANNED_CONSTRUCTORS:
            ctor = func.attr
        if ctor is not None:
            v = Violation(file=_rel(), line=line, symbol=ctor)
            # RDR-128 P3: an annotated construction is a documented
            # exception (counted, not failed); an un-annotated one is a
            # hard violation once scoped by the construction-allowlist in
            # scan_repo. Mirrors the sqlite3.connect epsilon-allow split.
            if _line_has_allowlist_token(source_lines, line):
                scan.t2database_constructions_documented.append(v)
            else:
                scan.t2database_constructions_undocumented.append(v)
            continue

        # ── Catalog(...) construction: RDR-146 P0.1 baseline ──
        cat_ctor: str | None = None
        if isinstance(func, ast.Name):
            cat_ctor = catalog_aliases.get(func.id)
        elif isinstance(func, ast.Attribute) and func.attr in CATALOG_BANNED_CONSTRUCTORS:
            cat_ctor = func.attr
        if cat_ctor is not None:
            scan.catalog_constructions.append(
                Violation(file=_rel(), line=line, symbol=cat_ctor)
            )

    return scan


def scan_file(
    path: pathlib.Path,
    repo_root: pathlib.Path,
) -> list[Violation]:
    """Scan a single Python file for banned call sites (hard violations).

    Backward-compatible thin wrapper over :func:`_scan_file_full`; callers
    that need the RDR-128 baseline populations use :func:`_scan_file_full`
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
) -> LintResult:
    """Scan the repo for banned call sites and the RDR-128 baseline
    populations.

    ``allowlist_prefixes`` defaults to :data:`DEFAULT_ALLOWLIST_PREFIXES`
    plus the catalog phase-allowlist; it scopes the hard ``sqlite3.connect``
    / ``chromadb.*`` violations and the epsilon-allow'd-connect count. Pass
    an empty tuple to disable path-prefix allowlisting (useful for tests).

    ``construction_allowlist_prefixes`` defaults to
    :data:`T2DATABASE_CONSTRUCTION_ALLOWLIST_PREFIXES` and scopes the
    ``T2Database(...)`` construction count (db/ defines it, daemon/ runs it).

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

    result = LintResult()

    # In-tree scan with allowlist filters.
    for py in _iter_py_files(repo_root):
        rel = py.relative_to(repo_root).as_posix()
        scan = _scan_file_full(py, repo_root)

        # Population 0 (hard violations) + population 1 (epsilon connects):
        # scoped by the connect-allowlist.
        if _is_allowlisted(rel, allowlist_prefixes):
            if rel.startswith(CATALOG_PHASE_ALLOWLIST_PREFIX):
                result.catalog_allowlist_count += len(scan.violations)
        else:
            result.violations.extend(scan.violations)
            result.epsilon_allow_connects += scan.epsilon_allow_connects

        # Population 2 (T2Database constructions): scoped by the separate
        # construction-allowlist (db/ + daemon/). RDR-128 P3 enforces:
        # annotated -> documented population; un-annotated -> hard violation.
        if not _is_allowlisted(rel, construction_allowlist_prefixes):
            result.t2database_constructions += len(
                scan.t2database_constructions_documented
            )
            result.violations.extend(scan.t2database_constructions_undocumented)

        # RDR-146 P0.1 (catalog constructions): counted baseline outside
        # the catalog construction-allowlist (catalog/ + db/ + daemon/).
        # NOT promoted to hard violations at P0.1 — the cutover surface.
        if not _is_allowlisted(rel, catalog_construction_allowlist_prefixes):
            result.catalog_constructions += len(scan.catalog_constructions)

    # Extra files: always scanned, never allowlisted by path prefix.
    if extra_files:
        for extra in extra_files:
            scan = _scan_file_full(pathlib.Path(extra), repo_root)
            result.violations.extend(scan.violations)
            result.epsilon_allow_connects += scan.epsilon_allow_connects
            result.t2database_constructions += len(
                scan.t2database_constructions_documented
            )
            result.violations.extend(scan.t2database_constructions_undocumented)
            result.catalog_constructions += len(scan.catalog_constructions)

    return result
