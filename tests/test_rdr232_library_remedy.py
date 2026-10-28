# SPDX-License-Identifier: AGPL-3.0-or-later
"""nexus-jypmj.16 (a): the remedy the not-reseeded warning names must be the one that clears the state.

The answer-kind gate runs in shadow (``library_not_reseeded``) while a builtin row a reseed can fix has no
declaration. The warning, the docs and the CHANGELOG said ``nx plan reseed`` clears it. Two things leave a row
undeclared through a reseed, and neither is cleared by running it again:

* the installed conexus rejects the shipped template (a ``seed load error``): the row is never rewritten, and
  only a build whose template validates writes it;
* the template's description is already another row's (``not reconciled ... description already belongs to
  library row id=N``): the seed loader refuses the write rather than overwrite that row, and the refusal stands
  until that row is gone.

These tests run the real seed loader over an in-memory library (the harness of ``tests/test_plan_answer_kind.py``)
and the real library check, so each claim in the remedy text is shown by what the code does. Tenant-free.
"""
from __future__ import annotations

import inspect
import shutil
from pathlib import Path

import pytest
from structlog.testing import capture_logs

from nexus.commands.catalog import seed_plan_templates
from nexus.commands.plan import plan
from nexus.plans import eligibility as el
from nexus.plans import seed_loader
from nexus.plans.seed_loader import load_seed_directory
from tests.test_plan_answer_kind import _BUILTIN_DIR, _pre_declaration_library


def _undeclared(lib) -> int:
    check = el.check_builtins(lib.rows.values())
    assert check is not None and check.checked > 0, "non-vacuity: the check examined the builtin rows"
    return check.undeclared


def test_a_description_another_row_holds_leaves_the_template_undeclared_through_a_reseed_until_that_row_is_deleted() -> None:
    lib = _pre_declaration_library()
    target = next(r for r in lib.rows.values() if r["name"] == "hybrid-factual-lookup")
    wanted = target["query"]
    target["query"] = "An older wording of the lookup template's description."  # the stored row predates a rewording
    holder_id = lib.save_plan(
        query=wanted, plan_json='{"steps": []}', outcome="success", tags="grown", project="", name="grown-clone",
        verb="lookup", scope="global", dimensions='{"scope":"global","strategy":"clone","verb":"lookup"}',
        default_bindings=None, parent_dims=None,
    )
    assert _undeclared(lib) == len(lib.rows) - 1

    result = load_seed_directory(_BUILTIN_DIR, library=lib, reconcile=True)
    assert [name for name, _ in result.protected] == ["hybrid-factual-lookup.yml"]
    assert f"library row id={holder_id}" in result.protected[0][1], "the reseed's own warning names the blocking row"
    assert _undeclared(lib) == 1, "every other builtin was declared by the reseed; this one was refused"

    again = load_seed_directory(_BUILTIN_DIR, library=lib, reconcile=True)
    assert [name for name, _ in again.protected] == ["hybrid-factual-lookup.yml"], "a second reseed changes nothing"
    assert _undeclared(lib) == 1, "so `nx plan reseed` alone does not clear it"

    assert lib.delete_plan(holder_id), "what `nx plan delete <id>` does to the row the warning named"
    cleared = load_seed_directory(_BUILTIN_DIR, library=lib, reconcile=True)
    assert cleared.protected == [] and cleared.errors == []
    assert _undeclared(lib) == 0


def test_a_template_the_installed_conexus_rejects_stays_undeclared_until_a_build_whose_template_validates(
    tmp_path: Path,
) -> None:
    lib = _pre_declaration_library()
    broken = tmp_path / "builtin"
    shutil.copytree(_BUILTIN_DIR, broken)
    (broken / "hybrid-factual-lookup.yml").write_text("name: hybrid-factual-lookup\ndimensions: {}\n", encoding="utf-8")

    result = load_seed_directory(broken, library=lib, reconcile=True)
    assert [Path(source).name for source, _ in result.errors] == ["hybrid-factual-lookup.yml"], "a seed load error"
    assert _undeclared(lib) == 1
    assert load_seed_directory(broken, library=lib, reconcile=True).errors, "running the reseed again does not clear it"
    assert _undeclared(lib) == 1

    fixed = load_seed_directory(_BUILTIN_DIR, library=lib, reconcile=True)  # the build whose template validates
    assert fixed.errors == [] and _undeclared(lib) == 0


def test_the_remedy_names_each_thing_that_clears_the_state_in_the_reseeds_own_words() -> None:
    remedy = el.LIBRARY_REMEDY
    # the plain case, and the two the reseed can fail to clear
    assert "nx plan reseed" in remedy and "nx plan delete" in remedy and "upgrade" in remedy
    # in the words the reseed prints, so the reader can match them to its output
    printed = inspect.getsource(seed_plan_templates)
    for phrase in ("seed load error", "not reconciled"):
        assert phrase in printed and f"'{phrase}'" in remedy, phrase
    assert "library row id=" in inspect.getsource(seed_loader) and "library row id=N" in remedy
    # and the commands it names exist
    assert {"reseed", "delete"} <= set(plan.commands)


def test_the_warning_carries_the_remedy_and_nothing_shorter() -> None:
    el._warned_libraries.clear()
    with capture_logs() as logs:
        el.warn_library_not_reseeded(el.BuiltinCheck(12, 1, ("default (id 3)",)))
    (event,) = [e for e in logs if e["event"] == "answer_kind_gate_shadowed_library_not_reseeded"]
    assert event["remedy"] == el.LIBRARY_REMEDY and event["undeclared_builtin_names"] == ["default (id 3)"]


@pytest.mark.parametrize("path", ["docs/configuration.md", "docs/cli-reference.md"])
def test_the_docs_do_not_promise_that_a_reseed_alone_clears_it(path: str) -> None:
    text = (Path(__file__).resolve().parents[1] / path).read_text(encoding="utf-8")
    # the three facts of LIBRARY_REMEDY, in the docs' words: both ways a reseed can leave the row, and the delete
    for needle in ("seed load error", "not reconciled", "library row id=N", "nx plan delete N"):
        assert needle in text, f"{path} does not say {needle!r} about library_not_reseeded"
