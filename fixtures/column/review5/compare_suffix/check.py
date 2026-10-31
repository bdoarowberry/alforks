"""compare._matches: a two-part expected source `<connection>.<column>` matches any node ending in it.
Run from the repository root: PYTHONPATH=src python review-fixtures/compare_suffix/check.py"""
from column_lineage.compare import compare_one


def trace(leaf: str) -> dict:
    return {"target": {"id": "demo.dbo.out.code"}, "completeness": {"status": "complete"},
            "steps": [{"target": "demo.dbo.out.code", "row_selection": [],
                       "inputs": [{"node": leaf, "role": "value", "status": "verified"}]}],
            "stops": [{"node": leaf, "reason": "source_without_writer"}]}


expected = {"target": "demo.dbo.out.code", "sources": ["feed.code"]}   # meant: file connection "feed", package A
for leaf in ("demo.archive.feed.code",                    # a database table that happens to be called feed
             "flat_file:pkg_other.dtsx::feed.code",       # another package's connection with the same name
             "flat_file:pkg_a.dtsx::[old.feed].code"):    # a dotted connection name
    print(leaf, "->", compare_one(expected, trace(leaf))[1]["sources"]["feed.code"]["result"])
