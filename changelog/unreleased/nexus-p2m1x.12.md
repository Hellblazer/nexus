### Removed

- `nexus.repo_identity.list_sibling_collections` (and its deprecated re-export through `nexus.registry`) is deleted. It matched sibling collections by a name pattern and had no caller in the CLI or the services; siblings are found by the catalog row's `(content_type, owner_id)` tuple. RDR-229 P2.7 (nexus-p2m1x.12).
