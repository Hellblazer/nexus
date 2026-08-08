# SPDX-License-Identifier: AGPL-3.0-or-later
# Copyright (c) 2026 Hal Hildebrand. All rights reserved.
"""nexus-h33x8.4: SessionStart guidance imperative — Tier B delivery.

Pins three things:

1. Byte-parity on landing: ``GUIDANCE_IMPERATIVE`` must equal the
   ``using-nx-skills/SKILL.md`` file content exactly, so this bead is
   isolated to CHANNEL (content restructuring is nexus-h33x8.5's job).
   This assertion is expected to need updating once .5 intentionally
   diverges the two — that is not a regression, it is item 3/sequencing
   from the bead working as designed.
2. ``legacy_cat_channel_active`` / ``guidance_block``: the interim
   double-emission guard required by the bead's item 4 — suppress the
   wheel-side emission whenever the installed (pinned) plugin's own
   ``hooks.json`` still carries the legacy ``cat .../SKILL.md`` entry,
   fail OPEN (emit) whenever the check cannot be completed.
"""
from __future__ import annotations

import json
import pathlib

from nexus.session_start_guidance import (
    GUIDANCE_IMPERATIVE,
    guidance_block,
    legacy_cat_channel_active,
)

REPO_ROOT = pathlib.Path(__file__).parent.parent
SKILL_MD = REPO_ROOT / "conexus" / "skills" / "using-nx-skills" / "SKILL.md"


# ── byte-parity on landing ───────────────────────────────────────────────────


def test_guidance_imperative_matches_skill_md_byte_for_byte():
    """Verification 2 from the bead: content must be equivalent to what
    `cat SKILL.md` delivered, so the channel change carries no content
    change. See module docstring for why this is a landing-time check,
    not a permanent invariant (nexus-h33x8.5 will intentionally diverge
    the two)."""
    assert GUIDANCE_IMPERATIVE == SKILL_MD.read_text(encoding="utf-8")


def test_guidance_imperative_is_nonempty():
    assert len(GUIDANCE_IMPERATIVE) > 1000


# ── legacy_cat_channel_active: the interim double-emission guard ───────────


def _write_hooks_json(tmp_path: pathlib.Path, *, session_start_commands: list[str]) -> pathlib.Path:
    hooks_dir = tmp_path / "hooks"
    hooks_dir.mkdir(parents=True, exist_ok=True)
    payload = {
        "hooks": {
            "SessionStart": [
                {
                    "matcher": "startup|resume|clear|compact",
                    "hooks": [
                        {"type": "command", "command": cmd, "timeout": 10}
                        for cmd in session_start_commands
                    ],
                }
            ]
        }
    }
    (hooks_dir / "hooks.json").write_text(json.dumps(payload))
    return tmp_path


class TestLegacyCatChannelActive:
    def test_no_plugin_root_fails_open(self, monkeypatch):
        monkeypatch.delenv("CLAUDE_PLUGIN_ROOT", raising=False)
        assert legacy_cat_channel_active() is False

    def test_explicit_none_root_and_no_env_fails_open(self, monkeypatch):
        monkeypatch.delenv("CLAUDE_PLUGIN_ROOT", raising=False)
        assert legacy_cat_channel_active(None) is False

    def test_legacy_entry_present_reports_active(self, tmp_path):
        root = _write_hooks_json(
            tmp_path,
            session_start_commands=[
                "nx upgrade --auto",
                "nx hook session-start",
                "cat $CLAUDE_PLUGIN_ROOT/skills/using-nx-skills/SKILL.md",
            ],
        )
        assert legacy_cat_channel_active(str(root)) is True

    def test_legacy_entry_absent_reports_inactive(self, tmp_path):
        root = _write_hooks_json(
            tmp_path,
            session_start_commands=[
                "nx upgrade --auto",
                "nx hook session-start",
            ],
        )
        assert legacy_cat_channel_active(str(root)) is False

    def test_missing_hooks_json_fails_open(self, tmp_path):
        # tmp_path has no hooks/hooks.json at all.
        assert legacy_cat_channel_active(str(tmp_path)) is False

    def test_malformed_hooks_json_fails_open(self, tmp_path):
        hooks_dir = tmp_path / "hooks"
        hooks_dir.mkdir(parents=True)
        (hooks_dir / "hooks.json").write_text("not json {{")
        assert legacy_cat_channel_active(str(tmp_path)) is False

    def test_hooks_json_not_a_dict_fails_open(self, tmp_path):
        hooks_dir = tmp_path / "hooks"
        hooks_dir.mkdir(parents=True)
        (hooks_dir / "hooks.json").write_text("[1, 2, 3]")
        assert legacy_cat_channel_active(str(tmp_path)) is False

    def test_env_var_used_when_no_explicit_root(self, tmp_path, monkeypatch):
        root = _write_hooks_json(
            tmp_path,
            session_start_commands=[
                "cat $CLAUDE_PLUGIN_ROOT/skills/using-nx-skills/SKILL.md",
            ],
        )
        monkeypatch.setenv("CLAUDE_PLUGIN_ROOT", str(root))
        assert legacy_cat_channel_active() is True


# ── guidance_block: the actual emission gate ────────────────────────────────


class TestGuidanceBlock:
    def test_emits_full_text_when_legacy_channel_absent(self, tmp_path):
        root = _write_hooks_json(tmp_path, session_start_commands=["nx hook session-start"])
        assert guidance_block(str(root)) == GUIDANCE_IMPERATIVE

    def test_suppressed_when_legacy_channel_present(self, tmp_path):
        root = _write_hooks_json(
            tmp_path,
            session_start_commands=[
                "cat $CLAUDE_PLUGIN_ROOT/skills/using-nx-skills/SKILL.md",
            ],
        )
        assert guidance_block(str(root)) == ""

    def test_emits_when_no_plugin_root_at_all(self, monkeypatch):
        """Bare CLI / dev / test invocation: no legacy channel could
        possibly be running, so fail-open means emit."""
        monkeypatch.delenv("CLAUDE_PLUGIN_ROOT", raising=False)
        assert guidance_block() == GUIDANCE_IMPERATIVE
