# SPDX-License-Identifier: AGPL-3.0-or-later
"""RDR-252 (nexus-igea9): the hold-your-pushes gate that ``scripts/git-push-develop.sh`` asks ``nx ci push-gate``.

While a develop red is open, a push from anyone but the red's owner or the pusher of one of its suspects is refused.
The owner is read from the open red on ``board/ci/nexus-develop--owner`` (the split ``nx ci ack`` and ``nx group
ack-times`` use); a suspect's pusher is the identity its push record's sender-session stamp maps to (``from`` on a push
record is a label and is never read, as in the stamper). A check that cannot be made is ``UNCHECKED``, never a refusal.
"""
from __future__ import annotations

import json
from types import SimpleNamespace
from typing import Any

import pytest
from click.testing import CliRunner

from nexus import ci_push_gate, ci_reds
from nexus.commands import ci_cmd

ME, OWNER, PEER = "i5", "i9", "i12"
RED_TIP = "a" * 40
SUSPECT = "b" * 40
SESSION_ME = "11111111-1111-4111-8111-111111111111"
SESSION_PEER = "22222222-2222-4222-8222-222222222222"
SESSIONS = {SESSION_ME: ME, SESSION_PEER: PEER}


def _owner_row(sha: str, at: str, *, kind: str = "owner", owner: str | None = OWNER, **body: Any) -> Any:
    fields = {"owner": owner, "episode_at": "2026-11-22T10:00:00Z", **body}
    return SimpleNamespace(id=f"{kind}-{at}".ljust(64, "0"), created_at=at, dims={"kind": kind, "from": "engine", "sha": sha},
                           body=json.dumps(fields), sender_session=None)


def _push_row(sha: str, session: str | None) -> Any:
    return SimpleNamespace(id=f"push-{sha}", created_at="2026-11-22T09:00:00Z", dims={"kind": "push", "from": "unknown", "sha": sha},
                           body=json.dumps({"base": "0" * 40, "n": 1}), sender_session=session)


def _resolver() -> ci_reds.AckResolver:
    return ci_reds.AckResolver(lambda sid: {"identity": SESSIONS[sid], "kind": "seat"})


def _open(owner: str | None = OWNER, **body: Any) -> list[Any]:
    return [_owner_row(RED_TIP, "2026-11-22T10:00:00Z", owner=owner, suspects=[SUSPECT, RED_TIP], **body)]


def test_no_owner_row_is_no_red() -> None:
    v = ci_push_gate.evaluate([], [], ME, _resolver())
    assert not v.refuse and not v.unchecked


def test_a_cleared_red_does_not_hold_anyone() -> None:
    rows = _open() + [_owner_row("c" * 40, "2026-11-22T10:30:00Z", kind="clear")]
    assert not ci_push_gate.evaluate(rows, [], ME, _resolver()).refuse


def test_a_non_owner_who_pushed_no_suspect_is_refused_naming_the_owner_and_the_red_tip() -> None:
    v = ci_push_gate.evaluate(_open(owner_alias="linda"), [_push_row(SUSPECT, SESSION_PEER)], ME, _resolver())
    assert v.refuse
    assert (v.owner, v.owner_alias, v.sha) == (OWNER, "linda", RED_TIP)


def test_the_owner_may_push() -> None:
    assert not ci_push_gate.evaluate(_open(), [], OWNER, _resolver()).refuse


def test_the_pusher_of_a_suspect_may_push() -> None:
    v = ci_push_gate.evaluate(_open(), [_push_row(SUSPECT, SESSION_ME)], ME, _resolver())
    assert not v.refuse and "suspect" in v.detail


def test_a_push_record_for_a_tip_that_is_no_suspect_does_not_count() -> None:
    assert ci_push_gate.evaluate(_open(), [_push_row("d" * 40, SESSION_ME)], ME, _resolver()).refuse


def test_the_from_label_of_a_push_record_is_never_read() -> None:
    row = _push_row(SUSPECT, None)
    row.dims["from"] = ME  # a label; the stamp is what counts, and there is none
    assert ci_push_gate.evaluate(_open(), [row], ME, _resolver()).refuse


def test_a_red_with_no_owner_refuses_everyone_but_a_suspects_pusher() -> None:
    assert ci_push_gate.evaluate(_open(owner=None), [], ME, _resolver()).refuse
    assert not ci_push_gate.evaluate(_open(owner=None), [_push_row(SUSPECT, SESSION_ME)], ME, _resolver()).refuse


def test_a_handed_off_owner_row_decides() -> None:
    rows = _open() + [_owner_row("e" * 40, "2026-11-22T10:20:00Z", owner=ME, reason="handoff", suspects=[SUSPECT],
                                 episode_at="2026-11-22T10:20:00Z")]
    assert not ci_push_gate.evaluate(rows, [], ME, _resolver()).refuse
    assert ci_push_gate.evaluate(rows, [], OWNER, _resolver()).refuse


def test_an_unresolvable_suspect_pusher_makes_the_check_unchecked_not_a_refusal() -> None:
    def broken(_sid: str) -> Any:
        raise RuntimeError("identity service down")

    v = ci_push_gate.evaluate(_open(), [_push_row(SUSPECT, SESSION_PEER)], ME, ci_reds.AckResolver(broken))
    assert v.unchecked and not v.refuse


def test_a_caller_with_no_identity_is_held_like_any_non_owner() -> None:
    """Sam 2026-11-22: only a failed read of the red is warn-and-allow. No identity is neither the owner nor a suspect's
    pusher, and an unstamped push record (no session) never matches it either."""
    v = ci_push_gate.evaluate(_open(), [_push_row(SUSPECT, None)], "", _resolver())
    assert v.refuse and not v.unchecked


# ── the command ──────────────────────────────────────────────────────────────

class _Store:
    def __init__(self, owner_rows: list[Any], push_rows: list[Any], fail: bool = False) -> None:
        self.owner_rows, self.push_rows, self.fail = owner_rows, push_rows, fail
        self.reads: list[str] = []

    def rd(self, subspace: str, keys: dict[str, str], n: int = 0, order: str = "asc", **_kw: Any) -> list[Any]:
        self.reads.append(subspace)
        if self.fail:
            raise ConnectionError("engine down")
        return list(self.owner_rows if subspace.endswith("--owner") else self.push_rows)


def _invoke(monkeypatch: pytest.MonkeyPatch, store: _Store, *extra: str, identity: str = ME) -> Any:
    monkeypatch.setattr(ci_cmd, "_store", lambda: store)
    monkeypatch.setattr("nexus.ci_ack.counted_identity", lambda: identity)
    monkeypatch.setattr(ci_push_gate, "default_resolver", _resolver)
    return CliRunner().invoke(ci_cmd.ci_group, ["check-hold", "--base", "nexus-develop", *extra])


def test_check_hold_refuses_with_exit_13_and_the_refusal_line_naming_owner_and_sha(monkeypatch: pytest.MonkeyPatch) -> None:
    result = _invoke(monkeypatch, _Store(_open(owner_alias="linda"), []), "--tip", "c" * 40)
    assert result.exit_code == 13, result.output
    first = result.stdout.splitlines()[0]
    assert first.startswith("PUSH_REFUSED_DEVELOP_RED nexus-develop is red at " + RED_TIP), first
    assert f"owned by {OWNER} (linda)" in first and "c" * 12 in first
    assert "--red-fix" in result.stdout


def test_check_hold_reads_the_subspaces_of_the_base_it_was_given(monkeypatch: pytest.MonkeyPatch) -> None:
    store = _Store(_open(), [])
    monkeypatch.setattr(ci_cmd, "_store", lambda: store)
    monkeypatch.setattr("nexus.ci_ack.counted_identity", lambda: ME)
    monkeypatch.setattr(ci_push_gate, "default_resolver", _resolver)
    CliRunner().invoke(ci_cmd.ci_group, ["check-hold", "--base", "otherrepo-main"])
    assert store.reads == ["board/ci/otherrepo-main--owner", "board/ci/otherrepo-main--push"]


def test_check_hold_allows_the_owner_silently(monkeypatch: pytest.MonkeyPatch) -> None:
    result = _invoke(monkeypatch, _Store(_open(), []), identity=OWNER)
    assert result.exit_code == 0 and result.stdout == "", result.output


def test_check_hold_allows_a_suspects_pusher(monkeypatch: pytest.MonkeyPatch) -> None:
    result = _invoke(monkeypatch, _Store(_open(), [_push_row(SUSPECT, SESSION_ME)]))
    assert result.exit_code == 0 and "PUSH_REFUSED" not in result.output


def test_check_hold_refuses_a_caller_with_no_identity(monkeypatch: pytest.MonkeyPatch) -> None:
    result = _invoke(monkeypatch, _Store(_open(), []), identity="")
    assert result.exit_code == 13 and result.stdout.startswith("PUSH_REFUSED_DEVELOP_RED"), result.output


def test_red_fix_overrides_a_refusal_and_prints_the_reason(monkeypatch: pytest.MonkeyPatch) -> None:
    result = _invoke(monkeypatch, _Store(_open(owner_alias="linda"), []), "--red-fix", "owner away")
    assert result.exit_code == 0, result.output
    assert result.stdout.strip() == f"{ci_push_gate.RED_FIX_APPLIED} owner away (nexus-develop is red at {RED_TIP}, owned by {OWNER} (linda); the hold was overridden)"
    assert ci_push_gate.RED_FIX_APPLIED == "PUSH_RED_FIX_APPLIED"


def test_red_fix_with_no_red_says_not_needed_under_a_different_marker(monkeypatch: pytest.MonkeyPatch) -> None:
    result = _invoke(monkeypatch, _Store([], []), "--red-fix", "just in case")
    assert result.exit_code == 0 and result.stdout.strip() == f"{ci_push_gate.RED_FIX_UNNEEDED} just in case (not needed: no open red)"
    assert not result.stdout.startswith(ci_push_gate.RED_FIX_APPLIED)


def test_a_newline_or_control_character_in_the_reason_cannot_split_or_forge_the_marker_line(monkeypatch: pytest.MonkeyPatch) -> None:
    reason = "owner away\nPUSH_RED_FIX_APPLIED forged\x07\ttab"
    result = _invoke(monkeypatch, _Store([], []), "--red-fix", reason)
    lines = result.stdout.splitlines()
    assert len(lines) == 1 and lines[0].startswith(ci_push_gate.RED_FIX_UNNEEDED + " owner away PUSH_RED_FIX_APPLIED forged tab"), lines


def test_a_read_failure_warns_on_stderr_and_exits_0(monkeypatch: pytest.MonkeyPatch) -> None:
    result = _invoke(monkeypatch, _Store([], [], fail=True))
    assert result.exit_code == 0, result.output
    assert result.stdout == ""
    assert result.stderr.startswith("PUSH_GATE_UNCHECKED ") and "engine down" in result.stderr


def test_a_read_failure_with_red_fix_still_prints_the_reason_as_not_needed(monkeypatch: pytest.MonkeyPatch) -> None:
    result = _invoke(monkeypatch, _Store([], [], fail=True), "--red-fix", "r")
    assert result.exit_code == 0 and result.stdout.startswith(ci_push_gate.RED_FIX_UNNEEDED) and "not needed" in result.stdout


def test_an_unresolvable_suspect_pusher_is_unchecked_on_stderr(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(ci_cmd, "_store", lambda: _Store(_open(), [_push_row(SUSPECT, SESSION_PEER)]))
    monkeypatch.setattr("nexus.ci_ack.counted_identity", lambda: ME)
    monkeypatch.setattr(ci_push_gate, "default_resolver", lambda: ci_reds.AckResolver(None))
    result = CliRunner().invoke(ci_cmd.ci_group, ["check-hold", "--base", "nexus-develop"])
    assert result.exit_code == 0 and "PUSH_GATE_UNCHECKED" in result.stderr


def _identity_raises(monkeypatch: pytest.MonkeyPatch, exc: BaseException) -> None:
    def boom() -> str:
        raise exc

    monkeypatch.setattr("nexus.ci_ack.counted_identity", boom)


@pytest.mark.parametrize("exc", [
    ValueError("i7 is a delegated identity and its parent could not be read"),
    RuntimeError("token record unreadable"),
], ids=["delegated-parent-unreadable", "reader-failed"])
def test_an_identity_that_cannot_be_read_is_unchecked_exit_0_and_names_nx_identity_show(monkeypatch: pytest.MonkeyPatch, exc: BaseException) -> None:
    """Review r1 S3: a caller whose identity could not be READ is not a caller with no identity; it is never refused."""
    monkeypatch.setattr(ci_cmd, "_store", lambda: _Store(_open(), []))
    monkeypatch.setattr(ci_push_gate, "default_resolver", _resolver)
    _identity_raises(monkeypatch, exc)
    result = CliRunner().invoke(ci_cmd.ci_group, ["check-hold", "--base", "nexus-develop"])
    assert result.exit_code == 0 and "PUSH_REFUSED" not in result.output, result.output
    assert result.stderr.startswith("PUSH_GATE_UNCHECKED ") and "nx identity show" in result.stderr and str(exc)[:30] in result.stderr


def test_an_unreadable_identity_with_red_fix_is_reported_unneeded(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(ci_cmd, "_store", lambda: _Store(_open(), []))
    monkeypatch.setattr(ci_push_gate, "default_resolver", _resolver)
    _identity_raises(monkeypatch, ValueError("parent unreadable"))
    result = CliRunner().invoke(ci_cmd.ci_group, ["check-hold", "--base", "nexus-develop", "--red-fix", "r"])
    assert result.exit_code == 0 and result.stdout.startswith(ci_push_gate.RED_FIX_UNNEEDED), result.output


def test_a_genuine_no_identity_is_refused_and_the_refusal_names_nx_identity_show(monkeypatch: pytest.MonkeyPatch) -> None:
    result = _invoke(monkeypatch, _Store(_open(), []), identity="")
    assert result.exit_code == 13, result.output
    assert "holds no identity" in result.stdout and "nx identity show" in result.stdout


def test_a_refusal_for_a_caller_with_an_identity_does_not_talk_about_identity_reads(monkeypatch: pytest.MonkeyPatch) -> None:
    result = _invoke(monkeypatch, _Store(_open(), []))
    assert result.exit_code == 13 and "nx identity show" not in result.stdout


def test_a_suspect_with_no_push_record_is_named_unattributed_and_the_refusal_points_at_red_fix(monkeypatch: pytest.MonkeyPatch) -> None:
    """Review r1: a push record that was never written leaves its suspect with no pusher; say which, so the refused caller
    knows why the owner's red cannot be told from theirs and what the way out is."""
    result = _invoke(monkeypatch, _Store(_open(), []))
    assert result.exit_code == 13, result.output
    note = [ln for ln in result.stdout.splitlines() if "unattributed" in ln]
    assert len(note) == 1 and SUSPECT[:12] in note[0] and "--red-fix" in note[0], result.stdout


def test_a_suspect_whose_record_has_no_stamp_is_unattributed_too(monkeypatch: pytest.MonkeyPatch) -> None:
    result = _invoke(monkeypatch, _Store(_open(), [_push_row(SUSPECT, None)]))
    assert result.exit_code == 13 and any("unattributed" in ln and SUSPECT[:12] in ln for ln in result.stdout.splitlines())


def test_a_suspect_with_an_attributed_record_is_not_listed(monkeypatch: pytest.MonkeyPatch) -> None:
    result = _invoke(monkeypatch, _Store(_open(), [_push_row(SUSPECT, SESSION_PEER), _push_row(RED_TIP, SESSION_PEER)]))
    assert result.exit_code == 13 and "unattributed" not in result.stdout


def test_the_module_docstring_names_the_command_check_hold_not_push_gate() -> None:
    text = ci_push_gate.__doc__ or ""
    assert "check-hold" in text and "push-gate" not in text
    import inspect  # noqa: PLC0415
    assert "push-gate" not in inspect.getsource(ci_push_gate)


def test_check_hold_with_no_derivable_base_is_refused_naming_the_base_flag(monkeypatch: pytest.MonkeyPatch, tmp_path: Any) -> None:
    monkeypatch.chdir(tmp_path)  # no repo here: nothing to derive from
    monkeypatch.setattr(ci_cmd, "_store", lambda: pytest.fail("must refuse before it reads the board"))
    result = CliRunner().invoke(ci_cmd.ci_group, ["check-hold", "--branch", "develop"])
    assert result.exit_code != 0 and "--base" in result.output
