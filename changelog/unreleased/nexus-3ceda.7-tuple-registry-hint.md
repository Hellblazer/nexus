### Changed

- **A tuple tool's "no template registered for subspace" error now says to call `tuple_registry`** (nexus-3ceda.7). The engine's text stays first and unchanged; the added line names the tool that lists the registered subspace templates. In a probe battery of 20 failed calls (`scripts/agent_error_probes.py`), this was the one error whose text sent an agent to a tool that does not exist (3 runs of 3); the battery's other two misses were defects in the probes themselves. The battery did not find a need for a wider rewrite of the error texts; its limits are listed in the script.
