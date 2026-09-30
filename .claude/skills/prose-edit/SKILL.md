---
name: prose-edit
description: Use when the author asks to line-edit, tighten or cut a document (an RDR, reference doc, essay, CHANGELOG entry, web page, or text pasted in for a commit message), to list or remove a document's stored rejections, or to add an exemplar passage to a genre. Invoked as /prose-edit.
disable-model-invocation: true
---

# Prose edit

Arguments: $ARGUMENTS

Run every command from the repository root. Pass each argument as its own argv token. Never build a shell string from `$ARGUMENTS`.

| Name | Command prefix |
| --- | --- |
| BRIEF | `python3 .claude/skills/prose-edit/scripts/brief.py` |
| MEMORY | `python3 .claude/skills/prose-edit/scripts/memory.py` |

## Invocation

| Form | Meaning |
| --- | --- |
| `<path>[:<start>-<end>] [--genre G] [--budget N]` | Edit run. Budget defaults to 10. A range limits proposals to those lines. |
| `- --genre G [--budget N]` | Stdin run. The flags are the first line. The text is everything after the first newline. Nothing is applied. |
| `rejections <path> [--remove N]` | List a document's stored rejections, or remove number N. |
| `exemplar <genre> <path>:<start>-<end>` | Store that passage as an exemplar of the genre. |
| `-- <path>` or `./<path>` | A file named `rejections` or `exemplar`. |

## Steps

1. Run `BRIEF parse <token>...` with the arguments as tokens. For a stdin run pass only `-` and the flags to `parse`. Never pass the text. On exit 1, show stderr to the author and stop. A null `genre` in the output only means the flag was not given for a path run. Do not ask about it here: step 4 finds the genre from the path and reports when none exists. On any failure, show stderr and stop. Never run `nx`, start a service or repair anything.
2. Branch on `mode` in the JSON.

   | mode | Do |
   | --- | --- |
   | `rejections` | Run `MEMORY <memory_argv...>`. Show the numbered rejections. Stop. |
   | `exemplar` | Run `MEMORY <memory_argv...>`. Show the stored passage and `duplicate`. Stop. |
   | `edit` | Continue. |

   If a `MEMORY` command fails, show stderr and stop. Never run `nx`, start a service or repair anything.
3. Fix the input.

   | `stdin` | Do |
   | --- | --- |
   | false | Use `target` from step 1. |
   | true | Run `BRIEF tmpdir`. Call its output WORK. Take the text after the first newline of the message. If there is none, ask the author to paste it and wait. Write the text with the Write tool to `WORK/input.txt`. Use `-` as the target and `--file WORK/input.txt`. |

4. Build the brief. The stdout is the brief.

   | `stdin` | Run |
   | --- | --- |
   | false | `BRIEF build <target> --budget <budget> [--genre <genre>] --work`. The first line of the output is `WORK=<dir>`. The brief is everything after the blank line that follows it. |
   | true | `BRIEF build - --budget <budget> --genre <genre> --file WORK/input.txt`. The whole output is the brief. |

   | Result | Do |
   | --- | --- |
   | exit 0 | Continue. |
   | exit 1, stderr contains `no genre` | Never choose a genre yourself. Ask the author which genre applies: rdr, reference-doc, how-to, exploration-essay, changelog, commit-message. End your turn with that question and do nothing else. Start again at step 1 with `--genre` only after the author answers. |
   | any other non-zero exit | On any failure, show stderr and stop. Never run `nx`, start a service or repair anything. |

5. Call the directory named in step 3 or 4 WORK. On every stop after WORK exists and before step 9, delete WORK first with `BRIEF rmtmp WORK`.
6. If the brief contains `No exemplars are stored`, tell the author the genre has no exemplars yet and the editor runs without them.
7. Dispatch the `line-editor` agent with the Agent tool (`subagent_type` `line-editor`, `run_in_background` false). The prompt is the brief, unchanged. Wait for its reply.
8. Write the agent's whole reply, once, with the Write tool to `WORK/reply.txt`.
9. Run `BRIEF filter <target> --budget <budget> --work WORK [--file WORK/input.txt] < WORK/reply.txt`. A successful filter deletes WORK. On any failure, show stderr and stop. Never run `nx`, start a service or repair anything.
10. Show the author the filtered JSON as follows.

    | Part | Show |
    | --- | --- |
    | `warnings` | Each, first. |
    | `note` | The editor's note. |
    | `voice_card` | The voice card. |
    | `paragraphs` | Each as `[P<n>] <action>: <paragraphs>. <advice>`. |
    | `edits` | Each as `<n>. <old> -> <new> (<reason>)`. |
    | `queries` | Each as `[Q<n>] <anchor>: <text>`. |
    | `dropped`, `dropped_queries`, `dropped_paragraphs` | Each as `<n> <cause>`. |

## Rules

| Rule |
| --- |
| Never change the document. |
| Never write to T2 except through MEMORY. |
| Never pass the author's text in a shell string. |
| On any failure, show stderr and stop. Never run `nx`, start a service or repair anything. |
