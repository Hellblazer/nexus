# SPDX-License-Identifier: AGPL-3.0-or-later
"""Plugin docs describe what the file they document does (nexus-pplwc).

* sn/README.md said Context7 is pinned in ``.mcp.json``; the entry is the hosted HTTP endpoint with no version.
* conexus/README.md and registry.yaml said ``/conexus:continuation`` writes under ``~/.cache/nexus/continuations/``;
  the command writes under ``/tmp`` and says why.
"""
from __future__ import annotations

import json
import re
from pathlib import Path

import pytest

pytestmark = pytest.mark.lint

_REPO = Path(__file__).resolve().parents[1]


def test_sn_readme_pins_only_what_mcp_json_pins() -> None:
    context7 = json.loads((_REPO / "sn" / ".mcp.json").read_text(encoding="utf-8"))["context7"]
    assert context7.get("type") == "http" and "@" not in json.dumps(context7), context7
    readme = (_REPO / "sn" / "README.md").read_text(encoding="utf-8")
    wrong = [ln for ln in readme.splitlines()
             if "Context7" in ln and re.search(r"pin", ln, re.IGNORECASE)
             and not re.search(r"not pinned|unpinned|hosted", ln, re.IGNORECASE)]
    assert not wrong, "\n".join(wrong)


def test_continuation_docs_name_the_directory_the_command_writes() -> None:
    command = (_REPO / "conexus" / "commands" / "continuation.md").read_text(encoding="utf-8")
    assert "/tmp" in command and "~/.cache/nexus/continuations" not in command
    for rel in ("conexus/README.md", "conexus/registry.yaml"):
        lines = [ln for ln in (_REPO / rel).read_text(encoding="utf-8").splitlines() if "ontinuation" in ln or "handoff" in ln]
        assert lines, rel
        assert not [ln for ln in lines if "~/.cache/nexus/continuations" in ln], rel
        assert any("/tmp" in ln for ln in lines), rel
