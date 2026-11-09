# SPDX-License-Identifier: AGPL-3.0-or-later
# Copyright (c) 2026 Hal Hildebrand. All rights reserved.
"""The gate_record procedure (RDR-240 Technical Design 6, nexus-qo7ie.34).

The script runs in the same test-local harness as research_add's: the environment the
runtime builds (RDR-242 Technical Design 7) from the static check's own allowlists, loaded
in mode "t", with its arguments as the main chunk's vararg. gate_record calls no verb.
The reserved arguments go through ``header.prepare_args`` as run_script will apply them.
"""
from __future__ import annotations

import re
import tomllib
from datetime import UTC, datetime
from pathlib import Path

import lupa.lua54 as lua54
import pytest

from nexus import memory_curate
from nexus.commands import rdr as rdr_cmd
from nexus.lua_runtime import header as hd
from nexus.lua_runtime import static_check as sc
from nexus.lua_runtime.errors import ScriptError
from tests.test_review_agent_verdict_vocabulary_lint import OUTCOMES

SCRIPT = Path(hd.__file__).parent.parent / "procedures" / "gate_record.lua"
SOURCE = SCRIPT.read_bytes()
AUDIT_TAGS = Path(hd.__file__).parent.parent / "tables" / "audit-tags.toml"
FIXED = datetime(2026, 11, 8, 23, 59, 58, tzinfo=UTC)
KINDS = ("review", "critique", "confirm", "fix-check")


def _run(**args: object) -> dict:
    rt = lua54.LuaRuntime(register_eval=False, register_builtins=False, unpack_returned_tuples=False)
    build = rt.execute(
        """
        return function(base, libs)
          local env = {}
          for _, name in ipairs(base) do env[name] = _G[name] end
          for lib, members in pairs(libs) do
            local t = {}
            for _, m in ipairs(members) do t[m] = _G[lib][m] end
            env[lib] = t
          end
          getmetatable("").__index = env.string
          return env
        end
        """
    )
    env = build(
        rt.table_from(sorted(sc.BASE_FUNCTIONS)),
        rt.table_from({lib: rt.table_from(sorted(m)) for lib, m in sc.LIB_MEMBERS.items()}),
    )
    loaded = rt.eval("load")(SOURCE.decode("ascii"), "=gate_record", "t", env)
    fn = loaded[0] if isinstance(loaded, tuple) else loaded
    return dict(fn(rt.table_from(args)).items())


BODY = "## Findings\nNone blocking.\n\n## Verdict\n\n- **outcome**: partial\n- **confidence**: high\n"
ARGS = {"kind": "review", "bead": "nexus-qo7ie.34", "rdr": 240, "sha": "9687b6cd8", "outcome": "partial",
        "body": BODY, "now": "2026-11-08T23:59:58Z", "repo": "nexus"}


def test_the_header_parses_and_the_static_check_passes_with_no_grants() -> None:
    h = hd.parse(SOURCE, name="gate_record")
    assert [(a.name, a.type, a.optional) for a in h.args] == [
        ("kind", "string", False), ("bead", "string", False), ("rdr", "integer", True), ("sha", "string", False),
        ("outcome", "string", False), ("body", "string", False), ("round", "integer", True), ("now", "string", False),
        ("repo", "string", False)]
    assert h.grants == frozenset()
    assert sc.check(SOURCE, h.grants) == frozenset()


def test_title_and_tags_are_derived_exactly() -> None:
    out = _run(**ARGS)
    assert out["project"] == "nexus"
    assert out["title"] == "review-9687b6cd8-nexus-qo7ie.34-2026-11-08"
    assert out["tags"] == "kind:gate,bead:nexus-qo7ie.34,rdr:240"


def test_without_an_rdr_the_rdr_tag_is_dropped() -> None:
    out = _run(**{k: v for k, v in ARGS.items() if k != "rdr"})
    assert out["tags"] == "kind:gate,bead:nexus-qo7ie.34"
    assert "rdr:" not in out["content"].split("\n\n", 1)[0]


def test_a_small_rdr_number_is_padded_to_three_as_the_tag_convention_writes_it() -> None:
    out = _run(**{**ARGS, "rdr": 7})
    assert out["tags"].endswith(",rdr:007")


@pytest.mark.parametrize("kind", KINDS)
def test_each_kinds_title_is_matched_by_the_audit_tag_table(kind: str) -> None:
    patterns = tomllib.loads(AUDIT_TAGS.read_text(encoding="utf-8"))
    found = [p for p in _title_patterns(patterns) if re.search(p, _run(**{**ARGS, "kind": kind})["title"])]
    assert found, kind


def _title_patterns(doc: object) -> list[str]:
    out: list[str] = []
    if isinstance(doc, dict):
        for k, v in doc.items():
            if k == "title_patterns" and isinstance(v, list):
                out += [p for p in v if isinstance(p, str)]
            else:
                out += _title_patterns(v)
    elif isinstance(doc, list):
        for v in doc:
            out += _title_patterns(v)
    return out


def test_the_audit_tag_table_has_title_patterns_to_match() -> None:
    assert len(_title_patterns(tomllib.loads(AUDIT_TAGS.read_text(encoding="utf-8")))) >= 4


def test_the_rollup_reads_the_record_as_a_gate_record_of_its_rdr() -> None:
    tags = memory_curate.split_tags(_run(**ARGS)["tags"])
    assert "kind:gate" in tags
    assert [m.group(1) for t in tags if (m := memory_curate._RDR_TAG.match(t))] == ["240"]
    assert memory_curate._phases_of(_run(**ARGS)["title"]) == []


def test_the_body_comes_back_verbatim_after_the_fields() -> None:
    body = "line one\n- **outcome**: partial\nunicode: é—ok; `x` $(y)\n\n\ttabbed\n"
    out = _run(**{**ARGS, "body": body})
    head, rest = out["content"].split("\n\n", 1)
    assert rest == body
    assert head.splitlines() == ["kind: review", "bead: nexus-qo7ie.34", "rdr: 240", "sha: 9687b6cd8",
                                 "outcome: partial", "date: 2026-11-08"]
    # The reader takes the first match, so the record's own fields win over the body's lines.
    assert rdr_cmd._preamble_parse_t2_field(out["content"], "outcome") == "partial"


@pytest.mark.parametrize("field,value,words", [
    ("outcome", "approved", "outcome must be one of justified, partial, not-justified; got approved"),
    ("outcome", "Partial", "outcome must be one of"),
    ("kind", "audit", "kind must be one of review, critique, confirm, fix-check; got audit"),
    ("sha", "XYZ1234", "sha must be"),
    ("sha", "abc12", "sha must be"),
    ("sha", "a" * 65, "sha must be"),
    ("bead", "not a bead", "bead must be"),
    ("bead", "nexus-a,rdr:999", "bead must be"),
    ("bead", "-nexus", "bead must be"),
    ("bead", "", "bead must be"),
    ("bead", "x\u00aa", "bead must be"),
    ("rdr", 0, "rdr must be a positive integer"),
    ("rdr", 10000, "rdr must be a positive integer up to 9999"),
    ("round", 0, "round must be"),
    ("round", 100, "round must be"),
    ("body", "  \n ", "body must not be empty"),
    ("now", "yesterday", "now must be"),
])
def test_a_bad_value_is_refused_with_no_record(field: str, value: object, words: str) -> None:
    with pytest.raises(lua54.LuaError) as e:
        _run(**{**ARGS, field: value})
    assert words in str(e.value)


def test_a_body_whose_verdict_disagrees_with_outcome_is_refused() -> None:
    with pytest.raises(lua54.LuaError) as e:
        _run(**{**ARGS, "outcome": "justified"})
    assert "disagrees" in str(e.value)


def test_the_scripts_vocabulary_is_the_review_agents() -> None:
    text = SOURCE.decode("ascii")
    listed = re.search(r"local OUTCOMES = \{([^}]*)\}", text)
    assert listed is not None
    assert tuple(re.findall(r'"([^"]+)"', listed.group(1))) == OUTCOMES


@pytest.mark.parametrize("kind", KINDS)
def test_every_kind_is_accepted(kind: str) -> None:
    assert _run(**{**ARGS, "kind": kind})["title"].startswith(f"{kind}-9687b6cd8-")


# ---- the reserved arguments, as run_script applies them (RDR-240 gate r2 Significant 1) ----

GIVEN = {k: v for k, v in ARGS.items() if k not in ("now", "repo")}


def test_now_and_repo_are_filled_by_the_host(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    h = hd.parse(SOURCE, name="gate_record")
    probe = hd.RepoProbe(tmp_path, "nexus")
    got = hd.prepare_args(h, GIVEN, cwd=tmp_path, clock=lambda: FIXED, repo_probe=lambda _: probe)
    assert (got["now"], got["repo"]) == ("2026-11-08T23:59:58Z", "nexus")
    assert _run(**got)["title"] == "review-9687b6cd8-nexus-qo7ie.34-2026-11-08"


def test_outside_a_repo_the_run_is_refused_naming_repo(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("GIT_CEILING_DIRECTORIES", str(tmp_path))
    plain = tmp_path / "Downloads"
    plain.mkdir()
    with pytest.raises(ScriptError) as e:
        hd.prepare_args(hd.parse(SOURCE, name="gate_record"), GIVEN, cwd=plain, clock=lambda: FIXED)
    assert e.value.kind == "args" and "'repo'" in str(e.value) and "no repo here" in str(e.value)


@pytest.mark.parametrize("reserved", ["now", "repo"])
def test_a_caller_passing_now_or_repo_is_refused(reserved: str, tmp_path: Path) -> None:
    with pytest.raises(ScriptError) as e:
        hd.prepare_args(hd.parse(SOURCE, name="gate_record"), {**GIVEN, reserved: "x"}, cwd=tmp_path,
                        clock=lambda: FIXED)
    assert e.value.kind == "args" and "reserved" in str(e.value)


@pytest.mark.parametrize("bead", ["ART-12", "my-proj-abc12", "nexus-qo7ie.34", "x"])
def test_any_repos_bead_id_is_accepted(bead: str) -> None:
    out = _run(**{**ARGS, "bead": bead})
    assert out["tags"].split(",")[1] == f"bead:{bead}"


def test_a_round_separates_a_redispatched_pass() -> None:
    assert _run(**{**ARGS, "round": 2})["title"] == "review-9687b6cd8-nexus-qo7ie.34-2026-11-08-r2"


def test_the_same_pass_twice_has_the_same_title() -> None:
    # Stated, not accidental: the second memory_put of one pass replaces the first.
    assert _run(**ARGS)["title"] == _run(**ARGS)["title"]


@pytest.mark.parametrize("body", [
    "Example block:\n- **outcome**: justified\n\n## Verdict\n- **outcome**: partial\n",
    "> - **outcome**: not-justified\n\n- **outcome**: partial\n",
    "- **outcome**: `partial`\n",
    "* **outcome**: **partial**\n",
    "no verdict block at all\n",
])
def test_the_last_verdict_line_is_compared(body: str) -> None:
    assert _run(**{**ARGS, "outcome": "partial", "body": body})["content"].endswith(body)


@pytest.mark.parametrize("body", [
    "- **outcome**: partial\nlater\n- **outcome**: justified\n",
    "- **outcome**: `justified`\n",
    "* **outcome**: **not-justified**\n",
    "- **outcome:** justified\n",
    "**outcome**: justified\n",
])
def test_a_disagreeing_last_verdict_line_is_refused(body: str) -> None:
    with pytest.raises(lua54.LuaError) as e:
        _run(**{**ARGS, "outcome": "partial", "body": body})
    assert "disagrees" in str(e.value)
