### Changed

- **A read of chunks by id tells the engine which fields it wants** (nexus-mjmjl). `HttpVectorClient`'s get by ids now sends `include` to `/v1/vectors/store-get`, as its get by filter already did. An engine that honours it can skip the chunk text a metadata-only read never uses, such as the upgrade ladder's manifest steps. An older engine ignores the key and answers as before. Every caller that passes `include` reads only what it asks for, so nothing changes until the engine projects.
