# SPDX-License-Identifier: AGPL-3.0-or-later
import hashlib
import os
import subprocess
import threading
from pathlib import Path

import pytest

from nexus.registry import RepoRegistry


@pytest.fixture
def reg(tmp_path: Path) -> RepoRegistry:
    return RepoRegistry(tmp_path / "repos.json")


@pytest.fixture
def repo(tmp_path: Path) -> Path:
    r = tmp_path / "myrepo"
    r.mkdir()
    return r


def _expected_hash(repo: Path) -> str:
    return hashlib.sha256(str(repo).encode()).hexdigest()[:8]


# ── add / get / remove ──────────────────────────────────────────────────────


def test_add_and_get(reg, repo) -> None:
    reg.add(repo)
    assert str(repo) in reg.all()
    info = reg.get(repo)
    assert info is not None
    assert info["collection"] == f"code__myrepo-{_expected_hash(repo)}"
    assert info["name"] == "myrepo"
    assert info["head_hash"] == ""


def test_persists_to_json(reg, repo) -> None:
    import json
    reg.add(repo)
    data = json.loads(reg._path.read_text())
    assert str(repo) in data["repos"]


def test_survives_reload(tmp_path, repo) -> None:
    path = tmp_path / "repos.json"
    RepoRegistry(path).add(repo)
    assert RepoRegistry(path).get(repo) is not None


def test_remove(reg, repo) -> None:
    reg.add(repo)
    reg.remove(repo)
    assert reg.get(repo) is None


def test_get_missing(reg) -> None:
    assert reg.get(Path("/no/such/repo")) is None


# ── update ──────────────────────────────────────────────────────────────────


@pytest.mark.parametrize("field,value", [
    ("head_hash", "abc123def456"),
    ("status", "indexing"),
])
def test_update(reg, repo, field, value) -> None:
    reg.add(repo)
    reg.update(repo, **{field: value})
    assert reg.get(repo)[field] == value


# ── atomic write / concurrent access ────────────────────────────────────────


def test_atomic_write_leaves_no_tmp(reg, repo) -> None:
    reg.add(repo)
    assert not reg._path.with_suffix(".json.tmp").exists()


def test_concurrent_reads_and_writes(tmp_path) -> None:
    reg = RepoRegistry(tmp_path / "repos.json")
    errors: list[Exception] = []

    def writer(n: int) -> None:
        try:
            r = tmp_path / f"repo{n}"
            r.mkdir(exist_ok=True)
            reg.add(r)
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
    assert len(reg.all()) == 10


# ── get() returns copy ─────────────────────────────────────────────────────


def test_get_returns_copy(reg, repo) -> None:
    reg.add(repo)
    entry = reg.get(repo)
    entry["collection"] = "code__MUTATED"
    entry["injected"] = True
    fresh = reg.get(repo)
    assert fresh["collection"] == f"code__myrepo-{_expected_hash(repo)}"
    assert "injected" not in fresh


# ── dual-collection names ──────────────────────────────────────────────────


def test_dual_collection_names(reg, repo) -> None:
    reg.add(repo)
    info = reg.get(repo)
    assert info["code_collection"].startswith("code__")
    assert info["docs_collection"].startswith("docs__")
    code_suffix = info["code_collection"].split("code__")[1]
    docs_suffix = info["docs_collection"].split("docs__")[1]
    assert code_suffix == docs_suffix
    assert info["collection"] == info["code_collection"]


def test_docs_collection_name_function() -> None:
    from nexus.registry import _docs_collection_name
    name = _docs_collection_name(Path("/some/path/myrepo"))
    assert name.startswith("docs__myrepo-")
    assert len(name.split("-")[-1]) == 8


# ── worktree-stable hashing ────────────────────────────────────────────────


def _git_env():
    return {**os.environ, "GIT_AUTHOR_NAME": "test", "GIT_AUTHOR_EMAIL": "t@t",
            "GIT_COMMITTER_NAME": "test", "GIT_COMMITTER_EMAIL": "t@t"}


def _init_git_repo(path: Path) -> None:
    path.mkdir(exist_ok=True)
    subprocess.run(["git", "init"], cwd=path, capture_output=True, check=True)
    (path / "f.txt").write_text("hello")
    subprocess.run(["git", "add", "."], cwd=path, capture_output=True, check=True)
    subprocess.run(["git", "commit", "-m", "init"], cwd=path, capture_output=True, check=True, env=_git_env())


def test_repo_identity_fallback_without_git(tmp_path) -> None:
    from nexus.registry import _repo_identity
    r = tmp_path / "not-a-git-repo"
    r.mkdir()
    name, hash8 = _repo_identity(r)
    assert name == "not-a-git-repo"
    assert hash8 == _expected_hash(r)


def test_repo_identity_stable_in_git_repo(tmp_path) -> None:
    from nexus.registry import _repo_identity
    r = tmp_path / "myrepo"
    _init_git_repo(r)
    name, hash8 = _repo_identity(r)
    assert name == "myrepo"
    assert hash8 == hashlib.sha256(str(r).encode()).hexdigest()[:8]


def test_worktree_matches_main(tmp_path) -> None:
    from nexus.registry import _repo_identity
    main_repo = tmp_path / "main-repo"
    _init_git_repo(main_repo)
    wt = tmp_path / "my-worktree"
    subprocess.run(["git", "worktree", "add", str(wt), "-b", "wt-branch"],
                   cwd=main_repo, capture_output=True, check=True)
    main_name, main_hash = _repo_identity(main_repo)
    wt_name, wt_hash = _repo_identity(wt)
    assert main_name == wt_name
    assert main_hash == wt_hash


def test_collection_names_stable_across_worktrees(tmp_path) -> None:
    from nexus.registry import _collection_name, _docs_collection_name
    main_repo = tmp_path / "repo"
    _init_git_repo(main_repo)
    wt = tmp_path / "wt"
    subprocess.run(["git", "worktree", "add", str(wt), "-b", "wt-branch"],
                   cwd=main_repo, capture_output=True, check=True)
    assert _collection_name(main_repo) == _collection_name(wt)
    assert _docs_collection_name(main_repo) == _docs_collection_name(wt)


# ── _rdr_collection_name ───────────────────────────────────────────────────


def test_rdr_collection_name() -> None:
    from nexus.registry import _rdr_collection_name
    name = _rdr_collection_name(Path("/some/path/myrepo"))
    assert name.startswith("rdr__myrepo-")
    assert len(name.split("-")[-1]) == 8


def test_all_collection_names_same_hash() -> None:
    from nexus.registry import _collection_name, _docs_collection_name, _rdr_collection_name
    repo = Path("/some/path/myrepo")
    suffixes = [fn(repo).split("-")[-1] for fn in (_collection_name, _docs_collection_name, _rdr_collection_name)]
    assert len(set(suffixes)) == 1


# ── long basename truncation ──────────────────────────────────────────────


@pytest.mark.parametrize("fn_name", ["_collection_name", "_docs_collection_name", "_rdr_collection_name"])
def test_truncates_long_basename(fn_name) -> None:
    import nexus.registry as reg_mod
    fn = getattr(reg_mod, fn_name)
    col_name = fn(Path(f"/tmp/{'a' * 60}"))
    assert len(col_name) <= 63
    prefix = fn_name.split("_")[1]  # "collection" or "docs" or "rdr"
    if prefix == "collection":
        prefix = "code"
    assert col_name.startswith(f"{prefix}__")


def test_truncated_names_valid() -> None:
    from nexus.corpus import validate_collection_name
    from nexus.registry import _collection_name, _docs_collection_name, _rdr_collection_name
    repo = Path(f"/tmp/{'a' * 60}")
    for fn in (_collection_name, _docs_collection_name, _rdr_collection_name):
        validate_collection_name(fn(repo))


def test_short_basename_not_truncated() -> None:
    from nexus.registry import _collection_name
    assert "myrepo" in _collection_name(Path("/tmp/myrepo"))


def test_truncated_names_still_unique() -> None:
    from nexus.registry import _collection_name
    assert _collection_name(Path("/tmp/" + "a" * 60)) != _collection_name(Path("/other/" + "a" * 60))
