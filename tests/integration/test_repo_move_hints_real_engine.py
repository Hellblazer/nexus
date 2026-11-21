# SPDX-License-Identifier: AGPL-3.0-or-later
"""RDR-254 254-P3-S3 against the real engine: the repository hints' reads (owner by repo hash, owner row, one ``/list``
page, the owner list, the metadata route) answer what the hints need.

``tests/integration/test_index_repo_move_hints.py`` drives the CLI over a mock catalog; this file asks the same
function, ``hints.repo_hints``, of the engine the suite boots, in the renamed-repository shape that CAN be registered
(the other shape, documents naming an old root under a live owner, only arises when registration re-roots an owner,
which the engine's containment guard keeps a plain register from imitating)."""
from __future__ import annotations

import subprocess

import pytest

from nexus.catalog.engine_writes import hints
from nexus.catalog.http_catalog_client import HttpCatalogClient

pytestmark = [pytest.mark.integration, pytest.mark.t2_tenant]


@pytest.fixture(autouse=True)
def _tenant(t2_service_env):
    """The engine-backed tenant the catalog client below talks to."""


def test_a_renamed_repository_names_the_vanished_owner_on_the_real_engine(tmp_path):
    repo = tmp_path / "newname"
    repo.mkdir()
    subprocess.run(["git", "init", "-q", str(repo)], check=True)
    cat = HttpCatalogClient()
    try:
        new_owner = cat.ensure_owner_for_repo(repo)
        old_owner = cat.register_owner("oldname", "repo", repo_hash="oldhash1", repo_root="/nonexistent-old-checkout/oldname")
        for i, h in enumerate(("hash-a" * 8, "hash-b" * 8, "hash-c" * 8, "hash-d" * 8)):
            cat.register(new_owner, f"f{i}.py", content_type="code", file_path=f"src/f{i}.py", meta={"content_hash": h})
            cat.register(old_owner, f"f{i}.py", content_type="code", file_path=f"src/f{i}.py", meta={"content_hash": h})

        root = str(repo.resolve())
        assert hints.repo_hints(repo) == [hints.owner_move_line(str(old_owner), root)]
    finally:
        cat.close()


def test_a_healthy_unmoved_repository_prints_none_on_the_real_engine(tmp_path):
    repo = tmp_path / "plain"
    repo.mkdir()
    subprocess.run(["git", "init", "-q", str(repo)], check=True)
    cat = HttpCatalogClient()
    try:
        owner = cat.ensure_owner_for_repo(repo)
        cat.register(owner, "a.py", content_type="code", file_path="src/a.py", meta={"content_hash": "hash-z" * 8})
        assert hints.repo_hints(repo) == []
    finally:
        cat.close()
