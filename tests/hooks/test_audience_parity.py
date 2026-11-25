# SPDX-License-Identifier: AGPL-3.0-or-later
"""The audience helper exists twice, and a third copy is installed on every user's box (nexus-sby49.3).

``conexus/hooks/scripts/_audience.py`` runs in the plugin's command hooks (stdlib only, no ``nexus`` import).
``src/nexus/hooks/_audience.py`` runs inside ``nx-hook`` and the MCP server. They must be byte-identical, and the
older plugin that a newer checkout is paired with (the copy at the plugin's pinned release tag) must give the same
answers on every row it can know. ``tests/fixtures/audience_corpus.json`` holds layouts x flag states -> answers; all
three copies are driven over it, so a change to one copy's walk, markers or flag file fails here until the others agree.

Lives under tests/hooks/ so the Windows set runs it: the byte identity (LF) and the walk through a worktree ``.git``
file on NTFS are exactly the claims a Windows checkout can break.
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


def _pinned_ref() -> str:
    data = json.loads(MARKETPLACE.read_text(encoding="utf-8"))
    for plugin in data["plugins"]:
        if plugin["name"] == "conexus":
            return str(plugin["source"]["ref"])
    raise AssertionError("marketplace.json has no conexus plugin")


def _previous_release_copy(tmp_path_factory: pytest.TempPathFactory) -> ModuleType | None:
    """The helper as the installed plugin carries it: ``git show <pinned ref>:...``.

    ``None`` when the tag cannot be read and the run is a local one. In CI the tag-fetch step is part of the job, so a
    missing tag there is a failure (a skip would report green having compared nothing).
    """
    ref = _pinned_ref()
    proc = subprocess.run(
        ["git", "show", f"{ref}:conexus/hooks/scripts/_audience.py"],
        cwd=REPO_ROOT, capture_output=True, check=False,
    )
    if proc.returncode != 0:
        if os.environ.get("CI") or os.environ.get("NX_REQUIRE_PREV_RELEASE_COPY") == "1":
            pytest.fail(f"the plugin's pinned tag {ref} is not readable here, so the previous release's copy was not compared")
        return None
    path = tmp_path_factory.mktemp("prev_release") / "_audience_prev.py"
    path.write_bytes(proc.stdout)
    return _load(path, "_audience_prev_release")


@pytest.fixture(scope="module")
def corpus() -> dict:
    return json.loads(CORPUS.read_text(encoding="utf-8"))


@pytest.fixture(scope="module")
def copies(tmp_path_factory: pytest.TempPathFactory) -> dict[str, ModuleType | None]:
    return {
        "plugin": _load(PLUGIN_COPY, "_audience_plugin_copy"),
        "src": _load(SRC_COPY, "_audience_src_copy"),
        "previous_release": _previous_release_copy(tmp_path_factory),
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
    flag = home / ".config" / "nexus" / "maintainer-profile"
    if row["flag"] == "dir":
        flag.mkdir(parents=True)
    elif row["flag"]:
        flag.parent.mkdir(parents=True)
        flag.write_text("")
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
    got = {
        "account_flag": mod.account_flag(),
        "nexus_repo": mod.nexus_repo(payload),
        "maintainer": mod.maintainer(payload),
    }
    return got


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
        assert {"account_flag", "nexus_repo", "maintainer"} <= set(row["expect"]), row["id"]
    # Both markers are named once, here and in the helper: a marker the corpus does not mention is untested.
    plugin = _load(PLUGIN_COPY, "_audience_marker_probe")
    assert list(plugin.MARKERS) == corpus["markers"]
    layout_paths = {e["p"].split("/", 1)[1] for r in rows for e in r["layout"] if "/" in e["p"]}
    for marker in corpus["markers"]:
        assert marker in layout_paths, f"no corpus row builds the marker {marker}"


@pytest.mark.parametrize("copy", ["plugin", "src", "previous_release"])
def test_every_copy_gives_the_corpus_answers(
    copy: str, corpus: dict, copies: dict[str, ModuleType | None], tmp_path_factory: pytest.TempPathFactory,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    mod = copies[copy]
    if mod is None:
        pytest.skip("the previous release's tag is not readable in this checkout (CI fails instead)")
    examined = 0
    skipped: list[str] = []
    wrong: list[str] = []
    for row in corpus["rows"]:
        if row.get("posix_only") and os.name == "nt":
            skipped.append(row["id"])
            continue
        if copy == "previous_release" and row.get("prev_release") is False:
            skipped.append(row["id"])
            continue
        tmp = tmp_path_factory.mktemp("row")
        payload, home = _build(tmp, row)
        monkeypatch.setattr(mod, "_ACCOUNT_HOME_OVERRIDE", str(home))
        if row.get("env_project_dir"):
            monkeypatch.setenv("CLAUDE_PROJECT_DIR", str(tmp.joinpath(*row["env_project_dir"].split("/"))))
        else:
            monkeypatch.delenv("CLAUDE_PROJECT_DIR", raising=False)
        got = _answers(mod, payload)
        expect = dict(row["expect"])
        if "remote_test_hosts" in expect:
            got["remote_test_hosts"] = sorted(mod.remote_test_hosts())
            expect["remote_test_hosts"] = sorted(expect["remote_test_hosts"])
        if got != expect:
            wrong.append(f"{row['id']}: got {got}, corpus says {expect}")
        examined += 1
    assert not wrong, f"{copy} copy disagrees with the corpus on {len(wrong)} row(s):\n" + "\n".join(wrong)
    assert examined >= MIN_ROWS - 3, f"{copy} copy examined {examined} rows (skipped: {skipped}); a pass over nothing"


def test_the_src_copy_imports_as_a_package_module() -> None:
    from nexus.hooks import _audience  # noqa: PLC0415 -- the import path the src hooks use

    assert _audience.MARKERS == _load(PLUGIN_COPY, "_audience_marker_probe_2").MARKERS
    assert sys.modules["nexus.hooks._audience"] is _audience
