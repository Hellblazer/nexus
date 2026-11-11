### Changed

- **`nx enrich bib` looks up paper documents only, by default** (GH #1611). It looked up every titled catalog document of the collection, knowledge notes and markdown included, and since a miss writes nothing, each of those was looked up again on every run. Documents of another content type are now counted in the run's summary and left alone; `--content-type any` restores the old reach, and `--content-type <type>` picks one type.
