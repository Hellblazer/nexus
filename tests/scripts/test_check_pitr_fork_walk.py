# SPDX-License-Identifier: AGPL-3.0-or-later
"""scripts/check_pitr_fork_walk.py (nexus-k9fs1): the fork-walk assertions, on fixed evidence."""
from __future__ import annotations

import sys
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
    rc, lines = cw.check_walk(_log(new=5, rex=25, pending=17), "nexus_admin", None, False, 12)
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


def test_only_the_last_walk_in_the_log_counts() -> None:
    log = _log(new=140, rex=12) + "\n" + _log(new=0, rex=12)
    rc, _ = cw.check_walk(log, None, None, True, 12)
    assert rc == 0


@pytest.mark.parametrize("text", ["", "   \n", "just some other log line\n"])
def test_unreadable_evidence_is_unverifiable(text: str) -> None:
    with pytest.raises(cw.Unverifiable):
        cw.check_walk(text, None, None, True, 12)


def test_a_complete_line_missing_a_count_is_unverifiable() -> None:
    with pytest.raises(cw.Unverifiable):
        cw.check_walk("event=schema_migration_complete new_changesets=0", None, None, True, 12)


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


def test_without_a_role_the_check_says_it_did_not_run() -> None:
    rc, lines = cw.check_schema(FakeDb(), None, None)
    assert rc == 0
    assert any("role-name check was NOT run" in line for line in lines)


def test_the_row_count_pin_catches_a_walk_that_added_rows() -> None:
    assert cw.check_schema(FakeDb(rows=152), "nexus_admin", 152)[0] == 0
    assert cw.check_schema(FakeDb(rows=290), "nexus_admin", 152)[0] == 1


def test_role_and_database_settings_are_reported_and_a_search_path_is_noted() -> None:
    db = FakeDb(settings=[("nexus_admin", "(all databases)", "search_path=nexus")])
    rc, lines = cw.check_schema(db, "nexus_admin", None)
    assert rc == 0
    assert any("pg_db_role_setting role=nexus_admin" in line for line in lines)
    assert any(line.startswith("note") for line in lines)


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
    assert cw.main(["schema", "--migration-role", "nexus_admin"], runner=FakeDb(history=("nexus",))) == 1
    capsys.readouterr()


def test_a_psql_that_cannot_run_is_unverifiable(capsys: pytest.CaptureFixture[str]) -> None:
    assert cw.main(["schema", "--psql", "/nonexistent/psql"]) == 2
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


def test_the_engine_release_skill_names_the_script() -> None:
    text = SKILL.read_text()
    assert "scripts/check_pitr_fork_walk.py schema" in text
    assert "scripts/check_pitr_fork_walk.py walk" in text
