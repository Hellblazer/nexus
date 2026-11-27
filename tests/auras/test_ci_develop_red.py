# SPDX-License-Identifier: AGPL-3.0-or-later
"""The first consumer as shipped: ``ci_red_notice.lua`` and ``ci_develop_red.aura.toml``
(RDR-249 Phase 3 Step 1, Technical Design 7, bead nexus-mdbds.12.1), retargeted by RDR-252 Step 4 part B
(bead nexus-igea9.7) onto the owner post ``board/ci/nexus-develop--owner``: one display per state of an owner row.

The procedure runs in the test-local harness ``tests/test_procedure_gate_record.py`` uses: the
environment the runtime builds from the static check's own allowlists, loaded in mode "t". The
aura tests read the real package root (``library.shipped_root``), not a planted copy, so a file
missing from ``src/nexus/procedures`` fails them. The dictionary check against a real engine's
registry for the shipped file is in ``test_registry_engine.py``; the wheel check is in
``tests/test_shipped_procedures_lint.py``. No engine, no tenant here.
"""
from __future__ import annotations

from pathlib import Path

import lupa.lua54 as lua54
import pytest

from nexus.auras import enumerate_auras, load_aura, parse_aura
from nexus.auras.aura import expand_template
from nexus.auras.check import check_auras, check_serving_group
from nexus.auras.enable import resolve
from nexus.auras.load_gate import GroupRecord
from nexus.auras.threads_block import MAX_STATE_CHARS, sanitize_state
from nexus.lua_runtime import header as hd
from nexus.lua_runtime import library
from nexus.lua_runtime import static_check as sc

ROOT = library.shipped_root()
SOURCE = (ROOT / "ci_red_notice.lua").read_bytes()
SHA = "8b30c161bdb23adab27171f3185c2b52f158b736"


def _run(**args: object) -> dict:
    rt = lua54.LuaRuntime(register_eval=False, register_builtins=False, unpack_returned_tuples=False)
    build = rt.execute(
        """
        return function(base, libs)
          local env = {}
          for _, name in ipairs(base) do env[name] = _G[name] end
          for lib, members in pairs(libs) do
            local t = {}
            for _, m in ipairs(members) do t[m] = _G[lib][m] end
            env[lib] = t
          end
          getmetatable("").__index = env.string
          return env
        end
        """
    )
    env = build(
        rt.table_from(sorted(sc.BASE_FUNCTIONS)),
        rt.table_from({lib: rt.table_from(sorted(m)) for lib, m in sc.LIB_MEMBERS.items()}),
    )
    loaded = rt.eval("load")(SOURCE.decode("ascii"), "=ci_red_notice", "t", env)
    fn = loaded[0] if isinstance(loaded, tuple) else loaded
    return dict(fn(rt.table_from(args)).items())


# -- the procedure ----------------------------------------------------------------

ME = "i7"
OTHER = "i9"
THIRD = "i12"
SHORT = SHA[:7]
OWNER_SUBSPACE = "board/ci/nexus-develop--owner"
#: What the owner displays append so the owner can acknowledge: the helper and the sha's first 7 digits. The helper
#: posts the row from the caller's own identity id (nexus-igea9), so the 200-character state line needs no command.
ACK = f" Ack: nx ci ack {SHA[:7]}."


def _row(
    owner: str | None,
    next_: str | None = None,
    reason: str = "earliest_suspect",
    refresh: bool = False,
    me: str = ME,
    sha: str = SHA,
    owner_alias: str | None = None,
    next_alias: str | None = None,
    stalled: bool | None = None,
    stalled_owner: str | None = None,
    stalled_since: str | None = None,
    stalled_sign: str | None = None,
    open_minutes: int | None = None,
    previous_owner: str | None = None,
) -> dict:
    """The arguments the aura maps from an owner row (body.owner, body.owner_alias, body.next, body.next_alias,
    body.reason, body.refresh, dims.sha) and from the session (self.identity). A null owner or next, and an alias the
    row lacks (an old engine, or an identity with no live alias), is an absent optional argument, as the runtime passes
    it."""
    out: dict = {"sha": sha, "reason": reason, "refresh": refresh, "me": me}
    if owner is not None:
        out["owner"] = owner
    if next_ is not None:
        out["next"] = next_
    if owner_alias is not None:
        out["owner_alias"] = owner_alias
    if next_alias is not None:
        out["next_alias"] = next_alias
    for name, value in (("stalled", stalled), ("stalled_owner", stalled_owner), ("stalled_since", stalled_since),
                        ("stalled_sign", stalled_sign), ("open_minutes", open_minutes),
                        ("previous_owner", previous_owner)):
        if value is not None:
            out[name] = value
    return out


#: The engine's stall escalation: reason escalated, stalled true, open_minutes since the red began, and the last sign of life
#: (stalled_sign ack or push, with that identity and instant; all three absent when there was none).
STALLED_SINCE = "2026-11-22T11:14:00Z"
STALL = {"stalled": True, "stalled_owner": OTHER, "stalled_since": STALLED_SINCE, "stalled_sign": "ack", "open_minutes": 74}
NO_SIGN = {"stalled_owner": None, "stalled_since": None, "stalled_sign": None}


def _stalled(owner: str | None, me: str = ME, **over: object) -> dict:
    """An escalated row of a red open 74 minutes whose last sign was OTHER's ack at 11:14 UTC; *owner* is who it names (the
    leader)."""
    return _row(owner, reason="escalated", me=me, **{**STALL, **over})


def test_the_header_declares_the_owner_row_fields_and_me_and_grants_nothing() -> None:
    h = hd.parse(SOURCE, name="ci_red_notice")
    assert [(a.name, a.type, a.optional) for a in h.args] == [
        ("sha", "string", False),
        ("branch", "string", True),
        ("owner", "string", True),
        ("owner_alias", "string", True),
        ("next", "string", True),
        ("next_alias", "string", True),
        ("refresh", "boolean", False),
        ("reason", "string", False),
        ("stalled", "boolean", True),
        ("stalled_owner", "string", True),
        ("stalled_since", "string", True),
        ("stalled_sign", "string", True),
        ("open_minutes", "integer", True),
        ("previous_owner", "string", True),
        ("me", "string", False),
    ]
    assert h.grants == frozenset()
    assert sc.check(SOURCE, h.grants) == frozenset()


# The displays, one per state of an owner row (bead nexus-igea9.7 item 11), each from a fixture row.
DISPLAYS = [
    pytest.param(
        _row(ME), f"Develop is red at {SHORT}, first red in your push. Diagnose it." + ACK, id="owner-earliest-suspect"
    ),
    pytest.param(
        _row(ME, reason="no_record"),
        f"Develop is red at {SHORT}; the leader takes it (no_record). Diagnose it." + ACK,
        id="owner-leader-takes-no-record",
    ),
    pytest.param(
        _row(ME, reason="handoff"),
        f"Develop is red at {SHORT}; it was handed to you after no acknowledgement. Diagnose it." + ACK,
        id="owner-after-handoff",
    ),
    pytest.param(
        _row(ME, reason="escalated"),
        f"Develop is red at {SHORT}; no one acknowledged. Raise an ask to Sam.",
        id="escalated-names-me",
    ),
    pytest.param(
        _row(None, reason="escalated"),
        f"Develop is red at {SHORT}; no one acknowledged. Raise an ask to Sam.",
        id="escalated-no-owner",
    ),
    pytest.param(_row(OTHER, ME), f"Develop is red at {SHORT}; {OTHER} owns it. You are next up.", id="next-up"),
    pytest.param(
        _row(OTHER, THIRD), f"Develop is red at {SHORT}; {OTHER} owns it. Hold your pushes.", id="everyone-else"
    ),
    pytest.param(
        _row(OTHER), f"Develop is red at {SHORT}; {OTHER} owns it. Hold your pushes.", id="everyone-else-no-next"
    ),
    pytest.param(
        _row(OTHER, reason="escalated"),
        f"Develop is red at {SHORT}; {OTHER} owns it. Hold your pushes.",
        id="escalated-names-someone-else",
    ),
    pytest.param(
        _row(ME, reason="disclaimed"),
        f"Develop is red at {SHORT}; handed to you: the previous owner said it is not theirs. Diagnose it." + ACK,
        id="owner-after-disclaim-previous-owner-unnamed",
    ),
    pytest.param(
        _row(ME, reason="disclaimed", previous_owner=OTHER),
        f"Develop is red at {SHORT}; handed to you: {OTHER} said it is not theirs. Diagnose it." + ACK,
        id="owner-after-disclaim-names-the-previous-owner",
    ),
    pytest.param(
        _stalled(ME),
        f"Develop is red at {SHORT}, still open after 74 min; last activity: {OTHER} acknowledged at 11:14 UTC. "
        "Raise an ask to Sam.",
        id="stalled-the-leader-last-sign-an-ack",
    ),
    pytest.param(
        _stalled(ME, stalled_sign="push", stalled_since="2026-11-22T09:05:59.5Z"),
        f"Develop is red at {SHORT}, still open after 74 min; last activity: {OTHER} pushed at 09:05 UTC. "
        "Raise an ask to Sam.",
        id="stalled-the-leader-last-sign-a-push",
    ),
    pytest.param(
        _stalled(None),
        f"Develop is red at {SHORT}, still open after 74 min; last activity: {OTHER} acknowledged at 11:14 UTC. "
        "Raise an ask to Sam.",
        id="stalled-no-owner-named-a-member",
    ),
    pytest.param(
        _stalled(ME, **NO_SIGN),
        f"Develop is red at {SHORT}, still open after 74 min with no acknowledgement or fix push. Raise an ask to Sam.",
        id="stalled-the-leader-no-sign",
    ),
    pytest.param(
        _stalled(THIRD, me=OTHER),
        f"Develop is red at {SHORT}, still open after 74 min; your last activity: acknowledged at 11:14 UTC. "
        "The leader is raising it with Sam.",
        id="stalled-the-owner-with-the-last-sign-an-ack",
    ),
    pytest.param(
        _stalled(THIRD, me=OTHER, stalled_sign="push"),
        f"Develop is red at {SHORT}, still open after 74 min; your last activity: pushed at 11:14 UTC. "
        "The leader is raising it with Sam.",
        id="stalled-the-owner-with-the-last-sign-a-push",
    ),
    pytest.param(
        _stalled(THIRD),
        f"Develop is red at {SHORT}, still open after 74 min; last activity: {OTHER} acknowledged at 11:14 UTC. "
        "The leader is raising it with Sam. Hold your pushes.",
        id="stalled-everyone-else",
    ),
    pytest.param(
        _stalled(THIRD, **NO_SIGN),
        f"Develop is red at {SHORT}, still open after 74 min with no acknowledgement or fix push. "
        "The leader is raising it with Sam. Hold your pushes.",
        id="stalled-everyone-else-no-sign",
    ),
    pytest.param(
        _stalled(ME, open_minutes=None, stalled_since="2026-11-22T23:07:00.123456Z"),
        f"Develop is red at {SHORT}, still open; last activity: {OTHER} acknowledged at 23:07 UTC. Raise an ask to Sam.",
        id="stalled-open-minutes-absent",
    ),
]


@pytest.mark.parametrize(("args", "text"), DISPLAYS)
def test_each_state_of_an_owner_row_shows_its_own_text(args: dict, text: str) -> None:
    out = _run(**args)
    assert out["show"] is True
    assert out["text"] == text


@pytest.mark.parametrize(
    "reason", ["owner_unleased", "owner_not_live", "owner_not_member", "no_record", "chain_gap", "stamper_error"]
)
def test_every_reason_the_leader_takes_is_named_in_the_owner_text(reason: str) -> None:
    text = _run(**_row(ME, reason=reason))["text"]
    assert text == f"Develop is red at {SHORT}; the leader takes it ({reason}). Diagnose it." + ACK


def test_a_hand_off_text_is_not_the_first_red_in_your_push_text() -> None:
    """Bead note 4: a handed-off owner did not push the red, so "first red in your push" is wrong for that row."""
    text = _run(**_row(ME, reason="handoff"))["text"]
    assert "first red in your push" not in text and "handed to you after no acknowledgement" in text


def test_a_next_up_that_is_also_the_owner_is_shown_as_the_owner() -> None:
    assert _run(**_row(ME, ME))["text"].startswith(f"Develop is red at {SHORT}, first red in your push.")


def test_a_row_naming_no_owner_that_is_not_escalated_holds_everyone() -> None:
    out = _run(**_row(None, reason="stamper_error"))
    assert out["text"] == f"Develop is red at {SHORT}; no one is named as owner. Hold your pushes."
    assert out["wake"] is False


# wake: true only on refresh false, for the owner, and on an escalated row that names me or has no owner. Next up is shown
# the red and not woken (RDR-252 audit S2, nexus-igea9): a nudge row wakes it, tests/auras/test_ci_red_nudge.py.
WAKES = [
    pytest.param(_row(ME), True, id="owner"),
    pytest.param(_row(ME, refresh=True), False, id="owner-refresh"),
    pytest.param(_row(ME, reason="handoff"), True, id="handoff-owner"),
    pytest.param(_row(ME, reason="no_record"), True, id="leader-takes-owner"),
    pytest.param(_row(OTHER, ME), False, id="next-up-is-shown-not-woken"),
    pytest.param(_row(OTHER, ME, refresh=True), False, id="next-up-refresh"),
    pytest.param(_row(OTHER, THIRD), False, id="everyone-else"),
    pytest.param(_row(OTHER), False, id="everyone-else-no-next"),
    pytest.param(_row(OTHER, THIRD, refresh=True), False, id="everyone-else-refresh"),
    pytest.param(_row(ME, reason="escalated"), True, id="escalated-names-me"),
    pytest.param(_row(ME, reason="escalated", refresh=True), False, id="escalated-names-me-refresh"),
    pytest.param(_row(None, reason="escalated"), True, id="escalated-no-owner-wakes-a-member"),
    pytest.param(_row(None, reason="escalated", refresh=True), False, id="escalated-no-owner-refresh"),
    pytest.param(_row(OTHER, reason="escalated"), False, id="escalated-names-someone-else"),
    pytest.param(_row(ME, reason="disclaimed"), True, id="disclaimed-owner"),
    pytest.param(_row(OTHER, ME, reason="disclaimed"), False, id="disclaimed-next-up-not-woken"),
    pytest.param(_stalled(ME), True, id="stalled-the-leader-wakes"),
    pytest.param(_stalled(ME, refresh=True), False, id="stalled-the-leader-refresh"),
    pytest.param(_stalled(None), True, id="stalled-no-owner-wakes-a-member"),
    pytest.param(_stalled(THIRD, me=OTHER), False, id="stalled-owner-not-woken"),
    pytest.param(_stalled(None, me=OTHER), False, id="stalled-owner-not-woken-when-no-owner-named"),
    pytest.param(_stalled(THIRD), False, id="stalled-everyone-else-not-woken"),
]


def test_an_escalated_row_that_is_not_marked_stalled_keeps_the_no_one_acknowledged_text() -> None:
    """stalled false, or absent with stalled_* fields left over, is the plain escalation: nobody acknowledged."""
    expected = f"Develop is red at {SHORT}; no one acknowledged. Raise an ask to Sam."
    assert _run(**_stalled(ME, stalled=False))["text"] == expected
    assert _run(**_row(ME, reason="escalated", stalled_owner=OTHER, open_minutes=74))["text"] == expected


def test_a_sign_with_no_identity_reads_as_no_sign_and_an_unknown_sign_too() -> None:
    no_sign = f"Develop is red at {SHORT}, still open after 74 min with no acknowledgement or fix push. Raise an ask to Sam."
    args = _stalled(ME)
    del args["stalled_owner"]
    assert _run(**args)["text"] == no_sign
    assert _run(**_stalled(ME, stalled_sign="review"))["text"] == no_sign


@pytest.mark.parametrize("bad", ["yesterday", "2026-13-45T25:61:61Z", "2026-11-22", "2026-11-22T24:00:00Z"])
def test_a_stall_instant_that_does_not_parse_leaves_the_time_out_not_an_error(bad: str) -> None:
    text = _run(**_stalled(ME, stalled_since=bad))["text"]
    assert text == f"Develop is red at {SHORT}, still open after 74 min; last activity: {OTHER} acknowledged. Raise an ask to Sam."


def test_the_stall_text_no_longer_claims_an_acknowledgement_it_cannot_show() -> None:
    for args in (_stalled(ME, stalled_sign="push"), _stalled(ME, **NO_SIGN), _stalled(THIRD, me=OTHER, stalled_sign="push")):
        text = _run(**args)["text"]
        assert "acknowledged by" not in text and "you acknowledged" not in text, text


@pytest.mark.parametrize(("args", "wake"), WAKES)
def test_wake_is_true_only_where_the_row_asks_this_session_to_act(args: dict, wake: bool) -> None:
    assert _run(**args)["wake"] is wake


@pytest.mark.parametrize("args", [pytest.param(p.values[0], id=p.id) for p in DISPLAYS])
def test_every_text_passes_the_threads_sanitiser_intact(args: dict) -> None:
    text = _run(**args)["text"]
    assert sanitize_state(text) == text
    assert len(text) <= MAX_STATE_CHARS


def test_the_longest_owner_text_with_a_full_sha_keeps_its_ack_hint_inside_the_state_line() -> None:
    text = _run(**_row(ME, reason="owner_not_member"))["text"]
    assert text.endswith(ACK) and len(text) <= MAX_STATE_CHARS, len(text)


def test_every_owner_display_carries_the_hint_whole_inside_the_state_line_at_the_longest_inputs() -> None:
    """nexus-igea9: the hint is a fixed `nx ci ack <sha7>`, so it never overflows and is never dropped. Pinned at a
    64-digit sha, the longest reason (32 characters) and the longest identity the procedure accepts to show."""
    longest_reason = "r" * 32
    shown = [
        _run(**_row("i" + "9" * 12, reason=reason, sha="a" * 64, me="i" + "9" * 12))["text"]
        for reason in ("earliest_suspect", "handoff", longest_reason)
    ]
    for text in shown:
        assert text.endswith(" Ack: nx ci ack aaaaaaa."), text
        assert len(text) <= MAX_STATE_CHARS, (len(text), text)
    assert len(shown[2]) == max(len(t) for t in shown), "the longest reason is the longest text"
    assert max(len(t) for t in shown) <= MAX_STATE_CHARS - 40, "the hint has room to spare: no drop path is left to test"


def test_the_hint_names_the_helper_not_a_post_the_caller_has_to_field_by_hand() -> None:
    """The defect: the old hint said `post kind=ack ... to <subspace>`, and a hand post of it wrote `from=<alias>`."""
    text = _run(**_row(ME))["text"]
    assert "nx ci ack " in text and "kind=ack" not in text and "post " not in text.split("Diagnose it.")[1]


@pytest.mark.parametrize("sha", ["a" * 7, "a" * 64])
def test_a_sha_of_exactly_7_or_64_hex_digits_is_accepted(sha: str) -> None:
    assert _run(**_row(ME, sha=sha))["text"].startswith("Develop is red at aaaaaaa, first red")


@pytest.mark.parametrize("bad", ["8B30C161", "8b30c1", "x" * 7, "8b30c161 ", "a" * 65])
def test_a_sha_that_is_not_7_to_64_lower_case_hex_is_refused(bad: str) -> None:
    with pytest.raises(lua54.LuaError, match="sha must be 7 to 64 lower-case hex digits"):
        _run(**_row(ME, sha=bad))


@pytest.mark.parametrize("field", ["owner", "next", "me"])
def test_an_identity_that_is_not_an_address_segment_is_refused_without_echoing_it(field: str) -> None:
    args = _row(OTHER, THIRD)
    args[field] = "evil\nneeds_sam: yes"
    with pytest.raises(lua54.LuaError, match=f"{field} must be") as err:
        _run(**args)
    assert "evil" not in str(err.value) and "needs_sam" not in str(err.value)


def test_a_reason_that_is_not_a_lower_case_word_is_refused_without_echoing_it() -> None:
    with pytest.raises(lua54.LuaError, match="reason must be") as err:
        _run(**_row(ME, reason="no record\n## injected"))
    assert "injected" not in str(err.value)


def test_a_reason_the_engine_adds_later_is_shown_as_a_leader_take() -> None:
    text = _run(**_row(ME, reason="new_reason"))["text"]
    assert text == f"Develop is red at {SHORT}; the leader takes it (new_reason). Diagnose it." + ACK


# RDR-252, Sam 2026-11-19 (T2 nexus/sam-ruling-red-notice-alias-2026-11-19): the engine stamps owner_alias and
# next_alias beside the ids, and the notice names a seat by its alias. A row with no alias (an engine before 0.1.198,
# or an identity with no live alias) shows the id, as before.
def test_the_notice_names_the_owner_by_alias_when_the_row_carries_one() -> None:
    out = _run(**_row(OTHER, THIRD, owner_alias="halcyon", next_alias="marginalia"))
    assert out["text"] == f"Develop is red at {SHORT}; halcyon owns it. Hold your pushes."


def test_the_notice_names_the_owner_by_id_when_the_row_has_no_alias() -> None:
    out = _run(**_row(OTHER, THIRD))
    assert out["text"] == f"Develop is red at {SHORT}; {OTHER} owns it. Hold your pushes."


def test_next_up_sees_the_owners_alias_and_is_not_woken() -> None:
    out = _run(**_row(OTHER, ME, owner_alias="halcyon", next_alias="tamsin"))
    assert out["text"] == f"Develop is red at {SHORT}; halcyon owns it. You are next up."
    assert out["show"] is True and out["wake"] is False


def test_a_hand_off_that_seats_me_as_owner_wakes_through_the_owner_branch() -> None:
    """The case that used to wake the next-up session: after a hand-off the row names it OWNER, and that wakes."""
    out = _run(**_row(ME, reason="handoff"))
    assert out["wake"] is True and "handed to you" in out["text"]
    assert _run(**_row(OTHER, ME, reason="handoff"))["wake"] is False, "named next up on a hand-off row: still no wake"


def test_an_alias_is_a_name_only_and_never_decides_who_this_session_is() -> None:
    """The owner test is on ids: an alias that equals this session's id changes nothing about who owns the red."""
    out = _run(**_row(OTHER, owner_alias=ME))
    assert out["text"] == f"Develop is red at {SHORT}; {ME} owns it. Hold your pushes."
    assert out["wake"] is False


def test_an_owner_who_is_me_ignores_the_alias_and_keeps_the_owner_text() -> None:
    assert _run(**_row(ME, owner_alias="halcyon"))["text"] == f"Develop is red at {SHORT}, first red in your push. Diagnose it." + ACK


@pytest.mark.parametrize("field", ["owner_alias", "next_alias"])
def test_an_alias_that_is_not_an_address_segment_is_refused_without_echoing_it(field: str) -> None:
    args = _row(OTHER, THIRD)
    args[field] = "evil\nneeds_sam: yes"
    with pytest.raises(lua54.LuaError, match=f"{field} must be") as err:
        _run(**args)
    assert "evil" not in str(err.value) and "needs_sam" not in str(err.value)
    assert "alias" in str(err.value) and "identity id" not in str(err.value), "the text names what the value is"


def test_a_row_missing_a_required_field_still_strikes_through_the_real_header() -> None:
    """docs/auras.md says what strikes: the aura runtime leaves out a body field the row lacks, and the procedure's
    header then refuses the run when that argument is required (kind ``args``) and accepts it when it is optional."""
    from nexus.lua_runtime import header as header_mod  # noqa: PLC0415
    from nexus.lua_runtime.errors import ScriptError  # noqa: PLC0415

    h = hd.parse(SOURCE, name="ci_red_notice")
    ok = {"sha": SHA, "refresh": False, "reason": "earliest_suspect", "me": ME}
    assert "owner_alias" not in header_mod.prepare_args(h, ok, cwd=None), "optional: omitted, accepted"
    for required in ("sha", "refresh", "reason", "me"):
        args = {k: v for k, v in ok.items() if k != required}
        with pytest.raises(ScriptError) as err:
            header_mod.prepare_args(h, args, cwd=None)
        assert err.value.kind == "args", required


# -- the aura ---------------------------------------------------------------------


def _template():
    return load_aura("ci_develop_red", cwd=None)


def _shipped():
    """The shipped template as push-train's one base expands it: the aura a session serving nexus-develop loads."""
    [one], _ = expand_template(_template(), ("nexus-develop",), group="push-train")
    return one


def test_the_shipped_aura_is_a_template_gated_on_push_train_and_its_nexus_expansion_triggers_on_the_owner_post() -> None:
    template = _template()
    assert template.is_template and template.subspace == "board/ci/{serves}--owner" and template.load_group is None
    aura = _shipped()
    assert (aura.name, aura.load_group, aura.threads) == ("ci_develop_red@nexus-develop", "push-train", "develop CI red (nexus-develop)")
    assert aura.subspace == OWNER_SUBSPACE
    assert dict(aura.match) == {"kind": "owner", "from": "engine"}
    assert dict(aura.clear_match or ()) == {"kind": "clear", "from": "engine"}
    assert aura.procedure == "ci_red_notice"
    assert aura.channel == "wake"


def test_the_shipped_aura_maps_the_owner_row_and_the_session_identity() -> None:
    aura = _shipped()
    assert {name: (spec.kind, spec.value) for name, spec in aura.args} == {
        "sha": ("dims", "sha"),
        "branch": ("literal", "develop"),
        "owner": ("body", "owner"),
        "owner_alias": ("body", "owner_alias"),
        "next": ("body", "next"),
        "next_alias": ("body", "next_alias"),
        "refresh": ("body", "refresh"),
        "reason": ("body", "reason"),
        "stalled": ("body", "stalled"),
        "stalled_owner": ("body", "stalled_owner"),
        "stalled_since": ("body", "stalled_since"),
        "stalled_sign": ("body", "stalled_sign"),
        "open_minutes": ("body", "open_minutes"),
        "previous_owner": ("body", "previous_owner"),
        "me": ("self", "identity"),
    }


def test_the_shipped_aura_file_is_the_parse_of_its_own_text() -> None:
    shipped = (ROOT / "ci_develop_red.aura.toml").read_text(encoding="utf-8")
    assert _template() == parse_aura(shipped.encode(), name="ci_develop_red")


def test_the_shipped_aura_passes_every_check_that_needs_no_engine(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv(library.REPO_ROOT_ENV, raising=False)
    entries = [e for e in enumerate_auras(cwd=None) if e.name == "ci_develop_red"]
    assert [e.root for e in entries] == ["shipped"], entries  # non-vacuity: the real package root
    [check] = check_auras(entries, cwd=None, templates=None)
    assert not check.findings, check.findings
    assert not check.warnings, check.warnings
    assert {s.check for s in check.skipped} == {"dictionary", "serving_group"}


def test_the_shipped_aura_is_not_warned_about_when_push_train_serves_nexus_develop(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.delenv(library.REPO_ROOT_ENV, raising=False)
    entries = [e for e in enumerate_auras(cwd=None) if e.name == "ci_develop_red"]
    asked: list[str] = []

    def read(name: str) -> GroupRecord | None:
        asked.append(name)
        return GroupRecord(name=name, serves=("nexus-develop",))

    [check] = check_auras(entries, cwd=None, templates=None, read_group=read, member_groups=lambda: ["push-train"])
    assert asked == ["push-train"]  # non-vacuity: the serving-group check ran against the shipped aura
    assert not check.findings and not check.warnings, (check.findings, check.warnings)
    assert {s.check for s in check.skipped} == {"dictionary"}


def test_the_shipped_aura_is_warned_about_when_the_group_serves_another_topic() -> None:
    [warning], skipped = check_serving_group(_shipped(), lambda name: GroupRecord(name=name, serves=("other-main",)))
    assert (warning.check, warning.key) == ("serving_group", "load.group") and not skipped


#: The shape of board-ci.yaml as the engine carries it once RDR-252 Step 1 (nexus-igea9.1) is in: the dimensions
#: and the names of the doc.body fields (only the names are checked here). ``tests/auras/test_registry_engine.py``
#: reads the same from a live engine.
OWNER_TEMPLATE = {
    "name": "board/ci/<topic>",
    "dimensions": {"from": {}, "kind": {}, "sha": {}, "outcome": {}},
    "doc": {
        "body": {
            name: "..."
            for name in (
                "state", "conclusion", "workflow", "job", "sha", "run", "n", "base", "owner", "owner_alias", "next",
                "next_alias", "reason", "suspects", "episode_at", "refresh", "handoffs", "runs", "attempt", "url",
                "stalled", "stalled_owner", "stalled_since", "stalled_sign", "open_minutes", "previous_owner",
            )
        }
    },
}


def test_the_shipped_aura_passes_the_dictionary_checks_of_a_template_with_the_owner_doc() -> None:
    from nexus.auras.check import check_dictionary  # noqa: PLC0415

    findings, skipped = check_dictionary(_shipped(), [OWNER_TEMPLATE])
    assert (findings, skipped) == ([], [])


def test_the_shipped_aura_is_refused_by_a_template_without_the_owner_fields() -> None:
    """Bead note D: against an engine that predates the owner doc block the body sources are unknown, so the
    conexus release that ships this aura pins the engine release that carries it."""
    from nexus.auras.check import check_dictionary  # noqa: PLC0415

    old_body = {k: "..." for k in ("state", "conclusion", "workflow", "job", "sha", "run", "n", "runs", "attempt", "url")}
    findings, _ = check_dictionary(_shipped(), [{**OWNER_TEMPLATE, "doc": {"body": old_body}}])
    assert {f.key for f in findings} == {
        "args.owner", "args.owner_alias", "args.next", "args.next_alias", "args.refresh", "args.reason",
        "args.stalled", "args.stalled_owner", "args.stalled_since", "args.stalled_sign",
        "args.open_minutes", "args.previous_owner"}


# -- who may post the owner row (RDR-252 "The stamper": the engine stamps ``from=engine``; round 1 B3) ----------------


class _Recorder:
    """A runtime's two outputs, recorded: the procedure runs (a fire) and the block writes and removals."""

    def __init__(self) -> None:
        self.runs: list[dict] = []
        self.sets: list[str] = []
        self.removes: list[str] = []

    def run(self, name, args=None, limits=None):
        self.runs.append(dict(args or {}))
        return {"show": True, "text": "red", "wake": False}

    def set_block(self, aura: str, threads_value: str, text: str) -> bool:
        self.sets.append(text)
        return True

    def remove_block(self, aura: str, threads_value: str) -> bool:
        self.removes.append(aura)
        return True


def _offer(row_dims: dict[str, str], *, sender_session: str | None = None, stamp_known: bool = True) -> _Recorder:
    """Offer one board row. The default is the engine's own write: the stamp rendered and null (``sender_session``
    is the session the engine stamped on a client's write, so a row the engine wrote itself carries none)."""
    from nexus.auras.runtime import AuraRuntime  # noqa: PLC0415
    from nexus.db.t2.records import TupleRow  # noqa: PLC0415

    aura = _shipped()
    rec = _Recorder()
    rt = AuraRuntime([aura], writer=rec, run=rec.run, identity=ME)
    body = '{"owner": "i7", "next": null, "reason": "earliest_suspect", "refresh": false}'
    rt.offer(aura.subspace, [TupleRow(
        id="t1", subspace=aura.subspace, template="", keys={}, dims=row_dims, body=body, claim_state=None, claimant=None,
        lease_until=None, attempts=0, consumed_at=None, consumed_by=None, expires_at=None, created_at=None,
        sender_session=sender_session, sender_session_known=stamp_known)])
    assert rt.wait_idle(30)
    return rec


def test_an_owner_row_from_the_engine_fires() -> None:
    """The control: the row the stamper posts fires, so the two refusals below are not a runtime that fires nothing."""
    rec = _offer({"kind": "owner", "sha": SHA, "from": "engine"})
    assert len(rec.runs) == 1 and rec.runs[0]["me"] == ME and rec.sets == ["red"]


@pytest.mark.parametrize("writer", ["", "tamsin", "i9", "Engine"])
def test_an_owner_row_from_any_other_writer_fires_nothing(writer: str) -> None:
    dims = {"kind": "owner", "sha": SHA, **({"from": writer} if writer else {})}
    rec = _offer(dims)
    assert rec.runs == [] and rec.sets == [] and rec.removes == []


SESSION = "5f0c1a2e-8b34-4d6f-9a10-0c2b7e4d9a31"  # a session id the engine stamped on a client's write


@pytest.mark.parametrize("kind", ["owner", "clear"])
@pytest.mark.parametrize(
    ("sender_session", "stamp_known"),
    [(SESSION, True), (None, False), ("", False), ("", True)],
    ids=["client-stamped", "stamp-not-rendered", "blank-and-not-rendered", "blank-stamp-rendered"],
)
def test_a_row_claiming_from_engine_counts_only_when_the_engine_wrote_it(
    kind: str, sender_session: str | None, stamp_known: bool,
) -> None:
    """RDR-252 critique S3: ``from`` is a claim the writer typed, so a client can post ``from=engine``. The engine's
    own rows carry no ``sender_session``; a row a client posted carries the session the engine stamped, and an engine
    that does not render the stamp cannot say. Neither fires the aura or clears its block."""
    rec = _offer({"kind": kind, "sha": SHA, "from": "engine"}, sender_session=sender_session, stamp_known=stamp_known)
    assert rec.runs == [] and rec.sets == [] and rec.removes == []


@pytest.fixture
def debug_logging():
    """structlog filters below the configured level before capture_logs sees an event; the line under test is debug."""
    import logging  # noqa: PLC0415

    import structlog  # noqa: PLC0415

    saved = structlog.get_config()
    structlog.configure(wrapper_class=structlog.make_filtering_bound_logger(logging.DEBUG))
    yield
    structlog.configure(**saved)


def test_a_from_engine_row_ignored_for_its_stamp_is_logged_once_per_row(debug_logging) -> None:
    """RDR-252 critique S3b: the gate must not go silent. If the engine's own rows ever carried a stamp, the aura would
    stop firing with no trace; one debug line per (aura, row) names the refusal."""
    import structlog  # noqa: PLC0415

    with structlog.testing.capture_logs() as logs:
        _offer({"kind": "owner", "sha": SHA, "from": "engine"}, sender_session=SESSION)
    refused = [e for e in logs if e["event"] == "aura_from_engine_ignored"]
    assert len(refused) == 1
    assert refused[0]["log_level"] == "debug" and refused[0]["aura"] == "ci_develop_red@nexus-develop" and refused[0]["tuple_id"] == "t1"
    assert SESSION not in str(refused[0]), "the line says the row was stamped, not by which session"
    with structlog.testing.capture_logs() as logs:
        _offer({"kind": "owner", "sha": SHA, "from": "engine"})  # the engine's own row
        _offer({"kind": "owner", "sha": SHA, "from": "tamsin"}, sender_session=SESSION)  # not claiming to be the engine
    assert [e for e in logs if e["event"] == "aura_from_engine_ignored"] == []


def test_a_runtime_logs_a_refused_from_engine_row_once_however_often_it_is_offered(debug_logging) -> None:
    import structlog  # noqa: PLC0415
    from nexus.auras.runtime import AuraRuntime  # noqa: PLC0415
    from nexus.db.t2.records import TupleRow  # noqa: PLC0415

    aura = _shipped()
    rec = _Recorder()
    rt = AuraRuntime([aura], writer=rec, run=rec.run, identity=ME)
    row = TupleRow(
        id="t1", subspace=aura.subspace, template="", keys={}, dims={"kind": "owner", "sha": SHA, "from": "engine"},
        body="{}", claim_state=None, claimant=None, lease_until=None, attempts=0, consumed_at=None, consumed_by=None,
        expires_at=None, created_at=None, sender_session=SESSION, sender_session_known=True)
    with structlog.testing.capture_logs() as logs:
        for _ in range(3):
            rt.offer(aura.subspace, [row])
        assert rt.wait_idle(30)
    assert [e["event"] for e in logs].count("aura_from_engine_ignored") == 1
    assert rec.runs == []


def test_a_clear_row_from_the_engine_removes_the_block() -> None:
    rec = _offer({"kind": "clear", "sha": SHA, "from": "engine"})
    assert rec.removes == ["ci_develop_red@nexus-develop"] and rec.runs == []


@pytest.mark.parametrize("writer", ["", "tamsin", "i9"])
def test_a_clear_row_from_any_other_writer_clears_nothing(writer: str) -> None:
    dims = {"kind": "clear", "sha": SHA, **({"from": writer} if writer else {})}
    rec = _offer(dims)
    assert rec.removes == [] and rec.runs == []


def test_with_no_config_the_aura_is_off_by_default(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("NEXUS_CONFIG_DIR", str(tmp_path / "config"))
    repo = tmp_path / "repo"
    repo.mkdir()
    got = resolve(["ci_develop_red"], repo_root=repo, env={})["ci_develop_red"]
    assert (got.on, got.level) == (False, "default")


def test_this_repos_own_config_leaves_the_aura_off(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """The repo-level layer as this checkout ships it (whatever .nexus.yml holds, or none), with an empty global
    layer: the aura stays off and the default decides. A shipped ``auras:`` key turning it on fails this."""
    monkeypatch.setenv("NEXUS_CONFIG_DIR", str(tmp_path / "config"))
    repo_root = Path(__file__).resolve().parents[2]
    assert (repo_root / "src" / "nexus" / "procedures" / "ci_develop_red.aura.toml").is_file()  # the real checkout
    got = resolve(["ci_develop_red"], repo_root=repo_root, env={})["ci_develop_red"]
    assert (got.on, got.level) == (False, "default")
