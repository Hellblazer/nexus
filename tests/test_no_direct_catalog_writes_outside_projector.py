# SPDX-License-Identifier: AGPL-3.0-or-later
"""RDR-101 Phase 3 PR ε: lint gate.

Forbids direct INSERT / UPDATE / DELETE / REPLACE / CREATE / DROP / ALTER /
TRUNCATE against catalog SQLite from any consumer outside the catalog
module (``src/nexus/catalog/``). The catalog module IS the projector and
is the only authorised mutation surface. Every other write must flow
through public catalog API (which under ``NEXUS_EVENT_SOURCED=1``
emits an event and projects).

WITH TEETH: this test guards the RDR-101 Phase 3 irreversibility window.
Once PR ζ flips ``NEXUS_EVENT_SOURCED`` ON by default, any direct write
outside the projector silently desyncs SQLite from the event log;
``nx catalog doctor --replay-equality`` then becomes meaningless. The
test must FAIL deterministically when a violating ``_db.execute`` call
appears in any non-allowlisted file. Verified at authoring time by
inserting a synthetic offender and re-running.

Allowlist policy
----------------
* All of ``src/nexus/catalog/`` is allowed (it is the projector module).
* Test fixtures that intentionally bypass the public API to construct
  invariant-violating state (e.g. forced alias cycle, forced stale span)
  are allowed when the offending line carries the trailing comment token
  ``# epsilon-allow: <reason>``. The reason text must be present.

Pattern reference: ``tests/test_hook_drift_guard.py`` uses the same
AST-walk + offender-aggregation shape.
"""
from __future__ import annotations

import ast
import pathlib

PROJECT_ROOT = pathlib.Path(__file__).parent.parent
SRC_ROOT = PROJECT_ROOT / "src" / "nexus"
TESTS_ROOT = PROJECT_ROOT / "tests"
SCRIPTS_ROOT = PROJECT_ROOT / "scripts"

# Files / directories where direct catalog writes are authorised.
# Path components are matched as POSIX-style relative paths.
ALLOWED_PREFIXES: tuple[str, ...] = (
    "src/nexus/catalog/",   # the projector module itself
)

# Allowlist for tests: lines tagged with this comment token are exempt
# from the ban. The lint gate enforces that the comment carries a reason
# (any non-empty trailing text after the colon).
ALLOWLIST_TOKEN = "# epsilon-allow:"

# SQL keywords that mutate catalog state. Every one of these on a
# ``*._db.execute(<literal>, ...)`` call is a violation when the call
# site sits outside ``ALLOWED_PREFIXES``.
WRITE_KEYWORDS: frozenset[str] = frozenset({
    "INSERT",
    "UPDATE",
    "DELETE",
    "REPLACE",
    "CREATE",
    "DROP",
    "ALTER",
    "TRUNCATE",
})


def _is_db_execute_call(node: ast.AST) -> bool:
    """True if *node* is a ``<expr>._db.execute(...)`` call.

    Covers ``cat._db.execute``, ``self._db.execute``,
    ``catalog._db.execute``, ``foo.bar._db.execute``, etc. Also catches
    ``executemany`` / ``executescript`` even though no current call site
    uses them — the lint must remain future-proof.
    """
    if not isinstance(node, ast.Call):
        return False
    func = node.func
    if not isinstance(func, ast.Attribute):
        return False
    if func.attr not in {"execute", "executemany", "executescript"}:
        return False
    inner = func.value
    if not isinstance(inner, ast.Attribute):
        return False
    return inner.attr == "_db"


def _extract_leading_sql_keyword(arg: ast.AST) -> str | None:
    """Return the first SQL keyword (uppercase) from the SQL literal *arg*.

    Handles:
      * ``ast.Constant`` (plain string literal)
      * ``ast.JoinedStr`` (f-string) — concatenates leading
        ``ast.Constant`` segments to recover the literal prefix before
        the first interpolation.

    Returns ``None`` for any other node shape (e.g. SQL passed via a
    variable). Variable-passed SQL is rare in this codebase and the few
    occurrences will surface in code review.
    """
    if isinstance(arg, ast.Constant) and isinstance(arg.value, str):
        text = arg.value
    elif isinstance(arg, ast.JoinedStr):
        prefix_parts: list[str] = []
        for piece in arg.values:
            if isinstance(piece, ast.Constant) and isinstance(piece.value, str):
                prefix_parts.append(piece.value)
            else:
                break
        text = "".join(prefix_parts)
    else:
        return None

    text = text.lstrip()
    if not text:
        return None

    first_token = text.split(None, 1)[0].upper().rstrip(";")
    return first_token


def _line_has_allowlist_marker(source_lines: list[str], lineno: int) -> bool:
    """True if the line at *lineno* (1-indexed) carries the allowlist
    token followed by a non-empty reason.

    Matches the token anywhere on the line (so multi-line ``execute``
    calls can carry the token on the first line of the call). The reason
    text must be non-empty after the colon.
    """
    if lineno < 1 or lineno > len(source_lines):
        return False

    # Scan the call's first line plus a small forward window: a multi-line
    # ``cat._db.execute(...)`` call may carry the marker on any of its
    # constituent lines. 8 lines is generous; the existing fixtures span
    # at most 2.
    for offset in range(8):
        idx = lineno - 1 + offset
        if idx >= len(source_lines):
            break
        line = source_lines[idx]
        token_pos = line.find(ALLOWLIST_TOKEN)
        if token_pos < 0:
            continue
        reason = line[token_pos + len(ALLOWLIST_TOKEN):].strip()
        if reason:
            return True
        # Token without reason text — keep scanning; the file may have
        # the canonical marker further down.
    return False


def _scan_file(path: pathlib.Path, *, allow_marker: bool) -> list[str]:
    """Return human-readable violation strings for *path*.

    *allow_marker* enables the ``# epsilon-allow:`` allowlist comment.
    For ``src/`` and ``scripts/`` the marker is disabled — production
    code must never contain a direct write.
    """
    try:
        source = path.read_text()
    except (UnicodeDecodeError, OSError):
        return []
    try:
        tree = ast.parse(source, filename=str(path))
    except SyntaxError:
        return []

    source_lines = source.splitlines()
    violations: list[str] = []

    for node in ast.walk(tree):
        if not _is_db_execute_call(node):
            continue
        if not node.args:
            continue

        keyword = _extract_leading_sql_keyword(node.args[0])
        if keyword is None or keyword not in WRITE_KEYWORDS:
            continue

        if allow_marker and _line_has_allowlist_marker(source_lines, node.lineno):
            continue

        violations.append(f"line {node.lineno}: {keyword} via _db.{node.func.attr}(...)")

    return violations


def _path_is_allowed(rel_posix: str) -> bool:
    return any(rel_posix.startswith(prefix) for prefix in ALLOWED_PREFIXES)


def _gather_violations(
    root: pathlib.Path,
    *,
    allow_marker: bool,
    skip_self: bool = False,
) -> dict[str, list[str]]:
    """Walk *root* and return ``{rel_path: [violations]}``.

    *skip_self* skips this very test file (for the ``tests/`` walk).
    """
    if not root.exists():
        return {}

    self_rel = pathlib.Path(__file__).resolve().relative_to(PROJECT_ROOT).as_posix()
    offenders: dict[str, list[str]] = {}

    for py in root.rglob("*.py"):
        rel = py.relative_to(PROJECT_ROOT).as_posix()
        if _path_is_allowed(rel):
            continue
        if skip_self and rel == self_rel:
            continue
        hits = _scan_file(py, allow_marker=allow_marker)
        if hits:
            offenders[rel] = hits

    return offenders


def test_no_direct_catalog_writes_in_src() -> None:
    """RDR-101 Phase 3 ε: no production source file outside
    ``src/nexus/catalog/`` may issue an INSERT / UPDATE / DELETE /
    REPLACE / CREATE / DROP / ALTER / TRUNCATE through ``_db.execute``.

    Stage B (PRs #438–#445) closed every legitimate live-write path by
    routing through the catalog hook. After this gate lands, PR ζ can
    safely flip ``NEXUS_EVENT_SOURCED=1`` by default without exposing
    unaudited direct writers.
    """
    offenders = _gather_violations(SRC_ROOT, allow_marker=False)

    if offenders:
        formatted = "\n".join(
            f"  {path}:\n    " + "\n    ".join(hits)
            for path, hits in sorted(offenders.items())
        )
        raise AssertionError(
            "RDR-101 Phase 3 ε: direct catalog writes outside the "
            "projector module are forbidden. Offenders must route "
            "through Catalog public API (which under "
            "NEXUS_EVENT_SOURCED=1 emits + projects). Violations:\n"
            f"{formatted}"
        )


def test_no_direct_catalog_writes_in_scripts() -> None:
    """Operator scripts may not bypass the projector either. Repair
    operations should land as ``nx catalog repair-*`` verbs that emit
    events through the public Catalog API.
    """
    offenders = _gather_violations(SCRIPTS_ROOT, allow_marker=False)

    if offenders:
        formatted = "\n".join(
            f"  {path}:\n    " + "\n    ".join(hits)
            for path, hits in sorted(offenders.items())
        )
        raise AssertionError(
            "RDR-101 Phase 3 ε: scripts/ may not issue direct catalog "
            "writes. Rewrite as `nx catalog repair-*` verbs or retire. "
            "Violations:\n"
            f"{formatted}"
        )


def test_no_direct_catalog_writes_in_tests_without_allowlist_marker() -> None:
    """Tests may carry intentional invariant-violation fixtures (e.g.
    forced alias cycle, forced stale span). Each such site MUST tag the
    offending line with ``# epsilon-allow: <reason>`` so the lint gate
    treats it as a documented exception. Untagged direct writes in
    tests are still violations.
    """
    offenders = _gather_violations(TESTS_ROOT, allow_marker=True, skip_self=True)

    if offenders:
        formatted = "\n".join(
            f"  {path}:\n    " + "\n    ".join(hits)
            for path, hits in sorted(offenders.items())
        )
        raise AssertionError(
            "RDR-101 Phase 3 ε: untagged direct catalog writes in tests. "
            "Either rewrite to use Catalog public API, or tag the line "
            f"with `{ALLOWLIST_TOKEN} <reason>`. Violations:\n"
            f"{formatted}"
        )


def test_lint_gate_detects_synthetic_violation(tmp_path: pathlib.Path) -> None:
    """Self-test: the AST walker must flag a freshly authored
    ``cat._db.execute("DELETE FROM ...")`` call. Guards against silent
    breakage of the gate (e.g. AST-shape regression after a Python
    upgrade, or accidental short-circuit in ``_is_db_execute_call``).
    """
    fake = tmp_path / "fake_offender.py"
    fake.write_text(
        "def f(cat):\n"
        "    cat._db.execute('DELETE FROM documents WHERE id = ?', (1,))\n"
    )

    hits = _scan_file(fake, allow_marker=False)
    assert hits, "lint gate failed to detect a literal DELETE — AST shape changed?"
    assert "DELETE" in hits[0]


def test_lint_gate_detects_fstring_violation(tmp_path: pathlib.Path) -> None:
    """Self-test: f-string SQL must be detected via the leading
    ``ast.Constant`` segment of the ``ast.JoinedStr``.
    """
    fake = tmp_path / "fake_fstring_offender.py"
    fake.write_text(
        "def f(cat, table):\n"
        "    cat._db.execute(f'DELETE FROM {table} WHERE id = ?', (1,))\n"
    )

    hits = _scan_file(fake, allow_marker=False)
    assert hits, "lint gate failed to detect an f-string DELETE"
    assert "DELETE" in hits[0]


def test_allowlist_marker_suppresses_violation(tmp_path: pathlib.Path) -> None:
    """Self-test: the ``# epsilon-allow: <reason>`` marker must suppress
    the violation when *and only when* a non-empty reason is present.
    """
    fake = tmp_path / "fake_with_marker.py"
    fake.write_text(
        "def f(cat):\n"
        "    cat._db.execute('UPDATE documents SET x = 1', ())  "
        "# epsilon-allow: forced fixture for cycle test\n"
    )

    assert _scan_file(fake, allow_marker=True) == []
    # Without the allow_marker flag (production scope), the marker is
    # ignored and the line is still flagged.
    assert _scan_file(fake, allow_marker=False), (
        "marker must NOT suppress in production scope"
    )


def test_allowlist_marker_without_reason_does_not_suppress(
    tmp_path: pathlib.Path,
) -> None:
    """Self-test: a bare ``# epsilon-allow:`` with no reason text must
    NOT suppress. The lint gate insists on documentation.
    """
    fake = tmp_path / "fake_bare_marker.py"
    fake.write_text(
        "def f(cat):\n"
        "    cat._db.execute('UPDATE documents SET x = 1', ())  # epsilon-allow:\n"
    )

    assert _scan_file(fake, allow_marker=True), (
        "bare marker without reason must NOT suppress"
    )


def test_select_calls_are_not_flagged(tmp_path: pathlib.Path) -> None:
    """Self-test: SELECT (and fetchone/fetchall callers) must never be
    flagged. The lint gate is for mutations only.
    """
    fake = tmp_path / "fake_select.py"
    fake.write_text(
        "def f(cat):\n"
        "    rows = cat._db.execute('SELECT * FROM documents').fetchall()\n"
        "    return rows\n"
    )

    assert _scan_file(fake, allow_marker=False) == []
