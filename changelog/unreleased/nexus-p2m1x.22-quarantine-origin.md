### Changed

- Engine: a background data move writes the origin collection's id onto quarantined chunks that recorded only its name, so a restore can still find them after collections are addressed by id (RDR-229 Phase 3).
