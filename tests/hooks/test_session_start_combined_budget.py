# SPDX-License-Identifier: AGPL-3.0-or-later
# Copyright (c) 2026 Hal Hildebrand. All rights reserved.
"""The SessionStart byte budget: what nexus injects on every start, summed from the REAL renders.

History. nexus-h33x8.5 first held this sum under 6,000 bytes against a 12,817-byte baseline. By 2026-11 it had crept back to
7-9 KB on resume, clear and fork (the reference blocks: the T2 memory scan, ready beads, the bd primer, the capabilities
block, the Knowledge Map and the RDR count were half of it). SessionStart progressive disclosure (T2
nexus/design-sessionstart-progressive-disclosure-2026-11-20, approved by Sam) moved those blocks behind ``nx brief <topic>``
and lowered the ceilings here to the measured values, so the total cannot creep back unnoticed.

What is counted, per start (the always-on set):

* guidance: the REAL ``nx hook session-start`` render, with the REAL mailbox-arm instruction (a budget that measured it with its
  largest conditional block absent would not be a budget), the stale-process probe mocked for determinism.
* the context hook: the REAL ``session_context.run`` output, with the one identity-notes line at its worst case (six titles at
  the 60-character cut and the capped overflow note, through the real render) and no threads due. That is the T1 contract,
  the notes line, the one static ``nx brief`` index line, and nothing else; the hygiene block fires only on a stale cache.
* sn's static ``session-start-section.md``: a plain file read, and not ours, so it is counted separately in the bounded sum.
* a ``/clear`` start also prints the RDR-253 Step 6 resume line, at its 400-byte cap plus the blank line before it.

Not counted, and why: the beads plugin's own ``bd prime`` (the repo ``.beads/PRIME.md``, 992 bytes) when the plugin is on is the
plugin's output, not nexus's, and when it is off the primer is the ``beads`` topic of ``nx brief``, no longer printed at start;
the fire-only notices (setup, preflight failure, mcp-connect timeout, version lockstep, RDR remedy and unchecked-fix lines,
hygiene), which are zero on a healthy start; and the compaction checkpoint, a state snapshot that fires on ``compact`` only and
is capped at 8,192 bytes by its own test. Per-event measurements are in the build record.
"""
from __future__ import annotations

from pathlib import Path
from unittest.mock import patch

import pytest

from nexus import brief
from nexus.hooks import identity_notes, session_context

#: Measured on the progressive-disclosure build: guidance 1,900 B + hook 1,241 B = 3,141 B. Rounded up by about 2%.
_COMBINED_BUDGET_BYTES = 3200

#: guidance 1,900 B + hook 1,241 B + sn static 1,143 B = 4,284 B. Rounded up by about 0.4%.
_BOUNDED_SESSIONSTART_BUDGET_BYTES = 4300

#: The hook's own always-on output with the notes line at its worst case: 1,241 B (T1 contract 603 B, the notes line at its
#: 300 B cap, the 189 B index line, joins). The figure that says the reference blocks stayed out.
_HOOK_ALWAYS_ON_BUDGET_BYTES = 1300

_SN_SESSION_START_SECTION = (
    Path(__file__).resolve().parents[2] / "sn" / "hooks" / "scripts" / "session-start-section.md"
)

#: nexus-6konb.12 (MM-3.4 fix 2): a uuid4()-shaped, 36-char session id -- the
#: real ``CLAUDE_CODE_SESSION_ID`` / ``generate_session_id()`` shape -- not
#: the short synthetic literal this pin used before. See
#: ``TestGuidanceByteBudgetIntegration._REAL_SESSION_ID`` in
#: tests/test_hooks.py for the sibling pin and the measured-vs-fictional
#: history this matches.
_REAL_SESSION_ID = "8154ea0d-6649-4a18-9ef7-ea42818188b0"


def _identity_notes_worst_case() -> list[str]:
    return identity_notes._render("i123", ["x" * 100 + str(i) for i in range(60)], capped=True)


def _real_hook_output(monkeypatch, tmp_path: Path, notes: list[str] | None = None) -> str:
    """The real ``session_context.run`` output for a session id, in an empty config dir, with no threads due and the notes
    line *notes* (default: the worst case)."""
    notes = _identity_notes_worst_case() if notes is None else notes
    monkeypatch.setenv("NEXUS_CONFIG_DIR", str(tmp_path / "config"))
    monkeypatch.setenv("CLAUDE_PROJECT_DIR", str(tmp_path))
    monkeypatch.chdir(tmp_path)
    monkeypatch.setattr("nexus.hooks.board_due.due_lines", lambda sid, **k: [])
    monkeypatch.setattr(identity_notes, "notes_lines", lambda sid, **k: list(notes))
    return session_context.run({"session_id": _REAL_SESSION_ID}).stdout or ""


def _real_guidance_emitter_output() -> str:
    """The REAL ``nx hook session-start`` emitter output, via the
    side-effect-free :func:`nexus.hooks.render_session_start`
    (nexus-cnzei.2 item 8: no writer to mock any more, the render never
    calls ``write_claude_session_id`` or any marker/lease writer, so this
    no longer needs to patch it). Stale-process probe mocked, matching
    ``TestGuidanceByteBudgetIntegration`` in tests/test_hooks.py; the
    mailbox-arm text is supplied directly as the REAL rendered
    instruction (never ``""``) for the same reason
    ``TestGuidanceByteBudgetIntegration`` does: a budget that measures an
    emitter with its largest conditional block silently absent is not a
    budget."""
    from nexus.hooks import render_session_start
    from nexus.mailbox_arm import mailbox_arm_instruction

    class _FakeSkewReport:
        def __init__(self) -> None:
            self.stale: list = []

    with patch(
        "nexus.upgrade_finish.detect_stale_processes",
        return_value=_FakeSkewReport(),
    ):
        return render_session_start(
            _REAL_SESSION_ID,
            mailbox_arm_text=f"\n\n{mailbox_arm_instruction(_REAL_SESSION_ID)}",
        )


def _bytes(text: str) -> int:
    return len(text.encode("utf-8"))


def test_combined_sessionstart_total_stays_under_its_ceiling(monkeypatch, tmp_path: Path) -> None:
    """The two emitters nexus controls, summed: the guidance and the context hook. The ceiling is the measured value rounded up
    a little (progressive disclosure target: about 3 KB on resume, clear and fork, from 7-9 KB)."""
    monkeypatch.delenv("CLAUDE_PLUGIN_ROOT", raising=False)
    guidance_bytes = _bytes(_real_guidance_emitter_output())
    hook_bytes = _bytes(_real_hook_output(monkeypatch, tmp_path))
    combined = guidance_bytes + hook_bytes
    assert combined < _COMBINED_BUDGET_BYTES, (
        f"combined SessionStart total {combined}B (guidance {guidance_bytes}B + context hook {hook_bytes}B) "
        f">= budget {_COMBINED_BUDGET_BYTES}B: move reference context behind `nx brief` instead of raising the ceiling"
    )
    assert combined > _COMBINED_BUDGET_BYTES * 0.9, (
        f"{combined}B is far under {_COMBINED_BUDGET_BYTES}B: the ceiling is no longer the measured value, so it no longer "
        "catches creep; re-measure and lower it"
    )


def test_the_context_hook_prints_only_the_always_on_set(monkeypatch, tmp_path: Path) -> None:
    """The hook's own output: the T1 contract, the notes line, the index line. Nothing that moved behind ``nx brief`` is in it,
    and its size is held where the reference blocks cannot return."""
    out = _real_hook_output(monkeypatch, tmp_path)
    assert _bytes(out) < _HOOK_ALWAYS_ON_BUDGET_BYTES, f"{_bytes(out)}B >= {_HOOK_ALWAYS_ON_BUDGET_BYTES}B"
    assert _bytes(out) > _HOOK_ALWAYS_ON_BUDGET_BYTES * 0.9, f"{_bytes(out)}B: re-measure and lower the ceiling"
    lines = [ln for ln in out.split("\n") if ln]
    assert lines[0].startswith("## T1:") and lines[-1] == session_context.BRIEF_INDEX_LINE
    for moved in ("## T2 Memory", "## Ready Beads", "## nx Capabilities", "## Knowledge Map"):
        assert moved not in out


def test_sessionstart_bounded_components_under_budget(monkeypatch, tmp_path: Path) -> None:
    """The guidance, the context hook and sn's static section, summed: what a healthy start shows the model (the plugin's own
    ``bd prime`` and the fire-only notices are not counted; see the module docstring)."""
    assert _SN_SESSION_START_SECTION.exists(), (
        f"{_SN_SESSION_START_SECTION} missing — sn plugin layout changed; update this census's file list"
    )
    monkeypatch.delenv("CLAUDE_PLUGIN_ROOT", raising=False)
    guidance_bytes = _bytes(_real_guidance_emitter_output())
    hook_bytes = _bytes(_real_hook_output(monkeypatch, tmp_path))
    sn_bytes = len(_SN_SESSION_START_SECTION.read_bytes())
    total = guidance_bytes + hook_bytes + sn_bytes
    assert total < _BOUNDED_SESSIONSTART_BUDGET_BYTES, (
        f"bounded SessionStart components {total}B (guidance {guidance_bytes}B + context hook {hook_bytes}B + sn static "
        f"{sn_bytes}B) >= budget {_BOUNDED_SESSIONSTART_BUDGET_BYTES}B"
    )


def test_the_identity_notes_line_is_one_line_inside_its_cap_and_counted_in_the_hook_output(monkeypatch, tmp_path: Path) -> None:
    """RDR-253 Step 6 part 3 adds exactly one SessionStart line, at most ``identity_notes.MAX_LINE`` bytes, and the budget above
    is measured with it present at its worst case (a measurement that left it out would measure a hook that no longer exists)."""
    worst = _identity_notes_worst_case()
    assert len(worst) == 1 and _bytes(worst[0]) <= identity_notes.MAX_LINE
    assert worst[0] in _real_hook_output(monkeypatch, tmp_path).split("\n")


def test_a_typical_notes_line_leaves_room_under_the_ceiling(monkeypatch, tmp_path: Path) -> None:
    """The ceiling is for the worst case; a typical seat (five short titles) is well under it, so the worst case is not what
    every session pays."""
    typical = identity_notes._render("i5", ["design-of-record", "open-questions", "startup", "recurring-commands", "owns"], capped=False)
    assert _bytes(_real_hook_output(monkeypatch, tmp_path, typical)) < _bytes(_real_hook_output(monkeypatch, tmp_path)) - 100


def test_the_resume_line_on_clear_is_counted_and_composes_with_the_other_new_lines(monkeypatch, tmp_path: Path) -> None:
    """RDR-253 Step 6 part 2: a ``/clear`` start also prints one resume line, at most 400 bytes plus the blank line before
    it. The resume line has its own allowance on top of the bounded ceiling, so it neither eats the headroom the notes line was
    measured against nor can be hidden by it: the components stay under the ceiling, and with the worst resume line added they
    stay under the ceiling plus that allowance. A ``clear`` start is the largest of the five by exactly this line."""
    from datetime import datetime, timedelta, timezone

    from nexus.hooks import resume_line

    monkeypatch.delenv("CLAUDE_PLUGIN_ROOT", raising=False)
    written = (datetime.now(timezone.utc) - timedelta(hours=1)).strftime("%Y-%m-%dT%H:%M:%SZ")
    worst = {"id": "1", "tags": "resume-pointer",
             "content": f"resume {written} | thread: " + "\U0001f600" * 300 + " | worktree: /w/" + "x" * 900 + " | state: " + "y " * 600}

    class Store:
        def list_entries(self):
            return [worst]

    line = resume_line.with_resume_line("", {"source": "clear"}, open_store=lambda: Store())
    assert line and "\n" not in line, "exactly one line"
    added = _bytes(line) + 2
    allowance = resume_line.MAX_BYTES + 2
    assert added <= allowance == 402, f"the resume line is {added} bytes with its blank line; the design says about 400"
    base = (_bytes(_real_guidance_emitter_output()) + _bytes(_real_hook_output(monkeypatch, tmp_path))
            + len(_SN_SESSION_START_SECTION.read_bytes()))
    assert base < _BOUNDED_SESSIONSTART_BUDGET_BYTES, f"{base}B without the resume line >= {_BOUNDED_SESSIONSTART_BUDGET_BYTES}B"
    assert base + added < _BOUNDED_SESSIONSTART_BUDGET_BYTES + allowance
    for source in ("startup", "resume", "compact", "fork"):
        assert resume_line.with_resume_line("", {"source": source}, open_store=lambda: Store()) == "", f"no line on {source}"


@pytest.mark.parametrize("topic", session_context.BRIEF_TOPICS)
def test_every_moved_block_is_reachable_through_a_brief_topic_the_index_names(topic: str) -> None:
    """The index line is the only pointer SessionStart leaves; each topic it names must exist in ``nexus.brief`` and each
    ``nexus.brief`` topic must be named by it (a block that moved and is not named is unreachable)."""
    assert topic in brief.TOPICS and f"{topic} (" in session_context.BRIEF_INDEX_LINE
    assert set(brief.TOPICS) == set(session_context.BRIEF_TOPICS)
