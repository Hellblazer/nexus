# SPDX-License-Identifier: AGPL-3.0-or-later
"""Point an opted-in test at an EXTERNAL engine and tenant (nexus-ckejb, RDR-235 Activation Step 1).

The engine-substrate fixtures (``t2_service_env`` / ``t2_tenant``, body ``_engine_tenant_env`` in
``tests/conftest.py``) boot a throwaway PG + service jar and mint a tenant on it. RDR-235 Activation Step 1 asks
for the MVV journey to run once in a managed-service tenant instead. This module is the opt-in that lets it.

Two variables name the target, a test opts in with a marker, and nothing else follows them:

* ``NX_TEST_EXTERNAL_ENGINE_URL``: the engine's base URL.
* ``NX_TEST_EXTERNAL_TENANT_TOKEN``: a bearer bound to a tenant the OPERATOR provisioned.
* ``@pytest.mark.external_engine_ok``: only a test carrying it reads the pair. A stray pair in a shell can
  therefore never point the rest of the suite at production; an unmarked test, or a marked one with only one of
  the two variables set, keeps the local minted tenant and one note says the variables were ignored.

What the external branch does, and why each part is there:

* It skips the jar check, ``ensure_engine`` and ``tenant_lease``: no second engine, no minted tenant, and no
  tenant wipe or drop at the end. The operator tears the tenant down.
* It never sets ``NX_ALLOW_PROD_WRITE``. The suite exempts itself from the production-write guard for the
  whole session with an in-process override (``_exempt_pytest_from_production_write_guard``); that exemption is
  true for the throwaway substrate and false here, so the branch clears it for the test, and then asks the
  real guard (``guard_production_write``) whether the operator gave a reason. No reason fails the test at setup
  naming the variable, before any write.
* ``NX_LOCAL`` follows the ENGINE, not the suite's default pin. ``NX_LOCAL=1`` names the client's local posture,
  under which ``register_collection`` sends the local embedder's model; the engine refuses a model that is not
  its tenant's profile model (422). A Voyage-profile engine (the managed service) therefore needs the cloud
  posture (``NX_LOCAL=0``), a local onnx engine needs ``NX_LOCAL=1``. ``GET /version`` ``embedding_mode`` says
  which.
* The tenant name comes from ``GET /v1/_whoami`` with the bearer (the engine binds tenant to the bearer). That
  probe is also the check that the URL answers and the token is accepted, so a wrong pair fails at setup naming
  the cause instead of 401s deep in a journey. A route that is absent leaves the literal ``external``.
* The storage-touch guard's tenant window stays open for the span, as for a minted tenant.

The token is never logged or put in an exception.
"""
from __future__ import annotations

import contextlib
import os
from collections.abc import Callable, Iterator, Mapping
from dataclasses import dataclass, field
from typing import Any

import httpx
import pytest
import structlog

from tests import _storage_touch_guard

_log = structlog.get_logger(__name__)

URL_ENV = "NX_TEST_EXTERNAL_ENGINE_URL"
TOKEN_ENV = "NX_TEST_EXTERNAL_TENANT_TOKEN"
#: The lookup below spells the name out: tests/test_marker_selection_coverage.py wires a marker by a literal
#: ``get_closest_marker("<name>")``, not by a constant.
MARKER = "external_engine_ok"

#: The tenant name when the engine does not report one.
FALLBACK_TENANT = "external"
#: ``GET /version`` ``embedding_mode`` of an engine whose tenants profile the local onnx embedder.
_LOCAL_EMBEDDING_MODE = "onnx-local"
_PROBE_TIMEOUT_S = 15.0

#: Reasons already noted in this process, so a stray pair costs one line, not one per test.
_noted: set[str] = set()


class ExternalEngineError(RuntimeError):
    """The external engine or its token cannot be used; the message names which and never carries the token."""


@dataclass(frozen=True)
class ExternalTarget:
    base_url: str
    token: str = field(repr=False)


@dataclass(frozen=True)
class ExternalProbe:
    tenant: str
    embedding_mode: str

    @property
    def local_posture(self) -> bool:
        return self.embedding_mode == _LOCAL_EMBEDDING_MODE


def _note(key: str, **fields: Any) -> None:
    if key in _noted:
        return
    _noted.add(key)
    _log.warning("external_engine.variables_ignored", reason=key, **fields)


def resolve(
    request: pytest.FixtureRequest | None, environ: Mapping[str, str] | None = None,
) -> ExternalTarget | None:
    """The external target for this test, or None to take the local minted tenant.

    External needs all three: the marker on the test, and both variables non-empty. The ignored cases log one
    line per process.
    """
    env = os.environ if environ is None else environ
    url = (env.get(URL_ENV) or "").strip()
    token = (env.get(TOKEN_ENV) or "").strip()
    if not url and not token:
        return None
    marked = request is not None and request.node.get_closest_marker("external_engine_ok") is not None
    if not marked:
        _note("test is not marked " + MARKER, variables=[n for n, v in ((URL_ENV, url), (TOKEN_ENV, token)) if v])
        return None
    if not url or not token:
        _note("only one of the two variables is set", set=URL_ENV if url else TOKEN_ENV)
        return None
    return ExternalTarget(base_url=url.rstrip("/"), token=token)


def requested(environ: Mapping[str, str] | None = None) -> bool:
    """True when BOTH variables are set: the operator is asking for an external run. A local-only test skips on
    this (``pytest.mark.skipif``, evaluated at collection, so its fixtures never boot an engine)."""
    env = os.environ if environ is None else environ
    return bool((env.get(URL_ENV) or "").strip() and (env.get(TOKEN_ENV) or "").strip())


def in_external_mode(request: pytest.FixtureRequest | None, environ: Mapping[str, str] | None = None) -> bool:
    """True when ``resolve`` would return a target. Does not log twice for the same reason."""
    return resolve(request, environ) is not None


def probe(target: ExternalTarget, *, get: Callable[..., httpx.Response] = httpx.get) -> ExternalProbe:
    """Ask the engine which posture it serves and which tenant the bearer is bound to.

    Raises :class:`ExternalEngineError` when the URL does not answer ``/version``, or when the engine refuses
    the bearer. ``/v1/_whoami`` absent or answering something else leaves the tenant name ``external``.
    """
    if not target.base_url.startswith(("http://", "https://")):
        raise ExternalEngineError(f"{URL_ENV} must be an http(s) URL, got {target.base_url.split('/')[0]!r}")
    where = _shown(target.base_url)
    try:
        version = get(f"{target.base_url}/version", timeout=_PROBE_TIMEOUT_S, follow_redirects=False)
    except httpx.HTTPError as exc:
        raise ExternalEngineError(f"engine at {where} does not answer GET /version: {type(exc).__name__}") from None
    if version.status_code != 200:
        raise ExternalEngineError(f"engine at {where} answered GET /version with HTTP {version.status_code}")
    try:
        mode = str(version.json().get("embedding_mode") or "unknown")
    except (ValueError, AttributeError):
        raise ExternalEngineError(f"engine at {where} answered GET /version with a body that is not a JSON object") from None

    tenant = FALLBACK_TENANT
    try:
        who = get(
            f"{target.base_url}/v1/_whoami", headers={"Authorization": f"Bearer {target.token}"},
            timeout=_PROBE_TIMEOUT_S, follow_redirects=False,
        )
    except httpx.HTTPError as exc:
        raise ExternalEngineError(f"engine at {where} does not answer GET /v1/_whoami: {type(exc).__name__}") from None
    if who.status_code in (401, 403):
        raise ExternalEngineError(
            f"engine at {where} refused the bearer in {TOKEN_ENV} (HTTP {who.status_code}): "
            "check that it is the tenant's token and has not been revoked",
        )
    if who.status_code == 200:
        try:
            name = who.json().get("tenant")
        except (ValueError, AttributeError):
            name = None
        if isinstance(name, str) and name:
            tenant = name
    return ExternalProbe(tenant=tenant, embedding_mode=mode)


def _shown(base_url: str) -> str:
    from nexus.db.service_endpoint import log_endpoint  # noqa: PLC0415 — keeps this module importable before nexus is configured

    return repr(log_endpoint(base_url))


@contextlib.contextmanager
def external_tenant_env(
    monkeypatch: pytest.MonkeyPatch, target: ExternalTarget,
    *, probe_fn: Callable[[ExternalTarget], ExternalProbe] | None = None,
) -> Iterator[str]:
    """Point this test's env at the external engine; yields the tenant name. See the module docstring.

    Fails the test (at setup) with a message that names the cause when the operator's production-write reason is
    missing, the engine does not answer, or the engine refuses the bearer.
    """
    from nexus.db import service_endpoint  # noqa: PLC0415 — deferred, as the conftest does
    from tests._tenant_pool import reset_client_caches  # noqa: PLC0415

    # The suite's session-wide exemption is for the throwaway substrate. Withdraw it for this test, so the real
    # guard decides, and ask it now: a missing reason must fail before any write, naming the variable.
    monkeypatch.setattr(service_endpoint, "_test_only_opt_in_reason", None)
    try:
        service_endpoint.guard_production_write(target.base_url)
    except service_endpoint.ProductionWriteGuardError as exc:
        pytest.fail(
            f"external engine mode needs the operator's reason in {service_endpoint.PROD_WRITE_OPT_IN_ENV} "
            f"(the harness never sets it): {exc}", pytrace=False,
        )
    try:
        info = (probe_fn or probe)(target)
    except ExternalEngineError as exc:
        pytest.fail(f"external engine mode: {exc}", pytrace=False)

    reset_client_caches()
    try:
        with _storage_touch_guard.tenant_active():
            monkeypatch.setenv("NX_STORAGE_BACKEND", "service")
            monkeypatch.setenv("NX_SERVICE_URL", target.base_url)
            monkeypatch.setenv("NX_SERVICE_TOKEN", target.token)
            monkeypatch.setenv("NX_LOCAL", "1" if info.local_posture else "0")
            yield info.tenant
    finally:
        reset_client_caches()
