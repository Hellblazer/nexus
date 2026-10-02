# SPDX-License-Identifier: AGPL-3.0-or-later
"""The filter replayed on real editor output, with no model (RDR-221, nexus-ger02.16 fix round).

fixtures/replay/session1-edits.json holds the edits the editor proposed in session 1 of the independent runs
of the rejection-memory gate (transcripts in /Volumes/SanHell/tmp/nx.noindex/ger216-gate): 10 runs on
fixtures/review-scenario.md and the 9 measurable runs on docs/storage-tiers.md (snapshots beside it). Those
sessions saw no stored rejections, so they are a no-memory baseline: pair run i's edits, taken as rejected,
with run j's edits (j differs from i), as if run j were the fresh session after run i. That is 324 pairs on
the small document and 360 on the storage document.

The replay is what the filter ALONE does with the edits a fresh editor really produces. It pins two things
the live gate could not show, because the brief kept the filter from having anything to drop: what the filter
catches, and what escapes it. The escapes are one class: the same spot with another replacement (a dash cut
to a comma in one run and to a semicolon in the other), which Sam's decision keeps out of the filter on
purpose ("overlap alone is NOT a match").
"""
from __future__ import annotations

import itertools
import json
from pathlib import Path
from typing import Any

import pytest

from tests.prose_edit.conftest import Prose
from tests.prose_edit.test_acceptance_tools import _gate
from tests.prose_edit.test_memory import _module as memory_module
from tests.prose_edit.test_rejection_memory import _filter

REPLAY = Path(__file__).parent / "fixtures" / "replay"
SNAPSHOT = {"small": "review-scenario.snapshot.txt", "storage": "storage-tiers.snapshot.txt"}

Obj = dict[str, Any]


def _runs() -> dict[str, dict[str, list[Obj]]]:
    return json.loads((REPLAY / "session1-edits.json").read_text(encoding="utf-8"))


def _classify(label: str) -> dict[str, int]:
    """Every (rejected edit of run i, run j) pair: how the filter and the span overlap see it."""
    gate = _gate()
    key = memory_module().change_key
    doc = (REPLAY / SNAPSHOT[label]).read_text(encoding="utf-8")
    runs = _runs()[label]
    counts = {"pairs": 0, "caught": 0, "escaped": 0, "overlap": 0, "unlocated": 0}
    for i, j in itertools.permutations(runs, 2):
        for rejected in runs[i]:
            counts["pairs"] += 1
            rejected_key = key(rejected["old"], rejected["new"])
            if any(key(e["old"], e["new"]) == rejected_key for e in runs[j]):
                counts["caught"] += 1  # the filter would drop it
                continue
            hits = [gate.same_spot_other(rejected, e, doc) for e in runs[j]]
            counts["unlocated"] += sum(1 for h in hits if h is None)
            if any(h for h in hits):
                counts["escaped"] += 1  # same spot, another change: the filter keeps it, by design
    counts["overlap"] = counts["caught"] + counts["escaped"]
    return counts


def test_every_edit_in_the_replay_fixture_is_found_in_its_snapshot() -> None:
    # a fixture whose spans cannot be located would make the escape count a statement about nothing
    for label, runs in _runs().items():
        doc = (REPLAY / SNAPSHOT[label]).read_text(encoding="utf-8")
        missing = [e["old"] for run in runs.values() for e in run if e["old"] not in doc]
        assert missing == [], (label, missing[:3])
        assert sum(len(run) for run in runs.values()) > 0


def test_the_filter_catches_every_recurrence_of_the_small_document_and_nothing_escapes() -> None:
    c = _classify("small")
    # 324 pairs; in 300 of them run j proposed the fix run i's author would have rejected, and the filter
    # drops all 300 whatever span the editor picked (the no-memory recurrence is 92.6%: the brief has real work)
    assert c == {"pairs": 324, "caught": 300, "escaped": 0, "overlap": 300, "unlocated": 0}


def test_the_storage_documents_escapes_are_the_same_spot_with_another_replacement() -> None:
    c = _classify("storage")
    # 360 pairs; 170 recur at the same spot, the filter drops 138 and keeps 32: the same spot, another change
    assert c == {"pairs": 360, "caught": 138, "escaped": 32, "overlap": 170, "unlocated": 0}


def test_the_escapes_are_a_minority_of_the_rejected_items_and_below_the_ten_percent_line() -> None:
    c = _classify("storage")
    assert c["escaped"] / c["pairs"] == pytest.approx(0.0889, abs=0.0001)  # 8.9%, the figure the README states
    assert c["escaped"] / c["pairs"] < 0.10


@pytest.mark.parametrize("label", ["small", "storage"])
def test_the_real_filter_drops_what_the_replay_says_it_catches(prose: Prose, label: str) -> None:
    # one real pair through memory.py reject and memory.py filter, so the pure replay is tied to the script
    runs = _runs()[label]
    key = memory_module().change_key
    for i, j in itertools.permutations(runs, 2):
        match = [(r, e) for r in runs[i] for e in runs[j] if key(r["old"], r["new"]) == key(e["old"], e["new"])
                 and (r["old"], r["new"]) != (e["old"], e["new"])]  # the editor picked another span
        if match:
            rejected, proposed = match[0]
            break
    else:  # pragma: no cover - the fixture always has one
        raise AssertionError("no pair with a different span")
    doc = f"docs/replay-{label}.md"
    prose.ok("reject", doc, "--old", rejected["old"], "--new", rejected["new"])
    out = _filter(prose, (proposed["old"], proposed["new"]), doc=doc)
    assert out["edits"] == [] and [d["cause"] for d in out["dropped"]] == ["rejected"]


def test_the_real_filter_keeps_every_distinct_escape_the_same_spot_with_another_replacement(prose: Prose) -> None:
    # the other half of the tie to memory.py: every distinct pair the replay counts as an escape really passes the filter
    gate = _gate()
    key = memory_module().change_key
    doc_text = (REPLAY / SNAPSHOT["storage"]).read_text(encoding="utf-8")
    runs = _runs()["storage"]
    pairs: dict[tuple[str, str, str, str], tuple[Obj, Obj]] = {}
    for i, j in itertools.permutations(runs, 2):
        for rejected in runs[i]:
            for proposed in runs[j]:
                if key(rejected["old"], rejected["new"]) != key(proposed["old"], proposed["new"]) and \
                        gate.same_spot_other(rejected, proposed, doc_text):
                    pairs[(rejected["old"], rejected["new"], proposed["old"], proposed["new"])] = (rejected, proposed)
    assert len(pairs) == 10  # the ten distinct escaping pairs among the 32
    for n, (rejected, proposed) in enumerate(pairs.values()):
        doc = f"docs/replay-escape-{n}.md"
        prose.ok("reject", doc, "--old", rejected["old"], "--new", rejected["new"])
        out = _filter(prose, (proposed["old"], proposed["new"]), doc=doc)
        assert [e["n"] for e in out["edits"]] == [1] and out["dropped"] == [], (rejected, proposed)
