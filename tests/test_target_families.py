# SPDX-License-Identifier: AGPL-3.0-or-later
"""RDR-189 P2 (nexus-z1d0l.2.3): the answer-object family registry.

The registry is source-controlled, versioned declarations plus a validating
read path. These tests pin the five invariants the bead names; every
declaration they read is a fixture under ``tests/fixtures/target_families/``
(no production family ships with this bead). Malformed cases are mutated
copies of a fixture, written to ``tmp_path``.

No storage is touched, so the whole module runs with
``NX_TEST_T2_SUBSTRATE=none``.
"""
from __future__ import annotations

import copy
import json
import tomllib
from pathlib import Path

import pytest

from nexus.plans.target_families import (
    ActivationRefused,
    FamilyDeclarationError,
    FamilyError,
    SlotObligation,
    activate,
    default_registry,
    interpret_run,
    load_registry,
)
from nexus.plans.target_families import _REQUIRED

FIXTURES = Path(__file__).parent / "fixtures" / "target_families"
REGISTRY = FIXTURES / "registry"
LINEAGE = "fixture-lineage"

REQUIRED_FIELDS = (
    "family_id",
    "family_version",
    "required_slots",
    "facets",
    "treatment_constraints",
    "witness_obligations",
    "revision",
    "status",
    "exemplars",
    "counterexamples",
    "disagreements",
    "observed_refs",
    "environment_capabilities",
)


def test_the_required_field_list_here_is_the_loaders() -> None:
    assert set(REQUIRED_FIELDS) == set(_REQUIRED)


# --------------------------------------------------------------------------
# fixture plumbing


def _base(family: str, name: str) -> dict:
    return tomllib.loads((REGISTRY / family / name).read_text(encoding="utf-8"))


def _scalar(value: object) -> str:
    if isinstance(value, bool):
        return "true" if value else "false"
    if isinstance(value, (int, float)):
        return repr(value)
    return json.dumps(value)


def _emit(data: dict) -> str:
    """The smallest TOML writer these fixtures need (the stdlib only reads)."""
    head: list[str] = []
    tail: list[str] = []
    for key, value in data.items():
        if isinstance(value, dict):
            tail.append(f"[{key}]\n" + "".join(f"{k} = {_scalar(v)}\n" for k, v in value.items()))
        elif isinstance(value, list) and value and all(isinstance(i, dict) for i in value):
            tail.extend(
                f"[[{key}]]\n" + "".join(f"{k} = {_scalar(v)}\n" for k, v in item.items())
                for item in value
            )
        elif isinstance(value, list):
            head.append(f"{key} = [{', '.join(_scalar(i) for i in value)}]")
        else:
            head.append(f"{key} = {_scalar(value)}")
    return "\n".join(head) + "\n\n" + "\n".join(tail)


def _write_tree(root: Path, files: dict[tuple[str, str], dict]) -> Path:
    for (family_dir, filename), data in files.items():
        target = root / family_dir
        target.mkdir(parents=True, exist_ok=True)
        (target / filename).write_text(_emit(data), encoding="utf-8")
    return root


def _lineage_v1() -> dict:
    return _base(LINEAGE, "v1.toml")


def _load_one(tmp_path: Path, data: dict, *, filename: str = "v1.toml", family_dir: str = LINEAGE):
    return load_registry(_write_tree(tmp_path / "reg", {(family_dir, filename): data}))


def _same_obligations(a, b) -> bool:
    return (
        a.required_slots == b.required_slots
        and a.witness_obligations == b.witness_obligations
        and a.treatment_constraints == b.treatment_constraints
        and a.revision == b.revision
    )


# --------------------------------------------------------------------------
# 1. the loader accepts only a validated declaration


def test_fixture_tree_loads_with_every_field_attached() -> None:
    registry = load_registry(REGISTRY)

    assert registry.family_ids() == (LINEAGE, "fixture-solo")
    v1 = registry.get(LINEAGE, 1)
    assert v1.family_version == 1
    assert v1.status == "active"
    assert v1.revision == "1" * 40
    assert v1.required_slots == (
        SlotObligation(name="decision", kind="claim"),
        SlotObligation(name="rationale", kind="text"),
    )
    assert [(w.slot, w.kind, w.on_missing) for w in v1.witness_obligations] == [
        ("decision", "quote", "requiresReview")
    ]
    assert [c.name for c in v1.treatment_constraints] == ["no-current-code-inference"]
    assert v1.facets == ("origin", "successor")
    assert v1.approval is not None and v1.approval.reviewer == "fixture-reviewer"
    assert v1.observed_refs == ("run:fixture-0001", "coverage:fixture-0001")
    assert v1.environment_capabilities == ("catalog", "t3-search")


@pytest.mark.parametrize("field", REQUIRED_FIELDS)
def test_a_missing_required_field_is_refused(tmp_path: Path, field: str) -> None:
    data = _lineage_v1()
    del data[field]

    with pytest.raises(FamilyDeclarationError) as err:
        _load_one(tmp_path, data)

    assert err.value.field == field
    assert err.value.path.name == "v1.toml"


@pytest.mark.parametrize("bad", [True, False, 0, -1, "1", 1.0, 1.5])
def test_family_version_must_be_a_positive_int_not_a_bool(tmp_path: Path, bad: object) -> None:
    data = _lineage_v1()
    data["family_version"] = bad

    with pytest.raises(FamilyDeclarationError) as err:
        _load_one(tmp_path, data)

    assert err.value.field == "family_version"


@pytest.mark.parametrize(
    ("field", "bad"),
    [
        ("family_id", ""),
        ("family_id", "Not A Dir"),
        ("family_id", "../escape"),
        ("revision", "main"),
        ("revision", "abc123"),
        ("revision", "G" * 40),
        ("required_slots", []),
        ("status", "approved"),
        ("facets", "origin"),
    ],
)
def test_invalid_field_values_are_refused(tmp_path: Path, field: str, bad: object) -> None:
    data = _lineage_v1()
    data[field] = bad

    with pytest.raises(FamilyDeclarationError) as err:
        _load_one(tmp_path, data)

    assert err.value.field == field


def test_slot_kind_and_witness_on_missing_are_optional(tmp_path: Path) -> None:
    data = copy.deepcopy(_lineage_v1())
    del data["required_slots"][0]["kind"]
    del data["witness_obligations"][0]["on_missing"]

    declaration = _load_one(tmp_path, data).get(LINEAGE, 1)

    assert declaration.required_slots[0] == SlotObligation(name="decision", kind=None)
    assert declaration.required_slots[1].kind == "text"
    assert declaration.witness_obligations[0].on_missing is None


# --------------------------------------------------------------------------
# 2. no recorded approval, no activation


@pytest.mark.parametrize("status", ["active", "retired"])
def test_a_file_declaring_a_non_candidate_status_without_approval_is_refused(
    tmp_path: Path, status: str
) -> None:
    data = _lineage_v1()
    data["status"] = status
    del data["approval"]

    with pytest.raises(FamilyDeclarationError) as err:
        _load_one(tmp_path, data)

    assert err.value.field == "approval"
    assert "approval-not-recorded" in str(err.value)


def test_activating_a_candidate_without_approval_is_refused_and_changes_nothing() -> None:
    registry = load_registry(REGISTRY)

    with pytest.raises(ActivationRefused) as err:
        activate(registry, "fixture-solo", 1)

    assert err.value.reason == "approval-not-recorded"
    assert registry.get("fixture-solo", 1).status == "candidate"
    assert registry.active("fixture-solo") is None


@pytest.mark.parametrize(("family", "version"), [(LINEAGE, 1)])
def test_activating_a_non_candidate_is_refused(family: str, version: int) -> None:
    with pytest.raises(ActivationRefused) as err:
        activate(load_registry(REGISTRY), family, version)

    assert err.value.reason == "illegal-transition"


def test_activating_an_unknown_version_is_refused() -> None:
    with pytest.raises(FamilyError):
        activate(load_registry(REGISTRY), LINEAGE, 99)


def test_activating_a_version_below_the_active_one_is_refused_as_a_rollback(tmp_path: Path) -> None:
    older_candidate = _base(LINEAGE, "v2.toml")
    older_candidate["family_version"] = 1  # a reviewed candidate numbered below the active v2
    active_v2 = _lineage_v1()
    active_v2["family_version"] = 2
    root = _write_tree(tmp_path / "reg", {(LINEAGE, "v1.toml"): older_candidate, (LINEAGE, "v2.toml"): active_v2})
    registry = load_registry(root)

    with pytest.raises(ActivationRefused) as err:
        activate(registry, LINEAGE, 1)

    assert err.value.reason == "rollback-not-designed"
    assert "rollback" in str(err.value)
    assert registry.active(LINEAGE).family_version == 2


# --------------------------------------------------------------------------
# 3. counterexamples and disagreements stay attached


def test_counterexamples_and_disagreements_survive_load_and_activation() -> None:
    registry = load_registry(REGISTRY)
    v1 = registry.get(LINEAGE, 1)

    assert [(a.ref, a.note) for a in v1.counterexamples] == [
        ("run:fixture-0002", "Negative: a changelog summary is not a lineage answer.")
    ]
    assert [(a.ref, a.note) for a in v1.disagreements] == [
        ("run:fixture-0003", "Reviewers disagree whether a pure timeline request belongs here.")
    ]
    assert [a.ref for a in v1.exemplars] == ["run:fixture-0001"]

    # activation rebuilds the declaration (v1 is retired); what was attached stays attached
    after = activate(registry, LINEAGE, 2).get(LINEAGE, 1)
    assert after.counterexamples == v1.counterexamples
    assert after.disagreements == v1.disagreements
    assert after.exemplars == v1.exemplars

    # a second load of the same tree compares equal, field for field
    assert load_registry(REGISTRY).get(LINEAGE, 1) == v1


# --------------------------------------------------------------------------
# 4. versions coexist; an old run is read under its own version


def test_activating_v2_keeps_v1_readable_and_an_old_run_reads_v1_unchanged() -> None:
    registry = load_registry(REGISTRY)
    run = json.loads((FIXTURES / "runs" / "old-run.json").read_text(encoding="utf-8"))
    before = interpret_run(registry, run)
    assert before.family_version == 1

    activated = activate(registry, LINEAGE, 2)

    assert activated.active(LINEAGE).family_version == 2
    assert activated.get(LINEAGE, 2).status == "active"
    assert activated.get(LINEAGE, 1).status == "retired"
    assert [d.family_version for d in activated.versions(LINEAGE)] == [1, 2]

    after = interpret_run(activated, run)
    assert after.family_version == 1, "an old run must never be rerouted to the latest version"
    assert _same_obligations(after, before)
    assert after.required_slots == (
        SlotObligation(name="decision", kind="claim"),
        SlotObligation(name="rationale", kind="text"),
    )

    # the registry that was activated from is untouched
    assert registry.active(LINEAGE).family_version == 1


def test_interpreting_a_run_never_falls_back_to_another_version() -> None:
    registry = load_registry(REGISTRY)

    with pytest.raises(FamilyError):
        interpret_run(registry, {"family_id": LINEAGE, "family_version": 7, "contract_version": 3})
    with pytest.raises(FamilyError):
        interpret_run(registry, {"family_id": "no-such-family", "family_version": 1, "contract_version": 3})


@pytest.mark.parametrize("bad", [True, 0, "3", None])
def test_a_run_reference_must_carry_a_positive_int_contract_version(bad: object) -> None:
    run = {"family_id": LINEAGE, "family_version": 1, "contract_version": bad}

    with pytest.raises(FamilyError):
        interpret_run(load_registry(REGISTRY), run)


# --------------------------------------------------------------------------
# 5. malformed input fails loud


@pytest.mark.parametrize(
    ("mutate", "field"),
    [
        (lambda d: d["required_slots"][0].update(kind="bogus-kind"), "required_slots[0].kind"),
        (lambda d: d["witness_obligations"][0].update(kind="bogus-witness"), "witness_obligations[0].kind"),
        (lambda d: d["witness_obligations"][0].update(on_missing="bogus-state"), "witness_obligations[0].on_missing"),
        (lambda d: d["witness_obligations"][0].update(on_missing="preserved"), "witness_obligations[0].on_missing"),
        (lambda d: d["witness_obligations"][0].update(slot="not-a-slot"), "witness_obligations[0].slot"),
        (lambda d: d["required_slots"].append({"name": "decision", "kind": "text"}), "required_slots[2].name"),
        (lambda d: d["witness_obligations"].append(dict(d["witness_obligations"][0])), "witness_obligations[1].kind"),
        (lambda d: d["treatment_constraints"].append(dict(d["treatment_constraints"][0])), "treatment_constraints[1].name"),
        (lambda d: d.update(requird_slots=[]), "requird_slots"),
        (lambda d: d["required_slots"][0].update(extra="x"), "required_slots[0].extra"),
        (lambda d: d["approval"].pop("reviewer"), "approval.reviewer"),
    ],
)
def test_malformed_declarations_fail_loud_naming_file_and_field(tmp_path: Path, mutate, field: str) -> None:
    data = copy.deepcopy(_lineage_v1())
    mutate(data)

    with pytest.raises(FamilyDeclarationError) as err:
        _load_one(tmp_path, data)

    assert err.value.field == field
    assert err.value.path.name == "v1.toml"
    assert "v1.toml" in str(err.value) and field in str(err.value)


def test_a_toml_syntax_error_fails_loud_naming_the_file(tmp_path: Path) -> None:
    root = tmp_path / "reg"
    (root / LINEAGE).mkdir(parents=True)
    (root / LINEAGE / "v1.toml").write_text("family_id = \n", encoding="utf-8")

    with pytest.raises(FamilyDeclarationError) as err:
        load_registry(root)

    assert err.value.path.name == "v1.toml"


def test_a_filename_version_that_disagrees_with_the_content_is_refused(tmp_path: Path) -> None:
    with pytest.raises(FamilyDeclarationError) as err:
        _load_one(tmp_path, _lineage_v1(), filename="v2.toml")

    assert err.value.field == "family_version"
    assert err.value.path.name == "v2.toml"


def test_a_directory_name_that_disagrees_with_the_family_id_is_refused(tmp_path: Path) -> None:
    with pytest.raises(FamilyDeclarationError) as err:
        _load_one(tmp_path, _lineage_v1(), family_dir="some-other-family")

    assert err.value.field == "family_id"


def test_a_second_spelling_of_one_version_is_refused_as_a_stray_file(tmp_path: Path) -> None:
    """A duplicate (id, version) pair cannot arise: the directory fixes the id, the filename
    the version, and v01.toml is not a valid filename, so it never reaches a pair comparison."""
    root = _write_tree(
        tmp_path / "reg",
        {(LINEAGE, "v1.toml"): _lineage_v1(), (LINEAGE, "v01.toml"): _lineage_v1()},
    )

    with pytest.raises(FamilyDeclarationError) as err:
        load_registry(root)

    assert err.value.path.name == "v01.toml"
    assert err.value.field == "<file>"


def test_two_active_versions_of_one_family_are_refused(tmp_path: Path) -> None:
    v2 = _base(LINEAGE, "v2.toml")
    v2["status"] = "active"
    root = _write_tree(
        tmp_path / "reg",
        {(LINEAGE, "v1.toml"): _lineage_v1(), (LINEAGE, "v2.toml"): v2},
    )

    with pytest.raises(FamilyDeclarationError) as err:
        load_registry(root)

    assert err.value.field == "status"
    assert "active" in str(err.value)


def test_one_bad_file_refuses_the_whole_tree_rather_than_loading_the_rest(tmp_path: Path) -> None:
    bad = _base("fixture-solo", "v1.toml")
    del bad["status"]
    root = _write_tree(
        tmp_path / "reg",
        {(LINEAGE, "v1.toml"): _lineage_v1(), ("fixture-solo", "v1.toml"): bad},
    )

    with pytest.raises(FamilyDeclarationError):
        load_registry(root)


@pytest.mark.parametrize("stray", ["notes.toml", "v1.yml", "v.toml", "v-1.toml"])
def test_a_stray_file_in_a_family_directory_is_refused_not_skipped(tmp_path: Path, stray: str) -> None:
    root = _write_tree(tmp_path / "reg", {(LINEAGE, "v1.toml"): _lineage_v1()})
    (root / LINEAGE / stray).write_text("x = 1\n", encoding="utf-8")

    with pytest.raises(FamilyDeclarationError) as err:
        load_registry(root)

    assert err.value.path.name == stray


def test_dotfiles_are_ignored_at_the_root_and_in_family_directories(tmp_path: Path) -> None:
    root = _write_tree(tmp_path / "reg", {(LINEAGE, "v1.toml"): _lineage_v1()})
    (root / ".DS_Store").write_bytes(b"\x00")
    (root / LINEAGE / ".DS_Store").write_bytes(b"\x00")

    assert load_registry(root).family_ids() == (LINEAGE,)


@pytest.mark.parametrize("dotfile", [None, ".DS_Store"])
def test_a_family_directory_without_declarations_is_refused(tmp_path: Path, dotfile: str | None) -> None:
    root = _write_tree(tmp_path / "reg", {(LINEAGE, "v1.toml"): _lineage_v1()})
    (root / "empty-family").mkdir()
    if dotfile:
        (root / "empty-family" / dotfile).write_bytes(b"\x00")

    with pytest.raises(FamilyDeclarationError) as err:
        load_registry(root)

    assert err.value.path.name == "empty-family"
    assert err.value.field == "<dir>"


def test_a_missing_registry_root_is_refused(tmp_path: Path) -> None:
    with pytest.raises(FamilyDeclarationError):
        load_registry(tmp_path / "does-not-exist")


def test_the_packaged_registry_tree_loads() -> None:
    """The shipped tree under conexus/plans/target_families validates (it ships no
    family in this bead; nexus-z1d0l.2.5 adds the first reviewed ones)."""
    registry = load_registry()

    for family_id in registry.family_ids():
        assert registry.versions(family_id)
    assert default_registry() == registry
    # non-vacuity: the packaged tree is empty until nexus-z1d0l.2.5, so the loop above
    # proves nothing by itself; this proves the same loader reads a non-empty tree.
    assert load_registry(REGISTRY).declarations
