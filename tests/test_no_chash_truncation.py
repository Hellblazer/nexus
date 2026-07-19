# SPDX-License-Identifier: AGPL-3.0-or-later
"""RDR-180 tripwire: NO new chash truncation (critic-chash-window M2).

The [:32] chunk-id truncation was "retired" TWICE before the nexus-p78a0
rehearsal caught two more live sites — ``HttpVectorClient.put``'s
``doc_id = content_hash[:32]`` (which 409'd every service-mode store_put
against the cohort engine's octet CHECK) and five width-normalization
sites in ``HttpCatalogClient``. Both were hand-rolled derivations that
bypassed the canonical width boundary (:mod:`nexus.chunk_identity`).

This test is the mechanical guard against a THIRD recurrence — the
``test_no_new_sqlite.py`` shape: grep the source tree for the truncation
idioms; any hit outside the explicit allowlist fails with instructions.
The allowlist entries are DELIBERATE legacy-era handling (dual-width read
tolerance, migration-source debt), each annotated with why it may slice.
"""
from __future__ import annotations

import re
from pathlib import Path

SRC = Path(__file__).resolve().parents[1] / "src" / "nexus"

#: The truncation idioms that produced the RDR-180 bug class. Comments and
#: docstrings are stripped before matching, so prose history stays legal.
_TRUNCATION_RE = re.compile(
    r"hexdigest\(\)\s*\[\s*:\s*32\s*\]"      # sha256(...).hexdigest()[:32]
    r"|\bchash\s*\[\s*:\s*32\s*\]"           # chash[:32]
    r"|\bhex_chash\s*\[\s*:\s*32\s*\]"       # hex_chash[:32]
    r"|\bcontent_hash\s*\[\s*:\s*32\s*\]"    # content_hash[:32]
    r"|\bchunk_text_hash\s*\[\s*:\s*32\s*\]"  # chunk_text_hash[:32]
)

#: (path-suffix, why it may slice). Keep this list SHORT and justified —
#: additions need the same scrutiny a new sqlite3.connect gets.
_ALLOWLIST: tuple[tuple[str, str], ...] = (
    (
        "chunk_identity.py",
        "THE canonical width boundary — owns the legacy derivation and its history",
    ),
    (
        "catalog/synthesizer.py",
        "deliberate dual-width cache tolerance: tries full width first, falls back "
        "to the legacy 32-prefix for pre-rekey rows (read-only)",
    ),
    (
        "db/t2/chash_index.py",
        "SQLite-era migration-source debt (RDR-186): its consumers operate on "
        "same-era truncated ids; frozen, never a destination",
    ),
    (
        "db/migrations.py",
        "SQLite-era migration-source debt (RDR-186): frozen legacy schema history",
    ),
)

_COMMENT_RE = re.compile(r"#[^\n]*")
_DOCSTRING_RE = re.compile(r'("""|\'\'\')(?:.|\n)*?\1')


def _code_only(text: str) -> str:
    return _COMMENT_RE.sub("", _DOCSTRING_RE.sub("", text))


def test_no_new_chash_truncation() -> None:
    offenders: list[str] = []
    for path in sorted(SRC.rglob("*.py")):
        rel = path.relative_to(SRC).as_posix()
        if any(rel.endswith(suffix) for suffix, _ in _ALLOWLIST):
            continue
        code = _code_only(path.read_text(encoding="utf-8", errors="replace"))
        for m in _TRUNCATION_RE.finditer(code):
            line = code[: m.start()].count("\n") + 1
            offenders.append(f"src/nexus/{rel}:~{line}: {m.group(0)!r}")
    assert not offenders, (
        "chash truncation idiom found OUTSIDE the allowlist — the chunk id is "
        "the FULL sha256 digest (RDR-180); derive ids through nexus.chunk_identity, "
        "never a hand-rolled [:32] slice. If this site is genuinely legacy-era "
        "read tolerance, add it to _ALLOWLIST with a justification:\n  "
        + "\n  ".join(offenders)
    )


def test_allowlist_entries_still_exist() -> None:
    """Non-vacuity: a renamed/deleted allowlist file must be pruned, not
    silently skipped forever."""
    for suffix, _ in _ALLOWLIST:
        assert list(SRC.rglob(suffix.split("/")[-1])), (
            f"allowlist entry {suffix!r} matches no file — prune it"
        )
