# conexus-prose-edit

A line editor for your own prose. `/prose-edit <path>` reads a document, proposes up to ten numbered sentence edits with a reason each, and opens a marked-up copy. You accept, reject or hold each edit by number; only accepted edits change the file. A rejection is stored for that document so the editor does not propose it again, and the editor's description of the document's voice can be kept as its voice card for later runs.

It is not installed with `conexus`. Nothing changes for anyone who does not install it.

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
/prose-edit setup
```

It checks four things and fixes each one only after you say yes:

1. `nx` is installed and T2 answers.
2. Two permission rules for the temp folder where a run keeps its work files are in `~/.claude/settings.json`. A plugin cannot ship permission rules, so setup prints the JSON for you to add. Without them every read and write of a work file asks you, and the editor cannot read its brief.
3. Which folders hold which genre of prose (rdr, reference-doc, how-to, exploration-essay, changelog, commit-message). The built-in map knows `docs/rdr/`, `docs/exploration/`, top-level `docs/*.md`, `web/*.html` and `CHANGELOG.md`; setup asks about the rest. A file no genre reaches makes a run ask for `--genre`.
4. One or two example passages per genre. The editor imitates them; a genre with none runs on the style sheet alone.

## Use

| Command | Does |
|---|---|
| `/prose-edit docs/guide.md` | Edit run on the whole file. |
| `/prose-edit docs/guide.md:40-90 --budget 5` | Proposals only inside lines 40-90, at most five. |
| `/prose-edit /abs/path/in/a/worktree.md` | A document in another checkout: the edit lands there. |
| `/prose-edit - --genre commit-message` then the text on the next lines | Edit pasted text; nothing is written. |
| `/prose-edit rejections docs/guide.md` | List the stored rejections, `--remove N` to drop one. |
| `/prose-edit exemplar reference-doc docs/guide.md:3-8` | Store a passage as an example of the genre. |

A repository with `.claude/skills/site-page/SKILL.md` gets that page's section 3 rules as an extra layer for how-to pages and essays; a repository without it simply has no such layer.
