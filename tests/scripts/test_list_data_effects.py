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

import json
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
        <comment>Backfills a column. DATA EFFECT: UPDATEs nexus.gadgets.note to 'legacy'
            for every row where status = 'archived'; reversible.</comment>
        <sql splitStatements="true">
UPDATE nexus.gadgets SET note = 'legacy' WHERE status = 'archived';
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
    """The table cell must carry the FULL DATA EFFECT sentence, continuation
    line included -- a naive ``line for line in comment.splitlines() if
    MARKER in line`` scan (the bug this pins) truncates at the first
    physical newline and would silently drop the "; reversible." clause
    that lives on this fixture's SECOND physical line, exactly the shape
    44/63 real backfilled lines use (nexus-f7dwp critic finding)."""
    rows = lde.find_added_data_effecting_changesets("v1", "v2", repo_root=fixture_repo)
    table = lde.render_markdown_table(rows)
    assert "a-2" in table and "MISSING" in table
    assert "b-1" in table
    assert (
        "DATA EFFECT: UPDATEs nexus.gadgets.note to 'legacy' for every row "
        "where status = 'archived'; reversible." in table
    )


def test_census_predicate_carries_the_exact_matched_statement(fixture_repo: Path):
    """The predicate must carry the statement's REAL literal values, not a
    blanked '' placeholder -- b-1's UPDATE has two non-empty literals
    ('legacy', 'archived'), the hygiene-004-1 shape (a real CASE/WHERE
    literal, not an already-blank one) that the old code's
    _STRING_LITERAL_RE.sub("''", ...) blanking-before-capture defect made
    invisible: an all-'' fixture literal can't distinguish "captured
    correctly" from "blanked and it happened to already be ''"."""
    rows = lde.find_added_data_effecting_changesets("v1", "v2", repo_root=fixture_repo)
    b_row = next(r for r in rows if r.changeset_id == "b-1")
    predicate = lde._census_predicate(b_row.finding)
    assert "UPDATE nexus.gadgets SET note = 'legacy' WHERE status = 'archived'" in predicate


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


# ---------------------------------------------------------------------------
# Relay attestation (nexus-iu43o): refuses when missing, passes when
# present and matching, not-applicable when the range has no data-effecting
# changesets at all.
# ---------------------------------------------------------------------------


class TestRelayAttestation:
    def test_verify_refuses_when_never_recorded(self, fixture_repo: Path, capsys):
        rc = lde.verify_relay_attestation("v1", "v2", repo_root=fixture_repo)
        assert rc == 1
        _, err = capsys.readouterr()
        assert "REFUSED" in err
        assert "a.xml:a-2" in err and "b.xml:b-1" in err

    def test_verify_passes_when_recorded_and_matching(self, fixture_repo: Path, capsys):
        path = lde.record_relay_attestation("v1", "v2", repo_root=fixture_repo)
        assert path == fixture_repo / "docs" / "data-effect-relay" / "v2.json"
        body = json.loads(path.read_text())
        assert body["engine_tag"] == "v2"
        assert body["from_tag"] == "v1"
        assert body["changeset_ids"] == ["a.xml:a-2", "b.xml:b-1"]
        assert body["recorded_at"].endswith("Z")

        rc = lde.verify_relay_attestation("v1", "v2", repo_root=fixture_repo)
        assert rc == 0
        out, _ = capsys.readouterr()
        assert "RELAY ATTESTATION OK" in out

    def test_verify_is_not_applicable_when_range_has_no_data_effects(
        self, fixture_repo: Path, capsys
    ):
        rc = lde.verify_relay_attestation("v1", "v1", repo_root=fixture_repo)
        assert rc == 0
        out, _ = capsys.readouterr()
        assert "NOT-APPLICABLE" in out
        # Never wrote an attestation file just because nothing was needed.
        assert not (fixture_repo / "docs" / "data-effect-relay" / "v1.json").exists()

    def test_verify_refuses_a_stale_attestation_missing_a_new_changeset(
        self, fixture_repo: Path, capsys
    ):
        """Recorded against an EARLIER state of the range (only a-2 known),
        then the range grows a new data-effecting changeset (b-1) before the
        battery runs -- the stale attestation must not silently pass."""
        stale_dir = fixture_repo / "docs" / "data-effect-relay"
        stale_dir.mkdir(parents=True)
        (stale_dir / "v2.json").write_text(
            json.dumps(
                {
                    "engine_tag": "v2",
                    "from_tag": "v1",
                    "changeset_ids": ["a.xml:a-2"],
                    "recorded_at": "2026-01-01T00:00:00Z",
                }
            )
        )
        rc = lde.verify_relay_attestation("v1", "v2", repo_root=fixture_repo)
        assert rc == 1
        _, err = capsys.readouterr()
        assert "not attested" in err
        assert "b.xml:b-1" in err

    def test_verify_refuses_an_attestation_for_a_different_range(
        self, fixture_repo: Path, capsys
    ):
        lde.record_relay_attestation("v1", "v2", repo_root=fixture_repo)
        # Re-tag the SAME file under a different from_ref to simulate a
        # differently-scoped range landing on the identical to_ref name.
        path = fixture_repo / "docs" / "data-effect-relay" / "v2.json"
        body = json.loads(path.read_text())
        body["from_tag"] = "some-other-tag"
        path.write_text(json.dumps(body))
        rc = lde.verify_relay_attestation("v1", "v2", repo_root=fixture_repo)
        assert rc == 1
        _, err = capsys.readouterr()
        assert "different range" in err

    def test_cli_record_then_verify_round_trips(self, fixture_repo: Path, capsys):
        rc = lde.main(
            ["v1", "v2", "--repo-root", str(fixture_repo), "--record-relay-attestation"]
        )
        assert rc == 0
        capsys.readouterr()
        rc = lde.main(
            ["v1", "v2", "--repo-root", str(fixture_repo), "--verify-relay-attestation"]
        )
        assert rc == 0


if __name__ == "__main__":
    raise SystemExit(pytest.main([__file__, "-v"]))
