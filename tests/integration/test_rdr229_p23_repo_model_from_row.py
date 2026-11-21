# SPDX-License-Identifier: AGPL-3.0-or-later
"""RDR-229 P2.3 (nexus-p2m1x.8): ``nx index repo`` takes its collections' target model from the
collection's catalog row, with the three outcomes ``nx index md`` / ``pdf`` already have.

The engine embeds by the registry row, not by the collection name. A rename keeps the source row's
model under the new name, so a conformant name's model token and its row can disagree. The repo
indexer used to read the token (``index_model_for_collection``), so it stamped chunks with a model
that did not embed them, compared the unchanged-content skip against that model, and registered the
row's model back with the token (the engine refuses a registration that disagrees with its profile
unless the row holds the model: ``EmbeddingProfileMismatchError``).

Outcomes, as ``doc_indexer._target_model`` names them:

* Row: the row's ``embedding_model``.
* NoRow: no row (a new collection) or a row with no model: the calibrated name derivation.
* Unreadable: the row read failed. The name's model answers, flagged as a guess, so the skip lets
  the content hash decide instead of judging the stored stamp against the guess.

Real engine (the suite-wide substrate, a local bge-768 profile), the real ``_run_index`` and the
real ``index_markdown``. A row whose model differs from its name's token is made the way a rename
leaves one: ``register_collection`` with the row's model under the other name (the engine accepts
it; a rename since nexus-ucagi would not). Requests are counted at the transport (the vector
client's ``_request_once`` and ``httpx.Client.send``), never by stubbing the row helper.

Integration-marked and not an RDR-192 pin file (``tests/test_rdr192_pins_not_integration_marked_lint.py``).
"""
from __future__ import annotations

import subprocess
import urllib.error
import uuid
from collections import Counter
from contextlib import contextmanager
from pathlib import Path
from typing import Iterator

import httpx
import pytest

import nexus.db.http_vector_client as hvc
import nexus.indexer as idx
import nexus.mcp_infra as mi
from nexus.corpus import discard_cached_registration, ensure_collection_registered
from nexus.db.http_vector_client import HttpVectorClient
from nexus.db.local_ef import LocalEmbeddingFunction
from nexus.doc_indexer import index_markdown
from nexus.registry import RepoRegistry

pytestmark = [pytest.mark.integration]

#: The substrate engine's profile model: the one a registration may hold on this engine.
ROW_MODEL = "bge-base-en-v15-768"
#: A model token a name may carry that is not the row's: the rename-left disagreement.
TOKEN_MODEL = "voyage-code-3"
DOCS_TOKEN_MODEL = "voyage-context-3"
#: A stamp that is neither the row's model nor either token.
OTHER_MODEL = "minilm-l6-v2-384"


# ── fixtures and helpers ──────────────────────────────────────────────────────


@pytest.fixture(autouse=True)
def _identity_and_cache(monkeypatch: pytest.MonkeyPatch) -> Iterator[None]:

    # The Phase-4 legacy-name migration (nexus-yqnr.6) moves a repo whose owner is registered onto the
    # collection names the catalog mints, on the run after the owner registers: every collection in
    # these tests would move on its second run, and a minted name always carries the engine's own
    # model token, so it could never disagree with its row. It is its own subject; the collections
    # here are the ones the registry names.
    monkeypatch.setattr(idx, "_migrate_legacy_collections", lambda *a, **k: {})

    for k in ("GIT_AUTHOR_NAME", "GIT_COMMITTER_NAME"):
        monkeypatch.setenv(k, "Test")
    for k in ("GIT_AUTHOR_EMAIL", "GIT_COMMITTER_EMAIL"):
        monkeypatch.setenv(k, "test@test.invalid")
    mi.invalidate_collections_cache()
    yield
    mi.invalidate_collections_cache()


def _git(repo: Path, *args: str) -> None:
    subprocess.run(["git", *args], cwd=repo, check=True, capture_output=True)


def _make_repo(root: Path, files: dict[str, str]) -> Path:
    repo = root / f"repo-{uuid.uuid4().hex[:8]}"
    repo.mkdir()
    for rel, text in files.items():
        (repo / rel).parent.mkdir(parents=True, exist_ok=True)
        (repo / rel).write_text(text, encoding="utf-8")
    _git(repo, "init", "-q")
    _git(repo, "add", ".")
    _git(repo, "commit", "-q", "-m", "seed")
    return repo


def _add_files(repo: Path, files: dict[str, str]) -> None:
    for rel, text in files.items():
        (repo / rel).parent.mkdir(parents=True, exist_ok=True)
        (repo / rel).write_text(text, encoding="utf-8")
    _git(repo, "add", ".")
    _git(repo, "commit", "-q", "-m", "more")


def _unique() -> str:
    return "p23" + uuid.uuid4().hex[:8]


def _register_row(name: str, content_type: str, model: str) -> None:
    """Register *name*'s row holding *model*, through the same call the write paths use (so the
    process registration cache now says the name is registered)."""

    ensure_collection_registered(name, kwargs={
        "content_type": content_type, "owner_id": "1.9999",
        "embedding_model": model, "model_version": "v1",
    })


def _forget_registration(name: str) -> None:
    """Make the next ``ensure_collection_registered`` run for real, as in a fresh process."""

    discard_cached_registration(name)


def _run(repo: Path, tmp_path: Path, names: dict[str, str], *, force: bool = False) -> dict:
    """The real ``nx index repo`` run, with the repo's registry entry naming *names*' collections:
    the names the run resolves, so a test can give a row its own model before the run reads it."""

    reg = RepoRegistry(tmp_path / "repos.json")
    if reg.get(repo) is None:
        reg.add(repo)
        reg.update(repo, collection=names["code"], code_collection=names["code"], docs_collection=names["docs"])
    return idx._run_index(repo, reg, force=force)


def _chunk_models(collection: str) -> Counter:
    """``embedding_model`` of every chunk in *collection*, as a counter."""

    got = HttpVectorClient().get_or_create_collection(collection).get(include=["metadatas"], limit=500, sensitivity="include")
    return Counter((m or {}).get("embedding_model", "") for m in got["metadatas"])


def _restamp(collection: str, model: str) -> int:
    """Stamp every chunk of *collection* with *model* (a metadata-only update); returns how many."""

    client = HttpVectorClient()
    ids = client.get_or_create_collection(collection).get(include=[], limit=500, sensitivity="include")["ids"]
    client.update_chunks(collection, ids, [{"embedding_model": model}] * len(ids))
    return len(ids)


class _Traffic:
    """Every request the process sends the engine, counted at the transport."""

    def __init__(self) -> None:
        self.log: list[tuple[str, str, str]] = []

    def listing(self) -> int:
        """The collection listings (the row source): the vector client's routing / stats reads."""
        return sum(1 for kind, _m, p in self.log if kind == "vec" and p.startswith("/v1/vectors/stats"))

    def total(self) -> int:
        return len(self.log)


@contextmanager
def _traffic(monkeypatch: pytest.MonkeyPatch, *, listing_outage: bool = False) -> Iterator[_Traffic]:
    """Record every vector-client request (``_request_once``) and every httpx request. With
    *listing_outage* a collection listing answers HTTP 500, the failure of the row read, and nothing
    else is touched."""


    seen = _Traffic()
    real_once = hvc._request_once
    real_send = httpx.Client.send

    def once(method, path, **kw):
        seen.log.append(("vec", method, path))
        if listing_outage and path.startswith(("/v1/vectors/stats", "/v1/vectors/collections")):
            raise urllib.error.HTTPError(path, 500, "listing unreachable", None, None)  # type: ignore[arg-type]
        return real_once(method, path, **kw)

    def send(self, request, **kw):
        seen.log.append(("httpx", request.method, request.url.path))
        return real_send(self, request, **kw)

    with monkeypatch.context() as m:
        m.setattr(hvc, "_request_once", once)
        m.setattr(httpx.Client, "send", send)
        yield seen


CODE = {"a.py": "def a():\n    return 'p23 alpha'\n", "b.py": "def b():\n    return 'p23 beta'\n"}
PROSE = {"notes.md": "# Notes\n\nProse that p23 indexes next to the code.\n"}


def _names(u: str, *, code_token: str = ROW_MODEL, docs_token: str = ROW_MODEL) -> dict[str, str]:
    return {
        "code": f"code__{u}__{code_token}__v1",
        "docs": f"docs__{u}__{docs_token}__v1",
    }


# ── 1. the row's model, not the name's token ─────────────────────────────────


def _seeded_divergent_repo(tmp_path: Path) -> tuple[Path, dict[str, str]]:
    """A repo already indexed once into collections whose rows hold the engine's bge model under names
    that say ``voyage-*``, as a rename leaves them, with chunks in both (the listing the row comes from
    carries only collections that hold a chunk, so a collection renamed WITH its data is visible and an
    empty one still follows its name: ``embedding_model_row_preferred``'s docstring). The seed run reads
    no row for the empty collections, so it stamps the name's token."""
    names = _names(_unique(), code_token=TOKEN_MODEL, docs_token=DOCS_TOKEN_MODEL)
    _register_row(names["code"], "code", ROW_MODEL)
    _register_row(names["docs"], "docs", ROW_MODEL)
    repo = _make_repo(tmp_path, {**CODE, **PROSE})
    assert _run(repo, tmp_path, names)["files_changed"] == 3
    assert set(_chunk_models(names["code"])) and set(_chunk_models(names["docs"])), "non-vacuity: seeded"
    return repo, names


def test_repo_chunks_are_stamped_with_the_rows_model_when_it_differs_from_the_names_token(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The names say ``voyage-*``; both rows hold the engine's bge model. The engine embeds with the
    row, so a run that writes new files stamps them with the row's model, and re-indexes the seed
    files, stamped with the token, to it."""
    repo, names = _seeded_divergent_repo(tmp_path)
    _add_files(repo, {"new.py": "def n():\n    return 'p23 new'\n", "new.md": "# New\n\nP23 new prose.\n"})

    stats = _run(repo, tmp_path, names)

    assert set(_chunk_models(names["code"])) == {ROW_MODEL}
    assert set(_chunk_models(names["docs"])) == {ROW_MODEL}
    assert stats["files_changed"] == 5, "the two new files, and the three the seed run stamped with the token"


def test_the_unchanged_content_skip_compares_against_the_rows_model(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Chunks stamped with the row's model are current: a second run writes nothing. Chunks stamped
    with any other model are not: the same run indexes the files again. Both halves, so the skip is
    shown to compare against the row's model and not to skip everything."""
    repo, names = _seeded_divergent_repo(tmp_path)

    # Whatever the seed run stamped, the row's model is what is stored now.
    assert _restamp(names["code"], ROW_MODEL) > 0 and _restamp(names["docs"], ROW_MODEL) > 0
    assert _run(repo, tmp_path, names)["files_changed"] == 0, "stamped with the row's model: current"

    # The name's token is not the row's model: chunks carrying it are stale.
    assert _restamp(names["code"], TOKEN_MODEL) > 0 and _restamp(names["docs"], DOCS_TOKEN_MODEL) > 0
    assert _run(repo, tmp_path, names)["files_changed"] == 3, "stamped with another model: indexed again"


def test_the_registration_the_run_makes_keeps_a_rows_model_that_differs_from_the_names_token(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    """In a fresh process the run registers each collection before it indexes. It used to register the
    name's token, which the engine's profile refuses (``EmbeddingProfileMismatchError``) unless the
    row holds it. It registers the model it resolved, the row's."""
    repo, names = _seeded_divergent_repo(tmp_path)
    _forget_registration(names["code"])
    _forget_registration(names["docs"])
    _add_files(repo, {"new.py": "def n():\n    return 'p23 new'\n", "new.md": "# New\n\nP23 new prose.\n"})

    stats = _run(repo, tmp_path, names)

    assert stats["files_changed"] == 5
    assert set(_chunk_models(names["code"])) == {ROW_MODEL}


def test_a_new_collection_with_no_row_takes_the_names_model(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    """NoRow: nothing registers the collection before the run, so no row backs it and the name's own
    token answers. The token is the engine's profile model: a row-less collection is registered by the
    run with the model it resolved, and the engine refuses a registration that is not its profile model,
    so no other token can be tested on this engine. The test still tells the token from the local
    embedder's model (the Python EF reports another one here), which is what a NoRow answered from the
    embedder would stamp."""
    u = _unique()
    assert LocalEmbeddingFunction().model_name != ROW_MODEL, "non-vacuity: the embedder's model is not the token"
    names = _names(u)
    repo = _make_repo(tmp_path, {**CODE, **PROSE})

    assert _run(repo, tmp_path, names)["files_changed"] == 3
    assert set(_chunk_models(names["code"])) == {ROW_MODEL}
    assert set(_chunk_models(names["docs"])) == {ROW_MODEL}
    assert _run(repo, tmp_path, names)["files_changed"] == 0, "and a second run skips them"


def test_an_unreadable_row_neither_fails_the_run_nor_reindexes_an_unchanged_file(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Unreadable: the listing the row comes from answers HTTP 500. The run goes on with the name's
    model flagged as a guess, so a file whose chunks are stamped with another model (the row's, here
    a model the name's token does not carry) and whose content has not changed is not indexed again."""
    repo, names = _seeded_divergent_repo(tmp_path)
    assert _restamp(names["code"], ROW_MODEL) > 0 and _restamp(names["docs"], ROW_MODEL) > 0


    mi.invalidate_collections_cache()
    with _traffic(monkeypatch, listing_outage=True) as seen:
        stats = _run(repo, tmp_path, names)

    assert seen.listing() > 0, "non-vacuity: the run did ask for the listing, and it failed"
    assert stats["files_changed"] == 0
    assert set(_chunk_models(names["code"])) == {ROW_MODEL}, "and nothing was re-stamped with the guess"


# ── 2. Critical Assumption 2: no extra round trip per document ────────────────

#: How many more collection listings FOUR more documents may cost a run than two, over and above the
#: baseline. Measured on develop 63825559a BEFORE the change, when the run read no row: 1 or 2 more (eight
#: runs; a flush registers its collection, which invalidates the shared collections cache, so the cache
#: refills, and how many times depends on the flush grouping). The row read is one read of that cache per
#: run, never one per document: a read per document would add FOUR to the baseline, so a margin of 3 over
#: the small run's count catches it with room on both sides. The total request count is not pinned: it
#: moves with every unrelated write-path change and says nothing about the row read.
LISTING_MARGIN_FOR_FOUR_DOCS: int = 3


def _doc_set(n: int, tag: str) -> dict[str, str]:
    out: dict[str, str] = {}
    for i in range(n):
        if i % 2 == 0:
            out[f"{tag}{i}.py"] = f"def f{tag}{i}():\n    return 'p23 {tag} code {i} {uuid.uuid4().hex}'\n"
        else:
            out[f"{tag}{i}.md"] = f"# Doc {tag} {i}\n\nP23 {tag} prose {i} {uuid.uuid4().hex}.\n"
    return out


def _requests_for_run(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, *, n_docs: int, existing: bool,
) -> _Traffic:
    """The requests ONE run sends to index *n_docs* new documents, in a repo whose collections already
    exist (*existing*: a seed run created them) or do not (no row yet)."""
    u = _unique()
    names = _names(u)
    repo = _make_repo(tmp_path, {"seed.py": f"def seed():\n    return '{u}'\n", "seed.md": f"# Seed\n\n{u}\n"})
    if existing:
        _run(repo, tmp_path, names)
        _add_files(repo, _doc_set(n_docs, "x"))
    else:
        # The collections do not exist yet: index only the new documents, none of the seed.
        for rel in ("seed.py", "seed.md"):
            (repo / rel).unlink()
        _git(repo, "add", "-A")
        _git(repo, "commit", "-q", "-m", "drop seed")
        _add_files(repo, _doc_set(n_docs, "x"))

    mi.invalidate_collections_cache()
    with _traffic(monkeypatch) as seen:
        stats = _run(repo, tmp_path, names)
    assert stats["files_changed"] == n_docs
    return seen


@pytest.mark.parametrize("existing", [True, False], ids=["existing-collection", "new-collection-no-row"])
def test_the_row_read_adds_no_request_per_document(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, existing: bool,
) -> None:
    """Critical Assumption 2. The run reads each collection's row from the shared collections cache, once
    per run, not once per document. Measured at the transport, on a collection that exists and on one
    with no row yet: the collection listings do not grow with the documents (``nx index md``, the
    per-document path, is measured in ``test_rdr229_p23_write_path_gaps.py``)."""
    small = _requests_for_run(tmp_path, monkeypatch, n_docs=2, existing=existing)
    large = _requests_for_run(tmp_path, monkeypatch, n_docs=6, existing=existing)
    assert large.listing() <= small.listing() + LISTING_MARGIN_FOR_FOUR_DOCS, (
        f"listings grew from {small.listing()} (two documents) to {large.listing()} (six): "
        "the row is read per document")


# ── 3. a listing outage during nx index md ────────────────────────────────────


def test_a_listing_outage_neither_fails_nx_index_md_nor_reindexes_an_unchanged_file(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    """``nx index md`` already had all three outcomes (``doc_indexer._target_model``). This is the pin.
    A file is indexed; its chunks then carry a stamp other than the name's model (the row's model, which
    the failed read cannot supply); the listing goes down: the write neither fails nor indexes the file
    again, and nothing is re-stamped with the name's guess. With the listing up the same stamp is stale
    (the control), so the skip is the unreadable arm's and not a vacuous one."""

    name = f"docs__{_unique()}__{ROW_MODEL}__v1"
    md = tmp_path / "outage.md"
    md.write_text("# Outage\n\nP23 unchanged prose that the listing outage must not re-index.\n", encoding="utf-8")

    assert index_markdown(md, corpus="p23md", t3=HttpVectorClient(), collection_name=name) > 0
    assert set(_chunk_models(name)) == {ROW_MODEL}
    assert _restamp(name, OTHER_MODEL) > 0

    mi.invalidate_collections_cache()
    with _traffic(monkeypatch, listing_outage=True) as seen:
        written = index_markdown(md, corpus="p23md", t3=HttpVectorClient(), collection_name=name)
    assert seen.listing() > 0, "non-vacuity: the write asked for the listing, and it failed"
    assert written == 0
    assert set(_chunk_models(name)) == {OTHER_MODEL}, "nothing was re-stamped with the name's guess"

    mi.invalidate_collections_cache()
    assert index_markdown(md, corpus="p23md", t3=HttpVectorClient(), collection_name=name) > 0, (
        "control: with the listing up, a stamp other than the row's model is stale")
