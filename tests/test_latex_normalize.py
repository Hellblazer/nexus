# SPDX-License-Identifier: AGPL-3.0-or-later
"""Tests for normalize_latex_spacing — MinerU/UniMERNet spaced-token normalizer.

Closes #1049.

Rules under test:
- Collapse whitespace between a LaTeX command and its brace: \\mathbf { s } → \\mathbf{s}
- Collapse whitespace inside { … } groups within formula context
- Rejoin spaced single-char tokens inside \\operatorname*{...} and \\operatorname{...}
- \\{ \\bf X \\} → \\mathbf{X}
- Idempotent: running twice == running once
- Prose text with normal spacing is NOT mangled by the markdown-level wiring
"""
from pathlib import Path

from nexus.indexer_utils import resolve_pdf_title
from nexus.pdf_extractor import (
    PDFExtractor,
    _normalize_mineru_latex,
    _unwrap_mineru_font_tags,
    normalize_latex_spacing,
)


# ── exact issue examples (lock with ==) ───────────────────────────────────────

def test_example_1_mathbb_expectation() -> None:
    inp = r"\mathbb { E } _ { \mathbf { a } \sim \mu ( \mathbf { a } | \mathbf { s } ) } [ Q _ { \phi } ( \mathbf { s } , \mathbf { a } ) ]"
    expected = r"\mathbb{E}_{\mathbf{a}\sim\mu(\mathbf{a}|\mathbf{s})}[Q_{\phi}(\mathbf{s},\mathbf{a})]"
    assert normalize_latex_spacing(inp) == expected


def test_example_2_frac_advantage() -> None:
    inp = r"\frac { 1 } { \beta } A ^ { \mathcal { D } } ( \mathbf { s } , \mathbf { a } )"
    expected = r"\frac{1}{\beta}A^{\mathcal{D}}(\mathbf{s},\mathbf{a})"
    assert normalize_latex_spacing(inp) == expected


def test_example_3_arg_operatorname_max() -> None:
    inp = r"\arg \operatorname* { m a x } _ { \pi } J ( \pi , \hat { M } )"
    expected = r"\arg\operatorname*{max}_{\pi}J(\pi,\hat{M})"
    assert normalize_latex_spacing(inp) == expected


def test_example_4_propto_exp_bf() -> None:
    inp = r"\propto \exp ( \hat { Q } ^ { k } ( { \bf s } , { \bf a } ) )"
    expected = r"\propto\exp(\hat{Q}^{k}(\mathbf{s},\mathbf{a}))"
    assert normalize_latex_spacing(inp) == expected


# ── individual rules ───────────────────────────────────────────────────────────

def test_command_brace_space_collapsed() -> None:
    """\\mathbf { s } → \\mathbf{s}"""
    assert normalize_latex_spacing(r"\mathbf { s }") == r"\mathbf{s}"


def test_subscript_brace_space_collapsed() -> None:
    """Q _ { \\phi } → Q_{\\phi}"""
    assert normalize_latex_spacing(r"Q _ { \phi }") == r"Q_{\phi}"


def test_superscript_brace_space_collapsed() -> None:
    """A ^ { 2 } → A^{2}"""
    assert normalize_latex_spacing(r"A ^ { 2 }") == r"A^{2}"


def test_bf_group_normalized() -> None:
    """{ \\bf s } → \\mathbf{s}"""
    assert normalize_latex_spacing(r"{ \bf s }") == r"\mathbf{s}"


def test_operatorname_single_char_rejoin() -> None:
    """\\operatorname* { m a x } → \\operatorname*{max}"""
    assert normalize_latex_spacing(r"\operatorname* { m a x }") == r"\operatorname*{max}"


def test_operatorname_no_star_single_char_rejoin() -> None:
    """\\operatorname { m i n } → \\operatorname{min}"""
    assert normalize_latex_spacing(r"\operatorname { m i n }") == r"\operatorname{min}"


def test_operatorname_multi_char_tokens_not_rejoined() -> None:
    """\\operatorname{softmax} — already clean, not rejoined."""
    assert normalize_latex_spacing(r"\operatorname{softmax}") == r"\operatorname{softmax}"


def test_nested_braces_collapsed() -> None:
    """\\mathcal { D } inside outer braces."""
    assert normalize_latex_spacing(r"A ^ { \mathcal { D } }") == r"A^{\mathcal{D}}"


def test_text_group_preserved() -> None:
    """\\text{some words} must not have internal spaces stripped."""
    result = normalize_latex_spacing(r"x \in \text{some words}")
    assert r"\text{some words}" in result


# ── idempotency ────────────────────────────────────────────────────────────────

def test_idempotent_example_1() -> None:
    inp = r"\mathbb { E } _ { \mathbf { a } \sim \mu ( \mathbf { a } | \mathbf { s } ) } [ Q _ { \phi } ( \mathbf { s } , \mathbf { a } ) ]"
    once = normalize_latex_spacing(inp)
    twice = normalize_latex_spacing(once)
    assert once == twice


def test_idempotent_example_3() -> None:
    inp = r"\arg \operatorname* { m a x } _ { \pi } J ( \pi , \hat { M } )"
    once = normalize_latex_spacing(inp)
    twice = normalize_latex_spacing(once)
    assert once == twice


def test_idempotent_already_clean() -> None:
    """Already-clean LaTeX is unchanged by a second pass."""
    clean = r"\mathbb{E}_{\mathbf{a}\sim\mu(\mathbf{a}|\mathbf{s})}[Q_{\phi}(\mathbf{s},\mathbf{a})]"
    assert normalize_latex_spacing(clean) == clean


def test_idempotent_frac() -> None:
    clean = r"\frac{1}{\beta}A^{\mathcal{D}}(\mathbf{s},\mathbf{a})"
    assert normalize_latex_spacing(clean) == clean


# ── markdown wiring (_normalize_mineru_latex) ─────────────────────────────────

def test_prose_not_mangled() -> None:
    """Plain prose text is not modified by the markdown-level wiring."""
    prose = "The quick brown fox jumps over the lazy dog."
    assert _normalize_mineru_latex(prose) == prose


def test_prose_with_underscores_not_mangled() -> None:
    """Prose with underscores (e.g. identifiers) is not modified."""
    prose = "See section_3 for details about the_method."
    assert _normalize_mineru_latex(prose) == prose


def test_display_math_normalized_prose_untouched() -> None:
    """Formula inside $$ is normalized; surrounding prose is untouched."""
    md = (
        "We minimize the loss:\n\n"
        r"$$\frac { 1 } { \beta } A ^ { \mathcal { D } } ( \mathbf { s } , \mathbf { a } )$$"
        "\n\nwhere the terms are defined above."
    )
    result = _normalize_mineru_latex(md)
    assert r"$$\frac{1}{\beta}A^{\mathcal{D}}(\mathbf{s},\mathbf{a})$$" in result
    assert "We minimize the loss:" in result
    assert "where the terms are defined above." in result


def test_inline_math_normalized_prose_untouched() -> None:
    """Formula inside $ is normalized; surrounding prose is untouched."""
    md = r"The value $Q _ { \phi } ( \mathbf { s } , \mathbf { a } )$ is the Q-function."
    result = _normalize_mineru_latex(md)
    assert r"$Q_{\phi}(\mathbf{s},\mathbf{a})$" in result
    assert "The value" in result
    assert "is the Q-function." in result


def test_multiple_inline_formulas() -> None:
    """Multiple inline formulas in one line are each normalized."""
    md = r"Let $\mathbf { s }$ and $\mathbf { a }$ be state and action."
    result = _normalize_mineru_latex(md)
    assert r"$\mathbf{s}$" in result
    assert r"$\mathbf{a}$" in result
    assert "be state and action." in result


def test_normalize_mineru_latex_idempotent() -> None:
    """Applying _normalize_mineru_latex twice gives the same result as once."""
    md = r"The loss is $$\mathbb { E } _ { \mathbf { a } }$$ defined above."
    once = _normalize_mineru_latex(md)
    twice = _normalize_mineru_latex(once)
    assert once == twice


def test_no_formula_unchanged() -> None:
    """Markdown with no formula delimiters passes through unchanged."""
    md = "# Title\n\nSome text without any math."
    assert _normalize_mineru_latex(md) == md


# ── operatorname allowlist (substantive-critic fix) ──────────────────────────

def test_operatorname_unknown_single_chars_not_explicitly_rejoined() -> None:
    """\\operatorname{ a b } -- 'ab' not in allowlist -- rejoin path skipped.

    The whitespace-collapse rule (Rule 3) still strips spaces from brace
    content, producing \\operatorname{ab}.  This documents the conservative
    behavior: the allowlist blocks the *explicit* rejoin path; unknown content
    is still whitespace-collapsed.  Verified not a silent wrong-merge of a
    known name.
    """
    # 'ab' is not in _KNOWN_OPERATOR_NAMES
    result = normalize_latex_spacing(r"\operatorname { a b }")
    # Explicit rejoin skipped; whitespace collapse produces \operatorname{ab}
    assert result == r"\operatorname{ab}", f"got {result!r}"


def test_operatorname_known_names_still_rejoin() -> None:
    """Known operator names are still rejoined correctly."""
    assert normalize_latex_spacing(r"\operatorname { m i n }") == r"\operatorname{min}"
    assert normalize_latex_spacing(r"\operatorname* { m a x }") == r"\operatorname*{max}"
    assert normalize_latex_spacing(r"\operatorname { a r g m a x }") == r"\operatorname{argmax}"
    assert normalize_latex_spacing(r"\operatorname { s o f t m a x }") == r"\operatorname{softmax}"


def test_operatorname_unknown_multi_char_tokens_untouched() -> None:
    """Multi-char tokens inside \\operatorname are not joined by Rule 2."""
    # "softmax" as a single already-joined token -- idempotent
    assert normalize_latex_spacing(r"\operatorname{softmax}") == r"\operatorname{softmax}"


# ── page_boundaries consistency regression (code-review-expert fix) ──────────

def test_page_boundaries_consistent_with_normalized_text() -> None:
    """page_boundaries char offsets must match the normalized text length.

    Regression for the drift bug: when normalization was applied AFTER
    per_page_lengths were captured, page offsets referenced pre-normalization
    lengths.  The fix moves normalization into the batch loop so
    per_page_lengths and page_boundaries are always consistent with the
    stored text.  This test validates the invariant at the
    _mineru_build_result level.
    """
    # Two pages of MinerU markdown with formula blocks
    page0_raw = r"Some text. $$\mathbb { E } _ { \mathbf { a } }$$"
    page1_raw = r"More text. $Q _ { \phi }$ is the Q-function."

    # Simulate what the batch loop now does: normalize BEFORE measuring length
    page0_norm = _normalize_mineru_latex(page0_raw)
    page1_norm = _normalize_mineru_latex(page1_raw)

    # Verify normalization actually shortened the text (test has real formulas)
    assert len(page0_norm) < len(page0_raw), "page0 should be shorter after normalization"
    assert len(page1_norm) < len(page1_raw), "page1 should be shorter after normalization"

    # Assemble the way _extract_with_mineru does
    md_text = page0_norm + "\n" + page1_norm

    # per_page_lengths uses normalized lengths (matching the fixed batch loop)
    per_page_lengths = [(0, len(page0_norm)), (1, len(page1_norm))]

    # Minimal pdf_info: 2 pages, no inline equations
    pdf_info: list[dict] = [
        {"para_blocks": []},
        {"para_blocks": []},
    ]

    result = PDFExtractor._mineru_build_result(
        Path("test.pdf"), md_text, [], pdf_info,
        per_page_lengths=per_page_lengths,
    )

    # The result text IS the md_text (no further normalization in build_result)
    assert result.text == md_text

    boundaries = result.metadata["page_boundaries"]
    assert len(boundaries) == 2

    # Sum of page_text_length values must equal len(result.text)
    # Each page gets +1 for the "\n" join separator, except the last
    total = sum(b["page_text_length"] for b in boundaries)
    assert total == len(result.text), (
        f"page_boundaries total {total} != text length {len(result.text)}. "
        f"Drift indicates normalization was applied after length measurement."
    )

    # Verify each page slice starts at the right offset and spans correct chars
    for b in boundaries:
        start = b["start_char"]
        length = b["page_text_length"]
        assert 0 <= start <= len(result.text)
        assert start + length <= len(result.text) + 1  # +1: last separator is 0


def test_page_boundaries_drift_with_prenorm_lengths() -> None:
    """Demonstrate that using pre-normalization lengths causes drift.

    This is the WRONG behavior that the fix prevents: if we pass
    pre-normalization lengths to _mineru_build_result, the total exceeds
    the actual text length.
    """
    page0_raw = r"Prose. $$\mathbb { E } _ { \mathbf { a } }$$"
    page1_raw = r"More. $Q _ { \phi }$"

    page0_norm = _normalize_mineru_latex(page0_raw)
    page1_norm = _normalize_mineru_latex(page1_raw)

    md_text = page0_norm + "\n" + page1_norm

    # Intentionally WRONG: use pre-normalization lengths (the old bug)
    wrong_per_page_lengths = [(0, len(page0_raw)), (1, len(page1_raw))]

    pdf_info: list[dict] = [{"para_blocks": []}, {"para_blocks": []}]

    result = PDFExtractor._mineru_build_result(
        Path("test.pdf"), md_text, [], pdf_info,
        per_page_lengths=wrong_per_page_lengths,
    )

    wrong_total = sum(b["page_text_length"] for b in result.metadata["page_boundaries"])
    # With pre-norm lengths, total EXCEEDS the actual text length (drift)
    assert wrong_total > len(result.text), (
        "Expected drift: pre-normalization lengths should exceed normalized text length"
    )


# ── nexus-gtltb: the inline pass must not re-enter $$ delimiters ─────────────
#
# _normalize_mineru_latex ran two passes over one string. The display pass left
# the `$$` delimiters in its output, so the inline pass — `\$([^$]+?)\$`, which
# cannot match the empty string between them — began matching ONE `$` INTO each
# pair. That consumed a delimiter and desynced every subsequent open/close, so
# from the first display block onward the regions treated as "math" were the
# PROSE BETWEEN formulas, and math regions get `re.sub(r"\s+", "", s)`.
#
# Measured on tests/fixtures/bft-to-smr.pdf: 918 characters removed, 592 of them
# (64%) word-spaces deleted from running prose, plus \n\n paragraph breaks and
# `##` headings. Live corpus: 535 catastrophic chunks, body text not references.


def test_prose_between_display_blocks_keeps_its_spaces() -> None:
    """The minimal repro. This is the whole bug in one line."""
    out = _normalize_mineru_latex(r"$$a + b$$ some prose here $c$ more prose $d$ end.")
    assert "some prose here" in out, out
    assert "more prose" in out, out


def test_display_block_does_not_desync_following_inline_math() -> None:
    """After a $$ block, `$x$` must still be recognised AS inline math.

    The desync did not merely damage prose — it shifted what counted as a
    formula, so real inline spans were skipped while prose was normalized.
    """
    out = _normalize_mineru_latex(r"$$\frac { 1 } { m }$$ text $Q _ { \phi }$ tail")
    assert "$$\\frac{1}{m}$$" in out, out
    assert "$Q_{\\phi}$" in out, out          # the inline span WAS normalized
    assert " text " in out and " tail" in out  # the prose was not


def test_paragraph_breaks_and_headings_survive() -> None:
    """The chunker's boundary signals must not be destroyed.

    \\n\\n and `##` were being collapsed along with the spaces, so downstream
    chunking lost its section boundaries as well as its word boundaries.
    """
    src = "$$E = mc^2$$\n\n## 1.2. A heading\n\nBody text follows here.\n"
    out = _normalize_mineru_latex(src)
    assert "\n\n## 1.2. A heading\n\n" in out, out
    assert "Body text follows here." in out, out


def test_multiple_display_blocks_do_not_compound() -> None:
    """Three blocks: the desync compounded, so later prose was worse hit."""
    src = r"$$a$$ one $$b$$ two $$c$$ three"
    out = _normalize_mineru_latex(src)
    for word in ("one", "two", "three"):
        assert f" {word} " in out or out.endswith(f" {word}"), (word, out)


def test_normalization_inside_display_blocks_still_happens() -> None:
    """The #1049 purpose must survive the fix — this is not a revert."""
    out = _normalize_mineru_latex(r"$$\operatorname* { m a x } _ { x }$$")
    assert out == r"$$\operatorname*{max}_{x}$$", out


def test_idempotent_across_the_display_boundary() -> None:
    src = r"$$\mathbf { s }$$ prose $Q _ { \phi }$ more"
    once = _normalize_mineru_latex(src)
    assert _normalize_mineru_latex(once) == once


def test_text_groups_keep_internal_spaces_inside_display() -> None:
    """\\text{...} protection must still work through the placeholder layer."""
    out = _normalize_mineru_latex(r"$$\text{some words} + x$$ and prose")
    assert r"\text{some words}" in out, out
    assert " and prose" in out, out


# ── nexus-cfy5k: the inline pass must not treat an ESCAPED dollar as a delimiter ─
#
# Same PROSE-not-formula inversion as nexus-gtltb, a different trigger, and it
# survived 57a392ff. MinerU emits literal currency as `\$`. The inline pass —
# `\$([^$]+?)\$` — does not honour the backslash escape, so the PROSE BETWEEN two
# `\$` occurrences is matched as an inline math span and gets
# `re.sub(r"\s+", "", s)` applied to it.
#
# Live corruption, catalog doc 1.14.23, chunk 375b2d1ad6ab17a1, written AFTER the
# gtltb fix landed:
#     contracts above \$1M.Iftheenvironmentisarelationaldatabasewithtablesfor...
#
# It takes a PAIR: a lone `\$` has no closing delimiter and is inert, which is why
# the class hid behind documents that mention money exactly once.


def test_prose_between_escaped_dollars_keeps_its_spaces() -> None:
    """The minimal repro. This is the whole bug in one line."""
    src = r"costs \$5 and then \$9 later."
    assert _normalize_mineru_latex(src) == src


def test_currency_prose_from_the_live_corpus() -> None:
    """The shape actually found corrupted in knowledge__semantic-operators."""
    src = (
        r"contracts above \$1M. If the environment is a relational database "
        r"with tables for suppliers, contracts, and locations, the target is "
        r"naturally SQL."
    )
    assert _normalize_mineru_latex(src) == src


def test_single_escaped_dollar_is_inert() -> None:
    """One `\\$` has no partner, so it must be a no-op — and must stay one."""
    src = r"a budget of \$5 million was approved."
    assert _normalize_mineru_latex(src) == src


def test_escaped_dollar_does_not_swallow_following_real_inline_math() -> None:
    """An escaped dollar must not consume the delimiter of a real formula.

    The failure mode is the gtltb one: shifting what counts as a formula, so
    real spans are skipped while prose is normalized.
    """
    out = _normalize_mineru_latex(r"it cost \$5 then $Q _ { \phi }$ tail")
    assert r"$Q_{\phi}$" in out, out          # the real span WAS normalized
    assert r"it cost \$5 then " in out, out   # the prose was not
    assert " tail" in out, out


def test_escaped_dollars_around_a_display_block() -> None:
    """Both protections must compose, not fight."""
    src = r"pay \$5 now $$a + b$$ or \$9 later and more prose"
    out = _normalize_mineru_latex(src)
    assert "$$a+b$$" in out, out
    assert r"pay \$5 now " in out, out
    assert r" or \$9 later and more prose" in out, out


def test_escaped_backslash_before_dollar_is_a_real_delimiter() -> None:
    r"""Backslash parity: `\\$x$` is a line break followed by real inline math.

    `\\` is an escaped backslash, so the `$` after it is NOT escaped. A naive
    `\\\$` match would misread it and protect a genuine delimiter.
    """
    out = _normalize_mineru_latex(r"line\\$Q _ { \phi }$ tail")
    assert r"$Q_{\phi}$" in out, out


def test_idempotent_with_escaped_dollars() -> None:
    src = r"costs \$5 and then \$9 later, with $x _ { i }$ inline."
    once = _normalize_mineru_latex(src)
    assert _normalize_mineru_latex(once) == once


def test_gtltb_control_still_clean_alongside_escapes() -> None:
    """The gtltb fix must not regress while cfy5k is fixed."""
    out = _normalize_mineru_latex(r"$$a + b$$ some prose here $c$ more prose $d$ end.")
    assert "some prose here" in out, out
    assert "more prose" in out, out


# ── MinerU small-caps <sub>/<sup> shredding (papers/2512.11001.pdf, 2026-08-19) ──
#
# MinerU renders ACM small-caps headings, run-in paragraph heads, and figure/
# table captions as letter-interleaved HTML subscript spans:
#   ``## 5<sub>.</sub>2 U<sub>n</sub>ifi<sub>e</sub>d C<sub>os</sub>t M<sub>o</sub>d<sub>e</sub>l<sub>s</sub>``
# Measured on that paper: 30/70 chunks and 17/35 headings carried the tags;
# the embedded tokens are word fragments, so heading retrieval is destroyed.
# MinerU emits real math as LaTeX, so HTML sub/sup in its markdown are font-
# size heuristics, not semantics — unwrapping keeps the inner text verbatim.


def test_unwrap_shredded_heading_exact() -> None:
    md = "## 5<sub>.</sub>2 U<sub>n</sub>ifi<sub>e</sub>d C<sub>os</sub>t M<sub>o</sub>d<sub>e</sub>l<sub>s</sub>"
    assert _unwrap_mineru_font_tags(md) == "## 5.2 Unified Cost Models"


def test_unwrap_caption_and_runin_head() -> None:
    md = (
        "Fi<sub>gure</sub> 2<sub>:</sub> P<sub>are</sub>t<sub>o</sub> f<sub>ron</sub>ti<sub>er</sub>\n"
        "T<sub>rans</sub>f<sub>orma</sub>ti<sub>ons.</sub> $G^{*}$ may differ.\n"
        "<sup>\\*</sup>Helium [29] caches outputs."
    )
    out = _unwrap_mineru_font_tags(md)
    assert out == (
        "Figure 2: Pareto frontier\n"
        "Transformations. $G^{*}$ may differ.\n"
        "\\*Helium [29] caches outputs."
    )


def test_unwrap_leaves_other_html_and_math_alone() -> None:
    md = "Let $x_{i}$ and <b>bold</b> and a <subtle> word stay."
    assert _unwrap_mineru_font_tags(md) == md


def test_unwrap_idempotent_and_case_insensitive() -> None:
    md = "CO<SUB>2</SUB> and x<Sup>2</Sup>"
    once = _unwrap_mineru_font_tags(md)
    assert once == "CO2 and x2"
    assert _unwrap_mineru_font_tags(once) == once


# ── PDF title resolution: H1 fallback before filename stem (2026-08-19) ──────

def test_resolve_pdf_title_prefers_extractor_then_h1_then_stem() -> None:
    p = Path("/data/2512.11001.pdf")
    body = "# Rethinking Query Optimization for Multi-Agent Systems [Vision]\n\nZoi Kaoudi\n"
    # extractor metadata wins
    assert resolve_pdf_title({"docling_title": "Docling Title"}, p, body) == "Docling Title"
    assert resolve_pdf_title({"docling_title": "", "pdf_title": "XMP Title"}, p, body) == "XMP Title"
    # MinerU path: both empty -> first H1 of the markdown body
    assert resolve_pdf_title({"docling_title": "", "pdf_title": ""}, p, body) == (
        "Rethinking Query Optimization for Multi-Agent Systems [Vision]"
    )
    # no H1 anywhere -> normalised stem
    assert resolve_pdf_title({}, p, "plain text only") == "2512.11001"
    assert resolve_pdf_title({}, Path("/d/attention-is-all.pdf"), "") == "Attention Is All"


def test_resolve_pdf_title_section_heading_h1_falls_to_stem() -> None:
    """A body whose first H1 is ``# Abstract``/``# 1 Introduction`` has no title
    heading; stamping that word as the catalog title would wedge it behind
    the curated-title stem-guard. Fall to the stem instead."""
    p = Path("/data/2512.11001.pdf")
    assert resolve_pdf_title({}, p, "# ABSTRACT\n\nWe study...\n# Real Title Later\n") == "2512.11001"
    assert resolve_pdf_title({}, p, "# 1 Introduction\n\nBody") == "2512.11001"
    assert resolve_pdf_title({}, p, "# Related Work:\n") == "2512.11001"
    # a genuine title H1 that merely CONTAINS such a word still wins
    assert resolve_pdf_title({}, p, "# Abstract Interpretation of Agents\n") == "Abstract Interpretation of Agents"
