# SPDX-License-Identifier: AGPL-3.0-or-later
"""RDR-229 P2.3 (nexus-p2m1x.8), review round 1: what the first test file left open.

Same substrate and helpers as ``test_rdr229_p23_repo_model_from_row.py`` (real engine, real
``_run_index`` and ``index_markdown``, requests counted at the transport). Added here:

* Critical Assumption 2 on the PER-DOCUMENT path: ``nx index md`` reads the row once per call
  (``doc_indexer._target_model``), so that is where a per-document cost would be, not in the repo run.
* The write half of the outage rule: a changed or new file is written when the row read fails.
* Markdown into a collection whose row differs from its name's token.
* The ``_run_index`` wiring: a legacy-named docs collection, an rdr collection, ``--since-head``.
* The legacy migration, run for real: the row read follows the rename.
* No row read for a collection the run has no files for.

Integration-marked and not an RDR-192 pin file (``tests/test_rdr192_pins_not_integration_marked_lint.py``).
"""
from __future__ import annotations

import subprocess
import uuid
from pathlib import Path

import pytest

import nexus.db.http_vector_client as hvc
import nexus.indexer as idx
import nexus.mcp_infra as mi
from nexus.db.http_vector_client import HttpVectorClient
from nexus.db.local_ef import LocalEmbeddingFunction
from nexus.doc_indexer import index_markdown
from nexus.registry import RepoRegistry
from tests.integration.test_rdr229_p23_repo_model_from_row import (  # noqa: F401 — helpers and the autouse environment fixture shared with the first file
    CODE,
    DOCS_TOKEN_MODEL,
    PROSE,
    ROW_MODEL,
    TOKEN_MODEL,
    _add_files,
    _chunk_models,
    _forget_registration,
    _make_repo,
    _names,
    _register_row,
    _restamp,
    _run,
    _seeded_divergent_repo,
    _traffic,
    _unique,
)

pytestmark = [pytest.mark.integration]


# ── helpers ───────────────────────────────────────────────────────────────────


@pytest.fixture(autouse=True)
def _env(monkeypatch: pytest.MonkeyPatch):
    for k in ("GIT_AUTHOR_NAME", "GIT_COMMITTER_NAME"):
        monkeypatch.setenv(k, "Test")
    for k in ("GIT_AUTHOR_EMAIL", "GIT_COMMITTER_EMAIL"):
        monkeypatch.setenv(k, "test@test.invalid")
    mi.invalidate_collections_cache()
    yield
    mi.invalidate_collections_cache()


@pytest.fixture
def no_migration(monkeypatch: pytest.MonkeyPatch) -> None:
    """The collections here are the ones the registry names: the Phase-4 migration would move a repo
    whose owner is registered onto minted names (see the first file). The migration test does not take it."""
    monkeypatch.setattr(idx, "_migrate_legacy_collections", lambda *a, **k: {})


def _md_file(root: Path, label: str, i: int) -> Path:
    path = root / f"{label}-{i}.md"
    path.write_text(f"# {label} {i}\n\nP23 {label} prose {i} {uuid.uuid4().hex}.\n", encoding="utf-8")
    return path


def _md_listings(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, *, n: int, existing: bool, non_default_tenant: bool,
) -> int:
    """Collection listings ``nx index md`` sends to index *n* new files, one call each, into a collection
    that already holds a chunk (*existing*) or into one with no row yet. *non_default_tenant* makes the
    client not the process's own (``_resolve_collection_row`` case 3): the shared cache is not used and
    each read lists."""
    name = f"docs__{_unique()}__{ROW_MODEL}__v1"
    client = HttpVectorClient()
    if existing:
        assert index_markdown(_md_file(tmp_path, "seed", 0), corpus="p23md", t3=client, collection_name=name) > 0
    files = [_md_file(tmp_path, f"f{uuid.uuid4().hex[:6]}", i) for i in range(n)]
    mi.invalidate_collections_cache()
    with _traffic(monkeypatch) as seen:
        if non_default_tenant:
            monkeypatch.setattr(hvc, "_process_default_tenant", lambda: "some-other-tenant")
        for f in files:
            assert index_markdown(f, corpus="p23md", t3=client, collection_name=name) > 0
    return seen.listing()


# ── B1: Critical Assumption 2 on the per-document path ───────────────────────


@pytest.mark.parametrize("existing", [True, False], ids=["existing-collection", "new-collection-no-row"])
def test_nx_index_md_reads_the_row_from_the_shared_cache_not_once_per_document(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, existing: bool,
) -> None:
    small = _md_listings(tmp_path, monkeypatch, n=2, existing=existing, non_default_tenant=False)
    large = _md_listings(tmp_path, monkeypatch, n=6, existing=existing, non_default_tenant=False)
    assert small >= 1, "non-vacuity: the counter saw the listing the row read makes"
    assert large <= small + 2, f"four more documents cost {large - small} more listings"


@pytest.mark.parametrize("existing", [True, False], ids=["existing-collection", "new-collection-no-row"])
def test_nx_index_md_with_a_non_default_tenant_client_lists_once_per_call(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, existing: bool,
) -> None:
    """A client that is not the process's own has no shared cache (``HttpVectorClient._resolve_collection_row``
    case 3, nexus-fryrd: a cache keyed to the process tenant would answer another tenant's rows), so each
    ``nx index md`` call lists once. Measured: exactly one listing per extra document (n=2 -> n=6 adds 4).
    The cost is accepted, not new: ``nx index md`` has read its row through the store's own resolver since
    nexus-7jy5r, one call per file, and a non-default tenant is not a client the CLI builds. P2.3 adds none
    to it; it pins the one-per-call bound so a second read per call fails."""
    small = _md_listings(tmp_path, monkeypatch, n=2, existing=existing, non_default_tenant=True)
    large = _md_listings(tmp_path, monkeypatch, n=6, existing=existing, non_default_tenant=True)
    assert large - small >= 1, "non-vacuity: the other-tenant client listed per call (case 3 engaged)"
    assert large - small <= 6 - 2, f"{large - small} more listings for four more documents: more than one per call"


# ── S-a: the write half of the outage rule ───────────────────────────────────


def _outage_md(monkeypatch: pytest.MonkeyPatch, path: Path, name: str) -> int:
    mi.invalidate_collections_cache()
    with _traffic(monkeypatch, listing_outage=True) as seen:
        written = index_markdown(path, corpus="p23md", t3=HttpVectorClient(), collection_name=name)
    assert seen.listing() > 0, "non-vacuity: the write asked for the listing, and it failed"
    return written


def test_nx_index_md_writes_a_new_and_a_changed_file_when_the_listing_is_down(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    name = f"docs__{_unique()}__{ROW_MODEL}__v1"
    first = _md_file(tmp_path, "first", 0)
    assert index_markdown(first, corpus="p23md", t3=HttpVectorClient(), collection_name=name) > 0

    assert _outage_md(monkeypatch, _md_file(tmp_path, "new", 0), name) > 0, "a new file is written"
    first.write_text("# first 0\n\nP23 changed prose " + uuid.uuid4().hex + "\n", encoding="utf-8")
    assert _outage_md(monkeypatch, first, name) > 0, "a changed file is written"
    assert sum(_chunk_models(name).values()) >= 2, "the new file's chunk is in the collection"


def test_the_repo_run_writes_a_new_and_a_changed_file_when_the_listing_is_down(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, no_migration: None,
) -> None:
    names = _names(_unique())
    repo = _make_repo(tmp_path, {**CODE, **PROSE})
    assert _run(repo, tmp_path, names)["files_changed"] == 3
    (repo / "a.py").write_text("def a():\n    return 'p23 alpha changed'\n", encoding="utf-8")
    _add_files(repo, {"new.py": "def n():\n    return 'p23 new'\n"})
    mi.invalidate_collections_cache()

    with _traffic(monkeypatch, listing_outage=True) as seen:
        stats = _run(repo, tmp_path, names)

    assert seen.listing() > 0, "non-vacuity: the run asked for the listing, and it failed"
    assert stats["files_changed"] == 2, "the changed file and the new one are written, the rest skipped"


# ── S-b: markdown into a collection whose row differs from its name's token ──


def _chunk_map(collection: str) -> dict[str, str]:
    got = HttpVectorClient().get_or_create_collection(collection).get(include=["metadatas"], limit=500, sensitivity="include")
    return {i: (m or {}).get("embedding_model", "") for i, m in zip(got["ids"], got["metadatas"])}


def test_nx_index_md_stamps_the_rows_model_when_it_differs_from_the_names_token(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The row holds the engine's bge model under a name that says ``voyage-*``. The first file goes into
    a collection with no chunk, so no row is visible and it takes the name's token; the second is written
    with the row visible and takes the row's model. A third, with the registration forgotten as in a fresh
    process, is not refused: the md path registers ``effective_embedding_model_for_writes``, not the token
    (``nx index repo`` registered the token, which is what P2.3 fixed there)."""
    name = f"docs__{_unique()}__{DOCS_TOKEN_MODEL}__v1"
    _register_row(name, "docs", ROW_MODEL)
    client = HttpVectorClient()
    assert index_markdown(_md_file(tmp_path, "one", 0), corpus="p23md", t3=client, collection_name=name) > 0
    seeded = _chunk_map(name)
    assert set(seeded.values()) == {DOCS_TOKEN_MODEL}, "no row visible while the collection holds no chunk"

    mi.invalidate_collections_cache()
    assert index_markdown(_md_file(tmp_path, "two", 0), corpus="p23md", t3=client, collection_name=name) > 0
    second = {i: m for i, m in _chunk_map(name).items() if i not in seeded}
    assert second and set(second.values()) == {ROW_MODEL}

    _forget_registration(name)
    mi.invalidate_collections_cache()
    assert index_markdown(_md_file(tmp_path, "three", 0), corpus="p23md", t3=client, collection_name=name) > 0
    third = {i: m for i, m in _chunk_map(name).items() if i not in seeded and i not in second}
    assert third and set(third.values()) == {ROW_MODEL}


# ── S-c: the _run_index wiring ───────────────────────────────────────────────


def _ef_model() -> str:
    """The model the local embedder reports: what a legacy name keeps in local mode when no row answers."""
    return LocalEmbeddingFunction().model_name


def _legacy_docs_run(tmp_path: Path, monkeypatch: pytest.MonkeyPatch, legacy: str) -> tuple[Path, dict[str, str]]:
    """A repo whose docs collection is the legacy name *legacy*, as the Phase-4 migration leaves one whose
    row names another model than the write model (``result[ct] = legacy``). The migration's outcome is
    what is stood in for; the rest is the real run."""
    names = _names(_unique())
    monkeypatch.setattr(idx, "_migrate_legacy_collections", lambda *a, **k: {"docs": legacy})
    return _make_repo(tmp_path, PROSE), names


def test_a_legacy_named_docs_collection_with_no_row_visible_keeps_the_local_embedders_model(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    legacy = f"docs__{_unique()}"
    _register_row(legacy, "docs", ROW_MODEL)
    repo, names = _legacy_docs_run(tmp_path, monkeypatch, legacy)

    assert _run(repo, tmp_path, names)["files_changed"] == 1

    assert _ef_model() != ROW_MODEL, "non-vacuity: the embedder's model is not the profile model"
    assert set(_chunk_models(legacy)) == {_ef_model()}, "a name with no token answers the local embedder's model"


def test_a_legacy_named_docs_collection_whose_row_holds_a_model_skips_chunks_stamped_with_it(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    legacy = f"docs__{_unique()}"
    _register_row(legacy, "docs", ROW_MODEL)
    repo, names = _legacy_docs_run(tmp_path, monkeypatch, legacy)
    assert _run(repo, tmp_path, names)["files_changed"] == 1
    assert _restamp(legacy, ROW_MODEL) > 0

    assert _run(repo, tmp_path, names)["files_changed"] == 0, "the row's model is current"
    assert _restamp(legacy, _ef_model()) > 0
    assert _run(repo, tmp_path, names)["files_changed"] == 1, "the embedder's model is not the row's: stale"


RDR = {"docs/rdr/rdr-001-p23.md": "---\ntitle: P23\nid: RDR-001\n---\n# P23\n\nAn rdr document that p23 indexes.\n"}


def _pin_rdr(monkeypatch: pytest.MonkeyPatch, name: str) -> None:
    real = idx._repo_collection_or_legacy
    monkeypatch.setattr(
        idx, "_repo_collection_or_legacy",
        lambda repo, ct: name if ct == "rdr" else real(repo, ct),
    )


def test_an_rdr_collection_takes_its_rows_model_and_skips_on_an_unreadable_listing(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, no_migration: None,
) -> None:
    rdr = f"rdr__{_unique()}__{DOCS_TOKEN_MODEL}__v1"
    _register_row(rdr, "rdr", ROW_MODEL)
    _pin_rdr(monkeypatch, rdr)
    names = _names(_unique())
    repo = _make_repo(tmp_path, RDR)
    assert _run(repo, tmp_path, names)["files_changed"] == 1
    assert set(_chunk_models(rdr)) == {DOCS_TOKEN_MODEL}, "no row visible while the collection holds no chunk"

    _add_files(repo, {"docs/rdr/rdr-002-p23.md": RDR["docs/rdr/rdr-001-p23.md"].replace("001", "002")})
    assert _run(repo, tmp_path, names)["files_changed"] == 2
    assert set(_chunk_models(rdr)) == {ROW_MODEL}, "stamped with the row's model, not the token's"

    mi.invalidate_collections_cache()
    with _traffic(monkeypatch, listing_outage=True) as seen:
        stats = _run(repo, tmp_path, names)
    assert seen.listing() > 0
    assert stats["files_changed"] == 0, "an unreadable listing re-indexes nothing whose content has not changed"


def test_an_unreadable_listing_re_indexes_nothing_unchanged_in_a_since_head_run(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, no_migration: None,
) -> None:
    """The delta path builds its staleness caches from the catalog (``seed_staleness_cache_from_catalog``),
    not a sweep; the flag is set on those caches too."""
    repo, names = _seeded_divergent_repo(tmp_path)
    base = subprocess.run(
        ["git", "rev-parse", "HEAD"], cwd=repo, check=True, capture_output=True, text=True,
    ).stdout.strip()
    _add_files(repo, {"new.py": "def n():\n    return 'p23 new'\n", "new.md": "# New\n\nP23 new prose.\n"})
    assert _run(repo, tmp_path, names)["files_changed"] == 5
    assert set(_chunk_models(names["code"])) == {ROW_MODEL}

    monkeypatch.setattr(idx, "_get_owner_head_hash", lambda r: base)
    reg = RepoRegistry(tmp_path / "repos.json")
    phases: list[str] = []
    mi.invalidate_collections_cache()
    with _traffic(monkeypatch, listing_outage=True) as seen:
        stats = idx._run_index(repo, reg, force=False, since_head=True, on_phase=phases.append)

    assert any(p.startswith("--since-head: 2 changed") for p in phases), f"the delta is the two new files: {phases}"
    assert seen.listing() > 0
    assert stats["files_changed"] == 0


# ── M1: the legacy migration, run for real ───────────────────────────────────


def test_the_row_read_follows_the_phase_4_rename(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """The first run of a repo whose owner is not registered writes the path-derived collections; the second
    renames them onto the catalog's minted names. The collections cache the row read goes through is up to
    60 s old and was filled before the rename, so it has no row for the new name: the read answers "no row"
    (the name's token) and the registration loop registers over the source's row. The run drops the cache
    after a migration, so the row is read from a listing made after the rename."""
    repo = _make_repo(tmp_path, {**CODE, **PROSE})
    reg = RepoRegistry(tmp_path / "repos.json")
    reg.add(repo)
    idx._run_index(repo, reg, force=False)
    _add_files(repo, {"new.py": "def n():\n    return 'p23 new'\n"})

    seen: list[tuple[str, bool]] = []
    real = idx._repo_target_model

    def spy(db, name, *, legacy_model):
        result = real(db, name, legacy_model=legacy_model)
        seen.append((name, mi.get_collection_row(name, refresh=False) is not None))
        return result

    monkeypatch.setattr(idx, "_repo_target_model", spy)
    migrated: list[dict] = []
    real_migrate = idx._migrate_legacy_collections

    def migrate(*a, **k):
        out = real_migrate(*a, **k)
        migrated.append(out)
        return out

    monkeypatch.setattr(idx, "_migrate_legacy_collections", migrate)
    with _traffic(monkeypatch) as traffic:
        stats = idx._run_index(repo, reg, force=False)

    renames = [p for _k, _m, p in traffic.log if p.endswith("/collections/rename")]
    assert len(renames) == 2, f"non-vacuity: the migration renamed the code and docs collections: {renames}"
    assert migrated and migrated[0].get("code") and migrated[0].get("docs"), migrated
    assert stats["files_changed"] >= 1
    assert seen and all(visible for _name, visible in seen), (
        f"the row of a collection the migration renamed is not in the cache the read used: {seen}")


# ── M2: no row read for a collection the run has no files for ───────────────


def test_a_code_only_run_reads_no_row_for_the_docs_collection(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, no_migration: None,
) -> None:
    names = _names(_unique())
    repo = _make_repo(tmp_path, CODE)
    read: list[str] = []
    real = idx._repo_target_model

    def spy(db, name, *, legacy_model):
        read.append(name)
        return real(db, name, legacy_model=legacy_model)

    monkeypatch.setattr(idx, "_repo_target_model", spy)
    assert _run(repo, tmp_path, names)["files_changed"] == 2
    assert read == [names["code"]], f"a row was read for a collection with no files: {read}"
