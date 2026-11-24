### Fixed

- **`nx ci` names a board the way the CI adapter does when a repo name has a double dash.** The adapter collapses runs of `-` in `<repo>-<branch>` (RDR-220); `nx ci record-push`, `check-hold`, `wait` and `ack` now do too, so a repo `a--b` pushing `develop` is the board `a-b-develop`.
