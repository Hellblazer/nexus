# SPDX-License-Identifier: AGPL-3.0-or-later
"""GH #1610: the stale index-run fence row names repo documents whose files are gone from the repo, and its only
repo remedy was `nx index <path> --force`, which cannot re-index a file that no longer exists.

The row now checks each named repo document's file against its owner's repo root (when that root is on this
machine). A missing file is counted on its own and pointed at `nx catalog prune-stale`, which removes catalog
entries whose file is gone; `--force` is offered only for the documents whose files are still there.
"""
from __future__ import annotations

from pathlib import Path
from typing import Any

import pytest

import nexus.health as h


def _entry(*, index_state: str | None, indexed_at: str, tumbler: str, file_path: str = "", source_uri: str = "") -> Any:
    return type("E", (), {
        "index_state": index_state, "index_started_at": "", "indexed_at": indexed_at,
        "index_state_reported": True, "source_uri": source_uri or f"file://{file_path or tumbler}",
        "tumbler": tumbler, "index_run_id": "", "file_path": file_path,
    })()


def _run(monkeypatch: pytest.MonkeyPatch, entries: list[Any], owners: list[dict], roots: dict[str, str] | Exception) -> h.HealthResult:
    class _Cat:
        def all_documents(self, limit: int = 0) -> list[Any]:
            return list(entries)

        def list_owners(self, *, include_deactivated: bool = False) -> list[dict]:
            return owners

        def owners_with_roots(self) -> dict[str, str]:
            if isinstance(roots, Exception):
                raise roots
            return roots

    monkeypatch.setattr("nexus.catalog.factory.make_catalog_reader", lambda *a, **k: _Cat(), raising=False)
    return h._check_stale_indexing_runs()[0]


_BASELINE = _entry(index_state="complete", indexed_at="2026-08-07T17:00:00+00:00", tumbler="1.22.1", file_path="src/Base.java")
_OWNERS = [{"tumbler_prefix": "1.22", "owner_type": "repo"}]


def _repo(tmp_path: Path) -> Path:
    repo = tmp_path / "tracked-topic-services"
    (repo / "src").mkdir(parents=True)
    (repo / "src" / "Kept.java").write_text("class Kept {}\n")
    return repo


def test_a_missing_file_is_named_with_prune_stale_and_not_force(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    repo = _repo(tmp_path)
    entries = [
        _BASELINE,
        _entry(index_state=None, indexed_at="2026-08-29T15:15:27+00:00", tumbler="1.22.127",
               file_path="src/persistence/ArrayLanguageDriver.java"),
        _entry(index_state=None, indexed_at="2026-10-07T10:00:00+00:00", tumbler="1.22.128",
               file_path="src/persistence/Gone.java"),
    ]
    r = _run(monkeypatch, entries, _OWNERS, {"1.22": str(repo)})
    assert r.ok is False and r.warn is True
    assert "2 of the named repo document(s) name a file that is no longer in its repo" in r.detail, r.detail
    assert "src/persistence/ArrayLanguageDriver.java" in r.detail
    assert any(f.startswith("nx catalog prune-stale --owner 1.22") for f in r.fix_suggestions), r.fix_suggestions
    assert not any("--force" in f for f in r.fix_suggestions), "no remaining repo document can use --force"


def test_present_files_keep_the_force_remedy_beside_the_prune(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    repo = _repo(tmp_path)
    entries = [
        _BASELINE,
        _entry(index_state=None, indexed_at="2026-08-29T15:15:27+00:00", tumbler="1.22.127", file_path="src/Gone.java"),
        _entry(index_state=None, indexed_at="2026-08-29T15:16:00+00:00", tumbler="1.22.130", file_path="src/Kept.java"),
    ]
    r = _run(monkeypatch, entries, _OWNERS, {"1.22": str(repo)})
    assert "1 of the named repo document(s) name a file that is no longer in its repo" in r.detail, r.detail
    assert any(f.startswith("nx catalog prune-stale --owner 1.22") for f in r.fix_suggestions)
    assert any("nx index <path> --force" in f for f in r.fix_suggestions), "the present file still gets --force"


@pytest.mark.parametrize("roots", [{}, {"1.22": "/nonexistent/clone/not/on/this/machine"}, RuntimeError("owner roots unavailable")])
def test_a_repo_not_on_this_machine_is_not_judged_missing(monkeypatch: pytest.MonkeyPatch, roots: Any) -> None:
    entries = [
        _BASELINE,
        _entry(index_state=None, indexed_at="2026-08-29T15:15:27+00:00", tumbler="1.22.127", file_path="src/Gone.java"),
    ]
    r = _run(monkeypatch, entries, _OWNERS, roots)
    assert "no longer in its repo" not in r.detail, r.detail
    assert not any("prune-stale" in f for f in r.fix_suggestions)
    assert any("nx index <path> --force" in f for f in r.fix_suggestions), "unchanged from before #1610"
