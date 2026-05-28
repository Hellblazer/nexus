# SPDX-License-Identifier: AGPL-3.0-or-later
# Copyright (c) 2026 Hal Hildebrand. All rights reserved.
"""Worktree-first-run repo_root contamination (RDR-137 gate-critique finding).

Pre-fix bug: ``ensure_owner_for_repo`` (catalog.py:1022) and ``_catalog_hook``
(indexer.py:640) both wrote ``repo_root=str(repo)`` — the raw input path
— instead of the canonical main-repo path that ``_repo_identity`` computes
internally. When ``nx index repo`` ran first from a worktree path
(e.g. ``.claude/worktrees/X``), the stored ``repo_root`` was the
transient worktree directory; after worktree deletion, ``resolve_path``
produced broken paths for all relative-path documents.

Live evidence in the wild: catalog owner ``1.22`` (qmrr-zm2n) has
``repo_root=/Users/.../.claude/worktrees/qmrr-zm2n``, the worktree
path — even though ``_repo_identity`` would now resolve both that
worktree and the nexus main repo to the same ``repo_hash`` (owner ``1.1``).

The fix exposes a 3-tuple ``_repo_identity_with_main(repo)`` returning
``(name, hash8, main_repo_path)``. Writers thread ``main_repo_path``
through and write ``repo_root=str(main_repo_path)`` instead of the
raw input. ``_repo_identity`` (2-tuple) is preserved unchanged so the
~15 existing call sites and test mocks keep working.
"""
from __future__ import annotations

from pathlib import Path

import pytest

from nexus.catalog import Catalog


@pytest.fixture()
def cat(tmp_path: Path) -> Catalog:
    """Fresh catalog rooted at tmp_path.

    Uses ``Catalog.init`` (not just the constructor) because
    ``_catalog_hook`` gates on ``Catalog.is_initialized`` which requires
    both ``.git/`` and ``documents.jsonl`` to exist.
    """
    catalog_dir = tmp_path / "catalog"
    catalog_dir.mkdir()
    return Catalog.init(catalog_dir)


def test_repo_identity_with_main_returns_three_tuple_with_main_repo_path(
    tmp_path: Path,
) -> None:
    """Public contract: new 3-tuple variant returns the main-repo Path."""
    from nexus.registry import _repo_identity_with_main

    # Non-git path falls back to the input as main_repo.
    plain = tmp_path / "no_git_here"
    plain.mkdir()
    name, hash8, main_repo = _repo_identity_with_main(plain)
    assert name == "no_git_here"
    assert len(hash8) == 8
    assert main_repo == plain


def test_repo_identity_two_tuple_unchanged(tmp_path: Path) -> None:
    """Backward compat: the 2-tuple variant keeps its signature so the
    ~15 production call sites and the existing test monkeypatches don't
    break.
    """
    from nexus.registry import _repo_identity

    plain = tmp_path / "no_git_here"
    plain.mkdir()
    result = _repo_identity(plain)
    assert len(result) == 2
    assert result[0] == "no_git_here"


def test_ensure_owner_for_repo_writes_main_repo_root_not_input_path(
    cat: Catalog, tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The headline regression. When the catalog hasn't seen this repo
    before AND the input path is a worktree, the registered owner's
    ``repo_root`` must be the main-repo path, NOT the worktree path.
    """
    main_repo = tmp_path / "main_nexus"
    main_repo.mkdir()
    worktree = tmp_path / "main_nexus" / ".claude" / "worktrees" / "qmrr"
    worktree.mkdir(parents=True)

    # Mock _repo_identity_with_main to return main_repo as the third
    # element regardless of which path is passed in (simulates the
    # _git rev-parse --git-common-dir_ resolution that both worktree
    # and main produce the same main repo).
    monkeypatch.setattr(
        "nexus.registry._repo_identity_with_main",
        lambda r: ("main_nexus", "abcd1234", main_repo),
    )

    # First-run from the WORKTREE path.
    owner = cat.ensure_owner_for_repo(worktree)
    assert owner is not None

    # owner_for_repo returns a Tumbler (just the id); _owner_repo_root
    # returns the persisted ``repo_root`` string.
    owner_tumbler = cat.owner_for_repo("abcd1234")
    assert owner_tumbler is not None
    repo_root = cat._owner_repo_root(owner_tumbler)
    assert repo_root == str(main_repo), (
        f"repo_root contaminated by input path: got {repo_root!r}, "
        f"expected main repo {str(main_repo)!r}"
    )
    # Specifically: must NOT be the worktree path.
    assert repo_root != str(worktree)


def test_catalog_hook_passes_main_repo_path_to_register_owner(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Parallel fix for _catalog_hook (indexer.py:~640). When the hook is
    the first registrar for a repo and the input path is a worktree,
    ``register_owner`` must receive the canonical main-repo path as
    ``repo_root``, NOT the worktree input path.

    Verified by spying on ``Catalog.register_owner``: we capture the
    ``repo_root`` kwarg the hook passes and assert it equals the
    mocked ``main_repo``, never the worktree input. This sidesteps
    the catalog projection / DB-init complexity and pins the exact
    contract that nexus-zr2ie introduced.
    """
    from nexus.indexer import _catalog_hook

    main_repo = tmp_path / "main_nexus"
    main_repo.mkdir()
    worktree = tmp_path / "main_nexus" / ".claude" / "worktrees" / "qmrr"
    worktree.mkdir(parents=True)

    # Initialize a real catalog so _catalog_hook's is_initialized gate passes.
    catalog_dir = tmp_path / "catalog"
    catalog_dir.mkdir()
    Catalog.init(catalog_dir)
    monkeypatch.setattr("nexus.config.catalog_path", lambda: catalog_dir)

    # Force the hook's owner-lookup to return None so the registration
    # branch fires.
    monkeypatch.setattr(
        "nexus.catalog.catalog.Catalog.owner_for_repo",
        lambda self, repo_hash: None,
    )

    # Mock _repo_identity_with_main to return main_repo as the third
    # element. The hook must use this value, not str(worktree).
    monkeypatch.setattr(
        "nexus.registry._repo_identity_with_main",
        lambda r: ("main_nexus", "abcd1234", main_repo),
    )

    # Spy on register_owner to capture the repo_root kwarg.
    captured: dict[str, object] = {}

    def _spy(self, **kwargs):  # noqa: ANN001
        captured.update(kwargs)
        # Return a fake Tumbler-like object that has __str__.
        class _FakeTumbler:
            def __str__(self_inner) -> str:  # noqa: N805
                return "1.1"
        return _FakeTumbler()

    monkeypatch.setattr("nexus.catalog.catalog.Catalog.register_owner", _spy)

    _catalog_hook(
        repo=worktree,
        repo_name="main_nexus",
        repo_hash="abcd1234",
        head_hash="abc",
        indexed_files=[],
    )

    assert "repo_root" in captured, "register_owner was not called"
    assert captured["repo_root"] == str(main_repo), (
        f"_catalog_hook contaminated repo_root: got {captured['repo_root']!r}, "
        f"expected {str(main_repo)!r}"
    )
    assert captured["repo_root"] != str(worktree)
