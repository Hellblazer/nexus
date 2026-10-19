# SPDX-License-Identifier: AGPL-3.0-or-later
"""nexus-sz651: every Postgres cluster start site the scan finds in the test tree is either read by
the DDL/bytes counter or listed in ``tests/ddl_uncounted_clusters.toml`` (docker-run and e2e shell
clusters are not scanned).

A counter that sees one kind of cluster and says nothing about the others reports a smaller
number than the run caused. The allowlist makes the omission a committed figure, and this lint
keeps it equal to the tree in both directions (a new site, and a site that went away).
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
    never reaches the run's tmpfs root or the "clusters on tmpfs N, on disk M" summary. Every initdb argv,
    ``_init_cluster`` and ``pg_ctl_start`` data dir must be ``pg_data_tempdir(...)`` itself (not nested, no
    ``parent_dir=``), or its file must be listed with a reason in ``tests/pg_data_dir_exceptions.toml``."""
    assert sites.provenance_problems() == []


def test_the_provenance_scan_is_not_vacuous() -> None:
    uses = sites.data_dir_uses()
    helper = [u for u in uses if u.from_helper]
    assert len(helper) >= 40, f"the scan traced only {len(helper)} helper-made data dirs"
    assert {u.kind for u in helper} >= {"initdb", "pg_ctl_start", "init_cluster"}
    # The two fixtures that used to call tmp_path_factory.mktemp (nexus-szyun) are among them.
    for converted in ("tests/db/test_nexus_diag_role.py", "tests/db/test_svc_monitor_role_live.py"):
        assert any(u.file == converted and u.kind == "init_cluster" and u.from_helper for u in uses), converted
    not_cluster, disk = sites.exceptions()
    assert len(disk) >= 5 and len(not_cluster) >= 4
    assert not set(disk) & {u.file for u in helper}, "a file proven to use the helper is still listed as disk-required"


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
    assert sorted(p.split(":")[0] for p in problems) == ["tests/test_missing.py", "tests/test_mixed.py", "tests/test_param.py"]


def test_a_listed_file_is_excused_and_a_stale_or_unreasoned_listing_is_not(tmp_path: Path) -> None:
    bad = "_INITDB = exe_name('initdb')\ndef fx(tmp_path):\n    subprocess.run([str(_INITDB), '-D', str(tmp_path)])\n"
    why = "provision() nests pgdata under the dir the test supplies, which the sweep cannot reap"
    listed = f'[not_a_cluster]\n[disk_required]\n"tests/test_a.py" = "{why}"\n'
    assert sites.provenance_problems(_fixture_repo(tmp_path / "ok", {"tests/test_a.py": bad}, listed)) == []
    gone = sites.provenance_problems(_fixture_repo(tmp_path / "gone", {"tests/test_b.py": "x = 1\n"}, listed))
    assert any("tests/test_a.py no longer exists" in p for p in gone), gone
    blank = '[not_a_cluster]\n[disk_required]\n"tests/test_a.py" = "ok"\n'
    assert any("no real reason" in p for p in sites.provenance_problems(
        _fixture_repo(tmp_path / "blank", {"tests/test_a.py": bad}, blank)))
    nothing = sites.provenance_problems(_fixture_repo(tmp_path / "nothing", {"tests/test_a.py": "x = 1\n"}, listed))
    assert any("left to except" in p for p in nothing), nothing


def test_a_census_file_with_no_traceable_data_dir_must_be_listed(tmp_path: Path) -> None:
    """A provisioning test that only looks initdb up (``exe_name``) and runs the product's ``provision()`` never
    names a data dir: without a listed reason it would pass the scan by being invisible to it."""
    body = "_INITDB = bin_dir / exe_name('initdb')\ndef test_x(tmp_path):\n    provision(tmp_path)\n"
    problems = sites.provenance_problems(_fixture_repo(tmp_path, {"tests/test_p.py": body}))
    assert len(problems) == 1 and "no data-dir use the scan can trace" in problems[0], problems
