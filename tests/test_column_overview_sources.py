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
    assert boundary < text.index("`db_a.dbo.loan.customer_id` — 2 routes · last step verified · whole route verified") < unresolved
    assert unresolved < text.index("`db_b.dbo.stage.customer_id` — 1 route · last step verified · whole route verified")
    assert "`db_b.dbo.stage.region` — 1 route · last step verified · whole route verified · decides between values only" in text
    assert text.index("**Source columns**") < text.index("**Source families:**")   # columns before db.schema


def test_repeated_routes_to_one_column_are_one_line_with_every_mapping_status(tmp_path):
    text = answer(page(tmp_path, "db_b.dbo.report.loan_id"))
    assert text.count("`db_b.dbo.stage.loan_id`") == 1
    assert "`db_b.dbo.stage.loan_id` — 2 routes · last step verified, inferred · whole route 1 verified, 1 inferred" in text


def test_sources_table_lists_columns_first_with_route_counts(tmp_path):
    md = page(tmp_path, "db_b.dbo.report.customer_id")
    table = md[md.index("## Sources at a glance"):md.index("## What changes the value")]
    assert '<a id="sources-at-a-glance"></a>' in table
    assert "| DB column | `db_a` | `dbo` | `loan` | `customer_id` | 2 |" in table


def test_shortened_list_keeps_the_unresolved_columns(tmp_path):
    """Ten boundary columns and two unresolved ones: the unresolved ones stay on the first screen."""
    text = answer(page(tmp_path, "db_b.dbo.total.amount"))
    assert "`db_b.dbo.stage.loan_id`" in text and "`db_b.dbo.stage.customer_id`" in text
    assert "**Requested boundary reached** (10 columns, 4 not listed here):" in text
    assert "and 4 more (4 at the boundary or a declared terminal) — [all source columns](#sources-at-a-glance)" in text


def test_whole_route_status_is_shown_apart_from_the_last_step(tmp_path):
    """target.col <- [asserted] staging.col <- source_a.col: the last step is not asserted, the route is."""
    root = FIXTURES / "overview_route_status"
    assert run_cli(cli_args(root / "sql", "demo.dbo.target.col", tmp_path, "--default-database", "demo",
                            "--assertions", str(root / "assertions.json"))) == 0
    md = (tmp_path / "demo.dbo.target.col.overview.md").read_text(encoding="utf-8")
    assert "`demo.dbo.source_a.col` — 1 route · last step inferred · whole route asserted" in answer(md)
    assert "; whole route: asserted |" in md
    detailed = (tmp_path / "demo.dbo.target.col.lineage.md").read_text(encoding="utf-8")
    assert "| `demo.dbo.source_a.col` | **Unresolved origin** — no writer in the scanned sources | 1 | inferred | " \
           "asserted | value |" in detailed
