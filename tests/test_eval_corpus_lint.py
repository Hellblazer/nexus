# SPDX-License-Identifier: AGPL-3.0-or-later
"""nexus-7zup9 follow-up (wave code review [23363] High): every eval-corpus
markdown file's YAML frontmatter must actually parse. Ten of nineteen
grader files shipped with unquoted ``input_match`` regexes that PyYAML
rejects; nothing else in the suite would ever have caught it, because the
eval runner is early-access and cannot run here. This is the harness-free
part of that proof: syntax, not semantics.

CI DOES NOT RUN THE EVAL CORPUS, AND THAT IS A DECISION, NOT AN OVERSIGHT.
A grep of .github/workflows/ for ``plugin eval`` returns zero hits on
purpose. A path-filtered CI job running the corpus on changes to
``conexus/evals/**`` or ``conexus/skills/*/SKILL.md`` was proposed as
nexus-dkotg item (2) and DECLINED by Sam on 2026-08-23: invoking
``claude plugin eval`` on a runner means live model dispatch, i.e. a
recurring spend commitment on every touch of those paths. See nexus-dkotg
for the decision and the residual it leaves.

Read that residual before proposing the job again. What this module
catches is bound coherence and frontmatter syntax -- two failure modes.
The corpus SCORE stays unmeasured by CI, so a grader with coherent bounds
but a wrong matcher, a skill description that silently stops triggering,
or a runtime change to how focus/last_message reaches the judge would all
land the way nexus-dkotg did: found by a human running the corpus by hand.
That is the control today, and it is a person remembering, not a gate."""
from __future__ import annotations

import re
from pathlib import Path

import pytest
import yaml

pytestmark = pytest.mark.lint

REPO = Path(__file__).resolve().parents[1]
EVALS = REPO / "conexus" / "evals"


def _frontmatter_files() -> list[Path]:
    return sorted(p for p in EVALS.rglob("*.md") if p.name != "README.md")


def test_eval_corpus_population_is_real():
    files = _frontmatter_files()
    prompts = [p for p in files if p.name == "prompt.md"]
    graders = [p for p in files if p.parent.name == "graders"]
    # 12 cases / 14 graders since nexus-dkotg dropped n03, p07 and p08. Those
    # three targeted .claude/skills/release and .claude/skills/engine-release,
    # which are repo-local and NOT part of the shipped conexus plugin. The eval
    # sandbox runs in a temp HOME, so it never loaded them: p07/p08 reported
    # "Skill called 0x" in every run of all three full runs, and n03 passed
    # VACUOUSLY -- its claim was never tested, which is also why nobody noticed
    # the claim is contestable (the release skill's own checklist step 4 is
    # "Update both changelogs", and n03's prompt asks for release notes).
    # A $2.43 probe confirmed relocating them does not help: targeting
    # `.claude` resolves a plugin, not the repo-local skills dir.
    assert len(prompts) >= 12, f"only {len(prompts)} prompt files found"
    assert len(graders) >= 14, f"only {len(graders)} grader files found"


@pytest.mark.parametrize(
    "path", _frontmatter_files(), ids=lambda p: p.relative_to(EVALS).as_posix()
)
def test_frontmatter_parses(path: Path):
    text = path.read_text(encoding="utf-8")
    m = re.match(r"\A---\n(.*?)\n---\n", text, re.DOTALL)
    assert m, f"{path}: no frontmatter block"
    data = yaml.safe_load(m.group(1))
    assert isinstance(data, dict) and data, f"{path}: frontmatter is not a mapping"
    if path.parent.name == "graders":
        assert "type" in data, f"{path}: grader missing 'type'"


# ---------------------------------------------------------------------------
# nexus-dkotg: bound coherence. The checks above prove a grader's frontmatter
# PARSES; they say nothing about whether what it parses to can ever be true.
#
# The nexus-7zup9 rewrite (7.16.1) converted nine not-triggered graders from
# ``type: llm`` to ``type: tool_used`` with ``max: 0`` and did not set ``min``.
# ``min`` defaults to 1, so the runtime evaluated ``expected 1..0`` -- an
# unsatisfiable range. Every one of the nine reported "Skill called 0x
# (expected 1..0)" and FAILED, on runs where zero calls was the CORRECT
# outcome. Corpus score fell from 0.833 to 0.333 against the v7.16.0 baseline.
#
# The graders did not become merely wrong, they became CONSTANT: 0 calls is
# below min and 1 call is above max, so correct and broken behaviour produce
# the identical verdict. A grader with no discriminating power is worse than
# no grader, because it still occupies the slot where coverage is believed to be.
#
# Nothing caught it. The syntax lint above passes (``min: 1, max: 0`` is valid
# YAML carrying a ``type``), and no workflow invokes ``claude plugin eval``, so
# the only instrument that could observe the defect was optional and was not
# run. This check is the harness-free half of closing that: it costs nothing,
# needs no runner, and makes this specific class impossible at PR time.
#
# Defaults are the runner's, read from its own schema and confirmed against
# live report output ("expected 1..0", "expected 1..∞"):
#   min -> 1          (at least one matching call)
#   max -> infinity   (no upper bound)

_MIN_DEFAULT = 1
_MAX_DEFAULT = float("inf")


def _grader_files() -> list[Path]:
    return sorted(p for p in _frontmatter_files() if p.parent.name == "graders")


def _frontmatter(path: Path) -> dict:
    m = re.match(r"\A---\n(.*?)\n---\n", path.read_text(encoding="utf-8"), re.DOTALL)
    assert m, f"{path}: no frontmatter block"
    return yaml.safe_load(m.group(1))


@pytest.mark.parametrize(
    "path", _grader_files(), ids=lambda p: p.relative_to(EVALS).as_posix()
)
def test_grader_bounds_are_satisfiable(path: Path):
    """A grader's [min, max] must contain at least one integer.

    Applying the runner's OWN defaults, not the ones the file happens to
    spell out -- the defect this exists to stop was an omitted ``min``, so a
    check that only looked at declared keys would have passed it too.
    """
    data = _frontmatter(path)
    lo = data.get("min", _MIN_DEFAULT)
    hi = data.get("max", _MAX_DEFAULT)

    assert lo <= hi, (
        f"{path.relative_to(EVALS).as_posix()}: unsatisfiable bounds "
        f"min={lo} max={hi} (declared min={data.get('min', '<default 1>')}, "
        f"declared max={data.get('max', '<default inf>')}). No call count can "
        f"satisfy this, so the grader fails on CORRECT behaviour as readily as "
        f"on broken behaviour and carries no signal. If you meant 'this tool "
        f"must NOT be called', set BOTH min: 0 and max: 0 -- min defaults to 1."
    )


@pytest.mark.parametrize(
    "path",
    [p for p in _grader_files() if "not-triggered" in p.stem],
    ids=lambda p: p.relative_to(EVALS).as_posix(),
)
def test_not_triggered_graders_assert_absence(path: Path):
    """A grader named ``*-not-triggered`` must actually assert absence.

    Ties the corpus's naming convention to its assertion shape. The nine
    graders this bead repairs were all named for absence while asserting a
    range that absence could not satisfy; the name was the only honest part.
    """
    data = _frontmatter(path)
    name = path.relative_to(EVALS).as_posix()

    assert data.get("type") == "tool_used", (
        f"{name}: a not-triggered grader must be type: tool_used. "
        f"Got {data.get('type')!r}. An 'llm' grader cannot see tool calls at "
        f"all -- its focus defaults to last_message -- which is the defect "
        f"nexus-7zup9 was opened to fix."
    )
    assert data.get("max") == 0, (
        f"{name}: a not-triggered grader must set max: 0 (got {data.get('max')!r})."
    )
    assert data.get("min") == 0, (
        f"{name}: a not-triggered grader must set min: 0 EXPLICITLY "
        f"(got {data.get('min')!r}). min defaults to 1, which makes "
        f"'max: 0' alone unsatisfiable -- see nexus-dkotg."
    )
