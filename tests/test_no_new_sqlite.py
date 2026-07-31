# SPDX-License-Identifier: AGPL-3.0-or-later
"""NO-SQLITE terminal ratchet (Hal directive 2026-07-18; RDR-186 P4,
bead nexus-146xx.18).

Nexus migrated from SQLite to PG in EVERY mode, and the migration is
COMPLETE on the client: RDR-158 P4 (nexus-i711w) deleted the SQLite
stores, schema, and migration chain; RDR-155 P4b deleted Chroma; RDR-186
P1-P3 retired the opt-out and the stray substrates. This suite is the
closing ratchet — it no longer freezes a census of known debt, it
asserts the debt is ZERO:

* NO inline SQLite DDL (``CREATE [VIRTUAL] TABLE``) anywhere in ``src/``.
* NO per-line ``# epsilon-allow:`` escape tokens anywhere in ``src/`` —
  the self-service exemption mechanism is retired with the last SQLite
  site (RDR-186 D1/D6). Surviving legitimate sites are enumerated in
  EXPLICIT NAMED allowlists in ``storage_boundary_lint.py`` (per-file
  exact counts, reason beside each entry) — growing one is a reviewed
  edit to that file, never a comment.
* NO ``sqlite3.connect`` anywhere in ``src/`` beyond the three READ-ONLY
  frozen-migration-source diagnostics named in
  ``storage_boundary_lint.SQLITE_CONNECT_ALLOWLIST``.
* ``ALTER TABLE`` text appears only at the named PG/Liquibase sites in
  :data:`PG_ALTER_TABLE_ALLOWLIST` — every survivor is Postgres DDL
  prose (changeset recipes, admin-SQL allowlist patterns), not SQLite.

A NEW SQLite site is a HARD TEST FAILURE, not a number to bump: there is
no census left to edit and no escape token left to write. Directive of
record: T2 ``nexus/directive-no-sqlite-pg-everywhere``; AGENTS.md hot
rule; retirement epic nexus-146xx.

Scanner is deliberately dumb (regex, per file) for the DDL and token
axes: the goal is a tripwire that cannot be silently satisfied.
``storage_boundary_lint.py`` remains the AST-precise boundary check
(``sqlite3.connect`` incl. aliased forms — a different axis than DDL
statements). NOTE (zero-review Sig-3): the ``sqlite3.connect`` arm scans
the whole of ``src/`` UNCONDITIONALLY — ``allowlist_prefixes`` gates only
the ``voyageai.Client`` arm; the connect assertion below passes ``()``
merely so the voyageai arm is also prefix-free in that scan, not because
the connect arm needs it.
"""
from __future__ import annotations

import pathlib
import re

REPO_ROOT = pathlib.Path(__file__).parent.parent
SRC = REPO_ROOT / "src" / "nexus"

_DDL_RE = re.compile(r"CREATE\s+(?:VIRTUAL\s+)?TABLE", re.IGNORECASE)
_ALTER_RE = re.compile(r"ALTER\s+TABLE", re.IGNORECASE)
_EPSILON_RE = re.compile(r"#\s*epsilon-allow\s*:")

_DIRECTIVE = (
    "NO SQLite (Hal directive 2026-07-18): SQLite is DELETED as a storage "
    "substrate — there is no census to bump and no per-line escape token. "
    "New persistent state goes to PG through Liquibase via the engine, in "
    "every mode. A legitimate new PG DDL mention belongs in the named "
    "allowlist in this file (an explicit Hal decision recorded on a bead, "
    "referenced inline); anything SQLite-shaped is forbidden outright. "
    "See T2 nexus/directive-no-sqlite-pg-everywhere and epic nexus-146xx."
)

#: The only files in ``src/`` allowed to contain ``ALTER TABLE`` text — all
#: of it is POSTGRES DDL prose (Liquibase changeset recipes an operator
#: hands to the engine, or the admin-SQL allowlist that recognises PG
#: statements), never an executed SQLite statement. The dumb regex cannot
#: tell dialects apart, so the allowlist names each survivor with its
#: reason; exact counts, both directions (a stale entry is a lie about
#: the ledger).
PG_ALTER_TABLE_ALLOWLIST: dict[str, int] = {
    # PG changeset RECIPE for the retired runtime promotion verb: module
    # docstring template + PROMOTION_RETIRED message (nexus-70x7y).
    "src/nexus/aspect_promotion.py": 2,
    # Comment stating the runtime statement is GONE (nexus-70x7y).
    "src/nexus/commands/enrich.py": 1,
    # RDR-180 .6: PG `ALTER TABLE ... VALIDATE CONSTRAINT` allowlist regex
    # + docstring.
    "src/nexus/db/admin_sql.py": 2,
    # PG/Liquibase RLS syntax in a comment (health.py:1673).
    "src/nexus/health.py": 1,
    # RDR-180 .6: PG `ALTER TABLE ... VALIDATE CONSTRAINT` statement text.
    "src/nexus/upgrade_ladder/rungs/chash_rekey.py": 1,
}


def _count_matches(pattern: re.Pattern[str]) -> dict[str, int]:
    counts: dict[str, int] = {}
    for path in SRC.rglob("*.py"):
        if "__pycache__" in path.parts:
            continue
        n = len(pattern.findall(path.read_text(encoding="utf-8")))
        if n:
            counts[path.relative_to(REPO_ROOT).as_posix()] = n
    return counts


def test_zero_inline_sqlite_ddl() -> None:
    """RDR-186 P4 end-state D1: src/ carries ZERO inline SQLite CREATE
    TABLE statements. The last inline-DDL site (db/migrations.py) died in
    RDR-158 P4 Stage 4; this asserts it stays dead."""
    live = _count_matches(_DDL_RE)
    assert not live, (
        f"inline SQLite DDL appeared at {live}: this is a HARD FAILURE, "
        f"not a census to bump. {_DIRECTIVE}"
    )


def test_zero_epsilon_allow_tokens() -> None:
    """RDR-186 P4 end-state D6: the per-line escape token is RETIRED with
    the last SQLite site — with no site left there is nothing to allow.
    Surviving read-only diagnostics are named (file + exact count + reason)
    in storage_boundary_lint.py's explicit allowlists instead."""
    live = _count_matches(_EPSILON_RE)
    assert not live, (
        f"'# epsilon-allow:' token(s) appeared at {live}: the self-service "
        f"per-line exemption is retired. Route through the substrate, or — "
        f"for a genuinely irreducible site — add a named entry to the "
        f"appropriate allowlist in storage_boundary_lint.py with an "
        f"explicit Hal decision recorded on a bead. {_DIRECTIVE}"
    )


def test_alter_table_only_at_named_pg_sites() -> None:
    """Every surviving ``ALTER TABLE`` mention is named PG/Liquibase prose.
    Exact in both directions: growth anywhere is a hard failure; shrink
    means a survivor died — lower its entry so the ledger stays exact."""
    live = _count_matches(_ALTER_RE)
    grown = sorted(
        f"{f}: {live.get(f, 0)} > {PG_ALTER_TABLE_ALLOWLIST.get(f, 0)}"
        for f in live.keys() | PG_ALTER_TABLE_ALLOWLIST.keys()
        if live.get(f, 0) > PG_ALTER_TABLE_ALLOWLIST.get(f, 0)
    )
    shrunk = sorted(
        f"{f}: {live.get(f, 0)} < {PG_ALTER_TABLE_ALLOWLIST.get(f, 0)}"
        for f in live.keys() | PG_ALTER_TABLE_ALLOWLIST.keys()
        if live.get(f, 0) < PG_ALTER_TABLE_ALLOWLIST.get(f, 0)
    )
    assert not grown, (
        f"ALTER TABLE text GREW at {grown}: if it is PG DDL prose, add a "
        f"named allowlist entry here with its reason; if it is SQLite, it "
        f"is forbidden outright. {_DIRECTIVE}"
    )
    assert not shrunk, (
        f"stale PG_ALTER_TABLE_ALLOWLIST entr(y/ies) {shrunk}: the text was "
        "removed (good!) — lower the entry so the ledger stays exact."
    )


def test_no_unallowlisted_sqlite_connect_anywhere() -> None:
    """RDR-186 P4 end-state D1/D6, connect axis: the AST scan (aliased
    forms included) over ALL of src/ — path prefixes disabled, so db/ is
    in scope too — finds exactly the named read-only frozen-source
    diagnostics and nothing else."""
    from nexus.storage_boundary_lint import (
        SQLITE_CONNECT_ALLOWLIST,
        scan_repo,
    )

    result = scan_repo(repo_root=REPO_ROOT, allowlist_prefixes=())
    sqlite_violations = [
        (v.file, v.line) for v in result.violations if v.symbol == "sqlite3.connect"
    ]
    assert sqlite_violations == [], (
        f"sqlite3.connect outside the named allowlist at {sqlite_violations}: "
        f"this is a HARD FAILURE — SQLite is deleted as a storage substrate "
        f"and there is no escape token. {_DIRECTIVE}"
    )
    # Exact-ledger discipline: the live allowlisted population equals the
    # allowlist sum. A survivor being deleted must lower its named entry.
    assert result.sqlite_allowlisted_connects == sum(
        SQLITE_CONNECT_ALLOWLIST.values()
    ), (
        f"live allowlisted-connect count "
        f"({result.sqlite_allowlisted_connects}) != allowlist sum "
        f"({sum(SQLITE_CONNECT_ALLOWLIST.values())}): a named survivor was "
        "removed (good!) — lower its SQLITE_CONNECT_ALLOWLIST entry so the "
        "ledger stays exact."
    )
    # The terminal survivor set is exactly the three read-only diagnostics.
    assert sum(SQLITE_CONNECT_ALLOWLIST.values()) == 3
    # Liveness: an allowlist entry naming a dead file is a free slot a
    # future connect could squat in without review.
    dead = sorted(
        f for f in SQLITE_CONNECT_ALLOWLIST if not (REPO_ROOT / f).is_file()
    )
    assert not dead, (
        f"SQLITE_CONNECT_ALLOWLIST entr(y/ies) name no live file: {dead} — "
        "delete the entry with the file."
    )


def test_scanner_is_nonvacuous() -> None:
    """The empty-asserts above must FAIL when debt exists — prove the
    scanner still sees the tree and the regexes still bite, so an
    all-clean result means clean, not broken scan (path drift, encoding)."""
    # The tree is substantial: rglob must see hundreds of files.
    n_files = sum(
        1 for p in SRC.rglob("*.py") if "__pycache__" not in p.parts
    )
    assert n_files > 200, f"scan saw only {n_files} files under {SRC}"
    # The regexes still match their targets (synthetic positives).
    assert _DDL_RE.search("CREATE TABLE t (id INTEGER)")
    assert _DDL_RE.search("create virtual table t using fts5(x)")
    assert _ALTER_RE.search("ALTER TABLE t ADD COLUMN c")
    assert _EPSILON_RE.search("conn = f()  # epsilon-allow: some old reason")
    # The ALTER scan sees the live named survivors (non-empty by design),
    # which proves _count_matches reads real file content end-to-end.
    assert _count_matches(_ALTER_RE), (
        "the ALTER scan sees nothing at all — if every named PG survivor "
        "truly died, empty PG_ALTER_TABLE_ALLOWLIST first; otherwise the "
        "scanner broke"
    )


# ── Act 3 (service/ half): the Java engine tree carries NO SQLite ────────────
#
# nexus-146xx.18's end state is verbatim "no sqlite3.connect / CREATE TABLE
# outside the allowlist across src/ AND service/" — the zero-critique
# Critical: a one-time clean grep is a point-in-time claim, not a ratchet.
# This mechanizes the service/ half. Allowed, by name:
#   * Liquibase changelogs (service/src/main/resources/db/changelog/**) —
#     the ONLY sanctioned DDL home (ALL-DDL-through-Liquibase directive).
#   * the test-scoped sqlite-jdbc dependency in service/pom.xml — the
#     deliberate FTS5 unicode61 parity comparand (RDR-155 P3). Its scope
#     must STAY test; a scope change is a violation, not a bump.

_SERVICE = REPO_ROOT / "service" / "src"
_SERVICE_CHANGELOG = REPO_ROOT / "service" / "src" / "main" / "resources" / "db" / "changelog"
_SERVICE_TEST = _SERVICE / "test"

_JAVA_SQLITE_RE = re.compile(r"org\.sqlite|jdbc:sqlite", re.IGNORECASE)
_JAVA_DDL_RE = re.compile(r"CREATE\s+TABLE", re.IGNORECASE)


def _service_files():
    for ext in ("*.java", "*.xml", "*.sql", "*.properties"):
        yield from _SERVICE.rglob(ext)


def test_service_tree_has_no_sqlite():
    """No SQLite JDBC surface anywhere under service/src except the
    test-scoped parity comparand (which lives in pom.xml, not src/)."""
    assert _SERVICE.is_dir(), "service/src moved — update the Act-3 scan"
    offenders = []
    for path in _service_files():
        rel = path.relative_to(REPO_ROOT)
        text = path.read_text(errors="replace")
        for m in _JAVA_SQLITE_RE.finditer(text):
            line = text.count("\n", 0, m.start()) + 1
            # The FTS5-parity comparand lives in TEST scope: Java test
            # sources may reference the sqlite JDBC driver; main must not.
            if _SERVICE_TEST in path.parents:
                continue
            offenders.append(f"{rel}:{line}")
    assert not offenders, (
        "SQLite JDBC surface in the engine MAIN tree: "
        + ", ".join(offenders)
        + "\nThe engine is PG-only (T2 nexus/directive-no-sqlite-pg-everywhere); "
        "this is a hard failure, not a number to bump (nexus-146xx.18 Act 3)."
    )


def test_service_ddl_lives_only_in_liquibase():
    """CREATE TABLE under service/src/main appears ONLY in Liquibase
    changelogs; Java test sources may build PG fixtures."""
    offenders = []
    main_tree = _SERVICE / "main"
    for path in main_tree.rglob("*"):
        if not path.is_file() or path.suffix not in (".java", ".xml", ".sql", ".properties"):
            continue
        if _SERVICE_CHANGELOG in path.parents:
            continue
        text = path.read_text(errors="replace")
        for m in _JAVA_DDL_RE.finditer(text):
            line = text.count("\n", 0, m.start()) + 1
            offenders.append(f"{path.relative_to(REPO_ROOT)}:{line}")
    assert not offenders, (
        "DDL outside Liquibase changelogs in the engine main tree: "
        + ", ".join(offenders)
        + "\nALL DDL goes through Liquibase (standing directive); hard "
        "failure, not a number to bump (nexus-146xx.18 Act 3)."
    )


def test_service_sqlite_jdbc_dependency_stays_test_scoped():
    """pom.xml's sqlite-jdbc (the FTS5 parity comparand) must keep
    <scope>test</scope> — a scope promotion would put a SQLite driver on
    the engine's runtime classpath."""
    pom = (REPO_ROOT / "service" / "pom.xml").read_text(errors="replace")
    m = re.search(
        r"<artifactId>sqlite-jdbc</artifactId>.*?(<scope>(\w+)</scope>|</dependency>)",
        pom, re.DOTALL,
    )
    assert m is not None, (
        "sqlite-jdbc dependency not found in service/pom.xml — if it was "
        "removed entirely, delete this test WITH it; if renamed, re-point."
    )
    assert m.group(2) == "test", (
        f"sqlite-jdbc scope is {m.group(2)!r}, not 'test' — the FTS5 parity "
        "comparand must never reach the engine runtime classpath "
        "(nexus-146xx.18 Act 3)."
    )
