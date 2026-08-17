# SPDX-License-Identifier: AGPL-3.0-or-later
from __future__ import annotations

import shutil
import subprocess
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest
from nexus.db.minilm_direct import MiniLMDirectEmbeddingFunction as DefaultEmbeddingFunction

from nexus.db.t3 import T3Database
from nexus.registry import RepoRegistry
from tests._catalog_fixture_ops import ActiveCatalog, active_reader, documents_by_file_path
from tests.conftest import fake_credentials, make_vector_test_client

# All tests in this module are end-to-end: real vector substrate, real local
# embeddings, real CLI subprocesses. They average ~5.8s/test on CI and
# accounted for ~138s (16%) of the pre-marker pytest runtime. Per the
# project convention (pyproject.toml addopts deselects integration), e2e
# suites belong under the ``integration`` marker. Run explicitly with
# ``uv run pytest -m integration``; release-shakedown already exercises
# the same code paths through ``nx index repo`` end-to-end on every tag.
#
# nexus-i711w C-store: the ``local_catalog_backend`` half of the old
# module pin (23f56218) is retired here, per the port contract — the
# CATALOG substrate moves to the suite's engine default (per-test tenant
# via ``t2_service_env``); the VECTOR half stays pinned local by
# ``_legacy_vector_backend`` below (the test seam these tests wire
# ``local_t3`` through). Every subject in this file is live indexer
# behaviour, so the whole file PORTS; the raw ``cat._db`` reads the pin
# was covering for are converted to the public reader surface
# (tests/_catalog_fixture_ops), and the manifest sabotage goes through
# the whitelisted writer ops.
#
# KNOWN HAZARD, recorded for the serial verify run (the pin commit's
# measured signature): under this vector-local/catalog-service split the
# nexus-7vuw legacy -> conformant collection rename's data plane can hit
# a collection the engine catalog has never seen (``HTTP 404``). That
# failure is CAUGHT in ``_migrate_legacy_collections`` (falls back to the
# legacy name, non-fatal), but the three rename-adjacent tests carry
# PORT-VERIFY marks so a mismatch is attributed, not shrugged at.
pytestmark = [pytest.mark.integration]

_CORPUS_FILES = [
    "src/nexus/ttl.py",
    "src/nexus/corpus.py",
    "src/nexus/types.py",
    "src/nexus/session.py",
]
_NEXUS_ROOT = Path(__file__).parent.parent

_PROSE_FILES = {
    "README.md": (
        "# Nexus\n\nNexus is a semantic search and knowledge management system.\n\n"
        "## Features\n\n- Semantic search across code repositories\n"
        "- Persistent memory across sessions\n- Three storage tiers\n"
    ),
    "docs/architecture.md": (
        "# Architecture\n\n## Overview\n\nNexus uses a three-tier storage model.\n\n"
        "### T1 — Session Scratch\n\nEphemeral ChromaDB with DefaultEmbeddingFunction.\n\n"
        "### T3 — Permanent Knowledge\n\nChromaDB Cloud with Voyage AI embeddings.\n"
    ),
    "config.yaml": (
        "server:\n  port: 7890\n  headPollInterval: 10\n"
        "embeddings:\n  rerankerModel: rerank-2.5\n"
    ),
}

_RDR_FILES = {
    "docs/rdr/ADR-001-storage-tiers.md": (
        "---\ntitle: Storage Tier Architecture\nstatus: accepted\n---\n\n"
        "# ADR-001: Storage Tier Architecture\n\n"
        "## Decision\n\nWe use three storage tiers for different persistence needs.\n"
        "## Consequences\n\nMore complexity but better separation of concerns.\n"
    ),
}


def _git_init(repo: Path, msg: str = "Initial commit") -> None:
    for cmd in (
        ["git", "init", "-b", "main"],
        ["git", "config", "user.email", "test@nexus"],
        ["git", "config", "user.name", "Nexus Test"],
        ["git", "add", "."],
        ["git", "commit", "-m", msg],
    ):
        subprocess.run(cmd, cwd=repo, check=True, capture_output=True)


# ── Fixtures ──────────────────────────────────────────────────────────────────

@pytest.fixture(scope="module")
def mini_repo(tmp_path_factory: pytest.TempPathFactory) -> Path:
    repo = tmp_path_factory.mktemp("nexus-mini")
    for rel in _CORPUS_FILES:
        dest = repo / rel
        dest.parent.mkdir(parents=True, exist_ok=True)
        shutil.copy2(_NEXUS_ROOT / rel, dest)
    _git_init(repo, "Initial corpus commit")
    return repo


@pytest.fixture
def local_t3() -> T3Database:
    return T3Database(
        _client=make_vector_test_client(), _ef_override=DefaultEmbeddingFunction()
    )


@pytest.fixture
def registry(tmp_path: Path, mini_repo: Path) -> RepoRegistry:
    reg = RepoRegistry(tmp_path / "repos.json")
    reg.add(mini_repo)
    return reg


@pytest.fixture(autouse=True)
def _legacy_vector_backend(monkeypatch):
    # nexus-o06g4: these e2e tests wire a raw-Chroma EphemeralClient ``local_t3``
    # + ``mock_voyage_client`` and exercise the CLIENT-embed indexing path. Pin
    # the vector backend off the 6.0 service default (is_vector_service_mode()
    # is True by default since RDR-155 P4a) so the indexer client-embeds into
    # the fixture instead of passing empty server-side-embed placeholders that
    # raw chromadb rejects with "IndexError: list index out of range in upsert"
    # (normalize_insert_record_set). The service-mode indexing path is covered
    # by the unit seam-B tests + the live service smokes, not here.
    monkeypatch.setenv("NX_STORAGE_BACKEND_VECTORS", "local")


@pytest.fixture(autouse=True)
def _pin_fake_voyage_key(monkeypatch):
    # The indexer's embedder ROUTING keys on get_credential("voyage_api_key")
    # presence (indexer.py) — with no key it silently falls back to local
    # MiniLM and every voyage-model assertion below fails. These tests mock
    # voyageai.Client (no real call is ever made), so key PRESENCE is all
    # routing needs; pin a fake so the suite is independent of the host's
    # .env / config.yml key state (dead, masked, or absent — the
    # NEXUS_GATE_NO_VOYAGE gate regime included).
    monkeypatch.setenv("VOYAGE_API_KEY", "fake-key-for-routing-only")


@pytest.fixture(autouse=True)
def mock_voyage_client():
    ef = DefaultEmbeddingFunction()
    mock_client = MagicMock()

    def fake_embed(texts, model, input_type="document"):
        r = MagicMock()
        r.embeddings = ef(texts)
        return r

    def fake_contextualized_embed(inputs, model, input_type="document"):
        r = MagicMock()
        br = MagicMock()
        br.embeddings = ef(inputs[0])
        r.results = [br]
        return r

    mock_client.embed.side_effect = fake_embed
    mock_client.contextualized_embed.side_effect = fake_contextualized_embed
    with patch("voyageai.Client", return_value=mock_client):
        yield mock_client


@pytest.fixture(scope="module")
def rich_repo(tmp_path_factory: pytest.TempPathFactory) -> Path:
    import pymupdf as _fitz
    repo = tmp_path_factory.mktemp("nexus-rich")
    for rel in _CORPUS_FILES:
        dest = repo / rel
        dest.parent.mkdir(parents=True, exist_ok=True)
        shutil.copy2(_NEXUS_ROOT / rel, dest)
    for rel, content in {**_PROSE_FILES, **_RDR_FILES}.items():
        dest = repo / rel
        dest.parent.mkdir(parents=True, exist_ok=True)
        dest.write_text(content, encoding="utf-8")
    pdf_doc = _fitz.open()
    page = pdf_doc.new_page()
    page.insert_text((72, 100), "Hello World. This is a test document for PDF ingest.", fontsize=12)
    pdf_doc.set_metadata({"title": "Test Document", "author": "Test Author"})
    (repo / "docs" / "test.pdf").write_bytes(pdf_doc.tobytes())
    pdf_doc.close()
    _git_init(repo, "Initial rich corpus commit")
    return repo


@pytest.fixture
def rich_registry(tmp_path: Path, rich_repo: Path) -> RepoRegistry:
    reg = RepoRegistry(tmp_path / "repos.json")
    reg.add(rich_repo)
    return reg


# ── Helpers ───────────────────────────────────────────────────────────────────

def _index(repo: Path, registry: RepoRegistry, t3: T3Database, **kw) -> None:
    from nexus.indexer import index_repository
    with patch("nexus.db.make_t3", return_value=t3), \
         patch("nexus.config.get_credential", side_effect=fake_credentials()):
        index_repository(repo, registry, **kw)


def _get_sources(t3: T3Database, col_name: str) -> set[str]:
    """RDR-102 D2: source_path was removed from the chunk schema. The
    chunk's ``title`` is constructed as ``"{file.relative_to(repo)}:
    chunk-{i}"`` for code (code_indexer.py:393) and prose
    (prose_indexer.py:96), so it still carries the filename — sufficient
    for the ``any(filename in p for p in sources)`` membership pattern
    these tests use to verify file-to-collection routing."""
    col = t3.get_or_create_collection(col_name)
    return {m.get("title", "") for m in col.get(include=["metadatas"])["metadatas"]}


# ── Basic indexer pipeline ────────────────────────────────────────────────────

def test_index_creates_chunks_in_t3(
    mini_repo: Path, registry: RepoRegistry, local_t3: T3Database
) -> None:
    _index(mini_repo, registry, local_t3)
    col = local_t3.get_or_create_collection(registry.get(mini_repo)["collection"])
    assert col.count() > 0

def test_index_does_not_write_repo_status(
    mini_repo: Path, registry: RepoRegistry, local_t3: T3Database
) -> None:
    """RDR-137 Phase 3.8 (nexus-tts0d.13) dropped indexer status writes:
    the per-repo ``status`` field was write-only with no consumer (RDR-137
    Gap5), so the indexer no longer transitions it from ``registered`` to
    ``ready``. Indexing success is verified by the chunk-count and
    source-path tests; ``status`` is now vestigial and must stay untouched.
    This guards against re-introducing the dropped status write."""
    assert registry.get(mini_repo)["status"] == "registered"
    _index(mini_repo, registry, local_t3)
    # No transition to "ready" — RDR-137 removed indexer status tracking.
    assert registry.get(mini_repo)["status"] == "registered"


def test_index_legacy_registry_name_no_catalog_owner_succeeds(
    mini_repo: Path, registry: RepoRegistry, local_t3: T3Database
) -> None:
    """nexus-5ut2a: a repos.json entry carrying a pre-RDR-103 legacy
    2-segment collection name (``code__<basename>-<hash8>``) for a repo
    with NO catalog owner must not crash on the strict conformant-name
    guard. ``_run_index`` re-routes the non-conformant registry value
    through the path-derived conformant synth, so the first index of a
    never-cataloged / stale-entry repo succeeds and creates a conformant
    collection.

    Reproduces the arcaneum post-release shakeout crash (pre-existing
    since v4.23.0): before the fix, ``db.get_or_create_collection`` got the
    legacy 2-segment name and raised ``ValueError: ... is not conformant``.

    PORT-VERIFY (nexus-i711w): the "no catalog owner" premise now holds via
    the fresh per-test engine tenant (nothing registered yet) instead of an
    uninitialized local dir; single index run, so the nexus-7vuw rename data
    plane should not fire — if this fails, see the module-header hazard.
    """
    from nexus.corpus import is_conformant_collection_name
    from nexus.indexer import _legacy_collection_name

    # RepoRegistry.add now mints conformant names; force the entry to the
    # legacy 2-segment shape to simulate a pre-RDR-103 repos.json entry.
    legacy_code = _legacy_collection_name(mini_repo, "code")
    legacy_docs = _legacy_collection_name(mini_repo, "docs")
    assert not is_conformant_collection_name(legacy_code), (
        "fixture precondition: the seeded name must be genuinely legacy"
    )
    entry = registry._data["repos"][str(mini_repo)]
    entry["collection"] = legacy_code
    entry["code_collection"] = legacy_code
    entry["docs_collection"] = legacy_docs
    registry._save()

    # No catalog owner in this tmp env → the no-owner fallback path that
    # crashed for arcaneum. Must complete without raising.
    _index(mini_repo, registry, local_t3)

    # Every code collection this repo created must be conformant — the fix
    # re-routed the legacy name through the synth.
    from nexus.repo_identity import _repo_identity
    _, path_hash = _repo_identity(mini_repo)
    code_cols = [
        e["name"]
        for e in local_t3.list_collections()
        if path_hash in e["name"] and e["name"].startswith("code__")
    ]
    assert code_cols, f"index created no code collection (path_hash={path_hash})"
    assert all(is_conformant_collection_name(c) for c in code_cols), (
        f"index created a non-conformant code collection: {code_cols}"
    )


@pytest.mark.parametrize("expected_file", ["ttl.py", "session.py"])
def test_index_chunks_carry_source_path(
    mini_repo: Path, registry: RepoRegistry, local_t3: T3Database, expected_file: str
) -> None:
    _index(mini_repo, registry, local_t3)
    sources = _get_sources(local_t3, registry.get(mini_repo)["collection"])
    assert any(expected_file in p for p in sources), f"{expected_file} missing from: {sources}"


def test_phase_5c_index_drops_corpus_store_type_git_meta(
    rich_repo: Path,
    rich_registry: RepoRegistry,
    local_t3: T3Database,
) -> None:
    """nexus-o6aa.13 (Phase 5c) acceptance: freshly-indexed chunks must
    NOT carry the 3 dropped keys (``corpus``, ``store_type``, ``git_meta``).
    ``title`` is intentionally KEPT — find_ids_by_title is load-bearing
    for nx store / MCP store_get title-fallback.

    Scoped to *this test's* collections only because EphemeralClient
    shares process-level state across tests.
    """
    from nexus.registry import _repo_identity

    _index(rich_repo, rich_registry, local_t3)

    _, path_hash = _repo_identity(rich_repo)
    my_cols = [
        e["name"]
        for e in local_t3.list_collections()
        if path_hash in e["name"]
    ]

    leaks: dict[str, list[str]] = {}
    titles_seen = 0
    total_chunks = 0
    dropped_keys = ("corpus", "store_type", "git_meta")
    for col_name in my_cols:
        col = local_t3.get_or_create_collection(col_name)
        metadatas = col.get(include=["metadatas"])["metadatas"]
        total_chunks += len(metadatas)
        for m in metadatas:
            for k in dropped_keys:
                if k in m:
                    leaks.setdefault(f"{col_name}:{k}", []).append(
                        m.get("doc_id", "<no-doc-id>")
                    )
            if m.get("title"):
                titles_seen += 1

    assert total_chunks > 0, "expected at least one chunk in fresh index"
    assert not leaks, (
        f"Phase 5c greenfield index leaked dropped keys (nexus-o6aa.13 "
        f"regression). Indexed {total_chunks} chunks; keys present in: "
        f"{sorted(leaks)}."
    )
    assert titles_seen > 0, (
        "Phase 5c kept title in the schema (audit / find_ids_by_title); "
        "at least one indexed chunk must carry one."
    )


def test_greenfield_index_writes_no_deprecated_keys(
    rich_repo: Path, rich_registry: RepoRegistry, local_t3: T3Database
) -> None:
    """nexus-e5uw acceptance: a fresh greenfield index must not write
    chunks carrying any of the 8 deprecated chunk-metadata keys that
    ``nx catalog migrate``'s prune-deprecated-keys verb targets
    (5 from RDR-101 Phase 4: ``source_path``, ``git_branch``,
    ``git_commit_hash``, ``git_project_name``, ``git_remote_url``;
    plus 3 from Phase 5c: ``corpus``, ``store_type``, ``git_meta``).
    The bead's acceptance is that a clean greenfield repro +
    ``nx catalog migrate`` produces 0 chunks_updated, which is
    equivalent to: no chunk written by ``index_repository`` carries
    any of those keys.

    RDR-102 Phase B drops ``source_path`` from
    :data:`ALLOWED_TOP_LEVEL`; ``normalize()`` packs the four
    ``git_*`` fields into a single ``git_meta`` JSON blob and drops
    the flat keys. RDR-101 Phase 5c subsequently dropped ``corpus``,
    ``store_type``, and ``git_meta`` itself from
    :data:`ALLOWED_TOP_LEVEL`. This test is the post-Phase-5c
    contract.
    """
    from nexus.commands.catalog import _PRUNE_DEPRECATED_KEYS
    from nexus.registry import _repo_identity

    _index(rich_repo, rich_registry, local_t3)

    # Scope to *this test's* collections only — chromadb's EphemeralClient
    # shares process-level state, so collections seeded by sibling test
    # modules (e.g. test_exporter.py's raw col.upsert with source_path)
    # leak into this view of list_collections().
    _, path_hash = _repo_identity(rich_repo)
    my_cols = [
        e["name"]
        for e in local_t3.list_collections()
        if path_hash in e["name"]
    ]

    leaks: dict[str, list[str]] = {}
    total_chunks = 0
    for col_name in my_cols:
        col = local_t3.get_or_create_collection(col_name)
        metadatas = col.get(include=["metadatas"])["metadatas"]
        total_chunks += len(metadatas)
        for m in metadatas:
            for k in _PRUNE_DEPRECATED_KEYS:
                if k in m:
                    leaks.setdefault(f"{col_name}:{k}", []).append(
                        m.get("title", "<no-title>")
                    )

    assert total_chunks > 0, "expected at least one chunk in fresh index"
    assert not leaks, (
        f"Greenfield index leaked deprecated keys (nexus-e5uw regression). "
        f"Indexed {total_chunks} chunks across code/docs/rdr collections; "
        f"prune-target keys present in: {sorted(leaks)}. "
        f"`nx catalog migrate` should be a no-op on a fresh greenfield."
    )


def test_index_staleness_skips_unchanged(
    mini_repo: Path, registry: RepoRegistry, local_t3: T3Database
) -> None:
    _index(mini_repo, registry, local_t3)
    col = local_t3.get_or_create_collection(registry.get(mini_repo)["collection"])
    count1 = col.count()
    _index(mini_repo, registry, local_t3)
    assert col.count() == count1


def test_index_frecency_only_preserves_count(
    mini_repo: Path, registry: RepoRegistry, local_t3: T3Database
) -> None:
    _index(mini_repo, registry, local_t3)
    col = local_t3.get_or_create_collection(registry.get(mini_repo)["collection"])
    count = col.count()
    _index(mini_repo, registry, local_t3, frecency_only=True)
    assert col.count() == count


def test_index_stamps_pipeline_version_without_force(
    rich_repo: Path, rich_registry: RepoRegistry, local_t3: T3Database
) -> None:
    """nexus-7yfm: incremental index writes pipeline_version stamp.

    The stamp asserts "these embeddings were produced by PIPELINE_VERSION
    code." Whether ``--force`` was used does not change which pipeline
    produced them — both paths run the same chunker + embedder. So the
    stamp must be written on every successful index, not only on force.
    """
    from nexus.indexer import PIPELINE_VERSION, get_collection_pipeline_version

    # PORTED (nexus-i711w): derive the rdr collection name BEFORE indexing —
    # the same resolution point the indexer itself uses. On a LIVE catalog,
    # _index registers the repo owner mid-run, so a post-run derivation
    # returns the owner-prefixed CONFORMANT name while this run's chunks
    # were written under the LEGACY name (first-index drift window; the
    # next-run convergence is _migrate_legacy_collections' subject, pinned
    # by the migration/tombstone-probe suites). The old pinned substrate
    # had no catalog at either point, which is why both derivations agreed.
    from nexus.indexer import _repo_collection_or_legacy
    rdr_col_name = _repo_collection_or_legacy(rich_repo, "rdr")

    _index(rich_repo, rich_registry, local_t3)
    info = rich_registry.get(rich_repo)
    code_col = local_t3.get_or_create_collection(info["code_collection"])
    docs_col = local_t3.get_or_create_collection(info["docs_collection"])
    assert get_collection_pipeline_version(code_col) == PIPELINE_VERSION
    assert get_collection_pipeline_version(docs_col) == PIPELINE_VERSION


def test_index_stamps_pipeline_version_with_force(
    rich_repo: Path, rich_registry: RepoRegistry, local_t3: T3Database
) -> None:
    """Regression guard — --force still stamps after the always-stamp fix."""
    from nexus.indexer import PIPELINE_VERSION, get_collection_pipeline_version

    _index(rich_repo, rich_registry, local_t3, force=True)
    info = rich_registry.get(rich_repo)
    code_col = local_t3.get_or_create_collection(info["code_collection"])
    docs_col = local_t3.get_or_create_collection(info["docs_collection"])
    assert get_collection_pipeline_version(code_col) == PIPELINE_VERSION
    assert get_collection_pipeline_version(docs_col) == PIPELINE_VERSION


# ── Smart indexing: dual collection ───────────────────────────────────────────

def test_smart_index_creates_both_collections(
    rich_repo: Path, rich_registry: RepoRegistry, local_t3: T3Database,
) -> None:
    # PORTED (nexus-i711w): derive the rdr collection name BEFORE indexing —
    # the same resolution point the indexer itself uses. On a LIVE catalog,
    # _index registers the repo owner mid-run, so a post-run derivation
    # returns the owner-prefixed CONFORMANT name while this run's chunks
    # were written under the LEGACY name (first-index drift window; the
    # next-run convergence is _migrate_legacy_collections' subject, pinned
    # by the migration/tombstone-probe suites). The old pinned substrate
    # had no catalog at either point, which is why both derivations agreed.
    from nexus.indexer import _repo_collection_or_legacy
    rdr_col_name = _repo_collection_or_legacy(rich_repo, "rdr")

    _index(rich_repo, rich_registry, local_t3)
    info = rich_registry.get(rich_repo)
    for key in ("code_collection", "docs_collection"):
        col = local_t3.get_or_create_collection(info[key])
        assert col.count() > 0, f"No chunks in {key}"


@pytest.mark.parametrize("file,expected_col,excluded_col", [
    ("ttl.py", "code_collection", "docs_collection"),
    ("README.md", "docs_collection", "code_collection"),
    ("architecture.md", "docs_collection", None),
])
def test_smart_index_file_routing(
    rich_repo: Path, rich_registry: RepoRegistry, local_t3: T3Database,
    file: str, expected_col: str, excluded_col: str | None,
) -> None:
    # PORTED (nexus-i711w): derive the rdr collection name BEFORE indexing —
    # the same resolution point the indexer itself uses. On a LIVE catalog,
    # _index registers the repo owner mid-run, so a post-run derivation
    # returns the owner-prefixed CONFORMANT name while this run's chunks
    # were written under the LEGACY name (first-index drift window; the
    # next-run convergence is _migrate_legacy_collections' subject, pinned
    # by the migration/tombstone-probe suites). The old pinned substrate
    # had no catalog at either point, which is why both derivations agreed.
    from nexus.indexer import _repo_collection_or_legacy
    rdr_col_name = _repo_collection_or_legacy(rich_repo, "rdr")

    _index(rich_repo, rich_registry, local_t3)
    info = rich_registry.get(rich_repo)
    sources = _get_sources(local_t3, info[expected_col])
    assert any(file in p for p in sources), f"{file} not in {expected_col}: {sources}"
    if excluded_col:
        excl_sources = _get_sources(local_t3, info[excluded_col])
        assert not any(file in p for p in excl_sources), f"{file} should not be in {excluded_col}"


def test_smart_index_config_yaml_excluded(
    rich_repo: Path, rich_registry: RepoRegistry, local_t3: T3Database,
) -> None:
    # PORTED (nexus-i711w): derive the rdr collection name BEFORE indexing —
    # the same resolution point the indexer itself uses. On a LIVE catalog,
    # _index registers the repo owner mid-run, so a post-run derivation
    # returns the owner-prefixed CONFORMANT name while this run's chunks
    # were written under the LEGACY name (first-index drift window; the
    # next-run convergence is _migrate_legacy_collections' subject, pinned
    # by the migration/tombstone-probe suites). The old pinned substrate
    # had no catalog at either point, which is why both derivations agreed.
    from nexus.indexer import _repo_collection_or_legacy
    rdr_col_name = _repo_collection_or_legacy(rich_repo, "rdr")

    _index(rich_repo, rich_registry, local_t3)
    info = rich_registry.get(rich_repo)
    docs_sources = _get_sources(local_t3, info["docs_collection"])
    assert not any("config.yaml" in p for p in docs_sources)


def test_smart_index_py_excluded_from_docs(
    rich_repo: Path, rich_registry: RepoRegistry, local_t3: T3Database,
) -> None:
    # PORTED (nexus-i711w): derive the rdr collection name BEFORE indexing —
    # the same resolution point the indexer itself uses. On a LIVE catalog,
    # _index registers the repo owner mid-run, so a post-run derivation
    # returns the owner-prefixed CONFORMANT name while this run's chunks
    # were written under the LEGACY name (first-index drift window; the
    # next-run convergence is _migrate_legacy_collections' subject, pinned
    # by the migration/tombstone-probe suites). The old pinned substrate
    # had no catalog at either point, which is why both derivations agreed.
    from nexus.indexer import _repo_collection_or_legacy
    rdr_col_name = _repo_collection_or_legacy(rich_repo, "rdr")

    _index(rich_repo, rich_registry, local_t3)
    info = rich_registry.get(rich_repo)
    docs_sources = _get_sources(local_t3, info["docs_collection"])
    assert not any(".py" in p for p in docs_sources)


def test_smart_index_rdr_routing(
    rich_repo: Path, rich_registry: RepoRegistry, local_t3: T3Database,
) -> None:
    """RDR-102 D2: source_path is gone from chunks. The frontmatter
    title for ADR-001-storage-tiers.md is "Storage Tier Architecture"
    (overrides the filename-derived stem), so the chunk's ``title``
    field doesn't carry "ADR-001". Verify routing via the chunk's
    ``section_title`` (the H1 "ADR-001: Storage Tier Architecture"
    propagates as section_title for all chunks under it) AND by
    asserting the rdr collection has chunks while docs does not.

    nexus-sghyo PORT (2026-08-07): this test used to run under the
    ``cloud_mode`` fixture solely so ``rdr_col_name`` would come out
    voyage-shaped for a hardcoded ``"voyage-context-3" in rdr_col_name``
    assertion (nexus-x2x6v). ``cloud_mode`` + the file's autouse
    ``_legacy_vector_backend`` (``NX_STORAGE_BACKEND_VECTORS=local``) is
    now exactly the retired non-service-embedding combination
    (indexer.py's ``CredentialsMissingError`` — "non-service embedding
    was retired... Set NX_STORAGE_BACKEND_VECTORS=service (the default)
    or unset it"). The routing behaviour under test (RDR content lands
    in rdr__, never docs__) is embedding-model-independent, so this now
    runs in the suite's default LOCAL mode; the voyage-shaped-name
    assertion is dropped rather than ported — cloud-mode collection
    naming is a pure client-side derivation with its own dedicated,
    network-free unit coverage:
    ``tests/test_rdr_109_phase2_dispatch.py::
    test_effective_in_cloud_mode_delegates_to_canonical``.
    """
    # PORTED (nexus-i711w): derive the rdr collection name BEFORE indexing —
    # the same resolution point the indexer itself uses. On a LIVE catalog,
    # _index registers the repo owner mid-run, so a post-run derivation
    # returns the owner-prefixed CONFORMANT name while this run's chunks
    # were written under the LEGACY name (first-index drift window; the
    # next-run convergence is _migrate_legacy_collections' subject, pinned
    # by the migration/tombstone-probe suites). The old pinned substrate
    # had no catalog at either point, which is why both derivations agreed.
    from nexus.indexer import _repo_collection_or_legacy
    rdr_col_name = _repo_collection_or_legacy(rich_repo, "rdr")

    _index(rich_repo, rich_registry, local_t3)
    info = rich_registry.get(rich_repo)
    docs_col = local_t3.get_or_create_collection(info["docs_collection"])
    docs_sections = {
        m.get("section_title", "")
        for m in docs_col.get(include=["metadatas"])["metadatas"]
    }
    assert not any("ADR-001" in s for s in docs_sections), (
        f"ADR-001 must not appear in docs__ section_titles: {docs_sections}"
    )

    rdr_col = local_t3.get_or_create_collection(rdr_col_name)
    rdr_metas = rdr_col.get(include=["metadatas"])["metadatas"]
    assert rdr_metas, f"expected ADR-001 chunks in {rdr_col_name}; got none"
    rdr_sections = {m.get("section_title", "") for m in rdr_metas}
    assert any("ADR-001" in s for s in rdr_sections), (
        f"ADR-001 H1 must surface as section_title in rdr__: {rdr_sections}"
    )


@pytest.mark.parametrize("query,corpus_key,expected_file", [
    ("parse TTL days weeks permanent", "code_collection", "ttl.py"),
    ("semantic search knowledge management features", "docs_collection", "README.md"),
])
def test_smart_index_search(
    rich_repo: Path, rich_registry: RepoRegistry, local_t3: T3Database,
    query: str, corpus_key: str, expected_file: str,
) -> None:
    # PORTED (nexus-i711w): derive the rdr collection name BEFORE indexing —
    # the same resolution point the indexer itself uses. On a LIVE catalog,
    # _index registers the repo owner mid-run, so a post-run derivation
    # returns the owner-prefixed CONFORMANT name while this run's chunks
    # were written under the LEGACY name (first-index drift window; the
    # next-run convergence is _migrate_legacy_collections' subject, pinned
    # by the migration/tombstone-probe suites). The old pinned substrate
    # had no catalog at either point, which is why both derivations agreed.
    from nexus.indexer import _repo_collection_or_legacy
    rdr_col_name = _repo_collection_or_legacy(rich_repo, "rdr")

    _index(rich_repo, rich_registry, local_t3)
    info = rich_registry.get(rich_repo)
    results = local_t3.search(query, [info[corpus_key]], n_results=5)
    assert len(results) > 0
    # RDR-102 D2: search results no longer carry source_path (filtered
    # by normalize() at write time). Title carries "{relpath}:chunk-{i}".
    sources = [r.get("title", "") for r in results]
    assert any(expected_file in p for p in sources), f"{expected_file} not in: {sources}"


# test_smart_index_embedding_model_metadata RETIRED (nexus-sghyo, 2026-08-07).
# This test drove a full index_repository() run under the `cloud_mode`
# fixture combined with the file's autouse `_legacy_vector_backend`
# (NX_STORAGE_BACKEND_VECTORS=local) + `mock_voyage_client`, then asserted
# the resulting T3 chunk metadata carried "voyage-code-3"/"voyage-context-3"
# as `embedding_model`. That is precisely the CLIENT-embed leg nexus-sghyo
# retired (Hal determination 2026-07-28: "we do no embedding on the
# client") — indexer.py now raises CredentialsMissingError for exactly this
# combination ("non-service embedding was retired... Set
# NX_STORAGE_BACKEND_VECTORS=service (the default) or unset it"), and a
# mocked voyageai.Client has no honest production equivalent to simulate
# against post-retirement.
#
# The actual behavior worth pinning — that cloud-mode indexing computes
# "voyage-code-3" for code / "voyage-context-3" for docs as the WRITE-side
# embedding-model token — is a pure client-side string derivation
# (`nexus.corpus.effective_embedding_model_for_writes`), independent of
# whether any embedder actually runs. It is not lost: see
# tests/test_rdr_109_phase2_dispatch.py::
# test_effective_in_cloud_mode_delegates_to_canonical, which pins exactly
# that derivation with zero network/service dependency. A genuine full
# round-trip verification that a real chunk's stored metadata carries the
# cloud model name requires a live Voyage-backed service, which this suite
# does not (and should not) exercise.


def test_smart_index_staleness_check(
    rich_repo: Path, rich_registry: RepoRegistry, local_t3: T3Database,
) -> None:
    # PORTED (nexus-i711w): derive the rdr collection name BEFORE indexing —
    # the same resolution point the indexer itself uses. On a LIVE catalog,
    # _index registers the repo owner mid-run, so a post-run derivation
    # returns the owner-prefixed CONFORMANT name while this run's chunks
    # were written under the LEGACY name (first-index drift window; the
    # next-run convergence is _migrate_legacy_collections' subject, pinned
    # by the migration/tombstone-probe suites). The old pinned substrate
    # had no catalog at either point, which is why both derivations agreed.
    from nexus.indexer import _repo_collection_or_legacy
    rdr_col_name = _repo_collection_or_legacy(rich_repo, "rdr")

    _index(rich_repo, rich_registry, local_t3)
    info = rich_registry.get(rich_repo)
    counts = {}
    for key in ("code_collection", "docs_collection"):
        counts[key] = local_t3.get_or_create_collection(info[key]).count()
    _index(rich_repo, rich_registry, local_t3)
    for key, c in counts.items():
        assert local_t3.get_or_create_collection(info[key]).count() == c


# test_smart_index_git_metadata removed by RDR-101 Phase 5c (nexus-o6aa.13).
# The test asserted that git provenance ("commit", "branch") flows into
# the per-chunk ``git_meta`` JSON blob. Phase 5c removed ``git_meta``
# from ALLOWED_TOP_LEVEL — git provenance is now carried at the catalog
# Document level instead. A meaningful replacement would query the
# catalog for the document's git metadata; that's covered by the catalog
# Document tests rather than the indexer tests.


# ── Migration ─────────────────────────────────────────────────────────────────

def test_migration_moves_prose_from_code_to_docs(
    rich_repo: Path, tmp_path: Path, local_t3: T3Database,
) -> None:
    """RDR-102 D2 update: ``_prune_misclassified`` in ``indexer.py``
    keys on ``doc_id`` (the post-Phase-A canonical identity) when the
    file is in the catalog hook's ``file_to_doc_id`` map. With Phase
    B removing source_path from the chunk schema, the doc_id-keyed
    lookup is the only path that can find a misclassified chunk; the
    legacy source_path fallback returns nothing because chunks no
    longer carry source_path.

    Test flow:
      1. Index once — populates the catalog with the canonical
         README.md tumbler.
      2. Read the tumbler from the catalog.
      3. Seed a bad chunk into code__ with that tumbler as doc_id.
      4. Re-index — the doc_id-keyed _prune_misclassified must find
         and remove the seed chunk because README.md is in
         file_to_doc_id (catalog hook re-runs every index).

    nexus-sghyo PORT (2026-08-07): dropped the ``cloud_mode`` fixture.
    ``cloud_mode`` + this file's autouse ``_legacy_vector_backend``
    (NX_STORAGE_BACKEND_VECTORS=local) is the retired non-service-embed
    combination (indexer.py's CredentialsMissingError). The migration/
    prune behaviour under test does not depend on cloud vs local
    embedding — the ``"voyage-code-3"`` string below (kept, see the
    inline note) is a fabricated metadata literal for the seeded fake
    chunk, never derived from an actual embedder call, so nothing about
    this test's assertions changes by running in the suite's default
    local mode. Registered in ``_MODE_LINT_EXCLUDE_NODEIDS``
    (tests/conftest.py) as a "string-literal-as-name" exemption from the
    RDR-109 mode-declaration lint.
    """
    # nexus-i711w: no Catalog.init — ``_catalog_hook``'s local
    # is_initialized gate does not apply in service mode (indexer.py:933),
    # so the hook registers README.md in the per-test engine tenant.
    # PORT-VERIFY: the second _index below triggers the nexus-7vuw
    # legacy-rename migration — under the vector-local/catalog-service
    # split its data plane may 404 and fall back to the legacy name (see
    # module header); the final registry re-read is what absorbs that.
    reg = RepoRegistry(tmp_path / "repos.json")
    reg.add(rich_repo)
    info = reg.get(rich_repo)

    # Step 1 — first index populates the catalog with README's tumbler.
    _index(rich_repo, reg, local_t3)

    # Step 2 — read the tumbler that _catalog_hook assigned, via the
    # public reader (raw ``cat._db`` has no service-mode equivalent).
    readme_docs = documents_by_file_path("README.md")
    assert readme_docs, (
        "expected catalog to have README.md after first index"
    )
    readme_tumbler = str(readme_docs[0].tumbler)

    # Step 3 — seed a bad chunk with README's doc_id into code__.
    code_col = local_t3.get_or_create_collection(info["code_collection"])
    code_col.add(
        ids=["fake-prose-in-code"],
        documents=["Nexus is a semantic search system"],
        metadatas=[{
            "doc_id": readme_tumbler,
            "title": "README.md:chunk-0",
            "content_hash": "old-hash",
            "embedding_model": "voyage-code-3", "store_type": "code",
        }],
    )
    assert len(code_col.get(ids=["fake-prose-in-code"])["ids"]) == 1

    # Step 4 — re-index triggers _prune_misclassified, which uses
    # the doc_id-keyed where-filter to find the seed chunk and remove
    # it from code__ (README.md classifies as prose, so its doc_id
    # in code__ is misclassified).
    _index(rich_repo, reg, local_t3)

    assert len(code_col.get(ids=["fake-prose-in-code"])["ids"]) == 0, (
        "migration must prune the seed chunk via the doc_id-keyed "
        "_prune_misclassified path"
    )
    # nexus-7vuw: ``_migrate_legacy_collections`` may have renamed the
    # registry's path-derived ``docs_collection`` to the catalog-derived
    # conformant shape; re-read the registry to follow the rename.
    info = reg.get(rich_repo)
    assert any("README.md" in p for p in _get_sources(local_t3, info["docs_collection"]))


# ── Index-then-search (mini_repo) ────────────────────────────────────────────

@pytest.mark.parametrize("query,expected_file", [
    ("parse TTL days weeks permanent", "ttl.py"),
    ("session identifier process group ID", "session.py"),
    ("collection name prefix code docs knowledge", "corpus.py"),
])
def test_index_then_search(
    mini_repo: Path, registry: RepoRegistry, local_t3: T3Database,
    query: str, expected_file: str,
) -> None:
    _index(mini_repo, registry, local_t3)
    col = registry.get(mini_repo)["collection"]
    results = local_t3.search(query, [col], n_results=5)
    assert len(results) > 0
    # RDR-102 D2: source_path is gone; title carries the filename via
    # the "{relpath}:chunk-{i}" pattern (code_indexer.py:393).
    sources = [r.get("title", "") for r in results]
    assert any(expected_file in p for p in sources), f"{expected_file} not in: {sources}"


# ── CLI tests ─────────────────────────────────────────────────────────────────


def test_cli_index_repo(
    mini_repo: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    """nexus-5xn3k PORT (2026-08-07): this test used to route the vector
    write through the file's ``local_t3`` (a raw, DECOUPLED local
    EphemeralClient) while the catalog stayed on the real engine
    substrate (``_pin_t2_substrate``, autouse for the whole suite). The
    RUNFENCE completion gate (nexus-5xn3k, shipped e1884d4d) makes
    ``nx index repo`` fail loud whenever the engine's fail-closed
    completion verify cannot find the referenced chunks in the SAME
    substrate the catalog manifest points at
    (``write_manifest_many_complete_refused``, referenced==missing==
    chunk_count) — exactly this decoupled-substrate shape, by design
    (see the module docstring of
    tests/db/test_5xn3k_runfence_gate.py: "the fence read must come
    from the substrate the chunks actually landed in, never a
    decoupled double"). The fix is to stop decoupling: do not patch
    ``nexus.db.make_t3`` here and undo the file's autouse
    ``_legacy_vector_backend`` pin for this test only, so ``make_t3()``
    resolves its production default (``HttpVectorClient`` against
    ``NX_SERVICE_URL``/``NX_SERVICE_TOKEN`` — the SAME real engine
    instance the catalog already uses), landing real chunks where the
    fence actually looks.
    """
    from click.testing import CliRunner
    from nexus.cli import main
    monkeypatch.setenv("HOME", str(tmp_path))
    monkeypatch.delenv("NX_STORAGE_BACKEND_VECTORS", raising=False)
    runner = CliRunner()
    with patch("nexus.config.get_credential", side_effect=fake_credentials()):
        result = runner.invoke(main, ["index", "repo", str(mini_repo)])
    assert result.exit_code == 0, result.output
    assert "Done" in result.output


def test_cli_index_then_search(
    mini_repo: Path, tmp_path: Path, registry: RepoRegistry, monkeypatch: pytest.MonkeyPatch,
) -> None:
    """nexus-5xn3k PORT (2026-08-07): see ``test_cli_index_repo``'s
    docstring — the same decoupled-substrate/RUNFENCE incompatibility
    applied here too. Ported the same way: let ``make_t3()`` resolve
    the real engine for both the ``index`` and ``search`` CLI
    invocations, so the collection actually indexed is the one search
    reads from. The old hardcoded ``col_name`` assumed the LEGACY
    2-segment naming shape a raw client-embed run used to produce;
    against the real engine the collection is the CONFORMANT
    RDR-103 name the ``registry`` fixture already knows (matching the
    pattern ``test_index_then_search`` above uses).
    """
    from click.testing import CliRunner
    from nexus.cli import main
    monkeypatch.setenv("HOME", str(tmp_path))
    monkeypatch.delenv("NX_STORAGE_BACKEND_VECTORS", raising=False)
    runner = CliRunner()
    with patch("nexus.config.get_credential", side_effect=fake_credentials()):
        idx = runner.invoke(main, ["index", "repo", str(mini_repo)])
        assert idx.exit_code == 0, idx.output
        col_name = registry.get(mini_repo)["collection"]
        search = runner.invoke(main, ["search", "parse TTL expiry days", "--corpus", col_name, "--n", "5"])
    assert search.exit_code == 0, search.output
    assert len(search.output.strip()) > 0


def test_cli_index_frecency_only(
    mini_repo: Path, tmp_path: Path, local_t3: T3Database, monkeypatch: pytest.MonkeyPatch,
) -> None:
    from click.testing import CliRunner
    from nexus.cli import main
    monkeypatch.setenv("HOME", str(tmp_path))
    runner = CliRunner()
    with patch("nexus.db.make_t3", return_value=local_t3), \
         patch("nexus.config.get_credential", side_effect=fake_credentials()):
        runner.invoke(main, ["index", "repo", str(mini_repo)])
        result = runner.invoke(main, ["index", "repo", str(mini_repo), "--frecency-only"])
    assert result.exit_code == 0
    assert "Done" in result.output


# ── AC-E5: PDF routing ───────────────────────────────────────────────────────

def test_index_repository_pdf_routing(
    rich_repo: Path, rich_registry: RepoRegistry, local_t3: T3Database,
) -> None:
    """Verify PDF chunks land in docs__ collection. Phase 5c dropped
    ``store_type`` from chunk metadata; ``content_type=='pdf'`` is the
    canonical replacement (always set by ``make_chunk_metadata``).
    """
    _index(rich_repo, rich_registry, local_t3)
    docs_col = rich_registry.get(rich_repo)["docs_collection"]
    results = local_t3.search("Hello World test document PDF ingest", [docs_col], n_results=5)
    pdf_results = [r for r in results if r.get("content_type") == "pdf"]
    assert pdf_results, f"No PDF chunks; content_types: {[r.get('content_type') for r in results]}"
    assert isinstance(pdf_results[0]["title"], str)


def test_reindex_self_heals_missing_manifest(
    rich_repo: Path, local_t3: T3Database, tmp_path: Path,
) -> None:
    """nexus-c21fk (GH #1397 field find): the staleness check keys on T3
    chunk state only, so a doc whose chunks exist in T3 but whose
    document_chunks manifest rows are gone was PERMANENTLY skipped as
    'current' — chunk_count frozen at 0, every catalog-routed surface
    blind to it, and the documented advice ('re-index repairs it')
    structurally wrong. The exact bead repro: index, delete a doc's
    manifest rows + zero its chunk_count, re-index. The self-heal pass
    must rebuild the manifest from T3 WITHOUT re-embedding."""
    # nexus-i711w: no Catalog.init — the hook writes to the active (engine)
    # catalog in service mode. PORT-VERIFY: the second settling pass can
    # trip the nexus-7vuw rename's mode-split 404 fallback (module header).
    reg = RepoRegistry(tmp_path / "repos.json")
    reg.add(rich_repo)

    # RDR-194 P3d / catalog-029-manifest-chunk-fk.xml: local_t3 is an
    # in-memory test double (this file's own _legacy_vector_backend pin) —
    # its chunks never reach the real engine's nexus.chunks table the
    # running (service-mode) catalog's manifest-chunk FK checks against,
    # so the manifest-write hook that fires moments after each upsert
    # (SAME index_repository() call, chunk-then-manifest ordering) would
    # 409 per-doc and be silently swallowed (manifest_write_batch_hook is
    # best-effort: any failure logged, never propagated) -- the doc's
    # chunk_count/manifest never populate and this test's own precondition
    # (count_before > 0) never holds. Mirror every local_t3 write into the
    # real engine SYNCHRONOUSLY, as it happens -- a zero-vector
    # FK-satisfying stub (tests/_catalog_fixture_ops.seed_manifest_chunks's
    # idiom) landing before the manifest hook fires in the SAME call. This
    # does not touch local_t3's own read side (the self-heal pass below
    # still rebuilds the manifest FROM local_t3, matching this test's own
    # subject); it only makes the manifest WRITE the indexer already
    # attempts satisfiable for real.
    from tests._catalog_fixture_ops import seed_manifest_chunks  # noqa: PLC0415

    def _mirror_to_engine(collection, ids) -> None:  # noqa: ANN001
        if ids:
            seed_manifest_chunks(collection, list(ids))

    _orig_upsert_chunks = local_t3.upsert_chunks
    _orig_upsert_chunks_with_embeddings = local_t3.upsert_chunks_with_embeddings

    def _upsert_chunks_mirrored(collection, ids, *a, **kw):  # noqa: ANN001, ANN202
        result = _orig_upsert_chunks(collection, ids, *a, **kw)
        _mirror_to_engine(collection, ids)
        return result

    def _upsert_chunks_with_embeddings_mirrored(collection_name, ids, *a, **kw):  # noqa: ANN001, ANN202
        result = _orig_upsert_chunks_with_embeddings(collection_name, ids, *a, **kw)
        _mirror_to_engine(collection_name, ids)
        return result

    local_t3.upsert_chunks = _upsert_chunks_mirrored
    local_t3.upsert_chunks_with_embeddings = _upsert_chunks_with_embeddings_mirrored

    # TWO settling passes: the first index creates path-derived collection
    # names; the second triggers the nexus-7vuw legacy-rename migration,
    # which re-embeds everything and rewrites manifests — masking exactly
    # the bug under test.
    _index(rich_repo, reg, local_t3)
    _index(rich_repo, reg, local_t3)

    readme_docs = documents_by_file_path("README.md")
    assert readme_docs
    tumbler = str(readme_docs[0].tumbler)
    count_before = readme_docs[0].chunk_count
    assert count_before > 0
    cat = ActiveCatalog()
    manifest_before = cat.get_manifest(tumbler)
    assert len(manifest_before) > 0

    # Sabotage: the GH #1397 shape — manifest rows gone, chunk_count zeroed.
    # nexus-i711w: the raw ``DELETE FROM document_chunks`` + ``UPDATE``
    # injection has no service-mode SQL seam; the SAME shape is produced
    # through the whitelisted writer surface — an atomic manifest replace
    # with ZERO rows plus a zeroed chunk_count cache.
    # PORT-VERIFY: relies on /manifest/write's documented atomic
    # delete+insert semantics purging all rows when ``rows=[]``.
    cat.atomic_manifest_replace(
        tumbler, [], collection=readme_docs[0].physical_collection, new_chunk_count=0,
    )
    assert cat.get_manifest(tumbler) == [], (
        "sabotage precondition: manifest rows must be gone"
    )

    # Reproduce the FIELD configuration (docs 1.3.142-150, GH #1397): the
    # stuck population's chunks carry INLINE doc_id metadata (the RDR-101
    # Phase-4-era write shape), which hits the staleness cache's by_doc_id
    # index directly — a hit that never consults the manifest, so the doc
    # is skipped as "current" forever. Without this stamp the fixture's
    # chunks (no inline doc_id) resolve doc_id THROUGH the manifest
    # (nexus-0ocy chash fallback), whose absence makes them look stale and
    # self-heal by re-embedding — masking the bug (verified: this test
    # passed vacuously before the stamp).
    info = reg.get(rich_repo)
    docs_col = local_t3.get_or_create_collection(info["docs_collection"])
    got = docs_col.get(include=["metadatas"])
    readme_ids = [
        cid for cid, m in zip(got["ids"], got["metadatas"])
        if "README.md" in (m or {}).get("title", "")
    ]
    assert readme_ids
    docs_col.update(
        ids=readme_ids,
        metadatas=[{"doc_id": str(tumbler)} for _ in readme_ids],
    )

    # Snapshot T3 so we can prove the heal never re-embedded anything.
    ids_before = sorted(docs_col.get()["ids"])

    # README.md is unchanged on disk -> staleness skips it (by_doc_id hit);
    # only the new self-heal pass can repair the manifest.
    _index(rich_repo, reg, local_t3)

    healed = active_reader().get_manifest(tumbler)
    healed_entry = active_reader().resolve(tumbler)
    assert healed_entry is not None
    count_after = healed_entry.chunk_count

    assert [r.chash for r in healed] == [r.chash for r in manifest_before], (
        "self-heal must rebuild the exact manifest from T3 chunks"
    )
    assert count_after == len(healed) > 0, (
        "chunk_count must resync from the healed manifest"
    )
    assert sorted(docs_col.get()["ids"]) == ids_before, (
        "the heal must NOT re-embed (T3 chunk set unchanged)"
    )


def test_manifest_self_heal_runs_after_indexing_before_prunes(
    rich_repo: Path, local_t3: T3Database, tmp_path: Path,
) -> None:
    """Critique 4711f521 Critical: the heal's PLACEMENT is load-bearing.
    It must run AFTER per-file indexing (so gaps created by this run's own
    manifest-write hook are healed too) and BEFORE the prune passes (so the
    manifest-keyed GC — the nexus-mr89x hazard — never sees an unhealed
    gap). Pin the call order."""
    import nexus.indexer as indexer_mod  # noqa: PLC0415 — file pattern: deferred imports
    from nexus.catalog import manifest_heal as heal_mod  # noqa: PLC0415 — file pattern: deferred imports

    # nexus-i711w: no Catalog.init — service mode needs no local dir; the
    # heal pass keys on the owner the hook registers in this same run.
    reg = RepoRegistry(tmp_path / "repos.json")
    reg.add(rich_repo)

    order: list[str] = []
    real_heal = heal_mod.heal_manifest_gaps
    real_prune = indexer_mod._prune_deleted_files
    real_index_prose = indexer_mod._index_prose_file

    def spy_heal(*a, **k):
        order.append("heal")
        return real_heal(*a, **k)

    def spy_prune(*a, **k):
        order.append("prune")
        return real_prune(*a, **k)

    def spy_prose(*a, **k):
        if "index" not in order:
            order.append("index")
        return real_index_prose(*a, **k)

    with patch.object(heal_mod, "heal_manifest_gaps", side_effect=spy_heal), \
            patch.object(indexer_mod, "_prune_deleted_files", side_effect=spy_prune), \
            patch.object(indexer_mod, "_index_prose_file", side_effect=spy_prose):
        _index(rich_repo, reg, local_t3)

    assert "heal" in order and "prune" in order and "index" in order
    assert order.index("index") < order.index("heal") < order.index("prune"), (
        f"self-heal must run after per-file indexing and before the prunes; got {order}"
    )
