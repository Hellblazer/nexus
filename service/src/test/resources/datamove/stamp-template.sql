UPDATE <driving_table> SET n = t.n + 1, touched_by = current_setting('nexus.tenant', true) WHERE <chunking_clause>
