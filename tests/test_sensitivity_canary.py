# SPDX-License-Identifier: AGPL-3.0-or-later
# Copyright (c) 2026 Hal Hildebrand. All rights reserved.
"""RDR-243 Phase 1 Step 1 (nexus-rfvyy.6.2): the sensitivity canary.

A document flagged ``--sensitive`` carries a unique string. Every surface in
``src/nexus/tables/llm-surfaces.toml`` whose behaviour is ``refuse`` or ``skip``
runs against a corpus that holds that document, with a capturing LLM backend
and an ``httpx`` mock transport in place, and the string must appear in no
captured LLM payload and no captured outbound request. The test is
parametrized over the table, so a new row brings its own case; it reads the
row's enforcing phase, never a row name.

The registry landed at nexus-rfvyy.6.16. Each ``refuse`` or ``skip`` row at or below
``PHASE_UNDER_TEST`` is a case, and a case with no driver in ``SURFACE_DRIVERS`` FAILS naming the
row; the Phase 1 drivers landed at nexus-rfvyy.6.21 (``tests/test_sensitivity_canary_drivers.py``
holds that every selected row has one and that none is stale). An absent
table still becomes one ``registry-missing`` case whose body fails, never a collection error.

How a row is judged, and why it cannot pass vacuously:

* The corpus holds a PAIR of documents. The sensitive one carries
  ``canary_sensitive``; its control twin is not flagged and carries
  ``canary_control``. After the driver runs the surface, the test asserts
  ``canary_sensitive`` is in no captured payload AND ``canary_control`` IS in
  one. A driver that never reached the LLM, that targeted the wrong documents,
  or whose capture is broken, fails the second half. Removing the gate under
  test makes the first half fail. That is "deleting any one gate turns its row
  red" (RDR Minimum Viable Validation).
* The corpus spans collections of two embedding models (bge-768 and MiniLM-384,
  both of which the substrate embeds without a Voyage key), so
  ``search_cross_corpus`` groups by model and fans out to its thread pools.
  ``_PoolProbe`` records the thread each search call ran on, and the harness
  tests assert a ``ThreadPoolExecutor`` worker ran one.

Drivers: a row's surface is invoked by ``SURFACE_DRIVERS[row.target]``, keyed
by the row's ``module:function``. A selected row with no driver FAILS naming the
row; adding a row to the table means adding its driver here.

A driver runs the surface to the point where the row's own gate is the only protection: where a
deeper layer would refuse the same document too (the extractor's refusal under the worker drain and
under ``nx enrich aspects`` selection), the driver stands an ungated recorder in for that layer, and
says so. The leg a surface sends on is captured where it leaves: the LLM funnels, the ``httpx``
transport, the ``claude -p`` spawn, a store write, or the DEVONthink seam.

No live-store writes: the corpus is seeded into the test substrate's minted
tenant with ``tests/_chunk_seed.py``; the LLM backend and every non-engine
HTTP request are fakes.
"""
from __future__ import annotations

import asyncio
import hashlib
import json
import os
import threading
import uuid
from collections.abc import Callable
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any
from urllib.parse import urlsplit

import httpx
import pytest

from tests._llm_surfaces import Surface, load_surfaces, registry_missing_message

pytestmark = pytest.mark.t2_tenant


@pytest.fixture(autouse=True)
def _no_document_is_sensitive() -> None:
    """Overrides tests/conftest.py's fixture of this name: the canary's subject is the gates, so
    every surface reads the real catalog, where the sensitive document is flagged."""

#: Behaviours the canary judges. ``exclude`` and ``withhold`` are read-side
#: behaviours (phase 2 and 3) with their own scenarios; ``disclose`` and the
#: ``n/a-*`` behaviours send nothing sensitive by definition.
CANARY_BEHAVIOURS: frozenset[str] = frozenset({"refuse", "skip"})

#: Rows enforced at or below this phase run. Phase 1 rows run at nexus-rfvyy.6.21;
#: the Phase 2 rows (``nx_answer`` over two embedding models) raise this to 2 at
#: nexus-rfvyy.7.13.
PHASE_UNDER_TEST = 1

#: Collection model tokens the substrate embeds locally, with no Voyage key.
MODEL_BGE = "bge-base-en-v15-768"
MODEL_MINILM = "minilm-l6-v2-384"

#: The fan-out groups by model, so the pool probe needs at least two models.
MIN_MODELS_IN_CORPUS = 2


# --------------------------------------------------------------------------- #
# The registry, read without ever raising at collection time.
# --------------------------------------------------------------------------- #


def _registry_cases() -> list[Any]:
    surfaces = load_surfaces()
    if surfaces is None:
        return [pytest.param(None, id="registry-missing")]
    selected = sorted(
        (s for s in surfaces if s.behaviour in CANARY_BEHAVIOURS and s.phase <= PHASE_UNDER_TEST),
        key=lambda s: s.name,
    )
    return [pytest.param(s, id=s.name) for s in selected]


# --------------------------------------------------------------------------- #
# Capture: what the LLM backend and the outbound HTTP layer were handed.
# --------------------------------------------------------------------------- #


@dataclass
class Capture:
    """Everything a surface sent out while the capture was armed."""

    llm_payloads: list[str] = field(default_factory=list)
    requests: list[str] = field(default_factory=list)
    spawns: list[str] = field(default_factory=list)
    stores: list[str] = field(default_factory=list)
    engine_request_count: int = 0
    #: What the capturing backend answers a schema-less request with.
    reply_text: str = "{}"
    #: ``(request) -> Response | None`` answerers a driver adds so a fake server (MinerU) can answer
    #: what the default empty-JSON reply would break. The first non-None answer wins; the request is
    #: recorded either way.
    responders: list[Callable[[httpx.Request], httpx.Response | None]] = field(default_factory=list)

    def record_spawn(self, argv: list[str], stdin: str = "") -> None:
        """A driver's fake of a ``claude -p`` spawn calls this with what it was handed."""
        self.spawns.append(json.dumps({"argv": argv, "stdin": stdin}))

    def record_llm(self, label: str, **kw: Any) -> None:
        """A driver's stand-in for a model call that is not routed through the two LLM funnels
        (the extractor the worker or ``nx enrich aspects`` hands a document to)."""
        self.llm_payloads.append(json.dumps({"label": label, **kw}, default=_json_default, sort_keys=True))

    def record_outbound(self, label: str, **kw: Any) -> None:
        """An outward call that is not an ``httpx`` request (the DEVONthink CrossRef leg)."""
        self.requests.append(json.dumps({"label": label, **kw}, default=_json_default, sort_keys=True))

    def record_store(self, label: str, **kw: Any) -> None:
        """What a surface handed to persistence (a queue row): content written to a store is content
        that left the surface, and it is read back by the drain."""
        self.stores.append(json.dumps({"label": label, **kw}, default=_json_default, sort_keys=True))

    @property
    def everything(self) -> str:
        return "\n".join([*self.llm_payloads, *self.requests, *self.spawns, *self.stores])

    def where(self, needle: str) -> str:
        """Which sinks hold *needle*, for the failure message."""
        found = [
            f"{name}[{i}]"
            for name, sink in (
                ("llm", self.llm_payloads), ("request", self.requests), ("spawn", self.spawns), ("store", self.stores),
            )
            for i, entry in enumerate(sink) if needle in entry
        ]
        return ", ".join(found) or "nowhere"


def _json_default(obj: Any) -> str:
    return repr(obj)


def _schema_default(schema: dict[str, Any] | None) -> Any:
    """A minimal value that satisfies *schema*'s required keys, so a surface that
    parses the reply carries on instead of raising."""
    if not schema:
        return {}
    out: dict[str, Any] = {}
    props = schema.get("properties", {})
    for key in schema.get("required", list(props)):
        kind = props.get(key, {}).get("type", "string")
        out[key] = {"string": "", "array": [], "object": {}, "integer": 0, "number": 0, "boolean": False}.get(kind, "")
    return out


class CapturingBackend:
    """An ``LLMBackend`` that records the whole request and answers a canned reply."""

    name = "canary-capture"

    def __init__(self, capture: Capture) -> None:
        self._capture = capture

    def available(self) -> Any:
        from nexus.operators.llm_backend import BackendStatus  # noqa: PLC0415 - nexus import after the substrate is pinned

        return BackendStatus(name=self.name, available=True)

    async def run(self, req: Any) -> Any:
        from nexus.operators.llm_backend import DispatchResult  # noqa: PLC0415 - as above

        self._capture.llm_payloads.append(json.dumps({
            "prompt": req.prompt,
            "json_schema": req.json_schema,
            "operator": req.operator,
            "allowed_tools": req.allowed_tools,
            "mcp_servers": req.mcp_servers,
        }, default=_json_default, sort_keys=True))
        if req.json_schema is None:
            return DispatchResult(text=self._capture.reply_text)
        return DispatchResult(structured=_schema_default(req.json_schema))


def _engine_netloc() -> str:
    """The engine's host:port, from the tenant env the substrate minted."""
    url = os.environ.get("NX_SERVICE_URL", "")
    assert url, "no NX_SERVICE_URL: the test must ask for the tenant (pytestmark t2_tenant)"
    return urlsplit(url).netloc


def _mock_handler(capture: Capture) -> Callable[[httpx.Request], httpx.Response]:
    def handler(request: httpx.Request) -> httpx.Response:
        try:
            body = request.read().decode("utf-8", errors="replace")
        except Exception:  # noqa: BLE001 - an unreadable streaming body is recorded as such, not fatal
            body = "<unreadable body>"
        capture.requests.append(json.dumps({"method": request.method, "url": str(request.url), "body": body}))
        for responder in capture.responders:
            answer = responder(request)
            if answer is not None:
                return answer
        return httpx.Response(200, json={"data": [], "results": [], "total": 0, "meta": {"count": 0}})

    return handler


def _arm_http(monkeypatch: pytest.MonkeyPatch, capture: Capture) -> None:
    """Route every request to a host other than the engine through an
    ``httpx.MockTransport`` that records it. Requests to the engine go through
    unchanged (the substrate is the store under test) and are only counted.

    ``Client.send`` / ``AsyncClient.send`` are the one funnel the verb helpers,
    ``httpx.get``/``post``/``stream`` and the clients all go through."""
    mock = httpx.MockTransport(_mock_handler(capture))
    real_send = httpx.Client.send
    real_asend = httpx.AsyncClient.send
    engine = _engine_netloc()

    def send(self: httpx.Client, request: httpx.Request, **kw: Any) -> httpx.Response:
        if request.url.netloc.decode("ascii") == engine:
            capture.engine_request_count += 1
            return real_send(self, request, **kw)
        response = mock.handle_request(request)
        response.request = request
        response.read()
        return response

    async def asend(self: httpx.AsyncClient, request: httpx.Request, **kw: Any) -> httpx.Response:
        if request.url.netloc.decode("ascii") == engine:
            capture.engine_request_count += 1
            return await real_asend(self, request, **kw)
        response = await mock.handle_async_request(request)
        response.request = request
        await response.aread()
        return response

    monkeypatch.setattr(httpx.Client, "send", send)
    monkeypatch.setattr(httpx.AsyncClient, "send", asend)


def _arm_llm(monkeypatch: pytest.MonkeyPatch, capture: Capture) -> None:
    """Both LLM funnels: ``claude_dispatch`` picks its backend in
    ``dispatch._backend_for``; ``run_text_sync`` builds its own in
    ``llm_backend.build_api_backend``. Every other caller reaches one of them."""
    from nexus.operators import dispatch, llm_backend  # noqa: PLC0415 - after the substrate is pinned

    backend = CapturingBackend(capture)
    monkeypatch.setattr(dispatch, "_backend_for", lambda req: backend)
    monkeypatch.setattr(llm_backend, "build_api_backend", lambda name: backend)


class _PoolProbe:
    """Thread names of every search call on the vector client.

    ``search_cross_corpus`` hands its per-model-group and per-batch searches to
    ``ThreadPoolExecutor``s, so a search that ran on a ``ThreadPoolExecutor-*``
    thread proves a pool task ran. The pools are built inside the function, so
    the probe watches the client methods the tasks call instead of the pools."""

    def __init__(self) -> None:
        self.threads: list[str] = []
        self._lock = threading.Lock()

    def record(self) -> None:
        with self._lock:
            self.threads.append(threading.current_thread().name)

    @property
    def pool_tasks_ran(self) -> int:
        return sum(1 for name in self.threads if name.startswith("ThreadPoolExecutor"))


def _arm_name_grouping(monkeypatch: pytest.MonkeyPatch) -> None:
    """Group collections by the model token in their NAME, not by catalog row.

    The local engine registers every collection under its profile model, so the
    row of ``...__minilm-l6-v2-384__v1`` says ``bge-base-en-v15-768`` and
    ``search_cross_corpus`` sees ONE model group, one engine call and no pool. A
    keyless substrate cannot register two real models, so this makes the search
    group as it does for any collection without a catalog row: by name token.
    The engine embeds the query by each group's registered model, so the search
    itself still works. Process-wide for the test (monkeypatch), so a surface
    that builds its own client is grouped the same way."""
    import nexus.search_engine as se  # noqa: PLC0415 - after the substrate is pinned

    monkeypatch.setattr(se, "_collection_model_resolver", lambda t3, collections: se._model_token)


def _arm_pool_probe(monkeypatch: pytest.MonkeyPatch) -> _PoolProbe:
    import nexus.db.http_vector_client as hvc  # noqa: PLC0415 - after the substrate is pinned

    probe = _PoolProbe()
    watched = sorted(
        n for n in vars(hvc.HttpVectorClient)
        if n.startswith("search") and callable(getattr(hvc.HttpVectorClient, n))
    )
    assert watched, "HttpVectorClient has no search* methods to probe: the pool probe would watch nothing"
    for name in watched:
        original = getattr(hvc.HttpVectorClient, name)

        def wrapped(self: Any, *a: Any, _orig: Callable[..., Any] = original, **kw: Any) -> Any:
            probe.record()
            return _orig(self, *a, **kw)

        monkeypatch.setattr(hvc.HttpVectorClient, name, wrapped)
    return probe


# --------------------------------------------------------------------------- #
# The corpus: a sensitive document and its control twin, over two models.
# --------------------------------------------------------------------------- #


@dataclass(frozen=True)
class CanaryWorld:
    """The seeded corpus and what the capture saw.

    Each document carries its canary string in three places a surface can send: the chunk text,
    the title, and a DOI printed in the text (``10.5555/<canary>``), because the bib and CrossRef rows
    send titles and DOIs and the taxonomy rows send topic terms, never chunk text."""

    canary_sensitive: str
    canary_control: str
    sensitive_tumbler: str
    control_tumbler: str
    collection_bge: str
    collection_minilm: str
    capture: Capture
    probe: _PoolProbe
    text_sensitive: str = ""
    text_control: str = ""
    title_sensitive: str = ""
    title_control: str = ""
    chash_sensitive: str = ""
    chash_control: str = ""
    monkeypatch: pytest.MonkeyPatch | None = None
    workdir: Path | None = None

    @property
    def collections(self) -> list[str]:
        return [self.collection_bge, self.collection_minilm]

    @property
    def doi_sensitive(self) -> str:
        return f"10.5555/{self.canary_sensitive}"

    @property
    def doi_control(self) -> str:
        return f"10.5555/{self.canary_control}"

    def text_of(self, tumbler: str) -> str:
        """The seeded text of the document at *tumbler*; any other tumbler is a driver bug."""
        return {self.sensitive_tumbler: self.text_sensitive, self.control_tumbler: self.text_control}[str(tumbler)]


def _seed_document(collection: str, owner: str, title: str, text: str) -> tuple[str, str]:
    """One catalog document with one live chunk in *collection*; returns ``(tumbler, chash)``."""
    from tests._catalog_fixture_ops import give_chunks_a_live_owner  # noqa: PLC0415 - substrate-backed helper
    from tests._chunk_seed import seed_chunks_direct  # noqa: PLC0415 - as above

    chash = hashlib.sha256(f"{collection}:{text}".encode()).hexdigest()
    seed_chunks_direct(
        collection, ids=[chash], documents=[text], embed=True,
        metadatas=[{"chunk_text_hash": chash, "title": title}],
    )
    return give_chunks_a_live_owner(collection, [chash], title=title, owner_name=owner), chash


def _flag_sensitive(tumbler: str) -> None:
    """Set the one-way flag the way a user does after the fact.

    ``HttpCatalogClient.set_sensitivity(tumblers, sensitive, reason=...)`` (RDR-243 Technical
    Design item 3, nexus-rfvyy.6.8) takes a LIST of tumblers and returns
    ``{"updated": [...], "targets": [...]}``; the canary asserts the flag moved, so a
    document the engine did not change (unknown, tombstoned, already set) fails here."""
    from nexus.catalog.http_catalog_client import HttpCatalogClient  # noqa: PLC0415 - after the substrate is pinned

    with HttpCatalogClient() as cat:
        out = cat.set_sensitivity([tumbler], True, reason="RDR-243 canary corpus")
    assert out["updated"] == [1], f"the canary document was not flagged: {out}"


def build_world(
    monkeypatch: pytest.MonkeyPatch, *, flag: bool = True, workdir: Path | None = None,
) -> CanaryWorld:
    """Seed the pair across two embedding models and arm the captures.

    The sensitive document goes in the bge collection (where Q3 routes a
    sensitive document in local mode), its control twin in the MiniLM one, so
    the pair alone makes ``search_cross_corpus`` group by two models. ``flag=False``
    leaves both unflagged (the harness tests, which need no flag verb)."""
    tag = uuid.uuid4().hex[:10]
    canary_sensitive = f"CANARY-SENSITIVE-{uuid.uuid4().hex}"
    canary_control = f"CANARY-CONTROL-{uuid.uuid4().hex}"
    col_bge = f"knowledge__canary-{tag}-a__{MODEL_BGE}__v1"
    col_minilm = f"knowledge__canary-{tag}-b__{MODEL_MINILM}__v1"

    def body(canary: str, title: str) -> str:
        return (
            f"{title}. Abstract. Quarterly lease renewal memo. {canary}. We study lease renewal. "
            f"doi: 10.5555/{canary}. The tuple lease renewal does not change attempts. References follow."
        )

    title_sensitive = f"{canary_sensitive} lease memo"
    title_control = f"{canary_control} lease memo"
    text_sensitive = body(canary_sensitive, title_sensitive)
    text_control = body(canary_control, title_control)
    sensitive, chash_sensitive = _seed_document(col_bge, f"canary-sensitive-{tag}", title_sensitive, text_sensitive)
    control, chash_control = _seed_document(col_minilm, f"canary-control-{tag}", title_control, text_control)
    if flag:
        _flag_sensitive(sensitive)
    capture = Capture()
    _arm_llm(monkeypatch, capture)
    _arm_http(monkeypatch, capture)
    _arm_name_grouping(monkeypatch)
    probe = _arm_pool_probe(monkeypatch)
    return CanaryWorld(
        canary_sensitive=canary_sensitive, canary_control=canary_control,
        sensitive_tumbler=sensitive, control_tumbler=control,
        collection_bge=col_bge, collection_minilm=col_minilm,
        capture=capture, probe=probe,
        text_sensitive=text_sensitive, text_control=text_control,
        title_sensitive=title_sensitive, title_control=title_control,
        chash_sensitive=chash_sensitive, chash_control=chash_control,
        monkeypatch=monkeypatch, workdir=workdir,
    )


# --------------------------------------------------------------------------- #
# Drivers: one per registry row, keyed by the row's ``module:function``.
# --------------------------------------------------------------------------- #

#: A driver runs the surface against the world's corpus, through the surface's
#: own entry point, and returns. It reaches the LLM only through the captured
#: funnels. Phase 1 drivers land with their gates at nexus-rfvyy.6.21; each
#: names the census row it covers.
SURFACE_DRIVERS: dict[str, Callable[[CanaryWorld], None]] = {}


def driver(target: str) -> Callable[[Callable[[CanaryWorld], None]], Callable[[CanaryWorld], None]]:
    """Register a driver under the registry row's primary ``module:function``."""

    def register(fn: Callable[[CanaryWorld], None]) -> Callable[[CanaryWorld], None]:
        assert target not in SURFACE_DRIVERS, f"two drivers for {target}"
        SURFACE_DRIVERS[target] = fn
        return fn

    return register


def _mp(world: CanaryWorld) -> pytest.MonkeyPatch:
    assert world.monkeypatch is not None, "this driver needs build_world(monkeypatch, ...)"
    return world.monkeypatch


def _pairs(world: CanaryWorld) -> list[tuple[str, str, str, str]]:
    """``(collection, tumbler, title, text)`` for the sensitive document and its control twin."""
    return [
        (world.collection_bge, world.sensitive_tumbler, world.title_sensitive, world.text_sensitive),
        (world.collection_minilm, world.control_tumbler, world.title_control, world.text_control),
    ]


def _an_aspect_record(collection: str, source_path: str) -> Any:
    from nexus.aspect_extractor import AspectRecord  # noqa: PLC0415 - after the substrate is pinned

    return AspectRecord(
        collection=collection, source_path=source_path, problem_formulation="P", proposed_method="M",
        experimental_datasets=["d"], experimental_baselines=["b"], experimental_results="R", extras={},
        confidence=0.9, extracted_at="2026-11-05T00:00:00+00:00", model_version="canary", extractor_name="canary",
    )


# Rows 1-2 -- the aspect queue. ----------------------------------------------------------------


@driver("nexus.aspect_worker:aspect_extraction_enqueue_hook")
def _drive_enqueue_hook(world: CanaryWorld) -> None:
    """Census row 1. The hook fires for each document as an indexer fires it; persistence is a recorder
    at the ``t2_index_write`` seam, so what the hook handed to the queue (the document text rides in the
    row) is what the canary reads. Nothing downstream of the hook runs, so the hook's own gate is the
    only protection."""
    import nexus.mcp_infra as infra  # noqa: PLC0415 - after the substrate is pinned
    from nexus.aspect_worker import aspect_extraction_enqueue_hook  # noqa: PLC0415 - as above

    capture = world.capture

    class _Queue:
        def enqueue(self, collection: str, source_path: str, *, content: str = "", doc_id: str = "") -> None:
            capture.record_store(
                "aspect_queue.enqueue", collection=collection, source_path=source_path, content=content, doc_id=doc_id,
            )

    class _T2:
        aspect_queue = _Queue()

    _mp(world).setattr(infra, "t2_index_write", lambda fn, **kw: fn(_T2()))
    for collection, tumbler, title, text in _pairs(world):
        aspect_extraction_enqueue_hook(f"/docs/{title}.md", collection, text, doc_id=tumbler)


@driver("nexus.aspect_worker:AspectExtractionWorker._extract_tuple_rows")
def _drive_worker_drain(world: CanaryWorld) -> None:
    """Census row 2. Both documents' tasks sit in the ``aspects`` tuple subspace (the only queue since
    nexus-hbnml), written straight to the subspace so the enqueue hook's gate (row 1) is not in the
    way. The worker's tuple side claims them and processes them one at a time, then both in one
    batch; its sensitivity verdict runs between the claim and the extract. A task carries identity,
    never text, so the extractor reads the document itself: the extractor the worker calls is an
    UNGATED recorder that sends what it is handed, the source path that carries the title (and so the
    canary string) included, so the drain's own gate is the only protection: the extractor's refusal
    (rows 3 and 4) is not in the way."""
    from nexus.aspect_worker import AspectExtractionWorker  # noqa: PLC0415 - after the substrate is pinned
    from nexus.db.t2.http_aspect_queue import HttpAspectQueue  # noqa: PLC0415 - as above

    mp = _mp(world)
    capture = world.capture
    mp.setenv("NX_ASPECT_WORKER_AUTOSTART", "0")
    mp.setattr("nexus.aspect_worker.signal_aspect_wake", lambda *a, **k: None)

    def single(content: str = "", source_path: str = "", collection: str = "", **kw: Any) -> Any:
        capture.record_llm("aspect_extract", content=content, source_path=source_path, collection=collection)
        return _an_aspect_record(collection, source_path)

    def batch(items: Any, **kw: Any) -> list[Any]:
        capture.record_llm("aspect_extract_batch", items=[list(i) for i in items])
        return [_an_aspect_record(i[0], i[1]) for i in items]

    mp.setattr("nexus.aspect_worker._extract_aspects", single)
    mp.setattr("nexus.aspect_worker._extract_aspects_batch", batch)
    queue = HttpAspectQueue()
    try:
        for label, batch_size in (("single", 1), ("batch", 2)):
            for collection, tumbler, title, _text in _pairs(world):
                queue._out_task(collection, f"/docs/{label}/{title}.md", f"h-{label}", tumbler)
            side = AspectExtractionWorker(poll_interval=0.1, batch_size=batch_size)._tuple_side
            claimed = 0
            while claimed < 2:
                claims, failed = side.claim_batch(park=False)
                assert not failed and claims, f"harness: both tasks were queued and claimed ({label}): {claimed}"
                claimed += len(claims)
                side.process(claims)
            assert claimed == 2, f"harness: exactly the two tasks were claimed ({label}): {claimed}"
    finally:
        queue.close()


# Rows 3-4 -- the extractor. -------------------------------------------------------------------


def _extract_both(world: CanaryWorld) -> None:
    """``extract_aspects`` (one document) and ``extract_aspects_batch`` over the pair, as the worker and
    ``nx enrich aspects`` call them. A refusal is the surface working, so it is not an error here."""
    import contextlib  # noqa: PLC0415 - stdlib, scoped to the driver

    from nexus.aspect_extractor import extract_aspects, extract_aspects_batch  # noqa: PLC0415 - after the substrate is pinned
    from nexus.sensitivity import SensitiveDocumentRefused  # noqa: PLC0415 - as above

    for collection, tumbler, title, text in _pairs(world):
        with contextlib.suppress(SensitiveDocumentRefused):
            extract_aspects(text, f"/docs/{title}.md", collection, doc_id_lookup=lambda c, s, t=tumbler: t)
    extract_aspects_batch([(collection, f"/docs/b/{title}.md", text, tumbler) for collection, tumbler, title, text in _pairs(world)])


@driver("nexus.aspect_extractor:_run_claude_isolated")
def _drive_extract_via_cli(world: CanaryWorld) -> None:
    """Census row 3. The `claude -p` spawn is a recorder of the argv and the prompt on stdin."""
    import subprocess  # noqa: PLC0415 - stdlib, scoped to the driver

    mp = _mp(world)
    mp.setattr("nexus.aspect_extractor._selected_api_backend", lambda: None)

    def spawn(prompt: str, timeout: float, *, model: str | None = None, _argv: list[str] | None = None) -> Any:
        world.capture.record_spawn(["claude", "-p"], stdin=prompt)
        return subprocess.CompletedProcess(args=[], returncode=0, stdout=json.dumps({"result": '{"papers": []}'}), stderr="")

    mp.setattr("nexus.aspect_extractor._run_claude_isolated", spawn)
    _extract_both(world)


@driver("nexus.aspect_extractor:_api_inner_text")
def _drive_extract_via_api(world: CanaryWorld) -> None:
    """Census row 4. The API backend is the capturing backend behind ``run_text_sync``."""
    mp = _mp(world)
    mp.setattr("nexus.aspect_extractor._selected_api_backend", lambda: "anthropic")
    world.capture.reply_text = '{"papers": []}'
    _extract_both(world)


# Rows 5-6 -- nx enrich aspects. -----------------------------------------------------------------


@driver("nexus.commands.enrich:enrich_aspects")
def _drive_enrich_aspects(world: CanaryWorld) -> None:
    """Census row 5. ``nx enrich aspects --all`` runs over each collection; the extraction it hands
    its selected entries to is an UNGATED recorder (it sends each entry's document text to the
    capture), so the selection drop is the only protection: the extractor's refusal (rows 3 and 4)
    is not in the way."""
    from click.testing import CliRunner  # noqa: PLC0415 - scoped to the driver

    from nexus.commands import enrich as enrich_mod  # noqa: PLC0415 - after the substrate is pinned

    mp = _mp(world)
    assert world.workdir is not None
    mp.setenv("NEXUS_CONFIG_DIR", str(world.workdir))
    mp.setattr(enrich_mod, "_require_llm_backend", lambda: None)

    def extraction(entries: list[Any], collection: str, config: Any) -> list[Any]:
        for entry in entries:
            world.capture.record_llm(
                "aspect_extract", collection=collection, title=entry.title, content=world.text_of(str(entry.tumbler)),
            )
        return []

    mp.setattr(enrich_mod, "_run_extraction", extraction)
    for collection in world.collections:
        result = CliRunner().invoke(enrich_mod.enrich_aspects, [collection, "--all"])
        assert result.exit_code == 0, result.output


@driver("nexus.commands.enrich:_run_validation_sample")
def _drive_validation_sample(world: CanaryWorld) -> None:
    """Census row 6. The pair is extracted (one record each, the claim carrying the document's canary)
    and a 100% validation sample is drawn over each collection. The verifier call is the real
    ``_verify`` through the capturing backend; the evidence is the indexed text of the sampled
    document, read from the seeded chunks."""
    from nexus.commands import enrich as enrich_mod  # noqa: PLC0415 - after the substrate is pinned

    assert world.workdir is not None
    for collection, tumbler, title, text in _pairs(world):
        record = _an_aspect_record(collection, title)
        record.problem_formulation = f"How the lease memo says: {text[:80]}"
        record.proposed_method = title
        enrich_mod._run_validation_sample(
            [(title, record, title)], collection=collection, sample_pct=100,
            validation_out=str(world.workdir / "failures.jsonl"), validation_all_out=str(world.workdir / "all.jsonl"),
        )


# Rows 7-8 -- bibliographic enrichment. ---------------------------------------------------------


@driver("nexus.commands.enrich:run_bib_enrichment")
def _drive_enrich_bib(world: CanaryWorld) -> None:
    """Census row 7. ``nx enrich bib`` over each collection, against Semantic Scholar and against
    OpenAlex (both are the mock transport). The title, and the DOI scanned from the chunks, are what
    the lookup sends."""
    from nexus.commands import enrich as enrich_mod  # noqa: PLC0415 - after the substrate is pinned

    for source in ("s2", "openalex"):
        for collection in world.collections:
            enrich_mod.run_bib_enrichment(collection, delay=0, source=source)


@driver("nexus.commands.enrich:_dt_crossref_bib")
def _drive_dt_crossref(world: CanaryWorld) -> None:
    """Census row 8. The DEVONthink CrossRef gap-fill (``nx dt index --enrich`` runs exactly this
    pass). DEVONthink is an MCP stdio call, not an ``httpx`` request, so the seam
    ``dt_resolve_doi`` is a recorder that puts the DOI it was handed into the capture. The driver
    asserts the control's DOI reached that seam, so the CrossRef leg itself ran."""
    from nexus.commands import enrich as enrich_mod  # noqa: PLC0415 - after the substrate is pinned

    mp = _mp(world)
    mp.setattr("nexus.mcp_client.devonthink.available", lambda: True)
    # The primary lookup (Semantic Scholar) is a silent stub, so a title sent there cannot stand in for the
    # CrossRef leg: whatever this run sends is sent through the gap-fill.
    mp.setattr(enrich_mod, "_resolve_bib_backend", lambda source: ("s2", lambda *a, **k: {}, "semantic_scholar_id"))

    def resolve(doi: str) -> None:
        world.capture.record_outbound("crossref-via-dt", doi=doi)

    mp.setattr("nexus.mcp_client.devonthink.dt_resolve_doi", resolve)
    for collection in world.collections:
        enrich_mod.run_bib_enrichment(collection, delay=0, source="dt")
    assert any(
        '"crossref-via-dt"' in r and world.canary_control in r for r in world.capture.requests
    ), "harness: the CrossRef gap-fill never asked about the control document's DOI, so the leg did not run"


# Rows 9-10 -- the taxonomy passes. ------------------------------------------------------------


class _Taxonomy:
    """The taxonomy store with the reads the label and review passes make, per collection."""

    def __init__(self, topics: list[dict[str, Any]], members: dict[int, list[str]]) -> None:
        self._topics = topics
        self._members = members
        self.labels: dict[int, str] = {}
        self.reviewed: dict[int, str] = {}

    def _of(self, collection: str) -> list[dict[str, Any]]:
        return [t for t in self._topics if not collection or t["collection"] == collection]

    def get_unreviewed_topics(self, collection: str = "", limit: int = 0) -> list[dict[str, Any]]:
        return self._of(collection)

    def get_all_topics(self, collection: str = "") -> list[dict[str, Any]]:
        return self._of(collection)

    def get_topic_doc_ids(self, topic_id: int, *, limit: int = 3) -> list[str]:
        return self._members[topic_id][:limit]

    def get_all_topic_doc_ids(self, topic_id: int) -> list[str]:
        return list(self._members[topic_id])

    def get_projection_counts_by_collection(self) -> dict[str, int]:
        return {}

    def update_topic_label(self, topic_id: int, label: str) -> None:
        self.labels[topic_id] = label

    def rename_topic(self, topic_id: int, label: str) -> None:
        self.labels[topic_id] = label

    def mark_topic_reviewed(self, topic_id: int, status: str) -> None:
        self.reviewed[topic_id] = status


def _taxonomy_world(world: CanaryWorld) -> _Taxonomy:
    """One pending topic per collection. Its terms are the document's canary string plus ordinary words
    (a topic's terms come from its documents' text), and its member is the document's chunk."""
    topics: list[dict[str, Any]] = []
    members: dict[int, list[str]] = {}
    for topic_id, (collection, canary, chash) in enumerate(
        [
            (world.collection_bge, world.canary_sensitive, world.chash_sensitive),
            (world.collection_minilm, world.canary_control, world.chash_control),
        ],
        start=1,
    ):
        terms = [canary, "lease", "renewal"]
        topics.append({
            "id": topic_id, "label": " ".join(terms), "terms": json.dumps(terms), "collection": collection,
            "doc_count": 1, "review_status": "pending",
        })
        members[topic_id] = [chash]
    return _Taxonomy(topics, members)


def _arm_taxonomy(world: CanaryWorld, store: _Taxonomy) -> Any:
    from types import SimpleNamespace  # noqa: PLC0415 - stdlib, scoped to the driver

    import nexus.mcp_infra as infra  # noqa: PLC0415 - after the substrate is pinned
    from nexus.commands import taxonomy_cmd  # noqa: PLC0415 - as above

    db = SimpleNamespace(taxonomy=store, memory=SimpleNamespace(get=lambda **kw: None))
    mp = _mp(world)
    mp.setattr(infra, "t2_index_write", lambda fn, **kw: fn(db))
    mp.setattr(taxonomy_cmd, "_save_review_cache", lambda *a, **k: None)
    return db


@driver("nexus.commands.taxonomy_cmd:_generate_labels_batch")
def _drive_taxonomy_label(world: CanaryWorld) -> None:
    """Census row 9. ``nx taxonomy label`` (``relabel_topics``) over each collection's pending topic;
    the label request goes through the capturing backend."""
    from nexus.commands import taxonomy_cmd  # noqa: PLC0415 - after the substrate is pinned

    store = _taxonomy_world(world)
    _arm_taxonomy(world, store)
    for collection in world.collections:
        taxonomy_cmd.relabel_topics(store, collection=collection, workers=1)


@driver("nexus.commands.taxonomy_cmd:_generate_review_verdicts_batch")
def _drive_taxonomy_review(world: CanaryWorld) -> None:
    """Census row 10. ``nx taxonomy review --auto`` over each collection's pending topic."""
    from nexus.commands import taxonomy_cmd  # noqa: PLC0415 - after the substrate is pinned

    store = _taxonomy_world(world)
    db = _arm_taxonomy(world, store)
    for collection in world.collections:
        taxonomy_cmd._review_auto(db, collection, limit=50, yes=True, dry_run=False, batch_size=10)


# Row 21 -- MinerU remote extraction. ------------------------------------------------------------


@driver("nexus.pdf_extractor:PDFExtractor._mineru_run_via_server")
def _drive_mineru_remote(world: CanaryWorld) -> None:
    """Census row 21. A PDF per document carries the document's canary string in its page text and in its
    plain-bytes info dictionary, so the string is in the bytes a MinerU server would be sent. Both are indexed once with
    the local text extractor to register them; the sensitive one is then flagged in the CATALOG (the
    state of a document flagged earlier), and both are re-indexed with ``extractor="mineru"`` and no
    verb flag. The MinerU server is a fake behind the capturing transport (health, ``/file_parse``);
    the local MinerU subprocess is an in-process fake, because a sensitive document must go there.
    The server fake sees the PDF bytes of every document that reaches it: the control's, never the
    sensitive one's."""
    import re  # noqa: PLC0415 - stdlib, scoped to the driver

    import pymupdf  # noqa: PLC0415 - the PDF stack, scoped to the driver

    from nexus.catalog.factory import make_catalog_reader  # noqa: PLC0415 - after the substrate is pinned
    from nexus.doc_indexer import index_pdf  # noqa: PLC0415 - as above
    from nexus.pdf_extractor import PDFExtractor  # noqa: PLC0415 - as above

    mp = _mp(world)
    assert world.workdir is not None
    corpus = f"canary-pdf-{uuid.uuid4().hex[:8]}"

    def make_pdf(name: str, canary: str) -> Path:
        doc = pymupdf.open()
        doc.new_page().insert_textbox(
            pymupdf.Rect(72, 72, 520, 760), f"Lease renewal memo {canary}. " * 6, fontsize=11,
        )
        # The page content stream is Flate-compressed, so the canary rides in the document info
        # dictionary too, which MuPDF writes as plain bytes.
        doc.set_metadata({"title": canary, "subject": f"lease renewal memo {canary}"})
        path = world.workdir / name
        doc.save(str(path), garbage=0, deflate=False)
        doc.close()
        assert canary.encode() in path.read_bytes(), f"harness: the canary is not in {name}'s raw bytes"
        return path

    pdfs = {
        "sensitive": make_pdf("canary-sensitive.pdf", world.canary_sensitive),
        "control": make_pdf("canary-control.pdf", world.canary_control),
    }
    for pdf in pdfs.values():
        index_pdf(pdf, corpus=corpus, extractor="pymupdf", force=True, streaming="never")
    reader = make_catalog_reader()
    (entry,) = reader.find_all_by_file_path(str(pdfs["sensitive"].resolve()))
    _flag_sensitive(str(entry.tumbler))

    def server(request: httpx.Request) -> httpx.Response | None:
        if request.url.path.endswith("/file_parse"):
            return httpx.Response(200, json={"results": {"served": {
                "md_content": "Extracted by the remote server.", "content_list": "[]", "middle_json": '{"pdf_info": []}',
            }}})
        return httpx.Response(200, json={"status": "ok"})

    world.capture.responders.append(server)
    mp.setattr("nexus.config.get_mineru_server_url", lambda *a, **k: "http://mineru.canary.invalid:8123")
    mp.setattr(
        PDFExtractor, "_mineru_run_subprocess",
        lambda self, pdf_path, start, end, formula_enable=True, **kw: ("Extracted by the local subprocess.", [], []),
    )
    for pdf in pdfs.values():
        index_pdf(pdf, corpus=corpus, extractor="mineru", force=True, streaming="never")
    assert re.search(r"/file_parse", world.capture.everything), (
        "harness: no PDF reached the MinerU server fake, so the leg did not run"
    )


def _run_search_fanout(world: CanaryWorld, tenant: Any) -> list[Any]:
    """The search every read-side surface goes through, over both models."""
    import nexus.db.http_vector_client as hvc  # noqa: PLC0415 - after the substrate is pinned
    from nexus.search_engine import search_cross_corpus  # noqa: PLC0415 - as above

    client = hvc.HttpVectorClient(tenant=tenant)
    return search_cross_corpus(
        "tuple lease renewal does not change attempts", world.collections, 5, client,
        threshold_override=float("inf"), cluster_by=None,
    )


# --------------------------------------------------------------------------- #
# The canary.
# --------------------------------------------------------------------------- #


@pytest.mark.parametrize("surface", _registry_cases())
def test_canary_is_absent_from_every_payload_of_a_refusing_surface(
    surface: Surface | None, t2_service_env: Any, monkeypatch: pytest.MonkeyPatch, tmp_path: Path,
) -> None:
    assert surface is not None, registry_missing_message()

    driver = SURFACE_DRIVERS.get(surface.target)
    assert driver is not None, (
        f"registry row {surface.name!r} ({surface.target}, behaviour {surface.behaviour}, "
        f"phase {surface.phase}) has no driver in SURFACE_DRIVERS: a row brings its own case, "
        f"so add a driver for {surface.target!r} to tests/test_sensitivity_canary.py"
    )

    world = build_world(monkeypatch, workdir=tmp_path)
    assert len({c.split("__")[2] for c in world.collections}) >= MIN_MODELS_IN_CORPUS
    driver(world)
    problem = judge(world, surface.name)
    assert problem is None, problem


def judge(world: CanaryWorld, surface_name: str) -> str | None:
    """The canary's verdict on one finished run: ``None`` when it holds, else why not.

    Two halves, both required: the sensitive string went nowhere, AND the control
    twin's string did reach a payload (else the run proved nothing)."""
    cap = world.capture
    counts = (
        f"{len(cap.llm_payloads)} LLM payload(s), {len(cap.requests)} outbound request(s), "
        f"{len(cap.spawns)} spawn(s), {len(cap.stores)} store write(s)"
    )
    if world.canary_sensitive in cap.everything:
        return (
            f"surface {surface_name!r} sent the sensitive document's text out: the canary string is in "
            f"{cap.where(world.canary_sensitive)} of {counts}"
        )
    if world.canary_control not in cap.everything:
        return (
            f"surface {surface_name!r} never sent the control document either ({counts}): the run did not "
            "reach the LLM, so the absence of the sensitive canary proves nothing"
        )
    return None


# --------------------------------------------------------------------------- #
# The harness, pinned on its own so the canary above cannot pass vacuously.
# These need no registry and no flag verb, so they run today.
# --------------------------------------------------------------------------- #


def test_harness_corpus_spans_two_models_and_the_fanout_runs_a_pool_task(
    t2_service_env: Any, monkeypatch: pytest.MonkeyPatch,
) -> None:
    world = build_world(monkeypatch, flag=False)
    results = _run_search_fanout(world, t2_service_env)

    assert {r.collection for r in results} == set(world.collections), "both models' collections answered"
    assert world.probe.pool_tasks_ran >= 1, (
        f"no search ran on a ThreadPoolExecutor worker (threads: {sorted(set(world.probe.threads))}): "
        "the corpus did not fan out, so the canary would not exercise the pools"
    )
    assert any(world.canary_control in (r.content or "") for r in results), "the control text came back in a result"


def test_harness_capture_sees_the_llm_payload_and_a_non_engine_request(
    t2_service_env: Any, monkeypatch: pytest.MonkeyPatch,
) -> None:
    from nexus.operators.dispatch import claude_dispatch  # noqa: PLC0415 - after the substrate is pinned
    from nexus.operators.llm_backend import run_text_sync  # noqa: PLC0415 - as above

    world = build_world(monkeypatch, flag=False)
    planted = f"PLANT-{uuid.uuid4().hex}"
    asyncio.run(claude_dispatch(f"summarize {planted}", {"type": "object", "properties": {}}))
    run_text_sync("anthropic", f"extract {planted}", model=None, timeout=5.0)
    httpx.post("https://api.example.invalid/v1/x", json={"text": planted})
    with httpx.Client() as client:
        client.get(f"https://papers.example.invalid/{planted}")

    cap = world.capture
    assert len(cap.llm_payloads) == 2 and all(planted in p for p in cap.llm_payloads)
    assert len(cap.requests) == 2 and planted in cap.requests[0] and planted in cap.requests[1]
    assert world.canary_sensitive not in cap.everything and planted in cap.everything


def test_harness_judge_fails_a_leak_and_fails_a_run_that_reached_nothing(
    t2_service_env: Any, monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The verdict has two ways to go red. Each is shown on a fake surface, so a
    canary row cannot pass by sending nothing or fail to notice a leak."""
    from nexus.operators.dispatch import claude_dispatch  # noqa: PLC0415 - after the substrate is pinned

    world = build_world(monkeypatch, flag=False)
    assert "reach the LLM" in (judge(world, "idle") or ""), "a surface that sends nothing is not a pass"

    asyncio.run(claude_dispatch(f"label {world.canary_control}", {"type": "object", "properties": {}}))
    assert judge(world, "clean") is None

    asyncio.run(claude_dispatch(f"label {world.canary_sensitive}", {"type": "object", "properties": {}}))
    assert "sent the sensitive document's text out" in (judge(world, "leaky") or "")

    other = build_world(monkeypatch, flag=False)
    httpx.post("https://api.example.invalid/v1/x", content=other.canary_sensitive.encode())
    asyncio.run(claude_dispatch(f"label {other.canary_control}", {"type": "object", "properties": {}}))
    assert "sent the sensitive document's text out" in (judge(other, "leaks-by-http") or ""), (
        "a leak in an outbound HTTP body, with a clean LLM payload, is caught"
    )
