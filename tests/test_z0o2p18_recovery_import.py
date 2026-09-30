# SPDX-License-Identifier: AGPL-3.0-or-later
"""RDR-223 P2.8 (nexus-z0o2p.18): the recovery-bundle import against the REAL engine.

Each note of a bundle is written through the note writer in ONE ``write_manifest_many`` request, so a
chunk of a note never lands without its owner row. The import used to write the pieces with
``t3.put`` (``/v1/vectors/upsert-chunks``) and the manifest in a later request, with compensation in
between. What these tests pin, all against a live engine:

* Test Plan 8: the client dies after the first request and no chunk that request wrote is ownerless;
* importing the same bundle twice ends with one manifest per note;
* one note that fails is reported per note, the others land, and ``nx catalog import`` exits non-zero;
* the import makes no ownerless chunk write (no ``/v1/vectors`` write route is ever called).

The collections name the bge-768 model the test engine embeds with, so none of this needs cloud mode.
"""
from __future__ import annotations

import hashlib

import pytest
from click.testing import CliRunner

import nexus.catalog.recovery_bundle as rb
from nexus.catalog.recovery_bundle import ExportSummary, import_bundle, write_bundle

_RECORDED = "knowledge__z0o2p18__bge-base-en-v15-768__v1"


class ClientDied(BaseException):
    """The simulated death of the client process: not an Exception, so nothing catches it."""


def _chash(text: str) -> str:
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


def _body(label: str, words: int = 1100) -> str:
    """A note longer than bge's 512-token window, so it is written as several pieces."""
    return " ".join(f"{label}w{i}" for i in range(words))


def _rec(title: str, content: str, *, tags: str = "", category: str = "") -> dict:
    return {
        "record": "knowledge_doc", "source_uri": "", "collection": _RECORDED,
        "title": title, "tags": tags, "category": category, "content": content,
    }


def _bundle(tmp_path, recs: list[dict]):
    tmp_path.mkdir(parents=True, exist_ok=True)
    path = tmp_path / "bundle.jsonl"
    write_bundle(path, recs, [], ExportSummary())
    return path


@pytest.fixture
def t3(t2_service_env):
    """The T3 handle, with the target collection already registered, so nothing here depends on
    which write path registers a first-touch collection: only on how the note reaches the engine."""
    from nexus.corpus import ensure_collection_registered
    from nexus.db import make_t3

    handle = make_t3()
    ensure_collection_registered(rb.target_collection_for(_RECORDED, handle))
    return handle


@pytest.fixture
def vec(t2_service_env):
    import nexus.db.http_vector_client as hvc

    return hvc.HttpVectorClient(tenant=t2_service_env)


@pytest.fixture(autouse=True)
def _no_retry_sleeps(monkeypatch):
    from nexus.rate_brake import reset_brake

    monkeypatch.setattr("nexus.retry.time.sleep", lambda seconds: None)
    reset_brake()
    yield
    reset_brake()


def _target(t3) -> str:
    return rb.target_collection_for(_RECORDED, t3)


def _docs(title: str, collection: str) -> list:
    from nexus.catalog.factory import make_catalog_reader

    return [d for d in make_catalog_reader().all_documents()
            if d.title == title and d.physical_collection == collection]


def _manifest(doc: str) -> list[tuple[int, str]]:
    from nexus.catalog.factory import make_catalog_reader

    return [(r.position, r.chash) for r in make_catalog_reader().get_manifest(doc)]


def _present(vec, chashes: list[str], collection: str) -> set[str]:
    from nexus.db.http_vector_client import VectorServiceError

    try:
        return set(vec.existing_ids(collection, chashes))
    except VectorServiceError as exc:
        if "not registered" in str(exc):
            return set()
        raise


def _pieces_of(content: str, collection: str) -> list[str]:
    from nexus.catalog.store_hook import note_pieces

    return note_pieces(content, collection)


def test_the_test_notes_really_split_into_several_pieces(t3):
    """Control for every test below: a one-piece note could not show a chunk left without an owner."""
    assert len(_pieces_of(_body("ctl"), _target(t3))) > 1


def test_client_dies_after_the_first_request_no_chunk_is_without_an_owner(t3, vec, tmp_path, monkeypatch):
    """Test Plan 8. The process dies at the HTTP layer right after the first request that writes
    anything has gone out, whichever route that is: a chunk write to ``/v1/vectors`` or the catalog's
    ``write_manifest_many``. The import makes ONE request per note and it carries the owner rows, so
    every chunk that reached T3 has an owner. (Written as separate requests, the first is the chunks
    alone and the death leaves them ownerless.)"""
    import nexus.catalog.http_catalog_client as hcc
    import nexus.db.http_vector_client as hvc

    content = _body("dies")
    target = _target(t3)
    chashes = [_chash(p) for p in _pieces_of(content, target)]
    bundle = _bundle(tmp_path, [_rec("z0o2p18-dies", content)])

    writes: list[str] = []
    real_embed_post, real_hvc_post = hcc.HttpCatalogClient._post_embedding_write, hvc._post

    def die_after_the_first_write(path: str) -> None:
        writes.append(path)
        raise ClientDied()

    def catalog_embed_post(self, path, *a, **k):
        out = real_embed_post(self, path, *a, **k)
        die_after_the_first_write(path)
        return out

    def vector_post(path, *a, **k):
        out = real_hvc_post(path, *a, **k)
        if any(path.endswith(s) for s in hvc._T3_WRITE_PATH_SUFFIXES):
            die_after_the_first_write(path)
        return out

    monkeypatch.setattr(hcc.HttpCatalogClient, "_post_embedding_write", catalog_embed_post)
    monkeypatch.setattr(hvc, "_post", vector_post)
    with pytest.raises(ClientDied):
        import_bundle(None, None, t3, bundle)

    in_t3 = _present(vec, chashes, target)
    docs = _docs("z0o2p18-dies", target)
    owned = {c for _, c in _manifest(str(docs[0].tumbler))} if docs else set()
    assert in_t3, "control: the request that went out put something in T3"
    assert in_t3 <= owned, f"chunks without an owner: {sorted(in_t3 - owned)}"
    assert in_t3 == set(chashes) and len(docs) == 1, "control: the one request committed the whole note"
    assert writes == ["/manifest/write_many"], f"the first write request of the import was {writes}"


def test_importing_the_same_bundle_twice_ends_with_one_manifest_per_note(t3, vec, tmp_path):
    notes = {f"z0o2p18-twice-{i}": _body(f"twice{i}") for i in range(2)}
    bundle = _bundle(tmp_path, [_rec(t, c) for t, c in notes.items()])
    target = _target(t3)

    s1 = import_bundle(None, None, t3, bundle)
    s2 = import_bundle(None, None, t3, bundle)

    assert (s1.docs_imported, s1.docs_failed) == (2, 0)
    assert (s2.docs_imported, s2.docs_failed) == (2, 0)
    for title, content in notes.items():
        docs = _docs(title, target)
        assert len(docs) == 1, f"{title!r} duplicated on re-import: {len(docs)} rows"
        expected = [(i, _chash(p)) for i, p in enumerate(_pieces_of(content, target))]
        assert len(expected) > 1
        assert _manifest(str(docs[0].tumbler)) == expected
        assert _present(vec, [c for _, c in expected], target) == {c for _, c in expected}


def test_reimporting_a_changed_note_leaves_no_chunk_of_the_old_version_without_an_owner(t3, vec, tmp_path):
    """A re-import that replaces the note with a shorter one: the manifest is exactly the new
    pieces, and the pieces the new version dropped are swept from T3 by the same write rather than
    left behind ownerless."""
    target = _target(t3)
    long_note, short_note = _body("chg", 1100), _body("chg", 300)
    old_chashes = {_chash(p) for p in _pieces_of(long_note, target)}
    new_pieces = _pieces_of(short_note, target)
    new_chashes = {_chash(p) for p in new_pieces}
    dropped = old_chashes - new_chashes
    assert dropped and len(_pieces_of(long_note, target)) > len(new_pieces), "control: the new note is shorter"

    import_bundle(None, None, t3, _bundle(tmp_path / "v1", [_rec("z0o2p18-chg", long_note)]))
    s2 = import_bundle(None, None, t3, _bundle(tmp_path / "v2", [_rec("z0o2p18-chg", short_note)]))

    assert (s2.docs_imported, s2.docs_failed) == (1, 0)
    docs = _docs("z0o2p18-chg", target)
    assert len(docs) == 1
    assert _manifest(str(docs[0].tumbler)) == [(i, _chash(p)) for i, p in enumerate(new_pieces)]
    assert _present(vec, sorted(dropped), target) == set(), "chunks of the old version left without an owner"


def test_one_failed_note_is_named_the_others_land_and_the_exit_is_nonzero(t3, vec, tmp_path):
    """A note over the 16 KiB document quota is refused before anything is written for it. The notes
    around it still land, the summary names it, and the command exits non-zero."""
    from nexus.commands.catalog_cmds.recovery import import_cmd

    target = _target(t3)
    good_a, good_b = _body("okA"), _body("okB")
    bundle = _bundle(tmp_path, [
        _rec("z0o2p18-ok-a", good_a),
        _rec("z0o2p18-too-big", "x" * 20_000),
        _rec("z0o2p18-ok-b", good_b),
    ])

    result = CliRunner().invoke(import_cmd, [str(bundle)])

    assert result.exit_code != 0, result.output
    assert "z0o2p18-too-big" in result.output
    for title, content in (("z0o2p18-ok-a", good_a), ("z0o2p18-ok-b", good_b)):
        docs = _docs(title, target)
        assert len(docs) == 1, f"{title!r} did not land"
        assert _manifest(str(docs[0].tumbler)) == [
            (i, _chash(p)) for i, p in enumerate(_pieces_of(content, target))]
    assert _docs("z0o2p18-too-big", target) == [], "a refused note must leave no catalog row"


def test_a_clean_bundle_exits_zero(t3, tmp_path):
    from nexus.commands.catalog_cmds.recovery import import_cmd

    bundle = _bundle(tmp_path, [_rec("z0o2p18-clean", _body("clean"))])
    result = CliRunner().invoke(import_cmd, [str(bundle)])
    assert result.exit_code == 0, result.output
    assert "docs imported: 1" in result.output


def test_the_import_makes_no_ownerless_chunk_write(t3, tmp_path, monkeypatch):
    """No call to a /v1/vectors route that writes chunks (/upsert-chunks, /store-put, ...), and no
    T3 ``put``: the pieces reach the engine only as the ``chunks`` of the one owner-carrying request."""
    import nexus.catalog.http_catalog_client as hcc
    import nexus.db.http_vector_client as hvc

    vector_writes: list[str] = []
    catalog_posts: list[str] = []
    real_hvc_post, real_hcc_post = hvc._post, hcc.HttpCatalogClient._post

    def hvc_post(path, *a, **k):
        if any(path.endswith(s) for s in hvc._T3_WRITE_PATH_SUFFIXES):
            vector_writes.append(path)
        return real_hvc_post(path, *a, **k)

    def hcc_post(self, path, *a, **k):
        catalog_posts.append(path)
        return real_hcc_post(self, path, *a, **k)

    def no_put(self, *a, **k):
        raise AssertionError("the import called T3.put: an ownerless chunk write")

    monkeypatch.setattr(hvc, "_post", hvc_post)
    monkeypatch.setattr(hcc.HttpCatalogClient, "_post", hcc_post)
    monkeypatch.setattr(type(t3), "put", no_put)

    bundle = _bundle(tmp_path, [_rec("z0o2p18-nopin-a", _body("np")), _rec("z0o2p18-nopin-b", _body("nq"))])
    summary = import_bundle(None, None, t3, bundle)

    assert summary.docs_imported == 2 and summary.docs_failed == 0
    assert vector_writes == [], vector_writes
    assert catalog_posts.count("/manifest/write_many") == 2, catalog_posts
    assert "/manifest/write" not in catalog_posts and "/manifest/atomic_replace" not in catalog_posts


# ── landing review (RDR-223 Phase 2 joint reviews) ───────────────────────────


@pytest.fixture
def refused_write(monkeypatch: pytest.MonkeyPatch):
    """Make the ENGINE refuse a note's request: add a manifest row naming a chunk that is not in the
    request, so the per-document transaction rolls back (``failed_doc_ids``). The refusal is the
    engine's, not a stub raised before the call."""
    from nexus.catalog.http_catalog_client import HttpCatalogClient

    real = HttpCatalogClient.write_manifest_many

    def _refused(self, docs, *a, **k):
        doc, rows = docs[0]
        return real(self, [(doc, [*rows, {"chash": "f" * 64, "position": len(rows)}])], *a, **k)

    return type("Refused", (), {
        "arm": staticmethod(lambda: monkeypatch.setattr(HttpCatalogClient, "write_manifest_many", _refused)),
        "disarm": staticmethod(lambda: monkeypatch.setattr(HttpCatalogClient, "write_manifest_many", real)),
    })


def test_a_failed_reimport_leaves_the_old_manifest_intact(t3, vec, tmp_path, refused_write):
    """TD3, against the real engine: a re-import of a changed note that the engine refuses changes
    nothing. The old manifest and its chunks stay, none of the new version's chunks is added, the
    summary counts the note failed (never imported), and a later good import replaces it whole."""
    target = _target(t3)
    old_note, new_note = _body("reimp-old", 1100), _body("reimp-new", 700)
    old_pieces, new_pieces = _pieces_of(old_note, target), _pieces_of(new_note, target)
    old_chashes, new_chashes = [_chash(p) for p in old_pieces], [_chash(p) for p in new_pieces]
    assert len(old_pieces) > 1 and not set(old_chashes) & set(new_chashes), "control: two different notes"

    first = import_bundle(None, None, t3, _bundle(tmp_path / "v1", [_rec("z0o2p18-reimport", old_note)]))
    assert (first.docs_imported, first.docs_failed) == (1, 0)
    (doc,) = _docs("z0o2p18-reimport", target)
    old_manifest = _manifest(str(doc.tumbler))
    assert [c for _, c in old_manifest] == old_chashes

    refused_write.arm()
    second = import_bundle(None, None, t3, _bundle(tmp_path / "v2", [_rec("z0o2p18-reimport", new_note)]))
    refused_write.disarm()

    assert (second.docs_imported, second.docs_failed, second.docs_unverified) == (0, 1, 1)
    assert "failed_doc_ids" in second.doc_failures[0]["error"]
    assert "The note was not stored" in second.doc_failures[0]["error"]
    assert _manifest(str(doc.tumbler)) == old_manifest, "the engine's refusal must leave the old manifest as it was"
    assert _present(vec, old_chashes, target) == set(old_chashes)
    assert _present(vec, new_chashes, target) == set()
    assert len(_docs("z0o2p18-reimport", target)) == 1, "a row this import did not mint stays"

    third = import_bundle(None, None, t3, _bundle(tmp_path / "v3", [_rec("z0o2p18-reimport", new_note)]))
    assert (third.docs_imported, third.docs_failed) == (1, 0)
    assert [c for _, c in _manifest(str(doc.tumbler))] == new_chashes
    assert _present(vec, sorted(set(old_chashes) - set(new_chashes)), target) == set(), "the old version was swept"


_FRESH_RECORDED = "knowledge__z0o2p18-fresh__bge-base-en-v15-768__v1"


def test_a_first_touch_collection_is_registered_by_the_notes_own_request(t2_service_env, vec, tmp_path, monkeypatch):
    """Phase 2's other registration case: the collection has never been written to in this tenant
    (the fixture above pre-registers it). ``write_manifest_many`` registers it before it sends, so
    the note lands in the one request with no separate T3 write to a collection that does not yet
    exist, and the registration is visible afterwards."""
    import nexus.catalog.http_catalog_client as hcc
    import nexus.db.http_vector_client as hvc
    from nexus.db import make_t3

    t3_handle = make_t3()
    fresh = rb.target_collection_for(_FRESH_RECORDED, t3_handle)
    with pytest.raises(hvc.VectorServiceError, match="not registered"):
        vec.existing_ids(fresh, ["0" * 64])  # control: nothing has registered it yet

    vector_writes: list[str] = []
    catalog_posts: list[str] = []
    real_hvc_post, real_hcc_post = hvc._post, hcc.HttpCatalogClient._post

    def hvc_post(path, *a, **k):
        if any(path.endswith(s) for s in hvc._T3_WRITE_PATH_SUFFIXES):
            vector_writes.append(path)
        return real_hvc_post(path, *a, **k)

    def hcc_post(self, path, *a, **k):
        catalog_posts.append(path)
        return real_hcc_post(self, path, *a, **k)

    monkeypatch.setattr(hvc, "_post", hvc_post)
    monkeypatch.setattr(hcc.HttpCatalogClient, "_post", hcc_post)
    content = _body("fresh", 1100)
    rec = {**_rec("z0o2p18-fresh", content), "collection": _FRESH_RECORDED}

    summary = import_bundle(None, None, t3_handle, _bundle(tmp_path, [rec]))

    assert (summary.docs_imported, summary.docs_failed, summary.docs_unverified) == (1, 0, 0)
    assert vector_writes == [], vector_writes
    assert catalog_posts.count("/manifest/write_many") == 1, catalog_posts
    chashes = [_chash(p) for p in _pieces_of(content, fresh)]
    assert len(chashes) > 1
    assert _present(vec, chashes, fresh) == set(chashes), "registered, and the whole note is in it"
    (doc,) = _docs("z0o2p18-fresh", fresh)
    assert [c for _, c in _manifest(str(doc.tumbler))] == chashes


def test_a_client_side_refusal_is_a_failed_note_that_leads_with_its_remedy(t3, tmp_path, monkeypatch):
    """The import's own wording of a refusal the client made before it sent anything: the note is
    counted failed (nothing landed), and the recorded error is the shared table's client-refusal row."""
    from nexus.corpus import LocalVoyageCredentialMissingError

    remedy = "no Voyage API key is configured. Set one with `nx config set voyage_api_key <key>`."

    def refuse(*_a, **_k):
        raise LocalVoyageCredentialMissingError(remedy)

    monkeypatch.setattr("nexus.corpus.ensure_collection_registered", refuse)
    summary = import_bundle(None, None, t3, _bundle(tmp_path, [_rec("z0o2p18-keyless", _body("kl"))]))

    assert (summary.docs_imported, summary.docs_failed, summary.docs_uncertain) == (0, 1, 0)
    error = summary.doc_failures[0]["error"]
    assert error.startswith(remedy), error
    assert "The note was not written and its chunks and manifest are unchanged" in error
    assert "retry is safe" not in error and "could not" not in error
    assert _docs("z0o2p18-keyless", _target(t3)) == []

