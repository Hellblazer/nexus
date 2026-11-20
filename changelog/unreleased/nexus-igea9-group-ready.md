### Added

- **`claude-restart-sessions --ask-ready --group <name>` and `--status --group <name>`.** The ready check becomes a group operation (RDR-252): it asks, and reports, only the live sessions whose names are the group's members, read from the engine roster (`nx group list <name> --json`). A group that is unknown, disbanded, empty, or has no live member here refuses in one line. The restart itself never takes a group, and a group round is not a round for the whole fleet: the restart's gate still names the sessions it never asked (nexus-igea9).
