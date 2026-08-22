"""Tests for scripts/decision_coverage_census.py (bead nexus-4bqre.1).

The census is the INSTRUMENT the nexus-4bqre epic's verdict (nexus-4bqre.8)
is computed with, so these tests exist to establish that its arithmetic is
right BEFORE the intervention is judged by it. Every number in the synthetic
fixture below is known by construction, not read back from the live corpus --
a census asserted against its own output would only prove it agrees with
itself.

Trap these tests are written against (both real, both from this epic's own
history): a fixture that makes the gate it exists to exercise vacuous, and a
pin test that pins a static file and therefore proves nothing about the
live-append race the pin mechanism was built for.
"""

from __future__ import annotations

import json
import subprocess
import sys
from pathlib import Path

import pytest

SCRIPT = Path(__file__).resolve().parents[1] / "scripts" / "decision_coverage_census.py"

WINDOW_START = "2026-08-01T00:00:00+00:00"
WINDOW_END = "2026-09-01T00:00:00+00:00"
IN_WINDOW_TS = "2026-08-10T12:00:00.000Z"
OUT_OF_WINDOW_TS = "2026-07-01T12:00:00.000Z"

THOUGHT = "mcp__plugin_conexus_sequential-thinking__sequentialthinking"


# Every record carries multibyte UTF-8. Without it chars == bytes and the pin
# tests below cannot tell a BYTE-prefix read from a CHARACTER-prefix read --
# which is the actual defect the pin mechanism had (found 2026-08-22: the
# checked-in script reproduced the frozen baseline off by one tool call
# because a text-mode read(nbytes) overshoots the pinned byte prefix on any
# file with non-ASCII content). A pure-ASCII fixture passes either way.
MULTIBYTE_PAYLOAD = "— ✓ é 日本語 " * 8


def _assistant(tool_name: str, ts: str, *, sidechain: bool = False) -> str:
    rec = {
        "type": "assistant",
        "timestamp": ts,
        "note": MULTIBYTE_PAYLOAD,
        "message": {"content": [{"type": "tool_use", "name": tool_name}]},
    }
    if sidechain:
        rec["isSidechain"] = True
    # ensure_ascii=False is load-bearing: the default escapes the multibyte
    # payload back to \uXXXX and the fixture silently reverts to pure ASCII.
    return json.dumps(rec, ensure_ascii=False)


def _write_run(path: Path, tools: list[str], ts: str, **kw) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text("\n".join(_assistant(t, ts, **kw) for t in tools) + "\n")


# Run A, 8 tool calls. Positions chosen so the front-half rule is FALSIFIABLE:
# N=8 so the midpoint is exactly 4, and a thought sits AT index 4. The frozen
# definition is `i < N/2`, so that thought is NOT front-half. An implementation
# using `<=` would report 2 front-half thoughts instead of 1, and this fixture
# is the only thing that can tell them apart.
#
#  idx 0 Read
#      1 THOUGHT      front-half (1 < 4)
#      2 Edit         ADJACENT (preceded by thought)
#      3 Read
#      4 THOUGHT      NOT front-half (4 < 4 is false)  <-- discriminator
#      5 Edit         ADJACENT
#      6 Bash
#      7 Write        not adjacent
#
# => tools 8, thoughts 2, mutations 3, adjacency 2, front-half 1
RUN_A = ["Read", THOUGHT, "Edit", "Read", THOUGHT, "Edit", "Bash", "Write"]

# Run B carries mutations but ZERO thoughts, so pct-zero has a real numerator
# and adjacency has a non-adjacent denominator contribution.
# => tools 3, thoughts 0, mutations 2, adjacency 0
RUN_B = ["Read", "Edit", "Write"]

# Run C is OUT OF WINDOW and deliberately large: if the window filter breaks,
# these 40 mutations land in the totals and every assertion below fails loudly.
RUN_C = ["Edit"] * 40


@pytest.fixture
def corpus(tmp_path: Path) -> Path:
    root = tmp_path / "projects"
    session = "11111111-1111-1111-1111-111111111111"

    def sub(agent_id: str, tools: list[str], ts: str, agent_type: str) -> None:
        p = root / session / "subagents" / f"agent-{agent_id}.jsonl"
        _write_run(p, tools, ts)
        meta = p.with_name(f"agent-{agent_id}.meta.json")
        meta.write_text(json.dumps({"agentType": agent_type}))

    sub("aaa", RUN_A, IN_WINDOW_TS, "conexus:developer")
    sub("bbb", RUN_B, IN_WINDOW_TS, "conexus:developer")
    sub("ccc", RUN_C, OUT_OF_WINDOW_TS, "conexus:developer")

    # Top-level session file. The two sidechain records are subagent traffic
    # replayed into the session file and MUST be excluded, so a broken filter
    # inflates top-level tools from 2 to 4.
    top = root / f"{session}.jsonl"
    top.parent.mkdir(parents=True, exist_ok=True)
    top.write_text(
        "\n".join(
            [
                _assistant(THOUGHT, IN_WINDOW_TS),
                _assistant("Edit", IN_WINDOW_TS),
                _assistant("Edit", IN_WINDOW_TS, sidechain=True),
                _assistant("Write", IN_WINDOW_TS, sidechain=True),
            ]
        )
        + "\n"
    )
    return root


def _run(root: Path, tmp_path: Path, *extra: str):
    cmd = [
        sys.executable, str(SCRIPT),
        "--root", str(root),
        "--ledger", str(tmp_path / "no-such-archive"),
        "--start", WINDOW_START,
        "--end", WINDOW_END,
        "--min-typed-agents", "1",
        "--min-mutations", "1",
        *extra,
    ]
    return subprocess.run(cmd, capture_output=True, text=True)


def _load(root: Path, tmp_path: Path, name: str, *extra: str) -> dict:
    out = tmp_path / name
    proc = _run(root, tmp_path, "--json-out", str(out), *extra)
    assert proc.returncode == 0, proc.stderr
    return json.loads(out.read_text())


class TestMetricArithmetic:
    """Every expected value here is derived by hand from RUN_A/RUN_B above."""

    def test_developer_row_matches_hand_computed_values(self, corpus, tmp_path):
        res = _load(corpus, tmp_path, "m.json")
        dev = res["by_agent_type"]["conexus:developer"]

        assert dev["runs"] == 2
        assert dev["tool_calls"] == 11          # 8 + 3
        assert dev["thoughts"] == 2
        assert dev["mutations"] == 5            # 3 + 2
        assert dev["thoughts_per_mutation"] == pytest.approx(0.4)
        assert dev["thoughts_per_100_tools"] == pytest.approx(18.182, abs=1e-3)
        assert dev["pct_runs_zero_thoughts"] == pytest.approx(50.0)

    def test_adjacency_counts_only_immediately_preceded_mutations(
        self, corpus, tmp_path
    ):
        # 2 of 5 mutations are immediately preceded by a thought. If adjacency
        # were counting "runs containing a thought" or "thoughts", this reads
        # 1 or 2 out of 2 instead of 2 out of 5.
        dev = _load(corpus, tmp_path, "m.json")["by_agent_type"]["conexus:developer"]
        assert dev["adjacent_mutations"] == 2
        assert dev["adjacency_pct"] == pytest.approx(40.0)

    def test_front_half_uses_strict_less_than_midpoint(self, corpus, tmp_path):
        # The thought at index 4 of an 8-call run is NOT front-half. A `<=`
        # implementation reports 2 here, i.e. 100.0 pct.
        dev = _load(corpus, tmp_path, "m.json")["by_agent_type"]["conexus:developer"]
        assert dev["front_half_thoughts"] == 1
        assert dev["front_half_share_pct"] == pytest.approx(50.0)

    def test_out_of_window_run_is_excluded_whole(self, corpus, tmp_path):
        # RUN_C's 40 mutations must not appear anywhere.
        res = _load(corpus, tmp_path, "m.json")
        assert res["by_agent_type"]["conexus:developer"]["mutations"] == 5
        assert res["TOTAL"]["mutations"] == 6   # 5 subagent + 1 top-level
        assert res["by_agent_type"]["conexus:developer"]["runs"] == 2

    def test_sidechain_records_excluded_from_toplevel(self, corpus, tmp_path):
        top = _load(corpus, tmp_path, "m.json")["by_agent_type"]["__toplevel__"]
        assert top["tool_calls"] == 2           # not 4
        assert top["mutations"] == 1            # not 3


class TestEveryRunEmitsAllFiveMetrics:
    """nexus-4bqre.8 audit fix 2: pre and post must be diffable untransformed."""

    FIVE = (
        "thoughts_per_mutation",
        "thoughts_per_100_tools",
        "pct_runs_zero_thoughts",
        "adjacency_pct",
        "front_half_share_pct",
    )

    def test_all_five_present_for_every_agent_type_with_denominator(
        self, corpus, tmp_path
    ):
        res = _load(corpus, tmp_path, "m.json")
        for name, row in res["by_agent_type"].items():
            for metric in self.FIVE:
                assert metric in row, f"{name} missing {metric}"
            assert "mutations" in row, f"{name} missing the denominator"

    def test_schema_is_stable_and_versioned(self, corpus, tmp_path):
        res = _load(corpus, tmp_path, "m.json")
        assert res["schema"] == "decision-coverage-census/v1"

    def test_frozen_baseline_schema_matches_this_runs_schema(
        self, corpus, tmp_path
    ):
        """The pre-baseline and any post run must share a key set.

        Skips rather than fails when the frozen artifact is not on this box:
        it lives in XDG state, not the repo, so CI has no copy. The
        non-vacuity doctrine applies to the CENSUS, not to this portability
        check -- but the skip is narrow and named so it cannot quietly become
        the normal outcome.
        """
        frozen = (
            Path.home() / ".local/state/nexus/census/frozen-baseline-2026-08-22.json"
        )
        if not frozen.exists():
            pytest.skip("frozen baseline artifact not present on this machine")
        base = json.loads(frozen.read_text())
        res = _load(corpus, tmp_path, "m.json")
        assert set(res) == set(base)
        assert set(res["TOTAL"]) == set(base["TOTAL"])
        a_row = next(iter(res["by_agent_type"].values()))
        b_row = next(iter(base["by_agent_type"].values()))
        assert set(a_row) == set(b_row)


class TestNonVacuityFloor:
    """nexus-moht0 doctrine: a census that found nothing is a FAILURE."""

    def test_starved_corpus_exits_non_zero(self, corpus, tmp_path):
        proc = subprocess.run(
            [
                sys.executable, str(SCRIPT),
                "--root", str(corpus),
                "--ledger", str(tmp_path / "none"),
                "--start", WINDOW_START,
                "--end", WINDOW_END,
            ],  # default floors: 50 typed agents, 500 mutations
            capture_output=True, text=True,
        )
        assert proc.returncode == 2
        assert "VACUOUS" in proc.stderr

    def test_mutation_floor_fires_independently_of_agent_floor(
        self, corpus, tmp_path
    ):
        proc = _run(corpus, tmp_path, "--min-mutations", "10000")
        assert proc.returncode == 2
        assert "VACUOUS" in proc.stderr
        assert "mutations" in proc.stderr

    def test_healthy_corpus_does_not_trip_the_floor(self, corpus, tmp_path):
        # Without this, "always exits 2" would satisfy both tests above.
        assert _run(corpus, tmp_path).returncode == 0


class TestPinFixtureIsNotVacuous:
    """The pin fixture must be multibyte or every pin test below is vacuous."""

    def test_fixture_records_have_more_bytes_than_characters(self, corpus):
        live = corpus / "11111111-1111-1111-1111-111111111111.jsonl"
        raw = live.read_bytes()
        assert len(raw) > len(raw.decode("utf-8")), (
            "fixture regressed to ASCII: a character-prefix read would then "
            "equal a byte-prefix read and the pin tests prove nothing"
        )


class TestPinReproducibility:
    """The pin exists for the LIVE-APPEND race, so the test must append.

    The first census attempt was not reproducible: two runs seconds apart over
    an identical file set returned 87363 then 87371 tool calls, because the
    running session's own transcript was being appended to mid-read. Pinning a
    static fixture and re-reading it would pass without exercising any of that.
    """

    def test_appending_after_the_pin_does_not_change_the_numbers(
        self, corpus, tmp_path
    ):
        first = _load(corpus, tmp_path, "first.json")

        live = corpus / "11111111-1111-1111-1111-111111111111.jsonl"
        with live.open("a") as fh:
            for _ in range(25):
                fh.write(_assistant("Edit", IN_WINDOW_TS) + "\n")

        unpinned = _load(corpus, tmp_path, "unpinned.json")
        assert unpinned["TOTAL"]["mutations"] != first["TOTAL"]["mutations"], (
            "fixture bug: the append did not change the unpinned result, so "
            "this test cannot detect whether the pin is doing anything"
        )

        pinned = _load(
            corpus, tmp_path, "pinned.json", "--pin", str(tmp_path / "first.json")
        )
        assert pinned["pin_mismatches"] == []
        assert pinned["TOTAL"] == first["TOTAL"]
        assert pinned["by_agent_type"] == first["by_agent_type"]

    def test_two_pinned_runs_are_byte_identical(self, corpus, tmp_path):
        _load(corpus, tmp_path, "base.json")
        a = _load(corpus, tmp_path, "a.json", "--pin", str(tmp_path / "base.json"))
        b = _load(corpus, tmp_path, "b.json", "--pin", str(tmp_path / "base.json"))
        assert json.dumps(a, sort_keys=True) == json.dumps(b, sort_keys=True)

    def test_pin_reports_mismatch_when_pinned_bytes_change(
        self, corpus, tmp_path
    ):
        _load(corpus, tmp_path, "base.json")
        # Rewrite a pinned prefix in place: same length, different bytes.
        target = corpus / "11111111-1111-1111-1111-111111111111.jsonl"
        body = target.read_text().replace("Edit", "Read", 1)
        target.write_text(body)
        res = _load(
            corpus, tmp_path, "mm.json", "--pin", str(tmp_path / "base.json")
        )
        assert res["pin_mismatches"], (
            "a changed pinned file must be REPORTED, never silently accepted"
        )
