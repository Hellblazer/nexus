-- Read-only dry count of the data move rdr229-reown-repo-stems-from-curator (vectors-067) for ONE tenant.
-- It uses the move template's stemrows/cand/stems/occ/ranked logic, with nothing written: no advisory lock, no INSERT,
-- no UPDATE. An owner the move would mint is given a placeholder 'new:<hash8>' and holds no rows yet, as on the
-- move's first chunk.
-- Held means unsuperseded and not in quarantine (superseded_by = '' AND lifecycle_state <> 'quarantine', Sam's P3.8 ruling),
-- for the rows that would move and for the rows that hold a destination tuple. A row's version is the stored model_version, else
-- the one D2's repair will write from the name (substring(name from '__(v[0-9]+)$')), else '': the move may run before D2.
-- Run once per tenant, as a role that can read the tenant's catalog_collections and catalog_owners:
--   BEGIN READ ONLY;
--   SET LOCAL nexus.tenant = '<tenant>';
--   SET LOCAL statement_timeout = '60s';
--   <this query>
--   ROLLBACK;
-- One row per affected collection. Columns:
--   action      'reown' | 'skip' | 'reown_quarantine_sibling'
--   name        the collection
--   owner_id    its owner now
--   dest_owner  the repo owner it would move to ('new:<hash8>' when the move would mint that owner)
--   reason      for a skip: exactly the string the move writes to gc_audit details->>'reason'
-- The summary is: SELECT action, count(*) FROM (<this query>) q GROUP BY action; and owners to mint =
-- count(DISTINCT dest_owner) FILTER (WHERE dest_owner LIKE 'new:%').
WITH stemrows AS (
    SELECT c.name, c.content_type, c.embedding_model,
           COALESCE(NULLIF(c.model_version, ''), substring(c.name from '__(v[0-9]+)$'), '') AS model_version,
           c.owner_id, split_part(c.name, '__', 2) AS stem
      FROM nexus.catalog_collections c
     WHERE c.tenant_id = current_setting('nexus.tenant', true)
       AND c.content_type IN ('code', 'docs', 'rdr') AND c.lifecycle_state <> 'quarantine' AND c.superseded_by = ''
       AND split_part(c.name, '__', 1) = c.content_type
       AND split_part(c.name, '__', 2) ~ '^.+-[0-9a-f]{8}$'
), cand AS (
    SELECT DISTINCT r.stem
      FROM stemrows r
     WHERE EXISTS (SELECT 1 FROM nexus.catalog_owners o
                    WHERE o.tenant_id = current_setting('nexus.tenant', true) AND o.owner_type = 'repo'
                      AND o.repo_hash = right(r.stem, 8))
        OR EXISTS (SELECT 1 FROM stemrows k
                    WHERE k.stem = r.stem AND k.content_type = 'code'
                      AND nexus.catalog_owner_is_curator(current_setting('nexus.tenant', true), k.owner_id))
), stems AS (
    SELECT stem, right(stem, 8) AS hash8 FROM cand
), repo_owner AS (
    SELECT replace(r.tumbler_prefix, '.', '-') AS dest, r.repo_hash
      FROM nexus.catalog_owners r
     WHERE r.tenant_id = current_setting('nexus.tenant', true) AND r.owner_type = 'repo'
       AND r.repo_hash IS NOT NULL AND r.repo_hash <> ''
    UNION
    SELECT 'new:' || s.hash8, s.hash8
      FROM (SELECT DISTINCT hash8 FROM stems) s
     WHERE NOT EXISTS (SELECT 1 FROM nexus.catalog_owners r
                        WHERE r.tenant_id = current_setting('nexus.tenant', true) AND r.repo_hash = s.hash8)
), occ AS (
    SELECT r.name, r.owner_id, w.dest, min(h.name) AS held_by
      FROM stemrows r
      JOIN stems s ON s.stem = r.stem
      JOIN repo_owner w ON w.repo_hash = s.hash8
      JOIN nexus.catalog_collections h
        ON h.tenant_id = current_setting('nexus.tenant', true)
       AND h.owner_id = w.dest
       AND h.content_type = r.content_type AND h.embedding_model = r.embedding_model
       AND COALESCE(NULLIF(h.model_version, ''), substring(h.name from '__(v[0-9]+)$'), '') = r.model_version
       AND h.lifecycle_state <> 'quarantine' AND h.superseded_by = '' AND h.name <> r.name
     WHERE nexus.catalog_owner_is_curator(current_setting('nexus.tenant', true), r.owner_id)
     GROUP BY r.name, r.owner_id, w.dest
), ranked AS (
    SELECT m.name, m.owner_id, m.dest,
           row_number() OVER (PARTITION BY m.dest, m.content_type, m.embedding_model, m.model_version
                              ORDER BY m.name) AS rn,
           first_value(m.name) OVER (PARTITION BY m.dest, m.content_type, m.embedding_model, m.model_version
                                     ORDER BY m.name) AS first_name
      FROM (SELECT r.name, r.owner_id, r.content_type, r.embedding_model, r.model_version, w.dest
              FROM stemrows r
              JOIN stems s ON s.stem = r.stem
              JOIN repo_owner w ON w.repo_hash = s.hash8
             WHERE nexus.catalog_owner_is_curator(current_setting('nexus.tenant', true), r.owner_id)
               AND NOT EXISTS (SELECT 1 FROM occ o WHERE o.name = r.name)) m
), skipped AS (
    SELECT o.name, o.owner_id, o.dest, 'destination tuple held by ' || o.held_by AS reason FROM occ o
    UNION ALL
    SELECT k.name, k.owner_id, k.dest, 'destination tuple claimed in this run by ' || k.first_name
      FROM ranked k WHERE k.rn > 1
), moved AS (
    SELECT k.name, k.owner_id, k.dest FROM ranked k WHERE k.rn = 1
)
SELECT 'reown' AS action, m.name, m.owner_id, m.dest AS dest_owner, NULL::text AS reason FROM moved m
UNION ALL
SELECT 'skip', k.name, k.owner_id, k.dest, k.reason FROM skipped k
UNION ALL
-- as the move's UPDATE: the named sibling of any live stem row of a candidate stem that is not skipped
SELECT 'reown_quarantine_sibling', q.name, q.owner_id, w.dest, NULL
  FROM stemrows o
  JOIN stems s ON s.stem = o.stem
  JOIN repo_owner w ON w.repo_hash = s.hash8
  JOIN nexus.catalog_collections q
    ON q.tenant_id = current_setting('nexus.tenant', true) AND q.lifecycle_state = 'quarantine'
   AND q.name = 'quarantine-' || o.name
   AND nexus.catalog_owner_is_curator(q.tenant_id, q.owner_id)
   AND q.owner_id IS DISTINCT FROM w.dest
 WHERE NOT EXISTS (SELECT 1 FROM skipped k WHERE k.name = o.name)
ORDER BY 1, 2
