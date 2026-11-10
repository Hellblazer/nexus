# SPDX-License-Identifier: AGPL-3.0-or-later
"""transcripts.sessions: the attachment lines no selected field reads are not
decoded, and the records are unchanged (nexus-qo7ie.39, after nexus-qo7ie.37).
User and assistant lines are always decoded. The helpers and the skip-on/off
comparison are the verb tests'."""

from __future__ import annotations

from pathlib import Path

import pytest

from nexus.lua_runtime import transcripts_read as tr
from nexus.lua_runtime import verbs_transcripts as vt
from tests.lua_runtime.test_verbs_transcripts import (  # noqa: F401  (fixtures)
    BIG,
    PROJECT,
    PROMPT,
    QUERIES,
    RESULT,
    _ask,
    _asst_msg,
    _c,
    _field_sets,
    _h,
    _line,
    _stamped,
    _tool_use,
    _tr_block,
    _write_bytes,
    decoded,
    root,
)

# ---- attachment lines no selected field reads ----------------------------------
#
# Their stamp and ids follow the nested attachment object, so they are read from
# the members after the last nested value (the flat tail), which the text
# proves are top-level. A line the selection reads (an attachment while
# instruction_loads or deferred_tools is selected) is decoded as before, and a
# user or assistant line is decoded whatever is selected.

SID_L, SID_M, SID_N, SID_O = (f"{c * 8}-2222-3333-4444-555555555555" for c in "lmno")
DECOY_HI = "2026-11-03T23:59:00.000Z"


def _att(
    hour: int,
    sec: int,
    sid: str,
    nested: dict,
    *,
    side: bool = False,
    head=(),
    tail=(),
    cwd: str = "/r",
) -> str:
    """An attachment line in the harness's key order: ids before the nested
    object, type, uuid and stamp after it, then flat members to the end."""
    return _c(
        ("parentUuid", None),
        ("isSidechain", side),
        *head,
        ("attachment", nested),
        ("type", "attachment"),
        ("uuid", f"u-{hour}-{sec}"),
        ("timestamp", _h(hour, sec)),
        *tail,
        ("userType", "external"),
        ("cwd", cwd),
        ("sessionId", sid),
        ("version", "2.1.0"),
        ("gitBranch", "main"),
    )


def _q39_root(root: Path, pad: int) -> None:
    x = "x" * pad
    hook = {"type": "hook_success", "pad": x}
    user = {"role": "user", "content": PROMPT + x}
    cmd = {"role": "user", "content": "<command-name>/q39:cmd</command-name>" + x}
    result = {
        "role": "user",
        "content": [_tr_block("l1", [{"type": "text", "text": RESULT + x}])],
    }

    def dup_head_stamp(line: str, sec: int) -> str:  # a second timestamp, in the head
        return line.replace(
            '"isSidechain":false,',
            f'"isSidechain":false,"timestamp":"{_h(19, sec)}",',
            1,
        )

    l = [  # noqa: E741
        # the first stamped line is a skippable attachment; its sessionId is not the stem
        _att(19, 0, "FOREIGN-L", hook),
        _line("user", _h(19, 1), "OTHER-L", user),
        _line(
            "assistant",
            _h(19, 2),
            "OTHER-L",
            _asst_msg("l1", [_tool_use("l1", "Bash", {"command": "ls"})]),
        ),
        # a sidechain attachment replayed into a main file: ignored, stamp and all
        _att(19, 50, "OTHER-L", hook, side=True),
        # a } inside a tail string: the tail cannot be read, so it is decoded
        _att(19, 4, "OTHER-L", hook, cwd="/a}b"),
        # non-ASCII in the nested object and in the tail
        _att(19, 5, "OTHER-L", {**hook, "note": "é中"}, cwd="/é"),
        # a stamp in the head as well as the tail: the last wins, so it is decoded
        dup_head_stamp(_att(19, 7, "OTHER-L", hook), 6),
        # a decoy stamp and session inside the nested object, a later decoy
        _att(
            19,
            8,
            "OTHER-L",
            {**hook, "timestamp": DECOY_HI, "sessionId": "DECOY", "isSidechain": True},
        ),
        # no top-level stamp, a nested one
        _c(
            ("parentUuid", None),
            ("isSidechain", False),
            ("attachment", {"timestamp": DECOY_HI}),
            ("type", "attachment"),
            ("sessionId", "OTHER-L"),
        ),
        # a stamp then a nested array: not provable from the tail
        _att(19, 9, "OTHER-L", hook, tail=(("rendered", [{"a": 1}]),)),
        # an escaped sessionId, a value past the 512-character bound
        _att(19, 10, "OTHER-L", hook).replace('"OTHER-L"', '"OTHER\\u002dL"'),
        _att(19, 11, "OTHER-L", hook, cwd="c" * 600),
        # a user prompt the harness wrote, a tool result with an object after the stamp
        _line("user", _h(19, 12), "OTHER-L", cmd),
        _line(
            "user",
            _h(19, 13),
            "OTHER-L",
            result,
            tail=(("toolUseResult", {"stdout": RESULT, "nested": {"k": [1]}}),),
        ),
        # a tool result with only flat members after the stamp: needs the tail
        _line("user", _h(19, 14), "OTHER-L", result),
        # an attachment with no top-level type
        _c(
            ("parentUuid", None),
            ("isSidechain", False),
            ("attachment", {"type": "hook_success", "pad": x}),
            ("uuid", "u"),
            ("timestamp", _h(19, 15)),
            ("sessionId", "OTHER-L"),
        ),
        # the type is in the head, the stamp after the nested value, and a nested
        # object ends the line: no flat tail, so it is decoded
        _c(
            ("parentUuid", None),
            ("isSidechain", False),
            ("type", "attachment"),
            ("attachment", hook),
            ("timestamp", _h(19, 16)),
            ("extra", {"a": 1}),
        ),
        # an instruction file and a deferred record: read when selected
        _att(
            19,
            17,
            "OTHER-L",
            {
                "type": "instructions",
                "files": [
                    {"path": "/r/CLAUDE.md", "type": "User", "content": "abc" + x}
                ],
            },
        ),
        _att(
            19,
            18,
            "OTHER-L",
            {
                "type": "deferred_tools_delta",
                "addedNames": ["mcp__q39__tool"],
                "pad": x,
            },
        ),
        _att(19, 19, "OTHER-L", hook),
    ]
    # an attachment carrying a lone 0xff: json.loads drops the line
    bad = (
        _att(19, 40, "OTHER-L", {"type": "hook_success", "pad": x + "é"})
        .encode()
        .replace(b"\xc3\xa9", b"\xff", 1)
    )
    # a torn last attachment (no newline): decoded, and dropped
    torn = _att(19, 41, "OTHER-L", hook).encode()[:-9]
    pj = root / PROJECT
    enc = lambda ls: [ln if isinstance(ln, bytes) else ln.encode() for ln in ls]  # noqa: E731
    _write_bytes(pj / f"{SID_L}.jsonl", enc([*l, bad, torn]), final_newline=False)
    # a whole last attachment with no newline
    m = [_att(20, 0, "FOREIGN-M", hook), _att(20, 9, "FOREIGN-M", hook)]
    _write_bytes(pj / f"{SID_M}.jsonl", enc(m), final_newline=False)
    # a subagent: its agentId is in the head of an attachment; a later one differs
    n = [
        _att(21, 0, SID_N, hook, head=(("agentId", "AGN"),)),
        _att(21, 1, SID_N, hook, head=(("agentId", "AGZ"),)),
        _line("user", _h(21, 2), SID_N, cmd, side=True),
        _line("user", _h(21, 3), SID_N, result, side=True),
    ]
    _write_bytes(pj / SID_N / "subagents" / "agent-FN.jsonl", enc(n))
    # a subagent whose first attachment quotes an agentId only inside its nested
    # object (decoded: it may be top-level), and whose next has it in the tail
    o = [
        _att(22, 0, SID_O, {**hook, "agentId": "AGNEST"}),
        _att(22, 1, SID_O, hook, tail=(("agentId", "AGT"),)),
    ]
    _write_bytes(pj / SID_O / "subagents" / "agent-FO.jsonl", enc(o))


@pytest.mark.parametrize(("pad", "floor"), [(0, 0), (BIG, None)])
def test_skipping_attachment_lines_changes_no_record(
    root: Path, monkeypatch: pytest.MonkeyPatch, pad: int, floor
) -> None:
    _q39_root(root, pad)
    sets = _field_sets() + [
        ("tool_calls.name", None),
        ("start,end,tool_call_count", None),
        ("tool_calls.name,tool_calls.result_time", None),
        ("tool_calls.name,slash_commands", None),
        ("instruction_loads", None),
        ("deferred_tools,start", None),
    ]
    for fields, bodies in sets:
        want = _ask(monkeypatch, False, fields, bodies, floor)
        got = _ask(monkeypatch, True, fields, bodies, floor)
        assert got == want, (fields, bodies)
    full = {
        (r["session_id"], r["agent_id"]): r
        for r in _ask(monkeypatch, False, None, None, floor)
    }
    # not vacuous: the first attachment's ids and the last stamps come back as read
    # from lines that never lead the file
    l = full[("FOREIGN-L", None)]
    assert l["start"] == _h(19, 0)
    assert l["end"] == _h(19, 19)  # the bad and the torn line after it stamp nothing
    assert [c["name"] for c in l["slash_commands"]] == ["q39:cmd"]
    assert l["instruction_loads"][0]["path"] == "/r/CLAUDE.md"
    assert l["deferred_tools"] == ["mcp__q39__tool"]
    assert l["tool_calls"][0]["name"] == "Bash"
    assert full[("FOREIGN-M", None)]["end"] == _h(20, 9)
    assert (SID_N, "AGN") in full and (SID_O, "AGT") in full


def test_at_the_real_floor_the_attachment_lines_nobody_reads_are_not_decoded(
    root: Path, monkeypatch: pytest.MonkeyPatch, decoded: list[str]
) -> None:
    _q39_root(root, BIG)
    tools = ",".join(QUERIES["tools"][0])
    _ask(monkeypatch, True, tools, None, None)
    # skipped: attachments whose stamp and ids are in the flat tail
    for hour, sec in [(19, 0), (19, 5), (19, 8), (19, 17), (19, 18), (19, 19)]:
        assert not _stamped(decoded, _h(hour, sec)), (hour, sec)
    for hour, sec in [(20, 0), (21, 0)]:
        assert not _stamped(decoded, _h(hour, sec)), (hour, sec)
    assert not _stamped(decoded, _h(22, 1))  # an agentId in the tail
    # decoded: the assistant and every user line (a prompt, a command, a tool
    # result with or without an object after the stamp, a subagent's), a } in
    # the tail, a stamp in head and tail, a stamp before a nested array, an
    # escaped id, a value past the bound, an attachment with no top-level type,
    # none with a flat tail, a bad line and a torn one, a whole last line with no
    # newline, a nested agentId
    for hour, sec in [(19, 1), (19, 12), (19, 13), (19, 14), (21, 2), (21, 3)]:
        assert _stamped(decoded, _h(hour, sec)), (hour, sec)
    for hour, sec in [(19, 2), (19, 4), (19, 7), (19, 9), (19, 10), (19, 11)]:
        assert _stamped(decoded, _h(hour, sec)), (hour, sec)
    for hour, sec in [(19, 15), (19, 16), (19, 40), (19, 41), (20, 9)]:
        assert _stamped(decoded, _h(hour, sec)), (hour, sec)
    assert _stamped(decoded, _h(22, 0))
    assert any(DECOY_HI in d for d in decoded)  # the nested-decoy line


@pytest.mark.parametrize(
    ("fields", "decoded_hour_secs"),
    [
        # instruction_loads and deferred_tools read attachments
        ("instruction_loads,tool_call_count,start", [(19, 0), (19, 17), (19, 18)]),
        ("deferred_tools,start", [(19, 0), (19, 17), (19, 18)]),
        ("deferred_tools_dropped,start", [(19, 0), (19, 17), (19, 18)]),
        # a result needs the user lines that carry one; a command needs a prompt
        ("tool_calls.name,tool_calls.result_time,start", [(19, 1), (19, 14)]),
        ("tool_calls.name,tool_calls.is_error,start", [(19, 1), (19, 14)]),
        ("slash_commands,start", [(19, 1), (19, 12), (19, 14)]),
        # no field reads a user line, and it is decoded all the same
        ("tool_calls.name,start", [(19, 1), (19, 12), (19, 14), (21, 2)]),
        ("start,end", [(19, 1), (19, 12), (19, 14), (21, 2)]),
    ],
)
def test_a_line_a_selected_field_reads_is_decoded(
    root: Path,
    monkeypatch: pytest.MonkeyPatch,
    decoded: list[str],
    fields: str,
    decoded_hour_secs: list[tuple[int, int]],
) -> None:
    _q39_root(root, BIG)
    _ask(monkeypatch, True, fields, None, None)
    for hour, sec in decoded_hour_secs:
        assert _stamped(decoded, _h(hour, sec)), (fields, hour, sec)


def test_a_skip_that_loses_what_the_tail_carries_is_caught_by_the_comparison(
    root: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Mutation checks: a stand-in without its stamp, session or agent differs."""
    _q39_root(root, 0)
    fields = ",".join(QUERIES["tools"][0]) + ",agent_id"
    want = _ask(monkeypatch, False, fields)
    real = tr._inert
    for lose in ("timestamp", "sessionId", "agentId"):

        def lossy(raw, s, lose=lose):
            rec = real(raw, s)
            if rec is not None:
                rec.pop(lose, None)
            return rec

        monkeypatch.setattr(tr, "_inert", lossy)
        assert _ask(monkeypatch, True, fields) != want, lose
    monkeypatch.setattr(tr, "_inert", real)
    assert _ask(monkeypatch, True, fields) == want


def test_an_assistant_line_over_the_floor_with_a_full_opening_run_is_decoded(
    root: Path, monkeypatch: pytest.MonkeyPatch, decoded: list[str]
) -> None:
    """The kind check comes before the tail: type, stamp, session and sidechain
    in the opening run leave nothing to look up, so only the kind keeps an
    assistant line from standing in as a skipped one."""
    sid = f"{'p' * 8}-2222-3333-4444-555555555555"
    asst = _c(
        ("type", "assistant"),
        ("timestamp", _h(23, 1)),
        ("sessionId", sid),
        ("isSidechain", False),
        (
            "message",
            _asst_msg(
                "p1",
                [
                    _tool_use("p1", "Bash", {"command": "ls"}),
                    {"type": "text", "text": "x" * BIG},
                ],
            ),
        ),
        ("uuid", "u-p1"),
    )
    assert len(asst) >= tr._PREPARSE_MIN
    _write_bytes(root / PROJECT / f"{sid}.jsonl", [asst.encode()])
    [rec] = _ask(monkeypatch, True, "session_id,tool_calls.name", None, None)
    assert rec["tool_calls"] == [{"name": "Bash"}]
    assert _stamped(decoded, _h(23, 1))


def test_a_crash_resumed_attachment_line_keeps_the_resumed_records_values(
    root: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The accepted residual of nexus-qo7ie.37 (ask baton/q37-residual), for an
    attachment head: a session that crashes mid-write and resumes appends its
    next record to the torn line. The decode drops the glued line. The skip finds
    the resumed record's type, stamp and ids in the tail and keeps a stand-in
    with them, so the transcript starts at the resumed record's stamp and takes
    its session id. The torn record is lost either way."""
    torn = '{"parentUuid":null,"isSidechain":false,"attachment":{"type":"hook_success","pad":"xx'
    resumed = _att(23, 5, "RESUMED-ID", {"type": "hook_success", "pad": "y"})
    glued = (torn + resumed).encode()
    after = _line(
        "assistant",
        _h(23, 30),
        "AFTER-ID",
        _asst_msg("g1", [_tool_use("g1", "Bash", {"command": "ls"})]),
    ).encode()
    sid = f"{'g' * 8}-2222-3333-4444-555555555555"
    _write_bytes(root / PROJECT / f"{sid}.jsonl", [glued, after])
    fields = "session_id,start,end"
    [dropped] = _ask(monkeypatch, False, fields, None, 0)
    assert (dropped["session_id"], dropped["start"]) == ("AFTER-ID", _h(23, 30))
    [kept] = _ask(monkeypatch, True, fields, None, 0)
    assert (kept["session_id"], kept["start"]) == ("RESUMED-ID", _h(23, 5))
    assert kept["end"] == dropped["end"] == _h(23, 30)
