### Changed

- `nx settings show` and the plugin's `show` say when the working copy of `.nexus/project.json` is looser than `HEAD` for `subagent_git` or `advice_block` (the committed value holds until the change is committed) and say "no committed copy" when `HEAD` has no file. (nexus-sby49.18)
- The SubagentStart Git row follows the same `HEAD`-aware answer the guard gives, so a repository strict only at `HEAD` gets the row. The destructive-action advice hook adds its second (committed-copy) read to the first, so a failing second read cannot lower a block. (nexus-sby49.18)
- `nx settings show` reads the supervisor lease by plain TTL freshness, as the plugin's telemetry senders do, with no health probe and no reap. (nexus-sby49.18)
