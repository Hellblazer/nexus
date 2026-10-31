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
  its tenant's profile model (422). The profile decides, so the posture is read from ``GET /version``
  ``embedding_profile`` (``voyage`` gives ``NX_LOCAL=0``, ``bge`` gives ``NX_LOCAL=1``), and only an engine
  that omits that key falls back to ``embedding_mode`` (``onnx-local`` gives 1). ``embedding_mode`` alone is the
  runtime family (``onnx-local`` | ``voyage`` | ``dual``) and a ``dual`` engine can profile either way. An
  engine that reports ``embedding_degraded`` (a chosen profile it cannot serve) fails the test at setup, before
  any write.
* The tenant name comes from ``GET /v1/_whoami`` with the bearer (the engine binds tenant to the bearer). That
  probe also checks that the URL answers and the token is accepted, so a wrong pair fails at setup naming the
  cause (401/403, or any status other than 200 and 404). A 404 means the engine has no such route: the tenant is
  the literal ``external``, the bearer is NOT validated, and the setup line says so. When the engine names the
  tenant, the operator's ``NX_ALLOW_PROD_WRITE`` reason must contain that name, so a reason written for one
  tenant cannot authorise a write to another. ``http://`` is accepted only for a loopback host (the bearer
  would cross in clear otherwise), and a URL carrying ``user:password@`` is refused.
* One line at setup says what the run is about to do (host, tenant, release, embedding mode and profile, the
  posture chosen, whether the bearer was validated). It goes to the terminal and to the log.
* The storage-touch guard's tenant window stays open for the span, as for a minted tenant.

The token is never logged or put in an exception.
"""
from __future__ import annotations

import contextlib
import ipaddress
import os
import urllib.parse
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
#: ``GET /version`` ``embedding_profile`` values (EmbeddingProfile.java): bge-768 for every content type, or the
#: Voyage tokens. The profile is what ``register_collection`` is checked against.
_PROFILE_LOCAL = "bge"
_PROFILE_CLOUD = "voyage"
#: ``GET /version`` ``embedding_mode`` of an engine whose runtime serves only the local onnx embedder; read for
#: the posture only when the engine omits ``embedding_profile``.
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
    #: ``GET /version`` ``embedding_profile``; None when the engine does not report one.
    embedding_profile: str | None = None
    #: ``GET /version`` ``embedding_degraded``: None when absent or null, else the engine's description.
    embedding_degraded: Mapping[str, Any] | None = None
    release_version: str = "unknown"
    #: True when ``/v1/_whoami`` answered 200 with a tenant name: the bearer was accepted and the tenant is the
    #: engine's. False for a 404 (no such route; the bearer is unvalidated) or a 200 with no name.
    tenant_named: bool = True
    whoami_status: int = 200

    @property
    def local_posture(self) -> bool:
        """``NX_LOCAL=1`` for this engine: its profile decides, its embedding mode only when it names none."""
        if self.embedding_profile is not None:
            return self.embedding_profile == _PROFILE_LOCAL
        return self.embedding_mode == _LOCAL_EMBEDDING_MODE

    @property
    def posture_basis(self) -> str:
        return "embedding_profile" if self.embedding_profile is not None else "embedding_mode (no embedding_profile reported)"


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


def in_external_mode(request: pytest.FixtureRequest | None, environ: Mapping[str, str] | None = None) -> bool:
    """True when ``resolve`` would return a target. Does not log twice for the same reason."""
    return resolve(request, environ) is not None


def _is_loopback(host: str) -> bool:
    if host == "localhost":
        return True
    try:
        return ipaddress.ip_address(host).is_loopback
    except ValueError:
        return False


def _check_url(base_url: str) -> str:
    """The log-safe identity of *base_url*, or :class:`ExternalEngineError` when it cannot carry the bearer."""
    from nexus._log_safe_url import has_userinfo, log_endpoint  # noqa: PLC0415 — keeps this module importable before nexus is configured

    where = log_endpoint(base_url)
    try:
        parts = urllib.parse.urlsplit(base_url)
        host = parts.hostname
    except ValueError:
        parts, host = None, None
    if parts is None or parts.scheme not in ("http", "https") or not host:
        raise ExternalEngineError(f"{URL_ENV} must be an http(s) URL with a host (got {where!r})")
    if has_userinfo(base_url):
        raise ExternalEngineError(
            f"{URL_ENV} carries credentials in the URL (user:password@ at {where!r}); the bearer goes in {TOKEN_ENV}",
        )
    if parts.scheme == "http" and not _is_loopback(host):
        raise ExternalEngineError(
            f"{URL_ENV} is plain http:// to {where!r}, which would send the bearer in clear: use https://, "
            "or a loopback host for a local engine",
        )
    return where


def probe(target: ExternalTarget, *, get: Callable[..., httpx.Response] = httpx.get) -> ExternalProbe:
    """Ask the engine which posture it serves and which tenant the bearer is bound to.

    Raises :class:`ExternalEngineError` when the URL cannot carry the bearer or does not answer ``/version``,
    when the engine refuses the bearer (401/403), or when ``/v1/_whoami`` answers anything but 200 and 404. A 404
    leaves the tenant ``external`` and ``tenant_named`` false: the route is absent, so the bearer is unvalidated.
    """
    where = repr(_check_url(target.base_url))
    try:
        version = get(f"{target.base_url}/version", timeout=_PROBE_TIMEOUT_S, follow_redirects=False)
    except httpx.InvalidURL:
        raise ExternalEngineError(f"{URL_ENV} ({where}) is not a usable URL") from None
    except httpx.HTTPError as exc:
        raise ExternalEngineError(f"engine at {where} does not answer GET /version: {type(exc).__name__}") from None
    if version.status_code != 200:
        raise ExternalEngineError(f"engine at {where} answered GET /version with HTTP {version.status_code}")
    try:
        body = version.json()
        mode = str(body.get("embedding_mode") or "unknown")
        profile = body.get("embedding_profile")
        degraded = body.get("embedding_degraded")
        release = str(body.get("release_version") or "unknown")
    except (ValueError, AttributeError):
        raise ExternalEngineError(f"engine at {where} answered GET /version with a body that is not a JSON object") from None
    if profile is not None and profile not in (_PROFILE_LOCAL, _PROFILE_CLOUD):
        raise ExternalEngineError(
            f"engine at {where} reports embedding_profile {profile!r}; this harness knows "
            f"{_PROFILE_CLOUD!r} and {_PROFILE_LOCAL!r}, so it cannot choose a client posture",
        )
    if degraded is not None and not isinstance(degraded, Mapping):
        degraded = {"chosen": "unknown", "serving": "unknown", "missing": "embedding_degraded is not an object"}

    tenant, named = FALLBACK_TENANT, False
    try:
        who = get(
            f"{target.base_url}/v1/_whoami", headers={"Authorization": f"Bearer {target.token}"},
            timeout=_PROBE_TIMEOUT_S, follow_redirects=False,
        )
    except httpx.InvalidURL:
        raise ExternalEngineError(f"{URL_ENV} ({where}) is not a usable URL") from None
    except httpx.HTTPError as exc:
        raise ExternalEngineError(f"engine at {where} does not answer GET /v1/_whoami: {type(exc).__name__}") from None
    if who.status_code in (401, 403):
        raise ExternalEngineError(
            f"engine at {where} refused the bearer in {TOKEN_ENV} (HTTP {who.status_code}): "
            "check that it is the tenant's token and has not been revoked",
        )
    if who.status_code not in (200, 404):
        raise ExternalEngineError(f"engine at {where} answered GET /v1/_whoami with HTTP {who.status_code}")
    if who.status_code == 200:
        try:
            name = who.json().get("tenant")
        except (ValueError, AttributeError):
            name = None
        if isinstance(name, str) and name:
            tenant, named = name, True
    return ExternalProbe(
        tenant=tenant, embedding_mode=mode, embedding_profile=profile, embedding_degraded=degraded,
        release_version=release, tenant_named=named, whoami_status=who.status_code,
    )


def terminal_writer(request: pytest.FixtureRequest | None) -> Callable[[str], None] | None:
    """A function that writes one line to pytest's terminal (visible under ``-q``, past output capture), or None."""
    reporter = None if request is None else request.config.pluginmanager.get_plugin("terminalreporter")
    return None if reporter is None else reporter.write_line


def setup_line(target: ExternalTarget, info: ExternalProbe) -> str:
    """What this run is about to do, token-free: the one line printed at setup."""
    from nexus._log_safe_url import log_endpoint  # noqa: PLC0415

    bearer = (
        "validated by GET /v1/_whoami" if info.tenant_named
        else f"UNVALIDATED (GET /v1/_whoami answered {info.whoami_status}"
        + (": no such route" if info.whoami_status == 404 else ", no tenant name") + ")"
    )
    return (
        f"external engine: host={log_endpoint(target.base_url)} tenant={info.tenant} "
        f"release_version={info.release_version} embedding_mode={info.embedding_mode} "
        f"embedding_profile={info.embedding_profile or 'absent'} "
        f"posture=NX_LOCAL={'1' if info.local_posture else '0'} (from {info.posture_basis}) bearer={bearer}"
    )


@contextlib.contextmanager
def external_tenant_env(
    monkeypatch: pytest.MonkeyPatch, target: ExternalTarget,
    *, probe_fn: Callable[[ExternalTarget], ExternalProbe] | None = None,
    report: Callable[[str], None] | None = None,
) -> Iterator[str]:
    """Point this test's env at the external engine; yields the tenant name. See the module docstring.

    Fails the test (at setup, before any write) with a message that names the cause when the operator's
    production-write reason is missing or does not name the tenant, the engine does not answer, the engine
    refuses the bearer, or the engine reports a degraded embedding posture. ``report`` receives the setup line.
    """
    from nexus.db import service_endpoint  # noqa: PLC0415 — deferred, as the conftest does
    from tests._tenant_pool import reset_client_caches  # noqa: PLC0415

    # The suite's session-wide exemption is for the throwaway substrate. Withdraw it for this test, so the real
    # guard decides, and ask it now: a missing reason must fail before any write, naming the variable. The
    # monkeypatch undoes the withdrawal at the test's teardown, which restores the session string.
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
    if info.embedding_degraded:
        d = info.embedding_degraded
        pytest.fail(
            "external engine mode: the engine reports a degraded embedding posture "
            f"(chosen={d.get('chosen')!r}, serving={d.get('serving')!r}, missing={d.get('missing')!r}); "
            "a write to it may be refused with a 422 after part of the journey has run. Fix the engine first.",
            pytrace=False,
        )
    reason = service_endpoint._opt_in_reason() or ""
    if info.tenant_named and info.tenant not in reason:
        pytest.fail(
            f"external engine mode: the engine says this bearer is bound to tenant {info.tenant!r}, and "
            f"{service_endpoint.PROD_WRITE_OPT_IN_ENV} does not name it. State the write for this tenant, e.g. "
            f"{service_endpoint.PROD_WRITE_OPT_IN_ENV}=\"... throwaway tenant {info.tenant} ...\"; "
            "a reason written for another tenant must not authorise this one.", pytrace=False,
        )

    line = setup_line(target, info)
    _log.warning("external_engine.setup", line=line)
    if report is not None:
        report(line)

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
