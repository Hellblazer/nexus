# SPDX-License-Identifier: AGPL-3.0-or-later
"""RDR-223 P2.4 (nexus-z0o2p.14): the ``nx index repo`` oversize fallbacks against the REAL engine.

A file that alone exceeds one ChunkBatcher batch is refused by ``ChunkBatcher.add`` and falls
through to the per-file path of its indexer: ``index_code_file`` (code, over the code cap),
``index_prose_file`` (prose and RDR, over the CCE cap) and ``_index_pdf_file`` (PDF). Each used
to write its chunks with ``upsert-chunks`` and its owner rows in a later, separate manifest
write, so a client that died between the two left chunks nobody owned. They now write through
``MultiBatchDocumentWriter``: chunks and owner rows in the same request.

Every journey runs once per fallback (code, prose, rdr, pdf) on the shared engine substrate,
with a REAL ``ChunkBatcher`` so ``add()`` itself refuses the file, and a ``ctx.db`` that fails
the test on any use, so an ``upsert-chunks`` call on the path is a red test rather than a
silently different topology. The journeys:

* a small file still stages in the batcher and never reaches the writer;
* the full run leaves a manifest and chunk set equal to ONE combined write of the same file;
* the client dies after the first request: no chunk that request wrote is without an owner;
* re-indexing an unchanged oversize file (``--force``, so staleness does not skip it) embeds
  nothing and sweeps nothing;
* re-indexing an edited oversize file sweeps only what the edit dropped, after the last batch;
* the post-store hooks still fire once for the whole file, and the manifest hook is not among
  them (the writer already wrote the manifest).

"Owner" means a ``catalog_document_chunks`` row of the document.
"""
from __future__ import annotations

import hashlib
import uuid
from contextlib import contextmanager
from pathlib import Path
from typing import Callable, Iterator

import pytest

from nexus.catalog.http_catalog_client import HttpCatalogClient
from nexus.chunk_batcher import ChunkBatcher
from nexus.db.http_vector_client import HttpVectorClient
from nexus.hook_registry import HookRegistry
from nexus.index_context import IndexContext

pytestmark = [pytest.mark.integration]

_MODEL = "bge-base-en-v15-768"
_NOW = "2026-09-30T00:00:00"
#: The most chunks one combined-write request carries in these journeys (the writer's cap) and
#: the most the ChunkBatcher stages for one file. A file over the second is the oversize case.
_WRITER_CAP = 10
_BATCHER_CAP = 8
_DATA_PATHS = ("/manifest/write_many", "/manifest/append")


class ClientDied(Exception):
    """The simulated death of the client process between two requests."""


class _ForbiddenDb(HttpVectorClient):
    """``ctx.db`` of the fallback under test: a service-backed T3 (an ``HttpVectorClient``, so the
    fallback picks the writer path) that fails the test on any use. The fallback writes through the
    combined writer; any use of this object is the retired split write (``upsert-chunks`` then a
    manifest write)."""

    def __init__(self) -> None:          # no client is built: nothing here may reach the engine
        pass

    def __getattribute__(self, name: str):
        if name.startswith("__"):
            return object.__getattribute__(self, name)
        raise AssertionError(
            f"the oversize fallback used ctx.db.{name}: chunks must be written by the combined "
            "writer, in the same request as their owner rows")


def _chash(text: str) -> str:
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


# ── traffic ───────────────────────────────────────────────────────────────────


class _Log(list):
    """Every catalog POST as ``(path, body, response)``; ``blocked`` holds the request a dying
    client never sent."""

    def __init__(self) -> None:
        super().__init__()
        self.blocked: list[tuple[str, dict]] = []


@contextmanager
def _traffic(*, die_after: int | None = None) -> Iterator[_Log]:
    """Record every catalog POST. With ``die_after=k`` the client "dies" at the (k+1)-th DATA
    request, i.e. right after the k-th one completed."""
    log = _Log()
    orig = HttpCatalogClient._post
    seen = {"n": 0}

    def _post(self, path, body=None, **kw):
        if path in _DATA_PATHS:
            if die_after is not None and seen["n"] >= die_after:
                log.blocked.append((path, body or {}))
                raise ClientDied(f"client died before data request {seen['n'] + 1}")
            seen["n"] += 1
        resp = orig(self, path, body, **kw)
        log.append((path, body or {}, resp if isinstance(resp, dict) else {}))
        return resp

    HttpCatalogClient._post = _post  # type: ignore[method-assign]
    try:
        yield log
    finally:
        HttpCatalogClient._post = orig  # type: ignore[method-assign]


def _data(log) -> list[tuple[str, dict, dict]]:
    return [e for e in log if e[0] in _DATA_PATHS]


def _sent(log) -> tuple[list[dict], list[dict]]:
    """The manifest rows and chunk payloads the data requests carried, in request order."""
    rows: list[dict] = []
    chunks: list[dict] = []
    for path, body, _ in _data(log):
        if path == "/manifest/write_many":
            rows += body["docs"][0]["rows"]
        else:
            rows += body["rows"]
        chunks += body.get("chunks") or []
    return rows, chunks


def _embedded(log) -> int:
    return sum(int(r.get("embed_embedded") or 0) for _, _, r in _data(log))


def _swept(log) -> int:
    return sum(int(r.get("swept") or 0) for _, _, r in _data(log))


# ── engine reads ──────────────────────────────────────────────────────────────


def _reader():
    from nexus.catalog.factory import make_catalog_reader

    return make_catalog_reader()


def _manifest(doc_id: str) -> list[tuple]:
    return [
        (r.position, r.chash, r.line_start, r.line_end, r.char_start, r.char_end, r.chunk_index)
        for r in _reader().get_manifest(doc_id)
    ]


def _index_state(doc_id: str) -> str | None:
    entry = _reader().resolve(doc_id)
    assert entry is not None
    return entry.index_state


def _present(collection: str, chashes: list[str]) -> set[str]:
    from nexus.db.http_vector_client import HttpVectorClient

    return set(HttpVectorClient().existing_ids(collection, chashes))


# ── one scenario per fallback ─────────────────────────────────────────────────


class _Env:
    """One oversize fallback, wired to the real engine.

    ``kind`` is one of ``code`` / ``prose`` / ``rdr`` / ``pdf``. ``write_file(variant)`` writes
    the source (``variant`` 1 edits the first few chunks), ``run()`` indexes it through the
    fallback and returns the request log, and ``control_write(rows, chunks)`` writes rows and
    chunks as ONE combined write into the control document."""

    def __init__(self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch, kind: str) -> None:
        import nexus.db.http_vector_client as hvc

        # Deterministic caps whatever the substrate's embedding mode: the writer clamps every
        # request to this, and the batcher stages nothing larger than _BATCHER_CAP.
        monkeypatch.setattr(hvc, "_serving_embedding_mode", lambda *a, **k: "onnx-local")
        monkeypatch.setattr(hvc, "_ONNX_LOCAL_UPSERT_CHUNK_CAP", _WRITER_CAP)
        self.monkeypatch = monkeypatch
        self.kind = kind
        self.tag = uuid.uuid4().hex[:10]
        self.repo = tmp_path / f"repo-{kind}"
        self.repo.mkdir()
        prefix = {"code": "code", "prose": "docs", "rdr": "rdr", "pdf": "docs"}[kind]
        self.collection = f"{prefix}__z0o2p14-{kind}-{self.tag}__{_MODEL}__v1"
        self.control_collection = f"{prefix}__z0o2p14-{kind}-ctl-{self.tag}__{_MODEL}__v1"
        self.path = self.repo / {
            "code": "mod.py", "prose": "notes.md", "rdr": "rdr-901-oversize.md", "pdf": "big.pdf",
        }[kind]
        self.doc_id = ""
        self.hook_calls: list[dict] = []
        self.variant = 0
        self._pdf_chunks: list[tuple[str, str, dict]] = []

    # sources ----------------------------------------------------------------

    def n_chunks(self) -> int:
        return {"code": 36, "prose": 60, "rdr": 60, "pdf": 30}[self.kind]

    def write_file(self, variant: int = 0, *, small: bool = False) -> Path:
        """``variant`` 0 is the original; 1 edits the first three chunks' text; 2 edits every
        chunk's text (nothing of variant 0 survives)."""
        self.variant = variant
        t = f"{self.tag}-v{variant}"
        fixed = t if variant == 2 else f"{self.tag}-v0"
        n = 3 if small else None
        if self.kind == "code":
            count = n or 60
            body = "".join(
                f"def fn_{i}():\n    return {i}  # {(t if i < 3 else fixed)} unique {i}\n\n"
                for i in range(count))
            self.path.write_text(body, encoding="utf-8")
        elif self.kind in ("prose", "rdr"):
            count = n or 60
            body = "".join(
                f"# Section {i}\n\nParagraph {i} {(t if i < 3 else fixed)} unique text.\n\n"
                for i in range(count))
            self.path.write_text(body, encoding="utf-8")
        else:
            self.path.write_bytes(f"%PDF-1.4 z0o2p14 {t}\n".encode())
            self._pdf_chunks = self._make_pdf_chunks(n or 30, t, fixed)
        return self.path

    def _make_pdf_chunks(self, count: int, t: str, fixed: str) -> list[tuple[str, str, dict]]:
        from nexus.metadata_schema import make_chunk_metadata

        content_hash = hashlib.sha256(self.path.read_bytes()).hexdigest()
        out = []
        for i in range(count):
            text = f"Page {i} {(t if i < 3 else fixed)} unique pdf text."
            h = _chash(text)
            meta = make_chunk_metadata(
                content_type="pdf", chunk_text_hash=h, content_hash=content_hash,
                chunk_start_char=i * 100, chunk_end_char=i * 100 + len(text), page_number=i + 1,
                indexed_at=_NOW, embedding_model=_MODEL, title="Big PDF", tags="pdf",
                category="paper")
            out.append((h, text, meta))
        return out

    def register(self) -> str:
        from nexus.doc_indexer import _register_or_lookup_doc_id

        content_type = {"code": "code", "prose": "prose", "rdr": "rdr", "pdf": "paper"}[self.kind]
        self.doc_id = _register_or_lookup_doc_id(
            self.path.resolve(), f"z0o2p14-{self.kind}-{self.tag}", content_type=content_type,
            physical_collection=self.collection)
        assert self.doc_id, "catalog registration must succeed against the real service"
        return self.doc_id

    # the fallback -------------------------------------------------------------

    def _hooks(self) -> HookRegistry:
        from nexus.mcp_infra import manifest_write_batch_hook

        reg = HookRegistry()
        calls = self.hook_calls

        def spy_batch(doc_ids, collection, contents, embeddings, metadatas, *, catalog_doc_id=""):
            calls.append({"ids": list(doc_ids), "collection": collection,
                          "catalog_doc_id": catalog_doc_id})

        reg.register_batch(spy_batch)
        # The real manifest hook is registered so a fallback that still fires it would write the
        # manifest a second time; the fallback must exclude it.
        reg.register_batch(manifest_write_batch_hook)
        return reg

    def run(self, *, force_re_embed: bool = False, die_after: int | None = None):
        self.hook_calls.clear()
        batcher = ChunkBatcher(flush=lambda *a: None, max_chunks=_BATCHER_CAP)
        self.batcher = batcher
        doc_id = self.doc_id
        with _traffic(die_after=die_after) as log:
            self.last_log = log
            if self.kind == "pdf":
                self._run_pdf(batcher, doc_id, force_re_embed)
            else:
                ctx = IndexContext(
                    col=None, db=_ForbiddenDb(), voyage_key="", voyage_client=None,
                    repo_path=self.repo, corpus=self.collection, embedding_model=_MODEL,
                    git_meta={}, now_iso=_NOW, score=0.0, chunk_lines=6, force=True,
                    force_re_embed=force_re_embed, doc_id_resolver=lambda p: doc_id,
                    hooks=self._hooks(), batcher=batcher)
                if self.kind == "code":
                    from nexus.code_indexer import index_code_file

                    index_code_file(ctx, self.path)
                else:
                    from nexus.prose_indexer import index_prose_file

                    index_prose_file(ctx, self.path)
        return log

    def _run_pdf(self, batcher, doc_id: str, force_re_embed: bool) -> None:
        import nexus.doc_indexer as di
        from nexus.indexer import _index_pdf_file

        prepared = self._pdf_chunks
        self.monkeypatch.setattr(di, "_pdf_chunks", lambda *a, **k: [
            (i, t, dict(m)) for i, t, m in prepared])
        _index_pdf_file(
            self.path, self.repo, self.collection, _MODEL, None, _ForbiddenDb(), "", {}, _NOW, 0.0,
            force=True, embed_fn=lambda texts: [[] for _ in texts],
            doc_id_resolver=lambda p: doc_id, hooks=self._hooks(), batcher=batcher,
            force_re_embed=force_re_embed)

    def control_write(self, rows: list[dict], chunks: list[dict]) -> str:
        """ONE combined write of the whole file into a separate document and collection."""
        from nexus.doc_indexer import _register_or_lookup_doc_id
        from nexus.mcp_infra import get_catalog_writer

        ctl_path = self.repo / f"control-{self.path.name}"
        ctl_path.write_bytes(self.path.read_bytes())
        ctl = _register_or_lookup_doc_id(
            ctl_path.resolve(), f"z0o2p14-{self.kind}-ctl-{self.tag}", content_type="paper",
            physical_collection=self.control_collection)
        assert ctl
        cat = get_catalog_writer()
        try:
            cat.write_manifest_many(
                [(ctl, rows)], chunks=chunks, sweep=True, collection=self.control_collection,
                complete={ctl: "hash-control"})
        finally:
            cat.close()
        return ctl


@pytest.fixture(params=["code", "prose", "rdr", "pdf"])
def env(request, tmp_path, monkeypatch) -> _Env:
    return _Env(tmp_path, monkeypatch, request.param)


def _chashes_sent(log) -> list[str]:
    return [r["chash"] for r in _sent(log)[0]]


# ── a small file is not the writer's business ────────────────────────────────


def test_a_small_file_still_stages_in_the_batcher_and_writes_nothing_itself(env: _Env) -> None:
    env.write_file(small=True)
    env.register()
    log = env.run()
    assert env.batcher.pending_summary["chunks"] > 0           # staged, not written
    assert _data(log) == []
    assert env.hook_calls == []                                # the batcher defers its hooks
    assert _reader().get_manifest(env.doc_id) == []


# ── full run equals one combined write ────────────────────────────────────────


def test_full_run_equals_one_combined_write_of_the_file(env: _Env) -> None:
    env.write_file()
    env.register()
    log = env.run()

    rows, chunks = _sent(log)
    n = env.n_chunks()
    assert len(rows) == n > _BATCHER_CAP                       # oversize, and every row sent once
    data = _data(log)
    # Multi-request (non-vacuity): first the write_many (sweep off, no stamp), then appends, the
    # stamp last, all within the writer's cap.
    assert len(data) == -(-n // _WRITER_CAP) > 1
    assert data[0][0] == "/manifest/write_many"
    assert not data[0][1].get("sweep") and "complete" not in data[0][1]
    assert [p for p, _, _ in data[1:]] == ["/manifest/append"] * (len(data) - 1)
    assert max(len(b.get("chunks") or []) for _, b, _ in data) <= _WRITER_CAP
    assert [p for p, _, _ in log][-1] == "/index-run/complete"
    # No request but the writer's wrote anything: the registered manifest hook was excluded, so
    # the only non-data requests are collection registration, the fence begins (the early one and the writer's) and the
    # stamp.
    assert {p for p, _, _ in log if p not in _DATA_PATHS} <= {"/index-run/begin", "/index-run/complete", "/collections/upsert"}

    ctl = env.control_write(rows, chunks)
    assert _manifest(env.doc_id) == _manifest(ctl) != []
    assert [m[0] for m in _manifest(env.doc_id)] == list(range(n))
    every = [r["chash"] for r in rows]
    assert _present(env.collection, every) == set(every) == _present(env.control_collection, every)
    assert _index_state(env.doc_id) == "complete" == _index_state(ctl)
    # The rows carry the position fields the old manifest hook derived from each chunk's own
    # metadata (line and character spans, an absent span stored as null).
    meta = {c["chash"]: c["metadata"] for c in chunks}
    expected = [
        (i, h, meta[h].get("line_start") or None, meta[h].get("line_end") or None,
         meta[h].get("chunk_start_char") or None, meta[h].get("chunk_end_char") or None, i)
        for i, h in enumerate(every)
    ]
    assert _manifest(env.doc_id) == expected
    if env.kind == "code":
        assert all(m[2] and m[3] for m in expected)            # non-vacuity: spans really present


# ── the hooks ─────────────────────────────────────────────────────────────────


def test_hooks_fire_once_for_the_whole_file_and_the_manifest_hook_is_excluded(env: _Env) -> None:
    env.write_file()
    env.register()
    log = env.run()
    assert len(env.hook_calls) == 1
    call = env.hook_calls[0]
    assert len(call["ids"]) == env.n_chunks()
    assert call["collection"] == env.collection and call["catalog_doc_id"] == env.doc_id
    # The registered manifest hook would have issued a manifest write of its own: there is none.
    assert len(_data(log)) == -(-env.n_chunks() // _WRITER_CAP)


# ── the client dies after the first request ───────────────────────────────────


def test_client_death_after_the_first_request_leaves_no_ownerless_chunk(env: _Env) -> None:
    env.write_file()
    env.register()
    # A previous, complete version: the run that dies drops most of its chunks from the manifest,
    # and the sweep of them waits for the last batch, which never arrives.
    first = env.run()
    assert _index_state(env.doc_id) == "complete"
    old = _chashes_sent(first)

    env.write_file(variant=2)
    with pytest.raises(ClientDied):
        env.run(die_after=1)
    log = env.last_log

    sent_rows, sent_chunks = _sent(log)
    landed = {c["chash"] for c in sent_chunks}
    assert len(_data(log)) == 1 and len(landed) == _WRITER_CAP        # non-vacuity: request 1 landed
    owners = {m[1] for m in _manifest(env.doc_id)}
    assert owners == {r["chash"] for r in sent_rows}
    # Everything request 1 wrote has an owner row.
    assert _present(env.collection, list(landed)) == landed <= owners
    # The request that never left the client wrote nothing.
    (_, blocked_body), = log.blocked
    never_sent = {c["chash"] for c in blocked_body.get("chunks") or []}
    assert never_sent and not never_sent & landed
    assert _present(env.collection, list(never_sent)) == set()
    # The previous version's chunks are the accepted leftovers: the sweep is deferred, never early.
    left = set(old) - owners
    assert left and _present(env.collection, list(left)) == left
    # The writer marked the fence failed (the exception ran its abort), so the next run redoes it.
    assert _index_state(env.doc_id) == "failed"


# ── unchanged re-index ────────────────────────────────────────────────────────


def test_reindexing_an_unchanged_file_embeds_nothing_and_sweeps_nothing(env: _Env) -> None:
    env.write_file()
    env.register()
    first = env.run()
    assert _embedded(first) == len(set(_chashes_sent(first))) > 0   # non-vacuity: run 1 embedded
    before = _manifest(env.doc_id)

    again = env.run()
    assert _embedded(again) == 0
    assert _swept(again) == 0
    for _, body, _ in _data(again):
        assert not body.get("sweep_chashes")
    assert len(_data(again)) == len(_data(first))
    assert _manifest(env.doc_id) == before
    assert _index_state(env.doc_id) == "complete"


# ── edited re-index ───────────────────────────────────────────────────────────


def test_reindexing_an_edited_file_sweeps_only_what_the_edit_dropped(env: _Env) -> None:
    env.write_file()
    env.register()
    first = env.run()
    old = _chashes_sent(first)

    env.write_file(variant=1)
    second = env.run()
    new = _chashes_sent(second)
    dropped = set(old) - set(new)
    kept = set(old) & set(new)
    assert dropped and kept                                    # the edit changed some, not all
    assert _swept(second) == len(dropped)
    assert _present(env.collection, list(dropped)) == set()
    assert _present(env.collection, new) == set(new)
    assert [m[1] for m in _manifest(env.doc_id)] == new
    # The sweep rides the LAST data request, never an earlier one.
    swept_at = [i for i, (_, _, r) in enumerate(_data(second)) if int(r.get("swept") or 0)]
    assert swept_at == [len(_data(second)) - 1]
    assert _index_state(env.doc_id) == "complete"


# ── metadata: the write merges, like the old upsert did ───────────────────────


def _stored_metadata(collection: str, chashes: list[str]) -> dict[str, dict]:
    got = HttpVectorClient().get_collection(collection).get(ids=chashes, include=["metadatas"])
    assert len(got["ids"]) == len(chashes), "every chunk is stored"
    return dict(zip(got["ids"], got["metadatas"]))


@pytest.mark.parametrize("re_embed", [False, True], ids=["force", "force-re-embed"])
def test_forced_reindex_keeps_enrichment_and_clears_an_owned_key_the_write_dropped(
    env: _Env, re_embed: bool,
) -> None:
    """The combined write REPLACES stored chunk metadata unless asked to merge; the upsert-chunks
    call the oversize fallbacks replaced MERGED it. So a ``bib_year`` that ``nx enrich bib`` set on
    an oversize file's chunks (a PDF's, in practice) must survive a forced re-index, and a stale
    value of a key the indexer owns and no longer sends must be cleared. With ``--re-embed`` the
    same holds through the engine's insert branch."""
    env.write_file()
    env.register()
    first = env.run()
    every = _chashes_sent(first)

    HttpVectorClient().update_chunks(
        env.collection, every, [{"bib_year": 2020, "quality_gate_overridden": True} for _ in every])
    before = _stored_metadata(env.collection, every)
    assert all(m["bib_year"] == 2020 and m["quality_gate_overridden"] is True for m in before.values())

    again = env.run(force_re_embed=re_embed)

    after = _stored_metadata(env.collection, every)
    assert all(m.get("bib_year") == 2020 for m in after.values()), "enrichment survived"
    assert all("quality_gate_overridden" not in m for m in after.values()), \
        "an owned key the write dropped is cleared"
    sent = [b for _, b, _ in _data(again) if b.get("chunks")]
    assert sent and all(b["metadata_merge"] is True for b in sent)
    assert all("quality_gate_overridden" in b["metadata_delete_keys"] for b in sent)
    assert all("bib_year" not in b["metadata_delete_keys"] for b in sent)
    if re_embed:
        assert _embedded(again) == len(every), "--re-embed really re-embeds"
    else:
        assert _embedded(again) == 0


# ── the ChunkBatcher flush merges per chunk, for a flush of several files ─────


def _git(repo: Path, *args: str) -> None:
    import subprocess

    subprocess.run(["git", "-C", str(repo), *args], check=True, capture_output=True)


@pytest.fixture
def flush_repo(tmp_path, monkeypatch):
    """A git repo of three SMALL files: ``a.md`` and ``b.md``, which both land in ONE ``docs__``
    flush, and ``m.py`` for ``code__``. Returns ``(repo, registry)``. The collection names are
    the catalog owner's (``docs__<owner>__<model>__v1``), which ``_run_index`` resolves itself once
    the repo has an owner, so the test reads them from the traffic of a first run."""
    from nexus.registry import RepoRegistry

    for k, v in (("GIT_AUTHOR_NAME", "Test"), ("GIT_AUTHOR_EMAIL", "t@test.invalid"),
                 ("GIT_COMMITTER_NAME", "Test"), ("GIT_COMMITTER_EMAIL", "t@test.invalid")):
        monkeypatch.setenv(k, v)
    tag = uuid.uuid4().hex[:10]
    repo = tmp_path / f"flushrepo-{tag}"
    repo.mkdir()
    (repo / "a.md").write_text(
        "".join(f"# Heading {i}\n\nAlpha {tag} paragraph {i} with some text.\n\n" for i in range(3)),
        encoding="utf-8")
    (repo / "b.md").write_text(
        "".join(f"Bravo {tag} plain paragraph {i}, no heading anywhere.\n\n" for i in range(3)),
        encoding="utf-8")
    (repo / "m.py").write_text(
        "".join(f"def fn_{i}():\n    return {i}  # {tag} flush unique {i}\n\n" for i in range(3)),
        encoding="utf-8")
    _git(repo, "init", "-q")
    _git(repo, "add", "a.md", "b.md", "m.py")
    _git(repo, "commit", "-q", "-m", "init")
    reg = RepoRegistry(tmp_path / "repos.json")
    reg.add(repo)
    return repo, reg


def _docs_by_file(collection: str) -> dict[str, tuple[str, list[str]]]:
    """``{file name: (doc_id, [chash in position order])}`` for the documents of *collection*."""
    out = {}
    for entry in _reader().list_by_collection(collection):
        tumbler = str(entry.tumbler)
        out[Path(entry.file_path).name] = (tumbler, [r.chash for r in _reader().get_manifest(tumbler)])
    return out


@pytest.fixture
def asymmetric_rows(monkeypatch):
    """Make the chunk rows of the two markdown files carry DIFFERENT owned keys, as a PDF's rows do
    next to a markdown file's in one ``docs__`` flush (``extraction_method`` is set on PDF chunks
    only). No markdown or code indexer emits a key the others drop, so the factory is wrapped:
    ``a.md``'s rows gain ``extraction_method`` and ``b.md``'s gain ``extraction_source``. Each file
    then lacks a key the other carries, so neither file's rows alone give the flush's delete list."""
    import nexus.metadata_schema as ms

    orig = ms.make_chunk_metadata

    def wrapped(**kw):
        meta = orig(**kw)
        title = kw.get("title", "")
        if title.startswith("a.md:"):
            meta["extraction_method"] = "probe-method"
        elif title.startswith("b.md:"):
            meta["extraction_source"] = "probe-source"
        return meta

    monkeypatch.setattr(ms, "make_chunk_metadata", wrapped)


@pytest.mark.parametrize("re_embed", [False, True], ids=["force", "force-re-embed"])
def test_a_forced_index_repo_flush_merges_each_chunks_metadata(
    flush_repo, asymmetric_rows, re_embed: bool,
) -> None:
    """``_batch_flush`` writes every file of a flush in ONE combined write and names one request-level
    ``metadata_delete_keys`` list for all of them: the keys the writer owns that SOME row lacks.
    The engine applies that list chunk by chunk, stripping each named key from the chunk's stored
    row before it merges the chunk's own metadata on top. So a chunk that carries a named key
    writes it back, a chunk that lacks it loses a stale value, and a key another writer owns
    (``bib_year``) is never named. Only a kwargs pin on a mock covered this. Here ``_run_index``
    (``_batch_flush`` is a closure) runs against the real engine on two markdown files that land in
    ONE ``docs__`` flush and carry different owned keys, plus a python file for ``code__``."""
    from nexus.indexer import _run_index

    repo, reg = flush_repo
    _run_index(repo, reg, force=False)            # registers the repo's owner
    with _traffic() as base:                      # from here on the owner names the collections
        _run_index(repo, reg, force=True)
    written = {b["collection"] for p, b, _ in base if p == "/manifest/write_many"}
    (docs_col,) = [c for c in written if c.startswith("docs__")]
    (code_col,) = [c for c in written if c.startswith("code__")]
    docs = _docs_by_file(docs_col)
    code = _docs_by_file(code_col)
    assert set(docs) == {"a.md", "b.md"} and set(code) == {"m.py"}
    a_hashes, b_hashes = docs["a.md"][1], docs["b.md"][1]
    m_hashes = code["m.py"][1]
    assert a_hashes and b_hashes and m_hashes and not set(a_hashes) & set(b_hashes)

    a_before = _stored_metadata(docs_col, a_hashes)
    b_before = _stored_metadata(docs_col, b_hashes)
    assert all(m.get("extraction_method") == "probe-method" and "extraction_source" not in m
               for m in a_before.values())
    assert all(m.get("extraction_source") == "probe-source" and "extraction_method" not in m
               for m in b_before.values())

    # Another writer's enrichment and a stale owned key on every chunk, and on each file the key
    # that only the OTHER file's rows carry.
    both = a_hashes + b_hashes
    stale = {"bib_year": 2020, "quality_gate_overridden": True}
    client = HttpVectorClient()
    client.update_chunks(docs_col, both, [dict(stale) for _ in both])
    client.update_chunks(docs_col, a_hashes, [{"extraction_source": "STALE-S"} for _ in a_hashes])
    client.update_chunks(docs_col, b_hashes, [{"extraction_method": "STALE-M"} for _ in b_hashes])
    client.update_chunks(code_col, m_hashes, [dict(stale) for _ in m_hashes])
    seeded = _stored_metadata(docs_col, both)
    assert all(m["bib_year"] == 2020 and m["quality_gate_overridden"] is True for m in seeded.values())
    assert all(seeded[h]["extraction_source"] == "STALE-S" for h in a_hashes)
    assert all(seeded[h]["extraction_method"] == "STALE-M" for h in b_hashes)

    with _traffic() as log:
        _run_index(repo, reg, force=True, force_re_embed=re_embed)

    # Non-vacuity: the two markdown files went out in ONE write_many of TWO documents, and that
    # write named the union of what each file's rows lack.
    flushes = [(b, r) for p, b, r in log if p == "/manifest/write_many" and b["collection"] == docs_col]
    assert len(flushes) == 1
    body, resp = flushes[0]
    assert len(body["docs"]) == 2
    assert body["metadata_merge"] is True and resp.get("metadata_merge") is True
    for key in ("extraction_method", "extraction_source", "quality_gate_overridden"):
        assert key in body["metadata_delete_keys"], key
    assert "bib_year" not in body["metadata_delete_keys"]
    code_writes = [b for p, b, _ in log if p == "/manifest/write_many" and b["collection"] == code_col]
    assert len(code_writes) == 1 and len(code_writes[0]["docs"]) == 1
    assert int(resp.get("embed_embedded") or 0) == (len(set(both)) if re_embed else 0)

    a_after = _stored_metadata(docs_col, a_hashes)
    b_after = _stored_metadata(docs_col, b_hashes)
    for after in (a_after, b_after, _stored_metadata(code_col, m_hashes)):
        assert all(m.get("bib_year") == 2020 for m in after.values()), "enrichment survived"
        assert all("quality_gate_overridden" not in m for m in after.values()), \
            "a stale owned key is cleared"
    # Each file keeps the key its own rows carry (stripped by the union list, then written back)
    # and loses the stale value of the key only the other file's rows carry.
    assert all(m.get("extraction_method") == "probe-method" and "extraction_source" not in m
               for m in a_after.values())
    assert all(m.get("extraction_source") == "probe-source" and "extraction_method" not in m
               for m in b_after.values())
