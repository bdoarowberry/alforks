"""Scalar functions whose body is one RETURN of a query are read from that body
(fixtures/column/scalar_udf_inline/, invented). The body never sees the caller's sources; each
parameter is the call's argument, read in the caller's scope. Other bodies stay opaque."""
from column_helpers import FIXTURES, inputs, trace

ROOT = FIXTURES / "scalar_udf_inline"
DB = ("--default-database", "demo")


def one(column, tmp_path):
    t = trace(ROOT, f"demo.dbo.report.{column}", tmp_path, *DB)
    return t, t.only_step_for(t.target_id)


def rows(step):
    return {(r["node"], r["expression"]) for r in step["row_selection"]}


def test_lookup_function_reads_its_table_and_the_callers_argument(tmp_path):
    t, step = one("region", tmp_path)
    assert inputs(step, "value") == ["demo.dbo.region.region_name"]
    assert step["status"] == "inferred" and t.complete and "opaque_function" not in t.gap_kinds
    # The unqualified argument "code" is bound in the caller's query, to dbo.sale.
    assert ("demo.dbo.sale.code", "scalar subquery: r.region_code = @code") in rows(step)
    assert any(e["role"] == "function_definition" and e["path"] == "functions.sql" for e in step["evidence"])
    assert step["inputs"][0]["via"] == ["dbo.fn_region_name"]


def test_declared_default_stands_for_default(tmp_path):
    t, step = one("rate", tmp_path)
    assert inputs(step, "value") == ["demo.dbo.fx_rate.rate"]   # "rate" in the body: fx_rate's, not sale's
    assert ("demo.dbo.sale.currency", "scalar subquery: currency = @currency") in rows(step)
    assert t.complete


def test_body_names_never_bind_to_the_callers_sources(tmp_path):
    t, step = one("wrong", tmp_path)
    assert "demo.dbo.sale.amount" not in t.all_ids()
    assert not t.complete


def test_other_bodies_stay_opaque(tmp_path):
    for column in ("multi", "twice"):
        t, step = one(column, tmp_path / column)
        assert "opaque_function" in t.gap_kinds and not t.complete
        assert inputs(step, "value") == ["demo.dbo.sale.code"]


def test_default_without_a_declared_default_stays_opaque_and_is_not_a_column(tmp_path):
    t, step = one("nodefault", tmp_path)
    assert t.gap_kinds == {"opaque_function"}
    assert not any(n.endswith(".default") for n in t.all_ids())


def test_recursion_stops_at_the_inner_call(tmp_path):
    t, step = one("looped", tmp_path)
    assert "opaque_function" in t.gap_kinds
    assert set(inputs(step, "value")) == {"demo.dbo.sale.id", "demo.dbo.region.weight"}


def test_nested_functions_and_wrappers_are_named_on_the_input(tmp_path):
    t, step = one("outer_name", tmp_path / "a")
    assert set(inputs(step, "value")) == {"demo.dbo.region.region_name", "demo.dbo.country.country_name",
                                          "constant:' / '"}
    via = {i["node"]: i.get("via") for i in step["inputs"]}
    assert via["demo.dbo.region.region_name"] == ["dbo.fn_outer > dbo.fn_region_name"]
    t, step = one("wrapped", tmp_path / "b")
    via = {i["node"]: i.get("via") for i in step["inputs"]}
    assert via["demo.dbo.region.region_name"] == ["ISNULL > UPPER > dbo.fn_region_name"]
    page = (tmp_path / "b" / "demo.dbo.report.wrapped.lineage.md").read_text(encoding="utf-8")
    assert "via ISNULL > UPPER > dbo.fn_region_name" in page


def test_caller_variable_with_the_parameters_name_is_the_callers(tmp_path):
    t, step = one("var_region", tmp_path)
    assert any(n.endswith("::@code") and n.startswith("variable:") for n, _ in rows(step))
    assert t.complete


def test_arithmetic_with_a_case_string_is_not_string_concatenation(tmp_path):
    _, step = one("signed", tmp_path)
    assert step["operation"] == "arithmetic"


def test_inline_tvf_uses_the_declared_default(tmp_path):
    t = trace(ROOT, "demo.dbo.report_tvf.tvf_rate", tmp_path, *DB)
    assert t.complete and "table_valued_function" not in t.gap_kinds
    assert any(r["expression"] == "rate_kind = 'SPOT'" for s in t.steps for r in s["row_selection"])


# Adversarial review cases: each inlined one version of a function that has several.
def test_a_function_with_several_versions_is_not_inlined(tmp_path):
    for column, kind in (("altered", "opaque_function"), ("swapped", "opaque_function"),
                         ("tvf_altered", "table_valued_function")):
        t = trace(ROOT, f"demo.dbo.report_versions.{column}", tmp_path / column, *DB)
        assert kind in t.gap_kinds and not t.complete, column


def more(column, tmp_path):
    return trace(ROOT, f"demo.dbo.report_more.{column}", tmp_path, *DB)


def selected(t):
    return next(s for s in t.steps_for(t.target_id) if s["writer_kind"] == "insert_select")


def value_ends(t):
    return {s["node"] for s in t.data["stops"]}


def test_two_inlined_calls_keep_their_own_derived_nodes(tmp_path):
    t = more("two_tvfs", tmp_path / "a")
    assert {"demo.dbo.lk_a.a_val", "demo.dbo.lk_b.b_val"} <= value_ends(t)
    rows = {r["node"] for s in t.steps for r in s["row_selection"]}
    assert {"demo.dbo.src2.a", "demo.dbo.src2.b"} <= rows
    t = more("applied", tmp_path / "b")                 # two TVFs with the same inner alias d
    assert {"demo.dbo.lk_a.a_val", "demo.dbo.lk_b.b_val"} <= value_ends(t)


def test_inlining_status_and_notes_belong_to_their_own_step(tmp_path):
    t = more("cte_first", tmp_path / "a")
    step = next(s for s in t.steps_for(t.target_id) if "fn_val(c.id)" in s["expression"])
    assert step["status"] == "inferred"                  # not reset by the CTE step built on the way
    t = more("cte_after", tmp_path / "b")
    step = next(s for s in t.steps_for(t.target_id) if "c.fv" in s["expression"])
    assert not any(e["role"] == "function_definition" for e in step["evidence"])   # that is the CTE step's
    assert not any("fn_val" in n for n in step["notes"])


def test_string_concatenation_through_isnull_is_string(tmp_path):
    t = more("names", tmp_path)
    assert selected(t)["operation"] == "string"


def test_via_names_each_function_as_written(tmp_path):
    t = more("labels", tmp_path)
    via = {i["node"]: i.get("via") for i in selected(t)["inputs"]}
    assert via["demo.dbo.src2.d"] == ["TRY_CONVERT"] and via["demo.dbo.src2.e"] == ["CONVERT"]
    assert via["demo.dbo.src2.fname"] == ["LEN > RTRIM"] and via["demo.dbo.src2.lname"] == ["LEN > LTRIM"]


def test_inlining_has_a_budget(tmp_path):
    """Each body calls the next function twice: 2^12 expansions without a budget."""
    sql = tmp_path / "sql"
    sql.mkdir()
    parts = ["CREATE TABLE dbo.lk (k int, v int);\nCREATE TABLE dbo.src (a int);\nCREATE TABLE dbo.out (x int);\nGO\n",
             "CREATE FUNCTION dbo.f0 (@p int) RETURNS int AS BEGIN RETURN (SELECT v FROM dbo.lk WHERE k = @p); END;\nGO\n"]
    for i in range(1, 13):
        parts.append(f"CREATE FUNCTION dbo.f{i} (@p int) RETURNS int AS BEGIN "
                     f"RETURN (SELECT dbo.f{i - 1}(@p) + dbo.f{i - 1}(v) FROM dbo.lk WHERE k = @p); END;\nGO\n")
    parts.append("CREATE PROCEDURE dbo.load AS INSERT INTO dbo.out (x) SELECT dbo.f12(a) FROM dbo.src;\nGO\n")
    (sql / "load.sql").write_text("".join(parts), encoding="utf-8")
    t = trace(sql, "demo.dbo.out.x", tmp_path / "out", *DB)
    assert "opaque_function" in t.gap_kinds and not t.complete
    step = t.only_step_for(t.target_id)
    assert len(step["row_selection"]) == len({(r["node"], r["role"], r["expression"]) for r in step["row_selection"]})
