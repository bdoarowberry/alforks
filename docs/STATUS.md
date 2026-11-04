# Column lineage: status and next steps

## Where it stands

- Engine: `src/column_lineage/`: catalog, control, analyzer, tracer, dynamic_eval, assertions, jobs,
  compare, cli, render_markdown, render_overview, gap_summary, and the SSIS adapter (ssis, ssis_xml,
  ssis_expression).
- Contract: `docs/column-lineage-design.md`. Usage: `README.md`. Handover: `docs/HANDOVER.md`.
  What it cannot settle: `docs/column-lineage-limitations.md`.
- Tests: 394 passing (`PYTHONPATH=src python -m pytest -q -p no:cacheprovider`): acceptance and
  messy-pattern tests, renderer and overview tests, regressions from adversarial reviews 4 to 8
  (`tests/test_column_review*.py`, fixtures under `fixtures/column/review*/`), SSIS adapter tests,
  gap-shape, earlier-state, dynamic SQL, assertion, job-script and corpus-generator tests.
- Synthetic ~3,000-object corpus (`tools/generate_corpus.py --objects 3000 --seed 42`), 20 known chains:
  - precision 1.0, static recall 0.975;
  - about 20 s per run, most of it parsing.
- Synthetic scale check, extended (generator 1.1): the same command now also writes 28 extension chains
  (14 kinds x 2 variations: earlier-content reads, INSERT ... EXEC, inline TVF via APPLY, * by name,
  positional INSERT into SELECT ... INTO tables, SQL Agent job ordering with scripts under `jobs/`, multi-column
  UPDATE with CROSS APPLY, ALTER TABLE ... ADD) whose manifest entries state the expected gaps, stops and
  evidence; `tools/perf.py` passes `--job-scripts`, checks them and scores a wrongly removed gap as a miss.
  The 20 legacy chains and their files are byte-for-byte unchanged (precision 1.0, static recall 0.975).
  Seeds 42 and 7: all chains precision 1.0, expectation checks 25/28; the 3 misses are tool limits (below).

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
- (Fixed) `INSERT INTO #t (...)` with `EXEC proc` on the next line, and `CROSS APPLY dbo.fn(p.id) AS x`
  without a subquery wrapper, were found by the extended corpus (both variants of `insert_exec_one`,
  `insert_exec_two` and `itvf_apply` now pass; expectation checks 28/28, static recall 0.99 over 48
  chains).
- Dynamic SQL that builds its projection from metadata cannot be followed. This is by design: it is
  reported as a `dynamic_sql` gap at the EXEC (its detail now says which piece is unknown).
- A dynamic SQL issue's literal "targets" are guessed from all string literals in its scope; text like
  `'INSERT INTO ' + @t` yields the bogus target `into`, and an issue with any target is not reported as a
  scope-level gap. Unchanged by the evaluator (it only matters when evaluation is incomplete).

## Still to do

- Robustness review: a reviewer was checking encodings, huge files, deep nesting and symlink
  loops, but its results were not collected. Rerun it.
- Performance: parsing dominates. The parallel parse (`--jobs`, worker processes from 20 files) is
  done. Still open:
  - a pickle cache (about 2.4x on small files);
  - a cheaper `mask_noncode` in `table_lineage/index.py`, which runs 4–5 times per statement.
- Renderer: a per-step scope field (steps carry `procedure` but no separate scope). Done since this
  list was written: CASE condition/value pairing (input branch labels `WHEN ... THEN` / `ELSE`),
  gap-to-step links on the detailed page, `docs/column-lineage-limitations.md`, and the handback
  package (`docs/HANDOVER.md`, sections 1 and 5).

## After the tester's review of overview v2

- Assertion provenance: assertions record `origin` (ai_assisted / person) and `review` (unreviewed /
  reviewed); pages say "recorded in an assertions file, not proved from code" plus who made it and its
  review state, or "not recorded" (no longer "a person's statement").
- Mermaid labels carry no entity codes (`#`, `|`, brackets render raw; checked with Mermaid 10 in a
  browser); identifiers are no longer cut short in tables; the "more families" node is labelled as a
  summary, not a source.
- Plain answer = four bullets: headline, source columns (full names, grouped by how routes end, with
  route counts, last-step and whole-route status; about 8, unresolved columns kept first, then a link
  to the full list), source families with family links (each named once), value rules. Tester check
  of f62290a (3 pages, 35 columns): groups and route counts right; every column read "mapping
  verified" although routes crossed asserted links, and 3 unresolved columns were hidden behind the
  link; both fixed (test_column_overview_sources.py).
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
  - A DEFAULT argument blocks inlining. (fixed: the declared default is used)
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

## Job scripts, extended

- **Package steps:** SSIS job steps (`/ISSERVER`, `/FILE`, `/SQL`) and T-SQL steps calling
  `SSISDB.catalog.create_execution @package_name` are matched to package writers by file name. A
  package load and a SQL fix in one job are now ordered, which settles the adapter's
  `package_and_sql_writers` gap.
- **Chained jobs:** a job started by exactly one `sp_start_job` step, and with no enabled schedule of
  its own, runs after the earlier steps of the job that starts it. `sp_start_job` does not wait, so the
  started job overlaps the later steps (`writers_overlap_in_chained_jobs`).
- **Run context:** each writing step says which job and step run it, with the schedule in words, or
  that only a disabled job runs it, or that no job in the scripts runs it (a hand-run or legacy copy?
  context only, never dropped). JSON: `run_context`. Overview: a "Run by (job scripts, static copies)"
  line. gap-summary: `writer_steps_by_job_context`.
- System procedures in job steps (msdb, master, unresolved `sp_*` / `xp_*`) are ignored.
- Tests: `tests/test_column_jobs.py` (11).

### Adversarial review 8 of job ordering (tests/test_column_review8.py; 14 cases failed before)

Ordering is now refused unless the job's whole run is understood. Each reason is recorded:
- **Steps the tool cannot follow:** CmdExec / PowerShell steps; T-SQL commands with inline writes,
  IF / WHILE / RETURN / THROW / TRY, dynamic EXEC or `EXEC @proc`.
- **Job flow:** step loops; failure branches (`on_fail_action` 3/4, kept for run context only);
  asynchronous SSISDB starts (not SYNCHRONIZED); `sp_start_job` at a named step; cycles of
  `sp_start_job`.
- **Scripts changing the job:** jobs defined twice, deleted, or with deleted steps; arguments that
  could not be read (no crash on `@step_id = @n`).
- **Packages:** names matching several package copies, or another project's package.
- **Calls:** a conditional call or write between the job step and the writer.

The reader now:
- applies `sp_update_jobstep`, and `sp_update_job` from any file (job names are global);
- attaches shared schedules by name;
- ignores commented-out `sp_start_job` and SSISDB calls;
- follows `USE` inside a step command, and runs a step without `@database_name` in master;
- orders procedures and packages in one command by their position;
- reads child packages run by a parent package's Execute Package task;
- reads arguments containing function calls and string concatenation.

Diagnostics for the 54 "writer not run by any job script" traces:
- `scan.job_scripts` adds `step_procedures` (resolved / other_database / unresolved /
  several_definitions), `jobs_by_problem`, `procedures_reached_from_job_steps` and
  `packages_named_in_steps`;
- writer-order gaps record `writers` and `writers_in_job_scripts`;
- gap-summary adds `writer_order_gaps_by_writers_in_job_scripts` (none / some / all).

## While waiting for job definitions and history

- Anonymous reasons for the two biggest evidence gaps, in `gap-summary.json` →
  `value_gap_traces_by_reason`:
  - `dynamic_sql`: `<where on the path>/<why not evaluated>`, e.g.
    `dynamic_sql_names_this_object_as_target/built_from_a_table_or_metadata_read`,
    `procedure_on_path_builds_target_at_runtime/parameter_without_caller`. The "why" categories come
    from the evaluator's reasons (table or metadata read, parameter without caller, variable scoping,
    loop, system function, procedure name in a variable, too many alternatives, control flow, …). They
    say which traces an assertion could settle and which could be evaluated with more scanned callers.
  - `external_object`: `linked_server_to_a_scanned_database_name` (then `--local-server` would close it;
    the page says so) or `linked_server_to_a_database_not_in_scan`.
- Reviews 9–12 (whole-system interactions, robustness, business-reader pages, docs vs behaviour) and
  an extension of the ground-truth corpus to the newer features were run as parallel agents; findings
  are recorded below as they are fixed.

## Robustness review (review 12)

Fixed in the build after 2829077: the four high items and the medium items marked (fixed) below;
the rest remain open. Tests: `tests/test_column_robustness.py`.

Invented inputs only; generators and timings under the session scratchpad are not kept. Fix in this
order (the first two are realistic on a real estate):

High
- (fixed) **A single very large SQL file is effectively a hang.** Per-file work is quadratic: `catalog._procedures`
  calls `procedure_scopes()` (a full-file regex scan) once per procedure, and `table_lineage.index`
  `context()` re-runs `GO.finditer` for every statement. Measured: 3,000 procedures in one file 76 s;
  10,000 statements in one procedure 177 s; a 20 MB file did not finish in 30 min. An SSMS "Generate
  Scripts" single-file export would not finish. Fix: compute procedure scopes and GO positions once per
  file (both are pure functions of the file text).
- (fixed) **CR-only line endings plus a `--` comment hide the whole file.** `mask_noncode` ends a line comment at
  the next `\n`; with CR-only endings the first `--` masks everything after it, the file yields 0
  statements, and nothing counts it (the masked text is blank, so it is not even in
  `files_without_statements`). Fix: normalise lone `\r` to `\n` in `decode_source` (also fixes line
  numbers for such files).
- (fixed) **Crash on long column names.** A 128-character table and column make an output file name longer
  than 255 bytes; `cli.py` raises `OSError` and the run aborts with no index or diagnostics written. On
  Windows the same happens earlier through MAX_PATH. Fix: cap the stem (about 120 characters) and append
  a short hash; catch `OSError` per trace and record it in `file_errors`.
- (fixed) **A nonexistent `--job-scripts` path is ignored silently** (`jobs.py` swallows `OSError`); a bad
  `--source-root` errors, this should too.

Medium
- (fixed) A RecursionError in sqlglot (expression nesting around 50 deep) drops **every** statement of that
  file (`index.add` catches only `SqlglotError`/`ValueError`); it should make one fallback statement.
- (fixed) Output stem collisions: `stem()` maps `[a b]` and `a_b`, `col-1` and `col_1` to the same file, and the
  index then points several columns at one trace. Fix: append a hash when sanitising is lossy.
- (fixed) Overlapping `--source-root`s (a folder and its sub-folder, or a folder and a file in it) index the
  same file twice, which duplicates writers and can add a spurious writer-order gap. Fix: dedupe by
  resolved path across roots.
- (fixed) One NUL byte in the first 8,000 bytes makes `decode_source` treat the file as UTF-16, so the file
  yields nothing (counted only in `files_without_statements`). Fix: require a UTF-16 pattern (BOM or a
  high share of NULs), else strip stray NULs.
- Default directory excludes (`.git .venv .pytest_cache .pytest-run-* node_modules bin obj`) are silent
  and undocumented; SSDT puts generated deployment scripts under `bin\`. Fix: count excluded files in
  the scan diagnostics and document the list.
- An INSERT with 50,000 VALUES rows gives one step per row (78 MB JSON) and every downstream column
  inherits them. Fix: collapse runs of literal rows into one step with a count note.
- (fixed) `_create_table` runs one regex per column over the whole statement (O(columns²)); harmless below the
  1,024-column limit but easy to precompute once.

Low
- (fixed: both are errors now) `--output` pointing at an existing file gives a traceback; `--columns-file` accepts lines that are not
  columns (validate 2–4 dotted parts or `#temp.col`); stale `*.lineage.*` files from an earlier run are
  not marked; symlinked directories are never followed (document); `--job-scripts` `rglob("*.sql")` is
  case-sensitive on Linux.

What behaved correctly: all encodings tried (UTF-8 ± BOM, UTF-16 LE/BE ± BOM, cp1252, invalid bytes),
CRLF and mixed endings, every GO placement, 20,000 small files (32 s; `--jobs 1` and `--jobs 8`
byte-identical), malformed and huge packages (all counted as unreadable), odd column quoting, repeat
runs byte-identical.

## After the 2829077 run (47 / 3 / 128)

- 35 traces moved from reached-boundary to incomplete, all through the linked-server boundary fix:
  `[alias].[db].schema.table` where `db` is a scanned database (109 gaps say
  `linked_server_to_a_scanned_database_name`). Earlier runs silently counted such reads as reaching
  the database boundary. If the alias is this server, `--local-server <alias>` restores those paths as
  local reads, now as a stated setting rather than an assumption. Steps were unchanged (0 added, 0
  removed); the assertion fixes moved nothing.
- Possible-writer families appeared on 27 detailed pages, counts matching the dynamic-SQL gaps; the
  label now reads "possible untraced writers (…): not sources, open points", and the diagram never cuts
  open-point families into the summary node.
- Windows: `tools/perf.py` no longer imports the Unix-only `resource` module unconditionally.
- Assertions on the estate: 170 used, all AI-assisted and unreviewed; 171 traces rest on at least one.
- Alias run (conditional: the alias is believed, not confirmed, to be the scanned staging instance):
  79 / 4 / 95; 32 of the 35 traces returned to reached-boundary. The remaining `external_object` gaps
  (29 traces) are mostly a linked database outside the scan.
- "Main evidence needed" no longer says "Nothing" when every route reaches the boundary: it says no
  further source definition is needed, then lists what is still to confirm before the routes count as
  verified (asserted mappings and their review state, the production identity of boundary objects, a
  linked server treated as local by `--local-server`, declared terminals).
- (fixed) 1,270 step ids changed between builds 2829077 and c67a066 with no writer change. Cause: the
  CR normalisation turned the stray CR of `\r\r\n` line endings (a file converted twice, common on
  Windows) into a line break, renumbering every line and so every step id (ids hash the evidence line).
  Now only files with no LF at all are normalised. Test: `test_double_cr_line_endings_keep_line_numbers_and_step_ids`.
  Step ids on the estate should return to their 2829077 values.

## Scenario runs (`--scenario`, tests/test_column_scenario.py)

Optional per-procedure parameter values exclude the branches they make unreachable in that procedure
(IF arms, statements after a certain RETURN), listed with the reason; uncertain branches are kept; a
table whose writers are all excluded stops with `scenario_not_written` (existing rows read; their
origin not determined). Pages state the values as assumptions and list upstream writers in other
procedures whose run order needs job or run evidence. Without `--scenario` nothing changes (corpus:
48/48 traces identical). Invented acceptance case: `fixtures/column/scenario/sql/publish.sql`.

Adversarial review of 2a7af58: 13 confirmed defects, all fixed (cases in `review.sql`; 13 of the new
tests fail on 2a7af58). Five excluded statements that run: one-line `PRINT ... END` / `... ELSE`,
`CASE ... END` over lines in an unindexed statement, a WHILE header swallowed by the statement before
it, `BEGIN DIALOG` taken for a block (now own statement ends plus a sanity net); values not converted to
the declared type (truncation, rounding, SET values); accent/width-insensitive equality decided;
`ANSI_NULLS OFF` ignored; `inf`/`nan`/`1_0` read as numbers (and a crash); evaluated dynamic SQL in an
excluded arm still traced (dynamic SQL is now evaluated before filtering); per-table instead of
per-column `scenario_not_written`; CREATE TABLE counted as a read; two scenarios for one procedure
merged silently.

Tester trial of 028ee99 (178 targets, real entry procedure): the scenario did not engage (0 excluded,
1 alignment note) and the overview still said "Only what these values allow is shown". Cause: after a
call with a trailing comment and no semicolon, the index starts the next statement at its line start,
the walk at its first word; exact offsets were compared. Fixed by comparing anchors; also a bracketed
`[End]` inside CASE, a WHILE indexed as its own statement, and an unparsed `RETURN INSERT` merge (which
would have dropped the INSERT's parse_error gap). Sweep: 1142 corpus procedures and every fixture
procedure now line up (GOTO procedures excepted, by design). A scenario that cannot be applied now says
so on both pages (`applied: false`).

Tester trial of 5060ed2 (three scenarios, 178 targets; 0 traces changed, as expected for this estate):
an excluded variable assignment crashed (`Assignment.name`; fixed); a parameter assigned anywhere in a
TRY was unknown from the TRY's start, keeping an early decidable test uncertain (the TRY body now runs
in order; only the CATCH sees possible changes); the pages did not separate writers run by the
invocation from separate writers, and claimed "only what these values allow is shown" when nothing on
the trace was excluded. Pages now list writers on the trace run by the invocation (call graph from
reachable lines) and those not run by it, say when nothing is excluded on the trace, and, for an
invocation that only reads existing rows, say that their origin is not determined. Fixture
`invocation.sql`.

Tester trial of eaa8e45: regular 2/178 traces changed (11 excluded, 0 uncertain), interim 0 (3, 3),
reuse 2 (11, 0); no excluded statement could run. Open: in reuse, 7 calls and 27 of 28 reads after a
certain RETURN were still listed as the invocation's. Not reproduced on the invented shape or five
variants. Calls, reads and invocation membership now require the walk to have reached the statement;
`statements.neither_lines` and a several-definitions line on the pages show the remaining causes.
Rerun (08e39ab): 1 definition, 91 reached / 12 excluded / 0 neither, still 7 calls and 27 reads after
the RETURN. Cause (tester's diagnosis): the RETURN sits in a TRY whose CATCH re-throws, and THROW was
treated as falling through, so the statements after the TRY/CATCH looked reached. THROW now ends the
path like RETURN. Verified by the tester: 0 calls and 1 read after the RETURN; regular and interim
runs unchanged. Their remaining point: reuse traces were labelled `complete_within_selected_scope`;
the result is now `not_written_in_scenario` when the target itself is not written.
Wording follow-ups (fba7692, 20134f0, 9529385): reuse pages ask for evidence from the run that
populated the existing rows and make no route, value-rule, row-selection or open-point claims; the
tester found no remaining sentence implying how those rows were produced.
Saved 178-target comparison against the unconstrained run of the same build: regular rebuild removes
6 steps (two variable, two constant, two fallback-expression) from excluded branches in 2 traces,
`via` changes only there; interim rebuild changes nothing; both keep 47 / 3 / 128 and every gap-kind
count.
Cross-build comparison (unconstrained, current build vs the last batch before 83f62d1; 3,002 files,
35,378 statements): 47 / 3 / 128 both, every gap-kind count equal, dynamic_sql reasons unchanged
(23 object name passed / not evaluated, 39 table or metadata read, 2 expression not evaluated), 0 step
ids added or removed, `via` set on 1,278 step instances (4 spot-checks correct). The sqlleaf-derived
rules found nothing to act on in this corpus: no opaque scalar functions, no sp_executesql parameter
lists on the blocking paths, no inline TVF derived nodes under CROSS APPLY. Next lever: what keeps the
23 + 2 "expression not evaluated" cases from evaluating (invented shapes requested).
Answer: one cause for all 25 traces (86 + 1 gap records at two EXEC sites). `EXEC (@sql)` without a
semicolon, a comment, then `SET @x = ...` on the next line was indexed as one statement (an EXEC only
ended at RAISERROR/RETURN/IF/ELSE/BEGIN/END), and the evaluator read the SET as an `AT`/`AS` suffix
("runs elsewhere or as another user"). Fixed in the shared index: a statement keyword at a line start
ends an EXEC; the "runs elsewhere" reason is now given only for AT/AS. Fixture
`fixtures/column/dynamic/exec_then_set`; 801 fixture traces: only that fixture's 3 changed; corpus 48/48
identical.
Rerun (4063159): 47 / 3 / 128 unchanged; 0 step-id changes; statements 35,378 -> 35,728; the false
reason is gone. The 23 traces now say "call chain deeper than 8 procedures" (a defect: the check
counted expression nesting, so a long concatenation looked like a deep call chain) plus a date
converted inside a quoted WHERE literal; 4 traces show SQL assembled in a WHILE loop and one EXEC runs
SQL returned by a helper (both genuinely not evaluable). Fixed: procedure calls are counted separately
(expressions may nest 120 deep), and an unknown date or number inside a quoted WHERE/ON/HAVING literal
is read with a placeholder (open evaluation, row-selection gap). Fixture
`fixtures/column/dynamic/literal_placeholder`.
Rerun (2b54c93): 47 / 3 / 128; 0 step-id changes; the 23 traces now stop at "the evaluated SQL contains
'(' which is not analysed": the built SQL is one parenthesized result-set query, `(SELECT DISTINCT ...
WHERE ... = '<date>')`. Fixed: such a string is unwrapped before indexing; the gap for an object name
passed to a procedure whose SQL is known except a filter value now says so (row selection only).

## T-SQL construct probe and sqlleaf comparison (tests/test_column_tsql_constructs.py)

Fixed (each was a false complete, a wrong binding or an unexplained end):
- PIVOT / UNPIVOT outputs were bound as phantom columns of the source; now an `unsupported_statement` gap.
- `IDENTITY(int,1,1)` in SELECT INTO bound `int` as a column; `NEXT VALUE FOR seq` bound the sequence as a
  column. Both are now generated-value ends.
- Safety net: an INSERT that omits a column no definition lists and no statement names no longer
  completes it as "default or NULL"; it is a `missing_definition` gap.
- A cursor over a UNION read only the first branch; every branch now feeds the FETCH variables.
- MERGE (or other DML) nested in `INSERT ... SELECT FROM (MERGE ... OUTPUT ...)` registered only the outer
  target; now a gap on every table involved.
- `VALUES (DEFAULT, ...)` now gives the declared default; CHOOSE keeps its choices (index as a condition,
  NULL when out of range); `LAG/LEAD` keep their default; literal-only expressions (`CAST(NULL AS int)`,
  `GETDATE()`, `CONVERT(date, '19000101', 112)`) record a constant end.
- `CROSS APPLY (VALUES (a,'x'),(b,'y')) AS v(val, name)` (the unpivot idiom) is traced.
- Dynamic SQL evaluation: `CONCAT_WS` was evaluated as `CONCAT` (wrong table name, so a wrong source);
  `ISNULL(@v, ...)` ignored truncation to `@v`'s type. Both fixed (ISNULL with an unknown type is now
  "not evaluated").

Ideas from the sqlleaf comparison, done (commits edd5956, 3e2aadc; tests `test_column_sp_executesql.py`,
`test_column_scalar_udf_inline.py`):
- `sp_executesql @sql, N'@p type', @p = arg`: parameters bound to their arguments (named, positional,
  parameter list literal or in a variable). OUTPUT parameters, a missing argument without a default
  and an unreadable list keep the `dynamic_sql` gap.
- A scalar UDF whose body is one `RETURN` of an expression reading tables is read from that body,
  isolated from the caller, with arguments read in the caller's scope. Other bodies stay opaque.
- `DEFAULT` arguments of scalar and inline table-valued functions use the declared default. An
  opaque call no longer reads `DEFAULT` as a column (a false `missing_definition`).
- Inputs carry `via` (the function chain), shown on the detailed and overview pages.
- `classify()`: arithmetic containing a CASE with a string literal is no longer labelled "string".

Adversarial review of these rules (15 confirmed defects, all fixed; regression tests in the two test
files fail on 3e2aadc):
- sp_executesql: `EXEC p @id = @id` had its argument name rewritten (false constant); a parameter
  reassigned in the batch, a batch variable named like the caller's, and nested `EXEC (...)` reading
  the caller's variable were false completes; an unquoted word became a column; `s.a-@p` with `-1`
  became a `--` comment; `@p = DEFAULT` was a column. All refused or handled as described in the design.
- Scalar inlining: a stale definition was inlined when an ALTER (or a copy with other parameter
  order or defaults) differed; now one definition text is required (also for inline TVFs). Two inline
  TVF calls in one statement (or in inlined bodies) shared a derived node id (`@-1`), so one call's
  inputs were lost (also before these commits); nodes are now placed by call position. A nested CTE
  step reset the enclosing step's `inferred` flag and leaked its notes and evidence; inlining had no
  budget (exponential) and duplicated row selections.
- `via` was cached per parser class (TRY_CONVERT/CONVERT, LTRIM/RTRIM mixed up); now the written name.
  `classify()` labelled `ISNULL(a,'') + ISNULL(b,'')` arithmetic; now "string" when an operand can
  yield a string literal.
- Step ids: across 719 fixture traces only the new fixtures, one APPLY trace and one function with
  two definitions changed; on the corpus one step (a derived node under CROSS APPLY, `@-1` → its
  position). On the estate, step ids of derived nodes under `CROSS APPLY fn(...)` will change once.

Expected effect on the estate: only traces whose blockers are `dynamic_sql` through `sp_executesql`
with a parameter list, or `opaque_function` for single-RETURN lookups, can move; step ids of other
traces are unchanged (corpus check: 48/48 traces with identical ids). Not yet done: per-route ids
diffed between runs; one whole-run graph export; FORMATMESSAGE; WHILE-loop unrolling.
