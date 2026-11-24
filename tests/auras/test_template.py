# SPDX-License-Identifier: AGPL-3.0-or-later
"""The ``{serves}`` aura template (RDR-252 post-close amendment, "Aura per base"; bead nexus-igea9).

One aura file triggers on ``board/ci/{serves}--owner``; the loader expands it into one aura per base in ``serves`` of
the group named in ``[load]``. Covered here: the grammar's accept and reject cases, the expansion, the loader for one
base (the nexus push-train case, unchanged) and for several, the notice texts that name the branch, and the
``nx aura check`` refusals.
"""
from __future__ import annotations

import logging
from datetime import datetime, timezone

import pytest
import structlog

from nexus import auras
from nexus.auras import AuraError, parse_aura
from nexus.auras.aura import expand_template
from nexus.auras.check import check_auras, check_serving_group
from nexus.auras.load_gate import GroupRecord, GroupUnreadable
from nexus.auras.loaded_state import AuraState
from nexus.auras.loader import AuraEntry
from nexus.auras.session import AuraLoader
from nexus.ci_base import branch_of
from nexus.mcp.subscriptions import SubscriptionSet

_SID = "11111111-2222-3333-4444-555555555555"
NOW = datetime(2026, 11, 24, 12, 0, tzinfo=timezone.utc)

TEMPLATE = '''\
aura = "tmpl"
summary = "a templated aura"
procedure = "ci_red_notice"

[load]
group = "push-train"

[trigger]
subspace = "board/ci/{serves}--owner"
match = { kind = "owner", from = "engine" }

[args]
sha = "dims.sha"
branch = "self.branch"
me = "self.identity"

[clear]
match = { kind = "clear", from = "engine" }

[display]
threads = "{branch} CI red ({base})"
'''


def _parse(text: str = TEMPLATE, name: str = "tmpl"):
    return parse_aura(text.encode(), name=name)


# -- the grammar --------------------------------------------------------------------------------------------------


def test_the_serves_placeholder_in_a_topic_is_a_template_and_not_a_literal_topic() -> None:
    aura = _parse()
    assert aura.is_template and aura.subspace == "board/ci/{serves}--owner" and aura.base is None
    concrete = TEMPLATE.replace("{serves}--owner", "nexus-develop--owner").replace("branch = \"self.branch\"\n", "")
    assert not _parse(concrete.replace("{branch} CI red ({base})", "develop CI red")).is_template


@pytest.mark.parametrize(
    "subspace", ["board/ci/{serves}", "board/ci/{serves}--owner", "board/{serves}", "board/ci/x-{serves}-y"],
)
def test_the_placeholder_is_accepted_once_inside_a_topic(subspace: str) -> None:
    assert _parse(TEMPLATE.replace("board/ci/{serves}--owner", subspace)).is_template


@pytest.mark.parametrize(
    "subspace",
    [
        "board/ci/{serves}{serves}--owner",      # twice
        "board/ci/{serve}--owner",               # a misspelling is not the placeholder
        "board/ci/{Serves}--owner",
        "board/ci/{other}--owner",               # any other brace use
        "board/ci/{serves}--{base}",
        "board/ci/{serves--owner",
        "board/ci/serves}--owner",
        "board/ci/{{serves}}--owner",
        "board/ci/{}--owner",
        "board/ci/x{y}",                         # braces with no placeholder
        "board/{serves}/x",                      # the placeholder is part of a topic, never a path segment of its own
        "board/ci/{serves}/x",
        "board/ci/{serves}*",
        "{serves}/ci/x",
        "board/ci",
    ],
)
def test_any_other_brace_use_is_refused(subspace: str) -> None:
    with pytest.raises(AuraError) as err:
        _parse(TEMPLATE.replace("board/ci/{serves}--owner", subspace))
    assert err.value.kind == "bad_subspace" and err.value.key == "trigger.subspace"


def test_a_template_needs_the_group_whose_serves_it_expands() -> None:
    with pytest.raises(AuraError) as err:
        _parse(TEMPLATE.replace('[load]\ngroup = "push-train"\n', ""))
    assert err.value.kind == "bad_subspace" and "[load] group" in err.value.detail


def test_a_template_threads_value_must_name_the_base_so_expansions_cannot_collide() -> None:
    with pytest.raises(AuraError) as err:
        _parse(TEMPLATE.replace('"{branch} CI red ({base})"', '"{branch} CI red"'))
    assert err.value.kind == "bad_threads" and "{base}" in err.value.detail
    assert _parse(TEMPLATE.replace('"{branch} CI red ({base})"', '"CI red {base}"')).is_template


@pytest.mark.parametrize("threads", ["CI {base} {other}", "CI {base", "CI {{base}}"])
def test_a_template_threads_value_takes_only_the_two_placeholders(threads: str) -> None:
    with pytest.raises(AuraError) as err:
        _parse(TEMPLATE.replace("{branch} CI red ({base})", threads))
    assert err.value.kind == "bad_threads"


def test_a_threads_placeholder_in_an_aura_with_no_serves_template_is_refused() -> None:
    plain = TEMPLATE.replace("{serves}--owner", "nexus-develop--owner").replace('branch = "self.branch"\n', "")
    with pytest.raises(AuraError) as err:
        _parse(plain)
    assert err.value.kind == "bad_threads"


def test_self_branch_is_a_source_only_a_template_can_fill() -> None:
    plain = TEMPLATE.replace("{serves}--owner", "nexus-develop--owner").replace("{branch} CI red ({base})", "develop CI red")
    with pytest.raises(AuraError) as err:
        _parse(plain)
    assert err.value.kind == "bad_source" and err.value.key == "args.branch"
    assert dict(_parse().args)["branch"].kind == "self"


# -- the branch of a base -----------------------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("base", "branch"),
    [("nexus-develop", "develop"), ("art-main", "main"), ("recording-rig-main", "main"), ("solo", "solo")],
)
def test_the_branch_is_the_part_after_the_last_hyphen(base: str, branch: str) -> None:
    assert branch_of(base) == branch


# -- the expansion ------------------------------------------------------------------------------------------------


def test_one_base_expands_to_one_aura_that_is_the_concrete_aura_it_replaces() -> None:
    template = _parse()
    [one], rejected = expand_template(template, ("nexus-develop",))
    assert rejected == []
    assert (one.name, one.subspace, one.base, one.expanded_from) == (
        "tmpl@nexus-develop", "board/ci/nexus-develop--owner", "nexus-develop", "tmpl")
    assert not one.is_template and one.load_group == "push-train"
    assert one.threads == "develop CI red (nexus-develop)"
    args = dict(one.args)
    assert (args["branch"].kind, args["branch"].value) == ("literal", "develop"), "the host fills self.branch from the base"
    assert (args["me"].kind, args["me"].value) == ("self", "identity") and args["sha"].kind == "dims"
    assert (one.match, one.clear_match, one.procedure, one.channel, one.limits) == (
        template.match, template.clear_match, template.procedure, template.channel, template.limits)


def test_n_bases_expand_to_n_auras_each_keyed_by_its_base() -> None:
    bases = ("nexus-develop", "art-main", "recording-rig-main")
    expansions, rejected = expand_template(_parse(), bases)
    assert rejected == []
    assert [a.name for a in expansions] == [f"tmpl@{b}" for b in bases]
    assert [a.subspace for a in expansions] == [f"board/ci/{b}--owner" for b in bases]
    assert [a.threads for a in expansions] == [
        "develop CI red (nexus-develop)", "main CI red (art-main)", "main CI red (recording-rig-main)"]
    assert len({a.threads for a in expansions}) == 3, "no two share a threads block"


def test_a_duplicate_base_expands_once_and_none_expands_to_nothing() -> None:
    assert [a.name for a in expand_template(_parse(), ("a-b", "a-b"))[0]] == ["tmpl@a-b"]
    assert expand_template(_parse(), ()) == ([], [])


def test_a_base_the_subspace_grammar_refuses_is_rejected_by_name_and_the_others_still_expand() -> None:
    expansions, rejected = expand_template(_parse(), ("ok-main", "has space-main", "a/b", "*"))
    assert [a.name for a in expansions] == ["tmpl@ok-main"]
    assert [b for b, _ in rejected] == ["has space-main", "a/b", "*"]


def test_an_expansion_whose_threads_value_is_over_the_limit_is_rejected() -> None:
    long = "x" * 70 + "-main"
    expansions, rejected = expand_template(_parse(), (long,))
    assert expansions == [] and [b for b, _ in rejected] == [long]


# -- the loader ---------------------------------------------------------------------------------------------------


@pytest.fixture(autouse=True)
def _log_everything() -> None:
    structlog.configure(wrapper_class=structlog.make_filtering_bound_logger(logging.DEBUG))


def _records(serves):
    return {
        "live": GroupRecord("push-train", serves=tuple(serves)),
        "none": None,
        "ended": GroupRecord("push-train", serves=tuple(serves), ended_at="2026-11-24T10:00:00Z"),
        "unreadable": GroupUnreadable("down"),
    }


class _Loader:
    def __init__(self, *, serves=("nexus-develop",), identity="i7", memberships=("push-train",), group="live", extra=()):
        self.subs = SubscriptionSet(session_id=_SID)
        self.attached: list = []
        self.states: list = []
        self.reads: list[str] = []
        records = _records(serves)

        def read_group(name):
            self.reads.append(name)
            found = records[group]
            if isinstance(found, Exception):
                raise found
            return found

        self.loader = AuraLoader(
            _SID, self.subs, discover=lambda: [_parse(), *extra], is_on=lambda _n: True,
            entry=lambda _sid: ("tamsin", 100), registered_owner=lambda: 100, make_runtime=lambda loaded: loaded,
            attach=self.attached.append, identity=lambda: identity, memberships=lambda _who: list(memberships),
            read_group=read_group, now=lambda: NOW, state_sink=lambda pid, state: self.states.append((pid, state)),
        )
        self.loader.on_resolve("tamsin")

    @property
    def loaded(self) -> list[str]:
        return [a.name for batch in self.attached for a in batch]


def test_the_nexus_push_train_group_serving_one_base_loads_the_one_aura_it_had_before() -> None:
    h = _Loader()
    assert h.loaded == ["tmpl@nexus-develop"]
    assert h.subs.aura_topics() == ["board/ci/nexus-develop--owner"]
    assert h.loader.decisions == {"tmpl@nexus-develop": AuraState(True, "loaded", "push-train")}
    assert h.reads == ["push-train"], "one group read"


def test_a_group_serving_n_bases_loads_one_aura_per_base_with_one_group_read() -> None:
    bases = ("nexus-develop", "art-main", "recording-rig-main")
    h = _Loader(serves=bases)
    assert sorted(h.loaded) == sorted(f"tmpl@{b}" for b in bases)
    assert sorted(h.subs.aura_topics()) == sorted(f"board/ci/{b}--owner" for b in bases)
    assert {n: s.loaded for n, s in h.loader.decisions.items()} == {f"tmpl@{b}": True for b in bases}
    assert h.reads == ["push-train"]
    assert sorted(h.states[-1][1].auras) == sorted(h.loader.decisions)


def test_a_group_that_serves_nothing_loads_nothing_and_says_so_under_the_template_name() -> None:
    h = _Loader(serves=())
    assert h.loaded == [] and h.subs.aura_topics() == []
    assert h.loader.decisions == {"tmpl": AuraState(False, "serves_nothing", "push-train")}


def test_a_base_the_grammar_refuses_is_recorded_not_loaded_and_the_rest_load() -> None:
    h = _Loader(serves=("ok-main", "bad base-main"))
    assert h.loaded == ["tmpl@ok-main"]
    assert h.loader.decisions["tmpl@bad base-main"] == AuraState(False, "bad_base", "push-train")


@pytest.mark.parametrize(
    ("kwargs", "reason"),
    [
        ({"memberships": ("other",)}, "not_a_member"),
        ({"group": "none"}, "no_group_record"),
        ({"group": "ended"}, "group_disbanded"),
        ({"group": "unreadable"}, "group_unreadable"),
    ],
)
def test_a_template_the_gate_refuses_is_recorded_under_its_own_name(kwargs: dict, reason: str) -> None:
    h = _Loader(**kwargs)
    assert h.loaded == [] and h.loader.decisions["tmpl"] == AuraState(False, reason, "push-train")


def test_a_server_that_loaded_unbound_expands_the_template_when_the_lease_binds() -> None:
    bound: list[str | None] = [None]
    subs = SubscriptionSet(session_id=_SID)
    runtimes: list = []

    class _Rt:
        def __init__(self, loaded) -> None:
            self.names = [a.name for a in loaded]

        def extend(self, loaded, identity) -> None:
            self.names += [a.name for a in loaded]

    def make(loaded):
        runtimes.append(_Rt(loaded))
        return runtimes[-1]

    loader = AuraLoader(
        _SID, subs, discover=lambda: [_parse()], is_on=lambda _n: True, entry=lambda _sid: ("tamsin", 100),
        registered_owner=lambda: 100, make_runtime=make, attach=lambda _rt: None, identity=lambda: bound[0],
        memberships=lambda _who: ["push-train"],
        read_group=lambda n: GroupRecord(n, serves=("nexus-develop", "art-main")), now=lambda: NOW,
    )
    loader.on_resolve("tamsin")
    assert loader.decisions == {"tmpl": AuraState(False, "unbound", "push-train")}
    loader.note_identity("i7")
    assert sorted(loader.decisions) == ["tmpl@art-main", "tmpl@nexus-develop"], "the stale unbound row for the template is gone"
    assert all(s.loaded for s in loader.decisions.values())
    assert sorted(runtimes[0].names) == ["tmpl@art-main", "tmpl@nexus-develop"]
    assert sorted(subs.aura_topics()) == ["board/ci/art-main--owner", "board/ci/nexus-develop--owner"]


def test_two_templates_cannot_both_write_one_base_threads_block() -> None:
    twin = _parse(TEMPLATE.replace('aura = "tmpl"', 'aura = "twin"'), name="twin")
    h = _Loader(extra=[twin])
    assert h.loaded == ["tmpl@nexus-develop"]
    assert h.loader.decisions["twin@nexus-develop"].reason == "refused_threads"


# -- the notice names the branch ----------------------------------------------------------------------------------


def _notice(**args: object) -> dict:
    from tests.auras.test_ci_develop_red import SHA, _run  # noqa: PLC0415

    base = {"sha": SHA, "refresh": False, "reason": "earliest_suspect", "me": "i7", "owner": "i7"}
    return _run(**{**base, **args})


def test_the_red_notice_names_the_branch_it_is_told() -> None:
    assert _notice(branch="main")["text"].startswith("Main is red at 8b30c16")
    assert _notice(branch="develop")["text"].startswith("Develop is red at 8b30c16")
    assert _notice(branch="release.2")["text"].startswith("Release.2 is red at")


def test_the_red_notice_with_no_branch_reads_as_it_did_before_the_template() -> None:
    text = _notice()["text"]
    assert text == _notice(branch="develop")["text"] and text.startswith("Develop is red at")


@pytest.mark.parametrize("branch", ["a b", "x\nneeds_sam: yes", "", "-x", "y" * 33])
def test_a_branch_that_breaks_its_grammar_is_refused_and_never_echoed(branch: str) -> None:
    import lupa.lua54 as lua54  # noqa: PLC0415

    with pytest.raises(lua54.LuaError, match="branch must be") as err:
        _notice(branch=branch)
    assert "needs_sam" not in str(err.value)


def test_the_escalated_and_handed_off_notices_name_the_branch_too() -> None:
    stalled = _notice(branch="main", reason="escalated", stalled=True, open_minutes=40)
    assert stalled["text"].startswith("Main is red at") and "Develop" not in stalled["text"]
    other = _notice(branch="main", reason="handoff", owner="i9", me="i7", next="i7")
    assert "Main is red" in other["text"] and "Develop" not in other["text"]
    ownerless = _notice(branch="main", owner=None, me="i7")
    assert "Main is red" in ownerless["text"] and "Develop" not in ownerless["text"]


def test_the_nudge_names_the_branch() -> None:
    from tests.auras.test_ci_red_nudge import SHA, _run  # noqa: PLC0415

    args = {"sha": SHA, "next": "i7", "me": "i7"}
    assert _run(**args, branch="main")["text"].startswith("Main is red at")
    assert _run(**args)["text"].startswith("Develop is red at")


def test_the_shipped_auras_are_templates_whose_expansion_fills_the_branch() -> None:
    for name in ("ci_develop_red", "ci_develop_nudge"):
        shipped = auras.load_aura(name, cwd=None)
        assert shipped.is_template and shipped.subspace == "board/ci/{serves}--owner" and shipped.load_group == "push-train"
        [one], _ = expand_template(shipped, ("nexus-develop",))
        assert one.subspace == "board/ci/nexus-develop--owner" and dict(one.args)["branch"].value == "develop", name
        [art], _ = expand_template(shipped, ("art-main",))
        assert dict(art.args)["branch"].value == "main" and art.threads.startswith("main CI "), name
        assert art.threads.endswith("(art-main)")


# -- nx aura check ------------------------------------------------------------------------------------------------


def _check(reader, *, templates=None, text: str = TEMPLATE):
    [result] = check_auras([AuraEntry("tmpl", "shipped", _parse(text))], cwd=None, templates=templates, read_group=reader, now=NOW)
    return result


def test_a_template_whose_group_serves_bases_is_clean_of_serving_findings() -> None:
    result = _check(lambda n: GroupRecord(n, serves=("nexus-develop", "art-main")))
    assert not [f for f in result.findings if f.check == "serving_group"] and not result.warnings, (result.findings, result.warnings)


def test_a_template_whose_group_serves_nothing_is_refused() -> None:
    result = _check(lambda n: GroupRecord(n, serves=()))
    assert [(f.check, f.key) for f in result.findings if f.check == "serving_group"] == [("serving_group", "load.group")]
    assert "serves nothing" in result.findings[-1].detail


@pytest.mark.parametrize(
    ("record", "words"),
    [(None, "no record"), (GroupRecord("push-train", serves=("a-b",), ended_at="2026-11-24T10:00:00Z"), "disbanded")],
)
def test_a_template_whose_group_has_no_live_record_is_refused(record, words: str) -> None:
    result = _check(lambda _n: record)
    serving = [f for f in result.findings if f.check == "serving_group"]
    assert [f.key for f in serving] == ["load.group"] and words in serving[0].detail


def test_a_group_that_cannot_be_read_skips_the_serving_group_check_by_name_and_never_passes_it() -> None:
    def down(_n):
        raise GroupUnreadable("engine is down")

    skipped = _check(down).skipped
    assert "serving_group" in {s.check for s in skipped}
    assert "engine is down" in next(s.reason for s in skipped if s.check == "serving_group")
    assert "serving_group" in {s.check for s in _check(None).skipped}


def test_a_template_is_validated_against_every_base_the_group_serves() -> None:
    """A base whose expansion the grammar refuses is a finding naming that base, and only that base."""
    result = _check(lambda n: GroupRecord(n, serves=("nexus-develop", "bad base-main", "art-main")))
    serving = [f for f in result.findings if f.check == "serving_group"]
    assert len(serving) == 1 and "bad base-main" in serving[0].detail and "nexus-develop" not in serving[0].detail


def test_the_dictionary_check_runs_for_each_base_and_names_it() -> None:
    template = {"name": "board/ci/<topic>", "dimensions": {"kind": {}, "sha": {}}, "doc": {"body": {"x": "..."}}}
    result = _check(lambda n: GroupRecord(n, serves=("nexus-develop", "art-main")), templates=[template])
    hits = [f for f in result.findings if f.key == "trigger.match.from"]
    assert len(hits) == 2, result.findings
    assert {f.detail.split("]")[0] for f in hits} == {"[nexus-develop", "[art-main"}


def test_the_dictionary_check_of_a_template_the_group_cannot_expand_uses_a_sample_base_and_still_runs() -> None:
    template = {"name": "board/ci/<topic>", "dimensions": {"kind": {}, "sha": {}}, "doc": {"body": {"x": "..."}}}
    result = _check(None, templates=[template])
    assert [f.key for f in result.findings if f.check == "dictionary"].count("trigger.match.from") == 1


def test_the_serving_group_warning_does_not_apply_to_the_template_itself() -> None:
    assert check_serving_group(_parse(), lambda n: GroupRecord(n, serves=("other-main",)), now=NOW) == ([], [])


# -- nx aura list -------------------------------------------------------------------------------------------------


def test_nx_aura_list_reads_a_templates_expansions_out_of_the_state_file() -> None:
    from nexus.commands.aura_cmd import _loaded_text, _template_load  # noqa: PLC0415

    state = {
        "tmpl@art-main": AuraState(True, "loaded", "push-train"),
        "tmpl@nexus-develop": AuraState(True, "loaded", "push-train"),
        "tmpl@bad base-main": AuraState(False, "bad_base", "push-train"),
        "tmplx@other-main": AuraState(True, "loaded", "push-train"),
    }
    seen, bases = _template_load(state, "tmpl")
    assert seen is not None and seen.loaded and bases == ["art-main", "nexus-develop"], "the sibling tmplx is not tmpl's"
    text = _loaded_text({"loaded": True, "reason": "loaded", "group": "push-train", "bases": bases})
    assert text == "  loaded for art-main, nexus-develop (group push-train)"
    refused, none = _template_load({"tmpl@x-main": AuraState(False, "bad_base", "push-train")}, "tmpl")
    assert (refused, none) == (AuraState(False, "bad_base", "push-train"), [])
    assert _template_load({}, "tmpl") == (None, [])
