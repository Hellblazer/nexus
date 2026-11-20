# SPDX-License-Identifier: AGPL-3.0-or-later
# Copyright (c) 2026 Hal Hildebrand. All rights reserved.
"""Tests for ``nexus.hooks.session_context`` -- the SessionStart context hook (RDR-215 bead nexus-q02nx.21).

Since the SessionStart progressive-disclosure change (T2 nexus/design-sessionstart-progressive-disclosure-2026-11-20)
the hook prints only what is a rule or about this session now: the T1 contract, the threads due, the one identity-notes
line, one static index line naming ``nx brief <topic>``, and the hygiene block. The T2 memory scan, ready beads, the bd
primer, the capabilities block and the Knowledge Map moved behind ``nx brief`` (:mod:`nexus.brief`), rendered by the
same functions in this module; the topic tests live in ``tests/test_brief.py``.

Carries the script-level hygiene and ready-beads/capabilities assertions across by re-driving them against the module
(the plugin script and its differential tests were deleted at nexus-z9cz2), and adds the ``run()`` composition tests: the
``HookResult`` contract (``stdout=None`` vs a joined string), what the hook prints, and that it prints none of what moved.
"""
from __future__ import annotations

import hashlib
import os
import subprocess
import time as _time
from pathlib import Path

import pytest

from nexus._hook_runtime._io import HookResult
from nexus.hooks import board_due, identity_notes, session_context, t2_prefix_scan

# -- Carried: the plugin script's hygiene tests, retargeted ------------------


class TestHygieneBlockCarried:
    def test_silent_when_cache_fresh(self, tmp_path: Path) -> None:
        cache = tmp_path / "cache.txt"
        cache.write_text("## Knowledge Map\nfresh\n", encoding="utf-8")
        lines: list[str] = []
        session_context._emit_hygiene_block(lines, str(cache))
        assert lines == []

    def test_silent_when_cache_path_missing(self, tmp_path: Path) -> None:
        lines: list[str] = []
        session_context._emit_hygiene_block(lines, str(tmp_path / "nonexistent.txt"))
        assert lines == []

    def test_silent_when_cache_path_none(self) -> None:
        lines: list[str] = []
        session_context._emit_hygiene_block(lines, None)
        assert lines == []

    def test_emits_warning_when_cache_stale(self, tmp_path: Path) -> None:
        cache = tmp_path / "cache.txt"
        cache.write_text("## Knowledge Map\nstale\n", encoding="utf-8")
        ten_days_ago = _time.time() - (10 * 86400)
        os.utime(cache, (ten_days_ago, ten_days_ago))

        lines: list[str] = []
        session_context._emit_hygiene_block(lines, str(cache))

        assert "## Hygiene" in lines
        signal_lines = [ln for ln in lines if ln.startswith("- L1 cache")]
        assert len(signal_lines) == 1
        assert "10d old" in signal_lines[0]
        assert "nx context refresh" in signal_lines[0]

    def test_threshold_is_exactly_7_days(self, tmp_path: Path) -> None:
        cache = tmp_path / "cache.txt"
        cache.write_text("x\n", encoding="utf-8")

        seven = _time.time() - (7 * 86400)
        os.utime(cache, (seven, seven))
        lines: list[str] = []
        session_context._emit_hygiene_block(lines, str(cache))
        assert lines == [], "7 days exactly should not trigger"

        eight = _time.time() - (8 * 86400)
        os.utime(cache, (eight, eight))
        lines = []
        session_context._emit_hygiene_block(lines, str(cache))
        assert any("L1 cache" in ln for ln in lines), "8 days should trigger"

    def test_block_appends_to_existing_output(self, tmp_path: Path) -> None:
        cache = tmp_path / "cache.txt"
        cache.write_text("x\n", encoding="utf-8")
        old = _time.time() - (15 * 86400)
        os.utime(cache, (old, old))

        lines = ["pre-existing line 1", "pre-existing line 2"]
        session_context._emit_hygiene_block(lines, str(cache))

        assert lines[0] == "pre-existing line 1"
        assert lines[1] == "pre-existing line 2"
        assert "## Hygiene" in lines[2:]

    def test_no_crash_on_unreadable_cache(self, tmp_path: Path) -> None:
        bad = tmp_path / "is_a_dir"
        bad.mkdir()
        lines: list[str] = []
        session_context._emit_hygiene_block(lines, str(bad))
        assert isinstance(lines, list)


# -- Carried: tests/hooks/test_session_start_combined_budget.py, the parts --
# specific to session_start_hook.py's own pure renders (not the whole
# combined-budget census, which is out of this bead's scope).


class TestReadyBeadsAndCapabilitiesCarried:
    _FIXTURE_READY_RAW = "\n".join(
        f"○ nexus-fx{i:03d} ● P1 [bug] Representative ready-bead title padded to "
        f"resemble real bead-title length for the combined-budget fixture, "
        f"item number {i} of the fixture set so each line differs"
        for i in range(15)
    )

    def test_render_ready_beads_caps_at_five_lines_with_overflow_count(self) -> None:
        rendered = session_context._render_ready_beads(self._FIXTURE_READY_RAW)
        assert rendered[0] == "## Ready Beads"
        body = [ln for ln in rendered if ln not in ("## Ready Beads", "```", "")]
        assert len(body) == 6
        assert all(len(ln) <= 160 for ln in body[:5])
        assert "10 more" in body[5]
        assert "bd ready" in body[5]

    def test_render_ready_beads_empty_input_yields_no_section(self) -> None:
        assert session_context._render_ready_beads(None) == []
        assert session_context._render_ready_beads("") == []

    def test_capabilities_block_preserves_every_distinct_token(self) -> None:
        text = "\n".join(session_context._build_capabilities_block())
        for token in (
            "`search`",
            'where="KEY>=VALUE"',
            'cluster_by="semantic"',
            'topic="Label"',
            "`chunk_text_hash`",
            "`query`",
            "`author`",
            "`content_type`",
            "`subtree`",
            "`follow_links`",
            "`depth`",
            "`nx_answer`",
            "`plan_save`",
            "`plan_search`",
            "`scratch`",
            "`links`",
            "`link`",
            "`chash:`",
            "`nx enrich bib COLLECTION`",
            "`nx enrich aspects COLLECTION`",
            "`offset=N`",
            "`mcp__plugin_conexus_nexus__`",
        ):
            assert token in text, f"capabilities block dropped distinct token {token!r}"


# -- run(): the HookResult contract and payload handling ---------------------


class TestRunHookResultContract:
    def _isolate(self, monkeypatch, tmp_path: Path) -> None:
        """No live L1 cache, no CLAUDE_PLUGIN_ROOT, a project dir of our own."""
        monkeypatch.setenv("HOME", str(tmp_path / "home"))
        monkeypatch.setenv("USERPROFILE", str(tmp_path / "home"))
        monkeypatch.setenv("NEXUS_CONFIG_DIR", str(tmp_path / "config"))
        monkeypatch.delenv("CLAUDE_PLUGIN_ROOT", raising=False)
        monkeypatch.setenv("CLAUDE_PROJECT_DIR", str(tmp_path / "project"))
        (tmp_path / "project").mkdir()
        monkeypatch.chdir(tmp_path / "project")

    def test_minimal_output_is_the_t1_contract_then_the_index_line(self, monkeypatch, tmp_path: Path) -> None:
        """nexus-mtheb: the top-level T1 contract leads. Then, with nothing due and no identity, the one index line."""
        self._isolate(monkeypatch, tmp_path)
        result = session_context.run(None)
        assert result.stdout is not None
        assert result.stdout.startswith("## T1: the working layer this session shares with its subagents")
        t1_block = "\n".join(session_context._T1_TOP_LINES)
        assert result.stdout[len(t1_block) + 1:] == session_context.BRIEF_INDEX_LINE
        for gone in ("## T2 Memory", "## Ready Beads", "## nx Capabilities", "## Knowledge Map", "## Hygiene"):
            assert gone not in result.stdout

    def test_the_payload_feeds_only_the_board_due_lookup(self, monkeypatch, tmp_path: Path) -> None:
        """run() reads one payload field, ``session_id``, and only to look up the session's due board threads
        (nexus-ct74m, nexus.hooks.board_due) and its identity's notes line: with those finding nothing, a payload and
        None give the same output. Both lookups are stubbed, so this test reaches no storage (it has no tenant)."""
        seen: list[str] = []
        monkeypatch.setattr(board_due, "due_lines", lambda sid: seen.append(sid) or [])
        monkeypatch.setattr(identity_notes, "notes_lines", lambda sid, **k: [])
        self._isolate(monkeypatch, tmp_path)
        result_none = session_context.run(None)
        result_payload = session_context.run({"session_id": "irrelevant", "source": "startup"})
        assert result_none.stdout == result_payload.stdout
        assert seen == ["irrelevant"]

    def test_hook_result_is_the_dataclass_from_hook_runtime_io(self, monkeypatch, tmp_path: Path) -> None:
        self._isolate(monkeypatch, tmp_path)
        result = session_context.run(None)
        assert isinstance(result, HookResult)
        assert result.crashed is False


class TestRunPrintsNothingThatMoved:
    """The moved blocks are not rendered, and not even computed, at session start: no nx or bd is spawned, no T2 call
    is made, no Knowledge Map is read (the hygiene check stats the cache's mtime and reads nothing)."""

    def test_no_moved_block_is_rendered_or_computed(self, monkeypatch, tmp_path: Path) -> None:
        monkeypatch.setenv("HOME", str(tmp_path / "home"))
        monkeypatch.setenv("NEXUS_CONFIG_DIR", str(tmp_path / "config"))
        monkeypatch.setenv("CLAUDE_PROJECT_DIR", str(tmp_path / "project"))
        (tmp_path / "project").mkdir()
        monkeypatch.chdir(tmp_path / "project")
        monkeypatch.setattr(session_context, "which", lambda cmd: pytest.fail(f"which({cmd!r}) at session start"))
        monkeypatch.setattr(session_context, "run_command", lambda *a, **k: pytest.fail(f"spawned {a!r}"))
        monkeypatch.setattr(t2_prefix_scan, "scan", lambda project: pytest.fail("the T2 scan ran at session start"))
        out = session_context.run(None).stdout or ""
        assert "T2 Memory" not in out and "Ready Beads" not in out and "Capabilities" not in out

    def test_the_hygiene_block_still_fires_on_a_stale_cache_without_reading_it(self, monkeypatch, tmp_path: Path) -> None:
        repo = tmp_path / "fakerepo"
        repo.mkdir()
        config = tmp_path / "config"
        (config / "context").mkdir(parents=True)
        digest = hashlib.sha1(os.path.realpath(repo).encode()).hexdigest()[:8]
        cache = config / "context" / f"fakerepo-{digest}.txt"
        cache.write_text("## Knowledge Map\nMAP-BODY-MARKER\n", encoding="utf-8")
        ten_days_ago = _time.time() - 10 * 86400
        os.utime(cache, (ten_days_ago, ten_days_ago))
        monkeypatch.setenv("NEXUS_CONFIG_DIR", str(config))
        monkeypatch.setenv("CLAUDE_PROJECT_DIR", str(repo))
        monkeypatch.chdir(repo)
        out = session_context.run(None).stdout or ""
        assert "## Hygiene" in out and "10d old" in out and "nx context refresh" in out
        assert "MAP-BODY-MARKER" not in out, "the map itself is not printed"


class TestTheIndexLine:
    def test_it_is_one_static_line_naming_every_topic_with_a_gloss(self) -> None:
        line = session_context.BRIEF_INDEX_LINE
        assert "\n" not in line and len(line.encode("utf-8")) <= 260, len(line.encode("utf-8"))
        assert "nx brief" in line and "`brief`" in line
        assert session_context.BRIEF_TOPICS == ("memory", "beads", "caps", "map", "rdr", "me")
        for topic in session_context.BRIEF_TOPICS:
            assert f"{topic} (" in line, f"{topic} has no gloss in the index line"

    def test_it_is_printed_after_the_identity_notes_line_and_before_hygiene(self, monkeypatch, tmp_path: Path) -> None:
        monkeypatch.setattr(identity_notes, "notes_lines", lambda sid, **k: ["Your notes (identity/i5): a, b"])
        monkeypatch.setattr("nexus.hooks.board_due.due_lines", lambda sid: ["## Due on your threads board (x)", "- X: due 2026-11-14", ""])
        monkeypatch.setenv("NEXUS_CONFIG_DIR", str(tmp_path / "config"))
        monkeypatch.setenv("CLAUDE_PROJECT_DIR", str(tmp_path))
        monkeypatch.chdir(tmp_path)
        out = session_context.run({"session_id": "s1"}).stdout or ""
        assert out.index("## Due on your") < out.index("Your notes (identity/i5)") < out.index(session_context.BRIEF_INDEX_LINE)


# -- the extracted renders: the same text the hook used to print --------------


class TestExtractedRenders:
    def test_t2_memory_arrives_carrying_the_project_name_without_a_plugin_path(self, monkeypatch, tmp_path: Path) -> None:
        repo = tmp_path / "myproject"
        repo.mkdir()
        subprocess.run(["git", "init", "-q", "."], cwd=repo, check=True)
        seen: list[str] = []

        def _fake_scan(project: str, **_kw) -> str:
            seen.append(project)
            return f"  entry for {project}"

        monkeypatch.setattr(t2_prefix_scan, "scan", _fake_scan)
        lines = session_context._render_t2_memory(str(repo))
        assert lines == ["## T2 Memory (Active Project)", "  entry for myproject", ""]
        assert seen == ["myproject"]
        assert not hasattr(session_context, "_plugin_root"), "the hook resolves no plugin path (nexus-b5ugt)"

    def test_t2_memory_is_empty_when_there_is_no_repo_or_nothing_scanned(self, monkeypatch, tmp_path: Path) -> None:
        monkeypatch.setattr(t2_prefix_scan, "scan", lambda project, **kw: "")
        monkeypatch.setattr(session_context, "run_command", lambda *a, **k: None)
        assert session_context._render_t2_memory(str(tmp_path)) == []
        monkeypatch.setattr(session_context, "run_command", lambda *a, **k: "/repo/p")
        assert session_context._render_t2_memory(str(tmp_path)) == []

    def test_beads_is_ready_then_prime_and_empty_without_bd(self, monkeypatch, tmp_path: Path) -> None:
        monkeypatch.setattr(session_context, "which", lambda cmd: False)
        assert session_context._render_beads(tmp_path) == []
        monkeypatch.setattr(session_context, "which", lambda cmd: cmd == "bd")
        monkeypatch.setattr(session_context, "run_command", lambda args, timeout, cwd=None: "○ nexus-abc123 ● P1 [bug] a ready bead")
        text = "\n".join(session_context._render_beads(tmp_path))
        assert text.startswith("## Ready Beads") and "a ready bead" in text

    def test_knowledge_map_reads_the_cache_and_names_its_path(self, monkeypatch, tmp_path: Path) -> None:
        repo = tmp_path / "fakerepo"
        repo.mkdir()
        config = tmp_path / "config"
        (config / "context").mkdir(parents=True)
        digest = hashlib.sha1(os.path.realpath(repo).encode()).hexdigest()[:8]
        cache = config / "context" / f"fakerepo-{digest}.txt"
        cache.write_text("## Knowledge Map\n\ncode: Topic (7)\n", encoding="utf-8")
        monkeypatch.setenv("NEXUS_CONFIG_DIR", str(config))
        assert session_context._knowledge_map_path(str(repo)) == str(cache)
        assert session_context._render_knowledge_map(str(repo)) == ["## Knowledge Map\n\ncode: Topic (7)", ""]
        assert session_context._render_knowledge_map(str(tmp_path)) == []
