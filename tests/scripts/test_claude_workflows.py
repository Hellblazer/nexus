"""Exercise `.claude/workflows/*.js` against stub Workflow primitives.

Both workflow scripts in this repo had never executed once: each passed its
stage functions as `pipeline`'s ITEMS array and a seed object as its only
stage, so nothing downstream of that call had ever run (bead nexus-xeoa0).
Neither a Python suite nor a lint bucket could see it, because nothing in this
repo had ever loaded those files at all.

These tests run the real script bodies through
`fixtures/workflow_harness.mjs`, whose stubs implement the primitive contract
as the workflow-authoring reference states it. What that proves is bounded and
worth stating: it catches a script that misuses a primitive's shape,
mishandles a `null` dispatch, or lets a hole in coverage read as a clean
result. It does NOT prove the real Workflow runtime agrees with the reference
-- only a real Workflow invocation does that, and that spend is Sam's call.

`test_harness_rejects_the_pipeline_inversion` is the non-vacuity assert: it
feeds the harness the exact defect these files carried and requires it to
fail. Without that, a green run here would say nothing about the bug it exists
to catch.
"""

from __future__ import annotations

import json
import shutil
import subprocess
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parents[2]
HARNESS = Path(__file__).parent / "fixtures" / "workflow_harness.mjs"
WORKFLOW_DIR = REPO_ROOT / ".claude" / "workflows"
DEAD_WIRE_CENSUS = WORKFLOW_DIR / "dead-wire-census.js"
PRESSURE_TEST = WORKFLOW_DIR / "pressure-test.js"

pytestmark = pytest.mark.skipif(
    shutil.which("node") is None, reason="node is not installed on this box"
)


def code_only(script: Path) -> str:
    """The script with its full-line `//` comments removed.

    Every comment in these two files is a full-line `//` comment, and their
    headers quote the defective forms these tests search for on purpose. A
    textual check run over the raw text would match the prose describing the
    bug instead of the code carrying it.
    """
    return "\n".join(
        line
        for line in script.read_text(encoding="utf-8").splitlines()
        if not line.lstrip().startswith("//")
    )


def run_workflow(script: Path, scenario: dict, tmp_path: Path) -> dict:
    """Run `script` under the stub harness and return the harness's report."""
    scenario_path = tmp_path / "scenario.json"
    scenario_path.write_text(json.dumps(scenario), encoding="utf-8")
    proc = subprocess.run(
        ["node", str(HARNESS), str(script), str(scenario_path)],
        capture_output=True,
        text=True,
        cwd=REPO_ROOT,
    )
    assert proc.returncode == 0, (
        f"harness itself failed (rc={proc.returncode}):\n{proc.stderr}"
    )
    return json.loads(proc.stdout)


# --- the non-vacuity assert -------------------------------------------------


def test_harness_rejects_the_pipeline_inversion(tmp_path: Path) -> None:
    """The harness must fail on the exact defect nexus-xeoa0 recorded.

    A stub that shrugged at `pipeline([stageFn, stageFn], {})` would let every
    other test here pass while proving nothing.
    """
    inverted = tmp_path / "inverted.js"
    inverted.write_text(
        "export const meta = {name: 'inverted', description: 'x'};\n"
        "const stageA = async (ctx) => ({...ctx, a: 1});\n"
        "const stageB = async (ctx) => ({...ctx, b: 2});\n"
        "const finalCtx = await pipeline([stageA, stageB], {});\n"
        "return finalCtx;\n",
        encoding="utf-8",
    )
    report = run_workflow(inverted, {"args": {}}, tmp_path)
    assert report["error"] is not None, (
        "the harness accepted stage functions as pipeline's items array; "
        "it cannot certify anything about these workflows"
    )
    assert "callable" in report["error"]["message"]


def test_harness_rejects_promises_where_parallel_wants_thunks(
    tmp_path: Path,
) -> None:
    """The second half of the same class: `parallel()` takes thunks."""
    eager = tmp_path / "eager.js"
    eager.write_text(
        "export const meta = {name: 'eager', description: 'x'};\n"
        "const jobs = [agent('one', {label: 'one'})];\n"
        "return await parallel(jobs);\n",
        encoding="utf-8",
    )
    report = run_workflow(eager, {"args": {}, "agentDefault": {"ok": True}}, tmp_path)
    assert report["error"] is not None
    assert "thunk" in report["error"]["message"]


# --- dead-wire-census -------------------------------------------------------


def test_dead_wire_census_traces_verifies_and_reports_holes(
    tmp_path: Path,
) -> None:
    scenario = {
        "args": {"surface": "cli-verbs"},
        "budget": {"total": 500_000},
        "agents": {
            "enumerate": {
                "items": [
                    {"id": "a", "location": "f1"},
                    {"id": "b", "location": "f2"},
                    {"id": "c", "location": "f3"},
                ]
            },
            "trace:a": {
                "id": "a",
                "classification": "live",
                "evidence": "called in cli.py",
            },
            "trace:b": {
                "id": "b",
                "classification": "dead",
                "evidence": "no callers",
            },
            # A dispatch that died: agent() resolves to null, it does not throw.
            "trace:c": None,
            "verify:b": {
                "id": "b",
                "upheld": False,
                "reasoning": "reached via config dispatch",
            },
        },
    }
    report = run_workflow(DEAD_WIRE_CENSUS, scenario, tmp_path)
    assert report["error"] is None
    result = report["result"]

    # Only the candidate-dead item gets an adversarial second look.
    labels = [d["label"] for d in report["dispatched"]]
    assert labels == ["enumerate", "trace:a", "trace:b", "trace:c", "verify:b"]

    rows = {row["id"]: row for row in result["rows"]}
    assert rows["a"]["classification"] == "live"
    assert rows["b"]["classification"] == "live (overturned on verify)"
    assert rows["b"]["verification"] == "reached via config dispatch"

    # The dropped item is a hole in the census, reported by id, not omitted.
    assert result["droppedIds"] == ["c"]
    assert rows["c"]["traceDropped"] is True
    assert result["complete"] is False
    assert any("never got a trace result" in line for line in report["logs"])


def test_dead_wire_census_flags_an_unverified_candidate(tmp_path: Path) -> None:
    """A candidate-dead item whose verify dispatch died is not a settled row."""
    scenario = {
        "args": {"surface": "routes"},
        "agents": {
            "enumerate": {"items": [{"id": "r1", "location": "H.java"}]},
            "trace:r1": {
                "id": "r1",
                "classification": "suspected",
                "evidence": "no literal found",
            },
            "verify:r1": None,
        },
    }
    result = run_workflow(DEAD_WIRE_CENSUS, scenario, tmp_path)["result"]
    assert result["unverifiedCandidateIds"] == ["r1"]
    assert result["rows"][0]["verificationMissing"] is True
    assert result["rows"][0]["classification"] == "suspected"
    assert result["complete"] is False


def test_dead_wire_census_keys_rows_on_the_enumerated_id(tmp_path: Path) -> None:
    """Row identity comes from the enumeration, not the trace agent's echo.

    `droppedIds` and `unverifiedCandidateIds` are keyed on `item.id`. Keying
    `rows[].id` on the id the trace agent retyped means a single divergent
    echo silently breaks the cross-reference inside one payload -- and every
    other scenario here hand-constructs the two as equal, so nothing else
    exercises the divergence.
    """
    scenario = {
        "args": {"surface": "routes"},
        "agents": {
            "enumerate": {
                "items": [
                    {"id": "GET /v1/remap", "location": "RemapHandler.java"},
                    {"id": "GET /v1/staging", "location": "StagingHandler.java"},
                ]
            },
            # The agent echoes a normalised id that is not the one it was given.
            "trace:GET /v1/remap": {
                "id": "remap",
                "classification": "dead",
                "evidence": "no client literal",
            },
            "trace:GET /v1/staging": {
                "id": "v1/staging",
                "classification": "suspected",
                "evidence": "no literal found",
            },
            "verify:GET /v1/remap": {
                "id": "remap",
                "upheld": True,
                "reasoning": "stands",
            },
            "verify:GET /v1/staging": None,
        },
    }
    result = run_workflow(DEAD_WIRE_CENSUS, scenario, tmp_path)["result"]

    assert [row["id"] for row in result["rows"]] == [
        "GET /v1/remap",
        "GET /v1/staging",
    ]
    # The cross-reference holds: every flagged id is findable among the rows.
    row_ids = {row["id"] for row in result["rows"]}
    assert set(result["unverifiedCandidateIds"]) <= row_ids
    assert result["unverifiedCandidateIds"] == ["GET /v1/staging"]


def test_dead_wire_census_calls_an_empty_enumeration_inconclusive(
    tmp_path: Path,
) -> None:
    """The vacuous-gate doctrine: a sweep that found nothing is not a pass."""
    scenario = {
        "args": {"surface": "mcp-tools"},
        "agents": {"enumerate": {"items": []}},
    }
    report = run_workflow(DEAD_WIRE_CENSUS, scenario, tmp_path)
    assert report["result"]["complete"] is False
    assert any("ZERO items" in line for line in report["logs"])


def test_dead_wire_census_requires_a_surface(tmp_path: Path) -> None:
    report = run_workflow(DEAD_WIRE_CENSUS, {"args": {}}, tmp_path)
    assert report["error"] is not None
    assert "args.surface" in report["error"]["message"]


def test_dead_wire_census_fails_loudly_when_enumeration_dies(
    tmp_path: Path,
) -> None:
    scenario = {"args": {"surface": "skills"}, "agents": {"enumerate": None}}
    report = run_workflow(DEAD_WIRE_CENSUS, scenario, tmp_path)
    assert report["error"] is not None
    assert "Nothing to census" in report["error"]["message"]


# --- pressure-test ----------------------------------------------------------


def _pt_reviews() -> dict:
    return {
        "review:code-mechanics": {
            "lens": "code-mechanics",
            "findings": [{"severity": "critical", "claim": "c1", "evidence": "e1"}],
        },
        "review:spec-fidelity": {
            "lens": "spec-fidelity",
            "findings": [{"severity": "minor", "claim": "c2", "evidence": "e2"}],
        },
        "review:adversarial-revert-case": {
            "lens": "adversarial-revert-case",
            "findings": [],
        },
    }


def test_pressure_test_votes_then_synthesizes(tmp_path: Path) -> None:
    agents = _pt_reviews()
    # c1 is critical: three votes, all upholding. c2 is minor: one vote.
    for i in range(3):
        agents[f"verify:code-mechanics:0:{i}"] = {
            "refuted": False,
            "reasoning": "stands",
        }
    agents["verify:spec-fidelity:1:0"] = {"refuted": True, "reasoning": "wrong"}
    agents["synthesize"] = {
        "verdict": "fix-then-ship",
        "ranked": [{"severity": "critical", "claim": "c1", "evidence": "e1"}],
    }
    scenario = {
        "args": {"target": "diff X", "spec": "directive Y"},
        "agents": agents,
    }
    report = run_workflow(PRESSURE_TEST, scenario, tmp_path)
    assert report["error"] is None
    result = report["result"]

    assert result["findingCount"] == 2
    # The minor finding's single vote refuted it; the critical one survived.
    assert result["survivingCount"] == 1
    assert result["verdict"] == "fix-then-ship"
    assert result["unverifiedFindings"] == []
    assert result["complete"] is True

    labels = [d["label"] for d in report["dispatched"]]
    assert labels.count("verify:code-mechanics:0:0") == 1
    assert sum(1 for label in labels if label.startswith("verify:")) == 4


def test_pressure_test_queues_votes_round_major(tmp_path: Path) -> None:
    """Every finding's first vote is queued before any finding's second.

    Finding-major order means a budget that runs out mid-fan-out gives the
    first findings every vote and the last ones none. Round-major degrades
    evenly instead. `parallel()` invokes its thunks in array order, so the
    harness's dispatch log is the queue order.
    """
    agents = {
        "review:code-mechanics": {
            "lens": "code-mechanics",
            "findings": [{"severity": "critical", "claim": "c1", "evidence": "e1"}],
        },
        "review:spec-fidelity": {
            "lens": "spec-fidelity",
            "findings": [{"severity": "critical", "claim": "c2", "evidence": "e2"}],
        },
        "review:adversarial-revert-case": {
            "lens": "adversarial-revert-case",
            "findings": [],
        },
        "synthesize": {"verdict": "fix-then-ship", "ranked": []},
    }
    for finding_index, lens in ((0, "code-mechanics"), (1, "spec-fidelity")):
        for vote_index in range(3):
            agents[f"verify:{lens}:{finding_index}:{vote_index}"] = {
                "refuted": False,
                "reasoning": "stands",
            }
    scenario = {
        "args": {"target": "diff X", "spec": "directive Y"},
        "agents": agents,
    }
    report = run_workflow(PRESSURE_TEST, scenario, tmp_path)

    verify_order = [
        d["label"] for d in report["dispatched"] if d["label"].startswith("verify:")
    ]
    assert verify_order == [
        "verify:code-mechanics:0:0",
        "verify:spec-fidelity:1:0",
        "verify:code-mechanics:0:1",
        "verify:spec-fidelity:1:1",
        "verify:code-mechanics:0:2",
        "verify:spec-fidelity:1:2",
    ]


def test_pressure_test_reports_a_finding_no_vote_landed_on(
    tmp_path: Path,
) -> None:
    """Zero landed votes is not a survival. The old majority rule said it was.

    `refutedCount * 2 <= votesForThis.length` is 0 <= 0 when every vote agent
    died, so a finding nobody checked would have been passed to synthesis under
    a prompt asserting it had already survived adversarial refutation.
    """
    agents = _pt_reviews()
    for i in range(3):
        agents[f"verify:code-mechanics:0:{i}"] = None
    agents["verify:spec-fidelity:1:0"] = {"refuted": False, "reasoning": "stands"}
    agents["synthesize"] = {"verdict": "ship", "ranked": []}
    scenario = {
        "args": {"target": "diff X", "spec": "directive Y"},
        "agents": agents,
    }
    report = run_workflow(PRESSURE_TEST, scenario, tmp_path)
    result = report["result"]

    assert [f["claim"] for f in result["unverifiedFindings"]] == ["c1"]
    assert result["survivingCount"] == 1  # only c2, which did get a vote
    assert result["complete"] is False
    assert any("ZERO landed verification votes" in line for line in report["logs"])


def test_pressure_test_reports_no_verdict_rather_than_ship(
    tmp_path: Path,
) -> None:
    """Nothing survived: the verdict is null, and no synthesis agent runs."""
    agents = _pt_reviews()
    for i in range(3):
        agents[f"verify:code-mechanics:0:{i}"] = {
            "refuted": True,
            "reasoning": "refuted",
        }
    agents["verify:spec-fidelity:1:0"] = {"refuted": True, "reasoning": "refuted"}
    scenario = {
        "args": {"target": "diff X", "spec": "directive Y"},
        "agents": agents,
    }
    report = run_workflow(PRESSURE_TEST, scenario, tmp_path)
    result = report["result"]

    assert result["verdict"] is None
    assert result["ranked"] == []
    assert "synthesize" not in [d["label"] for d in report["dispatched"]]


def test_pressure_test_reports_a_lens_that_never_reported(tmp_path: Path) -> None:
    agents = _pt_reviews()
    agents["review:adversarial-revert-case"] = None
    for i in range(3):
        agents[f"verify:code-mechanics:0:{i}"] = {
            "refuted": False,
            "reasoning": "stands",
        }
    agents["verify:spec-fidelity:1:0"] = {"refuted": False, "reasoning": "stands"}
    agents["synthesize"] = {"verdict": "fix-then-ship", "ranked": []}
    scenario = {
        "args": {"target": "diff X", "spec": "directive Y"},
        "agents": agents,
    }
    report = run_workflow(PRESSURE_TEST, scenario, tmp_path)

    assert report["result"]["reviewerCount"] == 2
    assert report["result"]["reviewerRequested"] == 3
    assert report["result"]["complete"] is False
    assert any("returned nothing" in line for line in report["logs"])


def test_pressure_test_requires_target_and_spec(tmp_path: Path) -> None:
    missing_target = run_workflow(PRESSURE_TEST, {"args": {"spec": "s"}}, tmp_path)
    assert "args.target" in missing_target["error"]["message"]

    missing_spec = run_workflow(PRESSURE_TEST, {"args": {"target": "t"}}, tmp_path)
    assert "args.spec" in missing_spec["error"]["message"]


# --- shared shape -----------------------------------------------------------


@pytest.mark.parametrize("script", [DEAD_WIRE_CENSUS, PRESSURE_TEST])
def test_meta_phases_are_titled_entries_matching_the_agent_phases(
    script: Path, tmp_path: Path
) -> None:
    """`meta.phases` is one `{title, detail?}` per progress group.

    Both files shipped a bare array of strings, which names no group the
    runtime can match an agent's `phase` against.
    """
    source = code_only(script)
    report = run_workflow(script, {"args": {}}, tmp_path)
    phases = report["meta"]["phases"]
    assert all(isinstance(p, dict) and p.get("title") for p in phases)

    # Every declared phase title is actually used by at least one agent call.
    for phase in phases:
        assert f"phase: '{phase['title']}'" in source


@pytest.mark.parametrize("script", [DEAD_WIRE_CENSUS, PRESSURE_TEST])
def test_no_export_default_result_channel(script: Path) -> None:
    """The body runs inside an async function; the result is a bare `return`."""
    source = code_only(script)
    assert "export default" not in source


@pytest.mark.parametrize("script", [DEAD_WIRE_CENSUS, PRESSURE_TEST])
def test_budget_is_read_as_the_documented_method(script: Path) -> None:
    """`budget.remaining` is a METHOD.

    Both files tested `typeof budget.remaining === 'number'`, which a method
    fails, so every read fell through to an `Infinity` fallback and the cap it
    guarded never fired once.
    """
    source = code_only(script)
    assert "typeof budget.remaining === 'number'" not in source
    if "budget.remaining" in source:
        assert "budget.remaining()" in source


@pytest.mark.parametrize("script", [DEAD_WIRE_CENSUS, PRESSURE_TEST])
def test_no_nondeterministic_builtins(script: Path) -> None:
    """The runtime throws on these: they would break resume."""
    source = code_only(script)
    for forbidden in ("Date.now(", "Math.random(", "new Date()"):
        assert forbidden not in source
