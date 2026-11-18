# Stored functions

One function per `.sql` file, grouped by the schema that owns it:

- `ddl_utils/` — lock-settings tables/accessors and the lock-aware DDL wrappers.
- `ddl_utils_lib/` — generic DDL helpers that take the lock settings explicitly.
  `alter_table` is the internal runner they build on (the only dynamic-SQL
  boundary); prefer the structured helpers, which assemble the fragment from
  validated identifiers. A helper whose statement takes only `SHARE UPDATE
  EXCLUSIVE` and therefore never blocks DML lives here only (`validate_constraint`
  and `has_top_level_comma`).

`changes/functions.xml` loads them one `createProcedure` per `runOnChange`
changeset; the matching drop lives in `changes/functions-rollback/`. Every
routine is `SECURITY INVOKER` except the setters/clearers, which are
`SECURITY DEFINER` (callers only have `SELECT` on the settings tables).

Each routine file ends with a `COMMENT ON FUNCTION` / `COMMENT ON PROCEDURE`
for the routine it creates, using the short form (`schema.name`, no argument
list). If a routine is ever overloaded, include its argument types so the
comment targets the right overload.

The three lock settings are `ddl_lock_timeout` (ms before a lock attempt gives
up), `sleep_time` (ms between retries) and `statement_duration` (ms budget for
acquiring the lock). The `ddl_utils.lock_settings` composite type holds the same
three fields; the lock-aware wrappers read `get_lock_settings` into it.

## `ddl_utils`

### `ddl_utils.get_database_lock_settings()`

```sql
RETURNS TABLE
(ddl_lock_timeout integer, sleep_time integer, statement_duration integer)
```

`STABLE`, `SECURITY INVOKER`. Returns the singleton database defaults (`database_lock_settings.id = 1`); raises `P0002`
when the row is missing.

### `ddl_utils.set_database_lock_settings(i_ddl_lock_timeout, i_sleep_time, i_statement_duration)`

```sql
i_ddl_lock_timeout   ddl_utils.non_negative_integer
i_sleep_time         ddl_utils.non_negative_integer
i_statement_duration ddl_utils.non_negative_integer
RETURNS void
```

`SECURITY DEFINER`. Overwrites the singleton row; raises `P0002` when it is
missing. Last writer wins.

### `ddl_utils.get_schema_lock_settings(i_schema_name)`

```sql
i_schema_name ddl_utils.non_null_text
RETURNS TABLE
(ddl_lock_timeout integer, sleep_time integer, statement_duration integer)
```

`STABLE`, `SECURITY INVOKER`. Returns the schema's row, or no row when there is
none.

### `ddl_utils.set_schema_lock_settings(i_schema_name, i_ddl_lock_timeout, i_sleep_time, i_statement_duration)`

```sql
i_schema_name        ddl_utils.non_null_text
i_ddl_lock_timeout   ddl_utils.non_negative_integer
i_sleep_time         ddl_utils.non_negative_integer
i_statement_duration ddl_utils.non_negative_integer
RETURNS void
```

`SECURITY DEFINER`. Upserts the schema's settings. The schema name is stored as
given (not resolved against `pg_namespace`), so settings can be provisioned
before the schema exists; a typo simply never matches.

### `ddl_utils.clear_schema_lock_settings(i_schema_name)`

```sql
i_schema_name ddl_utils.non_null_text
RETURNS void
```

`SECURITY DEFINER`. Deletes the schema's row; a no-op when there is none.

### `ddl_utils.get_table_lock_settings(i_schema_name, i_table_name)`

```sql
i_schema_name ddl_utils.non_null_text
i_table_name  ddl_utils.non_null_text
RETURNS TABLE
(ddl_lock_timeout integer, sleep_time integer, statement_duration integer)
```

`STABLE`, `SECURITY INVOKER`. Returns the table's row, or no row when there is
none.

###

`ddl_utils.set_table_lock_settings(i_schema_name, i_table_name, i_ddl_lock_timeout, i_sleep_time, i_statement_duration)`

```sql
i_schema_name        ddl_utils.non_null_text
i_table_name         ddl_utils.non_null_text
i_ddl_lock_timeout   ddl_utils.non_negative_integer
i_sleep_time         ddl_utils.non_negative_integer
i_statement_duration ddl_utils.non_negative_integer
RETURNS void
```

`SECURITY DEFINER`. Upserts the table's settings. The key is stored as given (not resolved against `pg_class`);
concurrent upserts on the same key can still
deadlock, so callers that need to survive that must retry.

### `ddl_utils.clear_table_lock_settings(i_schema_name, i_table_name)`

```sql
i_schema_name ddl_utils.non_null_text
i_table_name  ddl_utils.non_null_text
RETURNS void
```

`SECURITY DEFINER`. Deletes the table's row; a no-op when there is none.

### `ddl_utils.get_lock_settings(i_schema_name, i_table_name)`

```sql
i_schema_name ddl_utils.non_null_text
i_table_name  ddl_utils.non_null_text
RETURNS TABLE
(ddl_lock_timeout integer, sleep_time integer, statement_duration integer)
```

`STABLE`, `SECURITY INVOKER`. Resolves the effective settings for a table,
falling back table → schema → database. Raises `P0002` when nothing matches.

### `ddl_utils.set_updated_at()`

```sql
RETURNS trigger
```

`SECURITY INVOKER`, not callable by users (PUBLIC `EXECUTE` is revoked and it is
granted to no one). The shared `BEFORE UPDATE ... FOR EACH ROW` trigger function
that stamps `NEW.updated_at := now()` on every table, so no caller can bypass
it. Attach it to each table with a trigger named
`<table>_set_updated_at`; see `changes/triggers/ddl_utils/`.

### `ddl_utils.add_columns(i_schema_name, i_table_name, i_column_names, i_column_types, i_default_values, i_nullable)`

```sql
i_schema_name   ddl_utils.non_null_text
i_table_name    ddl_utils.non_null_text
i_column_names  ddl_utils.non_empty_non_null_text_array
i_column_types  ddl_utils.non_empty_non_null_text_array
i_default_values ddl_utils.non_empty_text_array
i_nullable      ddl_utils.non_empty_non_null_boolean_array
RETURNS void
```

`SECURITY INVOKER`. Lock-aware wrapper: resolves the settings via
`ddl_utils.get_lock_settings` and delegates to `ddl_utils_lib.add_columns`.

### `ddl_utils.add_column(i_schema_name, i_table_name, i_column_name, i_column_type, i_nullable [, i_default_value])`

```sql
i_schema_name   ddl_utils.non_null_text
i_table_name    ddl_utils.non_null_text
i_column_name   ddl_utils.non_null_text
i_column_type   ddl_utils.non_null_text
i_nullable      ddl_utils.non_null_boolean
i_default_value text DEFAULT NULL
RETURNS void
```

`SECURITY INVOKER`. Single-column lock-aware wrapper; delegates to
`ddl_utils.add_columns`. Omit `i_default_value` to add a column with no default.

### `ddl_utils.drop_column(i_schema_name, i_table_name, i_column_name)`

```sql
i_schema_name ddl_utils.non_null_text
i_table_name  ddl_utils.non_null_text
i_column_name ddl_utils.non_null_text
RETURNS void
```

`SECURITY INVOKER`. Single-column convenience over `ddl_utils.drop_columns`.

### `ddl_utils.drop_columns(i_schema_name, i_table_name, i_column_names)`

```sql
i_schema_name  ddl_utils.non_null_text
i_table_name   ddl_utils.non_null_text
i_column_names ddl_utils.non_empty_non_null_text_array
RETURNS void
```

`SECURITY INVOKER`. Lock-aware wrapper; resolves the table's settings via
`get_lock_settings` and delegates to `ddl_utils_lib.drop_columns`.

### `ddl_utils.rename_column(i_schema_name, i_table_name, i_column_name, i_new_column_name)`

```sql
i_schema_name     ddl_utils.non_null_text
i_table_name      ddl_utils.non_null_text
i_column_name     ddl_utils.non_null_text
i_new_column_name ddl_utils.non_null_text
RETURNS void
```

`SECURITY INVOKER`. Lock-aware wrapper; resolves the table's settings via
`get_lock_settings` and delegates to `ddl_utils_lib.rename_column`.

### Column attributes

`ddl_utils.set_column_default`, `drop_column_default`, `drop_not_null`,
`set_not_null`, `set_column_storage`, `set_column_compression`,
`drop_expression`, `add_identity` and `drop_identity` are lock-aware wrappers
over the matching `ddl_utils_lib` helpers (same names).
All are `SECURITY INVOKER` and resolve the settings via `get_lock_settings`.
All are metadata-only (no scan or rewrite) except `set_not_null`, which
ordinarily scans the table under `ACCESS EXCLUSIVE` unless a valid `CHECK`
constraint proves the column non-null; the `ddl_utils.set_not_null` procedure
composes that scan-avoiding sequence.

### Constraints and tables

`ddl_utils.add_check_constraint`, `add_foreign_key`, `drop_constraint`,
`rename_constraint`, `add_primary_key_using_index`,
`add_unique_constraint_using_index` and `rename_table` are lock-aware wrappers
over the matching `ddl_utils_lib` helpers (same names). `add_check_constraint`
and `add_foreign_key` emit `NOT VALID` (metadata-only); call
`ddl_utils_lib.validate_constraint` separately to enforce the constraint
against existing rows (it scans but takes only `SHARE UPDATE EXCLUSIVE`, so it
has no lock-aware wrapper). The two `*_using_index` helpers attach a pre-built
unique index (`CREATE UNIQUE INDEX CONCURRENTLY`), which is metadata-only when
the index is valid. `ddl_utils.rename_table` also moves the table's lock
settings to the new name in one statement (`ddl_utils.move_table_lock_settings`);
an existing row for the destination is replaced with a `NOTICE`.

## `ddl_utils_lib`

###

`ddl_utils_lib.alter_table(i_schema_name, i_table_name, i_alter_table_fragment, i_ddl_lock_timeout, i_sleep_time, i_statement_duration)`

```sql
i_schema_name           ddl_utils.non_null_text
i_table_name            ddl_utils.non_null_text
i_alter_table_fragment  ddl_utils.non_null_text
i_ddl_lock_timeout      ddl_utils.non_negative_integer
i_sleep_time            ddl_utils.non_negative_integer
i_statement_duration    ddl_utils.non_negative_integer
RETURNS void
```

**Internal runner.** `SECURITY INVOKER`. Runs
`ALTER TABLE <schema>.<table> <fragment>` with the given lock/retry settings,
restoring the caller's `lock_timeout` on success. Rejects a blank fragment and
one containing `;`, `--`, `/*` or `*/` outside quoted text (a best-effort
guard; the caller already holds the privileges the fragment uses). This is the
only routine that executes dynamic SQL; prefer the structured helpers, which
build the fragment from validated identifiers/values.

### `ddl_utils_lib.has_unquoted_token(i_value, i_tokens, i_top_level_only)`

```sql
i_value          text
i_tokens         ddl_utils.non_empty_non_null_text_array
i_top_level_only ddl_utils.non_null_boolean
RETURNS boolean
```

`IMMUTABLE`, `SECURITY INVOKER`. True when the value contains one of the tokens
in code (outside parentheses-nested groups, strings, quoted identifiers,
dollar-quoted literals and comments), optionally only at bracket depth 0. The
scanner is shared by `alter_table`'s statement-separator guard and
`has_top_level_comma`. A NULL value returns false.

### `ddl_utils_lib.has_top_level_comma(i_value)`

```sql
i_value text
RETURNS boolean
```

`IMMUTABLE`, `SECURITY INVOKER`. True when the value contains a comma outside
parentheses, brackets or a string literal. Used to reject defaults that could
append DDL clauses.

### `ddl_utils_lib.assert_one_based(i_values, i_context)`

```sql
i_values  anyarray
i_context ddl_utils.non_null_text
RETURNS void
```

`STABLE`, `SECURITY INVOKER`. Raises `22023` when the array is NULL, empty, not
one-dimensional, or does not start at subscript 1. Used by the helpers whose
loops assume a 1-based array. It raises, so it is not `IMMUTABLE` (a constant
call must not be folded at plan time for an unreachable branch).

### `ddl_utils_lib.assert_non_blank_elements(i_values, i_context, i_label)`

```sql
i_values  ddl_utils.non_empty_text_array
i_context ddl_utils.non_null_text
i_label   ddl_utils.non_null_text
RETURNS void
```

`STABLE`, `SECURITY INVOKER`. Raises `22023` when the array is not 1-based or
a non-null element is blank (whitespace-only, using the same trim set as the
`non_null_text` domain). NULL elements are allowed and skipped (a NULL default
means no `DEFAULT` clause). Used to validate the array arguments of
`add_columns`, `add_foreign_key` and `drop_columns`.

### `ddl_utils_lib.quote_identifiers(i_values [, i_prefix])`

```sql
i_values ddl_utils.non_empty_non_null_text_array
i_prefix text DEFAULT ''
RETURNS text
```

`IMMUTABLE`, `SECURITY INVOKER`. Joins the identifiers into a comma-separated
list, each quoted with `%I` and optionally prefixed with a literal SQL clause (for example `'DROP COLUMN '`). Used to
build the identifier lists in
`add_foreign_key` and `drop_columns`.

### `ddl_utils_lib.assert_no_top_level_comma(i_value, i_context)`

```sql
i_value   text
i_context ddl_utils.non_null_text
RETURNS void
```

`IMMUTABLE`, `SECURITY INVOKER`. Raises `22023` when the value contains a
top-level comma (one that could append another `ALTER TABLE` action). A NULL
value means "no `DEFAULT` clause" and returns without error. Used by
`add_check_constraint`, `add_columns` and `set_column_default`.

### `ddl_utils_lib.assert_allowed_keyword(i_value, i_allowed, i_context)`

```sql
i_value   ddl_utils.non_null_text
i_allowed text[]
i_context ddl_utils.non_null_text
RETURNS void
```

`IMMUTABLE`, `SECURITY INVOKER`. Raises `22023` when the value is not one of the
lower-case keyword allow-list (the comparison is case-insensitive). Used by
`add_identity`, `set_column_compression` and `set_column_storage`.

### `ddl_utils_lib.assert_equal_cardinality(i_a, i_b, i_context)`

```sql
i_a       anyarray
i_b       anyarray
i_context ddl_utils.non_null_text
RETURNS void
```

`IMMUTABLE`, `SECURITY INVOKER`. Raises `22023` when the two arrays have
different lengths (a NULL array counts as unequal). Used by `add_foreign_key`
and the `ensure_foreign_key` procedure.

###

`ddl_utils_lib.add_columns(i_schema_name, i_table_name, i_column_names, i_column_types, i_default_values, i_nullable, i_ddl_lock_timeout, i_sleep_time, i_statement_duration)`

```sql
i_schema_name        ddl_utils.non_null_text
i_table_name         ddl_utils.non_null_text
i_column_names       ddl_utils.non_empty_non_null_text_array
i_column_types       ddl_utils.non_empty_non_null_text_array
i_default_values     ddl_utils.non_empty_text_array
i_nullable           ddl_utils.non_empty_non_null_boolean_array
i_ddl_lock_timeout   ddl_utils.non_negative_integer
i_sleep_time         ddl_utils.non_negative_integer
i_statement_duration ddl_utils.non_negative_integer
RETURNS void
```

`SECURITY INVOKER`. Builds one `ADD COLUMN` clause per element and applies them
in a single `ALTER TABLE` (all-or-nothing) via `alter_table`. Validates array
lengths, blank names/types, that each type resolves to a single SQL type (`to_regtype`), and that no default has a
top-level comma.

###

`ddl_utils_lib.add_column(i_schema_name, i_table_name, i_column_name, i_column_type, i_nullable, i_default_value, i_ddl_lock_timeout, i_sleep_time, i_statement_duration)`

```sql
i_schema_name        ddl_utils.non_null_text
i_table_name         ddl_utils.non_null_text
i_column_name        ddl_utils.non_null_text
i_column_type        ddl_utils.non_null_text
i_nullable           ddl_utils.non_null_boolean
i_default_value      text
i_ddl_lock_timeout   ddl_utils.non_negative_integer
i_sleep_time         ddl_utils.non_negative_integer
i_statement_duration ddl_utils.non_negative_integer
RETURNS void
```

`SECURITY INVOKER`. Single-column convenience over `ddl_utils_lib.add_columns`
with explicit lock settings. A NULL `i_default_value` means no `DEFAULT` clause.

###

`ddl_utils_lib.drop_column(i_schema_name, i_table_name, i_column_name, i_ddl_lock_timeout, i_sleep_time, i_statement_duration)`

```sql
i_schema_name        ddl_utils.non_null_text
i_table_name         ddl_utils.non_null_text
i_column_name        ddl_utils.non_null_text
i_ddl_lock_timeout   ddl_utils.non_negative_integer
i_sleep_time         ddl_utils.non_negative_integer
i_statement_duration ddl_utils.non_negative_integer
RETURNS void
```

`SECURITY INVOKER`. Single-column convenience over `ddl_utils_lib.drop_columns`.

###

`ddl_utils_lib.drop_columns(i_schema_name, i_table_name, i_column_names, i_ddl_lock_timeout, i_sleep_time, i_statement_duration)`

```sql
i_schema_name        ddl_utils.non_null_text
i_table_name         ddl_utils.non_null_text
i_column_names       ddl_utils.non_empty_non_null_text_array
i_ddl_lock_timeout   ddl_utils.non_negative_integer
i_sleep_time         ddl_utils.non_negative_integer
i_statement_duration ddl_utils.non_negative_integer
RETURNS void
```

`SECURITY INVOKER`. Builds one `DROP COLUMN` clause per element and applies them
in a single `ALTER TABLE` (all-or-nothing) via `ddl_utils_lib.alter_table`. Each
name is quoted with `%I`; a blank element is rejected with `22023`.

###

`ddl_utils_lib.rename_column(i_schema_name, i_table_name, i_column_name, i_new_column_name, i_ddl_lock_timeout, i_sleep_time, i_statement_duration)`

```sql
i_schema_name        ddl_utils.non_null_text
i_table_name         ddl_utils.non_null_text
i_column_name        ddl_utils.non_null_text
i_new_column_name    ddl_utils.non_null_text
i_ddl_lock_timeout   ddl_utils.non_negative_integer
i_sleep_time         ddl_utils.non_negative_integer
i_statement_duration ddl_utils.non_negative_integer
RETURNS void
```

`SECURITY INVOKER`. Renames one column; both names are quoted with `%I` and
applied through `ddl_utils_lib.alter_table` with explicit lock settings.

The routines below all end in the same three lock settings (`i_ddl_lock_timeout`, `i_sleep_time`,
`i_statement_duration`, all
`ddl_utils.non_negative_integer`); they are shown once here rather than repeated.

### `ddl_utils_lib.set_column_default(i_schema_name, i_table_name, i_column_name, i_default_value, settings...)`

```sql
i_schema_name        ddl_utils.non_null_text
i_table_name         ddl_utils.non_null_text
i_column_name        ddl_utils.non_null_text
i_default_value      ddl_utils.non_null_text
i_ddl_lock_timeout   ddl_utils.non_negative_integer
i_sleep_time         ddl_utils.non_negative_integer
i_statement_duration ddl_utils.non_negative_integer
RETURNS void
```

`SECURITY INVOKER`. Sets a column default. `i_default_value` is raw SQL (quoted
literals must include their quotes); a top-level comma is rejected with `22023`.

### `ddl_utils_lib.drop_column_default(i_schema_name, i_table_name, i_column_name, settings...)`

```sql
i_schema_name        ddl_utils.non_null_text
i_table_name         ddl_utils.non_null_text
i_column_name        ddl_utils.non_null_text
settings             (see above)
RETURNS void
```

`SECURITY INVOKER`. Drops a column default; metadata-only.

### `ddl_utils_lib.drop_not_null(i_schema_name, i_table_name, i_column_name, settings...)`

```sql
i_schema_name        ddl_utils.non_null_text
i_table_name         ddl_utils.non_null_text
i_column_name        ddl_utils.non_null_text
settings             (see above)
RETURNS void
```

`SECURITY INVOKER`. Drops `NOT NULL`; metadata-only.

### `ddl_utils_lib.set_not_null(i_schema_name, i_table_name, i_column_name, settings...)`

```sql
i_schema_name        ddl_utils.non_null_text
i_table_name         ddl_utils.non_null_text
i_column_name        ddl_utils.non_null_text
settings             (see above)
RETURNS void
```

`SECURITY INVOKER`. Sets `NOT NULL` in a single `ALTER TABLE`. This is **not**
metadata-only: unless a valid `CHECK` constraint already proves the column
non-null, PostgreSQL scans the table under `ACCESS EXCLUSIVE`. The
`ddl_utils.set_not_null` procedure is the scan-avoiding, resumable form.

### `ddl_utils_lib.set_column_storage(i_schema_name, i_table_name, i_column_name, i_storage, settings...)`

```sql
i_schema_name        ddl_utils.non_null_text
i_table_name         ddl_utils.non_null_text
i_column_name        ddl_utils.non_null_text
i_storage            ddl_utils.non_null_text
settings             (see above)
RETURNS void
```

`SECURITY INVOKER`. Sets a column's storage (`PLAIN`, `EXTERNAL`, `EXTENDED`,
`MAIN`); an unknown keyword is rejected with `22023`. Affects future writes only.
The helper does not accept PostgreSQL's `DEFAULT` keyword (it would require
resolving the type's default storage, which belongs in a later operation).

### `ddl_utils_lib.set_column_compression(i_schema_name, i_table_name, i_column_name, i_compression, settings...)`

```sql
i_schema_name        ddl_utils.non_null_text
i_table_name         ddl_utils.non_null_text
i_column_name        ddl_utils.non_null_text
i_compression        ddl_utils.non_null_text
settings             (see above)
RETURNS void
```

`SECURITY INVOKER`. Sets a column's compression (`pglz`, `lz4`, `default`,
case-insensitive); an unknown keyword is rejected with `22023`. Affects future
writes only.

### `ddl_utils_lib.drop_expression(i_schema_name, i_table_name, i_column_name, settings...)`

```sql
i_schema_name        ddl_utils.non_null_text
i_table_name         ddl_utils.non_null_text
i_column_name        ddl_utils.non_null_text
settings             (see above)
RETURNS void
```

`SECURITY INVOKER`. Turns a generated column into a plain base column; a column
that is not a stored generated column raises `55000`.

### `ddl_utils_lib.add_identity(i_schema_name, i_table_name, i_column_name, i_generated, settings...)`

```sql
i_schema_name        ddl_utils.non_null_text
i_table_name         ddl_utils.non_null_text
i_column_name        ddl_utils.non_null_text
i_generated          ddl_utils.non_null_text
settings             (see above)
RETURNS void
```

`SECURITY INVOKER`. Adds an identity (`ALWAYS` or `BY DEFAULT`); an invalid mode
is rejected with `22023`.

### `ddl_utils_lib.drop_identity(i_schema_name, i_table_name, i_column_name, i_if_exists, settings...)`

```sql
i_schema_name        ddl_utils.non_null_text
i_table_name         ddl_utils.non_null_text
i_column_name        ddl_utils.non_null_text
i_if_exists          ddl_utils.non_null_boolean
settings             (see above)
RETURNS void
```

`SECURITY INVOKER`. Drops an identity; `i_if_exists` controls `IF EXISTS`.

###

`ddl_utils_lib.add_check_constraint(i_schema_name, i_table_name, i_constraint_name, i_check_expression, settings...)`

```sql
i_schema_name        ddl_utils.non_null_text
i_table_name         ddl_utils.non_null_text
i_constraint_name    ddl_utils.non_null_text
i_check_expression   ddl_utils.non_null_text
settings             (see above)
RETURNS void
```

`SECURITY INVOKER`. Adds a `CHECK` constraint as `NOT VALID` (metadata-only).
The expression is raw SQL; a top-level comma is rejected with `22023`.

###

`ddl_utils_lib.add_foreign_key(i_schema_name, i_table_name, i_constraint_name, i_column_names, i_referenced_schema_name, i_referenced_table_name, i_referenced_column_names, settings...)`

```sql
i_schema_name              ddl_utils.non_null_text
i_table_name               ddl_utils.non_null_text
i_constraint_name          ddl_utils.non_null_text
i_column_names             ddl_utils.non_empty_non_null_text_array
i_referenced_schema_name   ddl_utils.non_null_text
i_referenced_table_name    ddl_utils.non_null_text
i_referenced_column_names  ddl_utils.non_empty_non_null_text_array
settings                   (see above)
RETURNS void
```

`SECURITY INVOKER`. Adds a foreign key as `NOT VALID` (no scan; takes `SHARE
ROW EXCLUSIVE` on both tables). The two column arrays must be non-empty and of
equal length; blank names are rejected with `22023`.

### `ddl_utils_lib.validate_constraint(i_schema_name, i_table_name, i_constraint_name, settings...)`

```sql
i_schema_name        ddl_utils.non_null_text
i_table_name         ddl_utils.non_null_text
i_constraint_name    ddl_utils.non_null_text
settings             (see above)
RETURNS void
```

`SECURITY INVOKER`. Validates a constraint, scanning existing rows under
`SHARE UPDATE EXCLUSIVE` (does not block concurrent DML). Use it after the
`NOT VALID` helpers.

### `ddl_utils_lib.drop_constraint(i_schema_name, i_table_name, i_constraint_name, settings...)`

```sql
i_schema_name        ddl_utils.non_null_text
i_table_name         ddl_utils.non_null_text
i_constraint_name    ddl_utils.non_null_text
settings             (see above)
RETURNS void
```

`SECURITY INVOKER`. Drops a constraint; metadata-only. It does not use
`IF EXISTS`, so a wrong name fails.

###

`ddl_utils_lib.rename_constraint(i_schema_name, i_table_name, i_constraint_name, i_new_constraint_name, settings...)`

```sql
i_schema_name           ddl_utils.non_null_text
i_table_name            ddl_utils.non_null_text
i_constraint_name       ddl_utils.non_null_text
i_new_constraint_name   ddl_utils.non_null_text
settings                (see above)
RETURNS void
```

`SECURITY INVOKER`. Renames a constraint; metadata-only.

###

`ddl_utils_lib.add_primary_key_using_index(i_schema_name, i_table_name, i_constraint_name, i_index_name, settings...)`

```sql
i_schema_name        ddl_utils.non_null_text
i_table_name         ddl_utils.non_null_text
i_constraint_name    ddl_utils.non_null_text
i_index_name         ddl_utils.non_null_text
settings             (see above)
RETURNS void
```

`SECURITY INVOKER`. Attaches a pre-built unique index as the primary key. Build
the index with `CREATE UNIQUE INDEX CONCURRENTLY` first; the attach is
metadata-only when the index is valid and the columns are already `NOT NULL`.
Validates that the index exists in `i_schema_name`, is a valid, non-partial,
non-expression unique index on the table, and every key column is already
`NOT NULL`; otherwise raises `42704`/`42809`/`55000` before the ALTER.
`i_index_name` must be an unquoted, unqualified identifier, and both names must
be at most 63 bytes.

###

`ddl_utils_lib.add_unique_constraint_using_index(i_schema_name, i_table_name, i_constraint_name, i_index_name, settings...)`

```sql
i_schema_name        ddl_utils.non_null_text
i_table_name         ddl_utils.non_null_text
i_constraint_name    ddl_utils.non_null_text
i_index_name         ddl_utils.non_null_text
settings             (see above)
RETURNS void
```

`SECURITY INVOKER`. Attaches a pre-built unique index as a unique constraint;
metadata-only when the index is valid. Validates that the index exists in
`i_schema_name`, is a valid, non-partial, non-expression unique index on the
table; otherwise raises `42704`/`42809` before the ALTER. `i_index_name` must be
an unquoted, unqualified identifier, and both names must be at most 63 bytes.

### `ddl_utils_lib.rename_table(i_schema_name, i_table_name, i_new_table_name, settings...)`

```sql
i_schema_name        ddl_utils.non_null_text
i_table_name         ddl_utils.non_null_text
i_new_table_name     ddl_utils.non_null_text
settings             (see above)
RETURNS void
```

`SECURITY INVOKER`. Renames a table within its schema; metadata-only. The new
name is unqualified.
