# SPDX-License-Identifier: AGPL-3.0-or-later
# Copyright (c) 2026 Hal Hildebrand. All rights reserved.
"""Tests for ``nexus.hooks.session_context`` -- the SessionStart
T2-memory/beads/capabilities/knowledge-map hook ported onto the
command-tier dispatch mechanism (RDR-215 bead nexus-q02nx.21).

Carries the two existing script-level test files across
(``tests/hooks/test_session_start_hygiene.py``,
``tests/hooks/test_session_start_combined_budget.py``) by re-driving their
pure-function assertions against the NEW module, and adds:

* **Differential tests** against the still-wired script (imported via the
  same ``sys.path`` injection those two existing files already use) for
  every pure helper the port carries -- each assertion is a check that the
  port did not silently drift from what production still runs.
* **``run()`` composition tests** -- the T2/beads/capabilities/knowledge-map
  assembly order, the ``HookResult`` contract (``stdout=None`` vs a joined
  string), and that ``payload`` is accepted and ignored (the script never
  reads stdin).
* **``_plugin_root()`` resolution tests** -- this is the one place the port
  could not be a literal line-for-line carry: the script resolved its
  sibling ``t2_prefix_scan.py`` via ``Path(__file__).parent`` because both
  files lived in ``conexus/hooks/scripts/`` together; this module now lives
  in ``src/nexus/hooks/`` and the sibling has not moved (a separate bead's
  scope). One test proves the dev-checkout fallback still finds the REAL
  sibling script on disk; another proves ``CLAUDE_PLUGIN_ROOT`` is honored
  end-to-end through a real (but fake-content) subprocess call, with no
  live T2 substrate involved.
"""
from __future__ import annotations

import os
import subprocess
import sys
import time as _time
from pathlib import Path

from nexus._hook_runtime._io import HookResult
from nexus.hooks import session_context

# -- Access to the still-wired script, for differential assertions ----------
# Same sys.path-injection pattern as tests/hooks/test_session_start_hygiene.py
# and tests/hooks/test_session_start_combined_budget.py.
_HOOKS_DIR = Path(__file__).resolve().parents[2] / "conexus" / "hooks" / "scripts"
sys.path.insert(0, str(_HOOKS_DIR))
try:
    import session_start_hook as _script  # type: ignore[import-not-found]
finally:
    sys.path.remove(str(_HOOKS_DIR))


# -- Differential: pure helpers, port vs. still-wired script -----------------


class TestDifferentialAgainstTheStillWiredScript:
    """Same fixture in, same rendered lines out -- port vs. production."""

    _FIXTURE_READY_RAW = "\n".join(
        f"○ nexus-fx{i:03d} ● P1 [bug] fixture bead title {i}" for i in range(12)
    )

    def test_render_ready_beads_matches(self) -> None:
        assert session_context._render_ready_beads(self._FIXTURE_READY_RAW) == (
            _script._render_ready_beads(self._FIXTURE_READY_RAW)
        )

    def test_render_ready_beads_empty_input_matches(self) -> None:
        assert session_context._render_ready_beads(None) == _script._render_ready_beads(None)
        assert session_context._render_ready_beads("") == _script._render_ready_beads("")

    def test_build_capabilities_block_matches(self) -> None:
        assert session_context._build_capabilities_block() == _script._build_capabilities_block()

    def test_emit_hygiene_block_silent_case_matches(self, tmp_path: Path) -> None:
        cache = tmp_path / "cache.txt"
        cache.write_text("fresh\n")
        port_lines: list[str] = []
        script_lines: list[str] = []
        session_context._emit_hygiene_block(port_lines, str(cache))
        _script._emit_hygiene_block(script_lines, str(cache))
        assert port_lines == script_lines == []

    def test_emit_hygiene_block_stale_case_matches(self, tmp_path: Path) -> None:
        cache = tmp_path / "cache.txt"
        cache.write_text("stale\n")
        ten_days_ago = _time.time() - (10 * 86400)
        os.utime(cache, (ten_days_ago, ten_days_ago))

        port_lines: list[str] = []
        script_lines: list[str] = []
        session_context._emit_hygiene_block(port_lines, str(cache))
        _script._emit_hygiene_block(script_lines, str(cache))
        assert port_lines == script_lines


# -- Carried: tests/hooks/test_session_start_hygiene.py, retargeted ---------


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
            # the t2_prefix_scan.py subprocess invocation
            return "  fixture-t2-entry — a snippet"

        monkeypatch.setattr(session_context, "run_command", _fake_run_command)

        result = session_context.run(None)
        assert result.stdout is not None
        t2_idx = result.stdout.index("## T2 Memory (Active Project)")
        beads_idx = result.stdout.index("## Ready Beads")
        caps_idx = result.stdout.index("## nx Capabilities")
        assert t2_idx < beads_idx < caps_idx
        assert "fixture-t2-entry" in result.stdout
        assert "a ready bead" in result.stdout

        # The T2 subprocess call used sys.executable + the resolved sibling
        # script path (not a bare "python3" / "nx" invocation).
        t2_call = next(c for c in calls if c[0] == sys.executable)
        assert t2_call[1].endswith(str(Path("hooks") / "scripts" / "t2_prefix_scan.py"))
        assert t2_call[2] == "myproject"

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


class TestPluginRootResolution:
    def test_env_var_wins_when_set(self, monkeypatch, tmp_path: Path) -> None:
        monkeypatch.setenv("CLAUDE_PLUGIN_ROOT", str(tmp_path / "plugin"))
        assert session_context._plugin_root() == tmp_path / "plugin"

    def test_dev_checkout_fallback_finds_the_real_sibling_script(self, monkeypatch) -> None:
        """No CLAUDE_PLUGIN_ROOT: the fallback must resolve to THIS repo's
        real conexus/hooks/scripts/, where t2_prefix_scan.py still lives
        (that file's own move is a separate bead's scope). This is the
        exact defect a literal ``Path(__file__).parent`` carry would have
        introduced silently -- run_command swallows FileNotFoundError into
        None, so a broken path would produce no error, just no T2 memory
        context, ever."""
        monkeypatch.delenv("CLAUDE_PLUGIN_ROOT", raising=False)
        scan_script = session_context._plugin_root() / "hooks" / "scripts" / "t2_prefix_scan.py"
        assert scan_script.is_file(), (
            f"{scan_script} does not exist -- _plugin_root()'s dev-checkout "
            "fallback no longer finds the sibling script"
        )

    def test_claude_plugin_root_reaches_a_real_subprocess_call(
        self, monkeypatch, tmp_path: Path
    ) -> None:
        """End-to-end through a REAL subprocess.run call (not mocked run_command),
        with a fake sibling script standing in for t2_prefix_scan.py -- proves
        the plumbing (args, cwd, CLAUDE_PLUGIN_ROOT resolution) without any
        live T2 substrate dependency."""
        plugin_root = tmp_path / "plugin"
        scripts_dir = plugin_root / "hooks" / "scripts"
        scripts_dir.mkdir(parents=True)
        fake_scan = scripts_dir / "t2_prefix_scan.py"
        fake_scan.write_text(
            "import sys\n"
            "print(f'## T2 Memory (Active Project) FAKE for {sys.argv[1]}')\n"
        )

        monkeypatch.setenv("CLAUDE_PLUGIN_ROOT", str(plugin_root))
        monkeypatch.setenv("HOME", str(tmp_path / "home"))
        # Real git call, pinned to THIS process's own cwd so it and the
        # test's own verification git call agree on the toplevel:
        # this worktree IS a real repo, so `git rev-parse --show-toplevel`
        # succeeds for real.
        test_cwd = os.getcwd()
        monkeypatch.setenv("CLAUDE_PROJECT_DIR", test_cwd)
        monkeypatch.setattr(session_context, "which", lambda cmd: cmd == "nx")

        real_toplevel = subprocess.run(
            ["git", "rev-parse", "--show-toplevel"],
            capture_output=True, text=True, check=True, cwd=test_cwd,
        ).stdout.strip()
        project_name = Path(real_toplevel).name

        result = session_context.run(None)
        assert result.stdout is not None
        assert f"FAKE for {project_name}" in result.stdout
