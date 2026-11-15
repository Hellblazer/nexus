# SPDX-License-Identifier: AGPL-3.0-or-later
"""The on/off resolver of auras (RDR-249 Technical Design 3; bead nexus-mdbds.10.2, step 5).

Every aura ships off. ``auras: {<name>: on}`` in the global ``config.yml`` turns one on, the
per-repo ``.nexus.yml`` overrides that, and ``NX_AURAS_OFF`` (``all``, or a comma-separated
list of names) overrides both and can only turn auras off. Each decision carries the level
that made it: default, global, repo or env.
"""
from __future__ import annotations

from pathlib import Path
from typing import Any

import pytest

from nexus.auras import AuraConfigError, Enablement, is_enabled, resolve
from nexus.config import nexus_config_dir

NAMES = ("ci_develop_red", "other")


@pytest.fixture(autouse=True)
def _no_env_switch(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("NX_AURAS_OFF", raising=False)


@pytest.fixture
def repo(tmp_path: Path) -> Path:
    top = tmp_path / "repo"
    top.mkdir()
    return top


def _global(text: str) -> Path:
    path = nexus_config_dir() / "config.yml"
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text, encoding="utf-8")
    return path


def _repo(repo: Path, text: str) -> Path:
    path = repo / ".nexus.yml"
    path.write_text(text, encoding="utf-8")
    return path


def _resolve(repo: Path, **kw: Any) -> dict[str, tuple[str, str]]:
    """name to (state, level): the shape these tests pin."""
    return {n: ("on" if e.on else "off", e.level) for n, e in resolve(NAMES, repo_root=repo, **kw).items()}


def test_every_aura_is_off_by_default(repo: Path) -> None:
    assert _resolve(repo) == {n: ("off", "default") for n in NAMES}


def test_an_aura_is_on_when_global_config_says_so(repo: Path) -> None:
    _global("auras:\n  ci_develop_red: on\n")  # YAML reads a bare on as True
    got = _resolve(repo)
    assert got["ci_develop_red"] == ("on", "global")
    assert got["other"] == ("off", "default")


def test_the_string_forms_on_and_off_are_accepted(repo: Path) -> None:
    _global('auras:\n  ci_develop_red: "on"\n  other: "off"\n')
    got = _resolve(repo)
    assert got["ci_develop_red"] == ("on", "global")
    assert got["other"] == ("off", "global")


def test_off_in_repo_config_overrides_a_global_on(repo: Path) -> None:
    _global("auras:\n  ci_develop_red: on\n")
    _repo(repo, "auras:\n  ci_develop_red: off\n")
    assert _resolve(repo)["ci_develop_red"] == ("off", "repo")


def test_on_in_repo_config_overrides_a_global_off(repo: Path) -> None:
    _global("auras:\n  other: off\n")
    _repo(repo, "auras:\n  other: on\n")
    assert _resolve(repo)["other"] == ("on", "repo")


def test_a_repo_config_that_does_not_name_the_aura_leaves_the_global_decision(repo: Path) -> None:
    _global("auras:\n  ci_develop_red: on\n")
    _repo(repo, "auras:\n  other: on\n")
    got = _resolve(repo)
    assert got["ci_develop_red"] == ("on", "global")
    assert got["other"] == ("on", "repo")


def test_aura_off_all_turns_every_aura_off_over_a_config_on(repo: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    _global("auras:\n  ci_develop_red: on\n")
    _repo(repo, "auras:\n  other: on\n")
    monkeypatch.setenv("NX_AURAS_OFF", "all")
    assert _resolve(repo) == {n: ("off", "env") for n in NAMES}


def test_a_name_list_turns_only_those_auras_off_over_a_config_on(repo: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    _global("auras:\n  ci_develop_red: on\n  other: on\n")
    monkeypatch.setenv("NX_AURAS_OFF", " ci_develop_red , unknown_name,, ")
    got = _resolve(repo)
    assert got["ci_develop_red"] == ("off", "env")
    assert got["other"] == ("on", "global")


def test_the_env_switch_never_turns_an_aura_on(repo: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("NX_AURAS_OFF", "other")
    got = _resolve(repo)
    assert got["ci_develop_red"] == ("off", "default")
    assert got["other"] == ("off", "env")


def test_an_environment_mapping_can_be_passed(repo: Path) -> None:
    _global("auras:\n  other: on\n")
    got = _resolve(repo, env={"NX_AURAS_OFF": "other"})
    assert got["other"] == ("off", "env")


def test_a_blank_env_switch_changes_nothing(repo: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    _global("auras:\n  other: on\n")
    monkeypatch.setenv("NX_AURAS_OFF", "  ")
    assert _resolve(repo)["other"] == ("on", "global")


def test_a_config_without_an_auras_section_decides_nothing(repo: Path) -> None:
    _global("llm:\n  backend: cli\n")
    _repo(repo, "")
    assert _resolve(repo) == {n: ("off", "default") for n in NAMES}


@pytest.mark.parametrize("value", ["'yes'", "1", "'maybe'", "2", "[on]", "null"])
def test_a_value_that_is_not_on_or_off_names_the_file_and_the_key(repo: Path, value: str) -> None:
    path = _global(f"auras:\n  ci_develop_red: {value}\n")
    with pytest.raises(AuraConfigError) as err:
        _resolve(repo)
    assert err.value.file == str(path)
    assert err.value.key == "auras.ci_develop_red"
    assert str(path) in str(err.value) and "auras.ci_develop_red" in str(err.value)


def test_a_bad_value_in_the_repo_file_names_the_repo_file(repo: Path) -> None:
    path = _repo(repo, "auras:\n  other: nope\n")
    with pytest.raises(AuraConfigError) as err:
        _resolve(repo)
    assert err.value.file == str(path)


def test_an_auras_section_that_is_not_a_mapping_is_refused(repo: Path) -> None:
    path = _repo(repo, "auras: [on]\n")
    with pytest.raises(AuraConfigError) as err:
        _resolve(repo)
    assert (err.value.file, err.value.key) == (str(path), "auras")


def test_a_config_file_that_is_not_yaml_is_refused_naming_it(repo: Path) -> None:
    path = _global("auras: {unclosed\n")
    with pytest.raises(AuraConfigError) as err:
        _resolve(repo)
    assert err.value.file == str(path)


def test_the_repo_config_is_read_from_the_working_directory_by_default(
    repo: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _repo(repo, "auras:\n  other: on\n")
    monkeypatch.chdir(repo)
    monkeypatch.delenv("NEXUS_REPO_ROOT", raising=False)
    assert resolve(NAMES)["other"].level == "repo"


def test_resolve_returns_an_enablement_for_every_name_with_its_source(repo: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    g = _global("auras:\n  ci_develop_red: on\n")
    r = _repo(repo, "auras:\n  other: on\n")
    got = resolve([*NAMES, "third"], repo_root=repo)
    assert set(got) == {*NAMES, "third"}
    assert got["ci_develop_red"] == Enablement("ci_develop_red", True, "global", str(g))
    assert got["other"] == Enablement("other", True, "repo", str(r))
    assert got["third"] == Enablement("third", False, "default", None)
    monkeypatch.setenv("NX_AURAS_OFF", "other")
    assert resolve(NAMES, repo_root=repo)["other"] == Enablement("other", False, "env", "NX_AURAS_OFF")


def test_resolve_of_no_names_is_empty(repo: Path) -> None:
    assert resolve([], repo_root=repo) == {}


def test_is_enabled_is_the_one_name_wrapper(repo: Path) -> None:
    _global("auras:\n  ci_develop_red: on\n")
    assert is_enabled("ci_develop_red", repo_root=repo) is True
    assert is_enabled("other", repo_root=repo) is False
    assert is_enabled("ci_develop_red", repo_root=repo, env={"NX_AURAS_OFF": "all"}) is False


def test_a_config_error_is_a_value_error() -> None:
    assert issubclass(AuraConfigError, ValueError)
