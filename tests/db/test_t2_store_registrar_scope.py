# SPDX-License-Identifier: AGPL-3.0-or-later
"""Cross-store pin: every ``RefreshableHttpStoreMixin`` subclass that
pre-registers a collection before a write registers on ITS OWN endpoint,
never the ambient one (nexus-w1ip follow-up).

de34a879d bound all 13 ``write_with_registration_retry`` sites across five
store classes (``HttpChashIndex``, ``HttpAspectQueue``,
``HttpDocumentAspectsStore``, ``HttpDocumentHighlightsStore``,
``HttpTaxonomyStore``) to ``registrar=self._catalog_registrar`` instead of
the process-wide ambient default. That commit's own unit pin
(``tests/db/test_http_chash_index.py``) covered one store, via a spy that
never drives a real wire call; the critic review (T2
``nexus/w1ip-registrar-followup-critic-2026-09-14``, gap 2) asked for a
real two-server pin across all five.

Two REAL in-process fake HTTP servers per test case: "ambient" (pinned via
``NX_SERVICE_HOST``/``NX_SERVICE_PORT``/``NX_SERVICE_TOKEN``, exactly the
env ``RefreshableHttpStoreMixin`` and the shared default catalog writer
both resolve from when no explicit endpoint is supplied) and "own" (passed
directly as the store's ``base_url``/``_token``). Each store is
constructed pinned to "own" while "ambient" env is live, driven through
ONE real write that pre-registers a collection, and the test asserts the
registration route landed on "own" and never reached "ambient" (the
always-ambient, out-of-scope embedding-profile pre-check GET aside — see
``_RecordingHandler``'s docstring) — proving the registrar scoping, not
just the constructed writer's bound attributes (the
existing chash spy-based pin cannot see a wire-routing regression; this
one would fail the moment ``registrar=self._catalog_registrar`` is
dropped from any of the 13 sites — see the RED check recorded in this
bead's hand-off).
"""
from __future__ import annotations

from urllib.parse import urlparse

import pytest

from nexus.catalog.http_catalog_client import HttpCatalogClient
from nexus.db.t2.http_aspect_queue import HttpAspectQueue
from nexus.db.t2.http_chash_index import HttpChashIndex
from nexus.db.t2.http_document_aspects_store import HttpDocumentAspectsStore
from nexus.db.t2.http_document_highlights_store import HttpDocumentHighlightsStore
from nexus.db.t2.http_taxonomy_store import HttpTaxonomyStore
from nexus.db.t2.records import AspectRecord
from tests.db._fake_t2_server import FakeT2HandlerBase, fake_http_server

OWN_TOKEN = "own-server-token-w1ip"
AMBIENT_TOKEN = "ambient-server-token-w1ip"

REGISTRATION_ROUTE = "/v1/catalog/collections/upsert"
EMBEDDING_PROFILE_ROUTE = "/v1/catalog/embedding_profile"


class _RecordingHandler(FakeT2HandlerBase):
    """Answers the catalog registration route, plus any store-specific
    write route generically, and records every POST path on a
    subclass-owned ``calls`` list.

    A generic 200 response body (``{"ok": True, "updated": 1, "written":
    True, "created": True}``) covers every one of the five drivers below:
    chash/highlights ``rename_collection`` reads ``"updated"``,
    ``HttpDocumentAspectsStore.upsert`` reads ``"written"``, the
    registration route itself reads ``"created"``/``"name"``, and
    aspect-queue ``enqueue`` / taxonomy ``record_discover_count`` read
    nothing from the body at all.

    ``ensure_collection_registered``'s profile-mismatch guard reads
    ``GET /v1/catalog/embedding_profile`` through the AMBIENT catalog
    READER (``make_catalog_reader()``) unconditionally, regardless of
    which registrar the WRITE uses — that read is deliberately out of
    this bead's scope (see ``_profile_model_for_content_type``'s
    docstring), so it always lands on the ambient server here and is
    answered with an empty profile (no row for this content_type yet,
    the bootstrap case — proceeds with the client's own intent
    unchanged). Recorded like any other call; the test's assertions
    check for the REGISTRATION route specifically, not for zero calls.
    """

    TOKEN = ""  #: overridden per dynamically-built subclass below
    calls: list[str] = []  #: overridden per dynamically-built subclass below

    def do_POST(self):  # noqa: N802
        if not self._check_auth():
            return
        pp = urlparse(self.path).path
        self._read_body()
        type(self).calls.append(pp)
        if pp == REGISTRATION_ROUTE:
            self._send(200, {"created": True, "name": "recorded"})
            return
        self._send(200, {"ok": True, "updated": 1, "written": True})

    def do_GET(self):  # noqa: N802
        if not self._check_auth():
            return
        pp = urlparse(self.path).path
        type(self).calls.append(pp)
        if pp == EMBEDDING_PROFILE_ROUTE:
            self._send(200, {"profile": [], "count": 0})
            return
        self._send(200, {})


def _handler_subclass(token: str) -> type[_RecordingHandler]:
    """A fresh ``_RecordingHandler`` subclass with its own ``TOKEN`` and
    an empty, non-shared ``calls`` list — one per fake server per test."""
    return type("_RecordingHandlerInstance", (_RecordingHandler,), {"TOKEN": token, "calls": []})


def _mk_chash(base_url: str, token: str):
    return HttpChashIndex(base_url=base_url, _token=token)


def _drive_chash(store) -> None:
    store.rename_collection(old="old_col", new="new_col")


def _mk_aspect_queue(base_url: str, token: str):
    return HttpAspectQueue(base_url=base_url, _token=token)


def _drive_aspect_queue(store) -> None:
    store.enqueue("some_col", "path/to/doc.md", "chash123", "content", doc_id="doc-1")


def _mk_document_aspects(base_url: str, token: str):
    return HttpDocumentAspectsStore(base_url=base_url, _token=token)


def _drive_document_aspects(store) -> None:
    record = AspectRecord(
        collection="some_col",
        source_path="path/to/doc.md",
        problem_formulation="p",
        proposed_method="m",
        extracted_at="2026-09-14T00:00:00Z",
        model_version="v1",
        extractor_name="test-extractor",
        doc_id="doc-tumbler-1",
        confidence=0.9,
    )
    store.upsert(record)


def _mk_document_highlights(base_url: str, token: str):
    return HttpDocumentHighlightsStore(base_url=base_url, _token=token)


def _drive_document_highlights(store) -> None:
    store.rename_collection(old="old_col", new="new_col")


def _mk_taxonomy(base_url: str, token: str):
    return HttpTaxonomyStore(base_url=base_url, _token=token)


def _drive_taxonomy(store) -> None:
    store.record_discover_count("some_col", 5)


def _drive_taxonomy_persist_assignments(store) -> None:
    # Registers through a direct ensure_collection_registered call, not
    # write_with_registration_retry (nexus-dvgsf critic sweep).
    store.persist_assignments([{"source_collection": "some_col", "doc_id": "chash-1", "topic_id": 1}])


def _mk_catalog(base_url: str, token: str):
    return HttpCatalogClient(base_url=base_url, _token=token)


def _drive_catalog_write_manifest_many(store) -> None:
    store.write_manifest_many([("1.1.1", [])], collection="some_col")


#: Collection name each driver's write ultimately registers -- needed by
#: the collision-case test below to pre-register the RIGHT name against
#: the ambient server before driving the "own"-pinned store.
_CASES = [
    pytest.param(_mk_chash, _drive_chash, "new_col", id="HttpChashIndex"),
    pytest.param(_mk_aspect_queue, _drive_aspect_queue, "some_col", id="HttpAspectQueue"),
    pytest.param(_mk_document_aspects, _drive_document_aspects, "some_col", id="HttpDocumentAspectsStore"),
    pytest.param(_mk_document_highlights, _drive_document_highlights, "new_col", id="HttpDocumentHighlightsStore"),
    pytest.param(_mk_taxonomy, _drive_taxonomy, "some_col", id="HttpTaxonomyStore"),
    pytest.param(
        _mk_taxonomy, _drive_taxonomy_persist_assignments, "some_col",
        id="HttpTaxonomyStore.persist_assignments",
    ),
    pytest.param(
        _mk_catalog, _drive_catalog_write_manifest_many, "some_col",
        id="HttpCatalogClient.write_manifest_many",
    ),
]


def _pin_ambient_env(monkeypatch: pytest.MonkeyPatch, ambient_url: str) -> None:
    ambient = urlparse(ambient_url)
    monkeypatch.setenv("NX_SERVICE_HOST", ambient.hostname or "127.0.0.1")
    monkeypatch.setenv("NX_SERVICE_PORT", str(ambient.port))
    monkeypatch.setenv("NX_SERVICE_TOKEN", AMBIENT_TOKEN)
    monkeypatch.delenv("NX_SERVICE_URL", raising=False)


@pytest.mark.parametrize("make_store, drive_write, target_name", _CASES)
def test_registering_write_lands_on_the_stores_own_endpoint(
    make_store, drive_write, target_name, monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Each store's registering write must hit ITS OWN pinned server's
    ``/v1/catalog/collections/upsert`` at least once, and the ambient
    server (live at the env the mixin would otherwise resolve from) must
    NEVER see that registration route — the registrar this store's write
    passes must never be the process-wide ambient default. (The ambient
    server legitimately sees the unrelated, always-ambient embedding-
    profile GET — see ``_RecordingHandler``'s docstring; that call is
    out of this bead's scope and not what these assertions check.)"""
    del target_name  # only the collision-case test below needs it
    own_handler = _handler_subclass(OWN_TOKEN)
    ambient_handler = _handler_subclass(AMBIENT_TOKEN)

    with fake_http_server(own_handler) as own_url, fake_http_server(ambient_handler) as ambient_url:
        _pin_ambient_env(monkeypatch, ambient_url)

        store = make_store(own_url, OWN_TOKEN)
        try:
            drive_write(store)
        finally:
            store.close()

        assert REGISTRATION_ROUTE in own_handler.calls, (
            f"expected {REGISTRATION_ROUTE!r} on the OWN server; got {own_handler.calls!r}"
        )
        assert REGISTRATION_ROUTE not in ambient_handler.calls, (
            f"ambient server must never see {REGISTRATION_ROUTE!r}; got {ambient_handler.calls!r} "
            "(the store's registrar fell back to the process-wide ambient default)"
        )


@pytest.mark.parametrize("make_store, drive_write, target_name", _CASES)
def test_ambient_registration_first_does_not_short_circuit_the_stores_own_registrar(
    make_store, drive_write, target_name, monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Collision case (critic review, gap 1): pre-register *target_name*
    against the AMBIENT engine first — exactly what a different, earlier,
    ambient-only caller in this same process would have done — then drive
    the "own"-pinned store's write for that same name. Before the scope-
    keyed cache partition (``corpus._REGISTERED_COLLECTIONS_SCOPED``),
    ``ensure_collection_registered``'s single name-only cache would have
    seen *target_name* already marked registered from the ambient call
    and short-circuited before ever calling the store's own registrar —
    the store would then write to its OWN engine for a collection that
    was never actually registered THERE, 422ing "not registered" exactly
    like this bead's original symptom, just reached via cache collision
    instead of ambient misrouting. With the fix, the ambient registration
    lands in the unscoped partition and the store's own registration in
    the ``(base_url, tenant)``-scoped one, so the own server must still
    see the registration call."""
    own_handler = _handler_subclass(OWN_TOKEN)
    ambient_handler = _handler_subclass(AMBIENT_TOKEN)

    with fake_http_server(own_handler) as own_url, fake_http_server(ambient_handler) as ambient_url:
        _pin_ambient_env(monkeypatch, ambient_url)

        import nexus.corpus as corpus

        # Pre-register target_name against the ambient engine (the
        # unscoped, name-only cache partition) -- simulating a different,
        # earlier, ambient-only caller in this same process.
        corpus.ensure_collection_registered(target_name)
        assert REGISTRATION_ROUTE in ambient_handler.calls, (
            "test setup: the pre-registration itself must reach the ambient server"
        )

        store = make_store(own_url, OWN_TOKEN)
        try:
            drive_write(store)
        finally:
            store.close()

        assert REGISTRATION_ROUTE in own_handler.calls, (
            f"the store's own registrar must still be called for {target_name!r} even though "
            f"the ambient cache already marked it registered; got {own_handler.calls!r}"
        )


def _multi_bearer_handler(tokens: tuple[str, ...]) -> type[_RecordingHandler]:
    """A ``_RecordingHandler`` accepting any of *tokens*, recording which
    bearer each registration call carried."""

    class _Handler(_RecordingHandler):
        TOKEN = tokens[0]
        calls: list[str] = []
        registration_bearers: list[str] = []

        def _check_auth(self) -> bool:
            auth = self.headers.get("Authorization", "")
            if auth not in {f"Bearer {t}" for t in tokens}:
                self._send(401, {"error": "unauthorized"})
                return False
            if urlparse(self.path).path == REGISTRATION_ROUTE:
                type(self).registration_bearers.append(auth.removeprefix("Bearer "))
            return True

    return _Handler


def test_same_endpoint_and_tenant_with_a_different_bearer_registers_again(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The declared tenant is advisory: the engine binds the tenant from
    the bearer, so two stores on one endpoint that both declare the default
    tenant can be bound to two server tenants (nexus-dvgsf critic, T2
    ``nexus/nexus-dvgsf-critic-2026-09-14``). Each must register the
    collection with its own bearer. With a ``(base_url, tenant)`` cache key
    the second store hits the first store's entry and never registers."""
    tokens = (OWN_TOKEN, f"{OWN_TOKEN}-second-tenant")
    own_handler = _multi_bearer_handler(tokens)
    ambient_handler = _handler_subclass(AMBIENT_TOKEN)

    with fake_http_server(own_handler) as own_url, fake_http_server(ambient_handler) as ambient_url:
        _pin_ambient_env(monkeypatch, ambient_url)
        for token in tokens:
            store = _mk_chash(own_url, token)
            try:
                _drive_chash(store)
            finally:
                store.close()

    assert own_handler.registration_bearers == list(tokens), own_handler.registration_bearers
