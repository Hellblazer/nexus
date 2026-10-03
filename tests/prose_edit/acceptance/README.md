# prose-edit acceptance runs

Headless Claude Code runs of the real `prose-edit` skill and `line-editor` agent, read-only against T2.

| File | Use |
| --- | --- |
| `run-scenario.sh NAME PROMPT` | One restricted session. Writes `NAME.jsonl`, `NAME.err`, `NAME.rc` under `$OUT_DIR`. |
| `run-canaries.sh` | The tool-restriction canary, the T2-failure canary, unmapped, stdin, stdin without genre. |
| `verdicts.py OUT_DIR` | Verdict and compliance counts per run. Exit 1 on any failure, 3 when none failed but a run was `NOT-MEASURABLE`, 0 only when every run passed. |
| `run-review.sh` | The review-loop scenarios (nexus-ger02.4): reject then run again, list and remove a rejection then run again, a stdin run. Several turns each, answers through `RESUME_FROM`; the skill echoes an answer (`apply --dry-run`) and waits, so each answer turn is followed by a confirmation turn (`a2c`, `a4c`, `b4c`, `c2c`). The script sets `PROSE_EDIT_TEST=1`, which `PROSE_EDIT_OPEN` needs. Writes to T2 under `PROSE_EDIT_PROJECT_PREFIX` (default `zzprose024_`). |
| `review_verdicts.py OUT_DIR` | Verdicts for those runs from what the skill's scripts printed. A rejected fix shown again is a FAIL. `VACUOUS` (b3 only) means the editor did not propose the removed edit again, so the check proved nothing; it is reported as it is, never retried into a pass. |
| `run-memory-gate.sh LABEL SOURCE [N]` | The rejection-memory gate (nexus-ger02.16): N fresh runs on a copy of SOURCE. Writes `LABEL-<k>-<turn>.jsonl`. |
| `memory_gate_verdicts.py OUT_DIR` | Raw counts and the recurrence rate per document and pooled, against the threshold below. |
| `run-teach.sh` | Scenario 6 and step 13 (the voice card, the promote dry-run gate): seven turns, answers through `RESUME_FROM`; the script itself runs the checks a turn cannot (`voice-card`, a promote with no dry run). T2 prefix `zzprose0206_`. |
| `teach_verdicts.py OUT_DIR` | Verdicts for those runs, and `seed-json` (the user-level entry the runner stores). Exit 1 on a failure, 3 when a run is missing or `NOT-MEASURABLE`, 0 when all pass. |
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

The runner uses `--permission-mode dontAsk`, allows `Bash` only for `brief.py`, `memory.py` and `review.py`, plus `Read`, `Write` and `Agent`, and denies `Bash(nx:*)`, `Bash(uv:*)`, `Bash(bd:*)` and `Bash(curl:*)`. `--allowedTools` only pre-approves and `--disallowedTools` refuses, so `dontAsk` is what turns everything else into a denial. The `canary-nx` run proves it: the model is told to run `nx --version` and the call must come back denied.

A denied call in a scenario is a failure of the skill's instructions, not an incident. `verdicts.py` counts it under `denied`.

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
| `xanadu`, `linda`, `refrain` | at least one proposal, none on a device | an edit overlapping a refrain, tricolon, repeated opening or the unexplained SQL listed in `DEVICES`, or a paragraph proposal that cuts, merges or splits a paragraph holding one |

Every doc-editing run, and the stdin run, also needs proof that the `line-editor` subagent read `WORK/brief.md`. The file's last line is `Brief id: <id>` and the dispatch prompt carries no id, so a reply that names it came from a Read. The verdict needs a Read of `.../prose-edit-XXXXXXXX/brief.md` by the subagent, before its reply, in the work directory the skill filtered in, whose result is not an error and carries that line, and a `brief_sha` in the reply equal to it. Without it the run fails.

Every kind also fails on the scope and denial counts below and on more edits than its budget. The qualifier rows are Sam's ruling of 2026-09-30 (only filler words are cut; every other qualifier or intensifier is queried, with no class that stays untouched), which deviates from the RDR Technical Design sentence and Test Plan scenario 4 as accepted; it is recorded in the post-mortem at close. `NOT-MEASURABLE` means the run proposed nothing where something is expected (the qualifier kinds excepted), or the document, the protected ranges or the `DEVICES` list no longer matches: it proves nothing and is never a pass.

**What PASS means for the device kinds: none of the listed devices was touched.** `DEVICES` is read off the two documents by hand, with each phrase unique in the document (a phrase that is not there exactly once makes the run `NOT-MEASURABLE`). It lists the refrains, the tricolons, the repeated openings (the three "A ... had to" sentences, the "Nothing ..." pair, the "There is no way ..." triple and the parallel agent sentences) and the unexplained SQL that were found. A device that is not on the list is not checked: a human reads the rest of the edits.

The note of every run reports what the filter dropped, by cause (`dropped by the filter: over-budget=1, protected-region=2`), taken from the proposal's `dropped` lists. That is the only place the editor's own discipline shows, because an edit in a protected region or outside the range is dropped before the author sees it; for `protected` and `range` the note also counts the editor's own reply edits that broke the fixture. These counts are reported, not scored.

## Scenario 6 and step 13 (nexus-ger02.6, critique E)

`run-teach.sh` runs the flows `run-scenario.sh` cannot score on a single document, and `teach_verdicts.py OUT_DIR` scores them. Run once, T2 prefix `zzprose0206_`; delete the `zzprose0206_*` T2 projects afterwards. Not run by the test suite: every verdict has unit tests on planted transcripts.

| Run | What it does | Verdict |
| --- | --- | --- |
| `s6-1` | Scenario 6: seeds a user-level diagnostic (`teach_verdicts.py seed-json`), edits `fixtures/user-entry.md` as a `changelog` | the entry, with `(layer: user)`, is in the brief the editor read; the post-filter proposal holds an edit that replaces `worker` with `consumer`, or a query that names one of them |
| `t1`, `t2`, `t2c`, `t3` | Step 13, voice card: edit a copy, answer, confirm, then the author's yes to keeping the card | the card was shown before the yes and not stored then; stored after it; shown back after saving; `memory.py voice-card` (run by the script, `voice-card-after.json`) returns the card |
| `promote-nodry`, `t4`, `t4c` | Step 13, promote: the script runs a real promote with no dry run first, then the skill is asked to promote | the direct call exits 1 naming a dry run; the skill dry-runs before asking and promotes for real only after the confirm |

What they cannot prove: one run per flow shows the skill can do it, not how often the model does. The card text is compared after whitespace is normalised, so a model that retypes it with another word fails the check, which is the point of the check.

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
