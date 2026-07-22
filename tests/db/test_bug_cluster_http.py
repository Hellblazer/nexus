# SPDX-License-Identifier: AGPL-3.0-or-later
"""nexus-tjvgf + nexus-bgh2j: HTTP-layer correctness pair.

tjvgf — non-idempotent verbs must never ride the mixin's retry axes: a
lost RESPONSE after a successful server-side apply double-applies on
retry (a re-claimed queue row orphans the first claim; mark_retry
double-increments the retry budget toward premature terminal failure;
put_or_merge's merge branch appends the same content twice). Fix:
``idempotent=False`` issues the request exactly once.

bgh2j — the two STANDALONE (non-mixin) stores resolved their endpoint at
construction with the BARE resolver while every mixin adopter got the
evidence-gated bounded wait; the gate is now public in service_endpoint
and both stores alias it.
"""
from __future__ import annotations

import httpx
import pytest

from nexus.db import service_endpoint as se
from nexus.db.t2._refreshable_client import RefreshableHttpStoreMixin


class _Probe(RefreshableHttpStoreMixin):
    """Minimal adopter: counts transport attempts + re-resolves."""

    def __init__(self) -> None:
        self._base_url = "http://127.0.0.1:9"
        self.attempts = 0
        self.reresolves = 0
        self.fail_with: Exception | None = None

    def _request_once(self, method, path, **kwargs):  # noqa: ANN001, ANN003, ANN202
        self.attempts += 1
        if self.fail_with is not None:
            raise self.fail_with
        return {"ok": True}

    def _invalidate_and_reresolve(self) -> None:
        self.reresolves += 1


def _gateway_503() -> httpx.HTTPStatusError:
    req = httpx.Request("POST", "http://x/y")
    return httpx.HTTPStatusError(
        "503", request=req, response=httpx.Response(503, request=req),
    )


class TestIdempotentOptOut:
    def test_non_idempotent_gateway_failure_is_single_attempt(self, monkeypatch) -> None:
        import nexus.db.t2._refreshable_client as rc

        monkeypatch.setattr(rc.time, "sleep", lambda s: None)
        p = _Probe()
        p.fail_with = _gateway_503()
        with pytest.raises(httpx.HTTPStatusError):
            p._post("/claim_next", {}, idempotent=False)
        assert p.attempts == 1, "idempotent=False must never retry a 503"
        assert p.reresolves == 0

    def test_non_idempotent_connect_failure_never_reresolves(self) -> None:
        p = _Probe()
        p.fail_with = httpx.ConnectError("refused")
        with pytest.raises(httpx.ConnectError):
            p._post("/mark_retry", {}, idempotent=False)
        assert p.attempts == 1
        assert p.reresolves == 0, "idempotent=False must never take the re-resolve axis"

    def test_default_still_retries_gateway(self, monkeypatch) -> None:
        """The opt-out must not weaken the default path (regression pin)."""
        import nexus.db.t2._refreshable_client as rc

        monkeypatch.setattr(rc.time, "sleep", lambda s: None)
        p = _Probe()
        p.fail_with = _gateway_503()
        with pytest.raises(httpx.HTTPStatusError):
            p._post("/anything", {})
        assert p.attempts > 2, "default (idempotent) path lost its gateway retry loop"

    @pytest.mark.parametrize("verb,call", [
        ("claim_next", lambda q: q.claim_next()),
        ("claim_batch", lambda q: q.claim_batch(5)),
        ("mark_retry", lambda q: q.mark_retry("col", "src", interval_seconds=5)),
    ])
    def test_aspect_queue_verbs_opt_out(self, verb, call, monkeypatch) -> None:
        from nexus.db.t2.http_aspect_queue import HttpAspectQueue

        captured: dict = {}

        def _capture_send(self, method, path, *, idempotent=True, **kwargs):
            captured["path"] = path
            captured["idempotent"] = idempotent
            return {"rows": [], "row": None}

        monkeypatch.setattr(RefreshableHttpStoreMixin, "_send", _capture_send)
        q = HttpAspectQueue(base_url="http://127.0.0.1:9", _token="t")
        try:
            call(q)
        except Exception:  # noqa: BLE001,S110 — response-shape mismatch is irrelevant; the capture is the assertion
            pass
        assert captured.get("idempotent") is False, (
            f"{verb} must pass idempotent=False (nexus-tjvgf)"
        )

    def test_put_or_merge_opts_out(self, monkeypatch) -> None:
        from nexus.db.t2.http_memory_store import HttpMemoryStore

        captured: dict = {}

        def _capture_send(self, method, path, *, idempotent=True, **kwargs):
            captured["idempotent"] = idempotent
            return {"id": 1, "action": "merged"}

        monkeypatch.setattr(RefreshableHttpStoreMixin, "_send", _capture_send)
        store = HttpMemoryStore(base_url="http://127.0.0.1:9", _token="t")
        store.put_or_merge("p", "t", "content")
        assert captured.get("idempotent") is False, (
            "put_or_merge's merge branch appends content — must not retry"
        )


class TestConstructionEvidenceGate:
    def test_cold_process_fails_fast_no_wait(self, monkeypatch) -> None:
        calls: list = []

        def _fake_resolve(**kw):
            calls.append(kw)
            raise se.ServiceEndpointUnresolvableError("no lease")

        monkeypatch.setattr(se, "resolve_service_endpoint", _fake_resolve)
        monkeypatch.setattr(se, "_has_ever_resolved_lease", False)
        with pytest.raises(se.ServiceEndpointUnresolvableError):
            se.resolve_service_endpoint_with_evidence_gate()
        assert calls == [{}], "cold process must fail fast — exactly one no-budget attempt"

    def test_prior_evidence_retries_with_budget(self, monkeypatch) -> None:
        calls: list = []

        def _fake_resolve(**kw):
            calls.append(kw)
            if not kw:
                raise se.ServiceEndpointUnresolvableError("gap")
            return ("http://127.0.0.1:8080", "tok")

        monkeypatch.setattr(se, "resolve_service_endpoint", _fake_resolve)
        monkeypatch.setattr(se, "_has_ever_resolved_lease", True)
        assert se.resolve_service_endpoint_with_evidence_gate() == (
            "http://127.0.0.1:8080", "tok",
        )
        assert calls == [{}, {"wait_budget_s": se.DEFAULT_LEASE_WAIT_BUDGET_S}]

    def test_standalone_stores_alias_the_gated_resolver(self) -> None:
        """bgh2j: both previously-bare construction-time resolvers now point
        at the public gate (call sites unchanged — alias identity is the
        whole fix)."""
        from nexus.db import http_scratch_store
        from nexus.db.t2 import http_token_store

        assert http_token_store._resolve_endpoint is se.resolve_service_endpoint_with_evidence_gate
        assert http_scratch_store._resolve_endpoint is se.resolve_service_endpoint_with_evidence_gate


class TestAdopterOverridesForwardIdempotent:
    """Review 2026-07-22 (bugcluster): HttpAspectQueue's original ``_post``
    override silently DROPPED the new ``idempotent`` kwarg — a caller
    passing it got a TypeError in production. Four sibling stores carried
    the identical latent trap. This tripwire inspects EVERY
    RefreshableHttpStoreMixin adopter in nexus.db: any override of the
    transport verbs must both ACCEPT and FORWARD ``idempotent``.
    """

    def _adopters(self):
        import importlib
        import inspect
        import pkgutil

        import nexus.db as dbpkg
        from nexus.db.t2._refreshable_client import RefreshableHttpStoreMixin

        for mod in pkgutil.walk_packages(dbpkg.__path__, prefix="nexus.db."):
            try:
                m = importlib.import_module(mod.name)
            except Exception:  # pragma: no cover — optional deps
                continue
            for _, cls in inspect.getmembers(m, inspect.isclass):
                if (
                    issubclass(cls, RefreshableHttpStoreMixin)
                    and cls is not RefreshableHttpStoreMixin
                    and cls.__module__ == mod.name
                ):
                    yield cls

    def test_every_transport_verb_override_accepts_and_forwards(self) -> None:
        import inspect

        from nexus.db.t2._refreshable_client import RefreshableHttpStoreMixin

        verbs = ("_send", "_post", "_get", "_delete")
        adopters = list(self._adopters())
        assert len(adopters) >= 5, f"adopter discovery broke: {adopters}"
        offenders = []
        for cls in adopters:
            for verb in verbs:
                fn = cls.__dict__.get(verb)
                if fn is None:  # inherited — mixin handles it
                    continue
                sig = inspect.signature(fn)
                if "idempotent" not in sig.parameters:
                    offenders.append(f"{cls.__name__}.{verb}: kwarg not accepted")
                    continue
                src = inspect.getsource(fn)
                if "idempotent=idempotent" not in src:
                    offenders.append(f"{cls.__name__}.{verb}: accepted but not forwarded")
        assert not offenders, (
            "transport-verb overrides must accept AND forward idempotent "
            "(the HttpAspectQueue TypeError class): " + "; ".join(offenders)
        )
