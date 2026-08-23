# SPDX-License-Identifier: AGPL-3.0-or-later
"""nexus-7zup9 follow-up (wave code review [23363] High): every eval-corpus
markdown file's YAML frontmatter must actually parse. Ten of nineteen
grader files shipped with unquoted ``input_match`` regexes that PyYAML
rejects; nothing else in the suite would ever have caught it, because the
eval runner is early-access and cannot run here. This is the harness-free
part of that proof: syntax, not semantics."""
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
    assert len(prompts) >= 15, f"only {len(prompts)} prompt files found"
    assert len(graders) >= 19, f"only {len(graders)} grader files found"


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
