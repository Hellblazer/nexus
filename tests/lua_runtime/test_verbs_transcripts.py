# SPDX-License-Identifier: AGPL-3.0-or-later
"""transcripts.sessions against fixture transcripts (RDR-242 Technical
Design 10; RDR-240 Technical Design 4 and 5; gate r3 S2 and S3; RDR-240
gate r2 Significant 5; nexus-mdbds.8)."""

from __future__ import annotations

import json
import logging
import os
import time
from pathlib import Path

import pytest

from nexus.lua_runtime import verb_types
from nexus.lua_runtime import verbs_transcripts as vt
from nexus.lua_runtime.verbs import VERB_TABLE
from nexus.lua_runtime.verb_types import (
    ParamSpec,
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


def _sessions(since="2026-11-03", until="2026-11-04", project=PROJECT, bodies=None):
    return vt.sessions(since, until, project, bodies, deadline=_deadline(), ctx=CTX)


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


def test_the_verb_is_registered_with_its_four_parameters() -> None:
    [spec] = vt.VERBS
    assert spec.name == "transcripts.sessions"
    assert spec.params == (
        ParamSpec("since", "string"),
        ParamSpec("until", "string"),
        ParamSpec("project", "string", optional=True),
        ParamSpec("bodies", "boolean", optional=True),
    )
    assert spec.fn is vt.sessions


def test_the_verb_table_loads_it() -> None:
    assert VERB_TABLE["transcripts.sessions"].fn is vt.sessions
