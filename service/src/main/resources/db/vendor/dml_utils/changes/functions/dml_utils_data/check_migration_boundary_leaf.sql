CREATE OR REPLACE FUNCTION dml_utils_data.check_migration_boundary_leaf()
    RETURNS trigger
    LANGUAGE plpgsql
    SECURITY INVOKER
AS
$$
DECLARE
    l_partition_wise boolean;
BEGIN
    -- A processing update (claim, complete, cancel) leaves the run and the leaf
    -- unchanged, and migration_boundary_immutable rejects a change to either, so
    -- the row was already validated on insert. Skip the catalog lookup on that
    -- hot path; a caller that disabled the immutable trigger still gets the
    -- checks below.
    IF TG_OP = 'UPDATE'
        AND NEW.run_id IS NOT DISTINCT FROM OLD.run_id
        AND NEW.leaf_ordinal IS NOT DISTINCT FROM OLD.leaf_ordinal
    THEN
        RETURN NEW;
    END IF;

    SELECT run.partition_wise
    INTO l_partition_wise
    FROM dml_utils_data.migration_run AS run
    WHERE run.run_id = NEW.run_id;

    IF NOT FOUND THEN
        RAISE EXCEPTION 'run % does not exist', NEW.run_id
            USING ERRCODE = '23503';
    END IF;

    IF l_partition_wise AND NEW.leaf_ordinal IS NULL THEN
        RAISE EXCEPTION 'a partition-wise boundary requires leaf_ordinal'
            USING ERRCODE = '22023';
    END IF;
    IF NOT l_partition_wise AND NEW.leaf_ordinal IS NOT NULL THEN
        RAISE EXCEPTION 'a single-relation boundary must not have leaf_ordinal'
            USING ERRCODE = '22023';
    END IF;

    RETURN NEW;
END;
$$;

COMMENT ON FUNCTION dml_utils_data.check_migration_boundary_leaf IS
    'BEFORE INSERT OR UPDATE trigger that requires leaf_ordinal for a '
        'partition-wise run boundary and rejects it for a single-relation run; '
        'the composite foreign key resolves the ordinal to the leaf manifest. On '
        'update it skips the lookup when the run and leaf are unchanged.';
