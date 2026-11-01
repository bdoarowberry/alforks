# Column lineage: status and next steps

## Where it stands

- Engine: `src/column_lineage/` (catalog, control, analyzer, tracer, cli, render_markdown, gap_summary).
- Contract: `docs/column-lineage-design.md`. Usage: `README.md`.
- Tests: 97 passing (`python -m pytest -p no:cacheprovider`), including:
  - 46 acceptance and messy-pattern tests;
  - 20 renderer tests;
  - 24 regressions from an adversarial semantics review (`fixtures/column/review/`);
  - 7 corpus-generator tests.
- Synthetic ~3,000-object corpus (`tools/generate_corpus.py --objects 3000 --seed 42`), 20 known chains:
  - precision 1.0, static recall 0.975;
  - about 20 s per run, most of it parsing.

## Fixed after the first real-source test (feedback round 1)

- **False `complete` with an aliased unparsed or dynamic writer** (`UPDATE TOP (n) f … FROM x AS f`):
  - `TOP (n)` is blanked and the statement re-parsed;
  - aliases are resolved to tables for unparsed statements and dynamic-SQL literals.
- **Statement repair:**
  - INSERT table hints, `OPTION (...)` query hints and `OUTPUT … INTO` are blanked (the OUTPUT
    target is reported as a gap);
  - statements the indexer glued together (missing semicolon before TRUNCATE, WHILE, SET, …) are
    split and parsed piece by piece;
  - an INSERT cut off from its SELECT is rejoined.
- **Non-lineage statements** (RAISERROR, SET options, EXECUTE AS, IF-condition fragments) are counted
  as `ignored_non_lineage`, not as parse failures.
- **Gaps are classified** as `value`, `row_selection` or `context` (`affects` in the JSON):
  - completeness depends only on value-path and context gaps;
  - row-selection gaps are reported separately.
- **Report text:** SQL comments no longer leak into labels or excerpts; "and and N more" fixed.
- **Windows:** `render_markdown -o -` writes UTF-8 bytes (it used to raise UnicodeEncodeError on cp1252).
- **sqlglot fallback warnings** are silenced.
- **Anonymizer:** `@var` / `#temp` stay single tokens, and built-in function names are kept.

## Fixed after feedback round 3

- Worker processes no longer print SQLGlot fallback warnings, which quoted raw SQL.
- **Parse repairs:**
  - spaced comparison operators (`! =`, `< =`, `> =`);
  - `PRIMARY KEY` / `UNIQUE CLUSTERED` inside CREATE TABLE;
  - several DECLAREs on one line.
- `ALTER TABLE … ADD PRIMARY KEY/UNIQUE/…` and index-option fragments count as non-lineage.
- **"Where proof ends"** is comprehensive:
  - every stop and every node missing proof is its own bullet, with its location, the step(s) and
    branch(es) that reach it, and the next evidence needed;
  - it says explicitly when no value path reaches the requested boundary;
  - weaker (inferred/partial) links are listed;
  - the tracer guarantees every value-path end has a stop or a gap: otherwise an `unexplained_end`
    gap is recorded.

## Added after feedback round 4

- Variables whose names start with a digit (`@2_periods_ago`) parse; their names are kept.
- `--assertions FILE`: human-asserted mappings for dynamic SQL and external objects. Asserted steps
  are marked `asserted`, explained gaps move to `resolved_gaps`, and completeness reports how far
  the result relies on assertions.

## Added after feedback round 5

- `--local-server NAME`: linked-server references resolve to scanned local databases.
- Pages state reliance on assertions at the top and count value-path, context and row-selection
  gaps separately.
- "Where proof ends" lists the statements behind the most gaps.
- `scan-diagnostics.json` lists `assertion_effects`; assertion evidence uses the real file path.

## Fixed after feedback round 6

- Row-selection clauses no longer expand CTEs and derived tables (unless `--trace-row-selection`):
  their upstream steps and gaps were traced off the value path, inflating step and gap counts.
- Dynamic SQL that can only execute an empty string or NULL is reported as `dead_dynamic_sql` in
  scan diagnostics, not as a gap.
- "Statements behind the most gaps" counts only gaps that cite a statement.
- Assertion evidence paths are relative to the output folder (where the pages are).

## Fixed after feedback round 7 (75-column batch)

- **A writer after an IF condition on the same line was silently dropped.** For
  `IF NOT EXISTS (SELECT …) INSERT …`, the indexer starts at the inner SELECT; the fragment was
  classed as non-lineage, so the target looked like a source with no writer and the trace could
  claim "complete". The statement after the condition is now recovered, with its IF context.
- Text that names a write target is never classed as non-lineage.
- `compare`: `no_baseline` for columns without expected sources.
- Diagnostics rank the statements blocking the most traces, with their locations.

## Added: static evaluation of dynamic SQL (prototype)

- `src/column_lineage/dynamic_eval.py`: dynamic SQL whose every possible text is built from literals
  (also via variables, IF/ELSE, parameters with literal callers or defaults; at most 16 alternatives)
  is parsed and analysed as writers at the EXEC; the steps are `inferred`. See the design doc,
  "Dynamic SQL evaluation". Fixtures: `fixtures/column/dynamic/`; tests: `tests/test_column_dynamic_eval.py`.
- Parameter defaults that are string literals (`@p sysname = N'x',`) were cut to `N`; fixed.

## Fixed after adversarial review 4 (tests: tests/test_column_review4.py)

- Glued-statement splitting is accepted only when every piece is a whole statement (a line-start
  `WITH (NOLOCK)` hint no longer splits one UPDATE into a phantom writer and a lost tail).
- `OUTPUT … INTO` parsed natively, `ALTER TABLE … SWITCH`, unparsed writes through synonyms and
  views, and writes through views with several definitions are reported as gaps.
- Alias resolution for unparsed/dynamic targets uses every table an alias names.
- Targets read from string literals need INSERT/UPDATE/MERGE or SELECT … INTO; reserved words
  are never targets; runtime-built dynamic SQL keeps its scope gap (one gap per statement).
- `ALTER TABLE … ADD DEFAULT … FOR col` defaults; `.WRITE` updates; TOP (n) notes; compound
  assignment precedence; `WITH EXECUTE AS` headers; digit-variable placeholders cannot capture
  real names; functions in WHERE are row-selection gaps.
- Assertions: `writer.path` must match exactly (optionally prefixed by the root label);
  malformed `writer`/`resolves` are usage errors.
- `compare`: sources reached only through ambiguous bindings are `found_as_ambiguous_candidate`.

## Fixed after feedback round 8

- A no-writer stop now says what the writer search found (`writer_search` on the stop, counted
  in `gap-summary.json` under `totals.no_writer_stops`). In synthetic tests every shape of
  `INSERT ... SELECT` inside a procedure body traces correctly; the reproduced cause of the
  symptom is a writer whose database context (USE, `--database-map`, `--default-database`)
  resolves its target to another database than the reader's. Tests: `tests/test_column_round8.py`.
- **Root cause of the missed writer (C23): file encoding.** Files saved as UTF-16 (SSMS
  "Unicode") were decoded as UTF-8, so every statement in them silently vanished and their
  writers' targets looked like sources with no writer. Files are now decoded by BOM / NUL
  pattern (UTF-16 LE/BE, UTF-8, else Windows-1252); the scan reports `file_encodings` and
  `files_without_statements`. Confirmed on the real sources: 15 of 1,438 files were UTF-16 with a
  BOM; reading them added 309 statements, and the missed writer is now traced. The writer search now also reports writers of the same object that
  do not name the column, and statements that only wrote another object.
- `completeness.result` and `ends_by_meaning` distinguish reaching the requested boundary from
  being complete only within the selected sources.
- Parse repairs: `@x + = y`, qualified column names in an INSERT into a table variable, index
  options glued to the next statement, a lone `WITH EXECUTE AS` header (left an empty statement),
  `db..name` in text-matched targets (it was read as the database name).
- Parse-error classes keep SQLGlot's own rule names (`Required keyword: high missing for Between`).

## Fixed after the 178-column SQL batch

- Compound assignment ending in a string literal (`SET @sql += N'...'`, `SELECT @c += ',' + name + ']'`)
  was rewritten with the final literal cut off (the masked text is blank there), so the statement
  failed to parse; dynamic SQL built that way could not be evaluated. Test:
  `test_compound_assignment_ending_in_a_literal`.
- Parse-error classes now really keep SQLGlot's rule names (the stored message has a prefix).
- Variables named like keywords (`@End`, `@Update`, `@Set`, `@Declare` …) started a new statement at
  the keyword, cutting the statement after `@` (shared indexer, `table_lineage/index.py`). This was
  behind most remaining "Required keyword: this missing for Parameter" and "DECLARE <name>" fallbacks;
  writers among them became parse errors. Test: `test_keyword_named_variables_do_not_split_statements`.

## Added: optional SSIS adapter

- `--package-root`, `--package-include/--package-exclude`, `--connection-map`; modules `ssis.py`,
  `ssis_xml.py`, `ssis_expression.py`; contract in the design doc ("SSIS packages").
- An adversarial review found 3 realistic false edges (error outputs, variable scoping, cache
  Lookup), 2 binding-order false edges and several hidden runtime unknowns; all fixed with
  regression fixtures in `fixtures/column/ssis_review/` (`tests/test_column_ssis_review.py`).
  Runtime values (parameters, system/expression variables, configurations) are gaps, not stops.
- First real run (one production project, 3 packages): the file-to-staging column traces through
  conversion to the file column, all three links verified against the XML by the tester; unmapped
  table columns stay `package_column_not_mapped`. Fixed after it: disabled tasks no longer add
  context gaps to traces through their package; `gap-summary.json` lists anonymized shapes of SSIS
  expressions the parser rejects (`packages.unparsed_expression_samples`); `compare` accepts file
  sources (`<file connection>.<column>`) and counts file stops as reached ends.
- Raw SSIS expressions bracket function names (`[LEN](#{...}) == 0`); these are now parsed as function
  calls (this was the Conditional Split row-selection gap on the real package). Expression shapes in
  the gap summary tag reference kinds (`[IDn]`, `#IDn`, `@IDn`).
- Filegroup clauses on their own (`ON [PRIMARY]`) are non-lineage.

## Added after the blocker triage

- Scalar function summaries (pure functions are no longer `opaque_function` gaps); test
  `tests/test_column_function_summary.py`. Two synthetic corpus chains became complete.
- `--declared-terminals FILE` and the result `complete_to_declared_boundary` (stops keep their reasons).
- Positional INSERT steps cite the column order they used (`target_schema` evidence).
- `--entry-point PROCEDURE`: orders writers in several procedures by the call sequence from the run's
  entry procedure (single call path, unconditional or opposite IF/ELSE arms); otherwise the
  `ambiguous_writer_order` gap stays and says why. All writers' sources remain on the trace (an
  overwrite replaces only the rows it matches). Test: `tests/test_column_entry_point.py`.

## Fixed after adversarial review 5 (tests: tests/test_column_review5.py, fixtures/column/review5/)

- Encoding: a stray Windows-1252 byte no longer turns a UTF-8 file into cp1252 (mojibake identifiers,
  lost writer); any NUL byte means UTF-16 (UTF-16 without BOM and mostly non-Latin text was read as UTF-8).
  No-writer stops say when files yielded no statements.
- Function summaries: CLR (`EXTERNAL NAME`) and RETURN-less bodies are opaque; several different
  definitions (CREATE then ALTER) are opaque; three-part calls must match the function's database;
  parameters used only in IF/WHILE conditions are condition inputs; `DEFAULT` arguments are constants.
- Result labels: `reached_requested_boundary` only when every non-constant end is the boundary; depth cuts
  are `complete_to_requested_depth`; declarations apply only to no-writer, file and boundary stops.
- Compound assignment followed by a `--` comment parsed again (literal-aware trimming).
- Procedure options in any order (`WITH RECOMPILE, EXECUTE AS ...`, `ENCRYPTION`, `NATIVE_COMPILATION`,
  `BEGIN ATOMIC WITH (...)`) no longer swallow the first body statement.
- `--declared-terminals`: bad JSON is a usage error; `#temp` patterns work in text files.
- `server..schema.name` via `--local-server`: the database stays unknown instead of `--default-database`.
- `compare`: two-part sources match only package file columns; `<package>::<connection>.<column>` pins one package.
- Writer search looks statements up by binary search.
- The detailed Markdown page grew quadratically with thousands of writers into one table; cross-links
  are now capped (every step is still shown once).

## Added: overview pages and route index

- `<column>.overview.md`: short business-facing page (result in plain words, routes, end points table,
  row conditions, open points with next evidence); `--business-glossary FILE` for approved/proposed
  definitions; `routes-index.md` / `.json` group columns by shared terminal sources and writers.
  `python -m column_lineage.render_overview` re-renders from JSON. Tests: `tests/test_column_overview.py`.
- After the tester's review of real overview pages: a "Proven to / origin unproven" line beside the
  result, "Main evidence needed", a "Sources at a glance" section before the routes, and loop ends
  listed and linked in the End points table.
- `--entry-point` on the real run resolved none of the 56 writer-order traces: 256 of 261 gaps are
  writers from separate integration jobs, not called from the report entry point; 5 are package loads
  followed by SQL overwrites. Correct: their order needs schedule evidence.
- Overview v2, after the reviewer's presentation brief (`docs/presentation-brief.md`): the first screen
  is field and definition (approved, or "Definition not approved" plus a labelled proposed
  interpretation), a generated plain answer, a coverage line, at most three evidence requests, a
  Mermaid diagram of route families (parallel edges, shapes per end kind, at most 10 families) with a
  family table, and a sources table (db | schema | table | column; files and constants apart). Then
  what changes the value, which rows, evidence and uncertainty. Route families group routes only when
  source family, value rule, stopping reason and evidence status agree (stable `fam-` ids); the trace
  JSON gains an additive `route_families` block, the detailed page a "Route families" list with
  anchors, and the route index family ids and a "Needs attention" view. Fixture:
  `fixtures/column/overview_branches/`.

## Known open issues

- Corpus chain 08 ends in `missing_definition` (recall 0.6). Not investigated.
- Dynamic SQL that builds its projection from metadata cannot be followed. This is by design: it is
  reported as a `dynamic_sql` gap at the EXEC (its detail now says which piece is unknown).
- A dynamic SQL issue's literal "targets" are guessed from all string literals in its scope; text like
  `'INSERT INTO ' + @t` yields the bogus target `into`, and an issue with any target is not reported as a
  scope-level gap. Unchanged by the evaluator (it only matters when evaluation is incomplete).

## Still to do

- Robustness review: a reviewer was checking encodings, huge files, deep nesting and symlink
  loops, but its results were not collected. Rerun it.
- Performance: parsing dominates. Options:
  - parallel parse (`--jobs`);
  - a pickle cache (about 2.4x on small files);
  - a cheaper `mask_noncode` in `table_lineage/index.py`, which runs 4–5 times per statement.
- Renderer suggestions not yet done:
  - a structured pairing between a CASE condition and the value it selects;
  - a per-step scope field;
  - gap-to-step links.
- `docs/column-lineage-limitations.md`: referenced from the README but not written yet.
- Handback package for the tester LLM:
  - how to run;
  - how to send back `gap-summary.json`.

## After the tester's review of overview v2

- Assertion provenance: assertions record `origin` (ai_assisted / person) and `review` (unreviewed /
  reviewed); pages say "recorded in an assertions file, not proved from code" plus who made it and its
  review state, or "not recorded" (no longer "a person's statement").
- Mermaid labels carry no entity codes (`#`, `|`, brackets render raw; checked with Mermaid 10 in a
  browser); identifiers are no longer cut short in tables; the "more families" node is labelled as a
  summary, not a source.
- Plain answer = three bullets: headline, sources with family links (each named once), value rules.
  A scope-complete trace with unproven origins leads with "Trace stopped at unresolved sources".

## Fixed: empty layout copies created false loops (real run: 153 of 178 traces)

- `SELECT * INTO #r FROM target WHERE 1 = 2` (and `TOP (0)`, any constant-false WHERE) copies the layout
  and no rows. Such a branch gives no value steps, so the target no longer feeds #r and no false loop
  forms. Tests: `test_empty_layout_copy_is_not_a_value_source`, `test_yields_no_rows_only_for_row_independent_predicates`.
- Overview: a loop end is no longer an unresolved origin (every input into a loop is traced); it is
  counted separately and drawn as "Loop, no new source", so headline, coverage and end points agree.


## Earlier content instead of cycles (real run: 24 traces with 62 cycle entries)

- A write that reads its own column's earlier value is a real dependency, not an error. Each loop is now
  cut where it reads back into a node already on the walk (depth-first from the target): that read
  points to an **earlier content of X** node (`kind: prior_state`, `earlier_state_of: X`), so the trace
  shows the value before the write (the new node) and after it (X). The node ends with an explicit
  stop:
  - `prior_state` when the writes that produce X are traced on the page (its sources are the other
    routes; for a permanent table, the content earlier runs left). It needs no proof of its own, so it
    does not prevent `reached_requested_boundary`. The stop's `loop` records `earlier_state_of`,
    `read_by_steps` and `loop_nodes`, which keeps the dependency in the JSON.
  - `source_without_writer` when nothing in the selected sources adds rows to X's table (only UPDATE or
    MERGE-update steps write it): the earlier rows are an untraced source.
- **False complete fixed:** `UPDATE t SET a = a * 2` with no writer adding rows used to end only at
  `constant:2`. A column written only by updates now also gets a `retained` step ("rows not updated
  keep earlier content", inferred) when the updates are filtered or joined, and the earlier content
  is an unresolved origin. The SQL writers that add rows are INSERT, SELECT INTO, a view, a MERGE with
  WHEN NOT MATCHED; package destinations also count.
- Shapes covered (invented, `fixtures/column/earlier_state/`, `tests/test_column_earlier_state.py`):
  - preserve and restore of target rows through a holding table;
  - aggregate-then-update of a working table;
  - a self-update with and without a row creator;
  - an update-only joined update;
  - variable accumulation (`SELECT @s = @s + part`).
- Recursive CTEs stay as cycles.
- Overview: earlier-content ends are drawn as "No new source: earlier content of …" and are not
  unresolved origins.
- `gap-summary.json` → `totals.earlier_content` gives the counts: `loops_cut_traced`,
  `update_only_untraced`, `rows_not_updated_steps` and `remaining_cycles`.

## Fixed after the earlier-content run (real run: 10 cycles left in 5 traces; 48 false unexplained ends)

- **Batch-only false gap.** Expansions are memoized across the targets of one run, and the earlier-content
  pass changed those cached steps in place. A later target then reused a step that pointed to an
  earlier-content node but had no stop, which gave `unexplained_end` and `incomplete`. Each trace now
  works on its own copies of the steps. Tests:
  - `test_batch_and_single_target_runs_agree` compares batch and single-target JSON
    (`fixtures/column/earlier_state_batch/`);
  - a sweep over all earlier-content fixtures finds no batch/single difference.
- **Multi-column UPDATE with a derived expression over the columns it sets** (`SET a = CASE ... t.a ...,
  b = CASE ... t.b ...` with `CROSS APPLY (SELECT t.a + t.b)`). Loop cuts that landed on a
  derived-table column were skipped, so a cycle remained. Loops are now cut per strongly connected
  group, always at the table column or variable nearest the target, and the cutting repeats until no
  such group is left. Every right-hand-side read, including the CROSS APPLY read, is now the value
  before the UPDATE. Test: `test_multi_column_update_reads_every_column_before_the_update`
  (`fixtures/column/earlier_state_multi/`).
- **Wording.**
  - These ends are now "earlier content before this write", not "loops": the pages, the stop detail,
    the diagram ("Before this write: …") and the coverage line ("earlier-content ends") all use it.
  - The pages say that candidate earlier writers are shown and that the code does not prove which of
    them populated the rows before this run.
  - Genuine loops (recursive CTEs) keep the loop wording.
- Shortened lists (the "And N more end points" line, route summaries and the diagram's summary node)
  now say "earlier content: N", not "loop: N". The generic completeness note no longer calls an
  earlier-content read a loop. "Loop" is kept for genuine cycles such as recursive CTEs. Test:
  `test_shortened_lists_call_earlier_content_by_its_name`.
- Real run after these fixes: no cycles remain (102 loops cut, all ending at traced earlier content),
  no `unexplained_end` gaps, and no outcome changes (78 / 4 / 96).

## Gap shapes from the 178-column run (tests: tests/test_column_gap_shapes.py, fixtures/column/gap_shapes/)

The tester classified the remaining gaps (counts are affected traces). Changes:

- **Wildcard without schema (38).**
  - A `*` over a table with differing scanned definitions now supplies the names every definition has.
    These names carry through SELECT INTO copies and CTEs, and the lookup is inferred, noted "by name".
    This covers the empty-copy-then-CTE shape (28 traces).
  - A positional INSERT into a SELECT ... INTO table uses that SELECT's column order. It does not when an
    identity, computed or rowversion column may have been copied; a CTE or derived source also leaves
    it unsettled.
  - A by-name lookup past an unexpanded `*` relies on output names being unique (CTE, derived table,
    SELECT INTO, view).
  - A call of an inline table-valued function is read as its RETURN query, with the arguments in place
    of the parameters (inferred; cites the function). Multi-statement functions stay gaps.
- **Ambiguous column (10).**
  - `ALTER TABLE … WITH CHECK|NOCHECK ADD col` is now read.
  - An unqualified column with one source and nothing outside the query binds to that source, even when
    its scanned definition lacks the column. The gap is then `missing_definition` ("no column … in its
    known schema"), not "could not be bound".
  - Joins with two candidates stay ambiguous.
- **INSERT … EXEC (8).**
  - A #temp table is private to the procedure that creates it and its callees. An INSERT … EXEC or
    dynamic-SQL issue in another procedure with its own same-named #temp no longer applies. This was the
    archive-procedure shape behind 330 gap ids.
  - INSERT … EXEC of a named procedure is mapped by position from the callee's one row-returning
    statement (writer kind `insert_exec`, inferred; cites the call). It stays a gap when the callee has
    zero or several result statements, nested EXEC or dynamic SQL, unparsed statements or several
    definitions, and when the INSERT has no column list and the target has no declared order.
- **Missing definition (35, six backup-style tables).** Unchanged by default: no definition and no
  writer is a gap. `--declared-terminals` can now accept such a table as a terminal for the run (a
  declared stop), so the scope decision can be recorded without changing the tool.

### Adversarial review 6 of these rules (tests/test_column_review6.py, fixtures/column/review6/)

Eight high-severity findings, all fixed. Each test fails on the code before the fix.

- **H1.** A UNION branch after a `*` of unknown width was dropped silently; now it is a gap. This also
  closes an older silent drop of a later branch with `*`.
- **H2–H5: inline functions.**
  - The body is now isolated from the caller: its own names never see the caller's sources.
  - Argument columns must be qualified by a caller alias. They are bound under private aliases that the
    body cannot capture.
  - Only a definition in the caller's database is inlined.
  - CTEs in the RETURN query are used.
- **H6.** An INSERT … EXEC callee stays a gap when its body can return other rows: DML OUTPUT without
  INTO, FETCH without INTO, DBCC, RESTORE, READTEXT, RECEIVE. This is checked on the procedure text.
- **H7.** A callee's read of a #temp it does not create resolves to the calling procedure's #temp only.
- **H8.** A #temp created only under IF / WHILE / CATCH (`IF OBJECT_ID('tempdb..#t') IS NULL CREATE
  TABLE #t`) no longer hides the caller's same-named table, both for issues and for writers. This also
  closes an older false complete.
- **Low.**
  - The single-source binding fallback is not used in row-selection clauses (an ORDER BY alias).
  - A DEFAULT argument blocks inlining.
  - `SELECT IDENTITY(...) INTO` blocks the copied column order.

## After the a1bbf08 run (80 / 4 / 94; wildcard still 39 traces)

- **SSMS-scripted CREATE TABLE was not read.**
  - `TEXTIMAGE_ON` (scripted for every table with `(max)` or xml columns), `SPARSE`, `ROWGUIDCOL`,
    `MASKED WITH`, `ENCRYPTED WITH` and table `WITH (...)` options made the statement fall back to a
    command, so the table had no column list. Every `*` over it and every positional INSERT into it was
    then a gap. This is the most likely cause of the empty-copy-then-CTE shape that the earlier
    differing-definitions fix did not close.
  - These options are now blanked and the statement parsed again. If it still fails, the column list
    is read from the text.
  - An inline `INDEX` clause is no longer read as a column named `index`.
  - Temporal `GENERATED ALWAYS AS ROW START/END` columns are skipped in positional matching.
  - Tests: `tests/test_column_ssms_ddl.py`.
- **Diagnostics** (anonymous counts in gap-summary.json):
  - `scan.create_table_options_blanked`, `scan.create_table_read_from_text`, `scan.create_table_unread`;
  - `totals.wildcard_traces_by_unexpanded_star_reason`, which says why each value-path `*` could not be
    expanded: no definition, differing definitions, a copy of an unexpandable source, a temp table
    created elsewhere, a function source, …

## After the 821e657 run (same 80 / 4 / 94; identical step ids)

- The new diagnostics showed the real cause of the remaining wildcard gaps. The CREATE TABLE fixes
  engaged no definition here (all three counts 0). The reasons were:
  - `select_into_copy_of_an_unexpandable_source` (28 traces);
  - `written_but_no_definition_in_selected_sources` (10);
  - `function_or_external_source` (2).
  The copied tables have no CREATE TABLE in the selected sources.
- **Fix:** a column that a scanned INSERT column list, UPDATE SET or MERGE names certainly exists. It is
  now one of the names a `*` over that object certainly supplies, so a lookup by name through the `*`
  binds it (inferred, "by name"). Positions stay unknown, so positional uses remain gaps, and so do
  names that no writer mentions. Tests: `tests/test_column_no_definition.py`.
- The reasons now go one level deeper (`select_into_copy_of_an_unexpandable_source/<inner reason>`).
  A function source says why it was not inlined (another database, no inline definition, an unqualified
  argument column, …).
- **Batched with that fix** (so one run tests more than one hypothesis):
  - More existence proof for names a `*` certainly supplies:
    - a qualified read anywhere in the scanned SQL (`s.col_a` with `FROM dbo.T AS s`), where the alias is
      not also used for a CTE, derived table or another object;
    - a package destination's column mapping.
    Still names only, never positions.
  - `gap-summary.json` → `totals.value_gap_traces_by_reason`: anonymous reasons per value-path gap kind.
    - `insert_exec`: callee has several result statements, no definition, nested EXEC, …
    - `positional_without_schema`: target has no definition, SELECT INTO order not settled, …
    - `ambiguous_column`: several joined candidates, inner-or-outer, …
    - `missing_definition`: column not in the scanned definition, object without definition or writer, …
  - For a quick check, rerun only the affected columns with `--columns-file`.

## After the 83253bf run (81 / 4 / 93; wildcard 39 → 2)

- Existence proof closed 37 of the 39 wildcard traces: 874 new steps, and spot checks were correct.
  The 2 left are multi-statement table-valued functions (or ones not scanned).
- Using the new reason counts:
  - Inner-versus-outer binding: when the scanned code proves the inner source has the column, the inner
    one binds (SQL binds the innermost source that has it); inferred.
  - INSERT … EXEC with no column list into a SELECT ... INTO table now uses that table's column order.
- What is left needs evidence or decisions rather than code:
  - writer order (54): a SQL Agent export;
  - dynamic SQL (53): assertions or runtime capture;
  - backup tables without a definition (35): a scope decision, then `--declared-terminals`;
  - column not in the scanned definition (8): the deployed definition;
  - linked-server objects (8): their definitions;
  - several joined candidates (4) and positional INSERT into a target with no definition (2): the
    missing table definitions.

## After the 0f220a7 run (81 / 4 / 93; no step changes)

- **`--job-scripts PATH`** (`src/column_lineage/jobs.py`; tests: `tests/test_column_jobs.py`).
  - The tester found 10 local SQL Agent job scripts; they are not confirmed as current production.
  - The reader takes SSMS-style `sp_add_job` / `sp_add_jobstep` / `sp_update_job` / schedules.
  - When every writer of a column runs from steps of one enabled job, the order follows the job's
    success chain, and the call order inside each step. Inferred: the job step is cited and the page says
    the script is a static copy.
  - Otherwise the writer-order gap carries a reason: different jobs, a writer only in a disabled job,
    several steps or paths, procedures not in the scan, no job scripts, an entry point that does not
    settle it, or package and SQL writers. gap-summary counts these.
- **Adversarial review 7 of the existence proofs** (`tests/test_column_review7.py`; each test fails
  before the fix):
  - An alias a statement also uses for a CTE, table variable or function no longer proves a column on
    a real table.
  - Write and read proofs are not used for a #temp that is dropped and re-created.
  - `ALTER TABLE … DROP COLUMN` cancels a proof.
  - `SET udt_col.Prop` is not a column.
  - A bare `IDENTITY` is now an identity column (older bug; positional INSERT bound the wrong value).
  - Chained `SELECT *` CTEs are no longer exponential (depth 16: 9.9 s → 0.3 s).
  - `ALTER … ADD … IDENTITY` blocks a SELECT INTO order.
  - A package destination whose database is unsettled proves nothing.
