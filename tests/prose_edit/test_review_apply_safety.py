# SPDX-License-Identifier: AGPL-3.0-or-later
"""The write path of `review.py apply` (RDR-221 nexus-ger02.4, fix round 1).

The order of effects: stage and fsync the new text, check the file is the one we planned
against, store the rejections, check again right before the rename, rename, and only then log
the session as applied. In-process tests drive `stage_write` / `Staged.commit` with injected
faults; the end-to-end tests run the real script against the real engine substrate.
"""
from __future__ import annotations

import errno
import json
import os
import signal
import stat
import subprocess
import sys
import tempfile
import time
from pathlib import Path

import pytest

from tests.prose_edit.conftest import REPO_PROJECT, SPY, Prose, git, t2_get, t2_json, t2_titles
from tests.prose_edit.test_brief import edit
from tests.prose_edit.test_review import DOC, E1, E2, _review, review_ok, run_review, start, status

needs_perms = pytest.mark.skipif(os.geteuid() == 0, reason="root ignores file permissions")


def tracked(repo: Path, text: str = DOC, rel: str = "docs/s.md") -> Path:
    """The document, committed, so `git status` shows exactly what a write changes."""
    f = repo / rel
    f.parent.mkdir(parents=True, exist_ok=True)
    f.write_text(text, encoding="utf-8")
    git(repo, "add", rel)
    git(repo, "commit", "-q", "-m", f"add {rel}")
    return f


def stage_dir(repo: Path) -> Path:
    return Path(git(repo, "rev-parse", "--absolute-git-dir")) / "prose-edit-tmp"


def staged_files(repo: Path) -> list[str]:
    d = stage_dir(repo)
    return sorted(p.name for p in d.iterdir()) if d.is_dir() else []


def hook(tmp_path: Path, body: str) -> dict[str, str]:
    """PROSE_EDIT_BEFORE_REPLACE for a script whose first argument is the target path."""
    script = tmp_path / "before_replace.py"
    script.write_text("import os, signal, sys\ntarget = sys.argv[1]\n" + body + "\n")
    return {"PROSE_EDIT_BEFORE_REPLACE": f"{sys.executable} {script}"}


# ---------------------------------------------------------------------------
# stage_write / Staged.commit, in process
# ---------------------------------------------------------------------------


def test_a_save_in_the_window_before_the_replace_aborts_with_a_user_error_and_writes_nothing(repo: Path) -> None:
    mod = _review()
    f = tracked(repo)
    planned = f.read_bytes()
    staged = mod.stage_write(f, repo, b"the edited text\n")

    def author_saves(path: Path) -> None:
        path.write_text(DOC + "typed by the author\n", encoding="utf-8")

    with pytest.raises(mod.UserError, match="changed") as caught:
        staged.commit(planned, before_replace=author_saves)
    assert "run apply again" in str(caught.value)
    assert f.read_text(encoding="utf-8") == DOC + "typed by the author\n"
    assert staged_files(repo) == [] and status(repo) == "M docs/s.md"


def test_an_unchanged_file_is_replaced_keeping_its_mode_and_leaves_no_temp_file(repo: Path) -> None:
    mod = _review()
    f = tracked(repo)
    f.chmod(0o640)
    staged = mod.stage_write(f, repo, b"the edited text\n")
    staged.commit(f.read_bytes())
    assert f.read_bytes() == b"the edited text\n" and stat.S_IMODE(f.stat().st_mode) == 0o640
    assert staged_files(repo) == [] and status(repo) == "M docs/s.md"


def test_a_full_disk_while_staging_is_a_user_error_with_nothing_left_behind(repo: Path) -> None:
    mod = _review()
    f = tracked(repo)
    before = f.read_bytes()

    def full(fd: int) -> None:
        raise OSError(errno.ENOSPC, "No space left on device")

    with pytest.raises(mod.UserError, match="No space left on device") as caught:
        mod.stage_write(f, repo, b"x" * 100, fsync=full)
    assert "Nothing was changed" in str(caught.value) and "run apply again" in str(caught.value)
    assert f.read_bytes() == before and staged_files(repo) == [] and status(repo) == ""


def test_a_failing_rename_is_a_user_error_and_removes_the_staged_file(repo: Path) -> None:
    mod = _review()
    f = tracked(repo)
    staged = mod.stage_write(f, repo, b"edited\n")

    def refuse(src: str, dst: str) -> None:
        raise OSError(errno.EACCES, "Permission denied")

    with pytest.raises(mod.UserError, match="Permission denied"):
        staged.commit(f.read_bytes(), replace=refuse)
    assert f.read_text(encoding="utf-8") == DOC and staged_files(repo) == [] and status(repo) == ""


@needs_perms
def test_a_read_only_file_and_a_read_only_directory_are_refused_before_anything_is_staged(repo: Path) -> None:
    mod = _review()
    f = tracked(repo)
    f.chmod(0o444)
    try:
        with pytest.raises(mod.UserError, match="not writable"):
            mod.stage_write(f, repo, b"x")
    finally:
        f.chmod(0o644)
    f.parent.chmod(0o555)
    try:
        with pytest.raises(mod.UserError, match="directory .* not writable"):
            mod.stage_write(f, repo, b"x")
    finally:
        f.parent.chmod(0o755)
    assert staged_files(repo) == []


def test_a_document_that_resolves_outside_the_repository_is_refused_and_one_that_stays_inside_is_not(
    repo: Path, tmp_path: Path
) -> None:
    mod = _review()
    outside = tmp_path / "outside.md"
    outside.write_text(DOC, encoding="utf-8")
    (repo / "docs" / "escape.md").symlink_to(outside)
    (repo / "docs" / "elsewhere").symlink_to(tmp_path, target_is_directory=True)
    for rel in ("docs/escape.md", "docs/elsewhere/outside.md"):
        with pytest.raises(mod.UserError, match="outside the repository"):
            mod.stage_write(repo / rel, repo, b"x")
    assert outside.read_text(encoding="utf-8") == DOC and staged_files(repo) == []
    real = tracked(repo, rel="docs/real.md")
    (repo / "docs" / "inside.md").symlink_to("real.md")
    mod.stage_write(repo / "docs" / "inside.md", repo, b"x").commit(real.read_bytes())
    assert real.read_bytes() == b"x" and (repo / "docs" / "inside.md").is_symlink()


def test_the_temp_file_goes_in_the_git_directory_not_beside_the_document(repo: Path) -> None:
    mod = _review()
    f = tracked(repo)
    staged = mod.stage_write(f, repo, b"edited\n")
    try:
        assert staged.tmp.parent == stage_dir(repo)
        assert status(repo) == ""  # an untracked temp file beside the document would show here
    finally:
        staged.discard()
    assert staged_files(repo) == []


def test_when_the_git_directory_is_on_another_device_the_temp_file_sits_beside_the_document(
    repo: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    mod = _review()
    f = tracked(repo)
    monkeypatch.setattr(mod, "_same_device", lambda a, b: False)
    staged = mod.stage_write(f, repo, b"edited\n")
    try:
        assert staged.tmp.parent == f.parent
    finally:
        staged.discard()
    assert not any(p.name.endswith(".prose-edit") for p in f.parent.iterdir())


def test_a_stale_temp_file_in_the_git_directory_is_swept_and_a_young_one_is_kept(repo: Path) -> None:
    mod = _review()
    f = tracked(repo)
    d = stage_dir(repo)
    d.mkdir()
    old, young = d / ".old.abc.prose-edit", d / ".young.abc.prose-edit"
    for p in (old, young):
        p.write_text("x")
    os.utime(old, (time.time() - 3 * 3600,) * 2)
    mod.stage_write(f, repo, b"edited\n").discard()
    assert not old.exists() and young.exists()


# ---------------------------------------------------------------------------
# Order of effects, end to end
# ---------------------------------------------------------------------------


def test_a_real_apply_changes_only_the_document_and_leaves_no_stray_file(prose: Prose, repo: Path) -> None:
    f = tracked(repo)
    f.chmod(0o755)
    git(repo, "update-index", "--chmod=+x", "docs/s.md")
    git(repo, "commit", "-q", "-m", "exec")
    work, _ = start(prose, repo, [E1, E2])
    out = review_ok(prose, "apply", "--work", str(work), "--accept", "1")
    assert [a["n"] for a in out["applied"]] == [1]
    assert status(repo) == "M docs/s.md"  # a stray temp file would add an untracked line
    assert staged_files(repo) == [] and stat.S_IMODE(f.stat().st_mode) == 0o755
    assert f.read_text(encoding="utf-8") == DOC.replace(E1["old"], E1["new"])


@needs_perms
def test_a_read_only_directory_stops_the_apply_before_t2_and_the_log_never_claims_applied(
    prose: Prose, repo: Path
) -> None:
    f = tracked(repo)
    work, _ = start(prose, repo, [E1, E2])
    f.parent.chmod(0o555)
    try:
        proc = run_review(prose, "apply", "--work", str(work), "--accept", "1")
    finally:
        f.parent.chmod(0o755)
    assert proc.returncode == 1 and "Traceback" not in proc.stderr
    assert "directory" in proc.stderr and "not writable" in proc.stderr and "run apply again" in proc.stderr
    assert f.read_text(encoding="utf-8") == DOC and work.is_dir()
    assert t2_get(REPO_PROJECT, "doc/docs/s.md") is None
    assert not any(t.startswith("log/") for t in t2_titles(REPO_PROJECT))
    # the author fixes the permissions and answers again: the same work directory still serves
    out = review_ok(prose, "apply", "--work", str(work), "--accept", "1")
    assert [a["n"] for a in out["applied"]] == [1] and len(out["log"]["title"]) > 0


@needs_perms
def test_a_read_only_file_stops_the_apply_and_changes_nothing(prose: Prose, repo: Path) -> None:
    f = tracked(repo)
    work, _ = start(prose, repo, [E1, E2])
    f.chmod(0o444)
    try:
        proc = run_review(prose, "apply", "--work", str(work), "--accept", "1")
    finally:
        f.chmod(0o644)
    assert proc.returncode == 1 and "not writable" in proc.stderr and "Traceback" not in proc.stderr
    assert f.read_text(encoding="utf-8") == DOC and stat.S_IMODE(f.stat().st_mode) == 0o644
    assert t2_get(REPO_PROJECT, "doc/docs/s.md") is None
    assert not any(t.startswith("log/") for t in t2_titles(REPO_PROJECT))
    run_review(prose, "apply", "--work", str(work), "--accept", "none")


def test_a_save_during_the_apply_aborts_keeps_the_authors_text_and_logs_nothing_as_applied(
    prose: Prose, repo: Path, tmp_path: Path
) -> None:
    f = tracked(repo)
    work, _ = start(prose, repo, [E1, E2])
    env = {**prose.env, **hook(tmp_path, "open(target, 'a').write('AUTHOR SAVE\\n')")}
    proc = run_review(prose, "apply", "--work", str(work), "--accept", "1", env=env)
    assert proc.returncode == 1 and "Traceback" not in proc.stderr
    assert "changed" in proc.stderr and "run apply again" in proc.stderr
    assert f.read_text(encoding="utf-8") == DOC + "AUTHOR SAVE\n"
    assert staged_files(repo) == [] and work.is_dir()
    assert not any(t.startswith("log/") for t in t2_titles(REPO_PROJECT))  # no log claims an applied edit
    # run apply again: it plans against the file as it is now and applies on top of the author's save
    out = review_ok(prose, "apply", "--work", str(work), "--accept", "1")
    assert [a["n"] for a in out["applied"]] == [1]
    assert f.read_text(encoding="utf-8") == DOC.replace(E1["old"], E1["new"]) + "AUTHOR SAVE\n"


def test_the_before_replace_hook_runs_only_when_the_test_gate_is_set(
    prose: Prose, repo: Path, tmp_path: Path
) -> None:
    f = tracked(repo)
    work, _ = start(prose, repo, [E1])
    env = {k: v for k, v in prose.env.items() if k != "PROSE_EDIT_TEST"}
    env.update(hook(tmp_path, "open(target, 'a').write('AUTHOR SAVE\\n')"))
    proc = run_review(prose, "apply", "--work", str(work), "--accept", "1", env=env)
    assert proc.returncode == 0, proc.stderr
    assert f.read_text(encoding="utf-8") == DOC.replace(E1["old"], E1["new"])


def test_a_process_killed_between_staging_and_the_rename_leaves_no_untracked_file_in_the_repo(
    prose: Prose, repo: Path, tmp_path: Path
) -> None:
    f = tracked(repo)
    work, _ = start(prose, repo, [E1, E2])
    env = {**prose.env, **hook(tmp_path, "os.kill(os.getppid(), signal.SIGKILL)")}
    proc = run_review(prose, "apply", "--work", str(work), "--accept", "1", env=env)
    assert proc.returncode == -signal.SIGKILL
    assert f.read_text(encoding="utf-8") == DOC and status(repo) == ""  # nothing untracked, nothing modified
    assert len(staged_files(repo)) == 1  # the stray is in the git directory, where git status cannot see it


def test_a_document_that_is_a_symlink_to_outside_the_repository_is_refused_and_nothing_is_stored(
    prose: Prose, repo: Path, tmp_path: Path
) -> None:
    outside = tmp_path / "outside.md"
    outside.write_text(DOC, encoding="utf-8")
    (repo / "docs").mkdir(exist_ok=True)
    (repo / "docs" / "link.md").symlink_to(outside)
    work, _ = start(prose, repo, [E1, E2], text=DOC, rel="docs/real.md", target="docs/link.md")
    proc = run_review(prose, "apply", "--work", str(work), "--accept", "1")
    assert proc.returncode == 1 and "outside the repository" in proc.stderr
    assert outside.read_text(encoding="utf-8") == DOC
    assert t2_get(REPO_PROJECT, "doc/docs/link.md") is None
    assert not any(t.startswith("log/") for t in t2_titles(REPO_PROJECT))
    run_review(prose, "apply", "--work", str(work), "--accept", "none")


def _flaky_nx(tmp_path: Path, *, fail_when: str) -> dict[str, str]:
    """PROSE_EDIT_NX that reports T2 unavailable for a call whose argv contains `fail_when` ("" = every call)
    and otherwise runs the recording shim, so the real engine still serves the rest."""
    fake = tmp_path / "flakynx.py"
    fake.write_text(
        "import os, sys\n"
        f"if {fail_when!r} in ' '.join(sys.argv[1:]):\n"
        "    sys.stderr.write('T2 storage service unavailable: connection refused\\n')\n"
        "    sys.exit(1)\n"
        f"os.execv(sys.executable, [sys.executable, {str(SPY)!r}, *sys.argv[1:]])\n")
    return {"PROSE_EDIT_NX": f"{sys.executable} {fake}"}


def test_a_service_that_is_down_stops_an_accept_all_run_before_the_file_changes(
    prose: Prose, repo: Path, tmp_path: Path
) -> None:
    f = tracked(repo)
    work, _ = start(prose, repo, [E1, E2])
    env = {**prose.env, **_flaky_nx(tmp_path, fail_when="")}
    proc = run_review(prose, "apply", "--work", str(work), "--accept", "all", env=env)
    assert proc.returncode == 3 and "unavailable" in proc.stderr and proc.stdout == ""
    assert f.read_text(encoding="utf-8") == DOC and work.is_dir() and staged_files(repo) == []
    assert status(repo) == ""
    out = review_ok(prose, "apply", "--work", str(work), "--accept", "all")  # the service is back: run it again
    assert [a["n"] for a in out["applied"]] == [1, 2]


def test_a_log_that_fails_after_the_file_was_written_is_reported_and_the_exit_status_is_zero(
    prose: Prose, repo: Path, tmp_path: Path
) -> None:
    f = tracked(repo)
    work, _ = start(prose, repo, [E1, E2])
    env = {**prose.env, **_flaky_nx(tmp_path, fail_when="log/")}
    proc = run_review(prose, "apply", "--work", str(work), "--accept", "1", env=env)
    assert proc.returncode == 0, proc.stderr
    out = json.loads(proc.stdout)
    assert [a["n"] for a in out["applied"]] == [1] and out["log"] is None and "unavailable" in out["log_error"]
    assert "the file was written but the session log was not" in proc.stderr
    assert f.read_text(encoding="utf-8") == DOC.replace(E1["old"], E1["new"])
    assert work.is_dir() and (work / "log-pending.json").is_file()  # kept: log-retry sends it later
    assert not any(t.startswith("log/") for t in t2_titles(REPO_PROJECT))  # no log: none claims anything


# ---------------------------------------------------------------------------
# The author's answer, echoed first: apply --dry-run
# ---------------------------------------------------------------------------


def test_a_dry_run_prints_the_interpreted_sets_and_changes_nothing(prose: Prose, repo: Path) -> None:
    f = tracked(repo)
    work, _ = start(prose, repo, [E1, E2, edit(3, "Third paragraph repeats itself.", "T")])
    before = status(repo)
    out = review_ok(prose, "apply", "--work", str(work), "--accept", "1", "--hold", "2", "--dry-run")
    assert out["dry_run"] is True
    assert [e["n"] for e in out["accept"]] == [1] and [e["n"] for e in out["hold"]] == [2]
    assert out["accept"][0]["old"] == E1["old"] and out["accept"][0]["new"] == E1["new"]
    assert [e["n"] for e in out["unplaced"]] == [3]
    assert out["reject"] == [] and out["stores_rejections"] is False  # nothing would be stored
    assert out["would_skip"] == []
    assert f.read_text(encoding="utf-8") == DOC and status(repo) == before and work.is_dir()
    assert t2_get(REPO_PROJECT, "doc/docs/s.md") is None
    assert not any(t.startswith("log/") for t in t2_titles(REPO_PROJECT))
    assert staged_files(repo) == []
    # the real apply with the same words then does what the echo said
    done = review_ok(prose, "apply", "--work", str(work), "--accept", "1", "--hold", "2")
    assert [a["n"] for a in done["applied"]] == [1] and done["held"] == [2] and done["rejected"] == []


def test_a_dry_run_names_what_would_be_rejected_and_what_would_be_skipped_and_refuses_a_bad_answer(
    prose: Prose, repo: Path
) -> None:
    tracked(repo)
    work, _ = start(prose, repo, [E1, E2, edit(3, "is quite simple", "x")])
    out = review_ok(prose, "apply", "--work", str(work), "--accept", "1, 3", "--dry-run")
    assert [e["n"] for e in out["reject"]] == [2] and out["stores_rejections"] is True
    assert {s["n"]: s["cause"] for s in out["would_skip"]} == {1: "overlap", 3: "overlap"}
    assert out["would_apply"] == []
    bad = run_review(prose, "apply", "--work", str(work), "--accept", "all but 2", "--dry-run")
    assert bad.returncode == 1 and "--accept" in bad.stderr and work.is_dir()
    assert t2_get(REPO_PROJECT, "doc/docs/s.md") is None
    run_review(prose, "apply", "--work", str(work), "--accept", "none")


# ---------------------------------------------------------------------------
# Edits the copy could not show are not stored as rejections
# ---------------------------------------------------------------------------


def test_edits_that_were_never_shown_inline_are_not_stored_as_rejections_when_not_accepted(
    prose: Prose, repo: Path
) -> None:
    text = DOC + "Fourth paragraph says the queue drains in order and more.\n"
    edits = [
        E1,
        E2,
        edit(3, "Third paragraph repeats itself.", "T"),            # twice in the file: ambiguous
        edit(4, "Fourth paragraph says the queue", "F"),            # overlaps 5
        edit(5, "the queue drains in order and", "Q"),
    ]
    work, _ = start(prose, repo, edits, text=text)
    out = review_ok(prose, "apply", "--work", str(work), "--accept", "1, 4")
    assert out["rejected"] == [2]
    assert [u["n"] for u in out["unplaced"]] == [3, 5]  # not accepted, never shown inline: neither applied nor stored
    assert [a["n"] for a in out["applied"]] == [1, 4]  # accepted alone, one of an overlapping pair applies
    assert [r["old"] for r in t2_json(REPO_PROJECT, "doc/docs/s.md")["rejections"]] == [E2["old"]]
    assert (repo / "docs" / "s.md").read_text(encoding="utf-8") == text.replace(
        "It really is quite simple.", "It is simple.").replace("Fourth paragraph says the queue", "F")
    row = t2_json(REPO_PROJECT, out["log"]["title"])["session"]
    assert row["rejected"] == [2] and [u["n"] for u in row["unplaced"]] == [3, 5]


# ---------------------------------------------------------------------------
# The dry run is a mechanical gate: a real apply needs a matching dryrun.json
# ---------------------------------------------------------------------------


def test_an_apply_with_no_dry_run_is_refused_and_changes_nothing(prose: Prose, repo: Path) -> None:
    f = tracked(repo)
    work, _ = start(prose, repo, [E1, E2])
    proc = run_review(prose, "apply", "--work", str(work), "--accept", "1", dry_first=False)
    assert proc.returncode == 1 and "Traceback" not in proc.stderr
    assert "dry run" in proc.stderr and "--dry-run" in proc.stderr and "author" in proc.stderr
    assert f.read_text(encoding="utf-8") == DOC and work.is_dir() and staged_files(repo) == []
    assert t2_get(REPO_PROJECT, "doc/docs/s.md") is None
    assert not any(t.startswith("log/") for t in t2_titles(REPO_PROJECT))
    # the dry run, shown to the author, is what unlocks the real apply
    review_ok(prose, "apply", "--work", str(work), "--accept", "1", "--dry-run")
    out = review_ok(prose, "apply", "--work", str(work), "--accept", "1", dry_first=False)
    assert [a["n"] for a in out["applied"]] == [1]


def test_an_apply_with_a_different_answer_than_the_dry_run_is_refused_but_the_same_answer_in_other_words_is_not(
    prose: Prose, repo: Path
) -> None:
    f = tracked(repo)
    work, _ = start(prose, repo, [E1, E2])
    review_ok(prose, "apply", "--work", str(work), "--accept", "1", "--dry-run")
    for other in (("--accept", "2"), ("--accept", "all"), ("--accept", "1", "--hold", "2")):
        proc = run_review(prose, "apply", "--work", str(work), *other, dry_first=False)
        assert proc.returncode == 1 and "different answer" in proc.stderr and "--dry-run" in proc.stderr, other
        assert f.read_text(encoding="utf-8") == DOC and work.is_dir()
    assert not any(t.startswith("log/") for t in t2_titles(REPO_PROJECT))
    review_ok(prose, "apply", "--work", str(work), "--accept", "1,2", "--dry-run")
    out = review_ok(prose, "apply", "--work", str(work), "--accept", "2 1", dry_first=False)  # same set, spelled differently
    assert [a["n"] for a in out["applied"]] == [1, 2]


def test_an_apply_after_the_file_changed_since_the_dry_run_is_refused_until_the_dry_run_is_repeated(
    prose: Prose, repo: Path
) -> None:
    f = tracked(repo)
    work, _ = start(prose, repo, [E1, E2])
    review_ok(prose, "apply", "--work", str(work), "--accept", "1", "--dry-run")
    f.write_text(DOC + "AUTHOR SAVE\n", encoding="utf-8")
    proc = run_review(prose, "apply", "--work", str(work), "--accept", "1", dry_first=False)
    assert proc.returncode == 1 and "changed since the dry run" in proc.stderr and "--dry-run" in proc.stderr
    assert f.read_text(encoding="utf-8") == DOC + "AUTHOR SAVE\n" and work.is_dir()
    assert not any(t.startswith("log/") for t in t2_titles(REPO_PROJECT))
    review_ok(prose, "apply", "--work", str(work), "--accept", "1", "--dry-run")  # shown again, then it applies
    out = review_ok(prose, "apply", "--work", str(work), "--accept", "1", dry_first=False)
    assert [a["n"] for a in out["applied"]] == [1]
    assert f.read_text(encoding="utf-8") == DOC.replace(E1["old"], E1["new"]) + "AUTHOR SAVE\n"


def test_an_apply_after_the_filtered_proposal_changed_since_the_dry_run_is_refused(prose: Prose, repo: Path) -> None:
    f = tracked(repo)
    work, _ = start(prose, repo, [E1, E2])
    review_ok(prose, "apply", "--work", str(work), "--accept", "1", "--dry-run")
    filtered = json.loads((work / "filtered.json").read_text(encoding="utf-8"))
    filtered["edits"][0]["new"] = "SWAPPED."
    (work / "filtered.json").write_text(json.dumps(filtered), encoding="utf-8")
    proc = run_review(prose, "apply", "--work", str(work), "--accept", "1", dry_first=False)
    assert proc.returncode == 1 and "proposal" in proc.stderr and "--dry-run" in proc.stderr
    assert f.read_text(encoding="utf-8") == DOC


# ---------------------------------------------------------------------------
# After the file is written, nothing in the log step is a traceback; the log can be retried
# ---------------------------------------------------------------------------


def _inproc(prose: Prose, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str], *argv: str,
            fail_log: BaseException | None = None, dry_first: bool = True, same_device: bool | None = None
            ) -> tuple[object, int, str, str]:
    """main() of a fresh review.py module, in this process, in the prose fixture's environment;
    `fail_log` makes the session-log call (and only it) raise that."""
    mod = _review()
    for key, value in prose.env.items():
        monkeypatch.setenv(key, value)
    monkeypatch.chdir(prose.cwd)
    monkeypatch.setattr(tempfile, "tempdir", str(prose.tmp.resolve()))
    if fail_log is not None:
        real = mod._BRIEF.memory_json

        def memory_json(args: list[str], stdin: str | None = None) -> dict:
            if args and args[0] == "log":
                raise fail_log
            return real(args, stdin)

        monkeypatch.setattr(mod._BRIEF, "memory_json", memory_json)
    if same_device is not None:
        monkeypatch.setattr(mod, "_same_device", lambda a, b: same_device)
    if dry_first and argv[0] == "apply":
        assert mod.main([*argv, "--dry-run"]) == 0
        capsys.readouterr()
    code = mod.main(list(argv))
    cap = capsys.readouterr()
    return mod, code, cap.out, cap.err


@pytest.mark.parametrize("exc", [
    subprocess.TimeoutExpired("memory.py", 600), ValueError("Expecting value: line 1 column 1"),
    OSError(errno.EIO, "input/output error"), RuntimeError("boom")], ids=lambda e: type(e).__name__)
def test_any_failure_of_the_log_step_after_the_file_was_written_is_a_log_error_never_a_traceback(
    prose: Prose, repo: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str], exc: BaseException
) -> None:
    f = tracked(repo)
    work, _ = start(prose, repo, [E1, E2])
    _, code, out, err = _inproc(prose, monkeypatch, capsys, "apply", "--work", str(work), "--accept", "1", fail_log=exc)
    assert code == 0, err
    result = json.loads(out)
    assert result["log"] is None and result["log_error"] and "the file was written but the session log was not" in err
    assert [a["n"] for a in result["applied"]] == [1]
    assert f.read_text(encoding="utf-8") == DOC.replace(E1["old"], E1["new"])


def test_a_log_failure_with_nothing_written_is_a_user_error_and_keeps_the_work_directory(
    prose: Prose, repo: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    f = tracked(repo)
    work, _ = start(prose, repo, [E1, E2])
    _, code, out, err = _inproc(prose, monkeypatch, capsys, "apply", "--work", str(work), "--accept", "none",
                                fail_log=ValueError("Expecting value"))
    assert code == 1 and out == "" and "session log" in err and "Traceback" not in err
    assert f.read_text(encoding="utf-8") == DOC and work.is_dir()


def test_a_failed_log_keeps_work_and_log_retry_sends_it_so_the_session_is_not_lost(
    prose: Prose, repo: Path, tmp_path: Path
) -> None:
    f = tracked(repo)
    work, _ = start(prose, repo, [E1, E2])
    env = {**prose.env, **_flaky_nx(tmp_path, fail_when="log/")}
    proc = run_review(prose, "apply", "--work", str(work), "--accept", "1", env=env)
    assert proc.returncode == 0, proc.stderr
    out = json.loads(proc.stdout)
    assert out["log"] is None and out["log_error"] and "log-retry" in out["log_retry"] and str(work) in out["log_retry"]
    assert work.is_dir() and (work / "log-pending.json").is_file()
    assert not any(t.startswith("log/") for t in t2_titles(REPO_PROJECT))
    # the apply must not run twice over the same work directory; the author is pointed at the retry
    again = run_review(prose, "apply", "--work", str(work), "--accept", "1", dry_first=False)
    assert again.returncode == 1 and "log-retry" in again.stderr and "Traceback" not in again.stderr
    # still down: the retry fails the same way and keeps everything
    down = run_review(prose, "log-retry", "--work", str(work), env=env, dry_first=False)
    assert down.returncode == 3 and "unavailable" in down.stderr and (work / "log-pending.json").is_file()
    # back up: it sends the same log, deletes WORK, and the session is in the evidence
    done = review_ok(prose, "log-retry", "--work", str(work), dry_first=False)
    assert done["log"]["title"].startswith("log/") and not work.exists()
    row = t2_json(REPO_PROJECT, done["log"]["title"])["session"]
    assert row["accepted"] == [1] and row["applied"] == [1] and row["rejected"] == [2]
    assert f.read_text(encoding="utf-8") == DOC.replace(E1["old"], E1["new"])


def test_log_retry_needs_a_pending_log(prose: Prose, repo: Path) -> None:
    tracked(repo)
    work, _ = start(prose, repo, [E1, E2])
    proc = run_review(prose, "log-retry", "--work", str(work), dry_first=False)
    assert proc.returncode == 1 and "no pending session log" in proc.stderr and work.is_dir()


# ---------------------------------------------------------------------------
# The temp file beside the document: only for another device, visible, and swept
# ---------------------------------------------------------------------------


def test_the_beside_document_fallback_is_for_another_device_only_never_for_a_failure_to_make_the_stage_directory(
    repo: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    mod = _review()
    f = tracked(repo)
    stage_dir(repo).write_text("not a directory")  # mkdir cannot make the stage directory
    with pytest.raises(mod.UserError, match="cannot stage"):
        mod.stage_write(f, repo, b"edited\n")
    assert not any(p.name.endswith(".prose-edit") for p in f.parent.iterdir())
    assert f.read_text(encoding="utf-8") == DOC
    stage_dir(repo).unlink()

    def sweep_fails(directory: Path) -> None:
        raise OSError(errno.EIO, "sweep failed")

    monkeypatch.setattr(mod, "_sweep_stage", sweep_fails)  # a failing sweep is no reason to leave the git directory
    staged = mod.stage_write(f, repo, b"edited\n")
    try:
        assert staged.tmp.parent == stage_dir(repo) and staged.beside_document is False
    finally:
        staged.discard()


def test_a_stage_beside_the_document_is_reported_in_the_apply_output_and_on_stderr(
    prose: Prose, repo: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    f = tracked(repo)
    work, _ = start(prose, repo, [E1, E2])
    _, code, out, err = _inproc(prose, monkeypatch, capsys, "apply", "--work", str(work), "--accept", "1",
                                same_device=False)
    assert code == 0, err
    result = json.loads(out)
    assert result["staged_beside_document"] is True and "beside the document" in err
    assert f.read_text(encoding="utf-8") == DOC.replace(E1["old"], E1["new"]) and status(repo) == "M docs/s.md"
    # the ordinary path says nothing about it
    work2, _ = start(prose, repo, [edit(1, "the queue drains in order", "the queue drains", "filler")], text=DOC.replace(E1["old"], E1["new"]))
    _, code, out, err = _inproc(prose, monkeypatch, capsys, "apply", "--work", str(work2), "--accept", "1",
                                same_device=True)
    assert code == 0 and "staged_beside_document" not in json.loads(out) and "beside the document" not in err


def test_the_sweep_also_clears_old_strays_beside_the_document_but_only_this_documents_and_only_old_ones(
    repo: Path
) -> None:
    mod = _review()
    f = tracked(repo)
    old, young, other = f.parent / ".s.md.abc.prose-edit", f.parent / ".s.md.def.prose-edit", f.parent / ".t.md.abc.prose-edit"
    for p in (old, young, other):
        p.write_text("x")
    os.utime(old, (time.time() - 3 * 3600,) * 2)
    os.utime(other, (time.time() - 3 * 3600,) * 2)
    mod.stage_write(f, repo, b"edited\n").discard()  # the ordinary path: the stage is in the git directory
    assert not old.exists() and young.exists() and other.exists()
