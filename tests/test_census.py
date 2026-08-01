# SPDX-License-Identifier: AGPL-3.0-or-later
"""nexus-h33x8.1: per-capability tool-use census over transcript JSONL.

Fixture-driven. The parser is a pure function over parsed records, so
none of this touches ``~/.claude`` — the live transcript corpus is
append-only ambient state that grows while a session runs, which is
exactly why the golden numbers in nexus-3sij7 are a mid-session prefix
rather than a file's final content. The golden check therefore runs
against a FROZEN fixture (see ``TestGoldenSession`` and
``tests/fixtures/census/PROVENANCE.md``), never a live path.

The non-vacuity block is the load-bearing part. A census that returns a
clean zero when it measured *nothing* reproduces the false-clean failure
of nexus-nu7fo, where ``undeclared=0`` was structurally unfalsifiable.
"""
from __future__ import annotations

import json
import pathlib

import pytest
from click.testing import CliRunner

from nexus.census import (
    SUBSTANTIAL_THRESHOLD,
    UNMEASURABLE_EMPTY,
    UNMEASURABLE_NO_TOOL_USE,
    UNMEASURABLE_UNPARSEABLE,
    UNMEASURABLE_UNREADABLE,
    census_corpus,
    census_session,
    classify_tool,
    count_tool_uses,
    default_project_dir,
    iter_tool_use_blocks,
    render_text,
    to_json,
)
from nexus.cli import main
from nexus.commands.census import census_group

NEX = "mcp__plugin_conexus_nexus__"


# --------------------------------------------------------------------------
# fixtures
# --------------------------------------------------------------------------

def _assistant(*tool_names: str, sidechain: bool = False, ts: str = "2026-07-31T12:00:00.000Z") -> dict:
    """Build one assistant record carrying ``tool_names`` as tool_use blocks."""
    return {
        "type": "assistant",
        "isSidechain": sidechain,
        "timestamp": ts,
        "message": {
            "role": "assistant",
            "content": [{"type": "tool_use", "name": n, "input": {}} for n in tool_names],
        },
    }


def _write(path: pathlib.Path, records: list[dict]) -> pathlib.Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text("".join(json.dumps(r) + "\n" for r in records))
    return path


@pytest.fixture()
def corpus(tmp_path: pathlib.Path) -> pathlib.Path:
    """A two-session project dir: one orchestrator-only, one with subagents."""
    root = tmp_path / "project"
    _write(root / "sess-a.jsonl", [
        _assistant("Bash", "Bash", "Read"),
        _assistant("Skill"),
        {"type": "user", "message": {"role": "user", "content": "hi"}},
    ])
    _write(root / "sess-b.jsonl", [_assistant("Agent", "Bash")])
    _write(root / "sess-b" / "subagents" / "agent-a1.jsonl", [
        _assistant(NEX + "plan_search", NEX + "scratch", sidechain=True),
    ])
    _write(root / "sess-b" / "subagents" / "workflows" / "wf_1" / "agent-a2.jsonl", [
        _assistant("mcp__plugin_sn_serena__jet_brains_find_symbol", sidechain=True),
    ])
    # tool-results/ siblings are persisted outputs, never transcripts.
    (root / "sess-b" / "tool-results").mkdir(parents=True)
    (root / "sess-b" / "tool-results" / "out.txt").write_text("not a transcript")
    return root


# --------------------------------------------------------------------------
# classification
# --------------------------------------------------------------------------

@pytest.mark.parametrize(
    ("tool", "expected"),
    [
        ("Skill", "skill"),
        ("Agent", "agent"),
        ("mcp__plugin_sn_serena__jet_brains_find_symbol", "serena"),
        ("mcp__plugin_sn_serena__replace_in_files", "serena"),
        (NEX + "nx_answer", "nx_answer"),
        (NEX + "search", "search_query"),
        (NEX + "query", "search_query"),
        (NEX + "search_graph_hop", "search_query"),
        (NEX + "search_metadata_scoped", "search_query"),
        (NEX + "search_topic_scoped", "search_query"),
        (NEX + "memory_put", "other_nx_mcp"),
        (NEX + "plan_search", "other_nx_mcp"),
        ("mcp__plugin_conexus_nexus-catalog__search", "search_query"),
        ("mcp__plugin_conexus_nexus-catalog__link_query", "other_nx_mcp"),
        ("Bash", "baseline"),
        ("Read", "baseline"),
        ("Edit", "baseline"),
        ("Write", "baseline"),
        ("WebSearch", "other"),
        ("ToolSearch", "other"),
        ("mcp__devonthink__search_records", "other"),
    ],
)
def test_classify_tool(tool: str, expected: str) -> None:
    assert classify_tool(tool) == expected


def test_classify_tool_does_not_swallow_serena_into_search_query() -> None:
    """Serena's own search tool is a Serena use, not a retrieval use.

    Both buckets are epic signals (Serena 13%, search/query 33%); a
    substring rule that routed ``search_for_pattern`` to search_query
    would inflate one at the other's expense.
    """
    assert classify_tool("mcp__plugin_sn_serena__search_for_pattern") == "serena"


# --------------------------------------------------------------------------
# pure counting
# --------------------------------------------------------------------------

def test_count_tool_uses_is_pure_over_records() -> None:
    records = [
        _assistant("Bash", "Bash"),
        {"type": "user", "message": {"role": "user", "content": "x"}},
        _assistant("Skill"),
        {"type": "attachment"},
    ]
    counts, parsed = count_tool_uses(records)
    assert counts == {"Bash": 2, "Skill": 1}
    assert parsed == 4


def test_count_tool_uses_ignores_non_tool_use_blocks() -> None:
    rec = {
        "type": "assistant",
        "message": {"role": "assistant", "content": [
            {"type": "text", "text": "I will run Bash"},
            {"type": "thinking", "thinking": "Agent Skill Serena"},
        ]},
    }
    counts, _ = count_tool_uses([rec])
    assert counts == {}


# --------------------------------------------------------------------------
# session-level: the orchestrator / subagent split
# --------------------------------------------------------------------------

def test_session_splits_orchestrator_from_subagent(corpus: pathlib.Path) -> None:
    """The split is the epic's sharpest signal; collapsing it hides the finding."""
    sess = census_session(corpus, "sess-b")
    assert sess.measurable
    assert sess.orchestrator == {"Agent": 1, "Bash": 1}
    assert sess.subagent == {
        NEX + "plan_search": 1,
        NEX + "scratch": 1,
        "mcp__plugin_sn_serena__jet_brains_find_symbol": 1,
    }
    assert sess.subagent_files == 2  # includes the nested workflows/ agent


def test_session_ignores_tool_results_directory(corpus: pathlib.Path) -> None:
    sess = census_session(corpus, "sess-b")
    assert sess.parse_errors == 0
    assert sess.subagent_files == 2


def test_session_without_subagents(corpus: pathlib.Path) -> None:
    sess = census_session(corpus, "sess-a")
    assert sess.orchestrator == {"Bash": 2, "Read": 1, "Skill": 1}
    assert sess.subagent == {}
    assert sess.subagent_files == 0


# --------------------------------------------------------------------------
# NON-VACUITY — a measured zero and a measured-nothing must differ
# --------------------------------------------------------------------------

def test_unmeasurable_empty_file(tmp_path: pathlib.Path) -> None:
    root = tmp_path / "p"
    root.mkdir()
    (root / "s.jsonl").write_text("")
    sess = census_session(root, "s")
    assert not sess.measurable
    assert sess.unmeasurable_reason == UNMEASURABLE_EMPTY


def test_unmeasurable_corrupt_json(tmp_path: pathlib.Path) -> None:
    root = tmp_path / "p"
    root.mkdir()
    (root / "s.jsonl").write_text('{"type": "assistant", "mess\n{ broken\n')
    sess = census_session(root, "s")
    assert not sess.measurable
    assert sess.unmeasurable_reason == UNMEASURABLE_UNPARSEABLE
    assert sess.parse_errors == 2


def test_unmeasurable_valid_transcript_with_no_tool_use(tmp_path: pathlib.Path) -> None:
    root = tmp_path / "p"
    root.mkdir()
    _write(root / "s.jsonl", [
        {"type": "user", "message": {"role": "user", "content": "hello"}},
        {"type": "assistant", "message": {"role": "assistant", "content": [
            {"type": "text", "text": "hi"},
        ]}},
    ])
    sess = census_session(root, "s")
    assert not sess.measurable
    assert sess.unmeasurable_reason == UNMEASURABLE_NO_TOOL_USE


def test_partial_parse_is_flagged_not_silent(tmp_path: pathlib.Path) -> None:
    """A file that parses *mostly* is measurable — but never silently so."""
    root = tmp_path / "p"
    root.mkdir()
    (root / "s.jsonl").write_text(
        json.dumps(_assistant("Bash")) + "\n{ truncated\n"
    )
    sess = census_session(root, "s")
    assert sess.measurable
    assert sess.parse_errors == 1
    assert sess.partial


@pytest.mark.parametrize("body", ["", '{ broken\n', '{"type":"user","message":{"role":"user","content":"x"}}\n'])
def test_corpus_of_only_unmeasurable_sessions_fails(tmp_path: pathlib.Path, body: str) -> None:
    """The whole point: measuring nothing must never render as a clean zero."""
    root = tmp_path / "p"
    root.mkdir()
    (root / "s.jsonl").write_text(body)
    result = census_corpus(root)
    assert result.measurable_sessions == 0
    assert result.exit_code != 0
    assert result.unmeasurable_sessions == 1


def test_corpus_with_one_measurable_session_succeeds(corpus: pathlib.Path) -> None:
    result = census_corpus(corpus)
    assert result.measurable_sessions == 2
    assert result.exit_code == 0


def test_corpus_reports_unmeasurable_alongside_measurable(corpus: pathlib.Path) -> None:
    (corpus / "sess-c.jsonl").write_text("")
    result = census_corpus(corpus)
    assert result.measurable_sessions == 2
    assert result.unmeasurable_sessions == 1
    assert result.exit_code == 0
    assert result.unmeasurable_by_reason[UNMEASURABLE_EMPTY] == 1


def test_corpus_missing_project_dir_is_unmeasurable(tmp_path: pathlib.Path) -> None:
    result = census_corpus(tmp_path / "does-not-exist")
    assert result.exit_code != 0
    assert result.measurable_sessions == 0


# --------------------------------------------------------------------------
# aggregation + capability roll-up
# --------------------------------------------------------------------------

def test_capability_rollup_counts_sessions_and_calls(corpus: pathlib.Path) -> None:
    result = census_corpus(corpus)
    # orchestrator scope: sess-a has Skill, sess-b does not.
    assert result.orchestrator_sessions_using("skill") == 1
    assert result.orchestrator_calls("skill") == 1
    # subagent scope: serena appears only under sess-b's nested workflow agent.
    assert result.subagent_calls("serena") == 1
    assert result.orchestrator_calls("serena") == 0
    # combined scope is what the epic's lifetime column used.
    assert result.total_calls("other_nx_mcp") == 2


def test_since_filters_on_last_record_timestamp(tmp_path: pathlib.Path) -> None:
    root = tmp_path / "p"
    root.mkdir()
    _write(root / "old.jsonl", [_assistant("Bash", ts="2026-01-01T00:00:00.000Z")])
    _write(root / "new.jsonl", [_assistant("Bash", ts="2026-07-31T00:00:00.000Z")])
    result = census_corpus(root, since="2026-06-01")
    assert result.measurable_sessions == 1
    assert [s.session_id for s in result.sessions] == ["new"]


def test_single_session_selector(corpus: pathlib.Path) -> None:
    result = census_corpus(corpus, session="sess-a")
    assert [s.session_id for s in result.sessions] == ["sess-a"]
    assert result.exit_code == 0


def test_single_session_selector_unknown_id_is_unmeasurable(corpus: pathlib.Path) -> None:
    result = census_corpus(corpus, session="nope")
    assert result.exit_code != 0
    assert result.measurable_sessions == 0


# --------------------------------------------------------------------------
# rendering — the verdict refusal is a hard requirement, not a nicety
# --------------------------------------------------------------------------

def test_text_render_refuses_a_compliance_verdict(corpus: pathlib.Path) -> None:
    """Counts are the deliverable; judgement is not (bead HARD REQUIREMENT).

    A census that cannot tell "forgotten" from "correctly rejected" will
    manufacture false compliance debt — see child .6, where nx_answer's
    non-use is probably a rational latency trade.
    """
    out = render_text(census_corpus(corpus))
    assert "no compliance verdict" in out.lower()
    for banned in ("violation", "non-compliant", "should have used", "failure to use"):
        assert banned not in out.lower()


def test_text_render_distinguishes_measured_zero_from_measured_nothing(
    tmp_path: pathlib.Path,
) -> None:
    root = tmp_path / "p"
    root.mkdir()
    _write(root / "measured.jsonl", [_assistant("Bash")])
    out = render_text(census_corpus(root))
    assert "nx_answer" in out
    assert "UNMEASURABLE" not in out  # this corpus WAS measured; zero means zero

    (root / "nothing.jsonl").write_text("")
    out2 = render_text(census_corpus(root))
    assert "UNMEASURABLE" in out2
    assert UNMEASURABLE_EMPTY in out2


def test_json_render_carries_per_tool_counts(corpus: pathlib.Path) -> None:
    """Per-tool detail keeps the epic's narrower search/query number derivable.

    The baseline table's ``search / query = 138`` is exactly
    ``search`` + ``query``; this census's bucket also holds the three
    scoped variants. Both must be recoverable from one run.
    """
    payload = json.loads(to_json(census_corpus(corpus)))
    assert payload["tools"]["subagent"][NEX + "plan_search"] == 1
    assert payload["capabilities"]["all"]["orchestrator"]["skill"]["calls"] == 1
    assert payload["measurable_sessions"] == 2


# --------------------------------------------------------------------------
# CLI boundary — exit codes are the part a caller can act on
# --------------------------------------------------------------------------

def _invoke(args: list[str]):
    return CliRunner().invoke(census_group, args)


def test_cli_measurable_corpus_exits_zero(corpus: pathlib.Path) -> None:
    res = _invoke(["capability", "--project-dir", str(corpus)])
    assert res.exit_code == 0, res.output
    assert "measurable session(s)" in res.output


def test_cli_unmeasurable_corpus_exits_nonzero(tmp_path: pathlib.Path) -> None:
    root = tmp_path / "p"
    root.mkdir()
    (root / "s.jsonl").write_text("")
    res = _invoke(["capability", "--project-dir", str(root)])
    assert res.exit_code != 0
    assert "UNMEASURABLE" in res.output


def test_cli_missing_project_dir_exits_nonzero(tmp_path: pathlib.Path) -> None:
    res = _invoke(["capability", "--project-dir", str(tmp_path / "nope")])
    assert res.exit_code != 0
    assert "UNMEASURABLE" in res.output


def test_cli_json_mode_is_parseable(corpus: pathlib.Path) -> None:
    res = _invoke(["capability", "--project-dir", str(corpus), "--json"])
    assert res.exit_code == 0, res.output
    payload = json.loads(res.output)
    assert payload["verdict"] is None
    assert payload["measurable_sessions"] == 2


def test_cli_registered_on_main() -> None:
    assert "census" in main.commands


def test_default_project_dir_honors_env(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("NX_CENSUS_PROJECT_DIR", "/tmp/somewhere")
    assert str(default_project_dir()) == "/tmp/somewhere"


def test_default_project_dir_slugifies_cwd(
    monkeypatch: pytest.MonkeyPatch, tmp_path: pathlib.Path
) -> None:
    monkeypatch.delenv("NX_CENSUS_PROJECT_DIR", raising=False)
    monkeypatch.chdir(tmp_path)
    got = default_project_dir()
    assert got.name == str(tmp_path.resolve()).replace("/", "-")
    assert got.parent.name == "projects"


# --------------------------------------------------------------------------
# GOLDEN SESSION — bead VERIFICATION 1 and 4, against a FROZEN fixture
# --------------------------------------------------------------------------

GOLDEN_SESSION = "session-75695009-prefix.redacted"
GOLDEN_FIXTURE = pathlib.Path(__file__).parent / "fixtures" / "census"

#: nexus-3sij7's evidence line, verbatim. Exact, not >= — a relaxed
#: assertion cannot detect a deleted classification branch, which is what
#: bead VERIFICATION 4 turns on.
GOLDEN_COUNTS = {"Bash": 150, "Edit": 19, "Agent": 7, "Read": 6, "Write": 3, "ToolSearch": 2}


class TestGoldenSession:
    """The evidence session, frozen so the assertion can be exact.

    Transcripts are append-only and this one kept running for hours after
    nexus-3sij7 measured it (Bash 150 -> 190 -> 286). Asserting against
    the live path under ~/.claude would fail on day one and invite a
    relaxation to >=; the plan-audit comment on nexus-h33x8.1 prescribed
    freezing a copy instead. The fixture is a redacted projection —
    envelopes, ordering and tool names preserved exactly, free text
    dropped. See tests/fixtures/census/PROVENANCE.md.

    FALSIFY-BY-DELETION, and a correction to the bead. VERIFICATION 4
    says "delete the Skill-classification branch; test 1 must fail".
    That pairing is VACUOUS BY CONSTRUCTION: test 1 is this golden
    session, and the reason nexus-3sij7 cites it at all is that its
    Skill count is ZERO. Deleting the Skill branch reroutes ``Skill`` to
    ``other`` and changes nothing here, so these tests pass either way —
    verified, not assumed. The property the bead actually wants holds
    for every branch this session EXERCISES: deleting the Agent branch
    fails ``test_golden_session_capability_zeros_are_measured_zeros`` on
    ``agent == 7``. The Skill branch is falsified instead by the
    synthetic fixtures above, which do exercise it.
    """

    def test_golden_session_75695009_exact_counts(self) -> None:
        sess = census_session(GOLDEN_FIXTURE, GOLDEN_SESSION)
        assert sess.measurable
        for tool, expected in GOLDEN_COUNTS.items():
            assert sess.orchestrator.get(tool) == expected, f"{tool} drifted"

    def test_golden_session_capability_zeros_are_measured_zeros(self) -> None:
        """Skill=0 and serena=0 held for this session; nx-MCP did NOT.

        nexus-3sij7 asserts five "never invoked once" bullets. Two survive
        at this prefix. The third does not: TaskStop=1 sits in the same
        prefix and the session later called four nx MCP tools, which is
        why the bullet was corrected on that bead rather than restated here.
        """
        result = census_corpus(GOLDEN_FIXTURE, session=GOLDEN_SESSION)
        assert result.exit_code == 0
        assert result.total_calls("skill") == 0
        assert result.total_calls("serena") == 0
        assert result.total_calls("nx_answer") == 0
        assert result.total_calls("agent") == 7
        assert result.total_calls("baseline") == 150 + 19 + 6 + 3
        assert result.total_calls("other") == 2 + 1  # ToolSearch + TaskStop

    def test_golden_session_is_substantial(self) -> None:
        sess = census_session(GOLDEN_FIXTURE, GOLDEN_SESSION)
        assert sess.total_calls >= SUBSTANTIAL_THRESHOLD
        result = census_corpus(GOLDEN_FIXTURE, session=GOLDEN_SESSION)
        assert result.substantial_sessions == 1

    def test_fixture_retains_subagent_type_for_h33x8_2(self) -> None:
        """The fixture keeps Agent input.subagent_type so .2 can use it."""
        records = [
            json.loads(line)
            for line in (GOLDEN_FIXTURE / f"{GOLDEN_SESSION}.jsonl").read_text().splitlines()
            if line.strip()
        ]
        types = [
            (b.get("input") or {}).get("subagent_type")
            for _i, b in iter_tool_use_blocks(records)
            if b.get("name") == "Agent"
        ]
        assert len([t for t in types if t]) == 7
        assert all(":" in t for t in types if t), "plugin-namespaced types keep their colon"


# --------------------------------------------------------------------------
# review findings — regression pins
# --------------------------------------------------------------------------

def test_unreadable_file_is_distinguished_from_unparseable(tmp_path: pathlib.Path) -> None:
    """A permission problem must not send an operator hunting for bad JSON."""
    root = tmp_path / "p"
    root.mkdir()
    target = root / "s.jsonl"
    target.write_text(json.dumps(_assistant("Bash")) + "\n")
    target.chmod(0o000)
    try:
        sess = census_session(root, "s")
    finally:
        target.chmod(0o644)
    assert not sess.measurable
    assert sess.unmeasurable_reason == UNMEASURABLE_UNREADABLE


def test_unlistable_subagent_dir_does_not_crash(tmp_path: pathlib.Path) -> None:
    """Degrade the session, never die walking the corpus."""
    root = tmp_path / "p"
    root.mkdir()
    _write(root / "s.jsonl", [_assistant("Bash")])
    sub = root / "s" / "subagents"
    sub.mkdir(parents=True)
    _write(sub / "agent-a1.jsonl", [_assistant("Skill", sidechain=True)])
    sub.chmod(0o000)
    try:
        result = census_corpus(root)
        assert isinstance(result.measurable_sessions, int)
    finally:
        sub.chmod(0o755)


def test_unlistable_project_dir_is_unmeasurable(tmp_path: pathlib.Path) -> None:
    root = tmp_path / "p"
    root.mkdir()
    _write(root / "s.jsonl", [_assistant("Bash")])
    root.chmod(0o000)
    try:
        result = census_corpus(root)
        assert result.exit_code != 0
    finally:
        root.chmod(0o755)


def test_since_excluding_the_named_session_says_so(tmp_path: pathlib.Path) -> None:
    """"Not found" and "filtered out" are different facts, same empty result."""
    root = tmp_path / "p"
    root.mkdir()
    _write(root / "old.jsonl", [_assistant("Bash", ts="2026-01-01T00:00:00.000Z")])
    result = census_corpus(root, session="old", since="2026-06-01")
    assert result.exit_code != 0
    assert "predates --since" in (result.scope_error or "")
    assert result.filtered_by_since == 1

    missing = census_corpus(root, session="nope", since="2026-06-01")
    assert "session not found" in (missing.scope_error or "")


def test_since_keeps_sessions_that_carry_no_timestamp(tmp_path: pathlib.Path) -> None:
    """--since must never silently drop what it cannot date."""
    root = tmp_path / "p"
    root.mkdir()
    rec = _assistant("Bash")
    del rec["timestamp"]
    _write(root / "undated.jsonl", [rec])
    result = census_corpus(root, since="2026-06-01")
    assert result.measurable_sessions == 1


def test_subagent_files_matched_by_filename_not_by_being_nested(
    tmp_path: pathlib.Path,
) -> None:
    """Hal's plan-audit MINOR 4: encode the right discriminator.

    A stray non-``agent-*`` JSONL beside the real subagent transcripts
    must not be swept in — "lives in a subdirectory" is the wrong rule.
    """
    root = tmp_path / "p"
    root.mkdir()
    _write(root / "s.jsonl", [_assistant("Bash")])
    sub = root / "s" / "subagents"
    _write(sub / "agent-a1.jsonl", [_assistant("Skill", sidechain=True)])
    _write(sub / "notes.jsonl", [_assistant("Edit", sidechain=True)])
    sess = census_session(root, "s")
    assert sess.subagent == {"Skill": 1}
    assert sess.subagent_files == 1


def test_iter_tool_use_blocks_retains_input_for_h33x8_2(tmp_path: pathlib.Path) -> None:
    """.2 keys on Agent input.subagent_type; a name-only counter would lose it."""
    rec = {
        "type": "assistant",
        "message": {"role": "assistant", "content": [
            {"type": "tool_use", "name": "Agent",
             "input": {"subagent_type": "conexus:substantive-critic"}},
        ]},
    }
    blocks = list(iter_tool_use_blocks([rec]))
    assert len(blocks) == 1
    index, block = blocks[0]
    assert index == 0
    assert block["input"]["subagent_type"] == "conexus:substantive-critic"


def test_both_denominators_are_emitted(corpus: pathlib.Path) -> None:
    """nexus-h33x8.5 pre-registers against the substantial subset."""
    out = render_text(census_corpus(corpus))
    assert "ALL MEASURABLE SESSIONS" in out
    assert f">={SUBSTANTIAL_THRESHOLD} calls" in out

    payload = json.loads(to_json(census_corpus(corpus)))
    assert set(payload["capabilities"]) == {"all", "substantial"}
    assert payload["substantial_threshold"] == SUBSTANTIAL_THRESHOLD


def test_any_scope_session_count_follows_the_rollup_rule(corpus: pathlib.Path) -> None:
    """A session that used Serena only via a subagent still used Serena."""
    result = census_corpus(corpus)
    assert result.orchestrator_sessions_using("serena") == 0
    assert result.subagent_sessions_using("serena") == 1
    assert result.any_sessions_using("serena") == 1


def test_unmeasurable_share_is_reported(tmp_path: pathlib.Path) -> None:
    """The health signal the exit code deliberately does not carry."""
    root = tmp_path / "p"
    root.mkdir()
    _write(root / "a.jsonl", [_assistant("Bash")])
    (root / "b.jsonl").write_text("")
    (root / "c.jsonl").write_text("")
    result = census_corpus(root)
    assert result.exit_code == 0
    assert result.unmeasurable_share == pytest.approx(2 / 3)
    assert "66.7%" in render_text(result)
