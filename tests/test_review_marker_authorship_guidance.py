# SPDX-License-Identifier: AGPL-3.0-or-later
# Copyright (c) 2026 Hal Hildebrand. All rights reserved.
"""The gate's evidence is never written by a party the gate is checking.

nexus-e3mak. ``pre_close_verification_hook.sh`` decides whether a bead may close
by looking for the token ``review-completed`` plus the bead id, matched by
SUBSTRING across T1 tags/content and T2 title/content (see ``_t1_covers`` /
``_t2_covers``). It cannot tell coverage from a progress report, and it has no
author field to consult: ``nx scratch list`` prints tags and a truncated content
line, nothing about who wrote the entry.

So the invariant has to hold on the WRITE side, and the write side is agent
guidance. On 2026-08-26 a dispatched code-review-expert finished reviewer 1 of a
2-reviewer gate and left its sibling a handoff note starting
``review-completed bead=nexus-utpuw.23 (RG-C reviewer 1/2: ...)``. Honest, said
"1/2", and would have closed the gate with the critic never dispatched — on a
gate whose own text says the critic is never optional.

No agent definition mentioned the token at all, which is why the reviewer
improvised one. These assertions exist so that silence cannot come back: an
agent that never learns the token is reserved will keep inventing markers, and
the failure is invisible in the hook's output.

O(repo) meta-test, hence the lint marker.
"""
from __future__ import annotations

from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parent.parent

_SHARED = REPO_ROOT / "conexus" / "agents" / "_shared" / "CONTEXT_PROTOCOL.md"
#: The agents actually dispatched as the halves of a stacked review gate.
_GATE_REVIEWERS = (
    REPO_ROOT / "conexus" / "agents" / "code-review-expert.md",
    REPO_ROOT / "conexus" / "agents" / "substantive-critic.md",
)
_HOOK = REPO_ROOT / "conexus" / "hooks" / "scripts" / "pre_close_verification_hook.sh"

pytestmark = pytest.mark.lint


def test_the_hook_still_matches_the_token_this_guidance_is_about() -> None:
    """Non-vacuity, and the reason the guidance is load-bearing rather than
    advisory. If the hook stopped keying on this token, every assertion below
    would still pass while guarding nothing."""
    assert _HOOK.is_file(), f"hook moved: {_HOOK}"
    text = _HOOK.read_text(encoding="utf-8")
    assert "review-completed" in text, (
        "the pre-close hook no longer mentions 'review-completed' — either the "
        "gate's token changed (retarget this whole module) or the gate is gone"
    )


def test_the_shared_protocol_reserves_the_token() -> None:
    text = _SHARED.read_text(encoding="utf-8")
    assert "review-completed" in text, (
        "CONTEXT_PROTOCOL.md does not mention the reserved token at all — which "
        "is the state that let a reviewer invent its own marker (nexus-e3mak)"
    )
    lowered = text.lower()
    assert "reserved" in lowered, "the token is mentioned but not marked reserved"
    assert "nexus-e3mak" in text, (
        "the rule is stated without the incident that produced it; a rule whose "
        "cost is not recorded is the first one deleted for brevity"
    )


@pytest.mark.parametrize("path", _GATE_REVIEWERS, ids=lambda p: p.name)
def test_each_gate_reviewer_is_told_not_to_write_the_marker(path: Path) -> None:
    """Both halves of a stacked gate, at their own point of use. The shared
    protocol is linked from these files rather than inlined, and the reviewer
    that invented a marker had that link available."""
    assert path.is_file(), f"agent definition moved: {path}"
    text = path.read_text(encoding="utf-8")
    assert "review-completed" in text, (
        f"{path.name} never mentions the reserved token, so nothing stops it "
        "writing one after finishing its half of a two-reviewer gate"
    )
    assert "NEVER write" in text, (
        f"{path.name} mentions the token without prohibiting it"
    )
