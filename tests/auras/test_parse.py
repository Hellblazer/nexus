# SPDX-License-Identifier: AGPL-3.0-or-later
"""The aura file parser (RDR-249 Technical Design 1 and 6; bead nexus-mdbds.10.1).

One test per refusal kind, the valid TD1 example, and the closed key set. A refusal is an
:class:`~nexus.auras.AuraError` naming the file and the key.
"""
from __future__ import annotations

import pytest

from nexus.auras import ArgSource, Aura, AuraError, parse_aura
from nexus.lua_runtime.library import READ_LIMIT
from nexus.lua_runtime.limits import Limits

from ._support import TD1_EXAMPLE, minimal


def _parse(text: str | bytes, name: str = "mini") -> Aura:
    data = text.encode("utf-8") if isinstance(text, str) else text
    return parse_aura(data, name=name)


def _refused(text: str | bytes, kind: str, key: str, name: str = "mini") -> AuraError:
    with pytest.raises(AuraError) as err:
        _parse(text, name)
    assert err.value.kind == kind
    assert err.value.key == key
    assert err.value.file == f"{name}.aura.toml"
    assert f"{name}.aura.toml" in str(err.value)
    assert key in str(err.value)
    return err.value


# -- valid files --------------------------------------------------------------


def test_the_td1_example_round_trips() -> None:
    aura = _parse(TD1_EXAMPLE, "ci_develop_red")
    assert aura.name == "ci_develop_red"
    assert aura.summary == "A red develop sha, posted to this session's threads entry."
    assert aura.procedure == "ci_red_notice"
    assert aura.subspace == "board/ci/nexus-develop--done"
    assert dict(aura.match) == {"kind": "sha", "outcome": "failure"}
    assert dict(aura.args) == {"sha": ArgSource("dims", "sha"), "outcome": ArgSource("dims", "outcome")}
    assert aura.when_field == "show"
    assert dict(aura.clear_match or ()) == {"kind": "sha", "outcome": "success"}
    assert aura.threads == "develop CI red"
    assert aura.channel is True
    assert aura.limits == Limits()


def test_the_smallest_file_parses_with_its_defaults() -> None:
    aura = _parse(minimal())
    assert aura.match == ()
    assert aura.args == ()
    assert aura.when_field is None
    assert aura.clear_match is None
    assert aura.channel is False
    assert aura.limits == Limits()


@pytest.mark.parametrize(
    "subspace",
    [
        "board/topic", "board/ci/nexus-develop--done", "board/a.b_c-d", "board/ci/x",
        "board/ci/.github-main--done", "board/ci/a+b-main--done", "board/-lead", "board/ci/ci", "board/cix",
    ],
)
def test_one_concrete_board_subspace_is_accepted(subspace: str) -> None:
    assert _parse(minimal().replace("board/topic", subspace)).subspace == subspace


@pytest.mark.parametrize(
    ("source", "expected"),
    [
        ("dims.sha", ArgSource("dims", "sha")),
        ("body.run_id", ArgSource("body", "run_id")),
        ("id", ArgSource("id", "")),
        ("subspace", ArgSource("subspace", "")),
        ('= "main"', ArgSource("literal", "main")),
        ('= ""', ArgSource("literal", "")),
        ('= "two words"', ArgSource("literal", "two words")),
    ],
)
def test_each_arg_source_form_is_accepted(source: str, expected: ArgSource) -> None:
    aura = _parse(minimal(extra=f"\n[args]\nbranch = '{source}'\n"))
    assert dict(aura.args) == {"branch": expected}


def test_lower_limits_are_accepted() -> None:
    aura = _parse(minimal(extra="\n[limits]\ninstructions = 1000000\nwall_time = 2.5\ndepth = 8\n"))
    assert aura.limits == Limits().lowered(instructions=1_000_000, wall_time=2.5, depth=8)
    assert aura.limits.memory == Limits().memory


def test_limits_equal_to_the_default_are_accepted() -> None:
    assert _parse(minimal(extra=f"\n[limits]\ndepth = {Limits().depth}\n")).limits == Limits()


def test_a_clear_match_that_differs_from_the_trigger_match_is_accepted() -> None:
    text = minimal(extra='\n[clear]\nmatch = { kind = "sha", outcome = "success" }\n').replace(
        '[trigger]\nsubspace = "board/topic"\n',
        '[trigger]\nsubspace = "board/topic"\nmatch = { kind = "sha" }\n',
    )
    assert dict(_parse(text).clear_match or ()) == {"kind": "sha", "outcome": "success"}


# -- an unknown key, at any level -----------------------------------------------


def test_an_unknown_top_level_key_is_refused() -> None:
    _refused('zzz = "x"\n' + minimal(), "unknown_key", "zzz")


@pytest.mark.parametrize(
    ("text", "key"),
    [
        (minimal().replace('subspace = "board/topic"\n', 'subspace = "board/topic"\nmore = "y"\n'), "trigger.more"),
        (minimal(extra='\n[when]\nfield = "f"\nother = "o"\n'), "when.other"),
        (minimal(extra='\n[clear]\nmatch = { a = "b" }\nother = "o"\n'), "clear.other"),
        (minimal().replace('threads = "a block"\n', 'threads = "a block"\ncolour = "red"\n'), "display.colour"),
        (minimal(extra="\n[limits]\nthreads = 4\n"), "limits.threads"),
    ],
)
def test_an_unknown_key_inside_a_table_is_refused(text: str, key: str) -> None:
    _refused(text, "unknown_key", key)


def test_an_unknown_key_beside_a_missing_one_names_the_unknown_one_first() -> None:
    # The closed set is checked before the required keys, so a typo of a required key reads as
    # an unknown key, which names the typo.
    text = minimal().replace("summary =", "sumary =")
    _refused(text, "unknown_key", "sumary")


# -- a missing required key -----------------------------------------------------


@pytest.mark.parametrize(
    ("drop", "key"),
    [
        ('aura = "mini"\n', "aura"),
        ('summary = "does a thing"\n', "summary"),
        ('procedure = "some_proc"\n', "procedure"),
        ('subspace = "board/topic"\n', "trigger.subspace"),
        ('threads = "a block"\n', "display.threads"),
    ],
)
def test_a_missing_required_key_is_refused(drop: str, key: str) -> None:
    text = minimal()
    assert drop in text
    _refused(text.replace(drop, ""), "missing_key", key)


def test_a_missing_required_table_is_refused() -> None:
    head = 'aura = "mini"\nsummary = "s"\nprocedure = "p"\n'
    _refused(head + '[display]\nthreads = "t"\n', "missing_key", "trigger")
    _refused(head + '[trigger]\nsubspace = "board/topic"\n', "missing_key", "display")


def test_when_needs_its_field_and_clear_needs_its_match() -> None:
    _refused(minimal(extra="\n[when]\n"), "missing_key", "when.field")
    _refused(minimal(extra="\n[clear]\n"), "missing_key", "clear.match")


# -- a wrong type ---------------------------------------------------------------


@pytest.mark.parametrize(
    ("text", "key"),
    [
        (minimal().replace('summary = "does a thing"', "summary = 1"), "summary"),
        (minimal().replace('procedure = "some_proc"', "procedure = true"), "procedure"),
        (minimal().replace('aura = "mini"', "aura = 3"), "aura"),
        (minimal().replace('threads = "a block"', 'threads = "a block"\nchannel = "yes"'), "display.channel"),
        (minimal().replace('threads = "a block"', "threads = 5"), "display.threads"),
        (minimal().replace('subspace = "board/topic"', "subspace = 5"), "trigger.subspace"),
        (minimal().replace('subspace = "board/topic"', 'subspace = "board/topic"\nmatch = { kind = 1 }'), "trigger.match.kind"),
        (minimal().replace('subspace = "board/topic"', 'subspace = "board/topic"\nmatch = "kind"'), "trigger.match"),
        (minimal(extra='\n[when]\nfield = true\n'), "when.field"),
        (minimal(extra='\n[clear]\nmatch = { kind = false }\n'), "clear.match.kind"),
        (minimal(extra="\n[args]\nsha = 4\n"), "args.sha"),
        (minimal(extra='\n[limits]\ninstructions = "many"\n'), "limits.instructions"),
        (minimal(extra="\n[limits]\ndepth = true\n"), "limits.depth"),
    ],
)
def test_a_wrong_type_is_refused(text: str, key: str) -> None:
    _refused(text, "wrong_type", key)


def test_a_limit_that_is_not_a_finite_number_is_refused() -> None:
    _refused(minimal(extra="\n[limits]\nwall_time = nan\n"), "bad_limit", "limits.wall_time")


_TABLES = '[trigger]\nsubspace = "board/topic"\n\n[display]\nthreads = "a block"\n'
_HEAD = 'aura = "mini"\nsummary = "s"\nprocedure = "p"\n'


@pytest.mark.parametrize(
    ("text", "key"),
    [
        (_HEAD + 'trigger = "board/topic"\n[display]\nthreads = "t"\n', "trigger"),
        (_HEAD + 'display = 4\n[trigger]\nsubspace = "board/topic"\n', "display"),
        (_HEAD + "args = []\n" + _TABLES, "args"),
        (_HEAD + "when = 1\n" + _TABLES, "when"),
        (_HEAD + "clear = []\n" + _TABLES, "clear"),
        (_HEAD + "limits = 7\n" + _TABLES, "limits"),
    ],
)
def test_a_table_that_is_not_a_table_is_refused(text: str, key: str) -> None:
    _refused(text, "wrong_type", key)


# -- a name that breaks the rule ------------------------------------------------


@pytest.mark.parametrize("name", ["Bad", "1abc", "a-b", "a" * 65, "x.aura", "con", "prn", "com1", "lpt9"])
def test_an_aura_name_that_breaks_the_name_rule_is_refused(name: str) -> None:
    text = minimal().replace('aura = "mini"', f'aura = "{name}"')
    _refused(text, "invalid_name", "aura", name=name)


def test_an_aura_named_differently_from_its_file_is_refused() -> None:
    _refused(minimal(name="other"), "name_mismatch", "aura", name="mini")


@pytest.mark.parametrize("procedure", ["Bad", "con", "a b", "../x", "a" * 65])
def test_a_procedure_name_that_breaks_the_name_rule_is_refused(procedure: str) -> None:
    _refused(minimal().replace('"some_proc"', f'"{procedure}"'), "invalid_name", "procedure")


@pytest.mark.parametrize("arg", ["Sha", "1x", "a" * 33, "a-b"])
def test_an_argument_name_that_breaks_the_argument_rule_is_refused(arg: str) -> None:
    _refused(minimal(extra=f'\n[args]\n"{arg}" = "id"\n'), "invalid_name", f"args.{arg}")


# -- the trigger subspace -------------------------------------------------------


@pytest.mark.parametrize(
    "subspace",
    [
        "board/*",
        "board/ci/*",
        "board/ci/*--done",
        "mailbox/self",
        "mailbox/*",
        "board/",
        "board",
        "board/ci/",
        "board/a/b",
        "board/ci/a/b",
        "queue/work",
        "board/a b",
        "board/ci",
        " board/topic",
        "board/topic\n",
        "",
    ],
)
def test_a_subspace_that_is_not_one_concrete_board_topic_is_refused(subspace: str) -> None:
    text = minimal().replace('subspace = "board/topic"', f"subspace = {_toml_str(subspace)}")
    _refused(text, "bad_subspace", "trigger.subspace")


def _toml_str(value: str) -> str:
    escaped = value.replace("\\", "\\\\").replace('"', '\\"').replace("\n", "\\n")
    return f'"{escaped}"'


# -- the argument map -----------------------------------------------------------


@pytest.mark.parametrize("reserved", ["now", "repo"])
def test_mapping_a_reserved_argument_is_refused(reserved: str) -> None:
    _refused(minimal(extra=f'\n[args]\n{reserved} = "dims.sha"\n'), "reserved_arg", f"args.{reserved}")


@pytest.mark.parametrize(
    "source",
    ["dim.sha", "dims.", "dims", "body", "body.", "dims.a.b", "dims.1x", "sha", "ID", "= value", 'x = "v"', "=", '= "unterminated', "", " id"],
)
def test_an_arg_source_outside_the_four_forms_and_a_literal_is_refused(source: str) -> None:
    _refused(minimal(extra=f"\n[args]\nsha = {_toml_str(source)}\n"), "bad_source", "args.sha")


# -- clear.match ----------------------------------------------------------------


def test_a_clear_match_equal_to_the_trigger_match_is_refused() -> None:
    text = minimal(extra='\n[clear]\nmatch = { outcome = "failure", kind = "sha" }\n').replace(
        'subspace = "board/topic"\n',
        'subspace = "board/topic"\nmatch = { kind = "sha", outcome = "failure" }\n',
    )
    _refused(text, "clear_equals_trigger", "clear.match")


def test_a_clear_match_equal_to_an_absent_trigger_match_is_refused() -> None:
    # An absent trigger.match is every post; an empty clear.match is every post too.
    _refused(minimal(extra="\n[clear]\nmatch = {}\n"), "clear_equals_trigger", "clear.match")


# -- the limits (TD6) -----------------------------------------------------------


@pytest.mark.parametrize(
    "line",
    ["instructions = 50000001", "memory = 67108865", "wall_time = 60.5", "result = 1048577", "depth = 33"],
)
def test_a_limit_above_the_default_is_refused(line: str) -> None:
    key = f"limits.{line.split(' ')[0]}"
    _refused(minimal(extra=f"\n[limits]\n{line}\n"), "bad_limit", key)


def test_a_fractional_value_for_a_whole_number_limit_is_refused() -> None:
    _refused(minimal(extra="\n[limits]\ndepth = 1.5\n"), "wrong_type", "limits.depth")


@pytest.mark.parametrize("line", ["instructions = 0", "depth = -1", "memory = 1024", "result = 0"])
def test_a_limit_at_or_below_zero_or_under_the_memory_floor_is_refused(line: str) -> None:
    key = f"limits.{line.split(' ')[0]}"
    _refused(minimal(extra=f"\n[limits]\n{line}\n"), "bad_limit", key)


# -- the file itself ------------------------------------------------------------


def test_text_that_is_not_toml_is_refused() -> None:
    _refused("aura = = =\n", "bad_toml", "(file)")


def test_bytes_that_are_not_utf8_are_refused() -> None:
    _refused(b"aura = '\xff\xfe'\n", "bad_toml", "(file)")


def test_a_file_over_the_read_bound_is_refused() -> None:
    _refused(minimal() + "# " + "x" * READ_LIMIT + "\n", "too_large", "(file)")


def test_a_threads_value_with_a_line_break_or_control_character_is_refused() -> None:
    for bad in ("two\\nlines", "tab\\there", "nul\\u0000"):
        _refused(minimal().replace('threads = "a block"', f'threads = "{bad}"'), "bad_threads", "display.threads")


def test_a_blank_threads_value_is_refused() -> None:
    _refused(minimal().replace('threads = "a block"', 'threads = "  "'), "bad_threads", "display.threads")


def test_a_refusal_kind_is_a_closed_set() -> None:
    with pytest.raises(ValueError, match="unknown AuraError kind"):
        AuraError("nope", "x.aura.toml", "aura", "detail")


# -- the review fix round (51b40501d) --------------------------------------------------


def test_a_file_nested_too_deeply_for_the_toml_reader_is_refused_not_a_recursion_error() -> None:
    depth = 30_000
    data = b"a = " + b"[" * depth + b"]" * depth
    assert len(data) < READ_LIMIT
    _refused(data, "bad_toml", "(file)")


def test_the_read_bound_is_exact() -> None:
    base = minimal()
    exact = base + "#" * (READ_LIMIT - 1 - len(base) - 1) + "\n"
    assert len(exact.encode()) == READ_LIMIT - 1
    assert _parse(exact).name == "mini"
    _refused(exact + "\n", "too_large", "(file)")  # one byte over


@pytest.mark.parametrize("threads", [" lead", "trail ", " both "])
def test_a_threads_value_with_leading_or_trailing_space_is_refused(threads: str) -> None:
    _refused(minimal().replace('threads = "a block"', f'threads = "{threads}"'), "bad_threads", "display.threads")


@pytest.mark.parametrize("summary", ["", "   ", "two\\nlines", "tab\\there", "nul\\u0000"])
def test_a_summary_that_is_not_one_nonblank_printable_line_is_refused(summary: str) -> None:
    _refused(minimal().replace('summary = "does a thing"', f'summary = "{summary}"'), "bad_summary", "summary")


@pytest.mark.parametrize("field", ["", "1x", "a b", "a.b", "a-b"])
def test_a_when_field_that_is_not_a_field_name_is_refused(field: str) -> None:
    _refused(minimal(extra=f'\n[when]\nfield = "{field}"\n'), "bad_field", "when.field")


@pytest.mark.parametrize("source", ["body.a.b", "body.a/b", "body.a*", "body.a b", "body.", "body./x"])
def test_a_body_field_with_a_dot_slash_star_or_space_is_refused(source: str) -> None:
    # body.a.b also kills a fullmatch -> match mutant: match would accept the prefix body.a.
    _refused(minimal(extra=f'\n[args]\nsha = "{source}"\n'), "bad_source", "args.sha")


def test_a_body_field_may_carry_a_hyphen_or_a_digit_first() -> None:
    aura = _parse(minimal(extra='\n[args]\nsha = "body.1-x"\n'))
    assert dict(aura.args) == {"sha": ArgSource("body", "1-x")}


@pytest.mark.parametrize("source", ['= "x" trailing', '= "x"y', 'lead = "x"', '= "x""'])
def test_a_literal_with_anything_after_its_closing_quote_is_refused(source: str) -> None:
    _refused(minimal(extra=f"\n[args]\nsha = '{source}'\n"), "bad_source", "args.sha")
