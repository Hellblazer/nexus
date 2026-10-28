# conexus-prose-edit

A line editor for your own prose. `/conexus-prose-edit:prose-edit <path>` reads a document, proposes up to ten numbered sentence edits with a reason each, and opens a marked-up copy. You accept, reject or hold each edit by number; only accepted edits change the file. A rejection is stored for that document so the editor does not propose it again, and the editor's description of the document's voice can be kept as its voice card for later runs.

It is not installed with `conexus`. Nothing changes for anyone who does not install it.

## Quick start

1. Install `conexus` and run `/conexus:setup`. prose-edit keeps its memory in Nexus, and this gets Nexus installed and answering.
2. Install this plugin: `/plugin install conexus-prose-edit@nexus-plugins`.
3. Run `/conexus-prose-edit:prose-edit setup` and follow its prompts. It asks only about what is missing, offers a choice for each question, and ends with a document to try.

The rest of this page is detail you can skip on a first read.

## Genres

A genre is the kind of writing a document is. It sets what the editor looks for and whether it may add words to unpack a compressed sentence. Setup asks which genre each of your documentation folders holds; you pick from a suggestion it makes from the folder name.

| Genre | What it is and how the editor treats it |
|---|---|
| `rdr` | A design record: the question, the decision and why. The editor cuts hedging, may unpack, and keeps every decision and number. |
| `reference-doc` | Documentation read to look something up. The editor tightens it and may unpack compressed sentences. |
| `how-to` | A page that walks a reader through a task. The editor keeps the steps plain and in order, and may unpack. |
| `exploration-essay` | An essay that works out how something behaves. The editor keeps your voice, trims, and may unpack. |
| `changelog` | Release-note entries, short by design. The editor only cuts. |
| `commit-message` | A commit message, short by design. The editor only cuts. |

## Install

```
/plugin install conexus-prose-edit@nexus-plugins
```

The `nexus-plugins` marketplace is the one `conexus` and `sn` come from; add it first with `/plugin marketplace add Hellblazer/nexus` if you have not.

prose-edit keeps its memory (rejections, voice cards, example passages, the run log) in Nexus T2 through the `nx` command, so it needs Nexus installed and its service reachable: install the `conexus` plugin first (`/plugin install conexus@nexus-plugins`) and run its `/conexus:setup`, and keep the two plugins on the same release. Each person's memory lives in the T2 that person's `nx` points at; two people on one Nexus tenant share the user-level style sheet.

It runs on macOS and Linux, not Windows. The marked-up copy of a document opens in a viewer on macOS; elsewhere the run prints its path.

## Set up

Run this once per machine and once per repository:

```
/conexus-prose-edit:prose-edit setup
```

Setup is a short guided session. It checks what is already in place, asks only about what is missing, and changes nothing until you pick an option:

1. Nexus is installed and answers. If not, it says so and points you to `/conexus:setup`.
2. Two permission rules for the temp folder where a run keeps its work files are in `~/.claude/settings.json`. A plugin cannot ship permission rules, so setup offers to add them (Claude Code asks you to approve that edit), or to print the JSON for you to add. Without them every read and write of a work file asks you, and the editor cannot read its brief.
3. Which genre each documentation folder holds. The built-in map knows `docs/rdr/`, `docs/exploration/`, top-level `docs/*.md`, `web/*.html` and `CHANGELOG.md`. For up to eight other folders that look like documentation, setup suggests a genre and you pick it, pick another, or leave the folder unmapped. Folders such as tests, fixtures, scripts and hidden folders are not offered. A file no genre reaches makes a run ask which genre it is, and offers to remember the answer for its folder.
4. One or two example passages per genre in use. The editor imitates them; a genre with none runs on the style sheet alone. You can add them later.

Setup ends with "Ready." and a command to try on one of your own documents.

## Use

Each question a run asks, which genre a file is, which edits to accept, which to reject, is a short list of choices; typing your own answer is always one of them.

| Command | Does |
|---|---|
| `/conexus-prose-edit:prose-edit docs/guide.md` | Edit run on the whole file. |
| `/conexus-prose-edit:prose-edit docs/guide.md:40-90 --budget 5` | Proposals only inside lines 40-90, at most five. |
| `/conexus-prose-edit:prose-edit /abs/path/in/a/worktree.md` | A document in another checkout: the edit lands there. |
| `/conexus-prose-edit:prose-edit - --genre commit-message` then the text on the next lines | Edit pasted text; nothing is written. |
| `/conexus-prose-edit:prose-edit rejections docs/guide.md` | List the stored rejections, `--remove N` to drop one. |
| `/conexus-prose-edit:prose-edit exemplar reference-doc docs/guide.md:3-8` | Store a passage as an example of the genre. |

Where no other plugin has a skill of the same name, `/prose-edit` works as a short form.

A repository with `.claude/skills/site-page/SKILL.md` gets that page's section 3 rules as an extra layer for how-to pages and essays; a repository without it simply has no such layer.
