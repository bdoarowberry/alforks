"""The overview names every source column in full near the top (fixtures/column/overview_sources/, invented):
grouped by how its routes end there, with the route count and the status of the mapping."""
from column_helpers import FIXTURES, cli_args, run_cli

ROOT = FIXTURES / "overview_sources"


def page(tmp_path, column):
    assert run_cli(cli_args(ROOT, column, tmp_path, "--stop-at-database", "db_a")) == 0
    return (tmp_path / f"{column.lower()}.overview.md").read_text(encoding="utf-8")


def answer(md):
    return md[md.index("## Plain answer"):md.index("## Coverage")]


def test_source_columns_are_named_in_full_and_grouped_by_how_routes_end(tmp_path):
    text = answer(page(tmp_path, "db_b.dbo.report.customer_id"))
    assert "- **Source columns** (3 columns where value routes end; 1 at the requested boundary or a declared " \
           "terminal; 2 unresolved):" in text
    boundary = text.index("**Requested boundary reached** (1 column):")
    unresolved = text.index("**Unresolved origin** — no writer in the scanned sources (2 columns):")
    assert boundary < text.index("`db_a.dbo.loan.customer_id` — 2 routes · mapping verified") < unresolved
    assert unresolved < text.index("`db_b.dbo.stage.customer_id` — 1 route · mapping verified")
    assert "`db_b.dbo.stage.region` — 1 route · mapping verified · decides between values only" in text
    assert text.index("**Source columns**") < text.index("**Source families:**")   # columns before db.schema


def test_repeated_routes_to_one_column_are_one_line_with_every_mapping_status(tmp_path):
    text = answer(page(tmp_path, "db_b.dbo.report.loan_id"))
    assert text.count("`db_b.dbo.stage.loan_id`") == 1
    assert "`db_b.dbo.stage.loan_id` — 2 routes · mapping verified, inferred" in text


def test_sources_table_lists_columns_first_with_route_counts(tmp_path):
    md = page(tmp_path, "db_b.dbo.report.customer_id")
    table = md[md.index("## Sources at a glance"):md.index("## What changes the value")]
    assert '<a id="sources-at-a-glance"></a>' in table
    assert "| DB column | `db_a` | `dbo` | `loan` | `customer_id` | 2 |" in table
