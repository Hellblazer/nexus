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

[trigger]
subspace = "board/ci/{serves}--owner"
match = { kind = "owner", from = "engine" }

[args]
sha = "dims.sha"
branch = "self.branch"
refresh = "body.refresh"
reason = "body.reason"
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


def test_a_template_takes_no_load_group_because_it_expands_over_every_group_the_session_is_in() -> None:
    with pytest.raises(AuraError) as err:
        _parse(TEMPLATE.replace("[trigger]", '[load]\ngroup = "push-train"\n\n[trigger]', 1))
    assert err.value.kind == "bad_group" and err.value.key == "load.group"
    assert _parse().load_group is None


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
    [one], rejected = expand_template(template, ("nexus-develop",), group="push-train")
    assert rejected == []
    assert (one.name, one.subspace, one.base, one.expanded_from) == (
        "tmpl@nexus-develop", "board/ci/nexus-develop--owner", "nexus-develop", "tmpl")
    assert not one.is_template and one.load_group == "push-train", "each expansion carries the group that serves its base"
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


@pytest.mark.parametrize("base", ["x-" + "y" * 33, "repo-", "repo-.x"])
def test_a_base_whose_branch_the_procedure_would_refuse_is_rejected_at_expansion_not_at_the_first_red(base: str) -> None:
    """The Lua's own rule for ``branch``: a letter or digit first, then letters, digits, dot, dash and underscore, at most 32."""
    expansions, rejected = expand_template(_parse(), (base,))
    assert expansions == [] and [b for b, _ in rejected] == [base]
    assert "branch" in rejected[0][1]


def test_a_branch_nothing_maps_is_not_checked_so_a_template_without_self_branch_takes_any_base() -> None:
    plain = _parse(TEMPLATE.replace('branch = "self.branch"\n', "").replace('"{branch} CI red ({base})"', '"CI {base}"'))
    expansions, rejected = expand_template(plain, ("repo-" + "y" * 40,))
    assert rejected == [] and len(expansions) == 1


def test_an_expansion_whose_threads_value_is_over_the_limit_is_rejected() -> None:
    long = "x" * 70 + "-main"
    expansions, rejected = expand_template(_parse(), (long,))
    assert expansions == [] and [b for b, _ in rejected] == [long]


# -- the loader ---------------------------------------------------------------------------------------------------


@pytest.fixture(autouse=True)
def _log_everything() -> None:
    structlog.configure(wrapper_class=structlog.make_filtering_bound_logger(logging.DEBUG))


#: The groups the engine knows, as RDR-252:344 has them: a topic is served by at most one live group.
def _groups(**over):
    base = {
        "push-train": GroupRecord("push-train", serves=("nexus-develop",)),
        "recording-rig-train": GroupRecord("recording-rig-train", serves=("recording-rig-main",)),
        "reviewers": GroupRecord("reviewers", serves=()),
        "ended-train": GroupRecord("ended-train", serves=("old-main",), ended_at="2026-11-24T10:00:00Z"),
        "down-train": GroupUnreadable("down"),
    }
    base.update(over)
    return base


class _Loader:
    def __init__(self, *, memberships=("push-train",), identity="i7", groups=None, extra=(), memberships_error=None):
        self.subs = SubscriptionSet(session_id=_SID)
        self.attached: list = []
        self.states: list = []
        self.reads: list[str] = []
        self.membership_reads: list[str] = []
        records = _groups() if groups is None else groups

        def read_group(name):
            self.reads.append(name)
            found = records.get(name)
            if isinstance(found, Exception):
                raise found
            return found

        def read_memberships(who):
            self.membership_reads.append(who)
            if memberships_error is not None:
                raise memberships_error
            return list(memberships)

        self.loader = AuraLoader(
            _SID, self.subs, discover=lambda: [_parse(), *extra], is_on=lambda _n: True,
            entry=lambda _sid: ("tamsin", 100), registered_owner=lambda: 100, make_runtime=lambda loaded: loaded,
            attach=self.attached.append, identity=lambda: identity, memberships=read_memberships,
            read_group=read_group, now=lambda: NOW, state_sink=lambda pid, state: self.states.append((pid, state)),
        )
        self.loader.on_resolve("tamsin")

    @property
    def loaded(self) -> list[str]:
        return sorted(a.name for batch in self.attached for a in batch)


def test_the_nexus_push_train_group_serving_one_base_loads_the_one_aura_it_had_before() -> None:
    h = _Loader()
    assert h.loaded == ["tmpl@nexus-develop"]
    assert h.subs.aura_topics() == ["board/ci/nexus-develop--owner"]
    assert h.loader.decisions == {"tmpl@nexus-develop": AuraState(True, "loaded", "push-train")}
    assert h.reads == ["push-train"] and h.membership_reads == ["i7"], "one membership read, one group read"


def test_a_session_only_in_a_repos_own_group_gets_that_repos_aura_and_nothing_for_nexus() -> None:
    h = _Loader(memberships=("recording-rig-train",))
    assert h.loaded == ["tmpl@recording-rig-main"]
    assert h.subs.aura_topics() == ["board/ci/recording-rig-main--owner"]
    assert h.loader.decisions == {"tmpl@recording-rig-main": AuraState(True, "loaded", "recording-rig-train")}
    assert h.reads == ["recording-rig-train"], "the group the session is not in is never read"


def test_a_session_in_two_groups_gets_each_groups_bases_with_that_groups_verdict() -> None:
    h = _Loader(memberships=("push-train", "recording-rig-train"))
    assert h.loaded == ["tmpl@nexus-develop", "tmpl@recording-rig-main"]
    assert h.loader.decisions == {
        "tmpl@nexus-develop": AuraState(True, "loaded", "push-train"),
        "tmpl@recording-rig-main": AuraState(True, "loaded", "recording-rig-train"),
    }
    assert sorted(h.reads) == ["push-train", "recording-rig-train"] and h.membership_reads == ["i7"]
    assert sorted(a.threads for batch in h.attached for a in batch) == [
        "develop CI red (nexus-develop)", "main CI red (recording-rig-main)"]


def test_a_group_serving_n_bases_loads_one_aura_per_base() -> None:
    bases = ("nexus-develop", "art-main", "recording-rig-main")
    h = _Loader(groups=_groups(**{"push-train": GroupRecord("push-train", serves=bases)}))
    assert h.loaded == sorted(f"tmpl@{b}" for b in bases)
    assert sorted(h.subs.aura_topics()) == sorted(f"board/ci/{b}--owner" for b in bases)
    assert {n: s.group for n, s in h.loader.decisions.items()} == {f"tmpl@{b}": "push-train" for b in bases}
    assert sorted(h.states[-1][1].auras) == sorted(h.loader.decisions), "the state file names each base and its group"


def test_a_base_two_groups_both_serve_is_expanded_once() -> None:
    both = _groups(**{"recording-rig-train": GroupRecord("recording-rig-train", serves=("nexus-develop", "recording-rig-main"))})
    h = _Loader(memberships=("push-train", "recording-rig-train"), groups=both)
    assert h.loaded == ["tmpl@nexus-develop", "tmpl@recording-rig-main"]


@pytest.mark.parametrize(
    ("kwargs", "reason"),
    [
        ({"memberships": ()}, "not_a_member"),
        ({"memberships": ("reviewers",)}, "serves_nothing"),
        ({"memberships": ("ended-train",)}, "serves_nothing"),
        ({"memberships": ("nobody-train",)}, "serves_nothing"),
        ({"memberships": ("down-train",)}, "group_unreadable"),
        ({"memberships": ("push-train",), "groups": _groups(**{"push-train": GroupRecord("push-train", serves=())})}, "serves_nothing"),
        ({"identity": None}, "unbound"),
        ({"memberships_error": GroupUnreadable("down")}, "memberships_unreadable"),
    ],
)
def test_a_template_with_no_serving_group_loads_nothing_and_is_recorded_under_its_own_name(kwargs: dict, reason: str) -> None:
    h = _Loader(**kwargs)
    assert h.loaded == [] and h.subs.aura_topics() == []
    assert h.loader.decisions == {"tmpl": AuraState(False, reason, None)}


def test_a_base_the_grammar_refuses_is_recorded_not_loaded_and_the_rest_load() -> None:
    h = _Loader(groups=_groups(**{"push-train": GroupRecord("push-train", serves=("ok-main", "bad base-main"))}))
    assert h.loaded == ["tmpl@ok-main"]
    assert h.loader.decisions["tmpl@bad base-main"] == AuraState(False, "bad_base", "push-train")


def test_a_server_that_loaded_unbound_expands_the_template_when_the_lease_binds() -> None:
    bound: list[str | None] = [None]
    subs = SubscriptionSet(session_id=_SID)
    runtimes: list = []
    records = _groups()

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
        memberships=lambda _who: ["push-train", "recording-rig-train"], read_group=records.get, now=lambda: NOW,
    )
    loader.on_resolve("tamsin")
    assert loader.decisions == {"tmpl": AuraState(False, "unbound", None)}
    loader.note_identity("i7")
    assert sorted(loader.decisions) == ["tmpl@nexus-develop", "tmpl@recording-rig-main"], "the stale unbound row is gone"
    assert all(s.loaded for s in loader.decisions.values())
    assert sorted(runtimes[0].names) == ["tmpl@nexus-develop", "tmpl@recording-rig-main"]
    assert sorted(subs.aura_topics()) == ["board/ci/nexus-develop--owner", "board/ci/recording-rig-main--owner"]


def test_two_templates_cannot_both_write_one_base_threads_block() -> None:
    twin = _parse(TEMPLATE.replace('aura = "tmpl"', 'aura = "twin"'), name="twin")
    h = _Loader(extra=[twin])
    assert h.loaded == ["tmpl@nexus-develop"]
    assert h.loader.decisions["twin@nexus-develop"].reason == "refused_threads"


# -- the pre-template blocks --------------------------------------------------------------------------------------


class _RetiringRuntime:
    """A runtime that records the blocks the loader asks it to retire."""

    def __init__(self, loaded) -> None:
        self.retired: list[tuple[str, str]] = []

    def retire_blocks(self, pairs) -> None:
        self.retired += list(pairs)


def _shipped_loader(*, memberships=("push-train",)):
    runtimes: list[_RetiringRuntime] = []
    shipped = [auras.load_aura(n, cwd=None) for n in ("ci_develop_red", "ci_develop_nudge")]

    def make(loaded):
        runtimes.append(_RetiringRuntime(loaded))
        return runtimes[-1]

    loader = AuraLoader(
        _SID, SubscriptionSet(session_id=_SID), discover=lambda: shipped, is_on=lambda _n: True,
        entry=lambda _sid: ("tamsin", 100), registered_owner=lambda: 100, make_runtime=make, attach=lambda _rt: None,
        identity=lambda: "i7", memberships=lambda _who: list(memberships), read_group=_groups().get, now=lambda: NOW,
    )
    loader.on_resolve("tamsin")
    return loader, runtimes


def test_the_first_expansion_retires_the_pre_template_blocks_of_the_two_shipped_auras() -> None:
    _loader, [rt] = _shipped_loader()
    assert sorted(rt.retired) == [("ci_develop_nudge", "develop CI nudge"), ("ci_develop_red", "develop CI red")]


def test_a_session_whose_template_expanded_to_nothing_retires_nothing() -> None:
    loader, runtimes = _shipped_loader(memberships=())
    assert runtimes == [] and loader.decisions["ci_develop_red"].reason == "not_a_member"


def test_the_threads_writer_removes_the_old_heading_and_leaves_the_new_one() -> None:
    from nexus.auras.threads_block import ThreadsBlockWriter  # noqa: PLC0415

    entry = {
        "title": "s", "tags": "",
        "content": f"session: {_SID}\n\n## Work\nref: x\n\n## aura: develop CI red\nstate: old\n\n"
        "## aura: develop CI red (nexus-develop)\nstate: new\n",
    }
    store = [dict(entry)]
    writer = ThreadsBlockWriter(
        _SID, read_entries=lambda: list(store),
        put=lambda title, content, tags: store.__setitem__(0, {"title": title, "content": content, "tags": tags}),
    )
    assert writer.remove_block("ci_develop_red", "develop CI red") is True
    text = store[0]["content"]
    assert "## aura: develop CI red\n" not in text and "state: old" not in text
    assert "## aura: develop CI red (nexus-develop)\nstate: new" in text and "## Work" in text
    assert writer.remove_block("ci_develop_red", "develop CI red") is False, "idempotent: a second removal finds nothing"


def test_the_runtime_retires_blocks_through_its_writer_off_the_callers_thread() -> None:
    from nexus.auras.runtime import AuraRuntime  # noqa: PLC0415

    removed: list[tuple[str, str]] = []

    class _Writer:
        def set_block(self, *a) -> bool:
            return True

        def remove_block(self, aura: str, threads_value: str) -> bool:
            removed.append((aura, threads_value))
            return True

    rt = AuraRuntime([], writer=_Writer(), run=lambda *a, **k: {}, identity="i7")
    rt.retire_blocks([("ci_develop_red", "develop CI red")])
    assert rt.wait_idle(10) and removed == [("ci_develop_red", "develop CI red")]


def test_a_failing_retirement_is_logged_and_never_raised() -> None:
    from nexus.auras.runtime import AuraRuntime  # noqa: PLC0415

    class _Writer:
        def set_block(self, *a) -> bool:
            return True

        def remove_block(self, aura: str, threads_value: str) -> bool:
            raise RuntimeError("t2 down")

    rt = AuraRuntime([], writer=_Writer(), run=lambda *a, **k: {}, identity="i7")
    rt.retire_blocks([("a", "b")])
    assert rt.wait_idle(10)


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
        assert shipped.is_template and shipped.subspace == "board/ci/{serves}--owner" and shipped.load_group is None
        [one], _ = expand_template(shipped, ("nexus-develop",))
        assert one.subspace == "board/ci/nexus-develop--owner" and dict(one.args)["branch"].value == "develop", name
        [art], _ = expand_template(shipped, ("art-main",))
        assert dict(art.args)["branch"].value == "main" and art.threads.startswith("main CI "), name
        assert art.threads.endswith("(art-main)")


@pytest.mark.parametrize("branch", ["a b", "x\nneeds_sam: yes", "", "-x", "y" * 33])
def test_the_nudge_refuses_a_branch_that_breaks_its_grammar_and_never_echoes_it(branch: str) -> None:
    import lupa.lua54 as lua54  # noqa: PLC0415

    from tests.auras.test_ci_red_nudge import SHA, _run  # noqa: PLC0415

    with pytest.raises(lua54.LuaError, match="branch must be") as err:
        _run(sha=SHA, next="i7", me="i7", branch=branch)
    assert "needs_sam" not in str(err.value)


def test_a_self_source_the_runtime_does_not_fill_is_a_failure_not_the_identity() -> None:
    """``self.branch`` is a template's and expansion rewrites it to a literal; a copy that reached the runtime unexpanded
    must not be handed the session's identity in its place."""
    from nexus.auras.aura import ArgSource  # noqa: PLC0415
    from nexus.auras.runtime import _arg, _Failure  # noqa: PLC0415

    assert _arg(ArgSource("self", "identity"), None, "me", "i7") == "i7"
    with pytest.raises(_Failure, match="self.branch"):
        _arg(ArgSource("self", "branch"), None, "branch", "i7")


# -- nx aura check ------------------------------------------------------------------------------------------------


def _check(read_group, member_groups, *, templates=None, text: str = TEMPLATE):
    [result] = check_auras(
        [AuraEntry("tmpl", "shipped", _parse(text))], cwd=None, templates=templates, read_group=read_group,
        member_groups=member_groups, now=NOW,
    )
    return result


def _serving(result):
    return [x for x in (*result.findings, *result.warnings) if x.check == "serving_group"]


def _reader(**over):
    records = _groups(**over)

    def read(name):
        found = records.get(name)
        if isinstance(found, Exception):
            raise found
        return found

    return read


def test_a_template_checked_in_a_session_that_belongs_to_serving_groups_is_clean() -> None:
    result = _check(_reader(), lambda: ["push-train", "recording-rig-train"])
    assert result.ok and not result.warnings and not _serving(result), (result.findings, result.warnings)


@pytest.mark.parametrize(
    ("groups", "words"),
    [
        ([], "belongs to no group"),
        (["reviewers"], "serves nothing"),
        (["ended-train"], "disbanded"),
        (["nobody-train"], "no record"),
    ],
)
def test_group_state_that_leaves_a_template_with_no_base_is_a_warning_never_a_finding(groups: list[str], words: str) -> None:
    """The concrete aura only warned here, and the shipped auras must not turn a release's ``nx aura check`` red because the
    session is in no serving group (the first review's ship-blocker)."""
    result = _check(_reader(), lambda: groups)
    assert result.ok and not result.findings, result.findings
    [warning] = _serving(result)
    assert warning.key == "load.group" and words in warning.detail and "the template expands to no aura" in warning.detail


def test_a_serving_group_among_the_sessions_groups_is_enough_for_no_warning() -> None:
    result = _check(_reader(), lambda: ["push-train", "reviewers"])
    assert result.ok and not result.warnings


def test_a_base_whose_expansion_is_rejected_is_the_one_finding_and_names_it() -> None:
    groups = {"push-train": GroupRecord("push-train", serves=("nexus-develop", "bad base-main", "art-main"))}
    result = _check(groups.get, lambda: ["push-train"])
    serving = [f for f in result.findings if f.check == "serving_group"]
    assert len(serving) == 1 and "bad base-main" in serving[0].detail and "nexus-develop" not in serving[0].detail
    assert not result.ok


def test_a_group_that_cannot_be_read_skips_the_serving_group_check_by_name_and_never_passes_it() -> None:
    def down():
        raise GroupUnreadable("no identity here")

    skipped = _check(_reader(), down).skipped
    assert "no identity here" in next(s.reason for s in skipped if s.check == "serving_group")
    assert "serving_group" in {s.check for s in _check(None, None).skipped}
    assert "serving_group" in {s.check for s in _check(_reader(), lambda: ["down-train"]).skipped}


def test_the_dictionary_check_runs_for_each_base_of_each_group_and_names_it() -> None:
    template = {"name": "board/ci/<topic>", "dimensions": {"kind": {}, "sha": {}}, "doc": {"body": {"x": "..."}}}
    result = _check(_reader(), lambda: ["push-train", "recording-rig-train"], templates=[template])
    hits = [f for f in result.findings if f.key == "trigger.match.from"]
    assert {f.detail.split("]")[0] for f in hits} == {"[nexus-develop", "[recording-rig-main"} and len(hits) == 2


def test_the_dictionary_check_of_a_template_with_no_known_bases_uses_a_sample_base_and_still_runs() -> None:
    template = {"name": "board/ci/<topic>", "dimensions": {"kind": {}, "sha": {}}, "doc": {"body": {"x": "..."}}}
    result = _check(None, None, templates=[template])
    assert [f.key for f in result.findings if f.check == "dictionary"].count("trigger.match.from") == 1


def test_a_template_whose_sample_expansion_is_rejected_is_a_finding_naming_threads_not_an_index_error() -> None:
    long = TEMPLATE.replace('"{branch} CI red ({base})"', '"{base} ' + "x" * 78 + '"')
    template = {"name": "board/ci/<topic>", "dimensions": {"kind": {}, "sha": {}, "from": {}}, "doc": {"body": {"x": "..."}}}
    result = _check(None, None, templates=[template], text=long)
    assert [(f.check, f.key) for f in result.findings if f.key == "display.threads"] == [("threads", "display.threads")]


def test_the_serving_group_warning_does_not_apply_to_the_template_itself() -> None:
    assert check_serving_group(_parse(), lambda n: GroupRecord(n, serves=("other-main",)), now=NOW) == ([], [])


# -- nx aura list -------------------------------------------------------------------------------------------------


def test_nx_aura_list_through_the_command_shows_each_base_and_its_group(
    tmp_path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A state file holding ``tmpl@<base>`` rows, read by the real ``nx aura list`` (a mutant that dropped the template
    lookup in the command left every earlier test green)."""
    import json  # noqa: PLC0415

    from click.testing import CliRunner  # noqa: PLC0415

    from nexus.auras import loaded_state as ls  # noqa: PLC0415
    from nexus.commands import aura_cmd  # noqa: PLC0415
    from nexus.commands.aura_cmd import aura_group  # noqa: PLC0415
    from nexus.lua_runtime import library  # noqa: PLC0415
    from tests.identity.fakes import HOST, FakeProcesses  # noqa: PLC0415

    root = tmp_path / "shipped"
    root.mkdir()
    (root / "tmpl.aura.toml").write_text(TEMPLATE, encoding="utf-8")
    cfg = tmp_path / "cfg"
    cfg.mkdir()
    (cfg / "config.yml").write_text("auras:\n  tmpl: on\n", encoding="utf-8")
    monkeypatch.setattr(library, "shipped_root", lambda: root)
    monkeypatch.delenv(library.REPO_ROOT_ENV, raising=False)
    monkeypatch.delenv("NX_AURAS_OFF", raising=False)
    monkeypatch.setenv("NEXUS_CONFIG_DIR", str(cfg))
    monkeypatch.chdir(tmp_path)
    procs = FakeProcesses(tmp_path / "sessions")
    procs.start(4242, "2026-11-17T08:00:00Z", started_at=4242000)
    adir = tmp_path / "auras"
    state = ls.LoadedState(
        server_pid=9000, host=HOST, pid=4242, started_at=4242000, proc_start="2026-11-17T08:00:00Z", session_id="sid",
        identity="id-a", loaded_identity="id-a",
        auras={
            "tmpl@nexus-develop": ls.AuraState(True, "loaded", "push-train"),
            "tmpl@recording-rig-main": ls.AuraState(True, "loaded", "recording-rig-train"),
            "tmpl@bad base-main": ls.AuraState(False, "bad_base", "push-train"),
        },
    )
    ls.write_state(ls.state_path(adir, 4242, 4242000), state)
    monkeypatch.setattr(aura_cmd, "_loaded_inputs", lambda: (adir, procs, 4242, "id-a", None))
    res = CliRunner().invoke(aura_group, ["list"])
    assert res.exit_code == 0, res.output
    row = next(ln for ln in res.output.splitlines() if ln.startswith("tmpl"))
    assert "loaded for nexus-develop (push-train), recording-rig-main (recording-rig-train)" in row, row
    assert "not loaded" not in row
    as_json = json.loads(CliRunner().invoke(aura_group, ["list", "--json"]).output)
    [entry] = [r for r in as_json if r["name"] == "tmpl"]
    assert entry["loaded"]["loaded"] is True and entry["loaded"]["bases"] == [
        {"base": "nexus-develop", "group": "push-train"}, {"base": "recording-rig-main", "group": "recording-rig-train"}]


def test_nx_aura_list_says_why_when_no_base_of_a_template_loaded() -> None:
    from nexus.commands.aura_cmd import _loaded_text, _template_state  # noqa: PLC0415

    state = {"tmpl@x-main": AuraState(False, "bad_base", "push-train")}
    assert _template_state(state, "tmpl") == {"loaded": False, "reason": "bad_base", "group": "push-train"}
    assert _template_state({"tmpl": AuraState(False, "not_a_member", None)}, "other") is None
    assert _loaded_text({"loaded": False, "reason": "serves_nothing", "group": None}) == "  not loaded: serves_nothing"
