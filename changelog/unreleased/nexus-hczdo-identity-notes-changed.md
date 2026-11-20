### Changed

- **`nx memory put --mine` is refused for a delegated `claude -p` child.** It wrote the parent's `identity/<id>` project before; a delegated identity keeps no identity notes (they would orphan when the child ends). The child still reads its parent's notes with `--mine` (nexus-hczdo).
