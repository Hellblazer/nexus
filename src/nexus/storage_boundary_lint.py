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


#: RDR-128 P0c (RF-5): class names whose *direct construction* outside
#: daemon-internal code is a single-writer-invariant offender. Each
#: ``T2Database(...)`` outside the daemon opens eight SQLite connections
#: that contend on memory.db's one WAL writer lock. Counted (not failed)
#: as a baseline population so P1/P3 can drive it down measurably.
BANNED_CONSTRUCTORS: tuple[str, ...] = ("T2Database",)


#: Prefixes allowed to construct ``T2Database`` directly: the substrate
#: that defines it and the daemon that runs it as the single writer.
#: Distinct from the connect-allowlist (which includes ``catalog/`` P0-P4
#: but not ``daemon/``).
T2DATABASE_CONSTRUCTION_ALLOWLIST_PREFIXES: tuple[str, ...] = (
    "src/nexus/db/",
    "src/nexus/daemon/",
)


#: Banned call sites. Each entry is ``(module, attribute)`` matched
#: against AST ``Attribute(value=Name(id=module), attr=attribute)``
#: nodes inside ``Call`` nodes. Alias resolution maps the alias back
#: to the canonical module name before matching.
BANLIST: tuple[tuple[str, str], ...] = (
    ("sqlite3", "connect"),
    ("chromadb", "PersistentClient"),
    ("chromadb", "CloudClient"),
    ("chromadb", "EphemeralClient"),
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
    #: RDR-128 P0c population 2: direct ``T2Database(...)`` constructions
    #: outside the construction-allowlist (db/ + daemon/). Baseline metric;
    #: P1/P3 drive it toward the documented-irreducible set. Counts SYNTACTIC
    #: construction sites: a local wrapper like commands/taxonomy_cmd.py's
    #: ``_T2Database`` is counted once (at its ``return T2Database(...)`` body),
    #: not at each of its call sites — the wrapper body is the boundary.
    t2database_constructions: int = 0
    #: RDR-128 P0c population 1: ``sqlite3.connect`` sites outside the
    #: connect-allowlist that carry a valid ``# epsilon-allow:`` override.
    #: The deliberate raw-connect exceptions to the substrate boundary.
    epsilon_allow_connects: int = 0

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
        }


@dataclass
class FileScan:
    """Per-file scan result feeding :func:`scan_repo`'s aggregation."""

    violations: list[Violation] = field(default_factory=list)
    #: ``T2Database(...)`` construction sites (symbol == "T2Database").
    t2database_constructions: list[Violation] = field(default_factory=list)
    #: Count of epsilon-allow'd ``sqlite3.connect`` sites in this file.
    epsilon_allow_connects: int = 0


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


def _collect_constructor_aliases(tree: ast.AST) -> dict[str, str]:
    """Return ``{bound_name: canonical_class}`` for ``from`` imports of a
    banned constructor.

    ``from nexus.db.t2 import T2Database`` -> ``{"T2Database": "T2Database"}``.
    ``from nexus.db.t2 import T2Database as DB`` -> ``{"DB": "T2Database"}``.
    """
    aliases: dict[str, str] = {}
    banned = set(BANNED_CONSTRUCTORS)
    for node in ast.walk(tree):
        if isinstance(node, ast.ImportFrom):
            for alias in node.names:
                if alias.name in banned:
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
            scan.t2database_constructions.append(
                Violation(file=_rel(), line=line, symbol=ctor)
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
        # construction-allowlist (db/ + daemon/).
        if not _is_allowlisted(rel, construction_allowlist_prefixes):
            result.t2database_constructions += len(scan.t2database_constructions)

    # Extra files: always scanned, never allowlisted by path prefix.
    if extra_files:
        for extra in extra_files:
            scan = _scan_file_full(pathlib.Path(extra), repo_root)
            result.violations.extend(scan.violations)
            result.epsilon_allow_connects += scan.epsilon_allow_connects
            result.t2database_constructions += len(scan.t2database_constructions)

    return result
