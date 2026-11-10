# SPDX-License-Identifier: AGPL-3.0-or-later
"""nexus-qo7ie.18 (RDR-238 Phase 3 Step 2b): ``nx memory curate --files`` read counts from
RDR-240's ``memory`` query.

Most of these go through the real run path (``run_procedure`` -> ``run_script`` -> the
``transcripts`` procedure in its worker) over fixture transcripts under ``CLAUDE_CONFIG_DIR``,
in a fixture git repository: project ``repo`` finds a project directory by the cwd its
transcripts record. The guards that a fixture cannot reach (a non-page result, a join error,
a pager that does not advance, the page cap) use a stub procedure. No test mints a tenant.
"""
from __future__ import annotations

import json
import time
from datetime import UTC, datetime, timedelta
from pathlib import Path
from unittest.mock import patch

import pytest
from click.testing import CliRunner

import nexus.lua_runtime as lua_runtime
from nexus import memory_curate as mc
from nexus.cli import main
from nexus.commands import memory as memory_cmd
from nexus.lua_runtime import header, runner, transcripts_read, verbs_transcripts
from nexus.lua_runtime.verb_types import VerbContext
from nexus.memory_curate import cold, sessions
from tests._module_seam import setattr_in
from tests.test_memory_curate import _fm, _make_repo, _memdir

_NOW = datetime(2026, 11, 9, 12, 0, tzinfo=UTC)
_LABEL = "no Read-tool open or subagent load in any session that started in the window"
GET = "mcp__plugin_conexus_nexus__memory_get"
SEARCH = "mcp__plugin_conexus_nexus__memory_search"


def _assistant(ts: str, sid: str, cwd: Path, blocks: list, mid: str) -> dict:
    return {"type": "assistant", "timestamp": ts, "sessionId": sid, "isSidechain": False, "cwd": str(cwd),
            "message": {"id": mid, "model": "claude-opus-4-8", "role": "assistant", "content": blocks,
                        "usage": {"input_tokens": 1, "output_tokens": 1}}}


def _session(path: Path, sid: str, cwd: Path, start: str, calls: list[tuple[str, dict]],
             *, resumed: str | None = None) -> None:
    """One main-session transcript: a prompt at *start*, then one tool call a second. With
    *resumed*, the calls are written at that later time instead (a session resumed days on)."""
    t0 = datetime.fromisoformat(start.replace("Z", "+00:00"))
    t1 = datetime.fromisoformat(resumed.replace("Z", "+00:00")) if resumed else t0
    lines: list[dict] = [{"type": "user", "timestamp": start, "sessionId": sid, "cwd": str(cwd),
                          "message": {"role": "user", "content": "go"}}]
    for i, (name, inp) in enumerate(calls, start=1):
        ts = (t1 + timedelta(seconds=i)).strftime("%Y-%m-%dT%H:%M:%SZ")
        lines.append(_assistant(ts, sid, cwd, [{"type": "tool_use", "id": f"t{i}", "name": name, "input": inp}], f"m{i}"))
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text("".join(json.dumps(x) + "\n" for x in lines), encoding="utf-8")


@pytest.fixture
def world(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> dict[str, Path]:
    repo = _make_repo(tmp_path, "readrepo")
    config = tmp_path / "cfg"
    mem = _memdir(config, repo)
    (mem / "MEMORY.md").write_text("- [Read](read_me.md) - x\n- [Never](never.md) - y\n")
    (mem / "read_me.md").write_text(_fm("Read", "read in the window"))
    (mem / "never.md").write_text(_fm("Never", "not read in the window"))
    monkeypatch.chdir(repo)
    monkeypatch.setenv("CLAUDE_CONFIG_DIR", str(config))
    monkeypatch.setenv("NX_NO_TELEMETRY", "1")
    return {"repo": repo, "config": config, "mem": mem, "project": mem.parent}


def _lines(world: dict[str, Path], days: int = 30) -> list[str]:
    return mc.files_section(world["mem"], mc.load_reads(days, _NOW, world["mem"]))


# ── the window ────────────────────────────────────────────────────────────────────────


def test_the_window_is_capped_at_retention_and_at_the_oldest_session_and_says_why() -> None:
    w = mc.read_window(60, _NOW)
    assert (w.requested, w.covered) == (60, mc.RETENTION_DAYS)
    assert w.since == "2026-10-10T12:00:00Z" and w.until == "2026-11-09T12:00:00Z"
    assert w.describe() == ("covered 2026-10-10T12:00:00Z .. 2026-11-09T12:00:00Z "
                            "(requested 60 days, capped at 30: transcripts are kept about 30 days)")
    assert mc.read_window(7, _NOW).describe() == "covered 2026-11-02T12:00:00Z .. 2026-11-09T12:00:00Z (7 days)"
    # A shorter history than asked for (cleanupPeriodDays 7, or a new repo): the span on disk.
    held = mc.read_window(30, _NOW, oldest=_NOW - timedelta(days=6, hours=3))
    assert held.covered == 7 and "capped at 7: the oldest session on disk started 2026-11-03T09:00:00Z" in held.describe()
    # An oldest session further back than asked changes nothing.
    assert mc.read_window(10, _NOW, oldest=_NOW - timedelta(days=20)).covered == 10


def _raw(path: Path, records: list[dict]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text("".join(json.dumps(r) + "\n" for r in records), encoding="utf-8")


def _bytes(path: Path, *lines: bytes) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(b"".join(lines))


def _line(rec: dict) -> bytes:
    return json.dumps(rec).encode() + b"\n"


def _utc(stamp: str) -> datetime:
    t = datetime.fromisoformat(stamp.replace("Z", "+00:00"))
    return t.replace(tzinfo=UTC) if t.tzinfo is None else t.astimezone(UTC)


def _verb_starts(repo: Path, *, fields: str | None) -> list[datetime]:
    """The UTC starts of the main records ``transcripts.sessions`` returns over the same look-back."""
    verb = verbs_transcripts.sessions(
        (_NOW - timedelta(days=365)).strftime("%Y-%m-%dT%H:%M:%SZ"), _NOW.strftime("%Y-%m-%dT%H:%M:%SZ"), "repo",
        fields=fields, deadline=time.monotonic() + 60, ctx=VerbContext(repo_root=header.probe_repo(repo).root))
    return sorted(_utc(r["start"]) for r in verb if r["main"])


def test_the_session_starts_are_the_verbs_on_one_adversarial_fixture(world: dict[str, Path]) -> None:
    """Critic S1 (TD5, nexus-qo7ie.27): curate's first-record reader is the verb's ``first_start``, and on a fixture
    of the cases that could split them (framing, the large-line fast path, the sidechain skip, a tail stamp, a
    constant, a BOM, bad bytes) it agrees with ``transcripts.sessions`` on both the narrow selection and the full
    one, and with the hand count. A change to the verb's rule turns this red."""
    project, repo = world["project"], world["repo"]
    user = {"type": "user", "cwd": str(repo), "message": {"role": "user", "content": "go"}}
    day = "2026-11-08T"
    ok = lambda hh, extra="": _line({**user, "timestamp": f"{day}{hh}:00:00{extra or 'Z'}"})  # noqa: E731
    big = "z" * 5000
    expected: dict[str, int | None] = {}  # file -> the UTC hour its start must read as (None: no session)

    def case(name: str, hour: int | None, *lines: bytes) -> None:
        _bytes(project / f"{name}.jsonl", *lines)
        expected[name] = hour

    case("plain", 10, ok("10"))
    case("late", 11, *[_line({"type": "summary"})] * 250, ok("11"))  # past the old 200-line scan
    case("side", 12, _line({**user, "timestamp": "2026-11-01T00:00:00Z", "isSidechain": True}), ok("12"))
    case("naive", 13, _line({**user, "timestamp": f"{day}13:00:00"}))  # no offset: UTC
    case("invalid_json_first", 14, b"not json at all\n", ok("14"))
    case("list_first", 15, b"[1, 2, 3]\n", ok("15"))
    case("queue_operation_big", 16, _line({"type": "queue-operation", "operation": "enqueue", "sessionId": "q",
                                           "timestamp": f"{day}16:00:00Z", "content": big}))  # _inert's fast path
    case("sidechain_big_first", 17, _line({"type": "progress", "isSidechain": True, "timestamp": "2026-11-01T00:00:00Z",
                                           "content": big}), ok("17"))
    case("attachment_tail_stamp", 18, _line({"type": "attachment", "attachment": {"type": "x", "content": big},
                                              "timestamp": f"{day}18:00:00Z"}))
    case("nan", 19, b'{"type": "user", "timestamp": NaN}\n', ok("19"))
    case("no_trailing_newline", 20, ok("20").rstrip(b"\n"))
    case("offset", 16, _line({**user, "timestamp": f"{day}21:00:00+05:00"}))  # 21:00+05:00 is 16:00 UTC
    case("bom", 22, b"\xef\xbb\xbf" + ok("22"))
    case("bad_utf8", 23, b'{"type": "user", "timestamp": "\xff\xfe"}\n', ok("23"))
    case("bad_utf8_big", 9, b'{"type": "progress", "pad": "' + b"\xff" * 5000 + b'", "timestamp": "2026-11-08T09:30:00Z"}\n',
         ok("09"))
    case("nostart", None, _line({"type": "summary"}))
    _bytes(project / "sid" / "subagents" / "agent-x.jsonl", ok("10"))  # a subagent: a record of the verb, not a session
    hand = sorted(h for h in expected.values() if h is not None)
    assert len(hand) == 15

    found = mc.repo_session_starts(repo, _NOW)
    assert isinstance(found, mc.SessionStarts) and found.unscanned == 0, found
    mine = sorted(s.astimezone(UTC) for s in found.starts)
    assert [s.hour for s in mine] == hand, mine  # the hand count, by case
    for fields in ("start", None):  # the narrow selection (the _inert fast path) and the full decode
        assert _verb_starts(repo, fields=fields) == mine, fields
    opened = cold.open_window(30, _NOW, repo)
    assert not isinstance(opened, mc.ReadsUnavailable), opened
    assert opened[1] == len(hand) and opened[2] == 0


def test_a_failing_session_listing_is_a_reason_never_no_sessions(world: dict[str, Path], monkeypatch: pytest.MonkeyPatch) -> None:
    def boom(*_a: object, **_k: object) -> None:
        raise OSError("disk went away")

    monkeypatch.setattr(transcripts_read, "_files", boom)
    starts = mc.repo_session_starts(world["repo"], _NOW)
    assert starts == "the repository's sessions could not be listed: OSError: disk went away"
    reads = mc.load_reads(30, _NOW, world["mem"], world["repo"])
    assert isinstance(reads, mc.ReadsUnavailable) and reads.reason == starts


def test_a_file_with_no_timestamp_in_the_scan_bound_is_counted_apart_and_printed(
    world: dict[str, Path], monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The bound (START_SCAN_BYTES) is per file: a transcript whose first timestamp lies past it is not a session, a
    normal one is, a line longer than one read chunk is read whole, and the rows say how many were left out."""
    repo, project = world["repo"], world["project"]
    user = {"type": "user", "cwd": str(repo), "message": {"role": "user", "content": "go"}}
    _raw(project / "ok.jsonl", [{**user, "timestamp": "2026-11-08T10:00:00Z"}])
    _raw(project / "deep.jsonl", [{"type": "summary", "cwd": str(repo), "pad": "x" * 600}] * 50 + [{**user, "timestamp": "2026-11-08T11:00:00Z"}])
    _raw(project / "deep2.jsonl", [{"type": "summary", "cwd": str(repo), "pad": "x" * 600}] * 50 + [{**user, "timestamp": "2026-11-08T11:30:00Z"}])
    _raw(project / "long.jsonl", [{**user, "pad": "y" * 3000, "timestamp": "2026-11-08T12:00:00Z"}])
    _raw(project / "empty.jsonl", [{"type": "summary", "cwd": str(repo)}])  # no stamp, but inside the bound: not "unscanned"
    monkeypatch.setattr(sessions, "START_SCAN_BYTES", 4096)
    monkeypatch.setattr(transcripts_read, "LINE_CHUNK", 1024)  # the 3 KB line spans three chunks
    found = mc.repo_session_starts(repo, _NOW)
    assert isinstance(found, mc.SessionStarts)
    assert sorted(s.hour for s in found.starts) == [10, 12] and found.unscanned == 2, found
    opened = cold.open_window(30, _NOW, repo)
    assert not isinstance(opened, mc.ReadsUnavailable) and opened[1:] == (2, 2)
    reads = mc.load_reads(30, _NOW, world["mem"], repo)  # the real run path, for the row
    assert isinstance(reads, mc.ReadCounts) and (reads.sessions, reads.unscanned) == (2, 2), reads
    row = next(ln for ln in mc.files_section(world["mem"], reads) if "had no start in the first" in ln)
    assert row.startswith("read counts: 2 transcripts had no start in the first ") and row.endswith("were not counted")


def test_with_every_transcript_past_the_bound_the_reason_names_them(world: dict[str, Path], monkeypatch: pytest.MonkeyPatch) -> None:
    repo, project = world["repo"], world["project"]
    _raw(project / "deep.jsonl", [{"type": "summary", "cwd": str(repo), "pad": "x" * 600}] * 50
         + [{"type": "user", "cwd": str(repo), "timestamp": "2026-11-08T11:00:00Z"}])
    monkeypatch.setattr(sessions, "START_SCAN_BYTES", 4096)
    reads = mc.load_reads(30, _NOW, world["mem"], repo)
    assert isinstance(reads, mc.ReadsUnavailable)
    assert reads.reason.startswith("no session of this repository started in the window (1 transcript had no start in the first ")


def test_the_default_bound_prints_as_8_mib() -> None:
    assert sessions.START_SCAN_BYTES == 8 * 1024 * 1024
    assert sessions.unscanned_note(3) == "3 transcripts had no start in the first 8 MiB and were not counted"
    assert sessions.unscanned_note(1) == "1 transcript had no start in the first 8 MiB and was not counted"
    assert sessions.unscanned_note(0) == ""


def test_with_no_projects_directory_the_listing_says_so(world: dict[str, Path], monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    monkeypatch.setenv("CLAUDE_CONFIG_DIR", str(tmp_path / "nowhere"))
    starts = mc.repo_session_starts(world["repo"], _NOW)
    assert starts == f"no Claude Code projects directory at {tmp_path / 'nowhere' / 'projects'}"


# ── the real run path ────────────────────────────────────────────────────────────────


def test_a_file_read_in_the_window_is_not_listed_and_an_unread_one_is(world: dict[str, Path]) -> None:
    mem, repo = world["mem"], world["repo"]
    _session(world["project"] / "s1.jsonl", "s1", repo, "2026-11-08T10:00:00Z", [
        ("Read", {"file_path": str(mem / "read_me.md")}),
        (GET, {"project": "nexus", "title": "push-train"}),
        (GET, {"project": "nexus", "title": "push-train"}),
        (SEARCH, {"project": "nexus", "query": "x"}),
    ])
    lines = _lines(world)
    assert "read counts: covered 2026-11-07T12:00:00Z .. 2026-11-09T12:00:00Z (requested 30 days, capped at 2: " \
           "the oldest session on disk started 2026-11-08T10:00:00Z), 1 session started in it" in lines
    head = lines.index(f"{_LABEL}: 1 of 2 files (index files excluded)")
    assert lines[head + 1] == "  - never.md"
    assert lines[head + 2] == f"  ({mc.READ_SCOPE_NOTE})"
    assert lines[lines.index("T2 reads per title (memory_get): 1") + 1] == "  - nexus/push-train (2)"
    assert lines[lines.index("T2 searches per project (memory_search): 1") + 1] == "  - nexus (1)"


def test_a_session_with_no_reads_lists_every_file_and_says_none_for_t2(world: dict[str, Path]) -> None:
    _session(world["project"] / "s1.jsonl", "s1", world["repo"], "2026-11-08T10:00:00Z", [("Bash", {"command": "ls"})])
    lines = _lines(world)
    head = lines.index(f"{_LABEL}: 2 of 2 files (index files excluded)")
    assert lines[head + 1:head + 3] == ["  - never.md", "  - read_me.md"]
    assert "T2 reads per title (memory_get): none" in lines


def test_a_read_in_a_session_that_started_before_the_window_does_not_count(world: dict[str, Path]) -> None:
    project, repo, mem = world["project"], world["repo"], world["mem"]
    _session(project / "old.jsonl", "old", repo, "2026-10-20T10:00:00Z", [("Read", {"file_path": str(mem / "read_me.md")})])
    _session(project / "s1.jsonl", "s1", repo, "2026-11-08T10:00:00Z", [("Bash", {"command": "ls"})])
    assert f"{_LABEL}: 2 of 2 files (index files excluded)" in _lines(world, days=7)


def test_with_only_a_session_resumed_in_the_window_the_counts_are_not_available(world: dict[str, Path]) -> None:
    """Critic and code review r1: the session started before the window and was written in it
    (file time now). The verb counts it in its start window, so this window holds no session
    and must not read as "nothing was read"."""
    _session(world["project"] / "long.jsonl", "long", world["repo"], "2026-10-01T10:00:00Z",
             [("Read", {"file_path": str(world["mem"] / "read_me.md")})], resumed="2026-11-08T10:00:00Z")
    reads = mc.load_reads(7, _NOW, world["mem"])
    assert isinstance(reads, mc.ReadsUnavailable), reads
    assert reads.reason == "no session of this repository started in the window"
    lines = mc.files_section(world["mem"], reads)
    assert "unread files: not available: no session of this repository started in the window" in lines
    assert not any(ln.startswith(_LABEL) for ln in lines)


def test_with_no_transcripts_for_the_repo_the_counts_are_not_available(world: dict[str, Path]) -> None:
    reads = mc.load_reads(30, _NOW, world["mem"])
    assert isinstance(reads, mc.ReadsUnavailable) and reads.reason == "no session of this repository started in the window"
    assert "requested 30 days, capped at 30" not in reads.window.describe()  # no session: no oldest, retention cap only
    assert reads.window.covered == 30


def test_a_memory_directory_the_query_cannot_see_is_not_available(world: dict[str, Path], tmp_path: Path) -> None:
    """Critic and code review r1: an autoMemoryDirectory with no /memory/ component; the query
    marks no Read of it, so its files would all read as unread."""
    notes = tmp_path / "notes"
    notes.mkdir()
    (notes / "a.md").write_text(_fm("A", "x"))
    _session(world["project"] / "s1.jsonl", "s1", world["repo"], "2026-11-08T10:00:00Z", [("Read", {"file_path": str(notes / "a.md")})])
    reads = mc.load_reads(30, _NOW, notes)
    assert isinstance(reads, mc.ReadsUnavailable) and "only sees paths containing /memory/" in reads.reason


def test_without_the_runtime_the_rows_say_not_available(world: dict[str, Path], monkeypatch: pytest.MonkeyPatch) -> None:
    _session(world["project"] / "s1.jsonl", "s1", world["repo"], "2026-11-08T10:00:00Z", [("Bash", {"command": "ls"})])
    monkeypatch.delattr(lua_runtime, "run_script")
    reads = mc.load_reads(30, _NOW, world["mem"])
    assert isinstance(reads, mc.ReadsUnavailable) and reads.reason.startswith("unavailable: "), reads
    lines = mc.files_section(world["mem"], reads)
    assert any(ln.startswith("read counts: covered ") for ln in lines), lines  # the span prints on failure too
    assert any(ln.startswith("unread files: not available: unavailable: ") for ln in lines), lines
    assert any(ln.startswith("T2 read counts: not available: unavailable: ") for ln in lines), lines
    assert not any(ln.startswith(_LABEL) for ln in lines)  # never "every file unread"


def test_the_command_caps_days_prints_the_span_and_exits_0_without_the_runtime(
    world: dict[str, Path], monkeypatch: pytest.MonkeyPatch,
) -> None:
    _session(world["project"] / "s1.jsonl", "s1", world["repo"], "2026-10-01T10:00:00Z", [("Bash", {"command": "ls"})])
    _session(world["project"] / "s2.jsonl", "s2", world["repo"], "2026-11-08T10:00:00Z", [("Bash", {"command": "ls"})])
    monkeypatch.delattr(lua_runtime, "run_script")

    def no_store():
        raise RuntimeError("no store in this test")

    with patch("nexus.commands.memory.t2_handle", side_effect=no_store):
        res = CliRunner().invoke(main, ["memory", "curate", "--files", "--days", "60"])
    assert res.exit_code == 0, res.output
    assert "(requested 60 days, capped at 30: transcripts are kept about 30 days)" in res.output
    assert "unread files: not available: unavailable: " in res.output
    assert "report not stored: not available" in res.output


def test_an_unexpected_runtime_error_costs_only_the_read_count_rows(
    world: dict[str, Path], monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Code review I1 (nexus-qo7ie.27): ``run_script`` can raise outside PROCEDURE_ERRORS (an OSError from a
    worker spawn). It used to collapse the whole --files block into one line, losing the rows that need no runtime."""
    _session(world["project"] / "s1.jsonl", "s1", world["repo"], "2026-11-08T10:00:00Z", [("Bash", {"command": "ls"})])
    (world["mem"] / "stray.md").write_text(_fm("Stray", "in no index"))

    def boom(*_a: object, **_k: object) -> None:
        raise OSError("worker spawn failed")

    monkeypatch.setattr(lua_runtime, "run_script", boom)
    with patch("nexus.commands.memory.t2_handle", side_effect=RuntimeError("no store in this test")):
        res = CliRunner().invoke(main, ["memory", "curate", "--files"])
    assert res.exit_code == 0, res.output
    out = res.output
    assert "unindexed: 1" in out and "  - stray.md" in out, out  # the non-transcript rows survive
    assert "oversized (over " in out or "oversized: none" in out
    assert "unread files: not available: OSError: worker spawn failed" in out
    assert "T2 read counts: not available: OSError: worker spawn failed" in out
    assert "== --files ==\nnot available:" not in out


def test_a_deleted_working_directory_or_a_failing_identity_exits_0_with_a_not_available_line(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Code review S3 (nexus-qo7ie.27): the docstring promises exit 0 whatever happens; the cwd and the
    repository identity were read before any section guard."""
    def no_cwd() -> Path:
        raise FileNotFoundError("the working directory is gone")

    monkeypatch.setattr(Path, "cwd", staticmethod(no_cwd))
    res = CliRunner().invoke(main, ["memory", "curate", "--files"])
    assert res.exit_code == 0, res.output
    assert "not available: the repository could not be identified: FileNotFoundError: the working directory is gone" in res.output
    monkeypatch.undo()

    monkeypatch.chdir(tmp_path)

    def no_identity(_cwd: Path) -> tuple[str, str]:
        raise RuntimeError("git rev-parse did not run")

    monkeypatch.setattr("nexus.repo_identity._repo_identity", no_identity)
    res = CliRunner().invoke(main, ["memory", "curate", "--files"])
    assert res.exit_code == 0, res.output
    assert "not available: the repository could not be identified: RuntimeError: git rev-parse did not run" in res.output


def test_pages_are_joined_and_the_count_equals_what_the_fixture_planted(
    world: dict[str, Path], monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Over 768 KB of rows across three days forces more than one page (RDR-240 TD5's page
    bound); the joined counts must equal what the fixture planted, file by file."""
    mem, repo, project = world["mem"], world["repo"], world["project"]
    names = [f"note_{i:02d}.md" for i in range(25)]
    for n in names:
        (mem / n).write_text(_fm(n, "planted"))
    planted: dict[str, int] = {}
    for day in ("2026-11-05", "2026-11-06", "2026-11-07"):
        for k in range(30):
            _session(project / f"{day}-{k}.jsonl", f"{day}-{k}", repo, f"{day}T{k % 24:02d}:00:00Z",
                     [("Read", {"file_path": str(mem / n)}) for n in names])
            for n in names:
                planted[n] = planted.get(n, 0) + 1
    seen: list[dict] = []
    real = runner.run_procedure

    def spy(name: str, args: dict) -> object:
        seen.append(dict(args))
        return real(name, args)

    monkeypatch.setattr(runner, "run_procedure", spy)
    reads = mc.load_reads(30, _NOW, mem)
    assert isinstance(reads, mc.ReadCounts), reads
    assert len(seen) >= 2, "the fixture did not force a second page"
    assert seen[1]["since"] != seen[0]["since"] and seen[1]["until"] == seen[0]["until"]
    assert mc.file_read_counts(reads, mem) == {n: planted[n] for n in names}
    assert f"{_LABEL}: 2 of 27 files (index files excluded)" in mc.files_section(mem, reads)


# ── the counting rules, on rows ──────────────────────────────────────────────────────


def _counts(rows: list[dict], directory: Path) -> dict[str, int]:
    return mc.file_read_counts(mc.ReadCounts(mc.read_window(30, _NOW), rows), directory)


def test_a_subagent_load_counts_as_a_read_and_a_write_does_not(tmp_path: Path) -> None:
    mem = tmp_path / "memory"
    mem.mkdir()
    rows = [
        {"kind": "loaded", "name": str(mem / "a.md"), "count": 1},
        {"kind": "file_write", "name": str(mem / "b.md"), "count": 4},
        {"kind": "file_read", "name": str(mem / "c.md"), "count": 2},
        {"kind": "file_read", "name": str(mem / "c.md"), "count": 3},  # a second session
    ]
    assert _counts(rows, mem) == {"a.md": 1, "c.md": 5}


def test_a_same_named_file_in_another_directory_is_not_a_read_of_this_one(tmp_path: Path) -> None:
    mem = tmp_path / "memory"
    other = tmp_path / "other" / "memory"
    mem.mkdir()
    other.mkdir(parents=True)
    rows = [{"kind": "file_read", "name": str(other / "a.md"), "count": 1},
            {"kind": "file_read", "name": "memory/a.md", "count": 1},  # relative: never matched
            {"kind": "file_read", "name": str(mem / "sub" / "a.md"), "count": 1}]
    assert _counts(rows, mem) == {}
    link = tmp_path / "link"
    link.symlink_to(mem)
    assert _counts([{"kind": "file_read", "name": str(link / "a.md"), "count": 1}], mem) == {"a.md": 1}


def test_cut_records_are_reported_as_floors(world: dict[str, Path]) -> None:
    reads = mc.ReadCounts(mc.read_window(30, _NOW), [], truncated_records=3)
    lines = mc.files_section(world["mem"], reads)
    # nexus-qo7ie.27 S6: the --cold wording, since a cut record loses the reads that would have cleared a file
    assert ("read counts: 3 records were cut, so these counts are floors and files used in a cut record may be "
            "listed although they were used") in lines


# ── guards a fixture cannot reach, with a stub procedure ─────────────────────────────


@pytest.fixture
def stubbed(world: dict[str, Path], monkeypatch: pytest.MonkeyPatch):
    _session(world["project"] / "s1.jsonl", "s1", world["repo"], "2026-11-08T10:00:00Z", [("Bash", {"command": "ls"})])
    calls: list[dict] = []

    def install(fn):
        def stub(name: str, args: dict) -> object:
            calls.append(dict(args))
            return fn(args)

        monkeypatch.setattr(runner, "run_procedure", stub)
        return calls

    return install


def test_a_result_that_is_not_a_page_is_not_available(world, stubbed) -> None:
    stubbed(lambda _a: ["not", "a", "page"])
    reads = mc.load_reads(30, _NOW, world["mem"])
    assert isinstance(reads, mc.ReadsUnavailable)
    assert reads.reason == "result: the memory query returned list, not a page"


def test_pages_that_do_not_chain_are_not_available(world, stubbed) -> None:
    """The second page claims to start somewhere other than where the first ended."""

    def pages(a: dict) -> dict:
        if a["since"].endswith("T12:00:00Z") and "next" not in a:
            return {"rows": [], "since": a["since"], "until": a["until"], "next_since": "2026-11-08T13:00:00Z"}
        return {"rows": [], "since": "2026-11-08T14:00:00Z", "until": a["until"]}

    stubbed(pages)
    reads = mc.load_reads(30, _NOW, world["mem"])
    assert isinstance(reads, mc.ReadsUnavailable) and reads.reason.startswith("join: "), reads


def test_a_pager_that_does_not_advance_stops_at_once(world, stubbed) -> None:
    calls = stubbed(lambda a: {"rows": [], "since": a["since"], "until": a["until"], "next_since": a["since"]})
    reads = mc.load_reads(30, _NOW, world["mem"])
    assert isinstance(reads, mc.ReadsUnavailable) and "did not advance" in reads.reason
    assert len(calls) == 1


def test_a_pager_that_never_finishes_stops_at_the_page_cap(world, stubbed, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(mc, "MAX_READ_PAGES", 3)

    def step(a: dict) -> dict:
        t = datetime.strptime(a["since"], "%Y-%m-%dT%H:%M:%SZ") + timedelta(hours=1)
        return {"rows": [], "since": a["since"], "until": a["until"], "next_since": t.strftime("%Y-%m-%dT%H:%M:%SZ")}

    def bounded(a: dict) -> dict:
        # Fails instead of hanging if the cap stops working (a loop with no cap never ends).
        assert len(calls) <= 50, "the page cap did not stop the loop"
        return step(a)

    calls = stubbed(bounded)
    reads = mc.load_reads(30, _NOW, world["mem"])
    assert isinstance(reads, mc.ReadsUnavailable) and "did not finish in 3 pages" in reads.reason
    assert len(calls) == 3


def test_on_windows_the_counts_are_not_available(monkeypatch: pytest.MonkeyPatch) -> None:
    """Checked on every platform: the refusal looks at os.name before any path, and no CI leg
    runs this file on Windows."""
    setattr_in(monkeypatch, mc, "os.name", "nt")  # this module's os only (tests/_module_seam.py)
    reason = mc._unmatchable(Path("/x/memory"))
    assert reason is not None and "Windows paths" in reason
    setattr_in(monkeypatch, mc, "os.name", "posix")
    assert mc._unmatchable(Path("/x/memory")) is None


def test_cut_records_from_the_procedure_reach_the_report(world, stubbed) -> None:
    stubbed(lambda a: {"rows": [], "since": a["since"], "until": a["until"], "truncated_records": 2})
    reads = mc.load_reads(30, _NOW, world["mem"])
    assert isinstance(reads, mc.ReadCounts) and reads.truncated_records == 2


def test_a_project_listing_that_fails_is_not_available(world: dict[str, Path], monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("CLAUDE_CONFIG_DIR", str(world["config"] / "nowhere"))
    reads = mc.load_reads(30, _NOW, world["mem"])
    assert isinstance(reads, mc.ReadsUnavailable) and reads.reason.startswith("no Claude Code projects directory at ")


def test_a_symlinked_project_directory_is_not_counted_as_a_session(world: dict[str, Path], tmp_path: Path) -> None:
    """The verb reads plain directories only, so a session in a linked one is invisible to the
    query; the guard must not count it either, or an empty window would read as all unread."""
    real = tmp_path / "elsewhere" / "proj"
    _session(real / "s1.jsonl", "s1", world["repo"], "2026-11-08T10:00:00Z", [("Bash", {"command": "ls"})])
    (world["config"] / "projects" / "-linked").symlink_to(real, target_is_directory=True)
    reads = mc.load_reads(30, _NOW, world["mem"])
    assert isinstance(reads, mc.ReadsUnavailable) and reads.reason == "no session of this repository started in the window"


def test_the_command_help_names_the_title_of_every_kind_of_run() -> None:
    """nexus-qo7ie.27: the help said only that a partial run takes a suffix; a --cold run does too."""
    doc = " ".join((memory_cmd.curate_cmd.help or "").split())
    assert "curation-report-<date>-<sections>" in doc and "-files-t2-agents-cold" in doc
    assert "a default run never replaces a cold one" in doc
