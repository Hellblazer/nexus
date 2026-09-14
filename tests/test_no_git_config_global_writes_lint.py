# SPDX-License-Identifier: AGPL-3.0-or-later
"""No tracked script writes `git config --global` (nexus-oqh4s).

THE INCIDENT. ``tests/e2e/migration-rehearsal/rehearse_package_upgrade.sh``
ran ``git config --global user.email/user.name`` unconditionally near its
top. Every header in this family says the script "runs INSIDE the
container" (``Dockerfile.package-upgrade``'s ENTRYPOINT), but nothing in
the script itself enforces that — a bare invocation of the harness on a
real host runs the same lines against that host's real ``~/.gitconfig``.
On 2026-09-12 20:02 PDT it did exactly that, rewriting the operator's own
git identity: every commit made on this host afterward carried the
harness's throwaway author until the mistake was noticed.

A sibling sweep of the whole repo (nexus-oqh4s) found the identical
pattern in eight more scripts under the same directory
(``rehearse_era_hop.sh``, ``rehearse_shakeout.sh``, ``rehearse_cold.sh``,
``rehearse_acquire.sh``, ``rehearse.sh``, ``rehearse_candidate_migration.sh``,
``rehearse_stranded.sh``, ``rehearse_hole_punch.sh``) — the same
copy-pasted two lines, each with its own throwaway identity string. All
nine were converted to exported ``GIT_AUTHOR_NAME`` / ``GIT_AUTHOR_EMAIL``
/ ``GIT_COMMITTER_NAME`` / ``GIT_COMMITTER_EMAIL`` env vars, which reach
every ``git`` invocation in the same process tree (including a `git
init`/commit a legacy-era subprocess or a fake-repo helper performs)
without mutating any config file anywhere, host or container alike.

THIS LINT holds the line: it fails on any tracked shell script whose
non-comment text writes a global (or system) git identity/config value.
It does not attempt to catch every conceivable git-config write shape —
a BARE (unscoped) ``git config key value`` defaults to writing the
CURRENT repo's local config, which is a real but different hazard (it
depends on the ambient cwd rather than a fixed host-wide file); the one
confirmed instance of that shape in this repo
(``tests/e2e/upgrade-shakeout.sh``) runs inside a freshly ``git init``'d
throwaway directory it built for exactly this purpose, audited by hand as
part of the same nexus-oqh4s sweep, and is out of scope for this
mechanized check. ``--global``/``--system`` name a FIXED file outside any
repo, which is the actual shape that caused the incident and the one this
lint mechanizes against a recurrence.

A line is scanned only when it is not a comment (its stripped text does
not start with ``#``) — a script may still document the incident and the
forbidden command in its own comments (as the nine fixed scripts now do)
without tripping this lint on its own prose.
"""
from __future__ import annotations

import pathlib
import re

import pytest

pytestmark = pytest.mark.lint

REPO_ROOT = pathlib.Path(__file__).resolve().parent.parent

#: A non-comment line invoking `git config` with a host-wide write scope.
#: Matches `--global` or `--system` in any argument position after `config`
#: (git accepts the scope flag before or interleaved with other config
#: flags), case-sensitive (git's own flags are).
_GIT_CONFIG_GLOBAL_WRITE_RE = re.compile(
    r"^(?!\s*#).*\bgit\s+config\b(?:\s+\S+)*?\s+--(global|system)\b"
)


def _violations(text: str) -> list[str]:
    hits = []
    for line in text.splitlines():
        if _GIT_CONFIG_GLOBAL_WRITE_RE.search(line):
            hits.append(line.strip())
    return hits


def test_scan_is_non_vacuous() -> None:
    """A scan that silently walked zero shell scripts would make the real
    assertion below pass on an empty set -- pin a floor so a broken glob
    (wrong extension, wrong root) fails loud instead of quietly checking
    nothing."""
    scripts = sorted(REPO_ROOT.rglob("*.sh"))
    scripts = [p for p in scripts if ".git" not in p.parts]
    assert len(scripts) >= 50, (
        f"only found {len(scripts)} tracked .sh files under {REPO_ROOT} -- "
        "the scan may be broken (wrong root or extension) rather than the "
        "repo genuinely shrinking to fewer than 50 shell scripts"
    )


def test_detector_flags_the_incident_shape() -> None:
    """Kill control: proves the regex actually detects the exact two-line
    shape that caused the 2026-09-12 incident, on a synthetic fixture --
    never on a real repo file, so this can never pass vacuously because the
    real files happen to already be clean."""
    synthetic = (
        'export NX_SERVICE_MAX_HEAP="${NX_SERVICE_MAX_HEAP:-1g}"\n'
        'git config --global user.email "incident@nexus.local" >/dev/null 2>&1 || true\n'
        'git config --global user.name  "nexus incident"       >/dev/null 2>&1 || true\n'
    )
    hits = _violations(synthetic)
    assert len(hits) == 2, f"expected 2 flagged lines, got {hits}"


def test_detector_ignores_comment_lines() -> None:
    """A comment documenting the forbidden command (exactly what the nine
    fixed scripts now carry) must not itself trip this lint."""
    synthetic = (
        "# Identity via env, never `git config --global` (nexus-oqh4s): this\n"
        '# script is meant to run INSIDE its container, but nothing enforces that.\n'
        'export GIT_AUTHOR_NAME="nexus incident"\n'
    )
    assert _violations(synthetic) == []


def test_detector_ignores_scoped_local_config() -> None:
    """A `git config` call with no `--global`/`--system` (the default,
    local-repo-scoped write `tests/e2e/upgrade-shakeout.sh` uses inside its
    own throwaway `git init`'d directory) is out of this lint's scope --
    see the module docstring for why."""
    synthetic = 'git config user.email t@t.invalid && git config user.name T\n'
    assert _violations(synthetic) == []


def test_no_tracked_script_writes_git_config_global_or_system() -> None:
    scripts = sorted(REPO_ROOT.rglob("*.sh"))
    scripts = [p for p in scripts if ".git" not in p.parts]
    bad: list[str] = []
    for path in scripts:
        text = path.read_text(encoding="utf-8", errors="replace")
        for line in _violations(text):
            bad.append(f"{path.relative_to(REPO_ROOT)}: {line}")
    assert not bad, (
        "tracked script(s) write a global/system git config value -- this "
        "mutates a real host's ~/.gitconfig (or /etc/gitconfig) the instant "
        "the script runs outside its intended container (nexus-oqh4s, "
        "2026-09-12 incident): reattribute via exported GIT_AUTHOR_NAME / "
        "GIT_AUTHOR_EMAIL / GIT_COMMITTER_NAME / GIT_COMMITTER_EMAIL "
        "instead, which reaches every git invocation in the same process "
        f"tree without touching any config file:\n" + "\n".join(bad)
    )
