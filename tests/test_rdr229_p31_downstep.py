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
