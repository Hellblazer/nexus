### Changed

- **The `transcripts` procedure's `git-stash` violation rule counts only a stash that changes state** (nexus-qo7ie.12). `git stash list`, `show` and `create` are no longer counted; push, save, pop, apply, drop, clear, branch, store and a bare `git stash` still are. RDR-238's compliance measure is about stashing to revert and retest; over 14 days the detector found 31 of 78 main sessions with any stash command and 4 with one that changes state. A redirect glued to the subcommand word (`git stash list>file`) is read as that subcommand.
