# MarkupEditor reply fixtures (nexus-scmcs.2.1)

Captured 2026-10-29 from the suggestions build (stevengharris/MarkupEditorApp#32, d99a591).
The capture used a scratch copy of `../review-scenario.md` with three blocks added: a bold
run, a bash code block and a list item with curly quotes. Sam accepted s12, rejected s13 and
left s14 and s15 pending.

- `get_document.json`: verbatim. The `url` is shortened to a stand-in path.
- `propose_edits.json`: one call of six edits, verbatim, plus the refusal from a second
  one-edit call ("The retry" crosses into bold). The second call's refusal is appended as
  index 6; in the real reply it was index 0.
- `list_suggestions.json`: verbatim entries, trimmed. The real reply also held s3-s11 from
  the document open before this one.

## Facts later beads rely on

a. **Key names.**
   - get_document: `blocks[]` of `{index, kind, hash, markdown, warnings}`, plus `hasChanges`,
     `url`, `view`, `firstEditable`, `selectionBlock`, and `next` when `limit` cut the reply.
   - propose_edits: `results[]` of `{index, status: "proposed", id, block}` or
     `{index, status: "refused", reason}`.
   - list_suggestions: `suggestions[]` of `{id, status, block, find, replace, note}`. It has
     no `reason` key and no document key.
   - app_state: `url`, `hasChanges`, `modalWindow`.
b. **Locating edits.**
   - Block Markdown is the paragraph on one line. Repo docs wrap at about 80 columns, so an
     `old` that spans a line break in the file is not a substring of the block. Collapse
     runs of whitespace on both sides before locating.
   - The app rewrites list markers (`-` becomes `*`) and table rules (`|---|` becomes
     `| --- |`) in its Markdown.
   - Curly quotes come back as typed.
c. **Reply size.** `limit` is the most characters of block Markdown a get_document reply
   holds (default 100000), not a block count; `next` is the block index to pass as `from`.
   Read with `from` 0, no `limit`, then `from` = `next`. A short document comes back inline.
   A long one comes back in pages (the trial's 183-block document: two pages, about 190K
   characters) and the harness saves a large reply to a file instead of showing it
   (hand trial 2026-10-31), so the skill copies each saved page to its own file and
   `REVIEW translate` takes one `--blocks-file` for each.
d. **Saving from the app rewrites the whole file.** Saving after one accepted four-word edit
   on a copy of docs/collections.md took it from 226 lines to 98: every paragraph unwrapped,
   table rules and list markers rewritten, the final newline dropped. A pending suggestion
   saves its original text, and a curly quote in `replace` comes back straight.
   **Consequence (Sam, 2026-10-29):** the author never saves in the app. `REVIEW apply`
   writes the accepted edits. The skill then calls undo until `hasChanges` is false and
   reopens the file.
e. **Suggestion ids and document changes.** Ids do not restart when another document is
   opened (s12 followed s11). list_suggestions keeps returning the previous document's
   suggestions, with nothing that says which document each belongs to, and opening a new
   document did not make them stale. Match only the ids this run's propose_edits returned.
g. **What the serializer writes** (`serializer-source.md` gives `get_document_serializer.json`):
   - `_x_` becomes `*x*` and `__x__` becomes `**x**`.
   - `[sic]` becomes `\[sic\]`, `<` becomes `\<`, and `~x~` becomes `\~x\~` (plain text, not strikethrough).
   - `&amp;` becomes `&`.
   - A reference link `[t][d]` becomes inline `[t](url)`, and the definition line is dropped.
   - A hard break is `\` followed by a newline. Ordered lists are kind `numbered-list`, and quotes are kind `quote`.

   `propose_edits_serializer.json` holds the request and the reply. The app accepts a find written as visible text:
   - across `[sic]`, `<`, `~`, `&`, a backslash-escaped `*`, a link's text, an emphasis run's inner text, a list item, a quote and a table cell;
   - it refuses a find that spans a hard break when the find writes the break as a space.

   So `old` has to be located by visible text, not by file Markdown.
f. **open_document reports too early.** It replied "Opened" while the app was still showing
   its save prompt for the previous document, which had unsaved changes. The load also happens
   after the reply even with no prompt: a get_document sent straight after "Opened" returned
   the previous document. Before calling get_document, poll app_state until `url` is the
   target and `modalWindow` is empty.
h. **The changed-on-disk dialog.** When the file changes under a buffer with unsaved changes,
   the app raises its own dialog, "<file> changed on disk. This document has unsaved changes.
   Reloading will discard them.", with "Reload from Disk" and "Keep My Changes" (hand trial
   2026-10-31). The accepted suggestions are those unsaved changes. While the dialog is up the
   Assistants connection stalls (app_state errors "Resource temporarily unavailable", then the
   connection closes) and app_state does not report it as `modalWindow`. With a clean buffer the
   app reloaded without a dialog. **Consequence:** the skill undoes in the app until `hasChanges`
   is false BEFORE the real apply writes the file (the dry run hashes the file, which undo does
   not touch), then reopens it; if app_state fails after apply, the author chooses "Reload from
   Disk", never "Keep My Changes".
   **Live run 3 of the same trial:** after Accept All, two undos restored the text, a third said
   "There is nothing to undo", and `hasChanges` stayed true, so the dialog came up anyway. Undo
   does not clear the app's unsaved mark after an accept; the skill tells the author before the
   apply that the dialog is coming and that the answer is "Reload from Disk".
