# SPDX-License-Identifier: AGPL-3.0-or-later
"""Repo-wide lint: every host-runnable `nx init --yes` must carry
`--no-autostart` (nexus-d5yu5).

The mechanism (verified, not inferred; see `tests/AGENTS.md` § "E2E
isolation: a sandboxed HOME does NOT isolate a service install", and
`src/nexus/daemon/installer.py`'s `_activate_cmd`): `nx init -y`
without `--no-autostart` runs `launchctl bootstrap gui/$UID <plist>`
(Linux: `systemctl --user enable --now`), and that domain is keyed on
**uid**, not `$HOME`. The unit label is the hard constant
`_SERVICE_LAUNCHD_LABEL = "com.nexus.service"`
(`src/nexus/commands/daemon.py`) -- never derived from `$HOME` or
`NEXUS_CONFIG_DIR`. So a harness that swaps `$HOME` (a sandboxed venv,
an `env -i HOME=$HOME_DIR` wrapper such as this repo's `_nx()` helper,
a scratch `NEXUS_CONFIG_DIR`) still registers into the SAME domain
under the SAME label as the developer's real, production, cloud-mode
install running on this box. Worse: `InstallStatus.ALREADY_PRESENT`
reports an existing identical unit as success, so a harness on that
path can silently poll and pass against PRODUCTION's lease rather than
its own.

Two things keep every current site safe:

1. The non-interactive consent gate `_decide_autostart`
   (`src/nexus/commands/init.py`) -- a bare `nx init` (no `-y`/`--yes`)
   never writes a unit at all.
2. Explicit `--no-autostart` on every HOST-run harness that does pass
   `--yes`.

Nothing pinned invariant 2 until this file. The lawful escape hatch is
CONTAINER execution: every `tests/e2e/migration-rehearsal/rehearse_*.sh`
script runs inside Docker, so its launchd/systemd domain is the
container's, not the developer's. This lint asserts that every
`nx init` (or the repo's `_nx init` wrapper form) invocation carrying
`-y`/`--yes` without `--no-autostart`, anywhere under `tests/e2e/**` or
`scripts/**`, is one of the ten named container sites below -- named by
exact file AND exact per-file count, both directions, so a new
host-side site, a moved/renamed container script, or a stale allowlist
entry are all hard failures, never a silent pass.

Deliberately dumb (regex, per line) -- same philosophy as
`test_shell_continuation_lint.py` / `test_no_new_sqlite.py`: the goal
is a tripwire that cannot be silently satisfied, not a shell parser.
"""

from __future__ import annotations

import re
import subprocess
from dataclasses import dataclass
from pathlib import Path

import pytest

pytestmark = pytest.mark.lint

REPO_ROOT = Path(__file__).resolve().parent.parent

#: `nx init` or `_nx init` (this repo's host-side `env -i HOME=... nx`
#: wrapper defined in fresh-install-mvv.sh / warm-reindex-skip-gate.sh /
#: local-index-memory-gate.sh) -- a wrapped HOME is exactly the thing that
#: does NOT isolate the launchd/systemd domain, so the wrapper form must be
#: scanned identically to the bare form.
_INIT_INVOCATION_RE = re.compile(r"(?<![\w])_?nx\s+init(?=\s|$)")
_YES_FLAG_RE = re.compile(r"(?:^|\s)(?:-y|--yes)(?:\s|$)")
_NO_AUTOSTART_RE = re.compile(r"--no-autostart")

#: Explicit, exact-ledger allowlist: every file permitted to carry an
#: `nx init ... -y/--yes` site with no `--no-autostart` on the same line,
#: mapped to its exact live count of such sites. Both directions matter --
#: growth beyond the named count (a new site) is a hard failure; a count
#: that drops below the named number means a site died and the entry must
#: be lowered so the ledger stays exact (see test_ledger_matches_live_count
#: below). All ten are migration-rehearsal harnesses, run inside Docker
#: (nexus-d5yu5 investigation) -- their launchd/systemd domain is the
#: container's, not the developer's live machine's.
CONTAINER_ALLOWLIST: dict[str, int] = {
    "tests/e2e/migration-rehearsal/rehearse_era_hop.sh": 1,
    "tests/e2e/migration-rehearsal/rehearse_acquire.sh": 1,
    "tests/e2e/migration-rehearsal/rehearse_package_upgrade.sh": 2,
    "tests/e2e/migration-rehearsal/rehearse_stranded.sh": 3,
    "tests/e2e/migration-rehearsal/rehearse_chash_window.sh": 1,
    "tests/e2e/migration-rehearsal/rehearse_cold.sh": 1,
    "tests/e2e/migration-rehearsal/rehearse_hole_punch.sh": 1,
}

_DIRECTIVE = (
    "'nx init'/'_nx init' carrying -y/--yes without --no-autostart runs "
    "'launchctl bootstrap gui/$UID <plist>' (Linux: 'systemctl --user "
    "enable --now'), a domain keyed on UID, not $HOME -- it registers "
    "into the SAME domain, under the SAME hard-coded label "
    "'com.nexus.service', as the developer's live production install on "
    "this box. A sandboxed HOME/NEXUS_CONFIG_DIR does NOT protect against "
    "this (tests/AGENTS.md 'E2E isolation: a sandboxed HOME does NOT "
    "isolate a service install'). Either add --no-autostart to this "
    "invocation, or if this harness is genuinely container-executed "
    "(Docker), add it by exact path to CONTAINER_ALLOWLIST in this file "
    "with a reason. nexus-d5yu5."
)


@dataclass(frozen=True)
class _Site:
    file: str
    line: int
    text: str


def _tracked_shell_scripts() -> list[Path]:
    out = subprocess.run(
        ["git", "ls-files", "tests/e2e/*.sh", "tests/e2e/**/*.sh", "scripts/*.sh", "scripts/**/*.sh"],
        cwd=REPO_ROOT, capture_output=True, text=True, check=True,
    ).stdout
    seen: set[str] = set()
    paths: list[Path] = []
    for rel in out.splitlines():
        if not rel or rel in seen:
            continue
        seen.add(rel)
        paths.append(REPO_ROOT / rel)
    return paths


def _find_unguarded_yes_sites(lines: list[str]) -> list[tuple[int, str]]:
    """Return (1-based line number, line text) for every line that invokes
    `nx init`/`_nx init` with -y/--yes and no --no-autostart on that same
    line. Comment lines are skipped -- every real invocation observed in
    this repo is a live command, never prose (a genuine host-side
    violation is a command that RUNS, not a mention of one)."""
    hits: list[tuple[int, str]] = []
    for i, raw in enumerate(lines, start=1):
        stripped = raw.strip()
        if stripped.startswith("#"):
            continue
        if not _INIT_INVOCATION_RE.search(raw):
            continue
        if not _YES_FLAG_RE.search(raw):
            continue
        if _NO_AUTOSTART_RE.search(raw):
            continue
        hits.append((i, stripped))
    return hits


def _scan_repo() -> list[_Site]:
    sites: list[_Site] = []
    for script in _tracked_shell_scripts():
        lines = script.read_text(encoding="utf-8", errors="replace").splitlines()
        rel = script.relative_to(REPO_ROOT).as_posix()
        for lineno, text in _find_unguarded_yes_sites(lines):
            sites.append(_Site(file=rel, line=lineno, text=text))
    return sites


# ── Falsification controls (the detector must actually detect) ──────────────


def test_detector_flags_the_bare_host_side_shape() -> None:
    """Synthetic positive: a host-run `nx init --service --yes` with no
    --no-autostart must be flagged -- this is the exact shape that would
    collide with the developer's production launchd/systemd unit."""
    lines = ['echo "provisioning"', "nx init --service --embedder bge-768 --yes", "echo done"]
    hits = _find_unguarded_yes_sites(lines)
    assert hits == [(2, "nx init --service --embedder bge-768 --yes")]


def test_detector_flags_the_wrapper_form() -> None:
    """The repo's `_nx()` HOME-swap wrapper does not change the launchd/
    systemd domain (uid-keyed, not HOME-keyed) -- it must be scanned
    identically to the bare `nx init` form."""
    lines = ["_nx init -y"]
    assert _find_unguarded_yes_sites(lines) == [(1, "_nx init -y")]


def test_detector_ignores_no_autostart_guarded_sites() -> None:
    """The two sanctioned host-side defenses: --no-autostart on the same
    line, or no -y/--yes flag at all (the consent gate declines)."""
    lines = [
        "nx init -y --no-autostart",
        "nx init --service --embedder bge-768 --no-autostart",
        "nx init --service",
        "_nx init >\"$LOGS/init.log\" 2>&1",
    ]
    assert _find_unguarded_yes_sites(lines) == []


def test_detector_ignores_comment_prose() -> None:
    """A doc comment that mentions the shape in prose is not a live
    invocation -- only commands that actually run are in scope."""
    lines = ["# e.g. 'nx init --service --yes' would collide with prod"]
    assert _find_unguarded_yes_sites(lines) == []


def test_scanner_is_nonvacuous() -> None:
    """The sweep must actually see the tracked tree, and must see the ten
    known lawful sites -- an empty result here means the enumeration or
    the regex broke, not that every site vanished."""
    scripts = _tracked_shell_scripts()
    assert len(scripts) >= 10, f"suspicious sweep: only {len(scripts)} scripts enumerated"
    live_sites = _scan_repo()
    assert live_sites, (
        "scan found zero unguarded -y/--yes nx init sites anywhere, but "
        "CONTAINER_ALLOWLIST names 10 -- the scanner broke (path drift, "
        "regex regression), it did not discover that every site died"
    )


# ── The pinned invariant ──────────────────────────────────────────────────


def test_every_unguarded_yes_init_site_is_container_executed() -> None:
    """nexus-d5yu5: every `nx init`/`_nx init` invocation under
    tests/e2e/** or scripts/** that carries -y/--yes without
    --no-autostart must be one of the named migration-rehearsal
    (Docker-executed) sites. A new site outside the allowlist fails
    loudly, naming the file, the line, and the exact collision
    mechanism -- it never reaches CI red; it reaches the developer's live
    machine."""
    live_sites = _scan_repo()

    offenders = [s for s in live_sites if s.file not in CONTAINER_ALLOWLIST]
    assert not offenders, (
        "unguarded 'nx init ... -y/--yes' (no --no-autostart) outside the "
        "named container allowlist:\n  "
        + "\n  ".join(f"{s.file}:{s.line}  {s.text!r}" for s in offenders)
        + f"\n\n{_DIRECTIVE}"
    )


def test_ledger_matches_live_count() -> None:
    """Exact-ledger discipline (both directions): the live per-file count
    of allowlisted sites must equal CONTAINER_ALLOWLIST's named count. A
    live count BELOW the ledger means a site was fixed/removed and the
    entry must be lowered (a stale high entry is a free slot a future
    unguarded site could occupy unreviewed). A live count ABOVE the
    ledger is caught by test_every_unguarded_yes_init_site_is_
    container_executed already (a file already in the allowlist gaining
    an EXTRA site still deserves review, since each site is a distinct
    launchd/systemd-domain-touching command)."""
    live_sites = _scan_repo()
    live_counts: dict[str, int] = {}
    for s in live_sites:
        live_counts[s.file] = live_counts.get(s.file, 0) + 1

    mismatches = sorted(
        f"{f}: live={live_counts.get(f, 0)} ledger={CONTAINER_ALLOWLIST.get(f, 0)}"
        for f in live_counts.keys() | CONTAINER_ALLOWLIST.keys()
        if live_counts.get(f, 0) != CONTAINER_ALLOWLIST.get(f, 0)
    )
    assert not mismatches, (
        "CONTAINER_ALLOWLIST count drifted from the live scan (raise the "
        "entry for a genuine new container-side site with review; lower "
        "it if a site was fixed/removed): " + ", ".join(mismatches)
    )
