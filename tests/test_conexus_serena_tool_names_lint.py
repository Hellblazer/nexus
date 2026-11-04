# SPDX-License-Identifier: AGPL-3.0-or-later
"""conexus names only the Serena tools sn exposes in the claude-code context (nexus-pplwc).

sn's ``hooks/scripts/serena-tools.txt`` is generated from the Serena revision ``sn/.mcp.json`` pins
(``scripts/sync_sn_serena_tools.py``): the tools that context exposes, and on its header line the ones it excludes
(``search_for_pattern``, ``find_file``, ``list_dir`` and others). The serena-code-nav skill told agents to load and
use three excluded tools, and ``conexus/registry.yaml`` granted them and an LSP ``type_hierarchy`` the list does not
have, so an agent following the skill reached for a tool that is not there.

* Every ``mcp__plugin_sn_serena__<tool>`` in conexus markdown and ``registry.yaml`` is on the exposed list.
* No excluded tool is named in backticks in a markdown table row, which is where a skill recommends a tool.
* No JetBrains-only tool is named without its ``jet_brains_`` prefix: ``type_hierarchy``, ``inline_symbol`` and the
  inspection tools exist only as ``jet_brains_*``, so the bare name is a tool in no backend. Review round 1 found
  ``type_hierarchy(...)`` in a skill's code block and in sn's injected SessionStart section, outside the first two
  checks' reach.

The sweep covers conexus and sn markdown (less changelogs and the release ledger) and ``conexus/registry.yaml``.
"""
from __future__ import annotations

import re
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).parent.parent / "scripts"))
from sync_sn_serena_tools import parse_snapshot  # noqa: E402

pytestmark = pytest.mark.lint

_REPO = Path(__file__).resolve().parents[1]
_FULL_NAME_RE = re.compile(r"mcp__plugin_sn_serena__([a-z][a-z_]*)")
_BACKTICKED_RE = re.compile(r"`([a-z][a-z_]*)`")


_RECORDS = frozenset({"CHANGELOG.md", "PENDING_RELEASE.md"})


def _conexus_files() -> list[Path]:
    md = [p for pattern in ("conexus/**/*.md", "sn/**/*.md") for p in _REPO.glob(pattern) if p.name not in _RECORDS]
    return sorted(md) + [_REPO / "conexus" / "registry.yaml"]


def _jetbrains_only(available: set[str]) -> set[str]:
    """Bare names that exist only as ``jet_brains_<name>``; single words (``rename``, ``move``) are left out as
    ordinary English."""
    return {t.removeprefix("jet_brains_") for t in available
            if t.startswith("jet_brains_") and t.removeprefix("jet_brains_") not in available
            and "_" in t.removeprefix("jet_brains_")}


def test_every_full_serena_name_in_conexus_is_exposed() -> None:
    _, available, _ = parse_snapshot()
    assert len(available) > 20, available
    seen: set[str] = set()
    offenders: list[str] = []
    for path in _conexus_files():
        for lineno, line in enumerate(path.read_text(encoding="utf-8").splitlines(), 1):
            for name in _FULL_NAME_RE.findall(line):
                seen.add(name)
                if name not in available:
                    offenders.append(f"{path.relative_to(_REPO)}:{lineno}: mcp__plugin_sn_serena__{name}")
    assert len(seen) >= 8, f"only {sorted(seen)} found; the sweep no longer sees the skill's names"
    assert not offenders, "\n".join(offenders)


def test_no_excluded_serena_tool_is_recommended_in_a_table() -> None:
    _, _, excluded = parse_snapshot()
    assert "search_for_pattern" in excluded, excluded
    offenders = [
        f"{path.relative_to(_REPO)}:{lineno}: `{name}`"
        for path in _conexus_files() if path.suffix == ".md"
        for lineno, line in enumerate(path.read_text(encoding="utf-8").splitlines(), 1)
        if line.lstrip().startswith("|")
        for name in _BACKTICKED_RE.findall(line) if name in excluded
    ]
    assert not offenders, "\n".join(offenders)


def test_no_jetbrains_only_tool_is_named_without_its_prefix() -> None:
    _, available, _ = parse_snapshot()
    bare = _jetbrains_only(set(available))
    assert "type_hierarchy" in bare, bare
    # "`jet_brains_rename`/`move`/`safe_delete`" is shorthand for prefixed names, so a name right after "/`" is not bare.
    pattern = re.compile(r"(?<![A-Za-z0-9_])(?<!/`)(" + "|".join(sorted(bare)) + r")(?![A-Za-z0-9_])")
    offenders = [
        f"{path.relative_to(_REPO)}:{lineno}: {m.group(1)}"
        for path in _conexus_files()
        for lineno, line in enumerate(path.read_text(encoding="utf-8").splitlines(), 1)
        for m in pattern.finditer(line)
    ]
    assert not offenders, "\n".join(offenders)
