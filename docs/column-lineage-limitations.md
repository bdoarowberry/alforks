# Column lineage: what the tool cannot settle

The tool reads source files. It never executes anything, never contacts a database, and never
infers a mapping from a similar name or a table-level dependency. So some questions it cannot
answer; it reports each as a gap, a stop or a labelled status instead of guessing. This page says
what those are and why. Gap kinds are defined in `src/column_lineage/model.py` (`GAP_KINDS`); the
contract is `column-lineage-design.md`; how to read a result is in `HANDOVER.md`.

## Decided at run time, not in the code

- **Dynamic SQL built from data** (`dynamic_sql`). When the executed string is built only from
  literals (and variables or parameters assigned those), its texts are enumerated and analysed. When
  a column list or table name comes from a query, a cursor, metadata or an unknown caller, the texts
  cannot be enumerated and the gap stays; its detail names the unknown piece. Fix: an assertion from
  someone who knows the possible texts, or runtime capture.
- **Run order between jobs** (`ambiguous_writer_order`). When writers that overwrite (UPDATE, MERGE
  UPDATE) sit in different procedures or packages, which one runs last depends on schedules.
  `--entry-point` and `--job-scripts` order writers reached from one procedure or one job; writers in
  independently scheduled jobs stay unordered. A job script is a static copy, not proof of the
  deployed job.
- **Branches the code does not decide**: which IF arm, CASE branch or MERGE clause applied to a row.
  Every arm is kept as an alternative with its condition in the branch context; none is dropped and
  none is chosen.
- **Variables set from outside the analysed text** (`unresolved_variable`): OUTPUT parameters,
  return codes, cursor FETCH, values from statements that could not be analysed.
- **SSIS values only known at run time** (`package_runtime_value`): project and package parameters,
  system and expression variables, variables set by tasks or configurations, and destinations
  whose table comes from such a variable. Package expressions are never evaluated.
- **Which earlier writer populated the rows**. Where a write reads a column's own earlier content
  (`UPDATE t SET a = a * 2`, a working table aggregated into itself), the trace ends at an
  earlier-content node (stop `prior_state`) and traces the candidate writers; which of them filled
  the rows in question is not proved.

## Outside the scanned files

- **No writer in the selected sources** (stop `source_without_writer`). The loader may be a package,
  a feed, a job in another repository or a manual process. The stop carries a `writer_search` list of
  near-miss statements (another database, unparsed, not mapped, ...). This is never a claim that the
  column is an ultimate source-system field.
- **External or linked objects** (`external_object`): objects on other servers or databases. With
  `--local-server` a linked-server name can be mapped to scanned databases; otherwise the path ends.
- **Missing definitions** (`missing_definition`): a table, view, procedure or function that nothing
  scanned defines. Positional (`positional_without_schema`) and `*` (`wildcard_without_schema`)
  inserts also need a reliable ordered schema; without one no step is produced.
- **Flat files** (stop `external_file`): the path ends at the file column; what produces the file is
  unknown.
- **Procedure parameters with no caller in the scan** (stop `parameter_without_caller`).
- **Business meaning**. Never derived from names; only an approved glossary entry supplies it.

## Code the analyser does not follow

- **Statements it cannot parse or does not analyse** (`parse_error`, `unsupported_statement`).
  Common T-SQL is repaired and re-parsed first; what still fails is reported with an anonymized
  shape in `gap-summary.json`.
- **`INSERT ... EXEC`** (`insert_exec`) when the callee's result set is not settled: several
  definitions of the callee, more than one statement returning rows, or a nested EXEC or dynamic SQL
  inside it. A single result-set SELECT is mapped by position.
- **User functions**. A scalar function is summarised only when its scanned body reads no tables
  (`opaque_function` otherwise). An inline table-valued function is inlined only when it has one
  definition in the caller's database, positional arguments and alias-qualified argument columns
  (`table_valued_function` otherwise). Multi-statement functions are never followed.
- **Names that cannot be bound** (`ambiguous_column`, `ambiguous_object`, `unresolved_alias`,
  `column_count_mismatch`): a column that several sources could supply, a two-part name that several
  databases define, an alias with no source, or an INSERT whose column and value counts differ.
- **Recursion and depth**: a loop that remains after earlier-content handling (a recursive CTE) is
  listed in `cycles`; a `--max-depth` cut is a `max_depth` stop; an expression nested too deeply to
  analyse is a `depth_limit` gap. A path that ends with no recorded reason is an `unexplained_end`
  gap, which is a tool defect to report. The kinds `cycle`, `runtime_branch` and
  `column_not_written` are defined in `GAP_KINDS` but not produced at present.
- **SSIS components the adapter does not analyse** (`unsupported_package_step`): Script components
  and tasks, third-party and most transformation components (Fuzzy Lookup, Pivot, Unpivot, SCD,
  Character Map, Audit, sampling), cache-connection Lookups, Excel/ODBC/XML/raw sources, non-database
  destinations. Lineage is not traced across Execute Package tasks (context note only).
- **Several deployed copies** (`ambiguous_package_copy`): several in-scope package files write the
  same column; narrow with `--package-include/--package-exclude`.
- **Unproven package connections** (`package_database_unknown`): a connection string from an
  expression, parameter or configuration, or with no Initial Catalog; `--connection-map` fixes it.
- **Broken or unparsable package metadata** (`package_expression`, `package_metadata`), and tables a
  package loads without mapping the traced column (`package_column_not_mapped`).

## What a result does and does not claim

- `complete` means every value path ends at a stop with no value-path or context gap. It does not
  mean the stops are source-system fields: read `completeness.result` (`reached_requested_boundary`,
  `complete_to_declared_boundary`, `complete_within_selected_scope`, `complete_to_requested_depth`).
- Row-selection gaps (filters and joins that could not be fully resolved) are reported separately
  and never make the value lineage incomplete.
- `asserted` steps come from a person's statement in an assertions file, not from SQL. They are
  counted apart (`relies_on_assertions`) and their origin and review status are shown.
- A declared terminal records a reviewer's decision for one run; how it is populated upstream is
  not proved.
- A zero exit code only means traces were written.
