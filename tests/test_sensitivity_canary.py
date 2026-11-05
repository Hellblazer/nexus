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
row: that red is expected until the driver and the gate land (nexus-rfvyy.6.21). An absent
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
row; adding a row to the table means adding its driver here. The drivers land
with their gates (nexus-rfvyy.6.21).

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
from typing import Any
from urllib.parse import urlsplit

import httpx
import pytest

from tests._llm_surfaces import Surface, load_surfaces, registry_missing_message

pytestmark = pytest.mark.t2_tenant

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
    engine_request_count: int = 0

    def record_spawn(self, argv: list[str], stdin: str = "") -> None:
        """A driver's fake of a ``claude -p`` spawn calls this with what it was handed."""
        self.spawns.append(json.dumps({"argv": argv, "stdin": stdin}))

    @property
    def everything(self) -> str:
        return "\n".join([*self.llm_payloads, *self.requests, *self.spawns])


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
            return DispatchResult(text="")
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
        return httpx.Response(200, json={})

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
    """The seeded corpus and what the capture saw."""

    canary_sensitive: str
    canary_control: str
    sensitive_tumbler: str
    control_tumbler: str
    collection_bge: str
    collection_minilm: str
    capture: Capture
    probe: _PoolProbe

    @property
    def collections(self) -> list[str]:
        return [self.collection_bge, self.collection_minilm]


def _seed_document(collection: str, owner: str, text: str) -> str:
    """One catalog document with one live chunk in *collection*; returns its tumbler."""
    from tests._catalog_fixture_ops import give_chunks_a_live_owner  # noqa: PLC0415 - substrate-backed helper
    from tests._chunk_seed import seed_chunks_direct  # noqa: PLC0415 - as above

    chash = hashlib.sha256(f"{collection}:{text}".encode()).hexdigest()
    seed_chunks_direct(
        collection, ids=[chash], documents=[text], embed=True,
        metadatas=[{"chunk_text_hash": chash, "title": f"{owner}.md"}],
    )
    return give_chunks_a_live_owner(collection, [chash], title=f"{owner}.md", owner_name=owner)


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


def build_world(monkeypatch: pytest.MonkeyPatch, *, flag: bool = True) -> CanaryWorld:
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
    sensitive = _seed_document(
        col_bge, f"canary-sensitive-{tag}",
        f"Quarterly lease renewal memo. {canary_sensitive}. The tuple lease renewal does not change attempts.",
    )
    control = _seed_document(
        col_minilm, f"canary-control-{tag}",
        f"Quarterly lease renewal memo. {canary_control}. The tuple lease renewal does not change attempts.",
    )
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
    )


# --------------------------------------------------------------------------- #
# Drivers: one per registry row, keyed by the row's ``module:function``.
# --------------------------------------------------------------------------- #

#: A driver runs the surface against the world's corpus, through the surface's
#: own entry point, and returns. It reaches the LLM only through the captured
#: funnels. Phase 1 drivers land with their gates at nexus-rfvyy.6.21; each
#: names the census row it covers.
SURFACE_DRIVERS: dict[str, Callable[[CanaryWorld], None]] = {}


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
    surface: Surface | None, t2_service_env: Any, monkeypatch: pytest.MonkeyPatch,
) -> None:
    assert surface is not None, registry_missing_message()

    driver = SURFACE_DRIVERS.get(surface.target)
    assert driver is not None, (
        f"registry row {surface.name!r} ({surface.target}, behaviour {surface.behaviour}, "
        f"phase {surface.phase}) has no driver in SURFACE_DRIVERS: a row brings its own case, "
        f"so add a driver for {surface.target!r} to tests/test_sensitivity_canary.py"
    )

    world = build_world(monkeypatch)
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
        f"{len(cap.spawns)} spawn(s)"
    )
    if world.canary_sensitive in cap.everything:
        return f"surface {surface_name!r} sent the sensitive document's text out: the canary string is in {counts}"
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
