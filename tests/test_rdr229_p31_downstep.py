# SPDX-License-Identifier: AGPL-3.0-or-later
"""RDR-229 P3.1 (nexus-p2m1x.17, residual R5, nexus-p2m1x.44): the operator down-step and the conexus rollback runbook.

A pre-P3.1 engine cannot write a row the P3.1 CHECKs guard (a NOT VALID CHECK applies to every new row version): its chunk
INSERT, its supersede UPDATE and its carrier INSERTs fail with SQLSTATE 23514. So the rollback after P3.1 is the down-step
(DROP CONSTRAINT IF EXISTS on every listed constraint) FIRST and the tag flip second, never the tag flip alone.

The down-step is generated from the one list, ``service/src/main/resources/db/rdr229/p31-checks.tsv``, by
``scripts/rdr229_p31_downstep.py``; the committed SQL file and the runbook's embedded copies must equal a fresh run. The
engine test (``Rdr229P31DownStepIntegrationTest``) runs the SQL against a real PostgreSQL and asserts no listed constraint
remains, on the partitioned ``chunks`` parent and its leaves included.
"""

from __future__ import annotations

import importlib.util
import re
import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
SCRIPT = ROOT / "scripts" / "rdr229_p31_downstep.py"
LIST = ROOT / "service" / "src" / "main" / "resources" / "db" / "rdr229" / "p31-checks.tsv"
SQL = ROOT / "service" / "src" / "main" / "resources" / "db" / "rdr229" / "p31-downstep.sql"
RUNBOOK = ROOT / "docs" / "runbooks" / "rdr-229-p31-rollback.md"


def _load():
    spec = importlib.util.spec_from_file_location("rdr229_p31_downstep", SCRIPT)
    assert spec and spec.loader, f"{SCRIPT} is missing"
    module = importlib.util.module_from_spec(spec)
    sys.modules["rdr229_p31_downstep"] = module
    spec.loader.exec_module(module)
    return module


def _constraints() -> list[str]:
    """The listed constraint names, read here directly so the test does not use the generator to check the generator."""
    out = []
    for line in LIST.read_text(encoding="utf-8").splitlines():
        if not line.strip() or line.startswith("#") or line.startswith("table\t"):
            continue
        f = line.split("\t")
        if f[3] != "-":
            out.append(f[3])
    return out


def test_the_list_has_the_constraints_the_down_step_must_drop():
    names = _constraints()
    assert len(names) == 20, "non-vacuity: 20 CHECKs on the list (chunks and taxonomy_centroids carry two each)"
    assert len(set(names)) == 20


def test_the_generated_down_step_drops_every_listed_constraint_once_and_only_those():
    mod = _load()
    sql = mod.generate_down_step(LIST.read_text(encoding="utf-8"))
    drops = re.findall(r"^ALTER TABLE IF EXISTS nexus\.(\w+) DROP CONSTRAINT IF EXISTS (\w+);$", sql, re.M)
    assert [name for _, name in drops] == _constraints()
    assert len(re.findall(r"DROP CONSTRAINT", sql)) == 20
    # The statements the operator relies on: a bounded lock wait and a safe re-run (IF EXISTS on table and constraint).
    assert "SET LOCAL lock_timeout = '5s';" in sql
    for forbidden in ("DROP COLUMN", "DROP TABLE", "DELETE", "UPDATE ", "TRUNCATE", "data_moves"):
        assert forbidden not in sql, f"the down-step must leave the id columns, the identity column and data_moves alone: {forbidden}"


def test_the_down_step_follows_the_list_not_a_copy_of_it():
    """Mutation: add a row to the list and the generated SQL gains its constraint; remove one and it loses it."""
    mod = _load()
    text = LIST.read_text(encoding="utf-8")
    plus = text + "new_table\tcollection\tcollection_id\tnew_table_collection_id_check\tNAME_ID\n"
    assert "DROP CONSTRAINT IF EXISTS new_table_collection_id_check;" in mod.generate_down_step(plus)
    minus = "\n".join(line for line in text.splitlines() if "topics_collection_id_check" not in line) + "\n"
    assert "topics_collection_id_check" not in mod.generate_down_step(minus)
    # A history row (constraint '-') never produces a statement.
    assert "DROP CONSTRAINT IF EXISTS -;" not in mod.generate_down_step(text)


def test_an_empty_or_malformed_list_is_refused_not_turned_into_an_empty_down_step():
    mod = _load()
    for bad in ("", "# only a comment\n", "table\tname_column\tid_column\tconstraint\tkind\n", "a\tb\tc\n"):
        try:
            mod.generate_down_step(bad)
        except ValueError:
            continue
        raise AssertionError(f"accepted a list it cannot generate from: {bad!r}")


def test_the_committed_sql_equals_a_fresh_run():
    mod = _load()
    assert SQL.exists(), f"{SQL} is missing: run scripts/rdr229_p31_downstep.py --write"
    assert SQL.read_text(encoding="utf-8") == mod.generate_down_step(LIST.read_text(encoding="utf-8"))


def test_the_check_command_passes_on_the_tree_and_fails_on_a_doctored_sql_file(tmp_path: Path):
    ok = subprocess.run([sys.executable, str(SCRIPT), "--root", str(ROOT), "--check"], capture_output=True, text=True)
    assert ok.returncode == 0, ok.stdout + ok.stderr
    # A tree whose committed SQL has lost a statement and whose runbook still embeds the full text.
    for rel in (LIST, SQL, RUNBOOK):
        target = tmp_path / rel.relative_to(ROOT)
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text(rel.read_text(encoding="utf-8"), encoding="utf-8")
    doctored = tmp_path / SQL.relative_to(ROOT)
    doctored.write_text(
        doctored.read_text(encoding="utf-8").replace("DROP CONSTRAINT IF EXISTS chunks_collection_id_not_null;", ""),
        encoding="utf-8",
    )
    bad = subprocess.run([sys.executable, str(SCRIPT), "--root", str(tmp_path), "--check"], capture_output=True, text=True)
    assert bad.returncode == 1, bad.stdout + bad.stderr
    assert "p31-downstep.sql" in bad.stdout + bad.stderr


def test_the_runbook_embeds_the_generated_sql_and_the_check_catches_drift(tmp_path: Path):
    mod = _load()
    text = RUNBOOK.read_text(encoding="utf-8")
    assert mod.runbook_problems(text, LIST.read_text(encoding="utf-8")) == []
    drifted = text.replace("chunks_collection_id_not_null", "chunks_collection_id_not_nul", 1)
    problems = mod.runbook_problems(drifted, LIST.read_text(encoding="utf-8"))
    assert problems, "a runbook whose SQL no longer matches the list must be reported"
    # A runbook without the markers is a problem, not a pass.
    assert mod.runbook_problems("no markers here", LIST.read_text(encoding="utf-8"))


def test_the_runbook_says_down_step_first_then_the_tag_flip_and_the_symptom_without_it():
    text = RUNBOOK.read_text(encoding="utf-8")
    # The order, and that the tag flip alone is wrong.
    down = text.index("Run the down-step")
    flip = text.index("Flip the tag")
    assert down < flip, "the down-step comes before the tag flip"
    assert "never the tag flip alone" in text.lower() or "never the tag flip by itself" in text.lower()
    # The symptom an operator sees if they skip it: SQLSTATE 23514 naming a constraint of the list.
    assert "23514" in text
    assert "new row for relation" in text and "violates check constraint" in text
    assert "p31-checks.tsv" in text
    for example in ("chunks_collection_id_check", "catalog_collections_superseded_by_id_check", "topics_collection_id_check"):
        assert example in text, f"the symptom names a constraint such as {example}"
    # The window (Sam, 2026-11-20): only before a stamp completes, and in any case before P3.7's name-key drop move.
    assert "completed_at" in text and "rdr229-stamp-" in text
    assert "P3.7" in text and "PITR" in text
    # Who runs it and what it does not do.
    assert "conexus" in text.lower()
    assert "id columns" in text and "data_moves" in text
    # Local installs have no rollback (rdr-225-cloud-deploy.md section 8).
    assert "local" in text.lower() and "no rollback" in text.lower()


def test_the_runbook_is_pointed_at_from_the_engine_deploy_revert_paragraph():
    text = (ROOT / "docs" / "contributing.md").read_text(encoding="utf-8")
    assert "docs/runbooks/rdr-229-p31-rollback.md" in text


def test_the_runbook_pauses_the_open_stamp_moves_before_the_down_step_and_says_why():
    text = RUNBOOK.read_text(encoding="utf-8")
    pause = text.index("Pause the open stamp moves")
    down = text.index("Run the down-step")
    flip = text.index("Flip the tag")
    assert pause < down < flip, "pause, then the down-step, then the tag flip"
    # why: the previous engine's move worker runs moves whose on_complete fails once the CHECKs are gone
    assert "move worker" in text and "on_complete" in text and "VALIDATE CONSTRAINT" in text
    # on the cloud the pause is a data write under the same go as the down-step
    flat = " ".join(text.split())
    assert "data write" in flat and "same go as the down-step" in flat
    # the pause and its release are the generated, pinned statements, and the release touches only what the pause held
    mod = _load()
    assert mod.runbook_problems(text, LIST.read_text(encoding="utf-8")) == []
    assert "paused_by = 'rdr229 down-step'" in mod.generate_pause() + mod.generate_unpause()
    # one pattern, not a list: the previous engine's worker runs every open data_moves row, so the pause and its release match every rdr229 move
    old_form = "(label LIKE 'rdr229-stamp-%' OR label LIKE 'rdr229-fkvalidate-%' OR label LIKE 'rdr229-swap-%')"
    for statement in (mod.generate_pause(), mod.generate_unpause()):
        assert "label LIKE 'rdr229-%'" in statement
        assert old_form not in statement
        assert "rdr229-stamp-%" not in statement and "rdr229-fkvalidate-%" not in statement and "rdr229-swap-%" not in statement
    # the window query stays stamp-only (plus the validation moves it names)
    window = text[text.index("Check the window first"):text.index("Zero rows means")]
    assert "label LIKE 'rdr229-stamp-%'" in window
    assert "label LIKE 'rdr229-fkvalidate-%'" in window
    drifted = text.replace("paused_at IS NULL;", "paused_at IS NOT NULL;", 1)
    assert mod.runbook_problems(drifted, LIST.read_text(encoding="utf-8")), "a drifted pause statement is reported"


# ── the re-add (fix round C, code review F3) ─────────────────────────────────────────────────────────────────────────

READD_SQL = ROOT / "docs" / "runbooks" / "rdr-229-p31-readd.sql"
READD_DB_SQL = ROOT / "service" / "src" / "main" / "resources" / "db" / "rdr229" / "p31-readd.sql"
CHANGELOG_068 = ROOT / "service" / "src" / "main" / "resources" / "db" / "changelog" / "vectors-068-collection-id-expand.xml"


def _changeset_checks() -> dict[str, str]:
    """constraint name -> the expression vectors-068 adds it with, read from the changeset text itself."""
    text = CHANGELOG_068.read_text(encoding="utf-8")
    found = re.findall(r"ADD CONSTRAINT (\w+)\s+CHECK \((.+?)\) NOT VALID;", text, re.S)
    return {name: " ".join(expr.split()) for name, expr in found}


def _readd_statements(sql: str) -> dict[str, str]:
    """constraint name -> the expression one statement of the re-add adds it with."""
    found = re.findall(r"ADD CONSTRAINT (\w+) CHECK \((.+?)\) NOT VALID", sql, re.S)
    return {name: " ".join(expr.split()) for name, expr in found}


def test_the_readd_recreates_every_listed_constraint_with_the_expression_the_changeset_added_it_with():
    """The down-step dropped the CHECKs but DATABASECHANGELOG still records vectors-068-3..19 as run, so a redeploy does not
    add them again: the re-add does, and it must say what the changesets said, so it is compared with the changeset text and
    not with the generator's own table of expressions."""
    mod = _load()
    sql = mod.generate_readd(LIST.read_text(encoding="utf-8"))
    readd = _readd_statements(sql)
    assert list(readd) == _constraints(), "one statement per listed constraint, in list order"
    changeset = _changeset_checks()
    assert len(changeset) >= 20, "non-vacuity: the changeset text yields every CHECK it adds"
    for name in _constraints():
        assert readd[name] == changeset[name], f"{name}: the re-add differs from vectors-068's expression"


def test_the_readd_is_not_valid_guarded_and_touches_no_row():
    mod = _load()
    sql = mod.generate_readd(LIST.read_text(encoding="utf-8"))
    assert "SET LOCAL lock_timeout = '5s';" in sql
    for forbidden in ("VALIDATE", "UPDATE ", "DELETE", "TRUNCATE", "DROP ", "data_moves"):
        assert forbidden not in sql, f"the re-add only adds NOT VALID CHECKs: {forbidden}"
    # Safe to run twice and on an install that lacks a table or a column: each statement is guarded.
    assert sql.count("NOT VALID") >= 20
    assert sql.count("to_regclass(") >= 20 and sql.count("pg_catalog.pg_constraint") >= 20
    assert sql.count("pg_catalog.pg_attribute") >= 20, "the id column must exist before a CHECK names it"


def _068_19_precondition() -> str:
    """The data condition vectors-068-19's precondition holds the CHECK back on, read from the changeset text (entities decoded)."""
    text = CHANGELOG_068.read_text(encoding="utf-8")
    m = re.search(r'<changeSet id="vectors-068-19".*?<sqlCheck expectedResult="0">(.*?)</sqlCheck>', text, re.S)
    assert m, "vectors-068-19's precondition is not found"
    return " ".join(m.group(1).replace("&lt;", "<").replace("&gt;", ">").split())


def test_the_readd_holds_back_the_superseded_by_check_exactly_where_068_19_does_and_says_so():
    """Confirmation review N4. vectors-068-19 does not add catalog_collections_superseded_by_id_check while a tombstone with a blank
    model_version has no superseded_by_id (D2's repair move UPDATEs exactly those rows and a NOT VALID CHECK would refuse it with
    23514). The re-add must not bypass that: it SKIPS the one CHECK on the same data condition (taken from the changeset text) and
    says so with a NOTICE; every other CHECK is added as before. The operator runs the re-add again once D2's move has finished."""
    mod = _load()
    sql = mod.generate_readd(LIST.read_text(encoding="utf-8"))
    condition = _068_19_precondition().split("SELECT count(*) FROM ", 1)[1]
    block = sql[sql.index("catalog_collections_superseded_by_id_check") - 600:]
    block = block[: block.index("$q$;", block.index("ADD CONSTRAINT")) + 4]
    flat = " ".join(block.split())
    where = condition.split(" WHERE ", 1)[1]
    assert where in flat, "the re-add's guard is the precondition of vectors-068-19, word for word"
    assert "RAISE NOTICE" in flat and "skipped" in flat and "repair-catalog-collection-model-version" in flat
    # the other nineteen are not held back by it
    others = [b for b in sql.split("DO $q$") if "ADD CONSTRAINT" in b and "catalog_collections_superseded_by_id_check" not in b]
    assert len(others) == 19 and not any("RAISE NOTICE" in b for b in others), "only the superseded_by CHECK has the precondition"


def test_the_runbook_says_the_readd_skips_the_superseded_by_check_while_d2s_repair_is_open():
    flat = " ".join(RUNBOOK.read_text(encoding="utf-8").split())
    step = flat[flat.index("Re-add the CHECKs") :]
    assert "catalog_collections_superseded_by_id_check" in step and "model_version" in step
    assert "skipped" in step and "NOTICE" in step
    assert "run the re-add again" in step


def test_the_readd_follows_the_list_not_a_copy_of_it():
    mod = _load()
    text = LIST.read_text(encoding="utf-8")
    plus = text + "new_table\tcollection\tcollection_id\tnew_table_collection_id_check\tNAME_ID\n"
    assert "new_table_collection_id_check" in mod.generate_readd(plus)
    minus = "\n".join(line for line in text.splitlines() if "topics_collection_id_check" not in line) + "\n"
    assert "topics_collection_id_check" not in mod.generate_readd(minus)
    assert "ADD CONSTRAINT - " not in mod.generate_readd(text), "a history row never produces a statement"


def test_the_committed_readd_equals_a_fresh_run_and_the_check_command_covers_it(tmp_path: Path):
    mod = _load()
    assert READD_SQL.exists(), f"{READD_SQL} is missing: run scripts/rdr229_p31_downstep.py --write"
    assert READD_SQL.read_text(encoding="utf-8") == mod.generate_readd(LIST.read_text(encoding="utf-8"))
    assert READD_DB_SQL.read_text(encoding="utf-8") == READD_SQL.read_text(encoding="utf-8"), "the classpath copy the engine test runs"
    for rel in (LIST, SQL, RUNBOOK, READD_SQL, READD_DB_SQL):
        target = tmp_path / rel.relative_to(ROOT)
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text(rel.read_text(encoding="utf-8"), encoding="utf-8")
    doctored = tmp_path / READD_SQL.relative_to(ROOT)
    doctored.write_text(doctored.read_text(encoding="utf-8").replace("chunks_collection_id_not_null", "chunks_collection_id_not_nul", 1), encoding="utf-8")
    bad = subprocess.run([sys.executable, str(SCRIPT), "--root", str(tmp_path), "--check"], capture_output=True, text=True)
    assert bad.returncode == 1, bad.stdout + bad.stderr
    assert "p31-readd.sql" in bad.stdout + bad.stderr
    doctored.write_text(READD_SQL.read_text(encoding="utf-8"), encoding="utf-8")
    copy = tmp_path / READD_DB_SQL.relative_to(ROOT)
    copy.write_text(copy.read_text(encoding="utf-8") + "-- drift\n", encoding="utf-8")
    bad = subprocess.run([sys.executable, str(SCRIPT), "--root", str(tmp_path), "--check"], capture_output=True, text=True)
    assert bad.returncode == 1 and "db/rdr229/p31-readd.sql differs" in bad.stdout, bad.stdout + bad.stderr


def test_the_runbook_embeds_the_readd_and_the_check_catches_drift():
    mod = _load()
    text = RUNBOOK.read_text(encoding="utf-8")
    assert mod.runbook_problems(text, LIST.read_text(encoding="utf-8")) == []
    drifted = text.replace("chunks_collection_id_not_null", "chunks_collection_id_not_nul")
    assert mod.runbook_problems(drifted, LIST.read_text(encoding="utf-8")), "a runbook whose re-add SQL drifted must be reported"
    no_block = text.replace("<!-- p31-readd:begin -->", "").replace("<!-- p31-readd:end -->", "")
    assert any("re-add" in p for p in mod.runbook_problems(no_block, LIST.read_text(encoding="utf-8")))


def test_the_runbook_re_adds_the_checks_after_the_redeploy_and_before_it_releases_the_pause():
    text = RUNBOOK.read_text(encoding="utf-8")
    flip = text.index("Flip the tag")
    readd = text.index("Re-add the CHECKs")
    unpause = text.index("release the pause")
    assert flip < readd < unpause, "tag flip, then the re-add, then the release of the pause"
    flat = " ".join(text.split())
    # why: the changelog records the CHECKs as run, so a redeploy does not add them, and a stamp's on_complete would fail
    assert "DATABASECHANGELOG" in text and "42704" in text
    assert "p31-readd.sql" in text


def test_the_runbook_states_the_real_size_of_the_window():
    flat = " ".join(RUNBOOK.read_text(encoding="utf-8").split())
    # Sam, 2026-11-21 (ask p31-r5-window-registry-stamp): the registry stamp completes about a minute after the first boot
    # and does not close the window; the window query leaves it and its reruns out
    assert "registry stamp" in flat and "about a minute" in flat and "do not count" in flat
    assert "label NOT LIKE 'rdr229-stamp-catalog\\_collections%'" in flat
    assert "until the first data-table stamp completes" in flat
    # on the cloud the window lasts as long as the moves stay blocked, which is a stated condition
    assert "until pg_background is installed on the cloud's PostgreSQL (nexus-f3yqd.2.23)" in flat


def test_the_runbook_names_what_the_down_step_leaves_and_the_hazard_that_follows():
    flat = " ".join(RUNBOOK.read_text(encoding="utf-8").split())
    assert "24 function bodies" in flat and "vectors-069" in flat
    assert "canary_query_ring_upsert" in flat and "23503" in flat
    assert "drops the whole batch" in flat, "the previous engine's ring writer loses a batch on one bad entry"


def test_the_orphan_section_gives_the_restamp_step_and_no_placeholder_is_left():
    """Registering a missing collection stamps nothing (the stamp move is complete and does not run again), so the way back is
    ``nexus.rdr229_restamp``, which registers the move again once no orphan row remains."""
    text = RUNBOOK.read_text(encoding="utf-8")
    assert "## Orphan rows" in text
    assert "PLACEHOLDER" not in text
    flat = " ".join(text[text.index("## Orphan rows"):].split())
    assert "SELECT nexus.rdr229_restamp('<table>');" in flat
    assert "nexus_admin" in flat
    assert "-rerun-N" in flat and "supersedes" in flat
    assert "registered:" in flat and "not registered:" in flat
    assert "register each missing collection" in flat and "delete the rows" in flat


def test_the_restore_runbook_says_what_an_orphan_row_does_to_a_restore_and_how_to_resolve_it():
    text = (ROOT / "docs" / "operations" / "backup-restore.md").read_text(encoding="utf-8")
    flat = " ".join(text.split())
    assert "restore_orphan_collection" in flat
    assert "restore_orphan_collection_id" in flat and "restore_ambiguous_collection_name" in flat
    assert "fills a row's id by name only when the backup carries no id for it" in flat
    assert "fills every collection id from the restored registry row of that name" not in flat
    assert "no registry row in the backup" in flat
    assert "nothing is written" in flat.lower() or "nothing was written" in flat.lower()


def _ledger_entries(ledger) -> list:
    """Every ledger entry, Unshipped and Shipped: the release moves an entry from one section to the other, and what the entry
    says (its direction-safety token, the old-client failure) is the same in both."""
    return [*ledger.unshipped.values(), *ledger.shipped.values()]


def test_the_client_lag_ledger_names_the_older_client_failure_against_the_p31_engine():
    """A client from before P3.1 registers a document before its collection, and the P3.1 engine answers 422 for a document under
    an unregistered collection: its first put into a new collection fails. The engine cut reads this ledger
    (scripts/check_engine_release_floor.py check_client_lag_ledger), so the entry must lead with the direction-safety token and
    say both directions."""
    if str(ROOT / "scripts") not in sys.path:
        sys.path.insert(0, str(ROOT / "scripts"))
    wire = importlib.import_module("check_wire_contract_pairing")
    ledger = wire.parse_ledger(ROOT / "docs" / "wire-contract-pending.md")
    entries = [e for e in _ledger_entries(ledger) if e.bead == "nexus-p2m1x.17"]
    assert entries, "no ledger entry (Unshipped or Shipped) for the P3.1 engine half: an older client's first put into a new collection is not named"
    note = " ".join(" ".join(e.note for e in entries).split())
    assert any(e.additive is False for e in entries), "old client + new engine is NOT safe: the entry must lead with [not-additive]"
    low = note.lower()
    assert "old client + new engine" in low and "new client + old engine" in low
    assert "unregistered_collection" in note and "422" in note
    assert "first put into a new collection" in low
    assert "register_collection_for_write" in note or "registers the collection first" in low, "names the client half"
    assert "ships" in low, "the entry says when it moves to Shipped: when the client release carrying the half ships"


def test_the_client_lag_ledger_has_an_additive_entry_for_the_completion_status_field():
    """``/v1/status`` gains a top-level ``rdr229_completion`` object: an old client ignores it, and a new client against an old
    engine sees it absent and reports "not known"."""
    if str(ROOT / "scripts") not in sys.path:
        sys.path.insert(0, str(ROOT / "scripts"))
    wire = importlib.import_module("check_wire_contract_pairing")
    ledger = wire.parse_ledger(ROOT / "docs" / "wire-contract-pending.md")
    entries = [e for e in _ledger_entries(ledger) if "rdr229_completion" in e.note]
    assert entries, "no ledger entry (Unshipped or Shipped) names the rdr229_completion status field"
    for e in entries:
        assert e.additive is True, "an optional status field is additive"
        low = " ".join(e.note.split()).lower()
        assert "old client + new engine" in low and "new client + old engine" in low
        assert "not known" in low


def test_the_runbook_says_a_completed_stamp_needs_its_restamp_after_the_pause_is_released():
    """The registry stamp completes within about a minute; rows the previous engine writes after it stay unstamped and the re-add
    adds the CHECK back NOT VALID, so the way to finish is the restamp (verified by Rdr229P31RegistryStampRoundTripIntegrationTest)."""
    text = RUNBOOK.read_text(encoding="utf-8")
    flat = " ".join(text.split())
    release = text.index("After the re-add, release the pause")
    restamp = text.index("restamp its table")
    assert release < restamp < text.index("## If the tag was flipped without the down-step")
    step = flat[flat.index("restamp its table"):]
    assert "SELECT nexus.rdr229_restamp('catalog_collections');" in step
    assert "registered: rdr229-stamp-catalog_collections-rerun-1" in step
    assert "Rdr229P31RegistryStampRoundTripIntegrationTest" in step
    assert "blank `model_version`" in step and "run the re-add again" in step
