### Fixed

- Hybrid search with a selective text gate could come back empty or short when the gated chunks are a large share of the collection and rank behind many closer chunks that the gate excludes. The rank over the gated set now always runs exactly, as it already did when a manifest filter was present, instead of letting the planner walk the vector index and filter its nearest rows away (nexus-f3yqd.8).
