"""AC2: Repo registry add/remove/update, JSON persistence, atomic write."""
import json
from pathlib import Path

import pytest

from nexus.registry import RepoRegistry


def test_registry_add_repo(tmp_path: Path) -> None:
    reg = RepoRegistry(tmp_path / "repos.json")
    repo = tmp_path / "myrepo"
    repo.mkdir()

    reg.add(repo)

    assert str(repo) in reg.all()


def test_registry_add_sets_collection_name(tmp_path: Path) -> None:
    reg = RepoRegistry(tmp_path / "repos.json")
    repo = tmp_path / "myrepo"
    repo.mkdir()

    reg.add(repo)
    info = reg.get(repo)

    assert info is not None
    assert info["collection"] == "code__myrepo"
    assert info["name"] == "myrepo"


def test_registry_add_initialises_head_hash_empty(tmp_path: Path) -> None:
    reg = RepoRegistry(tmp_path / "repos.json")
    repo = tmp_path / "myrepo"
    repo.mkdir()

    reg.add(repo)

    assert reg.get(repo)["head_hash"] == ""


def test_registry_persists_to_json(tmp_path: Path) -> None:
    reg_path = tmp_path / "repos.json"
    reg = RepoRegistry(reg_path)
    repo = tmp_path / "myrepo"
    repo.mkdir()

    reg.add(repo)

    data = json.loads(reg_path.read_text())
    assert str(repo) in data["repos"]


def test_registry_survives_reload(tmp_path: Path) -> None:
    reg_path = tmp_path / "repos.json"
    repo = tmp_path / "myrepo"
    repo.mkdir()

    RepoRegistry(reg_path).add(repo)

    # Second instance reads from disk
    reg2 = RepoRegistry(reg_path)
    assert reg2.get(repo) is not None


def test_registry_remove_repo(tmp_path: Path) -> None:
    reg = RepoRegistry(tmp_path / "repos.json")
    repo = tmp_path / "myrepo"
    repo.mkdir()

    reg.add(repo)
    reg.remove(repo)

    assert reg.get(repo) is None


def test_registry_get_missing_returns_none(tmp_path: Path) -> None:
    reg = RepoRegistry(tmp_path / "repos.json")
    assert reg.get(Path("/no/such/repo")) is None


def test_registry_update_head_hash(tmp_path: Path) -> None:
    reg = RepoRegistry(tmp_path / "repos.json")
    repo = tmp_path / "myrepo"
    repo.mkdir()

    reg.add(repo)
    reg.update(repo, head_hash="abc123def456")

    assert reg.get(repo)["head_hash"] == "abc123def456"


def test_registry_update_status(tmp_path: Path) -> None:
    reg = RepoRegistry(tmp_path / "repos.json")
    repo = tmp_path / "myrepo"
    repo.mkdir()

    reg.add(repo)
    reg.update(repo, status="indexing")

    assert reg.get(repo)["status"] == "indexing"


def test_registry_atomic_write_leaves_no_tmp(tmp_path: Path) -> None:
    """Save writes to .tmp then os.replace(); no stale .tmp remains."""
    reg = RepoRegistry(tmp_path / "repos.json")
    repo = tmp_path / "myrepo"
    repo.mkdir()

    reg.add(repo)

    assert not (tmp_path / "repos.json.tmp").exists()
