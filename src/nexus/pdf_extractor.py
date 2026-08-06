# SPDX-License-Identifier: AGPL-3.0-or-later
"""PDF text extraction with auto-detect math routing.

Extraction backends (selected by ``extractor`` param):
1. Docling — neural layout model for multi-column academic PDFs, Type3 fonts,
   and complex tables.  Enriched mode enables formula detection via FormulaItem.
2. MinerU — math-aware extraction. Default-installed since nexus-2fyb (was
   previously an optional ``[mineru]`` extra; the extras gate produced silent
   formula loss for weeks because fresh installs never picked it up). Used
   when auto mode detects formulas in the Docling probe pass.
3. PyMuPDF normalized — fallback for the explicit ``extractor='docling'``
   path when Docling itself fails.

Auto mode (default): non-enriched Docling probe → if formulas detected, route
to MinerU. If MinerU fails on a formula-bearing PDF, raise ``RuntimeError``
rather than silently returning the formula-stripped probe (the original
silent-corruption bug). Users who explicitly accept stripped extraction can
opt out with ``--extractor docling``.
"""
from collections.abc import Callable
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any
import json
import os
import re
import subprocess
import sys
import tempfile

import httpx
import structlog

from nexus.errors import ExtractionQualityError

try:
    from mineru.cli.common import do_parse
except ImportError:
    do_parse = None  # type: ignore[assignment]


# Inline script executed in a child Python process for memory isolation.
# Uses os._exit to force-terminate without waiting for daemon threads / worker
# pools that MinerU's pipeline may leave running.
#
# RDR-148 Gap 3 (macOS spawn-guard) — VERIFY-FIRST spike outcome, source-
# verified 2026-06-24: the original diagnosis (in-process MinerU worker failing
# on macOS under multiprocessing's `spawn` start method without an
# `if __name__ == "__main__"` guard — exit 1 + "leaked semaphore") is MOOT for
# the boundary it described. The worker is now a plain
# ``subprocess.Popen([sys.executable, "-c", _MINERU_WORKER_SCRIPT, ...])`` — a
# fresh interpreter, NOT a multiprocessing-spawn child, so the parent-__main__
# re-import recursion that the guard protects against is categorically
# inapplicable at the nexus->worker boundary. ``os._exit(0)`` further skips the
# pool teardown that leaked the semaphore. Residual, distinct, and UNVERIFIED:
# if MinerU's ``do_parse`` itself spawns multiprocessing children, the un-
# guarded ``-c`` __main__ could re-trigger an analogous issue; reproducing that
# needs model weights (CA-3/CA-4 deferred — do not run casually on a dev host).
# Do NOT add a speculative multiprocessing guard here without that repro: it is
# untested surface (feedback_no_preventive_scope_beyond_evidence). The
# fresh-interpreter `-c` form is a load-bearing invariant — see the structural
# guard in tests/test_mineru_spawn_logging.py.
# RDR-148 Gap 5: distinct exit code for an in-process MemoryError so the parent
# can classify a memory exhaustion (the RLIMIT_AS-ceiling-breach path added by
# Gap 6) separately from a generic non-zero exit. A bare RLIMIT_AS breach exits
# the worker via an in-process MemoryError (code path below), NOT the OS SIGKILL
# (-9) path, so the -9-only mapping would miss it (gate finding). Sentinel is
# substituted into the worker script template below.
_MINERU_OOM_EXIT = 42

_MINERU_WORKER_SCRIPT = '''
import json, sys, os
from pathlib import Path
from mineru.cli.common import do_parse

pdf_path, result_dir, start, end_str = sys.argv[1], sys.argv[2], int(sys.argv[3]), sys.argv[4]
end = None if end_str == "none" else int(end_str)
try:
    do_parse(
        result_dir,
        pdf_file_names=[Path(pdf_path).name],
        pdf_bytes_list=[Path(pdf_path).read_bytes()],
        p_lang_list=["en"],
        formula_enable=True,
        table_enable=True,  # Note: server path uses config (default False) — see RDR-046 RF-2
        start_page_id=start,
        end_page_id=end,
    )
except MemoryError:
    # RDR-148 Gap 5/6: a memory-ceiling breach (RLIMIT_AS) surfaces in-process as
    # MemoryError, not OS SIGKILL. Exit with the sentinel so the parent maps it to
    # MineruMemoryError. os._exit (not sys.exit) to skip pool/daemon-thread teardown.
    os._exit(__MINERU_OOM_EXIT__)
os._exit(0)
'''.replace("__MINERU_OOM_EXIT__", str(_MINERU_OOM_EXIT))

_log = structlog.get_logger(__name__)


class MineruMemoryError(RuntimeError):
    """A MinerU subprocess died from memory exhaustion (RDR-148 Gap 5).

    Subclasses ``RuntimeError`` deliberately: the existing
    ``except RuntimeError`` 1-page OOM-retry in ``_extract_with_mineru``
    keeps catching it, while callers that want to special-case memory
    exhaustion (per-page degrade-to-docling) can catch this narrower type.
    Raised when a worker exits via SIGKILL (OS OOM-killer / jetsam), via the
    ``_MINERU_OOM_EXIT`` sentinel (in-process ``MemoryError`` from an
    ``RLIMIT_AS`` ceiling breach), or any non-zero exit once a memory ceiling
    was applied (Gap 6). The third arm is gated on
    ``PDFExtractor._mineru_ceiling_applied`` (default ``False`` until Gap 6
    wires the ceiling), so it is inert today and never misfires.
    """


# nexus-2fyb code-review R1-I3: progress messages are interactive UX for
# long-running PDF extractions (Docling layout pass, MinerU per-page
# inference). They MUST also go through structlog so non-interactive
# callers (library use, MCP server, batch jobs) capture them in structured
# logs. Setting NEXUS_PDF_PROGRESS_QUIET=1 disables the stderr write
# entirely (e.g. for tests that capture stderr).
import os as _os


def _progress(msg: str) -> None:
    """Emit a progress event via structlog AND optionally to stderr.

    Stderr writes are gated by ``NEXUS_PDF_PROGRESS_QUIET`` env var so
    tests and library callers can suppress the interactive output without
    losing the structured log event. This replaces the prior plain
    ``print()`` which violated the project's no-print-in-library-code
    rule and made tests that captured stderr brittle.
    """
    _log.info("pdf_extractor_progress", message=msg.strip())
    if _os.environ.get("NEXUS_PDF_PROGRESS_QUIET") != "1":
        print(msg, file=sys.stderr, flush=True)  # noqa: T201 — gated interactive stderr progress; structured event emitted above via _log.info


# nexus-2fyb code-review R5-I2: chained exceptions from MinerU/httpx can
# include the configured pdf.mineru_server_url. If a user (mis-)configured
# that URL with embedded credentials (http://user:pass@host/...), those
# credentials would surface in error messages, structlog events, and
# downstream log sinks. Redact userinfo from any URL we surface.
_URL_CREDENTIALS_PATTERN = re.compile(
    r"(https?://)([^/\s@]+)@",  # capture scheme + userinfo segment
)


def _redact_url_credentials(text: str) -> str:
    """Replace ``http://user:pass@host`` with ``http://[redacted]@host`` in *text*.

    Used in error-message construction to avoid leaking credentials that
    a user may have configured into ``pdf.mineru_server_url``.
    """
    return _URL_CREDENTIALS_PATTERN.sub(r"\1[redacted]@", text)


# Block-style formula delimiters — counted once per block.
_FORMULA_BLOCK_PATTERNS: tuple[re.Pattern[str], ...] = (
    re.compile(r"\$\$.+?\$\$", re.DOTALL),                                  # $$...$$
    re.compile(r"\\\(.+?\\\)", re.DOTALL),                                  # \(...\)
    re.compile(r"\\\[.+?\\\]", re.DOTALL),                                  # \[...\]
    re.compile(r"\\begin\{equation\*?\}.+?\\end\{equation\*?\}", re.DOTALL),
    re.compile(r"\\begin\{align\*?\}.+?\\end\{align\*?\}", re.DOTALL),
)

# Command tokens — counted independently of containing blocks. Each
# occurrence inside or outside a block is one marker. This is intentional:
# the original nexus-2fyb bug shape was the alternation pattern below,
# which used a single re.findall and let `\$\$.+?\$\$` consume the whole
# block — `\frac` instances inside were never separately counted (4 markers
# returned for a paper with 12 \frac calls). Counting commands independently
# avoids that undercount and gives the routing decision a true signal.
#
# Patterns use \b (word boundary) rather than requiring an immediate `{`
# because MinerU emits LaTeX with whitespace between the command and its
# argument: ``\\frac { 1 } { m }`` (note spaces), so `\\frac\{` would
# match zero of those. \b matches between the word `\\frac` and the
# subsequent non-word character (space, `{`, `(`, etc.). This was an
# adjacent regression to the C1 bug — the regex assumed Docling-shaped
# LaTeX and silently undercounted on MinerU output.
_FORMULA_COMMAND_PATTERNS: tuple[re.Pattern[str], ...] = (
    re.compile(r"\\frac\b"),       # fraction
    re.compile(r"\\sum\b"),        # summation
    re.compile(r"\\int\b"),        # integral
    re.compile(r"\\prod\b"),       # product
    re.compile(r"\\partial\b"),    # partial derivative
    re.compile(r"\\nabla\b"),      # nabla/gradient
    re.compile(r"\\mathbb\b"),     # blackboard bold
    re.compile(r"\\mathcal\b"),    # calligraphic
)

# Unicode math symbols that indicate formula content in raw PDF text.
# These are present in the PDF's embedded text even without enrichment.
_MATH_UNICODE = frozenset("∑∫∏∀∃∈∉∪∩⊆⊇⊂⊃→←↔∧∨¬⇒⇔⇐∂∇≤≥≠±×÷√∞≈≡∝∅⊕⊗⊥∥")


def _count_formula_markers(text: str) -> int:
    """Count LaTeX formula markers in *text*.

    Used as the routing heuristic in auto-mode extraction; ``count >= 5``
    escalates to MinerU. The count is the sum of two independent measures:

    1. **Block delimiters** (``$$..$$``, ``\\(..\\)``, ``\\[..\\]``, equation
       and align environments) — each delimited block contributes 1.
    2. **Command tokens** (``\\frac``, ``\\sum``, ``\\int``, etc.) — each
       occurrence contributes 1, *including* occurrences inside delimited
       blocks. A ``$$..$$`` block with three ``\\frac`` calls inside
       therefore contributes 4 (1 block + 3 fracs).

    The deliberate double-counting of commands inside blocks is the fix for
    nexus-2fyb's adjacent bug shape: prior versions used one alternation
    pattern with ``re.findall``, which would consume the whole block as a
    single match and skip the commands inside, undercounting by an order
    of magnitude on math papers.
    """
    count = 0
    for pat in _FORMULA_BLOCK_PATTERNS:
        count += len(pat.findall(text))
    for pat in _FORMULA_COMMAND_PATTERNS:
        count += len(pat.findall(text))
    return count


def _has_formulas_quick(pdf_path: Path) -> int:
    """Quick formula detection via raw PDF text Unicode math symbols.

    Uses pymupdf to extract raw text (~0.1s) and counts Unicode math symbols.
    Returns the count. A threshold of >=5 indicates a formula-containing paper.
    """
    try:
        import pymupdf  # noqa: PLC0415 — deferred import — optional/heavy dependency, branch-local
        with pymupdf.open(pdf_path) as doc:
            count = 0
            for page in doc:
                text = page.get_text()
                count += sum(1 for c in text if c in _MATH_UNICODE)
                if count >= 5:
                    return count  # early exit
            return count
    except Exception:  # noqa: BLE001 — best-effort page count; falls back to 0
        return 0


def _normalize_whitespace_edge_cases(text: str) -> str:
    """Normalize whitespace variants not covered by basic normalization.

    - Replace tab characters with a single space.
    - Collapse Unicode non-breaking and exotic whitespace to a single space.
    - Collapse 4+ consecutive newlines to three (preserving intentional breaks).
    """
    text = text.replace("\t", " ")
    text = re.sub(r"[\u00A0\u1680\u2000-\u200A\u202F\u205F\u3000]+", " ", text)
    text = re.sub(r"\n{4,}", "\n\n\n", text)
    return text.strip()


# Known operator names that MinerU/UniMERNet may emit as spaced single chars
# inside \operatorname{...} or \operatorname*{...} -- e.g. ``{ m a x }`` -> max.
# The rejoin is scoped to this allowlist so genuinely separate tokens are not
# silently merged via the explicit rejoin path.  Unknown content still has its
# surrounding whitespace collapsed by Rule 3 (all-whitespace strip within formula
# context).
_KNOWN_OPERATOR_NAMES: frozenset[str] = frozenset({
    "arg", "argmax", "argmin", "cos", "deg", "det", "diag",
    "exp", "inf", "lim", "ln", "log",
    "max", "min", "rank", "sign", "sin", "softmax", "sup", "tan", "tr",
})


def normalize_latex_spacing(s: str) -> str:
    """Normalize MinerU/UniMERNet spaced-token LaTeX formula string.

    MinerU/UniMERNet emits formula LaTeX with spurious whitespace between
    commands and their tokens -- e.g. ``\\operatorname* { m a x }``,
    ``\\mathbf { s }``, ``Q _ { \\phi }``.  This normalizer collapses those
    spaces so stored chunks render correctly.

    Conservative rules (closes #1049):
    - ``{ \\bf X }`` -> ``\\mathbf{X}``
    - ``\\operatorname*{ m a x }`` / ``\\operatorname{ m i n }`` -- rejoin
      spaced single-char tokens when the joined result is in
      ``_KNOWN_OPERATOR_NAMES`` (e.g. ``max``, ``min``, ``argmax``).
      Unknown operatorname content is left to the whitespace-collapse step.
    - Collapse all remaining whitespace within the formula.
    - ``\\text{...}`` groups are protected: internal spaces are preserved.

    Idempotent: running twice produces the same result as running once.

    Designed for formula strings, not prose.  At the wiring level
    (``_normalize_mineru_latex``) this is called only within ``$...$`` and
    ``$$...$$`` delimiters so prose chunks are never touched.
    """
    # Rule 1: { \\bf token } -> \\mathbf{token}
    # Handles MinerU's {\\bf X} legacy-font group notation.
    s = re.sub(r"\{\s*\\bf\s+(\S+)\s*\}", r"\\mathbf{\1}", s)

    # Rule 2: \\operatorname*{ m a x } -> \\operatorname*{max}
    # Rejoin scoped to \\operatorname / \\operatorname* with an allowlist so that
    # genuinely separate single-char tokens are not silently merged.
    def _rejoin_opname(m: re.Match) -> str:
        cmd = m.group(1)
        content = m.group(2).strip()
        parts = content.split()
        if parts and all(len(p) == 1 for p in parts):
            joined = "".join(parts)
            if joined in _KNOWN_OPERATOR_NAMES:
                return f"{cmd}{{{joined}}}"
        return m.group(0)

    s = re.sub(r"(\\operatorname\*?)\s*\{([^}]+)\}", _rejoin_opname, s)

    # Protect \text{...} groups: save them as placeholders so whitespace
    # stripping below does not collapse spaces inside \text{some words}.
    _placeholders: list[str] = []

    def _save_text(m: re.Match) -> str:
        _placeholders.append(m.group(0))
        return f"\x00T{len(_placeholders) - 1}\x00"

    s = re.sub(r"\\text\{[^}]*\}", _save_text, s)

    # Rule 3: collapse all whitespace in the formula string.
    # Safe because this function is only called on formula content, not prose.
    s = re.sub(r"\s+", "", s)

    # Restore \text{...} groups with their original internal spacing.
    for i, t in enumerate(_placeholders):
        s = s.replace(f"\x00T{i}\x00", t)

    return s


def _normalize_mineru_latex(md: str) -> str:
    """Apply ``normalize_latex_spacing`` within LaTeX formula blocks in markdown.

    Scopes the normalizer to ``$$...$$`` (display math) and ``$...$`` (inline
    math) delimiters so that prose text is never modified.  Idempotent.

    Called from ``PDFExtractor._extract_with_mineru`` on each per-page
    markdown fragment (batch loop and OOM-retry path) before the length
    is measured for ``per_page_lengths``/``page_boundaries``, so the
    stored offsets are consistent with the normalized text.  Existing
    indexed chunks need a re-index to pick up clean LaTeX.
    """
    # Display math first, REPLACED BY A PLACEHOLDER rather than left in place.
    #
    # nexus-gtltb: the previous version substituted the display blocks but kept
    # their `$$` delimiters in the string, then ran the inline pass over the
    # result. `\$([^$]+?)\$` cannot match the empty string between a `$$` pair,
    # so it began matching ONE `$` INTO each pair — consuming a delimiter and
    # desyncing every subsequent open/close on the page. From the first display
    # block onward the spans treated as "math" were the PROSE BETWEEN formulas,
    # and math spans get `re.sub(r"\s+", "", s)` applied. The comment claiming
    # "[^$] avoids matching across $$ boundaries" was the exact inverse of what
    # the expression does.
    #
    # Measured on tests/fixtures/bft-to-smr.pdf: 918 chars removed, 592 of them
    # word-spaces deleted from running prose, plus \n\n breaks and `##`
    # headings — so the chunker lost its boundary signals too. The inversion ran
    # both ways: eight real inline spans kept the spurious spaces #1049 exists
    # to remove, while the prose around them lost its real ones.
    #
    # Placeholders are the same discipline `normalize_latex_spacing` already
    # uses for `\text{...}`, one level down.
    #
    # nexus-cfy5k: escaped dollars must be withdrawn from the string BEFORE
    # either delimiter pass, for the same reason. MinerU writes literal currency
    # as `\$`, and neither pass honours the backslash, so a PAIR of them bracketed
    # the prose between as if it were an inline formula — `contracts above
    # \$1M.Iftheenvironmentisarelationaldatabase...`. A lone `\$` has no partner
    # and is inert, which is why this hid behind documents that mention money
    # exactly once and survived the gtltb fix.
    #
    # The match keeps backslash parity: `\\$x$` is an escaped backslash followed
    # by a REAL delimiter, so `(?:\\\\)*` consumes complete pairs first and the
    # lookbehind stops the scan from starting mid-pair. Protecting a genuine
    # delimiter would resurrect the desync this is here to prevent.
    _escaped: list[str] = []

    def _save_escaped(m: re.Match) -> str:
        _escaped.append(m.group(0))
        return f"\x00E{len(_escaped) - 1}\x00"

    md = re.sub(r"(?<!\\)(?:\\\\)*\\\$", _save_escaped, md)

    _display: list[str] = []

    def _save_display(m: re.Match) -> str:
        _display.append(f"$${normalize_latex_spacing(m.group(1))}$$")
        return f"\x00D{len(_display) - 1}\x00"

    md = re.sub(r"\$\$(.*?)\$\$", _save_display, md, flags=re.DOTALL)

    # Inline math. NOW `[^$]` genuinely cannot cross a display boundary, because
    # no display delimiter remains in the string to be re-entered.
    md = re.sub(
        r"\$([^$]+?)\$",
        lambda m: f"${normalize_latex_spacing(m.group(1))}$",
        md,
    )

    # Restore display blocks, already normalized above.
    for i, block in enumerate(_display):
        md = md.replace(f"\x00D{i}\x00", block)

    # Restore escaped dollars verbatim — they were never math, so unlike the
    # display blocks there is nothing to normalize on the way back.
    for i, esc in enumerate(_escaped):
        md = md.replace(f"\x00E{i}\x00", esc)
    return md


@dataclass
class ExtractionResult:
    """Result of PDF text extraction."""

    text: str
    metadata: dict[str, Any] = field(default_factory=dict)


# ── Post-extraction quality gate (nexus-wi1uv) ──────────────────────────────
#
# Motivating incident (nexus-5xn3k, 2026-07-31): ``nx index pdf --extractor
# docling`` completed on a real 69-page paper and reported success while
# producing SPACE-STRIPPED text — "istheasetofthe", "aspartofasequenceofinput"
# — plus heavy raw-LaTeX noise. Nothing in the pipeline flagged it; the
# garbage was indexed, embedded, and paid for. docling is documented as the
# SAFE recovery when MinerU OOM-fails on formula-dense pages ("--extractor
# docling: formula-stripped, but always completes"). A loud MinerU failure is
# strictly safer than a silent quality collapse that then looks like success.
#
# Three cheap, dependency-free signals over the raw extracted text, computed
# once per document before chunking:
#   - whitespace_ratio  — fraction of characters that are whitespace. Space
#     stripping collapses this toward zero (words run together).
#   - mean_token_len     — mean length of whitespace-split tokens. Space
#     stripping merges many words into a few enormous "tokens"; explodes.
#   - long_token_fraction — fraction of tokens longer than
#     ``_LONG_TOKEN_CHARS`` characters. A second, more robust view of the
#     same run-together-words signature (robust to a doc with one or two
#     genuinely long tokens skewing the mean).
# All three are computed over `str.split()` tokens (whitespace-delimited) —
# no lexicon, no network, no new dependency, per the bead's dependency-free
# constraint.
#
# CALIBRATION (2026-08-06, round 2 — revised after code-review-expert +
# substantive-critic independently found the round-1 calibration unsafe
# on three axes; see nx memory get -p nexus -t nexus-wi1uv-implementation
# for the full evidence table, both rounds):
#
#   HEALTHY corpus (18 real/representative samples):
#     - 5 fixture PDFs (tests/fixtures/{bft-to-smr,tc-sql,
#       distributed-bloom-filter,virgo,fireflies-tocs}.pdf), extracted with
#       BOTH docling and PyMuPDF (10 whole-document samples).
#     - 2 dense-notation regions carved out of those same fixtures
#       (tc-sql's IES(FO) relational-calculus derivation; virgo's
#       cryptographic-commitment section) — real extractor output, not
#       synthetic, chosen specifically to probe the "dense math/formula
#       notation" false-positive hazard the bead names.
#     - 1 sample hydrated from T3 (knowledge__dt-papers): the Appendix H
#       attention-mechanism proofs from "Zoology" — nexus-5xn3k's OWN
#       incident paper, post-repair (a legitimate, heavy-LaTeX MinerU
#       extraction) — plus prose from knowledge__probe-mineru. This is the
#       exact document that motivated this bead, at its most formula-dense.
#     - 1 short (130-char) real-prose sample — round-2: the critic proved
#       a *short* extract needs the SAME protection as a long one; this is
#       its healthy control.
#     - 2 code-identifier-dense passages (systems-paper-with-algorithm-
#       listing prose, and pseudocode/algorithm-listing style, both built
#       from this repo's own real identifiers) — round-2: the critic's
#       Significant finding that dense identifiers can trip
#       long_token_fraction on LEGITIMATE text, the exact "dense-notation"
#       hazard class the bead itself named but round-1 only tested via
#       LaTeX.
#   Measured ranges (round-2): whitespace_ratio 0.0799-0.2395;
#   mean_token_len 4.38-13.12; long_token_fraction 0.0000-0.25. The
#   code-identifier samples set the long_token_fraction high (0.20-0.25)
#   and pulled whitespace_ratio down to its new low (0.0799) — round-1's
#   dense-LaTeX-only calibration had not exercised this axis at all.
#
#   NON-SPACED-SCRIPT (CJK) — round-2, substantive-critic Significant:
#   ``str.split()`` cannot segment Han/Hiragana/Katakana/Hangul text at
#   all (no inter-word ASCII spaces), so a real CJK document is
#   STRUCTURALLY IDENTICAL to the space-stripped-garbage signature on all
#   three signals (measured on a real Chinese-prose sample: whitespace_
#   ratio=0.0, mean_token_len=399, long_token_fraction=1.0 — i.e. it would
#   have failed unconditionally). Not a calibration-threshold problem —
#   the signals are simply not meaningful for these scripts. Detected via
#   :func:`_non_spaced_script_fraction` and SKIPPED (passed=True,
#   ``skipped_reason`` set, logged) rather than force-fit into a
#   threshold that cannot discriminate for this input class. See
#   :data:`_NON_SPACED_SCRIPT_FRACTION_THRESHOLD`.
#
#   GARBAGE corpus (14 samples): every healthy sample above with spaces
#   stripped per line (``line.replace(" ", "")`` — preserves line/paragraph
#   breaks, strips only intra-line word-boundary spaces), reproducing the
#   incident's reported signature verbatim (run this box's extractor over a
#   fixture, strip spaces, and the output reads exactly like
#   "istheasetofthe"). Includes the 130-char short sample stripped to 111
#   chars — round-2's boundary-behavior proof (see below). Measured
#   ranges: whitespace_ratio 0.0000-0.0218; mean_token_len 52.09-840.44 for
#   the >=20-token samples, mean_token_len=111.0 (n_tokens=1) for the short
#   one. long_token_fraction 0.3366-1.0 (code-identifier samples are NOT
#   in the garbage corpus — they are real legitimate text, calibrated as
#   healthy above).
#
# THRESHOLDS (round-2):
_WHITESPACE_RATIO_FLOOR = 0.05
_MEAN_TOKEN_LEN_CEILING = 20.0
# Round-2 (was 0.10): the critic reproduced a 0.42 long_token_fraction on
# synthetic code-identifier-dense text — a real false positive at the
# round-1 threshold. This repo's OWN identifiers, worked into realistic
# prose (not a worst-case "nothing but identifiers" adversarial sample),
# measured 0.20-0.25 (see HEALTHY corpus above) — comfortably under the
# revised 0.5 ceiling, itself still 2x above the critic's reported 0.42
# and ~20x above the real-dense-LaTeX healthy ceiling (0.0246). Raising
# this ceiling loses ZERO garbage-detection power: every garbage sample in
# the corpus is independently caught by whitespace_ratio AND/OR
# mean_token_len (verified — see the calibration script referenced in the
# T2 record), so long_token_fraction is corroborating, not load-bearing.
_LONG_TOKEN_FRACTION_CEILING = 0.5
_LONG_TOKEN_CHARS = 20
# Round-2 (substantive-critic Significant #4): the round-1 char-based
# floor (500 chars, auto-pass below it) reproduced the incident's own
# signature UNDETECTED at 169 chars — short garbage still yields >=1 real
# chunk, so "defer to the zero-chunks check downstream" was FALSE for
# this case, not just imprecise. Replaced with a TOKEN-count floor that
# applies ONLY to the two signals that need enough samples to be
# statistically meaningful (whitespace_ratio, long_token_fraction) — a
# single genuinely-long legitimate token (a URL, a DOI) can otherwise
# swing a ratio over few tokens by chance. mean_token_len does NOT wait
# for this floor: it is computed and compared whenever there is at least
# one token, because it is meaningful even at n=1 (a single 100+-char
# glued-together "word" is unambiguous regardless of how few tokens
# surround it) and its calibration margin (7x between the healthy ceiling
# 7.01 and the garbage floor 52.09, confirmed down to n_tokens=1 in the
# 169-char boundary case: mean_token_len=111.0) does not depend on a
# large sample. This is what makes the 169-char repro fail post-fix.
_MIN_TOKENS_FOR_RATIO_SIGNALS = 20

#: Non-spaced-script (CJK) codepoint ranges — round-2, substantive-critic
#: Significant #5. Han (+ CJK Ext-A), Hiragana, Katakana, Hangul: scripts
#: written without inter-word ASCII spaces, where str.split() cannot
#: segment tokens at all.
_NON_SPACED_SCRIPT_RANGES: tuple[tuple[int, int], ...] = (
    (0x4E00, 0x9FFF),   # CJK Unified Ideographs
    (0x3400, 0x4DBF),   # CJK Unified Ideographs Extension A
    (0x3040, 0x309F),   # Hiragana
    (0x30A0, 0x30FF),   # Katakana
    (0xAC00, 0xD7A3),   # Hangul Syllables
)

#: Fraction of non-whitespace characters that must fall in a non-spaced
#: script before the whitespace/token signals are skipped as unreliable.
#: A real Chinese-prose calibration sample measured ~1.0 (essentially
#: every non-whitespace char is Han); 0.3 leaves wide margin for a mixed
#: CJK+Latin document (e.g. English section headers in an otherwise-CJK
#: paper) while still catching a CJK-dominant one.
_NON_SPACED_SCRIPT_FRACTION_THRESHOLD = 0.3


def _non_spaced_script_fraction(text: str) -> float:
    """Fraction of non-whitespace characters in *text* belonging to a
    script conventionally written without inter-word spaces (CJK).

    Pure/cheap — a single pass over the text, no dependency. Returns 0.0
    for empty or all-whitespace input.
    """
    non_ws_count = 0
    hits = 0
    for c in text:
        if c.isspace():
            continue
        non_ws_count += 1
        cp = ord(c)
        if any(lo <= cp <= hi for lo, hi in _NON_SPACED_SCRIPT_RANGES):
            hits += 1
    return hits / non_ws_count if non_ws_count else 0.0


@dataclass
class ExtractionQualityReport:
    """Cheap post-extraction text-quality signals (nexus-wi1uv).

    See the module-level comment above :data:`_WHITESPACE_RATIO_FLOOR` for
    the calibration evidence behind the thresholds ``passed`` is computed
    from.
    """

    whitespace_ratio: float
    mean_token_len: float
    long_token_fraction: float
    n_tokens: int
    n_chars: int
    passed: bool
    failing_signals: list[str] = field(default_factory=list)
    #: Round-2: non-empty when the signals were skipped as unreliable for
    #: this input (currently: non-spaced-script/CJK dominance) rather than
    #: evaluated and passed. ``passed`` is always True when this is set —
    #: a capability-honest skip, never treated as a silent clean pass of
    #: an actually-evaluated document.
    skipped_reason: str = ""


def assess_extraction_quality(text: str) -> ExtractionQualityReport:
    """Compute the post-extraction quality signals for *text*.

    Pure function — no I/O, no dependency beyond the stdlib — so it is
    testable in isolation from any PDF backend. Called once per document on
    the full extracted text, before chunking.
    """
    n_chars = len(text)
    if n_chars == 0:
        # Nothing to judge; the separately-enforced "chunker produced zero
        # chunks despite non-empty text" check (doc_indexer._pdf_chunks)
        # covers genuinely empty extraction.
        return ExtractionQualityReport(
            whitespace_ratio=1.0, mean_token_len=0.0, long_token_fraction=0.0,
            n_tokens=0, n_chars=0, passed=True, failing_signals=[],
        )

    tokens = text.split()
    n_tokens = len(tokens)

    script_frac = _non_spaced_script_fraction(text)
    if script_frac >= _NON_SPACED_SCRIPT_FRACTION_THRESHOLD:
        # Round-2: whitespace/token signals are structurally meaningless
        # for a non-spaced script — skip rather than force-fit a
        # threshold, and say so (never a silent pass indistinguishable
        # from "evaluated and clean").
        return ExtractionQualityReport(
            whitespace_ratio=0.0, mean_token_len=float(n_chars), long_token_fraction=1.0,
            n_tokens=n_tokens, n_chars=n_chars, passed=True, failing_signals=[],
            skipped_reason=(
                f"non-spaced-script dominant ({script_frac:.0%} of non-whitespace "
                f"chars) — whitespace/token signals are not meaningful for this "
                f"script (e.g. CJK); skipped rather than evaluated"
            ),
        )

    whitespace_chars = sum(1 for c in text if c.isspace())
    whitespace_ratio = whitespace_chars / n_chars

    if n_tokens == 0:
        # Non-empty text with zero whitespace-delimited tokens is itself
        # the degenerate case this gate exists to catch (the entire
        # document collapsed into unbroken run-on text).
        mean_token_len = float(n_chars)
        long_token_fraction = 1.0
    else:
        token_lens = [len(t) for t in tokens]
        mean_token_len = sum(token_lens) / n_tokens
        long_token_fraction = sum(1 for tok_len in token_lens if tok_len > _LONG_TOKEN_CHARS) / n_tokens

    failing: list[str] = []
    # mean_token_len is UNCONDITIONAL (any n_tokens >= 1) — see the
    # round-2 comment above _MIN_TOKENS_FOR_RATIO_SIGNALS for why this is
    # the signal that must not wait for a sample-size floor.
    if n_tokens >= 1 and mean_token_len > _MEAN_TOKEN_LEN_CEILING:
        failing.append(
            f"mean_token_len={mean_token_len:.2f} > ceiling {_MEAN_TOKEN_LEN_CEILING}"
        )
    if n_tokens >= _MIN_TOKENS_FOR_RATIO_SIGNALS:
        if whitespace_ratio < _WHITESPACE_RATIO_FLOOR:
            failing.append(
                f"whitespace_ratio={whitespace_ratio:.4f} < floor {_WHITESPACE_RATIO_FLOOR}"
            )
        if long_token_fraction > _LONG_TOKEN_FRACTION_CEILING:
            failing.append(
                f"long_token_fraction={long_token_fraction:.4f} > ceiling {_LONG_TOKEN_FRACTION_CEILING}"
            )

    return ExtractionQualityReport(
        whitespace_ratio=whitespace_ratio,
        mean_token_len=mean_token_len,
        long_token_fraction=long_token_fraction,
        n_tokens=n_tokens,
        n_chars=n_chars,
        passed=not failing,
        failing_signals=failing,
    )


def _enforce_extraction_quality(
    result: "ExtractionResult", pdf_path: Path, *, allow_degraded: bool,
) -> None:
    """Gate *result* against :func:`assess_extraction_quality`.

    Raises :class:`~nexus.errors.ExtractionQualityError` (fail loud, per the
    module docstring above) unless *allow_degraded* is True, in which case
    the failure is logged at WARNING (loud in the run output, per the
    bead's "must mark the run output loudly when used" requirement) and
    extraction proceeds with the degraded text.

    Round-2: a ``skipped_reason`` (non-spaced-script/CJK dominance) always
    passes regardless of *allow_degraded* — logged at INFO (capability-
    honest note, not a warning or an error; the signals were never
    evaluated, so there is nothing to override).
    """
    report = assess_extraction_quality(result.text)
    result.metadata["quality_gate_passed"] = report.passed
    if report.skipped_reason:
        _log.info(
            "extraction_quality_gate_skipped",
            path=str(pdf_path),
            reason=report.skipped_reason,
        )
        return
    if report.passed:
        return

    reasons = "; ".join(report.failing_signals)
    method = result.metadata.get("extraction_method", "unknown")
    if allow_degraded:
        result.metadata["quality_gate_overridden"] = True
        _log.warning(
            "extraction_quality_degraded_override",
            path=str(pdf_path),
            extraction_method=method,
            failing_signals=report.failing_signals,
            whitespace_ratio=report.whitespace_ratio,
            mean_token_len=report.mean_token_len,
            long_token_fraction=report.long_token_fraction,
        )
        _progress(
            f"  WARNING: {pdf_path.name} failed the extraction quality gate "
            f"({reasons}) — indexing anyway (--allow-degraded-extraction)."
        )
        return

    _log.error(
        "extraction_quality_gate_failed",
        path=str(pdf_path),
        extraction_method=method,
        failing_signals=report.failing_signals,
        whitespace_ratio=report.whitespace_ratio,
        mean_token_len=report.mean_token_len,
        long_token_fraction=report.long_token_fraction,
    )
    raise ExtractionQualityError(
        f"PDF {pdf_path.name} failed the post-extraction quality gate "
        f"(extraction_method={method}): {reasons}. This is the "
        f"space-stripped-garbage failure mode (nexus-wi1uv) — the extracted "
        f"text is likely unsearchable if indexed. Remedy: retry with "
        f"`--extractor mineru` (formula-aware, often avoids the corruption), "
        f"or if this document is legitimately dense/unusual and you have "
        f"reviewed the extracted text, rerun with "
        f"`--allow-degraded-extraction` to index it anyway."
    )


class PDFExtractor:
    """Extract PDF text via Docling with PyMuPDF normalized fallback.

    Docling uses a neural layout model to handle multi-column academic PDFs,
    producing structured markdown with headings and correct reading order.
    Falls back to PyMuPDF normalized extraction on any Docling failure.
    """

    def __init__(self) -> None:
        self._converter = None  # lazy init — fast mode (no formula enrichment)
        self._converter_enriched = None  # lazy init — enriched mode (formula enrichment)
        self._mineru_server_checked: bool = False
        self._mineru_server_up: bool = False
        self._mineru_server_restarts: int = 0
        # RDR-148 Gap 5/6: set True by Gap 6 when an RLIMIT_AS memory ceiling is
        # applied to the worker, so the OOM classifier treats ANY non-zero exit
        # as a ceiling breach (a breach may surface as a plain non-zero exit, not
        # only SIGKILL / the sentinel). Default False until Gap 6 lands.
        self._mineru_ceiling_applied: bool = False
        # RDR-148 Gap 6: page count of the document currently being extracted,
        # set at the top of _extract_with_mineru so the subprocess path scales
        # the per-page timeout for the whole-doc (end is None) batch without
        # re-opening the PDF. None for direct subprocess callers.
        self._mineru_run_total_pages: int | None = None

    def extract(
        self,
        pdf_path: Path,
        *,
        extractor: str = "auto",
        on_formula_oom: str = "fail",
        on_page: Callable[[int, str, dict], None] | None = None,
        allow_degraded: bool = False,
    ) -> ExtractionResult:
        """Extract text from *pdf_path*. Returns ExtractionResult.

        *extractor* selects the backend:
        - ``"auto"`` — Docling pass (enriched, to detect formulas); if
          formulas found, try MinerU then fall back to PyMuPDF normalized.
        - ``"docling"`` — Docling with PyMuPDF normalized fallback.
        - ``"mineru"`` — MinerU directly (no fallback).

        *on_formula_oom* (RDR-148 Gap 5) governs what happens when a *single*
        page reproducibly OOM-kills MinerU's formula model (page-content-specific
        exhaustion the 1-page-batch floor cannot mitigate):
        - ``"fail"`` (default) — re-raise the formula-aware error; preserves the
          no-silent-fallback-for-formulas guarantee.
        - ``"docling"`` — degrade THAT page to docling (formula-stripped) and
          continue, so one pathological page doesn't fail the whole document.

        *on_page* — optional streaming callback fired per extracted page (or
        per MinerU batch when ``mineru_page_batch > 1``):
        ``on_page(page_index, page_text, page_metadata)``.
        ``page_metadata`` contains ``"page_number"`` (1-based) and
        ``"text_length"``.

        *allow_degraded* (nexus-wi1uv) bypasses the post-extraction quality
        gate (see :func:`assess_extraction_quality`). Every backend routes
        through this one method, so the gate applies uniformly to MinerU,
        Docling, and PyMuPDF output — no extractor is special-cased. Default
        ``False``: a document whose extracted text trips the gate's
        thresholds (the space-stripped-garbage signature — see the
        function's calibration docstring) raises
        :class:`~nexus.errors.ExtractionQualityError` rather than being
        silently indexed as unsearchable garbage. Pass ``True`` to accept
        degraded output deliberately for a specific document (surfaced as
        ``--allow-degraded-extraction`` on the CLI).
        """
        if extractor not in ("auto", "docling", "mineru"):
            raise ValueError(
                f"extractor must be 'auto', 'docling', or 'mineru'; got {extractor!r}"
            )
        if on_formula_oom not in ("fail", "docling"):
            raise ValueError(
                f"on_formula_oom must be 'fail' or 'docling'; got {on_formula_oom!r}"
            )

        # nexus-2fyb code-review R1-I2: validate the path is readable before
        # dispatching. Without this, a directory or dangling symlink reaches
        # pymupdf/Docling and produces an opaque internal error that leaks
        # library paths through the message.
        if not pdf_path.is_file():
            raise FileNotFoundError(
                f"PDF not found or not a regular file: {pdf_path}"
            )

        result = self._extract_dispatch(
            pdf_path, extractor=extractor, on_formula_oom=on_formula_oom, on_page=on_page,
        )
        _enforce_extraction_quality(result, pdf_path, allow_degraded=allow_degraded)
        return result

    def _extract_dispatch(
        self,
        pdf_path: Path,
        *,
        extractor: str,
        on_formula_oom: str,
        on_page: Callable[[int, str, dict], None] | None,
    ) -> ExtractionResult:
        """Backend routing for :meth:`extract`, pre-quality-gate.

        Split out (nexus-wi1uv) so ``extract()`` has exactly one gated exit
        point instead of gating each of this method's several ``return``
        statements individually.
        """
        if extractor == "docling":
            _progress(f"  Docling: extracting {pdf_path.name}…")
            try:
                return self._extract_with_docling(pdf_path, on_page=on_page)
            except Exception as exc:  # noqa: BLE001 — fallback path; logged, falls back to PyMuPDF extractor
                _progress(f"  Docling failed ({type(exc).__name__}), falling back to PyMuPDF: {pdf_path.name}")
                _log.debug("docling_extraction_failed", error=str(exc), path=str(pdf_path))
                return self._extract_normalized(pdf_path, on_page=on_page)

        if extractor == "mineru":
            _progress(f"  MinerU: extracting {pdf_path.name}…")
            return self._extract_with_mineru(
                pdf_path, on_page=on_page, on_formula_oom=on_formula_oom,
            )

        # extractor == "auto"
        # Step 1: Quick formula pre-screen via raw PDF text (~0.1s)
        formula_count = _has_formulas_quick(pdf_path)

        # Step 2: Extract with non-enriched Docling (probe — no on_page callback
        # to avoid double-firing if MinerU takes over for formula PDFs)
        _progress(f"  Docling: extracting {pdf_path.name}…")
        try:
            fast_result = self._extract_with_docling(pdf_path, enriched=False)
        except Exception as exc:  # noqa: BLE001 — fallback path; logged, falls back to PyMuPDF extractor
            _progress(f"  Docling failed ({type(exc).__name__}), falling back to PyMuPDF: {pdf_path.name}")
            _log.debug("docling_auto_pass_failed", error=str(exc), path=str(pdf_path))
            return self._extract_normalized(pdf_path, on_page=on_page)

        # Also check the Docling markdown for LaTeX markers (catches formulas
        # that Docling renders as LaTeX even without enrichment)
        text_markers = _count_formula_markers(fast_result.text)
        formula_count = max(formula_count, text_markers)

        if formula_count < 5:
            # Docling wins — replay on_page from page_boundaries since the
            # probe pass didn't fire the callback.
            if on_page is not None:
                for boundary in fast_result.metadata.get("page_boundaries", []):
                    page_num = boundary["page_number"]
                    start = boundary["start_char"]
                    length = boundary["page_text_length"] - 1  # -1 for \n separator
                    page_text = fast_result.text[start : start + length]
                    on_page(page_num - 1, page_text, {"page_number": page_num, "text_length": length})
            return fast_result

        # Math paper detected — switch to MinerU for formula-aware extraction.
        # nexus-2fyb: previously, a MinerU failure here silently returned the
        # non-enriched Docling probe (formulas already stripped). That hid
        # extraction corruption from every caller — the result was
        # indistinguishable from a paper that legitimately had no math. Auto
        # mode now fails loudly so the user installs MinerU or explicitly opts
        # into formula-stripped extraction with --extractor docling.
        _progress(f"  Formulas detected ({formula_count}) — switching to MinerU: {pdf_path.name}")
        try:
            return self._extract_with_mineru(
                pdf_path, formula_count=formula_count, on_page=on_page,
                on_formula_oom=on_formula_oom,
            )
        except ImportError as exc:
            # do_parse is None — mineru is a default dep since nexus-2fyb so a
            # missing import means the conexus install itself is corrupt.
            _log.error(
                "mineru_import_failed",
                error=str(exc),
                formula_count=formula_count,
                path=str(pdf_path),
            )
            raise RuntimeError(
                f"PDF {pdf_path.name} contains formulas (detected {formula_count}) "
                f"but MinerU is not importable: {exc}. "
                f"MinerU is a required dependency since nexus-2fyb; if it is "
                f"missing your conexus install is corrupt — reinstall with "
                f"`uv tool install --reinstall conexus`. To bypass formula "
                f"extraction entirely, rerun with `--extractor docling`."
            ) from exc
        except Exception as exc:
            # MinerU is installed but extraction failed — subprocess timeout,
            # OOM kill, mineru-api server error, etc. Do NOT advise reinstall;
            # the install is fine and the failure is operational.
            sanitized_msg = _redact_url_credentials(str(exc))
            _log.error(
                "mineru_extraction_failed",
                error=sanitized_msg,
                error_type=type(exc).__name__,
                formula_count=formula_count,
                path=str(pdf_path),
            )
            raise RuntimeError(
                f"PDF {pdf_path.name} contains formulas (detected {formula_count}) "
                f"but MinerU extraction failed: {type(exc).__name__}: {sanitized_msg}. "
                f"To bypass formula extraction and accept formula-stripped "
                f"output for this PDF, rerun with `--extractor docling`."
            ) from exc

    # ── internal extraction methods ───────────────────────────────────────────

    def _get_converter(self, enriched: bool = False):
        """Lazily initialise the Docling DocumentConverter.

        *enriched* enables ``do_formula_enrichment`` for LaTeX extraction.
        Two converters are cached independently so callers can switch modes
        without re-creating the converter each time.
        """
        attr = "_converter_enriched" if enriched else "_converter"
        converter = getattr(self, attr)
        if converter is None:
            from docling.document_converter import DocumentConverter, PdfFormatOption  # noqa: PLC0415 — deferred import — optional/heavy dependency, branch-local
            from docling.datamodel.pipeline_options import PdfPipelineOptions  # noqa: PLC0415 — deferred import — optional/heavy dependency, branch-local

            opts = PdfPipelineOptions()
            opts.do_ocr = False                 # digital PDFs have embedded text
            opts.do_table_structure = True      # TableFormer for table detection
            opts.generate_page_images = False
            opts.generate_picture_images = False
            opts.do_formula_enrichment = enriched
            # Use PRE-DOWNLOADED models when a local artifacts directory is
            # named, instead of letting docling fetch from HuggingFace at
            # convert() time (docling loads layout/TableFormer/CodeFormula
            # LAZILY, so the fetch happens mid-extraction).
            #
            # CI sets this to our own hosted mirror (release asset
            # ci-assets-docling-v1) — runners keep no layer cache, so every
            # build otherwise re-fetched ~1.1GB anonymously from HuggingFace and
            # flaked on it: CAS data-processing errors, connection resets, 429s.
            # It is the same reason the bge-768 ONNX is self-hosted as
            # ci-assets-bge-768-v1. An offline or air-gapped install can point
            # this at any directory produced by
            # ``docling-tools models download -o <dir> layout tableformer code_formula``.
            #
            # Unset (the default) keeps docling's own resolution, so nothing
            # changes for an ordinary local install.
            artifacts = os.environ.get("NEXUS_DOCLING_ARTIFACTS_PATH", "").strip()
            if artifacts:
                artifacts_dir = Path(artifacts)
                if not artifacts_dir.is_dir():
                    raise ValueError(
                        f"NEXUS_DOCLING_ARTIFACTS_PATH is set to {artifacts!r} but that "
                        "is not a directory. Point it at a docling artifacts dir "
                        "(docling-tools models download -o <dir> layout tableformer "
                        "code_formula), or unset it to let docling resolve its own "
                        "models."
                    )
                opts.artifacts_path = artifacts_dir
            converter = DocumentConverter(
                format_options={"pdf": PdfFormatOption(pipeline_options=opts)}
            )
            setattr(self, attr, converter)
        return converter

    def _extract_with_docling(
        self,
        pdf_path: Path,
        *,
        enriched: bool = True,
        on_page: Callable[[int, str, dict], None] | None = None,
    ) -> ExtractionResult:
        """Extract per-page markdown via Docling."""
        result = self._get_converter(enriched=enriched).convert(str(pdf_path))
        doc = result.document
        page_count = doc.num_pages()

        page_texts: list[str] = []
        page_boundaries: list[dict] = []
        current_pos = 0

        for p in range(1, page_count + 1):
            page_md = doc.export_to_markdown(page_no=p).strip()
            if page_md:
                page_boundaries.append(
                    {
                        "page_number": p,
                        "start_char": current_pos,
                        # +1 includes the \n separator from "\n".join so that
                        # _page_for ranges are contiguous (same convention as the
                        # former _extract_markdown implementation).
                        "page_text_length": len(page_md) + 1,
                    }
                )
                if on_page is not None:
                    on_page(p - 1, page_md, {"page_number": p, "text_length": len(page_md)})
                page_texts.append(page_md)
                current_pos += len(page_md) + 1

        text = "\n".join(page_texts)
        if not text.strip():
            raise RuntimeError("docling produced empty output")

        # Collect TableItem regions and count formulas
        table_regions: list[dict] = []
        formula_count = 0
        if enriched:
            # Enriched mode: count FormulaItem objects (duck-typed, single pass)
            for item, _ in doc.iterate_items():
                item_type = type(item).__name__
                if item_type == "FormulaItem":
                    formula_count += 1
                elif item_type == "TableItem":
                    prov = getattr(item, "prov", [])
                    page_no = prov[0].page_no if prov else 0
                    html = ""
                    if callable(getattr(item, "export_to_html", None)):
                        try:
                            html = item.export_to_html(doc=doc)
                        except Exception as exc:  # noqa: BLE001 — best-effort table export; logged, html falls back to empty
                            _log.debug("table_html_export_failed", page=page_no, error=str(exc))
                            html = ""
                    table_regions.append({"page": page_no, "html": html})
        else:
            # Non-enriched mode: scan text for LaTeX formula patterns
            # This is 100x faster than running the enrichment pipeline
            formula_count = _count_formula_markers(text)
            for item, _ in doc.iterate_items():
                if type(item).__name__ == "TableItem":
                    prov = getattr(item, "prov", [])
                    page_no = prov[0].page_no if prov else 0
                    html = ""
                    if callable(getattr(item, "export_to_html", None)):
                        try:
                            html = item.export_to_html(doc=doc)
                        except Exception as exc:  # noqa: BLE001 — best-effort table export; logged, html falls back to empty
                            _log.debug("table_html_export_failed", page=page_no, error=str(exc))
                            html = ""
                    table_regions.append({"page": page_no, "html": html})

        if formula_count > 0:
            _log.warning("formula_content_detected", formula_count=formula_count, path=str(pdf_path))

        return ExtractionResult(
            text=text,
            metadata={
                "extraction_method": "docling",
                "page_count": page_count,
                "format": "markdown",
                "page_boundaries": page_boundaries,
                "table_regions": table_regions,
                "formula_count": formula_count,
                "docling_title": self._extract_title(doc),
                "pdf_title": "",  # XMP metadata not exposed by Docling
                "pdf_author": "",
                "pdf_subject": "",
                "pdf_keywords": "",
                "pdf_creator": "",
                "pdf_producer": "",
                "pdf_creation_date": "",
                "pdf_mod_date": "",
            },
        )

    # Page batch size is read from config via get_mineru_page_batch() (default 1).
    # Formula-dense PDFs OOM during MFR prediction at larger batch sizes.

    def _extract_page_via_docling(self, pdf_path: Path, page: int) -> str:
        """Formula-stripped docling extraction of a SINGLE page (0-based).

        RDR-148 Gap 5 ``on_formula_oom="docling"`` support: when one page
        reproducibly OOM-kills MinerU's formula model, extract just that page
        with docling (slicing it into a one-page temp PDF) so the rest of the
        document still gets formula-aware MinerU extraction. Returns the page's
        markdown (formulas rendered as best docling can, i.e. stripped).
        """
        import tempfile  # noqa: PLC0415 — deferred import — branch-local
        import pymupdf  # noqa: PLC0415 — deferred import — optional/heavy dependency, branch-local

        # Create the temp file FIRST so it is always bound for cleanup even if
        # pymupdf slicing raises (an insert_pdf failure must propagate as itself,
        # not as an UnboundLocalError on a never-assigned tmp name).
        fd, tmp_name = tempfile.mkstemp(suffix=".pdf")
        _os.close(fd)
        tmp_path = Path(tmp_name)
        try:
            with pymupdf.open(pdf_path) as doc:
                one = pymupdf.open()
                try:
                    one.insert_pdf(doc, from_page=page, to_page=page)
                    one.save(tmp_name)
                finally:
                    one.close()
            return self._extract_with_docling(tmp_path).text
        finally:
            tmp_path.unlink(missing_ok=True)

    def _degrade_page_to_docling(
        self, pdf_path: Path, page: int, total_pages: int, fname: str,
    ) -> tuple[str, list[dict], list[dict]]:
        """Degrade ONE page to docling after a formula-OOM, returning the
        ``(md, content_list, pdf_info)`` triple in MinerU's shape (empty
        structured lists, since docling does not emit MinerU content_list)."""
        _log.warning(
            "mineru_formula_oom_degrade_to_docling",
            page=page + 1, path=str(pdf_path),
        )
        _progress(
            f"  MinerU page {page + 1}/{total_pages} OOM (formula model) — "
            f"degrading THIS page to docling (formula-stripped, {fname})"
        )
        return self._extract_page_via_docling(pdf_path, page), [], []

    def _extract_with_mineru(
        self,
        pdf_path: Path,
        *,
        formula_count: int = 0,
        on_formula_oom: str = "fail",
        on_page: Callable[[int, str, dict], None] | None = None,
    ) -> ExtractionResult:
        """Extract text via MinerU (math-aware, optional dependency).

        Each page-range batch runs in a **subprocess** so that MinerU's
        GPU/model memory is fully reclaimed between batches.  Without this,
        memory accumulates across in-process ``do_parse`` calls and large
        formula-dense PDFs get OOM-killed.

        OOM retry: if a multi-page batch fails, retries at 1-page granularity.
        Single-page failures propagate immediately (no infinite retry).

        *on_page* fires once per batch (default batch size is 1 page via
        ``mineru_page_batch`` config).  The callback receives the batch start
        page index, the batch markdown, and metadata.
        """
        if do_parse is None:
            raise ImportError(
                "MinerU is not importable but is a required dependency since "
                "nexus-2fyb. Reinstall conexus: `uv tool install --reinstall conexus`."
            )

        import pymupdf  # lightweight — only used for page count  # noqa: PLC0415 — deferred import — optional/heavy dependency, branch-local

        with pymupdf.open(pdf_path) as doc:
            total_pages = len(doc)
        # Gap 6: expose to the subprocess path for whole-doc-batch timeout scaling.
        self._mineru_run_total_pages = total_pages

        from nexus.config import get_mineru_page_batch  # noqa: PLC0415 — deferred local import — avoids import-time cost / circular deps
        batch_size = get_mineru_page_batch()

        batches: list[tuple[int, int | None]] = []
        if total_pages <= batch_size:
            batches.append((0, None))
        else:
            _log.info(
                "mineru_splitting_large_pdf",
                total_pages=total_pages,
                batch_size=batch_size,
                path=str(pdf_path),
            )
            for start in range(0, total_pages, batch_size):
                batches.append((start, min(start + batch_size, total_pages)))

        md_parts: list[str] = []
        all_content_list: list[dict] = []
        all_pdf_info: list[dict] = []
        # nexus-2fyb code-review C2: track real per-page markdown lengths
        # so page_boundaries reflect actual content distribution, not a
        # uniform char/page average. Each entry: (page_index_0based, length).
        # OOM retry loop appends per-page entries directly; batch-mode
        # appends one entry covering the batch span.
        per_page_lengths: list[tuple[int, int]] = []
        # nexus-1oguj: pages that fell back to docling via the Gap 5
        # degrade-to-docling path (formula-OOM). Non-empty means the
        # document's extraction_method is the honest mixed aggregate
        # "mineru+docling-degraded" rather than a bare "mineru" that
        # would silently overstate MinerU's actual coverage.
        degraded_pages: list[int] = []

        def _append_page(
            page: int, md: str, content_list: list[dict], pdf_info: list[dict],
        ) -> None:
            # Single append point so the batch-success, 1-page-retry, and
            # degrade-to-docling paths produce identical bookkeeping. For
            # batch_size > 1 the success path passes the batch start as `page`;
            # page-number metadata is only exact at batch_size == 1 (the
            # default the streaming pipeline relies on).
            if on_page is not None:
                on_page(page, md, {"page_number": page + 1, "text_length": len(md)})
            md_parts.append(md)
            all_content_list.extend(content_list)
            all_pdf_info.extend(pdf_info)
            per_page_lengths.append((page, len(md)))

        def _append_batch(s: int, md: str, content_list: list[dict],
                          pdf_info: list[dict], batch_pages: int) -> None:
            # batch_size>1 success: the batch md covers `batch_pages` pages and
            # we distribute its length uniformly (the only resolution available
            # without per-page md from MinerU). on_page/md_parts/content fire
            # once for the batch; per_page_lengths is the distributed form.
            if on_page is not None:
                on_page(s, md, {"page_number": s + 1, "text_length": len(md)})
            md_parts.append(md)
            all_content_list.extend(content_list)
            all_pdf_info.extend(pdf_info)
            per_page = len(md) // batch_pages
            remainder = len(md) % batch_pages
            for offset in range(batch_pages):
                extra = 1 if offset < remainder else 0
                per_page_lengths.append((s + offset, per_page + extra))

        def _extract_range(s: int, e: int | None) -> None:
            # RDR-148 Gap 6 batch//2 ladder: extract pages [s, e) in one
            # subprocess; on failure BISECT (full range -> halves -> ... -> one
            # page) instead of dropping straight to 1-page, so a mid-size batch
            # that fits memory is tried before the slowest per-page path. A
            # single page that OOMs degrades-or-fails per on_formula_oom (Gap 5).
            rng_end = e if e is not None else total_pages
            span = rng_end - s
            try:
                md, content_list, pdf_info = self._mineru_run_isolated(pdf_path, s, e)
            except RuntimeError as exc:
                if span <= 1:
                    # Single page already run and OOM'd (exc in hand) — do not
                    # re-run. Gap 5: degrade THIS page to docling when opted in,
                    # else propagate so the document fails cleanly (no silent
                    # formula fallback). Single degrade site (Gap 6 lands once).
                    if isinstance(exc, MineruMemoryError) and on_formula_oom == "docling":
                        d_md, d_cl, d_pi = self._degrade_page_to_docling(
                            pdf_path, s, total_pages, fname,
                        )
                        _append_page(s, d_md, d_cl, d_pi)
                        degraded_pages.append(s)
                        return
                    raise
                mid = s + span // 2
                _log.warning(
                    "mineru_oom_retry", pages=f"{s + 1}–{rng_end}",
                    path=str(pdf_path), original_batch=span, retry_split=mid,
                )
                _progress(
                    f"  MinerU: pages {s + 1}–{rng_end} failed — bisecting "
                    f"at {mid + 1} ({fname})"
                )
                _extract_range(s, mid)
                _extract_range(mid, rng_end)
                return
            # Success. Normalize before measuring length so per_page_lengths is
            # consistent with the stored normalized text.
            md = _normalize_mineru_latex(md)
            if span <= 1:
                _append_page(s, md, content_list, pdf_info)
            else:
                _append_batch(s, md, content_list, pdf_info, span)

        fname = pdf_path.name
        for batch_idx, (start, end) in enumerate(batches):
            label = f"{start + 1}–{end}" if end is not None else f"{start + 1}–{total_pages}"
            _progress(
                f"  MinerU: page {start + 1}/{total_pages} ({fname})",
            )
            _log.info("mineru_batch", pages=label, path=str(pdf_path))
            _extract_range(start, end)

        if batches:
            _progress(f"  MinerU: {total_pages}/{total_pages} done ({fname})")

        md_text = "\n".join(md_parts)
        return self._mineru_build_result(
            pdf_path, md_text, all_content_list, all_pdf_info,
            per_page_lengths=per_page_lengths,
            formula_count_floor=formula_count,
            degraded_page_count=len(degraded_pages),
        )

    def _probe_mineru_health(self, base_url: str) -> tuple[bool, str]:
        """Probe ``{base_url}/health`` once. Return ``(ok, reason)``.

        ``reason`` is empty on success, else a short diagnostic
        (``http_503`` / ``ConnectError: ...``) for the caller to surface
        on the loud fallback decision. Per-probe failures are logged at
        DEBUG — the single WARNING belongs to the final fallback in
        :meth:`_mineru_server_available`, not to each probe.
        """
        url = f"{base_url}/health"
        try:
            resp = httpx.get(url, timeout=2)
            if resp.status_code == 200:
                return True, ""
            _log.debug("mineru_health_probe_non_200", url=url,
                       http_status=resp.status_code)
            return False, f"http_{resp.status_code}"
        except (httpx.ConnectError, httpx.TimeoutException,
                httpx.RemoteProtocolError) as exc:
            # RemoteProtocolError: a server dying mid-startup can accept the
            # TCP connection but return a truncated/malformed response. The
            # parse path (_mineru_run_isolated) already treats it as a
            # crash-and-fall-back; the health probe must too, not crash.
            _log.debug("mineru_health_probe_unreachable", url=url,
                       error=f"{type(exc).__name__}: {exc}")
            return False, f"{type(exc).__name__}: {exc}"

    def _mineru_server_available(self) -> bool:
        """Check if the MinerU API server is reachable.

        Result cached for the lifetime of this PDFExtractor instance —
        a False result is never retried. Create a new instance to re-check.

        RDR-148 Gap 2 (rediscover-then-fail-loud): on a /health failure
        the run must not silently degrade to the in-process subprocess
        path (where math-heavy / large PDFs OOM-kill the worker). Before
        degrading, perform exactly ONE rediscovery pass — re-resolve the
        endpoint, which re-reads the live PID file when config is at the
        default, so a server that restarted mid-run on a new port is
        picked up. Only when rediscovery still finds no live server is the
        subprocess path selected, and that decision is logged LOUD (a
        single WARNING + ``_progress`` line naming the reason), never
        silently (nexus-h1jk warn-on-fallback, made non-silent here).

        Known limitation (by Gap 1 design, for vehin.5): "rediscover" means
        re-resolve via ``get_mineru_server_url()``, whose pid-file read is
        gated by the Gap 1 precedence — an EXPLICIT non-default operator URL
        wins and the pid file is intentionally NOT consulted. So rediscovery
        re-reads the pid file only on the default-config path; with an
        explicit URL it is a transient-recovery re-probe of the same
        endpoint. This is deliberate: honoring "operator intent wins" (Gap 1)
        precludes a pid file silently redirecting an explicitly-pinned URL.
        """
        if self._mineru_server_checked:
            return self._mineru_server_up

        from nexus.config import get_mineru_server_url  # noqa: PLC0415 — deferred local import — avoids import-time cost / circular deps

        first_url = get_mineru_server_url()
        ok, first_reason = self._probe_mineru_health(first_url)
        if ok:
            self._mineru_server_up = True
            self._mineru_server_checked = True
            return True

        # Gap 2: exactly one rediscovery pass before degrading. Re-resolving
        # re-reads the PID file (default-config path), so a mid-run restart
        # to a new port is picked up; with an explicit operator URL this is
        # a single transient-recovery re-probe of the same endpoint.
        second_url = get_mineru_server_url()
        ok, reason = self._probe_mineru_health(second_url)
        if ok:
            _log.info("mineru_server_rediscovered",
                      url=second_url, prior_url=first_url)
            self._mineru_server_up = True
            self._mineru_server_checked = True
            return True

        # nexus-1qdb9: on-demand lifecycle before conceding — elect a
        # single spawner via the RDR-149 substrate and wait (bounded) for
        # health. Config-gated (pdf.mineru_autostart) and remote-intent-
        # safe; None = degrade exactly as before.
        try:
            from nexus.daemon.mineru_lifecycle import (  # noqa: PLC0415 — deferred import
                ensure_mineru_running,
                spawn_policy_allows,
            )
            if spawn_policy_allows(second_url):
                # Critique a29348b4: the wait below can run minutes with
                # only structlog — surface it on the same _progress channel
                # the fallback warning uses, or the run just looks hung.
                _progress(
                    "  MinerU server not responding — autostarting "
                    "(waits up to 2 min; first-ever start may download models)…"
                )
            ensured = ensure_mineru_running()
        except Exception:  # noqa: BLE001 — lifecycle must never break extraction
            _log.warning("mineru_ensure_crashed", exc_info=True)
            ensured = None
        if ensured is not None:
            _log.info("mineru_server_autostarted", url=ensured)
            self._mineru_server_up = True
            self._mineru_server_checked = True
            return True

        # No live server after rediscovery + autostart — loud, reasoned fallback.
        self._mineru_server_up = False
        self._mineru_server_checked = True
        _log.warning(
            "mineru_fallback_to_subprocess",
            first_url=first_url, rediscovered_url=second_url,
            reason=reason, first_reason=first_reason,
        )
        _progress(
            f"  warn: MinerU server unreachable after rediscovery "
            f"({reason}); falling back to in-process subprocess (slower, "
            f"OOM-risk on large math PDFs). Run `nx mineru start` to enable "
            f"server mode, or pass --extractor docling."
        )
        return self._mineru_server_up

    def _mineru_run_via_server(
        self, pdf_path: Path, start: int, end: int | None,
    ) -> tuple[str, list[dict], list[dict]]:
        """Extract via MinerU HTTP server (POST /file_parse)."""
        from nexus.config import get_mineru_server_url, get_mineru_table_enable  # noqa: PLC0415 — deferred local import — avoids import-time cost / circular deps

        url = f"{get_mineru_server_url()}/file_parse"
        with pdf_path.open("rb") as f:
            resp = httpx.post(
                url,
                files=[("files", (pdf_path.name, f, "application/pdf"))],
                data={
                    "backend": "pipeline",
                    "start_page_id": str(start),
                    "end_page_id": str(end if end is not None else 99999),
                    "formula_enable": "true",
                    "table_enable": str(get_mineru_table_enable()).lower(),
                    "return_md": "true",
                    "return_middle_json": "true",
                    "return_content_list": "true",
                    "parse_method": "auto",
                    "lang_list": "en",
                },
                timeout=300,
            )
        resp.raise_for_status()
        data = resp.json()

        all_results = data.get("results", {})
        stem = pdf_path.stem
        results = all_results.get(stem)
        if results is None:
            if len(all_results) == 1:
                results = next(iter(all_results.values()))
            else:
                raise RuntimeError(
                    f"Server results missing key {stem!r}; "
                    f"available keys: {list(all_results.keys())}"
                )

        md = results.get("md_content", "")
        if not md:
            # Empty page (image-only, blank, or figure plate) — not an error
            _log.debug("mineru_empty_page", path=str(pdf_path), start=start, end=end)
            md = ""

        raw_cl = results.get("content_list")
        raw_mj = results.get("middle_json")
        if raw_mj is None:
            _log.warning("mineru_server_no_middle_json", path=str(pdf_path))
        content_list = json.loads(raw_cl) if raw_cl else []
        middle = json.loads(raw_mj) if raw_mj else {}
        return md, content_list, middle.get("pdf_info", [])

    _MINERU_MAX_RESTARTS: int = 2

    def _restart_mineru_server(self) -> bool:
        """Attempt to restart the MinerU server after a crash.

        Returns True if the server was restarted and is healthy.
        Limited to _MINERU_MAX_RESTARTS per PDFExtractor instance.

        nexus-c7odl: this trigger honors the same policy gates and runs
        under the same RDR-149 election as the on-demand lifecycle — a
        bare inline spawn here raced ``ensure_mineru_running`` (two live
        servers, one orphaned) and bypassed ``mineru_autostart: false``.
        The spawn itself goes through the shared ``spawn_server_process``
        core (0o600 pid file + output_root + child log — the old inline
        copy had drifted on all three) and honors a configured fixed port
        (the 2026-07-01 invisible-server class: a fixed
        ``pdf.mineru_server_url`` with a restart onto a random port left
        a live server ``get_mineru_server_url`` would never find).
        """
        if self._mineru_server_restarts >= self._MINERU_MAX_RESTARTS:
            _log.warning("mineru_restart_budget_exhausted",
                         restarts=self._mineru_server_restarts)
            return False

        from nexus.daemon.mineru_lifecycle import spawn_policy_allows  # noqa: PLC0415 — deferred local import — avoids import-time cost / circular deps

        if not spawn_policy_allows():
            _log.info("mineru_restart_blocked_by_policy")
            return False

        self._mineru_server_restarts += 1
        _log.info("mineru_server_restarting",
                  attempt=self._mineru_server_restarts)

        import time as _time  # noqa: PLC0415 — deferred import — branch-local
        from nexus._mineru_pid import (  # noqa: PLC0415 — deferred local import — avoids import-time cost / circular deps
            _pid_file_path,
            is_process_alive,
            read_pid_file,
        )
        from nexus._mineru_spawn import (  # noqa: PLC0415 — deferred local import — avoids import-time cost / circular deps
            _HEALTH_POLL_INTERVAL,
            _find_free_port,
            spawn_server_process,
        )
        from nexus.config import (  # noqa: PLC0415 — deferred local import — avoids import-time cost / circular deps
            get_mineru_configured_fixed_port,
            nexus_config_dir,
        )
        from nexus.daemon.mineru_lifecycle import MINERU_TIER, _SPAWN_SCOPE  # noqa: PLC0415 — deferred local import — avoids import-time cost / circular deps
        from nexus.daemon.service_registry import ServiceRegistry  # noqa: PLC0415 — deferred local import — avoids import-time cost / circular deps

        try:
            registry = ServiceRegistry(dir=nexus_config_dir(), tier=MINERU_TIER)
            proc = None
            port = None
            with registry.election(_SPAWN_SCOPE):
                info = read_pid_file()
                if info is not None and is_process_alive(info["pid"]):
                    # A concurrent trigger already restarted — probe it below.
                    port = info["port"]
                else:
                    if info is not None:
                        _pid_file_path().unlink(missing_ok=True)
                    port = get_mineru_configured_fixed_port() or _find_free_port()
                    proc = spawn_server_process(port)
                    if proc is None:
                        _log.warning("mineru_restart_failed",
                                     reason="mineru-api not found")
                        return False
        except Exception:  # noqa: BLE001 — restart must never break extraction; the caller falls back
            _log.warning("mineru_restart_failed", exc_info=True)
            return False

        # Poll health for up to 60s (models already cached in memory by OS)
        url = f"http://127.0.0.1:{port}/health"
        deadline = _time.monotonic() + 60
        while _time.monotonic() < deadline:
            if proc is not None and proc.poll() is not None:
                _log.warning("mineru_restart_failed", reason="process exited")
                return False
            try:
                resp = httpx.get(url, timeout=2)
                if resp.status_code == 200:
                    break
            except (httpx.ConnectError, httpx.TimeoutException):
                pass
            _time.sleep(_HEALTH_POLL_INTERVAL)
        else:
            _log.warning("mineru_restart_failed", reason="health timeout")
            return False

        # Reset availability cache. The pid file was already written by
        # spawn_server_process (nexus-oa7r: pid file only, never config —
        # persisting ephemeral ports drifted across reboots).
        self._mineru_server_checked = True
        self._mineru_server_up = True
        _log.info("mineru_server_restarted",
                  pid=proc.pid if proc is not None else info["pid"], port=port)
        return True

    def _mineru_run_isolated(
        self, pdf_path: Path, start: int, end: int | None,
    ) -> tuple[str, list[dict], list[dict]]:
        """Dispatch to server or subprocess based on server availability."""
        if self._mineru_server_available():
            try:
                return self._mineru_run_via_server(pdf_path, start, end)
            except (httpx.ConnectError, httpx.TimeoutException,
                    httpx.RemoteProtocolError) as exc:
                # Server crashed — invalidate cache, try restart
                self._mineru_server_checked = True
                self._mineru_server_up = False
                _log.warning("mineru_server_lost", path=str(pdf_path),
                             pages=f"{start}–{end}", error=str(exc))
                if self._restart_mineru_server():
                    # Retry this page on the new server
                    try:
                        return self._mineru_run_via_server(pdf_path, start, end)
                    except Exception:  # noqa: BLE001 — best-effort server call; falls through to subprocess mode
                        pass  # fall through to subprocess
                return self._mineru_run_subprocess(pdf_path, start, end)
            except httpx.HTTPStatusError as exc:
                _log.warning("mineru_server_error", path=str(pdf_path),
                             pages=f"{start}–{end}", error=str(exc))
                return self._mineru_run_subprocess(pdf_path, start, end)
        return self._mineru_run_subprocess(pdf_path, start, end)

    def _mineru_run_subprocess(
        self, pdf_path: Path, start: int, end: int | None,
    ) -> tuple[str, list[dict], list[dict]]:
        """Run MinerU in a fresh OS process for full memory isolation.

        Uses ``subprocess.run`` with an inline Python script so the child
        loads MinerU models independently.  When the child exits, all
        GPU/model memory is reclaimed by the OS — no leaks across batches.
        """
        result_dir = tempfile.mkdtemp()
        try:
            import os as _os  # noqa: PLC0415 — deferred import — optional/heavy dependency, branch-local
            import signal  # noqa: PLC0415 — deferred import — optional/heavy dependency, branch-local

            # RDR-148 Gap 6: optional RLIMIT_AS address-space ceiling on the
            # worker. LINUX-GATED: macOS raises ValueError on
            # setrlimit(RLIMIT_AS) and does not enforce it (verified spike,
            # RDR-148 Spike Result 1) — an ungated preexec crashes darwin. When
            # a ceiling IS applied, set _mineru_ceiling_applied so the Gap 5
            # classifier treats any non-zero exit as OOM (a breach can surface
            # as a plain non-zero exit, not only SIGKILL / the sentinel).
            from nexus.config import (  # noqa: PLC0415 — deferred local import — avoids import-time cost / circular deps
                get_mineru_memory_ceiling_mb,
                get_mineru_page_timeout_s,
            )
            ceiling_mb = get_mineru_memory_ceiling_mb()
            preexec_fn = None
            if ceiling_mb > 0 and sys.platform.startswith("linux"):
                ceiling_bytes = ceiling_mb * 1024 * 1024

                def _apply_rlimit() -> None:  # pragma: no cover — runs in the child after fork
                    import resource  # noqa: PLC0415 — child-only, POSIX
                    resource.setrlimit(
                        resource.RLIMIT_AS, (ceiling_bytes, ceiling_bytes),
                    )

                preexec_fn = _apply_rlimit
            elif ceiling_mb > 0:
                # Operator configured a ceiling but it cannot be enforced here —
                # surface it loudly (a silent no-op resource cap is a hazard).
                _log.warning(
                    "mineru_memory_ceiling_unenforced_on_platform",
                    platform=sys.platform, ceiling_mb=ceiling_mb,
                    consequence="RLIMIT_AS is Linux-only; relying on the OS OOM-killer",
                )
            # Reflect THIS call's ceiling state for the OOM classifier.
            self._mineru_ceiling_applied = preexec_fn is not None

            # RDR-148 Gap 6: per-page wall-clock budget replaces the old fixed
            # batch-level 180s. For the whole-doc end==None batch the page count
            # is supplied by the caller (total_pages, already opened upstream) so
            # the budget scales without re-opening the PDF; explicit batches
            # scale by their span. Falls back to 1 page (the old flat 180s, no
            # regression) when total_pages is unknown (direct subprocess calls).
            if end is not None:
                pages = end - start
            elif self._mineru_run_total_pages is not None:
                pages = max(1, self._mineru_run_total_pages - start)
            else:
                pages = 1
            timeout_s = get_mineru_page_timeout_s() * max(1, pages)

            # Short-lived per-batch worker (Gap 4 carve-out): DEVNULL stdio is a
            # judged choice — failure is returncode-detected by the caller
            # (killpg + the Gap 5 OOM classification below).
            proc = subprocess.Popen(
                [
                    sys.executable, "-c", _MINERU_WORKER_SCRIPT,
                    str(pdf_path), result_dir,
                    str(start), "none" if end is None else str(end),
                ],
                stdin=subprocess.DEVNULL,
                stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL,
                start_new_session=True,  # own process group
                preexec_fn=preexec_fn,  # Linux-only RLIMIT_AS ceiling (or None)
            )
            # Use killpg(getpgid(pid)) rather than killpg(pid) directly —
            # with start_new_session=True the pgid equals pid at spawn time,
            # but by the time we kill the child may be dead and the PID
            # recycled by the kernel. getpgid() resolves the current pgid
            # from the live PID slot (raises ProcessLookupError if the
            # process is already gone, which we swallow). Matches the
            # session.py:301 idiom (indexing review C1).
            def _killpg_safe() -> None:
                # Delegated to nexus.util.process_group.safe_killpg so
                # the mock-guard + error-swallow contract is consistent
                # across every subprocess cleanup site in the codebase.
                from nexus.util.process_group import safe_killpg  # noqa: PLC0415 — deferred local import — avoids import-time cost / circular deps

                safe_killpg(proc, signal.SIGKILL)

            try:
                returncode = proc.wait(timeout=timeout_s)
            except subprocess.TimeoutExpired:
                _killpg_safe()
                proc.wait()
                raise RuntimeError(
                    f"MinerU subprocess timed out after {timeout_s}s "
                    f"(pages {start}–{end}, path={pdf_path})"
                )
            if returncode != 0:
                # Clean up any orphaned children in the process group
                _killpg_safe()
                # RDR-148 Gap 5: 3-way OOM classification. A memory exhaustion
                # surfaces three ways: (1) OS OOM-killer / macOS jetsam ->
                # negative SIGKILL returncode; (2) an RLIMIT_AS breach caught
                # in-process -> the _MINERU_OOM_EXIT sentinel; (3) once a memory
                # ceiling is applied (Gap 6), any non-zero exit is treated as a
                # breach. The SIGKILL-only mapping would miss (2) and (3) — the
                # gate finding that motivated this classification.
                is_oom = (
                    returncode == -signal.SIGKILL
                    or returncode == _MINERU_OOM_EXIT
                    or self._mineru_ceiling_applied
                )
                _log.error(
                    "mineru_subprocess_failed",
                    returncode=returncode,
                    classified_oom=is_oom,
                    pages=f"{start}–{end}",
                    path=str(pdf_path),
                )
                msg = (
                    f"MinerU subprocess exited with code {returncode} "
                    f"(pages {start}–{end}, path={pdf_path})"
                )
                if is_oom:
                    raise MineruMemoryError(msg)
                raise RuntimeError(msg)
            # Kill any lingering workers in the process group
            _killpg_safe()

            pdf_name = pdf_path.name
            base = Path(result_dir) / pdf_name / "auto"
            # Indexing review I2: assume MinerU's output layout but fail
            # loudly with a useful message when it diverges (e.g. version
            # upgrade changes the "auto" directory name). The subprocess
            # already exited 0, so a missing output file is an unexpected
            # state not a runtime error.
            md_file = base / f"{pdf_name}.md"
            if not md_file.exists():
                raise RuntimeError(
                    f"MinerU produced no output at {md_file} "
                    f"(subprocess exited 0; layout may have changed). "
                    f"Pages {start}–{end}, path={pdf_path}"
                )
            md = md_file.read_text(encoding="utf-8")
            content_list: list[dict] = json.loads(
                (base / f"{pdf_name}_content_list.json").read_text(encoding="utf-8")
            )
            middle: dict = json.loads(
                (base / f"{pdf_name}_middle.json").read_text(encoding="utf-8")
            )
            return md, content_list, middle.get("pdf_info", [])
        finally:
            import shutil  # noqa: PLC0415 — deferred import — optional/heavy dependency, branch-local
            shutil.rmtree(result_dir, ignore_errors=True)

    @staticmethod
    def _mineru_build_result(
        pdf_path: Path, md_text: str,
        content_list: list[dict], pdf_info: list[dict],
        *,
        per_page_lengths: list[tuple[int, int]] | None = None,
        formula_count_floor: int = 0,
        degraded_page_count: int = 0,
    ) -> ExtractionResult:
        """Assemble an ExtractionResult from (merged) MinerU outputs.

        *per_page_lengths* (nexus-2fyb code-review C2): list of
        ``(page_index_0based, markdown_char_length)`` tuples captured from
        the batch loop. Used to build accurate ``page_boundaries`` so
        chunks get correct ``page_number`` attribution. When ``None``
        (legacy callers, defensive), falls back to uniform char/page
        distribution — but logs a warning because that path produces
        wrong page_number metadata for any non-uniform document.

        *formula_count_floor* (nexus-2fyb code-review R1): the count
        produced by the auto-mode probe, used as a lower bound. If
        MinerU's structured response is missing or empty (e.g. server
        returned content_list=[] under degraded conditions), the
        recomputed formula_count would otherwise be 0, breaking the
        ``has_formulas`` flag downstream for confirmed math papers.

        *degraded_page_count* (nexus-1oguj): number of pages that fell
        back to docling via the Gap 5 per-page OOM-degrade path. A bare
        ``extraction_method="mineru"`` would overstate coverage when even
        one page was actually docling-rendered (formulas stripped), so
        any degradation flips the recorded value to the honest aggregate
        ``"mineru+docling-degraded"``.
        """
        display_count = sum(1 for e in content_list if e.get("type") == "equation")

        inline_count = 0
        for page in pdf_info:
            for block in page.get("para_blocks", []):
                for line in block.get("lines", []):
                    for span in line.get("spans", []):
                        if span.get("type") == "inline_equation":
                            inline_count += 1

        formula_count = max(display_count + inline_count, formula_count_floor)
        page_count = len(pdf_info)

        # md_text is already normalized: _extract_with_mineru applies
        # _normalize_mineru_latex per-page so per_page_lengths and
        # page_boundaries are consistent with the stored text.
        total_len = len(md_text)

        page_boundaries: list[dict] = []
        if per_page_lengths is not None and page_count > 0:
            # Use the real per-page lengths captured from the batch loop.
            # md_text is "\n".join(md_parts), so each per-page segment has
            # +1 separator except the last. start_char accumulates.
            page_lengths_by_idx = {idx: length for idx, length in per_page_lengths}
            pos = 0
            for i in range(page_count):
                length = page_lengths_by_idx.get(i, 0)
                # Add +1 for the "\n" separator (matches the join), except final.
                stored_length = length + (1 if i < page_count - 1 else 0)
                page_boundaries.append({
                    "page_number": i + 1,
                    "start_char": pos,
                    "page_text_length": stored_length,
                })
                pos += stored_length
        elif page_count > 0 and total_len > 0:
            # Fallback (legacy callers, no per-batch tracking). Uniform
            # distribution gives wrong page_number for non-uniform docs.
            _log.warning(
                "mineru_uniform_page_boundaries",
                path=str(pdf_path),
                page_count=page_count,
                reason="per_page_lengths not provided",
            )
            chars_per_page = total_len / page_count
            for i in range(page_count):
                start = int(i * chars_per_page)
                length = int(chars_per_page) + (1 if i < page_count - 1 else 0)
                page_boundaries.append({
                    "page_number": i + 1,
                    "start_char": start,
                    "page_text_length": length,
                })

        if formula_count > 0:
            _log.info(
                "mineru_formulas_extracted",
                formula_count=formula_count,
                path=str(pdf_path),
            )

        extraction_method = (
            "mineru+docling-degraded" if degraded_page_count > 0 else "mineru"
        )
        return ExtractionResult(
            text=md_text,
            metadata={
                "extraction_method": extraction_method,
                "page_count": page_count,
                "format": "markdown",
                "formula_count": formula_count,
                "page_boundaries": page_boundaries,
                "table_regions": [],
                "docling_title": "",
                "pdf_title": "",
                "pdf_author": "",
                "pdf_subject": "",
                "pdf_keywords": "",
                "pdf_creator": "",
                "pdf_producer": "",
                "pdf_creation_date": "",
                "pdf_mod_date": "",
            },
        )

    def _extract_title(self, doc) -> str:
        """Extract a paper title from Docling document items on page 1.

        Algorithm (verified on 19 corpus PDFs, 17/19 correct):
        1. Iterate page-1 items, skip section labels (abstract, introduction, keywords).
        2. Return first item with label containing 'title' or 'section_header'.
        3. Fallback: first text-labelled item on page 1 with 10 ≤ len < 120.
        """
        _SKIP = {"abstract", "introduction", "1 introduction", "keywords"}

        for item, _ in doc.iterate_items():
            prov = getattr(item, "prov", [])
            if not prov or prov[0].page_no != 1:
                continue
            text = (getattr(item, "text", "") or "").strip()
            if not text or len(text) < 10:
                continue
            lower = text.lower()
            if lower in _SKIP:
                continue
            if lower.startswith("abstract") and len(text) > 100:
                continue
            label = str(getattr(item, "label", ""))
            if "title" in label or "section_header" in label:
                return text

        # Fallback: first short text block on page 1
        for item, _ in doc.iterate_items():
            prov = getattr(item, "prov", [])
            if not prov or prov[0].page_no != 1:
                continue
            text = (getattr(item, "text", "") or "").strip()
            if text and 10 <= len(text) < 120:
                return text

        return ""

    def _extract_normalized(
        self,
        pdf_path: Path,
        *,
        on_page: Callable[[int, str, dict], None] | None = None,
    ) -> ExtractionResult:
        """Extract via raw PyMuPDF with whitespace normalization."""
        import pymupdf  # lazy  # noqa: PLC0415 — deferred import — optional/heavy dependency, branch-local

        text_parts: list[str] = []
        page_boundaries: list[dict] = []
        current_pos = 0

        with pymupdf.open(pdf_path) as doc:
            page_count = len(doc)
            doc_meta = doc.metadata or {}
            for page_num, page in enumerate(doc):
                raw: str = page.get_text(sort=True)
                # Normalize per-page so page_boundaries match character positions
                # in the final joined text (global normalization after the fact
                # would shift boundaries unpredictably).
                page_text = re.sub(r" +", " ", raw)
                page_text = re.sub(r"\n{3,}", "\n\n", page_text)
                page_text = "\n".join(line.rstrip() for line in page_text.split("\n")).strip()
                page_text = _normalize_whitespace_edge_cases(page_text)
                if page_text:
                    page_boundaries.append(
                        {
                            "page_number": page_num + 1,
                            "start_char": current_pos,
                            # +1 includes the \n separator from "\n".join (same
                            # rationale as _extract_with_docling: contiguous ranges).
                            "page_text_length": len(page_text) + 1,
                        }
                    )
                    if on_page is not None:
                        on_page(page_num, page_text, {"page_number": page_num + 1, "text_length": len(page_text)})
                    text_parts.append(page_text)
                    current_pos += len(page_text) + 1

        text = "\n".join(text_parts)
        if not text.strip():
            # nexus-aold: silent zero-chunk indexing was the failure
            # mode of large-PDF Docling crashes that cascaded into
            # the PyMuPDF fallback returning an empty result. Make
            # it a hard error here too, mirroring the equivalent
            # guard in _extract_with_docling. The indexer's outer
            # error path will surface this as a non-zero exit with
            # a named failure mode (was: silent 0 chunks indexed).
            raise RuntimeError(
                f"pymupdf produced empty output for {pdf_path.name} "
                f"(page_count={page_count}); the PDF may be image-only "
                "or have a damaged text layer. Try --extractor mineru "
                "or rerun OCR before indexing."
            )

        return ExtractionResult(
            text=text,
            metadata={
                "extraction_method": "pymupdf_normalized",
                "page_count": page_count,
                "format": "normalized",
                "page_boundaries": page_boundaries,
                "docling_title": "",
                "pdf_title": doc_meta.get("title", ""),
                "pdf_author": doc_meta.get("author", ""),
                "pdf_subject": doc_meta.get("subject", ""),
                "pdf_keywords": doc_meta.get("keywords", ""),
                "pdf_creator": doc_meta.get("creator", ""),
                "pdf_producer": doc_meta.get("producer", ""),
                "pdf_creation_date": doc_meta.get("creationDate", ""),
                "pdf_mod_date": doc_meta.get("modDate", ""),
                "formula_count": 0,
            },
        )
