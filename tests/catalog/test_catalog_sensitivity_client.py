# SPDX-License-Identifier: AGPL-3.0-or-later
"""RDR-243 Phase 1 Step 3a (nexus-rfvyy.6.8): the Python catalog client carries ``sensitive``.

Wire-shape tests on an ``httpx.MockTransport`` (the engine side is pinned Java-side by the
Catalog*Test family), a probe table, the refusal helper every ``--sensitive`` write calls, and
one substrate leg against the gate-jar engine.

Alias decision (T2 nexus/rdr243-p1-python-state): the engine's ``/sensitivity`` route does not
follow ``alias_of``, so a flag set on an alias tumbler would leave the canonical document, the
one that owns the chunks, unflagged. ``set_sensitivity`` therefore resolves each tumbler with
``follow_alias=True`` before the call and reports the canonical tumbler it flagged.
"""
from __future__ import annotations

import json
import os

import httpx
import pytest

from nexus.catalog import sensitivity_doctor
from nexus.catalog import catalog_protocol
from nexus.catalog.catalog_protocol import CATALOG_WRITE_OPS
from nexus.catalog.factory import _ServiceCatalogWriter
from nexus.catalog.http_catalog_client import (
    CatalogSensitivityUnsupportedError,
    HttpCatalogClient,
    _to_entry,
    require_catalog_sensitivity,
)
from nexus.catalog.tumbler import Tumbler


def _client(handler) -> HttpCatalogClient:
    cat = HttpCatalogClient(base_url="http://mock.test", _token="t")
    cat._client = httpx.Client(base_url="http://mock.test", transport=httpx.MockTransport(handler))
    return cat


def _doc(tumbler: str, **kw) -> dict:
    return {"tumbler": tumbler, "title": "t", "content_type": "knowledge", **kw}


# ── CatalogEntry.sensitive ──────────────────────────────────────────────────


def test_entry_reads_sensitive_strictly_and_round_trips() -> None:
    assert _to_entry(_doc("1.2.3", sensitive=True)).sensitive is True
    assert _to_entry(_doc("1.2.3", sensitive=False)).sensitive is False
    assert _to_entry(_doc("1.2.3")).sensitive is False, "an engine without the column"
    assert _to_entry(_doc("1.2.3", sensitive="true")).sensitive is False, "only a JSON true counts"
    assert _to_entry(_doc("1.2.3", sensitive=1)).sensitive is False
    assert _to_entry(_doc("1.2.3", sensitive=True)).to_dict()["sensitive"] is True
    assert _to_entry(_doc("1.2.3")).to_dict()["sensitive"] is False


# ── set_sensitivity: the wire ───────────────────────────────────────────────


def _route(handler_posts: list, *, show: dict[str, str] | None = None, updated=None):
    """A catalog double: /show follows ``show`` (input -> canonical), /sensitivity records its body."""
    show = show or {}

    def handler(request: httpx.Request) -> httpx.Response:
        path = request.url.path
        if path.endswith("/show"):
            asked = request.url.params["tumbler"]
            assert request.url.params.get("follow_alias", "").lower() in ("true", "1"), "alias must be followed"
            if asked == "9.9.9":
                return httpx.Response(404, json={"error": "not found"})
            return httpx.Response(200, json=_doc(show.get(asked, asked)))
        if path.endswith("/sensitivity"):
            body = json.loads(request.content)
            handler_posts.append((path, body))
            return httpx.Response(200, json={"updated": updated if updated is not None else [1] * len(body["tumblers"])})
        raise AssertionError(f"unexpected request {request.method} {path}")

    return handler


def test_set_sensitivity_posts_the_route_with_tumblers_flag_and_reason() -> None:
    posts: list = []
    out = _client(_route(posts)).set_sensitivity(["1.2.3"], True, reason="why")
    assert posts == [("/v1/catalog/sensitivity", {"tumblers": ["1.2.3"], "sensitive": True, "reason": "why"})]
    assert out == {"updated": [1], "targets": ["1.2.3"]}


def test_set_sensitivity_accepts_a_tumbler_object_and_clears_with_a_reason() -> None:
    posts: list = []
    _client(_route(posts)).set_sensitivity([Tumbler.parse("1.2.3")], False, reason="reviewed")
    assert posts[0][1] == {"tumblers": ["1.2.3"], "sensitive": False, "reason": "reviewed"}


@pytest.mark.parametrize("reason", ["", "   ", "\n\t"])
def test_clearing_without_a_reason_raises_before_any_request(reason: str) -> None:
    seen: list = []

    def handler(request: httpx.Request) -> httpx.Response:
        seen.append(request)
        return httpx.Response(200, json={})

    with pytest.raises(ValueError, match="reason"):
        _client(handler).set_sensitivity(["1.2.3"], False, reason=reason)
    with pytest.raises(ValueError, match="reason"):
        _client(handler).set_sensitivity(["1.2.3"], False)
    assert seen == []


def test_setting_needs_no_reason() -> None:
    posts: list = []
    _client(_route(posts)).set_sensitivity(["1.2.3"], True)
    assert posts[0][1]["sensitive"] is True


def test_a_non_boolean_flag_raises_before_any_request() -> None:
    seen: list = []
    with pytest.raises(TypeError, match="sensitive"):
        _client(lambda r: seen.append(r) or httpx.Response(200, json={})).set_sensitivity(["1.2.3"], "yes")  # type: ignore[arg-type]
    assert seen == []


@pytest.mark.parametrize("one", ["1.2.3", "Tumbler"])
def test_a_single_tumbler_instead_of_a_list_is_refused_not_split_into_characters(one) -> None:
    seen: list = []
    value = Tumbler.parse("1.2.3") if one == "Tumbler" else one
    with pytest.raises(TypeError, match="list"):
        _client(lambda r: seen.append(r) or httpx.Response(200, json={})).set_sensitivity(value, True)  # type: ignore[arg-type]
    assert seen == []


def test_an_empty_tumbler_list_makes_no_request() -> None:
    seen: list = []
    out = _client(lambda r: seen.append(r) or httpx.Response(200, json={})).set_sensitivity([], True)
    assert out == {"updated": [], "targets": []} and seen == []


def test_the_write_is_not_resent_after_a_lost_reply() -> None:
    """It writes an audit row, so the transport must not blindly resend (idempotent=False)."""
    cat = _client(lambda r: httpx.Response(200, json={"updated": [1]}))
    seen: dict = {}
    real = cat._post

    def spy(path, body=None, **kw):
        seen.update(kw)
        return real(path, body, **kw)

    cat._post = spy  # type: ignore[method-assign]
    cat.set_sensitivity(["1.2.3"], True)
    assert seen.get("idempotent") is False


# ── set_sensitivity: aliases ────────────────────────────────────────────────


def test_an_alias_is_resolved_to_its_canonical_before_the_call() -> None:
    posts: list = []
    out = _client(_route(posts, show={"1.2.5": "1.2.3"})).set_sensitivity(["1.2.5"], True, reason="r")
    assert posts[0][1]["tumblers"] == ["1.2.3"], "the canonical owns the chunks; the alias must not be what is sent"
    assert out == {"updated": [1], "targets": ["1.2.3"]}


def test_two_inputs_resolving_to_one_canonical_send_it_once_and_report_the_repeat_as_zero() -> None:
    posts: list = []
    out = _client(_route(posts, show={"1.2.5": "1.2.3"}, updated=[1, 1])).set_sensitivity(
        ["1.2.3", "1.2.5", "1.2.4"], True, reason="r",
    )
    assert posts[0][1]["tumblers"] == ["1.2.3", "1.2.4"]
    assert out == {"updated": [1, 0, 1], "targets": ["1.2.3", "1.2.3", "1.2.4"]}


def test_an_unknown_tumbler_is_sent_as_given_and_the_engine_answers_zero() -> None:
    posts: list = []
    out = _client(_route(posts, updated=[0, 1])).set_sensitivity(["9.9.9", "1.2.3"], True)
    assert posts[0][1]["tumblers"] == ["9.9.9", "1.2.3"]
    assert out["updated"] == [0, 1]


def test_a_failed_alias_lookup_aborts_with_no_write() -> None:
    posts: list = []

    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path.endswith("/show"):
            return httpx.Response(500, json={"error": "boom"})
        posts.append(request)
        return httpx.Response(200, json={"updated": [1]})

    with pytest.raises(httpx.HTTPStatusError):
        _client(handler).set_sensitivity(["1.2.3"], True)
    assert posts == [], "an unresolved alias must never fall through to flagging the alias itself"


def test_a_reply_whose_updated_list_does_not_line_up_is_refused() -> None:
    with pytest.raises(RuntimeError, match="updated"):
        _client(_route([], updated=[1, 1, 1])).set_sensitivity(["1.2.3"], True)


# ── the write whitelist (the nexus-kgos1 shape) ─────────────────────────────


def test_set_sensitivity_is_a_whitelisted_write_op_and_a_protocol_method() -> None:
    assert "set_sensitivity" in CATALOG_WRITE_OPS
    assert hasattr(catalog_protocol.CatalogWriter, "set_sensitivity")


def test_the_writer_proxy_resolves_set_sensitivity_and_a_proxy_without_it_raises_at_call_time(monkeypatch) -> None:
    cat = _client(lambda r: httpx.Response(200, json={"updated": [1]}))
    assert callable(_ServiceCatalogWriter(cat).set_sensitivity)
    monkeypatch.setattr(
        "nexus.catalog.factory.CATALOG_WRITE_OPS",
        tuple(op for op in CATALOG_WRITE_OPS if op != "set_sensitivity"),
    )
    with pytest.raises(AttributeError, match="set_sensitivity"):
        _ServiceCatalogWriter(cat).set_sensitivity


# ── the probe ───────────────────────────────────────────────────────────────


def test_the_probe_reads_its_own_flag_strictly() -> None:
    def make(body):
        return _client(lambda r: httpx.Response(200, json=body))

    assert make({"catalog_sensitivity_supported": True}).supports_catalog_sensitivity() is True
    assert make({"catalog_sensitivity_supported": "true"}).supports_catalog_sensitivity() is False
    assert make({"catalog_sensitivity_supported": 1}).supports_catalog_sensitivity() is False
    assert make({}).supports_catalog_sensitivity() is False
    assert make({"catalog_manifest_row_limit_supported": True}).supports_catalog_sensitivity() is False, "its own flag"
    assert _client(lambda r: httpx.Response(500, json={})).supports_catalog_sensitivity() is False


def test_a_transport_error_reads_unsupported_and_never_raises() -> None:
    def boom(request: httpx.Request) -> httpx.Response:
        raise httpx.ConnectError("down")

    assert _client(boom).supports_catalog_sensitivity() is False


def test_the_probe_is_cached_within_the_ttl() -> None:
    calls: list = []

    def handler(request: httpx.Request) -> httpx.Response:
        calls.append(request.url.path)
        return httpx.Response(200, json={"catalog_sensitivity_supported": True})

    cat = _client(handler)
    assert cat.supports_catalog_sensitivity() is True and cat.supports_catalog_sensitivity() is True
    assert len(calls) == 1


# ── the refusal helper ──────────────────────────────────────────────────────


def test_the_helper_passes_when_the_engine_advertises_the_capability() -> None:
    require_catalog_sensitivity(_client(lambda r: httpx.Response(200, json={"catalog_sensitivity_supported": True})))


def test_the_helper_refuses_an_engine_that_answered_without_the_flag_and_names_its_version_and_the_minimum() -> None:
    cat = _client(lambda r: httpx.Response(200, json={"release_version": "0.1.150"}))
    with pytest.raises(CatalogSensitivityUnsupportedError) as exc:
        require_catalog_sensitivity(cat, what="nx index pdf --sensitive")
    msg = str(exc.value)
    assert isinstance(exc.value, RuntimeError)
    assert "0.1.150" in msg and "minimum" in msg.lower() and "nx index pdf --sensitive" in msg
    assert "could not" not in msg.lower(), "the engine answered; this is not an unknown"


def test_the_helper_refuses_when_the_probe_could_not_confirm() -> None:
    cat = _client(lambda r: httpx.Response(500, json={}))
    with pytest.raises(CatalogSensitivityUnsupportedError, match="could not confirm"):
        require_catalog_sensitivity(cat)


def test_the_helper_refuses_a_handle_that_has_no_probe() -> None:
    """An unknown answer refuses: a double, or a proxy that forwards nothing, is not a yes."""

    class NoProbe:
        pass

    with pytest.raises(CatalogSensitivityUnsupportedError, match="could not confirm"):
        require_catalog_sensitivity(NoProbe())


def test_the_helper_refuses_a_flag_that_is_not_a_json_true() -> None:
    with pytest.raises(CatalogSensitivityUnsupportedError):
        require_catalog_sensitivity(_client(lambda r: httpx.Response(200, json={"catalog_sensitivity_supported": "yes"})))


# ── the substrate leg (the bead's acceptance line) ──────────────────────────


def test_substrate_set_and_clear_round_trip_with_an_alias(t2_service_env) -> None:
    cat = HttpCatalogClient(base_url=os.environ["NX_SERVICE_URL"], _token=os.environ["NX_SERVICE_TOKEN"])
    try:
        require_catalog_sensitivity(cat)  # the gate-jar engine carries the changeset
        owner = cat.register_owner("rfvyy68-owner", owner_type="curator", tumbler_prefix="68.1")
        canonical = str(cat.register(owner, "canonical", content_type="knowledge", source_uri="file:///rfvyy68/c.md"))
        dup = str(cat.register(owner, "dup", content_type="knowledge", source_uri="file:///rfvyy68/d.md"))
        cat.merge_documents(dup, canonical)  # dup becomes an alias of canonical
        assert cat.resolve(canonical, follow_alias=False).sensitive is False

        out = cat.set_sensitivity([dup], True, reason="rfvyy68 alias leg")
        assert out == {"updated": [1], "targets": [canonical]}
        assert cat.resolve(canonical, follow_alias=False).sensitive is True, "the canonical is the one flagged"
        assert cat.set_sensitivity([canonical], True)["updated"] == [0], "already set"

        # nexus-f3yqd.6 (F30): the bounded read nx doctor's sensitivity rows use, against the real route.
        listed = cat.sensitive_documents(1000)
        assert listed is not None, "the gate-jar engine advertises catalog_sensitive_list_supported"
        assert listed["capped"] is False and listed["count"] == len(listed["documents"]) >= 1
        assert canonical in {d["tumbler"] for d in listed["documents"]}
        assert dup not in {d["tumbler"] for d in listed["documents"]}, "the flag landed on the canonical only"
        walked = {str(e.tumbler) for e in cat.all_documents(limit=0) if e.sensitive is True}
        assert walked == {d["tumbler"] for d in listed["documents"]}, "the route and the walk find the same set"
        row = sensitivity_doctor.check_sensitive_in_external_embedder(reader=cat)
        assert row["sensitive_documents"] == listed["count"], row

        with pytest.raises(ValueError):
            cat.set_sensitivity([canonical], False)
        assert cat.resolve(canonical, follow_alias=False).sensitive is True, "a refused clear never reached the engine"

        assert cat.set_sensitivity([canonical], False, reason="reviewed")["updated"] == [1]
        assert cat.resolve(canonical, follow_alias=False).sensitive is False
    finally:
        cat.close()
