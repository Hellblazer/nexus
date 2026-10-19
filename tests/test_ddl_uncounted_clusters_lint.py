# SPDX-License-Identifier: AGPL-3.0-or-later
"""nexus-sz651: every Postgres cluster start site the scan finds in the test tree is either read by
the DDL/bytes counter or listed in ``tests/ddl_uncounted_clusters.toml``.

A counter that sees one kind of cluster and says nothing about the others reports a smaller
number than the run caused. The allowlist makes the omission a committed figure, and this lint
keeps it equal to the tree in both directions (a new site, and a site that went away).

nexus-szyun adds the second half: where a test cluster's DATA DIRECTORY comes from (``pg_data_tempdir``, which
lands on the run's tmpfs root, or a listed exception), and that a ``docker run`` of Postgres mounts its data dir
as tmpfs.

WHAT THIS LINT DOES NOT SCAN, so a green run is not read as "every cluster the suite starts":

* ``scripts/**`` (the Windows smoke tools are standalone programs run on a Windows host, and cannot import the
  helper), for the data-dir half; the census half does scan ``scripts/*.py``;
* ``tests/e2e/*.sh`` and every other shell script that starts a cluster (their data lives under ``$SCRATCH``);
* a ``docker run`` whose argv is not a literal list in a ``.py`` file under ``tests/``;
* a cluster started by product code under ``src/`` that a test merely drives (the five ``provision()`` files
  are listed, but a new caller reached through a helper in another module is only seen if that helper's own
  file has a ``provision``, ``_init_cluster``, ``pg_ctl_start`` or initdb argv call);
* the Java suites (``PgContainerTmpfsGateTest`` covers them) and the jOOQ code generator's container;
* what a listed file does: an entry excuses its whole file, bounded by the ceilings on the table, not per use.

The figure "N of M data-dir uses come from the helper" counts call sites the scan understands, not clusters
and not bytes: the five ``disk_required`` files hold most of the remaining non-substrate clusters and contribute
no helper-made use at all.
"""
from __future__ import annotations

import tomllib
from collections import Counter
from pathlib import Path

import pytest

from tests import _ddl_cluster_sites as sites
from tests import _ddl_counter as ddl

pytestmark = pytest.mark.lint


def _allowlist() -> tuple[list[str], dict[str, int]]:
    data = tomllib.loads(sites.ALLOWLIST.read_text())
    return list(data["counted"]["sites"]), {k: int(v) for k, v in data["uncounted"].items()}


def test_the_allowlist_equals_the_tree() -> None:
    found = sites.scan()
    counted, uncounted = _allowlist()
    assert counted == list(sites.COUNTED)
    expected = {k: n for k, n in found.items() if k not in sites.COUNTED}
    new = {k: n for k, n in expected.items() if uncounted.get(k) != n}
    gone = {k: n for k, n in uncounted.items() if k not in expected}
    assert not new and not gone, (
        "cluster start sites changed. A NEW site is a cluster the counter cannot see: read it through "
        "tests/_ddl_counter.py or list it on purpose (uv run python -m tests._ddl_cluster_sites --write, "
        f"then review the diff). new/changed: {new}; gone: {gone}"
    )


def test_the_counted_site_is_really_in_the_tree() -> None:
    """A counted entry the scan no longer finds would let the lint pass over a substrate that moved."""
    found = sites.scan()
    for site in sites.COUNTED:
        assert found[site] >= 1, site


def test_the_scan_is_not_vacuous() -> None:
    """The census must examine a real tree: the committed figure is dozens of sites, not zero."""
    found = sites.scan()
    assert sum(found.values()) >= 30, dict(found)
    assert any(k.startswith("tests/db/") for k in found)
    assert sites.POM_SITE in found


def test_the_report_figure_is_the_uncounted_sum() -> None:
    _, uncounted = _allowlist()
    assert ddl.uncounted_cluster_count() == sum(uncounted.values()) > 0


def test_the_scan_finds_a_site_in_a_fixture_tree(tmp_path: Path) -> None:
    (tmp_path / "tests").mkdir()
    (tmp_path / "scripts").mkdir()
    (tmp_path / "tests" / "test_a.py").write_text(
        'from tests._pg_ctl import pg_ctl_start\n_INITDB = exe_name("initdb")\n'
        "def fixture():\n    pg_ctl_start('x')\n    helpers.pg_ctl_start('y')\n"
        "async def other():\n    def inner():\n        pg_ctl_start('z')\n"
    )
    (tmp_path / "tests" / "test_bad.py").write_text("def broken(:\n")
    assert sites.scan(tmp_path) == Counter({
        "tests/test_a.py::<module>::initdb": 1,
        "tests/test_a.py::fixture::pg_ctl_start": 2,
        "tests/test_a.py::other::pg_ctl_start": 1,
    })


# ── Where a test cluster's data directory comes from (nexus-szyun) ───────────


def test_every_test_cluster_data_dir_comes_from_the_substrate_helper() -> None:
    """A cluster under tmp_path / tmp_path_factory / mkdtemp writes its data and WAL to the CI host's disk and
    never reaches the run's tmpfs root or the "clusters on tmpfs N, on disk M" summary. Every initdb / postgres
    argv, ``_init_cluster`` and ``pg_ctl_start`` data dir must be ``pg_data_tempdir(...)`` itself (not nested, no
    ``parent_dir=``), every ``provision(<dir>)`` dir must be a listed non-helper dir, and a file that starts a
    cluster through an argv the scan cannot read must be listed, all with a reason in
    ``tests/pg_data_dir_exceptions.toml``. The exceptions table may only shrink."""
    assert sites.provenance_problems() == []


def test_the_provenance_scan_is_not_vacuous() -> None:
    """Floors are on what the scan SEES (helper-made uses, every kind of use), never on the exceptions table:
    nexus-0ajjm drains ``disk_required`` to empty and must not have to edit a floor to do it."""
    uses = sites.data_dir_uses()
    helper = [u for u in uses if u.from_helper]
    assert len(helper) >= 40, f"the scan traced only {len(helper)} helper-made data dirs"
    assert {u.kind for u in helper} >= {"initdb", "pg_ctl_start", "init_cluster"}
    assert {u.kind for u in uses} >= {"initdb", "pg_ctl_start", "init_cluster", "provision"}, (
        "the scan saw no provision() call or no argv/call of one kind it exists to follow"
    )
    # The two fixtures that used to call tmp_path_factory.mktemp (nexus-szyun) are among them.
    for converted in ("tests/db/test_nexus_diag_role.py", "tests/db/test_svc_monitor_role_live.py"):
        assert any(u.file == converted and u.kind == "init_cluster" and u.from_helper for u in uses), converted
    not_cluster, disk = sites.exceptions()
    assert not set(disk) & {u.file for u in helper}, "a file proven to use the helper is still listed as disk-required"
    # The ceilings are the committed counts and the tree is at or under them: the tables shrink, never grow.
    assert len(not_cluster) <= sites.NOT_A_CLUSTER_CEILING and len(disk) <= sites.DISK_REQUIRED_CEILING
    runs = sites.docker_pg_runs()
    assert {f for f, _, _ in runs} >= {
        "tests/db/test_write_seam_gate_integration.py", "tests/db/test_http_combined_query_integration.py",
    }, runs


def _fixture_repo(tmp_path: Path, files: dict[str, str], toml: str = "") -> Path:
    (tmp_path / "scripts").mkdir(parents=True)
    for rel, body in files.items():
        f = tmp_path / rel
        f.parent.mkdir(parents=True, exist_ok=True)
        f.write_text(body)
    (tmp_path / "tests" / sites.EXCEPTIONS.name).write_text(toml or "[not_a_cluster]\n[disk_required]\n")
    return tmp_path


_GOOD = (
    "from tests._child_process import pg_data_tempdir\n"
    "def fx():\n"
    "    d = pg_data_tempdir('x_')\n"
    "    pgdata = d\n"
    "    subprocess.run([str(_INITDB), '-D', pgdata, '-U', 'u'])\n"
    "    pg_ctl_start(_PG_CTL, str(pgdata), log, '-p 1')\n"
    "    _init_cluster(bins, Path(pgdata), 'u', superuser_password='p')\n"
)

_WHY = "provision() nests pgdata under the dir the test supplies, which the sweep cannot reap (drained by nexus-0ajjm)"


@pytest.mark.parametrize(
    "bad_line",
    [
        "pgdata = tmp_path_factory.mktemp('pg') / 'data'",
        "pgdata = tempfile.mkdtemp(prefix='pg_')",
        "pgdata = pg_data_tempdir('x_', parent_dir=str(tmp_path))",
        "pgdata = Path(pg_data_tempdir('x_')) / 'data'",
        "pgdata = f'{pg_data_tempdir(\"x_\")}/data'",
        "pgdata = os.path.join(pg_data_tempdir('x_'), 'data')",
    ],
)
def test_the_provenance_scan_flags_a_data_dir_not_made_by_the_helper(tmp_path: Path, bad_line: str) -> None:
    body = f"def fx(tmp_path_factory, tmp_path):\n    {bad_line}\n    subprocess.run([str(_INITDB), '-D', str(pgdata)])\n"
    repo = _fixture_repo(tmp_path, {"tests/test_a.py": body})
    problems = sites.provenance_problems(repo)
    assert len(problems) == 1 and "test_a.py" in problems[0] and "initdb" in problems[0], problems


def test_the_provenance_scan_accepts_the_helper_through_aliases_and_wrappers(tmp_path: Path) -> None:
    repo = _fixture_repo(tmp_path, {"tests/test_a.py": _GOOD})
    assert sites.provenance_problems(repo) == []
    assert [(u.kind, u.from_helper) for u in sites.data_dir_uses(repo)] == [
        ("initdb", True), ("pg_ctl_start", True), ("init_cluster", True),
    ]


def test_the_provenance_scan_flags_a_parameter_a_missing_arg_and_mixed_bindings(tmp_path: Path) -> None:
    repo = _fixture_repo(tmp_path, {
        "tests/test_param.py": "def fx(pgdata):\n    subprocess.run([str(_INITDB), '-D', pgdata])\n",
        "tests/test_missing.py": "def fx():\n    pg_ctl_start(_PG_CTL)\n",
        "tests/test_mixed.py": (
            "def fx(flag, tmp_path):\n"
            "    pgdata = pg_data_tempdir('x_')\n"
            "    if flag:\n        pgdata = str(tmp_path)\n"
            "    subprocess.run([str(_INITDB), '-D', pgdata])\n"
        ),
    })
    problems = sites.provenance_problems(repo)
    # test_missing.py is named twice: its use has no data dir, and its pg_ctl_start site traces to no helper use.
    assert sorted({p.split(":")[0] for p in problems}) == [
        "tests/test_missing.py", "tests/test_mixed.py", "tests/test_param.py",
    ], problems


# A file with one helper-made cluster is "proven" only if every other way it can start one is traced too: each of
# these appends a SECOND cluster, in a form the first cut of the scan could not see, to a clean file.
_CLEAN_BASE = (
    "from tests._child_process import pg_data_tempdir\n"
    "def good():\n"
    "    d = pg_data_tempdir('x_')\n"
    "    subprocess.run([str(_INITDB), '-D', d])\n"
    "    pg_ctl_start(_PG_CTL, d, log, '-p 1')\n"
)


def test_the_clean_base_for_the_untraced_forms_is_clean(tmp_path: Path) -> None:
    """Positive control: the base the next test appends to is itself accepted, with its argv read through a name."""
    resolved = _CLEAN_BASE + (
        "def named():\n"
        "    d = pg_data_tempdir('y_')\n"
        "    cmd = [str(_INITDB), '-D', d]\n"
        "    subprocess.run(cmd)\n"
    )
    assert sites.provenance_problems(_fixture_repo(tmp_path, {"tests/test_a.py": resolved})) == []


@pytest.mark.parametrize(
    "bad",
    [
        "def bad(cmd):\n    subprocess.run(cmd)\n",
        "def bad(tmp_path):\n    subprocess.run(build_cmd(tmp_path))\n",
        "def bad(tmp_path):\n    subprocess.run([str(_INITDB), *['-D', str(tmp_path)]])\n",
        "def bad(tmp_path):\n    subprocess.run([str(_INITDB), f'-D{tmp_path}'])\n",
        "def bad(tmp_path):\n    subprocess.run([str(_INITDB), '--pgdata=' + str(tmp_path)])\n",
        "def bad(tmp_path):\n    subprocess.run([str(_INITDB), '--pgdata', str(tmp_path)])\n",
        "def bad(tmp_path):\n    subprocess.run(f'initdb -D {tmp_path}', shell=True)\n",
        "def bad(tmp_path):\n    subprocess.Popen([str(_POSTGRES), '-D', str(tmp_path)])\n",
        "def bad(tmp_path):\n    cmd = [str(_INITDB), '-D', str(tmp_path)]\n    subprocess.run(cmd)\n",
        "def bad(tmp_path):\n    cmd = [str(_INITDB)]\n    cmd += ['-D', str(tmp_path)]\n    subprocess.run(cmd)\n",
        "def bad():\n    d = pg_data_tempdir('x_')\n    d += '/data'\n    subprocess.run([str(_INITDB), '-D', d])\n",
        "from tests._pg_ctl import pg_ctl_start as _start\ndef bad(tmp_path):\n    _start(_PG_CTL, str(tmp_path), log)\n",
    ],
    ids=[
        "variable-argv-param", "argv-built-by-a-call", "splat", "f-string-D", "pgdata-equals", "pgdata-flag",
        "shell-string", "direct-postgres", "named-list-tmp-path", "named-list-augmented", "augassign-nesting",
        "import-alias",
    ],
)
def test_a_second_cluster_in_a_form_the_first_cut_missed_is_flagged(tmp_path: Path, bad: str) -> None:
    repo = _fixture_repo(tmp_path, {"tests/test_a.py": _CLEAN_BASE + bad})
    problems = sites.provenance_problems(repo)
    assert problems and all("tests/test_a.py" in p for p in problems), problems
    # ... and a listing with a reason excuses it (the table, not silence, is where an exception lives).
    toml = f'[not_a_cluster]\n"tests/test_a.py" = "{_WHY}"\n[disk_required]\n'
    assert sites.provenance_problems(_fixture_repo(tmp_path / "listed", {"tests/test_a.py": _CLEAN_BASE + bad}, toml)) == []


@pytest.mark.parametrize(
    "argv",
    [
        "['initdb.exe', '--version']",
        "[str(_PG_CTL), 'stop', '-D', str(tmp_path)]",
        "[('bin/initdb.exe', 'initdb.exe'), ('bin/pg_ctl.exe', 'pg_ctl.exe')]",
        "['psql', '-c', 'select 1']",
    ],
)
def test_an_argv_that_starts_no_cluster_is_not_flagged(tmp_path: Path, argv: str) -> None:
    body = _CLEAN_BASE + f"def other(tmp_path):\n    subprocess.run({argv})\n"
    assert sites.provenance_problems(_fixture_repo(tmp_path, {"tests/test_a.py": body})) == []


def test_traced_uses_are_counted_against_census_sites_not_one_proof_per_file(tmp_path: Path) -> None:
    """One helper-made use does not prove a file with two census sites (an initdb lookup and a pg_ctl_start the
    scan could not trace the dir of)."""
    body = (
        "_INITDB = bin_dir / exe_name('initdb')\n"
        "def fx():\n"
        "    subprocess.run([str(_INITDB), '-D', pg_data_tempdir('x_')])\n"
        "    other_start(_PG_CTL)\n"
        "    pg_ctl_start(_PG_CTL, pg_data_tempdir('y_'), log)\n"
        "    pg_ctl_start(_PG_CTL, pg_data_tempdir('z_'), log)\n"
        "_SECOND = exe_name('initdb')\n"
        "_THIRD = exe_name('initdb')\n"
        "_FOURTH = exe_name('initdb')\n"
    )
    problems = sites.provenance_problems(_fixture_repo(tmp_path, {"tests/test_a.py": body}))
    assert len(problems) == 1 and "census site" in problems[0] and "3 data-dir use" in problems[0], problems


# ── provision(): the nested-pgdata hazard ─────────────────────────────────────


def test_provision_on_a_non_helper_dir_must_be_listed(tmp_path: Path) -> None:
    """The five tmp_path provisioning files were caught only because each happened to name ``exe_name("initdb")``;
    a new ``provision(tmp_path)`` test must be caught on its own account."""
    body = "def test_x(tmp_path):\n    pp.provision(tmp_path / 'cfg')\n    provision(config_dir=tmp_path)\n"
    problems = sites.provenance_problems(_fixture_repo(tmp_path / "bad", {"tests/test_p.py": body}))
    assert len(problems) == 2 and all("provision(" in p and "test_p.py" in p for p in problems), problems
    listed = f'[not_a_cluster]\n[disk_required]\n"tests/test_p.py" = "{_WHY}"\n'
    assert sites.provenance_problems(_fixture_repo(tmp_path / "ok", {"tests/test_p.py": body}, listed)) == []


def test_a_method_named_provision_with_no_dir_is_not_the_product_call(tmp_path: Path) -> None:
    body = "def test_x(box):\n    box.provision()\n"
    assert sites.provenance_problems(_fixture_repo(tmp_path, {"tests/test_p.py": body})) == []


@pytest.mark.parametrize(
    "setup",
    [
        "provision(pg_data_tempdir('x_'))",
        "provision(Path(pg_data_tempdir('x_')) / 'cfg')",
        "d = pg_data_tempdir('x_')\n    provision(d, force_new_port=True)",
        "provision(config_dir=str(pg_data_tempdir('x_')))",
    ],
)
def test_provision_on_a_helper_dir_is_a_problem_even_when_listed(tmp_path: Path, setup: str) -> None:
    """provision() puts the cluster at <dir>/postgres; the sweep reads postmaster.pid at the top of a recorded dir
    only, so it would rmtree <dir> under a live orphan postmaster. Listing does not excuse it (nexus-0ajjm)."""
    body = f"def test_x():\n    {setup}\n"
    listed = f'[not_a_cluster]\n[disk_required]\n"tests/test_p.py" = "{_WHY}"\n'
    for toml in ("", listed):
        problems = sites.provenance_problems(_fixture_repo(tmp_path / f"t{len(toml)}", {"tests/test_p.py": body}, toml))
        assert any("nexus-0ajjm" in p and "nests the cluster" in p for p in problems), problems


# ── The exceptions table ──────────────────────────────────────────────────────


def test_a_listed_file_is_excused_and_a_stale_or_unreasoned_listing_is_not(tmp_path: Path) -> None:
    bad = "_INITDB = exe_name('initdb')\ndef fx(tmp_path):\n    subprocess.run([str(_INITDB), '-D', str(tmp_path)])\n"
    listed = f'[not_a_cluster]\n[disk_required]\n"tests/test_a.py" = "{_WHY}"\n'
    assert sites.provenance_problems(_fixture_repo(tmp_path / "ok", {"tests/test_a.py": bad}, listed)) == []
    gone = sites.provenance_problems(_fixture_repo(tmp_path / "gone", {"tests/test_b.py": "x = 1\n"}, listed))
    assert any("tests/test_a.py no longer exists" in p for p in gone), gone
    blank = '[not_a_cluster]\n[disk_required]\n"tests/test_a.py" = "ok"\n'
    assert any("no real reason" in p for p in sites.provenance_problems(
        _fixture_repo(tmp_path / "blank", {"tests/test_a.py": bad}, blank)))
    filler = '[not_a_cluster]\n[disk_required]\n"tests/test_a.py" = "x_x_x_x_x_x_x_x_x_x_x_x"\n'
    assert any("no real reason" in p for p in sites.provenance_problems(
        _fixture_repo(tmp_path / "filler", {"tests/test_a.py": bad}, filler)))
    nothing = sites.provenance_problems(_fixture_repo(tmp_path / "nothing", {"tests/test_a.py": "x = 1\n"}, listed))
    assert any("left to except" in p for p in nothing), nothing


def test_a_disk_required_reason_names_the_bead_that_drains_it(tmp_path: Path) -> None:
    body = "def test_x(tmp_path):\n    provision(tmp_path)\n"
    no_bead = '[not_a_cluster]\n[disk_required]\n"tests/test_p.py" = "provision nests the pgdata so the sweep cannot reap it"\n'
    problems = sites.provenance_problems(_fixture_repo(tmp_path / "no", {"tests/test_p.py": body}, no_bead))
    assert any("names no bead" in p for p in problems), problems
    with_bead = no_bead.replace("reap it", "reap it (nexus-0ajjm)")
    assert sites.provenance_problems(_fixture_repo(tmp_path / "yes", {"tests/test_p.py": body}, with_bead)) == []
    # not_a_cluster needs no bead: nothing drains it, it is simply not a cluster.
    nc = '[not_a_cluster]\n"tests/test_p.py" = "provision is stopped by a faked guard before any cluster starts"\n[disk_required]\n'
    assert sites.provenance_problems(_fixture_repo(tmp_path / "nc", {"tests/test_p.py": body}, nc)) == []


def test_a_word_in_a_docstring_or_comment_keeps_no_listing_alive(tmp_path: Path) -> None:
    """The staleness check reads the AST: ``provision(`` inside a docstring, a comment or a string is not a call."""
    body = (
        '"""Drives provision() against a bundle; _init_cluster() builds it."""\n'
        "# provision(tmp_path) is described here, never called\n"
        "NOTE = 'provision(tmp_path)'\n"
        "def test_x():\n    assert True\n"
    )
    listed = f'[not_a_cluster]\n[disk_required]\n"tests/test_p.py" = "{_WHY}"\n'
    problems = sites.provenance_problems(_fixture_repo(tmp_path, {"tests/test_p.py": body}, listed))
    assert any("left to except" in p for p in problems), problems


def test_the_exceptions_tables_may_only_shrink(tmp_path: Path) -> None:
    body = "def test_x(tmp_path):\n    provision(tmp_path)\n"
    toml = f'[not_a_cluster]\n[disk_required]\n"tests/test_p.py" = "{_WHY}"\n'
    repo = _fixture_repo(tmp_path, {"tests/test_p.py": body}, toml)
    assert sites.provenance_problems(repo, ceilings=(0, 1)) == []
    problems = sites.provenance_problems(repo, ceilings=(0, 0))
    assert len(problems) == 1 and "exceed the ceiling 0" in problems[0], problems
    # The committed tables sit at or under the committed ceilings (an upper bound only: draining a table to
    # empty is nexus-0ajjm's job and needs no edit here, though it should lower DISK_REQUIRED_CEILING).
    not_cluster, disk = sites.exceptions()
    assert len(not_cluster) <= sites.NOT_A_CLUSTER_CEILING and len(disk) <= sites.DISK_REQUIRED_CEILING


def test_a_census_file_with_no_traceable_data_dir_must_be_listed(tmp_path: Path) -> None:
    """A provisioning test that only looks initdb up (``exe_name``) and runs the product's ``provision()`` never
    names a data dir: without a listed reason it would pass the scan by being invisible to it."""
    body = "_INITDB = bin_dir / exe_name('initdb')\ndef test_x(tmp_path):\n    provision(tmp_path)\n"
    problems = sites.provenance_problems(_fixture_repo(tmp_path, {"tests/test_p.py": body}))
    assert any("census site" in p for p in problems) and any("provision(" in p for p in problems), problems


# ── Docker-run Postgres ───────────────────────────────────────────────────────


def test_every_docker_run_postgres_mounts_its_data_dir_as_tmpfs() -> None:
    """``docker run pgvector/pgvector:pg17`` without ``--tmpfs`` writes the cluster to the docker data-root of
    whatever host runs the test; the Java suites mount PGDATA as tmpfs (PgContainerHelper.PGDATA). Needs both
    python modules that start one to be seen (test_the_provenance_scan_is_not_vacuous)."""
    assert sites.docker_pg_problems() == []


def test_a_docker_run_postgres_without_tmpfs_is_flagged(tmp_path: Path) -> None:
    run = "subprocess.run(['docker', 'run', '-d', '--name', 'n', {extra}'pgvector/pgvector:pg17'])\n"
    bad = _fixture_repo(tmp_path / "bad", {"tests/test_d.py": run.format(extra="")})
    assert len(sites.docker_pg_problems(bad)) == 1
    wrong_path = _fixture_repo(tmp_path / "wrong", {"tests/test_d.py": run.format(extra="'--tmpfs', '/tmp', ")})
    assert len(sites.docker_pg_problems(wrong_path)) == 1
    good = _fixture_repo(
        tmp_path / "good", {"tests/test_d.py": run.format(extra="'--tmpfs', '/var/lib/postgresql/data:rw,size=2g', ")}
    )
    assert sites.docker_pg_problems(good) == []
    other = _fixture_repo(tmp_path / "other", {"tests/test_d.py": "subprocess.run(['docker', 'run', 'alpine'])\n"})
    assert sites.docker_pg_runs(other) == []


# ── The converted fixtures' teardown ──────────────────────────────────────────


def test_the_converted_fixtures_tear_down_through_the_shared_helper() -> None:
    """Both modules take their dir from pg_data_tempdir, which is not pytest-managed, so each must remove it itself;
    ``stop_and_remove_cluster`` does that in a ``finally`` (tests/test_integration_pg_cluster.py). A module-local
    teardown that stops, then removes, lets a pg_ctl timeout skip the removal."""
    import ast

    for rel in ("tests/db/test_nexus_diag_role.py", "tests/db/test_svc_monitor_role_live.py"):
        tree = ast.parse((sites.REPO / rel).read_text(encoding="utf-8"))
        calls = {n.func.id for n in ast.walk(tree) if isinstance(n, ast.Call) and isinstance(n.func, ast.Name)}
        attrs = {n.func.attr for n in ast.walk(tree) if isinstance(n, ast.Call) and isinstance(n.func, ast.Attribute)}
        assert "stop_and_remove_cluster" in calls | attrs, rel
        assert "rmtree" not in attrs, f"{rel}: a module-local rmtree is the teardown the helper replaced"
