# SPDX-License-Identifier: AGPL-3.0-or-later
"""transcripts.sessions against fixture transcripts (RDR-242 Technical
Design 10; RDR-240 Technical Design 4 and 5; gate r3 S2 and S3; RDR-240
gate r2 Significant 5; nexus-mdbds.8)."""

from __future__ import annotations

import json
import logging
import os
import subprocess
import time
from pathlib import Path

import pytest

from nexus.lua_runtime import verb_types
from nexus.lua_runtime import verbs_transcripts as vt
from nexus.lua_runtime.verbs import VERB_TABLE
from nexus.lua_runtime.verb_types import (
    ParamSpec,
    VerbArgError,
    VerbContext,
    VerbError,
    VerbTimeout,
    encode_result,
)

PROJECT = "-Users-someone-git-proj"
SID = "11111111-2222-3333-4444-555555555555"
SENT = "BODY-SENTINEL"
BODY = f"{SENT}-assistant-said-this"
RESULT = f"{SENT}-tool-result-content"
PROMPT = f"{SENT}-user-typed-this"
THINK = f"{SENT}-thinking"
SIDE = f"{SENT}-sidechain"
CTX = VerbContext(repo_root=None)


# ---- fixture builders: records in Claude Code's transcript shape ----------


def _base(ts, sid: str, *, side: bool = False) -> dict:
    return {
        "timestamp": ts,
        "sessionId": sid,
        "isSidechain": side,
        "uuid": f"{ts}{sid}",
    }


def _assistant(
    ts, sid, content, *, msg_id="m1", model="claude-test-1", usage=None, side=False
):
    r = _base(ts, sid, side=side)
    r["type"] = "assistant"
    r["message"] = {
        "id": msg_id,
        "model": model,
        "role": "assistant",
        "content": content,
        "usage": usage
        or {
            "input_tokens": 10,
            "output_tokens": 5,
            "cache_read_input_tokens": 100,
            "cache_creation_input_tokens": 20,
        },
    }
    return r


def _tool_use(tid, name, inp):
    return {"type": "tool_use", "id": tid, "name": name, "input": inp}


def _tool_result(ts, sid, tid, *, is_error: bool | None = False, side=False):
    r = _base(ts, sid, side=side)
    r["type"] = "user"
    block = {
        "type": "tool_result",
        "tool_use_id": tid,
        "content": [{"type": "text", "text": RESULT}],
    }
    if is_error is not None:
        block["is_error"] = is_error
    r["message"] = {"role": "user", "content": [block]}
    r["toolUseResult"] = {"stdout": RESULT}
    return r


def _user(ts, sid, text=PROMPT):
    r = _base(ts, sid)
    r["type"] = "user"
    r["message"] = {"role": "user", "content": text}
    return r


def _attachment(ts, sid, att):
    r = _base(ts, sid)
    r["type"] = "attachment"
    r["attachment"] = att
    return r


def _main_session(
    start: str = "2026-11-03T10:00:00.000Z", sid: str = SID
) -> list[dict]:
    day = start[:10]
    return [
        {"type": "custom-title", "customTitle": "x", "sessionId": sid},  # no timestamp
        _user(start, sid),
        _attachment(
            f"{day}T10:00:01.000Z",
            sid,
            {
                "type": "deferred_tools_record",
                "entries": [
                    {"name": "WebFetch", "description": RESULT, "defer_loading": True},
                    {"name": "Read", "description": RESULT, "defer_loading": False},
                ],
            },
        ),
        _attachment(
            f"{day}T10:00:02.000Z",
            sid,
            {"type": "deferred_tools_delta", "addedNames": ["mcp__x__y"]},
        ),
        _attachment(
            f"{day}T10:00:03.000Z",
            sid,
            {
                "type": "instructions",
                "files": [
                    {"path": "/r/CLAUDE.md", "type": "Project", "content": RESULT}
                ],
            },
        ),
        _attachment(
            f"{day}T10:00:04.000Z",
            sid,
            {
                "type": "nested_memory",
                "path": "/r/sub/CLAUDE.md",
                "content": {"type": "Local", "content": RESULT + "x"},
            },
        ),
        _assistant(
            f"{day}T10:00:05.000Z",
            sid,
            [{"type": "thinking", "thinking": THINK}, {"type": "text", "text": BODY}],
            msg_id="m1",
        ),
        # A second content block of the same API message repeats its usage.
        _assistant(
            f"{day}T10:00:05.500Z",
            sid,
            [_tool_use("t1", "Bash", {"command": "git status"})],
            msg_id="m1",
        ),
        _tool_result(f"{day}T10:00:06.000Z", sid, "t1", is_error=False),
        _user(
            f"{day}T10:00:06.500Z",
            sid,
            [
                {
                    "type": "text",
                    "text": f"<command-name>/conexus:rdr-gate</command-name> {PROMPT}",
                }
            ],
        ),
        _assistant(
            f"{day}T10:00:07.000Z",
            sid,
            [
                _tool_use(
                    "t2", "mcp__nexus__memory_get", {"project": "p", "title": "a title"}
                )
            ],
            msg_id="m2",
            model="claude-test-2",
            usage={
                "input_tokens": 1,
                "output_tokens": 2,
                "cache_read_input_tokens": 3,
                "cache_creation_input_tokens": 4,
            },
        ),
        _tool_result(f"{day}T10:00:08.000Z", sid, "t2", is_error=True),
        # Subagent traffic replayed into the main file is not the main session's.
        _assistant(
            f"{day}T10:00:09.000Z",
            sid,
            [{"type": "text", "text": SIDE}, _tool_use("t3", "Grep", {"pattern": "x"})],
            msg_id="m3",
            side=True,
        ),
    ]


def _write(path: Path, records: list, *, raw: list[str] | None = None) -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("w", encoding="utf-8", errors="surrogatepass") as fh:
        for r in records:
            fh.write(json.dumps(r) + "\n")
        for line in raw or []:
            fh.write(line + "\n")
    return path


@pytest.fixture
def root(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    cfg = tmp_path / "claude"
    monkeypatch.setenv("CLAUDE_CONFIG_DIR", str(cfg))
    return cfg / "projects"


def _deadline() -> float:
    return time.monotonic() + 60


def _sessions(
    since="2026-11-03",
    until="2026-11-04",
    project=PROJECT,
    bodies=None,
    fields=None,
    ctx=CTX,
):
    return vt.sessions(
        since, until, project, bodies, fields, deadline=_deadline(), ctx=ctx
    )


# ---- the record --------------------------------------------------------


def test_one_record_per_main_session_with_its_fields(root: Path) -> None:
    path = _write(root / PROJECT / f"{SID}.jsonl", _main_session())
    [rec] = _sessions()
    assert rec["session_id"] == SID
    assert rec["main"] is True
    assert rec["agent_id"] is None
    assert rec["project"] == PROJECT
    assert rec["start"] == "2026-11-03T10:00:00.000Z"
    assert rec["end"] == "2026-11-03T10:00:08.000Z"
    assert rec["models"] == ["claude-test-1", "claude-test-2"]
    assert rec["assistant_turns"] == 2
    assert rec["tokens"] == {
        "input": 11,
        "output": 7,
        "cache_read": 103,
        "cache_creation": 24,
    }
    assert rec["first_context_tokens"] == 130
    assert rec["tool_calls"] == [
        {
            "name": "Bash",
            "input": {"command": "git status"},
            "time": "2026-11-03T10:00:05.500Z",
            "result_time": "2026-11-03T10:00:06.000Z",
            "is_error": False,
        },
        {
            "name": "mcp__nexus__memory_get",
            "input": {"project": "p", "title": "a title"},
            "time": "2026-11-03T10:00:07.000Z",
            "result_time": "2026-11-03T10:00:08.000Z",
            "is_error": True,
        },
    ]
    assert rec["instruction_loads"] == [
        {
            "path": "/r/CLAUDE.md",
            "kind": "instructions",
            "type": "Project",
            "size": len(RESULT),
            "time": "2026-11-03T10:00:03.000Z",
        },
        {
            "path": "/r/sub/CLAUDE.md",
            "kind": "nested_memory",
            "type": "Local",
            "size": len(RESULT) + 1,
            "time": "2026-11-03T10:00:04.000Z",
        },
    ]
    assert rec["slash_commands"] == [
        {"name": "conexus:rdr-gate", "time": "2026-11-03T10:00:06.500Z"}
    ]
    assert rec["deferred_tools"] == ["WebFetch", "mcp__x__y"]
    assert rec["deferred_tools_dropped"] == 0
    assert rec["bytes"] == path.stat().st_size
    assert rec["records"] == 13
    assert rec["truncated"] is False
    assert "assistant_text" not in rec


def test_a_subagent_transcript_is_its_own_record(root: Path) -> None:
    _write(root / PROJECT / f"{SID}.jsonl", _main_session())
    first = _assistant(
        "2026-11-03T10:01:00.000Z",
        SID,
        [_tool_use("s1", "Read", {"file_path": "/r/x.py"})],
        msg_id="sm1",
        side=True,
    )
    first["agentId"] = "abc123"
    side = [
        first,
        _tool_result("2026-11-03T10:01:01.000Z", SID, "s1", is_error=None, side=True),
        {
            **_user(
                "2026-11-03T10:01:02.000Z",
                SID,
                "<command-name>/not-the-user</command-name>",
            ),
            "isSidechain": True,
        },
    ]
    _write(root / PROJECT / SID / "subagents" / "agent-abc123.jsonl", side)
    _write(
        root / PROJECT / SID / "subagents" / "workflows" / "w1" / "agent-def456.jsonl",
        [{**side[0], "agentId": None, "timestamp": "2026-11-03T10:02:00.000Z"}],
    )
    recs = _sessions()
    assert [(r["main"], r["agent_id"]) for r in recs] == [
        (True, None),
        (False, "abc123"),
        (False, "def456"),
    ]
    sub = recs[1]
    assert sub["session_id"] == SID
    assert sub["slash_commands"] == []
    assert sub["tool_calls"] == [
        {
            "name": "Read",
            "input": {"file_path": "/r/x.py"},
            "time": "2026-11-03T10:01:00.000Z",
            "result_time": "2026-11-03T10:01:01.000Z",
            "is_error": False,
        }
    ]


def test_a_call_with_no_result_has_null_is_error(root: Path) -> None:
    recs = _main_session()[:8]  # up to the Bash call; its result never came
    recs.append(
        _assistant(
            "2026-11-03T10:00:06.000Z",
            SID,
            [_tool_use("t9", "Bash", {"command": "sleep"})],
            msg_id="m9",
        )
    )
    _write(root / PROJECT / f"{SID}.jsonl", recs)
    [rec] = _sessions()
    assert [c["is_error"] for c in rec["tool_calls"]] == [None, None]
    assert [c["result_time"] for c in rec["tool_calls"]] == [None, None]


def test_a_replayed_tool_call_is_counted_once(root: Path) -> None:
    recs = _main_session()
    recs.insert(8, recs[7])  # the Bash call's record, written twice
    _write(root / PROJECT / f"{SID}.jsonl", recs)
    [rec] = _sessions()
    assert [c["name"] for c in rec["tool_calls"]] == ["Bash", "mcp__nexus__memory_get"]
    assert rec["tool_calls"][0]["is_error"] is False


# ---- bodies ------------------------------------------------------------


def test_no_body_reaches_the_result_without_bodies(root: Path) -> None:
    _write(root / PROJECT / f"{SID}.jsonl", _main_session())
    out = json.dumps(_sessions(bodies=False)) + json.dumps(_sessions(bodies=None))
    assert SENT not in out


def test_bodies_carry_assistant_text_only(root: Path) -> None:
    _write(root / PROJECT / f"{SID}.jsonl", _main_session())
    [rec] = _sessions(bodies=True)
    assert rec["assistant_text"] == [{"time": "2026-11-03T10:00:05.000Z", "text": BODY}]
    out = json.dumps(rec)
    for other in (RESULT, PROMPT, SIDE, THINK):
        assert other not in out


def test_no_log_line_or_output_carries_a_body(
    root: Path, caplog: pytest.LogCaptureFixture, capsys: pytest.CaptureFixture[str]
) -> None:
    _write(
        root / PROJECT / f"{SID}.jsonl",
        _main_session(),
        raw=['{"timestamp": "2026-11-03T11:00:00Z", "x": "' + RESULT],
    )
    caplog.set_level(logging.DEBUG)
    _sessions(bodies=False)
    captured = capsys.readouterr()
    assert SENT not in caplog.text + captured.out + captured.err


# ---- malformed records (review B1, B2, B3) ---------------------------------

_BAD = f"{SENT}-in-a-bad-field"
_HUGE = "x" * (2 * 1024 * 1024)


def _asst(**over):
    r = _assistant(
        "2026-11-03T10:00:30.000Z", SID, [{"type": "text", "text": "t"}], msg_id="bad"
    )
    for k, v in over.items():
        r["message"][k] = v
    return r


_BAD_RECORDS = {
    "token-string": _asst(usage={"input_tokens": _BAD}),
    "token-list": _asst(usage={"output_tokens": [_BAD]}),
    "token-negative": _asst(usage={"input_tokens": -5}),
    "message-id-list": _asst(id=[_BAD]),
    "content-int": _asst(content=5),
    "content-str": _asst(content=_BAD),
    "text-dict": _asst(content=[{"type": "text", "text": {"x": _BAD}}]),
    "tool-name-dict": _asst(content=[_tool_use("q1", {"x": _BAD}, {"k": "v"})]),
    "tool-id-list": _asst(content=[_tool_use([_BAD], "Bash", {"command": "c"})]),
    "result-id-dict": {
        **_base("2026-11-03T10:00:31.000Z", SID),
        "type": "user",
        "message": {"content": [{"type": "tool_result", "tool_use_id": {"x": _BAD}}]},
    },
    "entries-int": _attachment(
        "2026-11-03T10:00:32.000Z", SID, {"type": "deferred_tools_record", "entries": 5}
    ),
    "added-names-str": _attachment(
        "2026-11-03T10:00:32.000Z",
        SID,
        {"type": "deferred_tools_delta", "addedNames": _BAD},
    ),
    "files-int": _attachment(
        "2026-11-03T10:00:32.000Z", SID, {"type": "instructions", "files": 5}
    ),
    "nested-content-list": _attachment(
        "2026-11-03T10:00:32.000Z",
        SID,
        {"type": "nested_memory", "path": 5, "content": [_BAD]},
    ),
    "message-list": {
        **_base("2026-11-03T10:00:33.000Z", SID),
        "type": "assistant",
        "message": [_BAD],
    },
    "timestamp-dict": {
        **_base({"leak": _BAD}, SID),
        "type": "user",
        "message": {"content": [{"type": "tool_result", "tool_use_id": "t1"}]},
    },
}

_BAD_LINES = {
    "deep-nesting": "[" * 100_000,
    "nan-input": '{"timestamp": "2026-11-03T10:00:34Z", "type": "assistant", "message": '
    '{"id": "n1", "content": [{"type": "tool_use", "id": "n1", "name": "N", "input": {"v": NaN}}]}}',
    "not-an-object": '"' + _BAD + '"',
    "truncated-json": '{"timestamp": "2026-11-03T10:00:35Z", "x": "' + _BAD,
}


@pytest.mark.parametrize("bad", sorted(_BAD_RECORDS) + sorted(_BAD_LINES))
def test_a_malformed_record_is_skipped_and_never_leaks(root: Path, bad: str) -> None:
    records = _main_session()
    raw = []
    if bad in _BAD_RECORDS:
        records.insert(8, _BAD_RECORDS[bad])
    else:
        raw.append(_BAD_LINES[bad])
    _write(root / PROJECT / f"{SID}.jsonl", records, raw=raw)
    [rec] = _sessions(bodies=True)
    assert [c["name"] for c in rec["tool_calls"]][:1] == ["Bash"]
    # The bad record may pair with the Bash call first; a non-string
    # timestamp then gives no time, never the value itself.
    assert rec["tool_calls"][0]["result_time"] in (None, "2026-11-03T10:00:06.000Z")
    encode_result([rec])  # valid JSON within the wire rules
    assert _BAD not in json.dumps(rec)
    good = {"input": 11, "output": 7, "cache_read": 103, "cache_creation": 24}
    assert all(isinstance(v, int) and v >= good[k] for k, v in rec["tokens"].items())
    assert all(isinstance(c["time"], (str, type(None))) for c in rec["tool_calls"])


def test_lone_surrogates_are_made_valid(root: Path) -> None:
    recs = _main_session()
    recs.append(
        _assistant(
            "2026-11-03T10:00:10.000Z",
            SID,
            [
                {"type": "text", "text": "a\ud83db"},
                _tool_use("u1", "Bash", {"command": "e\ud83d"}),
            ],
            msg_id="u1",
        )
    )
    _write(root / PROJECT / f"{SID}.jsonl", recs)
    [rec] = _sessions(bodies=True)
    encode_result([rec])
    assert rec["tool_calls"][-1]["input"]["command"] == "e�"
    assert rec["assistant_text"][-1]["text"] == "a�b"


@pytest.mark.parametrize("field", ["sessionId", "model", "timestamp"])
def test_an_oversized_scalar_is_bounded(root: Path, field: str) -> None:
    recs = _main_session()
    later = _assistant(
        "2026-11-03T10:00:10.000Z", SID, [{"type": "text", "text": "t"}], msg_id="h"
    )
    if field == "model":
        later["message"]["model"] = _HUGE
    else:
        later[field] = _HUGE
    recs.append(later)
    _write(root / PROJECT / f"{SID}.jsonl", recs)
    [rec] = _sessions()
    assert len(encode_result(rec)) <= vt.RECORD_CAP_BYTES
    assert rec["end"] == "2026-11-03T10:00:08.000Z" if field == "timestamp" else True


# ---- the 1 MB record bound ------------------------------------------------


def test_a_record_over_1_mb_is_cut_to_fit_and_marked(root: Path) -> None:
    big = "x" * 600_000
    recs = _main_session()
    recs.append(
        _assistant(
            "2026-11-03T10:00:10.000Z",
            SID,
            [_tool_use("b1", "Write", {"file_path": "/r/f", "content": big})],
            msg_id="b1",
        )
    )
    recs.append(
        _assistant(
            "2026-11-03T10:00:11.000Z",
            SID,
            [{"type": "text", "text": big}],
            msg_id="b2",
        )
    )
    _write(root / PROJECT / f"{SID}.jsonl", recs)
    [rec] = _sessions(bodies=True)
    assert rec["truncated"] is True
    assert len(encode_result(rec)) <= vt.RECORD_CAP_BYTES
    names = [c["name"] for c in rec["tool_calls"]]
    assert names == ["Bash", "mcp__nexus__memory_get", "Write"]
    assert rec["tool_calls"][0]["input"] == {"command": "git status"}
    assert 0 < len(rec["tool_calls"][2]["input"]["content"]) < len(big)
    assert rec["tool_calls"][2]["input"]["file_path"] == "/r/f"


def test_a_record_too_many_calls_to_fit_keeps_short_inputs_and_counts_the_drop(
    root: Path,
) -> None:
    recs = _main_session()
    for i in range(20_000):
        recs.append(
            _assistant(
                "2026-11-03T10:00:10.000Z",
                SID,
                [_tool_use(f"z{i}", "Grep", {"pattern": "p" * 40})],
                msg_id=f"z{i}",
            )
        )
    _write(root / PROJECT / f"{SID}.jsonl", recs)
    [rec] = _sessions()
    assert rec["truncated"] is True
    assert len(encode_result(rec)) <= vt.RECORD_CAP_BYTES
    assert rec["tool_calls_dropped"] > 0
    assert len(rec["tool_calls"]) + rec["tool_calls_dropped"] == 20_002
    # Dropping the tail comes before cutting short strings to nothing.
    assert rec["tool_calls"][-1]["input"] == {"pattern": "p" * 40}


def test_an_input_string_is_bounded_as_it_is_read(root: Path) -> None:
    recs = _main_session()
    recs.append(
        _assistant(
            "2026-11-03T10:00:10.000Z",
            SID,
            [_tool_use("w1", "Write", {"content": "y" * (vt.INPUT_STRING_CAP + 10)})],
            msg_id="w1",
        )
    )
    _write(root / PROJECT / f"{SID}.jsonl", recs)
    [rec] = _sessions()
    assert len(rec["tool_calls"][-1]["input"]["content"]) == vt.INPUT_STRING_CAP
    assert rec["truncated"] is True


# ---- the window --------------------------------------------------------


def _start_at(root: Path, start: str, sid: str) -> None:
    _write(root / PROJECT / f"{sid}.jsonl", _main_session(start, sid))


def test_the_window_is_half_open(root: Path) -> None:
    _start_at(root, "2026-11-03T00:00:00.000Z", "on-since")
    _start_at(root, "2026-11-04T00:00:00.000Z", "on-until")
    ids = [r["session_id"] for r in _sessions("2026-11-03", "2026-11-04")]
    assert ids == ["on-since"]


def test_a_session_past_midnight_belongs_to_the_day_it_started(root: Path) -> None:
    recs = _main_session("2026-11-03T23:30:00.000Z", "late")
    recs.append(
        _assistant(
            "2026-11-04T01:00:00.000Z",
            "late",
            [{"type": "text", "text": "t"}],
            msg_id="late2",
        )
    )
    _write(root / PROJECT / "late.jsonl", recs)
    assert [r["session_id"] for r in _sessions("2026-11-03", "2026-11-04")] == ["late"]
    assert _sessions("2026-11-04", "2026-11-05") == []


@pytest.mark.parametrize(
    ("since", "until", "expect"),
    [
        ("2026-11-03T09:00:00Z", "2026-11-03T11:00:00Z", ["s10"]),
        ("2026-11-03T10:00:00+00:00", "2026-11-03T10:00:00.001Z", ["s10"]),
        ("2026-11-03T05:00:00-05:00", "2026-11-03T12:00:00", ["s10"]),
        ("2026-11-03", "2026-11-03T10:00:00Z", []),
        ("2026-11-03", "2026-11-03T10:00:00", []),  # naive is UTC, not local
    ],
)
def test_since_and_until_take_dates_and_timestamps(
    root: Path, monkeypatch: pytest.MonkeyPatch, since, until, expect
) -> None:
    monkeypatch.setenv("TZ", "America/New_York")
    if hasattr(time, "tzset"):
        time.tzset()
    try:
        _start_at(root, "2026-11-03T10:00:00.000Z", "s10")
        assert [r["session_id"] for r in _sessions(since, until)] == expect
    finally:
        monkeypatch.undo()
        if hasattr(time, "tzset"):
            time.tzset()


@pytest.mark.parametrize(
    ("since", "until"), [("2026-11-04", "2026-11-03"), ("2026-11-03", "2026-11-03")]
)
def test_an_empty_window_is_an_empty_list(root: Path, since, until) -> None:
    _start_at(root, "2026-11-03T00:00:00.000Z", "s")
    assert _sessions(since, until) == []


@pytest.mark.parametrize(
    ("since", "until"), [("yesterday", "2026-11-04"), ("2026-11-03", "2026-13-01")]
)
def test_a_bad_window_is_a_verb_error(root: Path, since, until) -> None:
    (root / PROJECT).mkdir(parents=True)
    with pytest.raises(VerbError):
        _sessions(since, until)


def test_a_file_last_written_before_the_window_is_not_opened(
    root: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    path = _write(
        root / PROJECT / "old.jsonl", _main_session("2026-11-03T10:00:00.000Z", "old")
    )
    old = time.mktime((2026, 11, 1, 0, 0, 0, 0, 0, 0))
    os.utime(path, (old, old))
    monkeypatch.setattr(
        vt, "_scan", lambda *a, **k: pytest.fail("opened a file older than since")
    )
    assert _sessions() == []


def test_a_linked_transcript_is_not_followed(root: Path, tmp_path: Path) -> None:
    real = _write(tmp_path / "outside" / "x.jsonl", _main_session(sid="linked"))
    (root / PROJECT).mkdir(parents=True)
    try:
        (root / PROJECT / "linked.jsonl").symlink_to(real)
    except OSError:
        pytest.skip("cannot make a symbolic link here")
    assert _sessions() == []


def test_sessions_come_back_in_start_order_across_projects(root: Path) -> None:
    _write(root / "-b" / "b.jsonl", _main_session("2026-11-03T09:00:00.000Z", "b"))
    _write(root / "-a" / "a.jsonl", _main_session("2026-11-03T11:00:00.000Z", "a"))
    for every in ("all", None):
        recs = _sessions(project=every)
        assert [(r["project"], r["session_id"]) for r in recs] == [
            ("-b", "b"),
            ("-a", "a"),
        ]
    assert [r["session_id"] for r in _sessions(project="-a")] == ["a"]


@pytest.mark.parametrize(
    "project", ["../etc", "a/b", "", ".", "..", "nope", "D:evil", "a\\b"]
)
def test_a_project_that_is_not_one_directory_is_a_verb_error(
    root: Path, project: str
) -> None:
    (root / PROJECT).mkdir(parents=True)
    if project == "D:evil" and os.name != "nt":
        (root / project).mkdir()  # a real directory here; still refused, for Windows
    with pytest.raises(VerbError):
        _sessions(project=project)


def test_no_projects_directory_is_a_verb_error(root: Path) -> None:
    with pytest.raises(VerbError, match="projects"):
        _sessions(project="all")


def test_files_without_a_timestamp_are_skipped(root: Path) -> None:
    _write(root / PROJECT / f"{SID}.jsonl", _main_session())
    _write(
        root / PROJECT / "no-ts.jsonl", [{"type": "custom-title", "sessionId": "no-ts"}]
    )
    assert [r["session_id"] for r in _sessions()] == [SID]


# ---- the deadline (gate r3 S2) -----------------------------------------


def _ticking(monkeypatch: pytest.MonkeyPatch) -> None:
    clock = iter(range(10_000))
    monkeypatch.setattr(vt, "_now", lambda: float(next(clock)))


def test_the_deadline_is_checked_between_transcript_files(
    root: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    for i in range(10):
        _start_at(root, f"2026-11-03T1{i}:00:00.000Z", f"s{i}")
    _ticking(monkeypatch)
    scanned: list[Path] = []
    real = vt._scan

    def counting(path, *a, **k):
        scanned.append(path)
        return real(path, *a, **k)

    monkeypatch.setattr(vt, "_scan", counting)
    with pytest.raises(VerbTimeout):
        vt.sessions("2026-11-03", "2026-11-04", PROJECT, None, deadline=3.0, ctx=CTX)
    assert 0 < len(scanned) < 10


def test_a_passed_deadline_scans_nothing(
    root: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _start_at(root, "2026-11-03T10:00:00.000Z", "s")
    monkeypatch.setattr(
        vt, "_scan", lambda *a, **k: pytest.fail("scanned past the deadline")
    )
    with pytest.raises(VerbTimeout):
        vt.sessions(
            "2026-11-03",
            "2026-11-04",
            PROJECT,
            None,
            deadline=time.monotonic() - 1,
            ctx=CTX,
        )


@pytest.mark.parametrize("filler", ["record", "blank"])
def test_the_deadline_is_checked_inside_a_long_file(
    root: Path, monkeypatch: pytest.MonkeyPatch, filler: str
) -> None:
    n = 3 * vt.LINES_PER_DEADLINE_CHECK
    raw = (
        [json.dumps(_user("2026-11-03T10:30:00.000Z", SID, "u"))] * n
        if filler == "record"
        else [""] * n
    )
    _write(root / PROJECT / f"{SID}.jsonl", _main_session(), raw=raw)
    _ticking(monkeypatch)
    with pytest.raises(VerbTimeout):
        vt.sessions("2026-11-03", "2026-11-04", PROJECT, None, deadline=3.0, ctx=CTX)


def test_the_deadline_is_checked_by_bytes_read(
    root: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(vt, "BYTES_PER_DEADLINE_CHECK", 1000)
    raw = [json.dumps(_user("2026-11-03T10:30:00.000Z", SID, "u" * 900))] * 5
    _write(root / PROJECT / f"{SID}.jsonl", _main_session(), raw=raw)
    _ticking(monkeypatch)
    with pytest.raises(VerbTimeout):
        vt.sessions("2026-11-03", "2026-11-04", PROJECT, None, deadline=3.0, ctx=CTX)


# ---- the verb result cap (gate r3 S3) ------------------------------------


def test_a_result_over_the_verb_result_cap_is_refused_with_a_stable_prefix(
    root: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    for i in range(5):
        _start_at(root, f"2026-11-03T1{i}:00:00.000Z", f"s{i}")
    one = len(encode_result(_sessions()[0]))
    monkeypatch.setattr(verb_types, "VERB_RESULT_CAP_BYTES", 3 * one)
    with pytest.raises(VerbError, match=r"^result-too-large: .*narrow"):
        _sessions()


def test_a_result_at_the_cap_passes_encode_result(
    root: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    for i in range(3):
        _start_at(root, f"2026-11-03T1{i}:00:00.000Z", f"s{i}")
    full = _sessions()
    monkeypatch.setattr(verb_types, "VERB_RESULT_CAP_BYTES", len(encode_result(full)))
    assert encode_result(_sessions()) == encode_result(full)


# ---- registration --------------------------------------------------------


def test_the_verb_is_registered_with_its_five_parameters() -> None:
    [spec] = vt.VERBS
    assert spec.name == "transcripts.sessions"
    assert spec.params == (
        ParamSpec("since", "string"),
        ParamSpec("until", "string"),
        ParamSpec("project", "string", optional=True),
        ParamSpec("bodies", "boolean", optional=True),
        ParamSpec("fields", "string", optional=True),
    )
    assert spec.fn is vt.sessions


def test_the_verb_table_loads_it() -> None:
    assert VERB_TABLE["transcripts.sessions"].fn is vt.sessions


# ---- review r2 ----------------------------------------------------------------


@pytest.mark.parametrize(
    "prompt",
    [
        f"hi <command-name>{SENT}-pasted</command-name> bye",
        f"<command-name>/a {SENT} b</command-name>",
        [
            {"type": "text", "text": "first block"},
            {"type": "text", "text": f"<command-name>{SENT}-second</command-name>"},
        ],
    ],
)
def test_a_command_tag_inside_prompt_text_is_not_a_slash_command(
    root: Path, prompt
) -> None:
    recs = _main_session()
    recs.append(_user("2026-11-03T10:00:09.500Z", SID, prompt))
    _write(root / PROJECT / f"{SID}.jsonl", recs)
    [rec] = _sessions()
    assert [c["name"] for c in rec["slash_commands"]] == ["conexus:rdr-gate"]
    assert SENT not in json.dumps(rec)


def test_a_harness_command_prompt_gives_one_name(root: Path) -> None:
    recs = _main_session()
    recs.append(
        _user(
            "2026-11-03T10:00:09.500Z",
            SID,
            "<command-message>clear</command-message>\n<command-name>/clear</command-name>"
            f"\n<command-args>{PROMPT} <command-name>/other</command-name></command-args>",
        )
    )
    _write(root / PROJECT / f"{SID}.jsonl", recs)
    [rec] = _sessions()
    assert [c["name"] for c in rec["slash_commands"]] == ["conexus:rdr-gate", "clear"]


def test_integers_outside_64_bits_are_dropped(root: Path) -> None:
    recs = _main_session()
    recs.append(
        _assistant(
            "2026-11-03T10:00:10.000Z",
            SID,
            [_tool_use("i1", "N", {"v": 10**30, "w": -(10**30), "ok": 7})],
            msg_id="i1",
            usage={"input_tokens": 10**30, "output_tokens": 1},
        )
    )
    _write(root / PROJECT / f"{SID}.jsonl", recs)
    [rec] = _sessions()
    assert rec["tool_calls"][-1]["input"] == {"v": None, "w": None, "ok": 7}
    assert rec["tokens"]["input"] == 11
    encode_result([rec])


def test_a_linked_subagent_directory_is_not_followed(
    root: Path, tmp_path: Path
) -> None:
    _write(root / PROJECT / f"{SID}.jsonl", _main_session())
    outside = tmp_path / "outside-subagents"
    _write(
        outside / "agent-x.jsonl",
        [_assistant("2026-11-03T10:05:00.000Z", SID, [], msg_id="x")],
    )
    (root / PROJECT / SID).mkdir()
    try:
        (root / PROJECT / SID / "subagents").symlink_to(
            outside, target_is_directory=True
        )
    except OSError:
        pytest.skip("cannot make a symbolic link here")
    assert [r["main"] for r in _sessions()] == [True]


def test_a_linked_projects_root_is_read(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    real = tmp_path / "moved-projects"
    _write(real / PROJECT / f"{SID}.jsonl", _main_session())
    cfg = tmp_path / "config"
    cfg.mkdir()
    try:
        (cfg / "projects").symlink_to(real, target_is_directory=True)
    except OSError:
        pytest.skip("cannot make a symbolic link here")
    monkeypatch.setenv("CLAUDE_CONFIG_DIR", str(cfg))
    assert [r["session_id"] for r in _sessions()] == [SID]


def test_a_line_over_the_line_cap_is_skipped(
    root: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(vt, "LINE_CHUNK", 64)
    monkeypatch.setattr(vt, "MAX_LINE_BYTES", 4096)
    long = json.dumps(
        _assistant(
            "2026-11-03T10:00:10.000Z",
            SID,
            [_tool_use("L1", "Long", {"x": "y" * 10_000})],
            msg_id="L1",
        )
    )
    after = json.dumps(
        _assistant(
            "2026-11-03T10:00:11.000Z", SID, [_tool_use("L2", "After", {})], msg_id="L2"
        )
    )
    _write(root / PROJECT / f"{SID}.jsonl", _main_session(), raw=[long, after])
    [rec] = _sessions()
    names = [c["name"] for c in rec["tool_calls"]]
    assert "Long" not in names
    assert names[-1] == "After"  # the line after it is read whole


def test_the_deadline_is_checked_while_listing_projects(
    root: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    for i in range(10):
        (root / f"-p{i}").mkdir(parents=True)
    _ticking(monkeypatch)
    with pytest.raises(VerbTimeout):
        vt.sessions("2026-11-03", "2026-11-04", None, None, deadline=4.0, ctx=CTX)


def test_a_record_that_cannot_fit_has_its_own_prefix(
    root: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _write(root / PROJECT / f"{SID}.jsonl", _main_session())
    monkeypatch.setattr(vt, "RECORD_CAP_BYTES", 50)
    with pytest.raises(VerbError, match=r"^record-too-large: "):
        _sessions()


def test_deferred_tools_past_the_cap_are_counted(
    root: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(vt, "_MAX_DEFERRED", 1)
    _write(root / PROJECT / f"{SID}.jsonl", _main_session())
    [rec] = _sessions()
    assert rec["deferred_tools"] == ["WebFetch"]
    assert rec["deferred_tools_dropped"] == 1


# ---- fields and project "repo" (RDR-242 A2, nexus-qo7ie.31) ---------------------

ALWAYS = {"session_id", "agent_id", "main", "truncated"}
# The field sets the Lua caller passes, one per query, and the keys each must
# return beyond the four that always come back (marlowe's set, 2026-11-08).
QUERIES = {
    "tools": (
        [
            "tool_calls.name",
            "tool_calls.time",
            "tool_calls.input_bytes",
            "tool_calls.input@Bash",
        ],
        False,
        {"tool_calls"},
    ),
    "deferred_tools": (["deferred_tools"], False, {"deferred_tools"}),
    "memory": (
        [
            "instruction_loads",
            "tool_calls.name",
            "tool_calls.time",
            "tool_calls.input@Read",
            "tool_calls.input@Write",
            "tool_calls.input@Edit",
            "tool_calls.input@MultiEdit",
            "tool_calls.input@*__memory_get",
            "tool_calls.input@*__memory_search",
        ],
        False,
        {"instruction_loads", "tool_calls"},
    ),
    "loads": (["instruction_loads"], False, {"instruction_loads"}),
    "agents": (
        [
            "start",
            "models",
            "tokens",
            "first_context_tokens",
            "assistant_turns",
            "tool_calls.name",
            "tool_calls.time",
            "tool_calls.is_error",
            "tool_calls.input@Agent",
            "tool_calls.input@Task",
        ],
        False,
        {
            "start",
            "models",
            "tokens",
            "first_context_tokens",
            "assistant_turns",
            "tool_calls",
        },
    ),
    "skills": (
        [
            "assistant_turns",
            "slash_commands",
            "tool_calls.name",
            "tool_calls.input@Skill",
            "tool_calls.input@Agent",
            "tool_calls.input@Task",
        ],
        False,
        {"assistant_turns", "slash_commands", "tool_calls"},
    ),
    "violations": (
        ["assistant_text", "tool_calls.name", "tool_calls.input@Bash"],
        True,
        {"assistant_text", "tool_calls"},
    ),
    "procedures": (
        [
            "tool_calls.name",
            "tool_calls.input@*__procedure_run",
            "tool_calls.input@*__memory_put",
            "tool_calls.input@Bash",
        ],
        False,
        {"tool_calls"},
    ),
}
# Every query also asks for tool_call_count: transcripts.lua bounds each page by
# tool calls, loads included (marlowe, 2026-11-08).
QUERIES = {
    name: ([*fields, "tool_call_count"], bodies, keys | {"tool_call_count"})
    for name, (fields, bodies, keys) in QUERIES.items()
}


def _one(fields, bodies=None):
    [rec] = _sessions(bodies=bodies, fields=fields)
    return rec


def test_without_fields_every_field_comes_back_with_the_call_count(root: Path) -> None:
    _write(root / PROJECT / f"{SID}.jsonl", _main_session())
    rec = _one(None)
    assert rec["tool_call_count"] == 2 == len(rec["tool_calls"])
    assert set(rec) == {
        "session_id", "main", "agent_id", "project", "start", "end", "models",
        "assistant_turns", "tokens", "first_context_tokens", "tool_calls",
        "tool_call_count", "instruction_loads", "slash_commands",
        "deferred_tools", "deferred_tools_dropped", "bytes", "records", "truncated",
    }  # fmt: skip
    assert "assistant_text" in _one(None, bodies=True)


def test_the_four_always_returned_fields_come_back_whatever_is_named(
    root: Path,
) -> None:
    _write(root / PROJECT / f"{SID}.jsonl", _main_session())
    rec = _one("start")
    assert set(rec) == ALWAYS | {"start"}
    assert rec["session_id"] == SID and rec["main"] is True
    assert rec["agent_id"] is None and rec["truncated"] is False


def test_names_are_split_on_commas_and_trimmed(root: Path) -> None:
    _write(root / PROJECT / f"{SID}.jsonl", _main_session())
    assert set(_one(" start ,\tmodels  ,tool_calls.name")) == ALWAYS | {
        "start",
        "models",
        "tool_calls",
    }


@pytest.mark.parametrize(
    "fields", ["", " ", "start,", ",start", "start,,models", "start, ,"]
)
def test_an_empty_fields_string_or_name_is_an_arg_error(
    root: Path, fields: str
) -> None:
    _write(root / PROJECT / f"{SID}.jsonl", _main_session())
    with pytest.raises(VerbArgError):
        _sessions(fields=fields)


@pytest.mark.parametrize(
    "fields",
    [
        "bogus",
        "start,bogus",
        "tool_calls.bogus",
        "bogus.name",
        "tokens.input",  # not a list: no sub-fields
        "start.x",
        "tool_calls.input@",
        "instruction_loads.input@Bash",  # @ belongs to tool_calls.input only
        "tool_calls.name@Bash",
        "tool_calls.input@ Bash",  # whitespace inside the tool name
        "Tool_calls",
        "assistant_turns.x",
    ],
)
def test_an_unknown_name_is_an_arg_error_naming_it(root: Path, fields: str) -> None:
    _write(root / PROJECT / f"{SID}.jsonl", _main_session())
    culprit = fields.split(",")[-1].strip()
    with pytest.raises(VerbArgError) as err:
        _sessions(fields=fields, bodies=True)
    assert culprit in str(err.value)


@pytest.mark.parametrize(
    "fields", ["assistant_text", "assistant_text.text", "start,assistant_text.time"]
)
@pytest.mark.parametrize("bodies", [None, False])
def test_assistant_text_needs_bodies(root: Path, fields: str, bodies) -> None:
    _write(root / PROJECT / f"{SID}.jsonl", _main_session())
    with pytest.raises(VerbArgError) as err:
        _sessions(fields=fields, bodies=bodies)
    assert "assistant_text" in str(err.value)


def test_fields_is_validated_even_when_no_transcript_matches(root: Path) -> None:
    (root / PROJECT).mkdir(parents=True)
    with pytest.raises(VerbArgError):
        _sessions(fields="bogus")


def test_tool_call_count_counts_calls_after_dedup_whatever_is_selected(
    root: Path,
) -> None:
    recs = _main_session()
    # The same call replayed: counted once.
    recs.append(
        _assistant(
            "2026-11-03T10:00:09.500Z",
            SID,
            [_tool_use("t1", "Bash", {"command": "git status"})],
            msg_id="m9",
        )
    )
    _write(root / PROJECT / f"{SID}.jsonl", recs)
    full = _one(None)
    assert full["tool_call_count"] == len(full["tool_calls"]) == 2
    only = _one("tool_call_count")
    assert only["tool_call_count"] == 2 and "tool_calls" not in only
    # Counted before any filter: an input@ that matches nothing still counts.
    narrow = _one("tool_call_count,tool_calls.name,tool_calls.input@Nope")
    assert narrow["tool_call_count"] == 2
    assert narrow["tool_calls"] == [
        {"name": "Bash"},
        {"name": "mcp__nexus__memory_get"},
    ]


def test_a_bare_list_name_is_all_its_sub_fields(root: Path) -> None:
    _write(root / PROJECT / f"{SID}.jsonl", _main_session())
    full = _one(None, bodies=True)
    lists = ("tool_calls", "instruction_loads", "slash_commands", "assistant_text")
    sel = _one(",".join(lists), bodies=True)
    for key in lists:
        assert sel[key] == full[key]
    assert set(sel) == ALWAYS | set(lists)


def test_a_named_sub_field_keeps_only_the_named_keys_per_element(
    root: Path,
) -> None:
    _write(root / PROJECT / f"{SID}.jsonl", _main_session())
    full = _one(None, bodies=True)
    sel = _one(
        "tool_calls.name,tool_calls.is_error,instruction_loads.path,"
        "instruction_loads.size,slash_commands.name,assistant_text.text",
        bodies=True,
    )
    assert sel["tool_calls"] == [
        {"name": c["name"], "is_error": c["is_error"]} for c in full["tool_calls"]
    ]
    assert sel["instruction_loads"] == [
        {"path": x["path"], "size": x["size"]} for x in full["instruction_loads"]
    ]
    assert sel["slash_commands"] == [{"name": "conexus:rdr-gate"}]
    assert sel["assistant_text"] == [{"text": BODY}]
    sel = _one(
        "tool_calls.time,tool_calls.result_time,instruction_loads.kind,"
        "instruction_loads.type,instruction_loads.time,slash_commands.time,"
        "assistant_text.time",
        bodies=True,
    )
    assert sel["tool_calls"] == [
        {"time": c["time"], "result_time": c["result_time"]} for c in full["tool_calls"]
    ]
    assert sel["instruction_loads"][0] == {
        "kind": "instructions",
        "type": "Project",
        "time": "2026-11-03T10:00:03.000Z",
    }
    assert sel["slash_commands"] == [{"time": "2026-11-03T10:00:06.500Z"}]
    assert sel["assistant_text"] == [{"time": "2026-11-03T10:00:05.000Z"}]


def test_input_at_tool_keeps_the_input_only_on_calls_of_that_tool(root: Path) -> None:
    _write(root / PROJECT / f"{SID}.jsonl", _main_session())
    sel = _one("tool_calls.name,tool_calls.input@Bash")
    assert sel["tool_calls"] == [
        {"name": "Bash", "input": {"command": "git status"}},
        {"name": "mcp__nexus__memory_get"},
    ]
    both = _one(
        "tool_calls.name,tool_calls.input@Bash,tool_calls.input@mcp__nexus__memory_get"
    )
    assert [c["input"] for c in both["tool_calls"]] == [
        {"command": "git status"},
        {"project": "p", "title": "a title"},
    ]
    # Exact name: a prefix or a different case matches nothing.
    none = _one("tool_calls.name,tool_calls.input@Bas,tool_calls.input@bash")
    assert all("input" not in c for c in none["tool_calls"])
    # Plain tool_calls.input keeps every input, alone or beside an @ form.
    plain = _one("tool_calls.name,tool_calls.input,tool_calls.input@Bash")
    assert [c["input"] for c in plain["tool_calls"]] == [
        {"command": "git status"},
        {"project": "p", "title": "a title"},
    ]


def test_a_sub_field_beside_its_bare_list_name_selects_all(root: Path) -> None:
    _write(root / PROJECT / f"{SID}.jsonl", _main_session())
    full = _one(None)
    assert _one("tool_calls.name,tool_calls")["tool_calls"] == full["tool_calls"]


@pytest.mark.parametrize("query", sorted(QUERIES))
def test_each_query_field_set_validates_and_returns_only_its_keys(
    root: Path, query: str
) -> None:
    fields, bodies, tops = QUERIES[query]
    _write(root / PROJECT / f"{SID}.jsonl", _main_session())
    rec = _one(",".join(fields), bodies=bodies or None)
    assert set(rec) == ALWAYS | tops
    allowed = {
        "tool_calls": {"name", "time", "is_error", "input", "input_bytes"},
        "instruction_loads": {"path", "kind", "type", "size", "time"},
        "slash_commands": {"name", "time"},
        "assistant_text": {"time", "text"},
    }
    for key, keys in allowed.items():
        for el in rec.get(key, []):
            assert set(el) <= keys
    if "tool_calls" in tops:
        calls = rec["tool_calls"]
        assert [c["name"] for c in calls] == ["Bash", "mcp__nexus__memory_get"]
        # An input rides only on a call whose tool an input@ named, exactly
        # or by a *<tail> suffix.
        wanted = {f.split("@", 1)[1] for f in fields if "@" in f}
        for c in calls:
            hit = any(
                c["name"].endswith(w[1:]) if w.startswith("*") else c["name"] == w
                for w in wanted
            )
            assert ("input" in c) == hit
    if query == "violations":
        assert rec["assistant_text"] == [
            {"time": "2026-11-03T10:00:05.000Z", "text": BODY}
        ]
        assert rec["tool_calls"][0] == {
            "name": "Bash",
            "input": {"command": "git status"},
        }
    if query == "deferred_tools":
        assert rec["deferred_tools"] == ["WebFetch", "mcp__x__y"]
    if query == "tools":
        assert [c["input_bytes"] for c in rec["tool_calls"]] == [10, 8]
        assert rec["tool_calls"][0]["time"] == "2026-11-03T10:00:05.500Z"
    if query == "memory":
        assert "input" not in rec["tool_calls"][0]
        assert rec["tool_calls"][1]["input"] == {"project": "p", "title": "a title"}
    if query == "agents":
        assert rec["tool_calls"][1]["is_error"] is True


def test_a_selected_out_input_is_never_cleaned(
    root: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    recs = _main_session()
    recs.append(
        _assistant(
            "2026-11-03T10:00:10.000Z",
            SID,
            [_tool_use("w1", "Write", {"content": "NEVER-CLEANED"})],
            msg_id="w1",
        )
    )
    _write(root / PROJECT / f"{SID}.jsonl", recs)
    seen: list[object] = []
    real = vt._clean

    def spy(value, cut, depth=0):
        if depth == 0:
            seen.append(value)
        return real(value, cut, depth)

    monkeypatch.setattr(vt, "_clean", spy)
    rec = _one("tool_calls.name,tool_calls.input@Bash")
    assert seen == [{"command": "git status"}]
    assert [c["name"] for c in rec["tool_calls"]] == [
        "Bash",
        "mcp__nexus__memory_get",
        "Write",
    ]
    seen.clear()
    assert _one("tool_calls.name,tool_call_count")["tool_call_count"] == 3
    assert seen == []
    _one(None)
    assert len(seen) == 3


def test_fields_never_change_which_records_come_back(root: Path) -> None:
    other = "22222222-0000-0000-0000-000000000000"
    _write(root / PROJECT / f"{SID}.jsonl", _main_session())
    _write(
        root / PROJECT / f"{other}.jsonl",
        _main_session("2026-11-03T12:00:00.000Z", other),
    )
    _write(
        root / PROJECT / SID / "subagents" / "agent-a1.jsonl",
        [_assistant("2026-11-03T10:30:00.000Z", SID, [], msg_id="s1")],
    )
    full = _sessions()
    narrow = _sessions(fields="models")
    assert len(full) == len(narrow) == 3
    assert [(r["session_id"], r["agent_id"], r["main"]) for r in full] == [
        (r["session_id"], r["agent_id"], r["main"]) for r in narrow
    ]


def test_ordering_does_not_need_start_to_be_selected(root: Path) -> None:
    other = "22222222-0000-0000-0000-000000000000"
    _write(
        root / PROJECT / f"{other}.jsonl",
        _main_session("2026-11-03T09:00:00.000Z", other),
    )
    _write(root / PROJECT / f"{SID}.jsonl", _main_session())
    assert [r["session_id"] for r in _sessions(fields="models")] == [other, SID]


def test_the_record_cap_applies_to_what_is_selected(root: Path) -> None:
    big = "x" * 600_000
    recs = _main_session()
    recs.append(
        _assistant(
            "2026-11-03T10:00:10.000Z",
            SID,
            [_tool_use("b1", "Write", {"file_path": "/r/f", "content": big})],
            msg_id="b1",
        )
    )
    _write(root / PROJECT / f"{SID}.jsonl", recs)
    # Not selected: nothing to cut, so nothing is marked.
    rec = _one("tool_calls.name")
    assert rec["truncated"] is False and len(rec["tool_calls"]) == 3
    # Selected: cut as before.
    rec = _one("tool_calls.name,tool_calls.input@Write")
    assert rec["truncated"] is True
    assert len(encode_result(rec)) <= vt.RECORD_CAP_BYTES
    assert 0 < len(rec["tool_calls"][2]["input"]["content"]) < len(big)
    # Calls with no input key survive the cut steps.
    assert "input" not in rec["tool_calls"][0]


def test_a_selected_list_is_dropped_by_the_cap_without_the_call_count_changing(
    root: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    recs = _main_session()
    for i in range(2000):
        recs.append(
            _assistant(
                "2026-11-03T10:00:10.000Z",
                SID,
                [_tool_use(f"z{i}", "Grep", {"pattern": "p" * 40})],
                msg_id=f"z{i}",
            )
        )
    _write(root / PROJECT / f"{SID}.jsonl", recs)
    monkeypatch.setattr(vt, "RECORD_CAP_BYTES", 20_000)
    rec = _one("tool_call_count,tool_calls.name,tool_calls.input")
    assert rec["truncated"] is True
    assert rec["tool_call_count"] == 2002
    assert rec["tool_calls_dropped"] > 0


def test_a_result_over_the_cap_is_judged_on_the_selected_fields(
    root: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    for i in range(3):
        _write(
            root / PROJECT / f"s{i}.jsonl",
            _main_session(f"2026-11-03T1{i}:00:00.000Z", f"s{i}"),
        )
    one = len(encode_result(_sessions()[0]))
    monkeypatch.setattr(verb_types, "VERB_RESULT_CAP_BYTES", 2 * one)
    with pytest.raises(VerbError, match=r"^result-too-large:"):
        _sessions()
    assert len(_sessions(fields="models")) == 3


# ---- project "repo" ----------------------------------------------------------

REPO_NEEDED = (
    'transcripts.sessions: project "repo" needs a repository '
    "(the run started outside one)"
)


def test_project_repo_outside_a_repository_is_a_verb_error(root: Path) -> None:
    _write(root / PROJECT / f"{SID}.jsonl", _main_session())
    with pytest.raises(VerbError) as err:
        _sessions(project="repo", ctx=VerbContext(repo_root=None))
    assert str(err.value) == REPO_NEEDED


def test_a_resolver_value_error_is_the_same_verb_error(
    root: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _write(root / PROJECT / f"{SID}.jsonl", _main_session())

    def not_a_repo(projects_root, repo_root, *, deadline):
        raise ValueError(f"{repo_root} is not a git repository")

    monkeypatch.setattr(vt, "repo_project_dirs", not_a_repo)
    with pytest.raises(VerbError) as err:
        _sessions(project="repo", ctx=VerbContext(repo_root=tmp_path))
    assert str(err.value) == REPO_NEEDED
    assert not isinstance(err.value, VerbArgError)


def test_project_repo_is_the_union_over_the_resolved_directories(
    root: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    ids = {
        "-a-proj": "aaaaaaaa-0000-0000-0000-000000000000",
        "-a-proj-wt-x": "bbbbbbbb-0000-0000-0000-000000000000",
        "-elsewhere": "cccccccc-0000-0000-0000-000000000000",
    }
    for i, (d, sid) in enumerate(ids.items()):
        _write(
            root / d / f"{sid}.jsonl", _main_session(f"2026-11-03T1{i}:00:00.000Z", sid)
        )
    calls: list[tuple] = []

    def fake(projects_root, repo_root, *, deadline):
        calls.append((projects_root, repo_root, deadline))
        return ["-a-proj", "-a-proj-wt-x"]

    monkeypatch.setattr(vt, "repo_project_dirs", fake)
    repo = tmp_path / "repo-root"
    deadline = _deadline()
    out = vt.sessions(
        "2026-11-03",
        "2026-11-04",
        "repo",
        None,
        None,
        deadline=deadline,
        ctx=VerbContext(repo_root=repo),
    )
    assert [(r["project"], r["session_id"]) for r in out] == [
        ("-a-proj", ids["-a-proj"]),
        ("-a-proj-wt-x", ids["-a-proj-wt-x"]),
    ]
    assert calls == [(root, repo, deadline)]
    # Fields compose with it.
    out = _sessions(project="repo", fields="project", ctx=VerbContext(repo_root=repo))
    assert [r["project"] for r in out] == ["-a-proj", "-a-proj-wt-x"]
    # "all" and nil are still every directory, and ask no resolver.
    calls.clear()
    assert len(_sessions(project="all")) == 3 and len(_sessions(project=None)) == 3
    assert calls == []


def test_project_repo_with_no_directories_is_an_empty_list(
    root: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _write(root / PROJECT / f"{SID}.jsonl", _main_session())
    monkeypatch.setattr(vt, "repo_project_dirs", lambda *a, **k: [])
    assert _sessions(project="repo", ctx=VerbContext(repo_root=tmp_path)) == []


def test_a_resolver_timeout_propagates(
    root: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _write(root / PROJECT / f"{SID}.jsonl", _main_session())

    def late(projects_root, repo_root, *, deadline):
        raise VerbTimeout("late")

    monkeypatch.setattr(vt, "repo_project_dirs", late)
    with pytest.raises(VerbTimeout):
        _sessions(project="repo", ctx=VerbContext(repo_root=tmp_path))


def test_project_repo_with_no_projects_directory_is_a_verb_error(
    root: Path, tmp_path: Path
) -> None:
    with pytest.raises(VerbError, match="no Claude Code projects directory"):
        _sessions(project="repo", ctx=VerbContext(repo_root=tmp_path))


def test_project_repo_through_the_real_resolver(root: Path, tmp_path: Path) -> None:
    def git(*args: str, cwd: Path) -> None:
        subprocess.run(["git", *args], cwd=cwd, check=True, capture_output=True)

    main = tmp_path / "git" / "proj"
    main.mkdir(parents=True)
    git("init", "-q", "-b", "main", cwd=main)
    git(
        "-c", "user.name=t", "-c", "user.email=t@t",
        "commit", "-q", "--allow-empty", "-m", "i",
        cwd=main,
    )  # fmt: skip
    gone = tmp_path / "git" / "proj-wt" / "feat"  # a removed worktree: no path on disk
    sessions = {
        "-main": (main, "aaaaaaaa-0000-0000-0000-000000000000"),
        "-wt": (gone, "bbbbbbbb-0000-0000-0000-000000000000"),
        "-other": (tmp_path / "git" / "other", "cccccccc-0000-0000-0000-000000000000"),
    }
    for i, (d, (cwd, sid)) in enumerate(sessions.items()):
        recs = _main_session(f"2026-11-03T1{i}:00:00.000Z", sid)
        recs[1]["cwd"] = str(cwd)
        _write(root / d / f"{sid}.jsonl", recs)
    out = _sessions(project="repo", ctx=VerbContext(repo_root=main))
    assert [r["project"] for r in out] == ["-main", "-wt"]
    assert len(_sessions(project="all")) == 3


# ---- round 2: never held, _fit on partial selections, input needs name ----------


@pytest.mark.parametrize(
    "fields",
    [
        "tool_calls.input",
        "tool_calls.input@Bash",
        "tool_calls.time,tool_calls.input@Bash",
        "tool_calls.input,tool_calls.input@Bash",
    ],
)
def test_an_input_without_the_name_is_an_arg_error(root: Path, fields: str) -> None:
    _write(root / PROJECT / f"{SID}.jsonl", _main_session())
    with pytest.raises(VerbArgError, match="tool_calls.name"):
        _sessions(fields=fields)


@pytest.mark.parametrize(
    "fields",
    [
        "tool_calls.name,tool_calls.input",
        "tool_calls",
        "tool_calls.input@Bash,tool_calls.name",
    ],
)
def test_an_input_with_the_name_or_a_bare_list_is_accepted(
    root: Path, fields: str
) -> None:
    _write(root / PROJECT / f"{SID}.jsonl", _main_session())
    assert all("name" in c for c in _one(fields)["tool_calls"])


@pytest.mark.parametrize("tool", [" Bash", "\tBash", " Bash", "* Bash", "*\tBash"])
def test_a_tool_name_with_surrounding_whitespace_is_an_arg_error(
    root: Path, tool: str
) -> None:
    _write(root / PROJECT / f"{SID}.jsonl", _main_session())
    with pytest.raises(VerbArgError, match="whitespace"):
        _sessions(fields=f"tool_calls.name,tool_calls.input@{tool}")


@pytest.mark.parametrize(
    "tool", ["*", "**x", "x*", "Ba*sh", "*a*b", "a*", "*__memory_get*"]
)
def test_a_star_anywhere_but_one_leading_one_is_an_arg_error(
    root: Path, tool: str
) -> None:
    _write(root / PROJECT / f"{SID}.jsonl", _main_session())
    with pytest.raises(VerbArgError, match="tool_calls.input@"):
        _sessions(fields=f"tool_calls.name,tool_calls.input@{tool}")


def test_input_at_star_tail_matches_names_ending_with_the_tail(root: Path) -> None:
    recs = _main_session()
    for tid, name in [
        ("s1", "mcp__plugin_conexus_nexus__memory_get"),
        ("s2", "mcp__plugin_conexus_nexus__memory_getter"),
        ("s3", "memory_get"),
        ("s4", "MEMORY_GET"),
        ("s5", "xx__memory_get"),
    ]:
        recs.append(
            _assistant(
                "2026-11-03T10:00:10.000Z",
                SID,
                [_tool_use(tid, name, {"k": tid})],
                msg_id=tid,
            )
        )
    _write(root / PROJECT / f"{SID}.jsonl", recs)
    rec = _one("tool_calls.name,tool_calls.input@*__memory_get,tool_calls.input@Bash")
    assert {c["name"]: "input" in c for c in rec["tool_calls"]} == {
        "Bash": True,
        "mcp__nexus__memory_get": True,
        "mcp__plugin_conexus_nexus__memory_get": True,
        "mcp__plugin_conexus_nexus__memory_getter": False,
        "memory_get": False,  # the tail is "__memory_get": both underscores
        "MEMORY_GET": False,
        "xx__memory_get": True,
    }
    # An exact name is not a suffix: "memory_get" matches only itself.
    rec = _one("tool_calls.name,tool_calls.input@memory_get")
    assert {c["name"] for c in rec["tool_calls"] if "input" in c} == {"memory_get"}


def test_input_bytes_sums_top_level_string_values_of_the_raw_input(
    root: Path,
) -> None:
    recs = _main_session()
    big = "x" * 300_000  # past INPUT_STRING_CAP: counted before any clean
    for tid, inp in [
        ("b1", {"a": "héllo", "n": 12345, "l": ["abcdef"], "d": {"s": "nested"}}),
        ("b2", {"a": big, "b": "yz"}),
        ("b3", {"a": "\ud800"}),  # a lone surrogate, as it reaches the file
        ("b4", ["a", "b"]),
        ("b5", "just a string"),
        ("b6", None),
        ("b7", {}),
    ]:
        recs.append(
            _assistant(
                "2026-11-03T10:00:10.000Z",
                SID,
                [_tool_use(tid, "Edit", inp)],
                msg_id=tid,
            )
        )
    _write(root / PROJECT / f"{SID}.jsonl", recs)
    rec = _one("tool_calls.name,tool_calls.input_bytes")
    assert rec["truncated"] is False  # counting a long input cuts nothing
    assert [(c["name"], c["input_bytes"]) for c in rec["tool_calls"]] == [
        ("Bash", 10),
        ("mcp__nexus__memory_get", 8),
        ("Edit", len("héllo".encode())),
        ("Edit", 300_002),
        ("Edit", 3),
        ("Edit", 0),
        ("Edit", 0),
        ("Edit", 0),
        ("Edit", 0),
    ]
    # On every call, whether or not that call's input is selected.
    rec = _one("tool_calls.name,tool_calls.input_bytes,tool_calls.input@Edit")
    assert all("input_bytes" in c for c in rec["tool_calls"])
    assert [("input" in c) for c in rec["tool_calls"]] == [False, False] + [True] * 7
    # Measured on the raw input: the carried copy was cut, the count was not.
    assert len(rec["tool_calls"][3]["input"]["a"]) == 256 * 1024
    assert rec["tool_calls"][3]["input_bytes"] == 300_002
    assert rec["truncated"] is True


def test_input_bytes_is_opt_in_and_needs_no_input_or_name(root: Path) -> None:
    _write(root / PROJECT / f"{SID}.jsonl", _main_session())
    assert "input_bytes" not in _one(None)["tool_calls"][0]
    assert "input_bytes" not in _one("tool_calls")["tool_calls"][0]
    assert _one("tool_calls.input_bytes")["tool_calls"] == [
        {"input_bytes": 10},
        {"input_bytes": 8},
    ]


def test_a_bare_list_beside_input_bytes_keeps_input_bytes(root: Path) -> None:
    _write(root / PROJECT / f"{SID}.jsonl", _main_session())
    call = _one("tool_calls,tool_calls.input_bytes")["tool_calls"][0]
    assert call["input_bytes"] == 10
    assert set(_one("tool_calls")["tool_calls"][0]) | {"input_bytes"} == set(call)


def test_input_bytes_alone_cleans_no_input(
    root: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _write(root / PROJECT / f"{SID}.jsonl", _main_session())
    monkeypatch.setattr(
        vt, "_clean", lambda *a, **k: pytest.fail("an unselected input was cleaned")
    )
    assert _one("tool_calls.name,tool_calls.input_bytes")["tool_calls"][0] == {
        "name": "Bash",
        "input_bytes": 10,
    }


@pytest.fixture
def held(monkeypatch: pytest.MonkeyPatch) -> dict:
    """The _Session each scan built, and every string _text and _byte_size saw."""
    sessions: list[vt._Session] = []
    texts: list[str] = []
    sized: list[object] = []

    class Seen(vt._Session):
        def __init__(self, *a, **k) -> None:
            super().__init__(*a, **k)
            sessions.append(self)

    real_text, real_size = vt._text, vt._byte_size

    def text(value, limit):
        if isinstance(value, str):
            texts.append(value)
        return real_text(value, limit)

    def size(content):
        sized.append(content)
        return real_size(content)

    monkeypatch.setattr(vt, "_Session", Seen)
    monkeypatch.setattr(vt, "_text", text)
    monkeypatch.setattr(vt, "_byte_size", size)
    return {"sessions": sessions, "texts": texts, "sized": sized}


def _held_session(root: Path, held: dict, fields: str, bodies=None) -> vt._Session:
    recs = _main_session()
    recs.append(  # a call that never gets a result stays pending if tracked
        _assistant(
            "2026-11-03T10:00:09.000Z",
            SID,
            [_tool_use("open1", "Grep", {"pattern": "x"})],
            msg_id="m9",
        )
    )
    _write(root / PROJECT / f"{SID}.jsonl", recs)
    _one(fields, bodies=bodies)
    [s] = held["sessions"]
    return s


@pytest.mark.parametrize("bodies", [None, True])
def test_a_scan_that_selects_no_list_holds_none(root: Path, held: dict, bodies) -> None:
    s = _held_session(root, held, "tokens,tool_call_count", bodies)
    assert s.calls == [] and s.loads == [] and s.commands == [] and s.text == []
    assert s.pending == {}
    assert s.deferred == set() and s.deferred_dropped == 0
    assert s.call_count == 3  # counted, not held
    assert BODY not in held["texts"]
    assert held["sized"] == []
    assert "Project" not in held["texts"] and "/r/CLAUDE.md" not in held["texts"]


def test_only_the_selected_list_is_held(root: Path, held: dict) -> None:
    s = _held_session(root, held, "tool_calls.name", True)
    assert len(s.calls) == 3
    assert s.pending == {}  # neither result_time nor is_error is selected
    assert s.loads == [] and s.commands == [] and s.text == []
    assert s.deferred == set()
    assert held["sized"] == [] and BODY not in held["texts"]


def test_results_are_tracked_only_when_a_result_key_is_selected(
    root: Path, held: dict
) -> None:
    s = _held_session(root, held, "tool_calls.name,tool_calls.is_error")
    assert list(s.pending) == ["open1"]
    held["sessions"].clear()
    s = _held_session(root, held, "tool_calls.name,tool_calls.result_time")
    assert list(s.pending) == ["open1"]


def test_deferred_tools_are_held_only_when_one_of_their_fields_is_selected(
    root: Path, held: dict
) -> None:
    s = _held_session(root, held, "instruction_loads")
    assert s.deferred == set() and s.deferred_dropped == 0
    held["sessions"].clear()
    s = _held_session(root, held, "deferred_tools_dropped")
    assert s.deferred == {"WebFetch", "mcp__x__y"}


def test_instruction_loads_hold_only_the_selected_keys_and_never_size_unasked(
    root: Path, held: dict
) -> None:
    s = _held_session(root, held, "instruction_loads.path")
    assert [set(x) for x in s.loads] == [{"path"}, {"path"}]
    assert held["sized"] == []  # no content measured
    assert "Project" not in held["texts"] and "Local" not in held["texts"]
    held["sessions"].clear()
    s = _held_session(root, held, "instruction_loads.size")
    assert len(held["sized"]) == 2


def test_assistant_text_holds_only_the_selected_keys_and_never_decodes_unasked(
    root: Path, held: dict
) -> None:
    s = _held_session(root, held, "assistant_text.time", True)
    assert s.text == [{"time": "2026-11-03T10:00:05.000Z"}]
    assert BODY not in held["texts"]
    held["sessions"].clear()
    s = _held_session(root, held, "assistant_text.text", True)
    assert s.text == [{"text": BODY}]
    assert BODY in held["texts"]


def test_slash_commands_are_held_only_when_selected(root: Path, held: dict) -> None:
    s = _held_session(root, held, "slash_commands.name")
    assert s.commands == [{"name": "conexus:rdr-gate"}]


def test_fit_cuts_partial_selections_and_counts_exactly(root: Path) -> None:
    # Six 250 KB inputs, all kept by _clean, are 1.5 MB: _fit must cut them.
    # Calls of other tools carry no input key; the cut must pass them by.
    recs = _main_session()
    n_other = 20
    for i in range(6):
        recs.append(
            _assistant(
                "2026-11-03T10:00:10.000Z",
                SID,
                [_tool_use(f"w{i}", "Write", {"content": "x" * 250_000})],
                msg_id=f"w{i}",
            )
        )
    for i in range(n_other):
        recs.append(
            _assistant(
                "2026-11-03T10:00:11.000Z",
                SID,
                [_tool_use(f"g{i}", "Grep", {"pattern": "p"})],
                msg_id=f"g{i}",
            )
        )
    _write(root / PROJECT / f"{SID}.jsonl", recs)
    rec = _one("tool_call_count,tool_calls.name,tool_calls.input@Write")
    assert len(encode_result(rec)) <= vt.RECORD_CAP_BYTES
    assert rec["truncated"] is True
    assert rec["tool_call_count"] == 2 + 6 + n_other
    assert "tool_calls_dropped" not in rec
    calls = rec["tool_calls"]
    assert len(calls) == 2 + 6 + n_other
    writes = [c for c in calls if c["name"] == "Write"]
    assert len(writes) == 6
    assert all(0 < len(c["input"]["content"]) < 250_000 for c in writes)
    assert all("input" not in c for c in calls if c["name"] != "Write")


def test_fit_drops_a_partial_text_list_without_its_text_key(root: Path) -> None:
    # 40,000 time-only text elements are about 1.5 MB with no text to cut: the
    # cut steps must pass them by, then the tails are dropped and counted.
    n_text = 40_000
    recs = _main_session()
    for i in range(n_text):
        recs.append(
            _assistant(
                "2026-11-03T10:00:10.000Z",
                SID,
                [{"type": "text", "text": "t"}],
                msg_id=f"t{i}",
            )
        )
    _write(root / PROJECT / f"{SID}.jsonl", recs)
    rec = _one("tool_call_count,tool_calls.name,assistant_text.time", bodies=True)
    assert len(encode_result(rec)) <= vt.RECORD_CAP_BYTES
    assert rec["truncated"] is True
    assert rec["tool_call_count"] == 2
    # tool_calls come first in the drop order, and carry no input to cut.
    assert rec["tool_calls_dropped"] + len(rec["tool_calls"]) == 2
    kept = rec["assistant_text"]
    assert all(set(t) == {"time"} for t in kept)
    assert rec["assistant_text_dropped"] > 0
    assert len(kept) + rec["assistant_text_dropped"] == n_text + 1  # +1: BODY


def test_project_repo_with_an_empty_window_never_asks_the_resolver(
    root: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _write(root / PROJECT / f"{SID}.jsonl", _main_session())
    monkeypatch.setattr(
        vt, "repo_project_dirs", lambda *a, **k: pytest.fail("resolver asked")
    )
    ctx = VerbContext(repo_root=tmp_path)
    assert _sessions("2026-11-04", "2026-11-04", "repo", ctx=ctx) == []
    assert _sessions("2026-11-05", "2026-11-04", "repo", ctx=ctx) == []
    with pytest.raises(VerbError) as err:  # still the repository check first
        _sessions("2026-11-04", "2026-11-04", "repo", ctx=VerbContext(repo_root=None))
    assert str(err.value) == REPO_NEEDED
