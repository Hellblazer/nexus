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

THIS LINT holds the line: it fails on any tracked shell script, Python
module, or GitHub Actions workflow whose non-comment text writes a
host-wide git identity/config value. It does not attempt to catch every
conceivable git-config write shape -- a BARE (unscoped) ``git config key
value`` defaults to writing the CURRENT repo's local config, which is a
real but different hazard (it depends on the ambient cwd rather than a
fixed host-wide file); the one confirmed instance of that shape in this
repo (``tests/e2e/upgrade-shakeout.sh``) runs inside a freshly ``git
init``'d throwaway directory it built for exactly this purpose, audited by
hand as part of the same nexus-oqh4s sweep, and is out of scope for this
mechanized check. ``--global``/``--system`` name a FIXED file outside any
repo, which is the actual shape that caused the incident and the one this
lint mechanizes against a recurrence; ``--file``/``-f`` pointed at a path
outside the repo or a temp directory is the same hazard under a different
flag, so it is treated the same way.

SCOPE, ROUND 2 (nexus-oqh4s code-review findings 2-4). The original version
of this lint scanned only ``*.sh`` files with a plain text regex. That left
three gaps, all closed here:

1. **Language coverage.** A Python ``subprocess`` call building the same
   argv (list form: ``["git", "config", "--global", ...]``) or a GitHub
   Actions workflow ``run:`` step running the same shell line were both
   invisible. This lint now also scans every tracked ``.py`` file and every
   ``.github/workflows/*.yml``/``*.yaml`` file. A Python list literal is
   matched by normalizing commas/brackets/quotes to whitespace before
   applying the same word-boundary regex used for shell text -- this covers
   the common literal-argv shape; a git-config call assembled through
   string formatting or a helper function is not attempted (the same
   accepted-cost posture the original scan already took for exotic shell
   quoting).
2. **Reads were flagged as writes.** ``git config --global --get
   user.email`` mutates nothing, but the original regex only checked for
   ``--global``/``--system`` and would have flagged it. A line carrying a
   read flag (``--get``, ``--get-all``, ``--get-regexp``,
   ``--get-urlmatch``, ``--list``/``-l``) is now excluded even when
   ``--global``/``--system`` is also present.
3. **``--file``/``-f`` writes were invisible.** ``git config --file
   ~/.gitconfig user.email x`` is the identical host-wide-file hazard as
   ``--global``, under a different flag the original regex never looked
   for. This is now flagged when the named path is not clearly repo- or
   temp-scoped (starts with ``~``, ``$HOME``/``${HOME}``, or is an absolute
   path not under ``/tmp``, ``$TMPDIR``/``${TMPDIR}``, or a ``mktemp``
   result) -- see ``_is_risky_config_file_path`` for the exact heuristic
   and its own accepted false-negative cost (a path built by string
   concatenation or a variable this scan cannot resolve statically).

A line is scanned only when it is not a comment (its stripped text does
not start with ``#``) -- a script may still document the incident and the
forbidden command in its own comments (as the nine fixed scripts now do)
without tripping this lint on its own prose.
"""
from __future__ import annotations

import pathlib
import re
import subprocess

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

#: A `git config` call carrying `--file`/`-f <path>` -- the same host-wide-
#: file hazard as `--global`, under a different flag.
_GIT_CONFIG_FILE_WRITE_RE = re.compile(
    r"^(?!\s*#).*\bgit\s+config\b(?:\s+\S+)*?\s+(?:--file|-f)\s+(?P<path>\S+)"
)

#: A read flag anywhere on the line -- present, the line queries git config
#: rather than mutating it, regardless of `--global`/`--system` also being
#: present (`git config --global --get user.email` reads, it does not
#: write). `--unset`/`--unset-all` are deliberately NOT here: removing a
#: value is still a mutation of the target file.
_READ_FLAG_RE = re.compile(r"--get(?:-all|-regexp|-urlmatch)?\b|(?:^|\s)-l\b|--list\b")

#: Path prefixes that keep a `--file`/`-f` target inside a throwaway/temp
#: scope rather than a real host-wide location.
_SAFE_CONFIG_FILE_PREFIXES = ("/tmp/", "$TMPDIR", "${TMPDIR}", "$(mktemp", "./", "../")

#: Path prefixes/substrings that name a fixed, real host location outside
#: any repo or temp scope -- the same hazard `--global`/`--system` name.
_RISKY_CONFIG_FILE_MARKERS = ("~", "$HOME", "${HOME}", "/etc/", "/root/", "/Users/", "/home/")


def _is_risky_config_file_path(path: str) -> bool:
    """True when *path* (the raw token following `--file`/`-f`) names a
    fixed host-wide location rather than a repo-relative or temp-scoped
    one. Heuristic, same accepted-cost posture as the rest of this lint: a
    path assembled via string concatenation or an unresolvable variable is
    not attempted."""
    path = path.strip("\"'")
    if path.startswith(_SAFE_CONFIG_FILE_PREFIXES):
        return False
    if any(marker in path for marker in _RISKY_CONFIG_FILE_MARKERS):
        return True
    # An absolute path with no other marker (e.g. `/var/lib/x/.gitconfig`)
    # is still outside any repo/temp scope this lint can vouch for.
    return path.startswith("/")


#: Characters a Python list-literal argv (`["git", "config", "--global", ...]`)
#: wraps tokens in, that a plain shell command line never needs quoted the
#: same way -- stripped to whitespace so ONE regex pass (the same
#: word-boundary patterns above) matches both a shell string and a Python
#: argv list without needing a second detector per language.
_LIST_LITERAL_NOISE_RE = re.compile(r"""["'\[\],]""")


def _normalize(line: str) -> str:
    return _LIST_LITERAL_NOISE_RE.sub(" ", line)


def _violations(text: str) -> list[str]:
    hits = []
    for line in text.splitlines():
        if line.lstrip().startswith("#"):
            continue
        normalized = _normalize(line)
        if _READ_FLAG_RE.search(normalized):
            continue
        if _GIT_CONFIG_GLOBAL_WRITE_RE.search(normalized):
            hits.append(line.strip())
            continue
        m = _GIT_CONFIG_FILE_WRITE_RE.search(normalized)
        if m and _is_risky_config_file_path(m.group("path")):
            hits.append(line.strip())
    return hits


def _git_tracked(pattern: str) -> list[pathlib.Path]:
    """Files git tracks under REPO_ROOT matching *pattern* (a ``git ls-files``
    pathspec such as ``*.py``). Tracked, not walked: an rglob over the
    checkout also scans every ignored venv on the box (a second
    ``.venv-3.13`` here flagged huggingface_hub's own ``git config
    --global`` on 2026-09-14), which is neither this repo's code nor
    anything a fix could reach.
    """
    out = subprocess.run(
        ["git", "-C", str(REPO_ROOT), "ls-files", "-z", "--", pattern],
        check=True, capture_output=True, text=True,
    ).stdout
    return sorted(REPO_ROOT / rel for rel in out.split("\0") if rel)


def _tracked_scripts() -> list[pathlib.Path]:
    return [p for p in _git_tracked("*.sh") if ".git" not in p.parts]


#: This lint's own file, excluded from its own Python scan: its docstring
#: and kill-control fixtures legitimately contain the forbidden command as
#: DATA (documentation prose, synthetic strings), not as executable code
#: that would run it -- the same reason a shell script's comments were
#: already excluded by the leading-`#` check.
_SELF = pathlib.Path(__file__).resolve()


def _tracked_python_files() -> list[pathlib.Path]:
    files = _git_tracked("*.py")
    return [
        p
        for p in files
        if ".git" not in p.parts
        and ".venv" not in p.parts
        and "node_modules" not in p.parts
        and p.resolve() != _SELF
    ]


def _tracked_workflow_files() -> list[pathlib.Path]:
    workflows_dir = REPO_ROOT / ".github" / "workflows"
    if not workflows_dir.is_dir():
        return []
    return sorted(list(workflows_dir.glob("*.yml")) + list(workflows_dir.glob("*.yaml")))


def test_scan_is_non_vacuous() -> None:
    """A scan that silently walked zero files in a corpus would make the
    real assertion below pass on an empty set -- pin a floor per corpus so a
    broken glob (wrong extension, wrong root) fails loud instead of quietly
    checking nothing."""
    scripts = _tracked_scripts()
    assert len(scripts) >= 50, (
        f"only found {len(scripts)} tracked .sh files under {REPO_ROOT} -- "
        "the scan may be broken (wrong root or extension) rather than the "
        "repo genuinely shrinking to fewer than 50 shell scripts"
    )
    py_files = _tracked_python_files()
    assert len(py_files) >= 200, (
        f"only found {len(py_files)} tracked .py files under {REPO_ROOT} -- "
        "the scan may be broken rather than the repo genuinely shrinking"
    )
    workflows = _tracked_workflow_files()
    assert len(workflows) >= 5, (
        f"only found {len(workflows)} workflow files under "
        f"{REPO_ROOT / '.github' / 'workflows'} -- the scan may be broken "
        "(wrong directory) rather than the repo genuinely shrinking below "
        "five scheduled/CI workflows"
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


# ---------------------------------------------------------------------------
# Round-2 kill controls (nexus-oqh4s code-review findings 2-4): one red per
# new detection shape, each on a synthetic fixture that never touches a real
# repo file.
# ---------------------------------------------------------------------------


def test_detector_ignores_global_reads() -> None:
    """Finding 3: `git config --global --get/--list/--get-regexp` reads the
    value, it does not write it -- must not be flagged even though
    `--global` is present."""
    synthetic = (
        "git config --global --get user.email\n"
        "git config --global --list\n"
        "git config --global --get-regexp 'user\\.*'\n"
        "git config --system -l\n"
    )
    assert _violations(synthetic) == []


def test_detector_flags_global_write_with_a_trailing_value() -> None:
    """The read-exclusion must not swallow a genuine write that happens to
    share a line with no read flag."""
    synthetic = 'git config --global user.email "still-a-write@nexus.local"\n'
    assert _violations(synthetic) == ['git config --global user.email "still-a-write@nexus.local"']


def test_detector_flags_python_subprocess_list_argv() -> None:
    """Finding 2: a Python subprocess call building the identical argv as a
    list literal (never a contiguous `git config` substring) must be caught
    by the same lint that catches the shell-string shape."""
    synthetic = (
        'subprocess.run(["git", "config", "--global", "user.email", "incident@nexus.local"])\n'
    )
    hits = _violations(synthetic)
    assert len(hits) == 1, f"expected 1 flagged line, got {hits}"


def test_detector_ignores_python_subprocess_read_list_argv() -> None:
    """The read exclusion must hold for the Python list-argv shape too."""
    synthetic = 'subprocess.run(["git", "config", "--global", "--get", "user.email"])\n'
    assert _violations(synthetic) == []


def test_detector_flags_file_write_to_a_risky_path() -> None:
    """Finding 4: `--file`/`-f` pointed at a fixed host location (a bare
    `~`, `$HOME`, `/etc/`, or another real user's home) is the same
    host-wide-file hazard as `--global`, under a different flag."""
    synthetic = (
        "git config --file ~/.gitconfig user.email incident@nexus.local\n"
        'git config --file "$HOME/.gitconfig" user.name incident\n'
        "git config --system --file /etc/gitconfig user.name incident\n"
        "git config -f /Users/someone/.gitconfig user.name incident\n"
    )
    hits = _violations(synthetic)
    assert len(hits) == 4, f"expected 4 flagged lines, got {hits}"


def test_detector_ignores_file_write_to_a_temp_or_repo_path() -> None:
    """The exact throwaway-scope shape `tests/e2e/upgrade-shakeout.sh` uses
    (a `--file` pointed inside a temp directory it built for the purpose)
    must stay out of scope, same as the bare-local-config case."""
    synthetic = (
        'git config --file "$TMPDIR/throwaway/.gitconfig" user.email t@t.invalid\n'
        "git config --file /tmp/throwaway/.gitconfig user.name T\n"
        'git config --file "$(mktemp -d)/.gitconfig" user.name T\n'
        "git config --file ./throwaway/.gitconfig user.name T\n"
    )
    assert _violations(synthetic) == []


def test_detector_ignores_file_read_from_a_risky_path() -> None:
    """A `--file`/`-f` READ (a get/list flag present) is not a write,
    regardless of how risky the path looks."""
    synthetic = "git config --file ~/.gitconfig --get user.email\n"
    assert _violations(synthetic) == []


def test_no_tracked_source_writes_git_config_global_system_or_a_risky_file() -> None:
    bad: list[str] = []
    for corpus in (_tracked_scripts(), _tracked_python_files(), _tracked_workflow_files()):
        for path in corpus:
            text = path.read_text(encoding="utf-8", errors="replace")
            for line in _violations(text):
                bad.append(f"{path.relative_to(REPO_ROOT)}: {line}")
    assert not bad, (
        "tracked file(s) write a global/system/risky-path git config value "
        "-- this mutates a real host's ~/.gitconfig (or /etc/gitconfig) the "
        "instant the code runs outside its intended container (nexus-oqh4s, "
        "2026-09-12 incident): reattribute via exported GIT_AUTHOR_NAME / "
        "GIT_AUTHOR_EMAIL / GIT_COMMITTER_NAME / GIT_COMMITTER_EMAIL "
        "instead, which reaches every git invocation in the same process "
        f"tree without touching any config file:\n" + "\n".join(bad)
    )
