# SQL lineage analyzers

Static analysis of local T-SQL files. Nothing is executed and no database is contacted.

- **Column lineage** (`column_lineage`): trace one column back to the columns, constants,
  variables and parameters that produce its value, with row-selection context, evidence
  and explicit gaps. This is the new tool.
- **Table lineage** (`table_lineage`): the original object-level tool, unchanged.

## Install

Python 3.11 or later.

```bash
python -m pip install -r requirements.txt
```

Run from the repository root with `src` on the path (or `pip install -e .` to get the
`column-lineage` and `table-lineage` commands):

```bash
export PYTHONPATH=src            # PowerShell: $env:PYTHONPATH = "src"
```

## Column lineage

```bash
python -m column_lineage --column db.schema.table.column --source-root path/to/sql --output out
```

The useful options:

| Option | Meaning |
|---|---|
| `--column X` | Target column; repeat for several. 2–4 part object names; brackets allowed. |
| `--columns-file FILE` | Target columns, one per line (`#` comments allowed). All are traced in one run and share the parsing. |
| `--source-root DIR ...` | Files or folders to scan (`.sql .tsql .ddl .prc .proc`). |
| `--database-map PATH=DB ...` | Files under `PATH` (relative to a source root, or absolute) belong to database `DB`. Use this when a repository has one folder per database and the code uses two-part names. |
| `--default-database DB` | Database for two-part names with no other context. |
| `--default-schema S` | Schema for one-part names (default `dbo`). |
| `--stop-at-database DB ...` | Stop when the trace reaches a column in one of these databases. |
| `--stop-at-object GLOB ...` | Stop at matching objects (`db.schema.name`, case-insensitive). |
| `--entry-point PROCEDURE ...` | The procedure(s) the run starts from. When writers in several procedures conflict, the call sequence from here orders them (each writer reached by one call path, unconditionally, or in the two arms of one IF/ELSE); the order is noted on the steps instead of an `ambiguous_writer_order` gap. Writers not called from it keep the gap. |
| `--declared-terminals FILE` | Objects or columns the reviewer accepts as terminal sources for this run (one glob per line, `# reason` optional). Stops there keep their reason and are labelled; the result can be `complete_to_declared_boundary`. Upstream population is not proved. |
| `--max-depth N` | Stop after N mapping steps. |
| `--trace-row-selection` | Also trace filter and join columns upstream (larger output). |
| `--gap-summary` | Also write `gap-summary.json`, a summary with no names in it, safe to share. |
| `--business-glossary FILE` | Business definitions shown on the overview pages (see below). |
| `--no-overview` | Do not write the `<column>.overview.md` pages. |
| `--local-server NAME ...` | Linked-server names whose four-part references mean the databases being scanned. Add that database's SQL as another `--source-root`, so its objects are traced instead of reported as external. |
| `--assertions FILE ...` | Human-asserted mappings for steps SQL cannot prove (see below). |
| `--jobs N` | Parser worker processes (default: CPUs, at most 8; `1` = none). |
| `--fail-on-incomplete` | Exit 3 if any trace is incomplete. |
| `--include` / `--exclude GLOB ...` | Filter files. |
| `--verbose` | Print timings and a per-trace summary. |

Database context for a two-part name, first match wins: an explicit three-part name, a
preceding `USE db` in the same file, `--database-map`, `--default-database`, otherwise
unknown (shown as `?` in ids).

### Outputs

For each `--column`, in `--output`:

- `<column>.lineage.json` — the full trace (`column-lineage/1.0`), the source of truth. Every
  page below is generated from it.
- `<column>.lineage.md` — the detailed review page: identity and result, short answer, value
  lineage tree, row-selection context, detailed steps with evidence, gaps. Regenerate it with
  `python -m column_lineage.render_markdown <json>`.
- `<column>.overview.md` — a short page for a business reader (no SQL needed; list lengths are
  capped, with links to the detailed page). Its first screen answers "what feeds this field, where
  does proof stop, and what is assumed": field and definition (approval status), a one- or
  two-sentence plain answer, a coverage line, at most three evidence requests, a small Mermaid
  diagram of *route families* (alternatives drawn side by side) with a family table, and a sources
  table (database | schema | table | column; files and constants in their own rows). Then what
  changes the value (CASE, fallbacks, overrides, merges, aggregations, package transformations),
  which rows (row selection, kept apart from the value), evidence and uncertainty, and shortened
  routes, open points and links. See `docs/examples/sample-overview.md` and the layout brief
  `docs/presentation-brief.md`. Skip it with `--no-overview`.
- The trace JSON also carries an additive `route_families` block: every family with all its member
  routes (end, object sequences, step ids, gaps, branch labels). Nothing else in the JSON changes.

Once per run: `scan-diagnostics.json` (parse statistics, files that failed, every
statement-level issue with its location), `routes-index.md` and `routes-index.json` (below) and,
with `--gap-summary`, `gap-summary.json`.

**Route index.** Many columns share long routes. `routes-index.md` groups the run's columns by
the set of terminal source objects their routes end at (files grouped by connection, constants
left out) and by the set of writer procedures, packages and scripts on their value routes. Each
group has a stable id (`src-…`, `wr-…`), its members (linked to their overview pages), the shared
objects or writers and the count of results. `routes-index.json` holds the same groups and, per
column, its signatures, route ids and route family ids, for tools and LLMs. A shared signature
does **not** mean identical expressions, branch selection or business meaning; every column's own
pages and JSON stay complete. A **Needs attention** view comes first: unresolved origins by source
family (with column counts), ambiguous writer order by statement, assertions awaiting review, and
columns whose definition awaits owner approval.

Re-render overview pages and the index from JSON only:

```bash
python -m column_lineage.render_overview out/*.lineage.json --index-out out [--business-glossary FILE]
```

**Business glossary** (`--business-glossary FILE`, also accepted by `render_overview`): the tool
never derives business meaning from names. Definitions come from a JSON file:

```json
{"columns": [{"column": "sales_db.mart.order_fact.net_amount", "definition": "Order amount after discounts",
              "status": "approved", "source": "Finance data owner, 2026-03 review"},
             {"column": "*.mart.order_fact.*_code", "definition": "Code from the order system",
              "status": "proposed", "source": "Analyst draft"}]}
```

`column` is `db.schema.table.column` or a shorter suffix; globs are allowed (a column whose
database is unknown is matched only by `*` in that position). An exact name wins over a glob,
then approved over proposed. Approved entries are shown as "Definition (approved)" with their
source; a proposed entry reads "Definition not approved" followed by a separately labelled
*proposed interpretation* with its source; a column without an entry reads "Definition not
approved" and nothing else.

**A zero exit code does not mean the lineage is complete.** Read the result line at the
top of the page, or `completeness.status` in the JSON.

### Reading a trace

- **Steps** say how a column gets its value in one writer statement and branch:
  `INSERT ... SELECT`, `SELECT ... INTO`, `UPDATE ... SET`, `MERGE`, views, CTEs, derived
  tables, variable assignments and procedure arguments.
- **Inputs** have roles. `value` inputs compute the value. `condition` inputs choose
  between alternatives (CASE/IIF predicates). `window` inputs are PARTITION BY and
  ORDER BY columns. Each input is labelled with its branch, e.g. `WHEN x > 0 THEN`,
  `ELSE`, or `COALESCE argument 2`.
- **Row selection** (WHERE, JOIN keys and predicates, GROUP BY, HAVING, MERGE
  conditions) is listed separately. It decides which rows contribute, not the value.
- **Status:**
  - `verified`: bound from explicit SQL.
  - `inferred`: bound by a stated rule, such as positional schema order, a default
    schema or database, or a #temp table created by the caller.
  - `partial`, `ambiguous`, `unresolved`: weaker; see the gaps.
- **Stops** end a path without a gap:
  - `boundary`: the boundary you asked for.
  - `source_without_writer`: nothing in the scanned files writes it.
  - `constant`, `parameter_without_caller`, `max_depth`.
- **Gaps** are where proof ends: dynamic SQL, parse failures, `INSERT ... EXEC`, missing
  definitions, ambiguous names or writers, `*` or positional inserts without a known
  schema, user functions, linked servers.
- **Dynamic SQL built only from literals** is evaluated: when every possible text of the executed
  string can be enumerated from the code (literals, `+`/`CONCAT`, `QUOTENAME`, `REPLACE`, `CHAR`,
  `CAST`; variables assigned those, also under IF/ELSE; procedure parameters from their callers or
  default; at most 16 alternatives), its statements are analysed as writers at the EXEC. Those
  steps are `inferred`, carry the alternative's condition in their branch context and a note
  "evaluated from dynamic SQL built at file:line". The `dynamic_sql` gap is removed only when all
  alternatives are known; otherwise its detail names the unknown piece and why.

### Assertions: steps SQL cannot prove

Some steps can never be proved from SQL text:
- dynamic SQL whose column list comes from metadata;
- objects defined and loaded outside the scanned files.

Someone who knows the system can state those steps in a JSON file and pass it with
`--assertions FILE` (repeatable). See `docs/examples/assertions.example.json` and the format
description in `src/column_lineage/assertions.py`.

- **Target and alternatives:** each assertion names a target column (`#temp` targets also take a
  `procedure`). It lists one alternative per possible branch: its input columns, and optionally
  row-selection columns, a branch label, an expression and an operation.
- **Tracing:** the tracer continues through every alternative. Those steps get the status
  `asserted` and cite the assertions file as their evidence.
- **Gaps:** gaps on the target that the assertion explains (by default dynamic SQL, unparsed
  statements, external objects and missing definitions; narrow it with `resolves` and
  `writer: {path, line}`) move to `resolved_gaps`, labelled with the assertion id. Nothing is
  deleted.
- **Completeness:** a complete result says it relies on N asserted steps. Unused or unresolvable
  assertions are listed in `scan-diagnostics.json`.
- **Provenance:** record who made each mapping (`"origin": "ai_assisted"` or `"person"`) and whether
  it has been independently reviewed (`"review": "unreviewed"` or `"reviewed"`, with `reviewed_by`).
  Pages show both, or "not recorded"; the trace JSON lists them under `assertion_provenance`.
- **Coverage:** for metadata-driven SQL, list **every** branch the metadata can produce, not
  only one observed run.
- **Scoping to one writer:** when a target has several dynamic or unparsed writers and an assertion
  covers only one:
  1. Copy the gap's **Where:** location from "Where proof ends" (`path:line`, with the path relative to
     its source root) into `writer: {"path": ..., "line": ...}`. Any line inside that statement matches.
  2. Leave `resolves` at its default, or narrow it, e.g. `["dynamic_sql"]`.
  3. Rerun and check `assertion_effects` in `scan-diagnostics.json`. It lists, per assertion, every gap
     it resolved (trace, kind, node, location), so you can confirm only the intended gap moved. Gaps
     from the other writers stay open.
- **Privacy:** an assertions file names real objects. It belongs with the source and must not
  be shared outside it.

### Declared terminals and scalar functions

- `--declared-terminals` records a source-boundary decision for a run, e.g. `demo.stage.*  # loaded by an
  external feed`. It does not stop the trace (use `--stop-at-object` for that) and never hides
  anything: stops keep their original reason, steps keep their status, and the page says that how a
  declared terminal is populated upstream is not proved.
- A scalar user function whose definition is scanned, reads no tables and calls only such functions is
  summarised: its result depends on the arguments its body uses (step status `inferred`, with a note
  citing the definition). Otherwise the call is an `opaque_function` gap saying why.

### Comparing with known lineage

```bash
python -m column_lineage.compare --expected expected.json --traces out
```

`expected.json` lists, for each target column, the source columns its known lineage gives, at the same
level as the requested boundary. The format is described in `src/column_lineage/compare.py`. It names
real objects, so keep it local.

The command writes two files:
- `comparison-details.json` (local, with names): for every expected source, how it was found or why
  it was missed, plus every extra source the trace reached.
- `comparison-summary.json` (counts and your generic labels only): safe to share.

`missing_unexplained` means an expected source was not reached and nothing in the trace explains
why. It is the strongest sign of a tool error.

### SSIS packages (optional)

Add SSIS packages (`.dtsx`) next to the SQL and their data flows become writers of database
columns:

```bash
python -m column_lineage --column db.schema.table.column --source-root path/to/sql \
       --package-root path/to/ssis --package-exclude "*/archive/*" --connection-map stage_conn=stage_db
```

| Option | Meaning |
|---|---|
| `--package-root DIR\|FILE ...` | Package files or folders (scanned recursively for `.dtsx`; project `.conmgr` files next to them are read for project connections). |
| `--package-include GLOB ...` | Only packages whose path relative to the package root matches (case-insensitive, `/` separators). |
| `--package-exclude GLOB ...` | Leave out matching packages (environment, test or historical copies). |
| `--connection-map NAME=DB ...` | Database of a connection manager. Use it when the connection string has no Initial Catalog, or is set by an expression or parameter. |

Without `--package-root` nothing changes.

What is followed (metadata links only — never package, task, component, table or column
names): destination input column → its destination table column; input lineage ids back
through data-flow paths; OLE DB / ADO.NET sources (table, or SQL command analysed with the
SQL analyzer); flat-file sources (the path ends at the file column, stop `external_file`);
Data Conversion; Derived Column (conditions are condition inputs); Conditional Split (its
condition is `filter` row selection); Lookup (join keys are `join_key` row selection, the
returned column is traced into the reference query); Union All / Merge (one step per input);
Merge Join; Sort; Aggregate (group-by row selection); Copy Column; Multicast and Row Count
(pass-through); OLE DB Command and Execute SQL task SQL (indexed as SQL writers; `?`
parameters bound to input columns or variables); precedence constraints, loops and disabled
tasks (branch context).

What becomes a gap or stays open:

- `unsupported_package_step` — Script Component, third-party and other components on the
  path, unsupported access modes;
- `ambiguous_package_copy` — several in-scope package files write the column; each copy is
  a candidate (`ambiguous`) until you narrow the scope with `--package-include/-exclude`;
- `package_database_unknown` — the connection's database is not proven (connection string
  from an expression or parameter, no Initial Catalog, project connection not found): the
  step is a candidate, never verified; fix with `--connection-map`;
- `package_runtime_value` — anything on the value path only known at run time: project or
  package parameters, system variables, expression variables, variables set at run time or by
  package configurations, and destinations whose table comes from such a variable (the gap is
  put on the object the design-time value names; the gap names the variable and expression);
- `package_expression`, `package_metadata` — an unparsable expression or a broken metadata link;
- `package_column_not_mapped` — a package loads the table but not this column (loading a
  table never explains its other columns).

Disabled tasks and data flows (or tasks in a disabled container) are not writers; the traced
column's stop says when one would have written it. Packages with configurations have no
proven connection databases. A file reachable from several package roots counts once.

Every package step cites the package file (relative to its root), the XML line, and the
package / task / component; `scan.packages` and `gap-summary.json` report packages found, in
scope and excluded (counts only). Details: `docs/column-lineage-design.md`, "SSIS packages".

### Handing results back

If you can't share the source or the traces, run with `--gap-summary` and share only
`gap-summary.json`. It holds:

- gap counts by kind;
- parse failures by statement keyword and error class;
- anonymized samples of unparsed SQL (identifiers become `ID1`, `ID2`, …, strings `'?'`,
  numbers `0`);
- per-trace statistics.

Check the file before sharing it.

## Table lineage (unchanged)

```bash
python -m table_lineage.cli --target demo.final_result --source-root fixtures/basic --output output
```

## Development

```bash
python -m pip install pytest
python -m pytest -p no:cacheprovider
```

- `docs/column-lineage-design.md` — the contract: identities, steps, roles, statuses, JSON shape.
- `fixtures/column/` — synthetic SQL for every acceptance case and messy pattern (see its README).
- `tools/generate_corpus.py`, `tools/perf.py` — synthetic large corpora and timing.
- `reference/` — the public SSIS Lineage Utility (MIT), a design reference only.

## Known limitations

See [docs/column-lineage-limitations.md](docs/column-lineage-limitations.md).
