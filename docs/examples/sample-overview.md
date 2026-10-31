# Column overview: `reporting.demo.output.result_value`

## Field and result

- **Field:** `reporting.demo.output.result_value`
- **Result:** Not fully traced: 1 open point on the value routes (see Open points); the routes below show what could be followed; 2 end points at the requested boundary (not traced beyond); 1 end point where proof stops.
- **Assertions:** none used; nothing here rests on a person's statement.
- **Confidence:** 3 steps on the value routes — 2 proved from code (verified), 1 partial. Partial = some inputs of the step are not proved.
- End points are where this trace stopped. None of them is claimed as the system where the value originates.

## Business definition

Not confirmed — question for the source owner. (No glossary entry; the tool never derives business meaning from names.)

## Routes: how the value arrives

2 routes: each is the sequence of objects the value passes, from this field back to where the trace ended (← reads "comes from"). Branch labels are the alternatives (IF/ELSE, CASE, MERGE, UNION, package paths) the route passes; they are listed, not combined. Exact expressions are on the [detailed page](sample-trace.md#value-lineage).

**Route 1** (2 ends: 2 boundary reached): `reporting.demo.output` ← `#work (temp table in reporting.demo.load_output)` ← `staging.demo.source`
- Branches on the way: `writer reporting.demo.load_output`
- Changes on the way: conditional choice (CASE/IIF)
- Ends: `staging.demo.source.input_a` (value, and decides between values; via `WHEN s.input_a IS NOT NULL`, `WHEN s.input_a IS NOT NULL THEN`); `staging.demo.source.input_b` (via `ELSE`)
- Proof: 2 verified (proved from code); written by `procedure reporting.demo.load_output`

**Route 2** (1 end: 1 open point): `reporting.demo.output`
- Branches on the way: `IF @apply_override = 1`; `writer reporting.demo.apply_overrides`
- Changes on the way: none (copied or renamed only)
- Ends: `@override_value (in reporting.demo.apply_overrides)`
- Proof: 1 partial (some inputs not proved); written by `procedure reporting.demo.apply_overrides`; open points: SQL built and run at run time (dynamic SQL)

## Rows included

These conditions decide **which rows** are included; they do not compute the value. 3 conditions on 2 objects:

- `staging.demo.source`: join key `s.id = k.id`; filter `s.is_active = 1`
- `staging.demo.lookup_key`: join key `s.id = k.id`

## End points

| Database | Schema | Table / source | Column | Why it stops | Proof of the step reaching it |
|---|---|---|---|---|---|
| `staging` | `demo` | `source` | `input_a` | requested boundary reached (the run was asked to stop here; not traced beyond) (value, and decides between values) | verified (proved from code) ([S3](sample-trace.md#step-9fa99888e121e84d)) |
| `staging` | `demo` | `source` | `input_b` | requested boundary reached (the run was asked to stop here; not traced beyond) | verified (proved from code) ([S3](sample-trace.md#step-9fa99888e121e84d)) |
| (variable) | — | `@override_value (in reporting.demo.apply_overrides)` | — | open point: SQL built and run at run time (dynamic SQL) — not proved | unresolved ([S2](sample-trace.md#step-a92b7a4a25e85778)) |

## Open points

- **SQL built and run at run time (dynamic SQL)** (1 place, affects value): [`@override_value (in reporting.demo.apply_overrides)`](sample-trace.md#gap-2ff3f09fbe22902d)
  - Next evidence: the SQL this statement builds and executes at runtime (a captured example or a confirmed mapping), or the metadata that drives it

## Links

- Detailed page: [sample-trace.md](sample-trace.md) — [short answer](sample-trace.md#short-answer), [value tree](sample-trace.md#value-lineage), [row selection](sample-trace.md#row-selection), [steps and evidence](sample-trace.md#detailed-steps), [gaps and next evidence](sample-trace.md#gaps)
- Full trace (source of truth): [sample-trace.lineage.json](sample-trace.lineage.json)
