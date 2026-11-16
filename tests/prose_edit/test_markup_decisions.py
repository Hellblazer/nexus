# SPDX-License-Identifier: AGPL-3.0-or-later
"""review.py decisions (nexus-scmcs.2.6, A1): the app's statuses as the flags the existing `apply` takes.

The verb writes nothing and logs nothing: `apply` stays the only writer of the document, the only store of
rejections and the only session logger.

Each test is one scenario in one tenant (a run's decisions never touch T2 until `apply`): several runs, one verb
call each, with a message on every assert naming the step (nexus-h72g3: the tenant ceiling).
"""
from __future__ import annotations

import hashlib
import json
from pathlib import Path

from tests.prose_edit.conftest import REPO_PROJECT, Prose, t2_titles
from tests.prose_edit.test_brief import brief_ok, edit
from tests.prose_edit.test_review import start
from tests.prose_edit.test_markup_verbs import (
    E1,
    E5,
    EDITS,
    TEXT,
    app_reply,
    listing,
    read,
    read_list,
    run_review,
    review_ok,
    translated,
    write_json,
)

QUERY = {"n": 1, "anchor": "wakes every ten", "text": "Every ten exactly?"}
ANSWER = [{"answers": "Q1", "old": "picks the oldest job.", "new": "picks the oldest job first."}]


def proposed(prose: Prose, repo: Path, refuse: dict[int, str] | None = None, **kw) -> tuple[Path, list[dict]]:
    """A run up to the author's turn in the app: translate (edits 1-4 sent, 5 refused locally) and proposed."""
    work, _ = translated(prose, repo, **kw)
    reply = write_json(work / "proposed.json", app_reply(read(work / "propose.json"), refuse=refuse))
    review_ok(prose, "proposed", "--work", str(work), "--from-file", str(reply))
    return work, read_list(work / "suggestions.json")


def decide(prose: Prose, work: Path, statuses: dict | list, *flags: str, name: str = "statuses.json"):
    return run_review(prose, "decisions", "--work", str(work), "--from-file",
                      str(write_json(work / name, statuses)), *flags)


def answered(prose: Prose, work: Path) -> int:
    """The author answers the run's one query; returns the number of the edit the answer added."""
    answers = write_json(work / "answers.json", ANSWER)
    return review_ok(prose, "answer", "--work", str(work), "--from-file", str(answers))["added"][0]["n"]


def test_the_apps_statuses_and_the_chats_flags_become_the_apply_flags(prose: Prose, repo: Path) -> None:
    # --- the app's statuses become apply flags, for this run's ids only
    work, sugg = proposed(prose, repo)  # ids s12..s15 for edits 1..4
    ids = {s["n"]: s["id"] for s in sugg}
    assert [(s["n"], s["find"], s["replace"]) for s in sugg] == [
        (n, e["find"], e["replace"]) for n, e in zip(read(work / "propose.json")["order"], read(work / "propose.json")["edits"])
    ], "the suggestions file records what was sent, not what the listing says"
    reply = listing(sugg, {ids[1]: "accepted", ids[2]: "rejected", ids[3]: "accepted", ids[4]: "pending"})
    proc = decide(prose, work, reply)
    assert proc.returncode == 0, proc.stderr
    out = json.loads(proc.stdout)
    assert out["accept"] == [1, 3] and out["reject"] == [2] and out["hold"] == [4], out
    assert out["apply_args"] == ["--accept", "1,3", "--reject", "2", "--hold", "4"], out["apply_args"]
    assert out["warnings"] == [], out["warnings"]
    assert out["undecided"] == [5], out["undecided"]  # refused locally: nobody decided it, so apply holds it unless the chat names it
    assert [(r["n"], r["cause"]) for r in out["leftover"]] == [(5, "formatting")], out["leftover"]

    # --- stale, pending and missing are held, with a warning for the missing one
    reply = listing([s for s in sugg if s["n"] != 4], {ids[1]: "stale", ids[2]: "accepted"})
    out = json.loads(decide(prose, work, reply).stdout)
    assert out["accept"] == [2] and out["hold"] == [1, 3, 4] and out["reject"] == [], out
    assert len(out["warnings"]) == 1 and "edit 4" in out["warnings"][0], out["warnings"]

    # --- the chat cannot decide an edit the app holds
    proc = decide(prose, work, listing(sugg, {}), "--accept", "1")
    assert proc.returncode == 1 and "1" in proc.stderr and "app" in proc.stderr, (proc.returncode, proc.stderr)

    # --- both flags on one edit are refused
    proc = decide(prose, work, listing(sugg, {}), "--accept", "5", "--hold", "5")
    assert proc.returncode == 1 and "both" in proc.stderr, (proc.returncode, proc.stderr)

    # --- an unknown chat number is refused (9 is no edit, x is no number)
    for bad in ("9", "x"):
        proc = decide(prose, work, listing(sugg, {}), "--accept", bad)
        assert proc.returncode == 1 and "--accept" in proc.stderr, (bad, proc.returncode, proc.stderr)

    # --- decisions writes nothing and stores nothing
    def held() -> dict[str, bytes]:
        return {p.name: p.read_bytes() for p in work.iterdir()
                if p.is_file() and p.name not in (".prose-edit-work", "statuses.json")}

    before, doc = held(), (repo / "docs" / "s.md").read_bytes()
    assert decide(prose, work, listing(sugg, {sugg[0]["id"]: "rejected"})).returncode == 0
    assert held() == before, "decisions changed a file in WORK"
    assert (repo / "docs" / "s.md").read_bytes() == doc and doc.decode("utf-8") == TEXT, "decisions changed the document"
    assert not any(t.startswith(("log/", "doc/")) for t in t2_titles(REPO_PROJECT)), "decisions stored something"

    # --- the chat's decisions for the leftovers join the app's (the app refuses edit 2)
    work, sugg = proposed(prose, repo, refuse={1: "find crosses a formatting boundary"})
    ids = {s["n"]: s["id"] for s in sugg}
    proc = decide(prose, work, listing(sugg, {ids[1]: "rejected", ids[3]: "accepted", ids[4]: "accepted"}),
                  "--accept", "2", "--reject", "5")
    assert proc.returncode == 0, proc.stderr
    out = json.loads(proc.stdout)
    assert out["accept"] == [2, 3, 4] and out["reject"] == [1, 5] and out["hold"] == [], out
    assert out["app"] == {"accept": [3, 4], "reject": [1], "hold": []}, out["app"]
    assert out["chat"] == {"accept": [2], "reject": [5], "hold": []}, out["chat"]
    assert out["apply_args"] == ["--accept", "2,3,4", "--reject", "1,5"], out["apply_args"]
    assert out["undecided"] == [], out["undecided"]

    # --- reject rest means the leftovers nobody named (the app refuses edit 1)
    work, sugg = proposed(prose, repo, refuse={0: "no"})
    out = json.loads(decide(prose, work, listing(sugg, {}), "--accept", "1", "--reject", "rest").stdout)
    assert out["accept"] == [1] and out["reject"] == [5], out


def test_decisions_refuses_what_is_not_this_runs_own_statuses_and_proposal(prose: Prose, repo: Path) -> None:
    # --- before `proposed`: refused, naming the verb, no traceback (twice over: the plain refusal and the named one)
    work, _ = translated(prose, repo)
    proc = decide(prose, work, listing([], {}))
    assert proc.returncode == 1 and "proposed" in proc.stderr, (proc.returncode, proc.stderr)
    assert "run `review.py proposed` first" in proc.stderr and "Traceback" not in proc.stderr, proc.stderr

    # --- translate records the sha256 of the filtered proposal, and `proposed` rewrites propose.json and keeps it
    work, _ = translated(prose, repo)
    want = hashlib.sha256((work / "filtered.json").read_bytes()).hexdigest()
    assert read(work / "propose.json")["filtered_sha"] == want, "translate did not record the filtered proposal's sha"
    review_ok(prose, "proposed", "--work", str(work), "--from-file",
              str(write_json(work / "proposed.json", app_reply(read(work / "propose.json")))))
    assert read(work / "propose.json")["filtered_sha"] == want, "`proposed` dropped the filtered proposal's sha"
    sugg = read_list(work / "suggestions.json")

    # --- a missing, foreign or malformed statuses file is refused
    outside = write_json(prose.tmp / "statuses.json", listing(sugg, {}))
    proc = run_review(prose, "decisions", "--work", str(work), "--from-file", str(outside))
    assert proc.returncode == 1 and "work" in proc.stderr, f"outside the work directory: {proc.returncode} {proc.stderr}"
    proc = run_review(prose, "decisions", "--work", str(work))  # edits are with the app: the statuses are needed
    assert proc.returncode == 1 and "--from-file" in proc.stderr, f"no --from-file: {proc.returncode} {proc.stderr}"
    assert "--from-file is required" in proc.stderr and "Traceback" not in proc.stderr, proc.stderr
    for bad in ({"ok": False}, "text", 3):
        proc = decide(prose, work, bad)
        assert proc.returncode == 1 and "list_suggestions" in proc.stderr, (bad, proc.returncode, proc.stderr)
    other = Path(brief_ok(prose, "tmpdir").strip())
    foreign = write_json(other / "statuses.json", listing(sugg, {}))
    proc = run_review(prose, "decisions", "--work", str(work), "--from-file", str(foreign))
    assert proc.returncode == 1 and "directly inside this work directory" in proc.stderr, \
        f"another run's work directory: {proc.returncode} {proc.stderr}"

    # --- the filtered proposal changed since translate: refused (this mutates filtered.json, so it comes last in the run)
    filtered = read(work / "filtered.json")
    filtered["edits"] = filtered["edits"][::-1]  # a re-filter that renumbered or reordered the edits
    write_json(work / "filtered.json", filtered)
    proc = decide(prose, work, listing(sugg, {sugg[0]["id"]: "accepted"}))
    assert proc.returncode == 1 and "filtered.json changed since translate" in proc.stderr, (proc.returncode, proc.stderr)
    assert "run `review.py translate` again" in proc.stderr and "Traceback" not in proc.stderr, proc.stderr
    assert proc.stdout.strip() == "", proc.stdout

    # --- an answer added after translate does not trip the check
    work, sugg = proposed(prose, repo, queries=[QUERY])
    n = answered(prose, work)
    proc = decide(prose, work, listing(sugg, {}), "--accept", str(n))
    assert proc.returncode == 0, proc.stderr
    assert json.loads(proc.stdout)["accept"] == [n], proc.stdout

    # --- a second answer after a changed filtered proposal does not launder the change
    work, sugg = proposed(prose, repo, queries=[QUERY])
    filtered = read(work / "filtered.json")
    filtered["edits"] = filtered["edits"][::-1]
    write_json(work / "filtered.json", filtered)
    answered(prose, work)
    proc = decide(prose, work, listing(sugg, {}))
    assert proc.returncode == 1 and "filtered.json changed since translate" in proc.stderr, (proc.returncode, proc.stderr)


def test_decisions_for_answers_unplaced_edits_and_runs_with_no_app_step(prose: Prose, repo: Path) -> None:
    # --- the author's answers join the leftovers (the run's own edits are 1..5, so the answer is edit 6)
    work, sugg = proposed(prose, repo, queries=[QUERY])
    n = answered(prose, work)
    assert n == 6, n
    out = json.loads(decide(prose, work, listing(sugg, {}), "--accept", str(n)).stdout)
    assert out["accept"] == [n], out

    # --- reject rest leaves the author's answer alone
    out = json.loads(decide(prose, work, listing(sugg, {}), "--reject", "rest").stdout)
    assert out["reject"] == [5] and n not in out["reject"], out

    # --- reject rest leaves an unplaced edit alone
    work, _ = translated(prose, repo, edits=[*EDITS, edit(6, "basically ", "", "occurs twice in the file")])
    assert [int(u["n"]) for u in read(work / "session.json")["unplaced"]] == [6], "setup: render did not leave edit 6 unplaced"
    review_ok(prose, "proposed", "--work", str(work), "--from-file",
              str(write_json(work / "proposed.json", app_reply(read(work / "propose.json")))))
    d = json.loads(decide(prose, work, listing(read_list(work / "suggestions.json"), {}), "--reject", "rest").stdout)
    assert d["reject"] == [5] and 6 not in d["reject"], d

    # --- a run that sent nothing needs no statuses
    work, _ = translated(prose, repo, edits=[E5])
    proc = run_review(prose, "decisions", "--work", str(work), "--accept", "5")
    assert proc.returncode == 0, proc.stderr
    assert json.loads(proc.stdout)["apply_args"] == ["--accept", "5"], proc.stdout

    # --- a missing propose.json is still refused when an editor edit is placed: skipping translate is only safe when
    # the app holds nothing, and the author's decisions in the app would be unmapped
    work, _ = start(prose, repo, [E1], text=TEXT)
    proc = run_review(prose, "decisions", "--work", str(work))
    assert proc.returncode == 1 and "translate" in proc.stderr, (proc.returncode, proc.stderr)

    # --- F4: step 10 skips translate when the proposal has no `edits`, so WORK/propose.json never exists. An answer to a
    # query then adds the run's only edit, and `decisions` (run without --from-file: nothing was sent) must not ask for
    # a translate that has nothing to translate. This one applies, so it runs last.
    work, _ = start(prose, repo, [], text=TEXT, queries=[QUERY])
    assert not (work / "propose.json").exists(), "setup: propose.json exists"
    n = answered(prose, work)
    review_ok(prose, "render", "docs/s.md", "--work", str(work), "--genre", "reference-doc")
    proc = run_review(prose, "decisions", "--work", str(work), "--accept", str(n))
    assert proc.returncode == 0, proc.stderr
    out = json.loads(proc.stdout)
    assert out["accept"] == [n] and out["apply_args"] == ["--accept", str(n)] and out["undecided"] == [], out
    # nothing was decided yet: the author's answer is undecided until the chat names it
    none = json.loads(run_review(prose, "decisions", "--work", str(work)).stdout)
    assert none["accept"] == [] and none["undecided"] == [n], none
    review_ok(prose, "apply", "--work", str(work), "--dry-run", *out["apply_args"])
    done = review_ok(prose, "apply", "--work", str(work), *out["apply_args"])
    assert done["applied"][0]["n"] == n, done["applied"]
    assert "picks the oldest job first." in (repo / "docs" / "s.md").read_text(encoding="utf-8")
