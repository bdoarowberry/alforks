# Column lineage: handover

A static, evidence-backed column-lineage tool for T-SQL code and (optionally) SSIS packages. It reads
source files only: nothing is executed and no database is contacted. Every claim it makes cites a file
and line; where the code cannot prove something, it says so and names the evidence that would.

This note is for whoever runs the tool or reads its results next. Contract details are in
`column-lineage-design.md`; options in `../README.md`; history in `STATUS.md`.

## 1. Running it

Python 3.11+, `pip install -r requirements.txt` (SQLGlot 30.x). From the repository root:

```bash
PYTHONPATH=src python -m column_lineage --columns-file targets.txt --source-root <sql roots...> --default-database <db> --database-map <folder>=<db> ... --output out --gap-summary
```

Options that matter on real estates:

| Need | Option |
|---|---|
| Many target columns | `--columns-file FILE` (one `db.schema.table.column` per line) |
| Which files belong to which database | `--database-map PATH=DB` (repeatable), `--default-database` |
| Linked servers that point at scanned databases | `--local-server NAME` |
| Leave out copies that are not on the execution path | `--exclude GLOB` |
| SSIS packages | `--package-root DIR`, `--package-include/--package-exclude GLOB` (select the deployed copies), `--connection-map NAME=DB` (only when documented) |
| Steps the code cannot prove (dynamic SQL, external loads) | `--assertions FILE` (each scoped to a writer; record `origin`: ai_assisted or person, and `review`: unreviewed or reviewed; pages show both, or "not recorded") |
| Sources the reviewer accepts as terminals for this investigation (also tables with no definition or writer) | `--declared-terminals FILE` |
| Where to stop | `--stop-at-database DB`, `--stop-at-object GLOB` |
| Run order fixed by an entry procedure's calls | `--entry-point PROCEDURE` |
| Run order from SQL Agent job scripts (SSMS "Script Job as"; inferred, a script is not proof of the deployed job) | `--job-scripts PATH` |
| Business definitions | `--business-glossary FILE` (approved / proposed) |
| Sharing results without names | `--gap-summary` (writes `gap-summary.json`) |

Compare with known lineage: `python -m column_lineage.compare --expected expected.json --traces out`.

## 2. Outputs per column

- `<column>.overview.md`: short page for business readers (about 60–150 lines). First screen: field and
  definition (approved / not approved / proposed interpretation), plain answer, coverage and proof line,
  up to three evidence requests, a Mermaid diagram of route families (alternatives side by side, each
  box saying where proof stops), and a sources table with database, schema, table and column. Then
  what changes the value, which rows are included, and evidence and uncertainty, each linked to the
  exact steps. Layout brief: `presentation-brief.md`.
- `<column>.lineage.md`: complete technical page (every step, branch, condition, evidence line, gap).
- `<column>.lineage.json`: the machine-readable source of truth.

Per run: `routes-index.md/.json` (columns grouped by shared sources, writers and route families, plus a
"needs attention" view: unresolved origins, writer-order gaps, assertions awaiting review, definitions
awaiting approval), `scan-diagnostics.json` (scan, statement issues, assertion use), `gap-summary.json`
(counts and anonymized shapes only; safe to share).

## 3. Reading a result

| Result | Meaning |
|---|---|
| `reached_requested_boundary` | Complete; at least one value path reaches the requested boundary, and every other end is a constant or an earlier-content node whose writers are traced on the page. |
| `complete_to_declared_boundary` | Complete; the other ends are terminals the reviewer declared for this run. How those are populated upstream is **not** proved. |
| `complete_within_selected_scope` | Complete within the scanned sources; some paths end at objects with no writer there, files, constants or parameters. **Not** proof of the originating system. |
| `complete_to_requested_depth` | Complete up to `--max-depth`; some paths were cut. |
| `incomplete` | At least one open point on the value path (or its context). The page lists each and the next evidence. |

Step status: **verified** (explicit code), **inferred** (a stated rule: column position, default
database, a caller's temp table, evaluated dynamic SQL, a function summary), **asserted** (a person's
statement in an assertions file), **partial / ambiguous / unresolved** (proof incomplete).
Row-selection conditions (filters, joins) are kept apart from value sources and do not make the value
lineage incomplete.

"Earlier content of X" is the value X had before a write that reads it back (rows preserved and
restored, a working table aggregated into itself, `SET a = a * 2`, `@s = @s + ...`). When X's writes are
traced it adds no new source; when only updates write X and nothing adds its rows, it is an unresolved
origin. Loops that remain listed as cycles are recursive CTEs.

"No writer in the selected sources" means only that: the loader may be a package, a feed, a job in
another repository, or a manual process.

## 4. What the tool cannot settle

These need evidence from outside the source files:

- **Run order between jobs** (`ambiguous_writer_order`): several procedures or packages write the same
  column and which runs last is decided by schedules. `--job-scripts` orders writers that run in one job
  (including package steps and jobs chained with `sp_start_job`), and says which job runs each writer.
  Writers in independently scheduled jobs stay unordered. Needed beyond that: a current SQL Agent export
  (to confirm the scripts) and execution history for the run in question.
- **Dynamic SQL built from data** (`dynamic_sql`): column lists or table choices read from metadata or
  tables at run time. Needed: owner confirmation of the possible texts (then an assertion), or runtime
  capture.
- **External or linked objects** (`external_object`): objects on servers or databases outside the scan.
  Needed: their definitions and loaders, with confirmation that they are the same environment.
- **Origins beyond the last stop**: staging and extract tables with no writer anywhere locally. Needed:
  the package, job, file specification or documented load mapping that fills them.
- **Business meaning**: never derived from names. Needed: owner-approved definitions in the glossary.

## 5. Working with a protected environment

The tool was developed against a real estate it could not see: a tester ran it locally and returned
only `gap-summary.json`, `comparison-summary.json` and a feedback note (counts, generic descriptions,
anonymized SQL shapes). That loop is how every real-data defect was found. Keep it: never share trace
pages, assertions, targets, expected lineage, package XML or connection details.

## 6. Maintaining it

- Tests: `PYTHONPATH=src python -m pytest -q -p no:cacheprovider`. Every real-data defect has an invented
  regression fixture under `fixtures/column/`.
- Synthetic scale check: `python tools/generate_corpus.py --objects 3000 --seed 42 --out corpus`, then
  `python tools/perf.py --corpus corpus --tool column` (precision should stay 1.0).
- Before trusting a new feature, run an adversarial review on invented fixtures: the nine adversarial
  reviews to date each found real false "complete" results.

## 7. State at handover (2 November 2026)

Branch `column-lineage`; 403 tests pass; synthetic corpus precision 1.0 (48 chains, 28/28 expectation
checks). Every real-data defect found so far has an invented regression fixture.

**Real estate (178 target columns, about 1,500 SQL files, SSIS packages, 10 job scripts, 170 unreviewed
assertions), last tester run:** 81 reach the requested boundary, 4 complete within the selected
sources, 93 incomplete. Blockers by trace: writer order 54, dynamic SQL 53, backup tables with no
definition 35, linked-server objects 8, column missing from the scanned definition 8, ambiguous joins
4, `INSERT ... EXEC` 4, positional INSERT 2, table-valued functions 2.

**What the next run should show.** The build after that run (commits `ccdd142`..`6b46992`) changed
behaviour the tester has not yet seen:
- an asserted mapping no longer counts as a row creator, and an asserted writer in another procedure
  keeps the writer-order question open: with 170 assertions in use, some traces may move from complete
  to incomplete (an over-claim stopping, not a regression);
- pages: possible writers whose effect is not traced now appear as a box and an unresolved origin; one
  status label; evidence requests phrased for data owners; counts of input columns, not routes;
- `gap-summary.json` now gives anonymous reasons for dynamic SQL (`<where>/<why>`), linked servers
  (whether the linked database is one in the scan), writer order, and assertion use by review state.

**What needs the business, not code:** job definitions or run history for the 54 writer-order traces
(the 10 local job scripts run none of those writers); owner-confirmed assertions or runtime capture for
dynamic SQL (the new reason categories say which traces an assertion could settle); a scope decision on
the six backup tables (record it with `--declared-terminals`); the deployed definition for the 8
columns missing from the scanned one; confirmation whether the linked server is this server
(`--local-server`).

**Open tool issues:** the robustness findings in `STATUS.md` (review 12), of which two matter on a real
estate: quadratic time on one very large SQL file, and CR-only line endings hiding a file. Neither has
been seen in the tester's runs (about 7 minutes for 178 columns), but a single-file SSMS export would
trigger the first.

**Paste-ready tester message** (counts and invented shapes only, as always):

> New build on `column-lineage` (commit 6b46992). Please run the Windows tests (403 expected) and the
> 178-column batch with the same settings plus `--job-scripts`. Send back: the outcome table and traces
> blocked by gap kind; `value_gap_traces_by_reason` in full (dynamic_sql reasons are new, as
> where/why); `totals.assertions`; steps added and removed; any trace that moved from complete to
> incomplete (expected: some, where an asserted writer shares a column with SQL writers in another
> procedure — give the count and one invented shape); and any page where a new "possible writer not
> traced" box looks wrong.
