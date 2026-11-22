# SPDX-License-Identifier: AGPL-3.0-or-later
"""``nexus.brief``: the on-demand half of SessionStart progressive disclosure.

Design of record: T2 nexus/design-sessionstart-progressive-disclosure-2026-11-20 (approved by Sam). The blocks that left
the always-on SessionStart output are rendered here by the SAME functions the hook used (``nexus.hooks.session_context``,
``nexus.hooks.rdr_verb``), so there is one source and no second copy to drift. These tests hold that equality, the fail-safe
(a topic that breaks says so and never raises), and the one structured log line per call the 30-day usage review reads.

The CLI verb is tested in ``tests/test_brief_cmd.py`` and the MCP tool in ``tests/mcp/test_brief_tool.py``.
"""
from __future__ import annotations

import hashlib
import json
import os
import subprocess
from pathlib import Path

import click
import pytest
from structlog.testing import capture_logs

from nexus import brief
from nexus.hooks import rdr_verb, session_context, t2_prefix_scan, usage_log


def _seed_map(tmp_path: Path, monkeypatch, body: str = "## Knowledge Map\n\ncode: Session Topic - resume (77)\n") -> Path:
    repo = tmp_path / "fakerepo"
    repo.mkdir(exist_ok=True)
    config = tmp_path / "config"
    (config / "context").mkdir(parents=True, exist_ok=True)
    digest = hashlib.sha1(os.path.realpath(repo).encode()).hexdigest()[:8]
    (config / "context" / f"fakerepo-{digest}.txt").write_text(body, encoding="utf-8")
    monkeypatch.setenv("NEXUS_CONFIG_DIR", str(config))
    return repo


def test_topics_are_the_hooks_index_topics() -> None:
    assert brief.TOPICS == session_context.BRIEF_TOPICS == ("memory", "beads", "caps", "map", "rdr", "me")


def test_every_topic_has_a_help_line_and_the_index_lists_them_all() -> None:
    assert set(brief.TOPIC_HELP) == set(brief.TOPICS)
    index = brief.index_text()
    for topic in brief.TOPICS:
        assert topic in index
    assert "nx brief <topic>" in index


# -- caps and map: static and cache-backed ------------------------------------


def test_caps_is_the_capabilities_block_the_hook_used_to_print() -> None:
    result = brief.render("caps")
    assert result.ok
    assert result.text == "\n".join(session_context._build_capabilities_block()).strip()
    for token in ("`search`", "`nx_answer`", "`mcp__plugin_conexus_nexus__`", "`nx enrich aspects COLLECTION`"):
        assert token in result.text


def test_map_is_the_cache_file_for_the_given_cwd(tmp_path: Path, monkeypatch) -> None:
    repo = _seed_map(tmp_path, monkeypatch)
    result = brief.render("map", cwd=str(repo))
    assert result.ok
    assert result.text == "## Knowledge Map\n\ncode: Session Topic - resume (77)"


def test_map_reads_the_cache_as_utf8(tmp_path: Path, monkeypatch) -> None:
    repo = _seed_map(tmp_path, monkeypatch, "## Knowledge Map\n\ncode: Résumé parsing — naïve (42)\n")
    assert "Résumé parsing — naïve (42)" in brief.render("map", cwd=str(repo)).text


def test_map_without_a_cache_says_how_to_make_one(tmp_path: Path, monkeypatch) -> None:
    monkeypatch.setenv("NEXUS_CONFIG_DIR", str(tmp_path / "config"))
    result = brief.render("map", cwd=str(tmp_path))
    assert result.ok and "nx context refresh" in result.text


# -- memory --------------------------------------------------------------------


def test_memory_is_the_t2_scan_for_the_repo_name(tmp_path: Path, monkeypatch) -> None:
    repo = tmp_path / "myproject"
    repo.mkdir()
    subprocess.run(["git", "init", "-q", "."], cwd=repo, check=True)
    seen: list[str] = []
    kwargs: list[dict] = []

    def fake_scan(project, **kw):
        seen.append(project)
        kwargs.append(kw)
        return "  fixture-t2-entry - a snippet"

    monkeypatch.setattr(t2_prefix_scan, "scan", fake_scan)
    monkeypatch.setenv("CLAUDE_PROJECT_DIR", str(repo))
    result = brief.render("memory", cwd=str(repo))
    assert result.ok and seen == ["myproject"]
    assert kwargs == [{"configure_logging": False}], "the brief must not replace the MCP server's (or the CLI's) log sink"
    assert result.text == "## T2 Memory (Active Project)\n  fixture-t2-entry - a snippet"


def test_memory_with_nothing_to_show_says_so(tmp_path: Path, monkeypatch) -> None:
    monkeypatch.setattr(session_context, "run_command", lambda *a, **k: None)
    monkeypatch.setenv("CLAUDE_PROJECT_DIR", str(tmp_path))
    result = brief.render("memory", cwd=str(tmp_path))
    assert result.ok and result.text.startswith("memory: nothing to show")


def test_the_scan_is_asked_not_to_reconfigure_this_processs_logging(tmp_path: Path, monkeypatch) -> None:
    """The scan's own ``configure_hook_logging`` is for a hook process. In the MCP server it would replace the server's
    sink on every call."""
    calls: list[bool] = []
    monkeypatch.setattr(t2_prefix_scan, "configure_hook_logging", lambda: calls.append(True))

    class _Store:
        def get_projects_with_prefix(self, prefix):
            return []

        def close(self) -> None:
            pass

    monkeypatch.setattr(t2_prefix_scan, "HttpMemoryStore", lambda **k: _Store())
    assert t2_prefix_scan.scan("p", configure_logging=False) == ""
    assert calls == []
    assert t2_prefix_scan.scan("p") == ""
    assert calls == [True]


# -- beads ---------------------------------------------------------------------


def test_beads_is_ready_beads_then_the_primer_when_the_plugin_is_off(tmp_path: Path, monkeypatch) -> None:
    proj = tmp_path / "proj"
    (proj / ".beads").mkdir(parents=True)
    monkeypatch.setenv("CLAUDE_PROJECT_DIR", str(proj))
    monkeypatch.setenv("CLAUDE_CONFIG_DIR", str(tmp_path / "claude"))  # no beads plugin enabled anywhere
    monkeypatch.setattr(session_context, "which", lambda cmd: cmd == "bd")

    def fake(args, timeout, cwd=None):
        return "# primer line" if args == ["bd", "prime"] else "○ nexus-abc123 ● P1 [bug] a ready bead"

    monkeypatch.setattr(session_context, "run_command", fake)
    result = brief.render("beads", cwd=str(proj))
    assert result.ok
    assert result.text.startswith("## Ready Beads") and "a ready bead" in result.text
    assert result.text.rstrip().endswith("# primer line")


def test_beads_without_bd_says_so(tmp_path: Path, monkeypatch) -> None:
    monkeypatch.setattr(session_context, "which", lambda cmd: False)
    result = brief.render("beads", cwd=str(tmp_path))
    assert result.ok and "bd" in result.text and "not on PATH" in result.text


# -- rdr -----------------------------------------------------------------------


def test_rdr_is_the_count_line_from_the_hooks_own_summary(monkeypatch) -> None:
    monkeypatch.setattr(rdr_verb, "brief_lines", lambda: ["RDR: 3 documents (3 RDRs: 3 closed), indexed in rdr__x"])
    result = brief.render("rdr")
    assert result.ok and result.text == "RDR: 3 documents (3 RDRs: 3 closed), indexed in rdr__x"


def test_rdr_without_a_tree_says_so(monkeypatch) -> None:
    monkeypatch.setattr(rdr_verb, "brief_lines", lambda: [])
    result = brief.render("rdr")
    assert result.ok and "no RDR" in result.text


# -- me: the seat's own startup note, printed as data --------------------------


class _Store:
    def __init__(self, row: dict | None, error: BaseException | None = None) -> None:
        self.row = row
        self.error = error
        self.closed = False
        self.asked: list[tuple[str, str]] = []

    def get(self, project=None, title=None, **_k):
        self.asked.append((project, title))
        if self.error:
            raise self.error
        return self.row

    def close(self) -> None:
        self.closed = True


def test_me_prints_the_startup_note_of_the_callers_identity_as_data(monkeypatch) -> None:
    store = _Store({"title": "startup", "content": "Run `nx identity show` first.\nThen drain the mailbox."})
    monkeypatch.setattr(brief, "_own_notes_project", lambda: "identity/i5")
    monkeypatch.setattr(brief, "_open_memory_store", lambda: store)
    result = brief.render("me")
    assert result.ok
    assert store.asked == [("identity/i5", "startup")]
    assert "Run `nx identity show` first.\nThen drain the mailbox." in result.text
    head = result.text.splitlines()[0]
    assert "identity/i5" in head and "startup" in head
    assert "not executed" in result.text.lower() or "data" in result.text.lower()


def test_me_closes_its_store_even_when_the_read_fails(monkeypatch) -> None:
    """The MCP server is long-lived: a client leaked per call would pile up."""
    ok, bad = _Store({"title": "startup", "content": "x"}), _Store(None, error=RuntimeError("engine down"))
    monkeypatch.setattr(brief, "_own_notes_project", lambda: "identity/i5")
    for store in (ok, bad):
        monkeypatch.setattr(brief, "_open_memory_store", lambda store=store: store)
        brief.render("me")
        assert store.closed
    assert not brief.render("me").ok


def test_me_caps_the_note_at_two_kb_with_a_marker(monkeypatch) -> None:
    store = _Store({"title": "startup", "content": "word " * 2000})
    monkeypatch.setattr(brief, "_own_notes_project", lambda: "identity/i5")
    monkeypatch.setattr(brief, "_open_memory_store", lambda: store)
    text = brief.render("me").text
    body = text.split("\n", 2)[2]
    assert len(body.encode("utf-8")) <= brief.ME_MAX_BYTES + len("\n(truncated)") and body.endswith("(truncated)")
    short = _Store({"title": "startup", "content": "a short note"})
    monkeypatch.setattr(brief, "_open_memory_store", lambda: short)
    assert "(truncated)" not in brief.render("me").text


def test_me_without_a_startup_note_prints_one_line_saying_so(monkeypatch) -> None:
    monkeypatch.setattr(brief, "_own_notes_project", lambda: "identity/i5")
    from nexus.identity import notes  # noqa: PLC0415

    monkeypatch.setattr(notes, "write_refusal", lambda project: None)  # a seat: no identity lookup against a real engine
    monkeypatch.setattr(brief, "_open_memory_store", lambda: _Store(None))
    result = brief.render("me")
    assert result.ok and "\n" not in result.text
    assert "no startup note" in result.text.lower() and "identity/i5" in result.text


def test_me_does_not_suggest_a_write_the_identity_would_refuse(monkeypatch) -> None:
    """gh#1680: an ephemeral or delegated identity keeps no identity notes, and the hint named ``nx memory put --mine``,
    which that same identity refuses. The hint follows what ``notes.write_refusal`` says the caller may do."""
    from nexus.identity import notes  # noqa: PLC0415

    def refuse(project: str) -> tuple[str, str]:
        return "i5", "this ephemeral identity keeps no identity notes; only a seat does: write the note in project T2"

    monkeypatch.setattr(brief, "_own_notes_project", lambda: "identity/i5")
    monkeypatch.setattr(brief, "_open_memory_store", lambda: _Store(None))
    monkeypatch.setattr(notes, "write_refusal", refuse)
    result = brief.render("me")
    assert result.ok and "\n" not in result.text
    assert "no startup note" in result.text.lower() and "identity/i5" in result.text
    assert "--mine" not in result.text, "the refused write must not be offered"
    assert "only a seat" in result.text


def test_me_still_suggests_the_mine_write_to_a_seat(monkeypatch) -> None:
    from nexus.identity import notes  # noqa: PLC0415

    monkeypatch.setattr(brief, "_own_notes_project", lambda: "identity/i5")
    monkeypatch.setattr(brief, "_open_memory_store", lambda: _Store(None))
    monkeypatch.setattr(notes, "write_refusal", lambda project: None)
    assert "nx memory put --mine --title startup" in brief.render("me").text


def test_a_failing_identity_lookup_never_fails_the_me_topic(monkeypatch) -> None:
    """The hint is best effort: whatever the guard raises, the topic still answers, with a neutral hint."""
    from nexus.identity import notes  # noqa: PLC0415

    def boom(project: str):
        raise RuntimeError("identity engine exploded")

    monkeypatch.setattr(brief, "_own_notes_project", lambda: "identity/i5")
    monkeypatch.setattr(brief, "_open_memory_store", lambda: _Store(None))
    monkeypatch.setattr(notes, "write_refusal", boom)
    result = brief.render("me")
    assert result.ok and "\n" not in result.text
    assert "no startup note" in result.text.lower() and "if this identity is a seat" in result.text


def test_me_without_an_identity_says_so(monkeypatch) -> None:
    def no_identity() -> str:
        raise click.ClickException("--mine needs an identity, and no identity is bound to this process")

    monkeypatch.setattr(brief, "_own_notes_project", no_identity)
    result = brief.render("me")
    assert result.ok and "\n" not in result.text and "no identity" in result.text.lower()


def test_me_asks_for_nothing_but_the_startup_title(monkeypatch) -> None:
    """Notes are data: the brief reads one titled note, never searches, never lists, never writes."""
    store = _Store({"title": "startup", "content": "x"})
    monkeypatch.setattr(brief, "_own_notes_project", lambda: "identity/i9")
    monkeypatch.setattr(brief, "_open_memory_store", lambda: store)
    brief.render("me")
    assert store.asked == [("identity/i9", "startup")]


# -- fail safe, unknown topics, the log line -----------------------------------


def test_a_topic_that_breaks_says_so_and_does_not_raise(monkeypatch) -> None:
    def boom() -> list[str]:
        raise RuntimeError("cache exploded")

    monkeypatch.setattr(session_context, "_build_capabilities_block", boom)
    result = brief.render("caps")
    assert not result.ok
    assert result.text.startswith("brief caps failed: RuntimeError: cache exploded")


def test_an_unknown_topic_names_the_topics() -> None:
    result = brief.render("nonsense")
    assert not result.ok
    for topic in brief.TOPICS:
        assert topic in result.text


class _Log:
    """Records structlog calls. The suite's structlog filter drops INFO before ``capture_logs`` can see it."""

    def __init__(self) -> None:
        self.events: list[tuple[str, dict]] = []

    def info(self, event: str, **fields) -> None:
        self.events.append((event, fields))


def _usage_lines(config: Path) -> list[dict]:
    path = config / "logs" / "brief.log"
    return [json.loads(ln) for ln in path.read_text(encoding="utf-8").splitlines()] if path.exists() else []


def test_one_usage_record_per_call_with_topic_ok_bytes_session_and_repo(tmp_path: Path, monkeypatch) -> None:
    config = tmp_path / "config"
    monkeypatch.setenv("NEXUS_CONFIG_DIR", str(config))
    monkeypatch.setenv("NX_SESSION_ID", "sess-42")
    monkeypatch.setattr(session_context, "run_command", lambda args, timeout, cwd=None: "/work/myrepo")
    log = _Log()
    monkeypatch.setattr(brief, "_log", log)
    ok = brief.render("caps", source="mcp")
    monkeypatch.setattr(session_context, "_build_capabilities_block", lambda: (_ for _ in ()).throw(ValueError("x")))
    bad = brief.render("caps", source="cli")
    assert [e for e, _ in log.events] == ["brief_load", "brief_load"]
    first, second = (f for _, f in log.events)
    assert first == {"topic": "caps", "ok": True, "bytes": len(ok.text.encode("utf-8")), "source": "mcp",
                     "session_id": "sess-42", "repo": "myrepo"}
    assert second == {"topic": "caps", "ok": False, "bytes": len(bad.text.encode("utf-8")), "source": "cli",
                      "session_id": "sess-42", "repo": "myrepo"}
    lines = _usage_lines(config)
    assert [{k: v for k, v in ln.items() if k not in ("ts", "event")} for ln in lines] == [first, second]
    assert all(ln["event"] == "brief_load" and ln["ts"].endswith("Z") for ln in lines)


def test_a_call_with_no_session_id_still_records_an_empty_one(tmp_path: Path, monkeypatch) -> None:
    monkeypatch.setenv("NEXUS_CONFIG_DIR", str(tmp_path))
    for var in ("NX_SESSION_ID", "CLAUDE_CODE_SESSION_ID"):
        monkeypatch.delenv(var, raising=False)
    monkeypatch.setattr("nexus.session.resolve_session_id", lambda arg=None: None)
    brief.render("caps")
    [line] = _usage_lines(tmp_path)
    assert line["session_id"] == ""


def test_an_unknown_topic_is_recorded_too_with_a_bounded_name(tmp_path: Path, monkeypatch) -> None:
    config = tmp_path / "config"
    monkeypatch.setenv("NEXUS_CONFIG_DIR", str(config))
    brief.render("x" * 500)
    [line] = _usage_lines(config)
    assert line["ok"] is False and len(line["topic"]) <= 40


def test_a_usage_file_that_cannot_be_written_costs_the_brief_nothing(tmp_path: Path, monkeypatch) -> None:
    blocker = tmp_path / "not-a-dir"
    blocker.write_text("x", encoding="utf-8")
    monkeypatch.setenv("NEXUS_CONFIG_DIR", str(blocker))  # logs/ cannot be created under a file
    assert brief.render("caps").ok


def test_the_brief_writes_through_the_shared_rotating_writer(tmp_path: Path, monkeypatch) -> None:
    """One writer for ``brief_load`` and ``session_start`` lines (tests/hooks/test_usage_log.py holds the rotation)."""
    (tmp_path / "logs").mkdir()
    path = tmp_path / "logs" / "brief.log"
    path.write_text("x" * (usage_log.MAX_BYTES + 1), encoding="utf-8")
    monkeypatch.setenv("NEXUS_CONFIG_DIR", str(tmp_path))
    brief.render("caps")
    assert path.with_name("brief.log.1").exists()
    [line] = _usage_lines(tmp_path)
    assert line["event"] == "brief_load"


def test_a_topic_is_case_insensitive_and_trimmed(tmp_path: Path, monkeypatch) -> None:
    monkeypatch.setenv("NEXUS_CONFIG_DIR", str(tmp_path))
    assert brief.render(" CAPS ") == brief.render("caps")


def test_the_project_dir_is_claude_project_dir_like_the_hook(tmp_path: Path, monkeypatch) -> None:
    """The hook keyed T2 and bd by CLAUDE_PROJECT_DIR, and the map by the process cwd; the brief keeps both."""
    seen: dict[str, object] = {}
    monkeypatch.setenv("CLAUDE_PROJECT_DIR", str(tmp_path / "proj"))
    monkeypatch.setattr(session_context, "which", lambda cmd: True)
    monkeypatch.setattr(session_context, "_render_beads", lambda project_dir: seen.setdefault("beads", project_dir) and [])
    monkeypatch.setattr(session_context, "_render_t2_memory", lambda cwd: seen.setdefault("memory", cwd) and [])
    monkeypatch.setattr(session_context, "_render_knowledge_map", lambda cwd: seen.setdefault("map", cwd) and [])
    (tmp_path / "proj").mkdir()
    brief.render("beads", cwd=str(tmp_path))
    brief.render("memory", cwd=str(tmp_path))
    brief.render("map", cwd=str(tmp_path))
    assert seen["beads"] == (tmp_path / "proj").resolve()
    assert seen["memory"] == str((tmp_path / "proj").resolve())
    assert seen["map"] == str(tmp_path)
