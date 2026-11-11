# SPDX-License-Identifier: AGPL-3.0-or-later
"""The stand-in MarkupEditor MCP server for the acceptance runners (nexus-scmcs.3).

It serves open_document, get_document, propose_edits, list_suggestions, undo and save_document in the shapes the real
app replied with (`fixtures/markupeditor/*.json`, NOTES.md), reproduces two quirks of NOTES.md (e: ids keep counting
across documents, list_suggestions keeps returning the previous document's entries), and records the author's
decisions from a script, so that they show up as list_suggestions statuses: the app decides, never the answer turn.

The replies are compared with the fixtures key for key. The hash is a stand-in (the app's algorithm is unknown): its
shape (14 hex digits) is pinned, not its value.
"""
from __future__ import annotations

import asyncio
import importlib.util
import json
import os
import re
import sys
from pathlib import Path
from types import ModuleType
from typing import Any

import pytest
from mcp import ClientSession
from mcp.client.stdio import StdioServerParameters, get_default_environment, stdio_client

from tests.prose_edit.test_brief import ROOT

ACC = ROOT / "tests" / "prose_edit" / "acceptance"
FIX = ROOT / "tests" / "prose_edit" / "fixtures" / "markupeditor"
SCRIPTS = ROOT / "prose-edit" / "skills" / "prose-edit" / "scripts"


def _load(path: Path, name: str) -> ModuleType:
    spec = importlib.util.spec_from_file_location(name, path)
    assert spec is not None and spec.loader is not None
    mod = importlib.util.module_from_spec(spec)
    sys.modules[name] = mod
    spec.loader.exec_module(mod)
    return mod


standin = _load(ACC / "markup_standin.py", "markup_standin_under_test")
suggest = _load(SCRIPTS / "suggest.py", "suggest_under_test")


def _fixture(name: str) -> dict[str, Any]:
    return json.loads((FIX / name).read_text(encoding="utf-8"))


def _write_fixture_doc(tmp_path: Path, name: str = "doc.md") -> Path:
    """A Markdown file whose blocks are the fixture's blocks, as the app reads them."""
    blocks = _fixture("get_document.json")["blocks"]
    path = tmp_path / name
    path.write_text("\n\n".join(b["markdown"] for b in blocks) + "\n", encoding="utf-8")
    return path


def _doc(tmp_path: Path, text: str, name: str = "doc.md") -> Path:
    path = tmp_path / name
    path.write_text(text, encoding="utf-8")
    return path


def _edit(block: int, expected: str, find: str, replace: str, note: str) -> dict[str, Any]:
    return {"block": block, "expected": expected, "find": find, "replace": replace, "note": note}


def _fixture_edits(hashes: list[str]) -> list[dict[str, Any]]:
    """The six edits of the fixture's first propose_edits call (hash of block i is hashes[i])."""
    return [
        _edit(1, hashes[1], "basically ordered", "ordered", "Edit 1: hedge"),
        _edit(2, hashes[2], "really quite simply ", "", "Edit 2: hedge"),
        _edit(5, hashes[5], "basically ", "", "Edit 3: hedge inside bold"),
        _edit(4, hashes[4], "just wakes", "just wakes", "Edit 4: no-op"),
        _edit(6, hashes[6], "--retries 1", "--retries 2", "Edit 5: code"),
        _edit(7, hashes[7], "it is “really” simple", 'it is "plainly" simple', "Edit 6: curly quotes"),
    ]


def _hashes(reply: dict[str, Any]) -> list[str]:
    return [b["hash"] for b in reply["blocks"]]


# ---------------------------------------------------------------------------
# Reply shapes, key for key against the fixtures
# ---------------------------------------------------------------------------


def test_get_document_has_the_fixtures_keys_and_blocks(tmp_path: Path) -> None:
    fixture = _fixture("get_document.json")
    app = standin.Standin()
    app.open_document(str(_write_fixture_doc(tmp_path)))
    reply = app.get_document()
    assert set(reply) == set(fixture) and "next" not in reply
    assert reply["documentType"] == "md" and reply["view"] == "editor" and reply["hasChanges"] is False
    assert reply["firstEditable"] == 0 and reply["selectionBlock"] == 0
    assert reply["url"] == os.path.realpath(tmp_path / "doc.md")
    assert len(reply["blocks"]) == len(fixture["blocks"])
    for got, want in zip(reply["blocks"], fixture["blocks"], strict=True):
        assert set(got) == set(want) == {"hash", "index", "kind", "markdown", "warnings"}
        assert (got["index"], got["kind"], got["markdown"], got["warnings"]) == (
            want["index"], want["kind"], want["markdown"], want["warnings"])
        assert re.fullmatch(r"[0-9a-f]{14}", got["hash"])
    assert len({h for h in _hashes(reply)}) == len(reply["blocks"])


def test_get_document_reads_the_serializer_fixture_layout(tmp_path: Path) -> None:
    """Wrapped paragraphs are unwrapped, `-` becomes `*`, a table rule becomes `| --- |`, a list item is its own block run."""
    text = ("# T\n\nA paragraph that is\nwrapped over two lines.\n\n- one\n- two\n\n1. first\n2. second\n\n"
            "> a quote\n> goes on\n\n|a|b|\n|---|---|\n|c|d|\n\n```bash\nx\n```\n")
    app = standin.Standin()
    app.open_document(str(_doc(tmp_path, text)))
    got = [(b["kind"], b["markdown"]) for b in app.get_document()["blocks"]]
    assert got == [
        ("heading", "# T"),
        ("paragraph", "A paragraph that is wrapped over two lines."),
        ("bullet-list", "* one\n\n* two"),
        ("numbered-list", "1. first\n\n2. second"),
        ("quote", "> a quote goes on"),
        ("table", "| a | b |\n| --- | --- |\n| c | d |"),
        ("code", "```bash\nx\n```"),
    ]


def test_limit_counts_characters_and_next_is_the_block_to_resume_at(tmp_path: Path) -> None:
    app = standin.Standin()
    app.open_document(str(_write_fixture_doc(tmp_path)))
    one = app.get_document(limit=1)
    assert [b["index"] for b in one["blocks"]] == [0] and one["next"] == 1
    assert set(one) >= {"url", "hasChanges", "view"}
    page2 = app.get_document(from_=one["next"], limit=100)
    assert page2["blocks"][0]["index"] == 1
    assert sum(len(b["markdown"]) for b in page2["blocks"][1:]) < 100
    last = app.get_document(from_=7)
    assert [b["index"] for b in last["blocks"]] == [7] and "next" not in last


def test_propose_edits_replies_as_the_fixture_does(tmp_path: Path) -> None:
    fixture = _fixture("propose_edits.json")
    app = standin.Standin(first_id=12)
    app.open_document(str(_write_fixture_doc(tmp_path)))
    hashes = _hashes(app.get_document())
    reply = app.propose_edits(_fixture_edits(hashes))
    assert reply == {"ok": True, "results": fixture["results"][:6]}
    crossing = app.propose_edits([_edit(5, hashes[5], "The retry", "The retries", "Edit 7: crossing")])
    assert crossing == {"ok": True, "results": [{**fixture["results"][6], "index": 0}]}


def test_a_refusal_has_index_reason_status_and_no_block_or_id(tmp_path: Path) -> None:
    app = standin.Standin()
    app.open_document(str(_write_fixture_doc(tmp_path)))
    hashes = _hashes(app.get_document())
    results = app.propose_edits([
        _edit(1, hashes[1], "not in the block", "x", "Edit 1"),
        _edit(1, "0" * 14, "basically ordered", "ordered", "Edit 2"),
        _edit(99, "0" * 14, "x", "y", "Edit 3"),
    ])["results"]
    assert [sorted(r) for r in results] == [["index", "reason", "status"]] * 3
    assert [r["status"] for r in results] == ["refused"] * 3
    assert results[0]["reason"].startswith("find not found in block 1; matching is exact and case-sensitive")
    assert "read" in results[1]["reason"] and "hash" in results[1]["reason"].lower()
    assert "99" in results[2]["reason"]


def test_a_find_is_matched_on_visible_text_as_the_serializer_notes_say(tmp_path: Path) -> None:
    """NOTES g: a find written as visible text crosses `\\[sic\\]`, `\\<`, `&`, a link's text and a table cell."""
    serializer = _fixture("get_document_serializer.json")
    request = _fixture("propose_edits_serializer.json")
    path = tmp_path / "ser.md"
    path.write_text("\n\n".join(b["markdown"] for b in serializer["blocks"]) + "\n", encoding="utf-8")
    app = standin.Standin(first_id=17)
    app.open_document(str(path))
    blocks = app.get_document()["blocks"]
    edits = [{**e, "expected": blocks[e["block"]]["hash"], "note": f"Edit {i + 1}"} for i, e in enumerate(request["request"])]
    got = app.propose_edits(edits)["results"]
    want = request["reply"]["results"]
    assert len(got) == len(want)
    for g, w in zip(got, want, strict=True):
        assert (g["status"], g["index"], g.get("id")) == (w["status"], w["index"], w.get("id")), (g, w)
        if w["status"] == "refused":
            assert g["reason"] == w["reason"]


def test_a_pending_suggestion_blocks_an_overlapping_edit_but_not_a_disjoint_one(tmp_path: Path) -> None:
    app = standin.Standin()
    app.open_document(str(_write_fixture_doc(tmp_path)))
    h = _hashes(app.get_document())
    first = app.propose_edits([_edit(1, h[1], "basically ordered", "ordered", "Edit 1")])
    assert first["results"][0]["status"] == "proposed"
    again = app.propose_edits([_edit(1, h[1], "ordered, and", "ordered and", "Edit 2"),
                               _edit(1, h[1], "entries leave", "items leave", "Edit 3")])["results"]
    assert [r["status"] for r in again] == ["refused", "proposed"]
    assert "overlap" in again[0]["reason"]


def test_list_suggestions_has_the_fixtures_keys_and_no_reason_or_document_key(tmp_path: Path) -> None:
    fixture = _fixture("list_suggestions.json")
    decisions = {"accepted": [1], "rejected": [2], "default": "pending"}
    app = standin.Standin(first_id=12, decisions=decisions)
    app.open_document(str(_write_fixture_doc(tmp_path)))
    app.propose_edits(_fixture_edits(_hashes(app.get_document())))
    reply = app.list_suggestions()
    assert set(reply) == {"ok", "suggestions"} and reply["ok"] is True
    want = {s["id"]: s for s in fixture["suggestions"] if s["id"] in {"s12", "s13", "s14", "s15"}}
    got = {s["id"]: s for s in reply["suggestions"]}
    assert got == want
    for s in reply["suggestions"]:
        assert set(s) == {"block", "find", "id", "note", "replace", "status"}


def test_the_replies_travel_as_json_text_with_sorted_keys() -> None:
    text = standin.render({"ok": True, "suggestions": [{"id": "s1", "block": 1}]})
    assert json.loads(text) == {"ok": True, "suggestions": [{"id": "s1", "block": 1}]}
    assert text.index('"block"') < text.index('"id"')


# ---------------------------------------------------------------------------
# The two NOTES.md quirks
# ---------------------------------------------------------------------------


def test_ids_keep_counting_across_documents_and_list_returns_the_previous_documents_entries(tmp_path: Path) -> None:
    a = _doc(tmp_path, "# A\n\nThe alpha is basically fine.\n", "a.md")
    b = _doc(tmp_path, "# B\n\nThe beta is really fine.\n", "b.md")
    app = standin.Standin()
    app.open_document(str(a))
    r1 = app.propose_edits([_edit(1, app.get_document()["blocks"][1]["hash"], "basically ", "", "Edit 1")])
    app.open_document(str(b))
    sent_b = [_edit(1, app.get_document()["blocks"][1]["hash"], "really ", "", "Edit 1")]
    r2 = app.propose_edits(sent_b)
    assert (r1["results"][0]["id"], r2["results"][0]["id"]) == ("s1", "s2")
    listed = app.list_suggestions()["suggestions"]
    assert [s["id"] for s in listed] == ["s1", "s2"]  # document A's entry is still listed, with nothing naming the document
    assert not any("url" in s or "document" in s for s in listed)
    # suggest.py maps only the ids this run sent and ignores the rest
    sent = [{"n": 1, "id": "s2", "find": "really ", "replace": ""}]
    mapped = suggest.map_statuses(sent, listed)
    assert mapped["hold"] == [1] and mapped["warnings"] == []


# ---------------------------------------------------------------------------
# Scripted decisions
# ---------------------------------------------------------------------------


def _proposed(tmp_path: Path, **kw: Any) -> Any:
    tmp_path.mkdir(exist_ok=True)
    app = standin.Standin(**kw)
    app.open_document(str(_write_fixture_doc(tmp_path)))
    h = _hashes(app.get_document())
    app.propose_edits([e for e in _fixture_edits(h) if e["note"] in
                       ("Edit 1: hedge", "Edit 2: hedge", "Edit 3: hedge inside bold", "Edit 6: curly quotes")])
    return app


def _status_by_edit(app: Any) -> dict[int, str]:
    return {int(re.match(r"Edit (\d+)", s["note"]).group(1)): s["status"] for s in app.list_suggestions()["suggestions"]}


def test_without_a_rule_every_suggestion_stays_pending(tmp_path: Path) -> None:
    assert set(_status_by_edit(_proposed(tmp_path)).values()) == {"pending"}


def test_a_rule_accepts_and_rejects_by_edit_number_and_the_default_covers_the_rest(tmp_path: Path) -> None:
    app = _proposed(tmp_path / "one", decisions={"accepted": [1], "default": "rejected"})
    assert _status_by_edit(app) == {1: "accepted", 2: "rejected", 3: "rejected", 6: "rejected"}
    app = _proposed(tmp_path / "two", decisions={"accepted": [1], "rejected": [2]})
    assert _status_by_edit(app) == {1: "accepted", 2: "rejected", 3: "pending", 6: "pending"}


def test_a_decision_is_kept_once_made(tmp_path: Path) -> None:
    app = _proposed(tmp_path, decisions={"accepted": [1], "default": "rejected"})
    assert _status_by_edit(app)[1] == "accepted"
    app.decisions = {"default": "accepted"}  # a later turn's rule does not reopen what the author already decided
    assert _status_by_edit(app) == {1: "accepted", 2: "rejected", 3: "rejected", 6: "rejected"}


def test_an_accepted_suggestion_dirties_the_buffer_and_undo_clears_it(tmp_path: Path) -> None:
    app = _proposed(tmp_path, decisions={"accepted": [1, 2], "default": "rejected"})
    assert app.get_document(limit=1)["hasChanges"] is False
    app.list_suggestions()
    assert app.get_document(limit=1)["hasChanges"] is True
    app.undo()
    assert app.get_document(limit=1)["hasChanges"] is True
    app.undo()
    assert app.get_document(limit=1)["hasChanges"] is False
    with pytest.raises(standin.StandinError, match="nothing to undo"):
        app.undo()
    app.open_document(app.get_document(limit=1)["url"])  # reopening discards unsaved changes


def test_rules_are_picked_per_scenario_by_pattern_and_the_first_match_wins() -> None:
    table = {"a2": {"accepted": [1], "default": "rejected"}, "*-[0-9]*-2": {"default": "rejected"}, "*": {"default": "pending"}}
    assert standin.rule_for("a2", table) == table["a2"]
    assert standin.rule_for("gate-3-2", table) == table["*-[0-9]*-2"]
    assert standin.rule_for("b4", table) == table["*"]
    assert standin.rule_for("a1", {"a2": {"default": "rejected"}}) is None  # no rule: the author has not decided


def test_the_shipped_decision_table_decides_the_answer_turns_the_runners_script() -> None:
    table = json.loads((ACC / "markup_standin_decisions.json").read_text(encoding="utf-8"))
    for name in ("a2", "t2"):  # "Accept edit 1 and reject all the other edits."
        assert standin.rule_for(name, table) == {"accepted": [1], "default": "rejected"}
    for name in ("memgate-1-2", "memgate-10-2"):  # the gate's "Accept none. Reject all the edits."
        assert standin.rule_for(name, table) == {"default": "rejected"}
    for name in ("a1", "a3", "a4", "b3", "b4", "t1", "t5", "memgate-1-1", "memgate-1-4", "a2c"):
        assert standin.rule_for(name, table) is None, name


def test_a_bad_rule_is_refused_not_ignored() -> None:
    for bad in ({"accepted": "1"}, {"default": "maybe"}, {"accepted": [1], "rejected": [1]}, {"nope": 1}):
        with pytest.raises(ValueError):
            standin.check_rule(bad)


# ---------------------------------------------------------------------------
# State kept between sessions, save_document, errors
# ---------------------------------------------------------------------------


def test_state_is_kept_in_a_file_so_the_next_session_sees_the_suggestions(tmp_path: Path) -> None:
    state = tmp_path / "state.json"
    doc = _write_fixture_doc(tmp_path)
    one = standin.Standin(state_path=state)
    one.open_document(str(doc))
    one.propose_edits([_edit(1, _hashes(one.get_document())[1], "basically ordered", "ordered", "Edit 1: hedge")])
    two = standin.Standin(state_path=state, decisions={"default": "rejected"})
    assert two.get_document(limit=1)["url"] == os.path.realpath(doc)
    assert two.list_suggestions()["suggestions"][0]["status"] == "rejected"
    three = standin.Standin(state_path=state)
    assert three.list_suggestions()["suggestions"][0]["status"] == "rejected"  # kept
    assert three.propose_edits([_edit(2, three.get_document()["blocks"][2]["hash"], "really ", "", "Edit 2")])["results"][0]["id"] == "s2"


def test_save_writes_accepted_text_and_the_whole_file_in_the_apps_layout(tmp_path: Path) -> None:
    path = _doc(tmp_path, "# T\n\nOne basically wrapped\nline here.\n\n- item\n")
    app = standin.Standin(decisions={"accepted": [1], "default": "rejected"})
    app.open_document(str(path))
    app.propose_edits([_edit(1, app.get_document()["blocks"][1]["hash"], "basically ", "", "Edit 1")])
    app.list_suggestions()
    assert app.save_document() == {"ok": True}
    assert path.read_text(encoding="utf-8") == "# T\n\nOne wrapped line here.\n\n* item"  # NOTES d: unwrapped, `*`, no final newline
    assert app.get_document(limit=1)["hasChanges"] is False


def test_tools_without_an_open_document_and_a_missing_file_fail_with_a_message(tmp_path: Path) -> None:
    app = standin.Standin()
    for call in (app.get_document, app.save_document, lambda: app.propose_edits([])):
        with pytest.raises(standin.StandinError, match="no document is open"):
            call()
    with pytest.raises(standin.StandinError, match="nothing.md"):
        app.open_document(str(tmp_path / "nothing.md"))
    assert app.list_suggestions() == {"ok": True, "suggestions": []}


# ---------------------------------------------------------------------------
# Over a real stdio transport, the way claude -p reaches it
# ---------------------------------------------------------------------------


def _server_env(tmp_path: Path, scenario: str, decisions: Path | None) -> dict[str, str]:
    env = get_default_environment()
    env.update({"MARKUP_STANDIN_STATE": str(tmp_path / "state.json"), "MARKUP_STANDIN_SCENARIO": scenario})
    if decisions is not None:
        env["MARKUP_STANDIN_DECISIONS"] = str(decisions)
    return env


async def _session(tmp_path: Path, scenario: str, decisions: Path | None, calls: list[tuple[str, dict[str, Any]]]) -> list[Any]:
    params = StdioServerParameters(command=sys.executable, args=[str(ACC / "markup_standin.py")],
                                   env=_server_env(tmp_path, scenario, decisions))
    out: list[Any] = []
    async with stdio_client(params) as (read, write), ClientSession(read, write) as session:
        await session.initialize()
        names = {t.name for t in (await session.list_tools()).tools}
        assert names >= {"open_document", "get_document", "propose_edits", "list_suggestions", "undo", "save_document"}
        for tool, args in calls:
            result = await session.call_tool(tool, args)
            text = result.content[0].text  # type: ignore[union-attr]
            out.append((result.is_error, text if result.is_error else json.loads(text) if text.startswith("{") else text))
    return out


def test_two_sessions_over_stdio_a_proposal_then_the_authors_decisions(tmp_path: Path) -> None:
    doc = _write_fixture_doc(tmp_path)
    decisions = tmp_path / "decisions.json"
    decisions.write_text(json.dumps({"a2": {"accepted": [1], "default": "rejected"}}), encoding="utf-8")
    first = asyncio.run(_session(tmp_path, "a1", decisions, [
        ("open_document", {"path": str(doc)}),
        ("get_document", {"from": 0, "limit": 1}),
        ("get_document", {"from": 1}),
        ("list_suggestions", {}),
    ]))
    assert first[0] == (False, "Opened") and first[1][1]["next"] == 1
    page = first[2][1]
    assert page["blocks"][0]["index"] == 1 and first[3][1]["suggestions"] == []
    edits = [_edit(1, page["blocks"][0]["hash"], "basically ordered", "ordered", "Edit 1: hedge"),
             _edit(2, page["blocks"][1]["hash"], "really quite simply ", "", "Edit 2: hedge")]
    proposed = asyncio.run(_session(tmp_path, "a1", decisions, [("propose_edits", {"edits": edits})]))[0][1]
    assert [r["id"] for r in proposed["results"]] == ["s1", "s2"]
    # a1 never lists, and a1's own list would not have decided: no rule for "a1".
    second = asyncio.run(_session(tmp_path, "a2", decisions, [("list_suggestions", {})]))[0][1]
    assert [(s["id"], s["status"]) for s in second["suggestions"]] == [("s1", "accepted"), ("s2", "rejected")]


def test_a_tool_error_is_an_error_result_over_stdio(tmp_path: Path) -> None:
    (is_error, text), = asyncio.run(_session(tmp_path, "x", None, [("save_document", {})]))
    assert is_error and "no document is open" in text


def test_scripted_statuses_reach_suggest_py_as_accept_reject_and_hold(tmp_path: Path) -> None:
    """The author's decisions are read from list_suggestions' reply by the skill's own mapper (review.py decisions)."""
    app = _proposed(tmp_path, decisions={"accepted": [1], "rejected": [2]})
    listed = app.list_suggestions()["suggestions"]
    sent = [{"n": int(s["note"].split()[1].rstrip(":")), "id": s["id"], "find": s["find"], "replace": s["replace"]} for s in listed]
    mapped = suggest.map_statuses(sent, listed)
    assert (mapped["accept"], mapped["reject"], mapped["hold"], mapped["warnings"]) == ([1], [2], [3, 6], [])
