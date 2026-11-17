### Changed

- **The vendored `dml_utils` takes a four-column primary key (RDR-246).** The pin moves to Hellblazer/dml_utils PR #3 (`9196f4b1c`), which appends changesets 011 and 012 and raises the ordered-key limit from three columns to four, the shape of `nexus.chunks`' key. A data move over a table or leaf with that key was refused when its run was created; it now runs, per leaf, and the worker's integration test covers a two-level partitioned table of that shape.
