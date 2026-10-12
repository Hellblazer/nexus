# SPDX-License-Identifier: AGPL-3.0-or-later
"""Pins for the benchmark corpus (nexus-2rs76) that CI can see without an engine.

``tests/e2e/benchmark-corpus.sh`` indexes a pinned snapshot and a 30-note
fixture on a real engine. This file drives the deterministic code that script
calls (``benchmark_corpus.py``) and the script's wiring:

* the pins are well formed, and (integration, needs full git history) they
  match the tag and the tree they claim;
* the snapshot filter excludes tests/ and the RDR archive, and a materialised
  snapshot is the filtered tree committed under a reproducible sha;
* the knowledge fixture is 30 notes in 2 subjects, byte-stable;
* each verdict function fails on the corpus gone wrong (nothing indexed, a
  skipped step, a missing note, a leftover process);
* the script calls every one of them, fences itself to its own sandbox and
  refuses to delete a directory that is not one.
"""
from __future__ import annotations

import hashlib
import json
import os
import re
import subprocess
import sys
from pathlib import Path

import pytest

import benchmark_corpus as bc

E2E = Path(__file__).resolve().parent.parent
REPO = E2E.parent.parent
SCRIPT = E2E / "benchmark-corpus.sh"

#: sha256 of the notes manifest. A deliberate edit to a note moves it.
NOTES_DIGEST = "52d7fd86056ffa5feb8a036c21b282d3c3982ac3444d5249516a364d845012bb"


def _text() -> str:
    return SCRIPT.read_text(encoding="utf-8")


def _norm(text: str) -> str:
    return re.sub(r"\s+", " ", text).strip().lower()


# ── pins ────────────────────────────────────────────────────────────────────


def test_pins_are_well_formed() -> None:
    assert re.fullmatch(r"[0-9a-f]{40}", bc.PIN_SHA), "PIN_SHA must be a full 40-hex commit id"
    assert re.fullmatch(r"v\d+\.\d+\.\d+", bc.PIN_TAG)
    assert re.fullmatch(r"\d+\.\d+\.\d+", bc.CONEXUS_VERSION)
    # The client under test and the corpus come from the same release.
    assert bc.PIN_TAG == f"v{bc.CONEXUS_VERSION}"
    assert bc.SNAPSHOT_FILE_COUNT > 0 and re.fullmatch(r"[0-9a-f]{64}", bc.SNAPSHOT_DIGEST)


def test_ranges_are_ordered_and_consistent() -> None:
    for lo, hi in (bc.CHUNK_RANGE_TOTAL, bc.CHUNK_RANGE_CODE, bc.CHUNK_RANGE_DOCS):
        assert 0 < lo < hi
    # The total range can hold the sum of the per-collection ranges' midpoints.
    mid = lambda r: (r[0] + r[1]) // 2  # noqa: E731
    assert bc.CHUNK_RANGE_TOTAL[0] <= mid(bc.CHUNK_RANGE_CODE) + mid(bc.CHUNK_RANGE_DOCS) <= bc.CHUNK_RANGE_TOTAL[1]
    # The numbers the ranges were pinned around (chas, 2026-10-12, see CHUNK_RANGE_NOTE).
    for rng, measured in ((bc.CHUNK_RANGE_CODE, 9_263), (bc.CHUNK_RANGE_DOCS, 1_620), (bc.CHUNK_RANGE_TOTAL, 10_883)):
        assert rng[0] <= measured <= rng[1]
    assert "10,883" in bc.CHUNK_RANGE_NOTE
    # Sam's target: about 8,000 to 12,000 chunks.
    assert 8_000 <= bc.CHUNK_RANGE_TOTAL[0] and bc.CHUNK_RANGE_TOTAL[1] <= 12_500


def test_the_script_takes_its_pins_from_the_helper_only() -> None:
    text = _text()
    assert bc.PIN_SHA not in text, "the sha lives in benchmark_corpus.py alone"
    for key in ("PIN_TAG", "PIN_SHA", "CONEXUS_VERSION"):
        assert f'corpus_py pin {key}' in text


@pytest.mark.integration
def test_pin_sha_is_the_commit_of_the_tag_it_claims() -> None:
    """Needs the tag, so it is integration: a shallow checkout fails it loudly."""
    tag = subprocess.run(
        ["git", "-C", str(REPO), "rev-list", "-n1", bc.PIN_TAG],
        capture_output=True, text=True,
    )
    assert tag.returncode == 0, (
        f"tag {bc.PIN_TAG} is not in this checkout (shallow clone?): run `git fetch --tags`; {tag.stderr.strip()}"
    )
    assert tag.stdout.strip() == bc.PIN_SHA


@pytest.mark.integration
def test_snapshot_at_the_pin_matches_the_pinned_count_and_digest() -> None:
    entries = bc.snapshot_entries(REPO, bc.PIN_SHA)
    assert len(entries) == bc.SNAPSHOT_FILE_COUNT
    assert bc.snapshot_digest(entries) == bc.SNAPSHOT_DIGEST
    paths = [e.path for e in entries]
    assert not [p for p in paths if p.startswith(("tests/", "docs/rdr/"))]


@pytest.mark.integration
@pytest.mark.parametrize(
    "probe,path",
    [("repo-code", "src/nexus/engine_version.py"), ("repo-docs", "docs/collections.md")],
)
def test_repo_probe_phrases_are_in_the_pinned_snapshot(probe: str, path: str) -> None:
    shown = subprocess.run(
        ["git", "-C", str(REPO), "show", f"{bc.PIN_SHA}:{path}"],
        capture_output=True, text=True, check=True,
    ).stdout
    p = next(x for x in bc.PROBES if x.name == probe)
    assert _norm(p.must_contain) in _norm(shown)
    assert bc.in_snapshot(path)


# ── snapshot filter and materialiser ────────────────────────────────────────


@pytest.mark.parametrize(
    "path",
    ["src/nexus/cli.py", "src/nexus/_resources/plans/a.yml", "docs/architecture.md", "docs/collections.md",
     "docs/exploration/x.md"],
)
def test_filter_keeps_source_and_docs(path: str) -> None:
    assert bc.in_snapshot(path)


@pytest.mark.parametrize(
    "path",
    ["tests/test_cli.py", "tests/e2e/benchmark-corpus.sh", "docs/rdr/rdr-001-x.md", "docs/rdr/AGENTS.md",
     "docs/rdr/sub/deep.md", "service/pom.xml", "scripts/x.sh", "conexus/skills/x/SKILL.md", "README.md",
     "src/nexus/__pycache__/a.cpython-312.pyc", "docs/architecture-diagram.png", "docs/tuple-row-life.svg",
     "src/other/cli.py", "docsx/a.md",
     # the trim that took the snapshot from 15,623 chunks to the target range
     "src/nexus/commands/index.py", "src/nexus/plans/builtin/a.yml", "src/nexus/_install/x.sh",
     "src/nexus/hooks/h.py", "src/nexus/console/app.js", "docs/cli-reference.md",
     "docs/wire-contract-pending.md", "docs/postmortem/2026-06-06-idle-peg.txt"],
)
def test_filter_leaves_out_tests_rdr_archive_and_binaries(path: str) -> None:
    assert not bc.in_snapshot(path)


def _git_env() -> dict[str, str]:
    return {
        "PATH": os.environ.get("PATH", "/usr/bin:/bin"), "HOME": "/nonexistent",
        "GIT_CONFIG_GLOBAL": os.devnull, "GIT_CONFIG_NOSYSTEM": "1",
        "GIT_AUTHOR_NAME": "t", "GIT_AUTHOR_EMAIL": "t@example.invalid",
        "GIT_COMMITTER_NAME": "t", "GIT_COMMITTER_EMAIL": "t@example.invalid",
    }


@pytest.fixture()
def source_repo(tmp_path: Path) -> tuple[Path, str]:
    """A small repo with kept files and every kind of excluded one."""
    repo = tmp_path / "src-repo"
    files = {
        "src/nexus/a.py": "print('a')\n",
        "src/nexus/sub/b.md": "# b\n",
        "docs/guide.md": "guide\n",
        "docs/rdr/rdr-001.md": "an rdr\n",
        "docs/pic.png": "not really a png",
        "tests/test_a.py": "def test(): pass\n",
        "service/Main.java": "class Main {}\n",
        "README.md": "readme\n",
    }
    for rel, body in files.items():
        p = repo / rel
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_text(body)
    env = _git_env()
    for args in (["init", "-q", "-b", "main"], ["add", "-A"], ["commit", "-q", "-m", "x"]):
        subprocess.run(["git", "-C", str(repo), *args], env=env, check=True, capture_output=True)
    sha = subprocess.run(
        ["git", "-C", str(repo), "rev-parse", "HEAD"], env=env, check=True, capture_output=True, text=True,
    ).stdout.strip()
    return repo, sha


@pytest.fixture()
def pinned_to_source_repo(source_repo: tuple[Path, str], monkeypatch: pytest.MonkeyPatch) -> tuple[Path, str]:
    repo, sha = source_repo
    entries = bc.snapshot_entries(repo, sha)
    monkeypatch.setattr(bc, "SNAPSHOT_FILE_COUNT", len(entries))
    monkeypatch.setattr(bc, "SNAPSHOT_DIGEST", bc.snapshot_digest(entries))
    return repo, sha


def test_snapshot_entries_are_the_filtered_tree(source_repo: tuple[Path, str]) -> None:
    repo, sha = source_repo
    assert [e.path for e in bc.snapshot_entries(repo, sha)] == [
        "docs/guide.md", "src/nexus/a.py", "src/nexus/sub/b.md",
    ]


def test_materialised_snapshot_is_only_the_filtered_tree_and_is_committed(
    pinned_to_source_repo: tuple[Path, str], tmp_path: Path,
) -> None:
    repo, sha = pinned_to_source_repo
    dest = tmp_path / "snap"
    info = bc.materialize(repo, dest, sha)
    on_disk = sorted(
        p.relative_to(dest).as_posix() for p in dest.rglob("*") if p.is_file() and ".git" not in p.parts
    )
    assert on_disk == ["docs/guide.md", "src/nexus/a.py", "src/nexus/sub/b.md"]
    tracked = subprocess.run(
        ["git", "-C", str(dest), "ls-files"], env=_git_env(), capture_output=True, text=True, check=True,
    ).stdout.split()
    assert sorted(tracked) == on_disk
    count = subprocess.run(
        ["git", "-C", str(dest), "rev-list", "--count", "HEAD"], env=_git_env(), capture_output=True, text=True, check=True,
    ).stdout.strip()
    assert count == "1" and info["files"] == 3


def test_materialised_snapshot_commit_is_reproducible(
    pinned_to_source_repo: tuple[Path, str], tmp_path: Path,
) -> None:
    repo, sha = pinned_to_source_repo
    first = bc.materialize(repo, tmp_path / "one", sha)
    second = bc.materialize(repo, tmp_path / "two", sha)
    assert first["head"] == second["head"]


def test_materialise_refuses_a_snapshot_that_is_not_the_pinned_one(
    source_repo: tuple[Path, str], tmp_path: Path,
) -> None:
    repo, sha = source_repo  # the module's real pins do not describe this tree
    with pytest.raises(SystemExit, match="pinned"):
        bc.materialize(repo, tmp_path / "snap", sha)
    assert not (tmp_path / "snap" / "src").exists()


def test_materialise_refuses_a_non_empty_destination(
    pinned_to_source_repo: tuple[Path, str], tmp_path: Path,
) -> None:
    repo, sha = pinned_to_source_repo
    dest = tmp_path / "busy"
    dest.mkdir()
    (dest / "x").write_text("x")
    with pytest.raises(SystemExit, match="not empty"):
        bc.materialize(repo, dest, sha)


# ── knowledge fixture ───────────────────────────────────────────────────────


def test_fixture_is_30_notes_in_2_subjects() -> None:
    assert len(bc.NOTES) == 30
    by_subject: dict[str, int] = {}
    for n in bc.NOTES:
        by_subject[n.subject] = by_subject.get(n.subject, 0) + 1
    assert by_subject == {"distributed-systems": 15, "vector-search": 15}
    assert set(by_subject) == set(bc.SUBJECTS)


def test_fixture_subjects_are_durable_not_placeholders() -> None:
    placeholders = {"default", "knowledge", "notes", "tmp", "test"}
    for s in bc.SUBJECTS:
        assert s not in placeholders
        assert re.fullmatch(r"[a-z]+(-[a-z]+){1,2}", s), "docs/collections.md: lowercase, hyphenated, 2-3 words"


def test_each_note_is_one_short_unique_chunk_with_a_listable_title() -> None:
    titles = [n.title for n in bc.NOTES]
    assert len(set(titles)) == 30
    assert len({n.body for n in bc.NOTES}) == 30
    for n in bc.NOTES:
        assert len(n.title) <= 40, "the store listing truncates titles at 40"
        assert 100 <= len(n.body) <= 600, f"{n.title} would not stay one chunk"


def test_fixture_is_deterministic_and_byte_stable(tmp_path: Path) -> None:
    first = bc.write_notes(tmp_path / "a")
    second = bc.write_notes(tmp_path / "b")
    assert [(s, t) for s, t, _ in first] == [(s, t) for s, t, _ in second]
    for (_, _, p1), (_, _, p2) in zip(first, second, strict=True):
        assert p1.read_bytes() == p2.read_bytes()
    digest = hashlib.sha256(json.dumps(bc.notes_manifest(), sort_keys=True).encode()).hexdigest()
    assert digest == NOTES_DIGEST, "a note changed; update NOTES_DIGEST in the same change"


def test_probes_are_drawn_from_the_pins_and_the_notes() -> None:
    by_name = {p.name: p for p in bc.PROBES}
    assert set(by_name) == {"repo-code", "repo-docs", "note-distributed", "note-vector"}
    assert by_name["repo-code"].collection_prefix == "code__"
    assert by_name["repo-docs"].collection_prefix == "docs__"
    for key, subject in (("note-distributed", "distributed-systems"), ("note-vector", "vector-search")):
        p = by_name[key]
        owners = [n for n in bc.NOTES if n.subject == subject and _norm(p.must_contain) in _norm(n.body)]
        assert len(owners) == 1, f"{key}'s phrase must appear in exactly one {subject} note"
        assert _norm(p.query) in _norm(owners[0].body)
        assert p.max_rank <= 3


# ── verdicts ────────────────────────────────────────────────────────────────


def _good_counts() -> dict[str, int]:
    mid = lambda r: (r[0] + r[1]) // 2  # noqa: E731
    return {
        "code__nexus-snapshot-1a2b3c4d__bge-768__v1": mid(bc.CHUNK_RANGE_CODE),
        "docs__nexus-snapshot-1a2b3c4d__bge-768__v1": mid(bc.CHUNK_RANGE_DOCS),
        "knowledge__distributed-systems__bge-768__v1": 15,
        "knowledge__vector-search__bge-768__v1": 15,
    }


def test_the_pinned_corpus_passes_its_own_count_check() -> None:
    assert bc.check_counts(_good_counts()) == []


@pytest.mark.parametrize("mutation", [
    "nothing_indexed", "repo_too_small", "repo_too_big", "no_docs_collection", "extra_collection",
    "note_missing", "extra_note", "missing_subject", "third_subject", "rdr_collection_present",
])
def test_the_count_check_fails_on_each_way_the_corpus_goes_wrong(mutation: str) -> None:
    c = _good_counts()
    code = next(n for n in c if n.startswith("code__"))
    docs = next(n for n in c if n.startswith("docs__"))
    ds = next(n for n in c if "distributed" in n)
    vs = next(n for n in c if "vector" in n)
    if mutation == "nothing_indexed":
        c[code] = c[docs] = 0
    elif mutation == "repo_too_small":
        c[code] = bc.CHUNK_RANGE_CODE[0] - 1
    elif mutation == "repo_too_big":
        c[docs] = bc.CHUNK_RANGE_DOCS[1] + 1
    elif mutation == "no_docs_collection":
        del c[docs]
    elif mutation == "extra_collection":
        c["quarantine-" + code] = 3
    elif mutation == "note_missing":
        c[ds] = 14
    elif mutation == "extra_note":
        c[vs] = 16
    elif mutation == "missing_subject":
        del c[vs]
    elif mutation == "third_subject":
        c["knowledge__consensus__bge-768__v1"] = 15
    elif mutation == "rdr_collection_present":
        c["rdr__nexus-snapshot-1a2b3c4d__bge-768__v1"] = 40
    assert bc.check_counts(c), f"{mutation} passed the count check"


def test_parse_collection_list_reads_the_cli_table() -> None:
    table = (
        "NAME                                          CHUNKS  STORED  TYPE       OWNER\n"
        "code__nexus-snapshot-1a2b3c4d__bge-768__v1      6000    6000  code       1-1\n"
        "knowledge__vector-search__bge-768__v1             15      15  knowledge  vector-search\n"
    )
    assert bc.parse_collection_list(table) == {
        "code__nexus-snapshot-1a2b3c4d__bge-768__v1": 6000,
        "knowledge__vector-search__bge-768__v1": 15,
    }
    assert bc.parse_collection_list("No collections found.\n") == {}


def test_the_empty_listing_fails_the_count_check_and_the_command() -> None:
    assert bc.check_counts(bc.parse_collection_list("No collections found.\n"))
    helper = Path(bc.__file__)
    run = subprocess.run(
        [sys.executable, str(helper), "check-counts", "10"],
        input="No collections found.\n", capture_output=True, text=True,
    )
    assert run.returncode == 1 and "FAIL" in run.stderr
    zero_time = subprocess.run(
        [sys.executable, str(helper), "check-counts", "0"],
        input="No collections found.\n", capture_output=True, text=True,
    )
    assert "wall time" in zero_time.stderr


def _hit(coll: str, content: str, title: str = "") -> dict[str, str]:
    return {"collection": coll, "content": content, "title": title}


def test_probe_verdict_accepts_the_right_hit_and_rejects_the_rest() -> None:
    probe = next(p for p in bc.PROBES if p.name == "repo-code")
    good = json.dumps([_hit("code__x__m__v1", "..  Never a  user-facing\nrefusal. ..")])
    assert bc.probe_verdict(probe, good) is None
    assert bc.probe_rank(probe, good) == 1
    wrong_collection = json.dumps([_hit("docs__x__m__v1", "never a user-facing refusal")])
    assert bc.probe_verdict(probe, wrong_collection)
    wrong_text = json.dumps([_hit("code__x__m__v1", "something else entirely")])
    assert bc.probe_verdict(probe, wrong_text)
    assert bc.probe_verdict(probe, "[]")
    assert bc.probe_verdict(probe, "not json")
    too_deep = json.dumps([_hit("code__x__m__v1", "filler")] * probe.max_rank + [_hit("code__x__m__v1", probe.must_contain)])
    assert bc.probe_verdict(probe, too_deep)


def test_note_probe_needs_the_note_in_the_first_three_hits() -> None:
    probe = next(p for p in bc.PROBES if p.name == "note-vector")
    note = next(n for n in bc.NOTES if n.title == "vs-11-scalar-quantization")
    hit = _hit("knowledge__vector-search__bge-768__v1", note.body, note.title)
    filler = _hit("knowledge__vector-search__bge-768__v1", "an unrelated note")
    assert bc.probe_verdict(probe, json.dumps([filler, filler, hit])) is None
    assert bc.probe_verdict(probe, json.dumps([filler, filler, filler, hit]))


def test_listing_check_needs_every_note() -> None:
    notes = [n for n in bc.NOTES if n.subject == "vector-search"]
    rows = "".join(f"  {hashlib.sha256(n.title.encode()).hexdigest()}  {n.title[:40]:<40}  permanent  2026-10-12\n" for n in notes)
    listing = "knowledge__vector-search__bge-768__v1  (showing 1-15; 15 stored)\n\n" + rows
    assert bc.listing_problems("vector-search", listing) == []
    short = "\n".join(listing.splitlines()[:-1]) + "\n"
    assert bc.listing_problems("vector-search", short)
    assert bc.listing_problems("vector-search", "No entries in x at offset 0.\n")


def test_leftover_check_matches_the_path_as_a_fixed_string_and_skips_its_own_tooling() -> None:
    sandbox = "/tmp/nxbc-a.b+c"
    ps = "\n".join([
        "  101 postgres -D /tmp/nxbc-a.b+c/home/.local/share/pg",
        "  102 /usr/bin/other /tmp/nxbc-aXb+c/home",
        "  103 bash tests/e2e/benchmark-corpus.sh down /tmp/nxbc-a.b+c",
        "  104 python benchmark_corpus.py leftover /tmp/nxbc-a.b+c",
        "  105 /bin/sleep 1",
    ])
    assert bc.leftover_processes(ps, sandbox) == ["101 postgres -D /tmp/nxbc-a.b+c/home/.local/share/pg"]
    assert bc.leftover_processes("  105 /bin/sleep 1", sandbox) == []
    with pytest.raises(ValueError):
        bc.leftover_processes(ps, "")


# ── the script ──────────────────────────────────────────────────────────────


def test_script_is_executable_and_parses() -> None:
    assert os.access(SCRIPT, os.X_OK)
    assert subprocess.run(["bash", "-n", str(SCRIPT)], capture_output=True).returncode == 0
    assert _text().splitlines()[0] == "#!/usr/bin/env bash"
    assert "set -euo pipefail" in _text()


def test_script_calls_every_assertion_the_helper_provides() -> None:
    text = _text()
    for call in ("corpus_py snapshot", "corpus_py notes", "corpus_py probes", "corpus_py check-counts",
                 "corpus_py check-listing", "corpus_py check-search", "corpus_py leftover"):
        assert call in text, f"the script no longer runs `{call}`"
    # Each assertion is followed by a loud failure, not a bare call.
    assert re.search(r'check-counts[^\n]*\n?[^\n]*\|\| _fail', text)
    assert re.search(r'check-listing[^\n]*\n?[^\n]*\|\| _fail', text)
    assert re.search(r'check-search[^\n]*\n?[^\n]*\|\| _fail', text)
    assert "stored $note_count notes" in text and '[ "$note_count" = 30 ]' in text
    assert '"$probe_count" = "$probe_expected"' in text
    assert "the leftover check would pass vacuously" in text


def test_script_runs_everything_in_an_env_i_sandbox() -> None:
    text = _text()
    assert text.count("env -i") >= 3  # nx, uv, follow-on commands
    assert "NX_NO_TELEMETRY=1" in text and "NX_LOCAL=1" in text
    assert "VOYAGE_API_KEY" in text  # named in the comment that explains the scrub
    assert "init -y --no-autostart" in text


def _run(*args: str, cwd: Path | None = None) -> subprocess.CompletedProcess:
    env = {"PATH": os.environ.get("PATH", "/usr/bin:/bin"), "HOME": os.environ.get("HOME", "/nonexistent")}
    return subprocess.run(["bash", str(SCRIPT), *args], capture_output=True, text=True, env=env, cwd=cwd, timeout=60)


def test_help_and_unknown_command() -> None:
    assert "Usage:" in _run("-h").stdout
    bad = _run("bogus")
    assert bad.returncode == 2 and "unknown command" in bad.stderr


def _fake_sandbox(parent: Path, name: str, *, marker: bool) -> Path:
    """A directory shaped like a sandbox in every respect but the ones a test removes.

    ``bin/nx`` is a stub that exits 0, so a ``down`` that gets past its guards
    reaches ``rm -rf`` and the test sees the loss, rather than dying earlier on
    a missing binary and passing for the wrong reason.
    """
    box = parent / name
    (box / "bin").mkdir(parents=True)
    nx = box / "bin" / "nx"
    nx.write_text("#!/bin/sh\nexit 0\n")
    nx.chmod(0o755)
    (box / "state.txt").write_text(f"BIN_DIR={box / 'bin'}\nPROBE_PYTHON=/nonexistent\nCONEXUS_INSTALLED=0\n")
    if marker:
        (box / ".nx-benchmark-corpus-sandbox").write_text("")
    (box / "precious.txt").write_text("do not delete")
    return box


def test_down_removes_a_well_formed_sandbox(tmp_path: Path) -> None:
    """Positive control: the refusals below mean something only if this deletes."""
    box = _fake_sandbox(tmp_path, "nxbc-good", marker=True)
    result = _run("down", str(box))
    assert result.returncode == 0, result.stderr
    assert "BENCHMARK CORPUS SANDBOX REMOVED" in result.stdout
    assert not box.exists()


@pytest.mark.parametrize("name,marker", [
    ("nxbc-no-marker", False),     # right name, no marker file
    ("important-data", True),      # marker file, wrong name
])
def test_down_refuses_a_directory_that_is_not_a_sandbox_and_deletes_nothing(
    tmp_path: Path, name: str, marker: bool,
) -> None:
    box = _fake_sandbox(tmp_path, name, marker=marker)
    result = _run("down", str(box))
    assert result.returncode != 0
    assert (box / "precious.txt").read_text() == "do not delete"
    assert "BENCHMARK CORPUS SANDBOX REMOVED" not in result.stdout


def test_down_refuses_a_sandbox_whose_install_never_finished(tmp_path: Path) -> None:
    box = _fake_sandbox(tmp_path, "nxbc-halfmade", marker=True)
    (box / "state.txt").unlink()
    result = _run("down", str(box))
    assert result.returncode != 0 and "no state.txt" in result.stderr
    assert (box / "precious.txt").exists()


def test_down_and_env_need_a_sandbox_argument() -> None:
    for cmd in ("down", "env", "exec"):
        assert _run(cmd).returncode != 0
