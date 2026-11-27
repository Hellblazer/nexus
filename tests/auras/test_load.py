# SPDX-License-Identifier: AGPL-3.0-or-later
"""Loading auras from the two library roots (RDR-249 Technical Design 1; bead nexus-mdbds.10.1).

The loader reuses RDR-242's library: the roots, the name rule, the shadow refusal and the
repo-root switch are ``nexus.lua_runtime.library``'s, reached through a suffix, never copied.
Both roots are tmp dirs here; nothing writes into ``src/nexus/procedures/``.
"""
from __future__ import annotations

import os
import shutil
import subprocess
from pathlib import Path

import pytest

from nexus import auras
from nexus.auras import AuraError
from nexus.lua_runtime import library
from nexus.lua_runtime.library import LibraryError

from ._support import TD1_EXAMPLE, minimal

needs_git = pytest.mark.skipif(shutil.which("git") is None, reason="git is a hard dependency of nx")


def _write(root: Path, name: str, text: str | None = None) -> Path:
    root.mkdir(parents=True, exist_ok=True)
    path = root / f"{name}.aura.toml"
    path.write_text(minimal(name) if text is None else text, encoding="utf-8")
    return path


def _git(cwd: Path, *argv: str) -> None:
    env = {k: v for k, v in os.environ.items() if not k.startswith("GIT_")}
    env.update(GIT_CONFIG_GLOBAL=os.devnull, GIT_CONFIG_SYSTEM=os.devnull,
               GIT_AUTHOR_NAME="t", GIT_AUTHOR_EMAIL="t@t", GIT_COMMITTER_NAME="t", GIT_COMMITTER_EMAIL="t@t")
    subprocess.run(["git", *argv], cwd=cwd, env=env, check=True, capture_output=True, timeout=60)


@pytest.fixture
def shipped(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    root = tmp_path / "shipped"
    root.mkdir()
    monkeypatch.setattr(library, "shipped_root", lambda: root)
    return root


@pytest.fixture(autouse=True)
def _switch_unset(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv(library.REPO_ROOT_ENV, raising=False)


@pytest.fixture
def repo_dir(tmp_path: Path) -> Path:
    top = tmp_path / "work" / "myrepo"
    top.mkdir(parents=True)
    _git(top, "init", "-q", "-b", "main")
    return top


@pytest.fixture
def repo(repo_dir: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    """A repo with the repo-root switch ON."""
    monkeypatch.setenv(library.REPO_ROOT_ENV, "1")
    return repo_dir


def _repo_root(repo: Path) -> Path:
    return repo / ".nexus" / "procedures"


# -- the shipped root -------------------------------------------------------------


def test_a_shipped_aura_loads_and_round_trips(shipped: Path) -> None:
    _write(shipped, "ci_develop_red", TD1_EXAMPLE)
    aura = auras.load_aura("ci_develop_red", cwd=None)
    assert aura.name == "ci_develop_red"
    assert aura.procedure == "ci_red_notice"
    assert aura.subspace == "board/ci/nexus-develop--done"


def test_a_name_in_neither_root_is_not_found_naming_it(shipped: Path) -> None:
    with pytest.raises(LibraryError) as err:
        auras.load_aura("ghost", cwd=None)
    assert err.value.reason == "not_found"
    assert "ghost" in str(err.value)
    assert "aura" in str(err.value)


@pytest.mark.parametrize("name", ["Bad", "con", "x.aura", "a/b", "../x", ""])
def test_a_name_that_breaks_the_rule_is_refused_before_any_path_is_built(shipped: Path, name: str) -> None:
    with pytest.raises(LibraryError) as err:
        auras.load_aura(name, cwd=None)
    assert err.value.reason == "invalid_name"


def test_the_stem_of_a_double_suffix_is_not_the_name(shipped: Path) -> None:
    # Path("x.aura.toml").stem is "x.aura": the loader and the listing strip the whole suffix.
    _write(shipped, "x")
    assert auras.load_aura("x", cwd=None).name == "x"
    assert [e.name for e in auras.list_auras(cwd=None)] == ["x"]


def test_only_a_file_whose_entry_is_exactly_the_name_is_found(shipped: Path) -> None:
    # A case-insensitive filesystem opens foo.aura.toml as Foo.aura.toml; the listing shows
    # Foo only, so load must not return a file the listing does not show under that name.
    (shipped / "Foo.aura.toml").write_text(minimal("foo"), encoding="utf-8")
    with pytest.raises(LibraryError) as err:
        auras.load_aura("foo", cwd=None)
    assert err.value.reason == "not_found"
    assert auras.list_auras(cwd=None) == []


def test_a_directory_named_like_an_aura_is_not_an_aura(shipped: Path) -> None:
    (shipped / "dir.aura.toml").mkdir()
    with pytest.raises(LibraryError) as err:
        auras.load_aura("dir", cwd=None)
    assert err.value.reason == "not_found"


def test_a_file_that_breaks_the_grammar_is_a_typed_refusal_naming_file_and_key(shipped: Path) -> None:
    _write(shipped, "bad", minimal("bad").replace('threads = "a block"', 'threads = "a block"\nchannel = 3'))
    with pytest.raises(AuraError) as err:
        auras.load_aura("bad", cwd=None)
    assert (err.value.kind, err.value.file, err.value.key) == ("wrong_type", "bad.aura.toml", "display.channel")


def test_the_file_name_is_what_the_aura_key_is_checked_against(shipped: Path) -> None:
    _write(shipped, "one", minimal("two"))
    with pytest.raises(AuraError) as err:
        auras.load_aura("one", cwd=None)
    assert err.value.kind == "name_mismatch"


@needs_git
def test_the_suffix_is_part_of_the_entry_so_a_procedure_and_an_aura_never_shadow_each_other(
    shipped: Path, repo: Path
) -> None:
    # foo.lua is not foo.aura.toml: a repo procedure named like a shipped aura is no shadow.
    _write(shipped, "foo")
    (_repo_root(repo)).mkdir(parents=True)
    (_repo_root(repo) / "foo.lua").write_text("-- procedure: foo\n", encoding="utf-8")
    assert auras.load_aura("foo", cwd=repo).name == "foo"


# -- the repo root ----------------------------------------------------------------


@needs_git
def test_a_repo_aura_loads_with_the_switch_on(shipped: Path, repo: Path) -> None:
    _write(_repo_root(repo), "mine")
    assert auras.load_aura("mine", cwd=repo).name == "mine"


@needs_git
def test_a_repo_aura_is_found_from_a_subdirectory(shipped: Path, repo: Path) -> None:
    _write(_repo_root(repo), "mine")
    sub = repo / "a" / "b"
    sub.mkdir(parents=True)
    assert auras.load_aura("mine", cwd=sub).name == "mine"


@needs_git
def test_a_repo_aura_is_refused_while_the_repo_root_is_off(shipped: Path, repo_dir: Path) -> None:
    _write(_repo_root(repo_dir), "mine")
    with pytest.raises(LibraryError) as err:
        auras.load_aura("mine", cwd=repo_dir)
    assert err.value.reason == "repo_root_off"
    assert library.REPO_ROOT_ENV in str(err.value)


@needs_git
def test_a_repo_aura_that_shadows_a_shipped_aura_is_refused(shipped: Path, repo: Path) -> None:
    _write(shipped, "common")
    _write(_repo_root(repo), "common")
    with pytest.raises(LibraryError) as err:
        auras.load_aura("common", cwd=repo)
    assert err.value.reason == "shadows_shipped"
    assert "common" in str(err.value)


@needs_git
def test_a_shipped_aura_still_loads_beside_a_repo_file_while_the_root_is_off(shipped: Path, repo_dir: Path) -> None:
    _write(shipped, "common")
    _write(_repo_root(repo_dir), "common", minimal("common").replace("does a thing", "SHADOW"))
    assert auras.load_aura("common", cwd=repo_dir).summary == "does a thing"


@needs_git
def test_a_repo_root_that_is_a_link_out_of_the_repo_is_refused(shipped: Path, repo: Path, tmp_path: Path) -> None:
    outside = tmp_path / "outside"
    _write(outside, "mine")
    (repo / ".nexus").mkdir()
    (repo / ".nexus" / "procedures").symlink_to(outside, target_is_directory=True)
    with pytest.raises(LibraryError) as err:
        auras.load_aura("mine", cwd=repo)
    assert err.value.reason == "outside_root"


# -- the listing --------------------------------------------------------------------


def test_the_listing_is_sorted_and_carries_each_summary_and_root(shipped: Path) -> None:
    _write(shipped, "zeta", minimal("zeta").replace("does a thing", "last one"))
    _write(shipped, "alpha", minimal("alpha").replace("does a thing", "first one"))
    assert auras.list_auras(cwd=None) == [
        library.Procedure("alpha", "first one", "shipped"),
        library.Procedure("zeta", "last one", "shipped"),
    ]


@needs_git
def test_the_listing_joins_both_roots_and_hides_a_shadowing_repo_file(shipped: Path, repo: Path) -> None:
    _write(shipped, "common", minimal("common").replace("does a thing", "shipped common"))
    _write(_repo_root(repo), "common", minimal("common").replace("does a thing", "shadow"))
    _write(_repo_root(repo), "mine", minimal("mine").replace("does a thing", "repo mine"))
    assert auras.list_auras(cwd=repo) == [
        library.Procedure("common", "shipped common", "shipped"),
        library.Procedure("mine", "repo mine", "repo"),
    ]


@needs_git
def test_the_repo_root_is_not_listed_while_it_is_off(shipped: Path, repo_dir: Path) -> None:
    _write(shipped, "common")
    _write(_repo_root(repo_dir), "mine")
    assert [e.name for e in auras.list_auras(cwd=repo_dir)] == ["common"]


def test_a_file_that_breaks_the_grammar_is_listed_with_the_refusal_not_dropped(shipped: Path) -> None:
    _write(shipped, "bad", 'zzz = "x"\n')
    [entry] = auras.list_auras(cwd=None)
    assert entry.name == "bad"
    assert entry.summary.startswith("refused: ")
    assert "zzz" in entry.summary


def test_a_file_whose_name_breaks_the_rule_is_not_listed(shipped: Path) -> None:
    (shipped / "Bad-Name.aura.toml").write_text(minimal("Bad-Name"), encoding="utf-8")
    (shipped / "con.aura.toml").write_text(minimal("con"), encoding="utf-8")
    assert auras.list_auras(cwd=None) == []


def test_the_listing_shows_auras_only_and_the_procedure_listing_shows_procedures_only(shipped: Path) -> None:
    _write(shipped, "an_aura")
    (shipped / "a_proc.lua").write_text(
        "-- procedure: a_proc\n-- summary: a proc\n-- args: none\n-- grants: none\nreturn 1\n", encoding="utf-8"
    )
    assert [e.name for e in auras.list_auras(cwd=None)] == ["an_aura"]
    assert [p.name for p in library.procedures(cwd=None)] == ["a_proc"]


def test_the_real_shipped_root_lists_without_failing() -> None:
    entries = auras.list_auras(cwd=None)
    assert "ci_develop_red" in {e.name for e in entries}  # the first shipped aura (nexus-mdbds.12.1)
    assert all(isinstance(e, library.Procedure) and e.root == "shipped" for e in entries)


def test_a_file_nested_too_deeply_is_refused_by_load_and_listed_as_refused(shipped: Path) -> None:
    depth = 30_000
    _write(shipped, "deep", "a = " + "[" * depth + "]" * depth)
    _write(shipped, "fine")
    with pytest.raises(AuraError) as err:
        auras.load_aura("deep", cwd=None)
    assert (err.value.kind, err.value.file) == ("bad_toml", "deep.aura.toml")
    listing = {e.name: e.summary for e in auras.list_auras(cwd=None)}
    assert listing["deep"].startswith("refused: ")
    assert listing["fine"] == "does a thing"


def test_a_file_over_the_read_bound_is_refused_even_when_what_was_read_parses(shipped: Path) -> None:
    # The library reads READ_LIMIT bytes; a file longer than that reaches the parser with
    # exactly READ_LIMIT. Here the first READ_LIMIT bytes are a valid aura and the [clear]
    # table (which would refuse the file) comes after the cut, so only the size check refuses.
    base = minimal("big")
    head = base + "#" * (library.READ_LIMIT - len(base) - 1) + "\n"
    assert len(head.encode()) == library.READ_LIMIT
    _write(shipped, "big", head + "[clear]\nmatch = {}\n")
    with pytest.raises(AuraError) as err:
        auras.load_aura("big", cwd=None)
    assert err.value.kind == "too_large"


# -- the structured enumeration (nexus-mdbds.10.2: what nx aura check walks) -------------------------


def _by_key(entries: list[auras.AuraEntry]) -> dict[tuple[str, str], object]:
    return {(e.name, e.root): e.result for e in entries}


def test_the_enumeration_yields_an_aura_a_grammar_refusal_and_a_name_refusal_per_file(shipped: Path) -> None:
    _write(shipped, "good")
    _write(shipped, "bad", 'zzz = "x"\n')
    (shipped / "Bad-Name.aura.toml").write_text(minimal("Bad-Name"), encoding="utf-8")
    got = _by_key(auras.enumerate_auras(cwd=None))
    assert isinstance(got[("good", "shipped")], auras.Aura)
    assert isinstance(got[("bad", "shipped")], AuraError)
    refused = got[("Bad-Name", "shipped")]
    assert isinstance(refused, LibraryError) and refused.reason == "invalid_name"


@needs_git
def test_the_enumeration_keeps_a_repo_file_that_shadows_a_shipped_aura(shipped: Path, repo: Path) -> None:
    _write(shipped, "common")
    _write(_repo_root(repo), "common")
    _write(_repo_root(repo), "mine")
    got = _by_key(auras.enumerate_auras(cwd=repo))
    assert isinstance(got[("common", "shipped")], auras.Aura)
    shadow = got[("common", "repo")]
    assert isinstance(shadow, LibraryError) and shadow.reason == "shadows_shipped"
    assert isinstance(got[("mine", "repo")], auras.Aura)


@needs_git
def test_the_enumeration_does_not_walk_the_repo_root_while_it_is_off(shipped: Path, repo_dir: Path) -> None:
    _write(shipped, "common")
    _write(_repo_root(repo_dir), "mine")
    assert [(e.name, e.root) for e in auras.enumerate_auras(cwd=repo_dir)] == [("common", "shipped")]


@needs_git
def test_an_enumerated_file_that_leaves_its_root_is_a_library_refusal_not_dropped(
    shipped: Path, repo: Path, tmp_path: Path
) -> None:
    outside = tmp_path / "outside"
    _write(outside, "mine")
    _write(_repo_root(repo), "other")
    (_repo_root(repo) / "mine.aura.toml").symlink_to(outside / "mine.aura.toml")
    got = _by_key(auras.enumerate_auras(cwd=repo))
    escaped = got[("mine", "repo")]
    assert isinstance(escaped, LibraryError) and escaped.reason == "outside_root"
    assert isinstance(got[("other", "repo")], auras.Aura)


def test_the_enumeration_is_sorted_by_name_then_root(shipped: Path) -> None:
    for name in ("zeta", "alpha", "mid"):
        _write(shipped, name)
    assert [e.name for e in auras.enumerate_auras(cwd=None)] == ["alpha", "mid", "zeta"]


def test_the_procedure_listing_still_hides_what_the_enumeration_shows(shipped: Path) -> None:
    # .lua behavior is unchanged: library.procedures drops a name that breaks the rule silently.
    (shipped / "Bad-Name.lua").write_text("-- procedure: x\n", encoding="utf-8")
    assert library.procedures(cwd=None) == []
    assert [e.name for e in library.scan_files(cwd=None, suffix=".lua", noun="procedure")] == ["Bad-Name"]


# -- review fixes (nexus-mdbds.10.2) -------------------------------------------------------------------------------


@needs_git
def test_one_name_in_both_roots_sorts_shipped_then_repo(shipped: Path, repo: Path) -> None:
    _write(shipped, "common")
    _write(_repo_root(repo), "common")
    _write(shipped, "aaa")
    assert [(s.name, s.root) for s in library.scan_files(cwd=repo, suffix=".aura.toml", noun="aura")] == [
        ("aaa", "shipped"), ("common", "shipped"), ("common", "repo"),
    ]
    assert [(e.name, e.root) for e in auras.enumerate_auras(cwd=repo)] == [
        ("aaa", "shipped"), ("common", "shipped"), ("common", "repo"),
    ]


def test_a_directory_named_like_an_aura_is_reported_not_dropped(shipped: Path) -> None:
    (shipped / "dir.aura.toml").mkdir()
    [entry] = auras.enumerate_auras(cwd=None)
    assert (entry.name, entry.root) == ("dir", "shipped")
    assert isinstance(entry.result, LibraryError) and entry.result.reason == "unreadable"
    assert "not a regular file" in entry.result.detail


def test_a_dangling_link_named_like_an_aura_is_reported_not_dropped(shipped: Path, tmp_path: Path) -> None:
    (shipped / "gone.aura.toml").symlink_to(tmp_path / "nowhere.aura.toml")
    [entry] = auras.enumerate_auras(cwd=None)
    assert entry.name == "gone"
    assert isinstance(entry.result, LibraryError) and entry.result.reason == "unreadable"


def test_the_lua_listing_still_ignores_such_entries(shipped: Path) -> None:
    (shipped / "dir.lua").mkdir()
    assert library.procedures(cwd=None) == []


# -- RDR-252 Step 4 part A (nexus-igea9.7): load by group membership ---------------------------------------------------

_GATED = minimal("gated").replace(
    'subspace = "board/topic"', 'subspace = "board/ci/nexus-develop--owner"'
) + '\n[load]\ngroup = "push-train"\n'


def test_group_gated_aura_is_not_loaded_for_a_non_member() -> None:
    """The first RDR-252 Step 4 test: a bound identity whose memberships lack the aura's group loads nothing and
    never subscribes to the aura's trigger subspace. Red before the work: [load] is an unknown key."""
    from nexus.auras.session import AuraLoader  # noqa: PLC0415
    from nexus.mcp.subscriptions import SubscriptionSet  # noqa: PLC0415

    aura = auras.parse_aura(_GATED.encode(), name="gated")
    subs = SubscriptionSet(session_id="11111111-2222-3333-4444-555555555555")
    attached: list = []
    group_reads: list[str] = []
    loader = AuraLoader(
        "11111111-2222-3333-4444-555555555555", subs,
        discover=lambda: [aura],
        is_on=lambda _n: True,
        entry=lambda _sid: ("tamsin", 100),
        registered_owner=lambda: 100,
        make_runtime=lambda loaded: loaded,
        attach=attached.append,
        identity=lambda: "identity-a",
        memberships=lambda _identity: ["some-other-group"],
        read_group=lambda name: group_reads.append(name),
    )
    loader.on_resolve("tamsin")
    assert attached == []
    assert subs.aura_topics() == []
    assert group_reads == []


class _GatedHarness:
    """The loader with RDR-252's injected reads counted: the identity, its memberships, one group record."""

    def __init__(self, *, identity="identity-a", memberships=("push-train",), groups=None, auras=None, now=None):
        from datetime import datetime, timezone  # noqa: PLC0415

        from nexus.auras.load_gate import GroupRecord  # noqa: PLC0415
        from nexus.auras.session import AuraLoader  # noqa: PLC0415
        from nexus.mcp.subscriptions import SubscriptionSet  # noqa: PLC0415

        self.subs = SubscriptionSet(session_id=_SID)
        self.attached: list = []
        self.membership_reads: list[str] = []
        self.group_reads: list[str] = []
        self.states: list = []
        records = {"push-train": GroupRecord("push-train", serves=("nexus-develop",))} if groups is None else groups
        parsed = auras_mod_parse(_GATED, "gated") if auras is None else auras

        def read_memberships(who):
            self.membership_reads.append(who)
            if isinstance(memberships, Exception):
                raise memberships
            return list(memberships)

        def read_group(name):
            self.group_reads.append(name)
            found = records.get(name)
            if isinstance(found, Exception):
                raise found
            return found

        self.loader = AuraLoader(
            _SID, self.subs,
            discover=lambda: parsed,
            is_on=lambda _n: True,
            entry=lambda _sid: ("tamsin", 100),
            registered_owner=lambda: 100,
            make_runtime=lambda loaded: loaded,
            attach=self.attached.append,
            identity=lambda: identity,
            memberships=read_memberships,
            read_group=read_group,
            now=now or (lambda: datetime(2026, 11, 19, 12, 0, tzinfo=timezone.utc)),
            state_sink=lambda pid, state: self.states.append((pid, state)),
        )
        self.loader.on_resolve("tamsin")

    @property
    def loaded(self) -> list[str]:
        return [a.name for batch in self.attached for a in batch]


_SID = "11111111-2222-3333-4444-555555555555"


def auras_mod_parse(text: str, name: str) -> list:
    return [auras.parse_aura(text.encode(), name=name)]


def _variant(name: str, *, group: str = "push-train", topic: str = "nexus-develop") -> object:
    text = (
        _GATED.replace('aura = "gated"', f'aura = "{name}"').replace('threads = "a block"', f'threads = "block {name}"')
        .replace('group = "push-train"', f'group = "{group}"').replace("nexus-develop--owner", f"{topic}--owner")
    )
    return auras.parse_aura(text.encode(), name=name)


def test_a_member_of_a_live_group_loads_the_aura_and_subscribes_to_its_trigger() -> None:
    h = _GatedHarness()
    assert h.loaded == ["gated"]
    assert h.subs.aura_topics() == ["board/ci/nexus-develop--owner"]
    assert h.loader.decisions["gated"].loaded and h.loader.decisions["gated"].group == "push-train"


def test_one_membership_read_and_one_group_read_per_named_group() -> None:
    from nexus.auras.load_gate import GroupRecord  # noqa: PLC0415

    h = _GatedHarness(
        memberships=("push-train", "review-pair"),
        auras=[_variant("one"), _variant("two"), _variant("other", group="review-pair", topic="nexus-x")],
        groups={"push-train": GroupRecord("push-train"), "review-pair": GroupRecord("review-pair")},
    )
    assert sorted(h.loaded) == ["one", "other", "two"]
    assert h.membership_reads == ["identity-a"]
    assert sorted(h.group_reads) == ["push-train", "review-pair"]


def test_an_ungated_aura_reads_nothing_and_loads_for_an_unbound_server() -> None:
    h = _GatedHarness(identity=None, auras=auras_mod_parse(minimal("plain"), "plain"))
    assert h.loaded == ["plain"]
    assert h.membership_reads == [] and h.group_reads == []


class _FakeRuntime:
    """What the loader asks of a runtime after the first load: take more auras, and the identity they were gated for."""

    def __init__(self, loaded) -> None:
        self.auras = [a.name for a in loaded]
        self.identity: str | None = None
        self.extended: list[list[str]] = []

    def extend(self, loaded, identity) -> None:
        self.extended.append([a.name for a in loaded])
        self.auras += [a.name for a in loaded]
        self.identity = identity


class _LateBind:
    """The loader with an identity that changes under it, the way the lease's pending bind does (RDR-252 round 1)."""

    def __init__(self, *, auras, identity=None, token_identity=None) -> None:
        from nexus.auras.load_gate import GroupRecord  # noqa: PLC0415
        from nexus.auras.session import AuraLoader  # noqa: PLC0415
        from nexus.mcp.subscriptions import SubscriptionSet  # noqa: PLC0415

        self.subs = SubscriptionSet(session_id=_SID)
        self.runtimes: list[_FakeRuntime] = []
        self.attached: list = []
        self.membership_reads: list[str] = []
        self.states: list = []
        self.token_identity = token_identity if token_identity is not None else identity

        def make(loaded):
            runtime = _FakeRuntime(loaded)
            self.runtimes.append(runtime)
            return runtime

        def memberships(who):
            self.membership_reads.append(who)
            return ["push-train"]

        self.loader = AuraLoader(
            _SID, self.subs, discover=lambda: auras, is_on=lambda _n: True, entry=lambda _sid: ("tamsin", 100),
            registered_owner=lambda: 100, make_runtime=make, attach=self.attached.append,
            identity=lambda: self.token_identity, memberships=memberships,
            read_group=lambda name: GroupRecord(name, serves=("nexus-develop",)),
            state_sink=lambda pid, state: self.states.append((pid, state)),
        )


def test_a_server_that_loaded_unbound_gates_again_once_when_the_lease_reports_its_first_bind() -> None:
    plain = auras_mod_parse(minimal("plain").replace('threads = "a block"', 'threads = "block plain"'), "plain")
    h = _LateBind(auras=[*plain, *auras_mod_parse(_GATED, "gated")])
    h.loader.on_resolve("tamsin")
    assert h.runtimes[0].auras == ["plain"] and h.loader.decisions["gated"].reason == "unbound"
    h.loader.note_identity("identity-a")                              # the pending bind: None -> identity-a
    assert h.loader.decisions["gated"].loaded and h.runtimes[0].extended == [["gated"]]
    assert h.runtimes[0].identity == "identity-a", "self.identity now fills"
    assert sorted(h.subs.aura_topics()) == ["board/ci/nexus-develop--owner", "board/topic"]
    assert h.membership_reads == ["identity-a"]
    last = h.states[-1][1]
    assert (last.identity, last.loaded_identity, last.auras["gated"].loaded) == ("identity-a", "identity-a", True)
    h.loader.note_identity("identity-b")                              # a later switch is the reconnect case, not a re-gate
    assert h.membership_reads == ["identity-a"] and h.runtimes[0].extended == [["gated"]]
    assert h.states[-1][1].loaded_identity == "identity-a" and h.states[-1][1].identity == "identity-b"


def test_a_desktop_server_with_only_gated_auras_builds_its_runtime_when_it_binds() -> None:
    h = _LateBind(auras=auras_mod_parse(_GATED, "gated"))
    h.loader.on_resolve("tamsin")
    assert h.attached == [] and h.runtimes == [] and h.subs.aura_topics() == []
    h.loader.note_identity("identity-a")
    assert len(h.runtimes) == 1 and h.attached == h.runtimes and h.runtimes[0].auras == ["gated"]
    assert h.subs.aura_topics() == ["board/ci/nexus-develop--owner"]


def test_a_bind_the_lease_reported_before_the_load_is_used_even_when_the_token_read_misses_it() -> None:
    """The pending-bind ordering: the lease adopted the identity (and told the loader) before the registry named the
    session; the token read at load time returns nothing. The loader keeps what it was told."""
    h = _LateBind(auras=auras_mod_parse(_GATED, "gated"), token_identity=None)
    h.loader.note_identity("identity-a")                              # before any load: nothing to rewrite yet
    assert h.states == [] and h.membership_reads == []
    h.loader.on_resolve("tamsin")
    assert h.loader.decisions["gated"].loaded and h.membership_reads == ["identity-a"]
    assert h.loader.loaded_identity == "identity-a"


def test_a_server_that_loaded_bound_does_not_gate_again_on_a_change() -> None:
    h = _LateBind(auras=auras_mod_parse(_GATED, "gated"), identity="identity-a")
    h.loader.on_resolve("tamsin")
    assert h.membership_reads == ["identity-a"]
    h.loader.note_identity("identity-b")
    assert h.membership_reads == ["identity-a"] and h.runtimes[0].extended == []


def _off_the_loop() -> bool:
    import asyncio  # noqa: PLC0415

    try:
        asyncio.get_running_loop()
    except RuntimeError:
        return True
    return False


def test_the_gate_reads_run_off_the_loop_and_the_subscription_on_it() -> None:
    """nexus-igea9.7 round 1: a membership read and a group read are blocking HTTP (up to a timeout each); on the
    waiter's loop they would stall every channel delivery. on_loop does the reads on a worker thread and commits on the loop."""
    import asyncio  # noqa: PLC0415

    from nexus.auras.load_gate import GroupRecord  # noqa: PLC0415
    from nexus.auras.session import AuraLoader, on_loop  # noqa: PLC0415
    from nexus.mcp.subscriptions import SubscriptionSet  # noqa: PLC0415

    seen: dict[str, bool] = {}

    async def go():
        subs = SubscriptionSet(session_id=_SID)
        committed: list[bool] = []

        def memberships(_who):
            seen["memberships"] = _off_the_loop()
            return ["push-train"]

        def read_group(name):
            seen["group"] = _off_the_loop()
            return GroupRecord(name)

        def make(loaded):
            committed.append(not _off_the_loop())
            return loaded

        loader = AuraLoader(
            _SID, subs, discover=lambda: auras_mod_parse(_GATED, "gated"), is_on=lambda _n: True,
            entry=lambda _sid: ("tamsin", 100), registered_owner=lambda: 100, make_runtime=make, attach=lambda _r: None,
            identity=lambda: "identity-a", memberships=memberships, read_group=read_group,
        )
        on_loop(loader, asyncio.get_running_loop())("tamsin")
        for _ in range(500):
            if loader.loaded and committed:
                break
            await asyncio.sleep(0.01)
        return loader, subs, committed

    loader, subs, committed = asyncio.run(go())
    assert seen == {"memberships": True, "group": True}, "the reads ran on a worker thread"
    assert committed == [True] and subs.aura_topics() == ["board/ci/nexus-develop--owner"], "the load itself committed on the loop"
    assert loader.decisions["gated"].loaded


def test_the_late_gate_after_a_first_bind_also_reads_off_the_loop() -> None:
    import asyncio  # noqa: PLC0415

    h = None

    async def go():
        nonlocal h
        h = _LateBind(auras=auras_mod_parse(_GATED, "gated"))
        reads: list[bool] = []
        original = h.loader._stage._memberships  # noqa: SLF001

        def watching(who):
            reads.append(_off_the_loop())
            return original(who)

        h.loader._stage._memberships = watching  # noqa: SLF001
        h.loader.on_resolve("tamsin")
        await h.loader.note_identity_async("identity-a")
        return reads

    assert asyncio.run(go()) == [True]
    assert h is not None and h.loader.decisions["gated"].loaded


def test_a_bind_the_lease_reports_while_a_load_that_saw_no_identity_runs_is_replayed_after_the_commit() -> None:
    """nexus-igea9.7 round 2: the lease adopts the identity after the load read the token (none) and before the commit.
    The note finds no state to re-gate (the commit has not run), so the loader keeps it and on_resolve_async replays it
    once the commit is in; without the replay the gated aura stays 'unbound' for the server's life."""
    import asyncio  # noqa: PLC0415

    async def go():
        h = _LateBind(auras=auras_mod_parse(_GATED, "gated"))
        original = h.loader._stage.gate  # noqa: SLF001
        loop = asyncio.get_running_loop()

        def gate_after_the_lease_binds(auras, identity):
            # Runs on the load's worker thread, after the identity read: the lease's listener now fires on the loop.
            if identity is None:  # the load's own gate; the replay's gate (with the identity) goes straight through
                asyncio.run_coroutine_threadsafe(h.loader.note_identity_async("identity-a"), loop).result(10)
            return original(auras, identity)

        h.loader._stage.gate = gate_after_the_lease_binds  # noqa: SLF001
        await h.loader.on_resolve_async("tamsin")
        return h

    h = asyncio.run(go())
    assert h.loader.decisions["gated"].loaded, "the replay gated again with the identity the lease reported"
    assert h.membership_reads == ["identity-a"] and len(h.runtimes) == 1 and h.runtimes[0].auras == ["gated"]
    assert h.loader.loaded_identity == "identity-a" and h.subs.aura_topics() == ["board/ci/nexus-develop--owner"]
    assert h.states[-1][1].auras["gated"].loaded


def test_the_state_sink_writes_on_one_worker_thread_in_order(tmp_path) -> None:
    import threading  # noqa: PLC0415

    from nexus.auras.session import threaded_sink  # noqa: PLC0415

    seen: list[tuple[int, str, str]] = []
    sink = threaded_sink(lambda pid, state: seen.append((pid, state, threading.current_thread().name)))
    sink(1, "a")  # type: ignore[arg-type]
    sink(1, "b")  # type: ignore[arg-type]
    assert sink.wait_idle(10)
    assert [s for _p, s, _t in seen] == ["a", "b"]
    assert {t for _p, _s, t in seen} != {threading.current_thread().name}, "not the caller's thread"
    assert len({t for _p, _s, t in seen}) == 1, "one worker keeps the writes in order"


def test_an_unbound_server_loads_no_group_gated_aura_and_says_why() -> None:
    plain = auras_mod_parse(minimal("plain"), "plain")
    h = _GatedHarness(identity=None, auras=[*plain, *auras_mod_parse(_GATED, "gated")])
    assert h.loaded == ["plain"]
    assert h.subs.aura_topics() == ["board/topic"]
    assert h.membership_reads == [] and h.group_reads == []
    assert (h.loader.decisions["gated"].loaded, h.loader.decisions["gated"].reason) == (False, "unbound")


def test_a_disbanded_group_loads_nothing() -> None:
    from nexus.auras.load_gate import GroupRecord  # noqa: PLC0415

    ended = {"push-train": GroupRecord("push-train", ended_at="2026-11-18T10:00:00Z")}
    passed = {"push-train": GroupRecord("push-train", ends="2026-11-19T11:59:59Z")}
    for groups, reason in ((ended, "group_disbanded"), (passed, "group_disbanded"), ({}, "no_group_record")):
        h = _GatedHarness(groups=groups)
        assert h.loaded == [] and h.subs.aura_topics() == [], reason
        assert h.loader.decisions["gated"].reason == reason


def test_a_group_whose_end_is_still_ahead_or_open_is_live() -> None:
    from nexus.auras.load_gate import GroupRecord  # noqa: PLC0415

    for ends in ("2026-11-19T12:00:01Z", "open", None):
        h = _GatedHarness(groups={"push-train": GroupRecord("push-train", ends=ends)})
        assert h.loaded == ["gated"], ends


def test_a_non_member_never_reads_the_group() -> None:
    h = _GatedHarness(memberships=("elsewhere",))
    assert h.loaded == [] and h.group_reads == []
    assert h.loader.decisions["gated"].reason == "not_a_member"


@pytest.mark.parametrize("which", ["memberships", "group"])
def test_an_unreadable_read_loads_no_gated_aura(which: str) -> None:
    from nexus.auras.load_gate import GroupUnreadable  # noqa: PLC0415

    if which == "memberships":
        h = _GatedHarness(memberships=GroupUnreadable("engine down"))
        assert h.loader.decisions["gated"].reason == "memberships_unreadable"
    else:
        h = _GatedHarness(groups={"push-train": GroupUnreadable("engine down")})
        assert h.loader.decisions["gated"].reason == "group_unreadable"
    assert h.loaded == [] and h.subs.aura_topics() == []


def test_every_decision_reaches_the_state_sink_with_the_identity() -> None:
    h = _GatedHarness(memberships=("elsewhere",))
    [(pid, state)] = h.states
    assert pid == 100
    assert state.loaded_identity == "identity-a"
    gated = state.auras["gated"]
    assert (gated.loaded, gated.reason, gated.group) == (False, "not_a_member", "push-train")


def test_group_liveness_follows_ended_at_ends_and_a_missing_record() -> None:
    from datetime import datetime, timezone  # noqa: PLC0415

    from nexus.auras.load_gate import GroupRecord, group_is_live, group_status  # noqa: PLC0415

    now = datetime(2026, 11, 19, 12, 0, tzinfo=timezone.utc)
    assert group_is_live(GroupRecord("g"), now) and group_status(GroupRecord("g"), now) == "live"
    assert not group_is_live(None, now) and group_status(None, now) == "no_group_record"
    assert group_status(GroupRecord("g", ended_at="2026-11-01T00:00:00Z"), now) == "group_disbanded"
    assert not group_is_live(GroupRecord("g", ends="2026-11-19T12:00:00Z"), now), "ends is the first instant it is over"
    assert not group_is_live(GroupRecord("g", ends="2026-11-19T11:00:00+00:00"), now)
    assert group_is_live(GroupRecord("g", ends="2026-11-19T13:00:00"), now), "a bare instant is UTC"
    assert group_is_live(GroupRecord("g", ends="garbage"), now), "an ends that is no instant never ends the group"


class _FakeGroups:
    """Stands in for nexus.groups.GroupsClient: ``record`` answers from a table or raises what it is told to."""

    def __init__(self, records: dict, fail: Exception | None = None) -> None:
        self.records, self.fail, self.reads = records, fail, []

    def record(self, name: str):
        self.reads.append(name)
        if self.fail is not None:
            raise self.fail
        return self.records.get(name)


def test_the_group_reader_maps_the_engine_record_and_a_missing_name() -> None:
    from nexus.auras.load_gate import GroupRecord as GateRecord  # noqa: PLC0415
    from nexus.auras.load_gate import group_reader_from  # noqa: PLC0415
    from nexus.groups import GroupRecord as EngineRecord  # noqa: PLC0415

    engine = EngineRecord(name="push-train", leader="i1", purpose=None, requested_by=None, formed_at="t",
                          ends=None, ended_at=None, ack_deadline=20, ack_deadline_by_topic={},
                          serves=("nexus-develop",), live=True)
    read = group_reader_from(_FakeGroups({"push-train": engine}))
    assert read("push-train") == GateRecord("push-train", serves=("nexus-develop",), leader="i1", live=True)
    assert read("gone") is None


def test_a_group_read_that_fails_is_unreadable_never_no_record() -> None:
    from nexus.auras.load_gate import GroupUnreadable, group_reader_from  # noqa: PLC0415
    from nexus.groups import GroupsError, GroupsUnavailableError  # noqa: PLC0415

    for exc in (GroupsError(500, "internal", "boom"), GroupsUnavailableError("down")):
        with pytest.raises(GroupUnreadable):
            group_reader_from(_FakeGroups({}, fail=exc))("push-train")


def test_the_default_reader_with_no_engine_endpoint_is_unreadable_not_a_failed_start(monkeypatch) -> None:
    from nexus import groups  # noqa: PLC0415
    from nexus.auras.load_gate import GroupUnreadable, default_group_reader  # noqa: PLC0415

    def _none(**_kw):
        raise groups.GroupsUnavailableError("no endpoint")

    monkeypatch.setattr(groups.GroupsClient, "from_config", staticmethod(_none))
    read = default_group_reader()
    with pytest.raises(GroupUnreadable):
        read("push-train")


def test_a_loader_built_without_a_group_reader_refuses_the_gated_aura_as_unreadable() -> None:
    from nexus.auras.session import AuraLoader  # noqa: PLC0415
    from nexus.mcp.subscriptions import SubscriptionSet  # noqa: PLC0415

    subs = SubscriptionSet(session_id=_SID)
    loader = AuraLoader(
        _SID, subs, discover=lambda: auras_mod_parse(_GATED, "gated"), is_on=lambda _n: True,
        entry=lambda _sid: ("tamsin", 100), registered_owner=lambda: 100, make_runtime=lambda a: a, attach=lambda _r: None,
        identity=lambda: "id-a", memberships=lambda _i: ["push-train"],
    )
    loader.on_resolve("tamsin")
    assert subs.aura_topics() == []
    assert loader.decisions["gated"].reason == "group_unreadable"


def test_a_loader_with_no_identity_source_is_unbound() -> None:
    from nexus.auras.session import AuraLoader  # noqa: PLC0415
    from nexus.mcp.subscriptions import SubscriptionSet  # noqa: PLC0415

    subs = SubscriptionSet(session_id=_SID)
    loader = AuraLoader(
        _SID, subs, discover=lambda: auras_mod_parse(_GATED, "gated"), is_on=lambda _n: True,
        entry=lambda _sid: ("tamsin", 100), registered_owner=lambda: 100, make_runtime=lambda a: a, attach=lambda _r: None,
    )
    loader.on_resolve("tamsin")
    assert loader.decisions["gated"].reason == "unbound"


def test_an_identity_that_cannot_be_read_is_an_unbound_server() -> None:
    def broken() -> str:
        raise OSError("token file unreadable")

    from nexus.auras.session import AuraLoader  # noqa: PLC0415
    from nexus.mcp.subscriptions import SubscriptionSet  # noqa: PLC0415

    subs = SubscriptionSet(session_id=_SID)
    loader = AuraLoader(
        _SID, subs, discover=lambda: auras_mod_parse(_GATED, "gated"), is_on=lambda _n: True,
        entry=lambda _sid: ("tamsin", 100), registered_owner=lambda: 100, make_runtime=lambda a: a, attach=lambda _r: None,
        identity=broken, memberships=lambda _i: ["push-train"], read_group=lambda _n: None,
    )
    loader.on_resolve("tamsin")
    assert loader.decisions["gated"].reason == "unbound"


def test_a_load_that_fails_after_deciding_marks_the_loaded_auras_failed_in_the_state() -> None:
    h = _GatedHarness()
    assert h.states[-1][1].auras["gated"].loaded is True

    def broken(_loaded):
        raise RuntimeError("runtime bug")

    from nexus.auras.session import AuraLoader  # noqa: PLC0415
    from nexus.mcp.subscriptions import SubscriptionSet  # noqa: PLC0415

    states: list = []
    subs = SubscriptionSet(session_id=_SID)
    loader = AuraLoader(
        _SID, subs, discover=lambda: auras_mod_parse(_GATED, "gated"), is_on=lambda _n: True,
        entry=lambda _sid: ("tamsin", 100), registered_owner=lambda: 100, make_runtime=broken, attach=lambda _r: None,
        identity=lambda: "id-a", memberships=lambda _i: ["push-train"], read_group=lambda n: _group(n),
        state_sink=lambda pid, state: states.append(state),
    )
    loader.on_resolve("tamsin")
    assert subs.aura_topics() == [] and states[-1].auras["gated"].reason == "load_failed"


def _group(name):
    from nexus.auras.load_gate import GroupRecord  # noqa: PLC0415

    return GroupRecord(name)


def test_read_memberships_returns_group_names_and_turns_engine_errors_into_unreadable(monkeypatch: pytest.MonkeyPatch) -> None:
    from nexus.auras import session  # noqa: PLC0415
    from nexus.auras.load_gate import GroupUnreadable  # noqa: PLC0415
    from nexus.identity import bind_client  # noqa: PLC0415

    class Fake:
        closed = 0
        rows: object = [{"group": "push-train"}, {"group": "review-pair"}]

        def memberships(self, identity):
            if isinstance(self.rows, Exception):
                raise self.rows
            assert identity == "id-a"
            return self.rows

        def close(self):
            Fake.closed += 1

    fake = Fake()
    monkeypatch.setattr(bind_client.IdentityClient, "from_config", classmethod(lambda cls, **_k: fake))
    assert session.read_memberships("id-a") == ["push-train", "review-pair"] and Fake.closed == 1
    for error in (bind_client.IdentityApiError(404, "unknown_identity", "no"), bind_client.IdentityUnavailableError("down")):
        fake.rows = error
        with pytest.raises(GroupUnreadable):
            session.read_memberships("id-a")
    assert Fake.closed == 3, "the client is closed on every path"
    monkeypatch.setattr(
        bind_client.IdentityClient, "from_config",
        classmethod(lambda cls, **_k: (_ for _ in ()).throw(bind_client.IdentityUnavailableError("no endpoint"))),
    )
    with pytest.raises(GroupUnreadable):
        session.read_memberships("id-a")


def test_read_memberships_raises_unreadable_on_a_row_with_no_string_group(monkeypatch: pytest.MonkeyPatch) -> None:
    """nexus-igea9.7 round 1 (S1): a row that names no group is a malformed answer, not a membership to skip."""
    from nexus.auras import session  # noqa: PLC0415
    from nexus.auras.load_gate import GroupUnreadable  # noqa: PLC0415
    from nexus.identity import bind_client  # noqa: PLC0415

    class Fake:
        def memberships(self, _identity):
            return [{"group": "push-train"}, {"role": "no group field"}]

        def close(self):
            pass

    monkeypatch.setattr(bind_client.IdentityClient, "from_config", classmethod(lambda cls, **_k: Fake()))
    with pytest.raises(GroupUnreadable):
        session.read_memberships("id-a")


def test_a_group_record_the_wire_cannot_build_is_unreadable_not_a_traceback() -> None:
    from nexus.auras.load_gate import GroupUnreadable, group_reader_from  # noqa: PLC0415

    class Broken:
        def record(self, _name):
            from nexus.groups import GroupRecord  # noqa: PLC0415

            return GroupRecord.from_wire({"leader": "i1"})  # no name

    with pytest.raises(GroupUnreadable):
        group_reader_from(Broken())("push-train")
