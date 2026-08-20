# SPDX-License-Identifier: AGPL-3.0-or-later
# Copyright (c) 2026 Hal Hildebrand. All rights reserved.
"""nexus-h33x8.5 fix-pass — VERIFICATION 1, the COMBINED SessionStart byte
budget, exactly the way the bead's own 12,817-byte baseline was measured:
piping the SessionStart payload through each unconditional hook AND
SUMMING (bead's MEASURED table: SKILL.md/imperative 7,507 +
session_start_hook.py 5,278 + nx-hook-session-start 32 = 12,817; the three
conditional emitters — preflight.py, rdr_hook.py, version_lockstep_hook.py
— stay at their carved-out 0 bytes and are not part of this sum).

The critic's Critical (T2 nexus/substantive-critique-h33x8.3-.5-2026-08-20,
[22935]) found that the first .5 pass only budgeted the emitter it directly
edited (``nx hook session-start`` — see ``TestGuidanceByteBudgetIntegration``
in tests/test_hooks.py) and left ``session_start_hook.py`` — the bead's own
second-largest baseline contributor, explicitly counted in 12,817 and NOT
among the three carved-out conditional emitters — untouched and unbudgeted.
This file closes that gap: it asserts the SUM of the two unconditional,
always-firing emitters this bead's scope now covers.

DETERMINISM: session_start_hook.py's real output depends on live, daily-
varying T2/bead state (T2 listing + bead prime) — hard-coding a live-state
assertion would be a flaky CI gate keyed on unrelated project activity
(dev notes T2 nexus/h33x8.5-dev-notes-2026-08-20 already flagged this
exact tension). So this test builds a REPRESENTATIVE FIXTURE: a realistic
T2-memory block and a realistic ready-beads raw payload, sized at or above
what a live run actually measured on 2026-08-20 (Ready Beads block 855B
post-trim; the T2 Memory sub-fixture is 626B, slightly UNDER the 847B live
figure — acceptable while the combined margin stays ~2,000B; see the
fix-pass section of the dev notes), run through the REAL render functions
(``_render_ready_beads``, ``_build_capabilities_block``) wherever those
exist as pure/testable seams, and a literal Knowledge Map fixture sized to
match a typical ``nx context refresh`` cache file. The guidance emitter
half is the REAL ``nexus.hooks.session_start()`` output (stale-process
probe mocked for determinism, mirroring
``TestGuidanceByteBudgetIntegration`` in tests/test_hooks.py) — not a
fixture, since that side is fully deterministic already.
"""
from __future__ import annotations

import sys
from pathlib import Path
from unittest.mock import patch

# Make the hook script importable as a module via path injection (same
# pattern as tests/hooks/test_session_start_hygiene.py).
_HOOKS_DIR = Path(__file__).resolve().parents[2] / "conexus" / "hooks" / "scripts"
sys.path.insert(0, str(_HOOKS_DIR))
try:
    from session_start_hook import _build_capabilities_block, _render_ready_beads  # type: ignore[import-not-found]
finally:
    sys.path.remove(str(_HOOKS_DIR))


#: Representative T2-memory block, sized to match (slightly above) a live
#: 2026-08-20 measurement AFTER the fix-pass render-cap trim (_HARD_CAP
#: 15->8, _SNIPPET_LIMIT 5->3, _TITLE_LIMIT 8->5, snippet max_chars
#: 120->70 in conexus/hooks/scripts/t2_prefix_scan.py). Not re-deriving
#: t2_prefix_scan.py's internal rendering here — that module has its own
#: dedicated suite (tests/hooks/test_t2_prefix_scan.py); this is a
#: representative SIZE fixture for the combined-budget sum only.
_FIXTURE_T2_MEMORY_BLOCK = (
    "## T2 Memory (Active Project)\n"
    "### T2 Memory\n"
    "  fixture-entry-one-2026-08-20 — a representative T2 entry snippet, "
    "about seventy characters long to mat…\n"
    "  fixture-entry-two-2026-08-20 — another representative snippet of "
    "similar length to a real one, trunca…\n"
    "  fixture-entry-three-2026-08-20\n"
    "  fixture-entry-four-2026-08-20\n"
    "  … (1200 more)\n"
    "\n"
    "### T2 Memory (active)\n"
    "  fixture-active-one-2026-08-20 — representative active-project "
    "snippet text, about seventy chars…\n"
    "  fixture-active-two-2026-08-20 — representative active-project "
    "snippet text, about seventy chars…\n"
    "  fixture-active-three-2026-08-20\n"
    "  … (20 more)\n"
)

#: Representative raw ``bd ready`` stdout: 15 lines, each padded to a
#: realistic bead-title length (~150-190 chars, matching real titles
#: measured live on 2026-08-20) so ``_render_ready_beads`` exercises its
#: real 5-line/160-char cap and overflow-count line against fixture input
#: at least as large as live traffic.
_FIXTURE_READY_RAW = "\n".join(
    f"○ nexus-fx{i:03d} ● P1 [bug] Representative ready-bead title padded to "
    f"resemble real bead-title length for the combined-budget fixture, "
    f"item number {i} of the fixture set so each line differs"
    for i in range(15)
)

#: Representative Knowledge Map fixture — session_start_hook.py inlines
#: the ``nx context refresh``-produced L1 cache file verbatim (no pure
#: render function to call; it is a plain file read). Sized to match the
#: 657 bytes measured live on 2026-08-20 for this repo's cache file.
_FIXTURE_KNOWLEDGE_MAP = (
    "## Knowledge Map\n\n"
    "code: representative topic label one (12345), representative topic "
    "label two (6789), representative topic label three (2345), a fourth "
    "representative label (901), a fifth representative label to pad this "
    "fixture out toward the real measured 657-byte size (42)\n"
    "docs: representative doc-corpus topic label (1500), another doc "
    "corpus label for padding (700)\n"
)

#: The combined budget (bead VERIFICATION 1: SessionStart total < 6,000
#: bytes). 6,000 exactly, per the bead's own target — this test is the
#: mechanical, non-negotiable check the bead calls for.
_COMBINED_BUDGET_BYTES = 6000


def _fixture_session_start_hook_output() -> str:
    """Assemble session_start_hook.py's REPRESENTATIVE combined output:
    fixture T2-memory block + REAL ``_render_ready_beads`` render of a
    fixture raw payload + REAL ``_build_capabilities_block`` + fixture
    Knowledge Map. Mirrors ``main()``'s own line assembly order."""
    lines: list[str] = [_FIXTURE_T2_MEMORY_BLOCK, ""]
    lines.extend(_render_ready_beads(_FIXTURE_READY_RAW))
    lines.extend(_build_capabilities_block())
    lines.append(_FIXTURE_KNOWLEDGE_MAP)
    return "\n".join(lines)


def _real_guidance_emitter_output() -> str:
    """The REAL ``nx hook session-start`` emitter output (deterministic:
    stale-process probe mocked, matching
    ``TestGuidanceByteBudgetIntegration`` in tests/test_hooks.py)."""
    from nexus.hooks import session_start

    class _FakeSkewReport:
        def __init__(self) -> None:
            self.stale: list = []

    with (
        patch("nexus.hooks.write_claude_session_id"),
        patch(
            "nexus.upgrade_finish.detect_stale_processes",
            return_value=_FakeSkewReport(),
        ),
    ):
        return session_start(claude_session_id="s-h33x8-5-combined-budget")


def test_combined_sessionstart_total_under_6000_bytes(monkeypatch) -> None:
    """VERIFICATION 1, the bead's own summing method: nx-hook-session-start
    emitter + session_start_hook.py emitter (fixture-representative T2/
    ready-beads state) must sum under 6,000 bytes. This is the fix-pass
    counterpart to ``TestGuidanceByteBudgetIntegration`` (tests/test_hooks.py),
    which pins only the first emitter's own share."""
    monkeypatch.delenv("CLAUDE_PLUGIN_ROOT", raising=False)
    guidance_bytes = len(_real_guidance_emitter_output().encode("utf-8"))
    hook_bytes = len(_fixture_session_start_hook_output().encode("utf-8"))
    combined = guidance_bytes + hook_bytes
    assert combined < _COMBINED_BUDGET_BYTES, (
        f"combined SessionStart total {combined}B "
        f"(guidance {guidance_bytes}B + session_start_hook.py fixture "
        f"{hook_bytes}B) >= budget {_COMBINED_BUDGET_BYTES}B"
    )


def test_render_ready_beads_caps_at_five_lines_with_overflow_count() -> None:
    """Direct unit test of the trimmed cap (was 10 lines/500 chars,
    uncapped total; now 5 lines/160 chars with an overflow count line)."""
    rendered = _render_ready_beads(_FIXTURE_READY_RAW)
    assert rendered[0] == "## Ready Beads"
    body = [ln for ln in rendered if ln not in ("## Ready Beads", "```", "")]
    # 5 bead lines + 1 overflow-count line.
    assert len(body) == 6
    assert all(len(ln) <= 160 for ln in body[:5]), (
        "a shown ready-bead line exceeds the 160-char cap"
    )
    assert "10 more" in body[5]
    assert "bd ready" in body[5]


def test_render_ready_beads_empty_input_yields_no_section() -> None:
    assert _render_ready_beads(None) == []
    assert _render_ready_beads("") == []


def test_capabilities_block_preserves_every_distinct_token() -> None:
    """Trim (nexus-h33x8.5 fix-pass) must drop prose, never a distinct
    backtick-quoted tool/flag/example token — the coordinator's "DATA
    stays data" instruction."""
    text = "\n".join(_build_capabilities_block())
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
