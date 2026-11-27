### Fixed

- **A collection label the readers misread can now be named by quoting it.** A label that passes the RDR-229 rule but is `all` (the corpus wildcard), ends in `*`, `-*` or `-<8 hex>` (stripped from a plan scope tag), or starts with `$` (a plan binding) could be registered and then not named in a `corpus` argument or a scope tag. Wrapping it in double quotes (`corpus="all"`, scope tag `"notes*"`) now names that label exactly in both the MCP corpus list and the plan scope-tag normaliser; unquoted parts read as before, and the label rule is unchanged (nexus-o5vbo).
