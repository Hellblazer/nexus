# SPDX-License-Identifier: AGPL-3.0-or-later
"""nexus-rqsh1 acceptance: the indexer must NOT register a catalog document
for a file it will not chunk (Hal directive, 2026-08-15).

Reproduces the exact phantom population found in the nexus repo's own
catalog (nexus-rqsh1 bead description): a zero-byte ``__init__.py`` (CODE
classification, ``chunk_file`` returns ``[]`` -> 0 chunks) and a binary
file with an extension unknown to ``classify_file``'s tables, which falls
through to the PROSE default and would otherwise fail ``read_text`` ->
0 chunks. Both used to still register a catalog document via the
pre-index registration pass (``indexer.py`` ``indexed_for_catalog``,
built from the discovery lists regardless of indexing outcome), minting
a ``chunk_count=0`` document a re-index can never clear.

This is an end-to-end pipeline test (real engine-backed catalog via the
suite's autouse ``t2_service_env`` substrate, real local T3 vectors) --
the fix lives in the discovery/classification loop deep inside
``_run_index``, not in any independently unit-testable helper, so the
non-vacuity assertion has to run the actual ``index_repository()`` entry
point and inspect the resulting catalog rows.

Non-vacuity: every assertion here is a negative claim about a document
that pre-fix code DOES register (``_run_index`` appended it to
``code_files``/``prose_files`` unconditionally, which fed straight into
``indexed_for_catalog``) -- so this test is red on the pre-fix tree by
construction, not merely green-by-omission. See the paired positive
assertions (real files still register) so an over-broad fix that drops
everything cannot pass either.
"""
from __future__ import annotations

import subprocess
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest
from nexus.db.minilm_direct import MiniLMDirectEmbeddingFunction as DefaultEmbeddingFunction

from nexus.db.t3 import T3Database
from nexus.registry import RepoRegistry
from tests._catalog_fixture_ops import documents_by_file_path
from tests.conftest import fake_credentials, make_vector_test_client

pytestmark = [pytest.mark.integration]


def _git_init(repo: Path) -> None:
    for cmd in (
        ["git", "init", "-b", "main"],
        ["git", "config", "user.email", "test@nexus"],
        ["git", "config", "user.name", "Nexus Test"],
        ["git", "add", "."],
        ["git", "commit", "-m", "seed"],
    ):
        subprocess.run(cmd, cwd=repo, check=True, capture_output=True)


@pytest.fixture(autouse=True)
def _pin_fake_voyage_key(monkeypatch):
    # Same routing pin as test_indexer_e2e.py: the embedder branches on
    # get_credential("voyage_api_key") *presence*, and voyageai.Client is
    # mocked below so no real call is ever made.
    monkeypatch.setenv("VOYAGE_API_KEY", "fake-key-for-routing-only")


@pytest.fixture(autouse=True)
def _legacy_vector_backend(monkeypatch):
    # Client-embed path into the local vector fixture, not the server-side
    # empty-placeholder service path (see test_indexer_e2e.py's identical
    # fixture for the full rationale).
    monkeypatch.setenv("NX_STORAGE_BACKEND_VECTORS", "local")


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


@pytest.fixture
def phantom_repo(tmp_path: Path) -> Path:
    """Seeds the exact rqsh1 phantom population plus real control files.

    - ``pkg/__init__.py``: zero bytes. CODE classification (``.py``
      extension). ``code_indexer.py``'s ``chunk_file`` returns ``[]`` on
      empty content -> 0 chunks (the 9-file nexus population).
    - ``data/fixture.blobx``: content with an embedded NUL byte and no
      recognized code/skip/binary-asset extension -> falls through
      ``classify_file``'s step-8 PROSE default, then fails
      ``read_text(encoding="utf-8")`` -> 0 chunks (the nexus .npz/.bundle
      population). Deterministic bytes, no randomness (CLAUDE.md).
    - ``src/real_code.py``: real, non-empty code -> must still chunk and
      register (positive control).
    - ``docs/real_prose.md``: real, non-empty prose -> must still chunk
      and register (positive control).
    """
    repo = tmp_path / "phantom-repo"
    repo.mkdir()

    empty_init = repo / "pkg" / "__init__.py"
    empty_init.parent.mkdir(parents=True)
    empty_init.write_bytes(b"")

    binary_npz = repo / "data" / "fixture.blobx"
    binary_npz.parent.mkdir(parents=True)
    binary_npz.write_bytes(bytes(range(256)) * 4)  # deterministic, has \x00

    real_code = repo / "src" / "real_code.py"
    real_code.parent.mkdir(parents=True)
    real_code.write_text(
        "def greet(name: str) -> str:\n    return f'hello {name}'\n",
        encoding="utf-8",
    )

    real_prose = repo / "docs" / "real_prose.md"
    real_prose.parent.mkdir(parents=True)
    real_prose.write_text(
        "# Real Document\n\nThis is genuine prose content for indexing.\n",
        encoding="utf-8",
    )

    _git_init(repo)
    return repo


@pytest.fixture
def local_t3() -> T3Database:
    return T3Database(
        _client=make_vector_test_client(), _ef_override=DefaultEmbeddingFunction()
    )


@pytest.fixture
def registry(tmp_path: Path, phantom_repo: Path) -> RepoRegistry:
    reg = RepoRegistry(tmp_path / "repos.json")
    reg.add(phantom_repo)
    return reg


def _index(repo: Path, registry: RepoRegistry, t3: T3Database, **kw) -> None:
    from nexus.indexer import index_repository
    with patch("nexus.db.make_t3", return_value=t3), \
         patch("nexus.config.get_credential", side_effect=fake_credentials()):
        index_repository(repo, registry, **kw)


@pytest.fixture
def phantom_repo_with_rdr(tmp_path: Path) -> Path:
    """nexus-rqsh1 round 2 (substantive-critic Critical, 2026-08-17): the
    ``rdr_md_paths`` walk (indexer.py, feeds ``indexed_for_catalog``
    alongside code/prose/pdf) is a SEPARATE discovery pass from
    ``candidate_files`` and had no zero-byte guard of its own -- a
    zero-byte RDR ``.md`` file still minted a phantom catalog document
    even after the candidate_files-loop fix.
    """
    repo = tmp_path / "phantom-repo-rdr"
    repo.mkdir()

    empty_rdr = repo / "docs" / "rdr" / "rdr-empty.md"
    empty_rdr.parent.mkdir(parents=True)
    empty_rdr.write_bytes(b"")

    real_rdr = repo / "docs" / "rdr" / "rdr-real.md"
    real_rdr.write_text(
        "---\nstatus: accepted\n---\n\n# RDR: Real Decision\n\nGenuine content.\n",
        encoding="utf-8",
    )

    _git_init(repo)
    return repo


@pytest.fixture
def registry_rdr(tmp_path: Path, phantom_repo_with_rdr: Path) -> RepoRegistry:
    reg = RepoRegistry(tmp_path / "repos-rdr.json")
    reg.add(phantom_repo_with_rdr)
    return reg


class TestRdrWalkUnchunkableFilesNeverRegister:
    """nexus-rqsh1 round 2: the rdr_md_paths walk gets the same zero-byte
    guard as candidate_files. RDR files are markdown text by
    construction (unlike the extension-agnostic candidate_files walk,
    which can encounter arbitrary binary content), so a zero-byte skip
    is the whole fix -- no binary-content sniff is needed here."""

    def test_zero_byte_rdr_file_gets_no_catalog_document(
        self, phantom_repo_with_rdr: Path, registry_rdr: RepoRegistry, local_t3: T3Database,
    ) -> None:
        _index(phantom_repo_with_rdr, registry_rdr, local_t3)
        assert documents_by_file_path("docs/rdr/rdr-empty.md") == [], (
            "a zero-byte RDR file was registered as a catalog document even "
            "though it can never produce a chunk -- nexus-rqsh1 round-2 regression"
        )

    def test_real_rdr_file_still_registers(
        self, phantom_repo_with_rdr: Path, registry_rdr: RepoRegistry, local_t3: T3Database,
    ) -> None:
        _index(phantom_repo_with_rdr, registry_rdr, local_t3)
        docs = documents_by_file_path("docs/rdr/rdr-real.md")
        assert docs, "a real, chunkable RDR file must still register a catalog document"


class TestUnchunkableFilesNeverRegister:
    def test_zero_byte_file_gets_no_catalog_document(
        self, phantom_repo: Path, registry: RepoRegistry, local_t3: T3Database
    ) -> None:
        _index(phantom_repo, registry, local_t3)
        assert documents_by_file_path("pkg/__init__.py") == [], (
            "a zero-byte file was registered as a catalog document even "
            "though it can never produce a chunk -- nexus-rqsh1 regression"
        )

    def test_binary_content_file_gets_no_catalog_document(
        self, phantom_repo: Path, registry: RepoRegistry, local_t3: T3Database
    ) -> None:
        _index(phantom_repo, registry, local_t3)
        assert documents_by_file_path("data/fixture.blobx") == [], (
            "a binary file misclassified as prose (unknown extension, "
            "classify_file step-8 default) was registered as a catalog "
            "document even though read_text() can never decode it -- "
            "nexus-rqsh1 regression"
        )

    def test_real_code_file_still_registers(
        self, phantom_repo: Path, registry: RepoRegistry, local_t3: T3Database
    ) -> None:
        _index(phantom_repo, registry, local_t3)
        docs = documents_by_file_path("src/real_code.py")
        assert docs, "a real, chunkable code file must still register a catalog document"

    def test_real_prose_file_still_registers(
        self, phantom_repo: Path, registry: RepoRegistry, local_t3: T3Database
    ) -> None:
        _index(phantom_repo, registry, local_t3)
        docs = documents_by_file_path("docs/real_prose.md")
        assert docs, "a real, chunkable prose file must still register a catalog document"

    def test_real_files_carry_chunks_in_t3(
        self, phantom_repo: Path, registry: RepoRegistry, local_t3: T3Database
    ) -> None:
        """Belt-and-suspenders: confirm the positive controls actually
        chunked (not just registered), so a fix that also broke real-file
        chunking wouldn't be masked by the catalog-only assertions above."""
        _index(phantom_repo, registry, local_t3)
        code_col_name = registry.get(phantom_repo)["code_collection"]
        docs_col_name = registry.get(phantom_repo)["docs_collection"]
        code_col = local_t3.get_or_create_collection(code_col_name)
        docs_col = local_t3.get_or_create_collection(docs_col_name)
        assert code_col.count() > 0
        assert docs_col.count() > 0
