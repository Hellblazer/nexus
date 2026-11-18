SELECT 1 WHERE ($1 IS NULL AND coalesce(current_setting('nexus.tenant', true), '') <> '') OR ($1 IS NOT NULL AND coalesce(current_setting('nexus.tenant', true), '') <> $1)
