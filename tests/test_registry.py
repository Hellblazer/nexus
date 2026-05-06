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


def _expected_code_collection(repo: Path) -> str:
    """RDR-103 Phase 5: ``RepoRegistry.add`` synthesises a conformant
    4-segment name from the path-derived ``<basename>-<hash8>`` identity
    when no catalog is supplied. Helper centralises the expected shape
    so test assertions stay readable."""
    return f"code__{repo.name}-{_expected_hash(repo)}__voyage-code-3__v1"


def _expected_docs_collection(repo: Path) -> str:
    return f"docs__{repo.name}-{_expected_hash(repo)}__voyage-context-3__v1"


def _expected_rdr_collection(repo: Path) -> str:
    return f"rdr__{repo.name}-{_expected_hash(repo)}__voyage-context-3__v1"


# ── add / get / remove ──────────────────────────────────────────────────────


def test_add_and_get(reg, repo) -> None:
    reg.add(repo)
    assert str(repo) in reg.all()
    info = reg.get(repo)
    assert info is not None
    assert info["collection"] == _expected_code_collection(repo)
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
    assert fresh["collection"] == _expected_code_collection(repo)
    assert "injected" not in fresh


# ── dual-collection names ──────────────────────────────────────────────────


def test_dual_collection_names(reg, repo) -> None:
    reg.add(repo)
    info = reg.get(repo)
    assert info["code_collection"] == _expected_code_collection(repo)
    assert info["docs_collection"] == _expected_docs_collection(repo)
    assert info["collection"] == info["code_collection"]


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
    from nexus.registry import _resolve_repo_collection
    main_repo = tmp_path / "repo"
    _init_git_repo(main_repo)
    wt = tmp_path / "wt"
    subprocess.run(["git", "worktree", "add", str(wt), "-b", "wt-branch"],
                   cwd=main_repo, capture_output=True, check=True)
    assert _resolve_repo_collection(main_repo, "code") == _resolve_repo_collection(wt, "code")
    assert _resolve_repo_collection(main_repo, "docs") == _resolve_repo_collection(wt, "docs")


# ── conformant fallback synthesis (RDR-103 Phase 5) ────────────────────────


def test_resolve_repo_collection_synthesises_conformant_for_rdr() -> None:
    """No-catalog fallback synthesises a conformant 4-segment name for
    each content_type."""
    from nexus.corpus import is_conformant_collection_name
    from nexus.registry import _resolve_repo_collection
    repo = Path("/some/path/myrepo")
    for ct in ("code", "docs", "rdr"):
        assert is_conformant_collection_name(_resolve_repo_collection(repo, ct))


def test_resolve_repo_collection_owner_segment_shared_across_types() -> None:
    """All three content_types share the same ``<basename>-<hash8>``
    owner segment so a repo's collections collapse to one identity."""
    from nexus.registry import _resolve_repo_collection
    repo = Path("/some/path/myrepo")
    owners = []
    for ct in ("code", "docs", "rdr"):
        name = _resolve_repo_collection(repo, ct)
        # Owner segment is the second double-underscore-separated chunk.
        owners.append(name.split("__", 2)[1])
    assert len(set(owners)) == 1


# ── long basename truncation (via _safe_collection) ────────────────────────


@pytest.mark.parametrize("ct", ["code", "docs", "rdr"])
def test_truncates_long_basename(ct) -> None:
    """Conformant fallback truncates the basename to keep the rendered
    name within ChromaDB's 63-char limit."""
    from nexus.corpus import validate_collection_name
    from nexus.registry import _resolve_repo_collection
    name = _resolve_repo_collection(Path(f"/tmp/{'a' * 60}"), ct)
    assert len(name) <= 63
    assert name.startswith(f"{ct}__")
    validate_collection_name(name)


def test_short_basename_not_truncated() -> None:
    from nexus.registry import _resolve_repo_collection
    assert "myrepo" in _resolve_repo_collection(Path("/tmp/myrepo"), "code")


def test_truncated_names_still_unique() -> None:
    from nexus.registry import _resolve_repo_collection
    assert (
        _resolve_repo_collection(Path("/tmp/" + "a" * 60), "code")
        != _resolve_repo_collection(Path("/other/" + "a" * 60), "code")
    )


# ── GH #551: dotted basename sanitization ──────────────────────────────────


def test_sanitise_owner_segment_replaces_dots() -> None:
    """GH #551: Java reverse-domain repo names (``com.foo.bar``) must
    be sanitised so the conformant collection name passes
    validate_collection_name. Pre-fix only ``_`` was replaced."""
    from nexus.registry import _sanitise_owner_segment

    assert _sanitise_owner_segment("com.conductor.sys.monitoring") == \
        "com-conductor-sys-monitoring"


def test_sanitise_owner_segment_collapses_multi_separators() -> None:
    """Adjacent rejected chars collapse to a single hyphen so the
    output stays compact and avoids ``--`` runs that look ugly in
    operator output (e.g. ``foo..bar`` -> ``foo-bar`` not ``foo--bar``)."""
    from nexus.registry import _sanitise_owner_segment

    assert _sanitise_owner_segment("foo..bar") == "foo-bar"
    assert _sanitise_owner_segment("foo_._bar") == "foo-bar"
    assert _sanitise_owner_segment("a/b/c") == "a-b-c"


def test_sanitise_owner_segment_strips_leading_trailing_hyphens() -> None:
    """validate_collection_name requires alphanumeric start AND end.
    A basename like ``.foo-bar.`` must come back as ``foo-bar``."""
    from nexus.registry import _sanitise_owner_segment

    assert _sanitise_owner_segment(".foo-bar.") == "foo-bar"
    assert _sanitise_owner_segment("__foo__") == "foo"


def test_sanitise_owner_segment_preserves_alnum_and_hyphens() -> None:
    """Already-clean inputs pass through unchanged."""
    from nexus.registry import _sanitise_owner_segment

    assert _sanitise_owner_segment("clean-repo-123") == "clean-repo-123"
    assert _sanitise_owner_segment("FooBar123") == "FooBar123"


def test_resolve_repo_collection_dotted_basename_passes_validation() -> None:
    """GH #551 end-to-end: the conformant name produced for a dotted
    basename must pass validate_collection_name (the regex enforced by
    ChromaDB on get_or_create). Pre-fix this raised because the dot
    survived sanitisation; the registry then persisted an invalid
    name and every subsequent index attempt crashed."""
    from nexus.corpus import validate_collection_name
    from nexus.registry import _resolve_repo_collection

    repo = Path("/Users/hal/git/com.conductor.sys.monitoring")
    for ct in ("code", "docs", "rdr"):
        name = _resolve_repo_collection(repo, ct)
        # Must start with ``ct__``, must validate, must NOT contain dots.
        assert name.startswith(f"{ct}__")
        validate_collection_name(name)
        assert "." not in name
