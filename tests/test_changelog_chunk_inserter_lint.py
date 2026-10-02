# SPDX-License-Identifier: AGPL-3.0-or-later
"""RDR-223 Phase 3 (nexus-z0o2p.24 follow-up): every SQL function that inserts into ``nexus.chunks`` is named.

The ownership rule (RDR-223) covers the routes that write chunks through the handler's
``OwnershipGuard``. A SQL function that does ``INSERT INTO nexus.chunks`` is outside that guard, and
``OwnershipGuardCoverageScan`` cannot see it (it scans Java sources, by method name). The prose that
names those functions (the ``OwnershipGuard`` Javadoc, RDR-223's "known exceptions") was a hand-kept
list; this lint is its source of truth. It reads ``db.changelog-master.xml`` in include order, takes the
LAST definition of every function (``CREATE [OR REPLACE] FUNCTION name``; a later ``DROP FUNCTION``
removes it; ``<rollback>`` blocks are not definitions, they restore an OLDER one), and fails when a live
function body contains ``INSERT INTO nexus.chunks`` and the function is not in ``_CHUNK_INSERTER_ALLOWLIST``
below, each entry with its reason. It also fails on an entry that no longer inserts or no longer exists,
so the list cannot rot in the other direction.

WHO MUST EXTEND THE ALLOWLIST. A change that adds (or re-defines into) such a function. That is the point of
the lint, not a nuisance: ownerless chunks are what RDR-223 exists to stop, and a function that writes them
must say why it is allowed to. Two unlanded branches will trip it when they land, by design: the reaper
(``vectors-024`` ``reaper_quarantine_chunks``, a NEW inserter; ``vectors-022`` re-defines
``gc_quarantine_orphans`` and ``gc_quarantine_orphans_bounded``, which moves their live definition) and the
quarantine restore verb (``vectors-025`` ``quarantine_restore_chunks``). Whoever lands them adds those entries
here (and nothing else needs hand-editing: the prose points here).

WHAT IT DOES NOT SEE. Dynamic SQL (``EXECUTE format('INSERT INTO ...')``), an insert through a view or
a differently named table, and a function defined outside the Liquibase changelog. A body it cannot
delimit (a ``CREATE FUNCTION`` with no dollar-quoted body) fails the lint instead of being skipped.
Functions are keyed by name and parameter count, because a different parameter list is a different
function in PostgreSQL.
"""
from __future__ import annotations

import re
import xml.etree.ElementTree as ET
from pathlib import Path

import pytest

from tests.test_changelog_rls_lint import CHANGELOG_DIR, REPO_ROOT

pytestmark = pytest.mark.lint

_NS = "{http://www.liquibase.org/xml/ns/dbchangelog}"
_MASTER = "db.changelog-master.xml"

_QUARANTINES = "copies the collection's manifest-less chunks into its quarantine collection (they are ownerless already; nothing new becomes ownerless)"
_RESTORES = "copies a quarantined chunk back to its origin collection only when a live manifest row there re-references it (it becomes owned, not ownerless)"

#: The functions whose LIVE definition inserts into ``nexus.chunks``, with why each may. The source of truth
#: for the "known exceptions" prose in ``OwnershipGuard.java`` and RDR-223. Add an entry, with a reason, when
#: a change lands a new inserter; remove one when its function stops inserting.
_CHUNK_INSERTER_ALLOWLIST: dict[str, str] = {
    "gc_quarantine_orphans": _QUARANTINES,
    "gc_quarantine_orphans_bounded": _QUARANTINES + "; bounded variant",
    "gc_restore_rereferenced": _RESTORES,
    "gc_restore_rereferenced_bounded": _RESTORES + "; bounded variant",
}

_DEFINITION = re.compile(
    r"\bCREATE\s+(?:OR\s+REPLACE\s+)?FUNCTION\s+(?P<create>[\w.\"]+)\s*\("
    r"|\bDROP\s+FUNCTION\s+(?:IF\s+EXISTS\s+)?(?P<drop>[\w.\"]+)\s*\(",
    re.IGNORECASE,
)
_DOLLAR_TAG = re.compile(r"\$(\w*)\$")
_CHUNK_INSERT = re.compile(r"\bINSERT\s+INTO\s+(?:nexus\.)?\"?chunks\"?(?![\w])", re.IGNORECASE)


def _bare(name: str) -> str:
    return name.rsplit(".", 1)[-1].strip('"').lower()


def _close_paren(text: str, open_idx: int) -> tuple[int, int]:
    """``(index of the matching ')', parameter count)`` for the '(' at ``open_idx``."""
    depth, params, seen = 0, 0, False
    for i in range(open_idx, len(text)):
        c = text[i]
        if c == "(":
            depth += 1
        elif c == ")":
            depth -= 1
            if depth == 0:
                return i, params + (1 if seen else 0)
        elif c == "," and depth == 1:
            params += 1
        elif not c.isspace() and depth >= 1:
            seen = True
    raise AssertionError("unbalanced parenthesis in a CREATE/DROP FUNCTION signature")


def _body(text: str, after: int, name: str, where: str) -> str:
    """The dollar-quoted body that follows a ``CREATE FUNCTION`` signature, tag to tag."""
    m = _DOLLAR_TAG.search(text, after)
    assert m, f"{where}: cannot delimit the body of function {name} (no dollar-quoted body); extend this lint"
    end = text.find(m.group(0), m.end())
    assert end != -1, f"{where}: unterminated dollar-quoted body of function {name}"
    return text[m.end():end]


def _changeset_sql(changeset: ET.Element) -> str:
    """The text of a changeset EXCLUDING its ``<rollback>`` blocks, which restore an older definition."""
    return "\n".join(
        "".join(child.itertext()) for child in changeset if child.tag != f"{_NS}rollback"
    )


def _included_files(changelog_dir: Path) -> list[Path]:
    master = ET.parse(changelog_dir / _MASTER).getroot()
    assert not master.findall(f"{_NS}includeAll"), "includeAll: this lint reads includes in written order only"
    files = []
    for inc in master.findall(f"{_NS}include"):
        f = changelog_dir / Path(inc.attrib["file"]).name
        assert f.is_file(), f"{_MASTER} includes {inc.attrib['file']}, which does not exist"
        files.append(f)
    return files


def live_functions(changelog_dir: Path = CHANGELOG_DIR) -> dict[tuple[str, int], tuple[str, str]]:
    """``{(function name, parameter count): (file, live body)}`` after walking the master's includes in order."""
    live: dict[tuple[str, int], tuple[str, str]] = {}
    for f in _included_files(changelog_dir):
        root = ET.parse(f).getroot()
        assert not root.findall(f"{_NS}include"), f"{f.name}: a nested include; this lint reads one level"
        for cs in root.iter(f"{_NS}changeSet"):
            sql = _changeset_sql(cs)
            for m in _DEFINITION.finditer(sql):
                name = _bare(m.group("create") or m.group("drop"))
                close, params = _close_paren(sql, m.end() - 1)
                if m.group("drop"):
                    live.pop((name, params), None)
                else:
                    live[(name, params)] = (f.name, _body(sql, close, name, f.name))
    return live


def live_chunk_inserters(changelog_dir: Path = CHANGELOG_DIR) -> dict[str, str]:
    """``{function name: defining file}`` for every live function whose body inserts into ``nexus.chunks``."""
    return {
        name: file
        for (name, _), (file, body) in live_functions(changelog_dir).items()
        if _CHUNK_INSERT.search(body)
    }


def test_every_live_chunk_inserting_function_is_allowlisted_with_a_reason() -> None:
    new = sorted(set(live_chunk_inserters()) - set(_CHUNK_INSERTER_ALLOWLIST))
    assert not new, (
        f"live SQL functions that INSERT INTO nexus.chunks and are not in _CHUNK_INSERTER_ALLOWLIST: {new}. "
        "RDR-223: a chunk is written with its owner. A function that writes chunks outside the "
        "OwnershipGuard must say why it may: add it to tests/test_changelog_chunk_inserter_lint.py with a reason."
    )


def test_the_allowlist_has_no_stale_entry_and_every_reason_is_real() -> None:
    inserters = live_chunk_inserters()
    stale = sorted(set(_CHUNK_INSERTER_ALLOWLIST) - set(inserters))
    assert not stale, f"allowlisted functions that no longer insert into nexus.chunks (remove the entry): {stale}"
    for name, reason in _CHUNK_INSERTER_ALLOWLIST.items():
        assert len(reason.strip()) > 20, f"{name}: reason too short to be a justification"


def test_the_walk_is_not_vacuous() -> None:
    live = live_functions()
    assert len(live) >= 50, f"the walk found only {len(live)} live functions; it is broken"
    assert len(_included_files(CHANGELOG_DIR)) >= 100
    # the allowlisted four are found, each as ONE live definition (a second parameter list would be a second function)
    for name in _CHUNK_INSERTER_ALLOWLIST:
        assert sum(1 for (n, _) in live if n == name) == 1, name
    # a function that is only ever replaced is read at its last definition, not its first
    assert live_chunk_inserters()["gc_quarantine_orphans"] != "catalog-023-quarantine-functions.xml"


def _write_changelog(root: Path, files: dict[str, str], include_order: list[str]) -> Path:
    head = '<databaseChangeLog xmlns="http://www.liquibase.org/xml/ns/dbchangelog">\n'
    (root / _MASTER).write_text(
        head + "".join(f'<include file="db/changelog/{n}"/>\n' for n in include_order) + "</databaseChangeLog>\n"
    )
    for name, body in files.items():
        (root / name).write_text(head + body + "</databaseChangeLog>\n")
    return root


def _fn(name: str, body: str, args: str = "p_tenant uuid", rollback: str = "") -> str:
    rb = f"<rollback>{rollback}</rollback>" if rollback else ""
    return (
        f'<changeSet id="{name}" author="t"><sql splitStatements="false">'
        f"CREATE OR REPLACE FUNCTION nexus.{name}({args}) RETURNS void LANGUAGE plpgsql AS $fn$\n"
        f"BEGIN {body} END; $fn$;</sql>{rb}</changeSet>\n"
    )


_INSERT = "INSERT INTO nexus.chunks (tenant_id) SELECT p_tenant;"
_NO_INSERT = "UPDATE nexus.chunks SET metadata = metadata;"


@pytest.mark.parametrize("case, files, order, expected", [
    ("a new inserting function is found",
     {"a.xml": _fn("brand_new_reaper", _INSERT)}, ["a.xml"], {"brand_new_reaper": "a.xml"}),
    ("a non-inserting function is not",
     {"a.xml": _fn("harmless", _NO_INSERT)}, ["a.xml"], {}),
    ("the LAST definition decides: a later non-inserting body retires the insert",
     {"a.xml": _fn("f", _INSERT), "b.xml": _fn("f", _NO_INSERT)}, ["a.xml", "b.xml"], {}),
    ("the LAST definition decides: a later inserting body makes it live",
     {"a.xml": _fn("f", _NO_INSERT), "b.xml": _fn("f", _INSERT)}, ["a.xml", "b.xml"], {"f": "b.xml"}),
    ("include order, not file name, decides which is last",
     {"a.xml": _fn("f", _NO_INSERT), "b.xml": _fn("f", _INSERT)}, ["b.xml", "a.xml"], {}),
    ("a rollback block restores an older definition and is not the live one",
     {"a.xml": _fn("f", _NO_INSERT, rollback=(
         "CREATE OR REPLACE FUNCTION nexus.f(p_tenant uuid) RETURNS void LANGUAGE plpgsql AS $r$ "
         "BEGIN " + _INSERT + " END; $r$;"))},
     ["a.xml"], {}),
    ("a later DROP removes the function",
     {"a.xml": _fn("f", _INSERT),
      "b.xml": '<changeSet id="d" author="t"><sql>DROP FUNCTION IF EXISTS nexus.f(uuid);</sql></changeSet>\n'},
     ["a.xml", "b.xml"], {}),
    ("an overload with another parameter list is another function",
     {"a.xml": _fn("f", _INSERT, args="p_tenant uuid"), "b.xml": _fn("f", _NO_INSERT, args="p_tenant uuid, p_n int")},
     ["a.xml", "b.xml"], {"f": "a.xml"}),
    ("a lower-case, unqualified insert is still an insert into the table",
     {"a.xml": _fn("g", "insert into chunks (tenant_id) values (p_tenant);")}, ["a.xml"], {"g": "a.xml"}),
    ("a different table with the same prefix is not",
     {"a.xml": _fn("h", "INSERT INTO nexus.chunks_768 (tenant_id) VALUES (p_tenant);")}, ["a.xml"], {}),
])
def test_the_walk_reads_the_last_live_definition(
    tmp_path: Path, case: str, files: dict[str, str], order: list[str], expected: dict[str, str],
) -> None:
    assert live_chunk_inserters(_write_changelog(tmp_path, files, order)) == expected, case


def test_a_fake_changeset_with_a_new_inserting_function_fails_the_allowlist_check(tmp_path: Path) -> None:
    """The mutation: the real changelog plus one changeset defining a function that inserts into
    ``nexus.chunks``. The set difference the first test asserts on must name it."""
    real = {
        f.name: f.read_text() for f in _included_files(CHANGELOG_DIR)
    }
    order = [f.name for f in _included_files(CHANGELOG_DIR)]
    for name, text in real.items():
        (tmp_path / name).write_text(text)
    master = (CHANGELOG_DIR / _MASTER).read_text()
    (tmp_path / "zz-fake-001.xml").write_text(
        '<databaseChangeLog xmlns="http://www.liquibase.org/xml/ns/dbchangelog">\n'
        + _fn("fake_new_inserter", _INSERT) + "</databaseChangeLog>\n"
    )
    (tmp_path / _MASTER).write_text(
        master.replace("</databaseChangeLog>", '<include file="db/changelog/zz-fake-001.xml"/>\n</databaseChangeLog>')
    )
    assert order  # non-vacuity: the real files were copied
    new = set(live_chunk_inserters(tmp_path)) - set(_CHUNK_INSERTER_ALLOWLIST)
    assert new == {"fake_new_inserter"}


def test_a_body_that_cannot_be_delimited_fails_instead_of_being_skipped(tmp_path: Path) -> None:
    sql = (
        '<changeSet id="x" author="t"><sql>CREATE FUNCTION nexus.q(p int) RETURNS int AS \'select 1\' '
        "LANGUAGE sql;</sql></changeSet>\n"
    )
    with pytest.raises(AssertionError, match="cannot delimit"):
        live_chunk_inserters(_write_changelog(tmp_path, {"a.xml": sql}, ["a.xml"]))


@pytest.mark.parametrize("rel", [
    "service/src/main/java/dev/nexus/service/vectors/OwnershipGuard.java",
    "docs/rdr/rdr-223-atomic-chunk-plus-owner-write.md",
])
def test_the_known_exception_prose_points_at_the_allowlist(rel: str) -> None:
    """The prose that names the exceptions defers to this file, so it cannot go stale by itself."""
    text = (REPO_ROOT / rel).read_text()
    assert "tests/test_changelog_chunk_inserter_lint.py" in text, rel
    assert "_CHUNK_INSERTER_ALLOWLIST" in text, rel
