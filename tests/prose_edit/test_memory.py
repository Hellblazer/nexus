# SPDX-License-Identifier: AGPL-3.0-or-later
"""memory.py (RDR-221 Step 1.2, nexus-ger02.1) against the real engine substrate.

The script runs as a subprocess; T2 is the per-test minted tenant of the real
engine (no mocks). `nxspy.py` records how the script drives `nx memory` so the
CLI constraints (content on stdin, reads by id, -y on delete) are pinned too.
"""
from __future__ import annotations

import ast
import importlib.util
import json
import os
import pty
import socket
import subprocess
import sys
import time
from pathlib import Path
from types import ModuleType

import pytest

from tests.prose_edit.conftest import (
    REPO_NAME,
    REPO_PROJECT,
    SCRIPT,
    USER_PROJECT,
    Prose,
    git,
    make_repo,
    t2_row,
    t2_json,
    t2_put,
    t2_titles,
)
from tests.prose_edit.test_brief import NAMES_A_REPAIR

TS = "20260929T171503.482913Z"  # PROSE_EDIT_NOW in the fixture, in title form


def _module() -> ModuleType:
    spec = importlib.util.spec_from_file_location("prose_edit_memory", SCRIPT)
    assert spec and spec.loader
    mod = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = mod
    spec.loader.exec_module(mod)
    return mod


# ---------------------------------------------------------------------------
# Pure pieces: no T2 needed
# ---------------------------------------------------------------------------


def test_script_is_stdlib_only_and_never_imports_nexus() -> None:
    tree = ast.parse(SCRIPT.read_text())
    imported: set[str] = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            imported |= {a.name.split(".")[0] for a in node.names}
        elif isinstance(node, ast.ImportFrom) and node.module:
            imported.add(node.module.split(".")[0])
    assert "nexus" not in imported
    assert imported <= set(sys.stdlib_module_names) | {"__future__"}, imported


@pytest.mark.parametrize(
    ("path", "genre"),
    [
        ("docs/rdr/rdr-221-prose-editor.md", "rdr"),
        ("docs/rdr/AGENTS.md", "rdr"),
        ("docs/rdr/diagram.png", None),  # rdr and exploration are markdown only
        ("docs/exploration/xanadu-in-nexus.md", "exploration-essay"),
        ("docs/exploration/data.json", None),
        ("web/getting-started.html", "how-to"),
        ("web/style.css", None),  # web/ is html only
        ("CHANGELOG.md", "changelog"),
        ("docs/querying-guide.md", "reference-doc"),
        ("docs/sub/deeper.md", None),  # deeper docs paths are the caller's to ask about
        ("docs/plans/plan.md", None),
        ("docs/img/diagram.png", None),
        ("src/nexus/cli.py", None),
    ],
)
def test_default_path_to_genre_map(path: str, genre: str | None) -> None:
    assert _module().genre_for(path, []) == genre


def test_genre_map_longest_prefix_wins_then_the_more_specific_layer() -> None:
    genre_for = _module().genre_for
    user, repo, site, doc = ["notes/=how-to"], ["notes/deep/=changelog"], [], []
    # A first-match reading (user is listed first) gives how-to; the longest prefix is repo's.
    assert genre_for("notes/deep/a.md", [user, repo, site, doc]) == "changelog"
    assert genre_for("notes/b.md", [user, repo, site, doc]) == "how-to"
    # Equal length: the later, more specific layer wins, at every step of the ladder.
    tie = [["ties/=how-to"], ["ties/=rdr"], ["ties/=changelog"], ["ties/=reference-doc"]]
    assert genre_for("ties/a.md", tie) == "reference-doc"
    assert genre_for("ties/a.md", tie[:3] + [[]]) == "changelog"
    assert genre_for("ties/a.md", tie[:2] + [[], []]) == "rdr"
    # The built-in defaults are the lowest layer: a tie goes to the override, a shorter
    # override loses to a longer built-in prefix.
    assert genre_for("docs/rdr/x.md", [["docs/rdr/=how-to"]]) == "how-to"
    assert genre_for("docs/rdr/x.md", [["docs/=how-to"]]) == "rdr"
    assert genre_for("docs/other.md", [["docs/=how-to"]]) == "how-to"


def test_projects_are_named_from_the_prefix_and_repo() -> None:
    mod = _module()
    assert mod.projects("", "nexus") == ("prose", "nexus_prose")
    assert mod.projects("pt_", "nexus") == ("pt_prose", "pt_nexus_prose")


def test_precedence_drives_the_merge_order(monkeypatch: pytest.MonkeyPatch) -> None:
    mod = _module()
    layers = {
        "user": {"scalars": {"k": "u"}, "lists": {}},
        "repo": {"scalars": {"k": "r"}, "lists": {}},
        "site-page": {"scalars": {"k": "s"}, "lists": {}},
        "document": {"scalars": {"k": "d"}, "lists": {}},
    }
    assert mod.merge_named(layers)["scalars"]["k"] == "d"
    monkeypatch.setattr(mod, "PRECEDENCE", ["document", "site-page", "repo", "user"])
    assert mod.merge_named(layers)["scalars"]["k"] == "u"  # the last listed layer wins


def test_merge_scalars_most_specific_wins_and_lists_add() -> None:
    mod = _module()
    merged = mod.merge_layers([
        {"scalars": {"tone": "u", "only_user": 1}, "lists": {"banned": ["a", "b"]}},
        {"scalars": {"tone": "r"}, "lists": {"banned": ["b", "c"]}},
        None,
        {"scalars": {"tone": "d"}, "lists": {"banned": ["d"], "other": ["x"]}},
    ])
    assert merged["scalars"] == {"tone": "d", "only_user": 1}
    assert merged["lists"] == {"banned": ["a", "b", "c", "d"], "other": ["x"]}


# ---------------------------------------------------------------------------
# <repo> and path resolution
# ---------------------------------------------------------------------------


def test_repo_is_the_same_from_primary_worktree_and_subdirectory(
    prose: Prose, repo: Path, tmp_path: Path
) -> None:
    """The name comes from the git common dir's parent, not the checkout's own
    directory. The worktree's directory name is deliberately different, so a
    toplevel-basename implementation returns 'wt-sibling' and fails here."""
    wt = tmp_path / "wt-sibling"
    git(repo, "worktree", "add", "-q", "-b", "side", str(wt))
    sub = repo / "docs" / "deeper"
    sub.mkdir()
    assert wt.name != REPO_NAME

    names = {
        label: prose.ok("repo", cwd=cwd)["repo"]
        for label, cwd in (("primary", repo), ("worktree", wt), ("subdir", sub))
    }
    assert names == {"primary": REPO_NAME, "worktree": REPO_NAME, "subdir": REPO_NAME}

    # <path> is repo-relative from all three, for an absolute and a relative spelling.
    assert prose.ok("repo", "docs/x.md", cwd=repo)["path"] == "docs/x.md"
    assert prose.ok("repo", "docs/x.md", cwd=wt)["path"] == "docs/x.md"
    assert prose.ok("repo", str(sub / "y.md"), cwd=sub)["path"] == "docs/deeper/y.md"
    assert prose.ok("repo", "x.md", cwd=repo / "docs")["path"] == "docs/x.md"
    assert prose.ok("repo", "docs/x.md", cwd=repo / "docs")["path"] == "docs/x.md"

    outside = prose.run("repo", "/etc/hosts", cwd=repo)
    assert outside.returncode != 0 and "outside" in outside.stderr


def test_a_root_relative_path_beats_a_cwd_relative_one_and_symlinks_stay_put(
    prose: Prose, repo: Path
) -> None:
    (repo / "docs" / "docs").mkdir()
    (repo / "docs" / "docs" / "x.md").write_text("collision\n")
    # Both docs/x.md (from the root) and docs/docs/x.md (from cwd) exist: the root wins.
    assert prose.ok("repo", "docs/x.md", cwd=repo / "docs")["path"] == "docs/x.md"

    # CLAUDE.md is a symlink to another file; the record must key on CLAUDE.md.
    (repo / "CLAUDE.md").symlink_to("docs/x.md")
    assert prose.ok("repo", "CLAUDE.md")["path"] == "CLAUDE.md"
    assert prose.ok("repo", str(repo / "CLAUDE.md"))["path"] == "CLAUDE.md"


def test_outside_a_git_repo_fails_loud(prose: Prose, tmp_path: Path) -> None:
    bare = tmp_path / "not-a-repo"
    bare.mkdir()
    proc = prose.run("repo", cwd=bare)
    assert proc.returncode != 0
    assert "git" in proc.stderr and proc.stdout == ""


def test_a_range_suffix_is_parsed_and_every_record_keys_on_the_bare_path(prose: Prose) -> None:
    assert prose.ok("repo", "CHANGELOG.md:44-60")["path"] == "CHANGELOG.md"
    read = prose.ok("read", "CHANGELOG.md:44-60")
    assert read["path"] == "CHANGELOG.md" and read["range"] == {"start": 44, "end": 60}
    assert read["genre"] == "changelog"  # the suffix did not defeat the path map
    assert prose.ok("read", "CHANGELOG.md")["range"] is None
    # A single line spelled PATH:N is the range N-N, not part of the file name.
    one = prose.ok("read", "CHANGELOG.md:44")
    assert one["path"] == "CHANGELOG.md" and one["range"] == {"start": 44, "end": 44}
    assert one["genre"] == "changelog"
    assert prose.ok("repo", "docs/x.md:3")["range"] == {"start": 3, "end": 3}
    for bad in ("docs/x.md:9-3", "docs/x.md:0-3", "docs/x.md:0"):
        assert prose.run("read", bad).returncode == 1, bad

    # A rejection stored during a range run filters the whole-file run.
    out = prose.ok("reject", "docs/x.md:2-3", "--old", OLD_C, "--new", "z")
    assert out["range"] == {"start": 2, "end": 3}
    assert t2_titles(REPO_PROJECT) == ["doc/docs/x.md"]
    whole = prose.ok("filter", "docs/x.md", stdin=_proposal((OLD_C, "z"), "kept"))
    assert [e["old"] for e in whole["edits"]] == ["kept"]
    assert [e["old"] for e in prose.ok("rejections", "docs/x.md:1-1")["rejections"]] == [OLD_C]

    log = prose.ok("log", "docs/x.md:2-3", stdin={"accepted": []})
    assert log["title"] == f"log/docs/x.md/{TS}" and log["range"] == {"start": 2, "end": 3}
    assert t2_json(REPO_PROJECT, log["title"])["range"] == {"start": 2, "end": 3}


# ---------------------------------------------------------------------------
# Layers and merge against real T2
# ---------------------------------------------------------------------------


def test_layers_merge_in_precedence_order_under_the_prefix(prose: Prose, tmp_path: Path) -> None:
    doc = "docs/x.md"
    # A fresh tenant: every layer is absent, and that is a normal answer, not an error.
    empty = prose.ok("read", doc)
    assert empty["layers"] == {"user": None, "repo": None, "document": None}
    assert empty["merged"] == {"scalars": {}, "lists": {}}
    assert empty["precedence"] == ["user", "repo", "site-page", "document"]

    prose.ok("add-entry", "--level", "user", "--key", "tone", "--value", "plain")
    prose.ok("add-entry", "--level", "user", "--key", "banned", "--value", "utilize", "--list")
    prose.ok("add-entry", "--level", "repo", "--key", "tone", "--value", "lead-with-point")
    prose.ok("add-entry", "--level", "repo", "--key", "banned", "--value", "load-bearing", "--list")
    prose.ok("add-entry", "--level", "repo", "--key", "banned", "--value", "load-bearing", "--list")

    merged = prose.ok("read", doc, "--genre", "reference-doc")
    assert merged["merged"]["scalars"]["tone"] == "lead-with-point"  # repo beats user
    assert merged["merged"]["lists"]["banned"] == ["utilize", "load-bearing"]  # add, deduped
    assert merged["layers"]["document"] is None
    assert merged["genre"] == "reference-doc" and merged["genre_source"] == "flag"

    # The site-page layer slots between repo and document.
    site = tmp_path / "site.json"
    site.write_text(json.dumps({"scalars": {"tone": "site"}, "lists": {"banned": ["it is known"]}}))
    with_site = prose.ok("read", doc, "--site-layer", str(site))
    assert with_site["merged"]["scalars"]["tone"] == "site"
    assert with_site["merged"]["lists"]["banned"] == ["utilize", "load-bearing", "it is known"]

    prose.ok("add-entry", "--level", "doc", "--path", doc, "--key", "tone", "--value", "doc-tone")
    prose.ok("add-entry", "--level", "doc", "--path", doc, "--key", "banned", "--value", "very", "--list")
    final = prose.ok("read", doc, "--site-layer", str(site))
    assert final["merged"]["scalars"]["tone"] == "doc-tone"  # document beats site beats repo
    assert final["merged"]["lists"]["banned"] == ["utilize", "load-bearing", "it is known", "very"]

    # Everything sits under the prefixed projects; the live names were never touched.
    assert t2_titles(USER_PROJECT) == ["stylesheet"]
    assert t2_titles(REPO_PROJECT) == ["doc/docs/x.md", "stylesheet"]
    assert t2_titles("prose") == [] and t2_titles(f"{REPO_NAME}_prose") == []
    # Style-sheet and document records are permanent.
    for project, title in ((USER_PROJECT, "stylesheet"), (REPO_PROJECT, "doc/docs/x.md")):
        assert t2_row(project, title)["ttl"] is None


def test_user_entry_reaches_a_document_in_another_genre(prose: Prose) -> None:
    prose.ok("add-entry", "--level", "user", "--key", "banned", "--value", "leverage", "--list")
    got = prose.ok("read", "CHANGELOG.md")
    assert got["genre"] == "changelog" and got["genre_source"] == "map"
    assert got["merged"]["lists"]["banned"] == ["leverage"]


def test_add_entry_from_stdin_json_merges_into_the_record(prose: Prose) -> None:
    prose.ok("add-entry", "--level", "repo", "--from-stdin",
             stdin={"scalars": {"header": "H"}, "lists": {"rules": ["r1"]}})
    prose.ok("add-entry", "--level", "repo", "--from-stdin",
             stdin={"scalars": {"extra": "E"}, "lists": {"rules": ["r1", "r2"]}})
    rec = t2_json(REPO_PROJECT, "stylesheet")
    assert rec["scalars"] == {"header": "H", "extra": "E"}
    assert rec["lists"] == {"rules": ["r1", "r2"]}


@pytest.mark.parametrize(
    "body",
    [{"scalars": ["a"]}, {"scalars": "x"}, {"lists": ["a"]}, {"lists": {"k": "abc"}},
     {"lists": {"k": 3}}, {"other": {}}, ["a"]],
    ids=json.dumps,
)
def test_add_entry_from_stdin_type_checks_its_input(prose: Prose, body: object) -> None:
    proc = prose.run("add-entry", "--level", "repo", "--from-stdin", stdin=json.dumps(body))
    assert proc.returncode == 1 and proc.stdout == "" and "Traceback" not in proc.stderr
    assert t2_titles(REPO_PROJECT) == []


def _seed_sheet(prose: Prose, *level_args: str) -> None:
    prose.ok("add-entry", *level_args, "--key", "tone", "--value", "plain")
    prose.ok("add-entry", *level_args, "--key", "voice", "--value", "warm")
    for word in ("utilize", "leverage", "very"):
        prose.ok("add-entry", *level_args, "--key", "banned", "--value", word, "--list")
    prose.ok("add-entry", *level_args, "--key", "genre_map", "--value", "notes/=how-to", "--list")


def test_entries_lists_and_takes_back_style_sheet_entries_at_every_level(prose: Prose) -> None:
    doc = "docs/x.md"
    _seed_sheet(prose, "--level", "repo")
    _seed_sheet(prose, "--level", "user")

    listed = prose.ok("entries", "--level", "repo")
    assert listed["title"] == "stylesheet" and listed["project"] == REPO_PROJECT
    assert listed["scalars"] == {"tone": "plain", "voice": "warm"}
    assert listed["lists"] == {"banned": ["utilize", "leverage", "very"], "genre_map": ["notes/=how-to"]}
    assert prose.ok("read", doc)["merged"]["scalars"]["tone"] == "plain"

    # A scalar goes; the merged read no longer has it (repo and user both held it, so
    # remove the repo copy first and the user copy still answers, then the user copy).
    out = prose.ok("entries", "--level", "repo", "--remove", "tone")
    assert out["scalars"] == {"voice": "warm"}
    assert t2_json(REPO_PROJECT, "stylesheet")["scalars"] == {"voice": "warm"}
    prose.ok("entries", "--level", "user", "--remove", "tone")
    assert "tone" not in prose.ok("read", doc)["merged"]["scalars"]

    # One value leaves a list; the others stay. A value containing '=' splits on the first '='.
    out = prose.ok("entries", "--level", "repo", "--remove-item", "banned=leverage")
    assert out["lists"]["banned"] == ["utilize", "very"]
    prose.ok("entries", "--level", "repo", "--remove-item", "genre_map=notes/=how-to")
    assert prose.ok("genre-for", "notes/a.md")["genre"] == "how-to"  # the user copy still maps it
    prose.ok("entries", "--level", "user", "--remove-item", "genre_map=notes/=how-to")
    assert prose.ok("genre-for", "notes/a.md")["genre"] is None
    read = prose.ok("read", doc)
    assert read["layers"]["repo"]["lists"]["banned"] == ["utilize", "very"]
    assert "genre_map" not in read["merged"]["lists"]

    # A whole list goes with --remove.
    out = prose.ok("entries", "--level", "repo", "--remove", "banned")
    assert "banned" not in out["lists"] and out["lists"] == {}
    assert prose.ok("read", doc)["layers"]["repo"]["lists"] == {}

    # Removing the last entry deletes the emptied record (and never with a prompt).
    prose.ok("entries", "--level", "repo", "--remove", "voice")
    assert "stylesheet" not in t2_titles(REPO_PROJECT)
    assert prose.ok("entries", "--level", "repo") == {
        "level": "repo", "project": REPO_PROJECT, "title": "stylesheet", "scalars": {}, "lists": {}}
    deletes = [c for c in prose.calls() if c[:2] == ["memory", "delete"]]
    assert len(deletes) == 1 and "-y" in deletes[0] and "--id" in deletes[0]
    assert "stylesheet" in t2_titles(USER_PROJECT)  # the other level is untouched


def test_entries_at_document_level_keeps_rejections_until_the_record_is_empty(prose: Prose) -> None:
    doc = "docs/x.md"
    prose.ok("add-entry", "--level", "doc", "--path", doc, "--key", "tone", "--value", "dry")
    prose.ok("add-entry", "--level", "doc", "--path", doc, "--key", "banned", "--value", "very", "--list")
    prose.ok("reject", doc, "--old", "A", "--new", "B")
    assert prose.ok("entries", "--level", "doc", "--path", doc)["scalars"] == {"tone": "dry"}

    prose.ok("entries", "--level", "doc", "--path", doc, "--remove", "tone")
    prose.ok("entries", "--level", "doc", "--path", doc, "--remove-item", "banned=very")
    # Only the rejection is left, so the record stays and the rejection survives.
    assert t2_json(REPO_PROJECT, "doc/docs/x.md")["rejections"][0]["old"] == "A"
    assert prose.ok("read", doc)["merged"] == {"scalars": {}, "lists": {}}

    # Emptied of everything, the record is deleted.
    prose.ok("rejections", doc, "--remove", "1")
    assert t2_titles(REPO_PROJECT) == []
    prose.ok("add-entry", "--level", "doc", "--path", doc, "--key", "tone", "--value", "dry")
    prose.ok("entries", "--level", "doc", "--path", doc, "--remove", "tone")
    assert t2_titles(REPO_PROJECT) == []


def test_entries_refuses_what_is_not_there(prose: Prose) -> None:
    prose.ok("add-entry", "--level", "repo", "--key", "banned", "--value", "very", "--list")
    for bad in (
        ("--level", "repo", "--remove", "nope"),
        ("--level", "repo", "--remove-item", "banned=absent"),
        ("--level", "repo", "--remove-item", "nolist=x"),
        ("--level", "repo", "--remove-item", "no-equals-sign"),
        ("--level", "user", "--remove", "banned"),  # no user record at all
        ("--level", "doc", "--remove", "banned"),  # doc level needs --path
        ("--level", "repo", "--remove", "a", "--remove-item", "b=c"),
    ):
        proc = prose.run("entries", *bad)
        assert proc.returncode in (1, 2) and proc.stdout == "" and "Traceback" not in proc.stderr, bad
    assert t2_json(REPO_PROJECT, "stylesheet")["lists"] == {"banned": ["very"]}


def test_concurrent_entry_removals_lose_none(prose: Prose, repo: Path) -> None:
    for i in range(4):
        prose.ok("add-entry", "--level", "repo", "--key", "banned", "--value", f"w{i}", "--list")
    prose.ok("add-entry", "--level", "repo", "--key", "keep", "--value", "k")
    env = dict(prose.env, PROSE_EDIT_NX_DELAY_PUT="2")
    procs = [prose.popen("entries", "--level", "repo", "--remove-item", f"banned=w{i}", stdin={},
                         cwd=repo, env=env) for i in range(4)]
    for p in procs:
        _out, err = p.communicate(timeout=240)
        assert p.returncode == 0, err
    rec = t2_json(REPO_PROJECT, "stylesheet")
    assert rec["lists"].get("banned", []) == [] and rec["scalars"] == {"keep": "k"}


def test_genre_flag_and_genre_records_accept_only_the_six_genres(prose: Prose) -> None:
    for genre in ("rdr", "reference-doc", "how-to", "exploration-essay", "changelog", "commit-message"):
        assert prose.ok("read", "docs/x.md", "--genre", genre)["genre"] == genre
    for bad in (("read", "docs/x.md", "--genre", "poem"), ("log", "docs/x.md", "--genre", "poem"),
                ("exemplar-add", "poem", "docs/x.md:1-1"), ("genre-put", "poem")):
        proc = prose.run(*bad, stdin="{}")
        assert proc.returncode == 1 and "poem" in proc.stderr and proc.stdout == "", bad
    assert t2_titles(REPO_PROJECT) == []


def test_genre_map_layers_resolve_the_same_way_in_read_and_genre_for(
    prose: Prose, tmp_path: Path
) -> None:
    """A user 'notes/' entry is listed before a repo 'notes/deep/' entry in the merged
    list; a first-match reading returns the user's genre. Both verbs share one merge."""
    prose.ok("add-entry", "--level", "user", "--key", "genre_map", "--value", "notes/=how-to", "--list")
    prose.ok("add-entry", "--level", "repo", "--key", "genre_map", "--value", "notes/deep/=changelog", "--list")
    for verb_out in (prose.ok("genre-for", "notes/deep/a.md"), prose.ok("read", "notes/deep/a.md")):
        assert verb_out["genre"] == "changelog"
    assert prose.ok("genre-for", "notes/b.md")["genre"] == "how-to"
    assert prose.ok("genre-for", "docs/querying-guide.md")["genre"] == "reference-doc"

    # Ties climb the layers: repo, then site-page, then the document record.
    prose.ok("add-entry", "--level", "user", "--key", "genre_map", "--value", "ties/=how-to", "--list")
    prose.ok("add-entry", "--level", "repo", "--key", "genre_map", "--value", "ties/=rdr", "--list")
    assert prose.ok("genre-for", "ties/a.md")["genre"] == "rdr"
    site = tmp_path / "site.json"
    site.write_text(json.dumps({"lists": {"genre_map": ["ties/=changelog"]}}))
    assert prose.ok("genre-for", "ties/a.md", "--site-layer", str(site))["genre"] == "changelog"
    prose.ok("add-entry", "--level", "doc", "--path", "ties/a.md", "--key", "genre_map",
             "--value", "ties/=reference-doc", "--list")
    assert prose.ok("genre-for", "ties/a.md", "--site-layer", str(site))["genre"] == "reference-doc"
    assert prose.ok("read", "ties/a.md", "--site-layer", str(site))["genre"] == "reference-doc"
    assert prose.ok("genre-for", "ties/other.md")["genre"] == "rdr"  # the document layer is per document


def test_titles_that_share_a_prefix_never_answer_for_each_other(prose: Prose) -> None:
    """`nx memory get -t` falls back to a unique prefix match, so doc/a.md would
    quietly return doc/a.md.bak. memory.py confirms exact titles with `list`."""
    prose.ok("add-entry", "--level", "doc", "--path", "docs/a.md.bak", "--key", "who", "--value", "bak")

    # Only the .bak record exists: docs/a.md is absent, whatever the prefix says.
    assert prose.ok("read", "docs/a.md")["layers"]["document"] is None
    assert prose.ok("rejections", "docs/a.md")["rejections"] == []
    proc = prose.run("rejections", "docs/a.md", "--remove", "1")
    assert proc.returncode != 0
    assert t2_json(REPO_PROJECT, "doc/docs/a.md.bak")["scalars"] == {"who": "bak"}

    prose.ok("add-entry", "--level", "doc", "--path", "docs/a.md", "--key", "who", "--value", "a")
    assert prose.ok("read", "docs/a.md")["layers"]["document"]["scalars"] == {"who": "a"}
    assert prose.ok("read", "docs/a.md.bak")["layers"]["document"]["scalars"] == {"who": "bak"}
    assert t2_json(REPO_PROJECT, "doc/docs/a.md.bak")["scalars"] == {"who": "bak"}


def test_a_malformed_record_or_site_layer_stops_the_command_without_a_traceback(
    prose: Prose, tmp_path: Path
) -> None:
    def fails(*args: str, stdin: str | None = None) -> str:
        proc = prose.run(*args, stdin=stdin)
        assert proc.returncode == 1, (args, proc.returncode, proc.stderr)
        assert proc.stdout == "" and "Traceback" not in proc.stderr
        return proc.stderr

    for body in ('["not", "an", "object"]', '{"lists": {"k": "abc"}}', '{"scalars": []}', "not json"):
        t2_put(REPO_PROJECT, "stylesheet", body)
        assert f"record {REPO_PROJECT}/stylesheet is malformed" in fails("read", "docs/x.md"), body
        assert "malformed" in fails("add-entry", "--level", "repo", "--key", "a", "--value", "b")
    t2_put(REPO_PROJECT, "stylesheet", "{}")
    t2_put(REPO_PROJECT, "doc/docs/x.md", '{"rejections": [{"old": 1, "new": "x"}]}')
    assert "malformed" in fails("filter", "docs/x.md", stdin=json.dumps(_proposal("x")))
    t2_put(REPO_PROJECT, "doc/docs/x.md", "{}")
    t2_put(REPO_PROJECT, "not-a-defect", '{"entries": "x"}')
    assert "malformed" in fails("not-a-defect", "--level", "repo")
    t2_put(REPO_PROJECT, "genre/rdr", '{"exemplars": [{"text": "t"}]}')
    t2_put(REPO_PROJECT, "not-a-defect", "{}")
    assert "malformed" in fails("read", "docs/rdr/a.md")

    site = tmp_path / "site.json"
    for text in ("[1, 2]", '{"lists": {"a": "str"}}', '{"scalars": 3}', "{not json"):
        site.write_text(text)
        assert "--site-layer" in fails("read", "docs/x.md", "--site-layer", str(site)), text
    assert "--site-layer" in fails("read", "docs/x.md", "--site-layer", str(tmp_path / "missing.json"))


# ---------------------------------------------------------------------------
# Rejections: store, filter, list, remove, promote
# ---------------------------------------------------------------------------

# Verbatim means verbatim: quotes, a line break, a non-ASCII dash, and edge whitespace.
OLD_A = ' It is "known" that the cache is\nload-bearing — in some cases.\n'
NEW_A = "  The cache matters.  "
OLD_C = "A different sentence entirely."


def _proposal(*edits: str | tuple[str, str]) -> dict:
    """A proposal; an item is an old string (its new string is "new<i>") or an (old, new) pair. A stored
    rejection drops an edit only when the change is the same, so a test that expects a drop names the new text."""
    pairs = [e if isinstance(e, tuple) else (e, f"new{i}") for i, e in enumerate(edits)]
    return {
        "voice_card": "vc",
        "note": "n",
        "paragraphs": [{"n": 1, "action": "keep", "paragraphs": "P1", "advice": ""}],
        "edits": [{"n": i + 1, "old": o, "new": n, "reason": "r"} for i, (o, n) in enumerate(pairs)],
        "queries": [{"n": 1, "anchor": "a", "text": "q"}],
    }


def test_reject_filter_list_remove_journey(prose: Prose) -> None:
    doc = "docs/x.md"
    prose.ok("reject", doc, "--from-stdin", stdin={"old": OLD_A, "new": NEW_A})

    # Stored verbatim in the document record, in the repo project.
    rec = t2_json(REPO_PROJECT, "doc/docs/x.md")
    assert [(r["old"], r["new"]) for r in rec["rejections"]] == [(OLD_A, NEW_A)]

    # The filter drops the stored old string and passes a different one; the rest of
    # the proposal is untouched and edit numbers keep their meaning (a gap remains).
    out = prose.ok("filter", doc, stdin=_proposal((OLD_A, NEW_A), OLD_C))
    assert [e["old"] for e in out["edits"]] == [OLD_C]
    assert out["edits"][0]["n"] == 2
    assert out["dropped"] == [{"n": 1, "old": OLD_A, "cause": "rejected"}]
    assert out["voice_card"] == "vc" and out["queries"] and out["paragraphs"]

    # Scoped to the document: the same old string on another document passes.
    other = prose.ok("filter", "docs/y.md", stdin=_proposal(OLD_A))
    assert [e["old"] for e in other["edits"]] == [OLD_A]

    # Listing is numbered; removing restores the edit.
    prose.ok("reject", doc, "--old", OLD_C, "--new", "z")
    listed = prose.ok("rejections", doc)["rejections"]
    assert [(r["n"], r["old"]) for r in listed] == [(1, OLD_A), (2, OLD_C)]
    after = prose.ok("rejections", doc, "--remove", "1")["rejections"]
    assert [(r["n"], r["old"]) for r in after] == [(1, OLD_C)]
    restored = prose.ok("filter", doc, stdin=_proposal((OLD_A, NEW_A), (OLD_C, "z")))
    assert [e["old"] for e in restored["edits"]] == [OLD_A]
    assert [d["old"] for d in restored["dropped"]] == [OLD_C]

    # Removal is by number, and the numbers close up: drop the middle of three.
    prose.ok("reject", doc, "--old", "third", "--new", "3")
    prose.ok("reject", doc, "--old", "fourth", "--new", "4")
    assert [r["old"] for r in prose.ok("rejections", doc)["rejections"]] == [OLD_C, "third", "fourth"]
    after = prose.ok("rejections", doc, "--remove", "2")["rejections"]
    assert [(r["n"], r["old"]) for r in after] == [(1, OLD_C), (2, "fourth")]

    bad = prose.run("rejections", doc, "--remove", "7")
    assert bad.returncode != 0 and "7" in bad.stderr


def test_rejecting_the_same_change_again_replaces_it_in_place(prose: Prose) -> None:
    doc = "docs/x.md"
    prose.ok("reject", doc, "--old", "X", "--new", "first")
    prose.ok("reject", doc, "--old", "Y", "--new", "y")
    prose.ok("reject", doc, "--old", "X", "--new", "first")  # the same change: the entry is replaced, not repeated
    prose.ok("reject", doc, "--old", "X", "--new", "second")  # another replacement of X is another change
    listed = prose.ok("rejections", doc)["rejections"]
    assert [(r["n"], r["old"], r["new"]) for r in listed] == [(1, "X", "first"), (2, "Y", "y"), (3, "X", "second")]


def test_a_pure_cut_is_a_valid_rejection_with_an_empty_new_string(prose: Prose) -> None:
    doc = "docs/x.md"
    prose.ok("reject", doc, "--old", OLD_C, "--new", "")
    assert t2_json(REPO_PROJECT, "doc/docs/x.md")["rejections"][0]["new"] == ""
    out = prose.ok("filter", doc, stdin=_proposal((OLD_C, "")))
    assert out["edits"] == [] and out["dropped"][0]["old"] == OLD_C
    empty_old = prose.run("reject", doc, "--old", "", "--new", "x")
    assert empty_old.returncode == 1


BASE_EDIT = {"n": 1, "old": "o", "new": "n", "reason": "r"}


@pytest.mark.parametrize(
    "body",
    [
        "not json",
        {"no_edits": []},
        {"edits": [{**BASE_EDIT, "old": 5}]},
        {"edits": [{**BASE_EDIT, "old": ""}]},  # old must be non-empty
        {"edits": [{**BASE_EDIT, "n": 0}]},
        {"edits": [{**BASE_EDIT, "n": -2}]},
        {"edits": [{**BASE_EDIT, "n": "1"}]},
        {"edits": [{**BASE_EDIT, "n": True}]},
        {"edits": [{k: v for k, v in BASE_EDIT.items() if k != "n"}]},
        {"edits": [BASE_EDIT, {**BASE_EDIT, "old": "p"}]},  # duplicate n
        {"edits": [{k: v for k, v in BASE_EDIT.items() if k != "reason"}]},
        {"edits": [{**BASE_EDIT, "reason": None}]},
        {"edits": [{**BASE_EDIT, "new": None}]},
        {"edits": [BASE_EDIT], "paragraphs": [{"action": "keep"}]},
        {"edits": [BASE_EDIT], "queries": [{"n": "Q1", "anchor": "a", "text": "t"}]},
        {"edits": [BASE_EDIT], "queries": "nope"},
        {"edits": [{**BASE_EDIT, "new": "o"}]},  # old == new is not an edit
        {"edits": [BASE_EDIT], "queries": [{"n": 1, "text": "t"}]},  # anchor missing
        {"edits": [BASE_EDIT], "queries": [{"n": 1, "anchor": "", "text": "t"}]},
        {"edits": [BASE_EDIT], "queries": [{"n": 1, "anchor": 3, "text": "t"}]},
        {"edits": [BASE_EDIT], "queries": [{"n": 1, "anchor": "a"}]},  # text missing
        {"edits": [BASE_EDIT], "queries": [{"n": 1, "anchor": "a", "text": None}]},
        {"edits": [BASE_EDIT], "queries": [{"n": 1, "anchor": "a", "text": "t"},
                                           {"n": 1, "anchor": "b", "text": "u"}]},  # duplicate n
        {"edits": [BASE_EDIT], "paragraphs": [{"n": 2}, {"n": 2}]},  # duplicate n
    ],
    ids=lambda b: b if isinstance(b, str) else json.dumps(b),
)
def test_filter_rejects_a_malformed_proposal(prose: Prose, body: dict | str) -> None:
    proc = prose.run("filter", "docs/x.md", stdin=body if isinstance(body, str) else json.dumps(body))
    assert proc.returncode == 1, proc.stdout
    assert proc.stdout == "" and "proposal" in proc.stderr


def test_filter_accepts_one_fenced_json_block_and_nothing_looser(prose: Prose) -> None:
    text = json.dumps(_proposal("kept sentence"), indent=1)
    for wrapped in (f"```json\n{text}\n```\n", f"\n```\n{text}\n```"):
        out = prose.ok("filter", "docs/x.md", stdin=wrapped)
        assert [e["old"] for e in out["edits"]] == ["kept sentence"]
    for loose in (f"Here you go:\n```json\n{text}\n```", f"```json\n{text}\n```\n```json\n{text}\n```"):
        assert prose.run("filter", "docs/x.md", stdin=loose).returncode == 1


def test_promote_dry_run_then_write_and_list_and_remove(prose: Prose) -> None:
    doc = "docs/x.md"
    prose.ok("reject", doc, "--old", OLD_A, "--new", NEW_A)
    prose.ok("reject", doc, "--old", OLD_C, "--new", "z")
    # A rejection alone never becomes a general entry, and neither does a dry run.
    assert "not-a-defect" not in t2_titles(REPO_PROJECT) + t2_titles(USER_PROJECT)
    dry = prose.ok("promote", doc, "1", "--level", "repo", "--dry-run")
    assert dry["dry_run"] is True and dry["entry"]["old"] == OLD_A and dry["entry"]["new"] == NEW_A
    assert "not-a-defect" not in t2_titles(REPO_PROJECT) + t2_titles(USER_PROJECT)

    out = prose.ok("promote", doc, "1", "--level", "repo")
    assert out["level"] == "repo" and out["entry"]["old"] == OLD_A
    entries = t2_json(REPO_PROJECT, "not-a-defect")["entries"]
    assert [(e["old"], e["new"]) for e in entries] == [(OLD_A, NEW_A)]
    assert "not-a-defect" not in t2_titles(USER_PROJECT)
    prose.ok("promote", doc, "2", "--level", "user")
    assert [e["old"] for e in t2_json(USER_PROJECT, "not-a-defect")["entries"]] == [OLD_C]

    # The rejections stay where they were, and a promoted entry reaches the editor through
    # the merged read; it does NOT drop an edit in another document (only that document's
    # own rejections filter).
    assert len(prose.ok("rejections", doc)["rejections"]) == 2
    fil = prose.ok("filter", "docs/y.md", stdin=_proposal(OLD_A, OLD_C, "kept"))
    assert [e["old"] for e in fil["edits"]] == [OLD_A, OLD_C, "kept"] and fil["dropped"] == []
    read = prose.ok("read", "docs/y.md")
    assert {e["old"] for e in read["merged"]["lists"]["not-a-defect"]} == {OLD_A, OLD_C}

    # Listing is numbered per level; removing one by number deletes just it.
    prose.ok("promote", doc, "2", "--level", "repo")
    listed = prose.ok("not-a-defect", "--level", "repo")["entries"]
    assert [(e["n"], e["old"]) for e in listed] == [(1, OLD_A), (2, OLD_C)]
    left = prose.ok("not-a-defect", "--level", "repo", "--remove", "1")["entries"]
    assert [(e["n"], e["old"]) for e in left] == [(1, OLD_C)]
    assert [e["old"] for e in t2_json(USER_PROJECT, "not-a-defect")["entries"]] == [OLD_C]
    assert prose.run("not-a-defect", "--level", "repo", "--remove", "5").returncode == 1
    # Removing the last entry deletes the record.
    prose.ok("not-a-defect", "--level", "repo", "--remove", "1")
    assert "not-a-defect" not in t2_titles(REPO_PROJECT)
    assert prose.ok("not-a-defect", "--level", "repo")["entries"] == []

    for bad in (("promote", doc, "9", "--level", "repo"), ("promote", doc, "1", "--level", "everyone")):
        assert prose.run(*bad).returncode != 0


# ---------------------------------------------------------------------------
# Session log and stdin runs
# ---------------------------------------------------------------------------


def test_session_log_is_titled_by_path_and_time_and_expires_in_90_days(prose: Prose) -> None:
    body = {"proposed": 2, "accepted": [1], "rejected": [2]}
    out = prose.ok("log", "docs/x.md", "--genre", "reference-doc", stdin=body)
    assert out["title"] == f"log/docs/x.md/{TS}"
    assert t2_titles(REPO_PROJECT) == [f"log/docs/x.md/{TS}"]
    row = t2_row(REPO_PROJECT, f"log/docs/x.md/{TS}")
    assert row["ttl"] == 90
    rec = json.loads(row["content"])
    assert rec["session"] == body and rec["genre"] == "reference-doc" and rec["path"] == "docs/x.md"
    # A log run writes no document record and no other project.
    assert t2_titles(USER_PROJECT) == []


def test_the_test_clock_is_ignored_unless_the_test_flag_is_also_set(prose: Prose) -> None:
    env = {k: v for k, v in prose.env.items() if k != "PROSE_EDIT_TEST"}
    assert env["PROSE_EDIT_NOW"]
    out = json.loads(prose.run("log", "docs/x.md", env=env, stdin="{}").stdout)
    assert out["title"].startswith("log/docs/x.md/") and out["title"] != f"log/docs/x.md/{TS}"


def test_log_refuses_a_terminal_on_stdin(prose: Prose) -> None:
    master, slave = pty.openpty()
    try:
        proc = subprocess.run(
            [sys.executable, str(SCRIPT), "log", "docs/x.md"], stdin=slave,
            capture_output=True, text=True, cwd=prose.cwd, env=prose.env, timeout=60,
        )
    finally:
        os.close(master)
        os.close(slave)
    assert proc.returncode == 1 and "terminal" in proc.stderr
    assert t2_titles(REPO_PROJECT) == []


def test_stdin_run_logs_under_log_stdin_and_writes_no_document_record(prose: Prose) -> None:
    prose.ok("log", "-", "--genre", "commit-message", stdin={"accepted": [1]})
    assert t2_titles(REPO_PROJECT) == [f"log/stdin/{TS}"]
    assert t2_row(REPO_PROJECT, f"log/stdin/{TS}")["ttl"] == 90
    assert t2_titles(USER_PROJECT) == []

    # A stdin run keeps no document record and stores no rejections.
    read = prose.ok("read", "-", "--genre", "commit-message")
    assert read["layers"]["document"] is None and read["path"] is None
    proc = prose.run("reject", "-", "--old", "a", "--new", "b")
    assert proc.returncode != 0 and "stdin" in proc.stderr
    assert prose.run("rejections", "-").returncode != 0
    assert prose.ok("filter", "-", stdin=_proposal("x"))["edits"][0]["old"] == "x"
    assert t2_titles(REPO_PROJECT) == [f"log/stdin/{TS}"]


# ---------------------------------------------------------------------------
# Genres, exemplars, viewer
# ---------------------------------------------------------------------------


def test_exemplar_stores_the_passage_text_not_a_reference(prose: Prose, repo: Path) -> None:
    prose.ok("exemplar-add", "reference-doc", "docs/x.md:2-3")
    rec = t2_json(REPO_PROJECT, "genre/reference-doc")
    assert rec["exemplars"] == [{"text": "two\nthree", "path": "docs/x.md", "start": 2, "end": 3}]

    # Editing the source afterwards does not change the stored exemplar.
    (repo / "docs" / "x.md").write_text("changed\nchanged\nchanged\n")
    prose.ok("exemplar-add", "reference-doc", "docs/x.md:1-1")
    rec = t2_json(REPO_PROJECT, "genre/reference-doc")
    assert [e["text"] for e in rec["exemplars"]] == ["two\nthree", "changed"]

    prose.ok("genre-put", "rdr", stdin={"exemplars": [], "notes": ["see docs/rdr/REGISTER.md"]})
    got = prose.ok("read", "docs/rdr/rdr-9.md")
    assert got["genre"] == "rdr" and got["genre_record"]["notes"] == ["see docs/rdr/REGISTER.md"]
    # An explicit genre with no record is reported absent, not invented.
    assert prose.ok("read", "docs/x.md", "--genre", "how-to")["genre_record"] is None

    bad = prose.run("exemplar-add", "rdr", "docs/x.md:9-12")
    assert bad.returncode != 0 and "range" in bad.stderr
    assert prose.run("exemplar-add", "rdr", "docs/x.md").returncode != 0


def test_exemplar_rev_reads_the_pinned_revision_and_a_repeat_is_not_added_twice(
    prose: Prose, repo: Path
) -> None:
    rev = git(repo, "rev-parse", "HEAD")
    # The working tree has moved on: a line was inserted, so lines 2-3 are now different.
    (repo / "docs" / "x.md").write_text("zero\none\ntwo\nthree\nfour\nfive\n")
    tree = prose.ok("exemplar-add", "how-to", "docs/x.md:2-3")["exemplar"]
    assert tree["text"] == "one\ntwo" and "rev" not in tree
    pinned = prose.ok("exemplar-add", "how-to", "docs/x.md:2-3", "--rev", rev)
    assert pinned["exemplar"] == {"text": "two\nthree", "path": "docs/x.md", "start": 2, "end": 3, "rev": rev}
    assert pinned["duplicate"] is False

    again = prose.ok("exemplar-add", "how-to", "docs/x.md:2-3", "--rev", rev)
    assert again["duplicate"] is True
    assert [e["text"] for e in t2_json(REPO_PROJECT, "genre/how-to")["exemplars"]] == [
        "one\ntwo", "two\nthree"]

    for bad in (["--rev", "no-such-rev"], ["--rev=--output=/tmp/x"]):
        proc = prose.run("exemplar-add", "how-to", "docs/x.md:2-3", *bad)
        assert proc.returncode == 1 and proc.stdout == ""

    # A moving name is stored as the commit it resolved to, not as typed.
    for spec in ("HEAD", "main", "HEAD~0"):
        got = prose.ok("exemplar-add", "how-to", "docs/x.md:1-1", "--rev", spec)["exemplar"]
        assert got["rev"] == rev and len(got["rev"]) == 40, spec
    # A rev that names a tree or blob rather than a commit is refused.
    tree_id = git(repo, "rev-parse", "HEAD^{tree}")
    assert prose.run("exemplar-add", "how-to", "docs/x.md:1-1", "--rev", tree_id).returncode == 1


def test_exemplar_from_an_unreadable_file_is_a_clean_error(prose: Prose, repo: Path) -> None:
    (repo / "docs" / "bad.md").write_bytes(b"\xff\xfe not utf-8\n")
    (repo / "docs" / "nul.md").write_bytes(b"a\x00b\n")
    git(repo, "add", "docs/bad.md", "docs/nul.md")
    git(repo, "commit", "-q", "-m", "binary-ish files")
    for name in ("bad", "nul"):
        for extra in ([], ["--rev", "HEAD"]):
            proc = prose.run("exemplar-add", "how-to", f"docs/{name}.md:1-1", *extra)
            assert proc.returncode == 1 and proc.stdout == "", (name, extra)
            assert f"cannot read docs/{name}.md as UTF-8" in proc.stderr
            assert "Traceback" not in proc.stderr
    assert t2_titles(REPO_PROJECT) == []


def test_exemplar_line_numbers_are_split_on_newlines_only(prose: Prose, repo: Path) -> None:
    """str.splitlines also breaks on U+2028 and U+0085, so line 2 of this file would be 'b'."""
    (repo / "docs" / "u.md").write_text("a b\nc\nd\n", encoding="utf-8")
    assert prose.ok("exemplar-add", "how-to", "docs/u.md:1-1")["exemplar"]["text"] == "a b"
    assert prose.ok("exemplar-add", "how-to", "docs/u.md:2-2")["exemplar"]["text"] == "c"
    assert prose.ok("exemplar-add", "how-to", "docs/u.md:2-3")["exemplar"]["text"] == "c\nd"
    assert prose.run("exemplar-add", "how-to", "docs/u.md:4-4").returncode == 1  # 3 lines, not 4
    assert prose.ok("exemplar-add", "how-to", "docs/x.md:2")["exemplar"]["text"] == "two"  # PATH:N


def test_genre_put_merges_unless_told_to_replace(prose: Prose) -> None:
    ex1 = {"text": "t1", "path": "a.md", "start": 1, "end": 1}
    ex2 = {"text": "t2", "path": "a.md", "start": 2, "end": 2}
    prose.ok("genre-put", "rdr", stdin={"exemplars": [ex1], "notes": ["n1"]})
    # A notes-only put must not wipe the exemplars; repeats are not doubled.
    prose.ok("genre-put", "rdr", stdin={"notes": ["n2", "n1"]})
    prose.ok("genre-put", "rdr", stdin={"exemplars": [ex1, ex2]})
    rec = t2_json(REPO_PROJECT, "genre/rdr")
    assert rec["exemplars"] == [ex1, ex2] and rec["notes"] == ["n1", "n2"]
    prose.ok("genre-put", "rdr", "--replace", stdin={"notes": ["only"]})
    assert t2_json(REPO_PROJECT, "genre/rdr") == {"exemplars": [], "notes": ["only"]}

    for bad in ({"exemplars": [{"text": "x"}]}, {"unknown": 1}, {"notes": [1]}, [1]):
        assert prose.run("genre-put", "rdr", stdin=json.dumps(bad)).returncode == 1, bad
    assert prose.run("genre-put", "Bad Name", stdin="{}").returncode == 1


def test_viewer_preference_is_a_user_record(prose: Prose) -> None:
    assert prose.ok("viewer")["viewer"] is None
    prose.ok("viewer", "--set", "glow")
    assert prose.ok("viewer")["viewer"] == "glow"
    assert t2_titles(USER_PROJECT) == ["viewer"]


# ---------------------------------------------------------------------------
# Concurrency, encoding, how memory.py drives nx, failure modes
# ---------------------------------------------------------------------------


def _race(prose: Prose, jobs: list[tuple[Path, str]], level: str, env: dict[str, str]) -> None:
    """Start one add-entry per (cwd, value) at once and wait for all of them."""
    procs = [
        prose.popen("add-entry", "--level", level, "--from-stdin",
                    stdin={"lists": {"banned": [value]}}, cwd=cwd, env=env)
        for cwd, value in jobs
    ]
    for p in procs:
        _out, err = p.communicate(timeout=240)
        assert p.returncode == 0, err


def test_concurrent_writers_from_the_primary_and_a_worktree_lose_no_update(
    prose: Prose, repo: Path, tmp_path: Path
) -> None:
    """Each add-entry is get-modify-put on one record. Without the lock, writers that read
    the same version overwrite one another; the shim delays every put by two seconds, so
    all four have read before the first write lands and the loss is certain, not probable.
    Two run from the primary and two from a worktree, so the repo lock has to live where
    the worktrees share it (the git common dir)."""
    wt = tmp_path / "wt-writers"
    git(repo, "worktree", "add", "-q", "-b", "writers", str(wt))
    env = dict(prose.env, PROSE_EDIT_NX_DELAY_PUT="2")
    _race(prose, [(repo, "w0"), (wt, "w1"), (repo, "w2"), (wt, "w3")], "repo", env)
    banned = t2_json(REPO_PROJECT, "stylesheet")["lists"]["banned"]
    assert sorted(banned) == ["w0", "w1", "w2", "w3"]


def test_user_level_records_lock_across_repos_in_a_per_user_directory(
    prose: Prose, repo: Path, tmp_path: Path
) -> None:
    """prose/stylesheet is shared by every repo, so two repos must contend on one lock. That
    lock cannot live in either repo's git dir; it lives in a per-user temp directory."""
    other = make_repo(tmp_path / "other-repo")
    scratch = tmp_path / "scratch-tmp"
    scratch.mkdir()
    env = dict(prose.env, PROSE_EDIT_NX_DELAY_PUT="2", TMPDIR=str(scratch))
    _race(prose, [(repo, "a0"), (other, "b0"), (repo, "a1"), (other, "b1")], "user", env)
    banned = t2_json(USER_PROJECT, "stylesheet")["lists"]["banned"]
    assert sorted(banned) == ["a0", "a1", "b0", "b1"]
    locks = list((scratch / f"prose-edit-locks-{os.getuid()}").glob("*.lock"))
    assert len(locks) == 1  # one user record, one lock, shared by both repos
    # Neither repo's git dir holds a lock for it.
    assert not list((repo / ".git" / "prose-edit-locks").glob("*.lock"))
    assert not list((other / ".git" / "prose-edit-locks").glob("*.lock"))


def test_non_ascii_survives_a_non_utf8_locale(prose: Prose) -> None:
    env = {k: v for k, v in prose.env.items() if k not in ("PYTHONIOENCODING", "LANG", "LC_ALL")}
    env.update(LC_ALL="C", LANG="C", PYTHONUTF8="0", PYTHONCOERCECLOCALE="0")
    old = "It — really — works; café “quoted”."
    reject = prose.run("reject", "docs/x.md", "--from-stdin", env=env,
                       stdin=json.dumps({"old": old, "new": "It works."}, ensure_ascii=False))
    assert reject.returncode == 0, reject.stderr
    assert t2_json(REPO_PROJECT, "doc/docs/x.md")["rejections"][0]["old"] == old
    filt = prose.run("filter", "docs/x.md", env=env,
                     stdin=json.dumps(_proposal((old, "It works.")), ensure_ascii=False))
    assert filt.returncode == 0, filt.stderr
    assert json.loads(filt.stdout)["dropped"][0]["old"] == old


def test_nx_is_driven_by_stdin_ids_and_confirmed_deletes(prose: Prose) -> None:
    secret = "SECRET-CONTENT-MARKER"
    prose.ok("add-entry", "--level", "doc", "--path", "docs/x.md", "--key", "k", "--value", secret)
    prose.ok("reject", "docs/x.md", "--old", secret, "--new", "n")
    prose.ok("read", "docs/x.md")
    prose.ok("rejections", "docs/x.md", "--remove", "1")

    calls = prose.calls()
    puts = [c for c in calls if c[:2] == ["memory", "put"]]
    gets = [c for c in calls if c[:2] == ["memory", "get"]]
    assert puts and gets
    for c in puts:
        assert "-" in c, c  # content arrives on stdin
        assert not any(secret in a for a in c), c  # never in argv
    for c in gets:
        assert c[2].isdigit() and len(c) == 3, c  # by numeric id only, never by title
    assert not any(a in ("-t", "--title") for c in gets for a in c)
    assert any(c[:2] == ["memory", "list"] for c in calls)


def test_emptying_a_document_record_deletes_it_with_yes(prose: Prose) -> None:
    """Removing the last rejection from a record that holds nothing else deletes the
    record rather than storing an empty one. `nx memory delete` without -y prompts
    and hangs, so the call must carry -y (and names the row by id, not title)."""
    prose.ok("reject", "docs/x.md", "--old", "a", "--new", "b")
    assert t2_titles(REPO_PROJECT) == ["doc/docs/x.md"]
    prose.ok("rejections", "docs/x.md", "--remove", "1")
    assert t2_titles(REPO_PROJECT) == []
    deletes = [c for c in prose.calls() if c[:2] == ["memory", "delete"]]
    assert len(deletes) == 1
    assert "-y" in deletes[0] and "--id" in deletes[0]
    assert prose.ok("rejections", "docs/x.md")["rejections"] == []

    # A record that still carries a style note is kept when its last rejection goes.
    prose.ok("add-entry", "--level", "doc", "--path", "docs/x.md", "--key", "k", "--value", "v")
    prose.ok("reject", "docs/x.md", "--old", "a", "--new", "b")
    prose.ok("rejections", "docs/x.md", "--remove", "1")
    assert t2_json(REPO_PROJECT, "doc/docs/x.md")["scalars"] == {"k": "v"}


def test_t2_down_exits_3_but_other_nx_failures_exit_1_with_nxs_message(prose: Prose) -> None:
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        dead_port = s.getsockname()[1]
    env = dict(prose.env, NX_SERVICE_URL=f"http://127.0.0.1:{dead_port}")
    started = time.monotonic()
    for args in (("read", "docs/x.md"), ("add-entry", "--level", "user", "--key", "k", "--value", "v"),
                 ("log", "docs/x.md"), ("filter", "docs/x.md")):
        proc = prose.run(*args, env=env, stdin=json.dumps(_proposal("x")))
        assert proc.returncode == 3, (args, proc.returncode, proc.stderr)
        assert "T2 unavailable" in proc.stderr
        assert proc.stdout == "", "no JSON on stdout: the caller must not see an empty style sheet"
    assert time.monotonic() - started < 120

    # The service is reachable but nx refuses the write (no production-write opt-in): that is
    # not "T2 unavailable"; it exits 1 and passes nx's own message through.
    env = {k: v for k, v in prose.env.items() if k != "NX_ALLOW_PROD_WRITE"}
    assert prose.run("read", "docs/x.md", env=env).returncode == 0  # reads are not guarded
    refused = prose.run("add-entry", "--level", "user", "--key", "k", "--value", "v", env=env)
    assert refused.returncode == 1, refused.stderr
    assert "ProductionWriteGuardError" in refused.stderr and "T2 unavailable" not in refused.stderr
    assert refused.stdout == ""


@pytest.mark.parametrize(
    ("detail", "unavailable"),
    [
        ("Error: T2 storage service error: [Errno 61] Connection refused. Check the storage service", True),
        ("Error: T2 storage service unavailable: no lease", True),
        ("Error: T2 storage service error: Server error '502 Bad Gateway' for url 'http://e/x'", True),
        ("Error: T2 storage service error: Server error '503 Service Unavailable' for url 'http://e/x'", True),
        ("Error: T2 storage service error: Server error '504 Gateway Timeout' for url 'http://e/x'", True),
        ("Error: T2 storage service error: Server error '500 Internal Server Error' for url 'http://e/x'", False),
        ("Error: T2 storage service error: Client error '401 Unauthorized' for url 'http://e/x'", False),
        ("nexus.db.service_endpoint.ProductionWriteGuardError: STOP", False),
        ("Error: entry not found", False),
    ],
)
def test_which_nx_failures_count_as_t2_unavailable(detail: str, unavailable: bool) -> None:
    assert _module().nx_unavailable(detail) is unavailable


_ACC_FAKE = Path(__file__).parent / "acceptance" / "fake_nx_unavailable.py"
_DOCTOR = ". Check the storage service: nx doctor"


_UNREACHABLE = "T2 unavailable: the storage service could not be reached"


@pytest.mark.parametrize(
    ("nx_stderr", "code", "says"),
    [
        (None, 3, _UNREACHABLE),
        ("Error: T2 storage service error: [Errno 61] Connection refused" + _DOCTOR, 3, _UNREACHABLE),
        # httpx's real status error is several lines; nx appends its remedy to the last one.
        ("Error: T2 storage service error: Server error '503 Service Unavailable' for url 'http://e/x'\n"
         "For more information check: https://developer.mozilla.org/en-US/docs/Web/HTTP/Status/503" + _DOCTOR,
         3, "T2 unavailable: the storage service answered HTTP 503"),
        ("Error: T2 storage service error: Server error '500 Internal Server Error' for url 'http://e/x'\n"
         "For more information check: https://developer.mozilla.org/en-US/docs/Web/HTTP/Status/500" + _DOCTOR,
         1, "T2 refused the request: the storage service answered HTTP 500"),
        ("Error: T2 storage service error: Client error '401 Unauthorized' for url 'http://e/x'" + _DOCTOR,
         1, "T2 refused the request: the storage service answered HTTP 401"),
        ("Error: T2 storage service error: Redirect response '307 Temporary Redirect' for url 'http://e/x'" + _DOCTOR,
         1, "T2 refused the request: the storage service answered HTTP 307"),
        # nx's top-level handler (src/nexus/cli.py) when the endpoint vanishes mid-command.
        ("Error: nexus-service endpoint is not resolvable (NX_STORAGE_BACKEND=service): start the "
         "supervisor with 'nx daemon service start' (publishes the endpoint lease this client "
         "auto-discovers), or export NX_SERVICE_PORT / NX_SERVICE_TOKEN (and optionally "
         "NX_SERVICE_HOST) explicitly.", 3, _UNREACHABLE),
        ("Error: a service this command needs did not answer (<urlopen error [Errno 61] Connection "
         "refused>). If it is the local nexus service, start it with 'nx daemon service start'; "
         "'nx doctor' checks every endpoint.", 3, _UNREACHABLE),
        ("", 3, "T2 unavailable: the storage client could not be started (No such file or directory)"),
    ],
    ids=["endpoint-unresolvable", "connection-refused", "edge-503", "engine-500", "client-401",
         "redirect-307", "cli-endpoint-unresolvable", "cli-did-not-answer", "nx-missing"],
)
def test_a_t2_failure_is_reported_in_the_scripts_words_not_nxs_remedy(
    prose: Prose, tmp_path: Path, nx_stderr: str | None, code: int, says: str,
) -> None:
    # nx ends these failures with a remedy for an operator ('nx daemon service start', 'nx doctor');
    # a model that reads it runs it (nexus-ger02.15). None = the acceptance fake, real nx wording.
    if nx_stderr is None:
        nx = f"{sys.executable} {_ACC_FAKE}"
    elif not nx_stderr:
        nx = str(tmp_path / "no-such-nx")
    else:
        fake = tmp_path / "fakenx.py"
        fake.write_text(f"import sys\nsys.stderr.write({nx_stderr + chr(10)!r})\nsys.exit(1)\n")
        nx = f"{sys.executable} {fake}"
    env = dict(prose.env, PROSE_EDIT_NX=nx)
    proc = prose.run("read", "docs/x.md", env=env)
    assert proc.returncode == code, proc.stderr
    assert proc.stderr == f"memory.py: {says}\n"
    assert not NAMES_A_REPAIR.search(proc.stderr)
    assert proc.stdout == ""


@pytest.mark.parametrize(
    ("nx_stderr", "says"),
    [
        ("Error: this install predates the current storage layout.\n"
         "Run 'nx upgrade', then 'nx daemon service stop' and 'nx doctor'.",
         "T2 request failed (exit 1): Error: this install predates the current storage layout."),
        ("Error: run nx doctor", "T2 request failed (exit 1)"),
        ("Usage: NX memory get\nError: no such option: --bogus", "T2 request failed (exit 1): Error: no such option: --bogus"),
        ("Error: the engine is older than this client. Upgrade it, or restart the\n"
         "supervisor: daemon service restart", "T2 request failed (exit 1): Error: the engine is older than "
         "this client. Upgrade it, or restart the"),
    ],
    ids=["remedy-line-dropped", "only-a-remedy", "case-blind", "wrapped-remedy"],
)
def test_any_other_nx_failure_keeps_its_cause_and_drops_the_lines_that_name_nx(
    prose: Prose, tmp_path: Path, nx_stderr: str, says: str,
) -> None:
    fake = tmp_path / "fakenx.py"
    fake.write_text(f"import sys\nsys.stderr.write({nx_stderr + chr(10)!r})\nsys.exit(1)\n")
    proc = prose.run("read", "docs/x.md", env=dict(prose.env, PROSE_EDIT_NX=f"{sys.executable} {fake}"))
    assert proc.returncode == 1, proc.stderr
    assert proc.stderr == f"memory.py: {says}\n"
    assert not NAMES_A_REPAIR.search(proc.stderr)


def test_prefix_flag_overrides_the_environment(prose: Prose) -> None:
    prose.ok("--prefix", "zz_", "add-entry", "--level", "user", "--key", "k", "--value", "v")
    assert t2_titles("zz_prose") == ["stylesheet"]
    assert t2_titles(USER_PROJECT) == []
