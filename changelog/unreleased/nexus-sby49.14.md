### Changed

- `nx settings show` reports a telemetry engine as missing under the same rules the plugin's telemetry senders use: a saved token goes only with the saved URL, a supervisor lease token only to its own endpoint, and an `http` URL that is not on this machine names no engine. (nexus-sby49.14)
