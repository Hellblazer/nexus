# SPDX-License-Identifier: AGPL-3.0-or-later
"""nexus-iedjp: the unresolvable-endpoint message during a login race.

When the storage-service unit is registered to start at login but has not
published its lease yet, the client used to say "start the supervisor with
'nx daemon service start'" even though the OS was already starting it. The
message now says the supervisor may still be starting and to retry, and keeps
the start hint for the case where the lease stays absent. With no unit
registered the message is the old one.

The registration seam is ``nexus.commands.daemon._service_autostart_unit_installed``
(the same lookup ``nx doctor`` and the finish pass use); no real unit directory
is read. The unit file it returns is a real systemd unit under ``tmp_path``,
because the predicate also reads the config dir the unit bakes and compares it
with the client's own (a unit installed for another directory is not this
client's supervisor starting).
"""
from __future__ import annotations

from pathlib import Path

import pytest

from nexus.db import service_endpoint as se

pytestmark = pytest.mark.expects_unresolvable_endpoint

_START_HINT = "nx daemon service start"


@pytest.fixture(autouse=True)
def _no_endpoint(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    monkeypatch.setenv("NEXUS_CONFIG_DIR", str(tmp_path / "client-dir"))
    for name in ("NX_SERVICE_HOST", "NX_SERVICE_PORT", "NX_SERVICE_TOKEN"):
        monkeypatch.delenv(name, raising=False)
    monkeypatch.setattr(se, "discover_lease_with_wait", lambda **_kw: (None, None))


def _unit(monkeypatch: pytest.MonkeyPatch, result: Path | None | Exception) -> None:
    from nexus.commands import daemon as daemon_cmd  # noqa: PLC0415 — file pattern: deferred imports

    def _lookup() -> Path | None:
        if isinstance(result, Exception):
            raise result
        return result

    monkeypatch.setattr(daemon_cmd, "_service_autostart_unit_installed", _lookup)


def _write_unit(tmp_path: Path, config_dir: Path | None) -> Path:
    flag = f" --config-dir {config_dir}" if config_dir is not None else ""
    unit = tmp_path / "nexus-service.service"
    unit.write_text(
        f"[Service]\nExecStart=/usr/bin/nx daemon service start --foreground{flag}\n",
        encoding="utf-8",
    )
    return unit


def _message() -> str:
    with pytest.raises(se.ServiceEndpointUnresolvableError) as caught:
        se.resolve_service_config()
    return str(caught.value)


def test_registered_unit_and_no_lease_says_it_may_still_be_starting(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path,
) -> None:
    _unit(monkeypatch, _write_unit(tmp_path, tmp_path / "client-dir"))
    message = _message()
    assert "still be starting" in message and "retry" in message.lower(), message
    # The start hint stays for the lease that never appears.
    assert _START_HINT in message, message
    assert "not resolvable" in message, message


def test_no_registered_unit_keeps_the_start_hint_alone(monkeypatch: pytest.MonkeyPatch) -> None:
    _unit(monkeypatch, None)
    message = _message()
    assert f"start the supervisor with '{_START_HINT}'" in message, message
    assert "still be starting" not in message, message


def test_a_failing_unit_lookup_reads_as_not_registered(monkeypatch: pytest.MonkeyPatch) -> None:
    """The lookup runs inside an error path; it must not replace the endpoint
    error with its own."""
    _unit(monkeypatch, OSError("home unreadable"))
    message = _message()
    assert f"start the supervisor with '{_START_HINT}'" in message, message
    assert "still be starting" not in message, message


def test_a_unit_installed_for_another_config_dir_keeps_the_start_hint(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path,
) -> None:
    """The unit starts a supervisor for some OTHER directory, so it will never
    publish the lease this client is waiting for."""
    _unit(monkeypatch, _write_unit(tmp_path, tmp_path / "somewhere-else"))
    message = _message()
    assert f"start the supervisor with '{_START_HINT}'" in message, message
    assert "still be starting" not in message, message


def test_a_flagless_legacy_unit_serves_the_default_dir_only(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path,
) -> None:
    """A unit with no ``--config-dir`` means ``~/.config/nexus``; this client's
    dir is not that, so the unit is not its supervisor."""
    _unit(monkeypatch, _write_unit(tmp_path, None))
    message = _message()
    assert "still be starting" not in message, message


def test_an_unreadable_unit_reads_as_not_registered(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path,
) -> None:
    _unit(monkeypatch, tmp_path / "missing.service")
    message = _message()
    assert "still be starting" not in message, message
