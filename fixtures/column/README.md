# Column lineage fixtures

Invented T-SQL used by `tests/test_column_acceptance.py` and `tests/test_column_messy.py`. One directory per scenario; each is passed as `--source-root`.

- `rename_temp` - `demo.output.result_value` <- `#stage.staged_value` (SELECT INTO with alias rename) <- `demo.source.input_a`.
- `case_choice` - searched CASE choosing `input_b` / `input_a` on `use_b` (value vs condition inputs, branch labels); a CASE without ELSE (implicit NULL).
- `row_selection` - WHERE filter, ON equality join keys and a non-equality ON predicate; the joined lookup table has its own writer (for `--trace-row-selection`).
- `positional_schema` - INSERT without a column list into a declared table with a leading IDENTITY column (positional mapping skips it).
- `positional_no_schema` - the same INSERT with no CREATE TABLE for the target (positional_without_schema gap).
- `separation` - two target columns from same-named columns of two tables; UNION ALL branches; IF/ELSE writers; three-part names `db_one`/`db_two` with identical schema.table.column.
- `dynamic_sql_exec` - `#stage` filled only by `EXEC (@sql)` before a static INSERT reads it (dynamic_sql gap on the path).
- `dynamic_sql_sp_executesql` - the only writer of the target is `EXEC sp_executesql @sql` built from string fragments.
- `ambiguous_writers` - two procedures UPDATE the same column (alias form and plain form) from different sources.
- `cross_database` - `land_db` (USE) -> `stage_db` (USE) -> report folder (needs `--database-map report=report_db`), three-part references, plus a decoy `land_db.demo.staged` with the same name as `stage_db.demo.staged`.
- `cte_chain` - three chained CTEs (rename, arithmetic, rename) feeding an INSERT; WHERE inside the first CTE.
- `derived_table` - subquery in FROM with an alias rename.
- `update_alias` - `UPDATE t SET t.col = s.x FROM target t JOIN source s`; COALESCE with two columns and a literal.
- `merge_writers` - MERGE with `WHEN MATCHED AND ... THEN UPDATE` and `WHEN NOT MATCHED BY TARGET THEN INSERT`.
- `nested_proc` - caller creates `#stage`, EXECs a callee that fills it, then inserts from it into the target.
- `insert_exec` - `INSERT INTO t (cols) EXEC proc` (the existing indexer splits this into an INSERT and an EXEC statement).
- `wildcard_schema` - `SELECT * INTO #copy` from a declared table, then a named column read from `#copy`.
- `wildcard_no_schema` - `SELECT * INTO #copy` from a table with no CREATE TABLE in the files.
- `ambiguous_column` - unqualified column present in both joined tables; a uniquely bindable unqualified column in the same statement; an unknown alias `x.`.
- `scalar_udf` - undefined scalar function `dbo.fn_adjust(s.input_a)`.
- `variable_lineage` - `SET @v = (SELECT MAX(...) FROM ...)`, then `@v` used in an INSERT arithmetic expression.
- `parameter_binding` - procedure parameter used as a value, called with a named literal and a positional literal; a second parameter with no caller.
- `cycle` - alpha -> beta -> alpha writers, with a seed source into alpha.
- `quoted_identifiers` - bracketed, double-quoted and mixed-case names for the same objects, columns and `#temp` table.
- `comments_strings` - line/block comments and a string literal containing SQL that mentions the target and a decoy table.
- `parse_failure` - writers that sqlglot cannot parse: `OPTION (USE HINT (...))` on an INSERT and `UPDATE ... OUTPUT ... INTO`.
- `dynamic/` - dynamic SQL evaluated statically (`tests/test_column_dynamic_eval.py`), one root per case:
  `literal`, `concat` (+ and CONCAT with literal variables), `if_else` (IF/ELSE choosing target and source;
  assignment and EXEC in the same branch), `quotename` (QUOTENAME, REPLACE, CHAR, CAST), `parameter`
  (literal default and literal callers; a procedure with no caller), `unknown_select` (value from a query),
  `loop` (built in a WHILE), `too_many` (32 alternatives), `multi_statement` (SELECT INTO #temp then INSERT
  in one string), `nested` (EXEC inside the string), `batch_variable` (a variable read inside the string;
  declared-length truncation).
- `ssis/` - invented SSIS packages for `tests/test_column_ssis.py`; `ssis/sql/tables.sql` (`USE demo_db`) declares
  their tables and each other folder is one `--package-root`: `basic` (acceptance: flat file `file_a.csv` ->
  Data Conversion -> OLE DB destination `demo.stage.amount`; `unmapped_column` not mapped; precedence expression),
  `split` (Conditional Split filter), `derived` (conditional Derived Column, in-place replacement), `lookup`
  (Lookup with a reference query), `query` (SqlCommand source; connection without Initial Catalog), `variable`
  (SqlCommandFromVariable with an expression), `copies/dev` + `copies/prod` (two copies of one package),
  `unsupported` (Script Component on the path), `name_only` (names match, metadata maps elsewhere), `legacy`
  (SSIS 2008 format, integer ids, DTSID precedence), `exec_sql` (Execute SQL INSERT with a `?` bound to a
  result-set variable), `union` (Union All + Aggregate), `project` (project `.conmgr` connections, one with a
  parameterised connection string; Foreach file loop; Derived Column, Conditional Split, Multicast; OLE DB
  Command and Row Count off the destination path; parent package with an Execute Package task).
- `ssis_review/` - invented cases from the adversarial review of the SSIS adapter, for
  `tests/test_column_ssis_review.py` (one folder per case; `ssis_review/sql/tables.sql` declares
  `rev_db.rv.*` and `other_db.rv.tgt`): error outputs of in-place Derived Columns, container-scoped
  variables, cache Lookups, ADO.NET quoted names, unreadable names, package configurations, Script
  Component setters, run-time destination tables, run-time values, disabled data flows and tasks,
  parameter binding by name, overlapping roots, split/aggregate row selection, deep expressions,
  unsupported components, duplicated lineage ids.
