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
import re
from typing import Any

import pytest
from click.testing import CliRunner

from nexus.census import (
    LEDGER_NAME_CHARSET_RE,
    MISSING_SUBAGENT_TYPE,
    SUBSTANTIAL_THRESHOLD,
    UNMEASURABLE_EMPTY,
    UNMEASURABLE_NO_TOOL_USE,
    UNMEASURABLE_UNPARSEABLE,
    UNMEASURABLE_UNREADABLE,
    SessionDispatchCensus,
    census_corpus,
    census_corpus_dispatches,
    census_session,
    census_session_dispatches,
    classify_tool,
    count_tool_uses,
    default_project_dir,
    dispatches_to_json,
    find_suspect_dispatch_shaped_blocks,
    iter_dispatches,
    iter_tool_use_blocks,
    render_dispatches_text,
    render_text,
    sanitize_dispatch_name,
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


def _agent(
    *dispatches: tuple[str | None, str | None],
    sidechain: bool = False,
    ts: str = "2026-07-31T12:00:00.000Z",
) -> dict:
    """Build one assistant record with Agent tool_use blocks.

    Each element of ``dispatches`` is ``(subagent_type, description)``; a
    ``None`` subagent_type omits ``input.subagent_type`` entirely (the
    "malformed record" case a real transcript should not produce but a
    recognizer must not crash on).
    """
    blocks = []
    for subagent_type, description in dispatches:
        block_input: dict[str, Any] = {}
        if subagent_type is not None:
            block_input["subagent_type"] = subagent_type
        if description is not None:
            block_input["description"] = description
        blocks.append({"type": "tool_use", "name": "Agent", "input": block_input})
    return {
        "type": "assistant",
        "isSidechain": sidechain,
        "timestamp": ts,
        "message": {"role": "assistant", "content": blocks},
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
        (NEX + "search_aspect_scoped", "search_query"),
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
    payload = json.loads(res.stdout)
    assert payload["verdict"] is None
    assert payload["measurable_sessions"] == 2


class _FakeCapabilityCensusStore:
    """Stand-in for HttpTelemetryStore in the --from-store CLI tests below —
    the real store requires a live engine; these tests exercise the CLI
    command's own rendering/plumbing, not the HTTP transport (already
    covered end-to-end by the Java handler test and
    test_http_t2_store_parity.py)."""

    def __init__(self, rows: list[dict], *, raises: Exception | None = None) -> None:
        self._rows = rows
        self._raises = raises
        self.closed = False
        self.query_kwargs: dict | None = None

    def query_capability_census(self, *, session_id, since, limit):
        if self._raises is not None:
            raise self._raises
        self.query_kwargs = {"session_id": session_id, "since": since, "limit": limit}
        return self._rows

    def close(self) -> None:
        self.closed = True


def test_cli_from_store_reports_measured_row(monkeypatch: pytest.MonkeyPatch) -> None:
    fake = _FakeCapabilityCensusStore([{
        "session_id": "sess-1", "ts": "2026-09-01T00:00:00Z", "blindspot": False,
        "unmeasurable_reason": None,
        "capabilities": {"skill": 3, "agent": 0}, "dispatches": 1, "total_calls": 3,
    }])
    monkeypatch.setattr(
        "nexus.db.t2.http_telemetry_store.HttpTelemetryStore", lambda: fake,
    )

    res = _invoke(["capability", "--from-store", "--session", "sess-1"])

    assert res.exit_code == 0, res.output
    assert "session=sess-1" in res.output
    assert "skill=3" in res.output
    assert fake.query_kwargs == {"session_id": "sess-1", "since": None, "limit": 100}
    assert fake.closed is True


def test_cli_from_store_reports_blindspot_row(monkeypatch: pytest.MonkeyPatch) -> None:
    fake = _FakeCapabilityCensusStore([{
        "session_id": "sess-2", "ts": "2026-09-01T00:00:00Z", "blindspot": True,
        "unmeasurable_reason": "no-transcript-found",
        "capabilities": {}, "dispatches": None, "total_calls": None,
    }])
    monkeypatch.setattr(
        "nexus.db.t2.http_telemetry_store.HttpTelemetryStore", lambda: fake,
    )

    res = _invoke(["capability", "--from-store", "--session", "sess-2"])

    assert res.exit_code == 0, res.output
    assert "BLINDSPOT" in res.output
    assert "no-transcript-found" in res.output


def test_cli_from_store_json_mode_is_parseable(monkeypatch: pytest.MonkeyPatch) -> None:
    fake = _FakeCapabilityCensusStore([{
        "session_id": "sess-3", "ts": "2026-09-01T00:00:00Z", "blindspot": False,
        "capabilities": {"skill": 1}, "dispatches": 0, "total_calls": 1,
    }])
    monkeypatch.setattr(
        "nexus.db.t2.http_telemetry_store.HttpTelemetryStore", lambda: fake,
    )

    res = _invoke(["capability", "--from-store", "--json"])

    assert res.exit_code == 0, res.output
    payload = json.loads(res.stdout)
    assert payload["rows"][0]["session_id"] == "sess-3"


def test_cli_from_store_no_rows_is_a_clean_exit(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(
        "nexus.db.t2.http_telemetry_store.HttpTelemetryStore",
        lambda: _FakeCapabilityCensusStore([]),
    )

    res = _invoke(["capability", "--from-store", "--session", "sess-nope"])

    assert res.exit_code == 0, res.output
    assert "No capability_census rows" in res.output


def test_cli_from_store_service_failure_exits_nonzero(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(
        "nexus.db.t2.http_telemetry_store.HttpTelemetryStore",
        lambda: _FakeCapabilityCensusStore([], raises=RuntimeError("service down")),
    )

    res = _invoke(["capability", "--from-store"])

    assert res.exit_code != 0
    assert "UNAVAILABLE" in res.output


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
    assert got.name == re.sub(r"[^A-Za-z0-9]", "-", str(tmp_path.resolve()))
    assert got.parent.name == "projects"


def test_default_project_dir_encodes_dots_like_claude_code(
    monkeypatch: pytest.MonkeyPatch, tmp_path: pathlib.Path
) -> None:
    """nexus-bieuw: Claude Code maps '.' -> '-' in project-dir names, not just
    '/' -> '-' (empirical: ``ls ~/.claude/projects/`` shows
    ``-Users-hal-hildebrand-git-nexus`` for cwd ``/Users/hal.hildebrand/git/nexus``
    — the dot in the username collapses to a dash exactly like a slash does).
    A dotted directory component in cwd must therefore also become a dash,
    not survive verbatim.
    """
    monkeypatch.delenv("NX_CENSUS_PROJECT_DIR", raising=False)
    dotted = tmp_path / "hal.hildebrand" / "git.repo"
    dotted.mkdir(parents=True)
    monkeypatch.chdir(dotted)

    got = default_project_dir()

    assert "." not in got.name
    assert "hal-hildebrand" in got.name
    assert "git-repo" in got.name
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


# ============================================================================
# nexus-h33x8.2 — dispatch recognition
#
# THE PROOF THIS SECTION EXISTS FOR: nexus-nu7fo recorded four consecutive
# sessions where the RDR-184 ledger recognised 0 of N dispatched agents
# (0/6, 0/10, 0/7, 0/2) because it keyed on a name-morphology the Agent
# tool cannot produce. TestGoldenSessionDispatches below recognises all 7
# of session 75695009's dispatches from the same transcript the ledger
# saw and reported 0/7 against — that 0-vs-7 delta is the deliverable.
# ============================================================================

# --------------------------------------------------------------------------
# sanitize_dispatch_name
# --------------------------------------------------------------------------

def test_sanitize_verbatim_when_already_ledger_valid() -> None:
    """AGENTS.md's hot-rule convention: verbatim, colon included, never invented.

    This is VERIFICATION 4 from the bead, checked against the CURRENT
    ledger charset (colon-inclusive, tests/e2e/lib/expectations.sh) rather
    than the OLDER colon-excluding charset the bead's BUILD text quotes —
    see LEDGER_NAME_CHARSET_RE's docstring for why sanitizing the colon
    away would be wrong today.
    """
    sanitized, changed = sanitize_dispatch_name("conexus:substantive-critic")
    assert sanitized == "conexus:substantive-critic"
    assert changed is False
    assert LEDGER_NAME_CHARSET_RE.match(sanitized)


def test_sanitize_transforms_disallowed_interior_characters() -> None:
    sanitized, changed = sanitize_dispatch_name("conexus/weird type!")
    assert changed is True
    assert LEDGER_NAME_CHARSET_RE.match(sanitized)
    assert "/" not in sanitized
    assert " " not in sanitized
    assert "!" not in sanitized


def test_sanitize_prefixes_a_non_alnum_leading_character() -> None:
    sanitized, changed = sanitize_dispatch_name("-leading-dash")
    assert changed is True
    assert LEDGER_NAME_CHARSET_RE.match(sanitized)
    assert sanitized[0].isalnum()


def test_sanitize_truncates_over_64_chars() -> None:
    raw = "a" * 100
    sanitized, changed = sanitize_dispatch_name(raw)
    assert changed is True
    assert len(sanitized) == 64
    assert LEDGER_NAME_CHARSET_RE.match(sanitized)


def test_sanitize_empty_raw_gets_a_placeholder() -> None:
    sanitized, changed = sanitize_dispatch_name("")
    assert changed is True
    assert sanitized
    assert LEDGER_NAME_CHARSET_RE.match(sanitized)


def test_sanitize_collision_two_distinct_raw_values_fold_together() -> None:
    """Collision behavior: flagged, never silently merged.

    Two DIFFERENT raw subagent_type strings that both scrub to the same
    sanitized key must not look like one type dispatched twice to a
    downstream consumer that only reads type_counts.
    """
    root_records = [_agent(("weird!type", "a"), ("weird?type", "b"))]
    dispatches = iter_dispatches(root_records, session_id="s", scope="orchestrator")
    assert len({d.subagent_type_sanitized for d in dispatches}) == 1

    sess = SessionDispatchCensus(session_id="s", dispatches=dispatches)
    collisions = sess.sanitize_collisions
    assert len(collisions) == 1
    assert set(collisions[0]["raw_values"]) == {"weird!type", "weird?type"}
    # Not silently merged: type_counts still credits both under one key,
    # which is exactly the fact sanitize_collisions exists to surface.
    assert sess.type_counts[collisions[0]["sanitized"]] == 2


# --------------------------------------------------------------------------
# iter_dispatches / DispatchRecord
# --------------------------------------------------------------------------

def test_iter_dispatches_ignores_non_agent_tool_use() -> None:
    """Falsify-by-deletion analog for the Agent-name filter: a fixture that
    mixes Agent and non-Agent tool_use must not count the non-Agent ones.
    If the ``block.get("name") != "Agent"`` guard in iter_dispatches were
    ever deleted, this assertion would go from 1 to 3 and fail.
    """
    records = [_assistant("Bash", "Read"), _agent(("conexus:debugger", None))]
    dispatches = iter_dispatches(records, session_id="s", scope="orchestrator")
    assert len(dispatches) == 1
    assert dispatches[0].subagent_type_sanitized == "conexus:debugger"


# --------------------------------------------------------------------------
# Rider 2 (fix round 2, 2026-08-08) — suspect non-Agent dispatch-shaped blocks
# --------------------------------------------------------------------------

def _non_agent_block_with_subagent_type(
    tool_name: str, subagent_type: str, ts: str = "2026-07-31T12:00:00.000Z"
) -> dict:
    """A tool_use block that LOOKS like a dispatch but is not named Agent —
    the drift signature Rider 2 asks the census to flag rather than silently
    drop. Mirrors ``conexus/hooks/scripts/agent-dispatch-expect.sh``'s own
    precedent: it special-cases ``"Task"`` as "the pre-rename spelling of
    the same tool" alongside ``"Agent"``, proof this exact rename shape has
    happened in this harness before.
    """
    return {
        "type": "assistant",
        "isSidechain": False,
        "timestamp": ts,
        "message": {
            "role": "assistant",
            "content": [
                {
                    "type": "tool_use",
                    "name": tool_name,
                    "input": {"subagent_type": subagent_type},
                }
            ],
        },
    }


def test_find_suspect_dispatch_shaped_blocks_flags_renamed_tool() -> None:
    records = [_non_agent_block_with_subagent_type("Task", "conexus:debugger")]
    suspects = find_suspect_dispatch_shaped_blocks(records, scope="orchestrator")
    assert len(suspects) == 1
    assert suspects[0]["tool_name"] == "Task"
    assert suspects[0]["subagent_type"] == "conexus:debugger"
    assert suspects[0]["scope"] == "orchestrator"


def test_find_suspect_dispatch_shaped_blocks_ignores_agent_and_no_subagent_type() -> None:
    records = [_agent(("conexus:debugger", None)), _assistant("Bash")]
    assert find_suspect_dispatch_shaped_blocks(records, scope="orchestrator") == []


def test_census_session_dispatches_surfaces_suspects_without_failing_measurability(
    tmp_path: pathlib.Path,
) -> None:
    """Warn-only: a suspect block is reported, but does not push the
    session to UNMEASURABLE and does not change exit_code."""
    root = tmp_path / "p"
    root.mkdir()
    _write(
        root / "s.jsonl",
        [_agent(("conexus:debugger", "real")), _non_agent_block_with_subagent_type("Task", "conexus:deep-analyst")],
    )
    sess = census_session_dispatches(root, "s")
    assert sess.measurable
    assert sess.total_dispatches == 1  # the Task block is NOT counted as a recognized dispatch
    assert len(sess.suspect_blocks) == 1
    assert sess.suspect_blocks[0]["tool_name"] == "Task"

    result = census_corpus_dispatches(root)
    assert result.exit_code == 0
    assert result.suspect_blocks_total == 1


def test_render_dispatches_text_warns_on_suspect_blocks(tmp_path: pathlib.Path) -> None:
    root = tmp_path / "p"
    root.mkdir()
    _write(root / "s.jsonl", [_non_agent_block_with_subagent_type("Task", "conexus:debugger")])
    out = render_dispatches_text(census_corpus_dispatches(root))
    assert "WARNING" in out
    assert "Task" in out


def test_dispatches_to_json_carries_suspect_blocks(tmp_path: pathlib.Path) -> None:
    root = tmp_path / "p"
    root.mkdir()
    _write(root / "s.jsonl", [_non_agent_block_with_subagent_type("Task", "conexus:debugger")])
    payload = json.loads(dispatches_to_json(census_corpus_dispatches(root)))
    assert payload["suspect_blocks_total"] == 1
    assert payload["sessions"][0]["suspect_blocks"][0]["tool_name"] == "Task"


def test_iter_dispatches_assigns_type_ordinal_per_sanitized_type() -> None:
    """type_ordinal disambiguates repeated same-type dispatches — the
    reason BUILD asks for an ordinal at all."""
    records = [
        _agent(("conexus:code-review-expert", "first")),
        _agent(("conexus:substantive-critic", "second")),
        _agent(("conexus:code-review-expert", "third")),
    ]
    dispatches = iter_dispatches(records, session_id="s", scope="orchestrator")
    ordinals = [(d.session_ordinal, d.type_ordinal, d.subagent_type_sanitized) for d in dispatches]
    assert ordinals == [
        (1, 1, "conexus:code-review-expert"),
        (2, 1, "conexus:substantive-critic"),
        (3, 2, "conexus:code-review-expert"),
    ]


def test_iter_dispatches_records_but_flags_missing_subagent_type() -> None:
    """A dispatch with no input.subagent_type is still enumerated — dropping
    the row would break VERIFICATION 2's raw-count equality — and keyed as
    ``general-purpose``, matching what agent-dispatch-expect.sh's own EXPECT
    row computes for the SAME omitted field (nexus-a795d:
    ``str(ti.get("subagent_type") or "general-purpose")``). Review finding
    S1 (fix round 2, 2026-08-08): the census must match the hook's key or
    "pass subagent_type straight to expectations_expect" is false for every
    one of these rows. ``subagent_type_missing``/``subagent_type_raw is
    None`` remain the provenance flags distinguishing this from a REAL,
    explicitly-general-purpose dispatch.
    """
    records = [_agent((None, "no type on this one"))]
    dispatches = iter_dispatches(records, session_id="s", scope="orchestrator")
    assert len(dispatches) == 1
    d = dispatches[0]
    assert d.subagent_type_missing is True
    assert d.subagent_type_raw is None
    assert d.subagent_type_sanitized == "general-purpose"
    assert d.subagent_type_sanitized == MISSING_SUBAGENT_TYPE


def test_to_dict_subagent_type_is_the_sanitized_ledger_consumable_form() -> None:
    records = [_agent(("weird!type", "d"))]
    d = iter_dispatches(records, session_id="s", scope="orchestrator")[0]
    row = d.to_dict()
    assert row["subagent_type"] == d.subagent_type_sanitized
    assert row["subagent_type_raw"] == "weird!type"
    assert LEDGER_NAME_CHARSET_RE.match(row["subagent_type"])


# --------------------------------------------------------------------------
# census_session_dispatches — non-vacuity and roll-up
# --------------------------------------------------------------------------

def test_session_with_no_agent_dispatch_is_a_measured_zero(tmp_path: pathlib.Path) -> None:
    """A session that used other tools but dispatched nothing is NOT the
    nexus-nu7fo defect class — it must not report UNMEASURABLE."""
    root = tmp_path / "p"
    root.mkdir()
    _write(root / "s.jsonl", [_assistant("Bash", "Skill")])
    sess = census_session_dispatches(root, "s")
    assert sess.measurable
    assert sess.total_dispatches == 0


def test_session_with_zero_tool_use_blocks_is_unmeasurable(tmp_path: pathlib.Path) -> None:
    root = tmp_path / "p"
    root.mkdir()
    _write(root / "s.jsonl", [{"type": "user", "message": {"role": "user", "content": "hi"}}])
    sess = census_session_dispatches(root, "s")
    assert not sess.measurable
    assert sess.unmeasurable_reason == UNMEASURABLE_NO_TOOL_USE


def test_empty_transcript_is_unmeasurable(tmp_path: pathlib.Path) -> None:
    root = tmp_path / "p"
    root.mkdir()
    (root / "s.jsonl").write_text("")
    sess = census_session_dispatches(root, "s")
    assert not sess.measurable
    assert sess.unmeasurable_reason == UNMEASURABLE_EMPTY


def test_corrupt_transcript_is_unmeasurable(tmp_path: pathlib.Path) -> None:
    root = tmp_path / "p"
    root.mkdir()
    (root / "s.jsonl").write_text("{not json\n{also not json\n")
    sess = census_session_dispatches(root, "s")
    assert not sess.measurable
    assert sess.unmeasurable_reason == UNMEASURABLE_UNPARSEABLE


def test_subagent_dispatch_rolls_up_to_parent_session(tmp_path: pathlib.Path) -> None:
    """A subagent that itself dispatches a nested Agent must attribute to
    the PARENT session, per .1's roll-up rule — not appear as its own
    session or vanish."""
    root = tmp_path / "p"
    root.mkdir()
    _write(root / "s.jsonl", [_assistant("Bash")])
    _write(
        root / "s" / "subagents" / "agent-a1.jsonl",
        [_agent(("conexus:code-explorer", "nested"), sidechain=True)],
    )
    sess = census_session_dispatches(root, "s")
    assert sess.total_dispatches == 1
    assert sess.dispatches[0].scope == "subagent"
    assert sess.dispatches[0].subagent_type_sanitized == "conexus:code-explorer"


def test_type_ordinal_threads_across_files_within_one_session(tmp_path: pathlib.Path) -> None:
    """Review finding I1 (fix round 2026-08-08): the same subagent_type
    dispatched once in the orchestrator file and again in a subagent file
    must land on type_ordinal [1, 2], not [1, 1] — a fresh per-file
    Counter would silently produce duplicate (subagent_type, type_ordinal)
    keys within one session, contradicting the BUILD contract.
    """
    root = tmp_path / "p"
    root.mkdir()
    _write(root / "s.jsonl", [_agent(("conexus:code-review-expert", "orch dispatch"))])
    _write(
        root / "s" / "subagents" / "agent-a1.jsonl",
        [_agent(("conexus:code-review-expert", "nested dispatch"), sidechain=True)],
    )
    sess = census_session_dispatches(root, "s")
    assert sess.total_dispatches == 2
    type_ordinals = [d.type_ordinal for d in sess.dispatches]
    assert type_ordinals == [1, 2]
    assert sess.type_counts == {"conexus:code-review-expert": 2}
    # session_ordinal keeps its own independent, already-correct sequence.
    assert [d.session_ordinal for d in sess.dispatches] == [1, 2]


def test_corpus_sanitize_collisions_span_sessions(tmp_path: pathlib.Path) -> None:
    """Review finding M2 (fix round 2026-08-08): a collision can be
    invisible at the SESSION level (each session contributes only one raw
    value to the shared sanitized key) yet real at the CORPUS level, where
    type_counts merges across sessions. Merging each session's own
    (already-filtered, single-raw-value) collision list would miss this.
    """
    root = tmp_path / "p"
    root.mkdir()
    _write(root / "a.jsonl", [_agent(("weird!type", "in session a"))])
    _write(root / "b.jsonl", [_agent(("weird?type", "in session b"))])

    sess_a = census_session_dispatches(root, "a")
    sess_b = census_session_dispatches(root, "b")
    assert sess_a.sanitize_collisions == []  # no collision visible within EITHER session alone
    assert sess_b.sanitize_collisions == []

    result = census_corpus_dispatches(root)
    assert result.type_counts == {"weird-type": 2}  # merged, as the ledger would see it
    collisions = result.sanitize_collisions
    assert len(collisions) == 1
    assert collisions[0]["sanitized"] == "weird-type"
    assert set(collisions[0]["raw_values"]) == {"weird!type", "weird?type"}


def test_corpus_dispatches_reproduces_raw_agent_tool_use_count(tmp_path: pathlib.Path) -> None:
    """VERIFICATION 2 in miniature: recognized count must equal the raw
    Agent tool_use count from the same transcript, not merely a bucket
    total that happens to look plausible."""
    root = tmp_path / "p"
    root.mkdir()
    records = [
        _agent(("conexus:code-review-expert", "a"), ("conexus:substantive-critic", "b")),
        _assistant("Bash"),
        _agent(("conexus:debugger", "c")),
    ]
    _write(root / "s.jsonl", records)
    raw_counts, _ = count_tool_uses(records)
    result = census_corpus_dispatches(root)
    assert result.total_dispatches == raw_counts["Agent"] == 3


# --------------------------------------------------------------------------
# GOLDEN SESSION — VERIFICATION 1: exact recognized count and type multiset
# --------------------------------------------------------------------------

class TestGoldenSessionDispatches:
    """Contrast with nexus-nu7fo's own recorded ledger result for the SAME
    class of session: `checked=8 recognized=0`. This module recognizes all
    7 dispatches in the frozen prefix — the 0-vs-7 delta is the proof the
    bead asks to be printed side by side when it closes.
    """

    def test_golden_session_exact_dispatch_count(self) -> None:
        result = census_corpus_dispatches(GOLDEN_FIXTURE, session=GOLDEN_SESSION)
        assert result.exit_code == 0
        assert result.total_dispatches == 7  # ledger recorded recognized=0 for this class

    def test_golden_session_type_multiset(self) -> None:
        sess = census_session_dispatches(GOLDEN_FIXTURE, GOLDEN_SESSION)
        assert sess.type_counts == {
            "conexus:code-review-expert": 2,
            "conexus:substantive-critic": 2,
            "conexus:debugger": 1,
            "conexus:deep-analyst": 1,
            "conexus:architect-planner": 1,
        }

    def test_golden_session_ordinals_disambiguate_repeated_types(self) -> None:
        sess = census_session_dispatches(GOLDEN_FIXTURE, GOLDEN_SESSION)
        ordinals = [(d.session_ordinal, d.type_ordinal, d.subagent_type_sanitized) for d in sess.dispatches]
        assert ordinals == [
            (1, 1, "conexus:code-review-expert"),
            (2, 1, "conexus:substantive-critic"),
            (3, 2, "conexus:code-review-expert"),
            (4, 2, "conexus:substantive-critic"),
            (5, 1, "conexus:debugger"),
            (6, 1, "conexus:deep-analyst"),
            (7, 1, "conexus:architect-planner"),
        ]

    def test_golden_session_no_sanitization_needed_but_verified(self) -> None:
        """Every real subagent_type in the golden session already satisfies
        the ledger charset verbatim — sanitization exists for the edge
        case, not the common path."""
        sess = census_session_dispatches(GOLDEN_FIXTURE, GOLDEN_SESSION)
        assert all(not d.subagent_type_changed for d in sess.dispatches)
        assert sess.sanitize_collisions == []

    def test_golden_session_falsify_by_deletion(self) -> None:
        """Delete the Agent-name filter (simulated: count ALL tool_use
        blocks instead) and the golden count changes — proving the
        assertions above depend on the filter rather than surviving its
        removal vacuously (nexus-h33x8.1 VERIFICATION 4's own finding:
        Skill-branch deletion is vacuous on this session, Agent is not)."""
        sess = census_session(GOLDEN_FIXTURE, GOLDEN_SESSION)  # capability census
        all_tool_use_total = sum(sess.orchestrator.values())
        assert all_tool_use_total > 7  # Bash/Edit/Read/etc. alongside the 7 Agent calls


# --------------------------------------------------------------------------
# rendering / JSON
# --------------------------------------------------------------------------

def test_render_dispatches_text_marks_changed_rows(tmp_path: pathlib.Path) -> None:
    root = tmp_path / "p"
    root.mkdir()
    _write(root / "s.jsonl", [_agent(("weird!type", "needs sanitizing"))])
    out = render_dispatches_text(census_corpus_dispatches(root))
    assert "*" in out
    assert "needs sanitizing" in out
    assert "N-of-type" in out


def test_render_dispatches_text_flags_partial_parse(tmp_path: pathlib.Path) -> None:
    """Rider 1 (fix round 2, 2026-08-08): a corrupted transcript tail must
    not read as a clean, lower dispatch count in TEXT mode — the PARTIAL
    fact JSON already carried via ``partial``/``parse_errors`` must also
    render here, mirroring ``render_text``'s own PARTIAL block."""
    root = tmp_path / "p"
    root.mkdir()
    (root / "s.jsonl").write_text(
        json.dumps(_agent(("conexus:debugger", "before the corruption"))) + "\n{ truncated\n"
    )
    result = census_corpus_dispatches(root)
    assert result.sessions[0].partial
    out = render_dispatches_text(result)
    assert "PARTIAL" in out
    assert "1 line(s) skipped" in out


def test_dispatches_to_json_row_shape(tmp_path: pathlib.Path) -> None:
    root = tmp_path / "p"
    root.mkdir()
    _write(root / "s.jsonl", [_agent(("conexus:debugger", "d"))])
    payload = json.loads(dispatches_to_json(census_corpus_dispatches(root)))
    assert payload["total_dispatches"] == 1
    assert payload["type_counts"] == {"conexus:debugger": 1}
    row = payload["sessions"][0]["dispatches"][0]
    assert row["subagent_type"] == "conexus:debugger"
    assert row["session_ordinal"] == 1
    assert row["type_ordinal"] == 1
    assert payload["ledger_name_charset"] == LEDGER_NAME_CHARSET_RE.pattern


def test_dispatches_unmeasurable_scope_error_reported(tmp_path: pathlib.Path) -> None:
    result = census_corpus_dispatches(tmp_path / "nope")
    assert result.exit_code != 0
    assert "UNMEASURABLE" in render_dispatches_text(result)


# --------------------------------------------------------------------------
# CLI boundary
# --------------------------------------------------------------------------

def test_cli_dispatches_registered_on_census_group() -> None:
    assert "dispatches" in census_group.commands


def test_cli_dispatches_measurable_exits_zero(tmp_path: pathlib.Path) -> None:
    root = tmp_path / "p"
    root.mkdir()
    _write(root / "s.jsonl", [_agent(("conexus:debugger", "d"))])
    res = _invoke(["dispatches", "--project-dir", str(root)])
    assert res.exit_code == 0, res.output
    assert "Agent dispatch(es)" in res.output


def test_cli_dispatches_unmeasurable_exits_nonzero(tmp_path: pathlib.Path) -> None:
    res = _invoke(["dispatches", "--project-dir", str(tmp_path / "nope")])
    assert res.exit_code != 0
    assert "UNMEASURABLE" in res.output


def test_cli_dispatches_json_mode_is_parseable(tmp_path: pathlib.Path) -> None:
    root = tmp_path / "p"
    root.mkdir()
    _write(root / "s.jsonl", [_agent(("conexus:debugger", "d"))])
    res = _invoke(["dispatches", "--project-dir", str(root), "--json"])
    assert res.exit_code == 0, res.output
    payload = json.loads(res.stdout)
    assert payload["total_dispatches"] == 1


def test_cli_dispatches_golden_session() -> None:
    res = _invoke(["dispatches", "--project-dir", str(GOLDEN_FIXTURE), "--session", GOLDEN_SESSION])
    assert res.exit_code == 0, res.output
    assert "7 Agent dispatch(es)" in res.output
