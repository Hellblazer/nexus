# SPDX-License-Identifier: AGPL-3.0-or-later
"""RDR-253 policy table: "a seat that was renamed gets a notice naming its new name", with no time bound. Inside the
alias's 7-day grace the engine answers 200 with state ``retired`` (an engine row in test_session_start_bind covers it);
after it, 410 ``renamed`` with ``renamed_to``. Tenant-free: a stand-in engine that gives the 410."""
from __future__ import annotations

from pathlib import Path
from typing import Any

from nexus.identity.bind_client import IdentityApiError
from nexus.identity.binder import SessionBinder
from tests.identity.fakes import FakeProcesses


class _Alias:
    def __init__(self, error: IdentityApiError) -> None:
        self.error = error

    def alias(self, name: str) -> dict[str, Any]:
        raise self.error


def _binder(tmp_path: Path, error: IdentityApiError) -> SessionBinder:
    return SessionBinder(client=_Alias(error), procs=FakeProcesses(tmp_path / "sessions"), identity_dir=tmp_path / "identity")


def test_a_name_renamed_past_its_grace_still_names_the_new_name(tmp_path: Path) -> None:
    gone = IdentityApiError(410, "renamed", "renamed to beta on 2026-11-01", {"renamed_to": "beta", "identity": "i1"})
    identity, notice = _binder(tmp_path, gone)._seat_named("alpha", from_env=True)
    assert identity is None and notice is not None and "NX_SEAT=alpha was renamed to beta" in notice


def test_a_name_that_was_never_a_seat_says_so(tmp_path: Path) -> None:
    identity, notice = _binder(tmp_path, IdentityApiError(404, "unknown_name", "no seat", {}))._seat_named("zeta", from_env=True)
    assert identity is None and notice is not None and "NX_SEAT=zeta names no seat" in notice
