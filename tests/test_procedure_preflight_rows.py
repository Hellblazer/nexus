# SPDX-License-Identifier: AGPL-3.0-or-later
"""The rows of the preflight procedure that a diff's paths decide (nexus-qo7ie.36).

Each test says which paths fire a row, which near-misses do not, and what the row runs.
The rules are cited by the numbers of the census in T2
``nexus/qo7ie.36-census-feedback_test_runs_before_push`` (R1 to R74). The harness is
tests/_preflight_harness.py.
"""

from __future__ import annotations

import re
import subprocess
import sys
from pathlib import Path

import pytest

from tests._preflight_harness import (
    BEAD_MANIFEST,
    _env,
    EVERYTHING,
    FOLD_CHECK,
    REPO_ROOT,
    _bare,
    _change,
    _class_names,
    _command,
    _commands,
    _fires,
    _git,
    _inner,
    _rows,
    _Run,
    _run,
    repo,  # noqa: F401  (fixture)
    stubs,  # noqa: F401  (fixture)
)



def test_a_procedure_change_runs_the_procedure_tests(repo: Path) -> None:
    """A shipped or repo Lua procedure is its own class, not unclassified: a
    change to one runs every procedure's tests (gate_record.lua was
    unclassified, so preflight never named its tests; nexus-qo7ie.34).
    From aba431241, with its command moved to the form the collect-only pin accepts."""
    _change(repo, {"src/nexus/procedures/x.lua": "return 1\n", ".nexus/procedures/y.lua": "return 2\n"})
    out = _Run(repo)()
    assert out["unclassified"] == {}
    assert _class_names(out) == ["procedure"]
    assert out["code"] is True
    assert _bare(_commands(out)["procedure"]) == [
        "uv run pytest -q -rf -p no:randomly --color=no -m 'not integration and not slow'"
        " tests/test_*procedure*.py tests/test_run_cmd.py"
    ]


@pytest.mark.parametrize(
    "path",
    [
        "src/nexus/procedures/research_add.lua",
        ".nexus/procedures/preflight.lua",
        # The shipped procedures' table inputs (test_mcp_procedure_run builds the tool description
        # from the shipped headers; these two tables feed the procedures themselves).
        "src/nexus/tables/audit-tags.toml",
        "src/nexus/tables/rdr-research-vocab.toml",
    ],
)
def test_each_procedure_input_is_the_procedure_class(repo: Path, path: str) -> None:
    _change(repo, {path: "x\n"})
    out = _Run(repo)()
    assert _class_names(out) == ["procedure"], path
    assert out["unclassified"] == {}


@pytest.mark.parametrize(
    "near",
    [
        "src/nexus/procedures/sub/x.lua",
        ".nexus/procedures/sub/x.lua",
        "src/nexus/procedures/x.py",
        "src/nexus/procedures/README.md",
        ".nexus/other.lua",
        ".nexus/procedures/x.txt",
        "src/nexus/tables/other.toml",
        "src/nexus/tables/audit-tags.toml.bak",
    ],
)
def test_a_near_miss_is_not_the_procedure_class(repo: Path, near: str) -> None:
    _change(repo, {near: "x\n"})
    assert "procedure" not in _class_names(_Run(repo)()), near


@pytest.mark.parametrize(
    ("paths", "fires"),
    [
        (["src/nexus/x.py"], True),
        (["service/A.java"], True),
        (["tests/test_x.py"], False),
        (["scripts/s.py"], False),
        (["docs/a.md"], False),
        (["conexus/skills/x/SKILL.md"], False),
    ],
)
def test_r5_the_full_python_suite_follows_src_nexus_and_service(
    repo: Path, paths: list[str], fires: bool
) -> None:
    rows = _fires(repo, paths, "the full Python suite")
    assert bool(rows) is fires
    if fires:
        [row] = rows
        assert (row["class"], row["heavy"]) == ("tripwire", True)
        # Once per tree (AGENTS.md rule 13): the text says when to skip it.
        assert "already has a green full run" in row["check"]
        inner = _inner(row["command"])
        assert inner.startswith("lab-remote -c lab-l --fetch ")
        assert "scripts/build-gate-jar.sh" in inner and "-n auto" in inner


def test_r5_the_full_suite_is_the_last_check(repo: Path) -> None:
    _change(repo, EVERYTHING)
    out = _Run(repo)()
    assert out["checks"][-1]["check"].startswith("the full Python suite")


@pytest.mark.parametrize(
    ("paths", "fires"),
    [
        (["service/A.java", "src/nexus/x.py"], True),
        # The script's own pairing also counts a tests/ file holding a raw
        # /manifest or /import envelope, which has no src/nexus/ path, and it
        # decides per commit: so any service/ change runs it (loss check r1 L2).
        (["service/A.java"], True),
        (["service/A.java", "tests/test_x.py"], True),
        (["src/nexus/x.py"], False),
        (["tests/test_x.py"], False),
    ],
)
def test_r7_the_wire_pairing_check_runs_for_any_service_change(
    repo: Path, paths: list[str], fires: bool
) -> None:
    rows = _fires(repo, paths, "the wire-contract pairing check")
    assert bool(rows) is fires
    if fires:
        [row] = rows
        assert row["command"] == "uv run python scripts/check_wire_contract_pairing.py"
        assert row["heavy"] is False
        # It scans commits, so it runs after the commit; the ledger bullet's shape.
        assert "after the commit" in row["check"]
        assert "docs/wire-contract-pending.md" in row["check"]


@pytest.mark.parametrize(
    ("path", "fires"),
    [
        ("src/nexus/db/t2/http_memory_store.py", True),
        ("src/nexus/db/t2/http_taxonomy_store.py", True),
        ("src/nexus/db/http_scratch_store.py", True),
        ("src/nexus/db/t2/records.py", False),
        ("src/nexus/db/http_vector_client.py", False),
        ("tests/db/test_http_t2_store_parity.py", False),
    ],
)
def test_r8_the_store_parity_test_follows_the_http_stores(
    repo: Path, path: str, fires: bool
) -> None:
    rows = _fires(repo, [path], "the Http store parity test")
    assert bool(rows) is fires
    if fires:
        assert _inner(rows[0]["command"]).endswith(
            "pytest -q -rf -p no:randomly --color=no tests/db/test_http_t2_store_parity.py"
        )


@pytest.mark.parametrize(
    ("path", "fires"),
    [("conexus/hooks/hooks.json", True), ("conexus/hooks/x.py", False), ("conexus/hooks.json", False)],
)
def test_r10_hooks_json_gets_the_sc8_upgrade_test(
    repo: Path, path: str, fires: bool
) -> None:
    rows = _fires(repo, [path], "the SC8 hooks.json test")
    assert bool(rows) is fires
    if fires:
        assert rows[0]["class"] == "plugin"
        assert _inner(rows[0]["command"]) == (
            "uv run pytest -q -rf -p no:randomly --color=no tests/test_upgrade_e2e.py -k SC8"
        )


@pytest.mark.parametrize(
    ("path", "fires"),
    [("src/nexus/x.py", True), ("src/nexus/catalog/y.py", True), ("tests/test_x.py", False), ("service/A.java", False)],
)
def test_r11_the_catalog_gate_census_follows_src_nexus(
    repo: Path, path: str, fires: bool
) -> None:
    rows = _fires(repo, [path], "the catalog gate census")
    assert bool(rows) is fires
    if fires:
        assert _inner(rows[0]["command"]).endswith(
            "pytest -q -rf -p no:randomly --color=no tests/test_kmo9h_catalog_gate_census.py"
        )


def test_r13_the_cli_output_invariant_is_in_the_lint_bucket_not_the_changed_tests_run(
    repo: Path,
) -> None:
    """The source names two default-loop guards. The mode guard is still one (it judges
    ``request.session.items``, so it rides in the changed tests' invocation). The output
    invariant became a lint test (dc2a2cca0), which default addopts deselect: naming it in that
    invocation would run nothing, so the whole lint bucket carries it. This pin goes red the
    day it loses the marker, when it has to be appended again."""
    invariant = (REPO_ROOT / "tests/test_cli_runner_output_invariant.py").read_text("utf-8")
    assert re.search(r"^pytestmark = pytest\.mark\.lint$", invariant, re.M)
    mode = (REPO_ROOT / "tests/test_mode_declarations_are_explicit.py").read_text("utf-8")
    assert not re.search(r"^pytestmark\s*=.*lint", mode, re.M)
    _change(repo, {"tests/sub/test_x.py": "def test(): pass\n"})
    cmd = _inner(_command(_Run(repo)(), "the changed test files"))
    assert "test_mode_declarations_are_explicit.py" in cmd
    assert "test_cli_runner_output_invariant" not in cmd


@pytest.mark.parametrize(
    ("path", "fires"),
    [
        ("tests/sub/test_x.py", True),
        ("tests/_tmp_root.py", True),
        ("tests/conftest.py", True),
        ("src/nexus/x.py", False),
        ("tests/e2e/x.sh", False),
    ],
)
def test_r31_the_tmp_root_scan_follows_any_test_file(
    repo: Path, path: str, fires: bool
) -> None:
    """The scan (test_every_tmpdir_override...) walks every tests/**/*.py and is not
    lint-marked, so any changed python file under tests/ can trip it."""
    rows = _fires(repo, [path], "the TMPDIR override scan")
    assert bool(rows) is fires
    if fires:
        assert _inner(rows[0]["command"]).endswith(
            "pytest -q -rf -p no:randomly --color=no tests/test_tmp_root.py"
        )


def test_r33_untracked_files_are_checked_before_the_lint_bucket(
    repo: Path, stubs: Path
) -> None:
    _change(repo, {"src/nexus/x.py": "x = 1\n"})
    out = _Run(repo)()
    names = [c["check"] for c in out["checks"]]
    pre = next(i for i, n in enumerate(names) if n.startswith("precondition of the lint bucket"))
    assert names[pre + 1].startswith("the whole lint bucket"), names
    assert out["checks"][pre]["heavy"] is False
    cmd = out["checks"][pre]["command"]
    assert _run(repo, stubs, cmd)[0] == 0
    (repo / "new file.txt").write_text("x\n", encoding="utf-8")
    rc, lines = _run(repo, stubs, cmd)
    assert rc != 0 and any("new file.txt" in line for line in lines), lines
    _git(repo, "add", "--", "new file.txt")
    assert _run(repo, stubs, cmd)[0] == 0


@pytest.mark.parametrize(
    ("paths", "fires"),
    [(["docs/a.md"], True), (["docs/rdr/x.md", "src/nexus/x.py"], True), (["README.md"], False), (["web/index.html"], False)],
)
def test_r37_a_docs_change_refreshes_the_bead_manifest(
    repo: Path, paths: list[str], fires: bool
) -> None:
    rows = _fires(repo, paths, "the bead ids cited in docs/")
    assert bool(rows) is fires
    if fires:
        assert (rows[0]["class"], rows[0]["heavy"]) == ("docs", False)
        assert _inner(rows[0]["command"]) == BEAD_MANIFEST
        # The remedy is named for when the lint fails, never required up front.
        assert "refresh_bead_id_manifest.py" in rows[0]["check"]


def test_r37_never_requires_the_manifest_to_hold_every_live_bead(repo: Path) -> None:
    """A refresh pulls in every bead filed since the last one, cited or not, so a
    refresh-then-clean-diff step reds every docs batch whenever anyone has filed a
    bead (2026-11-09: nexus-z7can and nexus-zu8rq, cited nowhere)."""
    _change(repo, {"docs/a.md": "a\n"})
    cmd = _command(_Run(repo)(), "the bead ids cited in docs/")
    # The whole command is the two lint tests under the result contract, nothing before
    # them: no refresh, no diff of the manifest (bead-row review r1, note a).
    assert _inner(cmd) == BEAD_MANIFEST
    assert "bead_ids.txt" not in _inner(cmd)
    assert "refresh_bead_id_manifest" not in _inner(cmd)


def test_r37_the_bead_row_selects_exactly_its_two_lint_tests() -> None:
    """The row selects by -k substring; a later test whose name contains either fragment would
    run too, unseen by any pin (bead-row review r1, note b). Collect over the row's own argv."""
    argv = BEAD_MANIFEST.removeprefix("uv run pytest -q ")  # one -q below: two print counts, not ids
    proc = subprocess.run(
        ["bash", "-c", f"{sys.executable} -m pytest --collect-only -q -p no:cacheprovider {argv}"],
        cwd=REPO_ROOT, env={**_env(), "NX_TEST_T2_SUBSTRATE": "none"},
        capture_output=True, text=True, encoding="utf-8", check=False,
    )
    ids = sorted(line for line in proc.stdout.splitlines() if "::" in line)
    assert [i.split("::")[-1].split("[")[0] for i in ids] == [
        "test_bead_id_manifest_is_fresh_where_bd_is",
        "test_cited_bead_ids_resolve",
    ], proc.stdout[-2000:]


@pytest.mark.parametrize(
    ("path", "fires"),
    [("tests/e2e/x.sh", True), ("tests/e2e/sub/y.py", True), ("tests/scripts/test_x.py", False), ("tests/hooks/x.py", False)],
)
def test_r39_an_e2e_change_runs_tests_scripts(repo: Path, path: str, fires: bool) -> None:
    rows = _fires(repo, [path], "the e2e script census")
    assert bool(rows) is fires
    if fires:
        assert _inner(rows[0]["command"]).endswith("pytest -q -rf -p no:randomly --color=no tests/scripts")


@pytest.mark.parametrize(
    ("path", "fires"),
    [
        ("src/nexus/exporter.py", True),
        ("src/nexus/mcp/core.py", True),
        # The gate AST-scans every fire_batch( call site under src/nexus/, so an indexer
        # edit or a new module reds it as surely as an anchored file (z7can item 7).
        ("src/nexus/indexer.py", True),
        ("src/nexus/catalog/note_write.py", True),
        ("src/nexus/health.py", True),
        ("src/nexus/procedures/x.lua", False),
        ("src/conexus_shim.py", False),
        ("tests/test_vw594_fence_coverage_gate.py", False),
    ],
)
def test_r62_the_fence_gate_follows_every_python_file_it_scans(
    repo: Path, path: str, fires: bool
) -> None:
    rows = _fires(repo, [path], "the vw594 fence coverage gate")
    assert bool(rows) is fires
    if fires:
        assert _inner(rows[0]["command"]).endswith(
            "pytest -q -rf -p no:randomly --color=no tests/test_vw594_fence_coverage_gate.py"
        )


# ---- three rows from the day's develop reds (coordinator, 2026-11-09) ------------------


@pytest.mark.parametrize(
    ("path", "fires"),
    [
        ("src/nexus/commands/store.py", True),
        ("src/nexus/commands/sub/x.py", True),
        ("src/nexus/cli.py", True),
        ("src/nexus/cli_helpers.py", False),
        ("src/nexus/x.py", False),
        ("tests/test_cli_registration.py", False),
    ],
)
def test_the_cli_registration_test_follows_commands_and_cli(
    repo: Path, path: str, fires: bool
) -> None:
    """It checks every command module is wired into cli.py: a new or moved command module
    under src/nexus/commands/, or an edit to cli.py, can break it, and no other test names them."""
    rows = _fires(repo, [path], "the CLI registration test")
    assert bool(rows) is fires
    if fires:
        assert rows[0]["class"] == "python"
        assert _inner(rows[0]["command"]).endswith(
            "pytest -q -rf -p no:randomly --color=no tests/test_cli_registration.py"
        )


@pytest.mark.parametrize(
    ("path", "fires"),
    [
        ("service/src/main/resources/db/changelog/x-001.xml", True),
        ("service/src/main/java/dev/nexus/service/db/CatalogRepository.java", True),
        ("service/src/main/java/dev/nexus/service/db/CatalogHandler.java", False),
        ("service/A.java", False),
        ("src/nexus/x.py", False),
    ],
)
def test_the_collection_scoped_tables_parity_test_follows_changelogs_and_catalog_repository(
    repo: Path, path: str, fires: bool
) -> None:
    """It reads the changelogs against CatalogRepository.COLLECTION_SCOPED_TABLES through a
    live engine substrate (a physical_collection column went red at 939625129), so it is heavy
    and comes after the gate jar."""
    rows = _fires(repo, [path], "the collection-scoped tables schema parity test")
    assert bool(rows) is fires
    if fires:
        assert (rows[0]["class"], rows[0]["heavy"]) == ("schema", True)
        assert _inner(rows[0]["command"]).endswith(
            "pytest -q -rf -p no:randomly --color=no tests/catalog/test_collection_scoped_tables_schema_parity.py"
        )


def test_a_catalog_repository_change_is_schema_but_needs_no_changeset_validation(
    repo: Path,
) -> None:
    _change(repo, {"service/src/main/java/dev/nexus/service/db/CatalogRepository.java": "x\n"})
    out = _Run(repo)()
    assert _class_names(out) == ["engine", "schema"]
    assert not _rows(out, "the changeset validation lint")


@pytest.mark.parametrize(
    ("path", "fires"),
    [
        ("conexus/hooks/hooks.json", True),
        ("tests/e2e/hook-cli-skew/run.sh", True),
        ("conexus/hooks/x.py", False),
        ("tests/e2e/hook-cli/run.sh", False),
    ],
)
def test_the_hooks_json_pin_tests_follow_hooks_json_and_the_skew_harness(
    repo: Path, path: str, fires: bool
) -> None:
    """They pin hooks.json's entry count and form; the hook-floor red came from a new hook
    entry. Two are lint-marked, so the step selects whatever the mark."""
    rows = _fires(repo, [path], "the hooks.json pin tests")
    assert bool(rows) is fires
    if fires:
        assert rows[0]["class"] == "plugin"
        assert _inner(rows[0]["command"]) == (
            "uv run pytest -q -rf -p no:randomly --color=no -m 'not integration and not slow'"
            " tests/scripts/test_hook_harness_launcher_form.py"
            " tests/test_hook_budgets_pinned_to_hooks_json.py"
            " tests/test_hooks_json_verb_release_floor.py"
        )
    if path.startswith("tests/e2e/hook-cli-skew/"):
        assert "plugin" in _class_names(_Run(repo)())


@pytest.mark.parametrize(
    ("paths", "fires"),
    [
        (["changelog/unreleased/nexus-x.md"], True),
        (["CHANGELOG.md"], True),
        (["conexus/CHANGELOG.md"], True),
        (["docs/a.md"], False),
        (["changelog/unreleased/sub/nexus-x.md"], False),
    ],
)
def test_a_fragment_or_changelog_change_runs_the_fragment_check(
    repo: Path, paths: list[str], fires: bool
) -> None:
    rows = _fires(repo, paths, "the changelog fragments")
    assert bool(rows) is fires
    if fires:
        assert (rows[0]["class"], rows[0]["heavy"]) == ("docs", False)
        assert rows[0]["command"] == FOLD_CHECK


def test_a_fragment_alone_is_a_docs_change_as_ci_reads_it(repo: Path) -> None:
    _change(repo, {"changelog/unreleased/nexus-x.md": "### Added\n- x\n"})
    out = _Run(repo)()
    assert (out["code"], _class_names(out), out["unclassified"]) == (False, ["docs"], {})


# ---- the changed-tests trigger and the daemon class (z7can item 2; window 41) ----------------


@pytest.mark.parametrize(
    ("path", "fires"),
    [
        ("tests/test_x.py", True),
        ("tests/sub/test_x.py", True),
        ("tests/a/b/test_y.py", True),
        # helpers whose names only contain "test_"
        ("tests/_test_helpers.py", False),
        ("tests/helpers/latest_x.py", False),
        ("tests/sub/attest_x.py", False),
        ("tests/test_x.txt", False),
        ("src/test_x.py", False),
    ],
)
def test_the_changed_tests_step_follows_test_files_only(repo: Path, path: str, fires: bool) -> None:
    assert bool(_fires(repo, [path], "the changed test files")) is fires


@pytest.mark.parametrize(
    ("path", "fires"),
    [
        ("src/nexus/daemon/service_registry.py", True),
        ("src/nexus/daemon/sub/x.py", True),
        ("src/nexus/daemonx.py", False),
        ("src/nexus/daemon/AGENTS.md", False),  # prose no test reads: not a heavy run
        ("src/nexus/x_daemon/y.py", False),
        ("tests/daemon/test_x.py", False),
    ],
)
def test_a_daemon_change_runs_the_whole_daemon_directory(repo: Path, path: str, fires: bool) -> None:
    """A change under src/nexus/daemon/ ran the test file named for its module and not the file
    beside it that pins the same module (window 41: test_service_registry_election_bound.py)."""
    rows = _fires(repo, [path], "the daemon tests")
    assert bool(rows) is fires
    if fires:
        [row] = rows
        assert (row["class"], row["heavy"]) == ("daemon", True)
        assert _inner(row["command"]) == (
            "lab-remote -- bash -c 'scripts/build-gate-jar.sh && uv run pytest -q -rf -p no:randomly"
            ' --color=no -m "not integration and not slow" -n auto tests/daemon\''
        )
        assert "daemon" in _class_names(_Run(repo)())


def test_every_row_that_builds_the_jar_under_lab_remote_says_what_75_means(repo: Path) -> None:
    """Inside lab-remote's grant a held build lease exits 75 at once, and the script's own hint
    (--lab-run, outside lab-run) is one lab-remote cannot follow (preflight review r1)."""
    _change(repo, {**EVERYTHING, "src/nexus/daemon/x.py": "x = 1\n"})
    rows = [c for c in _Run(repo)()["checks"]
            if "lab-remote" in c["command"] and "build-gate-jar.sh" in c["command"]]
    names = sorted(c["check"].split(".")[0] for c in rows)
    assert len(rows) == 4, names  # lint bucket, integration, daemon, full suite
    for c in rows:
        assert "exit 75 from the gate jar build" in c["check"] and "rerun" in c["check"], c["check"]
