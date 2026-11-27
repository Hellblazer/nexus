# SPDX-License-Identifier: AGPL-3.0-or-later
"""Edges of the ``{serves}`` template's load and check (RDR-252 "Aura per base"; bead nexus-igea9, round 3): an unreadable
group beside a serving one, the batched removal of the pre-template blocks, the sample base, group order, and the command
wiring of ``nx aura check``. Split from ``test_template.py`` for size; it reuses that file's harness."""
from __future__ import annotations

import logging
from pathlib import Path

import pytest
import structlog
from click.testing import CliRunner

from nexus import auras
from nexus.auras.aura import expand_template
from nexus.auras.check import check_auras
from nexus.auras.load_gate import GroupRecord
from nexus.auras.loaded_state import AuraState
from nexus.auras.loader import AuraEntry
from nexus.auras.session import AuraLoader
from nexus.mcp.subscriptions import SubscriptionSet

from .test_template import NOW, TEMPLATE, _groups, _Loader, _parse, _RetiringRuntime, _SID

LEGACY = [("ci_develop_nudge", "develop CI nudge"), ("ci_develop_red", "develop CI red")]


@pytest.fixture(autouse=True)
def _log_everything() -> None:
    structlog.configure(wrapper_class=structlog.make_filtering_bound_logger(logging.DEBUG))


# -- an unreadable group beside a serving one ---------------------------------------------------------------------


def test_an_unreadable_group_beside_a_serving_one_is_recorded_and_listed_not_dropped() -> None:
    from nexus.commands.aura_cmd import _loaded_text, _template_state  # noqa: PLC0415

    h = _Loader(memberships=("down-train", "push-train"))
    assert h.loaded == ["tmpl@nexus-develop"]
    assert h.loader.decisions == {
        "tmpl@nexus-develop": AuraState(True, "loaded", "push-train"),
        "tmpl@group:down-train": AuraState(False, "group_unreadable", "down-train"),
    }
    assert "tmpl@group:down-train" in h.states[-1][1].auras, "the state file carries it too"
    text = _loaded_text(_template_state(h.loader.decisions, "tmpl"))
    assert text == "  loaded for nexus-develop (push-train); not read: group down-train (reconnect)"


def test_with_one_group_and_it_unreadable_the_template_still_says_group_unreadable_under_its_own_name() -> None:
    h = _Loader(memberships=("down-train",))
    assert h.loader.decisions == {"tmpl": AuraState(False, "group_unreadable", None)}


def test_an_aura_never_holds_a_colon_in_a_base_so_group_keys_cannot_collide_with_bases() -> None:
    expansions, rejected = expand_template(_parse(), ("ok-main", "group:x-main"))
    assert [a.name for a in expansions] == ["tmpl@ok-main"] and [b for b, _ in rejected] == ["group:x-main"]


def test_nx_aura_list_through_the_command_says_which_group_was_not_read(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    from nexus.auras import loaded_state as ls  # noqa: PLC0415
    from nexus.commands import aura_cmd  # noqa: PLC0415
    from nexus.commands.aura_cmd import aura_group  # noqa: PLC0415
    from nexus.lua_runtime import library  # noqa: PLC0415
    from tests.identity.fakes import HOST, FakeProcesses  # noqa: PLC0415

    root = tmp_path / "shipped"
    root.mkdir()
    (root / "tmpl.aura.toml").write_text(TEMPLATE, encoding="utf-8")
    (tmp_path / "cfg").mkdir()
    (tmp_path / "cfg" / "config.yml").write_text("auras:\n  tmpl: on\n", encoding="utf-8")
    monkeypatch.setattr(library, "shipped_root", lambda: root)
    monkeypatch.delenv(library.REPO_ROOT_ENV, raising=False)
    monkeypatch.delenv("NX_AURAS_OFF", raising=False)
    monkeypatch.setenv("NEXUS_CONFIG_DIR", str(tmp_path / "cfg"))
    monkeypatch.chdir(tmp_path)
    procs = FakeProcesses(tmp_path / "sessions")
    procs.start(4242, "2026-11-17T08:00:00Z", started_at=4242000)
    adir = tmp_path / "auras"
    ls.write_state(ls.state_path(adir, 4242, 4242000), ls.LoadedState(
        server_pid=9000, host=HOST, pid=4242, started_at=4242000, proc_start="2026-11-17T08:00:00Z", session_id="sid",
        identity="id-a", loaded_identity="id-a",
        auras={
            "tmpl@nexus-develop": ls.AuraState(True, "loaded", "push-train"),
            "tmpl@group:down-train": ls.AuraState(False, "group_unreadable", "down-train"),
        },
    ))
    monkeypatch.setattr(aura_cmd, "_loaded_inputs", lambda: (adir, procs, 4242, "id-a", None))
    res = CliRunner().invoke(aura_group, ["list"])
    row = next(ln for ln in res.output.splitlines() if ln.startswith("tmpl"))
    assert "loaded for nexus-develop (push-train); not read: group down-train (reconnect)" in row, row


# -- group order --------------------------------------------------------------------------------------------------


def test_a_base_two_groups_serve_belongs_to_the_first_group_by_name_whatever_the_membership_order() -> None:
    both = _groups(**{"recording-rig-train": GroupRecord("recording-rig-train", serves=("nexus-develop", "recording-rig-main"))})
    for order in (("push-train", "recording-rig-train"), ("recording-rig-train", "push-train")):
        h = _Loader(memberships=order, groups=both)
        assert h.loader.decisions["tmpl@nexus-develop"] == AuraState(True, "loaded", "push-train"), order
        assert h.loader.decisions["tmpl@recording-rig-main"].group == "recording-rig-train"


# -- the pre-template blocks, batched and quiet -------------------------------------------------------------------


def _writer(content: str, puts: list[str]):
    from nexus.auras.threads_block import ThreadsBlockWriter  # noqa: PLC0415

    store = [{"title": "s", "tags": "", "content": content}]

    def put(title: str, text: str, tags: str) -> None:
        puts.append(text)
        store[0] = {"title": title, "tags": tags, "content": text}

    return ThreadsBlockWriter(_SID, read_entries=lambda: list(store), put=put), store


def test_both_pre_template_headings_leave_in_one_read_modify_write() -> None:
    puts: list[str] = []
    content = (
        f"session: {_SID}\n\n## Work\nref: x\n\n## aura: develop CI red\nstate: old\n\n## aura: develop CI nudge\nstate: old2\n\n"
        "## aura: develop CI red (nexus-develop)\nstate: new\n"
    )
    writer, store = _writer(content, puts)
    assert writer.remove_blocks(LEGACY[::-1]) is True
    assert len(puts) == 1, "one write for both headings"
    text = store[0]["content"]
    assert "state: old" not in text and "## aura: develop CI nudge" not in text
    assert "## aura: develop CI red (nexus-develop)\nstate: new" in text and "## Work" in text
    assert writer.remove_blocks(LEGACY) is False and len(puts) == 1, "nothing left: no second write"


def test_a_session_with_no_threads_entry_logs_no_warning_when_the_old_blocks_are_retired() -> None:
    writer, _ = _writer("title only, no session line\n", [])
    with structlog.testing.capture_logs() as logs:
        assert writer.remove_blocks(LEGACY) is False
    assert [e for e in logs if e["event"] == "aura_threads_no_entry"] == []


def test_the_runtime_retires_through_one_batched_call_when_the_writer_has_one() -> None:
    from nexus.auras.runtime import AuraRuntime  # noqa: PLC0415

    calls: list[object] = []

    class _Writer:
        def set_block(self, *a) -> bool:
            return True

        def remove_block(self, aura: str, threads_value: str) -> bool:
            calls.append(("one", aura))
            return True

        def remove_blocks(self, pairs) -> bool:
            calls.append(("batch", sorted(pairs)))
            return True

    rt = AuraRuntime([], writer=_Writer(), run=lambda *a, **k: {}, identity="i7")
    rt.retire_blocks(LEGACY)
    assert rt.wait_idle(10) and calls == [("batch", LEGACY)]


def test_a_raising_batched_writer_is_logged_by_name_and_never_raised() -> None:
    from nexus.auras.runtime import AuraRuntime  # noqa: PLC0415

    class _Writer:
        def set_block(self, *a) -> bool:
            return True

        def remove_blocks(self, pairs) -> bool:
            raise RuntimeError("t2 down")

    rt = AuraRuntime([], writer=_Writer(), run=lambda *a, **k: {}, identity="i7")
    with structlog.testing.capture_logs() as logs:
        rt.retire_blocks(LEGACY)
        assert rt.wait_idle(10)
    assert [e["event"] for e in logs].count("aura_legacy_blocks_not_removed") == 1


def test_a_writer_without_a_batch_call_is_retried_pair_by_pair_and_one_failure_spares_the_rest() -> None:
    from nexus.auras.runtime import AuraRuntime  # noqa: PLC0415

    seen: list[str] = []

    class _Writer:
        def set_block(self, *a) -> bool:
            return True

        def remove_block(self, aura: str, threads_value: str) -> bool:
            seen.append(aura)
            if aura == "ci_develop_nudge":
                raise RuntimeError("t2 down")
            return True

    rt = AuraRuntime([], writer=_Writer(), run=lambda *a, **k: {}, identity="i7")
    with structlog.testing.capture_logs() as logs:
        rt.retire_blocks(LEGACY)
        assert rt.wait_idle(10)
    assert seen == ["ci_develop_nudge", "ci_develop_red"], "the second pair is still tried"
    assert [e["event"] for e in logs].count("aura_legacy_block_not_removed") == 1


def _bindable_loader(*, first_aura=None):
    runtimes: list = []
    bound: list[str | None] = [None]
    shipped = [auras.load_aura(n, cwd=None) for n in ("ci_develop_red", "ci_develop_nudge")]

    class _Rt(_RetiringRuntime):
        def __init__(self, loaded) -> None:
            super().__init__(loaded)
            self.extended: list[str] = []

        def extend(self, loaded, identity) -> None:
            self.extended += [a.name for a in loaded]

    def make(loaded):
        runtimes.append(_Rt(loaded))
        return runtimes[-1]

    loader = AuraLoader(
        _SID, SubscriptionSet(session_id=_SID), discover=lambda: [*(first_aura or []), *shipped], is_on=lambda _n: True,
        entry=lambda _sid: ("tamsin", 100), registered_owner=lambda: 100, make_runtime=make, attach=lambda _rt: None,
        identity=lambda: bound[0], memberships=lambda _who: ["push-train"], read_group=_groups().get, now=lambda: NOW,
    )
    return loader, runtimes


def test_a_late_bind_that_builds_the_runtime_retires_the_pre_template_blocks() -> None:
    loader, runtimes = _bindable_loader()
    loader.on_resolve("tamsin")
    assert runtimes == []
    loader.note_identity("i7")
    assert len(runtimes) == 1 and sorted(runtimes[0].retired) == LEGACY


def test_a_late_bind_that_extends_a_runtime_retires_the_pre_template_blocks() -> None:
    plain = auras.parse_aura(
        b'aura = "plain"\nsummary = "x"\nprocedure = "ci_red_notice"\n\n[trigger]\nsubspace = "board/topic"\n\n[display]\nthreads = "plain"\n',
        name="plain",
    )
    loader, runtimes = _bindable_loader(first_aura=[plain])
    loader.on_resolve("tamsin")
    assert len(runtimes) == 1 and runtimes[0].retired == [], "the ungated aura loaded; the templates wait for the bind"
    loader.note_identity("i7")
    assert runtimes[0].extended and sorted(runtimes[0].retired) == LEGACY


# -- the sample base, and a template that ends with no aura -------------------------------------------------------


def test_a_long_but_valid_threads_value_is_not_refused_in_a_session_with_no_group() -> None:
    long = TEMPLATE.replace('"{branch} CI red ({base})"', '"{base} ' + "x" * 70 + '"')
    template = {"name": "board/ci/<topic>", "dimensions": {"kind": {}, "sha": {}, "from": {}}, "doc": {"body": {"refresh": "", "reason": ""}}}
    [result] = check_auras(
        [AuraEntry("tmpl", "shipped", _parse(long))], cwd=None, templates=[template], read_group=lambda n: None,
        member_groups=lambda: [], now=NOW,
    )
    assert not [f for f in result.findings if f.key == "display.threads"], result.findings
    assert result.ok


def test_each_template_that_ends_with_no_aura_logs_its_own_serves_nothing() -> None:
    too_long = "x-" + "y" * 40
    groups = _groups(**{"push-train": GroupRecord("push-train", serves=(too_long,))})
    twin = _parse(TEMPLATE.replace('aura = "tmpl"', 'aura = "twin"').replace('threads = "{branch} CI red ({base})"', 'threads = "twin {base}"'), name="twin")
    with structlog.testing.capture_logs() as logs:
        h = _Loader(groups=groups, extra=[twin])
    assert h.loaded == []
    assert sorted(e["aura"] for e in logs if e["event"] == "aura_serves_nothing") == ["tmpl", "twin"]


# -- the command wiring of nx aura check --------------------------------------------------------------------------


def _check_command(tmp_path: Path, monkeypatch: pytest.MonkeyPatch, member_groups) -> str:
    from nexus.commands import aura_cmd  # noqa: PLC0415
    from nexus.commands.aura_cmd import aura_group  # noqa: PLC0415
    from nexus.lua_runtime import library  # noqa: PLC0415

    root = tmp_path / "shipped"
    root.mkdir(exist_ok=True)
    (root / "tmpl.aura.toml").write_text(TEMPLATE, encoding="utf-8")
    shipped_lua = Path(__file__).resolve().parents[2] / "src" / "nexus" / "procedures" / "ci_red_notice.lua"
    (root / "ci_red_notice.lua").write_bytes(shipped_lua.read_bytes())
    monkeypatch.setattr(library, "shipped_root", lambda: root)
    monkeypatch.delenv(library.REPO_ROOT_ENV, raising=False)
    monkeypatch.delenv("NX_AURAS_OFF", raising=False)
    monkeypatch.chdir(tmp_path)
    monkeypatch.setattr(aura_cmd, "_templates", lambda: (None, "no registry in this test"))
    monkeypatch.setattr(aura_cmd, "_group_reader", lambda: (lambda n: GroupRecord(n, serves=("nexus-develop",))))
    monkeypatch.setattr(aura_cmd, "_member_groups", lambda: member_groups)
    res = CliRunner().invoke(aura_group, ["check", "tmpl"])
    assert res.exit_code == 0, res.output
    return res.output


def test_nx_aura_check_hands_the_sessions_groups_to_the_template_check(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    served = _check_command(tmp_path, monkeypatch, lambda: ["push-train"])
    assert "skipped: dictionary" in served and "serving_group" not in served, served
    none = _check_command(tmp_path, monkeypatch, lambda: [])
    assert "warning: serving_group" in none and "belongs to no group" in none, none


def test_nx_aura_check_skips_the_serving_check_by_name_when_the_groups_cannot_be_read(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    from nexus.auras.load_gate import GroupUnreadable  # noqa: PLC0415

    def down():
        raise GroupUnreadable("this process holds no identity")

    out = _check_command(tmp_path, monkeypatch, down)
    assert "skipped: serving_group" in out or "serving_group" in out.split("skipped:", 1)[-1], out
