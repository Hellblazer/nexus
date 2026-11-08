# SPDX-License-Identifier: AGPL-3.0-or-later
"""RDR-243 Phase 3 fix round r1 (nexus-rfvyy.8), item 2: LLM-bound surfaces outside the named set.

``nx catalog links-for-file`` feeds the SubagentStart hook's "Linked RDRs" section (injected into a subagent's
context) and prints peer titles to the Bash tool. Q2's posture applies: under Claude Code a sensitive document (the
entry or a peer) is the stub; in a terminal it is shown with a badge. The hook asks for the withhold posture
explicitly (``--for-model``), so it does not depend on the environment marker.
"""
from __future__ import annotations

import pytest
from click.testing import CliRunner

from nexus.cli import main
from nexus.sensitive_output import BADGE, stub_line

from tests.commands.test_catalog_cmd_sensitivity import (  # type: ignore[import-not-found]
    CANARY, CLEAN_A, CLEAN_B, SECRET, SECRET_TITLE, _FakeCatalog,
)


class _PathCatalog(_FakeCatalog):
    """The fake plus the file-path lookups ``links-for-file`` uses."""

    def find_all_by_file_path(self, path):
        # The hook only looks at paths under src/, tests/, docs/ or nx/: match by file name.
        return [e for e in self.entries if e.file_path and e.file_path.rsplit("/", 1)[-1] == path.rsplit("/", 1)[-1]]


@pytest.fixture
def run(monkeypatch: pytest.MonkeyPatch):
    monkeypatch.delenv("CLAUDECODE", raising=False)
    monkeypatch.delenv("CLAUDE_CODE_ENTRYPOINT", raising=False)
    fake = _PathCatalog()
    monkeypatch.setattr("nexus.commands.catalog._get_catalog", lambda: fake)

    def go(*args: str) -> str:
        result = CliRunner().invoke(main, ["catalog", "links-for-file", *args])
        assert result.exit_code == 0, result.output
        return result.stdout

    return go


def test_links_for_file_stubs_a_sensitive_peer_under_claude_code(run, monkeypatch) -> None:
    monkeypatch.setenv("CLAUDECODE", "1")
    out = run("/pub/garden.md")
    assert CANARY not in out
    assert f"→ [cites] {stub_line(SECRET)}" in out
    assert "Cooking" in out, "a clean peer keeps its title"


def test_links_for_file_stubs_a_sensitive_entry_header_under_claude_code(run, monkeypatch) -> None:
    monkeypatch.setenv("CLAUDECODE", "1")
    out = run(next(e.file_path for e in _PathCatalog().entries if e.sensitive))
    assert CANARY not in out
    assert stub_line(SECRET) in out
    assert f"← [cites] {CLEAN_A} knowledge: Gardening" in out


def test_for_model_withholds_without_the_environment_marker(run) -> None:
    """The hook's call: the output is injected into a model's context whatever the environment says."""
    out = run("--for-model", "/pub/garden.md")
    assert CANARY not in out and f"→ [cites] {stub_line(SECRET)}" in out


def test_a_terminal_shows_the_sensitive_peer_with_a_badge(run) -> None:
    out = run("/pub/garden.md")
    assert SECRET_TITLE in out and BADGE in out


def test_a_clean_file_prints_the_same_under_either_posture(run, monkeypatch) -> None:
    terminal = run("/pub/cook.md")
    monkeypatch.setenv("CLAUDECODE", "1")
    assert run("/pub/cook.md") == terminal
    assert CLEAN_B in terminal and BADGE not in terminal


# ── the SubagentStart hook's Linked RDRs section ─────────────────────────────


def test_the_hook_section_asks_for_the_withhold_posture_and_carries_no_sensitive_peer(run, monkeypatch) -> None:
    """The hook injects ``nx catalog links-for-file`` output into a subagent's context: it passes ``--for-model``
    and what it injects holds the stub, never the peer's title."""
    from nexus.hooks import subagent_start
    from tests._module_seam import setattr_in

    seen: list[list[str]] = []

    def fake_run(argv, **_kw):
        seen.append(list(argv))
        return CliRunner().invoke(main, argv[argv.index("catalog"):]).stdout

    monkeypatch.setattr(subagent_start, "_run_captured", fake_run)
    setattr_in(monkeypatch, subagent_start, "shutil.which", lambda _c: "/usr/bin/nx")
    out = subagent_start._linked_rdrs_section("look at src/garden.md please", {})
    assert seen and all("--for-model" in argv for argv in seen)
    assert "Linked RDRs" in out and stub_line(SECRET) in out
    assert CANARY not in out and SECRET_TITLE not in out
