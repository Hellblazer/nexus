### Fixed

- The compaction checkpoint's list of running tasks names the command itself when the shell ran it as an unquoted `eval` word (`eval ./watcher.sh < /dev/null && ...`), instead of printing the whole shell wrapper line.
