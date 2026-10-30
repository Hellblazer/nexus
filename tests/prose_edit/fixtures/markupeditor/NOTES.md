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
c. **Reply size.** The harness does not save a get_document reply to a file. The model has
   to copy it with Write. Use `from` and `limit` to read only the blocks that hold edits.
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
f. **open_document reports too early.** It replied "Opened" while the app was still showing
   its save prompt for the previous document, which had unsaved changes. Confirm the open by
   reading `url` from get_document or app_state, and check that `modalWindow` is empty.
