Answer-object family declarations (RDR-189 P2). One directory per family,
one `v<N>.toml` per version; `nexus.plans.target_families` is the only reader
and refuses a malformed tree. Promotion is a reviewed change to these files;
the field shape and the lifecycle are in that module's docstring and in
`src/nexus/tables/target-family-*.toml`. No declaration ships yet.
