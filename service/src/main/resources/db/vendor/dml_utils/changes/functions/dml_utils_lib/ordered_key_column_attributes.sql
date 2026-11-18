CREATE OR REPLACE FUNCTION dml_utils_lib.ordered_key_column_attributes(
    i_index_oid oid,
    i_schema_name text,
    i_table_name text,
    i_index_name text
)
    RETURNS TABLE
            (
                ordinality  integer,
                column_name name,
                column_oid  oid,
                column_type pg_catalog.regtype,
                key_kind    text
            )
    LANGUAGE plpgsql
    STABLE
    SECURITY INVOKER
AS
$$
DECLARE
    l_index    record;
    l_btree    oid;
    l_key      record;
    l_key_kind text;
BEGIN
    SELECT i.indrelid,
           i.indnkeyatts,
           i.indkey,
           i.indclass,
           i.indcollation
    INTO l_index
    FROM pg_catalog.pg_index AS i
    WHERE i.indexrelid = i_index_oid;

    IF NOT FOUND THEN
        RAISE EXCEPTION 'index %.% does not exist', i_schema_name, i_index_name
            USING ERRCODE = '42704';
    END IF;

    SELECT oid INTO l_btree FROM pg_catalog.pg_am WHERE amname = 'btree';

    -- indkey/indclass/indcollation hold one entry per key column; limiting to
    -- indnkeyatts deliberately ignores INCLUDE columns, which do not take part
    -- in the key order.
    FOR l_key IN
        SELECT k.key_position::integer        AS key_ordinality,
               a.attname                      AS key_column_name,
               a.atttypid                     AS key_column_oid,
               a.atttypid::pg_catalog.regtype AS key_column_type,
               a.attnotnull                   AS key_column_not_null,
               a.attcollation                 AS key_column_collation,
               oc.opclass_oid                 AS key_opclass_oid,
               co.collation_oid               AS key_index_collation
        FROM pg_catalog.unnest(l_index.indkey) WITH ORDINALITY
                 AS k(attnum, key_position)
                 JOIN pg_catalog.pg_attribute AS a
                      ON a.attrelid = l_index.indrelid AND a.attnum = k.attnum
                 JOIN pg_catalog.unnest(l_index.indclass) WITH ORDINALITY
            AS oc(opclass_oid, key_position)
                      ON oc.key_position = k.key_position
                 JOIN pg_catalog.unnest(l_index.indcollation) WITH ORDINALITY
            AS co(collation_oid, key_position)
                      ON co.key_position = k.key_position
        WHERE k.key_position <= l_index.indnkeyatts
        ORDER BY k.key_position
        LOOP
        -- The integer family collapses to bigint, as it does for a primary
        -- key; the other supported types map one-to-one.
            l_key_kind := CASE
                              WHEN l_key.key_column_oid IN ('smallint'::pg_catalog.regtype,
                                                            'integer'::pg_catalog.regtype,
                                                            'bigint'::pg_catalog.regtype)
                                  THEN 'bigint'
                              WHEN l_key.key_column_oid = 'text'::pg_catalog.regtype
                                  THEN 'text'
                              WHEN l_key.key_column_oid = 'uuid'::pg_catalog.regtype
                                  THEN 'uuid'
                              WHEN l_key.key_column_oid = 'timestamptz'::pg_catalog.regtype
                                  THEN 'timestamptz'
                              WHEN l_key.key_column_oid = 'bytea'::pg_catalog.regtype
                                  THEN 'bytea'
                END;

            IF l_key_kind IS NULL THEN
                RAISE EXCEPTION 'ordered-key column %.%.% has unsupported type %',
                    i_schema_name, i_table_name, l_key.key_column_name,
                    l_key.key_column_type
                    USING ERRCODE = '22023';
            END IF;

            -- A unique btree index does not make a nullable key safe: NULLs are
            -- distinct by default, so a range predicate cannot order them. The
            -- NULLS FIRST/LAST rejection covers the index option; this covers the
            -- column itself.
            IF NOT l_key.key_column_not_null THEN
                RAISE EXCEPTION 'ordered-key column %.%.% must be NOT NULL',
                    i_schema_name, i_table_name, l_key.key_column_name
                    USING ERRCODE = '0A000';
            END IF;

            -- The predicate is rendered with the type's default operator, so the
            -- index must use that type's default btree operator class. A
            -- non-default opclass (for example text_pattern_ops) would order
            -- differently from the comparison the renderer emits.
            IF NOT EXISTS (SELECT 1
                           FROM pg_catalog.pg_opclass AS o
                           WHERE o.oid = l_key.key_opclass_oid
                             AND o.opcdefault
                             AND o.opcmethod = l_btree
                             AND o.opcintype = l_key.key_column_oid)
            THEN
                RAISE EXCEPTION 'ordered-key column %.%.% must use its default btree operator class',
                    i_schema_name, i_table_name, l_key.key_column_name
                    USING ERRCODE = '0A000';
            END IF;

            -- The rendered predicate compares the column with its own collation;
            -- an index built under a different one would not match the order.
            IF l_key.key_index_collation IS DISTINCT FROM l_key.key_column_collation THEN
                RAISE EXCEPTION 'ordered-key column %.%.% must use its column collation',
                    i_schema_name, i_table_name, l_key.key_column_name
                    USING ERRCODE = '0A000';
            END IF;

            ordinality := l_key.key_ordinality;
            column_name := l_key.key_column_name;
            column_oid := l_key.key_column_oid;
            column_type := l_key.key_column_type;
            key_kind := l_key_kind;
            RETURN NEXT;
        END LOOP;
END;
$$;

COMMENT ON FUNCTION dml_utils_lib.ordered_key_column_attributes(
    oid,
    text,
    text,
    text
    ) IS
    'Resolves the key columns of an index already accepted by '
        'assert_ordered_key_index_usable: each column''s type must collapse to a '
        'supported kind, be NOT NULL, use its default btree operator class and '
        'keep its column collation. Returns the key columns in index order.';
