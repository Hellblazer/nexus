# SPDX-License-Identifier: AGPL-3.0-or-later
"""The settings helper exists twice, and a third copy is installed on every user's box (nexus-sby49.3; reworked in
nexus-sby49.4 and nexus-sby49.8).

``conexus/hooks/scripts/_audience.py`` runs in the plugin's command hooks (stdlib only, no ``nexus`` import).
``src/nexus/hooks/_audience.py`` runs inside ``nx-hook`` and the MCP server. They must be byte-identical, and the
older plugin that a newer checkout is paired with (the helper at the newest release tag that carries one) must give the
same answer on every row it can know (it knows the two retired markers only, which this release no longer reads: a row
where they differ on purpose says ``prev_release: false``). ``tests/fixtures/audience_corpus.json``
holds layouts x user settings -> the answer of each reader (RDR-259 Decisions 1-4, 7 and 11); the two live copies are
driven over it, so a change to one copy's walk, files or defaults fails here until the other agrees. Every row of the
Decision 4 failure table, for the project file and the user file, is covered by at least one row (``covers``).

Lives under tests/hooks/ so the Windows set runs the byte identity and the two live copies (LF, and the walk through a
worktree ``.git`` file on NTFS are claims a Windows checkout can break). The previous-release comparison needs the release
tags, and develop's ``test`` shards and the Windows job check out at depth 1 with none, so that one test is marked ``lint``:
the ``test-lint`` job checks out at depth 0 (``.github/workflows/ci.yml``, ``fetch-depth: 0``) and is the only CI job that
runs it. Elsewhere it skips when no tag is readable, unless ``NX_REQUIRE_PREV_RELEASE_COPY=1`` (or ``CI`` is set, which in
the lint job means the checkout lost its tags) makes that a failure.
"""
from __future__ import annotations

import base64
import importlib.util
import json
import os
import shutil
import subprocess
import sys
from pathlib import Path
from types import ModuleType

import pytest

REPO_ROOT = Path(__file__).resolve().parents[2]
PLUGIN_COPY = REPO_ROOT / "conexus" / "hooks" / "scripts" / "_audience.py"
SRC_COPY = REPO_ROOT / "src" / "nexus" / "hooks" / "_audience.py"
CORPUS = REPO_ROOT / "tests" / "fixtures" / "audience_corpus.json"
MARKETPLACE = REPO_ROOT / ".claude-plugin" / "marketplace.json"

#: A corpus below this size is a vacuous corpus; a copy that examines fewer rows than this is a vacuous pass. A row a
#: platform cannot run (posix_only on Windows, git absent) is reported as skipped, never counted.
MIN_ROWS = 90
#: The previous release's helper knows only the retired markers and no project file, so it examines only the rows where it
#: and this release agree: no project file that says anything, no HEAD, no user file, no account-home change and no
#: CLAUDE_PROJECT_DIR.
MIN_PREVIOUS_ROWS = 12

#: The fields the readers are compared on, in the order the corpus states them.
COMPARED = (
    "guard_mode", "project", "user", "telemetry", "t1_nudge", "advice_blocked", "brief_questions",
    "integration_branch", "problems",
)
_LAYOUT_KINDS = {"dir", "file", "link", "symlink", "fifo", "rm", "git"}

#: Every Decision 4 failure row, for the project file and the user file, plus the cases Decisions 1, 3 and 7 add.
_DECISION_FOUR = (
    "absent", "symlink", "not-regular", "oversize", "exact-limit", "unreadable-notutf8", "notjson", "notobject", "bom",
    "subagent-git-bad", "list-notlist", "list-item-wrong", "advice-unknown-id", "text-over-limit", "text-multiline",
    "text-control", "text-multibyte", "text-empty", "unknown-key", "dollar-key", "wrong-file-key", "version-newer",
    "version-bad", "version-bool", "brief-cap", "brief-dup",
)
_USER_ONLY = ("telemetry-notbool", "t1-nudge-notbool")
_OTHER = (
    "no-home", "retired-marker-1", "retired-marker-2", "retired-marker-beside-project", "head-union", "head-absent",
    "head-unborn", "head-invalid-json", "head-oversize", "head-lowers-strict-cannot-lower-wt", "cwd-checkout-adds",
    "cwd-checkout-cannot-lower", "session-root-env-wins", "precedence-matrix",
)
REQUIRED_TAGS = frozenset(
    [f"{file}:{case}" for file in ("project", "user") for case in _DECISION_FOUR]
    + [f"user:{case}" for case in _USER_ONLY]
    + list(_OTHER)
)


def _load(path: Path, name: str) -> ModuleType:
    spec = importlib.util.spec_from_file_location(name, path)
    assert spec is not None and spec.loader is not None
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


_HELPER_IN_TREE = "conexus/hooks/scripts/_audience.py"


def _release_tags() -> list[str]:
    """Every ``v<major>.<minor>.<patch>`` tag this checkout has, newest first ([] on a tagless clone)."""
    proc = subprocess.run(
        ["git", "tag", "--list", "v[0-9]*", "--sort=-version:refname"],
        cwd=REPO_ROOT, capture_output=True, text=True, encoding="utf-8", check=False,
    )
    return proc.stdout.split() if proc.returncode == 0 else []


def _previous_release_helper() -> tuple[str, bytes] | None:
    """``(tag, bytes)`` of the helper at the newest release tag that carries one, or None when no tag does.

    Not the marketplace pin: the release commit moves the pin to the tag it is about to cut, which does not exist yet,
    and a tag older than the helper's first release (8.5.7) has no file to compare."""
    for tag in _release_tags():
        proc = subprocess.run(["git", "show", f"{tag}:{_HELPER_IN_TREE}"], cwd=REPO_ROOT, capture_output=True, check=False)
        if proc.returncode == 0:
            return tag, proc.stdout
    return None


@pytest.fixture(scope="module")
def corpus() -> dict:
    return json.loads(CORPUS.read_text(encoding="utf-8"))


@pytest.fixture(scope="module")
def copies() -> dict[str, ModuleType]:
    return {
        "plugin": _load(PLUGIN_COPY, "_audience_plugin_copy"),
        "src": _load(SRC_COPY, "_audience_src_copy"),
    }


def _content(entry: dict) -> bytes:
    """A file entry's bytes: ``bytes_b64`` as given, else ``c`` as UTF-8; ``pad_to`` appends spaces up to that size."""
    if "bytes_b64" in entry:
        data = base64.b64decode(entry["bytes_b64"])
    else:
        data = entry.get("c", "").encode("utf-8")
    if "pad_to" in entry:
        assert len(data) <= entry["pad_to"], f"{entry} is larger than its pad_to"
        data += b" " * (entry["pad_to"] - len(data))
    return data


def _git(cwd: Path, args: list[str]) -> None:
    """Run git with no global or system config and a fixed identity, failing loudly on a non-zero exit."""
    cwd.mkdir(parents=True, exist_ok=True)
    env = {key: value for key, value in os.environ.items() if not key.startswith("GIT_")}
    env["GIT_CONFIG_GLOBAL"] = os.devnull
    env["GIT_CONFIG_SYSTEM"] = os.devnull
    proc = subprocess.run(
        ["git", "-c", "user.name=t", "-c", "user.email=t@t", *args],
        cwd=cwd, env=env, capture_output=True, check=False,
    )
    assert proc.returncode == 0, f"git {' '.join(args)} in {cwd} exited {proc.returncode}: {proc.stderr.decode('utf-8', 'replace')}"


def _apply(entry: dict, base: Path) -> None:
    """Lay out one entry. Layout entries are relative to the tmp dir; home_files entries to the account home."""
    kind = entry.get("k", "file")
    if kind == "git":
        _git(base.joinpath(*entry["dir"].split("/")), entry["args"])
        return
    target = base.joinpath(*entry["p"].split("/"))
    target.parent.mkdir(parents=True, exist_ok=True)
    if kind == "dir":
        target.mkdir(exist_ok=True)
    elif kind == "file":
        target.write_bytes(_content(entry))
    elif kind in ("link", "symlink"):
        os.symlink(base.joinpath(*entry["t"].split("/")), target)
    elif kind == "fifo":
        os.mkfifo(target)
    elif kind == "rm":
        target.unlink()
    else:  # pragma: no cover - a corpus typo
        raise AssertionError(f"unknown layout kind {kind!r}")


def _build(tmp: Path, row: dict) -> tuple[object, Path]:
    """Lay out the row's tree and return (payload, account home)."""
    for entry in row["layout"]:
        _apply(entry, tmp)
    home = tmp / "acct-home"
    home.mkdir()
    user = home / ".config" / "nexus" / "conexus.json"
    if row["user"] == "dir":
        user.mkdir(parents=True)
    elif isinstance(row["user"], str):
        user.parent.mkdir(parents=True)
        user.write_text(row["user"], encoding="utf-8")
    elif row["user"] is not None:
        user.parent.mkdir(parents=True)
        user.write_text(json.dumps(row["user"]), encoding="utf-8")
    for entry in row.get("home_files", []):
        _apply(entry, home)
    payload = row["payload"]
    if isinstance(payload, dict):
        payload = dict(payload)
        cwd = payload.get("cwd")
        if cwd is not None and not row.get("raw_cwd"):
            payload["cwd"] = str(tmp.joinpath(*cwd.split("/")))
    return payload, home


def _drop_empty_lists(values: dict) -> dict:
    """An empty list is an unset list key: a reader may leave the key out or keep it empty, and both answer the same."""
    return {key: value for key, value in values.items() if not (isinstance(value, list) and not value)}


def _answers(mod: ModuleType, payload: object, head: bool) -> dict:
    settings = mod.read_settings(payload, head=head)
    return {
        "guard_mode": mod.guard_mode(payload, head=head),
        "project": _drop_empty_lists(mod.project_settings(payload)),
        "user": _drop_empty_lists(mod.user_settings()),
        "telemetry": mod.telemetry_enabled(),
        "t1_nudge": mod.t1_nudge_enabled(),
        "advice_blocked": sorted(mod.advice_blocked(payload, head=head)),
        "brief_questions": list(settings.values["brief_questions"]),
        "integration_branch": mod.integration_branch(payload),
        "problems": sorted([problem.file, problem.key] for problem in settings.problems),
    }


def _skip_reason(row: dict, previous: bool) -> str:
    if row.get("posix_only") and os.name == "nt":
        return "posix only"
    if row.get("needs_git") and shutil.which("git") is None:
        return "no git on PATH"
    if previous and row.get("prev_release") is False:
        return "the previous release's helper cannot know this row"
    return ""


def test_the_two_copies_are_byte_identical() -> None:
    assert PLUGIN_COPY.read_bytes() == SRC_COPY.read_bytes(), (
        "src/nexus/hooks/_audience.py must be a byte copy of conexus/hooks/scripts/_audience.py: "
        "edit one, then `cp` it over the other"
    )


def test_the_corpus_is_big_enough_and_complete(corpus: dict) -> None:
    rows = corpus["rows"]
    assert len(rows) >= MIN_ROWS
    ids = [row["id"] for row in rows]
    assert len(ids) == len(set(ids)), "duplicate corpus row ids"
    assert set(COMPARED) <= set(corpus["defaults"]), "the corpus defaults must state every compared field"
    for row in rows:
        assert set(row.get("expect", {})) <= set(COMPARED) | {"remote_test_hosts"}, row["id"]
        for entry in row["layout"]:
            assert entry["k"] in _LAYOUT_KINDS, (row["id"], entry)
    # The retired markers are named once, in the corpus: a marker no row builds is untested, and the readers no longer know them.
    probe = _load(PLUGIN_COPY, "_audience_marker_probe")
    for name in ("LEGACY_MARKERS", "LEGACY_ACCEPTED_UNTIL"):
        assert not hasattr(probe, name), name
    layout_paths = {e["p"].split("/", 1)[1] for r in rows for e in r["layout"] if "p" in e and "/" in e["p"]}
    for marker in corpus["retired_markers"]:
        assert marker in layout_paths, f"no corpus row builds the retired marker {marker}"


def test_the_corpus_covers_every_decision_four_case(corpus: dict) -> None:
    covered = {tag for row in corpus["rows"] for tag in row.get("covers", [])}
    missing = REQUIRED_TAGS - covered
    assert not missing, f"no corpus row covers: {sorted(missing)}"
    unknown = covered - REQUIRED_TAGS
    assert not unknown, f"unknown cover tag(s), a typo? {sorted(unknown)}"


def _drive(
    label: str, mod: ModuleType, corpus: dict, tmp_path_factory: pytest.TempPathFactory, monkeypatch: pytest.MonkeyPatch,
    *, previous: bool = False,
) -> None:
    defaults = corpus["defaults"]
    original_home = getattr(mod, "account_home", None)
    examined = 0
    skipped: list[str] = []
    wrong: list[str] = []
    for row in corpus["rows"]:
        reason = _skip_reason(row, previous)
        if reason:
            skipped.append(f"{row['id']} ({reason})")
            continue
        tmp = tmp_path_factory.mktemp("row")
        payload, home = _build(tmp, row)
        monkeypatch.setattr(mod, "_ACCOUNT_HOME_OVERRIDE", str(home))
        if row.get("no_home"):
            monkeypatch.setattr(mod, "account_home", lambda: None)
        elif original_home is not None:
            monkeypatch.setattr(mod, "account_home", original_home)
        if row.get("env_project_dir"):
            monkeypatch.setenv("CLAUDE_PROJECT_DIR", str(tmp.joinpath(*row["env_project_dir"].split("/"))))
        else:
            monkeypatch.delenv("CLAUDE_PROJECT_DIR", raising=False)
        expect = {**defaults, **row.get("expect", {})}
        expect["project"] = _drop_empty_lists(expect["project"])
        expect["user"] = _drop_empty_lists(expect["user"])
        expect["problems"] = sorted(expect["problems"])
        expect["advice_blocked"] = sorted(expect["advice_blocked"])
        if previous:
            got = {"strict": mod.nexus_repo(payload)}
            want = {"strict": expect["project"].get("subagent_git") == "strict"}
        else:
            got = _answers(mod, payload, bool(row.get("head")))
            want = {key: expect[key] for key in COMPARED}
            if "remote_test_hosts" in row.get("expect", {}):
                got["remote_test_hosts"] = sorted(mod.remote_test_hosts())
                want["remote_test_hosts"] = sorted(row["expect"]["remote_test_hosts"])
        if got != want:
            wrong.append(f"{row['id']}: got {got}, corpus says {want}")
        examined += 1
    assert not wrong, f"{label} copy disagrees with the corpus on {len(wrong)} row(s):\n" + "\n".join(wrong)
    floor = MIN_PREVIOUS_ROWS if previous else MIN_ROWS
    assert examined >= floor, f"{label} copy examined {examined} rows (skipped: {skipped}); a pass over nothing"


@pytest.mark.parametrize("copy", ["plugin", "src"])
def test_every_live_copy_gives_the_corpus_answers(
    copy: str, corpus: dict, copies: dict[str, ModuleType], tmp_path_factory: pytest.TempPathFactory,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _drive(copy, copies[copy], corpus, tmp_path_factory, monkeypatch)


@pytest.mark.lint
def test_the_previous_releases_copy_gives_the_corpus_answers(
    corpus: dict, tmp_path_factory: pytest.TempPathFactory, monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The plugin an older install carries must agree with this corpus on every row it can know (``lint``: see the module
    docstring for why this is the one test that needs tags)."""
    found = _previous_release_helper()
    if found is None:
        if os.environ.get("CI") or os.environ.get("NX_REQUIRE_PREV_RELEASE_COPY") == "1":
            pytest.fail("no release tag with the audience helper is readable here: the checkout lost its tags, so nothing was compared")
        pytest.skip("no release tag with the audience helper is readable in this checkout (a shallow or tagless clone)")
    tag, data = found
    path = tmp_path_factory.mktemp("prev_release") / "_audience_prev.py"
    path.write_bytes(data)
    _drive(f"the {tag}", _load(path, "_audience_prev_release"), corpus, tmp_path_factory, monkeypatch, previous=True)


def test_the_src_copy_imports_as_a_package_module() -> None:
    from nexus.hooks import _audience  # noqa: PLC0415 -- the import path the src hooks use

    assert sys.modules["nexus.hooks._audience"] is _audience
