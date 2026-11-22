### Changed

- **`nx ci wait` no longer defaults to the nexus board.** Without `--repo` it reads the repository name from the `origin` remote, and its GitHub reads use the origin's owner and name instead of `Hellblazer/<repo>`. With no origin remote it exits 5 and names `--repo`; a `--repo` that is not the origin's repository needs `--gh-repo`. A wait run from another repo used to read nexus's board silently (nexus-igea9).
