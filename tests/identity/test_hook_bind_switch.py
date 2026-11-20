# SPDX-License-Identifier: AGPL-3.0-or-later
"""``NX_IDENTITY_BIND=0``: the hook's identity leg binds nothing and makes no engine call (a process that carries an
engine endpoint and credential it must not bind with sets it). Tenant-free: the client is never built, so there is
nothing to talk to."""
from __future__ import annotations

from pathlib import Path

import pytest

from nexus.hooks.identity_bind import bind_at_session_start
from nexus.identity.bind_client import IdentityClient, IdentityUnavailableError
from tests.identity.fakes import FakeProcesses

T0 = "2026-11-17T08:00:00Z"


class _Calls:
    """Stands in for ``IdentityClient.from_config`` and counts how often the hook builds a client."""

    def __init__(self) -> None:
        self.built = 0

    def __call__(self, *args, **kwargs):
        self.built += 1
        raise IdentityUnavailableError("test engine: none")


@pytest.fixture
def built(monkeypatch: pytest.MonkeyPatch) -> _Calls:
    calls = _Calls()
    monkeypatch.setattr(IdentityClient, "from_config", classmethod(lambda cls, *a, **k: calls(*a, **k)))
    return calls


def _hook(tmp_path: Path, env: dict[str, str]):
    procs = FakeProcesses(tmp_path / "sessions")
    procs.start(700, T0, registry=False)
    return bind_at_session_start("sid-s", "startup", env=env, claude_pid=700, procs=procs, config_dir=tmp_path / "config")


def test_the_switch_binds_nothing_says_so_and_builds_no_client(tmp_path: Path, built: _Calls) -> None:
    got = _hook(tmp_path, {"NX_IDENTITY_BIND": "0"})
    assert built.built == 0, "no IdentityClient is constructed, so no engine call is possible"
    assert got.outcome is None and got.sweep is None
    assert len(got.notices) == 1 and "NX_IDENTITY_BIND=0" in got.notices[0] and "binds nothing" in got.notices[0]
    assert not (tmp_path / "config").exists(), "no pending file or token file is written either"


def test_the_switch_makes_no_bind_call_even_with_a_client_handed_in(tmp_path: Path) -> None:
    calls: list[str] = []

    class _Engine:
        """Records every touch: the hook swallows an exception raised here, so a raise would prove nothing."""

        def __getattr__(self, name: str):
            calls.append(name)
            return lambda *a, **k: {}

    procs = FakeProcesses(tmp_path / "sessions")
    procs.start(700, T0)
    got = bind_at_session_start("sid-s", "startup", env={"NX_IDENTITY_BIND": "0"}, claude_pid=700, procs=procs,
                                client=_Engine(), config_dir=tmp_path / "config")
    assert calls == [], "no engine call of any kind"
    assert got.outcome is None and any("NX_IDENTITY_BIND=0" in n for n in got.notices)


@pytest.mark.parametrize("value", [" 0", "0 ", "0\n"])
def test_the_switch_strips_whitespace_as_the_lease_does(tmp_path: Path, built: _Calls, value: str) -> None:
    got = _hook(tmp_path, {"NX_IDENTITY_BIND": value})
    assert built.built == 0 and any("NX_IDENTITY_BIND=0" in n for n in got.notices)


@pytest.mark.parametrize("env", [{}, {"NX_IDENTITY_BIND": "1"}, {"NX_IDENTITY_BIND": ""}])
def test_without_the_switch_the_hook_goes_on_to_build_its_client(tmp_path: Path, built: _Calls, env: dict[str, str]) -> None:
    got = _hook(tmp_path, env)
    assert built.built == 1, "behaviour is unchanged: the hook reaches for an engine"
    assert any("no engine to bind against" in n for n in got.notices)
    assert not any("NX_IDENTITY_BIND" in n for n in got.notices)
