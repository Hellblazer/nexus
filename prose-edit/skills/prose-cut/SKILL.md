---
name: prose-cut
description: Use only from the site-page review gate, to count a page's prose words, to cut words from one web page or exploration essay against a voice card and a word budget, and then to record the gate's decisions on those cuts. Invoked as conexus-prose-edit:prose-cut.
allowed-tools: Bash(python3 ${CLAUDE_PLUGIN_ROOT}/skills/prose-edit/scripts/brief.py *) Bash(python3 ${CLAUDE_PLUGIN_ROOT}/skills/prose-edit/scripts/review.py *)
---

# Prose cut

Arguments: $ARGUMENTS

The cut-only half of prose-edit, for a caller that is a model and not an author: the site-page review gate. It proposes cuts and nothing else. It never changes the document except through `REVIEW apply`, which the decide run below calls. It asks no question: the caller decides, in a second run.

Run every command from the directory this session started in. Never run `cd`, and put nothing in front of a command and nothing after it: no `;`, `&&`, `||`, `|` or `echo` joined to it. The exit code comes back with the result, and a command with anything joined to it no longer matches the permission rule, so it is refused. Write the command prefix of the table below exactly as it is shown: unquoted, with no `./` and no `cd` before it. The skill's permission rule matches that text, and a quoted or `./`-prefixed script path, or a `cd '<dir>' &&` in front, does not match it. Pass each argument as its own argv token. Never build a shell string from the arguments. Only the arguments after the prefix go in single quotes (a single quote inside one is written `'\''`). In a command below, the names of the first column (BRIEF, REVIEW) stand for the prefix text of their row, typed as it is shown, and a `<placeholder>` stands for one argument in single quotes.

| Name | Command prefix |
| --- | --- |
| BRIEF | `python3 ${CLAUDE_PLUGIN_ROOT}/skills/prose-edit/scripts/brief.py` |
| REVIEW | `python3 ${CLAUDE_PLUGIN_ROOT}/skills/prose-edit/scripts/review.py` |

## Invocation

| Form | Meaning |
| --- | --- |
| `<path>[:<start>-<end>] --mode cut-only --voice-card <file> --word-budget <N> [--genre <G>] [--budget <N>]` | Cut run. The voice card is a text file the caller wrote. The word budget is a target, not a cap. Budget caps the sentence edits and defaults to 10. |
| `count <path>[:<start>-<end>]` | Count run. Prints `{"words": N}`: the prose words of the page or range, the visible words outside protected regions and markup, an entity counted as the character it stands for. It is the unit of `words.document` in a cut run's output, so the caller picks the word budget from it and reports the final count in it. Nothing is written. |
| `decide <work> --accept <spec> [--reject <spec>] [--hold <spec>] [--reasons-file <file>]` | Record the caller's decisions on a cut run's cuts. `<work>` is the `WORK=` path the cut run printed. Every cut run is decided, `--accept none` included. |

## Cut run

1. Run `BRIEF parse <token>...` with the arguments as tokens. On exit 1 show stderr and stop. Branch on `mode` in the JSON: `cut-only` continues; `count` goes to the Count section; `decide` goes to the Decide section; any other mode is not this skill's (it only counts, cuts and decides): say that `/prose-edit` does it, and stop.
2. Build the brief: `BRIEF build <target> --budget <budget> [--genre <genre>] --mode cut-only --voice-card <voice_card_file> --word-budget <word_budget> --work`, with `--genre` left out when `genre` in step 1 is null. It prints a header, a blank line and the brief. The header names `WORK=<dir>`, `DISPATCH=<prompt>`, `REPLY=<path>`, `FILTERED=<path>` and more paths this skill does not use.

   | Result | Do |
   | --- | --- |
   | exit 0 | Continue. |
   | exit 1, stderr contains `no genre` | Show stderr, say the caller must pass `--genre`, and stop. Never choose a genre, and never ask for one. |
   | any other non-zero exit | Show stderr and stop. |

3. WORK is the directory on the `WORK=` line. Every path below comes from a header line and is used exactly as printed: `<work>` is the `WORK=` path, `<reply>` the `REPLY=` path, `<filtered>` the `FILTERED=` path. Never type the word WORK as a path and never create a directory named WORK. On every stop after WORK exists and before step 7, delete WORK first with `BRIEF rmtmp '<work>'`. Step 7 ends the run with WORK in place, because the decide run needs it.
4. Dispatch the `line-editor` agent with the Agent tool (`subagent_type` `conexus-prose-edit:line-editor`, `run_in_background` false). The prompt is the text after `DISPATCH=` on the header line, exactly as printed: copy it whole, change nothing, add nothing, and never type a path yourself. Do not paste the brief into the prompt. If the line-editor says it cannot read the brief, delete WORK, show its error and stop. Wait for its reply. The harness may run the agent in the background even so: then the tool result only says the agent started, and the reply comes later as a message from the agent. Wait for that message and do nothing else meanwhile.
5. Write the agent's whole reply, once, with the Write tool to the `<reply>` path. Keep its indentation and its json fence as they are: the filter reads both. Never copy the reply with Bash.
6. Run `BRIEF filter <target> --budget <budget> --mode cut-only --word-budget <word_budget> --save '<filtered>' < '<reply>'`, then `REVIEW render <target> --work '<work>' [--genre <genre>]`, passing `--genre` when step 1 gave one. On exit 3 (a T2 outage) keep WORK, show stderr and stop. On any other failure delete WORK, show stderr and stop.
7. Report to the caller and stop. Print `WORK=<the work path>` first, so the caller can decide later. Then show, from the filter output:

   | Part | Show |
   | --- | --- |
   | `words` | `document`, `budget`, `removed`, `after` and `counts`: `removed` counts the sentence edits only, never the paragraph cuts. Then each `warnings` entry, among them a kept cut that removes a negation (not, never, don't and the like) or a limit (only, must, unless and the like), and a markdown cut that leaves a `*` or a backtick without its pair: the filter checks structure, so the caller reads such a cut for its meaning, and the list is short, so no warning is not proof that no negation went. |
   | `edits` | Each as `<n>. <old> -> <new> (<reason>)`. |
   | `paragraphs` | Each as `[P<n>] cut: <paragraphs>. <advice>`. A paragraph cut is advice: the caller applies it with its own edit or ignores it, and this skill never does. It takes no decide number and nothing is stored for it, so one the caller declines comes back on the next run. |
   | `note` | The editor's note. |
   | `dropped`, `dropped_paragraphs`, `dropped_queries` | Each as `<n> <old or paragraphs or anchor> (<cause>)`. |
   | `unplaced` | Each edit `REVIEW render` could not place, with its cause. |

   Say that the cuts are proposals and the document is unchanged until the caller runs `decide`.

## Count

1. Run `BRIEF parse count <path>` with the caller's arguments as tokens (the first token is `count`). On exit 1 show stderr and stop. The JSON holds `target`.
2. Run `BRIEF count '<target>'`. Show its output, `{"words": N}`, and say that N counts the visible words outside protected regions and markup, an entity counted as the character it stands for. On a non-zero exit show stderr and stop. It changes nothing and makes no work directory.

## Decide

The caller's reasons for its rejections go in one file: it writes `{"<n>": "<reason>"}` (one key per rejected cut) to `<work>/reasons.json`, a file directly in `<work>`, and passes `--reasons-file '<work>/reasons.json'`. A reasons file anywhere else is refused.

A cut named in no set is held: the text stays and nothing is stored. A rejection stays until the author removes it with `/conexus-prose-edit:prose-edit rejections '<page>' --remove N`: the cut is stored for the page and the editor does not propose it again, so `--reject` is for a cut declined on its merits and for nothing else. An accepted cut whose old text no longer matches the page (the caller's own fix changed it) is skipped with the cause `not-found`, and a skipped cut is not stored. A paragraph cut takes no decide number and nothing is stored for it.

1. Run `BRIEF parse decide <work> --accept <spec> [--reject <spec>] [--hold <spec>] [--reasons-file <file>]`, with the caller's arguments as tokens. On exit 1 show stderr and stop. The JSON holds `apply_argv`, the arguments for `REVIEW apply`.
2. Run `REVIEW apply <apply_argv...> --dry-run`, each element of `apply_argv` as its own argument. It prints the sets it understood and changes nothing. Show `accept` (the new text goes in), `hold` (the text stays, nothing stored), `reject` (the text stays and the cut is stored as a rejection for the document), `unplaced` and `would_skip` with its causes.
3. Run `REVIEW apply <apply_argv...>`, the same command without `--dry-run`, without asking: the caller's arguments are its decision. The dry run and this run are one step, and the dry run is what lets the script allow the real run.
4. Show what the run printed: `applied` as `<n>. <old> -> <new>`, `skipped` as `<n> skipped: <cause> (<detail>)`, `rejected`, `held` and `unplaced` as numbers. A successful apply has deleted WORK.

   | Result | Do |
   | --- | --- |
   | `log_error` in the output | The file was written and the session log was not. Run `REVIEW log-retry --work '<work>'`, and show its result. |
   | exit 3, or exit 1 with `run apply again` in stderr | Nothing was changed. Keep WORK, show stderr, say the caller may run decide again, and stop. |
   | exit 1 with `expired` in stderr | The work directory was swept after two hours idle. Show stderr and stop; the caller starts again from the cut run. |
   | exit 1 with `--accept`, `--hold`, `--reject` or `--reasons-file` in stderr | The caller's decision names a number that is not an edit, or an edit that is not a cut. Show stderr and stop. WORK is still there. |
   | any other failure | Show stderr and stop. |

## Rules

| Rule |
| --- |
| Never change the document except through `REVIEW apply`. |
| Never write to T2 except through REVIEW. |
| Never ask the caller a question: a missing genre, a failure or a doubt is reported and the run stops. |
| Never pass the caller's text in a shell string. |
| On any failure, show stderr and stop. |
