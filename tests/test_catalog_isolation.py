# SPDX-License-Identifier: AGPL-3.0-or-later
"""Regression test: integration-test leak pattern must not touch user's real catalog.

Historical context (nexus-dqr3, part of nexus-b34f): between 2026-04-05 and
2026-04-08, integration tests like ``test_cce_query_retrieves_cce_indexed_markdown``
invoked ``_catalog_markdown_hook`` via ``index_markdown``, which read
``catalog_path()`` at call time and registered curator owners (``int-cce-<uid>``)
in the user's live ``~/.config/nexus/catalog/``. By 2026-04-19 there were 64
orphan ``int-cce-*`` owners — each an alias for roughly the same fixture files.

Fix (RDR-060, commit d276719, 2026-04-08): the autouse ``_isolate_catalog``
fixture in ``tests/conftest.py`` redirects ``NEXUS_CATALOG_PATH`` per test.
Because the hooks guard on ``Catalog.is_initialized(cat_path)`` and the tmp
path is never initialised, the hook returns early and the real catalog is
never touched.

This file locks that behaviour in: it exercises the historical leak pattern
and asserts no owner is registered (and, redundantly, that the real user
catalog mtime is unchanged for the duration of the test).
"""
from __future__ import annotations

import os
import sqlite3
from pathlib import Path

import pytest

from nexus.catalog import Catalog
from nexus.config import catalog_path


def test_autouse_isolate_catalog_redirects_env(tmp_path: Path) -> None:
    """The autouse fixture must set NEXUS_CATALOG_PATH to a tmp location."""
    env_path = os.environ.get("NEXUS_CATALOG_PATH", "")
    assert env_path, (
        "NEXUS_CATALOG_PATH is unset — the autouse _isolate_catalog fixture "
        "in tests/conftest.py is missing or has been disabled. This is the "
        "guardrail that prevents tests from polluting the user's real catalog."
    )
    real_catalog = Path.home() / ".config" / "nexus" / "catalog"
    assert Path(env_path) != real_catalog, (
        "NEXUS_CATALOG_PATH resolves to the user's real catalog directory; "
        "test isolation is broken."
    )


def test_catalog_path_does_not_resolve_to_user_home() -> None:
    """catalog_path() under pytest must never return ~/.config/nexus/catalog."""
    resolved = catalog_path()
    real_catalog = Path.home() / ".config" / "nexus" / "catalog"
    assert resolved != real_catalog, (
        f"catalog_path() returned {resolved!r}, which is the user's real "
        "catalog. The autouse _isolate_catalog fixture must redirect "
        "NEXUS_CATALOG_PATH before any catalog-writing code runs."
    )


def test_hook_skips_uninitialized_tmp_catalog(tmp_path: Path) -> None:
    """_catalog_markdown_hook must return early when the tmp catalog path is
    not a real catalog (no ``.git``, ``documents.jsonl``). This is the core
    guard that makes the autouse fixture effective."""
    from nexus.doc_indexer import _catalog_markdown_hook

    # NEXUS_CATALOG_PATH is set by the autouse fixture to a tmp dir that is
    # *not* initialised as a catalog. The hook should detect this and do
    # nothing.
    cat_path = catalog_path()
    assert not Catalog.is_initialized(cat_path), (
        "Test tmp catalog path is unexpectedly initialized; cannot assert "
        "the hook's guard works."
    )

    md_path = tmp_path / "leak_probe.md"
    md_path.write_text("# Leak probe\n\nIrrelevant content.\n")

    # Call the hook directly — this matches the path taken by index_markdown
    # when registering documents after embedding.
    _catalog_markdown_hook(
        md_path=md_path,
        collection_name="docs__int-cce-deadbeef",
        content_type="prose",
        corpus="int-cce-deadbeef",
        chunk_count=1,
    )

    # The tmp catalog path must still be absent — the hook created nothing.
    assert not cat_path.exists() or not Catalog.is_initialized(cat_path)


def test_initialized_tmp_catalog_receives_writes_not_user_home(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """When a test explicitly initialises a catalog at the tmp path, writes
    land there — never in ``~/.config/nexus/catalog/``. This guards against
    subtle path leaks where a test initialises one catalog but module state
    points at another."""
    from nexus.doc_indexer import _catalog_markdown_hook

    tmp_catalog = tmp_path / "my-catalog"
    monkeypatch.setenv("NEXUS_CATALOG_PATH", str(tmp_catalog))
    Catalog.init(tmp_catalog)

    md_path = tmp_path / "scoped.md"
    md_path.write_text("# Scoped doc\n\nSome content.\n")

    _catalog_markdown_hook(
        md_path=md_path,
        collection_name="docs__int-cce-cafebabe",
        content_type="prose",
        corpus="int-cce-cafebabe",
        chunk_count=1,
    )

    # Owner landed in the tmp catalog, not the user's real one.
    db_path = tmp_catalog / ".catalog.db"
    assert db_path.exists(), "tmp catalog DB was not created"
    with sqlite3.connect(db_path) as conn:
        rows = conn.execute(
            "SELECT name FROM owners WHERE name = ?",
            ("int-cce-cafebabe",),
        ).fetchall()
    assert rows == [("int-cce-cafebabe",)], (
        "Owner did not land in the redirected tmp catalog — something is "
        "reading catalog_path() before the env override applies."
    )


def test_user_real_catalog_untouched_by_leak_pattern(tmp_path: Path) -> None:
    """Belt-and-braces: run the exact historical leak pattern
    (index_markdown with ``corpus=int-cce-<uid>``) and verify the user's real
    catalog mtime is unchanged.

    This guards against future regressions where, e.g., a new pathway
    registers owners outside the ``is_initialized`` guard.
    """
    real_catalog = Path.home() / ".config" / "nexus" / "catalog"
    if not real_catalog.exists():
        pytest.skip("user has no live catalog; nothing to compare")

    owners_jsonl = real_catalog / "owners.jsonl"
    catalog_db = real_catalog / ".catalog.db"
    before = {
        "owners_jsonl_mtime": owners_jsonl.stat().st_mtime if owners_jsonl.exists() else None,
        "owners_jsonl_size": owners_jsonl.stat().st_size if owners_jsonl.exists() else None,
        "catalog_db_mtime": catalog_db.stat().st_mtime if catalog_db.exists() else None,
        "catalog_db_size": catalog_db.stat().st_size if catalog_db.exists() else None,
    }

    # Run the historical leak pattern: invoke the hook with a synthetic
    # int-cce-* corpus name. Under the autouse fixture this must be a
    # no-op against the real catalog.
    from nexus.doc_indexer import _catalog_markdown_hook

    md_path = tmp_path / "leak.md"
    md_path.write_text("# leak probe\n\nContent.\n")
    _catalog_markdown_hook(
        md_path=md_path,
        collection_name="docs__int-cce-feedface",
        content_type="prose",
        corpus="int-cce-feedface",
        chunk_count=1,
    )

    after = {
        "owners_jsonl_mtime": owners_jsonl.stat().st_mtime if owners_jsonl.exists() else None,
        "owners_jsonl_size": owners_jsonl.stat().st_size if owners_jsonl.exists() else None,
        "catalog_db_mtime": catalog_db.stat().st_mtime if catalog_db.exists() else None,
        "catalog_db_size": catalog_db.stat().st_size if catalog_db.exists() else None,
    }
    assert before == after, (
        "User's real catalog was modified during the test — the isolation "
        "fixture is broken. Diff:\n"
        f"  before: {before}\n"
        f"  after:  {after}"
    )
