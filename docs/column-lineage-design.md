# Column lineage: design and contract

This document is the contract between the analyzer, tracer, renderers and tests.
The data model lives in `src/column_lineage/model.py`; this file explains its meaning.

## Scope

- Input: local SQL files only (and, optionally, SSIS packages: see "SSIS packages"). Nothing is
  executed and no database is contacted.
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
                         [--business-glossary <file>]      business definitions for the overview pages
                         [--no-overview]                   do not write <stem>.overview.md
                         [--no-progress] [--verbose]
```

`<object>` may have two, three or four parts (`schema.table`, `db.schema.table`,
`server.db.schema.table`). Bracketed identifiers are accepted. The target object may
be a table, view or `#temp` table (use `--column "#t.col"` only with a single file root).

Outputs per requested column, in `--output`:

- `<stem>.lineage.json` — the versioned trace (schema `column-lineage/1.0`), the source of truth.
- `<stem>.lineage.md` — the detailed review page, regenerated from the JSON model.
- `<stem>.overview.md` — the short reader page (see "Renderers"); not written with `--no-overview`.

`<stem>` is the requested spec lower-cased with brackets removed and characters
outside `[a-z0-9._-]` replaced by `_`. For example `--column Demo.Output.Result_Value`
writes `demo.output.result_value.lineage.json`.

Also written once per run: `scan-diagnostics.json` (files, statement parse statistics,
all scan-level gaps with names), `routes-index.md` / `routes-index.json` (see "Renderers") and,
with `--gap-summary`, `gap-summary.json`.

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

A path ends at a `Stop` (`boundary`, `source_without_writer`, `constant`, the SSIS stops below,
`parameter_without_caller`, `max_depth`) or at a `Gap`. Gap kinds are listed in
`model.GAP_KINDS`. Never infer a mapping from similar names or from a table-level
dependency.

`completeness.status` is `complete` only if every value path ends at a stop and there
are no gaps. A `source_without_writer` stop means "no writer exists in the selected
scanned sources", not "this is the ultimate source-system field".

`completeness.result` keeps the meanings apart:

- `reached_requested_boundary`: complete, and every value path ends at the requested boundary or a constant;
- `complete_to_requested_depth`: complete, but some paths were cut by `--max-depth`;
- `complete_to_declared_boundary`: complete, and every other end (no writer in the selected sources,
  external file) is a terminal declared with `--declared-terminals`; parameters and depth cuts are never
  declared away;
- `complete_within_selected_scope`: complete, but some paths end at objects with no writer in
  the selected sources (other sources, such as packages or feeds, may write them);
- `incomplete`: open value-path or context gaps (for example an unsupported package step).

For readers (overview pages, route index) the ends are named by what they prove: an end at the
requested **boundary** or at a **declared terminal** is where the run was asked to stop, never an
originating system; an end with no writer in the selected sources, an external file, a parameter
without caller, a depth cut, a loop or a gap is an **unresolved origin** — even when the step that
reaches it is verified. An incomplete trace is reported as incomplete even when some of its
branches reach the boundary.

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
  "cycles": [[node-id...]],
  "route_families": {"schema_version": "column-lineage-route-families/1.0", "note": ...,
                     "families": [{"id": "fam-<hash>", "label": "F1", "source": {"kind", "key", "label"},
                                   "role": "value|condition", "value_rule": [tag...], "value_rule_text",
                                   "headline", "stop", "stop_text", "status", "route_count",
                                   "route_count_is_lower_bound", "ends": [node-id], "diagram_kind",
                                   "members": [{"end", "object_sequences", "steps", "gaps", "branches"}]}]}
}
```

`route_families` is additive (written by the CLI next to the fields above, derived from them; the
renderers recompute it from the steps, stops and gaps and never read it back).

`Node`: `{id, kind, name, display, object: ObjectRef|null, scope}`. A `prior_state` node is the earlier
content of another node (`earlier_state_of`, `of_kind`; id `prior_state:<that id>`): the value a write
reads back before it writes again. Its stop is `prior_state` (the writes producing it are traced on the
page; no proof needed of its own; `loop` lists the steps that read it and the loop it cuts) or
`source_without_writer` (only updates write the column; nothing in the selected sources adds rows).
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
`route_index` adds counts only: columns, terminal-source, writer and route signatures, route
families, the sizes of the largest groups and the number of entries in each "needs attention" list.

## Renderers

Every page is rendered from trace JSON only (`TraceResult.to_dict()`); nothing in a page is
computed from the sources. Re-render with `python -m column_lineage.render_markdown <json>`
(detailed page) and `python -m column_lineage.render_overview <json>... [--out-dir DIR]
[--index-out DIR] [--business-glossary FILE]` (overview pages and route index).

### Detailed page (`render_markdown.py`)

Identity and result, short answer, value lineage tree, row-selection context, every step once
with its evidence, gaps and next evidence. Its size is linear in the trace: cross-references
between steps (other writers of the same column, upstream steps of an input, steps a gap
affects) list a sample of links and a count, never every pair. The tree and the Mermaid diagram
are capped.

### Overview page (`render_overview.py`)

For a reader who does not read SQL; lists are capped ("and N more — see detailed page"), so a
page stays under 150 lines however large the trace. The first screen answers "what feeds this
field, where does proof stop, and what is assumed" (`docs/presentation-brief.md` is the brief):

1. **Field and definition** — the qualified target (database, schema, table, column); from
   `--business-glossary` either "Definition (approved)" with its source, or "Definition not
   approved" plus, only when the glossary supplies one, a separately labelled *proposed
   interpretation* with its source. Never derived from names.
2. **Plain answer** — one or two generated sentences: the head from `completeness` ("Complete only
   within the selected sources", "Not fully traced (n open points …)", "Traced to the requested
   boundary", …), the main source families with their headline rules (primary, fallback, CASE
   branch or default, later UPDATE override, UPDATE of unproved run order, MERGE, package mapping,
   direct), how many routes end without a proved origin, then the value rules that change the
   result (CASE choice and its condition columns and constants, COALESCE/ISNULL fallbacks, UPDATE
   overrides, MERGE, package transformations, aggregation).
3. **Coverage** — status (an incomplete trace says **Incomplete** even when some ends reach the
   boundary), value routes and families, ends at the requested boundary, declared terminals,
   unresolved origins by kind, constants, and verified / inferred / asserted step counts; a
   boundary or declared terminal is stated not to be an originating system.
4. **Main evidence needed** — at most three requests, ranked by the routes they affect: gap kinds
   (with the next-evidence text) and unresolved origins per source family ("who writes …", "who
   produces file connection …", callers, depth, loops), each linked to the detailed page.
5. **Route families** — a Mermaid `flowchart LR` with one node per family (at most 10, then a
   "N more families" node) and one edge per family straight into the target, labelled with the
   value rule and the stopping reason (and the status when not verified). Alternatives are
   parallel edges, never a chain. Shapes: database column `[( )]`, file column `[/ /]`, constant
   `([ ])`, unresolved origin `{{ }}`, declared terminal `[[ ]]` ("not an origin"); condition
   families use dotted edges. Labels are quoted and Markdown/Mermaid-active characters become entity
   codes. A family table (linked to the detailed page's `#fam-…` anchors) follows.
6. **Sources at a glance** — one row per end: kind (DB column, file column, constant, variable,
   parameter, temp table, package data flow) | database | schema | table / source | column |
   families | why it stops | proof status of the step reaching it (linked). Files and constants
   never appear in the database column.
7. **What changes the value** — CASE/IIF choices, COALESCE/ISNULL fallbacks, overrides (UPDATE
   after other writers; in the same procedure after them, or with run order not proved), MERGE,
   aggregations and window calculations, package transformations; other calculations in one line.
8. **Which rows?** — row-selection conditions of the value-route steps, grouped by object, stated
   as conditions and kept apart from values.
9. **Evidence and uncertainty** — step counts by status (verified / inferred / asserted always
   shown), links to every weaker step, assertion ids and the assertions file, unresolved origins,
   value / context / row-selection gap counts, source files with line ranges.
10. **Routes**, **Open points**, **Links** — the object-sequence routes (top 2), open points by
    kind, links to the detailed page sections, the JSON and the route index.

**Route families.** A *value route* is a path from the target through value, condition and window
inputs to an end, identified by its end, its object sequence, its value rule and its status. A
*family* groups value routes that agree on all four of:

- source family — `database.schema` (server-qualified when known; `?` when the database is
  unknown), a flat-file connection per package, constants, variables, procedure parameters, package
  variables, a temp table or package data flow;
- value rule — the set of tags met on the way: writer kind (later UPDATE override, UPDATE of
  unproved run order, plain UPDATE, MERGE matched / not matched, assertion), branch context (IF,
  ELSE, WHILE, CATCH, UNION branch, conditional split, lookup or error output), package mapping,
  input branch (CASE branch `WHEN … THEN`, CASE default `ELSE`, COALESCE/ISNULL primary or
  fallback *n*), role (decides between values, orders or partitions rows) and operation
  (conversion, arithmetic, function, text, aggregation, window, subquery, other calculation);
- stopping reason — boundary, declared terminal, no writer, external file, parameter without
  caller, depth limit, constant, loop, or the gap kinds at the end;
- evidence status — the worst step and input status on the route, raised to partial (ambiguous for
  ambiguity gaps) when an open point lies on it.

Family ids (`fam-` + 10 hex digits of the SHA-256 of those four) are stable across runs and
columns. Route counts are distinct (end, object sequence) pairs, enumerated with at most 50 object
sequences per (end, rule, stop, status) (`route_count_is_lower_bound` when cut). The detailed page
lists every family under "Route families" (anchor `route-families`, one `fam-…` anchor each, up to
25 member ends each), and the JSON `route_families` block keeps every member.

An *end* is a node on a value route with no step into it: a stop or a node with a gap. No end is
ever described as a confirmed or ultimate source.

### Route index (`routes-index.md`, `routes-index.json`, schema `column-lineage-routes/1.0`)

Written once per run (and by `render_overview --index-out`). Columns are grouped by:

- **terminal sources** (`src-<hash>`): the set of objects of the column's value-route ends —
  table or view keys, a flat-file connection per package copy, parameters and variables by id;
  constants are left out;
- **writers** (`wr-<hash>`): the set of code units that write the value-route steps — a
  procedure (normalised name and file), a package file, a script file, or an assertions file;
  views, CTEs and derived tables are objects on the route, not writers.

Ids are the first 10 hex digits of the SHA-256 of the sorted keys, so they are stable across
runs. Each group lists its members, the shared objects or writers and the count of
`completeness.result` values. `routes-index.json` also lists `route_signatures` (route id = hash
of the object keys upstream of the target's own object, with the columns that have it) and, per
column, `source_signature`, `writer_signature`, `route_signatures`, `route_families` (family ids),
`value_routes` and `definition_status` (approved, proposed or missing). The top-level
`route_families` lists each family id with its source family, rule, stop, status and columns. A
shared signature or family does not mean identical expressions, branch selection or business
meaning, and a group never stands in for a column's own trace.

`needs_attention` (rendered first in `routes-index.md`):

- `unresolved_origins` — per (source family, stopping kind): columns and routes (declared terminals
  are listed too, since their upstream is not proved);
- `ambiguous_writer_order` — per written object of an `ambiguous_writer_order` value gap: the writer
  locations and the columns affected;
- `assertions_awaiting_review` — per assertion id used: the columns relying on it;
- `definitions_awaiting_approval` — columns without an approved glossary definition, with their
  glossary status, and whether a glossary was supplied.

Every list is sorted by the number of columns affected.

## SSIS packages (optional adapter)

`src/column_lineage/ssis.py` (with `ssis_xml.py` and `ssis_expression.py`) reads SSIS
packages (`.dtsx`) and makes their data flows writers of database columns. It is off
unless `--package-root` is given; without it nothing in a trace or the scan changes.

```
                         [--package-root <dir-or-file> ...]   .dtsx files to scan (recursively)
                         [--package-include <glob> ...]       only packages whose root-relative path matches
                         [--package-exclude <glob> ...]       leave out packages whose root-relative path matches
                         [--connection-map <name>=<db> ...]   database of a connection manager (overrides Initial Catalog)
```

Globs use `fnmatch` on the forward-slash path relative to the package root,
case-insensitively (`dev/*`, `*/archive/*.dtsx`). `scan.packages` in every trace and
`packages` in `gap-summary.json` report packages found / in scope / excluded /
unreadable, data flows, destinations and Execute SQL tasks (counts only in the summary).
`scan-diagnostics.json` lists `package_issues` (with names).

### Reading packages

- Both layouts: SSIS 2012+ (`PackageFormatVersion` 6/8: `refId` paths, properties as
  attributes, holder elements such as `DTS:Executables`) and SSIS 2005/2008 (versions 2/3:
  integer pipeline ids and lineage ids, `DTS:Property` child elements, executables and
  connection managers as direct children, DTSID references in precedence constraints and
  connections). Namespaces are ignored; DOCTYPE/entity declarations are refused. A file
  that cannot be read is counted as `unreadable` and listed in `scan-diagnostics.json`;
  it never stops the run.
- Project connection managers: a reference the package does not define (2012+
  `{DTSID}:external` / `Project.ConnectionManagers[name]`, or a DTSID in an Execute SQL
  task) is resolved against the `.conmgr` files of the package's folder, or of the nearest
  parent folder (up to the package root) that has any: by DTSID, then by name.
- Inventory: connection managers (name, refId, DTSID, connection string → server,
  database = `Initial Catalog`/`Database`, flat-file path and columns, property
  expressions), variables (value, expression when `EvaluateAsExpression`), executables
  (package path `Package\Container\Task`, refId or DTSID, kind, disabled), precedence
  constraints, Execute SQL tasks and data flows (components, inputs, outputs, columns,
  external metadata columns, paths).
- Component types come from, in order: `componentClassID` names (`Microsoft.OLEDBSource`,
  ...), the `UserComponentTypeName` of managed components (ADO.NET source/destination;
  Script Component is unsupported), the first field of `contactInfo` (written by the
  component, not the author: `OLE DB Source;Microsoft Corporation;...`), a short table of
  known 2008 GUID class ids. Never from the component's name or its `description` (both
  are editable by the package author). Anything else is `unsupported`
  (`unsupported_package_step` when it is on a traced path).
- Execute Package tasks are inventoried (project `PackageName` or file connection). Steps
  of a package that a scanned parent runs carry a note naming the parent package and task
  (matched by the child's file name; context only, lineage is not traced across).

### Identities

- Package-internal data-flow columns: node kind `package_column`, object kind
  `package_flow`, id `package_flow:<root>/<package path>::<task path>::<component>.<port>.<column>`.
  A column replaced in place by a Derived Column (read/write input column) is a separate
  node `<component>.<input>.<column>` so the value before and after the rewrite stay apart.
  Lookup reference columns are `<lookup>.reference.<column>`.
- Flat-file columns: node kind `file_column`, object kind `flat_file`, id
  `flat_file:<root>/<package path>::<connection>.<column>` (one per package copy).
- SSIS variables and parameters: node kind `package_variable`, id
  `package_flow:<root>/<package path>::variables.<Namespace>::<name>` for a package-level
  definition and `...::[variables of <container path>].<Namespace>::<name>` for one defined
  on a task, sequence or loop. Variables are scoped per container: a reference resolves to
  the innermost definition visible from the referring component's container (for a
  destination table, a source query, a Derived Column expression, a parameter binding, a
  variable expression); a definition elsewhere is never used.
- Object names in components are read with brackets, double quotes (the ADO.NET
  designer's `"schema"."table"`), backticks or bare parts, in any mix. A name that cannot
  be read is counted (`scan.packages.unreadable_table_names`) and reported as a
  `package_metadata` package issue; it never silently disappears.
- Package files are deduplicated by resolved path: a file reachable from several
  `--package-root` values (a directory and a file inside it, or nested directories) is one
  package copy, labelled by the first root (`scan.packages.duplicate_paths` counts the
  repeats).
- Database columns written or read by a package are ordinary `column` nodes. The database
  of a connection is, first match wins:
  1. `--connection-map NAME=DB` (`database_source = configured`, verified);
  2. a three-part table name in the component;
  3. the connection's literal Initial Catalog when no property expression can change the
     connection string, catalog or server, and the package has no configurations
     (`connection_manager`, verified);
  4. `--default-database` when the connection names no database and has no expression
     (inferred);
  5. otherwise **unproven**: the design-time literal is not used. The object is matched
     without a database, like SQL with no database context (unified when exactly one
     database is known for `schema.name`, `ambiguous_object` when several are), every step
     through that connection gets status `ambiguous`, and a `package_database_unknown`
     gap names the connection, the reason (expression, package configurations, missing
     project connection, no catalog), the exact expressions or configurations and the
     design-time value. Such a destination is a candidate writer, never verified.

**Package configurations** (`<DTS:Configurations>`, package deployment model): a
configuration can set any connection or variable property at run time, so in a package
that has any, every package connection is unproven (rule 5, the gap names the
configurations) and every variable is run-time set (`package_runtime_value`, see Gaps).
Flat-file stops of such a package say that the configurations can change the path.

### Steps

Edges come only from package metadata links. A package, task, component, table or
column name never establishes a mapping.

| writer_kind | how the target is matched | operation |
|---|---|---|
| `package_destination` | destination input column `externalMetadataColumnId` → external column name = table column; input `lineageId` → upstream column | direct / rename |
| `package_source` | source output column `externalMetadataColumnId` → table column (table access) or flat-file column | direct / rename |
| `package_source_query` | embedded `SqlCommand` (or a static variable's value) analysed by the SQL analyzer; the external column name is the query's output name | from SQL |
| `package_data_conversion` | output `SourceInputColumnLineageID` | cast |
| `package_derived_column` | `Expression` (lineage ids `#{...}` / `#n`) or `FriendlyExpression` (input names of the same component) | from the expression |
| `package_lookup` / `package_lookup_reference` | output `CopyFromReferenceColumn` → reference query (SQL analyzer) or table | direct / rename |
| `package_union_all`, `package_merge` | input column `OutputColumnLineageID`, one step per input (`branch_context` `Union All <name> input i of n`); an input that maps nothing gives `constant:NULL` | direct / rename / constant |
| `package_merge_join` | output `InputColumnID` | direct / rename |
| `package_sort`, `package_copy_column` | `SortColumnId`, `copyColumnId` | direct / rename |
| `package_aggregate` | `AggregationColumnId` + `AggregationType` | direct (group by) / aggregate |
| `package_variable` | variable expression or design-time value (inferred) | from the expression / constant |
| `package_parameter_binding` | Execute SQL `ParameterBinding` (`?` marker n ↔ variable) or OLE DB Command (`?` marker n ↔ the input column mapped to its n-th external parameter column) | direct |

The path between a consumer and the component that produced a lineage id is followed
through `path` start/end ids and synchronous outputs. On the way:

- Conditional Split output → `filter` row selection on the consumer's step for every
  column its condition reads; the default output gets `NOT (c1) AND NOT (c2) ...`;
  `branch_context` names the split and output.
- Lookup match / no-match output → `join_key` row selection (input column and reference
  column); the Lookup output column's own step carries the same keys.
- Merge Join → `join_key` from the inputs' sort key positions; Aggregate → `group_by` for
  the group-by columns of the output the traced column belongs to (other outputs have
  their own keys).
- A Conditional Split sends a row through the first output (by `EvaluationOrder`) whose
  condition holds, so the n-th output's row selection is `NOT (earlier conditions) AND
  its own`, and the default output's is `NOT` of all.
- Lookup on a cache connection (`ConnectionType` 1 or a `CACHE` connection manager):
  `unsupported_package_step`; a saved `SqlCommand` left in such a component is never used.
- Error outputs → `branch_context` `error output ... of <component>`. Rows on an error
  output failed in that component, so they carry the column as it arrived: a column the
  component replaces in place (Derived Column read/write) is mapped to the upstream
  original, never to the replacement expression, and the step is at best `inferred`. SSIS
  error columns (`ErrorCode`, `ErrorColumn`) are `package_runtime_value` gaps.
- Data Conversion, Derived Column, Multicast, Row Count, Copy Column, OLE DB Command and
  Lookup pass rows through. Only actual path and lineage links are followed, so components
  hanging off a Multicast are not on another output's path.
  Any other component on the path (Script Component, third-party, ...) adds an
  `unsupported_package_step` gap to the step (value effect, status `partial`); if it
  rewrites the column in place the path ends there.

Every package step has `procedure` = `package <path>, task <task path>, component <name>`,
evidence `{root, path (relative to the package root), start_line (the XML element), role,
excerpt}` and a note `From package copy <root>/<path>.`. `branch_context` begins with the
task's precedence constraints (`IF <expression> AND <task> succeeds (precedence
constraint)`, `AFTER <task> fails`) and loop containers.

**Disabled tasks** (`DTS:Disabled="True"` on the task or on any container around it) do not
run as saved, so they are not writers: a disabled data flow's destinations and OLE DB
Commands and a disabled Execute SQL task's statements produce no steps and are not indexed.
Each is a package issue (`detail.disabled`) and counted (`disabled_data_flows`,
`disabled_tasks`). When a disabled destination maps the traced column, or a disabled task's
SQL names the traced object as a write target, the column's `source_without_writer` stop
detail says so ("Not counted as writers: a disabled task in <package> ... maps this
column"); when other writers exist, the note goes on their steps.

### Stops

- `external_file` — a flat-file column; detail names the connection and its path. When a
  property expression sets the path (typically `@[User::file]` mapped by a Foreach loop),
  the detail names the expression and who sets the variable, and says the design-time
  path is only the saved value.

`external_file` is the only package stop: a file column is a genuine end within the scanned
scope (`ends_by_meaning`: `external_file_source`). Values only known at run time are gaps,
not stops (below), so a trace that reaches one is `incomplete`.

### Gaps

| kind | when |
|---|---|
| `unsupported_package_step` | a component, access mode or task the adapter does not analyse (identity in `detail`) |
| `ambiguous_package_copy` | more than one in-scope package file writes the column (environment, test, historical copies); every copy's steps are kept with status `ambiguous`; `detail.copies` lists them |
| `package_runtime_value` | a value on the path is only known at run time: a project/package parameter (`$Project::`, `$Package::`), a system variable (`System::StartTime`, ...), an expression variable (its step lists the variables it reads, status `partial`, never verified), a variable set at run time (result set, output parameter, Foreach mapping, Script Task or Script Component `ReadWriteVariables`, Expression task, package configuration), an SSIS error column, an unbound `?` marker, or SQL from such a variable. `detail` names the exact parameter, variable, expression, setters and design-time value. Affects value; counted in `ends_by_meaning` |
| `package_expression` | an SSIS expression could not be parsed (step `partial`, lineage ids in it kept as inputs) |
| `package_metadata` | a metadata link is missing or inconsistent (unconnected input, unknown lineage id, no external column) |
| `package_column_not_mapped` | an in-scope destination loads the table but maps no input to this column (not raised for identity, computed or rowversion columns); the `source_without_writer` stop detail says the same |
| `package_database_unknown` | the database of a connection is not proven by the package (see Identities); `detail` has the connection, reason, expressions, design-time database and `.conmgr` file |

Context gaps: an Execute SQL task in the same package whose statement is not in the file
(from a variable, a property expression or a file connection) is added to every trace
that passes through that package's destinations.

**Destinations whose table is not static** (OpenRowset from a variable that is an
expression, is set at run time or is configured; other run-time table access modes) never
make an edge. They are a package issue, and when the variable's design-time value names an
object, a trace of any column of that object (with or without database) gets a
`package_runtime_value` gap (value effect) naming the destination, the variable, its
expression or setters and the design-time value, like dynamic SQL that names a target. A
variable with a plain design-time value and nothing that sets it at run time is still used
(status `inferred`, with a note).

### Package copies

When several in-scope package files write the same column (destinations, or Execute SQL
tasks from different package files), the deployed version and run order need evidence
outside the files, so none is treated as the active writer: each copy's steps are
candidates with status `ambiguous` and one `ambiguous_package_copy` gap lists the copies.
Narrow the scope with `--package-include` / `--package-exclude`. Several destinations in
one package file are ordinary alternatives.

A destination that loads a table explains only the columns it maps. Every other column
of that table gets no package step; if nothing else writes it, it keeps the stop it would
have without packages and gains a `package_column_not_mapped` gap, so it is never complete
because the table was loaded.

### Execute SQL tasks and OLE DB Commands

Direct-input Execute SQL statements and OLE DB Command `SqlCommand` text are indexed by the SQL catalog (`Catalog.add_embedded`) under the
package's root-relative path, one virtual unit per task: a `USE [db]` from the connection
precedes the statement, its first line is the line of the task's `SqlTaskData` element
(later lines count on from there), its scope is `<root>/<package>::<task path>` and its
`procedure` label is the package and task. OLE DB `?` markers become `@ssis_param_<n>`;
the adapter binds each by parameter name: Execute SQL `ParameterBinding` `ParameterName`
n, OLE DB Command input columns mapped to external column `Param_n`, and OLE DB source
`ParameterMapping` entries `"ParameterN:Input"` (or a named `@p`), never by list
position. A marker with no binding is a `package_runtime_value` gap. Precedence
branches of the task (and `OLE DB Command <name>: runs once per row reaching it`) are
added to the statements' control context. When the connection's database is unproven no
`USE` is added and the writer steps get a `package_database_unknown` gap and status
`ambiguous`. Statements from a variable, property expression or file connection are not
indexed (context gap, above). An Execute SQL task that only clears a table (TRUNCATE)
is not lineage, as in SQL files.

### Not supported (gap or issue instead)

Script components and tasks, third-party components, Fuzzy Lookup/Grouping, Pivot,
Unpivot, Term Extraction, Slowly Changing Dimension, Character Map, Audit,
sampling components, cache connections for Lookup, Excel/ODBC/XML/raw-file sources,
flat-file and other non-database destinations, destinations whose table comes from a
runtime variable (a gap on the named object, above), values of package configurations and
`.params`/environment values (a parameter is a `package_runtime_value` gap; a connection
set from one is unproven), multi-statement or EXEC source queries (a gap), lineage across
Execute Package tasks (context note only), the row-selection context of the path into an
OLE DB Command (its SQL is indexed as a writer; the binding steps carry the path), expression
evaluation of variables (an expression is never evaluated, only its references are
listed), and the run order of different packages.
