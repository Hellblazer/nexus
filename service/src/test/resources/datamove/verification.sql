SELECT 1 FROM <driving_table> WHERE t.n = 0 AND (t.tenant_id = $1 OR $1 IS NULL)
