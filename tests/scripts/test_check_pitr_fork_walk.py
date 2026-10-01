# SPDX-License-Identifier: AGPL-3.0-or-later
"""scripts/check_pitr_fork_walk.py (nexus-k9fs1): the fork-walk assertions, on fixed evidence."""
from __future__ import annotations

import sys
import xml.etree.ElementTree as ET
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(REPO_ROOT / "scripts"))

import check_pitr_fork_walk as cw  # noqa: E402

SKILL = REPO_ROOT / ".claude" / "skills" / "engine-release" / "SKILL.md"

SESSION = (
    'event=schema_migration_session role={role} current_schema=public '
    'search_path="$user", public pg_db_role_setting=[]'
)


def _log(*, new: int, rex: int, mark: int = 0, pending: int | None = None,
         role: str = "nexus_admin", anomaly: bool = False, session: bool = True,
         failed: bool = False) -> str:
    pending = new + rex + mark if pending is None else pending
    out = ["event=schema_migration_start changelog=db/changelog/master.xml"]
    if session:
        out.append("2026-10-01T10:00:00Z INFO " + SESSION.format(role=role))
    out.append(f"event=schema_migration_pending changesets={pending}")
    if anomaly:
        out.append(f"event=schema_migration_count_anomaly accounted_for=1 pending_at_start={pending}")
    if failed:
        out.append("event=schema_migration_failed error=boom")
    out.append(
        f"event=schema_migration_complete new_changesets={new} "
        f"reexecuted_changesets={rex} pending_at_start={pending} mark_ran_changesets={mark}"
    )
    return "\n".join(out)


# --- walk -------------------------------------------------------------------

def test_a_clean_first_walk_passes() -> None:
    rc, lines = cw.check_walk(_log(new=5, rex=12), "nexus_admin", 5, False, 12)
    assert rc == 0, lines


def test_a_clean_second_walk_is_a_noop() -> None:
    rc, lines = cw.check_walk(_log(new=0, rex=12), "nexus_admin", None, True, 12)
    assert rc == 0, lines
    assert any("second walk is a no-op" in line for line in lines)


def test_a_second_walk_that_replans_the_world_fails() -> None:
    """The q81g7 failure: boot 2 reads an empty history and applies everything again."""
    rc, lines = cw.check_walk(_log(new=140, rex=12), "nexus_admin", None, True, 12)
    assert rc == 1
    assert any("a second walk must apply nothing" in line for line in lines)


def test_the_v0_1_118_shape_fails_the_count_identity() -> None:
    """new=5 pending=17 reexecuted=25 (the duplicate-row overcount, nexus-jl08t)."""
    rc, lines = cw.check_walk(_log(new=5, rex=25, pending=17), "nexus_admin", 5, False, 12)
    assert rc == 1
    assert any("!= pending_at_start 17" in line for line in lines)


def test_an_anomaly_event_fails_even_when_the_counts_add_up() -> None:
    rc, _ = cw.check_walk(_log(new=0, rex=12, anomaly=True), None, None, True, 12)
    assert rc == 1


def test_a_failed_event_fails() -> None:
    rc, _ = cw.check_walk(_log(new=0, rex=12, failed=True), None, None, True, 12)
    assert rc == 1


def test_a_noop_walk_with_the_wrong_reexecuted_count_fails() -> None:
    rc, lines = cw.check_walk(_log(new=0, rex=11), None, None, True, 12)
    assert rc == 1
    assert any("expected 12" in line for line in lines)
    rc, _ = cw.check_walk(_log(new=0, rex=11), None, None, True, 11)
    assert rc == 0


def test_a_mark_ran_in_a_noop_walk_fails() -> None:
    rc, _ = cw.check_walk(_log(new=0, rex=11, mark=1), None, None, True, 11)
    assert rc == 1


def test_an_engine_without_the_session_line_cannot_pass_vacuously() -> None:
    rc, lines = cw.check_walk(_log(new=0, rex=12, session=False), None, None, True, 12)
    assert rc == 1
    assert any("predates the q81g7" in line for line in lines)


def test_the_session_role_must_match_the_role_read_from_the_env() -> None:
    rc, lines = cw.check_walk(_log(new=0, rex=12, role="someone_else"), "nexus_admin", None, True, 12)
    assert rc == 1
    assert any("!= --migration-role" in line for line in lines)


@pytest.mark.parametrize("role", ["nexus", "t1", "staging"])
def test_a_schema_named_session_role_fails(role: str) -> None:
    rc, _ = cw.check_walk(_log(new=0, rex=12, role=role), None, None, True, 12)
    assert rc == 1


def test_a_log_holding_two_boots_is_unverifiable_not_read_as_the_last_one() -> None:
    """The last-boot reading hid boot 1: a walk1.log that also held boot 2 passed on boot 2's counts."""
    log = _log(new=140, rex=12) + "\n" + _log(new=0, rex=12)
    with pytest.raises(cw.Unverifiable, match="2 boots"):
        cw.check_walk(log, None, None, True, 12)


def test_a_log_holding_two_boots_exits_2_through_main(tmp_path: Path) -> None:
    log = tmp_path / "walk1.log"
    log.write_text(_log(new=0, rex=12) + "\n" + _log(new=0, rex=12))
    assert cw.main(["walk", "--engine-log", str(log), "--noop"]) == 2


def test_walk_with_neither_expect_new_nor_noop_is_unverifiable() -> None:
    """A no-op boot of the new image is self-consistent; without --expect-new it would pass walk 1."""
    with pytest.raises(cw.Unverifiable, match="none of --expect-recorded"):
        cw.check_walk(_log(new=0, rex=12), "nexus_admin", None, False, 12)


def test_expect_recorded_counts_a_mark_ran_changeset_that_expect_new_does_not() -> None:
    """This tag's staging-6-drop-landing-schema is MARK_RAN-guarded: on a database whose staging
    schema is already gone it is recorded (a row) but not executed. Three added changesets then log
    new=2 mark_ran=1."""
    log = _log(new=2, rex=12, mark=1)
    rc, lines = cw.check_walk(log, "nexus_admin", None, False, 12, expect_recorded=3)
    assert rc == 0, lines
    assert any("new + mark_ran == 3 (executed 2, marked ran 1)" in line for line in lines)
    rc, lines = cw.check_walk(log, "nexus_admin", 3, False, 12)
    assert rc == 1
    assert any("new_changesets = 2, expected 3" in line and "MARK_RAN" in line for line in lines)


def test_expect_recorded_fails_on_the_wrong_total_and_on_a_no_op_boot() -> None:
    rc, lines = cw.check_walk(_log(new=2, rex=12, mark=1), "nexus_admin", None, False, 12, expect_recorded=4)
    assert rc == 1
    assert any("= 3 (executed 2, marked ran 1), expected 4" in line for line in lines)
    rc, _ = cw.check_walk(_log(new=0, rex=12), "nexus_admin", None, False, 12, expect_recorded=3)
    assert rc == 1


def test_expect_recorded_zero_is_unverifiable_without_noop() -> None:
    with pytest.raises(cw.Unverifiable, match="--expect-recorded 0"):
        cw.check_walk(_log(new=0, rex=12), "nexus_admin", None, False, 12, expect_recorded=0)


def test_expect_recorded_through_the_cli(tmp_path: Path) -> None:
    log = tmp_path / "walk1.log"
    log.write_text(_log(new=2, rex=12, mark=1))
    base = ["walk", "--engine-log", str(log), "--migration-role", "nexus_admin"]
    assert cw.main([*base, "--expect-recorded", "3"]) == 0
    assert cw.main([*base, "--expect-recorded", "2"]) == 1
    assert cw.main([*base, "--expect-recorded", "0"]) == 2


def test_a_no_op_boot_fails_walk_1_of_a_schema_carrying_tag() -> None:
    rc, lines = cw.check_walk(_log(new=0, rex=12), "nexus_admin", 3, False, 12)
    assert rc == 1
    assert any("new_changesets = 0, expected 3" in line for line in lines)


def test_expect_new_zero_without_noop_is_unverifiable() -> None:
    with pytest.raises(cw.Unverifiable, match="expect-new 0"):
        cw.check_walk(_log(new=0, rex=12), "nexus_admin", 0, False, 12)


def test_the_cli_requires_one_of_expect_new_or_noop(tmp_path: Path) -> None:
    log = tmp_path / "walk1.log"
    log.write_text(_log(new=5, rex=12))
    assert cw.main(["walk", "--engine-log", str(log), "--migration-role", "nexus_admin"]) == 2
    assert cw.main(["walk", "--engine-log", str(log), "--migration-role", "nexus_admin", "--expect-new", "5"]) == 0
    assert cw.main(["walk", "--engine-log", str(log), "--migration-role", "nexus_admin", "--expect-new", "4"]) == 1
    assert cw.main(["walk", "--engine-log", str(log), "--migration-role", "nexus_admin", "--expect-new", "0"]) == 2


def test_a_padded_migration_role_compares_stripped(tmp_path: Path) -> None:
    """main validated the stripped role but passed the padded one on, so " nexus_admin " mismatched the log."""
    log = tmp_path / "walk.log"
    log.write_text(_log(new=0, rex=12, role="nexus_admin"))
    assert cw.main(["walk", "--engine-log", str(log), "--noop", "--migration-role", " nexus_admin "]) == 0
    rc, _ = cw.check_walk(_log(new=0, rex=12, role="nexus_admin"), " nexus_admin ", None, True, 12)
    assert rc == 0


@pytest.mark.parametrize("text", ["", "   \n", "just some other log line\n"])
def test_unreadable_evidence_is_unverifiable(text: str) -> None:
    with pytest.raises(cw.Unverifiable):
        cw.check_walk(text, None, None, True, 12)


def test_a_complete_line_missing_a_count_is_unverifiable() -> None:
    with pytest.raises(cw.Unverifiable):
        cw.check_walk("event=schema_migration_complete new_changesets=0", None, None, True, 12)


def test_a_counts_unavailable_line_is_unreadable_not_an_identity_mismatch() -> None:
    """The engine logs -1 for the three counts it could not compute (counts_unavailable=true).
    That is evidence that cannot be read (exit 2), not a count identity that failed (exit 1)."""
    text = (
        "event=schema_migration_start changelog=x\n"
        + "2026-10-01T10:00:00Z INFO " + SESSION.format(role="nexus_admin") + "\n"
        + "event=schema_migration_complete new_changesets=-1 reexecuted_changesets=-1 "
        "pending_at_start=17 mark_ran_changesets=-1 counts_unavailable=true"
    )
    with pytest.raises(cw.Unverifiable, match="counts_unavailable"):
        cw.check_walk(text, "nexus_admin", None, False, 12)


def test_a_counts_unavailable_walk_exits_2_through_main(tmp_path: Path) -> None:
    log = tmp_path / "engine.log"
    log.write_text(
        "event=schema_migration_complete new_changesets=-1 reexecuted_changesets=-1 "
        "pending_at_start=17 mark_ran_changesets=-1 counts_unavailable=true"
    )
    assert cw.main(["walk", "--engine-log", str(log), "--migration-role", "nexus_admin"]) == 2


def test_an_empty_migration_role_on_walk_is_exit_2_not_a_pass(tmp_path: Path) -> None:
    """The skill passes "$NX_DB_ADMIN_USER", which is "" when that variable is unset."""
    log = tmp_path / "engine.log"
    log.write_text(_log(new=0, rex=12))
    assert cw.main(["walk", "--engine-log", str(log), "--noop", "--migration-role", ""]) == 2
    assert cw.main(["walk", "--engine-log", str(log), "--noop", "--migration-role", "  "]) == 2


def test_an_omitted_walk_role_is_taken_from_the_log_and_says_so() -> None:
    rc, lines = cw.check_walk(_log(new=0, rex=12, role="nexus_admin"), None, None, True, 12)
    assert rc == 0, lines
    assert any("taken from the engine log" in line for line in lines)


# --- schema -----------------------------------------------------------------

class FakeDb:
    """Answers the checker's queries from a small in-memory description."""

    def __init__(self, *, history=("public",), lock=("public",), locked="f",
                 rows=152, schemas=("public", "nexus", "t1", "staging"), settings=()):
        self.history, self.lock, self.locked = history, lock, locked
        self.rows, self.schemas, self.settings = rows, schemas, settings

    def __call__(self, sql: str) -> list[list[str]]:
        if "relname = 'databasechangelog'" in sql:
            return [[s] for s in self.history]
        if "relname = 'databasechangeloglock'" in sql:
            return [[s] for s in self.lock]
        if "databasechangeloglock order by id" in sql:
            return [[self.locked]]
        if "count(*) from public.databasechangelog" in sql:
            return [[str(self.rows)]]
        if "pg_namespace where nspname" in sql:
            return [[s] for s in self.schemas]
        if "pg_db_role_setting" in sql:
            return [list(r) for r in self.settings]
        raise AssertionError(f"unexpected SQL: {sql}")


def test_a_clean_database_passes() -> None:
    rc, lines = cw.check_schema(FakeDb(), "nexus_admin", None)
    assert rc == 0, lines


def test_a_second_changelog_in_a_role_named_schema_fails() -> None:
    rc, lines = cw.check_schema(FakeDb(history=("nexus", "public")), "nexus_admin", None)
    assert rc == 1
    assert any("databasechangelog lives in" in line for line in lines)


def test_a_history_only_outside_public_fails() -> None:
    rc, _ = cw.check_schema(FakeDb(history=("nexus",)), "nexus_admin", None)
    assert rc == 1


def test_a_missing_lock_table_fails() -> None:
    rc, _ = cw.check_schema(FakeDb(lock=()), "nexus_admin", None)
    assert rc == 1


def test_a_held_lock_fails() -> None:
    rc, lines = cw.check_schema(FakeDb(locked="t"), "nexus_admin", None)
    assert rc == 1
    assert any("HELD" in line for line in lines)


@pytest.mark.parametrize("role", ["nexus", "t1", "staging"])
def test_a_forbidden_migration_role_fails(role: str) -> None:
    rc, _ = cw.check_schema(FakeDb(), role, None)
    assert rc == 1


def test_a_role_equal_to_any_schema_fails() -> None:
    rc, lines = cw.check_schema(FakeDb(schemas=("public", "billing")), "billing", None)
    assert rc == 1
    assert any("equals an existing schema" in line for line in lines)


@pytest.mark.parametrize("role", [None, "", "   "])
def test_without_a_role_the_schema_check_is_unverifiable_not_a_pass(role: str | None) -> None:
    """An empty role used to skip the role-name check and still end PASSED (vacuous)."""
    with pytest.raises(cw.Unverifiable, match="--migration-role"):
        cw.check_schema(FakeDb(), role, None)


def test_the_row_count_pin_catches_a_walk_that_added_rows() -> None:
    assert cw.check_schema(FakeDb(rows=152), "nexus_admin", 152)[0] == 0
    assert cw.check_schema(FakeDb(rows=290), "nexus_admin", 152)[0] == 1


def test_the_min_rows_floor_catches_a_walk_that_recorded_fewer_than_the_tree_carries() -> None:
    rc, lines = cw.check_schema(FakeDb(rows=490), "nexus_admin", None, min_rows=494)
    assert rc == 1
    assert any("fewer than the 494 changeSet(s)" in line for line in lines)
    assert cw.check_schema(FakeDb(rows=494), "nexus_admin", None, min_rows=494)[0] == 0
    # production holds rows beyond the tree's (superseded changesets, duplicate rows): a floor, not an equality
    assert cw.check_schema(FakeDb(rows=530), "nexus_admin", None, min_rows=494)[0] == 0


def test_min_rows_through_the_cli() -> None:
    assert cw.main(["schema", "--migration-role", "nexus_admin", "--min-rows", "100"], runner=FakeDb(rows=152)) == 0
    assert cw.main(["schema", "--migration-role", "nexus_admin", "--min-rows", "200"], runner=FakeDb(rows=152)) == 1


def test_role_and_database_settings_are_reported_and_a_search_path_is_noted() -> None:
    db = FakeDb(settings=[("nexus_admin", "(all databases)", "search_path=nexus")])
    rc, lines = cw.check_schema(db, "nexus_admin", None)
    assert rc == 0
    assert any("pg_db_role_setting role=nexus_admin" in line for line in lines)
    assert any(line.startswith("note") for line in lines)


def test_the_settings_baseline_is_saved_then_compared(tmp_path: Path) -> None:
    saved = tmp_path / "settings.json"
    before = FakeDb(settings=[("nexus_admin", "(all databases)", "search_path=public")])
    rc, lines = cw.check_schema(before, "nexus_admin", None, save_settings=saved)
    assert rc == 0 and saved.exists(), lines
    assert cw.check_schema(before, "nexus_admin", None, compare_settings=saved)[0] == 0


def test_a_settings_change_after_the_baseline_fails(tmp_path: Path) -> None:
    saved = tmp_path / "settings.json"
    cw.check_schema(FakeDb(settings=[("nexus_admin", "(all databases)", "search_path=public")]),
                    "nexus_admin", None, save_settings=saved)
    after = FakeDb(settings=[("nexus_admin", "(all databases)", "search_path=nexus")])
    rc, lines = cw.check_schema(after, "nexus_admin", None, compare_settings=saved)
    assert rc == 1
    assert any("pg_db_role_setting changed" in line for line in lines)
    # a row appearing where there was none
    cw.check_schema(FakeDb(), "nexus_admin", None, save_settings=saved)
    assert cw.check_schema(after, "nexus_admin", None, compare_settings=saved)[0] == 1


def test_a_missing_settings_baseline_is_unverifiable(tmp_path: Path) -> None:
    with pytest.raises(cw.Unverifiable, match="--compare-settings"):
        cw.check_schema(FakeDb(), "nexus_admin", None, compare_settings=tmp_path / "absent.json")


# --- the runAlways count ------------------------------------------------------

def _count_run_always_changesets() -> int:
    """<changeSet runAlways="true"> across the files the master changelog includes."""
    changelog = REPO_ROOT / "service" / "src" / "main" / "resources" / "db" / "changelog"
    master = ET.parse(changelog / "db.changelog-master.xml").getroot()
    included = [
        e.get("file", "") for e in master.iter() if e.tag.rsplit("}", 1)[-1] == "include"
    ]
    assert len(included) > 100, "the master changelog's includes were not read"
    total = 0
    for name in included:
        root = ET.parse(REPO_ROOT / "service" / "src" / "main" / "resources" / name).getroot()
        total += sum(
            1 for e in root.iter()
            if e.tag.rsplit("}", 1)[-1] == "changeSet" and e.get("runAlways") == "true"
        )
    return total


def test_default_reexecuted_is_the_changelogs_run_always_count() -> None:
    """DEFAULT_REEXECUTED is defined once, in the checker, and pinned here to the changelog by
    XML parse: a new runAlways changeset fails this test instead of failing a fork rehearsal."""
    assert cw.DEFAULT_REEXECUTED == _count_run_always_changesets()


def _all_changeset_start_tags() -> int:
    """Independent of the checker: every `<changeSet` start tag, by regex over the raw text of the
    files the master includes (a multi-line tag has its name on the first line, so it counts once)."""
    import re
    resources = REPO_ROOT / "service" / "src" / "main" / "resources"
    master = ET.parse(resources / "db" / "changelog" / "db.changelog-master.xml").getroot()
    names = [e.get("file", "") for e in master.iter() if e.tag.rsplit("}", 1)[-1] == "include"]
    total = 0
    for name in names:
        text = (resources / name).read_text()
        text = re.sub(r"<!--.*?-->", "", text, flags=re.S)
        total += len(re.findall(r"<changeSet\b", text))
    return total


def test_the_tree_changeset_count_matches_an_independent_regex_count() -> None:
    assert cw.tree_changeset_count() == _all_changeset_start_tags() > 400


def test_the_tree_changeset_count_reads_multiline_tags_and_ignores_comments(tmp_path: Path) -> None:
    changelog = tmp_path / "db" / "changelog"
    changelog.mkdir(parents=True)
    (changelog / "db.changelog-master.xml").write_text(
        '<databaseChangeLog xmlns="http://www.liquibase.org/xml/ns/dbchangelog">'
        '<include file="db/changelog/a.xml"/><include file="db/changelog/b.xml"/></databaseChangeLog>'
    )
    ns = 'xmlns="http://www.liquibase.org/xml/ns/dbchangelog"'
    (changelog / "a.xml").write_text(
        f'<databaseChangeLog {ns}>\n<!-- <changeSet id="commented" author="x"> -->\n'
        '<changeSet\n    id="one"\n    author="x"\n    runAlways="true">\n<comment>c</comment></changeSet>\n'
        '<changeSet id="two" author="x"></changeSet></databaseChangeLog>'
    )
    (changelog / "b.xml").write_text(f'<databaseChangeLog {ns}><changeSet id="three" author="y"/></databaseChangeLog>')
    assert cw.tree_changeset_count(tmp_path) == 3


def test_an_unreadable_changelog_is_unverifiable_not_zero(tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
    with pytest.raises(cw.Unverifiable):
        cw.tree_changeset_count(tmp_path)
    assert cw.main(["changelog-count", "--resources", str(tmp_path)]) == 2
    capsys.readouterr()


def test_changelog_count_prints_the_number(capsys: pytest.CaptureFixture[str]) -> None:
    assert cw.main(["changelog-count"]) == 0
    assert capsys.readouterr().out.strip() == str(cw.tree_changeset_count())


def test_nothing_else_hardcodes_the_run_always_count() -> None:
    """two-walk-check.sh and the skill defer to the checker's default."""
    script = (REPO_ROOT / "tests" / "e2e" / "two-walk-check.sh").read_text()
    assert f":-{cw.DEFAULT_REEXECUTED}}}" not in script
    assert "TWO_WALK_EXPECTED_REEXECUTED" in script
    skill = SKILL.read_text()
    # Any literal number next to "runAlways", in either order and whatever the phrasing: a stale
    # hardcode of an OLD count would otherwise survive a bump of DEFAULT_REEXECUTED.
    import re
    near = re.compile(r"\b\d+\b[^.\n]{0,40}runAlways|runAlways[^.\n]{0,40}\b\d+\b")
    assert not near.findall(skill), near.findall(skill)
    assert near.search("the 12 runAlways changesets"), "the pattern must see a hardcoded count"


# --- CLI --------------------------------------------------------------------

def test_main_exit_codes(tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
    log = tmp_path / "engine.log"
    log.write_text(_log(new=0, rex=12))
    assert cw.main(["walk", "--engine-log", str(log), "--noop", "--migration-role", "nexus_admin"]) == 0
    log.write_text(_log(new=3, rex=12))
    assert cw.main(["walk", "--engine-log", str(log), "--noop"]) == 1
    log.write_text("")
    assert cw.main(["walk", "--engine-log", str(log), "--noop"]) == 2
    assert cw.main(["walk", "--engine-log", str(tmp_path / "absent.log"), "--noop"]) == 2
    assert cw.main(["schema", "--migration-role", "nexus_admin"], runner=FakeDb()) == 0
    assert cw.main(["schema", "--migration-role", ""], runner=FakeDb()) == 2
    assert cw.main(["schema"], runner=FakeDb()) == 2
    assert cw.main(["schema", "--migration-role", "nexus_admin"], runner=FakeDb(history=("nexus",))) == 1
    capsys.readouterr()


def test_a_psql_that_cannot_run_is_unverifiable(capsys: pytest.CaptureFixture[str]) -> None:
    assert cw.main(["schema", "--psql", "/nonexistent/psql", "--migration-role", "nexus_admin"]) == 2
    capsys.readouterr()


def test_the_local_rehearsal_runs_the_same_assertions_on_two_boots() -> None:
    """tests/e2e/two-walk-check.sh must drive both subcommands, the second walk as --noop, and
    read the migration role from the engine's credentials file rather than typing it."""
    text = (REPO_ROOT / "tests" / "e2e" / "two-walk-check.sh").read_text()
    assert 'check_pitr_fork_walk.py"' in text or "check_pitr_fork_walk.py" in text
    assert 'schema --psql' in text and "walk --engine-log" in text
    assert "--noop" in text
    assert "NX_DB_ADMIN_USER" in text
    assert '--expect-rows "$ROWS_AFTER_1"' in text
    assert "--save-settings" in text and text.count("--compare-settings") == 2
    # the engine appends every boot to one log; the checker refuses two boots in a file
    assert "_boot_slice" in text and text.count("_boot_slice \"$SVC_LOG\"") == 2
    assert "--expect-recorded" in text and "--noop" in text
    # the independent source: the tree's own changeset count, asserted against the table after walk 1
    assert "changelog-count" in text and '--expect-rows "$TREE_CHANGESETS"' in text
    # each start must add exactly one schema_migration_start, or a slice could re-read an earlier boot
    assert text.count('_starts_grew_by_one "$STARTS_BEFORE_') == 2


def test_the_engine_release_skill_names_the_script() -> None:
    text = SKILL.read_text()
    assert "scripts/check_pitr_fork_walk.py schema" in text
    assert "scripts/check_pitr_fork_walk.py walk" in text
