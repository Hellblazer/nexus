# SPDX-License-Identifier: AGPL-3.0-or-later
"""Prose lint: surface patterns of the LLM register, checked by regex.

The rule set is the part of ``docs/writing-style.md`` a machine can
check. Every rule is either a standing house rule (em dash,
"load-bearing") or a pattern with measured or lineaged backing in the
research record (T3 ``research-ai-slop-prose-removal-2026-08-21``).
Readability formulas are deliberately absent: they score syllables and
sentence length, and a text can hit any score without getting clearer.

Fenced code, inline code, YAML frontmatter and HTML comments are not
prose and are masked before matching. Line numbers refer to the
original file.
"""
from __future__ import annotations

import json
import re
from dataclasses import dataclass
from pathlib import Path

# "may" is matched lowercase-only so the month ("The May 2026 release")
# never counts as a hedge; a sentence-initial "May ..." hedge is a rare,
# accepted miss. The other hedges keep IGNORECASE via inline (?i:).
_HEDGES = r"(?:(?i:could|might|potentially|arguably|somewhat|relatively|perhaps)|may)"

# (rule, pattern, message). Patterns run per line unless noted.
_LINE_RULES: tuple[tuple[str, re.Pattern[str], str], ...] = (
    (
        "em-dash",
        re.compile("—"),
        "em dash; use a comma, colon, period, or parentheses",
    ),
    (
        "load-bearing",
        re.compile(r"\bload[- ]bearing\b", re.IGNORECASE),
        "say what is meant: relied-upon, decisive, on the critical path",
    ),
    (
        "marker-lexicon",
        re.compile(
            r"\b(?:delv(?:e|es|ed|ing)|tapestry|plethora|meticulous(?:ly)?"
            r"|seamless(?:ly)?|boast(?:s|ed|ing)?|leverag(?:e|es|ed|ing)"
            r"|game-changer|paradigm shift"
            r"|it is worth noting|it's worth noting|it is important to note)\b",
            re.IGNORECASE,
        ),
        "LLM marker word; use the plain term (use, cover, careful, important)",
    ),
    (
        "contrast-frame",
        re.compile(
            r"\b(?:not|isn['\u2019]t|aren['\u2019]t|wasn['\u2019]t|weren['\u2019]t|is not|are not)"
            r" (?:just|only|merely|simply) .{1,60}?"
            r",\s*(?:it['\u2019]?s|it is|this is|that is)\b",
            re.IGNORECASE,
        ),
        "'not X, it's Y' contrast frame; state Y, or show the real tension",
    ),
    (
        "vague-attribution",
        re.compile(
            r"\b(?:some|many|most) (?:critics|experts|people|researchers|observers|users|engineers)"
            r" (?:argue|believe|say|suggest|claim|feel|think)\b",
            re.IGNORECASE,
        ),
        "unnamed source; name who, or drop the appeal",
    ),
)

_OPENER = re.compile(
    r"^\s*(?:[>*-]\s+)*(?:(?:Great|Excellent|Good|Fantastic) (?:question|point|idea)"
    r"|Certainly!"
    r"|Absolutely!"
    r"|I'd be happy to\b"
    r"|I would be happy to\b)",
    re.IGNORECASE,
)

_CLOSER = re.compile(
    r"^\s*(?:[>*-]\s+)*(?:In conclusion|In summary|To summarize|To sum up),",
    re.IGNORECASE,
)
# Split on terminal punctuation followed by an uppercase/quote/paren start,
# so "e.g. this" and "vs. that" stay inside their sentence.
_SENTENCE_SPLIT = re.compile(r"(?<=[.!?])\s+(?=[A-Z0-9`*\"'(\[])")
_HEDGE = re.compile(rf"\b{_HEDGES}\b")

# Closer must use the same character run as the opener (CommonMark: at least
# as long; we accept same-or-longer). An unterminated fence masks to EOF,
# which is what a renderer does with it too.
_FENCE = re.compile(
    r"^ {0,3}(`{3,}|~{3,})[^\n]*$.*?(?:^ {0,3}\1[`~]*[ \t]*$|\Z)",
    re.MULTILINE | re.DOTALL,
)
_INLINE_CODE = re.compile(r"`[^`\n]*`")
_HTML_COMMENT = re.compile(r"<!--.*?-->", re.DOTALL)
_FRONTMATTER = re.compile(r"\A---\n.*?\n---[^\n]*\n?", re.DOTALL)


@dataclass(frozen=True, slots=True)
class Finding:
    line: int
    rule: str
    message: str
    excerpt: str

    def render(self, path: Path | str) -> str:
        return f"{path}:{self.line}: {self.rule}: {self.message} ({self.excerpt!r})"


def _blank_preserving_lines(text: str, pattern: re.Pattern[str]) -> str:
    """Replace each match with spaces, keeping newlines so line numbers hold."""

    def _mask(m: re.Match[str]) -> str:
        return re.sub(r"[^\n]", " ", m.group(0))

    return pattern.sub(_mask, text)


def mask_non_prose(text: str) -> str:
    for pat in (_FRONTMATTER, _FENCE, _HTML_COMMENT, _INLINE_CODE):
        text = _blank_preserving_lines(text, pat)
    return text


def _excerpt(line: str, m: re.Match[str] | None) -> str:
    if m is None:
        return line.strip()[:60]
    start = max(0, m.start() - 20)
    return line[start : m.end() + 20].strip()[:80]


def lint_text(text: str) -> list[Finding]:
    """Return findings for one markdown document."""
    masked = mask_non_prose(text)
    findings: list[Finding] = []
    lines = masked.split("\n")
    prev_blank = True
    for idx, line in enumerate(lines, start=1):
        for rule, pat, msg in _LINE_RULES:
            for m in pat.finditer(line):
                findings.append(Finding(idx, rule, msg, _excerpt(line, m)))
        # Closers only count at paragraph start: the first line after a
        # blank line (or the file start). Mid-paragraph "Overall," is a
        # normal transition, not the formulaic wrap-up.
        if prev_blank and (m := _CLOSER.search(line)):
            findings.append(
                Finding(idx, "formulaic-closer", "restates instead of adding; cut or synthesize",
                        _excerpt(line, m))
            )
        if prev_blank and (m := _OPENER.search(line)):
            findings.append(
                Finding(idx, "sycophantic-opener", "sycophantic opener; start with the content",
                        _excerpt(line, m))
            )
        for sentence in _SENTENCE_SPLIT.split(line):
            if len(_HEDGE.findall(sentence)) >= 2:
                findings.append(
                    Finding(idx, "hedge-stack", "two or more hedges in one sentence; commit or drop",
                            sentence.strip()[:80])
                )
        prev_blank = not line.strip()
    findings.sort(key=lambda f: (f.line, f.rule))
    return findings


def lint_file(path: Path) -> list[Finding]:
    return lint_text(path.read_text(encoding="utf-8", errors="replace"))


# --- ratchet baseline ----------------------------------------------------


def load_baseline(path: Path) -> dict[str, int]:
    if not path.exists():
        return {}
    data = json.loads(path.read_text(encoding="utf-8"))
    return {str(k): int(v) for k, v in data.items()}


def write_baseline(path: Path, counts: dict[str, int]) -> None:
    kept = {k: v for k, v in sorted(counts.items()) if v > 0}
    path.write_text(json.dumps(kept, indent=2, sort_keys=True) + "\n", encoding="utf-8")


def check_against_baseline(
    counts: dict[str, int], baseline: dict[str, int]
) -> list[str]:
    """Return violations. A file may never exceed its baseline; a baseline
    may never sit above the file's real count (stale-high), and may not
    name a file that was not scanned."""
    problems: list[str] = []
    for rel, n in sorted(counts.items()):
        allowed = baseline.get(rel, 0)
        if n > allowed:
            problems.append(f"{rel}: {n} findings > baseline {allowed}")
        elif n < allowed:
            problems.append(
                f"{rel}: {n} findings, baseline {allowed} is stale; lower it to {n}"
            )
    for rel in sorted(set(baseline) - set(counts)):
        problems.append(f"{rel}: in baseline but not scanned; remove the entry")
    return problems
