# SPDX-License-Identifier: AGPL-3.0-or-later
"""``t2_handle``'s SQLite arm after the T2 daemon retirement (nexus-i711w
Stage 2 sub-stage B; gap found by the sub-stage-B substantive-critic pass).

The arm used to hold a ``T2Client`` talking to the daemon that arbitrated the
single SQLite writer. The daemon is gone, so it now fails loud. That branch
shipped with NO coverage at all — not the exit code, not the
one-liner-not-traceback contract, not the presence of a recovery hint — which
is how its message came to describe only WRITES while it also blocks reads.

These tests pin the contract. Fail-loud is now the DECIDED end state for the
remainder of RDR-158 P4 — Hal, 2026-07-28, on ``nexus-vw7zk``: both ways of
restoring function invest in the substrate the arc is deleting, so the CLI/MCP
asymmetry (MCP still writes SQLite via the grandfathered ``mcp_infra`` arms
while ``nx memory`` exits 1) is accepted until sub-stage A removes the branch
whole. These stop being interim and become the thing that holds the line.

They should be DELETED, not rewritten, when sub-stage A lands: at that point
``storage_backend_for`` no longer has a SQLite answer for this helper and the
arm they cover does not exist.
"""
from __future__ import annotations

import click
import pytest

from nexus.commands._helpers import t2_handle
from nexus.db.storage_mode import StorageBackend


@pytest.fixture
def _sqlite_backend(monkeypatch: pytest.MonkeyPatch) -> None:
    """Pin the SQLite arm explicitly.

    The suite's autouse ``_pin_t2_substrate`` sets ``NX_STORAGE_BACKEND=service``,
    so a test that merely *calls* ``t2_handle`` exercises the SERVICE arm and
    would pass no matter what the SQLite arm does. Patching the resolver — not
    just the env var — keeps that from silently re-happening.
    """
    monkeypatch.setattr(
        "nexus.db.storage_mode.storage_backend_for",
        lambda _tier: StorageBackend.SQLITE,
    )


@pytest.mark.usefixtures("_sqlite_backend")
class TestSqliteArmFailsLoud:
    def test_raises_clickexception_not_a_raw_traceback(self) -> None:
        """A ClickException renders as ``Error: <msg>`` and exit 1; anything
        else reaches the user as a traceback."""
        with pytest.raises(click.ClickException) as exc:
            with t2_handle():
                pytest.fail("t2_handle must not yield a handle in SQLite mode")
        assert exc.value.exit_code == 1

    def test_message_says_READS_are_blocked_too(self) -> None:
        """The original message said only 'arbitrated SQLite-mode writes',
        but `nx memory list/get/search` go through this same helper and are
        equally blocked. An operator told only about writes will read a
        failed `nx memory list` as a different bug."""
        with pytest.raises(click.ClickException) as exc:
            with t2_handle():
                pass
        assert "reads included" in str(exc.value)

    def test_message_names_a_recovery_path_of_LIVE_verbs(self) -> None:
        """Fail-loud without a next step is just a wall. And the verbs it
        names must exist — this whole sub-stage exists because deleted verbs
        rotted in operator-facing text."""
        from click.testing import CliRunner

        from nexus.cli import main as cli

        with pytest.raises(click.ClickException) as exc:
            with t2_handle():
                pass
        msg = str(exc.value)
        named = [v for v in ("nx doctor", "nx upgrade") if v in msg]
        assert named, f"no recovery hint in: {msg!r}"
        for verb in named:
            res = CliRunner().invoke(cli, [*verb.split()[1:], "--help"])
            assert res.exit_code == 0, f"dead verb in operator message: {verb}"

    def test_does_not_open_a_raw_sqlite_connection(self) -> None:
        """The load-bearing half. Fail-loud exists because restoring function
        would add a NEW raw-SQLite site to the CLI, and the no-new-SQLite
        directive makes raising EPSILON_CENSUS a Hal decision. If someone
        later 'fixes' the arm by constructing a T2Database here, this fails
        and points at nexus-vw7zk rather than letting the census be the only
        thing that notices.
        """
        import nexus.db.t2 as t2_mod

        constructed: list[object] = []
        real = t2_mod.T2Database

        class _Spy(real):  # type: ignore[misc, valid-type]
            def __init__(self, *a: object, **kw: object) -> None:
                constructed.append(a)
                super().__init__(*a, **kw)

        with pytest.MonkeyPatch.context() as mp:
            mp.setattr(t2_mod, "T2Database", _Spy)
            with pytest.raises(click.ClickException):
                with t2_handle():
                    pass
        assert not constructed, (
            "t2_handle opened a T2Database on the SQLite arm — that is a new "
            "raw-SQLite site; see nexus-vw7zk before adding an epsilon-allow"
        )


def test_service_arm_is_still_reachable(monkeypatch: pytest.MonkeyPatch) -> None:
    """Non-vacuity partner for the class above: prove the fixture is what
    selects the SQLite arm, so those tests are not passing because
    ``t2_handle`` raises unconditionally."""
    monkeypatch.setattr(
        "nexus.db.storage_mode.storage_backend_for",
        lambda _tier: StorageBackend.SERVICE,
    )
    sentinel = object()
    monkeypatch.setattr(
        "nexus.db.t2.T2Database", lambda *a, **kw: sentinel  # noqa: ARG005
    )
    with pytest.raises(Exception) as exc:  # noqa: PT011 — see below
        with t2_handle() as db:
            assert db is sentinel
            raise RuntimeError("_reached_the_service_arm")
    # Either we reached the yield (our RuntimeError escapes) or construction
    # failed for a service-specific reason — either way NOT the SQLite message.
    assert "reads included" not in str(exc.value)
