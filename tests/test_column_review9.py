"""Adversarial review of feature interactions (fixtures/column/review9/, invented). Each case was a false
complete, a lost gap or a wrong statement when two features combined."""
from column_helpers import FIXTURES, trace

ROOT = FIXTURES / "review9"
DB = ("--default-database", "demo")


def run(case, tmp_path, *extra):
    root = ROOT / case
    args = list(extra)
    if (root / "assertions.json").exists():
        args += ["--assertions", str(root / "assertions.json")]
    if (root / "agent").exists():
        args += ["--job-scripts", str(root / "agent")]
    return trace(root / "sql", "demo.dbo.Tgt.v", tmp_path, *DB, *args)


def order_reasons(t):
    return sorted((g.get("detail") or {}).get("reason") for g in t.data["gaps"] if g["kind"] == "ambiguous_writer_order")


def test_assertion_does_not_count_as_a_row_creator(tmp_path):
    """Static UPDATE plus an asserted dynamic UPDATE: nothing adds rows, so the earlier content stays an end."""
    plain = run("p01a", tmp_path / "a", "--stop-at-database", "src_db")
    asserted = run("p01c", tmp_path / "b", "--stop-at-database", "src_db")
    for t in (plain, asserted):
        assert t.data["completeness"]["result"] == "complete_within_selected_scope"
        assert "retained" in [s["writer_kind"] for s in t.steps_for(t.target_id)]
        assert ("prior_state:demo.dbo.tgt.v", "source_without_writer") in t.stop_pairs()


def test_asserted_writer_in_another_procedure_keeps_the_order_question(tmp_path):
    static = run("p19a", tmp_path / "a")
    assert order_reasons(static) == ["no_job_scripts_or_entry_point"]
    asserted = run("p19b", tmp_path / "b")
    assert order_reasons(asserted) == ["asserted_writer_order_unknown"] and not asserted.complete


def test_asserted_writer_outside_every_job_is_not_silently_ordered(tmp_path):
    t = run("p30", tmp_path)
    assert order_reasons(t) == ["asserted_writer_order_unknown"]
    asserted = next(s for s in t.steps_for(t.target_id) if s["writer_kind"] == "asserted")
    assert asserted["run_context"]["status"] == "not_in_job_scripts"


def test_entry_point_and_job_scripts_that_disagree_are_a_gap(tmp_path):
    t = run("p04", tmp_path, "--entry-point", "dbo.run_all")
    assert order_reasons(t) == ["entry_point_and_job_scripts_disagree"]


def test_database_boundary_never_matches_a_linked_server_object(tmp_path):
    t = run("p21", tmp_path, "--stop-at-database", "src_db")
    assert [g["kind"] for g in t.data["gaps"]] == ["external_object"] and not t.complete
    assert not any(s["reason"] == "boundary" for s in t.data["stops"])


def test_boundaries_and_terminals_match_a_synonym_of_the_object(tmp_path):
    t = run("p10", tmp_path / "a", "--stop-at-object", "demo.dbo.srcsyn")
    assert t.data["completeness"]["result"] == "reached_requested_boundary"
    t = run("p10", tmp_path / "b", "--declared-terminals", str(ROOT / "p10" / "terminals.txt"))
    # The table has a writer, so the trace follows it (a terminal labels stops; it does not cut traces),
    # but the stop at the synonym's table carries the declared reason.
    assert any(s.get("declared_terminal") == "accepted synonym terminal" for s in t.data["stops"])


def test_no_missing_source_is_not_presented_as_verified(tmp_path):
    """Every route reaches the boundary, but the page rests on an assertion and a boundary the run chose."""
    t = run("p01c", tmp_path, "--stop-at-database", "src_db")
    page = (tmp_path / "demo.dbo.tgt.v.overview.md").read_text(encoding="utf-8")
    needed = page[page.index("## Main evidence needed"):page.index("## Route families")]
    assert "Nothing" not in needed
    assert "Before treating these routes as verified" in needed or "Also to confirm" in needed
    assert "**Confirm the asserted mapping**" in needed and "not independently reviewed" not in needed   # A1 is reviewed
    assert "**Confirm the production identity of" in needed


def test_local_server_assumption_is_shown_on_the_page(tmp_path):
    main = FIXTURES / "review" / "linked_server" / "main"
    remote = FIXTURES / "review" / "linked_server" / "remote"
    t = trace(main, "demo.output.result_value", tmp_path, *DB, "--source-root", str(remote),
              "--database-map", f"{remote}=remote_db", "--local-server", "LNK")
    page = (tmp_path / "demo.output.result_value.overview.md").read_text(encoding="utf-8")
    assert "**Confirm that linked server `lnk` is this server**" in page
