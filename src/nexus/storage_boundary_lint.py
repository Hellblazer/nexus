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

    @property
    def total_violations(self) -> int:
        return len(self.violations)

    def as_metric_dict(self) -> dict[str, int]:
        """Shape suitable for structlog or T2 metric storage."""
        return {
            "violations": self.total_violations,
            "catalog_allowlist_count": self.catalog_allowlist_count,
        }


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


def scan_file(
    path: pathlib.Path,
    repo_root: pathlib.Path,
) -> list[Violation]:
    """Scan a single Python file for banned call sites."""
    try:
        source = path.read_text(encoding="utf-8")
    except (OSError, UnicodeDecodeError):
        return []
    try:
        tree = ast.parse(source)
    except SyntaxError:
        return []

    source_lines = source.splitlines()
    aliases = _collect_module_aliases(tree)

    violations: list[Violation] = []
    banlist_map = {module: {attr for _, attr in BANLIST if _ == module}
                   for module, _ in BANLIST}

    for node in ast.walk(tree):
        if not isinstance(node, ast.Call):
            continue
        func = node.func
        if not isinstance(func, ast.Attribute):
            continue
        if not isinstance(func.value, ast.Name):
            continue
        alias_name = func.value.id
        canonical = aliases.get(alias_name)
        if canonical is None:
            continue
        if func.attr not in banlist_map.get(canonical, set()):
            continue
        line = node.lineno
        if _line_has_allowlist_token(source_lines, line):
            continue
        try:
            rel = path.relative_to(repo_root).as_posix()
        except ValueError:
            rel = str(path)
        violations.append(
            Violation(file=rel, line=line, symbol=f"{canonical}.{func.attr}")
        )
    return violations


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
) -> LintResult:
    """Scan the repo for banned call sites.

    ``allowlist_prefixes`` defaults to :data:`DEFAULT_ALLOWLIST_PREFIXES`
    plus the catalog phase-allowlist. Pass an empty tuple to disable
    path-prefix allowlisting (useful for tests).

    ``extra_files`` is a list of additional files (typically test
    fixtures outside the repo) to scan and report against.
    """
    repo_root = repo_root.resolve()
    if allowlist_prefixes is None:
        allowlist_prefixes = (
            *DEFAULT_ALLOWLIST_PREFIXES,
            CATALOG_PHASE_ALLOWLIST_PREFIX,
        )
    else:
        allowlist_prefixes = tuple(allowlist_prefixes)

    result = LintResult()

    # In-tree scan with allowlist filter.
    for py in _iter_py_files(repo_root):
        rel = py.relative_to(repo_root).as_posix()
        file_violations = scan_file(py, repo_root)
        if _is_allowlisted(rel, allowlist_prefixes):
            # Count catalog-prefix hits for the phase-boundary metric.
            if rel.startswith(CATALOG_PHASE_ALLOWLIST_PREFIX):
                result.catalog_allowlist_count += len(file_violations)
            continue
        result.violations.extend(file_violations)

    # Extra files (e.g. synthetic test offenders living outside the
    # repo): always scanned, never allowlisted by path prefix.
    if extra_files:
        for extra in extra_files:
            result.violations.extend(scan_file(pathlib.Path(extra), repo_root))

    return result
