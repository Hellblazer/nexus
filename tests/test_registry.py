"""AC2: Repo registry add/remove/update, JSON persistence, atomic write."""
import hashlib
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
    expected_hash = hashlib.sha256(str(repo).encode()).hexdigest()[:8]
    assert info["collection"] == f"code__myrepo-{expected_hash}"
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


def test_registry_concurrent_reads_and_writes(tmp_path: Path) -> None:
    """T5: Concurrent add() and get() calls do not raise or corrupt data."""
    import threading

    reg = RepoRegistry(tmp_path / "repos.json")
    errors: list[Exception] = []

    def writer(n: int) -> None:
        try:
            repo = tmp_path / f"repo{n}"
            repo.mkdir(exist_ok=True)
            reg.add(repo)
        except Exception as e:
            errors.append(e)

    def reader() -> None:
        try:
            for _ in range(50):
                reg.all()
                reg.get(tmp_path / "repo0")
        except Exception as e:
            errors.append(e)

    threads = [threading.Thread(target=writer, args=(i,)) for i in range(10)]
    threads += [threading.Thread(target=reader) for _ in range(5)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()

    assert errors == [], f"Concurrent access raised: {errors}"
    # All writers completed — 10 repos should be registered
    assert len(reg.all()) == 10


# ── nexus-bgv: get() returns a copy, not a mutable reference ─────────────────

def test_registry_get_returns_copy_not_reference(tmp_path: Path) -> None:
    """registry.get() returns a copy; mutating it does not corrupt the registry."""
    reg = RepoRegistry(tmp_path / "repos.json")
    repo = tmp_path / "myrepo"
    repo.mkdir()
    reg.add(repo)

    entry = reg.get(repo)
    assert entry is not None

    # Mutate the returned dict
    entry["collection"] = "code__MUTATED"
    entry["injected"] = True

    # Internal state must be unchanged
    fresh = reg.get(repo)
    expected_hash = hashlib.sha256(str(repo).encode()).hexdigest()[:8]
    assert fresh["collection"] == f"code__myrepo-{expected_hash}"
    assert "injected" not in fresh


# ── dual-collection names ─────────────────────────────────────────────────────


def test_add_stores_both_collection_names(tmp_path: Path) -> None:
    reg = RepoRegistry(tmp_path / "repos.json")
    repo = tmp_path / "myrepo"
    repo.mkdir()
    reg.add(repo)
    info = reg.get(repo)
    assert "code_collection" in info
    assert "docs_collection" in info
    assert info["code_collection"].startswith("code__")
    assert info["docs_collection"].startswith("docs__")
    # Same hash suffix
    code_suffix = info["code_collection"].split("code__")[1]
    docs_suffix = info["docs_collection"].split("docs__")[1]
    assert code_suffix == docs_suffix


def test_backward_compat_collection_key(tmp_path: Path) -> None:
    """The 'collection' key still works as alias for code_collection."""
    reg = RepoRegistry(tmp_path / "repos.json")
    repo = tmp_path / "myrepo"
    repo.mkdir()
    reg.add(repo)
    info = reg.get(repo)
    assert info["collection"] == info["code_collection"]


def test_docs_collection_name_function() -> None:
    from nexus.registry import _docs_collection_name

    repo = Path("/some/path/myrepo")
    name = _docs_collection_name(repo)
    assert name.startswith("docs__myrepo-")
    assert len(name.split("-")[-1]) == 8  # 8-char hash


# ── worktree-stable hashing ──────────────────────────────────────────────────


def test_repo_identity_fallback_without_git(tmp_path: Path) -> None:
    """_repo_identity falls back to repo path when not a git repo."""
    from nexus.registry import _repo_identity

    repo = tmp_path / "not-a-git-repo"
    repo.mkdir()
    name, hash8 = _repo_identity(repo)
    assert name == "not-a-git-repo"
    expected = hashlib.sha256(str(repo).encode()).hexdigest()[:8]
    assert hash8 == expected


def test_repo_identity_stable_in_git_repo(tmp_path: Path) -> None:
    """_repo_identity uses the git common dir for a real git repo."""
    import subprocess

    repo = tmp_path / "myrepo"
    repo.mkdir()
    subprocess.run(["git", "init"], cwd=repo, capture_output=True, check=True)

    from nexus.registry import _repo_identity

    name, hash8 = _repo_identity(repo)
    assert name == "myrepo"
    expected = hashlib.sha256(str(repo).encode()).hexdigest()[:8]
    assert hash8 == expected


def test_repo_identity_worktree_matches_main(tmp_path: Path) -> None:
    """A worktree produces the same identity as the main repo."""
    import subprocess

    # Create a main repo with an initial commit (required for worktree)
    main_repo = tmp_path / "main-repo"
    main_repo.mkdir()
    subprocess.run(["git", "init"], cwd=main_repo, capture_output=True, check=True)
    (main_repo / "f.txt").write_text("hello")
    subprocess.run(["git", "add", "."], cwd=main_repo, capture_output=True, check=True)
    subprocess.run(
        ["git", "commit", "-m", "init"],
        cwd=main_repo, capture_output=True, check=True,
        env={**__import__("os").environ, "GIT_AUTHOR_NAME": "test", "GIT_AUTHOR_EMAIL": "t@t",
             "GIT_COMMITTER_NAME": "test", "GIT_COMMITTER_EMAIL": "t@t"},
    )

    # Create a worktree
    wt = tmp_path / "my-worktree"
    subprocess.run(
        ["git", "worktree", "add", str(wt), "-b", "wt-branch"],
        cwd=main_repo, capture_output=True, check=True,
    )

    from nexus.registry import _repo_identity

    main_name, main_hash = _repo_identity(main_repo)
    wt_name, wt_hash = _repo_identity(wt)

    assert main_name == wt_name, f"names differ: {main_name} vs {wt_name}"
    assert main_hash == wt_hash, f"hashes differ: {main_hash} vs {wt_hash}"


def test_collection_names_stable_across_worktrees(tmp_path: Path) -> None:
    """_collection_name and _docs_collection_name are identical for main and worktree."""
    import subprocess

    from nexus.registry import _collection_name, _docs_collection_name

    main_repo = tmp_path / "repo"
    main_repo.mkdir()
    subprocess.run(["git", "init"], cwd=main_repo, capture_output=True, check=True)
    (main_repo / "f.txt").write_text("hello")
    subprocess.run(["git", "add", "."], cwd=main_repo, capture_output=True, check=True)
    subprocess.run(
        ["git", "commit", "-m", "init"],
        cwd=main_repo, capture_output=True, check=True,
        env={**__import__("os").environ, "GIT_AUTHOR_NAME": "test", "GIT_AUTHOR_EMAIL": "t@t",
             "GIT_COMMITTER_NAME": "test", "GIT_COMMITTER_EMAIL": "t@t"},
    )

    wt = tmp_path / "wt"
    subprocess.run(
        ["git", "worktree", "add", str(wt), "-b", "wt-branch"],
        cwd=main_repo, capture_output=True, check=True,
    )

    assert _collection_name(main_repo) == _collection_name(wt)
    assert _docs_collection_name(main_repo) == _docs_collection_name(wt)
