# SPDX-License-Identifier: AGPL-3.0-or-later
"""The first consumer as shipped: ``ci_red_notice.lua`` and ``ci_develop_red.aura.toml``
(RDR-249 Phase 3 Step 1, Technical Design 7, bead nexus-mdbds.12.1).

The procedure runs in the test-local harness ``tests/test_procedure_gate_record.py`` uses: the
environment the runtime builds from the static check's own allowlists, loaded in mode "t". The
aura tests read the real package root (``library.shipped_root``), not a planted copy, so a file
missing from ``src/nexus/procedures`` fails them. The dictionary check against a real engine's
registry for the shipped file is in ``test_registry_engine.py``; the wheel check is in
``tests/test_shipped_procedures_lint.py``. No engine, no tenant here.
"""
from __future__ import annotations

from pathlib import Path

import lupa.lua54 as lua54
import pytest

from nexus.auras import enumerate_auras, load_aura, parse_aura
from nexus.auras.check import check_auras
from nexus.auras.enable import resolve
from nexus.auras.threads_block import MAX_STATE_CHARS, sanitize_state
from nexus.lua_runtime import header as hd
from nexus.lua_runtime import library
from nexus.lua_runtime import static_check as sc

from ._support import TD1_EXAMPLE

ROOT = library.shipped_root()
SOURCE = (ROOT / "ci_red_notice.lua").read_bytes()
SHA = "8b30c161bdb23adab27171f3185c2b52f158b736"


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
    loaded = rt.eval("load")(SOURCE.decode("ascii"), "=ci_red_notice", "t", env)
    fn = loaded[0] if isinstance(loaded, tuple) else loaded
    return dict(fn(rt.table_from(args)).items())


# -- the procedure ----------------------------------------------------------------


def test_the_header_declares_sha_and_outcome_and_grants_nothing() -> None:
    h = hd.parse(SOURCE, name="ci_red_notice")
    assert [(a.name, a.type, a.optional) for a in h.args] == [("sha", "string", False), ("outcome", "string", False)]
    assert h.grants == frozenset()
    assert sc.check(SOURCE, h.grants) == frozenset()


def test_a_full_sha_shows_the_td7_text_with_its_short_form() -> None:
    out = _run(sha=SHA, outcome="failure")
    assert out["show"] is True
    assert out["text"] == "develop red at 8b30c16: diagnose before the next push (worktrees rule 7)"


def test_the_text_passes_the_threads_sanitiser_intact() -> None:
    text = _run(sha=SHA, outcome="failure")["text"]
    assert sanitize_state(text) == text
    assert len(text) <= MAX_STATE_CHARS


@pytest.mark.parametrize("bad", ["8B30C161", "8b30c1", "x" * 7, "8b30c161 ", "a" * 65])
def test_a_sha_that_is_not_7_to_64_lower_case_hex_is_refused(bad: str) -> None:
    with pytest.raises(lua54.LuaError, match="sha must be 7 to 64 lower-case hex digits"):
        _run(sha=bad, outcome="failure")


# -- the aura ---------------------------------------------------------------------


def test_the_shipped_aura_is_the_td1_example() -> None:
    shipped = (ROOT / "ci_develop_red.aura.toml").read_text(encoding="utf-8")
    assert shipped == TD1_EXAMPLE
    assert load_aura("ci_develop_red", cwd=None) == parse_aura(TD1_EXAMPLE.encode(), name="ci_develop_red")


def test_the_shipped_aura_passes_every_check_that_needs_no_engine(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv(library.REPO_ROOT_ENV, raising=False)
    entries = [e for e in enumerate_auras(cwd=None) if e.name == "ci_develop_red"]
    assert [e.root for e in entries] == ["shipped"], entries  # non-vacuity: the real package root
    [check] = check_auras(entries, cwd=None, templates=None)
    assert not check.findings, check.findings
    assert {s.check for s in check.skipped} == {"dictionary"}


def test_with_no_config_the_aura_is_off_by_default(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("NEXUS_CONFIG_DIR", str(tmp_path / "config"))
    repo = tmp_path / "repo"
    repo.mkdir()
    got = resolve(["ci_develop_red"], repo_root=repo, env={})["ci_develop_red"]
    assert (got.on, got.level) == (False, "default")


def test_the_repo_ships_no_config_that_turns_an_aura_on() -> None:
    repo_root = Path(__file__).resolve().parents[2]
    for name in (".nexus.yml", ".nexus.yaml"):
        path = repo_root / name
        if path.exists():
            assert "auras" not in path.read_text(encoding="utf-8"), f"{name} sets auras: they ship off (RDR-249 TD3)"
