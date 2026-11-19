### Changed

- **`nx_answer(structured=True)` no longer returns every retrieved chunk once per step.** The envelope's `chunks` list is now deduplicated by chunk id (the first occurrence stays, so the order the plan's steps returned them in survives) and capped at 50 entries; a six-step plan had returned a reply of 96k characters, most of it this list. The new always-present `chunks_truncated` key counts the distinct chunk ids the cap dropped (0 when none; duplicates removed by the dedupe are not counted). GH #1667.
