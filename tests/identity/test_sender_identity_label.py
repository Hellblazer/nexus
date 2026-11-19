# SPDX-License-Identifier: AGPL-3.0-or-later
"""RDR-253 Step 5: an identity-shaped ``from`` is labelled by what the stamped session is bound to.

``mailbox_send`` lets a sender put its own identity in ``from`` (the guard accepts it), but the engine stamps only
the session id, so a receiver comparing ``from`` to the stamp called every such message "unverified". The receiver
now asks which identity the stamped session is bound to (a token-file read on its host): the same identity is no
label; another identity, or no binding it can read, stays ``unverified``.
"""
from __future__ import annotations

import importlib.util
import json
from pathlib import Path
from typing import Any

import pytest

from nexus.mcp.channel import _row_case
from nexus.tuple_sender import CASE_MISMATCH, UNVERIFIED, sender_case, sender_label

SENDER_SESSION = "3f2a9c1e-5b7d-4e8a-9c21-0a1b2c3d4e5f"
SCRIPTS = Path(__file__).resolve().parents[2] / "conexus" / "hooks" / "scripts"


def _bound(session: str, identity: str):
    return lambda s: identity if s == session else ""


def test_an_identity_from_that_the_stamped_session_is_bound_to_earns_no_label() -> None:
    bound = _bound(SENDER_SESSION, "i12")
    assert sender_case("i12", SENDER_SESSION, identity_of=bound) is None
    assert sender_label("i12", SENDER_SESSION, identity_of=bound) is None


@pytest.mark.parametrize("claim", ["i13", "i1", "I12", "i12x", "i"])
def test_another_identity_or_an_unresolved_binding_stays_unverified(claim: str) -> None:
    assert sender_case(claim, SENDER_SESSION, identity_of=_bound(SENDER_SESSION, "i12")) == CASE_MISMATCH
    assert sender_label(claim, SENDER_SESSION, identity_of=_bound(SENDER_SESSION, "i12")) == UNVERIFIED


def test_an_identity_from_with_no_resolver_or_a_failing_one_is_unverified() -> None:
    def boom(_session: str) -> str:
        raise OSError("no token files here")

    assert sender_case("i12", SENDER_SESSION) == CASE_MISMATCH
    assert sender_case("i12", SENDER_SESSION, identity_of=boom) == CASE_MISMATCH
    assert sender_case("i12", SENDER_SESSION, identity_of=lambda _s: "") == CASE_MISMATCH


def test_the_resolver_is_asked_only_for_an_identity_shaped_claim_under_a_canonical_stamp() -> None:
    asked: list[str] = []

    def spy(session: str) -> str:
        asked.append(session)
        return "i12"

    sender_case("peer-a", SENDER_SESSION, identity_of=spy)
    sender_case("i12", "not-a-uuid", identity_of=spy)
    sender_case("i12", None, identity_of=spy)
    assert asked == [], "no file read for a claim that is not an identity, or a stamp that is not a session"


def _token_file(config_dir: Path, identity: str, session: str) -> None:
    directory = config_dir / "identity"
    directory.mkdir(parents=True, exist_ok=True)
    (directory / "1.1.token").write_text(
        json.dumps({"identity": identity, "generation": 1, "pid": 1, "started_at": 1,
                    "session_id": session, "session_ids": [session]}), encoding="utf-8")


@pytest.fixture(params=["plugin", "wheel"])
def tag(request: pytest.FixtureRequest, monkeypatch: pytest.MonkeyPatch, tmp_path: Path):
    """The drain hook's ``_sender_tag`` (both copies) reading token files under *tmp_path*."""
    if request.param == "plugin":
        import sys  # noqa: PLC0415

        sys.path.insert(0, str(SCRIPTS))
        try:
            spec = importlib.util.spec_from_file_location("_plugin_drain_label", SCRIPTS / "mailbox_drain.py")
            assert spec and spec.loader
            mod = importlib.util.module_from_spec(spec)
            spec.loader.exec_module(mod)
        finally:
            sys.path.remove(str(SCRIPTS))
    else:
        import nexus.hooks.mailbox_drain as mod  # noqa: PLC0415
    monkeypatch.setattr(mod, "_config_dir", lambda: tmp_path)
    return mod._sender_tag


def test_the_drain_hook_labels_an_identity_from_by_the_senders_token_file(tag: Any, tmp_path: Path) -> None:
    row = {"sender_session": SENDER_SESSION}
    dims = {"from": "i12"}
    assert tag(row, dims) == f" [{UNVERIFIED}]", "no token file on this host: not resolvable"
    _token_file(tmp_path, "i12", SENDER_SESSION)
    assert tag(row, dims) == "", "the stamped session is bound to i12: the claim is backed"
    assert tag(row, {"from": "i13"}) == f" [{UNVERIFIED}]", "another seat's identity is never backed"


def test_the_channel_push_labels_an_identity_from_by_the_senders_token_file(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    import nexus.config as nx_config  # noqa: PLC0415
    from nexus.mcp.channel import TupleRow  # noqa: PLC0415

    monkeypatch.setattr(nx_config, "nexus_config_dir", lambda: tmp_path)
    _token_file(tmp_path, "i12", SENDER_SESSION)

    def row(frm: str) -> Any:
        return TupleRow(id="t1", subspace="mailbox/i1", template="mailbox", keys={}, dims={"from": frm}, body="hi",
                        claim_state=None, claimant=None, lease_until=None, attempts=0, consumed_at=None,
                        consumed_by=None, expires_at=None, created_at="2026-11-19T00:00:00Z",
                        sender_session=SENDER_SESSION, sender_session_known=True)

    assert _row_case(row("i12")) is None
    assert _row_case(row("i13")) == CASE_MISMATCH
