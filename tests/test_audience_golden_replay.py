# SPDX-License-Identifier: AGPL-3.0-or-later
"""Golden replay (nexus-sby49 Phase 1, T1): the maintainer path did not move.

Phase 1 puts an audience gate in front of the subagent git-write guard. For the
maintainer (the account flag, or a nexus checkout) the guard must answer exactly
as it did before. Two replays prove it, each running the guard from
``PHASE1_PARENT`` (the commit before Phase 1, extracted with ``git archive``) and
the working tree over the same inputs, and comparing the hook's stdout:

* **Synthetic** (``test_the_synthetic_replay_...``): every command in
  ``test_routing_subagent_git_write``'s deny, allow, exemption and ssh tables, in a
  marked primary checkout, a marked linked worktree, a non-repo cwd and with no
  cwd. Zero differences, and a floor so a replay that examined nothing fails.
* **Real corpus** (``test_the_real_corpus_replay_...``): every subagent Bash call
  recorded on the maintainer's machine over 30 days, as ``(command, cwd)`` from
  ``~/.local/state/nexus/plans/corpus/subagent-bash-30d.jsonl`` (Phase 0 extracts
  it; it stays on that machine, never in the repo). With the flag present,
  ``maintainer()`` is true for every cwd that still exists and the old and new
  verdicts are identical for every one of those commands. With the flag removed
  through the test seam, ``nexus_repo()`` is true for every cwd under a nexus
  checkout that carries the marker, and the checkouts that would fall to user mode
  are listed. The floor is 20 existing cwds and 500 commands; below it the test
  FAILS (unverified), and so does an absent corpus. Cwds that no longer exist
  cannot be judged: they are counted and listed, never dropped silently.

Both are ``slow`` (a hand run, not the default loop), because they replay against
an old commit and the second needs the corpus:

    uv run pytest -m slow tests/test_audience_golden_replay.py -s

Phase 2 retires ``PHASE1_PARENT`` (and with it the synthetic test) when it changes the maintainer path on
purpose. The nightly ``slow`` job (``local-service-gate-nightly.yml``) runs both on a runner with full history
and no corpus: the corpus test skips there unless ``NX_REQUIRE_REAL_CORPUS=1``, which the hand run that is
evidence sets (``NX_REQUIRE_REAL_CORPUS=1 uv run pytest -m slow tests/test_audience_golden_replay.py -s``).
"""
from __future__ import annotations

import collections
import io
import json
import os
import pathlib
import subprocess
import sys
import tarfile
from concurrent.futures import ThreadPoolExecutor

import pytest

from tests import test_routing_subagent_git_write as guard_tests
from tests.hooks.audience_harness import flagged_home, mark_nexus

pytestmark = pytest.mark.slow

PROJECT_ROOT = pathlib.Path(__file__).parent.parent
SCRIPTS_REL = "conexus/hooks/scripts"
WORKER = PROJECT_ROOT / "tests" / "hooks" / "golden_replay_worker.py"
#: The commit before the first Phase 1 commit: develop at the plan's base.
PHASE1_PARENT = "7ae5dcfe75"
def _real_home() -> pathlib.Path:
    """The account's home from the account database: the suite fences ``HOME``."""
    if os.name == "nt":
        return pathlib.Path(os.environ.get("USERPROFILE", ""))
    import pwd

    return pathlib.Path(pwd.getpwuid(os.getuid()).pw_dir)


CORPUS = _real_home() / ".local" / "state" / "nexus" / "plans" / "corpus" / "subagent-bash-30d.jsonl"
FLOOR_CWDS = 20
FLOOR_COMMANDS = 500
#: The hand run that is shipping evidence sets this to 1: a missing corpus then FAILS instead of skipping.
#: The nightly `-m slow` job runs on a runner with no corpus, where the skip is right.
REQUIRE_CORPUS_ENV = "NX_REQUIRE_REAL_CORPUS"
#: A replay in which the old guard denied nothing proves nothing about its denies.
FLOOR_OLD_DENIES = 1000
SYNTHETIC_FLOOR = 1500
REMOTE_HOSTS = "chas,chas-test"


def _git(*args: str, cwd: pathlib.Path = PROJECT_ROOT) -> bytes:
    env = {**os.environ, "GIT_CONFIG_GLOBAL": os.devnull, "GIT_CONFIG_SYSTEM": os.devnull}
    done = subprocess.run(
        ["git", "-c", "user.name=t", "-c", "user.email=t@t", *args], cwd=cwd, env=env,
        capture_output=True, check=True,
    )
    return done.stdout


@pytest.fixture(scope="module")
def old_tree(tmp_path_factory) -> pathlib.Path:
    """``conexus/hooks/scripts`` as it was at PHASE1_PARENT. Fails loud if the commit is unreachable."""
    try:
        blob = _git("archive", "--format=tar", PHASE1_PARENT, SCRIPTS_REL)
    except subprocess.CalledProcessError as exc:
        pytest.fail(f"{PHASE1_PARENT} is not reachable in this clone, so the replay cannot run: {exc.stderr.decode()[:300]}")
    root = tmp_path_factory.mktemp("old-guard")
    with tarfile.open(fileobj=io.BytesIO(blob)) as tar:
        tar.extractall(root, filter="data")
    tree = root / SCRIPTS_REL
    assert (tree / "routing" / "subagent_git_write_requires_orchestrator.py").is_file()
    assert not (tree / "_audience.py").exists(), "the old tree already has the audience helper"
    return tree


def _worker(tree: pathlib.Path, mode: str, home: pathlib.Path | None, cases: list[dict], env_extra: dict | None = None) -> list[dict]:
    argv = [sys.executable, str(WORKER), str(tree), mode] + ([str(home)] if home else [])
    env = {**os.environ, **(env_extra or {})}
    done = subprocess.run(
        argv, input="".join(json.dumps(c) + "\n" for c in cases), capture_output=True, text=True,
        timeout=3600, env=env,
    )
    assert done.returncode == 0, done.stderr[-2000:]
    rows = [json.loads(line) for line in done.stdout.splitlines()]
    assert len(rows) == len(cases), f"{len(rows)} verdicts for {len(cases)} cases: {done.stderr[-500:]}"
    return rows


def _both(old_tree, home, cases, *, mode_home_has_hosts: bool = True):
    """Old and new verdicts for *cases*, the two workers running at once."""
    new_tree = PROJECT_ROOT / SCRIPTS_REL
    with ThreadPoolExecutor(max_workers=2) as pool:
        old = pool.submit(_worker, old_tree, "old", None, cases, {"NX_REMOTE_TEST_HOSTS": REMOTE_HOSTS})
        new = pool.submit(_worker, new_tree, "new", home, cases)
        return old.result(), new.result()


# ---------------------------------------------------------------------------
# Synthetic
# ---------------------------------------------------------------------------


def _synthetic_commands() -> list[str]:
    T = guard_tests
    commands: list[str] = []

    def take(items) -> None:
        for item in items:
            if isinstance(item, str) and "{" not in item.replace("${", ""):
                commands.append(item)
            elif isinstance(item, (list, tuple)) and item and isinstance(item[0], str):
                take([item[0]])

    for table in (T._DENY_FAMILIES.values(), T._ALLOW_FAMILIES.values()):
        for family in table:
            take(family)
    for name in (
        "_EXEMPTION_PROBES", "_CHAS_ALLOWED", "_CHAS_DENIED", "_SHARED_CONFIG_WRITES",
        "_SIDEFX_ALLOWED", "_SIDEFX_SHARED_ONLY", "_SIDEFX_READS", "_SIDEFX_WORKTREE_CONFIG",
    ):
        take(getattr(T, name, []))
    commands.append(T._REMOTE_CMD.format(host="chas"))
    commands.append(T._REMOTE_CMD.format(host="devbox"))
    return sorted(set(commands))


def test_the_synthetic_replay_shows_zero_maintainer_path_differences(old_tree, tmp_path):
    commands = _synthetic_commands()
    primary = tmp_path / "primary"
    primary.mkdir()
    _git("init", "-q", cwd=primary)
    mark_nexus(primary)
    (primary / "f.txt").write_text("x")
    _git("add", "f.txt", "scripts/agent-worktree-preflight.sh", cwd=primary)
    _git("commit", "-q", "-m", "init", cwd=primary)
    linked = tmp_path / "linked"
    _git("worktree", "add", "-q", "-b", "wt", str(linked), cwd=primary)
    plain = tmp_path / "plain"
    plain.mkdir()
    unflagged = tmp_path / "unflagged-home"
    flagged = pathlib.Path(flagged_home(tmp_path / "flagged-home"))
    for h in (unflagged, flagged):
        hosts = h / ".config" / "nexus" / "remote-test-hosts"
        hosts.parent.mkdir(parents=True, exist_ok=True)
        hosts.write_text(REMOTE_HOSTS + "\n")

    #: where the marker alone (unflagged home) decides, and where the flag does.
    places = [
        ("primary", str(primary), unflagged),
        ("linked worktree", str(linked), unflagged),
        ("non-repo (flagged)", str(plain), flagged),
        ("no cwd (flagged)", "", flagged),
    ]
    examined = denies = passes = 0
    differences: list[str] = []
    for label, cwd, home in places:
        cases = [{"command": c, "cwd": cwd} for c in commands]
        old, new = _both(old_tree, home, cases)
        for c, o, n in zip(commands, old, new):
            examined += 1
            denies += '"deny"' in o["verdict"]
            passes += o["verdict"] == ""
            if o["verdict"] != n["verdict"]:
                differences.append(f"[{label}] {c[:120]!r}")
            assert n["maintainer"] is True, f"[{label}] new guard saw a non-maintainer for {c[:80]!r}"
    print(f"\nsynthetic replay: {len(commands)} commands x {len(places)} places = {examined} verdicts, "
          f"{denies} deny, {passes} pass, {len(differences)} differences")
    assert examined >= SYNTHETIC_FLOOR, f"only {examined} verdicts replayed (floor {SYNTHETIC_FLOOR})"
    assert denies >= 500 and passes >= 100, f"a vacuous replay: {denies} denies, {passes} passes"
    assert differences == [], differences[:20]


# ---------------------------------------------------------------------------
# Real corpus
# ---------------------------------------------------------------------------


def _toplevel(cwd: str) -> str | None:
    here = os.path.realpath(cwd)
    while True:
        if os.path.lexists(os.path.join(here, ".git")):
            return here
        parent = os.path.dirname(here)
        if parent == here:
            return None
        here = parent


def test_the_real_corpus_replay_meets_the_floor_and_the_bars(old_tree, tmp_path):
    if not CORPUS.is_file():
        reason = f"the real corpus is absent ({CORPUS}): Phase 0 extracts it on the maintainer's machine. UNVERIFIED."
        if os.environ.get(REQUIRE_CORPUS_ENV) == "1":
            pytest.fail(reason)
        pytest.skip(reason + f" Set {REQUIRE_CORPUS_ENV}=1 on the hand run that is evidence, so an absent corpus fails there.")
    pairs: dict[tuple[str, str], None] = {}
    sessions: dict[str, int] = collections.Counter()
    for line in CORPUS.open(encoding="utf-8"):
        rec = json.loads(line)
        if rec.get("command") and rec.get("cwd"):
            pairs[(rec["command"], rec["cwd"])] = None
            sessions[rec["cwd"]] += 1
    cwds = sorted(sessions)
    existing = [c for c in cwds if os.path.isdir(c)]
    gone = [c for c in cwds if not os.path.isdir(c)]
    cases = [{"command": c, "cwd": w} for (c, w) in pairs if w in set(existing)]
    print(f"\nreal corpus: {len(pairs)} distinct (command, cwd) pairs, {len(cwds)} distinct cwds: "
          f"{len(existing)} still exist, {len(gone)} gone; replaying {len(cases)} pairs on existing cwds")
    assert len(existing) >= FLOOR_CWDS and len(cases) >= FLOOR_COMMANDS, (
        f"UNVERIFIED: {len(existing)} existing cwds (floor {FLOOR_CWDS}), {len(cases)} commands (floor {FLOOR_COMMANDS})"
    )

    flagged = pathlib.Path(flagged_home(tmp_path / "flagged"))
    empty = tmp_path / "empty-home"
    empty.mkdir()
    # The old guard read its exempt ssh hosts from the environment, the new one from a
    # file under the account home (D11): give both the same hosts, so a difference is a
    # difference in the guard and not in how the hosts were named.
    for h in (flagged, empty):
        hosts = h / ".config" / "nexus" / "remote-test-hosts"
        hosts.parent.mkdir(parents=True, exist_ok=True)
        hosts.write_text(REMOTE_HOSTS + "\n")
    new_tree = PROJECT_ROOT / SCRIPTS_REL
    with ThreadPoolExecutor(max_workers=3) as pool:
        old = pool.submit(_worker, old_tree, "old", None, cases, {"NX_REMOTE_TEST_HOSTS": REMOTE_HOSTS})
        new = pool.submit(_worker, new_tree, "new", flagged, cases)
        noflag = pool.submit(_worker, new_tree, "new", empty, cases)
        old_rows, new_rows, noflag_rows = old.result(), new.result(), noflag.result()

    # Bar 1: with the flag, maintainer() is true everywhere, and the verdicts are identical.
    not_maintainer = sorted({c["cwd"] for c, r in zip(cases, new_rows) if r["maintainer"] is not True})
    verdict_diffs = [c for c, o, n in zip(cases, old_rows, new_rows) if o["verdict"] != n["verdict"]]
    old_denies = sum('"deny"' in r["verdict"] for r in old_rows)
    denied_cwds = len({c["cwd"] for c, r in zip(cases, old_rows) if '"deny"' in r["verdict"]})
    print(f"flag present: maintainer() true for {len(existing) - len(not_maintainer)}/{len(existing)} cwds; "
          f"verdicts identical for {len(cases) - len(verdict_diffs)}/{len(cases)} commands; "
          f"the old guard denied {old_denies} of them, in {denied_cwds} cwds")
    assert old_denies >= FLOOR_OLD_DENIES, (
        f"a vacuous corpus replay: the old guard denied {old_denies} commands (floor {FLOOR_OLD_DENIES})"
    )

    # Bar 2: the flag removed through the seam; the marker alone must carry every nexus checkout that has one.
    by_cwd = {}
    for c, r in zip(cases, noflag_rows):
        by_cwd[c["cwd"]] = r
    nexus_like: list[str] = []
    user_mode: list[str] = []
    for cwd, r in sorted(by_cwd.items()):
        top = _toplevel(cwd)
        is_nexus_checkout = bool(top and os.path.isfile(os.path.join(top, "src", "nexus", "__init__.py")))
        if is_nexus_checkout:
            nexus_like.append(cwd)
            if r["nexus_repo"] is not True:
                user_mode.append(cwd)
    marker_missing = [
        c for c in user_mode
        if not any(os.path.isfile(os.path.join(_toplevel(c) or "", *m.split("/"))) for m in ("scripts/agent-worktree-preflight.sh", ".nexus/dev-process.json"))
    ]
    other_user_mode = sorted(c for c, r in by_cwd.items() if r["maintainer"] is not True and c not in nexus_like)
    print(f"flag removed: nexus_repo() true for {len(nexus_like) - len(user_mode)}/{len(nexus_like)} cwds under a nexus checkout")
    print(f"  nexus checkouts that fall to user mode ({len(user_mode)}; {len(marker_missing)} predate the marker): {user_mode}")
    print(f"  non-nexus cwds that fall to user mode without the flag ({len(other_user_mode)}): {other_user_mode}")
    print(f"  cwds no longer on disk ({len(gone)}): {gone[:10]}{' ...' if len(gone) > 10 else ''}")

    assert not_maintainer == [], not_maintainer
    assert verdict_diffs == [], [(d["cwd"], d["command"][:100]) for d in verdict_diffs[:10]]
    assert nexus_like, "no nexus checkout among the existing cwds: the marker bar examined nothing"
    assert user_mode == marker_missing, "a nexus checkout WITH the marker fell to user mode"


def test_an_absent_corpus_skips_unless_it_is_required(tmp_path, monkeypatch):
    """The nightly slow job has no corpus and must not go red; the evidence run must not skip silently."""
    monkeypatch.setattr(sys.modules[__name__], "CORPUS", tmp_path / "missing.jsonl")
    monkeypatch.delenv(REQUIRE_CORPUS_ENV, raising=False)
    with pytest.raises(pytest.skip.Exception):
        test_the_real_corpus_replay_meets_the_floor_and_the_bars(None, tmp_path)
    monkeypatch.setenv(REQUIRE_CORPUS_ENV, "1")
    with pytest.raises(pytest.fail.Exception):
        test_the_real_corpus_replay_meets_the_floor_and_the_bars(None, tmp_path)
