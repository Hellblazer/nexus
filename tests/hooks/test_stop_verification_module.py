# SPDX-License-Identifier: AGPL-3.0-or-later
# Copyright (c) 2026 Hal Hildebrand. All rights reserved.
"""The ported Stop verification hook (RDR-215 bead nexus-q02nx.13).

``test_stop_verification_hook.py`` owns the decision surface and drives both
implementations. This file owns the two things that have no bash counterpart
to differential against:

* the contract that NO path can emit deny or block -- asserted over the
  whole decision space rather than spot-checked, since "warns only" is the
  script's own stated guarantee and the close gate is what enforces;
* the catalog sync's move to a daemon thread, which is the bead's one
  deliberate behaviour change and the only part of the port that can fail
  in a way the script could not.
"""
from __future__ import annotations

import json
import logging
import os
import subprocess
from pathlib import Path

import pytest
import structlog
import structlog.testing

from nexus._hook_runtime._io import configure_hook_logging
from nexus.hooks import stop_verification as hook


def _fake_bin(tmp_path: Path, name: str, body: str) -> Path:
    """A one-file executable on PATH, so the hook's shutil.which finds it."""
    bin_dir = tmp_path / "fakebin"
    bin_dir.mkdir(exist_ok=True)
    p = bin_dir / name
    p.write_text(f"#!/bin/sh\n{body}\n")
    p.chmod(0o755)
    return bin_dir


def _catalog_repo(tmp_path: Path, *, dirty: bool = True) -> Path:
    """A git-backed catalog with a documents.jsonl, optionally dirty."""
    root = tmp_path / "catalog"
    root.mkdir()
    subprocess.run(["git", "init", str(root)], capture_output=True, check=True)
    subprocess.run(
        ["git", "-C", str(root), "config", "user.email", "t@t.com"],
        capture_output=True, check=True,
    )
    subprocess.run(
        ["git", "-C", str(root), "config", "user.name", "T"],
        capture_output=True, check=True,
    )
    (root / "documents.jsonl").write_text('{"a": 1}\n')
    subprocess.run(["git", "-C", str(root), "add", "."], capture_output=True, check=True)
    subprocess.run(
        ["git", "-C", str(root), "commit", "-m", "init"], capture_output=True, check=True
    )
    if dirty:
        (root / "documents.jsonl").write_text('{"a": 1}\n{"b": 2}\n')
    return root


class TestItCanOnlyEverApprove:
    """The contract is "warns only", so this is checked across the decision
    space rather than on one happy path. A deny or block from here would be
    a silent expansion of what a Stop hook can do to a session."""

    @pytest.mark.parametrize(
        "payload",
        [
            None,
            {},
            {"session_id": "s1"},
            {"session_id": ""},
            {"session_id": "../../escape"},
            {"session_id": "s1", "stop_hook_active": True},
            "not a dict at all",
            [],
        ],
    )
    def test_every_payload_shape_approves(self, payload, monkeypatch, tmp_path):
        monkeypatch.setenv("NEXUS_CONFIG_DIR", str(tmp_path / "cfg"))
        monkeypatch.setenv("CLAUDE_PLUGIN_ROOT", str(tmp_path / "nowhere"))
        result = hook.run(payload)
        parsed = json.loads(result.stdout)
        assert parsed["decision"] == "approve"
        assert result.exit_code == 0

    def test_the_only_key_the_envelope_can_carry_is_reason(self, tmp_path, monkeypatch):
        monkeypatch.setenv("CLAUDE_PLUGIN_ROOT", str(tmp_path / "nowhere"))
        parsed = json.loads(hook.run({"session_id": "s1"}).stdout)
        assert set(parsed) <= {"decision", "reason"}

    def test_the_bare_envelope_matches_the_scripts_bytes(self):
        assert hook._approve().stdout == '{"decision": "approve"}'

    def test_the_reason_envelope_matches_the_scripts_bytes(self):
        assert (
            hook._approve("WARNING: x\n").stdout
            == '{"decision": "approve", "reason": "WARNING: x\\n"}'
        )

    def test_a_reason_with_quotes_cannot_break_the_envelope(self):
        """The script escaped through json.dumps too, with a printf
        fallback that would NOT have escaped correctly. Here there is no
        fallback path to get wrong."""
        parsed = json.loads(hook._approve('has "quotes" and \\ backslash').stdout)
        assert parsed["reason"] == 'has "quotes" and \\ backslash'


