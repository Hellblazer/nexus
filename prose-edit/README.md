# conexus-prose-edit

A line editor for your own prose. `/conexus-prose-edit:prose-edit <path>` reads a document, proposes up to ten numbered sentence edits with a reason each, and opens a marked-up copy. You accept, reject or leave each edit undecided by ticking choices in a short list; only accepted edits change the file. A rejection is stored for that document so the editor does not propose it again, and the editor's description of the document's voice can be kept as its voice card for later runs.

It is not installed with `conexus`. Nothing changes for anyone who does not install it.

## Quick start

Type these in Claude Code, in order, in your own repository:

1. `/plugin marketplace add Hellblazer/nexus` adds the marketplace both plugins come from.
2. `/plugin install conexus@nexus-plugins` installs `conexus`, which provides the Nexus that prose-edit keeps its memory in.
3. `/conexus:setup` checks what Nexus is missing and asks one question: where to run it (this machine, a managed service, or the command line only); pick this machine unless someone gave you a service address and a token. The install runs in the background and the first download is about a gigabyte, so it takes a few minutes. Run `/conexus:setup` again to see how far it is; it reports that nothing is missing when it is done.
4. `/plugin install conexus-prose-edit@nexus-plugins` installs this plugin.
5. Start a new session, or run `/reload-plugins`, so the new plugin loads.
6. `/conexus-prose-edit:prose-edit setup` is a short guided session of choices: it asks only about what is missing and ends with "Ready." and a command to try on one of your own documents.

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
3. Which genre each documentation folder holds. The built-in map knows `docs/rdr/`, `docs/exploration/`, top-level `docs/*.md`, `web/*.html` and `CHANGELOG.md`. For up to eight other folders that look like documentation, setup suggests a genre for each and asks once: use all the suggestions, go through the folders one by one (pick the suggestion, pick another, or leave the folder unmapped), or map nothing and be asked when you edit. Folders such as tests, fixtures, scripts, build output and hidden folders, and root files other than `README.md`, are not offered. A file no genre reaches makes a run ask which genre it is, and offers to remember the answer for its folder.
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

### Cut-only, for the site-page review gate

`/prose-edit` is the author's own command: Claude Code does not let a model call it. A repository whose site-page review gate is a model needs a voice pass it can call, so the plugin has a second skill, `conexus-prose-edit:prose-cut`, that a model can invoke. It runs the same scripts and the same editor in cut-only mode: every edit only removes words, paragraph proposals are cuts, and the editor raises no queries.

| Command | Does |
|---|---|
| `conexus-prose-edit:prose-cut web/page.html --mode cut-only --voice-card card.txt --word-budget 900 --genre how-to --budget 20` | Proposes cuts against the voice card in `card.txt`. Prints the work directory (`WORK=`) and the filtered proposal with the word count: the page's prose words, the budget, the words the cuts remove and the words left. The page is unchanged. |
| `conexus-prose-edit:prose-cut count web/page.html` | Prints `{"words": N}`: the page's prose words in the unit of `words.document`, for picking the word budget and reporting the final count. |
| `conexus-prose-edit:prose-cut decide <WORK> --accept 1,3 --reject 2 --hold 4` | Records the decisions: applies the accepted cuts, stores the rejected ones for that page so the editor does not propose them again, and leaves the held ones alone. It runs `apply`'s dry run and then the real run, with no question. Every cut run is decided, `--accept none` included. |

The word budget is a target and not a cap: the editor cuts toward it where words repeat or are filler, and `words` in the output shows the gap (`removed` counts sentence edits only; paragraph cuts are advice the caller applies or ignores, take no decide number and are never stored). The filter checks that a cut only removes words and adds no markup, and warns on a cut that removes a negation (contractions included) or a limit such as only, must or unless, and on a markdown cut that leaves emphasis unpaired: it is a structural check, and the meaning is the reader's. The given voice card is used for that run whatever card is stored for the page, and it is never stored. On an HTML page the nav, heading, button and sample-output elements are protected along with code, tables, quotes, comments and the head.

A repository with `.claude/skills/site-page/SKILL.md` gets that page's section 3 rules as an extra layer for how-to pages and essays; a repository without it simply has no such layer.
