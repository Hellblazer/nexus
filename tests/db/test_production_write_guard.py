# SPDX-License-Identifier: AGPL-3.0-or-later
# Copyright (c) 2026 Hal Hildebrand. All rights reserved.
"""nexus-a2qhz — the dev-checkout production-write guard.

Design of record (bead nexus-a2qhz, locked by three recorded incidents —
2026-08-19 read-only probe, 2026-08-21 arc-end MVV write, 2026-08-21
scratchpad-script write): gate WRITES with an explicit opt-in
(``NX_ALLOW_PROD_WRITE=1``), fail loud naming the opt-in, never a silent
cwd-based redirect.

This file is pure unit tests against ``nexus.db.service_endpoint`` — no
network, no engine substrate, no T2 jar. Every test either drives the
detection/guard functions directly with a synthetic ``start`` path (bypasses
the process-wide cache entirely) or monkeypatches the guard's own
dependencies (env vars, the cached checkout-root function). The wiring
tests below prove ``RefreshableHttpStoreMixin._send`` (T2 + catalog) and
``http_vector_client._post`` (T3) actually call the guard for writes and
never for reads, again without touching a real store or the network.

The full acceptance-criteria repros (a subprocess with default config
resolution, a script outside tests/ importing tests._catalog_fixture_ops,
the opt-in path, and the installed-tool shape) live in
tests/db/test_production_write_guard_acceptance.py — those need real
subprocesses and are kept separate so this file stays fast and dependency-
free.
"""
from __future__ import annotations

import threading
from contextlib import contextmanager
from http.server import BaseHTTPRequestHandler, HTTPServer
from pathlib import Path

import httpx
import pytest

from nexus.db import service_endpoint


# ── _dev_checkout_root / is_dev_checkout_process ────────────────────────────


class TestDevCheckoutDetection:
    def test_plain_checkout_layout_is_detected(self, tmp_path: Path) -> None:
        """A conexus checkout: <root>/pyproject.toml (name="conexus") +
        <root>/.git (directory) + <root>/src/nexus/db/service_endpoint.py."""
        root = tmp_path / "checkout"
        (root / "src" / "nexus" / "db").mkdir(parents=True)
        (root / ".git").mkdir()
        (root / "pyproject.toml").write_text('[project]\nname = "conexus"\n')
        fake_module_file = root / "src" / "nexus" / "db" / "service_endpoint.py"

        assert service_endpoint._dev_checkout_root(start=fake_module_file) == root

    def test_git_worktree_layout_is_detected(self, tmp_path: Path) -> None:
        """A git worktree's .git is a FILE (gitdir: pointer), not a directory —
        the marker check must accept either."""
        root = tmp_path / "worktrees" / "agent-xyz"
        (root / "src" / "nexus" / "db").mkdir(parents=True)
        (root / ".git").write_text("gitdir: /somewhere/else/.git/worktrees/agent-xyz\n")
        (root / "pyproject.toml").write_text('[project]\nname = "conexus"\n')
        fake_module_file = root / "src" / "nexus" / "db" / "service_endpoint.py"

        assert service_endpoint._dev_checkout_root(start=fake_module_file) == root

    def test_installed_generation_layout_is_not_detected(self, tmp_path: Path) -> None:
        """An installed generation: <tools>/gen-<stamp>/lib/python3.12/
        site-packages/nexus/db/service_endpoint.py — no pyproject.toml or
        .git at ANY ancestor, since nothing under site-packages ships one."""
        fake_module_file = (
            tmp_path
            / "tools"
            / "gen-20260101T000000Z"
            / "lib"
            / "python3.12"
            / "site-packages"
            / "nexus"
            / "db"
            / "service_endpoint.py"
        )
        fake_module_file.parent.mkdir(parents=True)

        assert service_endpoint._dev_checkout_root(start=fake_module_file) is None

    def test_uv_tool_install_layout_is_not_detected(self, tmp_path: Path) -> None:
        """A plain `uv tool install conexus` copy: same site-packages shape,
        different root name — still no pyproject.toml/.git ancestor."""
        fake_module_file = (
            tmp_path
            / ".local"
            / "share"
            / "uv"
            / "tools"
            / "conexus"
            / "lib"
            / "python3.12"
            / "site-packages"
            / "nexus"
            / "db"
            / "service_endpoint.py"
        )
        fake_module_file.parent.mkdir(parents=True)

        assert service_endpoint._dev_checkout_root(start=fake_module_file) is None

    def test_foreign_git_checkout_with_different_project_name_is_not_detected(
        self, tmp_path: Path
    ) -> None:
        """A .git + pyproject.toml ancestor whose project is NOT conexus
        (e.g. nexus vendored into an unrelated monorepo) must not match —
        the name check, not just the marker files, decides."""
        root = tmp_path / "some-other-repo"
        (root / "vendor" / "nexus" / "db").mkdir(parents=True)
        (root / ".git").mkdir()
        (root / "pyproject.toml").write_text('[project]\nname = "not-conexus"\n')
        fake_module_file = root / "vendor" / "nexus" / "db" / "service_endpoint.py"

        assert service_endpoint._dev_checkout_root(start=fake_module_file) is None

    def test_malformed_pyproject_toml_does_not_crash(self, tmp_path: Path) -> None:
        """A malformed pyproject.toml at some ancestor must be skipped, never
        raise — the detector must never crash a real write path."""
        root = tmp_path / "checkout"
        (root / "src" / "nexus" / "db").mkdir(parents=True)
        (root / ".git").mkdir()
        (root / "pyproject.toml").write_text("this is not valid toml [[[")
        fake_module_file = root / "src" / "nexus" / "db" / "service_endpoint.py"

        assert service_endpoint._dev_checkout_root(start=fake_module_file) is None

    def test_real_process_matches_this_worktree(self) -> None:
        """Sanity check tying the unit tests to the real running process:
        this test itself executes from an editable install inside THIS
        checkout (a worktree), so the real, uncached default-arg call must
        detect it as a dev checkout."""
        service_endpoint.reset_dev_checkout_cache_for_tests()
        try:
            assert service_endpoint.is_dev_checkout_process() is True
        finally:
            service_endpoint.reset_dev_checkout_cache_for_tests()


# ── guard_production_write ───────────────────────────────────────────────────


class TestGuardProductionWrite:
    @pytest.fixture(autouse=True)
    def _clean_guard_env(self, monkeypatch: pytest.MonkeyPatch) -> None:
        """Every guard-relevant env var starts unset for each test in this
        class (overriding the suite-wide autouse exemption in
        tests/conftest.py — a later monkeypatch call on the same fixture
        instance wins, same contract as _isolate_config_dir); individual
        tests opt back in explicitly."""
        for var in (
            service_endpoint.PROD_WRITE_OPT_IN_ENV,
            "NX_SERVICE_URL",
            "NX_SERVICE_HOST",
            "NX_SERVICE_PORT",
            "NX_SERVICE_TOKEN",
        ):
            monkeypatch.delenv(var, raising=False)

    def test_dev_checkout_no_opt_in_raises(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        fake_root = tmp_path / "checkout"
        monkeypatch.setattr(service_endpoint, "_dev_checkout_root", lambda start=None: fake_root)

        with pytest.raises(service_endpoint.ProductionWriteGuardError) as exc_info:
            service_endpoint.guard_production_write("http://127.0.0.1:1")

        msg = str(exc_info.value)
        assert "NX_ALLOW_PROD_WRITE" in msg
        assert "http://127.0.0.1:1" in msg
        assert str(fake_root) in msg

    def test_exported_service_url_does_not_exempt_the_write(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """Ship-blocker fix (second review round): a PERMANENTLY exported
        NX_SERVICE_URL -- exactly conexus's own documented cloud-onboarding
        shape (docs/getting-started.md, docs/managed-onboarding.md:
        `export NX_SERVICE_URL=https://api.conexus-nexus.com`) -- must NOT
        exempt a write. The old "an endpoint env var is set" heuristic is
        gone entirely; only a reason-bearing opt-in exempts anything."""
        fake_root = tmp_path / "checkout"
        monkeypatch.setattr(service_endpoint, "_dev_checkout_root", lambda start=None: fake_root)
        monkeypatch.setenv("NX_SERVICE_URL", "https://api.conexus-nexus.com")
        monkeypatch.setenv("NX_SERVICE_TOKEN", "a-real-looking-bearer")

        with pytest.raises(service_endpoint.ProductionWriteGuardError):
            service_endpoint.guard_production_write("https://api.conexus-nexus.com")

    @pytest.mark.parametrize(
        "var", ["NX_SERVICE_URL", "NX_SERVICE_HOST", "NX_SERVICE_PORT", "NX_SERVICE_TOKEN"]
    )
    def test_no_single_endpoint_var_exempts_the_write(
        self, var: str, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        fake_root = tmp_path / "checkout"
        monkeypatch.setattr(service_endpoint, "_dev_checkout_root", lambda start=None: fake_root)
        monkeypatch.setenv(var, "set")

        with pytest.raises(service_endpoint.ProductionWriteGuardError):
            service_endpoint.guard_production_write("http://127.0.0.1:9")

    def test_opt_in_with_a_real_reason_exempts_the_write(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        fake_root = tmp_path / "checkout"
        monkeypatch.setattr(service_endpoint, "_dev_checkout_root", lambda start=None: fake_root)
        monkeypatch.setenv(
            service_endpoint.PROD_WRITE_OPT_IN_ENV,
            "arc-end MVV must exercise the real engine — nexus-a2qhz, reviewed",
        )

        service_endpoint.guard_production_write("http://127.0.0.1:9")  # must not raise

    @pytest.mark.parametrize("value", ["1", "0", "true", "false", "yes", "no", "TRUE", "", "   "])
    def test_boolean_lookalike_opt_in_values_do_not_exempt(
        self, value: str, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """A bare "1" (the retired spelling) and every other boolean-shaped
        leftover are refused exactly like an unset var — only a genuine
        reason string exempts a write, so the intent stays greppable."""
        fake_root = tmp_path / "checkout"
        monkeypatch.setattr(service_endpoint, "_dev_checkout_root", lambda start=None: fake_root)
        monkeypatch.setenv(service_endpoint.PROD_WRITE_OPT_IN_ENV, value)

        with pytest.raises(service_endpoint.ProductionWriteGuardError):
            service_endpoint.guard_production_write("http://127.0.0.1:9")

    def test_non_dev_checkout_never_raises_regardless_of_env(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """Sam's installed nx (or any non-editable install) must never trip
        this, even with zero env overrides and no opt-in."""
        monkeypatch.setattr(service_endpoint, "_dev_checkout_root", lambda start=None: None)

        service_endpoint.guard_production_write("http://127.0.0.1:9")  # must not raise


# ── Wiring: RefreshableHttpStoreMixin._send (T2 + catalog) ──────────────────


def _bare_echo_store() -> "object":
    """A RefreshableHttpStoreMixin subclass built via __new__, bypassing
    __init__'s real endpoint resolution entirely (the same pattern the
    mixin's own docstring documents for a test fixture against a fake
    server) — no network, no jar, no real resolution ever happens."""
    from nexus.db.t2._refreshable_client import RefreshableHttpStoreMixin

    class _EchoStore(RefreshableHttpStoreMixin):
        def echo_post(self, value: str) -> None:
            self._post("/v1/echo", {"value": value})

        def echo_read_via_post(self, value: str) -> None:
            self._post("/v1/echo", {"value": value}, mutates=False)

        def echo_delete(self) -> None:
            self._delete("/v1/echo")

        def echo_get(self) -> None:
            self._get("/v1/echo")

    store = _EchoStore.__new__(_EchoStore)
    store._base_url = "http://127.0.0.1:1"  # never dialed in the POST/DELETE cases
    store._tenant = "default"
    store._token = "test-token"
    store._client = httpx.Client()
    store._base_url_pinned = True
    store._token_pinned = True
    return store


class TestRefreshableClientGuardWiring:
    def test_post_calls_guard_and_stops_before_any_network_attempt(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        from nexus.db.t2 import _refreshable_client as rc

        calls: list[str] = []

        def _fake_guard(base_url: str) -> None:
            calls.append(base_url)
            raise service_endpoint.ProductionWriteGuardError("blocked")

        monkeypatch.setattr(rc, "guard_production_write", _fake_guard)
        store = _bare_echo_store()

        with pytest.raises(service_endpoint.ProductionWriteGuardError):
            store.echo_post("x")

        assert calls == ["http://127.0.0.1:1"]

    def test_delete_calls_guard(self, monkeypatch: pytest.MonkeyPatch) -> None:
        from nexus.db.t2 import _refreshable_client as rc

        calls: list[str] = []

        def _fake_guard(base_url: str) -> None:
            calls.append(base_url)
            raise service_endpoint.ProductionWriteGuardError("blocked")

        monkeypatch.setattr(rc, "guard_production_write", _fake_guard)
        store = _bare_echo_store()

        with pytest.raises(service_endpoint.ProductionWriteGuardError):
            store.echo_delete()

        assert calls == ["http://127.0.0.1:1"]

    def test_post_with_mutates_false_never_calls_guard(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        from nexus.db.t2 import _refreshable_client as rc

        calls: list[str] = []
        monkeypatch.setattr(rc, "guard_production_write", lambda base_url: calls.append(base_url))
        store = _bare_echo_store()

        # A read-shaped POST (mutates=False) never reaches the guard, and
        # then hits the real (unreachable) transport -- SOME failure is
        # expected past that point (a connection error, or the mixin's own
        # "cannot self-heal a fully-pinned endpoint" RuntimeError once the
        # connection error's retry path runs) and is irrelevant to this
        # test; only "the guard was never consulted" is being asserted.
        with pytest.raises(Exception):  # noqa: B017 — exact exception type is the mixin's internal retry/self-heal mechanics, not this test's subject
            store.echo_read_via_post("x")

        assert calls == []

    def test_get_never_calls_guard(self, monkeypatch: pytest.MonkeyPatch) -> None:
        from nexus.db.t2 import _refreshable_client as rc

        calls: list[str] = []
        monkeypatch.setattr(rc, "guard_production_write", lambda base_url: calls.append(base_url))
        store = _bare_echo_store()

        with pytest.raises(Exception):  # noqa: B017 — see test_post_with_mutates_false_never_calls_guard
            store.echo_get()

        assert calls == []


# ── Wiring: http_vector_client._post (T3) ───────────────────────────────────


class TestHttpVectorClientGuardWiring:
    def test_write_shaped_path_calls_guard(self, monkeypatch: pytest.MonkeyPatch) -> None:
        from nexus.db import http_vector_client as hvc

        monkeypatch.setattr(hvc, "_resolve_endpoint", lambda: ("http://127.0.0.1:1", "tok"))
        calls: list[str] = []

        def _fake_guard(base_url: str) -> None:
            calls.append(base_url)
            raise service_endpoint.ProductionWriteGuardError("blocked")

        monkeypatch.setattr(service_endpoint, "guard_production_write", _fake_guard)

        with pytest.raises(service_endpoint.ProductionWriteGuardError):
            hvc._post("/v1/vectors/store-put", {"ids": ["x"]})

        assert calls == ["http://127.0.0.1:1"]

    @pytest.mark.parametrize(
        "path",
        [
            "/v1/vectors/search",
            "/v1/vectors/get",
            "/v1/vectors/get-all-metadata",
            "/v1/vectors/store-get",
            "/v1/vectors/search-metadata-scoped",
        ],
    )
    def test_read_shaped_path_never_calls_guard(
        self, path: str, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        from nexus.db import http_vector_client as hvc

        monkeypatch.setattr(hvc, "_resolve_endpoint", lambda: ("http://127.0.0.1:1", "tok"))
        calls: list[str] = []
        monkeypatch.setattr(service_endpoint, "guard_production_write", lambda base_url: calls.append(base_url))

        # Unreachable endpoint -- SOME failure is expected past the guard
        # check; only "the guard was never consulted" is this test's subject.
        with pytest.raises(Exception):  # noqa: B017 — exact exception type is the module's own retry/remedy mechanics
            hvc._post(path, {"query": "x"})

        assert calls == []


# ── Wiring: HttpTokenStore (bespoke, not a mixin adopter) ───────────────────


def _bare_token_store() -> "object":
    """An HttpTokenStore built via __new__ — guard_production_write is the
    FIRST thing ``_post`` touches, before self._client or any header, so
    only ``_base_url`` is required for the raise-path tests; the
    mutates=False path additionally needs a real (if unreachable) client."""
    from nexus.db.t2.http_token_store import HttpTokenStore

    store = HttpTokenStore.__new__(HttpTokenStore)
    store._base_url = "http://127.0.0.1:1"
    store._tenant = "default"
    store._auth_token = "test-token"
    store._prefer_data_token = False
    store._using_data_token = False
    store._client = httpx.Client(
        base_url=store._base_url,
        headers={"Authorization": "Bearer test-token", "X-Nexus-Tenant": "default"},
    )
    return store


class TestHttpTokenStoreGuardWiring:
    def test_write_method_calls_guard_and_stops_before_any_network_attempt(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        from nexus.db import service_endpoint as se

        calls: list[str] = []

        def _fake_guard(base_url: str) -> None:
            calls.append(base_url)
            raise se.ProductionWriteGuardError("blocked")

        monkeypatch.setattr(se, "guard_production_write", _fake_guard)
        store = _bare_token_store()

        with pytest.raises(se.ProductionWriteGuardError):
            store.create_tenant("some-tenant")

        assert calls == ["http://127.0.0.1:1"]

    def test_start_session_calls_guard(self, monkeypatch: pytest.MonkeyPatch) -> None:
        """The T1 session-lifecycle call site (incident 2's mechanism)."""
        from nexus.db import service_endpoint as se

        calls: list[str] = []

        def _fake_guard(base_url: str) -> None:
            calls.append(base_url)
            raise se.ProductionWriteGuardError("blocked")

        monkeypatch.setattr(se, "guard_production_write", _fake_guard)
        store = _bare_token_store()

        with pytest.raises(se.ProductionWriteGuardError):
            store.start_session("session-id-x")

        assert calls == ["http://127.0.0.1:1"]

    def test_list_tokens_mutates_false_never_calls_guard(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """Regression test for the ship-blocker: list_tokens passed
        mutates=False to a _post signature that didn't accept it
        (TypeError on every call). This exercises the REAL class, not a
        double, and asserts BOTH that the call no longer TypeErrors on the
        kwarg and that the guard is never consulted for this read."""
        from nexus.db import service_endpoint as se

        calls: list[str] = []
        monkeypatch.setattr(se, "guard_production_write", lambda base_url: calls.append(base_url))
        store = _bare_token_store()

        with pytest.raises(Exception):  # noqa: B017 — unreachable transport past the guard-skip; not this test's subject
            store.list_tokens()

        assert calls == []


# ── Wiring: HttpScratchStore / T1 (bespoke, not a mixin adopter) ────────────


def _bare_scratch_store() -> "object":
    from nexus.db.http_scratch_store import HttpScratchStore

    store = HttpScratchStore.__new__(HttpScratchStore)
    store._base_url = "http://127.0.0.1:1"
    store._tenant = "default"
    store._session_id = "session-id-x"
    store._session_token = "session-id-x"
    store._token_pinned = True
    store._headers = {
        "Authorization": "Bearer test-token",
        "X-Nexus-Tenant": "default",
        "X-Nexus-T1-Session": "session-id-x",
        "Content-Type": "application/json",
    }
    store._client = httpx.Client(base_url=store._base_url, headers=store._headers)
    return store


class TestHttpScratchStoreGuardWiring:
    def test_put_calls_guard_and_stops_before_any_network_attempt(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        from nexus.db import service_endpoint as se

        calls: list[str] = []

        def _fake_guard(base_url: str) -> None:
            calls.append(base_url)
            raise se.ProductionWriteGuardError("blocked")

        monkeypatch.setattr(se, "guard_production_write", _fake_guard)
        store = _bare_scratch_store()

        with pytest.raises(se.ProductionWriteGuardError):
            store.put("some content")

        assert calls == ["http://127.0.0.1:1"]

    def test_close_session_calls_guard(self, monkeypatch: pytest.MonkeyPatch) -> None:
        from nexus.db import service_endpoint as se

        calls: list[str] = []

        def _fake_guard(base_url: str) -> None:
            calls.append(base_url)
            raise se.ProductionWriteGuardError("blocked")

        monkeypatch.setattr(se, "guard_production_write", _fake_guard)
        store = _bare_scratch_store()

        with pytest.raises(se.ProductionWriteGuardError):
            store.close_session()

        assert calls == ["http://127.0.0.1:1"]

    @pytest.mark.parametrize("method_name", ["list_entries", "flagged_entries"])
    def test_read_shaped_method_never_calls_guard(
        self, method_name: str, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        from nexus.db import service_endpoint as se

        calls: list[str] = []
        monkeypatch.setattr(se, "guard_production_write", lambda base_url: calls.append(base_url))
        store = _bare_scratch_store()

        with pytest.raises(Exception):  # noqa: B017 — unreachable transport past the guard-skip; not this test's subject
            getattr(store, method_name)()

        assert calls == []


# ── Regression: a read-shaped POST override that DROPS the mutates kwarg ────
#
# nexus-a2qhz (second review round): HttpTokenStore.list_tokens passed
# mutates=False to a bespoke _post signature that didn't declare it --
# TypeError on every call, undetected because the existing test suite
# exercised a _FakeStore double whose signature happened to tolerate the
# extra kwarg. Fixing that ALSO surfaced the identical bug independently in
# HttpCatalogClient's own local _post override (which prefixes the path and
# had the same missing-kwarg gap) -- these two tests exercise the REAL
# classes end-to-end against a real (if tiny) local HTTP server, not a
# double, so a reintroduced signature mismatch fails here immediately
# rather than shipping silently again.


class _JSONStubHandler(BaseHTTPRequestHandler):
    """Answers every POST with a fixed JSON body, whatever the path/auth."""

    response_body: bytes = b"{}"

    def log_message(self, *_a: object) -> None:  # noqa: D102 — suppress test noise
        pass

    def do_POST(self) -> None:  # noqa: N802 — BaseHTTPRequestHandler's naming convention
        length = int(self.headers.get("Content-Length", "0"))
        self.rfile.read(length)
        self.send_response(200)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(self.response_body)))
        self.end_headers()
        self.wfile.write(self.response_body)


@contextmanager
def _json_stub_server(response_body: bytes):
    handler = type("_Handler", (_JSONStubHandler,), {"response_body": response_body})
    server = HTTPServer(("127.0.0.1", 0), handler)
    port = server.server_address[1]
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        yield f"http://127.0.0.1:{port}"
    finally:
        server.shutdown()
        server.server_close()


class TestReadShapedOverrideRegressions:
    def test_http_token_store_list_tokens_round_trips_for_real(self) -> None:
        from nexus.db.t2.http_token_store import HttpTokenStore

        with _json_stub_server(b'{"tokens": [{"tenant": "x"}]}') as url:
            store = HttpTokenStore(base_url=url, _token="test-token")
            try:
                assert store.list_tokens() == [{"tenant": "x"}]
            finally:
                store.close()

    def test_http_catalog_client_relation_counts_round_trips_for_real(self) -> None:
        from nexus.catalog.http_catalog_client import HttpCatalogClient

        with _json_stub_server(b'{"counts": {"nexus.memory": 3}}') as url:
            client = HttpCatalogClient(base_url=url, _token="test-token")
            try:
                assert client.relation_counts(["nexus.memory"]) == {"nexus.memory": 3}
            finally:
                client.close()
