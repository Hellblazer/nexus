### Fixed

- **`nx ci` names a board exactly as the CI adapter does.** The adapter lower-cases the repo, maps each run of characters outside `[A-Za-z0-9._-]` to `-`, collapses runs of `-`, strips leading `._-`, cuts the topic at 80 characters and strips trailing `._-`; `nx ci record-push`, `check-hold`, `wait` and `ack` now do the same, so a repo `a--b` pushing `develop` is the board `a-b-develop`, and a name the adapter reduces to nothing is refused as underivable.
