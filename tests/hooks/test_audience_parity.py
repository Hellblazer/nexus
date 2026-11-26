# SPDX-License-Identifier: AGPL-3.0-or-later
"""The settings helper exists twice, and a third copy is installed on every user's box (nexus-sby49.3; reworked in nexus-sby49.4).

``conexus/hooks/scripts/_audience.py`` runs in the plugin's command hooks (stdlib only, no ``nexus`` import).
``src/nexus/hooks/_audience.py`` runs inside ``nx-hook`` and the MCP server. They must be byte-identical, and the
older plugin that a newer checkout is paired with (the helper at the newest release tag that carries one) must give the
same answer on every row it can know (it knows the two legacy markers only). ``tests/fixtures/audience_corpus.json`` holds
layouts x user settings -> the answer of each reader; the two live copies are driven over it, so a change to one copy's walk,
files or defaults fails here until the other agrees.

Lives under tests/hooks/ so the Windows set runs the byte identity and the two live copies (LF, and the walk through a
worktree ``.git`` file on NTFS are claims a Windows checkout can break). The previous-release comparison needs the release
tags, and develop's ``test`` shards and the Windows job check out at depth 1 with none, so that one test is marked ``lint``:
the ``test-lint`` job checks out at depth 0 (``.github/workflows/ci.yml``, ``fetch-depth: 0``) and is the only CI job that
runs it. Elsewhere it skips when no tag is readable, unless ``NX_REQUIRE_PREV_RELEASE_COPY=1`` (or ``CI`` is set, which in
the lint job means the checkout lost its tags) makes that a failure.
"""
from __future__ import annotations

import importlib.util
import json
import os
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

#: A corpus below this size, or a copy that examined fewer rows than it should, is a vacuous pass.
MIN_ROWS = 25


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


def _build(tmp: Path, row: dict) -> tuple[dict | None, Path]:
    """Lay out the row's tree and return (payload, account home)."""
    for entry in row["layout"]:
        target = tmp.joinpath(*entry["p"].split("/"))
        kind = entry["k"]
        if kind == "dir":
            target.mkdir(parents=True, exist_ok=True)
        elif kind == "file":
            target.parent.mkdir(parents=True, exist_ok=True)
            target.write_text(entry.get("c", ""), encoding="utf-8")
        elif kind == "link":
            target.parent.mkdir(parents=True, exist_ok=True)
            os.symlink(tmp.joinpath(*entry["t"].split("/")), target, target_is_directory=True)
        else:  # pragma: no cover - a corpus typo
            raise AssertionError(f"unknown layout kind {kind!r}")
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
        target = home.joinpath(*entry["p"].split("/"))
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text(entry["c"], encoding="utf-8")
    payload = row["payload"]
    if isinstance(payload, dict):
        payload = dict(payload)
        cwd = payload.get("cwd")
        if cwd is not None and not row.get("raw_cwd"):
            payload["cwd"] = str(tmp.joinpath(*cwd.split("/")))
    return payload, home


def _answers(mod: ModuleType, payload: object) -> dict:
    return {
        "guard_mode": mod.guard_mode(payload),
        "project": mod.project_settings(payload),
        "telemetry": mod.telemetry_enabled(),
        "t1_nudge": mod.t1_nudge_enabled(),
        "advice_blocked": sorted(mod.advice_blocked(payload)),
    }


def test_the_two_copies_are_byte_identical() -> None:
    assert PLUGIN_COPY.read_bytes() == SRC_COPY.read_bytes(), (
        "src/nexus/hooks/_audience.py must be a byte copy of conexus/hooks/scripts/_audience.py: "
        "edit one, then `cp` it over the other"
    )


def test_the_corpus_is_big_enough_and_complete(corpus: dict) -> None:
    rows = corpus["rows"]
    assert len(rows) >= MIN_ROWS
    ids = [r["id"] for r in rows]
    assert len(ids) == len(set(ids)), "duplicate corpus row ids"
    for row in rows:
        assert {"guard_mode", "project", "telemetry", "t1_nudge", "advice_blocked"} <= set(row["expect"]), row["id"]
    # Both legacy markers are named once, here and in the helper: a marker the corpus does not mention is untested.
    plugin = _load(PLUGIN_COPY, "_audience_marker_probe")
    assert list(plugin.LEGACY_MARKERS) == corpus["markers"]
    layout_paths = {e["p"].split("/", 1)[1] for r in rows for e in r["layout"] if "/" in e["p"]}
    for marker in corpus["markers"]:
        assert marker in layout_paths, f"no corpus row builds the marker {marker}"


def _drive(
    label: str, mod: ModuleType, corpus: dict, tmp_path_factory: pytest.TempPathFactory, monkeypatch: pytest.MonkeyPatch,
    *, previous: bool = False,
) -> None:
    examined = 0
    skipped: list[str] = []
    wrong: list[str] = []
    for row in corpus["rows"]:
        if row.get("posix_only") and os.name == "nt":
            skipped.append(row["id"])
            continue
        if previous and row.get("prev_release") is False:
            skipped.append(row["id"])
            continue
        tmp = tmp_path_factory.mktemp("row")
        payload, home = _build(tmp, row)
        monkeypatch.setattr(mod, "_ACCOUNT_HOME_OVERRIDE", str(home))
        if row.get("env_project_dir"):
            monkeypatch.setenv("CLAUDE_PROJECT_DIR", str(tmp.joinpath(*row["env_project_dir"].split("/"))))
        else:
            monkeypatch.delenv("CLAUDE_PROJECT_DIR", raising=False)
        expect = dict(row["expect"])
        got = {"strict": mod.nexus_repo(payload)} if previous else _answers(mod, payload)
        if previous:
            expect = {"strict": expect["project"].get("subagent_git") == "strict"}
        if "remote_test_hosts" in expect and not previous:
            got["remote_test_hosts"] = sorted(mod.remote_test_hosts())
            expect["remote_test_hosts"] = sorted(expect["remote_test_hosts"])
        if got != expect:
            wrong.append(f"{row['id']}: got {got}, corpus says {expect}")
        examined += 1
    assert not wrong, f"{label} copy disagrees with the corpus on {len(wrong)} row(s):\n" + "\n".join(wrong)
    assert examined >= MIN_ROWS - 3, f"{label} copy examined {examined} rows (skipped: {skipped}); a pass over nothing"


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

    assert _audience.LEGACY_MARKERS == _load(PLUGIN_COPY, "_audience_marker_probe_2").LEGACY_MARKERS
    assert sys.modules["nexus.hooks._audience"] is _audience
