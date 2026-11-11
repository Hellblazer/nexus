### Changed

- **A tuple tool's "no template registered for subspace" error now says to call `tuple_registry`** (nexus-3ceda.7). The engine's text stays first and unchanged; the added line names the tool that lists the registered subspace templates. A probe battery of 20 real failed calls (`scripts/agent_error_probes.py`) found this the one error whose text left an agent stuck: it reached for a tool that does not exist in 3 runs of 3. The other 17 kinds were fixed on the next call nearly every time, so the wider error-text rewrite was not done.
