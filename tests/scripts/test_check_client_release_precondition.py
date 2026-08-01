# SPDX-License-Identifier: AGPL-3.0-or-later
"""check_client_release_precondition.py — the 9ssih deploy-order gate.

The mirror of test_check_engine_release_floor.py: this gate refuses an
engine tag whose required client commits are not in a RELEASED conexus
version. Inert gates are the recurring failure class (nexus-qc4p1), so
beyond the logic tests there is a WIRING test pinning that the
engine-release skill actually invokes the script.
"""
from __future__ import annotations

import re
import subprocess
from pathlib import Path

import pytest

import check_client_release_precondition as gate

REPO_ROOT = Path(__file__).resolve().parents[2]


class TestLogic:
    def test_no_preconditions_registered_is_ok(self, capsys):
        assert gate.check("engine-service-v0.0.0-nonexistent") == 0
        assert "no client-release preconditions" in capsys.readouterr().out

    def test_registered_tag_blocks_when_commit_unreleased(self, monkeypatch, capsys):
        monkeypatch.setattr(gate, "latest_release_tag", lambda: "v0.0.1")
        monkeypatch.setattr(gate, "is_ancestor", lambda commit, tag: False)
        monkeypatch.setitem(
            gate.ENGINE_CLIENT_PRECONDITIONS, "engine-service-vTEST",
            {"deadbeef": "test precondition"},
        )
        assert gate.check("engine-service-vTEST") == 1
        assert "BLOCKED" in capsys.readouterr().err

    def test_registered_tag_passes_when_commit_released(self, monkeypatch, capsys):
        monkeypatch.setattr(gate, "latest_release_tag", lambda: "v0.0.1")
        monkeypatch.setattr(gate, "is_ancestor", lambda commit, tag: True)
        monkeypatch.setitem(
            gate.ENGINE_CLIENT_PRECONDITIONS, "engine-service-vTEST",
            {"deadbeef": "test precondition"},
        )
        assert gate.check("engine-service-vTEST") == 0

    def test_unverifiable_git_state_is_exit_2_not_pass(self, monkeypatch, capsys):
        """'Could not verify' is never 'must be fine'."""
        monkeypatch.setattr(gate, "latest_release_tag", lambda: "v0.0.1")

        def boom(commit, tag):
            raise RuntimeError("git exploded")

        monkeypatch.setattr(gate, "is_ancestor", boom)
        monkeypatch.setitem(
            gate.ENGINE_CLIENT_PRECONDITIONS, "engine-service-vTEST",
            {"deadbeef": "test precondition"},
        )
        assert gate.check("engine-service-vTEST") == 2

    @pytest.fixture()
    def hermetic_repo(self, tmp_path, monkeypatch):
        """A scratch git repo with both tag shapes — CI checkouts are
        SHALLOW AND TAGLESS (found on the 7.0.0 release PR: the two
        against-the-real-repo tests 128'd), so these tests must carry
        their own git state."""
        repo = tmp_path / "repo"
        repo.mkdir()
        env = ["-c", "user.email=t@t.invalid", "-c", "user.name=t"]
        def g(*args):
            subprocess.run(["git", "-C", str(repo), *env, *args],
                           capture_output=True, text=True, check=True)
        g("init", "-q")
        g("commit", "--allow-empty", "-q", "-m", "one")
        g("commit", "--allow-empty", "-q", "-m", "two")
        g("tag", "v1.2.3")
        g("tag", "engine-service-v9.9.9")
        # The gate's git helpers run in CWD by design (the script runs from
        # the repo root) — chdir scopes every git call to the scratch repo.
        monkeypatch.chdir(repo)
        return repo

    def test_latest_release_tag_ignores_engine_tags(self, hermetic_repo):
        """The v[0-9]* glob must never match engine-service-v* tags."""
        tag = gate.latest_release_tag()
        assert tag == "v1.2.3"
        assert re.fullmatch(r"v\d+\.\d+\.\d+", tag), tag
        assert not tag.startswith("engine-service")

    def test_v0161_precondition_is_registered(self):
        """The row this script was born for: a62649ef gates the v0.1.61 deploy."""
        pre = gate.ENGINE_CLIENT_PRECONDITIONS["engine-service-v0.1.61"]
        assert any(c.startswith("a62649ef") for c in pre)

    def test_is_ancestor_real_git_smoke(self, hermetic_repo):
        """A root-ward commit is an ancestor of HEAD (hermetic repo — the
        real CI checkout is single-commit, HEAD~1 does not resolve)."""
        proc = subprocess.run(
            ["git", "-C", str(hermetic_repo), "rev-parse", "HEAD~1"],
            capture_output=True, text=True,
        )
        assert gate.is_ancestor(proc.stdout.strip(), "HEAD")


class TestWiring:
    """An unwired gate is a prose gate with extra steps (nexus-qc4p1 class)."""

    def test_engine_release_skill_invokes_the_gate(self):
        skill = (REPO_ROOT / ".claude" / "skills" / "engine-release" / "SKILL.md").read_text()
        assert "check_client_release_precondition.py" in skill, (
            "the engine-release skill must run the client-precondition gate "
            "before the tag push — without the invocation step this script "
            "is exactly as skippable as the prose gate it replaced"
        )
        # The invocation must come BEFORE the tag-push step, not after.
        gate_pos = skill.index("check_client_release_precondition.py")
        push_pos = skill.index("git push origin engine-service-v")
        assert gate_pos < push_pos, "the gate must run before the tag push"
