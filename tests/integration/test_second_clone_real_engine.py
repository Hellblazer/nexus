# SPDX-License-Identifier: AGPL-3.0-or-later
"""RDR-254 254-P4-S1 (Gap 4) against the real engine: two live clones with one folder name get two owners.

``tests/commands/test_index_repo_second_clone.py`` drives ``nx index repo`` over a mock engine that imitates the
engine's name-first owner matching. This file asks the real engine, through the same ``ensure_owner_for_repo`` the
command calls: the second clone registers under its own name, neither owner's hash or root moves when either clone
registers again, and the engine's matching (``OwnerConvergenceByNameCharacterizationTest``) is unchanged."""
from __future__ import annotations

import subprocess

import pytest

from nexus.catalog.http_catalog_client import HttpCatalogClient
from nexus.repo_identity import _repo_identity_with_main

pytestmark = [pytest.mark.integration, pytest.mark.t2_tenant]


@pytest.fixture(autouse=True)
def _tenant(t2_service_env):
    """The engine-backed tenant the catalog client below talks to."""


def _clone(base, where):
    path = base / where / "proj"
    path.mkdir(parents=True)
    subprocess.run(["git", "init", "-q", str(path)], check=True)
    return path


def _owners(cat):
    return {o["repo_hash"]: (o["name"], o["repo_root"]) for o in cat.list_owners_by_type("repo")}


def test_two_live_clones_get_two_owners_on_the_real_engine(tmp_path):
    first, second = _clone(tmp_path, "work"), _clone(tmp_path, "scratch")
    _n, hash_a, root_a = _repo_identity_with_main(first)
    _n, hash_b, root_b = _repo_identity_with_main(second)
    cat = HttpCatalogClient()
    try:
        owner_a = cat.ensure_owner_for_repo(first)
        owner_b = cat.ensure_owner_for_repo(second)
        assert owner_a != owner_b
        expected = {hash_a: ("proj", str(root_a)), hash_b: (f"proj-{hash_b}", str(root_b))}
        assert _owners(cat) == expected

        for repo in (first, second, first, second):  # a run from either clone changes neither owner
            cat.ensure_owner_for_repo(repo)
            assert _owners(cat) == expected
        assert cat.owner_for_repo(hash_a) == owner_a and cat.owner_for_repo(hash_b) == owner_b
    finally:
        cat.close()


def test_a_second_clone_of_a_gone_first_takes_the_owner_as_today_on_the_real_engine(tmp_path):
    first, second = _clone(tmp_path, "work"), _clone(tmp_path, "scratch")
    _n, hash_b, root_b = _repo_identity_with_main(second)
    cat = HttpCatalogClient()
    try:
        owner_a = cat.ensure_owner_for_repo(first)
        subprocess.run(["rm", "-rf", str(first.parent)], check=True)  # the first clone's root is gone
        assert cat.ensure_owner_for_repo(second) == owner_a
        assert _owners(cat) == {hash_b: ("proj", str(root_b))}
    finally:
        cat.close()
