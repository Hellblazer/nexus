# prose-edit acceptance runs

Headless Claude Code runs of the real `prose-edit` skill and `line-editor` agent, read-only against T2.

| File | Use |
| --- | --- |
| `run-scenario.sh NAME PROMPT` | One restricted session. Writes `NAME.jsonl`, `NAME.err`, `NAME.rc` under `$OUT_DIR`, then calls `record-run.sh`. Every other runner goes through it, so its allowlist is the only one. |
| `record-run.sh OUT_DIR NAME` | Writes `NAME.head` (the checkout's HEAD sha), `NAME.sha256` (the transcript's hash, `<hex>  NAME.jsonl`), `NAME.model` (the session's model, from the stream-json init event, `unknown` when there is none) and `NAME.status` (`git status --porcelain` when the run ended; it lists the runner's own untracked scenario copies, so it is read, not scored). `verdicts.py` prints the distinct shas of a batch on its `HEADS` line and the models on `MODELS`; copy the hashes somewhere durable with the verdict output, not only `/tmp`. |
| `run-canaries.sh` | The tool-restriction canary, the T2-failure canary, unmapped, stdin, stdin without genre. |
| `verdicts.py OUT_DIR` | Verdict and compliance counts per run. Exit 1 on any failure, 3 when none failed but a run was `NOT-MEASURABLE`, 0 only when every run passed. |
| `runner_guard.bash` | Sourced by `run-review.sh`, `run-teach.sh` and `run-memory-gate.sh` (not run on its own): the exclusive runner lock, the sweep of an older runner's leftover copies, and the exit trap that removes the copies. See "One runner at a time". |
| `run-review.sh` | The review-loop scenarios (nexus-ger02.4): reject then run again, list and remove a rejection then run again, a stdin run. `SCENARIOS="a b c"` (default all) picks the groups and is recorded in `scenarios.txt`. `SCENARIOS="b"` alone is refused (exit 2): b1 lists the rejections a2 stored, so b needs a's stored rejections and name `a` with it (`SCENARIOS="a b"`). Several turns each, answers through `RESUME_FROM`; the skill echoes an answer (`apply --dry-run`) and waits, so each answer turn is followed by a confirmation turn (`a2c`, `a4c`, `b4c`, `c2c`). The script sets `PROSE_EDIT_TEST=1`, which `PROSE_EDIT_OPEN` needs. Writes to T2 under `PROSE_EDIT_PROJECT_PREFIX` (default `zzprose024_`). |
| `review_verdicts.py OUT_DIR` | Verdicts for those runs from what the skill's scripts printed. A rejected fix shown again is a FAIL. A group that `scenarios.txt` does not list and that left no transcript is `NOT-RUN`, not FAIL (exit 3 when every row is PASS or NOT-RUN; a listed group with no transcript, or no `scenarios.txt`, still fails). `VACUOUS` (b3 only) means the editor did not propose the removed edit again, so the check proved nothing; it is reported as it is, never retried into a pass. |
| `run-memory-gate.sh LABEL SOURCE [N]` | The rejection-memory gate (nexus-ger02.16): N fresh runs on a copy of SOURCE. Writes `LABEL-<k>-<turn>.jsonl`. `GATE_JOBS` runs at once, default 1 (the preregistered runs were one at a time; several copies at once sit as siblings in `docs/zz-memgate/`, within reach of the editor's Glob and Grep). |
| `memory_gate_verdicts.py OUT_DIR` | Raw counts and the recurrence rate per document and pooled, against the threshold below. |
| `run-teach.sh` | Scenario 6 and step 13 (the voice card, the promote dry-run gate, the card reused): eight turns, answers through `RESUME_FROM`; the script itself runs the checks a turn cannot (`voice-card`, a promote with no dry run). It aborts before any session when the scenario document does not start clean. T2 prefix `zzprose0206_`. |
| `teach_verdicts.py OUT_DIR` | Verdicts for those runs, `seed-json` (the user-level entry the runner stores) and `clean-start` (the starting-state check). Exit 1 on a failure, 3 when a run is missing or `NOT-MEASURABLE`, 0 when all pass. |
| `fake_nx_unavailable.py` | `PROSE_EDIT_NX` stand-in that reports T2 unavailable. |

```bash
export TMPDIR=/Volumes/SanHell/tmp/nx.noindex   # any directory outside the repo
export CLAUDE_BIN=/path/to/claude                # default: claude on PATH (an alias does not count)
OUT_DIR=/tmp/prose-edit-acceptance tests/prose_edit/acceptance/run-canaries.sh
python3 tests/prose_edit/acceptance/verdicts.py /tmp/prose-edit-acceptance
```

`RESUME_FROM=<earlier name> run-scenario.sh NAME "<the author's answer>"` continues that run's session, which is how a scenario answers the question the skill ends its turn with. Delete the `zzprose024_*` T2 projects after a review run. A scenario is run once: there is no retry loop, because a retry that keeps the run in which the editor happened to behave selects the pass.

For a scenario, call `OUT_DIR=... run-scenario.sh NAME "/prose-edit <args>"` with at most 8 running at once. More made the cloud edge answer 503. The fixtures are in `../fixtures/`; name runs `xanadu-1`, `refrain-2`, `qa-3` and so on so that `verdicts.py` finds their document.

## The restriction

The runner uses `--permission-mode dontAsk`, allows `Bash` only for `brief.py`, `memory.py` and `review.py`, plus `Read`, `Write` and `Agent`, and denies `Bash(nx:*)`, `Bash(uv:*)`, `Bash(bd:*)` and `Bash(curl:*)`. The allowed rule is a prefix match on the literal text `python3 .claude/skills/prose-edit/scripts/<script>.py`, so the model has to type that text, relative and unquoted, with no `cd` and no `./` before it (the exit batch of 2026-10-03 was denied at its first call because the skill had told the model to quote every path). `SKILL.md` says so, and `tests/prose_edit/test_fix_round4.py` extracts every command form the skill teaches and checks it against this allowlist and `verdicts.py`'s `ALLOWED_BASH`. `--allowedTools` only pre-approves and `--disallowedTools` refuses, so `dontAsk` is what turns everything else into a denial. The `canary-nx` run proves it: the model is told to run `nx --version` and the call must come back denied.

A denied call in a scenario is a failure of the skill's instructions, not an incident. `verdicts.py` counts it under `denied`.

## One runner at a time

`run-review.sh`, `run-teach.sh` and `run-memory-gate.sh` each copy a scenario document into the shared worktree. The editor Greps the genre's paths (the brief's `Genre paths:` line, built from the tracked and the untracked files, so an untracked copy counts). In the exit batch of 2026-10-03 the three runners ran at once with their copies in `docs/*.md`: each copy was a sibling document of the others, the editor found its own sentences there and called the filler lines house refrains, and the results of review a3, the teach card text and every memory-gate session were not evidence.

Three controls, all in `runner_guard.bash`:

- **One runner at a time.** `runner_lock` takes a lock directory (`prose-edit-runner.lock` in the repository's common directory, so every worktree sees it) for the whole run and exits 75 naming the holder when another runner has it. A lock whose holder pid is dead is reclaimed; one with no holder record is held until it is a minute old. `run-canaries.sh` and the verdict scripts copy nothing and do not take it.
- **Copies outside the editor's search.** The copies live in `docs/zz-review/`, `docs/zz-teach/` and `docs/zz-memgate/`, nested directories no built-in genre maps, so no `Genre paths:` list reaches them. `--genre reference-doc` is passed on every edit run for that reason. `runner_sweep_stale` removes untracked `docs/zz-*` left by an older runner at the start (the lock holder knows none is live).
- **Nothing left behind.** `runner_track` registers the copy directory and the exit trap removes it, on a normal exit, an error, INT, TERM or HUP.

A runner that exits 75 wrote nothing: no output directory, no seeded viewer, no copy.

## The per-kind verdicts (nexus-ger02.6, S1)

A run named `xanadu-N`, `linda-N`, `refrain-N`, `qa-N`, `qb-N`, `qc-N`, `protected-N`, `budget-N` or `range-N` is scored on the post-filter proposal, the last `brief.py filter` output in its transcript (what the author sees), against the Verify line of the RDR's Test Plan. The document is read from `--root` as it is now: an edit whose old string is not in it makes the run `NOT-MEASURABLE`, so rerun the verdicts against the checkout the run edited.

What each kind scores, and against what. Nothing in `verdicts.py` calls the filter's code (`brief.py`): a verdict that reused it could not fail when the filter was wrong, and the filter's output cannot show an edit it already dropped. The oracles are the fixtures' own.

| Kind | PASS needs | FAIL on |
| --- | --- | --- |
| `protected` | one or more edits survived the filter | an edit on a line of the fixture's frontmatter (1-5), quote (11), code block (13-17) or table (19-22); these ranges are listed in `verdicts.py` (`FIXTURE_PROTECTED`) and checked against the fixture first, so a changed fixture is `NOT-MEASURABLE` |
| `budget` | one or more edits, at most the `--budget N` in the filter command | more than N edits |
| `range` | one or more edits, all inside the `PATH:START-END` of the filter command, by plain line arithmetic over `fixtures/range-notes.md` (run it as `.../range-notes.md:10-20 --genre changelog`) | an edit or a query anchor outside the lines |
| `qa` | `basically` cut; `may` queried and not cut | the filler left in, or queried; `may` cut, or neither cut nor queried |
| `qb` | `may` queried and not cut | `may` cut, or neither cut nor queried (a run with no proposal at all fails) |
| `qc` | `likely` and `truly` each queried and neither cut | either cut, or neither cut nor queried (silence fails) |
| `xanadu`, `linda`, `refrain` | at least one proposal, none on a device | an edit overlapping an always-protected device (refrain, repeated opening, closing line, the unexplained SQL: `DEVICES`) or a three-item construction the editor's `voice_card` names (`CARD_DEVICES`), or a paragraph proposal that cuts, merges or splits a paragraph holding one |

Every doc-editing run, and the stdin run, also needs proof that the `line-editor` subagent read `WORK/brief.md`. The file's last line is `Brief id: <id>` and the dispatch prompt carries no id, so a reply that names it came from a Read. The verdict needs a Read of `.../prose-edit-XXXXXXXX/brief.md` by the subagent, before its reply, in the work directory the skill filtered in, whose result is not an error and carries that line, and a `brief_sha` in the reply equal to it. Without it the run fails.

**Queried** means the word is in the query's `anchor`: a query attached to another sentence whose prose says "may" is not about the qualifier. An edit whose old string occurs more than once in the document breaks the editor's own once-only rule (the filter keeps it when any occurrence is editable, and apply refuses it), so it fails as its own problem, `old string occurs N times: apply refuses it`, and is not scored as a protected-region or range hit; only an edit with a unique old string is scored for those. A query anchor that occurs both inside and outside the range is reported the same way. The `xanadu` and `linda` notes also count the semicolon queries (an anchor with a `;`, or a query text that says "semicolon"), against the number of `;` in the document.

Every kind also fails on the scope and denial counts below and on more edits than its budget. The qualifier rows are Sam's ruling of 2026-09-30 (only filler words are cut; every other qualifier or intensifier is queried, with no class that stays untouched), which deviates from the RDR Technical Design sentence and Test Plan scenario 4 as accepted; it is recorded in the post-mortem at close. `NOT-MEASURABLE` means the run proposed nothing where something is expected (the qualifier kinds excepted), or the document, the protected ranges or the `DEVICES` list no longer matches: it proves nothing and is never a pass.

**What PASS means for the device kinds: none of the scored devices was touched.** The lists are read off the two documents by hand, with each phrase unique in the document (a phrase that is not there exactly once makes the run `NOT-MEASURABLE`). They are in two groups. `DEVICES` is always scored: the refrains, the closing lines, the repeated openings (the three "A ... had to" sentences, the "Nothing ..." pair, the "There is no way ..." triple and the parallel agent sentences) and the unexplained SQL. `CARD_DEVICES` holds the three-item constructions (tricolons and three-part catalogues) and is scored only when the editor's reply `voice_card` names the construction, matched by a distinctive phrase of it (case and quote style ignored): Sam's ruling of 2026-09-30 lets the editor restructure a three-item construction, a catalogue into a list for one, unless it is a device on the voice card. The note says how many card-conditional devices were scored. "The hash pins which chunk; the range pins where within it." is on neither list (Sam, 2026-10-03: not a device). A device that is not on a list is not checked: a human reads the rest of the edits.

The note of every run reports what the filter dropped, by cause (`dropped by the filter: over-budget=1, protected-region=2`), taken from the proposal's `dropped` lists. That is the only place the editor's own discipline shows, because an edit in a protected region or outside the range is dropped before the author sees it; for `protected` and `range` the note also counts the editor's own reply edits that broke the fixture. These counts are reported, not scored.

## Scenario 6 and step 13 (nexus-ger02.6, critique E)

`run-teach.sh` runs the flows `run-scenario.sh` cannot score on a single document, and `teach_verdicts.py OUT_DIR` scores them. Run once, T2 prefix `zzprose0206_`; delete the `zzprose0206_*` T2 projects afterwards. Not run by the test suite: every verdict has unit tests on planted transcripts.

| Run | What it does | Verdict |
| --- | --- | --- |
| `s6-1` | Scenario 6: seeds a user-level diagnostic (`teach_verdicts.py seed-json`), edits `fixtures/user-entry.md` as a `changelog` | the entry, with `(layer: user)`, is in the brief the editor read; the post-filter proposal holds an edit that replaces `worker` with `consumer`, or a query that names one of them |
| `t1`, `t2`, `t2c`, `t3` | Step 13, voice card: edit a copy, answer, confirm, then the author's yes to keeping the card | the card was shown before the yes and not stored then; stored after it; shown back after saving; `memory.py voice-card` (run by the script, `voice-card-after.json`) returns the card |
| `promote-nodry`, `t4`, `t4c` | Step 13, promote: the script runs a real promote with no dry run first, then the skill is asked to promote | the direct call exits 1 naming a dry run; the skill dry-runs before asking, the entry's old text is in the reply that asks, and it promotes for real only after the confirm. `NOT-MEASURABLE` when `t1` showed fewer than two edits or `t2c` stored no rejection |
| `t5` | A second edit run of the same document, a fresh session, after the card is stored | the brief the editor `Read` holds the stored card between the `voice_card` tags, the reply's `voice_card` equals it, and the filter printed no `voice_card` warning (`reuse` row) |

The script aborts (exit 1, before any session) when the scenario document already holds a voice card or a rejection, or when a promote dry-run record under two hours old exists in the repository's common directory: each changes the flow, and a verdict read from the changed flow would look like a gate failure. Delete the `zzprose0206_*` T2 projects after each batch.

What they cannot prove: one run per flow shows the skill can do it, not how often the model does. The card text is compared after whitespace is normalised, so a model that retypes it with another word fails the check, which is the point of the check.

### What stays by hand (not performed in Phase 1)

Two parts of step 13 have no run here, and neither was performed in Phase 1, by hand or through a model. They are judged by reading a session, not by a verdict, and nobody has read one:

- **Device recognition with and without the card.** Whether a stored card makes the editor leave a device alone that it would otherwise cut needs two sessions on the same document and a human reading the edits. `t5` proves the card reaches the editor and comes back unchanged, not that it changes what the editor proposes.
- **The correction flow through the skill.** A correction in the author's answer, the question about the level, the `Write` of the entry, `memory.py add-entry` and `rmtmp`. The author's wording and level are free text, the run would store a record the scenario then has to remove, and the model's part is one question and one file write. The script side (`add-entry`, the document-layer section 3 treatments, the stale-entry message that names the remove command) is covered by unit tests in `tests/prose_edit`. Scenario 6 seeds its entry by script for the same reason.

## What the counts cover, and what they do not

They cover tool use (denials, off-list commands, `nx` attempts), Grep scope, Reads of `docs/rdr`, edits to short closing lines, words inserted by an edit, and queries that say "no twin". They do not judge tricolons, meaning changes or content cuts. Read the edits for those.

## The rejection-memory gate (nexus-ger02.16, RDR-221)

**The claim, stated narrowly.** The author rejects every edit a session shows, a fresh session then reviews the same unchanged document once, and it does not show the same change at the same place again. That is what was measured: one rerun, reject-all, N runs on two documents. It is not a claim about a class of change (the editor still proposes another em dash after the author rejected five; class-level suppression is the not-a-defect promotion, by design), about a different change at the same spot (overlap alone is not a match), or about several reruns. Two mechanisms carry it: the document's rejections are in the editor's brief, and `memory.py filter` drops a proposal whose minimal change equals a stored rejection's. A fix the brief holds back is never proposed, so it leaves no trace except the editor's own note, which nothing enforces.

**Threshold, fixed before any run: the recurrence rate, shown-again divided by rejected, is at most 10% across all measurable runs.** The gate passes when the pooled rate over every measurable run is at most 10%. Each document's rate is reported against the same 10%; a document above it is reported as a miss for that document, with its transcripts, whatever the pooled figure is.

Definitions, fixed with the threshold (the non-vacuity row was added in the fix round, after the first batch, and applied to it afterwards):

| Term | Meaning |
| --- | --- |
| Run | Four sessions on one copy of a document: (1) `/prose-edit <copy> --genre reference-doc --budget 5`; (2) the author's answer, which the skill echoes (`apply --dry-run`); (3) "Yes, apply." stores the rejections; (4) a fresh `/prose-edit` of the same copy with the same arguments, in a new session. Each run has its own copy and so its own T2 record. No run is repeated or replaced. |
| Rejected | An edit the skill showed in session 1 and stored as a rejection in session 3. |
| Shown again | An edit in the `edits` of the last `brief.py filter` of session 4 (what the author would see in the copy) that is a rejected fix again: the same minimal change, or an edit whose changed words contain, or are contained in, the rejected edit's changed words with the same replacement text (a cut that removes words the author wanted kept is the same fix). The filter's own match is the first of these, so a hit there is a bug; the second is what the brief has to prevent. |
| Not shown again | Dropped by the filter (`cause` rejected), or never proposed. Both are counted, separately, so the report shows how much each mechanism carried. |
| Measurable | Session 4 proposed at least one edit (shown, or dropped by the filter). A run whose session 4 proposed nothing is NOT-MEASURABLE: an editor with nothing left to say shows no rejected fix because it shows no fix. It is listed with its R, adds nothing to the rate, and is never a pass. `review_verdicts.py` applies the same floor to its a3 check. |
| Runs that count | Every started run is listed. Measurable runs make the rate. A run that errored is listed with its transcript and is not replaced. |
| Reported, not scored | Session-4 edit counts per run and per document; the same-spot column (a shown edit whose old text overlaps a rejected edit's, with a different change: the filter does not drop it, Sam's decision says overlap is not a match); held edits shown again, the positive control when the answer holds some edits. |

Two documents of different size: `fixtures/review-scenario.md` (4 sentences) and a real repository document copied into the scenario sandbox (never edited in place). At least 10 runs on each. The runner is `run-scenario.sh` as documented above, T2 prefix `zzprose216_`; delete the `zzprose216_*` records afterwards. If the rate misses, the report says it missed. Do not tune the brief or the filter and run again to get under the line.

### Result of the first batch (2026-10-01; transcripts in `/Volumes/SanHell/tmp/nx.noindex/ger216-gate`), re-scored with the non-vacuity floor

`memory_gate_verdicts.py /Volumes/SanHell/tmp/nx.noindex/ger216-gate --source small=tests/prose_edit/fixtures/review-scenario.md --source storage=docs/storage-tiers.md --min-measurable 10`

| Document | Runs started | Measurable | Not measurable | Rejected | Shown again | Dropped by the filter | Not proposed again | Session-4 edits shown | Same-spot other | Rate |
| --- | --- | --- | --- | --- | --- | --- | --- | --- | --- | --- |
| `fixtures/review-scenario.md` (11 lines) | 10 | 4 | 6 | 12 | 0 | 0 | 12 | 4 (0 or 1 per run) | 0 | 0.0% |
| `docs/storage-tiers.md` (349 lines) | 10 | 9 (1 error) | 0 | 45 | 0 | 0 | 45 | 40 (2 to 5 per run) | 0 | 0.0% |
| Pooled | 20 | 13 | 6 | 57 | 0 | 0 | 57 | 44 | 0 | 0.0% (threshold 10%) |

As first reported, before the floor existed: 19 measurable runs, 81 rejected, 0 shown again. The floor takes out six small-document runs (24 rejected edits): in them the fresh session proposed no edit at all, because all of the document's three or four defects had been rejected and nothing else was left. Those runs say nothing about memory. Both documents are short of the 10 measurable runs the decision asks for: small has 4, storage has 9 (`storage-2` stopped in session 1 to ask which file version to edit, proposed nothing, and was correctly not replaced).

What the zeros are worth:

- **The no-memory baseline.** The session-1 edit lists of the ten independent runs per document had no stored rejections. Pairing run i's edits, taken as rejected, with run j's (j differs from i) gives the recurrence a session without memory shows: 300 of 324 pairs on the small document (92.6%) and 138 of 360 on the storage document (38.3% by the same fix; 47.2%, 170 of 360, by overlapping span). Against that, 0 of 57 is not luck. The pairs are pinned in `tests/prose_edit/test_filter_replay.py` from `fixtures/replay/`.
- **The interval.** Zero events in 57 independent rejected items has a 95% upper bound of about 3/57 = 5.3% (rule of three); the same on the 81 first reported is 3.7%. The items inside one run are not independent (a session that over-generalises does so for all its rejections), so the bound that counts is per run: 0 of 13 measurable runs, an upper bound of 3/13 = 23%. The gate threshold of 10% is inside that bound. Read the result as "no recurrence seen in 13 runs", not as "under 10%".
- **The filter alone, replayed with no model.** On those pairs the filter drops 300 of the 300 small-document recurrences, and on the storage document drops 138 and lets 32 through, 8.9% of the 360 rejected items (below the 10% line by 1.1 points). All 32 are one class: the same spot with another replacement (a dash cut to a comma in one run and to a semicolon in the other). The live gate never exercised this: the brief alone kept all 57 from coming back, the filter dropped nothing, and its minimal-change match is covered by the replay and the unit tests.
- **The same-spot column** found no such edit in the live runs (0 of 57), so the brief did not merely hide a filter escape. The column compares whole old texts, so another edit in the same sentence would also have counted: it is an upper bound.
- **Session-4 edit counts.** Storage: 40 edits in 9 runs. Small: 4 edits in 4 runs, and in each of those the one edit was the sentence the session-1 editor had not proposed ("The scheduler just wakes ..."), a positive control: the editor is not suppressed on a defect that was not rejected. 37 of the 40 storage edits are em-dash replacements, the class of all 45 rejected edits (every one of the 45 was a dash): class recurrence is about 100% and out of the claim, as stated above. In one storage run (`storage-1`) the editor proposed two edits instead of five and noted that each further dash fix "would be the same kind of change": the brief's "wherever it sits" wording read as a class once in nine runs.

### Re-measure on the small document with a subset rejected (fix round of nexus-ger02.16)

Fixed before any of these runs; the time they were fixed is in T2 `nexus/ger216b-remeasure-prereg` (written before the first session started).

The first gate's small document mostly measured an editor with nothing left to say (six of its ten fresh sessions proposed no edit, see the result above). This run gives the fresh session something legitimate to propose.

| Item | Fixed value |
| --- | --- |
| Document | `fixtures/review-scenario.md`, copied per run, as before |
| Runs | 10, at most. No retry, no replacement, no second batch. |
| Runner | `ANSWER="Accept none. Reject edits 1 and 3." OUT_DIR=<dir> run-memory-gate.sh sub tests/prose_edit/fixtures/review-scenario.md 10`, T2 prefix `zzprose216b_` (the restricted runner `run-scenario.sh`) |
| Answer | Reject edits 1 and 3 (in the editor's document order, the sentences with "basically" and "It should be noted that"), accept nothing, leave the rest undecided, so held. The numbers are the author's; what was stored is what is scored. A run whose answer names an edit that does not exist stops and is an ERROR, never replaced. |
| Threshold | The recurrence rate (shown again divided by rejected, over measurable runs) is at most 10%. |
| Non-vacuity floor | A run whose fresh session proposes no edit at all (none shown, none dropped by the filter) is NOT-MEASURABLE: listed, not in the rate, never a pass. At least 8 of the 10 runs must be measurable; with fewer the result is reported as short (exit 3), not as a pass. |
| Reported, not scored | Turn-4 edit counts per run; held edits shown again (the positive control: a held edit is a legitimate proposal, so a session that suppresses it as well is over-suppressing); the same-spot column. |
| Also reported | The rule-of-three upper bound at the measured number of rejected items, and per run. |

The scorer for it is `memory_gate_verdicts.py OUT_DIR --source sub=tests/prose_edit/fixtures/review-scenario.md --min-measurable 8`.

#### Result of the re-measure (2026-10-02; transcripts in `/Volumes/SanHell/tmp/nx.noindex/ger216b-gate`, `zzprose216b_*` records deleted afterwards)

Raw counts, `memory_gate_verdicts.py ... --min-measurable 8`:

| Runs started | Measurable | Not measurable | Errors | Rejected | Shown again | Dropped by the filter | Not proposed again | Same-spot other | Session-4 edits | Held | Held, shown again | Rate |
| --- | --- | --- | --- | --- | --- | --- | --- | --- | --- | --- | --- | --- |
| 10 | 10 | 0 | 0 | 20 | 0 | 0 | 20 | 0 | 16 (1 or 2 per run) | 15 | 14 | 0.0% (threshold 10%): PASS |

In every run the answer stored two rejections, the "basically" edit and the "It should be noted that" edit (the editor's spans differed: a whole sentence in some runs, the bare word in others), and held the rest. Session 4 had something to say every time: 16 edits, and 14 of the 15 held edits came back, so the editor was not over-suppressed (`sub-6` held "just" and the fresh session did not find it again; in the first batch's session 1 the editor missed that word in four of ten runs). The filter dropped nothing, as in the first batch; the brief carried it. The rule-of-three bound on 0 of 20 items is 15%, and per run, 0 of 10 runs, 30%: ten runs on four sentences cannot show a rate under 10%, they show that the first batch's zero was not only an empty editor.

What this does not cover, unchanged: a different run configuration. The author accepted nothing here, and the document was unchanged between the sessions. The mixed case (accept one, reject one with a reason, hold one by name and leave one unnamed, then the author changes the document and a fresh session proposes again) is a scripted test, `test_a_mixed_answer_on_a_document_the_author_then_changed_keeps_each_decision_apart` in `tests/prose_edit/test_rejection_memory.py`; it needs no model and was not run headless.

## What Phase 1 exit does not claim

The Phase 1 exit batch shows the skill and the agent complete the listed flows end to end under a restricted headless runner. It does not show the following, and nothing in this directory should be read as showing it.

- **No pass rate and no failure rate.** Each flow ran n=1 to 4 times, and a failed flow got one rerun after the instructions were edited between the failure and the pass. A pass after a fix aimed at that failure shows the flow can pass, not how often. The combined table mixes rows from more than one tree (read `HEADS`); only a batch run at one sha is evidence for that sha.
- **Only one model pair, under one restriction.** The orchestrator was haiku and the editor opus, in a `dontAsk` headless session with `--add-dir TMPDIR`. A different orchestrator model is not measured.
- **Interactive mode is unexercised.** A real session asks for permission to Read the work directory (outside the repository by design) where the runner pre-approves it with `--add-dir`, and the Read size limit for a large stdin brief is not tried. If either blocks the editor, the skill stops and shows the author the error; nobody has seen that happen.
- **Not performed in Phase 1:** device recognition with and without a stored voice card, the correction flow of step 13 and the section 3 treatments (ignore, query only, note only) through a model. The script side of each has unit tests; the model's part has no transcript.
- **Edit quality beyond the listed devices, protected spans and qualifiers.** `DEVICES` is a hand list read off two documents; PASS means none of those was touched. Meaning changes, tricolons not on the list and content cuts are for a human to read.
- **Scenarios 7, 8, 12 and 14** (a file changed under an edit, duplicate or overlapping edits, repo detection from the primary, a worktree and a subdirectory, the marked-up copy's location) are covered by the unit tests in `tests/prose_edit`, not by transcripts. That is Sam's decision (2026-10-03): they test script behaviour the model does not influence.
- **The memory gate at 3 runs is a mechanism check, not a rate.** The threshold above is stated for at least 10 measurable runs per document; a run of 3 with 0 shown again shows the mechanism works, and it gives no recurrence rate (0 of 6 rejected items has a 95% upper bound near 50%, and 0 of 3 runs one near 100%, by the rule of three).
- **The first-run voice card is model-written.** The editor writes it on the first run of a document, the author approves it, and later runs reuse it. Its device coverage is unchecked: a card that misses a device leaves that device unprotected, and a broad card can hold back edits and turn them into queries. Nothing measures either.
- **Concurrent runners are not supported.** One runner at a time (see "One runner at a time"); a batch that shared the worktree was contaminated once already.
