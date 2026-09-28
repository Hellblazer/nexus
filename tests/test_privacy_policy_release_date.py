# SPDX-License-Identifier: AGPL-3.0-or-later
"""nexus-5zv4j: ``docs/privacy-policy.md``'s effective-date placeholder must
not survive into a tagged release.

THE PLACEHOLDER. Line 3 of the policy carries the effective date. It was
set to a literal ``_Effective: RELEASE-DATE (set when the release carrying
nexus-5zv4j is cut)_`` because the bead's engine half landed with no known
release date yet -- the release skill's Step 4a (``.claude/skills/release/
SKILL.md``) is the mechanism that is supposed to replace it with a real
``YYYY-MM-DD`` in the same commit that bumps the version.

THE PREDICATE, chosen deliberately over the more obvious one. "A tagged
release tree, one whose pyproject version equals the newest v* tag's
version" sounds like a working-tree check (read pyproject.toml's version,
read the newest tag, compare) -- but that comparison is true almost
CONTINUOUSLY on ``develop``, not just at release time: pyproject.toml's
version is bumped ONLY during release prep (release skill Step 3) and
otherwise sits at the LAST shipped tag's version until the next release
starts, per the repo's own release cadence policy. A working-tree-based
version of this test would therefore fail on every ordinary develop commit
between now and the day nexus-5zv4j's own release finally ships -- a
false-positive lint break with no relationship to whether Step 4a was
actually followed.

The honest way to ask "did a tagged release ship the placeholder" is to
read the TAGGED COMMIT'S OWN file, not the working tree: ``git show
<newest-tag>:docs/privacy-policy.md``. A tag's content is immutable, so
this answers the real question exactly once per tag, forever, independent
of how much further development happens afterward. Today the newest tag
predates nexus-5zv4j entirely, so its own committed policy file carries a
real historical date (not the placeholder) and this test passes; it stays
meaningful without editing once nexus-5zv4j's fix actually ships in some
future tag with Step 4a done, and it will catch the tag where Step 4a was
skipped, forever, because that tag's content never changes.

NON-VACUITY. If the newest tag's own ``docs/privacy-policy.md`` has no
``_Effective: ...`` line at all (a parse miss, or the file moved / was
restructured at that tag), the test fails loud rather than silently
passing -- a predicate that cannot find its own subject proves nothing
(the nexus-moht0 vacuous-gate doctrine).
"""
from __future__ import annotations

import re
import subprocess
from pathlib import Path

import pytest

pytestmark = pytest.mark.lint

REPO_ROOT = Path(__file__).parent.parent

_EFFECTIVE_RE = re.compile(r"^_Effective:\s*(.+?)_\s*$", re.MULTILINE)


def _git(*args: str) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        ["git", *args],
        cwd=REPO_ROOT,
        capture_output=True,
        text=True,
        timeout=30,
        check=False,
    )


def newest_tag() -> str | None:
    """Newest published ``vX.Y.Z`` tag reachable from this checkout, or
    ``None`` if none exist (mirrors ``scripts.check_wire_contract_pairing
    .newest_client_tag``'s sort, kept independent rather than imported so
    this lint has no import-time dependency on a script module)."""
    proc = _git("tag", "-l", "v[0-9]*", "--sort=-v:refname")
    if proc.returncode != 0:
        return None
    tags = [line.strip() for line in proc.stdout.splitlines() if line.strip()]
    return tags[0] if tags else None


def file_at_tag(tag: str, path: str) -> str | None:
    """Contents of *path* as committed at *tag*, or ``None`` if unreadable."""
    proc = _git("show", f"{tag}:{path}")
    return proc.stdout if proc.returncode == 0 else None


def test_newest_tag_is_found() -> None:
    """Non-vacuity floor for the real test below: this repo has shipped
    tags (v7.63.0 and earlier at the time this test was written), so a
    ``None`` result means the git invocation itself is broken (a shallow
    checkout, a missing git binary), not a legitimately tagless repo."""
    assert newest_tag() is not None, (
        "no vX.Y.Z tag found in this checkout -- git tag listing is broken "
        "or this is an unexpectedly shallow clone; the test below cannot "
        "check anything without a tag to read"
    )


def test_privacy_policy_effective_date_is_stamped_at_the_newest_tag() -> None:
    tag = newest_tag()
    assert tag is not None  # test_newest_tag_is_found is the diagnostic for this

    content = file_at_tag(tag, "docs/privacy-policy.md")
    assert content is not None, (
        f"docs/privacy-policy.md did not exist, or could not be read, at {tag}"
    )

    m = _EFFECTIVE_RE.search(content)
    assert m, (
        f"docs/privacy-policy.md at {tag} has no '_Effective: ..._' line at "
        "all -- the parse this test relies on found nothing to check, which "
        "is a failure, not a pass"
    )

    assert not m.group(1).startswith("RELEASE-DATE"), (
        f"docs/privacy-policy.md's effective-date line at the shipped tag "
        f"{tag} still reads the RELEASE-DATE placeholder ({m.group(1)!r}) -- "
        "the release that cut this tag skipped the release skill's Step 4a "
        "(stamp the real date); see .claude/skills/release/SKILL.md"
    )


if __name__ == "__main__":
    raise SystemExit(pytest.main([__file__, "-v"]))
