### Changed

- **The `transcripts` procedure's `git-stash` violation rule counts only a stash that changes state** (nexus-qo7ie.12). `git stash list`, `show` and `create` are no longer counted; push, save, pop, apply, drop, clear, branch, store and a bare `git stash` still are. RDR-238's compliance measure is about stashing to revert and retest, and over 14 days 27 of 37 sessions that ran `git stash` only listed or showed.
