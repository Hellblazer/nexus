Answer-object family declarations (RDR-189 P2). One directory per family,
one `v<N>.toml` per version; `nexus.plans.target_families` is the only reader
and refuses a malformed tree. Promotion is a reviewed change to these files;
the field shape and the lifecycle are in that module's docstring and in
the `target-family-*.toml` tables in the [tables directory](https://github.com/Hellblazer/nexus/tree/main/src/nexus/tables). No declaration ships yet.
