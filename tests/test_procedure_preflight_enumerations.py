# SPDX-License-Identifier: AGPL-3.0-or-later
"""The preflight rows for the hand-kept enumerations (nexus-qo7ie.36).

Source: the maintainer's memory file feedback_hand_kept_enumerations.md, numbered R1 to R45 by
the census in T2 ``nexus/qo7ie.36-census-feedback_hand_kept_enumerations``. Each row is a
check a default-suite test or a hand-kept list needs and that no targeted run reaches: an MCP
tool's wire surface, a tuple template, a changeset's tenant table and DDL count. Each test says
which paths fire a row, which near-misses do not, what the row runs and what its check text
carries (the part a path list cannot decide). The harness is tests/_preflight_harness.py.
"""

from __future__ import annotations

import re
import tomllib
from pathlib import Path

import pytest

from tests._preflight_harness import (
    _SHELLS,
    REPO_ROOT,
    _bare,
    _change,
    _class_names,
    _fires,
    _inner,
    _rows,
    _Run,
    _run,
    repo,  # noqa: F401  (fixture)
    stubs,  # noqa: F401  (fixture)
)
from tests.test_procedure_preflight_contract import _collected, _pytest_step

CHANGESET = "service/src/main/resources/db/changelog/x-001.xml"
CATALOG_REPOSITORY = "service/src/main/java/dev/nexus/service/db/CatalogRepository.java"
TEMPLATE = "service/src/main/resources/tuples/templates/aspects.yaml"

MCP = "the MCP wire surface pins"
TUPLE = "the tuple template census"
HEALTH = "the health service checks"
DDL = "the DDL ceiling"

#: The one pytest argv each row emits, behind the result-contract wrapper (R44, R55 of the
#: test-runs-before-push census).
PYTEST = "uv run pytest -q -rf -p no:randomly"


def _check(out: dict, prefix: str) -> str:
    [row] = _rows(out, prefix)
    return row["check"]


# ---- the mcp-surface class (R18, R20, R21) ------------------------------------------------


@pytest.mark.parametrize(
    ("path", "member"),
    [
        ("src/nexus/mcp/core.py", True),
        ("src/nexus/mcp/hooks.py", True),
        ("src/nexus/mcp/sub/x.py", True),
        ("src/nexus/mcp/schema.json", True),
        ("src/nexus/mcp_infra.py", False),
        ("src/nexus/hooks/auto_approve.py", False),
        ("tests/test_mcp_package.py", False),
        ("docs/mcp-servers.md", False),
    ],
)
def test_r18_a_change_under_src_nexus_mcp_is_the_mcp_surface_class(
    repo: Path, path: str, member: bool
) -> None:
    _change(repo, {path: "x\n"})
    out = _Run(repo)()
    assert ("mcp-surface" in _class_names(out)) is member, path
    assert bool(_rows(out, MCP)) is member, path


def test_r18_the_mcp_surface_row_runs_the_three_pins_not_lint_marked(repo: Path) -> None:
    rows = _fires(repo, ["src/nexus/mcp/core.py"], MCP)
    [row] = rows
    assert (row["class"], row["heavy"]) == ("mcp-surface", False)
    # No -m: none of the three is lint-marked, so default addopts collect them all.
    assert _bare([row["command"]]) == [
        f"{PYTEST} tests/test_mcp_wire_snapshot.py tests/test_mcp_package.py"
        " tests/hooks/test_permission_request_hooks.py"
    ]


def test_r18_the_mcp_check_text_says_when_the_snapshot_moves_and_how_to_regenerate_it(
    repo: Path,
) -> None:
    _change(repo, {"src/nexus/mcp/core.py": "x\n"})
    text = _check(_Run(repo)(), MCP)
    assert "a parameter's type, name or default or the set of parameters" in text
    # What is NOT in the snapshot is part of the limit: a description edit moves nothing.
    assert "a description is not in the snapshot" in text
    assert "in the same commit" in text
    assert "`uv run python scripts/mcp_wire_snapshot.py --write`" in text
    assert "review the diff" in text


def test_r20_r21_the_mcp_check_text_names_the_lists_a_registration_edits(repo: Path) -> None:
    _change(repo, {"src/nexus/mcp/core.py": "x\n"})
    text = _check(_Run(repo)(), MCP)
    assert "test_core_registered_tools" in text
    # The hook lists are for hook_* tools only; any other tool is covered by _ALLOWED_TOOLS.
    assert "for a hook_* tool only, HOOK_TOOLS in src/nexus/mcp/hooks.py" in text
    assert "_ALLOWED_HOOK_TOOLS in src/nexus/hooks/auto_approve.py" in text
    # A new non-hook tool is ADDED to _ALLOWED_TOOLS, or, when destructive, to the manual-approval set
    # with a requires-manual-approval case; the drift guard fails on a tool in neither.
    assert "a new non-hook tool is ADDED to _ALLOWED_TOOLS in src/nexus/hooks/auto_approve.py" in text
    assert (
        "or, if it is destructive, to _MANUAL_APPROVAL_REQUIRED in tests/hooks/test_permission_request_hooks.py"
        " with a requires-manual-approval case" in text
    )
    assert "test_every_registered_conexus_tool_is_auto_approved" in text
    # The catalog server has its own name set, docstring count and doc row and heading.
    assert "a tool registered on the nexus-catalog server edits test_catalog_registered_tools" in text
    assert "the module docstring count of src/nexus/mcp/catalog.py" in text
    assert "the catalog row and section heading in docs/mcp-servers.md" in text
    # The three hand-typed counts and the lint that checks them (lint-marked, so the whole bucket).
    assert "docs/mcp-servers.md table row and section heading" in text
    assert "module docstring of src/nexus/mcp/core.py" in text
    assert "tests/test_mcp_tool_count_docs.py" in text
    assert "lint bucket" in text
    # R21's two greps, which no path list can run.
    assert "grep the tree for an existing tool's bare name" in text
    assert "the current count as a standalone number" in text


# ---- the tuple-template class (R22 to R26) ------------------------------------------------


@pytest.mark.parametrize(
    ("path", "member"),
    [
        (TEMPLATE, True),
        ("service/src/main/resources/tuples/templates/queue.yaml", True),
        ("service/src/main/resources/tuples/templates/sub/x.yaml", True),
        ("service/src/main/resources/tuples/other.yaml", False),
        ("service/src/main/resources/tuples/templatesX/a.yaml", False),
        ("service/src/test/resources/tuples/templates/a.yaml", False),
        ("src/nexus/commands/tuple_cmd.py", False),
    ],
)
def test_r23_a_change_under_the_tuple_templates_is_the_tuple_template_class(
    repo: Path, path: str, member: bool
) -> None:
    _change(repo, {path: "x\n"})
    out = _Run(repo)()
    assert ("tuple-template" in _class_names(out)) is member, path
    assert bool(_rows(out, TUPLE)) is member, path


def test_r23_the_tuple_template_row_runs_the_shakeout_census(repo: Path) -> None:
    [row] = _fires(repo, [TEMPLATE], TUPLE)
    assert (row["class"], row["heavy"]) == ("tuple-template", False)
    assert _bare([row["command"]]) == [f"{PYTEST} tests/test_tuple_shakeout_semantics.py"]


def test_r22_to_r26_the_tuple_check_text_lists_every_mirror_no_test_pins(repo: Path) -> None:
    _change(repo, {TEMPLATE: "x\n"})
    text = _check(_Run(repo)(), TUPLE)
    assert "needs no substrate" in text
    # R22: the Java count pins ride the engine suite.
    for java in ("TemplateRegistryTest", "TupleTemplatesTest", "TupleHandlerWiringTest", "TupleRepositoryTest"):
        assert java in text, java
    # R23: a census, not examples.
    assert "src/nexus/mcp/core.py" in text
    assert "EVERY template whose id_from is keys+nonce (a census, not examples)" in text
    # R24: the help no test pins.
    assert "the --nonce help of nx tuple out in src/nexus/commands/tuple_cmd.py, which no test pins" in text
    # R25: the five docs, and what the doc-path lint does not check.
    docs = re.findall(r"docs/[A-Za-z0-9_./-]+\.md", text)
    assert docs == [
        "docs/tuple-space.md",
        "docs/operations/tuple-space.md",
        "docs/architecture.md",
        "docs/storage-tiers.md",
        "docs/exploration/linda-in-nexus.md",
    ]
    assert "checks cited paths, not counts" in text
    # R26: the prose grep before the first run.
    assert "grep src/nexus and conexus for one existing template name in prose (queue/<name>)" in text


def test_the_files_the_enumeration_rows_name_exist() -> None:
    """A guessed name makes a reader (or pytest) find nothing: every path the new check texts
    name, outside the tests/ and scripts/ files the contract module already census-checks."""
    src = (REPO_ROOT / ".nexus" / "procedures" / "preflight.lua").read_text("utf-8")
    named = set(
        re.findall(
            r"\b((?:docs|src/nexus|service/src/test|service/src/main)/[A-Za-z0-9_./-]*\.(?:md|py|java))\b", src
        )
    )
    assert {"docs/tuple-space.md", "src/nexus/mcp/hooks.py", "src/nexus/health.py"} <= named, named
    assert {p for p in named if not (REPO_ROOT / p).exists()} == set()


# ---- the schema class (R10, R13, R17) -----------------------------------------------------


def test_r13_a_changeset_runs_the_health_service_checks(repo: Path) -> None:
    [row] = _fires(repo, [CHANGESET], HEALTH)
    assert (row["class"], row["heavy"]) == ("schema", False)
    assert _inner(row["command"]) == f"{PYTEST} tests/test_health_service_checks.py"


@pytest.mark.parametrize(
    ("path", "fires"),
    [
        (CHANGESET, True),
        # CatalogRepository.java is schema (the parity test reads it) but holds no tenant table list.
        (CATALOG_REPOSITORY, False),
        ("service/A.java", False),
        ("src/nexus/health.py", False),
        ("tests/test_health_service_checks.py", False),
    ],
)
def test_r13_the_health_row_follows_changesets_only(repo: Path, path: str, fires: bool) -> None:
    assert bool(_fires(repo, [path], HEALTH)) is fires


def test_r13_the_health_check_text_names_both_lists(repo: Path) -> None:
    _change(repo, {CHANGESET: "<x/>\n"})
    text = _check(_Run(repo)(), HEALTH)
    assert "a tenant table goes into _RLS_TENANT_TABLES in src/nexus/health.py" in text
    assert "the mirrored list in tests/test_health_service_checks.py" in text


def test_r13_the_schema_steps_come_in_the_order_cheap_first_parity_last(repo: Path) -> None:
    _change(repo, {CHANGESET: "<x/>\n"})
    checks = [c["check"] for c in _Run(repo)()["checks"] if c["class"] == "schema"]
    wanted = [
        "the changeset validation lint",
        HEALTH,
        DDL,
        "the collection-scoped tables schema parity test",
    ]
    assert len(checks) == len(wanted), checks
    assert all(c.startswith(w) for c, w in zip(checks, wanted)), checks


def _ceiling_line() -> str:
    """The line the command must print, read from the toml itself (tomllib for the value, the file for
    the line number), so it follows the ceiling the day it moves."""
    toml = REPO_ROOT / "tests" / "ddl_ceiling.toml"
    value = tomllib.loads(toml.read_text("utf-8"))["python_unit"]["migration_ddl_per_worker"]
    at = [n for n, line in enumerate(toml.read_text("utf-8").splitlines(), 1) if line.startswith("migration_ddl_per_worker = ")]
    assert len(at) == 1, at
    return f"{at[0]}:migration_ddl_per_worker = {value}"


@pytest.mark.parametrize("shell", _SHELLS)
def test_r10_the_ddl_command_prints_exactly_the_ceiling_line(repo: Path, stubs: Path, shell: str) -> None:
    """Exactly one line, equal to the current ceiling: an unanchored grep prints about forty comment lines,
    and the line is what the bump edits."""
    [row] = _fires(repo, [CHANGESET], DDL)
    rc, lines = _run(REPO_ROOT, stubs, row["command"], shell)
    assert rc == 0, lines
    assert lines == [_ceiling_line()], lines


@pytest.mark.parametrize("shell", _SHELLS)
def test_r10_the_ddl_command_fails_when_the_ceiling_key_is_gone(
    repo: Path, stubs: Path, tmp_path: Path, shell: str
) -> None:
    """The control: a toml without the key makes the step fail, so a command that cannot fail
    (`|| true`, a grep that matches anything) is red here."""
    [row] = _fires(repo, [CHANGESET], DDL)
    bare = tmp_path / "bare"
    (bare / "tests").mkdir(parents=True)
    (bare / "tests" / "ddl_ceiling.toml").write_text("[python_unit]\nother = 1\n", encoding="utf-8")
    rc, lines = _run(bare, stubs, row["command"], shell)
    assert rc != 0, lines


def test_r10_a_changeset_says_what_the_ddl_ceiling_counts_and_how_to_measure_the_bump(repo: Path) -> None:
    [row] = _fires(repo, [CHANGESET], DDL)
    assert (row["class"], row["heavy"]) == ("schema", False)
    text = row["check"]
    # Every DDL statement a fresh migration runs, however the changeset spells it.
    assert "counts every DDL statement a fresh migration runs" in text
    for kind in ("ALTER TABLE", "CREATE INDEX", "CREATE TRIGGER", "a REVOKE", "CREATE POLICY", "ENABLE and FORCE ROW LEVEL SECURITY"):
        assert kind in text, kind
    assert "a NO FORCE and FORCE ROW LEVEL SECURITY pair" in text
    assert "DDL a function runs" in text and "aspects-005" in text
    # A DROP is itself a counted statement (+1; vectors-036-1, aspects-005): the net goes down only
    # because a dropped table also removes its runAlways table grant (review r2).
    assert "a DROP lowers" not in text
    assert "a dropped table also removes its runAlways table grant" in text
    assert "changes nothing" not in text  # a NO FORCE toggle or a function's DDL counts with no spelled-out statement
    assert "migration_ddl_per_worker in tests/ddl_ceiling.toml in the same commit, tolerance 0" in text
    assert "citing the commit and the measurement on the comment line above" in text
    # The count follows the toml's method (a scoped substrate run), on a tree rebased onto develop.
    assert "Take the number from a scoped substrate run, not a hand count" in text
    assert "rebased onto current develop" in text
    assert "tests/test_scenario_journeys.py::test_store_put_is_searchable_and_catalogued" in text
    assert "`ddl: migration=N`" in text
    assert "runAlways table grant (+1, +2 with a sequence)" in text
    assert "CREATE FUNCTION plus its GRANT" not in text  # the source's hand count, superseded


def test_r10_the_ceiling_row_does_not_fire_for_catalog_repository_alone(repo: Path) -> None:
    assert not _fires(repo, [CATALOG_REPOSITORY], DDL)


# ---- text carried on rows that already exist (R1, R4, R41) ---------------------------------


def test_r1_r4_the_engine_main_suite_says_whole_suite_and_the_name_filter_is_the_inner_loop(
    repo: Path,
) -> None:
    _change(repo, {"service/src/main/java/A.java": "class A {}\n"})
    text = _check(_Run(repo)(), "the engine main suite")
    assert "needs the whole suite" in text
    assert "a behavioral constant a fixture is sized against" in text
    assert "a -Dtest name filter is the inner loop only" in text
    assert "*GateTest, *WiringTest and *ConformanceTest" in text
    assert "`grep -rl Main.java service/src/test tests`" in text
    # The repo's hand-run limits (AGENTS.md rule 13, docs/agents/test-running.md items 2 to 4).
    assert "through scripts/mvnw-leased.sh --lab-run, never wrapped in lab-run" in text
    assert "Once per tree" in text
    assert "this sha, clean" in text and "already has a green full engine run" in text
    assert "'no green full run of this tree exists'" in text
    assert "by sha and status file" in text
    assert "a fix round that touches only a test or a doc file does not trigger one" in text
    # The narrow triggers: a shared test changelog is consumed by every class that installs test
    # objects, so it runs the whole suite plus the integration group (loss check r1, d9d1a2891);
    # SQL-looking literals added to a Java test run a scanner set or the suite.
    assert "Two test-only edits do." in text
    assert "db/changelog-test/" in text and "the whole suite plus the integration group" in text
    assert "SQL-looking literals or fixtures" in text
    assert "-Dtest='*GateTest,*WiringTest,*ConformanceTest,*ScanTest,<touched>'" in text


def test_r41_the_drift_ledger_step_says_commit_first(repo: Path) -> None:
    _change(repo, {"src/nexus/x.py": "x = 1\n"})
    text = _check(_Run(repo)(), "the drift ledger and the lifecycle gate")
    assert "commit first" in text
    assert "diffs committed trees against the pinned tag" in text
    assert "an uncommitted run reports on the previous commit" in text
    # test_plugin_ledger.py is parser coverage, not an entry check: it is not a step of its own.
    assert "tests/test_plugin_ledger.py is parser coverage, not an entry check" in text


# ---- the new steps under the contract the other rows keep ---------------------------------

_NEW = [
    (MCP, ["src/nexus/mcp/core.py"], 3),
    (TUPLE, [TEMPLATE], 1),
    (HEALTH, [CHANGESET], 1),
]


@pytest.mark.parametrize(("prefix", "paths", "files"), _NEW)
def test_each_new_step_collects_a_test_from_each_file_it_names_under_its_own_argv(
    repo: Path, prefix: str, paths: list[str], files: int
) -> None:
    """The pin of the contract module, run on these rows by name so a row that loses its file (or
    gains a -m that deselects it) is red here even when the broad pin's paths stop reaching it."""
    _change(repo, {p: "x\n" for p in paths})
    [row] = _rows(_Run(repo)(), prefix)
    step = _pytest_step(row["command"])
    assert step is not None
    argv, named, appended = step
    assert len(named) == files, named  # non-vacuity: the row names what it should
    ids = _collected(argv, appended)
    for name in named:
        assert any(i.startswith(name + "::") for i in ids), (name, argv)


@pytest.mark.parametrize("shell", _SHELLS)
@pytest.mark.parametrize(("prefix", "paths", "files"), _NEW)
def test_each_new_pytest_step_is_gated_and_runs_under_the_shell(
    repo: Path, stubs: Path, prefix: str, paths: list[str], files: int, shell: str
) -> None:
    _change(repo, {p: "x\n" for p in paths})
    [row] = _rows(_Run(repo)(), prefix)
    assert row["command"].startswith("log=$(mktemp "), prefix
    assert " -rf " in _inner(row["command"]), prefix
    rc, lines = _run(repo, stubs, row["command"], shell)
    assert rc == 0, lines
    assert "1 passed in 0.01s" in lines
