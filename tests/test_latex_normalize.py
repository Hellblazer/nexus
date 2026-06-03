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
from nexus.pdf_extractor import _normalize_mineru_latex, normalize_latex_spacing


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
