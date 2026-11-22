# SPDX-License-Identifier: AGPL-3.0-or-later
"""``ci_red_nudge.lua`` and ``ci_develop_nudge.aura.toml`` (RDR-252 post-close audit S2, bead nexus-igea9).

Next up is shown the red and not woken (``ci_red_notice``): the owner usually acknowledges within two minutes, and a wake
for every red cost the conductor ten wakes in a night. The engine posts a ``kind=nudge`` row on the ``--owner`` post
when half the acknowledgement deadline has passed with no ack, naming next up in ``body.next``; this aura is what wakes
next up then. The engine cut that posts the row is a later one, so until it ships no nudge fires and next up is
display-only. The procedure runs in the same harness as tests/auras/test_ci_develop_red.py: no engine, no tenant.
"""
from __future__ import annotations

import lupa.lua54 as lua54
import pytest

from nexus.auras import enumerate_auras, load_aura, parse_aura
from nexus.auras.check import check_auras
from nexus.auras.threads_block import MAX_STATE_CHARS, sanitize_state
from nexus.lua_runtime import header as hd
from nexus.lua_runtime import library
from nexus.lua_runtime import static_check as sc

ROOT = library.shipped_root()
SOURCE = (ROOT / "ci_red_nudge.lua").read_bytes()
SHA = "8b30c161bdb23adab27171f3185c2b52f158b736"
SHORT = SHA[:7]
ME, OTHER = "i7", "i9"
OWNER_SUBSPACE = "board/ci/nexus-develop--owner"


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
    loaded = rt.eval("load")(SOURCE.decode("ascii"), "=ci_red_nudge", "t", env)
    fn = loaded[0] if isinstance(loaded, tuple) else loaded
    return dict(fn(rt.table_from(args)).items())


def _nudge(next_: str | None, me: str = ME, **extra: object) -> dict:
    out: dict = {"sha": SHA, "me": me, **extra}
    if next_ is not None:
        out["next"] = next_
    return out


def test_a_nudge_naming_me_shows_and_wakes() -> None:
    out = _run(**_nudge(ME))
    assert out["show"] is True and out["wake"] is True
    assert out["text"] == f"Develop is red at {SHORT}; no one has acknowledged it yet. You are next up."


def test_a_nudge_naming_another_session_neither_shows_nor_wakes() -> None:
    out = _run(**_nudge(OTHER))
    assert out["show"] is False and out["wake"] is False


def test_a_nudge_naming_no_one_neither_shows_nor_wakes() -> None:
    out = _run(**_nudge(None))
    assert out["show"] is False and out["wake"] is False


def test_the_text_passes_the_threads_sanitiser_intact() -> None:
    text = _run(**_nudge(ME))["text"]
    assert sanitize_state(text) == text and len(text) <= MAX_STATE_CHARS


@pytest.mark.parametrize(("field", "value"), [("next", "evil\nneeds_sam: yes"), ("sha", "XYZ"), ("me", "a b")])
def test_a_value_that_is_not_in_its_grammar_is_refused_without_echoing_it(field: str, value: str) -> None:
    args = _nudge(ME)
    args[field] = value
    with pytest.raises(lua54.LuaError) as err:
        _run(**args)
    assert value not in str(err.value)


def test_the_header_declares_the_fields_a_nudge_row_carries_and_grants_nothing() -> None:
    h = hd.parse(SOURCE, name="ci_red_nudge")
    assert [(a.name, a.type, a.optional) for a in h.args] == [
        ("sha", "string", False), ("next", "string", True), ("next_alias", "string", True), ("me", "string", False)]
    assert h.grants == frozenset()
    assert sc.check(SOURCE, h.grants) == frozenset()


# -- the aura ---------------------------------------------------------------------


def _shipped():
    return load_aura("ci_develop_nudge", cwd=None)


def test_the_aura_triggers_on_the_nudge_kind_of_the_owner_post_and_is_gated_on_push_train() -> None:
    aura = _shipped()
    assert aura.load_group == "push-train"
    assert aura.subspace == OWNER_SUBSPACE
    assert dict(aura.match) == {"kind": "nudge", "from": "engine"}
    assert dict(aura.clear_match or ()) == {"kind": "clear", "from": "engine"}
    assert aura.procedure == "ci_red_nudge" and aura.channel == "wake"
    assert {name: (spec.kind, spec.value) for name, spec in aura.args} == {
        "sha": ("dims", "sha"), "next": ("body", "next"), "next_alias": ("body", "next_alias"), "me": ("self", "identity")}


def test_the_aura_shows_in_a_block_of_its_own_so_it_never_overwrites_the_red_notice() -> None:
    assert _shipped().threads != load_aura("ci_develop_red", cwd=None).threads


def test_the_aura_passes_every_check_that_needs_no_engine(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv(library.REPO_ROOT_ENV, raising=False)
    entries = [e for e in enumerate_auras(cwd=None) if e.name in {"ci_develop_nudge", "ci_develop_red"}]
    assert sorted(e.name for e in entries) == ["ci_develop_nudge", "ci_develop_red"]  # non-vacuity: both, the real root
    for check in check_auras(entries, cwd=None, templates=None):
        assert not check.findings and not check.warnings, (check.name, check.findings, check.warnings)


def test_the_aura_file_is_the_parse_of_its_own_text() -> None:
    shipped = (ROOT / "ci_develop_nudge.aura.toml").read_text(encoding="utf-8")
    assert _shipped() == parse_aura(shipped.encode(), name="ci_develop_nudge")


# -- through the runtime: only an engine-written nudge fires, and a nudge to someone else writes no block ---------


class _Recorder:
    def __init__(self, result: dict) -> None:
        self.result, self.runs, self.sets, self.removes = result, [], [], []

    def run(self, name, args=None, limits=None):
        self.runs.append(dict(args or {}))
        return self.result

    def set_block(self, aura: str, threads_value: str, text: str) -> bool:
        self.sets.append(text)
        return True

    def remove_block(self, aura: str, threads_value: str) -> bool:
        self.removes.append(aura)
        return True


def _offer(dims: dict[str, str], body: str, result: dict, sender_session: str | None = None) -> _Recorder:
    from nexus.auras.runtime import AuraRuntime  # noqa: PLC0415
    from nexus.db.t2.records import TupleRow  # noqa: PLC0415

    aura = _shipped()
    rec = _Recorder(result)
    rt = AuraRuntime([aura], writer=rec, run=rec.run, identity=ME)
    rt.offer(aura.subspace, [TupleRow(
        id="t1", subspace=aura.subspace, template="", keys={}, dims=dims, body=body, claim_state=None, claimant=None,
        lease_until=None, attempts=0, consumed_at=None, consumed_by=None, expires_at=None, created_at=None,
        sender_session=sender_session, sender_session_known=True)])
    assert rt.wait_idle(30)
    return rec


def test_an_engine_nudge_naming_me_runs_the_procedure_with_the_row_and_my_identity() -> None:
    rec = _offer({"kind": "nudge", "sha": SHA, "from": "engine"}, '{"next": "i7"}', {"show": True, "text": "x", "wake": True})
    assert rec.runs == [{"sha": SHA, "next": ME, "me": ME}] and rec.sets == ["x"]


def test_a_result_that_does_not_show_writes_no_block() -> None:
    rec = _offer({"kind": "nudge", "sha": SHA, "from": "engine"}, '{"next": "i9"}', {"show": False, "text": "", "wake": False})
    assert len(rec.runs) == 1 and rec.sets == []


@pytest.mark.parametrize("writer", ["", "tamsin", "i9"])
def test_a_nudge_from_any_other_writer_fires_nothing(writer: str) -> None:
    dims = {"kind": "nudge", "sha": SHA, **({"from": writer} if writer else {})}
    rec = _offer(dims, '{"next": "i7"}', {"show": True, "text": "x", "wake": True})
    assert rec.runs == [] and rec.sets == []


def test_a_nudge_a_client_posted_as_the_engine_fires_nothing() -> None:
    rec = _offer({"kind": "nudge", "sha": SHA, "from": "engine"}, '{"next": "i7"}', {"show": True, "text": "x", "wake": True},
                 sender_session="5f0c1a2e-8b34-4d6f-9a10-0c2b7e4d9a31")
    assert rec.runs == [] and rec.sets == []


def test_a_clear_row_removes_the_nudge_block() -> None:
    rec = _offer({"kind": "clear", "sha": SHA, "from": "engine"}, "{}", {"show": True, "text": "x", "wake": True})
    assert rec.removes == ["ci_develop_nudge"] and rec.runs == []
