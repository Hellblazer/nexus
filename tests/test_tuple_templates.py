# SPDX-License-Identifier: AGPL-3.0-or-later
# Copyright (c) 2026 Hal Hildebrand. All rights reserved.
"""The shared subspace-to-template resolver (RDR-239 Phase 3, bead
nexus-mdbds.2.17), and the census row's ``template`` field.

Pure functions over the ``registry()`` wire's ``templates`` list; no engine.
"""
from __future__ import annotations

import subprocess
import sys

from nexus.db.t2.http_tuple_store import _body_to_census
from nexus.tuple_templates import resolve_tuple_template

TEMPLATES = [
    {"name": "board/<topic>", "take": {"enabled": False}},
    {"name": "board/ci/<topic>", "take": {"enabled": False}},
    {"name": "aspects", "take": {"enabled": True}},
    {"name": "queue/<name>", "take": {"enabled": True}},
    {"name": "board/ci/pinned", "take": {"enabled": True}},
]


def _name(subspace: str) -> str | None:
    t = resolve_tuple_template(TEMPLATES, subspace)
    return None if t is None else t["name"]


def test_a_param_segment_matches_one_segment_and_the_segment_count_must_agree() -> None:
    assert _name("board/ci/example-branch") == "board/ci/<topic>"
    assert _name("board/example") == "board/<topic>"
    assert _name("queue/work") == "queue/<name>"
    assert _name("queue/a/b") is None
    assert _name("nothing/here") is None


def test_a_literal_template_name_wins_over_a_patterned_one() -> None:
    assert _name("board/ci/pinned") == "board/ci/pinned"
    assert _name("aspects") == "aspects"


def test_a_template_name_resolves_to_itself() -> None:
    for t in TEMPLATES:
        assert _name(t["name"]) == t["name"]


def test_the_module_pulls_in_no_heavy_import() -> None:
    """MCP tool calls import this; ``nexus.health`` is what it exists to avoid."""
    out = subprocess.run(
        [sys.executable, "-c",
         "import sys, nexus.tuple_templates; print('nexus.health' in sys.modules)"],
        capture_output=True, text=True, check=True,
    ).stdout.strip()
    assert out == "False"


def test_a_census_row_carries_the_engines_template_name_and_knows_when_the_key_is_missing() -> None:
    base = {"subspace": "board/ci/x", "total": 1, "available": 1, "claimed": 0, "dead": 0,
            "consumed": 0, "expired_unpurged": 0, "oldest_created_at": None, "newest_created_at": None}
    named = _body_to_census({**base, "template": "board/ci/<topic>"})
    assert (named.template, named.template_known) == ("board/ci/<topic>", True)
    orphan = _body_to_census({**base, "template": None})
    assert (orphan.template, orphan.template_known) == (None, True)
    old = _body_to_census(base)
    assert (old.template, old.template_known) == (None, False)
