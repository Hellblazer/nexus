# SPDX-License-Identifier: AGPL-3.0-or-later
"""Plugin text a user's model reads names no T2 record only the operator has.

A line such as ``(T2 nexus/prompt-ratchet-history-test-2026-08-17)`` in a skill
sends the reader to a record in the maintainer's own store; a user's T2 has no
project ``nexus`` and no such title, so the pointer dead-ends (user-surface
inventory 2026-11-04, row A-3159). The scan reads every markdown file under
``conexus/`` that reaches a model or a user (skills, commands, agents,
resources, the READMEs) and flags ``T2 nexus/<title>`` and ``T2 `nexus/<title>```.
The release ledger and changelogs record history and are skipped.

File-only, so it runs in the ``-m lint`` job. A planted fixture proves the rule
fires, and the live check asserts it scanned files.
"""
from __future__ import annotations

import re
from pathlib import Path

import pytest

pytestmark = pytest.mark.lint

REPO_ROOT = Path(__file__).resolve().parent.parent
CONEXUS_DIR = REPO_ROOT / "conexus"
_SKIP_NAMES = frozenset({"CHANGELOG.md", "PENDING_RELEASE.md"})
_OPERATOR_RECORD = re.compile(r"\bT2\s+`?nexus/[A-Za-z0-9][A-Za-z0-9_.-]*")

#: (file relative to conexus/, text fragment) -> reason. A site that must stay
#: names why; an entry whose fragment no longer matches is dead weight, delete it.
_ALLOWED: dict[tuple[str, str], str] = {
    ("skills/orchestration/reference.md", "nx-answer-capability-analysis-2026-08-19"): (
        "reported to the lane A branch (nexus-z7ddd), which owns that file this round; "
        "delete this entry when its fix lands"
    ),
}


def _scan(root: Path) -> tuple[int, list[str]]:
    """(files examined, offending ``path:line: text`` strings)."""
    examined = 0
    found: list[str] = []
    for md in sorted(root.rglob("*.md")):
        if md.name in _SKIP_NAMES:
            continue
        examined += 1
        rel = md.relative_to(root).as_posix()
        for lineno, line in enumerate(md.read_text(encoding="utf-8").splitlines(), 1):
            if not _OPERATOR_RECORD.search(line):
                continue
            if any(rel == f and frag in line for (f, frag) in _ALLOWED):
                continue
            found.append(f"{rel}:{lineno}: {line.strip()[:120]}")
    return examined, found


def test_the_rule_fires_on_planted_text(tmp_path: Path) -> None:
    (tmp_path / "skills").mkdir()
    (tmp_path / "skills" / "a.md").write_text(
        "Counter-metric (T2 nexus/prompt-ratchet-history-test-2026-08-17): x\n"
        "Measured (T2 `nexus/some-record-2026-08-19`): y\n"
        "A user's own T2 project is fine: memory_get(project=\"myproj\").\n",
        encoding="utf-8",
    )
    (tmp_path / "CHANGELOG.md").write_text("history: T2 nexus/old-record\n", encoding="utf-8")
    examined, found = _scan(tmp_path)
    assert examined == 1
    assert [f.split(":")[1] for f in found] == ["1", "2"]


def test_no_plugin_text_names_an_operator_only_t2_record() -> None:
    examined, found = _scan(CONEXUS_DIR)
    assert examined > 100, f"scanned only {examined} markdown files under {CONEXUS_DIR}"
    assert not found, (
        "plugin text names a T2 record in the maintainer's store, which a user "
        "does not have; state the fact inline or link a public page:\n  " + "\n  ".join(found)
    )


def test_every_allowlist_entry_still_matches() -> None:
    for (rel, frag), reason in _ALLOWED.items():
        text = (CONEXUS_DIR / rel).read_text(encoding="utf-8")
        assert frag in text, f"stale allowlist entry ({reason}): {rel} no longer contains {frag!r}"
