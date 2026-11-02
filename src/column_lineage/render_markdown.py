"""Render a column lineage trace as a Markdown review page.

The renderer reads only the JSON form of a trace (``TraceResult.to_dict()``), so a
page can always be regenerated from the companion ``.lineage.json`` file::

    python -m column_lineage.render_markdown trace.lineage.json [-o trace.lineage.md]

Page structure (see BUILD_BRIEF.md, "Outputs"):

1. Identity and result
2. Short answer
3. Value lineage overview
4. Row-selection context
5. Detailed steps
6. Gaps and next evidence

Output is deterministic: everything is sorted or kept in the trace's own
(deterministic) order, and nothing time-dependent is added.
"""
from __future__ import annotations

import argparse
import json
import os
import re
import sys
from pathlib import Path

EXCERPT_LIMIT = 160          # characters of SQL excerpt shown per evidence item
EXPRESSION_LIMIT = 160       # characters of a step expression shown inline
LABEL_LIMIT = 60             # characters of a branch label inside the tree
TREE_MAX_DEPTH = 12          # step levels expanded in the overview tree
TREE_MAX_INPUTS = 12         # inputs shown per step in the overview tree
TREE_MAX_LINES = 400         # hard cap on overview tree lines
MERMAID_MAX_STEPS = 40       # the Mermaid diagram is only drawn up to this many steps
SUMMARY_MAX_ITEMS = 8        # items listed per Short-answer bullet before "and N more"
LINK_SAMPLE = 5              # step links listed per cross-reference (other writers, upstream steps) before "N more"
AFFECTS_MAX = 50             # steps listed under one gap's "Affects"

STATUS_ORDER = ("verified", "inferred", "asserted", "partial", "ambiguous", "unresolved")
ROW_ROLE_ORDER = ("filter", "join_key", "join_predicate", "group_by", "having", "qualify", "merge_condition")
ROW_ROLE_LABEL = {
    "filter": "filter (WHERE)",
    "join_key": "join key",
    "join_predicate": "join predicate",
    "group_by": "group by",
    "having": "having",
    "qualify": "qualify",
    "merge_condition": "merge condition",
}
STOP_LABEL = {
    "boundary": "requested boundary",
    "source_without_writer": "source with no writer in the selected scanned sources",
    "constant": "constant value",
    "parameter_without_caller": "parameter with no caller in the scanned files",
    "max_depth": "maximum depth reached",
    "external_file": "external file read by an SSIS package",
    "prior_state": "earlier content before this write (candidate earlier writers are shown on this page)",
}

SECTION_TITLES = (
    "Identity and result",
    "Short answer",
    "Value lineage overview",
    "Row-selection context",
    "Detailed steps",
    "Gaps and next evidence",
)
SECTION_ANCHORS = ("identity", "short-answer", "value-lineage", "row-selection", "detailed-steps", "gaps")


# --------------------------------------------------------------------------- text helpers

_WS = re.compile(r"\s+")


def _one_line(value) -> str:
    return _WS.sub(" ", str(value if value is not None else "")).strip()


def _truncate(value, limit: int) -> str:
    text = _one_line(value)
    return text if len(text) <= limit else text[: max(0, limit - 1)].rstrip() + "…"


def _code(value, limit: int | None = None) -> str:
    """An inline code span that survives backticks in the content."""
    text = _truncate(value, limit) if limit else _one_line(value)
    if not text:
        return "—"
    longest = max((len(run) for run in re.findall(r"`+", text)), default=0)
    if longest == 0:
        return f"`{text}`"
    fence = "`" * (longest + 1)
    return f"{fence} {text} {fence}"


_MD_SPECIAL = re.compile(r"([\\`*\[\]<>|]|(?<![A-Za-z0-9])_|_(?![A-Za-z0-9]))")
_MERMAID_ESCAPES = {"&": "#38;", "#": "#35;", '"': "#34;", "<": "#60;", ">": "#62;", "|": "#124;", "`": "#96;"}


def _text(value) -> str:
    """Plain prose with Markdown-active characters escaped (intraword underscores are left alone)."""
    return _MD_SPECIAL.sub(r"\\\1", _one_line(value))


def _cell(markdown: str) -> str:
    """Make already-formatted Markdown safe inside a table cell (pipes and newlines)."""
    out = _one_line(markdown)
    out = re.sub(r"(?<!\\)\|", r"\\|", out)
    return out or "—"


def _plural(n: int, word: str, plural: str | None = None) -> str:
    return f"{n} {word if n == 1 else (plural or word + 's')}"


def _status_rank(status: str | None) -> int:
    return STATUS_ORDER.index(status) if status in STATUS_ORDER else len(STATUS_ORDER)


def _anchor(raw: str) -> str:
    return re.sub(r"[^a-z0-9_-]+", "-", str(raw).lower()).strip("-") or "x"


def _join_list(items: list[str], limit: int = SUMMARY_MAX_ITEMS) -> str:
    if len(items) > limit:
        return ", ".join(items[:limit]) + f", and {len(items) - limit} more"
    if len(items) <= 1:
        return "".join(items)
    if len(items) == 2:
        return f"{items[0]} and {items[1]}"
    return ", ".join(items[:-1]) + f", and {items[-1]}"


# --------------------------------------------------------------------------- trace index

class _Trace:
    """Lookups over the JSON trace, built once per render."""

    def __init__(self, data: dict):
        self.data = data
        self.target = data.get("target") or {}
        self.target_id = self.target.get("id") or data.get("requested") or "?"
        self.nodes: dict[str, dict] = {}
        for node in data.get("nodes") or []:
            if node.get("id"):
                self.nodes.setdefault(node["id"], node)
        if self.target:
            self.nodes.setdefault(self.target_id, self.target)

        steps = []
        for index, step in enumerate(data.get("steps") or []):
            step = dict(step)
            step.setdefault("id", f"step-{index}")
            steps.append(step)
        steps.sort(key=lambda s: (str(s.get("target") or ""), s.get("order") or 0, str(s["id"])))
        self.steps = steps
        self.steps_by_target: dict[str, list[dict]] = {}
        for step in steps:
            self.steps_by_target.setdefault(step.get("target") or "?", []).append(step)

        self.gaps: dict[str, dict] = {}
        for index, gap in enumerate(data.get("gaps") or []):
            self.gaps.setdefault(gap.get("id") or f"gap-{index}", dict(gap, id=gap.get("id") or f"gap-{index}"))
        self.gaps_by_node: dict[str, list[dict]] = {}
        for gap in self.sorted_gaps():
            if gap.get("node"):
                self.gaps_by_node.setdefault(gap["node"], []).append(gap)

        self.stops = sorted(data.get("stops") or [], key=lambda s: (str(s.get("node")), str(s.get("reason")),
                                                                    str(s.get("detail") or "")))
        self.stops_by_node: dict[str, list[dict]] = {}
        for stop in self.stops:
            self.stops_by_node.setdefault(stop.get("node") or "?", []).append(stop)
        self.cycles = sorted([list(c) for c in data.get("cycles") or []])
        self.cycle_nodes = {n for c in self.cycles for n in c}

        self.ordered, self.depth = self._order()
        self.label = {s["id"]: f"S{i}" for i, s in enumerate(self.ordered, 1)}
        self.reachable = {s["id"] for s in self.ordered if s["id"] in self.depth}
        roots = {ev.get("root") for ev in self._all_evidence() if ev.get("root")}
        self.show_root = len(roots) > 1

    # ordering ---------------------------------------------------------------
    def _order(self) -> tuple[list[dict], dict[str, int]]:
        """Target-first, then upstream breadth-first by depth; unreachable steps last."""
        ordered: list[dict] = []
        depth: dict[str, int] = {}
        seen_nodes = {self.target_id}
        frontier = [self.target_id]
        level = 0
        while frontier:
            upcoming: list[str] = []
            for node_id in frontier:
                for step in self.steps_by_target.get(node_id, []):
                    if step["id"] in depth:
                        continue
                    depth[step["id"]] = level
                    ordered.append(step)
                    refs = [i.get("node") for i in step.get("inputs") or []]
                    refs += [r.get("node") for r in step.get("row_selection") or []]
                    for ref in refs:
                        if ref and ref not in seen_nodes:
                            seen_nodes.add(ref)
                            upcoming.append(ref)
            frontier = upcoming
            level += 1
        ordered += [s for s in self.steps if s["id"] not in depth]
        return ordered, depth

    def _all_evidence(self):
        for step in self.steps:
            yield from step.get("evidence") or []
            for rs in step.get("row_selection") or []:
                if rs.get("evidence"):
                    yield rs["evidence"]
        for gap in self.gaps.values():
            yield from gap.get("evidence") or []

    # lookups ----------------------------------------------------------------
    def sorted_gaps(self) -> list[dict]:
        return sorted(self.gaps.values(), key=lambda g: (str(g.get("kind")), str(g.get("id"))))

    def display(self, node_id: str | None) -> str:
        if not node_id:
            return "?"
        node = self.nodes.get(node_id)
        return (node or {}).get("display") or node_id

    def kind(self, node_id: str | None) -> str:
        return (self.nodes.get(node_id or "") or {}).get("kind") or ""

    def gap_kind(self, gap_id: str | None) -> str:
        gap = self.gaps.get(gap_id or "")
        return gap.get("kind") or "gap" if gap else "gap"

    def step_link(self, step: dict) -> str:
        return f"[{self.label[step['id']]}](#{_anchor(step['id'])})"

    def step_links(self, steps: list[dict], limit: int = LINK_SAMPLE) -> str:
        """Links to at most ``limit`` steps, then a count, so cross-references stay linear in size."""
        shown = ", ".join(self.step_link(s) for s in steps[:limit])
        return shown + (f" and {len(steps) - limit} more" if len(steps) > limit else "")

    def gap_link(self, gap_id: str) -> str:
        return f"[{self.gap_kind(gap_id)}](#{_anchor(gap_id)})"

    def location(self, ev: dict | None) -> str:
        if not ev:
            return ""
        start, end = ev.get("start_line"), ev.get("end_line")
        lines = f"{start}" if end in (None, start) else f"{start}-{end}"
        where = f"{ev.get('path') or '?'}:{lines}" if start is not None else f"{ev.get('path') or '?'}"
        if self.show_root and ev.get("root"):
            where = f"{ev['root']}/{where}"
        return where

    def evidence_md(self, ev: dict | None, excerpt: bool = True) -> str:
        if not ev:
            return "—"
        if ev.get("role") == "assertion" and ev.get("path"):
            # Assertion files are cited relative to the page, so make them a working link.
            out = f"[{_text(self.location(ev))}]({_link_target(ev['path'])})"
        else:
            out = _code(self.location(ev))
        if excerpt and ev.get("excerpt"):
            out += " " + _code(ev["excerpt"], EXCERPT_LIMIT)
        return out

    def reach(self, node_id: str, roles: tuple[str, ...]) -> tuple[set[str], set[str]]:
        """Nodes and step ids reachable from ``node_id`` following inputs with these roles."""
        nodes, steps = {node_id}, set()
        stack = [node_id]
        while stack:
            current = stack.pop()
            for step in self.steps_by_target.get(current, []):
                if step["id"] in steps:
                    continue
                steps.add(step["id"])
                for inp in step.get("inputs") or []:
                    ref = inp.get("node")
                    if ref and inp.get("role") in roles and ref not in nodes:
                        nodes.add(ref)
                        stack.append(ref)
        return nodes, steps


# --------------------------------------------------------------------------- sections

def _result_text(t: _Trace) -> tuple[str, int]:
    comp = t.data.get("completeness") or {}
    n_gaps = len(t.gaps) if t.gaps else sum((comp.get("gaps_by_kind") or {}).values())
    n_rows = sum((comp.get("row_selection_gaps_by_kind") or {}).values())
    row_note = f"; {_plural(n_rows, 'row-selection gap')} reported separately" if n_rows else ""
    if comp.get("status") == "complete":
        qualifiers = []
        if comp.get("asserted_steps"):
            qualifiers.append(f"relies on {_plural(comp['asserted_steps'], 'asserted step')}")
        if n_rows:
            qualifiers.append(f"value lineage{row_note}")
        label = {"reached_requested_boundary": "Complete — reached the requested boundary",
                 "complete_to_declared_boundary": "Complete to the declared boundary — every other path ends at a "
                                                  "terminal declared for this run (upstream population not proved)",
                 "complete_to_requested_depth": "Complete to the requested depth — some paths were cut by "
                                                "--max-depth and are not traced further",
                 "complete_within_selected_scope": "Complete within the selected sources — paths end at objects with "
                                                   "no writer in them, files, constants or parameters, not at a "
                                                   "proven origin"}.get(comp.get("result"), "Complete")
        return label + (f" ({'; '.join(qualifiers)})" if qualifiers else ""), n_gaps
    if n_gaps:
        n_context = sum((comp.get("context_gaps_by_kind") or {}).values())
        n_value = n_gaps - n_rows - n_context
        parts = [_plural(n_value, "value-path gap")] if n_value or not n_context else []
        if n_context:
            parts.append(_plural(n_context, "context gap"))
        return f"Incomplete — {', '.join(parts)}{row_note}", n_gaps
    overall = comp.get("overall_step_status") or "unknown"
    return f"Incomplete — no gaps recorded, overall step status {overall}", n_gaps


def assertion_provenance_text(data: dict) -> str:
    """Who made the assertions a trace relies on and whether they were reviewed, as the assertions
    file records it; "not recorded" when it does not."""
    rows = data.get("assertion_provenance") or []
    if not rows:
        return "who made them and whether they were reviewed is not recorded"
    origin = {"ai_assisted": "AI-assisted", "person": "made by a person"}
    made: dict[str, int] = {}
    review: dict[str, int] = {}
    for row in rows:
        key = origin.get(row.get("origin")) or (f"recorded as made by {row['asserted_by']} (person or AI-assisted "
                                                "not recorded)" if row.get("asserted_by") else "maker not recorded")
        made[key] = made.get(key, 0) + 1
        rkey = {"reviewed": "independently reviewed", "unreviewed": "not independently reviewed"}.get(
            row.get("review"), "review not recorded")
        review[rkey] = review.get(rkey, 0) + 1
    fmt = lambda counts: ", ".join(f"{n} {k}" for k, n in sorted(counts.items(), key=lambda kv: -kv[1]))
    records = len(rows)
    return f"{records} assertion record{'' if records == 1 else 's'}: {fmt(made)}; {fmt(review)}"


def _section_identity(t: _Trace, json_name: str | None) -> list[str]:
    data = t.data
    comp = data.get("completeness") or {}
    scan = data.get("scan") or {}
    boundary = data.get("boundary") or {}
    result, _ = _result_text(t)
    out = [f"> **Result: {result}**" + (f" (overall step status: {_text(comp['overall_step_status'])})"
                                         if comp.get("overall_step_status") else "")]
    if comp.get("asserted_steps"):
        ids = ", ".join(_code(i) for i in sorted(data.get("assertions_used") or [])) or "see Detailed steps"
        out.append(">")
        out.append(f"> **This trace relies on {_plural(comp['asserted_steps'], 'asserted step')}** (assertions {ids}). "
                   "Asserted steps are mappings recorded in an assertions file, not proved from SQL ("
                   + assertion_provenance_text(data) + ")"
                   + (f"; {_plural(comp['gaps_resolved_by_assertions'], 'gap')} "
                      f"{'is' if comp['gaps_resolved_by_assertions'] == 1 else 'are'} marked resolved by them."
                      if comp.get("gaps_resolved_by_assertions") else "."))
    out.append(">")
    out.append("> A *stop* at the requested boundary, or at a source with no writer in the scanned files, "
               "is where this trace ended. It is not a claim that those columns are ultimate "
               "source-system fields.")
    out.append("")

    target_kind = t.target.get("kind")
    rows = [
        ("Target", f"{_code(t.display(t.target_id))} (id {_code(t.target_id)}"
                   + (f", {_text(target_kind)}" if target_kind else "") + ")"),
        ("Requested", _code(data.get("requested"))),
        ("Target resolution", _text(data.get("target_status") or "—")
         + (" — gaps: " + ", ".join(t.gap_link(g) for g in sorted(data.get("target_gaps") or []))
            if data.get("target_gaps") else "")),
    ]
    roots = scan.get("roots") or []
    scope = (", ".join(_code(r) for r in sorted(map(str, roots))) or "—")
    counts = []
    for key, label in (("files", "files"), ("statements", "statements"), ("parsed", "parsed"),
                       ("fallback", "parse fallbacks")):
        if scan.get(key) is not None:
            counts.append(f"{scan[key]} {label}")
    if counts:
        scope += "; " + ", ".join(counts)
    rows.append(("Scan scope", scope))
    packages = scan.get("packages")
    if isinstance(packages, dict):
        rows.append(("SSIS packages", (", ".join(_code(r) for r in sorted(map(str, packages.get("roots") or [])))
                                       or "—") + f"; {packages.get('in_scope', 0)} in scope, "
                                                 f"{packages.get('excluded', 0)} excluded, "
                                                 f"{packages.get('scanned', 0)} found"))

    parts = []
    if boundary.get("databases"):
        parts.append("databases " + ", ".join(_code(d) for d in sorted(boundary["databases"])))
    if boundary.get("objects"):
        parts.append("objects " + ", ".join(_code(o) for o in sorted(boundary["objects"])))
    if boundary.get("max_depth") is not None:
        parts.append(f"max depth {boundary['max_depth']}")
    rows.append(("Requested boundary", "; ".join(parts) if parts else "none"))
    if scan.get("source_revision"):
        rows.append(("Source revision", _code(scan["source_revision"])))
    rows.append(("Trace schema", _code(data.get("schema_version") or "unknown")))

    steps_by_status = comp.get("steps_by_status") or {}
    if steps_by_status:
        status_text = ", ".join(f"{_text(k)}: {v}" for k, v in sorted(steps_by_status.items(),
                                                                        key=lambda kv: (_status_rank(kv[0]), kv[0])))
    else:
        status_text = "no steps"
    rows.append(("Steps", f"{len(t.steps)} ({status_text})"))
    stops_by_reason = comp.get("stops_by_reason") or {}
    if stops_by_reason:
        rows.append(("Stops", ", ".join(f"{_text(k)}: {v}" for k, v in sorted(stops_by_reason.items()))))
    gaps_by_kind = comp.get("gaps_by_kind") or {}
    rows.append(("Gaps", ", ".join(f"{_text(k)}: {v}" for k, v in sorted(gaps_by_kind.items())) or "none"))
    if json_name:
        rows.append(("Companion JSON", f"[{_text(json_name)}]({_link_target(json_name)})"))

    out += ["| Field | Value |", "|---|---|"]
    out += [f"| {k} | {_cell(v)} |" for k, v in rows]
    if comp.get("note"):
        out += ["", f"*{_text(comp['note'])}*"]
    return out


def _link_target(path: str) -> str:
    return str(path).replace("\\", "/").replace(" ", "%20").replace("(", "%28").replace(")", "%29")


def _section_short_answer(t: _Trace) -> list[str]:
    target = _code(t.display(t.target_id))
    out: list[str] = []
    gaps = t.sorted_gaps()
    target_steps = t.steps_by_target.get(t.target_id, [])

    if not t.steps:
        stops = t.stops_by_node.get(t.target_id, [])
        if stops:
            reasons = ", ".join(STOP_LABEL.get(s.get("reason"), _text(s.get("reason"))) for s in stops)
            out.append(f"No mapping step leads into {target}: the trace stops at the target itself "
                       f"({reasons}). It has no writer to follow in the scanned files, or it is inside "
                       "the requested boundary.")
        else:
            out.append(f"No mapping step could be established for {target}, so its value sources are "
                       "**unresolved**.")
        out.append("")
        out.append(_uncertainty_bullet(t, gaps))
        return out

    value_nodes, value_steps = t.reach(t.target_id, ("value",))
    all_nodes, all_steps = t.reach(t.target_id, ("value", "condition", "window"))
    leaves = sorted(n for n in value_nodes if n != t.target_id and not t.steps_by_target.get(n))

    # Value sources.
    by_reason: dict[str, list[str]] = {}
    open_leaves: list[str] = []
    for node_id in leaves:
        stops = t.stops_by_node.get(node_id, [])
        if stops:
            for stop in stops:
                by_reason.setdefault(stop.get("reason") or "stop", []).append(_code(t.display(node_id)))
        else:
            gap_kinds = sorted({g.get("kind") for g in t.gaps_by_node.get(node_id, [])})
            note = f"⚠ {', '.join(gap_kinds)}" if gap_kinds else "not traced further"
            open_leaves.append(f"{_code(t.display(node_id))} ({_text(note)})")
    source_parts = []
    for reason in sorted(by_reason):
        names = sorted(set(by_reason[reason]))
        source_parts.append(f"{_join_list(names)} ({STOP_LABEL.get(reason, _text(reason))})")
    if source_parts:
        out.append(f"- **Value comes from:** {_join_list(source_parts, 99)}.")
    else:
        out.append("- **Value comes from:** no value path reaches a stop.")
    if open_leaves:
        out.append(f"- **Value paths without proof:** {_join_list(open_leaves)}.")

    intermediates = sorted(n for n in value_nodes if n != t.target_id and t.steps_by_target.get(n))
    route = f"- **Route:** {_plural(len(value_steps), 'value step')}"
    if intermediates:
        route += " through " + _join_list([_code(t.display(n)) for n in intermediates])
    renames = [s for s in t.ordered if s["id"] in value_steps and s.get("operation") == "rename"]
    if renames:
        route += f"; {_plural(len(renames), 'rename')} ({t.step_links(renames, SUMMARY_MAX_ITEMS)})"
    out.append(route + ".")

    # Conditions and alternatives.
    conditions = []
    for step in t.ordered:
        if step["id"] not in all_steps:
            continue
        inputs = step.get("inputs") or []
        values = [i for i in inputs if i.get("role") == "value"]
        conds = [i for i in inputs if i.get("role") == "condition"]
        branched = [i for i in values if i.get("branch")]
        if not conds and len(branched) < 2:
            continue
        alts = "; ".join(f"{_code(i.get('branch') or 'value', LABEL_LIMIT)} → {_code(t.display(i.get('node')))}"
                         for i in values)
        text = (f"{t.step_link(step)} ({_code(t.display(step.get('target')))}, "
                f"{_text(step.get('operation') or 'expression')}) has {_plural(len(values), 'value alternative')}: "
                f"{alts or 'none'}")
        if conds:
            by_branch: dict[str, set[str]] = {}
            for i in conds:
                by_branch.setdefault(i.get("branch") or "condition", set()).add(_code(t.display(i.get("node"))))
            chosen = [f"{_code(branch, LABEL_LIMIT)} (reads {_join_list(sorted(names))})"
                      for branch, names in sorted(by_branch.items())]
            text += f"; chosen by {_join_list(chosen)}"
        conditions.append(text)
    if conditions:
        shown = conditions[:SUMMARY_MAX_ITEMS]
        if len(conditions) > len(shown):
            shown.append(f"… and {len(conditions) - len(shown)} more (see Detailed steps)")
        out.append("- **Conditions:** " + ("" if len(shown) == 1 else "\n  - ")
                   + "\n  - ".join(item + "." for item in shown))

    # Multiple writers of one node.
    multi = []
    for node_id in sorted(all_nodes):
        writers = t.steps_by_target.get(node_id, [])
        if len(writers) > 1:
            labels = [f"{t.step_link(s)} {_text(_writer_text(s))}" for s in writers]
            multi.append(f"{_code(t.display(node_id))} has {_plural(len(writers), 'alternative writer')}: "
                         f"{_join_list(labels)}")
    if multi:
        out.append("- **Alternative writers:** " + "; ".join(multi[:SUMMARY_MAX_ITEMS])
                   + ("; …" if len(multi) > SUMMARY_MAX_ITEMS else "") + ". All are kept as alternatives.")

    # Row selection.
    rs_counts: dict[str, int] = {}
    for step in t.ordered:
        if step["id"] in all_steps:
            for rs in step.get("row_selection") or []:
                rs_counts[rs.get("role") or "?"] = rs_counts.get(rs.get("role") or "?", 0) + 1
    if rs_counts:
        detail = ", ".join(f"{_text(ROW_ROLE_LABEL.get(k, k))}: {v}"
                           for k, v in sorted(rs_counts.items(), key=lambda kv: _row_role_rank(kv[0])))
        out.append(f"- **Row selection:** {_plural(sum(rs_counts.values()), 'row-selection column')} ({detail}) "
                   "decide which rows contribute; they are not value sources "
                   "(see [Row-selection context](#row-selection)).")

    # Where it stops.
    reasons = sorted({s.get("reason") for n in all_nodes for s in t.stops_by_node.get(n, [])} - {None})
    if reasons:
        out.append("- **Where the trace stops:** " + ", ".join(STOP_LABEL.get(r, _text(r)) for r in reasons)
                   + ". This is where the trace ended, not a claim about ultimate source-system fields.")
    out.append(_uncertainty_bullet(t, gaps))
    return out


def _uncertainty_bullet(t: _Trace, gaps: list[dict]) -> str:
    parts = []
    for label, chosen in (("value-path gap", [g for g in gaps if g.get("affects", "value") != "row_selection"]),
                          ("row-selection gap", [g for g in gaps if g.get("affects") == "row_selection"])):
        if not chosen:
            continue
        kinds: dict[str, int] = {}
        for gap in chosen:
            kinds[gap.get("kind") or "gap"] = kinds.get(gap.get("kind") or "gap", 0) + 1
        parts.append(f"{_plural(len(chosen), label)} ("
                     + ", ".join(f"{_text(k)}: {v}" for k, v in sorted(kinds.items()))
                     + ") — see [Gaps and next evidence](#gaps)")
    weak: dict[str, list[str]] = {}
    for step in t.ordered:
        status = step.get("status") or "verified"
        if status != "verified":
            weak.setdefault(status, []).append(t.step_link(step))
    for status in sorted(weak, key=_status_rank):
        parts.append(f"{_plural(len(weak[status]), 'step')} {_text(status)} ({_join_list(weak[status])})")
    if t.cycles:
        parts.append(f"{_plural(len(t.cycles), 'cycle')} detected")
    if (t.data.get("target_status") or "verified") != "verified":
        parts.append(f"target resolution is {_text(t.data['target_status'])}")
    if not parts:
        return f"- **Uncertainty:** none recorded — {_plural(len(t.steps), 'step')} verified and no gaps."
    return "- **Uncertainty:** " + "; ".join(parts) + "."


def _row_role_rank(role: str | None) -> tuple[int, str]:
    return (ROW_ROLE_ORDER.index(role) if role in ROW_ROLE_ORDER else len(ROW_ROLE_ORDER), str(role))


def _writer_text(step: dict) -> str:
    text = step.get("writer_kind") or "writer"
    if step.get("procedure"):
        text += f" in {step['procedure']}"
    if step.get("branch_context"):
        text += " under " + " / ".join(_one_line(b) for b in step["branch_context"])
    return text


# --------------------------------------------------------------------------- overview tree

class _TreeBuilder:
    def __init__(self, t: _Trace):
        self.t = t
        self.lines: list[str] = []
        self.expanded: dict[str, list[str]] = {}
        self.shown_steps: set[str] = set()
        self.truncated = False

    def emit(self, line: str) -> bool:
        if len(self.lines) >= TREE_MAX_LINES:
            self.truncated = True
            return False
        self.lines.append(line.rstrip())
        return True

    def node_marks(self, node_id: str, extra_gaps: list[str] = ()) -> str:
        t = self.t
        marks = []
        for stop in t.stops_by_node.get(node_id, []):
            marks.append(f"■ stop: {stop.get('reason')}")
        kinds = sorted({g.get("kind") for g in t.gaps_by_node.get(node_id, [])}
                       | {t.gap_kind(g) for g in extra_gaps if g})
        marks += [f"⚠ gap: {k}" for k in kinds]
        if not marks and not t.steps_by_target.get(node_id):
            marks.append("■ constant" if t.kind(node_id) == "constant" else "(not traced further)")
        return ("  " + "  ".join(marks)) if marks else ""

    def build(self) -> list[str]:
        t = self.t
        root = f"{t.display(t.target_id)}  ◀ target"
        target_gap_marks = [f"⚠ gap: {t.gap_kind(g)}" for g in sorted(t.data.get("target_gaps") or [])]
        self.node(t.target_id, root, "", "", [], 0, prefix_marks=target_gap_marks, is_root=True)
        hidden = [s for s in t.ordered if s["id"] not in self.shown_steps and s["id"] in t.depth]
        if self.truncated and hidden:
            self.lines.append(f"… {_plural(len(hidden), 'more step')} not shown (see Detailed steps)")
        return self.lines

    def node(self, node_id, head, connector, child_prefix, stack, depth, prefix_marks=(), input_gaps=(),
             is_root=False):
        t = self.t
        steps = t.steps_by_target.get(node_id, [])
        text = head if is_root else head + t.display(node_id)
        own = ("  " + "  ".join(prefix_marks)) if prefix_marks else ""
        if steps and node_id in stack:
            self.emit(f"{connector}{text}  ↻ (cycle){own}")
            return
        if steps and node_id in self.expanded:
            self.emit(f"{connector}{text}  (see above: {', '.join(self.expanded[node_id])}){own}")
            return
        marks = self.node_marks(node_id, list(input_gaps)) + own
        if not self.emit(f"{connector}{text}{marks}"):
            return
        if not steps:
            return
        if depth >= TREE_MAX_DEPTH:
            _, upstream = t.reach(node_id, ("value", "condition", "window"))
            self.emit(f"{child_prefix}└─ … {_plural(len(upstream), 'more step')} upstream (see Detailed steps)")
            return
        labels = [t.label[s["id"]] for s in steps[:LINK_SAMPLE]]
        if len(steps) > LINK_SAMPLE:
            labels.append(f"and {len(steps) - LINK_SAMPLE} more")
        self.expanded[node_id] = labels
        stack.append(node_id)
        for index, step in enumerate(steps):
            last = index == len(steps) - 1
            alt = (index + 1, len(steps)) if len(steps) > 1 else None
            self.step(step, child_prefix + ("└─ " if last else "├─ "), child_prefix + ("   " if last else "│  "),
                      alt, stack, depth)
        stack.pop()

    def step(self, step, connector, child_prefix, alt, stack, depth):
        t = self.t
        self.shown_steps.add(step["id"])
        parts = [f"{t.label[step['id']]} {step.get('writer_kind') or 'writer'}"
                 + (f" in {step['procedure']}" if step.get("procedure") else ""),
                 step.get("operation") or "expression",
                 step.get("status") or "verified"]
        if step.get("branch_context"):
            parts.append("branch: " + _truncate(" / ".join(step["branch_context"]), LABEL_LIMIT))
        line = ("writer %d of %d: " % alt if alt else "") + " · ".join(_one_line(p) for p in parts)
        gap_kinds = sorted({t.gap_kind(g) for g in step.get("gaps") or []})
        if gap_kinds:
            line += "  " + "  ".join(f"⚠ gap: {k}" for k in gap_kinds)
        if not self.emit(connector + line):
            return
        inputs = step.get("inputs") or []
        if not inputs:
            self.emit(f"{child_prefix}└─ (no inputs recorded)")
            return
        shown = inputs[:TREE_MAX_INPUTS]
        extra = len(inputs) - len(shown)
        for index, inp in enumerate(shown):
            last = index == len(shown) - 1 and not extra
            role = inp.get("role") or "value"
            tag = role if not inp.get("branch") else f"{role} · {_truncate(inp['branch'], LABEL_LIMIT)}"
            head = f"[{tag}] "
            status = inp.get("status") or "verified"
            marks = [f"({status})"] if status != "verified" else []
            self.node(inp.get("node") or "?", head, child_prefix + ("└─ " if last else "├─ "),
                      child_prefix + ("   " if last else "│  "), stack, depth + 1,
                      prefix_marks=marks, input_gaps=[inp.get("gap")] if inp.get("gap") else [])
            if self.truncated:
                return
        if extra:
            self.emit(f"{child_prefix}└─ … {_plural(extra, 'more input')} (see {t.label[step['id']]} "
                      "in Detailed steps)")


def _mermaid(t: _Trace) -> list[str]:
    _, value_steps = t.reach(t.target_id, ("value", "condition", "window"))
    steps = [s for s in t.ordered if s["id"] in value_steps]
    node_ids = set()
    for step in steps:
        node_ids.add(step.get("target") or "?")
        node_ids.update(i.get("node") or "?" for i in step.get("inputs") or [])
    ids = {n: f"n{i}" for i, n in enumerate(sorted(node_ids))}

    def esc(value: str) -> str:
        return "".join(_MERMAID_ESCAPES.get(ch, ch) for ch in _one_line(value))

    lines = ["```mermaid", "flowchart RL"]
    for node_id in sorted(node_ids):
        label = esc(_truncate(t.display(node_id), 80))
        lines.append(f'  {ids[node_id]}["{label}"]')
    for step in steps:
        for inp in step.get("inputs") or []:
            role = inp.get("role") or "value"
            label = t.label[step["id"]] + (f" {role}" if role != "value" else "")
            if inp.get("branch"):
                label += f": {_truncate(inp['branch'], 40)}"
            arrow = "-.->" if role != "value" else "-->"
            lines.append(f'  {ids[inp.get("node") or "?"]} {arrow}|"{esc(label)}"| {ids[step.get("target") or "?"]}')
    stops = sorted(n for n in node_ids if t.stops_by_node.get(n))
    gapped = sorted(n for n in node_ids if t.gaps_by_node.get(n))
    lines.append(f"  classDef target stroke-width:3px")
    lines.append(f"  class {ids[t.target_id]} target" if t.target_id in ids else "")
    if stops:
        lines.append("  classDef stop stroke-dasharray:4 2")
        lines.append(f"  class {','.join(ids[n] for n in stops)} stop")
    if gapped:
        lines.append("  classDef gap stroke:#c00,stroke-width:2px")
        lines.append(f"  class {','.join(ids[n] for n in gapped)} gap")
    lines.append("```")
    return [line for line in lines if line]


def _fence(lines: list[str], info: str = "text") -> list[str]:
    longest = max((len(run) for line in lines for run in re.findall(r"`+", line)), default=0)
    fence = "`" * max(3, longest + 1)
    return [fence + info] + lines + [fence]


def _section_overview(t: _Trace) -> list[str]:
    out = ["Read upward from the target: each `S<n>` line is a mapping step (writer, operation, status, "
           "branch) and the lines under it are its inputs. `[value · …]` inputs compute the value; "
           "`[condition · …]` inputs choose between value alternatives. `■ stop` marks where the trace "
           "ended; `⚠ gap` marks where proof is missing. Row-selection columns are not shown here.", ""]
    tree = _TreeBuilder(t).build()
    out += _fence(tree)
    _, value_steps = t.reach(t.target_id, ("value", "condition", "window"))
    via_rows = [s for s in t.ordered if s["id"] in t.depth and s["id"] not in value_steps]
    unreachable = [s for s in t.ordered if s["id"] not in t.depth]
    if via_rows:
        out += ["", f"{_plural(len(via_rows), 'step')} reached only through row-selection columns "
                    f"(not value inputs): {t.step_links(via_rows, 20)} (see Detailed steps)."]
    if unreachable:
        out += ["", f"{_plural(len(unreachable), 'step')} in the trace are not reachable from the target: "
                    f"{t.step_links(unreachable, 20)} (see Detailed steps)."]
    reachable = [s for s in t.ordered if s["id"] in value_steps]
    if 0 < len(reachable) <= MERMAID_MAX_STEPS:
        out += ["", "<details><summary>Diagram (Mermaid)</summary>", ""]
        out += _mermaid(t)
        out += ["", "</details>"]
    # Route families (the overview's diagram and tables link here); imported lazily: render_overview
    # imports this module.
    from .render_overview import detailed_family_lines
    out += [""] + detailed_family_lines(t.data, t)
    return out


# --------------------------------------------------------------------------- row selection

def _section_row_selection(t: _Trace) -> list[str]:
    out = ["These columns decide **which rows** reach each step (filters, join keys, grouping). They do not "
           "compute the value and are listed separately from value inputs. They are not traced upstream "
           "unless the trace was run with `--trace-row-selection`.", ""]
    any_rows = False
    for step in t.ordered:
        items = step.get("row_selection") or []
        if not items:
            continue
        any_rows = True
        items = sorted(items, key=lambda r: (_row_role_rank(r.get("role")), str(r.get("node")),
                                             str(r.get("expression"))))
        out.append(f"**{t.step_link(step)}** — rows feeding {_code(t.display(step.get('target')))} "
                   f"({_text(_writer_text(step))})")
        out.append("")
        out += ["| Role | Column | Predicate | Status | Evidence |", "|---|---|---|---|---|"]
        for rs in items:
            node_id = rs.get("node")
            column = _code(t.display(node_id))
            traced = t.steps_by_target.get(node_id or "")
            if traced:
                column += " (traced: " + t.step_links(traced) + ")"
            status = _text(rs.get("status") or "verified")
            if rs.get("gap"):
                status += " ⚠ " + t.gap_link(rs["gap"])
            ev = rs.get("evidence")
            out.append("| " + " | ".join(_cell(c) for c in (
                _text(ROW_ROLE_LABEL.get(rs.get("role"), rs.get("role") or "?")), column,
                _code(rs.get("expression"), EXPRESSION_LIMIT), status,
                _code(t.location(ev)) if ev else "—")) + " |")
        out.append("")
    if not any_rows:
        out.append("No row-selection dependencies were recorded for the steps in this trace.")
    elif out[-1] == "":
        out.pop()
    return out


# --------------------------------------------------------------------------- detailed steps

def _section_steps(t: _Trace) -> list[str]:
    if not t.steps:
        stops = t.stops_by_node.get(t.target_id, [])
        if stops:
            return [f"No mapping steps: {_code(t.display(t.target_id))} is where the trace stops ("
                    + ", ".join(STOP_LABEL.get(s.get("reason"), _text(s.get("reason"))) for s in stops) + ")."]
        return [f"No mapping steps: no writer of {_code(t.display(t.target_id))} could be established. "
                "See Gaps and next evidence."]
    out = ["Ordered target-first, then upstream by depth."]
    for step in t.ordered:
        label = t.label[step["id"]]
        out += ["", f'<a id="{_anchor(step["id"])}"></a>', "",
                f"### {label}. {_code(t.display(step.get('target')))} ← {_code(step.get('expression'), 80)}", ""]
        depth = t.depth.get(step["id"])
        fields = [
            ("Target", f"{_code(t.display(step.get('target')))} (id {_code(step.get('target'))})"),
            ("Expression", _code(step.get("expression"), EXPRESSION_LIMIT)),
            ("Writer", _text(step.get("writer_kind") or "—")
             + (f" in {_code(step['procedure'])}" if step.get("procedure") else "")),
            ("Mapping method", _text(step.get("mapping_method") or "—")),
            ("Operation", _text(step.get("operation") or "—")),
            ("Branch context", "; ".join(_code(b, EXPRESSION_LIMIT) for b in step.get("branch_context") or [])
             or "none (unconditional)"),
            ("Status", f"**{_text(step.get('status') or 'verified')}**"),
            ("Depth", "not reachable from the target" if depth is None else str(depth)),
        ]
        writers = t.steps_by_target.get(step.get("target") or "", [])
        if len(writers) > 1:
            # A sample and a count, not every other writer: n writers of one column would otherwise make the
            # page grow with n squared. Every writer still has its own entry in this section.
            others = [s for s in writers[: LINK_SAMPLE + 1] if s["id"] != step["id"]][:LINK_SAMPLE]
            if len(writers) - 1 > len(others):
                text = (f"{len(writers) - 1} others, e.g. " + ", ".join(t.step_link(s) for s in others)
                        + f"; all {len(writers)} writers of this column are listed in this section")
            else:
                text = ", ".join(t.step_link(s) for s in others)
            fields.append(("Alternative writers", text))
        if step.get("gaps"):
            fields.append(("Gaps", ", ".join(f"⚠ {t.gap_link(g)}" for g in sorted(step["gaps"]))))
        if step.get("row_selection"):
            fields.append(("Row selection", f"{_plural(len(step['row_selection']), 'item')} — see "
                                            "[Row-selection context](#row-selection)"))
        out += [f"- **{k}:** {v}" for k, v in fields]
        evidence = sorted(step.get("evidence") or [], key=lambda e: (str(e.get("path")), e.get("start_line") or 0,
                                                                      str(e.get("role")), str(e.get("excerpt"))))
        if evidence:
            out.append("- **Evidence:**")
            out += [f"  - {t.evidence_md(ev)}" + (f" ({_text(ev['role'])})" if ev.get("role") else "")
                    for ev in evidence]
        else:
            out.append("- **Evidence:** none recorded")
        for note in step.get("notes") or []:
            out.append(f"- **Note:** {_text(note)}")
        inputs = step.get("inputs") or []
        out.append("")
        if inputs:
            out += ["| # | Input | Role | Branch | As written | Status |", "|---|---|---|---|---|---|"]
            for index, inp in enumerate(inputs, 1):
                node_id = inp.get("node")
                name = _code(t.display(node_id))
                upstream = t.steps_by_target.get(node_id or "")
                if upstream:
                    name += " → " + t.step_links(upstream)
                status = _text(inp.get("status") or "verified")
                if inp.get("gap"):
                    status += " ⚠ " + t.gap_link(inp["gap"])
                for stop in t.stops_by_node.get(node_id or "", []):
                    status += f" ■ {_text(stop.get('reason'))}"
                if inp.get("note"):
                    status += f" ({_text(inp['note'])})"
                out.append("| " + " | ".join(_cell(c) for c in (
                    str(index), name, _text(inp.get("role") or "value"),
                    _code(inp.get("branch"), LABEL_LIMIT * 2) if inp.get("branch") else "—",
                    _code(inp.get("expression"), LABEL_LIMIT * 2), status)) + " |")
        else:
            out.append("*No inputs recorded for this step.*")
    return out


# --------------------------------------------------------------------------- gaps

NEXT_EVIDENCE = {
    "dynamic_sql": "the SQL this statement builds and executes at runtime (a captured example or a confirmed "
                   "mapping), or the metadata that drives it",
    "external_object": "the definition of the external object and the code that loads it (add those files to "
                       "--source-root)",
    "missing_definition": "the definition and writers of this object (add their files to --source-root, or check "
                          "the database mapping)",
    "parse_error": "the statement at the cited line, confirmed or rewritten by hand (send its anonymized shape)",
    "unsupported_statement": "the statement at the cited line, confirmed or rewritten by hand (send its anonymized "
                             "shape)",
    "insert_exec": "the ordered result-set columns of the called procedure",
    "opaque_function": "the body of the user function (add its definition) to see what else it reads",
    "table_valued_function": "the definition of the table-valued function and its output columns",
    "ambiguous_object": "database context for the name: a USE, a three-part name or a --database-map entry",
    "ambiguous_column": "a qualified column reference or the schemas of the candidate sources",
    "unresolved_alias": "the query at the cited line; the alias names nothing in scope",
    "ambiguous_writer_order": "the run order of the listed procedures (job steps or orchestration); if they run from one entry procedure, name it with --entry-point",
    "positional_without_schema": "the ordered column list (CREATE TABLE) of the target",
    "wildcard_without_schema": "the ordered column list (CREATE TABLE) of the source behind the *",
    "column_count_mismatch": "the INSERT column list and SELECT list at the cited line",
    "unresolved_variable": "how the variable is set (OUTPUT parameter, return code, cursor or dynamic SQL)",
    "runtime_branch": "which branch runs at runtime",
    "cycle": "manual review of the cycle",
    "depth_limit": "manual review; the expression is too deeply nested to analyse",
    "unexplained_end": "manual review of the last step; please report this as a tool defect",
    "unsupported_package_step": "what the cited SSIS component or task does (script code, component settings), "
                                "or an assertion for the mapping",
    "ambiguous_package_copy": "which package copy is deployed and runs (deployment records, catalog or job "
                              "configuration); then narrow with --package-include / --package-exclude",
    "package_runtime_value": "the run-time value of the cited variable or expression (execution logs, environment "
                             "configuration), or an assertion",
    "package_expression": "the SSIS expression at the cited line, read by hand",
    "package_metadata": "the cited package element; its metadata links (lineage ids, paths, connections) are "
                        "incomplete",
    "package_column_not_mapped": "whether something else sets this column after the load (defaults, triggers, "
                                 "later UPDATEs); the package leaves it at its default or NULL",
    "package_database_unknown": "the database the cited connection points to when the package runs (environment "
                                "or parameter values); give it with --connection-map NAME=DB",
}


def _where_proof_ends(t: _Trace, gaps: list[dict]) -> list[str]:
    """Every place a value path ends, each as its own bullet, with location and next evidence."""
    out = ["### Where proof ends", ""]
    consumers: dict[str, list[tuple[dict, dict]]] = {}
    for step in t.ordered:
        for inp in step.get("inputs") or []:
            consumers.setdefault(inp.get("node"), []).append((step, inp))
    _, value_step_ids = t.reach(t.target_id, ("value", "condition", "window"))

    def via(node: str) -> list[str]:
        """One line per step that reads this node, with its location and branches."""
        grouped: dict[str, tuple[dict, list[str]]] = {}
        for step, inp in consumers.get(node, []):
            entry = grouped.setdefault(step["id"], (step, []))
            if inp.get("branch") and inp["branch"] not in entry[1]:
                entry[1].append(inp["branch"])
        lines = []
        for step, branches in list(grouped.values())[:5]:
            ev = (step.get("evidence") or [None])[0]
            text = "Reached via " + t.step_link(step) + (f" at {_code(t.location(ev))}" if ev else "")
            if branches:
                text += " — branch " + ", ".join(_code(b, 80) for b in branches)
            lines.append(text)
        if len(grouped) > 5:
            lines.append(f"Reached via {len(grouped) - 5} more steps (see Detailed steps)")
        return lines

    boundary = t.data.get("boundary") or {}
    requested = [*(f"database {d}" for d in boundary.get("databases") or []),
                 *(f"objects {o}" for o in boundary.get("objects") or [])]
    if requested and not any(s.get("reason") == "boundary" for s in t.stops):
        out.append(f"- **No value path reaches the requested boundary** ({_text(', '.join(requested))}); every "
                   "path ends at one of the points below first.")

    by_reason: dict[str, list[dict]] = {}
    for stop in t.stops:
        by_reason.setdefault(stop.get("reason") or "stop", []).append(stop)
    for reason in sorted(by_reason):
        out.append(f"- Stopped — {STOP_LABEL.get(reason, _text(reason))}:")
        for stop in by_reason[reason]:
            line = f"  - {_code(t.display(stop.get('node')))}"
            if stop.get("detail"):
                line += f" ({_text(stop['detail'])})"
            if stop.get("declared_terminal"):
                line += (f" — **declared terminal for this run** ({_text(stop['declared_terminal'])}); how this "
                         "source is populated upstream is not proved")
            out.append(line)
            out += [f"    - {item}" for item in via(stop.get("node"))]

    gap_nodes = sorted({g["node"] for g in gaps if g.get("node")})
    if gap_nodes:
        out.append("- Proof is missing at:")
        for node in gap_nodes:
            node_gaps = [g for g in gaps if g.get("node") == node]
            kinds = sorted({g.get("kind") or "gap" for g in node_gaps})
            scope = "row selection only" if all(g.get("affects") == "row_selection" for g in node_gaps) else "value path"
            out.append(f"  - {_code(t.display(node))} — {_text(', '.join(kinds))} ({scope})")
            places = sorted({t.location(ev) for g in node_gaps for ev in (g.get("evidence") or [])[:1]})
            if places:
                out.append("    - Where: " + ", ".join(_code(p) for p in places[:5])
                           + (f" and {len(places) - 5} more" if len(places) > 5 else ""))
            out += [f"    - {item}" for item in via(node)]
            for kind in kinds:
                out.append(f"    - Next evidence ({_text(kind)}): {_text(NEXT_EVIDENCE.get(kind, 'manual review of the cited statement'))}")

    gap_free = [g for g in gaps if not g.get("node")]
    if gap_free:
        out.append("- Not tied to a node (procedure or statement level):")
        for gap in sorted(gap_free, key=lambda g: (g.get("kind") or "", g.get("id") or "")):
            ev = (gap.get("evidence") or [None])[0]
            where = f" — {_code(t.location(ev))}" if ev else ""
            out.append(f"  - {_text(gap.get('kind') or 'gap')}: {_text(gap.get('message') or '')}{where}")
            out.append(f"    - Next evidence: {_text(NEXT_EVIDENCE.get(gap.get('kind'), 'manual review of the cited statement'))}")

    weak: dict[tuple[str, str], list[dict]] = {}
    for step in t.ordered:
        if step["id"] in value_step_ids and (step.get("status") or "verified") in ("inferred", "asserted", "partial", "ambiguous"):
            weak.setdefault((step.get("status"), step.get("mapping_method") or "?"), []).append(step)
    if weak:
        total = sum(len(v) for v in weak.values())
        out.append(f"- Weaker links on the value paths (not ends, but not verified): {_plural(total, 'step')}")
        for (status, method), steps in sorted(weak.items(), key=lambda kv: (_status_rank(kv[0][0]), kv[0][1])):
            out.append(f"  - {_text(status)} — {_text(method)}: {_join_list([t.step_link(s) for s in steps], 12)}")

    by_statement: dict[str, dict] = {}
    for gap in gaps:
        ev = (gap.get("evidence") or [None])[0]
        if not ev:
            continue
        key = ev.get("statement_id") or t.location(ev)
        entry = by_statement.setdefault(key, {"ev": ev, "kinds": set(), "nodes": set(), "gaps": 0})
        entry["kinds"].add(gap.get("kind") or "gap")
        entry["gaps"] += 1
        if gap.get("node"):
            entry["nodes"].add(gap["node"])
    repeated = sorted(by_statement.values(), key=lambda e: (-e["gaps"], t.location(e["ev"])))
    cited = sum(e["gaps"] for e in by_statement.values())
    if cited > len(by_statement) and repeated:
        uncited = len(gaps) - cited
        out.append(f"- Statements behind the most gaps ({_plural(cited, 'gap')} that cite a statement come from "
                   f"{_plural(len(by_statement), 'statement')}"
                   + (f"; {_plural(uncited, 'other gap')} cite no statement and are listed by node above" if uncited else "")
                   + "; resolving or asserting these first helps most):")
        for entry in repeated[:10]:
            out.append(f"  - {_code(t.location(entry['ev']))} — {_plural(entry['gaps'], 'gap')} "
                       f"({_text(', '.join(sorted(entry['kinds'])))}) on {_plural(len(entry['nodes']), 'node')}")
        if len(repeated) > 10:
            out.append(f"  - {len(repeated) - 10} more statements (see Gaps)")

    resolved = t.data.get("resolved_gaps") or []
    if resolved:
        out.append(f"- Explained by assertions, not proved from SQL: {_plural(len(resolved), 'gap')}")
        for gap in sorted(resolved, key=lambda g: (g.get("resolved_by") or "", g.get("kind") or "")):
            ev = (gap.get("evidence") or [None])[0]
            where = f" at {_code(t.location(ev))}" if ev else ""
            node = f" on {_code(t.display(gap['node']))}" if gap.get("node") else ""
            out.append(f"  - {_text(gap.get('kind') or 'gap')}{node}{where} — assertion {_code(gap.get('resolved_by') or '?')}")

    if len(out) > 2:
        out.append("")
    result, _ = _result_text(t)
    if gaps:
        out.append(f"Proof ends at the {_plural(len(gap_nodes), 'node')} listed above with gaps; value paths "
                   f"through them are unproven. The result is **{_text(result)}**. Stops mark where the "
                   "trace ended, not ultimate source-system fields.")
    elif t.stops:
        out.append(f"Every recorded value path ends at a stop. The result is **{_text(result)}**. Stops mark "
                   "where the trace ended, not ultimate source-system fields.")
    else:
        out.append(f"No stops or gaps were recorded. The result is **{_text(result)}**.")
    return out


def _section_gaps(t: _Trace, json_name: str | None) -> list[str]:
    out: list[str] = []
    gaps = t.sorted_gaps()
    if gaps:
        by_kind: dict[str, list[dict]] = {}
        for gap in gaps:
            by_kind.setdefault(gap.get("kind") or "gap", []).append(gap)
        users_by_gap: dict[str, list[dict]] = {}
        for step in t.ordered:
            refs = set(step.get("gaps") or []) | {i.get("gap") for i in step.get("inputs") or []} \
                | {r.get("gap") for r in step.get("row_selection") or []}
            for ref in sorted(refs - {None}):
                users_by_gap.setdefault(ref, []).append(step)
        for kind in sorted(by_kind):
            out += [f"### {_text(kind)} ({len(by_kind[kind])})", ""]
            for gap in by_kind[kind]:
                out.append(f'- <a id="{_anchor(gap["id"])}"></a>**{_text(gap.get("message") or kind)}**')
                if gap.get("node"):
                    out.append(f"  - Node: {_code(t.display(gap['node']))} (id {_code(gap['node'])})")
                users = users_by_gap.get(gap["id"], [])
                if users:
                    out.append("  - Affects: " + t.step_links(users, AFFECTS_MAX))
                evidence = gap.get("evidence") or []
                for ev in evidence:
                    out.append(f"  - Evidence: {t.evidence_md(ev)}")
                if not evidence:
                    out.append("  - Evidence: none recorded")
                detail = gap.get("detail") or {}
                for key in sorted(detail):
                    value = detail[key]
                    if isinstance(value, (list, tuple)):
                        rendered = ", ".join(_code(v, 80) for v in value) or "—"
                    elif isinstance(value, dict):
                        rendered = _code(json.dumps(value, sort_keys=True, ensure_ascii=False), EXCERPT_LIMIT)
                    else:
                        rendered = _code(value, EXCERPT_LIMIT)
                    out.append(f"  - {_text(key)}: {rendered}")
            out.append("")
    else:
        out += ["No gaps were recorded.", ""]

    weak = [s for s in t.ordered if (s.get("status") or "verified") != "verified"]
    if weak:
        out += ["### Steps that are not verified", ""]
        out += [f"- {t.step_link(s)} {_code(t.display(s.get('target')))} — **{_text(s.get('status'))}**"
                + (f" ({_text(_writer_text(s))})") for s in weak]
        out.append("")
    if t.cycles:
        out += ["### Cycles", ""]
        out += ["- " + " → ".join(_code(t.display(n)) for n in cycle) for cycle in t.cycles]
        out.append("")

    out += _where_proof_ends(t, gaps)
    out.append("")
    if json_name:
        out.append(f"Companion JSON (complete trace graph and full evidence): "
                   f"[{_text(json_name)}]({_link_target(json_name)})")
    else:
        out.append("Companion JSON: not named; this page was rendered from the trace JSON "
                   "(`python -m column_lineage.render_markdown <trace.json>` regenerates it).")
    return out


# --------------------------------------------------------------------------- page

def render_markdown(data: dict, json_name: str | None = None) -> str:
    """Render the JSON form of a trace (``TraceResult.to_dict()``) as a Markdown page."""
    t = _Trace(data or {})
    title = t.display(t.target_id)
    out = [f"# Column lineage: {_code(title)}", ""]
    sections = (
        _section_identity(t, json_name),
        _section_short_answer(t),
        _section_overview(t),
        _section_row_selection(t),
        _section_steps(t),
        _section_gaps(t, json_name),
    )
    for number, (heading, anchor, body) in enumerate(zip(SECTION_TITLES, SECTION_ANCHORS, sections), 1):
        out += [f'<a id="{anchor}"></a>', "", f"## {number}. {heading}", ""]
        out += body
        out.append("")
    while out and out[-1] == "":
        out.pop()
    return "\n".join(out) + "\n"


def _default_output(json_path: Path) -> Path:
    name = json_path.name
    if name.endswith(".json"):
        name = name[: -len(".json")]
    return json_path.with_name(name + ".md")


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(prog="python -m column_lineage.render_markdown",
                                     description="Regenerate a column lineage Markdown page from its JSON trace.")
    parser.add_argument("trace", help="path to a <stem>.lineage.json trace")
    parser.add_argument("-o", "--output", help="output Markdown path ('-' for stdout); "
                                               "default: the JSON path with .json replaced by .md")
    args = parser.parse_args(argv)
    json_path = Path(args.trace)
    try:
        data = json.loads(json_path.read_text(encoding="utf-8"))
    except (OSError, ValueError) as exc:
        print(f"error: cannot read trace {json_path}: {exc}", file=sys.stderr)
        return 2
    if not isinstance(data, dict):
        print(f"error: {json_path} does not contain a trace object", file=sys.stderr)
        return 2
    if args.output == "-":
        # Write UTF-8 bytes: the page contains symbols a Windows console code page cannot encode.
        text = render_markdown(data, json_path.name)
        buffer = getattr(sys.stdout, "buffer", None)
        if buffer is not None:
            buffer.write(text.encode("utf-8"))
            buffer.flush()
        else:
            sys.stdout.write(text)
        return 0
    out_path = Path(args.output) if args.output else _default_output(json_path)
    json_name = os.path.relpath(json_path.resolve(), out_path.resolve().parent).replace(os.sep, "/")
    out_path.parent.mkdir(parents=True, exist_ok=True)
    with open(out_path, "w", encoding="utf-8", newline="\n") as handle:
        handle.write(render_markdown(data, json_name))
    print(out_path)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
