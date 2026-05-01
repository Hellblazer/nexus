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
# from the ban. The lint gate enforces that the comment carries a
# meaningful reason — at least 8 characters of trailing text after the
# colon, which forces a short documentary phrase rather than a placeholder.
# RDR-101 Phase 3 follow-up C (nexus-o6aa.9.8): pre-fix any non-empty
# string passed; ``# epsilon-allow: x`` was indistinguishable from no
# reason. Existing fixture allowlist reasons (forced alias cycle, stale
# span audit, etc.) all pass the 8-char threshold.
ALLOWLIST_TOKEN = "# epsilon-allow:"
ALLOWLIST_REASON_MIN_LENGTH = 8

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


def _collect_db_aliases(tree: ast.AST) -> frozenset[str]:
    """Return Name targets that are bound to ``<expr>._db`` somewhere in
    *tree*. Detects the alias-evasion pattern ``db = cat._db`` (then
    ``db.execute("DELETE...")``) that the bare ``_db.execute`` AST
    matcher would miss (deep-analyst review, 2026-05-01: 7 such sites
    in ``commands/catalog.py`` are currently SELECT-only but one
    character from a write violation).

    Module-scoped — aliases bound inside one function are visible
    everywhere. Conservative (false-positive friendly) since the
    keyword filter still requires the call site to issue a write SQL
    before the lint flags it.
    """
    aliases: set[str] = set()
    for node in ast.walk(tree):
        if not isinstance(node, ast.Assign):
            continue
        if len(node.targets) != 1:
            continue
        target = node.targets[0]
        value = node.value
        if isinstance(target, ast.Name) and isinstance(value, ast.Attribute):
            if value.attr == "_db":
                aliases.add(target.id)
    return frozenset(aliases)


def _receiver_chain_touches_db(receiver: ast.AST) -> bool:
    """Walk a receiver attribute chain. Return True if any segment is
    ``_db`` — catches nested patterns like ``cat._db._conn.execute(...)``
    where the immediate receiver is ``_conn`` but ``_db`` sits one hop
    above.
    """
    cur: ast.AST = receiver
    while isinstance(cur, ast.Attribute):
        if cur.attr == "_db":
            return True
        cur = cur.value
    return False


def _is_db_execute_call(
    node: ast.AST, *, db_aliases: frozenset[str] = frozenset(),
) -> bool:
    """True if *node* is a ``<expr>._db.execute(...)`` call OR an
    aliased equivalent (``db = cat._db; db.execute(...)``) OR a nested
    ``_db.<...>.execute(...)`` chain.

    Covers ``execute`` / ``executemany`` / ``executescript``.
    """
    if not isinstance(node, ast.Call):
        return False
    func = node.func
    if not isinstance(func, ast.Attribute):
        return False
    if func.attr not in {"execute", "executemany", "executescript"}:
        return False
    inner = func.value

    # Direct ``<expr>._db.execute(...)`` — immediate receiver is _db.
    if isinstance(inner, ast.Attribute) and inner.attr == "_db":
        return True

    # Aliased ``<name>.execute(...)`` where <name> was bound to ``_db``
    # somewhere in the module.
    if isinstance(inner, ast.Name) and inner.id in db_aliases:
        return True

    # Nested chain ``<expr>._db.<x>.<y>.execute(...)`` — receiver is
    # an Attribute whose ancestry includes ``_db``. Catches
    # ``cat._db._conn.execute(...)``.
    if isinstance(inner, ast.Attribute) and _receiver_chain_touches_db(inner):
        return True

    return False


def _extract_sql_text(arg: ast.AST) -> str | None:
    """Recover the literal SQL text from *arg* if possible.

    Handles ``ast.Constant`` (plain string literal) and ``ast.JoinedStr``
    (f-string — concatenates leading ``ast.Constant`` segments to recover
    the literal prefix before the first interpolation).

    Returns ``None`` for variable-passed SQL or string concatenation
    (``ast.BinOp``). Those scope boundaries are documented in the
    negative self-tests below; future expansion would need dataflow,
    not just AST shape.
    """
    if isinstance(arg, ast.Constant) and isinstance(arg.value, str):
        return arg.value
    if isinstance(arg, ast.JoinedStr):
        prefix_parts: list[str] = []
        for piece in arg.values:
            if isinstance(piece, ast.Constant) and isinstance(piece.value, str):
                prefix_parts.append(piece.value)
            else:
                break
        return "".join(prefix_parts)
    return None


def _extract_leading_sql_keyword(arg: ast.AST) -> str | None:
    """Return the first SQL keyword (uppercase) from the SQL literal *arg*.

    Returns ``None`` if no keyword can be recovered (variable SQL,
    empty f-string prefix, etc.).
    """
    text = _extract_sql_text(arg)
    if text is None:
        return None
    text = text.lstrip()
    if not text:
        return None
    return text.split(None, 1)[0].upper().rstrip(";")


def _extract_all_sql_keywords(arg: ast.AST) -> list[str]:
    """Return every leading-statement keyword from a multi-statement
    SQL string. ``executescript`` accepts a script of ``;``-delimited
    statements; the leading-keyword check would miss a write hiding
    after a benign first statement (e.g. ``"SELECT 1; DELETE FROM
    documents"``). Walk every statement and return its leading
    keyword.

    For non-script callers (``execute``, ``executemany``) this is
    typically a single-element list; the caller still benefits from
    the multi-statement scan since SQLite happily accepts trailing
    statements in ``execute()`` too.
    """
    text = _extract_sql_text(arg)
    if text is None:
        return []
    keywords: list[str] = []
    for stmt in text.split(";"):
        stripped = stmt.lstrip()
        if not stripped:
            continue
        kw = stripped.split(None, 1)[0].upper().rstrip(";")
        if kw:
            keywords.append(kw)
    return keywords


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
        if len(reason) >= ALLOWLIST_REASON_MIN_LENGTH:
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
    db_aliases = _collect_db_aliases(tree)

    for node in ast.walk(tree):
        if not _is_db_execute_call(node, db_aliases=db_aliases):
            continue
        if not node.args:
            continue

        # ``executescript`` accepts a ``;``-delimited script; check
        # every statement's leading keyword. ``execute`` and
        # ``executemany`` typically take a single statement but SQLite
        # tolerates trailing ones, so the same scan applies.
        keywords = _extract_all_sql_keywords(node.args[0])
        write_kw = next((k for k in keywords if k in WRITE_KEYWORDS), None)
        if write_kw is None:
            continue
        keyword = write_kw

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


def test_allowlist_marker_with_trivial_reason_does_not_suppress(
    tmp_path: pathlib.Path,
) -> None:
    """Self-test (nexus-o6aa.9.8): a one- or few-character "reason"
    after the marker must not suppress. The 8-char minimum forces a
    short documentary phrase rather than a placeholder.
    """
    fake = tmp_path / "fake_trivial_reason.py"
    fake.write_text(
        "def f(cat):\n"
        "    cat._db.execute('UPDATE documents SET x = 1', ())  "
        "# epsilon-allow: x\n"
    )

    assert _scan_file(fake, allow_marker=True), (
        "trivial single-char reason must NOT suppress; require at "
        "least ALLOWLIST_REASON_MIN_LENGTH chars"
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


# ─────────────────────────────────────────────────────────────────────
# RDR-101 Phase 3 follow-up C (nexus-o6aa.9.8): expanded coverage of
# evasion patterns that the original ε gate missed (substantive-critic
# + code-review-expert review, 2026-05-01).
# ─────────────────────────────────────────────────────────────────────


def test_lint_gate_detects_alias_pattern(tmp_path: pathlib.Path) -> None:
    """Self-test: the alias-evasion ``db = cat._db; db.execute(...)``
    pattern must be flagged. ``commands/catalog.py`` already has 7
    such alias sites (currently SELECT-only) — without this detector
    a single character change to one of them would slip past the gate.
    """
    fake = tmp_path / "fake_alias.py"
    fake.write_text(
        "def f(cat):\n"
        "    db = cat._db\n"
        "    db.execute('DELETE FROM documents WHERE id = ?', (1,))\n"
    )

    hits = _scan_file(fake, allow_marker=False)
    assert hits, "alias evasion not flagged — _is_db_execute_call missed it"
    assert "DELETE" in hits[0]


def test_lint_gate_does_not_flag_alias_pattern_for_select(
    tmp_path: pathlib.Path,
) -> None:
    """Self-test: the alias detector is conservative (catches every
    aliased ``execute`` call) but the keyword filter still requires a
    write SQL before flagging. Aliased SELECT must pass.
    """
    fake = tmp_path / "fake_alias_select.py"
    fake.write_text(
        "def f(cat):\n"
        "    db = cat._db\n"
        "    rows = db.execute('SELECT * FROM documents').fetchall()\n"
        "    return rows\n"
    )

    assert _scan_file(fake, allow_marker=False) == []


def test_lint_gate_detects_nested_db_chain(tmp_path: pathlib.Path) -> None:
    """Self-test: ``cat._db._conn.execute('DELETE...')`` — the
    immediate receiver is ``_conn`` but ``_db`` is one hop above. The
    receiver-chain walker must catch this.
    """
    fake = tmp_path / "fake_nested.py"
    fake.write_text(
        "def f(cat):\n"
        "    cat._db._conn.execute('DELETE FROM documents WHERE id = ?', (1,))\n"
    )

    hits = _scan_file(fake, allow_marker=False)
    assert hits, "nested _db.<x>.execute chain not flagged"
    assert "DELETE" in hits[0]


def test_lint_gate_detects_executescript_with_trailing_write(
    tmp_path: pathlib.Path,
) -> None:
    """Self-test: ``executescript('SELECT 1; DELETE FROM x')`` — the
    leading-keyword check would clear it on ``SELECT`` and miss the
    trailing ``DELETE``. The full-script walker must flag it.
    """
    fake = tmp_path / "fake_executescript.py"
    fake.write_text(
        "def f(cat):\n"
        "    cat._db.executescript('SELECT 1; DELETE FROM documents')\n"
    )

    hits = _scan_file(fake, allow_marker=False)
    assert hits, "executescript trailing DELETE not flagged"
    assert "DELETE" in hits[0]


def test_lint_gate_does_not_flag_executescript_all_select(
    tmp_path: pathlib.Path,
) -> None:
    """Self-test: ``executescript('SELECT 1; SELECT 2')`` — multiple
    statements, all reads. Must not be flagged.
    """
    fake = tmp_path / "fake_executescript_select.py"
    fake.write_text(
        "def f(cat):\n"
        "    cat._db.executescript('SELECT 1; SELECT 2')\n"
    )

    assert _scan_file(fake, allow_marker=False) == []


# ─────────────────────────────────────────────────────────────────────
# Documented scope boundaries — patterns the gate intentionally does
# NOT detect today. Encoded as negative self-tests so the boundary is
# explicit in code rather than buried in docstring prose; if a future
# regression makes one of these flag, the developer is forced to
# decide whether the new behaviour is desired.
# ─────────────────────────────────────────────────────────────────────


def test_lint_gate_does_not_detect_variable_sql(tmp_path: pathlib.Path) -> None:
    """Documented scope: SQL passed via a variable (``stmt = "DELETE
    ..."; cat._db.execute(stmt)``) is not detected. Detection would
    require dataflow analysis. The pattern is rare in this codebase
    today; will surface in code review.
    """
    fake = tmp_path / "fake_variable_sql.py"
    fake.write_text(
        "def f(cat):\n"
        "    stmt = 'DELETE FROM documents WHERE id = ?'\n"
        "    cat._db.execute(stmt, (1,))\n"
    )

    assert _scan_file(fake, allow_marker=False) == [], (
        "variable-SQL detection unexpectedly enabled — review the gate "
        "design before pruning this negative test"
    )


def test_lint_gate_does_not_detect_string_concat(
    tmp_path: pathlib.Path,
) -> None:
    """Documented scope: SQL constructed via ``+`` concatenation
    (``cat._db.execute("DELETE" + " FROM documents")``) is not
    detected — the AST node is ``ast.BinOp``, not ``ast.Constant`` or
    ``ast.JoinedStr``. The pattern is unidiomatic; SQL formatting via
    Python ``%`` / ``.format`` / explicit ``+`` is a security
    anti-pattern (parameter binding fixes injection without breaking
    the gate).
    """
    fake = tmp_path / "fake_concat.py"
    fake.write_text(
        "def f(cat):\n"
        "    cat._db.execute('DELETE' + ' FROM documents WHERE id = ?', (1,))\n"
    )

    assert _scan_file(fake, allow_marker=False) == []


def test_lint_gate_does_not_detect_fstring_with_interpolated_first_token(
    tmp_path: pathlib.Path,
) -> None:
    """Documented scope: f-string where the leading SQL keyword sits
    inside an interpolated expression (``f"{op} FROM documents"``).
    The ``ast.JoinedStr`` value list starts with a ``FormattedValue``,
    not an ``ast.Constant``, so the leading-keyword extractor returns
    empty. Detection would require constant-folding of the
    interpolated expression — not worth the implementation cost for
    a pattern that does not occur today.
    """
    fake = tmp_path / "fake_fstring_interp.py"
    fake.write_text(
        "def f(cat, op):\n"
        "    cat._db.execute(f'{op} FROM documents WHERE id = ?', (1,))\n"
    )

    assert _scan_file(fake, allow_marker=False) == []
