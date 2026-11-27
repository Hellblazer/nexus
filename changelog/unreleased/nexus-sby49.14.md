### Changed

- `nx settings show` reports a telemetry engine as missing under the same rules the plugin's telemetry senders use: a saved token goes only with the saved URL, a supervisor lease token only to its own endpoint, and an `http` URL that is not on this machine names no engine. (nexus-sby49.14)
- The telemetry setting's missing-engine problem now appears in the first-prompt notice and in the plugin's own `show` as well as in `nx settings`, and a repository with a large git index and no settings file no longer starts a git process to read settings. (nexus-sby49.14)
