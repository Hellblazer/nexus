# SPDX-License-Identifier: AGPL-3.0-or-later
"""nexus-sz651: every Postgres cluster start site in the test tree is either read by the DDL/bytes
counter or listed in ``tests/ddl_uncounted_clusters.toml``.

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
