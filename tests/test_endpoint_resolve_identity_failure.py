# SPDX-License-Identifier: AGPL-3.0-or-later
"""A Windows user SID that cannot be read fails the hook OPEN (RDR-224,
nexus-f9bgu.33, code review m10).

``conexus/hooks/scripts/_endpoint_resolve.py`` is the plugin's stdlib mirror of the
endpoint resolver. Every hook caller reads "no lease" as "degrade, never block the
tool call". ``storage_service_lease_path`` derives the lease name from the service
identity, and on Windows that is the user SID: when the token query fails the
mirror's ``service_identity`` raises ``ServiceIdentityError`` (a ``RuntimeError``),
which the callers do not catch. It used to escape from ``read_storage_service_lease``
(documented "never raises"), from ``resolve_base_url`` and from
``read_local_supervisor_token``.
"""
from __future__ import annotations

import importlib.util
from pathlib import Path

import pytest

PLUGIN_SCRIPT = Path(__file__).resolve().parent.parent / "conexus" / "hooks" / "scripts" / "_endpoint_resolve.py"


def _mirror():
    spec = importlib.util.spec_from_file_location("_endpoint_resolve_identity_test", PLUGIN_SCRIPT)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


@pytest.fixture
def mirror(monkeypatch: pytest.MonkeyPatch):
    module = _mirror()

    def no_identity() -> str:
        raise module.ServiceIdentityError("cannot read the Windows user SID: token query failed")

    monkeypatch.setattr(module, "service_identity", no_identity)
    for var in ("NX_SERVICE_URL", "NX_SERVICE_HOST", "NX_SERVICE_PORT"):
        monkeypatch.delenv(var, raising=False)
    return module


def test_the_unfixed_primitive_really_raises(mirror) -> None:
    # Non-vacuity: the seam the other tests rely on does raise the error class.
    with pytest.raises(mirror.ServiceIdentityError):
        mirror.storage_service_lease_path(Path("/cfg"))


def test_reading_the_lease_degrades_to_none_and_never_raises(mirror, tmp_path: Path) -> None:
    assert mirror.read_storage_service_lease(tmp_path) is None


def test_resolving_the_base_url_reports_unresolvable_not_a_runtime_error(mirror, tmp_path: Path) -> None:
    with pytest.raises(mirror.EndpointUnresolvable, match="Windows user identity cannot be read"):
        mirror.resolve_base_url(tmp_path)


def test_the_host_env_leg_without_a_port_reports_unresolvable_too(
    mirror, tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("NX_SERVICE_HOST", "127.0.0.1")
    with pytest.raises(mirror.EndpointUnresolvable, match="Windows user identity cannot be read"):
        mirror.resolve_base_url(tmp_path)


def test_the_local_supervisor_token_read_reports_unresolvable(mirror, tmp_path: Path) -> None:
    with pytest.raises(mirror.EndpointUnresolvable, match="identity cannot be read"):
        mirror.read_local_supervisor_token(tmp_path)


def test_a_readable_identity_is_unchanged(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    module = _mirror()
    for var in ("NX_SERVICE_URL", "NX_SERVICE_HOST", "NX_SERVICE_PORT"):
        monkeypatch.delenv(var, raising=False)
    assert module.read_storage_service_lease(tmp_path) is None  # no lease file: still just None
    with pytest.raises(module.EndpointUnresolvable, match="no service endpoint resolvable"):
        module.resolve_base_url(tmp_path)
