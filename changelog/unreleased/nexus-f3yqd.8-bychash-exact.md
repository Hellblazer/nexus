### Fixed

- Hybrid search with a selective text gate no longer comes back empty when the gated chunks are a large share of the collection and lie far from the query. The rank over the gated set now always runs exactly, as it already did when a manifest filter was present, instead of letting the planner walk the vector index and filter its nearest rows down to nothing (nexus-f3yqd.8).
