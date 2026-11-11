### Added

- **`nx doctor --fix` runs the dead-tuple VACUUM** (GH #1609 follow-up). When the `tuples.dead_tuple_ratio` row fails, its fix line used to be raw SQL (`VACUUM ANALYZE nexus.<table>;`), which needed the database password. On a local install `--fix` now runs that VACUUM through the install's own admin credentials, only on the tables the row would fail; `--fix --dry-run` lists them first, a managed service's database is left to its operator, and a failed VACUUM fails the command. The row's fix line now names `nx doctor --fix`.
