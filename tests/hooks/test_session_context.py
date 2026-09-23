# SPDX-License-Identifier: AGPL-3.0-or-later
# Copyright (c) 2026 Hal Hildebrand. All rights reserved.
"""Tests for ``nexus.hooks.session_context`` -- the SessionStart
T2-memory/beads/capabilities/knowledge-map hook ported onto the
command-tier dispatch mechanism (RDR-215 bead nexus-q02nx.21).

Carries the script-level hygiene and ready-beads/capabilities assertions
across by re-driving them against the NEW module (the plugin script and
its differential tests were deleted at nexus-z9cz2), and adds:

* **``run()`` composition tests** -- the T2/beads/capabilities/knowledge-map
  assembly order, the ``HookResult`` contract (``stdout=None`` vs a joined
  string), and that ``payload`` is accepted and ignored (the script never
  reads stdin).
* **In-process T2 section tests** -- the T2 Memory section comes from
  ``nexus.hooks.t2_prefix_scan.scan()`` in-process (bead nexus-b5ugt), so
  no plugin path is resolved at all.
"""
from __future__ import annotations

import os
import subprocess
import sys
import time as _time
from pathlib import Path

from nexus._hook_runtime._io import HookResult
from nexus.hooks import session_context, t2_prefix_scan

# -- Carried: the plugin script's hygiene tests, retargeted ------------------


class TestHygieneBlockCarried:
    def test_silent_when_cache_fresh(self, tmp_path: Path) -> None:
        cache = tmp_path / "cache.txt"
        cache.write_text("## Knowledge Map\nfresh\n")
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
        cache.write_text("## Knowledge Map\nstale\n")
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
        cache.write_text("x\n")

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
        cache.write_text("x\n")
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
            "`/conexus:query`",
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
        """No live L1 cache, no CLAUDE_PLUGIN_ROOT, both nx/bd unavailable."""
        monkeypatch.setattr(session_context, "which", lambda cmd: False)
        monkeypatch.setenv("HOME", str(tmp_path / "home"))
        monkeypatch.delenv("CLAUDE_PLUGIN_ROOT", raising=False)
        monkeypatch.setenv("CLAUDE_PROJECT_DIR", str(tmp_path / "project"))
        (tmp_path / "project").mkdir()

    def test_minimal_output_is_just_the_capabilities_block(self, monkeypatch, tmp_path: Path) -> None:
        self._isolate(monkeypatch, tmp_path)
        result = session_context.run(None)
        assert result.exit_code == 0
        assert result.stdout is not None
        assert result.stdout.startswith("## nx Capabilities")
        assert "## T2 Memory" not in result.stdout
        assert "## Ready Beads" not in result.stdout
        assert "## Hygiene" not in result.stdout

    def test_payload_is_accepted_and_ignored(self, monkeypatch, tmp_path: Path) -> None:
        """The script never reads stdin -- run() must produce identical
        output for None and for an arbitrary payload dict."""
        self._isolate(monkeypatch, tmp_path)
        result_none = session_context.run(None)
        result_payload = session_context.run({"session_id": "irrelevant", "source": "startup"})
        assert result_none.stdout == result_payload.stdout
        assert result_none.exit_code == result_payload.exit_code == 0

    def test_hook_result_is_the_dataclass_from_hook_runtime_io(self, monkeypatch, tmp_path: Path) -> None:
        self._isolate(monkeypatch, tmp_path)
        result = session_context.run(None)
        assert isinstance(result, HookResult)
        assert result.crashed is False


class TestRunComposition:
    """T2 memory / ready beads sections, when present, in the script's own
    assembly order: T2 memory, then ready beads, then capabilities."""

    def test_t2_memory_and_ready_beads_sections_appear_in_order(
        self, monkeypatch, tmp_path: Path
    ) -> None:
        monkeypatch.setenv("HOME", str(tmp_path / "home"))
        monkeypatch.delenv("CLAUDE_PLUGIN_ROOT", raising=False)
        monkeypatch.setenv("CLAUDE_PROJECT_DIR", str(tmp_path / "project"))
        (tmp_path / "project").mkdir()

        monkeypatch.setattr(session_context, "which", lambda cmd: cmd in ("nx", "bd"))

        calls: list[list[str]] = []

        def _fake_run_command(args, timeout, cwd=None):
            calls.append(args)
            if args[:2] == ["git", "rev-parse"]:
                return "/repo/myproject"
            if args[0] == "bd":
                return "○ nexus-abc123 ● P1 [bug] a ready bead"
            return ""

        monkeypatch.setattr(session_context, "run_command", _fake_run_command)
        # The T2 body arrives in-process now (bead nexus-b5ugt), not from
        # a subprocess, so it is stubbed where it is actually called.
        scanned: list[str] = []

        def _fake_scan(project: str) -> str:
            scanned.append(project)
            return "  fixture-t2-entry — a snippet"

        monkeypatch.setattr(t2_prefix_scan, "scan", _fake_scan)

        result = session_context.run(None)
        assert result.stdout is not None
        t2_idx = result.stdout.index("## T2 Memory (Active Project)")
        beads_idx = result.stdout.index("## Ready Beads")
        caps_idx = result.stdout.index("## nx Capabilities")
        assert t2_idx < beads_idx < caps_idx
        assert "fixture-t2-entry" in result.stdout
        assert "a ready bead" in result.stdout

        # The T2 body came from scan(), called with the project name the
        # hook derived from git. This replaces an assertion on the old
        # subprocess argv (sys.executable + the resolved sibling script);
        # bead nexus-b5ugt removed the subprocess, and the claim worth
        # keeping was always which project got scanned, not how.
        assert scanned == ["myproject"]
        assert not [c for c in calls if c and c[0] == sys.executable], (
            "session_context spawned a python subprocess for T2 again"
        )

    def test_nx_not_found_skips_t2_memory_but_not_capabilities(
        self, monkeypatch, tmp_path: Path
    ) -> None:
        monkeypatch.setenv("HOME", str(tmp_path / "home"))
        monkeypatch.delenv("CLAUDE_PLUGIN_ROOT", raising=False)
        monkeypatch.setenv("CLAUDE_PROJECT_DIR", str(tmp_path / "project"))
        (tmp_path / "project").mkdir()
        monkeypatch.setattr(session_context, "which", lambda cmd: False)

        result = session_context.run(None)
        assert result.stdout is not None
        assert "## T2 Memory" not in result.stdout
        assert "## nx Capabilities" in result.stdout


# -- _plugin_root(): the one unavoidable deviation from a literal carry -----


class TestTheT2SectionIsProducedInProcess:
    """The T2 Memory section reaches run()'s output, without a subprocess.

    This replaces TestPluginRootResolution, which pinned three facts
    about LOCATING conexus/hooks/scripts/t2_prefix_scan.py: that the env
    var won, that the dev-checkout fallback found the real sibling, and
    that the plumbing reached a real subprocess. Bead nexus-b5ugt
    removed the thing all three described -- session_context calls
    nexus.hooks.t2_prefix_scan.scan() in-process and resolves no plugin
    path at all, so _plugin_root has no callers and is gone.

    Deleting them outright would have dropped the one claim worth
    keeping: that the section actually arrives, carrying the project
    name. run_command swallows a failure into None, so a broken T2 path
    produces no error and no section, forever -- which is exactly how
    the defect this bead fixes stayed invisible. That claim is kept
    here, against the seam that exists now.
    """

    def test_the_section_arrives_carrying_the_project_name(
        self, monkeypatch, tmp_path: Path
    ) -> None:
        monkeypatch.setenv("HOME", str(tmp_path / "home"))

        # A repo this test OWNS, rather than whatever checkout pytest
        # happens to run from. The first draft took the project name from
        # `git rev-parse --show-toplevel` in the ambient cwd, which made
        # the test silently dependent on being run from inside a git
        # repository -- it failed the moment the suite was run from a
        # scratch directory to reproduce CI's environment. The hook's job
        # is to derive the name from the repo it is pointed at; the test
        # should point it at one.
        repo = tmp_path / "myproject"
        repo.mkdir()
        subprocess.run(["git", "init", "-q", "."], cwd=repo, check=True)
        monkeypatch.setenv("CLAUDE_PROJECT_DIR", str(repo))
        monkeypatch.setattr(session_context, "which", lambda cmd: cmd == "nx")
        project_name = "myproject"

        seen: list[str] = []

        def _fake_scan(project: str) -> str:
            seen.append(project)
            return f"## T2 Memory (Active Project) FAKE for {project}"

        monkeypatch.setattr(t2_prefix_scan, "scan", _fake_scan)

        result = session_context.run(None)
        assert result.stdout is not None
        assert f"FAKE for {project_name}" in result.stdout
        assert seen == [project_name], (
            f"scan() was called with {seen}, not the repo name the hook "
            f"derived from git"
        )

    def test_the_hook_resolves_no_plugin_path_at_all(self) -> None:
        """The property that made the three old tests unnecessary.

        Asserted on the module attribute, not on its source text: the
        first draft of this grepped for "_plugin_root" and matched a
        stale docstring reference, which is the same grep-cannot-tell-an
        -identifier-from-a-sentence error this bead had just fixed in
        test_plugin_sibling_resolution's own scan.
        """
        assert not hasattr(session_context, "_plugin_root"), (
            "session_context resolves a plugin path again; if that is "
            "deliberate, put it back in test_plugin_sibling_resolution's "
            "RESOLVERS so the env-var contract is pinned for it"
        )

