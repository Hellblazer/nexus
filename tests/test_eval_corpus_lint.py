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

import os
import re
import subprocess
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
    # 11 cases / 13 graders since nexus-dkotg dropped n03, p07 and p08, and
    # nexus-cnzei.6 dropped p03. All four targeted a skill that is repo-local
    # and NOT part of the shipped conexus plugin: n03/p07/p08 targeted
    # .claude/skills/release and .claude/skills/engine-release; p03 targeted
    # test-authoring, moved to .claude/skills/test-authoring/ by nexus-cnzei.6
    # (it was nexus-repo-specific content — this repo's own test suite,
    # dev-loop layers, tests/AGENTS.md — shipping to every conexus plugin user
    # for no benefit outside this repo). The eval sandbox runs in a temp HOME,
    # so it never loads a repo-local skill: p07/p08 reported "Skill called 0x"
    # in every run of all three full runs, and n03 passed VACUOUSLY -- its
    # claim was never tested, which is also why nobody noticed the claim is
    # contestable (the release skill's own checklist step 4 is "Update both
    # changelogs", and n03's prompt asks for release notes). A $2.43 probe
    # confirmed relocating them does not help: targeting `.claude` resolves a
    # plugin, not the repo-local skills dir. p03's own grader (a positive
    # `tool_used` match on `conexus:test-authoring`) would fail the same way,
    # so it was deleted outright rather than left to fail vacuously; n04's
    # negative-absorption grader kept its test-validation half and dropped the
    # test-authoring alternative (same skill, no longer reachable through the
    # plugin's own Skill-tool surface at all).
    assert len(prompts) >= 11, f"only {len(prompts)} prompt files found"
    assert len(graders) >= 13, f"only {len(graders)} grader files found"


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


# ---------------------------------------------------------------------------
# nexus-dg7i6.7: outcome cases. The o-cases grade what the model produced
# (regex over the final message or a file, llm criteria) on a project their
# own scaffold builds, because the model rightly does those tasks without a
# skill (nexus-dg7i6.5: 0 skill loads in 12 eval runs and 509 real sessions).
# The pins below keep the scaffolds and the new grader kinds well formed; the
# corpus score itself is still measured by hand (see the module docstring).


def _case_dirs() -> list[Path]:
    return sorted(p for p in EVALS.iterdir() if p.is_dir() and (p / "prompt.md").exists())


def test_every_case_is_named_for_its_grading_shape():
    """p = a procedural skill must load, a = an agent must be dispatched, o = the outcome is
    graded, n = an absorption guard."""
    for case in _case_dirs():
        assert re.fullmatch(r"[paon]\d\d-[a-z0-9-]+", case.name), case.name
        graders = [_frontmatter(g) for g in (case / "graders").glob("*.md")]
        kinds = {g.get("type") for g in graders}
        if case.name.startswith("a"):
            assert any(g.get("tool") == "Agent" and g.get("min", 1) >= 1 for g in graders), (
                f"{case.name}: an agent case asserts a dispatch (tool_used on Agent, min >= 1)")
        if case.name.startswith("o"):
            assert kinds & {"regex", "llm"}, f"{case.name}: an outcome case needs a regex or llm grader"
            assert (case / "case.yaml").exists(), f"{case.name}: an outcome case builds its project with a scaffold"


@pytest.mark.parametrize("case", [c for c in _case_dirs() if (c / "case.yaml").exists()], ids=lambda p: p.name)
def test_case_yaml_names_an_executable_scaffold(case: Path):
    data = yaml.safe_load((case / "case.yaml").read_text(encoding="utf-8"))
    assert data.get("schema_version") == "1.1", case.name
    assert data.get("name"), case.name
    script = case / data["context"]["scaffold_script"]
    assert script.is_file() and script.stat().st_mode & 0o111, f"{case.name}: {script.name} missing or not executable"


@pytest.mark.parametrize("path", _grader_files(), ids=lambda p: p.relative_to(EVALS).as_posix())
def test_regex_and_llm_graders_are_complete(path: Path):
    data = _frontmatter(path)
    if data.get("type") == "regex":
        re.compile(data["pattern"], re.I if "i" in str(data.get("flags", "")) else 0)
        assert str(data.get("match", "contains")) in ("contains", "not_contains") or str(
            data["match"]).startswith("count:"), path.name
    if data.get("type") == "llm":
        assert len(str(data.get("criteria", ""))) > 40, f"{path.name}: llm criteria too thin to judge by"


@pytest.mark.parametrize("case", [c for c in _case_dirs() if (c / "fixture.sh").exists()], ids=lambda p: p.name)
def test_scaffold_is_deterministic(case: Path, tmp_path: Path):
    """Outcome graders read the scaffold's files, so two builds must be the same commit."""
    heads = []
    for i in (1, 2):
        d = tmp_path / str(i)
        d.mkdir()
        env = {"PATH": os.environ.get("PATH", "/usr/bin:/bin"), "HOME": str(d), "TERM": "dumb"}
        subprocess.run(["bash", str(case / "fixture.sh")], cwd=d, env=env, check=True,
                       capture_output=True, timeout=120)
        heads.append(subprocess.run(["git", "-C", str(d), "rev-parse", "HEAD"], check=True,
                                    capture_output=True, text=True).stdout.strip())
    assert heads[0] == heads[1], f"{case.name}: scaffold is not deterministic {heads}"


# Crafted answers each regex grader must pass and must fail (nexus-dg7i6.7 critique). A grader that
# passes a wrong or clearing answer, or misses a correct one in a common phrasing, fails here before
# a paid run. The fail lists hold the critic's counterexamples verbatim.
_REGEX_CONTROLS: dict[str, tuple[list[str], list[str]]] = {
    "o02-review-branch-before-pr/graders/finds-logged-api-key.md": (
        ["embed() logs the API key at INFO, so it lands in every log file.",
         "The Voyage API key is written to the logs on every call."],
        ["The key is read via os.environ.get('VOYAGE_API_KEY','') and the batching logic is fine.",
         "No secrets or credentials are logged anywhere in this diff."],
    ),
    "o02-review-branch-before-pr/graders/finds-shell-injection.md": (
        ["--cache-path goes straight into os.system, so a crafted path runs shell commands.",
         "Command injection: cache_path is concatenated into a shell string."],
        ["Replace os.system('chmod 666 ...') with os.chmod(path, 0o600).",
         "There are no injection concerns with --cache-path."],
    ),
    "o02-review-branch-before-pr/graders/finds-world-writable-cache.md": (
        ["The cache is chmod 666, so it is world-writable.",
         "Any local user can overwrite the cache file in /tmp."],
        ["The cache path defaults to /tmp."],
    ),
    "o04-retry-coverage-verdict/graders/says-coverage-is-not-enough.md": (
        ["I don't think the coverage is solid enough to open the PR yet.",
         "No, the coverage isn't enough.", "The coverage is weak: one happy-path test."],
        ["No reason to hold the PR, the retry path is solid.", "Coverage looks good to me."],
    ),
    "o05-intermittent-npe-race/graders/names-the-race.md": (
        ["The pool has no locking, so two threads take the same slot.",
         "It is a race between the check and the read in borrow()."],
        ["Here is the stack trace of the NPE.", "Embrace a bigger pool size."],
    ),
    "o01-voyage-retry-wrapper/graders/bounded-attempts.md": (
        ["for attempt in range(5):", "MAX_RETRIES = 4\nretries = 0", "for _ in range(3):", "@retry(stop=stop_after_attempt(3))"],
        ["while True:\n    try:\n        return post()"],
    ),
}


@pytest.mark.parametrize("rel", sorted(_REGEX_CONTROLS), ids=lambda r: r.split("/")[0])
def test_regex_grader_controls(rel: str) -> None:
    data = _frontmatter(EVALS / rel)
    pat = re.compile(data["pattern"], re.I if "i" in str(data.get("flags", "")) else 0)
    must_pass, must_fail = _REGEX_CONTROLS[rel]
    assert [t for t in must_pass if not pat.search(t)] == [], f"{rel}: misses a correct answer"
    assert [t for t in must_fail if pat.search(t)] == [], f"{rel}: passes a wrong or clearing answer"
