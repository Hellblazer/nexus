# SPDX-License-Identifier: AGPL-3.0-or-later
"""Plugin text a user's model reads names no T2 record only the operator has.

A line such as ``(T2 nexus/prompt-ratchet-history-test-2026-08-17)`` or
``see T2 `nexus_rdr/067-spike-disposition` (id 754)`` in a skill sends the
reader to a record in the maintainer's own store; a user's T2 has no project
``nexus`` or ``nexus_rdr`` and no such title, so the pointer dead-ends
(user-surface inventory 2026-11-04, rows A-3159, A-2377). The scan reads every
markdown file under ``conexus/`` that reaches a model or a user (skills,
commands, agents, resources, the READMEs) and flags every citation form: a
``nexus/<title>`` or ``nexus_rdr/<title>`` pair (backticked or bare, with or
without a ``T2`` lead-in or a date suffix) and a ``project="nexus"`` /
``project="nexus_rdr"`` argument. The release ledger and changelogs record
history and are skipped. This is the only rule for the class; it replaces the
dated-suffix-only rule that tests/test_repo_only_references_lint.py carried on
the sibling branch.

File-only, so it runs in the ``-m lint`` job. A planted fixture proves each
form fires, and the live check asserts it scanned files.
"""
from __future__ import annotations

import re
from pathlib import Path

import pytest

pytestmark = pytest.mark.lint

REPO_ROOT = Path(__file__).resolve().parent.parent
CONEXUS_DIR = REPO_ROOT / "conexus"
_SKIP_NAMES = frozenset({"CHANGELOG.md", "PENDING_RELEASE.md"})
# ``nexus/x`` and ``nexus_rdr/x`` as a pair, not the tail of a longer path
# (``src/nexus/mcp``) or of another project name (``myproj_nexus/x``).
_OPERATOR_RECORD = re.compile(
    r"(?<![A-Za-z0-9_/.-])`?(?:nexus|nexus_rdr)/[A-Za-z0-9*][A-Za-z0-9_.*-]*"
    r"|\bproject\s*=\s*[\"']?(?:nexus|nexus_rdr)[\"']?\s*[,)]"
)

#: A line that offers the record as an optional override of something the plugin
#: already ships ("T2 `nexus_rdr/x` overrides it when present") is not a dead
#: pointer: a user who has no such record loses nothing.
_OPTIONAL_OVERRIDE = re.compile(r"\boverrides it\b", re.IGNORECASE)

#: (file relative to conexus/, text fragment) -> reason. A site that must stay
#: names why; an entry whose fragment no longer matches is dead weight, delete it.
_AUDIT_PROMPT_REASON = (
    "runtime load of the RDR-067 canonical audit prompt from the maintainer's T2; the "
    "prompt is not shipped with the plugin, so rdr-audit cannot run for a plugin user "
    "until it is. Delete these entries when the prompt ships (the sibling branch ships it as "
    "resources/rdr_process/AUDIT-PROMPT.md and rewrites these lines as overrides, which the "
    "override rule then passes)."
)
_ALLOWED: dict[tuple[str, str], str] = {
    ("skills/rdr-audit-checklist/SKILL.md", "067-canonical-prompt-v1"): _AUDIT_PROMPT_REASON,
    ("commands/rdr-audit.md", "067-canonical-prompt-v1"): _AUDIT_PROMPT_REASON,
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
            if _OPTIONAL_OVERRIDE.search(line):
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
        "Spike (T2 `nexus_rdr/067-spike-disposition` id 754): z\n"
        "See nexus_rdr/067-research-2-audit-methodology for the evidence.\n"
        "**T2 reference**: `nexus_rdr/066-research-5-spike`\n"
        "Pattern `nexus_rdr/*-close-override-*` exceeds 20%.\n"
        "Load memory_get(project=\"nexus_rdr\", title=\"x\") first.\n"
        "Shipped prompt; T2 `nexus_rdr/067-canonical-prompt-v1` overrides it when present.\n"
        "A user's own T2 project is fine: memory_get(project=\"myproj\"), `myproj/decision-2026-08-19`,\n"
        "`{repo}_rdr/*-close-override-*`, and a source path like src/nexus/mcp/core.py or `nexus/` alone.\n",
        encoding="utf-8",
    )
    (tmp_path / "CHANGELOG.md").write_text("history: T2 nexus/old-record\n", encoding="utf-8")
    examined, found = _scan(tmp_path)
    assert examined == 1
    assert [f.split(":")[1] for f in found] == ["1", "2", "3", "4", "5", "6", "7"]


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
