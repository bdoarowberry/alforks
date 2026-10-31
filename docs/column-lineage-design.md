# Column lineage: design and contract

This document is the contract between the analyzer, tracer, renderers and tests.
The data model lives in `src/column_lineage/model.py`; this file explains its meaning.

## Scope

- Input: local SQL files only. Nothing is executed and no database is contacted.
- One requested column per report. A run may request several columns; parsed
  sources and per-object analysis are shared between them.
- Generic: no database, schema, object, column or folder name appears in code.
  Repository layout is described to the tool through options (`--database-map`).
- The existing table-level tool (`python -m table_lineage.cli`) is unchanged.

## CLI

```
python -m column_lineage --column <object>.<column> [--column ...]
                         --source-root <dir-or-file> [...]
                         [--output <dir>]                  default ./column-lineage-output
                         [--stop-at-database <db> ...]     stop when a column in this database is reached
                         [--stop-at-object <glob> ...]     stop at matching objects (fnmatch on db.schema.name, case-insensitive)
                         [--max-depth <n>]                 stop after n mapping steps from the target
                         [--default-database <db>]         database for two-part names with no other context
                         [--database-map <path-prefix>=<db> ...]  files under this path (relative to a source root,
                                                           or absolute) belong to this database
                         [--default-schema <schema>]       schema for one-part names (default: dbo)
                         [--trace-row-selection]           also trace row-selection columns upstream
                         [--include <glob> ...] [--exclude <glob> ...]
                         [--gap-summary]                   also write gap-summary.json (sanitized, no names)
                         [--fail-on-incomplete]            exit 3 when any requested trace is incomplete
                         [--no-progress] [--verbose]
```

`<object>` may have two, three or four parts (`schema.table`, `db.schema.table`,
`server.db.schema.table`). Bracketed identifiers are accepted. The target object may
be a table, view or `#temp` table (use `--column "#t.col"` only with a single file root).

Outputs per requested column, in `--output`:

- `<stem>.lineage.json` — the versioned trace (schema `column-lineage/1.0`).
- `<stem>.lineage.md` — the review page, regenerated from the JSON model.

`<stem>` is the requested spec lower-cased with brackets removed and characters
outside `[a-z0-9._-]` replaced by `_`. For example `--column Demo.Output.Result_Value`
writes `demo.output.result_value.lineage.json`.

Also written once per run: `scan-diagnostics.json` (files, statement parse statistics,
all scan-level gaps with names) and, with `--gap-summary`, `gap-summary.json`.

Exit codes: `0` traces written (whether complete or not), `2` usage error,
`3` with `--fail-on-incomplete` when any trace is incomplete. A zero exit never
means the lineage is complete; read `completeness.status`.

## Identities

**Objects** (`ObjectRef.key`):

- Persistent: `[server.]database.schema.name`, lower-cased. Unknown parts are `?`,
  e.g. `?.demo.output` when no database context exists.
- Database context for a two-part name, first match wins: explicit three-part name;
  a preceding `USE <db>` in the same file; `--database-map`; `--default-database`;
  otherwise unknown (`database_source` records which rule applied).
- An unknown-database reference is unified with a known-database object only when
  exactly one database defines or writes that `schema.name`; the binding is then
  `inferred`. If several do, an `ambiguous_object` gap lists the candidates.
- `#temp` tables: `temp_table:<scope>::#name`, where scope is the procedure (or batch)
  that creates it. A procedure that reads a `#temp` it does not create resolves it to
  the creating callers (SQL Server visibility rules); each caller is a distinct
  alternative and the binding is `inferred`.
- `@table` variables: `table_variable:<scope>::@name`.
- CTEs and derived tables: `cte:<statement-id>::name`, `derived:<statement-id>::alias`.

**Nodes** (`Node.id`): `<object key>.<column>` for columns; `variable:<scope>::@v`,
`parameter:<scope>::@p`, `constant:<sql literal>`, `unknown:<expression>`.

Columns with the same name in different objects or databases always have different ids.

## Steps

A `Step` says: in this writer statement (and branch), the target column receives the
value of this expression. Writers are:

| writer_kind | statement |
|---|---|
| `insert_select`, `insert_values` | `INSERT [INTO] t [(cols)] SELECT/VALUES` |
| `select_into` | `SELECT ... INTO t` |
| `update` | `UPDATE ... SET col = expr [FROM ...]` |
| `merge_update`, `merge_insert` | `MERGE` WHEN clauses |
| `view` | `CREATE [OR ALTER] VIEW` |
| `cte`, `derived` | a CTE or derived-table column inside one statement |
| `variable_assignment` | `SET @v = ...`, `SELECT @v = ...`, `DECLARE @v = ...` |
| `parameter_binding` | `EXEC proc @p = expr` / positional argument |

`mapping_method` records how the target column was matched to its expression:
`target_list`, `positional_schema` (INSERT without a column list, using the declared
ordered schema minus identity/computed/rowversion columns), `projection_alias`
(SELECT INTO / view / CTE by output name), `view_column_list`, `set_clause`,
`wildcard_schema` (a `*` expanded from a reliable ordered schema), `assignment`, `argument`.

Positional and wildcard mappings require a reliable ordered schema. Without one the
step is not produced and a `positional_without_schema` / `wildcard_without_schema` gap is.

### Inputs and roles

Each column, variable, parameter or literal referenced by the value expression is an
`Input` with a role:

- `value` — contributes to the computed value.
- `condition` — chooses between alternatives (`CASE WHEN` predicates, simple-CASE
  operand, `IIF` predicate, `NULLIF` comparison).
- `window` — `PARTITION BY` / `ORDER BY` of a window function.

`branch` labels where the input sits: `WHEN <predicate>` for a condition,
`WHEN <predicate> THEN` / `ELSE` for CASE results, `COALESCE argument <n>` /
`ISNULL argument <n>` for precedence. A CASE without ELSE records the implicit
`ELSE NULL` as a `constant:NULL` value input.

### Row selection

`RowSelection` items are separate from inputs: `filter` (WHERE), `join_key` (column
equality in ON/USING/MERGE ON), `join_predicate` (other ON predicates), `group_by`,
`having`, `merge_condition`. Row selection is recorded for the statement level at which
it occurs (a CTE's WHERE belongs to the CTE step). Row-selection columns are not traced
upstream unless `--trace-row-selection` is set.

### Branches and multiple writers

- `branch_context` holds enclosing control flow (`IF <cond>`, `ELSE (of IF <cond>)`,
  `WHILE <cond>`, `TRY`, `CATCH`), `UNION ALL branch 2 of 3`, and MERGE `WHEN` clauses.
- Every writer of a column produces its own steps. They are alternatives; none is
  dropped. When writers that overwrite (UPDATE, MERGE UPDATE) exist in different
  procedures, or their order cannot be established, an `ambiguous_writer_order` gap
  names them. INSERT writers are row-additive and are not ambiguous by themselves.
- For `#temp` tables, only writers in the same scope that precede the reading statement
  (or that are in an enclosing loop) are used.

## Status

- `verified` — every value input is bound to a unique identity from explicit SQL.
- `inferred` — bound using a stated rule: positional schema, default schema/database,
  caller-created temp table, unknown-database unification.
- `partial` — some inputs bound, some are gaps.
- `ambiguous` — several candidate bindings or writers, none provable.
- `unresolved` — nothing could be bound.

## Stops and gaps

A path ends at a `Stop` (`boundary`, `source_without_writer`, `constant`,
`parameter_without_caller`, `max_depth`) or at a `Gap`. Gap kinds are listed in
`model.GAP_KINDS`. Never infer a mapping from similar names or from a table-level
dependency.

`completeness.status` is `complete` only if every value path ends at a stop and there
are no gaps. A `source_without_writer` stop means "no writer exists in the selected
scanned sources", not "this is the ultimate source-system field".

`completeness.result` keeps the meanings apart:

- `reached_requested_boundary`: complete, and no value path ends at an object without a writer;
- `complete_within_selected_scope`: complete, but some paths end at objects with no writer in
  the selected sources (other sources, such as packages or feeds, may write them);
- `incomplete`: open value-path or context gaps (for example an unsupported package step).

`completeness.ends_by_meaning` counts value-path ends by meaning (`reached_requested_boundary`,
`no_writer_in_selected_scope`, `constant`, `parameter_without_caller`, `max_depth`,
`unsupported_package_step`). Asserted steps stay `asserted` and `relies_on_assertions` is
reported separately.

Every `source_without_writer` stop on a table or view carries `writer_search`: statements in
the scanned files that look like writers of an object with the same name but gave no step,
each with a category and location — `other_object` (resolves to another database, schema or
server; the detail says where the database context came from), `same_object_column_not_mapped`,
`unparsed`, `ignored_non_lineage`, `parsed_no_writer`, `not_indexed`, `comment_or_string`,
`skipped_file`. The stop's `detail` summarises them. An empty list means the search found no
write of that name in the selected sources. `gap-summary.json` counts the categories
(`totals.no_writer_stops`).

### Dynamic SQL evaluation (`dynamic_eval.py`)

After loading, every `dynamic_sql` statement issue (other than dead dynamic SQL) is evaluated once:

- The executed expression (`EXEC (expr)`, `EXEC sp_executesql expr | @stmt = expr`) is evaluated to
  the set of strings it can take. Evaluated: string and N-string literals, NULL, numbers (as text only
  through CAST/CONVERT), `+`, `CONCAT`, `QUOTENAME`, `REPLACE` (case-insensitive), `CHAR`/`NCHAR`,
  `CAST`/`CONVERT` to a string type (with its length), `ISNULL`/`COALESCE`, `UPPER`/`LOWER`,
  `LTRIM`/`RTRIM`, `LEFT`/`RIGHT`.
- A variable's value at the EXEC is computed from its assignments before it in the same scope (DECLARE,
  SET, SELECT without FROM), in order, using `control.control_regions`: an assignment inside an IF/ELSE
  arm or CATCH that does not enclose the EXEC is conditional, so earlier values survive under the
  negated condition. Arms of the same IF exclude each other, so variables set together stay paired.
  The declared length of the variable truncates the value.
- A procedure parameter takes what each caller in the scanned files passes (evaluated in the caller,
  recursively, at most 8 calls deep), or its default for callers that omit it. With no caller the
  default is the only known value and the evaluation is **open**.
- Anything else makes the evaluation **unknown**: a value from a query, FETCH, UPDATE or OUTPUT; an
  assignment in a WHILE loop; an unparsed statement that may assign the variable; control flow that
  could not be followed; a non-literal argument; a parameter with no caller and no default; more than
  16 alternatives.
- Each string is split and parsed like a file. It is refused (unknown) if it reads a variable (they
  belong to the dynamic batch or to sp_executesql parameters), contains control flow (IF, WHILE, ...),
  a statement the indexer does not recognise (USE, CREATE PROCEDURE, GRANT, ...), or one that is not
  a DML/SELECT INTO/CREATE TABLE/VIEW/EXEC procedure statement. DECLARE/SET of literals inside it feed
  a nested `EXEC`, which is evaluated recursively.
- For a **complete** or **open** evaluation the statements of every alternative are indexed as
  writers at the EXEC (same scope, offset and database context, so `#temp` scoping is that of the
  procedure). Their steps are `inferred` (never `verified`), `branch_context` adds
  `dynamic SQL alternative i of n` and the alternative's conditions, and a note says where the string
  was built and executed. Evidence cites the EXEC's lines with the evaluated SQL as the excerpt.
- Only a **complete** evaluation removes the `dynamic_sql` gap. Otherwise the gap stays, with
  `detail.evaluation` (`open` / `unknown`), `detail.unknown_pieces` / `detail.open_pieces` (which
  variable or piece, why, where) and `detail.alternatives_enumerated`.
- `scan-diagnostics.json` lists fully evaluated statements as `evaluated_dynamic_sql`, and every
  evaluated issue has an `evaluation` entry with each alternative's conditions and SQL.

## JSON shape (`column-lineage/1.0`)

```
{
  "schema_version": "column-lineage/1.0",
  "requested": "demo.output.result_value",
  "target": Node, "target_status": "verified", "target_gaps": [gap-id],
  "boundary": {"databases": [], "objects": [], "max_depth": null},
  "scan": {"roots": [...], "files": n, "statements": n, "parsed": n, "fallback": n, "source_revision": str|null},
  "completeness": {"status": "complete|incomplete", "overall_step_status": ..., "steps_by_status": {},
                   "gaps_by_kind": {}, "stops_by_reason": {}, "note": ...},
  "nodes": [Node...],
  "steps": [{"id", "target", "writer_kind", "procedure", "expression", "operation", "mapping_method",
             "status", "branch_context": [], "inputs": [Input...], "row_selection": [RowSelection...],
             "evidence": [Evidence...], "gaps": [gap-id], "notes": [], "order"}],
  "stops": [{"node", "reason", "detail"}],
  "gaps": [{"id", "kind", "message", "node", "evidence": [...], "detail": {}}],
  "cycles": [[node-id...]]
}
```

`Node`: `{id, kind, name, display, object: ObjectRef|null, scope}`.
`Input`: `{node, role, expression, branch, status, gap, note}`.
`RowSelection`: `{node, role, expression, evidence, status, gap}`.
`Evidence`: `{root, path, start_line, end_line, role, excerpt, statement_id}` — `path` is
relative to its source root; excerpts are short (≤ 240 chars), never whole procedures.

Output is deterministic: lists are sorted and ids are content hashes.

## Gap summary (`gap-summary.json`)

For handing results back without sharing source: counts of gaps by kind, parse
fallbacks by statement keyword and error class, and up to N anonymized samples of
unsupported statements in which identifiers become `ID1, ID2, ...`, string literals
become `'?'` and numbers become `0`. It contains no object, column, path or file names.

## Later: SSIS

An SSIS adapter will add writers whose evidence is a package path plus component id:
OLE DB source SQL → `derived` columns, Derived Column → `expression` steps, Lookup →
`join_key` row selection plus value inputs, Conditional Split → `branch_context`,
destination column mappings → `target_list` steps, Execute SQL tasks → procedure calls,
connection managers → `database_source = configured`. Nothing in the tracer depends on
the writer being SQL text.
