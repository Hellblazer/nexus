# SPDX-License-Identifier: AGPL-3.0-or-later
"""scripts/list_data_effects.py — fixture-repo tests (nexus-f7dwp).

Builds a small, wholly synthetic git repository under ``tmp_path`` (never
touches this checkout) with two tagged commits, and exercises
``find_added_data_effecting_changesets`` / ``render_markdown_table`` / the
``check()`` CLI entry point against it. This is the "test it against a
small fixture" requirement from the bead: a real ``git show`` round trip
through two refs, not a mocked one.
"""
from __future__ import annotations

import subprocess
from pathlib import Path

import pytest

import list_data_effects as lde

pytestmark = pytest.mark.lint

_MASTER_TEMPLATE = """<?xml version="1.0" encoding="UTF-8"?>
<databaseChangeLog
    xmlns="http://www.liquibase.org/xml/ns/dbchangelog"
    xmlns:xsi="http://www.w3.org/2001/XMLSchema-instance"
    xsi:schemaLocation="http://www.liquibase.org/xml/ns/dbchangelog
        http://www.liquibase.org/xml/ns/dbchangelog/dbchangelog-4.4.xsd">
{includes}
</databaseChangeLog>
"""


def _changelog(changesets_xml: str) -> str:
    return (
        '<?xml version="1.0" encoding="UTF-8"?>\n'
        '<databaseChangeLog\n'
        '    xmlns="http://www.liquibase.org/xml/ns/dbchangelog"\n'
        '    xmlns:xsi="http://www.w3.org/2001/XMLSchema-instance"\n'
        '    xsi:schemaLocation="http://www.liquibase.org/xml/ns/dbchangelog '
        'http://www.liquibase.org/xml/ns/dbchangelog/dbchangelog-4.4.xsd">\n'
        f"{changesets_xml}\n"
        "</databaseChangeLog>\n"
    )


def _git(repo: Path, *args: str) -> None:
    subprocess.run(["git", *args], cwd=repo, check=True, capture_output=True, text=True)


@pytest.fixture
def fixture_repo(tmp_path: Path) -> Path:
    """A tiny two-commit, two-tag git repo with a real changelog tree shape:

    v1: a.xml has ONE additive changeset only.
    v2: a.xml GAINS a data-effecting changeset with NO DATA EFFECT line
        (the MISSING case); a brand-new file b.xml carries a data-effecting
        changeset that DOES have the line (the disclosed case); a second
        brand-new file c.xml carries only an additive changeset (must not
        appear in the report at all).
    """
    repo = tmp_path / "repo"
    changelog_dir = repo / "service" / "src" / "main" / "resources" / "db" / "changelog"
    changelog_dir.mkdir(parents=True)

    _git(repo, "init", "-q")
    _git(repo, "config", "user.email", "test@example.com")
    _git(repo, "config", "user.name", "Test")

    (changelog_dir / "a.xml").write_text(
        _changelog(
            """    <changeSet id="a-1" author="t">
        <comment>Creates a table, purely additive.</comment>
        <sql splitStatements="true">
CREATE TABLE nexus.widgets (id int);
        </sql>
    </changeSet>"""
        )
    )
    (changelog_dir / "db.changelog-master.xml").write_text(
        _MASTER_TEMPLATE.format(includes='    <include file="a.xml"/>')
    )
    (repo / ".gitignore").write_text("")
    _git(repo, "add", "-A")
    _git(repo, "commit", "-q", "-m", "v1")
    _git(repo, "tag", "v1")

    # v2: a.xml gains a naked DELETE (no DATA EFFECT line); new files b.xml
    # (disclosed DELETE) and c.xml (additive only, must not appear).
    (changelog_dir / "a.xml").write_text(
        _changelog(
            """    <changeSet id="a-1" author="t">
        <comment>Creates a table, purely additive.</comment>
        <sql splitStatements="true">
CREATE TABLE nexus.widgets (id int);
        </sql>
    </changeSet>
    <changeSet id="a-2" author="t">
        <comment>Deletes stale widgets. No disclosure line -- this is the MISSING case.</comment>
        <sql splitStatements="true">
DELETE FROM nexus.widgets WHERE stale = true;
        </sql>
    </changeSet>"""
        )
    )
    (changelog_dir / "b.xml").write_text(
        _changelog(
            """    <changeSet id="b-1" author="t">
        <comment>Backfills a column. DATA EFFECT: UPDATEs nexus.gadgets.note to ''
            for every NULL row; reversible.</comment>
        <sql splitStatements="true">
UPDATE nexus.gadgets SET note = '' WHERE note IS NULL;
        </sql>
    </changeSet>"""
        )
    )
    (changelog_dir / "c.xml").write_text(
        _changelog(
            """    <changeSet id="c-1" author="t">
        <comment>Purely additive, must not appear in the report.</comment>
        <sql splitStatements="true">
ALTER TABLE nexus.widgets ADD COLUMN extra text;
        </sql>
    </changeSet>"""
        )
    )
    (changelog_dir / "db.changelog-master.xml").write_text(
        _MASTER_TEMPLATE.format(
            includes=(
                '    <include file="a.xml"/>\n'
                '    <include file="b.xml"/>\n'
                '    <include file="c.xml"/>'
            )
        )
    )
    _git(repo, "add", "-A")
    _git(repo, "commit", "-q", "-m", "v2")
    _git(repo, "tag", "v2")

    return repo


def test_finds_only_added_data_effecting_changesets(fixture_repo: Path):
    rows = lde.find_added_data_effecting_changesets("v1", "v2", repo_root=fixture_repo)
    ids = {(r.file, r.changeset_id) for r in rows}
    assert ids == {("a.xml", "a-2"), ("b.xml", "b-1")}, ids
    # a-1 (unchanged, additive) and c-1 (new file, but additive) must never
    # appear -- the report is scoped to added AND data-effecting.


def test_missing_disclosure_is_flagged_in_the_table(fixture_repo: Path):
    rows = lde.find_added_data_effecting_changesets("v1", "v2", repo_root=fixture_repo)
    table = lde.render_markdown_table(rows)
    assert "a-2" in table and "MISSING" in table
    assert "b-1" in table and "DATA EFFECT: UPDATEs nexus.gadgets.note" in table


def test_census_predicate_carries_the_exact_matched_statement(fixture_repo: Path):
    rows = lde.find_added_data_effecting_changesets("v1", "v2", repo_root=fixture_repo)
    b_row = next(r for r in rows if r.changeset_id == "b-1")
    predicate = lde._census_predicate(b_row.finding)
    assert "UPDATE nexus.gadgets SET note = '' WHERE note IS NULL" in predicate


def test_check_exits_nonzero_when_a_missing_row_is_present(fixture_repo: Path, capsys):
    rc = lde.check("v1", "v2", repo_root=fixture_repo)
    assert rc == 1
    out, err = capsys.readouterr()
    assert "a-2" in out
    assert "MISSING a DATA EFFECT" in err


def test_check_exits_zero_when_nothing_added(fixture_repo: Path):
    rc = lde.check("v1", "v1", repo_root=fixture_repo)
    assert rc == 0


def test_check_exits_two_on_unresolvable_ref(fixture_repo: Path, capsys):
    rc = lde.check("nonexistent-ref-xyz", "v2", repo_root=fixture_repo)
    assert rc == 2
    _, err = capsys.readouterr()
    assert "ERROR" in err


def test_main_cli_entry_point(fixture_repo: Path, capsys):
    rc = lde.main(["v1", "v2", "--repo-root", str(fixture_repo)])
    assert rc == 1
    out, _ = capsys.readouterr()
    assert "a-2" in out and "b-1" in out


if __name__ == "__main__":
    raise SystemExit(pytest.main([__file__, "-v"]))
