# SPDX-License-Identifier: AGPL-3.0-or-later
"""The checks of ``nx aura check`` (RDR-249 Technical Design 2; bead nexus-mdbds.10.2, steps 1, 2 and 4).

Unit tests: no engine. The dictionary checks run on templates shaped like the registry's wire
(``nexus.tuple_templates.resolve_tuple_template``'s input); the real engine's registry is read in
``test_registry_engine.py``.
"""
from __future__ import annotations

from pathlib import Path
from typing import Any

import pytest

from nexus.auras import AuraEntry, enumerate_auras, parse_aura
from nexus.auras.check import (
    AuraCheck,
    Finding,
    check_arguments,
    check_auras,
    check_clear,
    check_dictionary,
)
from nexus.lua_runtime import library
from nexus.lua_runtime.header import parse as parse_header
from nexus.lua_runtime.library import LibraryError

from ._support import TD1_EXAMPLE, minimal

HEADER = "-- procedure: {name}\n-- summary: a procedure\n-- args: {args}\n-- grants: none\nreturn 1\n"

BOARD_CI = {
    "name": "board/ci/<topic>",
    "dimensions": {"from": {"type": "string"}, "kind": {"type": "string"}, "sha": {}, "outcome": {}},
    "doc": {"body": {"state": "queued", "sha": "40 hex", "url": "a url"}},
}
BOARD = {"name": "board/<topic>", "dimensions": {"from": {"type": "string"}}}


@pytest.fixture
def shipped(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    root = tmp_path / "shipped"
    root.mkdir()
    monkeypatch.setattr(library, "shipped_root", lambda: root)
    monkeypatch.delenv(library.REPO_ROOT_ENV, raising=False)
    return root


def _proc(root: Path, name: str, args: str) -> None:
    (root / f"{name}.lua").write_text(HEADER.format(name=name, args=args), encoding="ascii")


def _aura(text: str, name: str = "mini"):
    return parse_aura(text.encode("utf-8"), name=name)


def _header(args: str, name: str = "some_proc"):
    return parse_header(HEADER.format(name=name, args=args).encode("ascii"), name=name)


def _keys(findings: list[Finding]) -> set[tuple[str, str]]:
    return {(f.check, f.key) for f in findings}


# -- step 1: the argument mapping -------------------------------------------------------------------


def test_every_required_argument_mapped_is_clean() -> None:
    aura = _aura(TD1_EXAMPLE, "ci_develop_red")
    assert check_arguments(aura, _header("sha:string, outcome:string", "ci_red_notice")) == []


def test_a_required_argument_that_is_not_mapped_is_refused_naming_it() -> None:
    aura = _aura(minimal(extra='\n[args]\nsha = "dims.sha"\n'))
    got = check_arguments(aura, _header("sha:string, outcome:string"))
    assert _keys(got) == {("arguments", "args.outcome")}


def test_a_mapping_for_an_argument_the_header_does_not_declare_is_refused() -> None:
    aura = _aura(minimal(extra='\n[args]\nsha = "dims.sha"\nbogus = "id"\n'))
    got = check_arguments(aura, _header("sha:string"))
    assert _keys(got) == {("arguments", "args.bogus")}


def test_an_optional_argument_may_stay_unmapped() -> None:
    aura = _aura(minimal(extra='\n[args]\nsha = "dims.sha"\n'))
    assert check_arguments(aura, _header("sha:string, note:string?")) == []


def test_the_reserved_arguments_the_host_fills_need_no_mapping() -> None:
    aura = _aura(minimal(extra='\n[args]\nsha = "dims.sha"\n'))
    assert check_arguments(aura, _header("sha:string, now:string, repo:string")) == []


def test_a_procedure_with_no_arguments_and_an_aura_with_no_args_is_clean() -> None:
    assert check_arguments(_aura(minimal()), _header("none")) == []


def test_both_a_missing_and_an_unknown_mapping_are_reported() -> None:
    aura = _aura(minimal(extra='\n[args]\nbogus = "id"\n'))
    assert _keys(check_arguments(aura, _header("sha:string"))) == {
        ("arguments", "args.sha"), ("arguments", "args.bogus"),
    }


# -- step 2: [clear] --------------------------------------------------------------------------------


def _with_tables(trigger_match: str | None, clear_match: str | None):
    text = minimal()
    if trigger_match is not None:
        text = text.replace('subspace = "board/topic"\n', f'subspace = "board/topic"\nmatch = {trigger_match}\n')
    if clear_match is not None:
        text += f"\n[clear]\nmatch = {clear_match}\n"
    return _aura(text)


def test_no_clear_table_is_clean() -> None:
    assert check_clear(_with_tables('{ kind = "sha" }', None)) == []


def test_a_clear_beside_an_absent_trigger_match_is_dead_and_refused() -> None:
    # Every post on the subspace is a trigger, so no post could ever clear.
    [f] = check_clear(_with_tables(None, '{ kind = "sha" }'))
    assert (f.check, f.key) == ("clear", "clear.match") and "absent" in f.detail


def test_a_clear_every_trigger_pair_of_which_also_holds_is_refused() -> None:
    # trigger kind=sha; clear kind=sha and outcome=success: a clear post is also a trigger post.
    got = check_clear(_with_tables('{ kind = "sha" }', '{ kind = "sha", outcome = "success" }'))
    assert _keys(got) == {("clear", "clear.match")}


def test_a_clear_a_post_could_meet_with_the_trigger_is_refused() -> None:
    # No key is in both tables with different values: kind=sha, outcome=success meets both.
    got = check_clear(_with_tables('{ kind = "sha" }', '{ outcome = "success" }'))
    assert _keys(got) == {("clear", "clear.match")}


def test_a_clear_that_differs_from_the_trigger_on_a_shared_key_is_clean() -> None:
    got = check_clear(
        _with_tables('{ kind = "sha", outcome = "failure" }', '{ kind = "sha", outcome = "success" }')
    )
    assert got == []


def test_a_clear_on_a_different_value_of_one_shared_key_is_clean_even_with_other_keys() -> None:
    assert check_clear(_with_tables('{ outcome = "failure" }', '{ outcome = "success", kind = "sha" }')) == []


# -- the dictionary (the checks that need the registry) ---------------------------------------------


def _dictionary(aura, templates: list[dict[str, Any]]):
    return check_dictionary(aura, templates)


def test_an_aura_whose_fields_are_all_in_the_template_is_clean() -> None:
    aura = _aura(TD1_EXAMPLE, "ci_develop_red")
    findings, skipped = _dictionary(aura, [BOARD, BOARD_CI])
    assert (findings, skipped) == ([], [])


def test_a_subspace_no_template_matches_is_refused() -> None:
    findings, _ = _dictionary(_aura(minimal()), [BOARD_CI])  # board/topic: only board/ci/<topic> is listed
    assert _keys(findings) == {("dictionary", "trigger.subspace")}


def test_a_trigger_match_field_the_template_lacks_is_refused() -> None:
    aura = _aura(minimal().replace('subspace = "board/topic"\n', 'subspace = "board/topic"\nmatch = { zzz = "1" }\n'))
    findings, _ = _dictionary(aura, [BOARD])
    assert _keys(findings) == {("dictionary", "trigger.match.zzz")}


def test_a_clear_match_field_the_template_lacks_is_refused() -> None:
    aura = _aura(
        minimal().replace('subspace = "board/topic"\n', 'subspace = "board/topic"\nmatch = { from = "a" }\n')
        + '\n[clear]\nmatch = { zzz = "1" }\n'
    )
    findings, _ = _dictionary(aura, [BOARD])
    assert _keys(findings) == {("dictionary", "clear.match.zzz")}


def test_a_dims_source_the_template_lacks_is_refused_and_one_it_has_is_not() -> None:
    aura = _aura(minimal(extra='\n[args]\nok = "dims.from"\nbad = "dims.zzz"\n'))
    findings, _ = _dictionary(aura, [BOARD])
    assert _keys(findings) == {("dictionary", "args.bad")}


def test_a_body_source_is_checked_against_the_docs_body_fields() -> None:
    aura = _aura(minimal(extra='\n[args]\nok = "body.sha"\nbad = "body.zzz"\n').replace("board/topic", "board/ci/x"))
    findings, skipped = _dictionary(aura, [BOARD_CI])
    assert _keys(findings) == {("dictionary", "args.bad")}
    assert skipped == []


def test_a_body_source_against_a_template_with_no_doc_body_is_skipped_by_name_never_passed() -> None:
    aura = _aura(minimal(extra='\n[args]\nx = "body.anything"\n'))
    findings, skipped = _dictionary(aura, [BOARD])
    assert findings == []
    assert [s.check for s in skipped] == ["dictionary.body"]
    assert "doc" in skipped[0].reason


def test_the_id_subspace_and_literal_sources_need_no_dictionary() -> None:
    aura = _aura(minimal(extra="\n[args]\na = \"id\"\nb = \"subspace\"\nc = '= \"x\"'\n"))
    assert _dictionary(aura, [BOARD]) == ([], [])


def test_the_most_specific_template_is_the_one_checked() -> None:
    # board/ci/x resolves to board/ci/<topic>, not board/<topic>: sha is a dimension of the first only.
    aura = _aura(minimal(extra='\n[args]\ns = "dims.sha"\n').replace("board/topic", "board/ci/x"))
    assert _dictionary(aura, [BOARD, BOARD_CI])[0] == []


# -- the whole check of one aura: procedure, arguments, clear, dictionary ---------------------------


def _write_aura(root: Path, name: str, text: str) -> None:
    (root / f"{name}.aura.toml").write_text(text, encoding="utf-8")


def _check(shipped_root: Path, **kw: Any) -> dict[str, AuraCheck]:
    del shipped_root
    kw.setdefault("templates", [BOARD, BOARD_CI])
    return {c.name: c for c in check_auras(enumerate_auras(cwd=None), cwd=None, **kw)}


def test_a_clean_aura_passes_every_check(shipped: Path) -> None:
    _proc(shipped, "ci_red_notice", "sha:string, outcome:string")
    _write_aura(shipped, "ci_develop_red", TD1_EXAMPLE)
    c = _check(shipped)["ci_develop_red"]
    assert (c.ok, c.findings, c.skipped) == (True, (), ())


def test_a_procedure_that_is_not_in_the_library_is_refused(shipped: Path) -> None:
    _write_aura(shipped, "ci_develop_red", TD1_EXAMPLE)
    c = _check(shipped)["ci_develop_red"]
    assert not c.ok
    assert ("procedure", "procedure") in _keys(list(c.findings))


def test_a_procedure_whose_header_does_not_parse_is_refused(shipped: Path) -> None:
    (shipped / "ci_red_notice.lua").write_text("return 1\n", encoding="ascii")
    _write_aura(shipped, "ci_develop_red", TD1_EXAMPLE)
    c = _check(shipped)["ci_develop_red"]
    assert ("procedure", "procedure") in _keys(list(c.findings))


def test_the_argument_findings_come_with_the_procedure_found(shipped: Path) -> None:
    _proc(shipped, "ci_red_notice", "sha:string, outcome:string, extra:string")
    _write_aura(shipped, "ci_develop_red", TD1_EXAMPLE)
    c = _check(shipped)["ci_develop_red"]
    assert _keys(list(c.findings)) == {("arguments", "args.extra")}


def test_without_the_registry_the_dictionary_checks_are_skipped_by_name_and_the_rest_still_run(
    shipped: Path,
) -> None:
    _proc(shipped, "ci_red_notice", "sha:string, outcome:string, extra:string")
    _write_aura(shipped, "ci_develop_red", TD1_EXAMPLE)
    c = _check(shipped, templates=None, unavailable="the engine is unreachable")["ci_develop_red"]
    assert _keys(list(c.findings)) == {("arguments", "args.extra")}  # the offline checks ran
    assert [s.check for s in c.skipped] == ["dictionary"]
    assert "unreachable" in c.skipped[0].reason


def test_a_clean_offline_aura_with_the_registry_unavailable_is_not_reported_as_fully_passed(
    shipped: Path,
) -> None:
    _proc(shipped, "ci_red_notice", "sha:string, outcome:string")
    _write_aura(shipped, "ci_develop_red", TD1_EXAMPLE)
    c = _check(shipped, templates=None, unavailable="down")["ci_develop_red"]
    assert c.ok  # nothing is refused ...
    assert c.skipped  # ... but the skip is carried, so a caller cannot print it as a full pass


def test_a_registry_that_lists_no_templates_skips_the_dictionary_rather_than_passing_it(shipped: Path) -> None:
    _proc(shipped, "ci_red_notice", "sha:string, outcome:string")
    _write_aura(shipped, "ci_develop_red", TD1_EXAMPLE)
    c = _check(shipped, templates=[])["ci_develop_red"]
    assert [s.check for s in c.skipped] == ["dictionary"]
    assert "no templates" in c.skipped[0].reason


def test_a_file_that_does_not_parse_is_a_parse_finding_naming_the_key(shipped: Path) -> None:
    _write_aura(shipped, "bad", 'zzz = "x"\n')
    c = _check(shipped)["bad"]
    assert [(f.check, f.key) for f in c.findings] == [("parse", "zzz")]
    assert not c.ok


def test_a_library_refusal_is_a_finding_naming_the_reason(shipped: Path) -> None:
    (shipped / "Bad-Name.aura.toml").write_text(minimal("Bad-Name"), encoding="utf-8")
    c = _check(shipped)["Bad-Name"]
    assert [f.check for f in c.findings] == ["library"]
    assert "invalid_name" in c.findings[0].detail


def test_two_auras_that_write_the_same_threads_block_are_both_refused(shipped: Path) -> None:
    _proc(shipped, "some_proc", "none")
    _write_aura(shipped, "one", minimal("one").replace("a block", "same"))
    _write_aura(shipped, "two", minimal("two").replace("a block", "same"))
    _write_aura(shipped, "three", minimal("three").replace("a block", "own"))
    got = _check(shipped)
    for name in ("one", "two"):
        assert ("threads", "display.threads") in _keys(list(got[name].findings)), name
    assert "two" in got["one"].findings[-1].detail
    assert ("threads", "display.threads") not in _keys(list(got["three"].findings))


def test_a_naming_filter_checks_one_aura_but_still_sees_the_other_threads_users(shipped: Path) -> None:
    _proc(shipped, "some_proc", "none")
    _write_aura(shipped, "one", minimal("one").replace("a block", "same"))
    _write_aura(shipped, "two", minimal("two").replace("a block", "same"))
    out = check_auras(enumerate_auras(cwd=None), cwd=None, templates=[BOARD], only="one")
    assert [c.name for c in out] == ["one"]
    assert ("threads", "display.threads") in _keys(list(out[0].findings))


def test_a_procedure_in_no_root_is_a_procedure_finding_naming_it(shipped: Path) -> None:
    _write_aura(shipped, "mini", minimal("mini"))
    c = _check(shipped)["mini"]
    [f] = [f for f in c.findings if f.check == "procedure"]
    assert "some_proc" in f.detail and "not_found" in f.detail


def test_every_entry_kind_is_an_auracheck_not_a_raise(shipped: Path) -> None:
    entries = [
        AuraEntry("lib", "shipped", LibraryError("shadows_shipped", "x")),
    ]
    [c] = check_auras(entries, cwd=None, templates=[BOARD])
    assert isinstance(c, AuraCheck) and not c.ok
