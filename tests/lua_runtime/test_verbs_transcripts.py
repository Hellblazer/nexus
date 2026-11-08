# SPDX-License-Identifier: AGPL-3.0-or-later
"""transcripts.sessions against fixture transcripts (RDR-242 Technical
Design 10; RDR-240 Technical Design 4; gate r3 S2 and S3; RDR-240 gate r2
Significant 5; nexus-mdbds.8)."""

from __future__ import annotations

import json
import logging
import time
from pathlib import Path

import pytest

from nexus.lua_runtime import verbs_transcripts as vt
from nexus.lua_runtime.verb_types import ParamSpec, VerbError, VerbTimeout

PROJECT = "-Users-someone-git-proj"
SID = "11111111-2222-3333-4444-555555555555"
BODY = "BODY-SENTINEL-assistant-said-this"
RESULT = "BODY-SENTINEL-tool-result-content"
PROMPT = "BODY-SENTINEL-user-typed-this"
SIDE = "BODY-SENTINEL-sidechain"


# ---- fixture builders: records in Claude Code's transcript shape ----------


def _base(ts: str, sid: str, *, side: bool = False) -> dict:
    return {"timestamp": ts, "sessionId": sid, "isSidechain": side, "uuid": ts + sid}


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
    block = {"type": "tool_result", "tool_use_id": tid, "content": RESULT}
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
            {
                "type": "deferred_tools_delta",
                "addedNames": ["mcp__x__y"],
                "addedLines": ["mcp__x__y"],
            },
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
                "content": {"content": RESULT},
            },
        ),
        _assistant(
            f"{day}T10:00:05.000Z",
            sid,
            [{"type": "text", "text": BODY}],
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


def _write(path: Path, records: list[dict]) -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("w", encoding="utf-8") as fh:
        for r in records:
            fh.write(json.dumps(r) + "\n")
    return path


@pytest.fixture
def root(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    cfg = tmp_path / "claude"
    monkeypatch.setenv("CLAUDE_CONFIG_DIR", str(cfg))
    return cfg / "projects"


def _deadline() -> float:
    return time.monotonic() + 60


def _sessions(since="2026-11-03", until="2026-11-04", project=PROJECT, bodies=None):
    return vt.sessions(since, until, project, bodies, deadline=_deadline())


# ---- the record --------------------------------------------------------


def test_one_record_per_main_session_with_its_fields(root: Path) -> None:
    _write(root / PROJECT / f"{SID}.jsonl", _main_session())
    [rec] = _sessions()
    assert rec["session_id"] == SID
    assert rec["main"] is True
    assert rec["agent_id"] is None
    assert rec["project"] == PROJECT
    assert rec["start"] == "2026-11-03T10:00:00.000Z"
    assert rec["end"] == "2026-11-03T10:00:08.000Z"
    assert rec["models"] == ["claude-test-1", "claude-test-2"]
    assert rec["tokens"] == {
        "input": 20,
        "output": 10,
        "cache_read": 200,
        "cache_creation": 40,
    }
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
            "time": "2026-11-03T10:00:03.000Z",
        },
        {
            "path": "/r/sub/CLAUDE.md",
            "kind": "nested_memory",
            "time": "2026-11-03T10:00:04.000Z",
        },
    ]
    assert rec["deferred_tools"] == ["WebFetch", "mcp__x__y"]
    assert rec["bytes"] == (root / PROJECT / f"{SID}.jsonl").stat().st_size
    assert rec["records"] == 12
    assert rec["truncated"] is False
    assert "assistant_text" not in rec


def test_a_subagent_transcript_is_its_own_record(root: Path) -> None:
    _write(root / PROJECT / f"{SID}.jsonl", _main_session())
    side = [
        _assistant(
            "2026-11-03T10:01:00.000Z",
            SID,
            [_tool_use("s1", "Read", {"file_path": "/r/x.py"})],
            msg_id="sm1",
            side=True,
        ),
        _tool_result("2026-11-03T10:01:01.000Z", SID, "s1", is_error=None, side=True),
    ]
    _write(root / PROJECT / SID / "subagents" / "agent-abc123.jsonl", side)
    recs = _sessions()
    assert [(r["main"], r["agent_id"]) for r in recs] == [
        (True, None),
        (False, "agent-abc123"),
    ]
    sub = recs[1]
    assert sub["session_id"] == SID
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


# ---- bodies ------------------------------------------------------------


def test_no_body_reaches_the_result_without_bodies(root: Path) -> None:
    _write(root / PROJECT / f"{SID}.jsonl", _main_session())
    out = json.dumps(_sessions(bodies=False)) + json.dumps(_sessions(bodies=None))
    assert "BODY-SENTINEL" not in out


def test_bodies_carry_assistant_text_only(root: Path) -> None:
    _write(root / PROJECT / f"{SID}.jsonl", _main_session())
    [rec] = _sessions(bodies=True)
    assert rec["assistant_text"] == [{"time": "2026-11-03T10:00:05.000Z", "text": BODY}]
    out = json.dumps(rec)
    assert RESULT not in out
    assert PROMPT not in out
    assert SIDE not in out


def test_no_log_line_or_output_carries_a_body(
    root: Path, caplog: pytest.LogCaptureFixture, capsys: pytest.CaptureFixture[str]
) -> None:
    _write(root / PROJECT / f"{SID}.jsonl", _main_session())
    (root / PROJECT / "broken.jsonl").write_text(
        '{"timestamp": "2026-11-03T11:00:00Z", "x": "' + RESULT + "\n", encoding="utf-8"
    )
    caplog.set_level(logging.DEBUG)
    _sessions(bodies=False)
    captured = capsys.readouterr()
    assert "BODY-SENTINEL" not in caplog.text + captured.out + captured.err


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
    assert len(json.dumps(rec).encode("utf-8")) <= vt.RECORD_CAP_BYTES
    # The call and the text survive, cut; the small input is untouched.
    names = [c["name"] for c in rec["tool_calls"]]
    assert names == ["Bash", "mcp__nexus__memory_get", "Write"]
    assert rec["tool_calls"][0]["input"] == {"command": "git status"}
    assert len(rec["tool_calls"][2]["input"]["content"]) < len(big)
    assert rec["tool_calls"][2]["input"]["file_path"] == "/r/f"


def test_a_record_too_many_calls_to_fit_drops_the_tail_and_counts_it(
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
    assert len(json.dumps(rec).encode("utf-8")) <= vt.RECORD_CAP_BYTES
    assert rec["tool_calls_dropped"] > 0
    assert len(rec["tool_calls"]) + rec["tool_calls_dropped"] == 20_002


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
        (
            "2026-11-03T05:00:00-05:00",
            "2026-11-03T12:00:00",
            ["s10"],
        ),  # 10:00 UTC; naive is UTC
        ("2026-11-03", "2026-11-03T10:00:00Z", []),
    ],
)
def test_since_and_until_take_dates_and_timestamps(
    root: Path, since, until, expect
) -> None:
    _start_at(root, "2026-11-03T10:00:00.000Z", "s10")
    assert [r["session_id"] for r in _sessions(since, until)] == expect


def test_until_defaults_to_now(root: Path) -> None:
    _start_at(root, "2026-11-03T10:00:00.000Z", "s10")
    assert [
        r["session_id"]
        for r in vt.sessions("2026-11-01", None, PROJECT, None, deadline=_deadline())
    ] == ["s10"]


@pytest.mark.parametrize(
    ("since", "until"),
    [
        ("yesterday", "2026-11-04"),
        ("2026-11-03", "2026-13-01"),
        ("2026-11-04", "2026-11-03"),
    ],
)
def test_a_bad_window_is_a_verb_error(root: Path, since, until) -> None:
    (root / PROJECT).mkdir(parents=True)
    with pytest.raises(VerbError):
        _sessions(since, until)


def test_sessions_come_back_in_start_order_across_projects(root: Path) -> None:
    _write(root / "-b" / "b.jsonl", _main_session("2026-11-03T09:00:00.000Z", "b"))
    _write(root / "-a" / "a.jsonl", _main_session("2026-11-03T11:00:00.000Z", "a"))
    recs = _sessions(project="all")
    assert [(r["project"], r["session_id"]) for r in recs] == [("-b", "b"), ("-a", "a")]
    assert [r["session_id"] for r in _sessions(project="-a")] == ["a"]


@pytest.mark.parametrize("project", ["../etc", "a/b", "", ".", "..", "nope"])
def test_a_project_that_is_not_one_directory_is_a_verb_error(
    root: Path, project: str
) -> None:
    (root / PROJECT).mkdir(parents=True)
    with pytest.raises(VerbError):
        _sessions(project=project)


def test_no_projects_directory_is_a_verb_error(root: Path) -> None:
    with pytest.raises(VerbError, match="projects"):
        _sessions(project="all")


def test_unparseable_lines_and_files_without_a_timestamp_are_skipped(
    root: Path,
) -> None:
    _write(root / PROJECT / f"{SID}.jsonl", _main_session())
    with (root / PROJECT / f"{SID}.jsonl").open("a", encoding="utf-8") as fh:
        fh.write("{not json\n")
    _write(
        root / PROJECT / "no-ts.jsonl", [{"type": "custom-title", "sessionId": "no-ts"}]
    )
    assert [r["session_id"] for r in _sessions()] == [SID]


# ---- the deadline (gate r3 S2) -----------------------------------------


def test_the_deadline_is_checked_between_transcript_files(
    root: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    for i in range(10):
        _start_at(root, f"2026-11-03T1{i}:00:00.000Z", f"s{i}")
    clock = iter(range(1000))
    monkeypatch.setattr(vt, "_now", lambda: float(next(clock)))
    scanned: list[Path] = []
    real = vt._scan

    def counting(path, *a, **k):
        scanned.append(path)
        return real(path, *a, **k)

    monkeypatch.setattr(vt, "_scan", counting)
    with pytest.raises(VerbTimeout):
        vt.sessions("2026-11-03", "2026-11-04", PROJECT, None, deadline=3.0)
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
            "2026-11-03", "2026-11-04", PROJECT, None, deadline=time.monotonic() - 1
        )


def test_the_deadline_is_checked_inside_a_long_file(
    root: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    recs = _main_session()
    recs += [_user("2026-11-03T10:30:00.000Z", SID, "u")] * (
        3 * vt.LINES_PER_DEADLINE_CHECK
    )
    _write(root / PROJECT / f"{SID}.jsonl", recs)
    clock = iter(range(1000))
    monkeypatch.setattr(vt, "_now", lambda: float(next(clock)))
    with pytest.raises(VerbTimeout):
        vt.sessions("2026-11-03", "2026-11-04", PROJECT, None, deadline=3.0)


# ---- the verb result cap (gate r3 S3) ------------------------------------


def test_a_result_over_the_verb_result_cap_is_refused(
    root: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    for i in range(5):
        _start_at(root, f"2026-11-03T1{i}:00:00.000Z", f"s{i}")
    one = len(json.dumps(_sessions()[0]).encode("utf-8"))
    monkeypatch.setattr(vt, "VERB_RESULT_CAP_BYTES", 3 * one)
    with pytest.raises(VerbError, match="narrow"):
        _sessions()


# ---- registration --------------------------------------------------------


def test_the_verb_is_registered_with_its_four_parameters() -> None:
    [spec] = vt.VERBS
    assert spec.name == "transcripts.sessions"
    assert spec.params == (
        ParamSpec("since", "string"),
        ParamSpec("until", "string", optional=True),
        ParamSpec("project", "string"),
        ParamSpec("bodies", "boolean", optional=True),
    )
    assert spec.fn is vt.sessions
