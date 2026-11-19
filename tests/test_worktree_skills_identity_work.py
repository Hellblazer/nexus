# SPDX-License-Identifier: AGPL-3.0-or-later
# Copyright (c) 2026 Hal Hildebrand. All rights reserved.
"""The two skills an agent follows literally when it makes and removes a worktree (RDR-253 Step 6, nexus-hczdo.1.9).

An agent obeys the text, so the text is pinned: removal asks ``nx identity may-stop`` first, treats ANY nonzero exit
as a stop, and marks the row done only when the removal succeeded (``&&``); creation labels the worktree and never lets
the label block the work (``|| true``). Both use the real path (``pwd -P``), because the label is spelled by it."""
from __future__ import annotations

import re
from pathlib import Path

SKILLS = Path(__file__).resolve().parent.parent / "conexus" / "skills"
FINISH = (SKILLS / "finishing-branch" / "SKILL.md").read_text(encoding="utf-8")
CREATE = (SKILLS / "git-worktrees" / "SKILL.md").read_text(encoding="utf-8")
DOCS = Path(__file__).resolve().parent.parent / "docs" / "agents"
GUIDE = (DOCS / "worktrees.md").read_text(encoding="utf-8")
RUNNERS = (DOCS / "self-hosted-runners.md").read_text(encoding="utf-8")


def test_removal_asks_may_stop_first_with_the_real_path_and_stops_on_any_nonzero_exit() -> None:
    ask = 'nx identity may-stop --kind worktree --ref "$(pwd -P)"'
    assert ask in FINISH
    section = FINISH[FINISH.index(ask):]
    assert "Any nonzero exit: stop, do not remove" in section.split("```bash")[0]
    assert FINISH.index(ask) < FINISH.index("git worktree remove"), "the check comes before the removal"


def test_done_is_chained_to_the_removal_so_a_failed_removal_leaves_the_row_running() -> None:
    chain = re.search(r"^git worktree remove \S+ && nx identity work done --kind worktree --ref \S+$", FINISH, re.M)
    assert chain, "git worktree remove <path> && nx identity work done ..."
    assert not re.search(r"^git worktree remove [^&\n]*$", FINISH, re.M), "no unchained removal line"


def test_an_unowned_worktree_is_not_removed_on_the_checks_say_so_alone() -> None:
    text = " ".join(FINISH.split())
    assert "If unowned, confirm it is yours" in text and "never remove one you cannot account for" in text


def test_creation_labels_the_worktree_by_its_real_path_and_the_label_never_blocks() -> None:
    assert 'nx identity work register --kind worktree --ref "$(pwd -P)" || true' in CREATE


def test_the_worktree_guide_labels_creation_and_asks_before_removal() -> None:
    """Agents make worktrees from the guide's rule 1 as often as from the skill (nexus-hczdo.1.10 r1 item 1)."""
    rule1 = GUIDE[GUIDE.index("1. **Site worktrees as siblings**"):GUIDE.index("2. **The primary KEEPS")]
    assert 'nx identity work register --kind worktree --ref "$(pwd -P)" || true' in rule1
    ask = 'nx identity may-stop --kind worktree --ref "$(pwd -P)"'
    assert ask in rule1 and rule1.index(ask) < rule1.index("git worktree remove")
    assert re.search(r"git worktree remove \S+ && nx identity work done --kind worktree --ref \S+", rule1)


def test_the_runner_prune_asks_may_stop_before_it_removes() -> None:
    prune = RUNNERS[RUNNERS.index("**Prune the others.**"):]
    prune = prune[:prune.index("\n\n")]
    assert "nx identity may-stop --kind worktree --ref" in prune
    assert prune.index("nx identity may-stop") < prune.index("git worktree remove") < prune.index("git worktree prune")


def test_the_hand_run_guide_launches_labrun_and_watcher_through_work_run() -> None:
    """r1 item 8 (critic S1): a labrun or watcher row exists only when the run is launched through the wrapper."""
    guide = (DOCS / "test-running.md").read_text(encoding="utf-8")
    assert "nx identity work run --kind labrun --ref <label> -- lab-remote" in guide
    assert "nx identity work run --kind watcher --ref <label> --" in guide
