# SPDX-License-Identifier: AGPL-3.0-or-later
# Copyright (c) 2026 Hal Hildebrand. All rights reserved.
"""RDR-243 Phase 1 Step 1 (nexus-rfvyy.6.2): the sensitivity canary.

A document flagged ``--sensitive`` carries a unique string. Every surface in
``src/nexus/tables/llm-surfaces.toml`` whose behaviour is ``refuse``, ``skip`` or ``exclude``
and whose phase is at or below ``PHASE_UNDER_TEST`` runs against a corpus that holds that
document, with a capturing LLM backend and an ``httpx`` mock transport in place, and the string
must appear in no captured LLM payload, outbound request, spawn, store write or return value.
The test is parametrized over the table, so a new row brings its own case; it reads the row's
enforcing phase, never a row name.

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
import types
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

#: Behaviours the canary judges. ``exclude`` is the read-side behaviour of Phase 2 (the engine
#: drops a sensitive chunk from a read made under ``llm_bound``); ``withhold`` is Phase 3's output
#: side (the MCP read tools and the CLI replace a sensitive hit with a one-line stub); ``disclose``
#: and the ``n/a-*`` behaviours send nothing sensitive by definition.
CANARY_BEHAVIOURS: frozenset[str] = frozenset({"refuse", "skip", "exclude", "withhold"})

#: Rows enforced at or below this phase run. Phase 1 rows (``refuse``/``skip``) landed at
#: nexus-rfvyy.6.21; the Phase 2 ``exclude`` rows (the ``nx_answer`` family, ``nx_tidy``,
#: ``nx_enrich_beads``, ``nx_plan_audit``) at nexus-rfvyy.7.13; the Phase 3 rows (the Knowledge Map
#: and the rerank ``exclude`` rows, the MCP read tools and the CLI ``withhold`` rows) at
#: nexus-rfvyy.8 fix r1.
PHASE_UNDER_TEST = 3

#: Judged ``exclude`` rows whose surface makes no search through ``search_cross_corpus``, keyed by
#: the row's primary target, with the reason. An ``exclude`` row not named here must show, in its own
#: case, that a search ran on a ``ThreadPoolExecutor`` worker: a read that never fanned out proves
#: nothing about the engine filter the pools carry.
NON_SEARCH_EXCLUDE_ROWS: dict[str, str] = {
    "nexus.hooks.subagent_start:_knowledge_map_section": (
        "the Knowledge Map is topic labels read from a cache file the taxonomy wrote; no search runs"
    ),
}

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
    # An ``exclude`` row reads through the engine's filter, so it needs the live ``read_sensitivity``
    # (conftest pins ``include`` otherwise); a write-side row keeps the pin it has always run under.
    # ``plan_grow_generalizer`` releases the conftest stub of ``_generalize_grown_match_description``, which
    # the plan-helpers row drives for real (its dispatch ends in the capturing backend, never a model).
    read_marks = [pytest.mark.sensitivity_reads, pytest.mark.plan_grow_generalizer]
    return [
        pytest.param(s, id=s.name, marks=read_marks if s.behaviour == "exclude" else [])
        for s in selected
    ]


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
    #: What a surface handed BACK to its caller (the session model reads it): a tool's return value, a
    #: plan run's step outputs and final answer.
    returns: list[str] = field(default_factory=list)
    #: Every request the capturing backend served, as the object the funnel built, for a driver that
    #: asserts on the tool grant a dispatch carried.
    dispatched: list[Any] = field(default_factory=list)
    #: ``operator name -> (request) -> structured reply``: what a driver scripts a dispatch to answer
    #: (the planner must hand back a plan, or the surface stops before it reads anything).
    scripted: dict[str, Callable[[Any], Any]] = field(default_factory=dict)
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

    def record_return(self, label: str, **kw: Any) -> None:
        """What a surface returned to its caller. A value the session model reads is as much an outbound
        payload as a prompt, and an ``exclude`` row's reads end there."""
        self.returns.append(json.dumps({"label": label, **kw}, default=_json_default, sort_keys=True))

    @property
    def everything(self) -> str:
        return "\n".join([*self.llm_payloads, *self.requests, *self.spawns, *self.stores, *self.returns])

    def where(self, needle: str) -> str:
        """Which sinks hold *needle*, for the failure message."""
        found = [
            f"{name}[{i}]"
            for name, sink in (
                ("llm", self.llm_payloads), ("request", self.requests), ("spawn", self.spawns), ("store", self.stores),
                ("return", self.returns),
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

        self._capture.dispatched.append(req)
        self._capture.llm_payloads.append(json.dumps({
            "prompt": req.prompt,
            "json_schema": req.json_schema,
            "operator": req.operator,
            "allowed_tools": req.allowed_tools,
            "mcp_servers": req.mcp_servers,
        }, default=_json_default, sort_keys=True))
        scripted = self._capture.scripted.get(req.operator)
        if scripted is not None:
            return DispatchResult(structured=scripted(req))
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
            # "any": the corpus is knowledge notes, and the canary wants the widest reach, not
            # the paper-only default (GH #1611 item 5).
            enrich_mod.run_bib_enrichment(collection, delay=0, source=source, content_type="any")


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
        enrich_mod.run_bib_enrichment(collection, delay=0, source="dt", content_type="any")
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


# Rows 12-17 -- the Phase 2 ``exclude`` rows: the read side. ----------------------------------------
#
# These run the surface under its own ``llm_bound`` scope with the LIVE ``read_sensitivity()`` (the
# case carries ``sensitivity_reads``), against the real engine and the real catalog, over the two-model
# corpus, so the search fans out to its pools and the engine's own ``exclude`` filter is the first line.
# Nothing is stubbed on the read path: the capturing backend stands in for the model, and a surface
# that hydrates through the catalog keeps its catalog refusal (the second line). A leak is judged on
# the canary string AND on the sensitive chunk's id (``judge(..., reads=True)``): a chunk the engine let
# through and the runner then caught still turns the case red, because the engine filter is the line
# these rows are about.

#: What the search steps ask. The corpus text answers it, so both documents are near.
_READ_QUERY = "tuple lease renewal does not change attempts"


def _arm_reads(world: CanaryWorld) -> None:
    """A search page cached by an earlier case in this worker must not answer this one."""
    from nexus.mcp import core  # noqa: PLC0415 - after the substrate is pinned

    core._page_cache_invalidate()


def _corpus_arg(world: CanaryWorld) -> str:
    return ",".join(world.collections)


def _search_step(world: CanaryWorld) -> dict[str, Any]:
    return {
        "tool": "search",
        "args": {"query": _READ_QUERY, "corpus": _corpus_arg(world), "limit": 10, "threshold": float("inf")},
    }


def _run_plan(world: CanaryWorld, plan: dict[str, Any], *, surface: str, bundle: bool = False) -> Any:
    """Run *plan* the way ``nx_answer`` runs one, inside the row's own scope; what comes back is recorded
    as a return value (the step outputs are what the next step and the caller read)."""
    from nexus.plans.match import Match  # noqa: PLC0415 - after the substrate is pinned
    from nexus.plans.runner import plan_run  # noqa: PLC0415 - as above
    from nexus.sensitivity import llm_bound  # noqa: PLC0415 - as above

    match = Match(
        plan_id=1, name="canary", description="canary", confidence=0.9, dimensions={"verb": "test"}, tags="",
        plan_json=json.dumps(plan), required_bindings=[], optional_bindings=[], default_bindings={}, parent_dims=None,
    )

    async def run() -> Any:
        with llm_bound(surface):
            return await plan_run(match, {}, bundle_operators=bundle)

    result = asyncio.run(run())
    world.capture.record_return(f"plan_run:{surface}", steps=result.steps, final=result.final)
    return result


@driver("nexus.mcp.core:operator_extract")
def _drive_nx_answer_operators(world: CanaryWorld) -> None:
    """Census row 12. Two plans run inside ``nx_answer_operators``. The chash route: a search over both
    models' collections, then ``summarize`` and ``extract`` over the hit ids, hydrated by chunk id. The
    tumbler route: ``summarize`` over the two documents' tumblers named outright (no search, so the
    catalog's refusal is the only line). The control document's text must reach an operator prompt on
    the chash route, and the sensitive chunk's id must not be among the search hits."""
    _arm_reads(world)
    chash_plan = {"steps": [
        _search_step(world),
        {"tool": "summarize", "args": {"ids": "$step1.ids", "collections": "$step1.collections"}},
        {"tool": "extract", "args": {"ids": "$step1.ids", "collections": "$step1.collections", "fields": "title"}},
    ]}
    result = _run_plan(world, chash_plan, surface="nx_answer_operators")
    assert result.steps[0].get("ids"), "harness: the search step found nothing, so no operator was handed a hit"
    tumbler_plan = {"steps": [
        {"tool": "summarize", "args": {"ids": [world.sensitive_tumbler, world.control_tumbler]}},
    ]}
    _run_plan(world, tumbler_plan, surface="nx_answer_operators")


@driver("nexus.plans.bundle:dispatch_bundle")
def _drive_nx_answer_bundle(world: CanaryWorld) -> None:
    """Census row 13. A search, then two adjacent operators fused into ONE dispatch
    (``bundle_operators=True``): the fused step's inputs are hydrated from the hit ids."""
    _arm_reads(world)
    plan = {"steps": [
        _search_step(world),
        {"tool": "summarize", "args": {"ids": "$step1.ids", "collections": "$step1.collections"}},
        {"tool": "generate", "args": {"template": "memo", "context": "$step2.summary"}},
    ]}
    result = _run_plan(world, plan, surface="nx_answer_bundle", bundle=True)
    assert any(r.source == "bundle" for r in result.step_records), (
        f"harness: no step ran as a fused bundle ({[r.source for r in result.step_records]}), so dispatch_bundle did not run"
    )


@driver("nexus.mcp.core:_nx_answer_plan_miss")
def _drive_nx_answer_plan_helpers(world: CanaryWorld) -> None:
    """Census row 14. The three helpers run in the order ``nx_answer`` runs them on a plan miss: request
    coverage, the planner (scripted to answer a search-and-summarize plan, so the miss path reaches a
    read), the plan itself under ``nx_answer_operators``, then the grown-plan description. The helpers
    send the question and plan text, never a document, so what this row proves is that nothing a read
    brought back reaches THEM: the planner prompt, the coverage extraction and the generalizer prompt are
    each captured and held to the canary, and the read the plan makes is the one the other rows judge."""
    from nexus.mcp import core  # noqa: PLC0415 - after the substrate is pinned
    from nexus.operators import dispatch  # noqa: PLC0415 - as above

    _arm_reads(world)
    # The suite's autouse guard (tests/conftest.py ``_no_live_structured_extraction``) makes the coverage
    # extraction fail before it reaches a backend. Here the backend is the capturing one, so no model is
    # called: the guard's own documented release is to replace ``claude_dispatch``, and the unwrapped function
    # still ends in ``_backend_for``, which ``_arm_llm`` has pointed at the capture.
    _mp(world).setattr(dispatch, "claude_dispatch", dispatch.claude_dispatch.__wrapped__)
    plan = {"steps": [
        _search_step(world),
        {"tool": "summarize", "args": {"ids": "$step1.ids", "collections": "$step1.collections"}},
    ]}
    world.capture.scripted["planner"] = lambda req: plan

    async def helpers() -> Any:
        evidence = await core._nx_answer_evidence_request(bindings=None, scope=_corpus_arg(world))
        coverage = await core._nx_answer_request_coverage(
            question=_READ_QUERY, scope=_corpus_arg(world), evidence_request=evidence,
            explicit_kind=None, deadline=None, trace=False,
        )
        match = await core._nx_answer_plan_miss(_READ_QUERY, scope=_corpus_arg(world), coverage=coverage)
        return match

    match = asyncio.run(helpers())
    result = _run_plan(world, json.loads(match.plan_json), surface="nx_answer_operators")
    assert result.steps[0].get("ids"), "harness: the planned search found nothing"
    asyncio.run(core._generalize_grown_match_description(question=_READ_QUERY, plan_json=match.plan_json))
    dispatched = [getattr(r, "operator", None) for r in world.capture.dispatched]
    for operator, what in (
        ("request_coverage_extract", "the request-coverage extraction"),
        ("planner", "the planner"),
        ("plan_grow_generalize", "the grown-plan description"),
    ):
        assert operator in dispatched, f"harness: {what} was never dispatched (saw {dispatched}), so that helper was not judged"


@driver("nexus.mcp.core:nx_tidy")
def _drive_nx_tidy(world: CanaryWorld) -> None:
    """Census row 15. ``nx_tidy`` over both collections: its prefetch searches and hydrates, and inlines
    the entries into the prompt. The summary line counts what the CATALOG check removed after the
    engine's filter; a nonzero count means the engine let a sensitive entry through."""
    from nexus.mcp import core  # noqa: PLC0415 - after the substrate is pinned

    _arm_reads(world)
    out = asyncio.run(core.nx_tidy(topic=_READ_QUERY, collection=_corpus_arg(world)))
    world.capture.record_return("nx_tidy", result=out)
    assert "Sensitive entries withheld from this pass: 0" in out, (
        f"nx_tidy withheld sensitive entries the engine should have excluded, or did not report the exclude posture: {out!r}"
    )


def _child_reads(world: CanaryWorld, request: Any) -> None:
    """What the dispatched ``claude -p`` child's nested ``nx-mcp`` would hand its model.

    No test launches ``claude``. The grant the dispatch carried is checked (the nested servers are named
    in ``allowed_tools`` and their registration exports ``llm_bound``), and then the child's read tools are
    called in process the way the server calls them: inside ``child_scope()``, the scope the nested
    server's middleware enters for every ``tools/call``. ``search`` and ``store_get_many`` are the reads
    the child's exploration uses; each result is what the child's model would read, so it is a payload."""
    from nexus.mcp import core  # noqa: PLC0415 - after the substrate is pinned
    from nexus.sensitivity import POSTURE_ENV, child_scope  # noqa: PLC0415 - as above

    servers = request.mcp_servers or {}
    assert "mcp__nexus" in (request.allowed_tools or []), f"harness: the dispatch granted no nx MCP tools: {request.allowed_tools}"
    assert servers.get("nexus", {}).get("env", {}).get(POSTURE_ENV) == "llm_bound", (
        f"the child's nx-mcp registration does not export {POSTURE_ENV}=llm_bound: {servers.get('nexus')}"
    )
    core._page_cache_invalidate()
    with child_scope():
        found = core.search(query=_READ_QUERY, corpus=_corpus_arg(world), limit=10, threshold=float("inf"))
        world.capture.record_llm("child_tool_result", tool="search", result=found)
        got = core.store_get_many(
            ids=[world.chash_sensitive, world.chash_control],
            collections=[world.collection_bge, world.collection_minilm],
        )
        # The tool names an id it could not return in its ``missing`` list, so the sensitive id the child ASKED
        # for comes back in the result whether or not the chunk was read. It is not a read; the canary text is
        # the evidence here (a chunk that was read carries it), so the echoed id is masked.
        masked = str(got).replace(world.chash_sensitive, "<the sensitive id the child asked for>")
        world.capture.record_llm("child_tool_result", tool="store_get_many", result=masked)


@driver("nexus.mcp.core:nx_enrich_beads")
def _drive_nx_enrich_beads(world: CanaryWorld) -> None:
    """Census row 16. ``nx_enrich_beads`` dispatches with the nx MCP tools granted; the child's reads are
    driven in process (``_child_reads``)."""
    from nexus.mcp import core  # noqa: PLC0415 - after the substrate is pinned

    _arm_reads(world)
    out = asyncio.run(core.nx_enrich_beads(bead_description="Renew the tuple lease on a schedule."))
    world.capture.record_return("nx_enrich_beads", result=out)
    (request,) = [r for r in world.capture.dispatched if r.mcp_servers]
    _child_reads(world, request)


@driver("nexus.mcp.core:nx_plan_audit")
def _drive_nx_plan_audit(world: CanaryWorld) -> None:
    """Census row 17. As row 16, for ``nx_plan_audit``."""
    from nexus.mcp import core  # noqa: PLC0415 - after the substrate is pinned

    _arm_reads(world)
    out = asyncio.run(core.nx_plan_audit(plan_json='{"steps": [{"tool": "search", "args": {"query": "lease"}}]}'))
    world.capture.record_return("nx_plan_audit", result=out)
    (request,) = [r for r in world.capture.dispatched if r.mcp_servers]
    _child_reads(world, request)


# Rows 11, 20, 23 and 24 -- the Phase 3 rows: the Knowledge Map, rerank, and the two output postures. ----
#
# Rows 23 and 24 (``withhold``) run the surface with its DEFAULT output posture against the real engine and
# the real catalog, with the default read posture (``include``), so the sensitive hit reaches the output
# layer flagged by the engine and the posture is the only protection: the canary text and title must be
# in no return value, and the control twin's must be. Row 11 (the Knowledge Map) and row 20 (rerank) are
# judged where Python can observe them (see each driver). A mutation case per row removes the row's gate
# and must turn it red (``test_harness_a_phase_3_gate_removed_turns_its_row_red``).


class _KnowledgeMapTaxonomy:
    """Two root topics whose labels were computed BEFORE the sensitive document was flagged: the sensitive
    document's topic carries its canary in the label (a label is words of the topic's members), the control's
    carries the control's. Each topic holds its document's one chunk."""

    def __init__(self, world: CanaryWorld) -> None:
        self.topics: dict[int, dict[str, Any]] = {
            1: {"id": 1, "label": f"{world.canary_sensitive} lease renewal", "parent_id": None,
                "collection": world.collection_bge, "doc_count": 9},
            2: {"id": 2, "label": f"{world.canary_control} lease renewal", "parent_id": None,
                "collection": world.collection_minilm, "doc_count": 4},
        }
        self.assignments = {world.chash_sensitive: 1, world.chash_control: 2}

    def get_topics(self, *, parent_id: int | None = None) -> list[dict[str, Any]]:
        return sorted((t for t in self.topics.values() if t["parent_id"] == parent_id), key=lambda t: -t["doc_count"])

    def get_assignments_for_docs(self, doc_ids: list[str]) -> dict[str, int]:
        return {d: self.assignments[d] for d in doc_ids if d in self.assignments}

    def get_topic_by_id(self, topic_id: int) -> dict[str, Any] | None:
        return self.topics.get(topic_id)

    def update_topic_label(self, topic_id: int, label: str) -> None:
        self.topics[topic_id] = {**self.topics[topic_id], "label": label}


@driver("nexus.hooks.subagent_start:_knowledge_map_section")
def _drive_knowledge_map(world: CanaryWorld) -> None:
    """Census row 11. The sensitive document was flagged after its topic was built, so the topic's label
    carries its words. The flagging step (what ``nx catalog sensitive`` runs after setting the flag:
    ``sensitive_topics.withhold_after_flag``, against the real catalog and a taxonomy double) relabels the
    topics holding the document and drops the cached map; the Knowledge Map is then generated and read back
    the way the SubagentStart hook reads it, and that section is what every subagent's context receives."""
    import hashlib  # noqa: PLC0415 - stdlib, scoped to the driver

    from nexus import context, sensitive_topics  # noqa: PLC0415 - after the substrate is pinned
    from nexus.catalog.http_catalog_client import HttpCatalogClient  # noqa: PLC0415 - as above
    from nexus.hooks.subagent_start import _knowledge_map_section  # noqa: PLC0415 - as above

    mp = _mp(world)
    assert world.workdir is not None
    mp.setenv("NEXUS_CONFIG_DIR", str(world.workdir))
    taxonomy = _KnowledgeMapTaxonomy(world)

    # The relabel reaches T2 through ``nexus.mcp_infra.t2_index_write``; the taxonomy double stands in for the
    # tenant's topics (the topics are seeded by this driver, not by a discover run).
    mp.setattr(
        "nexus.mcp_infra.t2_index_write",
        lambda write_fn, **_kw: write_fn(types.SimpleNamespace(taxonomy=taxonomy)),
    )
    with HttpCatalogClient() as cat:
        sensitive_topics.withhold_after_flag(cat, [world.sensitive_tumbler])
    repo_root = str(world.workdir / "repo")
    cache = world.workdir / "context" / f"repo-{hashlib.sha1(repo_root.encode()).hexdigest()[:8]}.txt"  # noqa: S324 - the hook's cache key
    context.generate_context_l1(taxonomy, output_path=cache)
    section = _knowledge_map_section(repo_root)
    assert section.strip(), "harness: the Knowledge Map section the hook reads is empty"
    world.capture.record_llm("knowledge_map_section", section=section)


@driver("nexus.search_engine:search_cross_corpus")
def _drive_rerank(world: CanaryWorld) -> None:
    """Census row 20. The Voyage rerank request itself is made by the engine, to a fixed host, and what the
    engine puts in it is pinned ONLY in Java: ``VoyageRerankSensitivityTest`` asserts the raw request bodies
    (the sensitive row's text is in none of them) and is the named engine check for this row. Python cannot
    observe that leg, so this driver judges the leg Python owns, the client's rerank gate
    (``HttpVectorClient._rerank_permitted``), against the real engine:

    * a ``lexical=true`` ``include`` search through the real route must actually ASK the engine to rerank (a
      ``rerank: true`` search body is captured at ``_post``). A search that never requested one proves nothing
      about the hold, so that is a harness failure, not a pass;
    * the engine's own report of whether it keeps sensitive rows out of the reranker (``GET /version``'s
      ``rerank_sensitive_exclusion_supported``) is read through the same ``_get`` the client uses, and a rerank
      request that goes to an engine that does not report it is recorded as the sensitive text handed over: an
      engine without the hold reranks every candidate it is given through Voyage, sensitive rows included.

    The read is an ``include`` read whatever the case's marks say: the session model's own search, the one that
    can reach a sensitive row, is what the hold exists for (an ``exclude`` read would never see the row). The
    page the session model reads is captured as a return value, so the output posture is judged here too."""
    from nexus.db import http_vector_client as hvc  # noqa: PLC0415 - after the substrate is pinned
    from nexus.mcp import core  # noqa: PLC0415 - as above

    mp = _mp(world)
    mp.setattr("nexus.sensitivity.read_sensitivity", lambda posture=None: "include")
    _arm_reads(world)
    rerank_bodies: list[dict[str, Any]] = []
    tenants: list[str] = []
    real_post = hvc._post

    def spy(path: str, body: dict, **kw: Any) -> Any:
        if path.startswith("/v1/vectors/") and "search" in path and isinstance(body, dict) and body.get("rerank") is True:
            rerank_bodies.append({"path": path, "sensitivity": body.get("sensitivity")})
            tenants.append(kw.get("tenant", "default"))
        return real_post(path, body, **kw)

    mp.setattr(hvc, "_post", spy)
    out = core.search(query=_READ_QUERY, corpus=_corpus_arg(world), limit=10, threshold=float("inf"), lexical=True)
    world.capture.record_return("search(lexical=true)", result=out)
    assert rerank_bodies, (
        "harness: the include search never asked the engine to rerank (no search body carried rerank: true), so "
        "the row's gate was not exercised"
    )
    info = hvc._get("/version", tenant=tenants[0], timeout=10.0)
    holds = isinstance(info, dict) and info.get("rerank_sensitive_exclusion_supported") is True
    if not holds:
        world.capture.record_outbound(
            "rerank_request_to_an_engine_without_the_hold", requests=rerank_bodies, would_hand_over=world.canary_sensitive,
        )


@driver("nexus.mcp.core:search")
def _drive_mcp_read_tools(world: CanaryWorld) -> None:
    """Census row 23. The MCP read tools, each called as the session model calls it, over the pair; what each
    returns is a return value. ``search``, ``query``, ``store_get``/``store_get_many`` by the sensitive chunk's
    id, ``store_list`` (flat and by document), the scoped searches that take a query and a corpus, and the
    catalog tools (``search``, ``show``, ``list``, ``links``, ``link_query``) over the sensitive document's
    tumbler. The default output posture (``withhold``) is the only protection."""
    from nexus.mcp import core  # noqa: PLC0415 - after the substrate is pinned
    from nexus.mcp_server import (  # noqa: PLC0415 - as above
        catalog_link_query, catalog_links, catalog_list, catalog_search, catalog_show,
    )

    cap = world.capture
    _arm_reads(world)
    corpus = _corpus_arg(world)
    cap.record_return("search", result=core.search(query=_READ_QUERY, corpus=corpus, limit=10, threshold=float("inf")))
    cap.record_return("query", result=core.query(question=_READ_QUERY, corpus=corpus, limit=10))
    cap.record_return("search_metadata_scoped", result=core.search_metadata_scoped(query=_READ_QUERY, corpus=corpus, limit=10))
    cap.record_return("store_get", result=core.store_get(world.chash_sensitive, collection=world.collection_bge))
    cap.record_return("store_get", result=core.store_get(world.chash_control, collection=world.collection_minilm))
    cap.record_return(
        "store_get_many",
        result=core.store_get_many(
            ids=[world.chash_sensitive, world.chash_control], collections=[world.collection_bge, world.collection_minilm],
        ),
    )
    for collection in world.collections:
        cap.record_return("store_list", result=core.store_list(collection=collection))
        cap.record_return("store_list(docs)", result=core.store_list(collection=collection, docs=True))
    cap.record_return("catalog_search", result=catalog_search(query="lease memo"))
    cap.record_return("catalog_list", result=catalog_list(content_type="knowledge"))
    cap.record_return("catalog_show", result=catalog_show(tumbler=world.sensitive_tumbler))
    cap.record_return("catalog_show", result=catalog_show(tumbler=world.control_tumbler))
    cap.record_return("catalog_links", result=catalog_links(tumbler=world.sensitive_tumbler))
    cap.record_return("catalog_link_query", result=catalog_link_query(tumbler=world.sensitive_tumbler))


@driver("nexus.commands.search_cmd:search_cmd")
def _drive_cli_output(world: CanaryWorld) -> None:
    """Census row 24. The CLI verbs a Claude Code Bash call reads, run under the Claude Code marker so the
    posture is the one Q2 decided (``withhold``): ``nx search`` in each text format and ``--json``,
    ``nx store list`` and ``nx store list --docs``, ``nx store get`` of the sensitive chunk, and
    ``nx catalog show``/``list``/``search``/``links``. Their stdout is a return value."""
    from click.testing import CliRunner  # noqa: PLC0415 - scoped to the driver

    from nexus.cli import main  # noqa: PLC0415 - after the substrate is pinned

    mp = _mp(world)
    mp.setenv("CLAUDECODE", "1")
    cap = world.capture
    corpus = _corpus_arg(world)
    runs: list[list[str]] = []
    for flags in ([], ["--content"], ["--compact"], ["--vimgrep"], ["--files"], ["--json"]):
        runs.append(["search", _READ_QUERY, "--corpus", corpus, "--no-threshold", *flags])
    for collection in world.collections:
        runs.append(["store", "list", "-c", collection])
        runs.append(["store", "list", "-c", collection, "--docs"])
    runs.append(["store", "get", world.chash_sensitive, "-c", world.collection_bge])
    runs.append(["store", "get", world.chash_control, "-c", world.collection_minilm])
    runs.append(["catalog", "show", world.sensitive_tumbler])
    runs.append(["catalog", "show", world.control_tumbler])
    runs.append(["catalog", "list", "--json"])
    runs.append(["catalog", "search", "lease memo"])
    runs.append(["catalog", "links", world.sensitive_tumbler, "--json"])
    runs.append(["catalog", "links", world.control_tumbler, "--resolve"])
    for argv in runs:
        result = CliRunner().invoke(main, argv)
        cap.record_return("nx " + " ".join(argv[:2]), exit_code=result.exit_code, stdout=result.stdout, stderr=result.stderr)


def _run_search_fanout(world: CanaryWorld, tenant: Any) -> list[Any]:
    """The search every read-side surface goes through, over both models."""
    import nexus.db.http_vector_client as hvc  # noqa: PLC0415 - after the substrate is pinned
    from nexus.search_engine import search_cross_corpus  # noqa: PLC0415 - as above

    client = hvc.HttpVectorClient(tenant=tenant)
    return search_cross_corpus(
        "tuple lease renewal does not change attempts", world.collections, 5, client,
        threshold_override=float("inf"), cluster_by=None, sensitivity="include",
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
    reads = surface.behaviour == "exclude"
    problem = judge(world, surface.name, reads=reads, pool=reads and surface.target not in NON_SEARCH_EXCLUDE_ROWS)
    assert problem is None, problem


def judge(world: CanaryWorld, surface_name: str, *, reads: bool = False, pool: bool = False) -> str | None:
    """The canary's verdict on one finished run: ``None`` when it holds, else why not.

    Two halves, both required: the sensitive string went nowhere, AND the control
    twin's string did reach a payload (else the run proved nothing).

    ``reads`` (an ``exclude`` row) adds the engine's line: the sensitive chunk's id is in no payload either.
    A catalog refusal downstream can keep the text out of a prompt after the engine let the chunk through,
    and the id is what shows the engine did not drop it. ``pool`` (a search-shaped row) adds that a search
    ran on a ``ThreadPoolExecutor`` worker in THIS case, so the read went through ``search_cross_corpus``'s
    pools and not around them."""
    cap = world.capture
    counts = (
        f"{len(cap.llm_payloads)} LLM payload(s), {len(cap.requests)} outbound request(s), "
        f"{len(cap.spawns)} spawn(s), {len(cap.stores)} store write(s), {len(cap.returns)} return value(s)"
    )
    if world.canary_sensitive in cap.everything:
        return (
            f"surface {surface_name!r} sent the sensitive document's text out: the canary string is in "
            f"{cap.where(world.canary_sensitive)} of {counts}"
        )
    if reads and world.chash_sensitive in cap.everything:
        return (
            f"surface {surface_name!r} read the sensitive chunk: its id is in {cap.where(world.chash_sensitive)} of "
            f"{counts}, so the engine's exclude did not drop it from a read made under llm_bound"
        )
    if world.canary_control not in cap.everything:
        return (
            f"surface {surface_name!r} never sent the control document either ({counts}): the run did not "
            "reach the LLM, so the absence of the sensitive canary proves nothing"
        )
    if pool and world.probe.pool_tasks_ran < 1:
        return (
            f"surface {surface_name!r} ran no search on a ThreadPoolExecutor worker (threads: "
            f"{sorted(set(world.probe.threads))}): its read did not fan out through search_cross_corpus's pools"
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


def test_harness_judge_of_a_read_row_fails_a_chunk_id_leak_and_a_missing_pool(
    t2_service_env: Any, monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The two extra halves of an ``exclude`` verdict each go red on a fake surface, and a write-side
    verdict (``reads=False``) ignores them, so the Phase 1 rows keep the verdict they always had."""
    from concurrent.futures import ThreadPoolExecutor  # noqa: PLC0415 - stdlib, scoped to the test

    world = build_world(monkeypatch, flag=False)
    world.capture.record_llm("operator", text=world.canary_control)
    assert judge(world, "reader", reads=True) is None, "a clean run with the control present holds"
    assert "ThreadPoolExecutor" in (judge(world, "reader", reads=True, pool=True) or ""), (
        "a search-shaped row whose read ran on no pool worker is red"
    )

    with ThreadPoolExecutor(max_workers=1) as pool:
        pool.submit(world.probe.record).result()
    assert judge(world, "reader", reads=True, pool=True) is None, "a pool worker ran a search in this case"

    world.capture.record_return("search", ids=[world.chash_sensitive])
    assert "did not drop it" in (judge(world, "reader", reads=True, pool=True) or ""), (
        "the sensitive chunk's id in a return value is a read of the chunk, even with its text kept out"
    )
    assert judge(world, "writer") is None, "a refuse or skip row is not judged on the chunk id"


def _exclude_cases() -> list[Any]:
    """The ``exclude`` rows that read through the search pools. A non-search row (the Knowledge Map) is not
    moved by a search mutation; its own gate is removed in ``test_harness_a_phase_3_gate_removed_turns_its_row_red``."""
    return [
        c for c in _registry_cases()
        if c.values and c.values[0] is not None and c.values[0].behaviour == "exclude"
        and c.values[0].target not in NON_SEARCH_EXCLUDE_ROWS
    ]


def _every_exclude_row_goes_red_when_a_read_path_ignores_the_posture(
    surface: Surface, monkeypatch: pytest.MonkeyPatch, tmp_path: Path, *, route: bool,
) -> None:
    """The mutation: the client method a search read goes through (``search_per_collection`` with the
    per-collection route opted in, else ``search``, the batched ``/v1/vectors/search`` read that is the
    default since nexus-tu8wp.3) is made to send ``include`` whatever the caller passed, which is what
    hard-coding ``sensitivity="include"`` at its ``search_engine`` call site does. Every Phase 2 row must
    go red: with the engine filter gone, the canary text, the sensitive chunk's id or the surface's own
    report of it reaches a payload. A row that stays green here would pass with its filter deleted."""
    import nexus.db.http_vector_client as hvc  # noqa: PLC0415 - after the substrate is pinned

    if route:
        monkeypatch.setenv(hvc.PER_COLLECTION_ROUTE_ENV, "1")
    else:
        monkeypatch.delenv(hvc.PER_COLLECTION_ROUTE_ENV, raising=False)
    world = build_world(monkeypatch, workdir=tmp_path)
    method = "search_per_collection" if route else "search"
    current = getattr(hvc.HttpVectorClient, method)

    def include_always(self: Any, *a: Any, **kw: Any) -> Any:
        kw["sensitivity"] = "include"
        return current(self, *a, **kw)

    monkeypatch.setattr(hvc.HttpVectorClient, method, include_always)
    # RDR-243 Phase 3 (nexus-rfvyy.8.2): the MCP search tools now withhold a sensitive hit at their output whatever
    # the read asked for, so with the engine filter deleted that second layer would hide the leak this mutation is
    # meant to show. The mutation removes both layers: the output posture reads ``flag`` for the run.
    import nexus.sensitive_output as so  # noqa: PLC0415 - after the substrate is pinned

    monkeypatch.setattr(so, "mcp_output_setting", lambda: so.McpOutputSetting("flag", "flag"))
    try:
        SURFACE_DRIVERS[surface.target](world)
    except AssertionError as exc:
        red = str(exc)
    else:
        red = judge(world, surface.name, reads=True, pool=True) or ""
    assert any(k in red for k in ("sensitive", "engine")), (
        f"row {surface.name!r} stayed green on the {'route' if route else 'batched /search'} path "
        f"with every search read as include: {red!r}"
    )


@pytest.mark.sensitivity_reads
@pytest.mark.parametrize("surface", _exclude_cases())
def test_harness_a_read_path_that_ignores_the_posture_turns_every_exclude_row_red(
    surface: Surface, t2_service_env: Any, monkeypatch: pytest.MonkeyPatch, tmp_path: Path,
) -> None:
    """The mutation, in the suite, on both read paths in one tenant: first the per-collection route (opt-in
    since nexus-tu8wp.3, so this phase opts in) with ``search_per_collection`` sending ``include``, then the
    default batched ``/v1/vectors/search`` path (route off) with ``search`` sending ``include``. Each phase
    builds its own world in its own undo scope, and each must turn the row red."""
    for route in (True, False):
        workdir = tmp_path / ("route" if route else "batched")
        workdir.mkdir()
        with pytest.MonkeyPatch.context() as mp:
            _every_exclude_row_goes_red_when_a_read_path_ignores_the_posture(surface, mp, workdir, route=route)


# --------------------------------------------------------------------------- #
# Phase 3: one real-substrate end-to-end test, and a mutation per row.
# --------------------------------------------------------------------------- #


def test_mcp_withhold_end_to_end_on_the_real_engine(
    t2_service_env: Any, monkeypatch: pytest.MonkeyPatch, tmp_path: Path,
) -> None:
    """Real engine row -> ``SearchResult.metadata`` -> stub. The engine flags the sensitive hit (``sensitive:
    true``, from the live owner's catalog flag) and ``search_cross_corpus`` carries the flag into the result's
    metadata; the MCP ``search`` tool, under the default output posture, replaces that hit with the stub line
    that names the document's tumbler and prints the control twin whole."""
    from nexus.mcp import core  # noqa: PLC0415 - after the substrate is pinned

    world = build_world(monkeypatch, workdir=tmp_path)
    hits = _run_search_fanout(world, t2_service_env)
    flags = {r.collection: r.metadata.get("sensitive") for r in hits}
    assert flags[world.collection_bge] is True, f"the engine did not flag the sensitive hit: {flags}"
    assert flags[world.collection_minilm] is False, f"the engine flagged the control hit: {flags}"

    core._page_cache_invalidate()
    out = core.search(query=_READ_QUERY, corpus=_corpus_arg(world), limit=10, threshold=float("inf"))
    assert f"[sensitive] {world.sensitive_tumbler}: content withheld" in out, out
    assert "1 sensitive hit withheld" in out
    assert world.canary_control in out, "the control twin prints whole"
    assert world.canary_sensitive not in out and world.chash_sensitive not in out, out


def _mutate_labels_not_withheld(mp: pytest.MonkeyPatch) -> None:
    mp.setattr("nexus.sensitive_topics.withhold_topic_labels", lambda *a, **k: [])


def _mutate_mcp_posture_off(mp: pytest.MonkeyPatch) -> None:
    import nexus.sensitive_output as so  # noqa: PLC0415 - after the substrate is pinned

    mp.setattr(so, "mcp_output_setting", lambda: so.McpOutputSetting("flag", "flag"))


def _mutate_rerank_gate_removed_on_a_keyless_engine(mp: pytest.MonkeyPatch) -> None:
    """Row 20's gate removed: the engine answers ``/version`` without ``rerank_sensitive_exclusion_supported``
    (an engine that predates the hold) and the client's ``_rerank_permitted`` is bypassed, so an ``include``
    search sends ``rerank: true`` to it."""
    import nexus.db.http_vector_client as hvc  # noqa: PLC0415 - after the substrate is pinned

    real_get = hvc._get

    def keyless_get(path: str, **kw: Any) -> Any:
        out = real_get(path, **kw)
        if path == "/version" and isinstance(out, dict):
            return {k: v for k, v in out.items() if k != "rerank_sensitive_exclusion_supported"}
        return out

    mp.setattr(hvc, "_get", keyless_get)
    mp.setattr(hvc.HttpVectorClient, "_rerank_permitted", lambda self, rerank, sensitivity, rerank_meta_out: rerank)


def _mutate_cli_posture_off(mp: pytest.MonkeyPatch) -> None:
    mp.setattr("nexus.session.started_by_claude_code", lambda: False)
    mp.setattr("nexus.commands.search_cmd.started_by_claude_code", lambda: False)


#: The gate each Phase 3 row stands on, removed. A row with no entry here fails
#: ``tests/test_sensitivity_canary_drivers.py::test_every_phase_3_row_has_a_mutation``.
PHASE_3_MUTATIONS: dict[str, Callable[[pytest.MonkeyPatch], None]] = {
    "nexus.hooks.subagent_start:_knowledge_map_section": _mutate_labels_not_withheld,
    "nexus.search_engine:search_cross_corpus": _mutate_rerank_gate_removed_on_a_keyless_engine,
    "nexus.mcp.core:search": _mutate_mcp_posture_off,
    "nexus.commands.search_cmd:search_cmd": _mutate_cli_posture_off,
}


def _phase_3_cases() -> list[Any]:
    return [c for c in _registry_cases() if c.values and c.values[0] is not None and c.values[0].phase == 3]


@pytest.mark.parametrize("surface", _phase_3_cases())
def test_harness_a_phase_3_gate_removed_turns_its_row_red(
    surface: Surface, t2_service_env: Any, monkeypatch: pytest.MonkeyPatch, tmp_path: Path,
) -> None:
    """The mutation, per Phase 3 row: the gate the row stands on is removed (labels left as they were, the MCP
    output posture off, the CLI treated as a terminal) and the row's own driver must show the leak."""
    mutate = PHASE_3_MUTATIONS.get(surface.target)
    assert mutate is not None, f"row {surface.name!r} has no mutation in PHASE_3_MUTATIONS"
    world = build_world(monkeypatch, workdir=tmp_path)
    mutate(monkeypatch)
    SURFACE_DRIVERS[surface.target](world)
    red = judge(world, surface.name, reads=surface.behaviour == "exclude")
    assert red is not None and "sent the sensitive document's text out" in red, (
        f"row {surface.name!r} stayed green with its gate removed: {red!r}"
    )
