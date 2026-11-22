### Fixed

- Remedy hints that print an `nx` command with a collection label in it quote the label (`shlex.quote`), so a label holding a space, `;` or `%` pastes as one argument. A lint keeps new hints quoted (nexus-88aj0).
