# SPDX-License-Identifier: AGPL-3.0-or-later
"""Golden replay (nexus-sby49 Phase 1, T1; re-expressed for the three postures in nexus-sby49.9): the strict path did not move.

The subagent git-write guard has a mode, the ``subagent_git`` setting: ``strict`` runs the full guard, anything else the short
seatbelt (``_user_git_seatbelt``). RDR-259 Decision 11 names three postures, and this replay runs each:

* **project-strict**: a repository whose tracked ``.nexus/project.json`` says ``subagent_git: strict`` (the nexus checkout, and
  every linked worktree of it), on an account with no settings file;
* **user-strict**: an account whose ``conexus.json`` says it, in any repository, in no repository and with no cwd;
* **plain**: a user's repository, no settings file anywhere. The seatbelt answers.

The two strict postures must answer exactly as the guard did before Phase 1 (``PHASE1_PARENT``, extracted with ``git archive``):
the hook's stdout is compared decision by decision. Two replays prove it:

* **Synthetic** (``test_the_synthetic_replay_...``): every command in ``test_routing_subagent_git_write``'s deny, allow,
  exemption and ssh tables, plus the admission tables of ``test_routing_subagent_git_admit``, in a project-strict primary
  checkout, a project-strict linked worktree, and the three user-strict places (a plain repository, a non-repository, no cwd).
  No tightening, every loosening named in a table and carrying the ``allow_admit_`` outcome of nexus-5y47c, and a floor so a
  replay that examined nothing fails. The plain posture is replayed too: the seatbelt loosens nearly everything, and the only
  command it denies that the old guard passed is an irrecoverable form (``branch -D``, ``stash drop``...) in a linked worktree,
  denied with the seatbelt's own stop text.
* **Real corpus** (``test_the_real_corpus_replay_...``): every subagent Bash call recorded on the maintainer's machine over 30
  days, as ``(command, cwd)`` from ``~/.local/state/nexus/plans/corpus/subagent-bash-30d.jsonl`` (Phase 0 extracts it; it stays
  on that machine, never in the repo). Replayed under user-strict (every cwd reads strict, and the old and new decisions are
  identical for every command), then under the plain account: every cwd that reads strict there carries its own project
  settings and replays identically, the checkouts that fall to the seatbelt are listed (each must lack the settings file), and the seatbelt tightens only with its own stop text. The floor is 20 existing cwds and 500 commands; below it the test
  FAILS (unverified), and so does an absent corpus. Cwds that no longer exist cannot be judged: they are counted and listed,
  never dropped silently.

Both are ``slow`` (a hand run, not the default loop), because they replay against an old commit and the second needs the
corpus:

    uv run pytest -m slow tests/test_audience_golden_replay.py -s

Phase 2 retires ``PHASE1_PARENT`` (and with it the synthetic test) when it changes the strict path on purpose. The nightly
``slow`` job (``local-service-gate-nightly.yml``) runs both on a runner with full history and no corpus: the corpus test skips
there unless ``NX_REQUIRE_REAL_CORPUS=1``, which the hand run that is evidence sets
(``NX_REQUIRE_REAL_CORPUS=1 uv run pytest -m slow tests/test_audience_golden_replay.py -s``).
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

from tests import test_routing_subagent_git_admit as admit_tests
from tests import test_routing_subagent_git_write as guard_tests
from tests.hooks.audience_harness import mark_strict, plain_home, strict_home

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
#: The file that makes a checkout read strict (the retired Phase 1 and Phase 2 markers no longer do).
STRICT_FILES = (".nexus/project.json",)


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


def _both(old_tree, home, cases):
    """Old and new verdicts for *cases*, the two workers running at once."""
    new_tree = PROJECT_ROOT / SCRIPTS_REL
    with ThreadPoolExecutor(max_workers=2) as pool:
        old = pool.submit(_worker, old_tree, "old", None, cases, {"NX_REMOTE_TEST_HOSTS": REMOTE_HOSTS})
        new = pool.submit(_worker, new_tree, "new", home, cases)
        return old.result(), new.result()


def _with_hosts(home: "os.PathLike[str] | str") -> pathlib.Path:
    """*home* with the exempt ssh hosts the old guard read from the environment: the new one reads a file under the account
    home (D11), so both get the same hosts and a difference is a difference in the guard and not in how the hosts were named."""
    path = pathlib.Path(home)
    hosts = path / ".config" / "nexus" / "remote-test-hosts"
    hosts.parent.mkdir(parents=True, exist_ok=True)
    hosts.write_text(REMOTE_HOSTS + "\n")
    return path


def _repo(path: pathlib.Path, *, strict: bool) -> pathlib.Path:
    """A real repository with one commit; project-strict when *strict* (``.nexus/project.json`` committed), else plain."""
    path.mkdir()
    _git("init", "-q", cwd=path)
    (path / "f.txt").write_text("x")
    paths = ["f.txt"]
    if strict:
        mark_strict(path)
        paths.append(".nexus/project.json")
    _git("add", *paths, cwd=path)
    _git("commit", "-q", "-m", "init", cwd=path)
    return path


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
    commands.extend(_admission_commands())
    return sorted(set(commands))


def _admission_commands() -> list[str]:
    """The read-shaped tables of the admission tests: no placeholder but the scratch directory."""
    A = admit_tests
    out = [c.replace("{S}", "/tmp/scratch-replay") for c in A._READ_ADMITTED]
    out += list(A.CATALOGUE_NOW_ADMITTED)
    out += list(A._READ_PLANTS)
    return out


def _decision(row: dict) -> str:
    return "deny" if '"deny"' in row["verdict"] else ("pass" if row["verdict"] == "" else "other")


def _changes(cases: list[dict], old_rows: list[dict], new_rows: list[dict]) -> tuple[list[dict], list[dict]]:
    """``(loosenings, tightenings)``: the commands whose DECISION moved. The deny text changed on purpose
    (nexus-5y47c fix 6, nexus-sby49.9), so a command denied by both is the same verdict whatever its text says."""
    loosened: list[dict] = []
    tightened: list[dict] = []
    for case, o, n in zip(cases, old_rows, new_rows):
        before, after = _decision(o), _decision(n)
        if before == after:
            continue
        rec = {"command": case["command"], "cwd": case["cwd"], "outcome": n.get("outcome", ""), "was": before, "now": after}
        (loosened if before == "deny" and after == "pass" else tightened).append(rec)
    return loosened, tightened


#: The seatbelt's deny text opens with this (``_user_git_seatbelt.deny_text``): a plain-posture command the old full guard passed
#: and the working tree denies is explained only when the new verdict carries it.
SEATBELT_STOP = "was stopped: it can destroy history or uncommitted work"


def _unnamed_tightenings(cases: list[dict], new_rows: list[dict], tightened: list[dict]) -> list[dict]:
    """The *tightened* records whose new verdict is not the seatbelt's own stop text."""
    verdict = {(c["command"], c["cwd"]): r["verdict"] for c, r in zip(cases, new_rows)}
    return [rec for rec in tightened if SEATBELT_STOP not in verdict[(rec["command"], rec["cwd"])]]


#: The one deliberate change to the strict path since Phase 1 (nexus-sby49.3): plan section 2.1 says the full guard denies
#: ``git reflog delete``, and the verb set only knew ``reflog expire``. Everything else must replay with zero differences.
INTENDED_TIGHTENINGS = ["git reflog delete HEAD@{1}", "git reflog delete --rewrite --updateref HEAD@{2}"]


def test_the_replay_shows_exactly_the_intended_tightening(old_tree, tmp_path):
    """Phase 1's guard (``PHASE1_PARENT``) passes ``git reflog delete``; the working tree denies it in both strict postures.
    The zero-difference test below holds the rest of the table to identical verdicts."""
    primary = _repo(tmp_path / "primary", strict=True)
    plain = _with_hosts(plain_home(tmp_path / "plain-home"))
    user_strict = pathlib.Path(strict_home(tmp_path / "user-strict-home"))
    for label, cwd, home in (("project-strict", str(primary), plain), ("user-strict, no cwd", "", user_strict)):
        cases = [{"command": c, "cwd": cwd} for c in INTENDED_TIGHTENINGS]
        old, new = _both(old_tree, home, cases)
        for c, o, n in zip(INTENDED_TIGHTENINGS, old, new):
            assert o["verdict"] == "", f"[{label}] Phase 1 already denied {c!r}: the tightening is not new"
            assert '"deny"' in n["verdict"], f"[{label}] the working tree still passes {c!r}"


def test_the_synthetic_replay_shows_only_the_named_loosenings(old_tree, tmp_path):
    commands = _synthetic_commands()
    primary = _repo(tmp_path / "primary", strict=True)
    linked = tmp_path / "linked"
    _git("worktree", "add", "-q", "-b", "wt", str(linked), cwd=primary)
    user_repo = _repo(tmp_path / "user-repo", strict=False)
    non_repo = tmp_path / "non-repo"
    non_repo.mkdir()
    plain = _with_hosts(plain_home(tmp_path / "plain-home"))
    user_strict = _with_hosts(strict_home(tmp_path / "user-strict-home"))

    #: where the project's own settings file decides, and where the user's does.
    places = [
        ("project-strict primary", str(primary), plain),
        ("project-strict linked worktree", str(linked), plain),
        ("user-strict, plain repo", str(user_repo), user_strict),
        ("user-strict, non-repo", str(non_repo), user_strict),
        ("user-strict, no cwd", "", user_strict),
    ]
    examined = denies = passes = 0
    loosened_all: list[str] = []
    tightened_all: list[str] = []
    unnamed: list[str] = []
    by_outcome: collections.Counter[str] = collections.Counter()
    named = set(admit_tests.CATALOGUE_NOW_ADMITTED) | {c.replace("{S}", "/tmp/scratch-replay") for c in admit_tests._READ_ADMITTED}
    for label, cwd, home in places:
        cases = [{"command": c, "cwd": cwd} for c in commands]
        old, new = _both(old_tree, home, cases)
        for o, n in zip(old, new):
            examined += 1
            denies += '"deny"' in o["verdict"]
            passes += o["verdict"] == ""
            assert n["strict"] is True, f"[{label}] the new guard did not read strict"
        loosened, tightened = _changes(cases, old, new)
        for rec in loosened:
            by_outcome[rec["outcome"]] += 1
            loosened_all.append(f"[{label}] {rec['command'][:100]!r}")
            if rec["command"] not in named or not rec["outcome"].startswith("allow_admit_"):
                unnamed.append(f"[{label}] {rec['command'][:100]!r} ({rec['outcome']})")
        tightened_all += [f"[{label}] {rec['command'][:100]!r}" for rec in tightened]
    print(f"\nsynthetic replay (strict postures): {len(commands)} commands x {len(places)} places = {examined} verdicts, "
          f"{denies} deny, {passes} pass; {len(loosened_all)} loosened {dict(by_outcome)}, {len(tightened_all)} tightened")
    assert examined >= SYNTHETIC_FLOOR, f"only {examined} verdicts replayed (floor {SYNTHETIC_FLOOR})"
    assert denies >= 500 and passes >= 100, f"a vacuous replay: {denies} denies, {passes} passes"
    assert tightened_all == [], tightened_all[:20]
    assert unnamed == [], unnamed[:20]
    # The named catalogue loosenings are all exercised, in every place: a loosening that is not seen is not proven.
    seen = {line for line in loosened_all}
    missing = [c for c in admit_tests.CATALOGUE_NOW_ADMITTED if f"[project-strict primary] {c[:100]!r}" not in seen]
    assert missing == [], missing


def test_the_plain_posture_runs_the_seatbelt_and_tightens_only_what_it_names(old_tree, tmp_path):
    """In a plain repository the seatbelt answers: it reads not-strict, it denies a short list (so something still denies),
    and a command the old guard passed is denied only by the seatbelt's own stop text (the irrecoverable forms the old guard
    let a linked worktree run: ``branch -D``, ``stash drop``, ``update-ref -d``, ``worktree remove -f``...)."""
    commands = _synthetic_commands()
    user_repo = _repo(tmp_path / "user-repo", strict=False)
    linked = tmp_path / "linked"
    _git("worktree", "add", "-q", "-b", "wt", str(linked), cwd=user_repo)
    non_repo = tmp_path / "non-repo"
    non_repo.mkdir()
    plain = _with_hosts(plain_home(tmp_path / "plain-home"))
    examined = new_denies = named_by_seatbelt = 0
    tightened_all: list[str] = []
    for label, cwd in (("plain primary", str(user_repo)), ("plain linked worktree", str(linked)),
                       ("plain non-repo", str(non_repo)), ("plain, no cwd", "")):
        cases = [{"command": c, "cwd": cwd} for c in commands]
        old, new = _both(old_tree, plain, cases)
        for n in new:
            examined += 1
            new_denies += '"deny"' in n["verdict"]
            assert n["strict"] is False, f"[{label}] a plain repository read strict"
        _loosened, tightened = _changes(cases, old, new)
        named_by_seatbelt += len(tightened) - len(_unnamed_tightenings(cases, new, tightened))
        tightened_all += [f"[{label}] {rec['command'][:100]!r}" for rec in _unnamed_tightenings(cases, new, tightened)]
    print(f"\nsynthetic replay (plain): {examined} verdicts, the seatbelt denied {new_denies}; "
          f"{named_by_seatbelt} tightened by the seatbelt's stop text, {len(tightened_all)} tightened otherwise")
    assert examined >= SYNTHETIC_FLOOR
    assert new_denies >= 1, "a vacuous plain replay: the seatbelt denied nothing"
    assert tightened_all == [], tightened_all[:20]


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

    user_strict = _with_hosts(strict_home(tmp_path / "user-strict"))
    plain = _with_hosts(plain_home(tmp_path / "plain"))
    new_tree = PROJECT_ROOT / SCRIPTS_REL
    with ThreadPoolExecutor(max_workers=3) as pool:
        old = pool.submit(_worker, old_tree, "old", None, cases, {"NX_REMOTE_TEST_HOSTS": REMOTE_HOSTS})
        new = pool.submit(_worker, new_tree, "new", user_strict, cases)
        no_user = pool.submit(_worker, new_tree, "new", plain, cases)
        old_rows, strict_rows, plain_rows = old.result(), new.result(), no_user.result()

    old_denies = sum('"deny"' in r["verdict"] for r in old_rows)
    denied_cwds = len({c["cwd"] for c, r in zip(cases, old_rows) if '"deny"' in r["verdict"]})
    assert old_denies >= FLOOR_OLD_DENIES, (
        f"a vacuous corpus replay: the old guard denied {old_denies} commands (floor {FLOOR_OLD_DENIES})"
    )

    # Bar 1, user-strict: the guard reads strict for every cwd. Every command whose decision moved is a loosening that the
    # admission module made (outcome allow_admit_*), counted per class; none tightened.
    not_strict = sorted({c["cwd"] for c, r in zip(cases, strict_rows) if r["strict"] is not True})
    loosened, tightened = _changes(cases, old_rows, strict_rows)
    verdict_diffs = tightened + [rec for rec in loosened if not rec["outcome"].startswith("allow_admit_")]
    by_class = collections.Counter(rec["outcome"] for rec in loosened)
    print(f"user-strict: strict for {len(existing) - len(not_strict)}/{len(existing)} cwds; "
          f"{len(loosened)} of {len(cases)} commands loosened {dict(by_class)}, {len(tightened)} tightened; "
          f"the old guard denied {old_denies} of them, in {denied_cwds} cwds")

    # Bar 2, project-strict: on an account with no settings file, every cwd under a nexus checkout that carries the settings
    # file reads strict; the commands replayed there are held to the same zero-difference bar.
    by_cwd = {c["cwd"]: r for c, r in zip(cases, plain_rows)}
    nexus_like: list[str] = []
    fell_to_seatbelt: list[str] = []
    for cwd, r in sorted(by_cwd.items()):
        top = _toplevel(cwd)
        if top and os.path.isfile(os.path.join(top, "src", "nexus", "__init__.py")):
            nexus_like.append(cwd)
            if r["strict"] is not True:
                fell_to_seatbelt.append(cwd)
    no_settings_file = [
        c for c in fell_to_seatbelt
        if not any(os.path.isfile(os.path.join(_toplevel(c) or "", *m.split("/"))) for m in STRICT_FILES)
    ]
    project_cases = [(c, o, n) for c, o, n in zip(cases, old_rows, plain_rows) if n["strict"] is True]
    p_loosened, p_tightened = _changes([c for c, _, _ in project_cases], [o for _, o, _ in project_cases], [n for _, _, n in project_cases])
    project_diffs = p_tightened + [rec for rec in p_loosened if not rec["outcome"].startswith("allow_admit_")]
    print(f"project-strict: strict for {len(nexus_like) - len(fell_to_seatbelt)}/{len(nexus_like)} cwds under a nexus checkout; "
          f"{len(project_cases)} commands replayed, {len(p_loosened)} loosened, {len(p_tightened)} tightened")
    print(f"  nexus checkouts that fall to the seatbelt ({len(fell_to_seatbelt)}; {len(no_settings_file)} lack the settings file): {fell_to_seatbelt}")

    # Bar 3, plain: the cwds that read not-strict run the seatbelt. A command the old guard passed is denied only by the
    # seatbelt's own stop text (the irrecoverable forms a linked worktree used to run); nothing else tightens.
    plain_cases = [(c, o, n) for c, o, n in zip(cases, old_rows, plain_rows) if n["strict"] is not True]
    s_cases, s_new = [c for c, _, _ in plain_cases], [n for _, _, n in plain_cases]
    s_loosened, s_tightened = _changes(s_cases, [o for _, o, _ in plain_cases], s_new)
    s_unnamed = _unnamed_tightenings(s_cases, s_new, s_tightened)
    other_seatbelt = sorted({c["cwd"] for c, _, _ in plain_cases} - set(nexus_like))
    print(f"plain: {len(plain_cases)} commands in {len({c['cwd'] for c, _, _ in plain_cases})} cwds read not-strict "
          f"({len(other_seatbelt)} outside a nexus checkout); {len(s_loosened)} loosened, {len(s_tightened)} tightened "
          f"({len(s_tightened) - len(s_unnamed)} by the seatbelt's stop text, {len(s_unnamed)} otherwise)")
    print(f"  cwds no longer on disk ({len(gone)}): {gone[:10]}{' ...' if len(gone) > 10 else ''}")

    assert not_strict == [], not_strict
    assert verdict_diffs == [], [(d["cwd"], d["command"][:100]) for d in verdict_diffs[:10]]
    assert nexus_like, "no nexus checkout among the existing cwds: the project-strict bar examined nothing"
    assert len(project_cases) >= FLOOR_COMMANDS // 5, f"project-strict replayed only {len(project_cases)} commands"
    assert fell_to_seatbelt == no_settings_file, "a nexus checkout WITH the settings file fell to the seatbelt"
    assert project_diffs == [], [(d["cwd"], d["command"][:100]) for d in project_diffs[:10]]
    assert plain_cases, "no cwd read not-strict: the plain bar examined nothing"
    assert s_unnamed == [], [(d["cwd"], d["command"][:100]) for d in s_unnamed[:10]]


def test_an_absent_corpus_skips_unless_it_is_required(tmp_path, monkeypatch):
    """The nightly slow job has no corpus and must not go red; the evidence run must not skip silently."""
    monkeypatch.setattr(sys.modules[__name__], "CORPUS", tmp_path / "missing.jsonl")
    monkeypatch.delenv(REQUIRE_CORPUS_ENV, raising=False)
    with pytest.raises(pytest.skip.Exception):
        test_the_real_corpus_replay_meets_the_floor_and_the_bars(None, tmp_path)
    monkeypatch.setenv(REQUIRE_CORPUS_ENV, "1")
    with pytest.raises(pytest.fail.Exception):
        test_the_real_corpus_replay_meets_the_floor_and_the_bars(None, tmp_path)
