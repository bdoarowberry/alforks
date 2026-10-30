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
| `--source-root DIR ...` | Files or folders to scan (`.sql .tsql .ddl .prc .proc`). |
| `--database-map PATH=DB ...` | Files under `PATH` (relative to a source root, or absolute) belong to database `DB`. Use this when a repository has one folder per database and the code uses two-part names. |
| `--default-database DB` | Database for two-part names with no other context. |
| `--default-schema S` | Schema for one-part names (default `dbo`). |
| `--stop-at-database DB ...` | Stop when the trace reaches a column in one of these databases. |
| `--stop-at-object GLOB ...` | Stop at matching objects (`db.schema.name`, case-insensitive). |
| `--max-depth N` | Stop after N mapping steps. |
| `--trace-row-selection` | Also trace filter and join columns upstream (larger output). |
| `--gap-summary` | Also write `gap-summary.json`, a summary with no names in it, safe to share. |
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

- `<column>.lineage.md` — the review page: identity and result, short answer, value
  lineage tree, row-selection context, detailed steps with evidence, gaps.
- `<column>.lineage.json` — the full trace (`column-lineage/1.0`). The page is generated
  from it and can be regenerated with `python -m column_lineage.render_markdown <json>`.

Once per run: `scan-diagnostics.json` (parse statistics, files that failed, every
statement-level issue with its location) and, with `--gap-summary`, `gap-summary.json`.

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
- **Coverage:** for metadata-driven SQL, list **every** branch the metadata can produce, not
  only one observed run.
- **Privacy:** an assertions file names real objects. It belongs with the source and must not
  be shared outside it.

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
