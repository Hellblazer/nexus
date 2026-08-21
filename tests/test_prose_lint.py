# SPDX-License-Identifier: AGPL-3.0-or-later
"""nexus-ptwm2: prose lint rules and the ratchet baseline.

Each rule here is backed either by a standing house rule (em dash,
"load-bearing") or by graded evidence in the research record
(T3 ``research-ai-slop-prose-removal-2026-08-21``). No readability
scores: the research grades them a trap.
"""
from __future__ import annotations

import json
from pathlib import Path

import pytest
from click.testing import CliRunner

from nexus.commands.prose import prose
from nexus.prose_lint import Finding, lint_text, load_baseline


def _rules(text: str) -> set[str]:
    return {f.rule for f in lint_text(text)}


# --- lexical -------------------------------------------------------------


def test_em_dash_is_flagged():
    assert _rules("The gate failed — twice.") == {"em-dash"}


def test_load_bearing_is_flagged_case_insensitively():
    assert "load-bearing" in _rules("This is a Load-Bearing assumption.")
    assert "load-bearing" in _rules("load bearing evidence")


@pytest.mark.parametrize(
    "word",
    ["delve", "delves", "Delving", "tapestry", "plethora", "meticulous",
     "seamlessly", "boasts", "leverage", "leveraging", "crucially"],
)
def test_marker_lexicon(word: str):
    assert "marker-lexicon" in _rules(f"We {word} into the data.")


def test_marker_lexicon_does_not_match_substrings():
    # "underscore" the noun (as in ``__``) is legitimate project prose.
    assert "marker-lexicon" not in _rules("Use a double underscore.")
    assert "marker-lexicon" not in _rules("The robust_client module.")


# --- syntactic -----------------------------------------------------------


def test_contrast_frame():
    assert "contrast-frame" in _rules(
        "It's not just a bug fix, it's a paradigm shift."
    )
    assert "contrast-frame" in _rules("This isn't just faster, it's cheaper.")
    assert "contrast-frame" in _rules("Not only faster, but cheaper.")


def test_contrast_frame_spares_plain_not_only_but_also():
    # A factual conjunction with no comma-anchored reveal is not the frame.
    assert "contrast-frame" not in _rules(
        "The client must not only page on count but also on bytes."
    )


def test_hedge_stack_splitter_keeps_abbreviations_inside_the_sentence():
    assert "hedge-stack" in _rules("It may help, e.g. it could cut cost.")


def test_unterminated_fence_masks_to_end_of_file():
    assert lint_text("Prose.\n\n```\ncode — delve\nmore — code\n") == []


def test_longer_fence_does_not_close_on_shorter_run():
    text = "````md\n```\ninner — fence\n```\nstill — inside\n````\nout.\n"
    assert lint_text(text) == []


def test_hedge_stack_needs_two_hedges_in_one_sentence():
    assert "hedge-stack" in _rules(
        "It could potentially be argued that this may sometimes help."
    )
    assert "hedge-stack" not in _rules(
        "This may help. It could also hurt."
    )


def test_vague_attribution():
    assert "vague-attribution" in _rules("Some experts argue the approach fails.")
    assert "vague-attribution" in _rules("Many researchers believe otherwise.")
    assert "vague-attribution" not in _rules("Williams (1990) argues the approach fails.")


# --- structural ----------------------------------------------------------


@pytest.mark.parametrize(
    "opener", ["In conclusion,", "In summary,", "Overall,", "To summarize,"]
)
def test_formulaic_closer_at_paragraph_start(opener: str):
    assert "formulaic-closer" in _rules(f"Body.\n\n{opener} the gate works.")


def test_formulaic_closer_not_flagged_mid_sentence():
    assert "formulaic-closer" not in _rules("The overall, aggregate count is 3.")


@pytest.mark.parametrize("opener", ["Great question!", "Certainly!", "I'd be happy to help."])
def test_sycophantic_opener(opener: str):
    assert "sycophantic-opener" in _rules(f"{opener} Here is the answer.")


# --- what the lint must skip ---------------------------------------------


def test_fenced_code_is_skipped():
    text = "Prose.\n\n```python\nx = 'a — b'  # delve\n```\n\nMore prose.\n"
    assert lint_text(text) == []


def test_inline_code_is_skipped():
    assert lint_text("Use `a — b` and `delve()` here.") == []


def test_frontmatter_is_skipped():
    text = "---\ntitle: a — b\n---\n\nClean body.\n"
    assert lint_text(text) == []


def test_html_comments_are_skipped():
    assert lint_text("<!-- delve — -->\nClean.\n") == []


def test_finding_carries_line_number_of_the_source_file():
    text = "---\nx: 1\n---\n\nLine five.\nLine six — bad.\n"
    (f,) = lint_text(text)
    assert isinstance(f, Finding)
    assert f.line == 6
    assert f.rule == "em-dash"


def test_clean_house_prose_has_no_findings():
    text = (
        "# Title\n\n"
        "The gate failed twice: once on the jar stamp, once on the lease.\n"
        "We assessed both. The second was a race in `SharedCluster`.\n\n"
        "- ***Stamp*** because the build ran against a stale tree.\n"
        "- ***Lease*** because two writers held the same path.\n"
    )
    assert lint_text(text) == []


# --- CLI + ratchet -------------------------------------------------------


def _write(p: Path, body: str) -> Path:
    p.write_text(body, encoding="utf-8")
    return p


def test_cli_exit_1_on_findings_and_reports_path_line_rule(tmp_path: Path):
    bad = _write(tmp_path / "bad.md", "ok\nbad — here\n")
    r = CliRunner().invoke(prose, ["lint", str(bad)])
    assert r.exit_code == 1
    assert f"{bad}:2: em-dash" in r.stderr


def test_cli_exit_0_when_clean(tmp_path: Path):
    good = _write(tmp_path / "good.md", "All clean.\n")
    r = CliRunner().invoke(prose, ["lint", str(good)])
    assert r.exit_code == 0, r.stderr


def test_cli_refuses_when_nothing_to_lint(tmp_path: Path):
    r = CliRunner().invoke(prose, ["lint", str(tmp_path)])
    assert r.exit_code == 2


def test_baseline_allows_counts_at_or_below_and_fails_above(tmp_path: Path):
    f = _write(tmp_path / "legacy.md", "a — b\nc — d\n")
    base = tmp_path / "base.json"
    base.write_text(json.dumps({"legacy.md": 2}))
    r = CliRunner().invoke(
        prose, ["lint", str(f), "--baseline", str(base), "--root", str(tmp_path)]
    )
    assert r.exit_code == 0, r.stderr

    f.write_text("a — b\nc — d\ne — f\n")
    r = CliRunner().invoke(
        prose, ["lint", str(f), "--baseline", str(base), "--root", str(tmp_path)]
    )
    assert r.exit_code == 1
    assert "3 findings > baseline 2" in r.stderr


def test_baseline_must_be_lowered_when_count_drops(tmp_path: Path):
    """A stale-high baseline is a lie about the state of the file. Same
    honesty rule as the plugin drift ledger: the ratchet only turns one
    way, and the recorded number must track reality."""
    f = _write(tmp_path / "legacy.md", "a — b\n")
    base = tmp_path / "base.json"
    base.write_text(json.dumps({"legacy.md": 5}))
    r = CliRunner().invoke(
        prose, ["lint", str(f), "--baseline", str(base), "--root", str(tmp_path)]
    )
    assert r.exit_code == 1
    assert "baseline 5 is stale" in r.stderr


def test_baseline_entry_for_missing_file_fails(tmp_path: Path):
    _write(tmp_path / "present.md", "clean\n")
    base = tmp_path / "base.json"
    base.write_text(json.dumps({"gone.md": 1}))
    r = CliRunner().invoke(
        prose, ["lint", str(tmp_path), "--baseline", str(base), "--root", str(tmp_path)]
    )
    assert r.exit_code == 1
    assert "gone.md" in r.stderr


def test_write_baseline_without_baseline_is_a_usage_error(tmp_path: Path):
    _write(tmp_path / "a.md", "clean\n")
    r = CliRunner().invoke(prose, ["lint", str(tmp_path), "--write-baseline"])
    assert r.exit_code == 2
    assert "requires --baseline" in r.stderr


def test_write_baseline_records_current_counts_per_file(tmp_path: Path):
    _write(tmp_path / "a.md", "x — y\n")
    _write(tmp_path / "b.md", "clean\n")
    base = tmp_path / "base.json"
    r = CliRunner().invoke(
        prose,
        ["lint", str(tmp_path), "--baseline", str(base), "--root", str(tmp_path),
         "--write-baseline"],
    )
    assert r.exit_code == 0, r.stderr
    assert load_baseline(base) == {"a.md": 1}
