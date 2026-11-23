# SPDX-License-Identifier: AGPL-3.0-or-later
"""RDR-229 P3.4 (nexus-p2m1x.22, linda 2026-11-22): nothing in the product updates, deletes or truncates nexus.rdr229_gate_latch.

The gate 3 latch is written once, by CompletionCheck.insertLatch's ``INSERT ... ON CONFLICT DO NOTHING``, and never removed: once gate 3 has
held, P3.5's readers and P3.7's one-row rename stay on the id, and after the first one-row rename the name columns are stale, so a deleted
latch would send readers back to wrong results. nexus_svc holds the uniform UPDATE and DELETE grant every owned nexus table gets (linda
accepted that, against a narrower grant's DDL), so the rule is enforced here instead: in the engine's main Java, its changelogs and the
client's Python, no statement may update, delete, truncate, merge into or drop the table outside vectors-080-1's own rollback, and the only
code that names the table is the writer, its read and its table-existence probe. Tests (service/src/test) may delete the row to stand for a
restore, so they are out of scope.
"""

from __future__ import annotations

import re
import xml.etree.ElementTree as ET
from pathlib import Path

import pytest

pytestmark = pytest.mark.lint

REPO = Path(__file__).resolve().parents[1]
MAIN_JAVA = REPO / "service" / "src" / "main" / "java"
CHANGELOG_DIR = REPO / "service" / "src" / "main" / "resources" / "db" / "changelog"
PYTHON_SRC = REPO / "src"
TABLE = "rdr229_gate_latch"
_NS = "{http://www.liquibase.org/xml/ns/dbchangelog}"

# SQL that changes or removes the row (or the table), schema-qualified or not, any case.
_SQL_FORBIDDEN = re.compile(
    r"\b(?:update\s+(?:only\s+)?|delete\s+from\s+(?:only\s+)?|truncate\s+(?:table\s+)?(?:only\s+)?|merge\s+into\s+|drop\s+table\s+(?:if\s+exists\s+)?)"
    r"(?:\"?nexus\"?\s*\.\s*)?\"?" + TABLE + r"\b",
    re.IGNORECASE,
)

# jOOQ calls that would change or remove rows of the table, through the LATCH constant or a DSL.table naming it.
_JOOQ_FORBIDDEN = re.compile(r"\.(?:update|deleteFrom|delete|truncate|mergeInto|dropTable|dropTableIfExists)\s*\(\s*(?:LATCH\b|[^;]*\"" + TABLE + "\")")

# The code that may name the table: the writer and its read (CompletionCheck), the latch session's settings (CompletionBootWork), and the
# boot read before the engine serves (Gate3Latch, a read-only session's settings; linda's pre-review 2026-11-23).
_MAY_NAME = {"CompletionCheck.java", "CompletionBootWork.java", "Gate3Latch.java"}


def _strip_java_comments(src: str) -> str:
    """Java source without // and /* */ comments; string literals are kept."""
    out, i, n = [], 0, len(src)
    while i < n:
        c = src[i]
        if c == "/" and i + 1 < n and src[i + 1] == "/":
            while i < n and src[i] != "\n":
                i += 1
        elif c == "/" and i + 1 < n and src[i + 1] == "*":
            end = src.find("*/", i + 2)
            i = n if end < 0 else end + 2
        elif c == '"':
            j = i + 1
            while j < n and src[j] != '"':
                j += 2 if src[j] == "\\" else 1
            out.append(src[i : j + 1])
            i = j + 1
        else:
            out.append(c)
            i += 1
    return "".join(out)


def java_violations(name: str, src: str) -> list[str]:
    """THE RULE for one Java file. Shared by the corpus test and the planted-violation tests."""
    code = _strip_java_comments(src)
    out = [f"{name}: {m.group(0)!r}" for m in _JOOQ_FORBIDDEN.finditer(code)]
    out += [f"{name}: SQL text {m.group(0)!r}" for m in _SQL_FORBIDDEN.finditer(code)]
    if f'"{TABLE}"' in code and name not in _MAY_NAME:
        out.append(f"{name}: names {TABLE} in code; only {sorted(_MAY_NAME)} may")
    return out


def sql_violations(name: str, text: str) -> list[str]:
    return [f"{name}: {m.group(0)!r}" for m in _SQL_FORBIDDEN.finditer(text)]


def _changelog_executable_text(path: Path) -> list[tuple[str, str]]:
    """Every changeset's text but its rollback and comment (the rollback of vectors-080-1 drops the table by design)."""
    out = []
    for cs in ET.parse(path).getroot().iter(f"{_NS}changeSet"):
        parts = [ET.tostring(c, encoding="unicode") for c in cs if c.tag not in (f"{_NS}rollback", f"{_NS}comment")]
        out.append((f"{path.name}::{cs.get('id')}", "\n".join(parts)))
    return out


def test_no_product_code_changes_or_removes_the_gate_latch() -> None:
    violations: list[str] = []
    java_files = sorted(MAIN_JAVA.rglob("*.java"))
    for p in java_files:
        violations += java_violations(p.name, p.read_text(encoding="utf-8"))
    changesets = [cs for p in sorted(CHANGELOG_DIR.glob("*.xml")) for cs in _changelog_executable_text(p)]
    for name, text in changesets:
        violations += sql_violations(name, text)
    py_files = sorted(PYTHON_SRC.rglob("*.py"))
    for p in py_files:
        violations += sql_violations(str(p.relative_to(REPO)), p.read_text(encoding="utf-8"))
    assert not violations, "the gate 3 latch is never updated or deleted (linda 2026-11-22):\n" + "\n".join(violations)
    # non-vacuity: the scan saw the product, and the one write it allows is there
    assert len(java_files) > 300 and len(changesets) > 600 and py_files, (len(java_files), len(changesets), len(py_files))
    writer = (MAIN_JAVA / "dev" / "nexus" / "service" / "datamove" / "CompletionCheck.java").read_text(encoding="utf-8")
    assert re.search(r"insertInto\(LATCH\b[^;]*\.onConflictDoNothing\(\)", _strip_java_comments(writer), re.S), (
        "the writer's INSERT ... ON CONFLICT DO NOTHING is the one allowed write"
    )
    assert any(name.startswith("vectors-080-rdr229-gate-latch.xml::") for name, _ in changesets)


@pytest.mark.parametrize(
    "src",
    [
        "ctx.deleteFrom(LATCH).execute();",
        "ctx.update(LATCH).set(LATCH_GATE, \"x\").execute();",
        "ctx.truncate(LATCH).execute();",
        'ctx.deleteFrom(DSL.table(DSL.name("nexus", "rdr229_gate_latch"))).execute();',
        'st.execute("DELETE FROM nexus.rdr229_gate_latch");',
        'st.execute("update rdr229_gate_latch set gate = 1");',
    ],
)
def test_the_rule_catches_a_planted_change_or_removal(src: str) -> None:
    assert java_violations("CompletionCheck.java", "class X { void f() { " + src + " } }")


def test_the_rule_ignores_comments_and_allows_the_writer() -> None:
    allowed = (
        "class X { // ctx.deleteFrom(LATCH) is never written\n"
        "  /* DELETE FROM nexus.rdr229_gate_latch */\n"
        "  void f() { ctx.insertInto(LATCH, LATCH_GATE).values(\"g\").onConflictDoNothing().execute(); } }"
    )
    assert java_violations("CompletionCheck.java", allowed) == []


def test_only_the_writer_files_may_name_the_table() -> None:
    assert java_violations("Elsewhere.java", 'class X { Object t = DSL.table(DSL.name("nexus", "rdr229_gate_latch")); }')


def test_sql_rule_catches_changes_and_drops_in_changelog_text() -> None:
    for text in ("DELETE FROM nexus.rdr229_gate_latch;", "TRUNCATE TABLE nexus.rdr229_gate_latch;", 'UPDATE "nexus"."rdr229_gate_latch" SET x = 1;',
                 "DROP TABLE IF EXISTS nexus.rdr229_gate_latch;"):
        assert sql_violations("x", text), text
    assert sql_violations("x", "INSERT INTO nexus.rdr229_gate_latch VALUES (1) ON CONFLICT DO NOTHING;") == []
