---
name: prose-edit
description: Use when the author asks to line-edit, tighten or cut a document (an RDR, reference doc, essay, CHANGELOG entry, web page, or text pasted in for a commit message), to list or remove a document's stored rejections, or to add an exemplar passage to a genre. Invoked as /prose-edit.
disable-model-invocation: true
---

# Prose edit

Arguments: $ARGUMENTS

Run every command from the repository root. Pass each argument as its own argv token. Never build a shell string from `$ARGUMENTS`. On every Bash line, put each token and each path in single quotes (a single quote inside one is written `'\''`): a path with a space or a `;` is author input, and so is every token. A `<placeholder>` below stands for one such quoted token.

| Name | Command prefix |
| --- | --- |
| BRIEF | `python3 .claude/skills/prose-edit/scripts/brief.py` |
| MEMORY | `python3 .claude/skills/prose-edit/scripts/memory.py` |
| REVIEW | `python3 .claude/skills/prose-edit/scripts/review.py` |

## Invocation

| Form | Meaning |
| --- | --- |
| `<path>[:<start>-<end>] [--genre G] [--budget N]` | Edit run. Budget defaults to 10. A range limits proposals to those lines. |
| `- --genre G [--budget N]` | Stdin run. The flags are the first line. The text is everything after the first newline. Nothing is applied. |
| `rejections <path> [--remove N]` | List a document's stored rejections, or remove number N. |
| `exemplar <genre> <path>:<start>-<end>` | Store that passage as an exemplar of the genre. |
| `-- <path>` or `./<path>` | A file named `rejections` or `exemplar`. |

## Steps

1. Run `BRIEF parse <token>...` with the arguments as tokens. For a stdin run pass only `-` and the flags to `parse`. Never pass the text. On exit 1, show stderr to the author and stop. A null `genre` in the output only means the flag was not given for a path run. Do not ask about it here: step 4 finds the genre from the path and reports when none exists. On any failure, show stderr and stop.
2. Branch on `mode` in the JSON.

   | mode | Do |
   | --- | --- |
   | `rejections` | Run `MEMORY <memory_argv...>`. Show the numbered rejections. Stop. |
   | `exemplar` | Run `MEMORY <memory_argv...>`. Show the stored passage and `duplicate`. Stop. |
   | `edit` | Continue. |

   If a `MEMORY` command fails, show stderr and stop.
3. Fix the input.

   | `stdin` | Do |
   | --- | --- |
   | false | Use `target` from step 1. |
   | true | Take the text after the first newline of the message. If there is none, ask the author to paste it and end your turn: no WORK exists yet. Otherwise run `BRIEF tmpdir` and call its output WORK. Write the text with the Write tool to `WORK/input.txt`. Use `-` as the target and `--file WORK/input.txt`. |

4. Build the brief. The stdout is a header and then the brief.

   | `stdin` | Run |
   | --- | --- |
   | false | `BRIEF build <target> --budget <budget> [--genre <genre>] --work`. The first two lines of the output are `WORK=<dir>` and `BRIEF_SHA=<sha>`. The brief is everything after the blank line that follows them. |
   | true | `BRIEF build - --budget <budget> --genre <genre> --file WORK/input.txt`. The first line of the output is `BRIEF_SHA=<sha>`. The brief is everything after the blank line that follows it. |

   Either way `build` has written the brief to `WORK/brief.md`. The line-editor reads that file; the text printed here is for you to check step 6.

   | Result | Do |
   | --- | --- |
   | exit 0 | Continue. |
   | exit 1, stderr contains `no genre` | Never choose a genre yourself. Ask the author which genre applies: rdr, reference-doc, how-to, exploration-essay, changelog, commit-message. End your turn with that question and do nothing else. Start again at step 1 with `--genre` only after the author answers. |
   | any other non-zero exit | On any failure, show stderr and stop. |

5. Call the directory named in step 3 or 4 WORK. WORK holds the marked-up copy and the saved proposal until step 12 deletes it. On every stop after WORK exists and before step 12, delete WORK first with `BRIEF rmtmp WORK`. Step 11, a retry in step 12, and a retry of step 9 or step 10 after exit 3 are the exceptions: the turn ends there with WORK in place.
6. If the brief contains `No exemplars are stored`, tell the author the genre has no exemplars yet and the editor runs without them.
7. Dispatch the `line-editor` agent with the Agent tool (`subagent_type` `line-editor`, `run_in_background` false). The prompt is these two lines and nothing else, with WORK as its full path and `<sha>` from step 4: `Read WORK/brief.md in full with Read and follow it.` and `BRIEF_SHA <sha>: put it in your reply as brief_sha.` Do not paste the brief into the prompt. Wait for its reply.
8. Write the agent's whole reply, once, with the Write tool to `WORK/reply.txt`.
9. Run `BRIEF filter <target> --budget <budget> --save WORK/filtered.json [--file WORK/input.txt] < 'WORK/reply.txt'`. Its output is the filtered proposal and the saved file holds the same text. A `warnings` entry about `brief_sha` means the editor may not have read the brief file: show it to the author with the others in step 10. On exit 3 (a T2 outage; `WORK/reply.txt` holds the editor's work): show stderr, keep WORK, tell the author, end your turn, and run this step again when the author says to retry. On any failure, show stderr and stop.
10. Run `REVIEW render <target> --work WORK [--file WORK/input.txt] [--genre <genre>]`. A stdin run passes `--file WORK/input.txt` and the genre it was given. A path run passes `--genre` only when the author gave one. If `REVIEW render` fails with exit 3 (T2 became unavailable after the editor ran), keep WORK: show stderr, tell the author, end your turn, and run the same command again when the author says to retry. Any other failure: show stderr and stop. Tell the author where the marked-up copy is (`copy`) and whether it opened in the viewer (`opened`; when false, `reason` says why and the path is how to read it). Show each filter `warnings` entry and each `unplaced` edit with its cause. If `opened` is false, also show the filtered proposal:

    | Part | Show |
    | --- | --- |
    | `note` | The editor's note. |
    | `voice_card` | The voice card. |
    | `paragraphs` | Each as `[P<n>] <action>: <paragraphs>. <advice>`. |
    | `edits` | Each as `<n>. <old> -> <new> (<reason>)`. |
    | `queries` | Each as `[Q<n>] <anchor>: <text>`. |
    | `dropped` | Each as `<n> <old> -> <new> (<cause>)`. The author cannot see these edits in the copy otherwise. |
    | `dropped_queries`, `dropped_paragraphs` | Each as `<n> <anchor or paragraphs> (<cause>)`. |

11. Ask the author which sentence edits to accept, which to reject (with a reason if the author wants to give one) and any corrections. Say that an edit the author does not name is left undecided: it is not applied and nothing is stored for it. Say that a rejection is stored for the document only when the author says `reject` with the numbers, or `reject the rest` (a stdin run stores none), and that `reject the rest` leaves alone an edit listed as one that cannot be placed. End your turn with that question and do nothing else. Never choose the numbers yourself.
12. When the author answers, turn the answer into an accept spec, a reject spec when the author rejects anything, and a hold spec when the author names edits to hold. A spec is explicit numbers and ranges (`1, 3-5`), `all` or `none`, in the author's own words. The reject spec may also be `rest`, only when the author says "reject the rest" or "reject everything else". Never reject an edit the author did not name: an edit the author leaves out is held, and so is every edit when the answer is "accept 1" or "none". For each reason the author gives, write all of them as one JSON object `{"<n>": "<the author's words>"}` (one key per rejected edit) with the Write tool to `WORK/reasons.json`, and pass `--reasons-file WORK/reasons.json`. Never put the author's words on the Bash line. If the answer names no explicit numbers (for example "all but 2", "the cuts", "looks good", "edit one", "no"), or `REVIEW` refuses it, ask the author again for explicit numbers and end your turn; never compute, infer or choose the set yourself.

    Run `REVIEW apply --work WORK --accept <spec> [--reject <spec>] [--hold <spec>] [--reasons-file WORK/reasons.json] --dry-run`, each spec as one argv token. `--accept` may be left out when the author accepts nothing: it defaults to `none`. It prints the sets it understood and changes nothing. Show the author `accept`, `hold` (undecided: not applied, nothing stored) and `reject` (each as `<n>. <old> -> <new>`, with its `reason` when it has one), `unplaced` (the edits the copy could not show, which are left alone) and `would_skip` (each with its `cause`). Say that only the edits under `reject` are stored as rejections for the document (`stores_rejections`; a stdin run stores none). Ask the author to confirm and end your turn. When the author confirms, run the same command without `--dry-run`. When the author changes a number, run the dry run again. The script refuses a real apply that no matching dry run came before (the same answer, the file not saved since): exit 1 with `dry run` in stderr, nothing written, WORK still there. Run the dry run, show it, and ask the author to confirm; never skip it.

    On exit 1 with `--accept`, `--hold`, `--reject`, `--reason` or `--reasons-file` in stderr (a number that is not an edit, an edit named twice, an answer that is not numbers, or a reasons file that is not valid), show stderr and ask again; WORK is still there. On exit 2 (a usage error: nothing ran), keep WORK, show stderr, fix the command and run it again. On exit 1 with `expired` in stderr, the work directory was swept after two hours idle and the review in it is gone: there is nothing to delete, so tell the author and start again at step 1. On exit 3, or exit 1 with `run apply again` in stderr (T2 unavailable or refusing, the file changed while the edits were applied, mixed line endings, a permission or disk problem), the file was not changed and WORK is still there: keep WORK, show stderr, tell the author the cause, and ask the author to say when to retry; on retry run the dry run again, show it, and run the real apply after the author confirms. On any other failure, delete WORK, show stderr and stop. A successful apply has already deleted WORK (unless it reports `log_error`, below); do not delete it again. Show the author:

    | Part | Show |
    | --- | --- |
    | `applied` | Each as `<n>. <old> -> <new>`. A stdin run applies nothing. |
    | `skipped` | Each as `<n> skipped: <cause> (<detail>)`. The edit stays in the file as it was. |
    | `rejected` | The numbers the author rejected. A path run stored them as rejections for the document, each with its reason when it had one. A stdin run stores none: say they were declined. |
    | `held` | The numbers left undecided: not applied, nothing stored. |
    | `unplaced` | The numbers the copy could not show. They were not stored. |
    | `log_error` | When present: the file was written but the session log was not, and WORK is still there. Tell the author the edits are in the file and the session is not yet in the accept-rate record, then run `REVIEW log-retry --work WORK` (now, and again when the author says T2 is back). It prints `log` and deletes WORK. If it fails, show stderr, keep WORK and stop; a WORK idle for more than two hours may be swept when the next one is made. Never run apply again for it. |
    | `staged_beside_document` | When present: the new text was staged beside the document, not in the git directory. Say so; nothing else to do. |
    | `text` | A stdin run only: the text with the accepted edits applied, in a code block. |

13. Offer to keep what the author taught you. Do either only after the author has said yes and has read the text that will be stored.
    - A correction in the answer or in conversation (for example "lead with the point here"): write it as a diagnostic that states its exception, ask which level it belongs to (user, repo or this document; a stdin run has no document level), show the text, and wait. Write `{"scalars": {}, "lists": {"diagnostics": ["<text>"]}}` with the Write tool to a file in a fresh work directory (make one as step 3 does), run `MEMORY add-entry --level <level> [--path <path>] --from-stdin < <file>`, then `BRIEF rmtmp` that directory.
    - An edit the author says is never a defect: run `MEMORY promote <path> <n> --level <user|repo> --dry-run`, where `n` is the number in `MEMORY rejections <path>`. Show the entry. When the author agrees, run it again without `--dry-run`.

## Rules

| Rule |
| --- |
| Never change the document except through `REVIEW apply`. |
| Never write to T2 except through MEMORY and REVIEW. |
| Never pass the author's text in a shell string. |
| On any failure, show stderr and stop. |
