"""Turn one writer statement into column mapping steps.

For a requested target column the analyzer finds the expression that produces it
in each writer and branch, binds every column reference in that expression to an
identity, labels each input's role, and records the row-selection clauses.
CTEs and derived tables become statement-local nodes with their own steps.
"""
from __future__ import annotations

from dataclasses import dataclass, field

from sqlglot import exp

from table_lineage.index import Statement

from .catalog import Catalog, ColumnInfo, Unit, Writer, ident_text, norm, statement_id, table_parts, update_target
from .model import (
    AMBIGUOUS, COLUMN, CONDITION, CONSTANT, FILTER, GROUP_BY, HAVING, INFERRED, JOIN_KEY, JOIN_PREDICATE,
    LOCAL_COLUMN, MERGE_CONDITION, PARAMETER, PARTIAL, TEMP_COLUMN, UNKNOWN, UNRESOLVED, VALUE, VARIABLE,
    VERIFIED, WINDOW, Gap, Input, Node, ObjectRef, RowSelection, Step, worst,
)

SET_OPS = (exp.Union, exp.Except, exp.Intersect)
EXTERNAL_FUNCTIONS = {"openquery", "openrowset", "opendatasource", "openxml", "openjson"}


def unparen(e):
    while isinstance(e, exp.Paren):
        e = e.this
    return e


def branches(query) -> list[exp.Expression]:
    query = unparen(query)
    if isinstance(query, exp.Subquery):
        return branches(query.this)
    if isinstance(query, SET_OPS):
        return branches(query.this) + branches(query.expression)
    return [query]


def set_op_label(query) -> str:
    query = unparen(query)
    if isinstance(query, exp.Union):
        return "UNION" if query.args.get("distinct") else "UNION ALL"
    if isinstance(query, exp.Except):
        return "EXCEPT"
    if isinstance(query, exp.Intersect):
        return "INTERSECT"
    return ""


def conjuncts(e) -> list[exp.Expression]:
    e = unparen(e)
    if isinstance(e, exp.And):
        return conjuncts(e.this) + conjuncts(e.expression)
    return [e]


@dataclass
class Local:
    kind: str                 # cte, derived, values
    name: str
    query: exp.Expression
    column_aliases: list[str] | None
    ctes: dict
    parent: "Scope | None"
    ref: ObjectRef


@dataclass
class Source:
    alias: str
    kind: str                 # object, local, opaque
    refs: list[tuple[ObjectRef, str]] = field(default_factory=list)
    columns: list[str] | None = None
    reliable: bool = False
    local: Local | None = None
    gap: str | None = None
    note: str | None = None
    names: tuple[str, ...] = ()   # extra names this source answers to (object name, schema.name)


@dataclass
class Item:
    name: str | None
    expr: exp.Expression | None
    source: Source | None = None   # set for columns expanded from a wildcard
    column: str | None = None
    unknown: bool = False          # a wildcard that could not be expanded


@dataclass
class Binding:
    node: Node
    status: str
    gap: str | None = None
    note: str | None = None


class Ctx:
    """Per-statement working state."""

    def __init__(self, analyzer: "Analyzer", unit: Unit, statement: Statement, controls: tuple[str, ...] = ()):
        self.analyzer = analyzer
        self.catalog = analyzer.catalog
        self.unit = unit
        self.statement = statement
        self.stmt_id = statement_id(unit, statement)
        self.scope_name = statement.scope
        self.controls = controls
        self.steps: list[Step] = []
        self.gaps: list[Gap] = []
        self.local_done: dict[tuple[str, str], Node] = {}
        self.local_active: set[tuple[str, str]] = set()
        self.cycles: list[list[str]] = []

    def gap(self, kind: str, message: str, node: Node | None = None, expr: exp.Expression | None = None,
            detail: dict | None = None) -> str:
        gap = Gap(kind, message, node, [self.catalog.evidence(self.unit, self.statement, expr, "gap")], detail or {})
        if all(g.id != gap.id for g in self.gaps):
            self.gaps.append(gap)
        return gap.id

    def evidence(self, expr=None, role="expression"):
        return self.catalog.evidence(self.unit, self.statement, expr, role)


class Scope:
    """Names visible to one SELECT (or UPDATE/MERGE) level."""

    def __init__(self, ctx: Ctx, ctes: dict, parent: "Scope | None" = None):
        self.ctx = ctx
        self.ctes = ctes
        self.parent = parent
        self.sources: list[Source] = []

    # ---------------------------------------------------------------- sources
    def add_from(self, node) -> Source | None:
        source = self.ctx.analyzer.source_for(node, self)
        if source is not None:
            self.sources.append(source)
        return source

    def add_select_sources(self, select: exp.Expression) -> None:
        from_ = select.args.get("from_")
        if from_ is not None and from_.this is not None:
            self.add_from(from_.this)
            for join in from_.args.get("joins") or []:
                self.add_from(join.this)
        for join in select.args.get("joins") or []:
            self.add_from(join.this)

    def find_alias(self, qualifier: str) -> list[Source]:
        q = qualifier.lower()
        exact = [s for s in self.sources if s.alias == q]
        if exact:
            return exact
        return [s for s in self.sources if q in s.names]

    # ---------------------------------------------------------------- binding
    def bind(self, column: exp.Column, expand_local: bool = True) -> list[Binding]:
        name = norm(ident_text(column.this)) or ""
        qualifier_parts = [ident_text(column.args.get(k)) for k in ("catalog", "db", "table") if column.args.get(k)]
        qualifier = ".".join(qualifier_parts).lower() if qualifier_parts else None
        scope: Scope | None = self
        while scope is not None:
            result = scope._bind_here(name, qualifier, column, expand_local)
            if result is not None:
                if scope is not self and result:
                    result = [Binding(b.node, b.status, b.gap, (b.note + "; " if b.note else "") + "correlated outer reference")
                              for b in result]
                return result
            scope = scope.parent
        ctx = self.ctx
        node = Node(UNKNOWN, column.sql(dialect="tsql"))
        if qualifier:
            gap = ctx.gap("unresolved_alias", f"Alias or object '{qualifier}' in {column.sql(dialect='tsql')} is not "
                          "visible in this query.", node, column)
        else:
            gap = ctx.gap("ambiguous_column", f"Column {name} could not be bound to any source in this query.", node, column)
        return [Binding(node, UNRESOLVED, gap)]

    def _bind_here(self, name, qualifier, column, expand_local) -> list[Binding] | None:
        if qualifier:
            candidates = self.find_alias(qualifier.split(".")[-1]) if "." not in qualifier else \
                [s for s in self.sources if qualifier in s.names or qualifier.split(".")[-1] == s.alias]
            if not candidates:
                return None
            if len(candidates) > 1:
                node = Node(UNKNOWN, column.sql(dialect="tsql"))
                gap = self.ctx.gap("ambiguous_column", f"Qualifier '{qualifier}' matches several sources.", node, column)
                return [Binding(node, AMBIGUOUS, gap)]
            return self.ctx.analyzer.source_column(candidates[0], name, self, column, expand_local)
        known = [s for s in self.sources if s.columns is not None and name in s.columns]
        unknown = [s for s in self.sources if s.columns is None]
        if len(known) == 1 and not unknown:
            return self.ctx.analyzer.source_column(known[0], name, self, column, expand_local)
        if len(self.sources) == 1:
            return self.ctx.analyzer.source_column(self.sources[0], name, self, column, expand_local)
        if len(known) == 1 and unknown:
            # Working SQL cannot have two sources supplying an unqualified name, so the known one is
            # the binding provided the unknown sources lack it; that is an assumption, not proof.
            out = self.ctx.analyzer.source_column(known[0], name, self, column, expand_local)
            return [Binding(b.node, worst(b.status, INFERRED), b.gap,
                            "bound by elimination: other sources have no known schema") for b in out]
        if not known and len(unknown) == 1 and self.sources:
            # Only one source could supply it, assuming the declared schemas are complete.
            out = self.ctx.analyzer.source_column(unknown[0], name, self, column, expand_local)
            return [Binding(b.node, worst(b.status, INFERRED), b.gap,
                            "bound by elimination: the only source without a known schema") for b in out]
        if not self.sources:
            return None
        if not known and not unknown:
            return None  # maybe an outer (correlated) reference
        node = Node(UNKNOWN, column.sql(dialect="tsql"))
        candidates = sorted(s.alias for s in (known or unknown))
        gap = self.ctx.gap("ambiguous_column", f"Unqualified column {name} could come from any of: {', '.join(candidates)}.",
                           node, column, {"candidates": candidates})
        return [Binding(node, AMBIGUOUS, gap)]


class Analyzer:
    def __init__(self, catalog: Catalog):
        self.catalog = catalog
        self._object_columns: dict[str, tuple[list[str] | None, bool]] = {}
        self._object_columns_active: set[str] = set()

    # ================================================================ sources
    def resolve_table(self, table: exp.Table, ctx: Ctx) -> list[tuple[ObjectRef, str]]:
        """Object candidates for a table reference with a binding status each."""
        ref = self.catalog.ref_from_table(table, ctx.unit, ctx.statement)
        if ref.kind == "temp_table":
            scopes = self.catalog.temp_scopes(ref.name, ctx.scope_name)
            if not scopes:
                return [(ref, VERIFIED)]
            if scopes == [ctx.scope_name]:
                return [(ref, VERIFIED)]
            return [(ObjectRef(ref.name, kind="temp_table", scope=s), INFERRED) for s in scopes]
        if ref.kind == "table_variable":
            return [(ref, VERIFIED)]
        canonical, candidates = self.catalog.canonical(ref)
        if candidates:
            return [(ObjectRef(ref.name, ref.schema, db, ref.server, ref.kind, None, "candidate"), AMBIGUOUS)
                    for db in candidates]
        status = VERIFIED
        if canonical.database_source in ("default", "unified", "configured") or (
                canonical.database_source is None and canonical.database is None and False):
            status = INFERRED if canonical.database_source in ("default", "unified") else VERIFIED
        _, _, schema, _ = table_parts(table)
        if not schema and canonical.kind in ("table", "view"):
            status = worst(status, INFERRED)
        return [(canonical, status)]

    def source_for(self, node, scope: Scope) -> Source | None:
        ctx = scope.ctx
        alias_node = node.args.get("alias") if isinstance(node, exp.Expression) else None
        alias = norm(alias_node.name) if isinstance(alias_node, exp.TableAlias) and alias_node.name else None
        alias_columns = [norm(ident_text(c)) for c in alias_node.columns] if isinstance(alias_node, exp.TableAlias) and alias_node.columns else None
        if isinstance(node, exp.Table):
            server, database, schema, name = table_parts(node)
            if isinstance(node.this, (exp.Func, exp.Anonymous)) or (isinstance(node.this, exp.Dot) and isinstance(node.this.expression, exp.Func)):
                func = node.this if not isinstance(node.this, exp.Dot) else node.this.expression
                fname = (func.name or func.sql(dialect="tsql")).lower()
                kind = "external_object" if fname in EXTERNAL_FUNCTIONS else "table_valued_function"
                n = Node(UNKNOWN, node.sql(dialect="tsql"))
                gap = ctx.gap(kind, f"Rows come from {fname.upper()}(...); its columns are not traced.", n, node)
                return Source(alias or fname, "opaque", gap=gap, names=(fname,))
            lname = norm(name) or ""
            if not database and not schema and lname in scope_ctes(scope):
                local = scope_ctes(scope)[lname]
                return Source(alias or lname, "local", columns=self.local_names(local, ctx), reliable=True,
                              local=local, names=(lname,))
            refs = self.resolve_table(node, ctx)
            ref = refs[0][0]
            columns, reliable = self.object_columns(ref) if len(refs) == 1 else (None, False)
            names = {lname}
            if schema:
                names.add(f"{norm(schema)}.{lname}")
            if database and schema:
                names.add(f"{norm(database)}.{norm(schema)}.{lname}")
            return Source(alias or lname, "object", refs, columns, reliable, names=tuple(sorted(names)))
        if isinstance(node, (exp.Subquery, exp.Lateral)):
            inner = node.this
            if isinstance(inner, exp.Subquery):
                alias_node = alias_node or inner.args.get("alias")
                alias = alias or (norm(alias_node.name) if isinstance(alias_node, exp.TableAlias) and alias_node.name else None)
                if alias_columns is None and isinstance(alias_node, exp.TableAlias) and alias_node.columns:
                    alias_columns = [norm(ident_text(c)) for c in alias_node.columns]
                inner = inner.this
            name = alias or f"derived{len(scope.sources) + 1}"
            ctes = dict(scope_ctes(scope))
            local = Local("derived", name, inner, alias_columns, ctes,
                          scope if isinstance(node, exp.Lateral) else scope.parent,
                          ObjectRef(name, kind="derived", scope=ctx.stmt_id))
            return Source(name, "local", columns=self.local_names(local, ctx), reliable=True, local=local, names=(name,))
        if isinstance(node, exp.Values):
            name = alias or f"values{len(scope.sources) + 1}"
            local = Local("values", name, node, alias_columns, {}, None, ObjectRef(name, kind="derived", scope=ctx.stmt_id))
            return Source(name, "local", columns=alias_columns, reliable=bool(alias_columns), local=local, names=(name,))
        n = Node(UNKNOWN, node.sql(dialect="tsql") if isinstance(node, exp.Expression) else str(node))
        gap = ctx.gap("unsupported_statement", f"Unsupported row source {type(node).__name__}.", n,
                      node if isinstance(node, exp.Expression) else None)
        return Source(alias or f"source{len(scope.sources) + 1}", "opaque", gap=gap)

    def object_columns(self, ref: ObjectRef) -> tuple[list[str] | None, bool]:
        """Ordered column names of an object and whether that order is reliable."""
        key = ref.key
        if key in self._object_columns:
            return self._object_columns[key]
        if key in self._object_columns_active:
            return None, False
        self._object_columns_active.add(key)
        try:
            columns, status, _ = self.catalog.schema_for(ref)
            if status == "declared":
                result = ([c.name for c in columns], True)
            else:
                result = (None, False)
                definers = [w for w in self.catalog.writers_for(ref) if w.kind in ("view", "select_into")]
                name_sets = []
                for writer in definers:
                    names = self.writer_output_names(writer)
                    if names is not None:
                        name_sets.append(tuple(names))
                    else:
                        name_sets.append(None)
                if definers and None not in name_sets and len(set(name_sets)) == 1:
                    result = (list(name_sets[0]), True)
                elif definers and name_sets and all(n is not None for n in name_sets):
                    result = (sorted({c for names in name_sets for c in names}), False)
        finally:
            self._object_columns_active.discard(key)
        self._object_columns[key] = result
        return result

    def writer_output_names(self, writer: Writer) -> list[str] | None:
        tree = writer.statement.tree
        ctx = Ctx(self, writer.unit, writer.statement)
        if writer.kind == "view":
            if isinstance(tree.this, exp.Schema) and tree.this.expressions:
                return [norm(ident_text(c)) for c in tree.this.expressions]
            query = tree.args.get("expression")
        else:
            query = tree
        ctes = statement_ctes(tree, ctx)
        first = branches(query)[0]
        if not isinstance(first, exp.Select):
            return None
        scope = Scope(ctx, ctes)
        scope.add_select_sources(first)
        items = self.projection(first, scope)
        if any(i.unknown or not i.name for i in items):
            return None
        return [i.name for i in items]

    def local_names(self, local: Local, ctx: Ctx) -> list[str] | None:
        if local.column_aliases:
            return list(local.column_aliases)
        if local.kind == "values":
            return None
        first = branches(local.query)[0]
        if not isinstance(first, exp.Select):
            return None
        scope = Scope(ctx, local_ctes(local, first), local.parent)
        scope.add_select_sources(first)
        items = self.projection(first, scope)
        if any(i.unknown for i in items):
            return None
        return [i.name for i in items]

    # ============================================================ projections
    def projection(self, select: exp.Select, scope: Scope) -> list[Item]:
        items: list[Item] = []
        for expression in select.expressions:
            if isinstance(expression, exp.EQ) and isinstance(expression.this, exp.Parameter):
                continue  # SELECT @v = ... is an assignment, not an output column
            target = expression.this if isinstance(expression, exp.Alias) else expression
            if isinstance(target, exp.Star) or (isinstance(target, exp.Column) and isinstance(target.this, exp.Star)):
                qualifier = target.table if isinstance(target, exp.Column) else None
                chosen = scope.find_alias(qualifier) if qualifier else list(scope.sources)
                for source in chosen:
                    if source.columns is None or not source.reliable:
                        items.append(Item(None, target, source, None, unknown=True))
                        continue
                    for column in source.columns:
                        items.append(Item(column, target, source, column))
                if not chosen:
                    items.append(Item(None, target, None, None, unknown=True))
                continue
            if isinstance(expression, exp.Alias):
                items.append(Item(norm(expression.alias), expression.this))
            elif isinstance(expression, exp.Column):
                items.append(Item(norm(ident_text(expression.this)), expression))
            else:
                items.append(Item(None, expression))
        return items

    # ============================================================== locals
    def source_column(self, source: Source, name: str, scope: Scope, column: exp.Column,
                      expand_local: bool = True) -> list[Binding]:
        ctx = scope.ctx
        if source.kind == "opaque":
            node = Node(UNKNOWN, column.sql(dialect="tsql"))
            return [Binding(node, UNRESOLVED, source.gap)]
        if source.kind == "local":
            local = source.local
            if source.columns is not None and name not in source.columns:
                node = Node(LOCAL_COLUMN, name, local.ref)
                gap = ctx.gap("ambiguous_column", f"{local.kind} {local.name} has no output column {name}.", node, column)
                return [Binding(node, UNRESOLVED, gap)]
            node = Node(LOCAL_COLUMN, name, local.ref)
            if expand_local:
                self.local_steps(local, name, ctx)
            return [Binding(node, VERIFIED)]
        out = []
        for ref, status in source.refs:
            kind = TEMP_COLUMN if ref.kind in ("temp_table", "table_variable") else COLUMN
            node = Node(kind, name, ref)
            note = None
            if source.columns is not None and name not in source.columns and source.reliable:
                gap = ctx.gap("missing_definition", f"{ref.display} has no column {name} in its known schema.", node, column)
                out.append(Binding(node, UNRESOLVED, gap))
                continue
            if status == AMBIGUOUS:
                gap = ctx.gap("ambiguous_object", f"{ref.schema}.{ref.name} has no database context and exists in "
                              f"several databases.", node, column,
                              {"candidates": sorted(r.database or "?" for r, _ in source.refs)})
                out.append(Binding(node, AMBIGUOUS, gap))
                continue
            if status == INFERRED and ref.kind == "temp_table" and ref.scope != ctx.scope_name:
                note = f"#temp table created by caller {ref.scope_label}"
            out.append(Binding(node, status, None, note))
        return out

    def local_steps(self, local: Local, name: str, ctx: Ctx) -> Node:
        node = Node(LOCAL_COLUMN, name, local.ref)
        key = (local.ref.key, name)
        if key in ctx.local_done:
            return node
        if key in ctx.local_active:
            ctx.cycles.append([node.id, node.id])
            return node
        ctx.local_active.add(key)
        try:
            if local.kind == "values":
                rows = [r for r in local.query.expressions if isinstance(r, exp.Tuple)]
                position = local.column_aliases.index(name) if local.column_aliases and name in local.column_aliases else None
                for i, row in enumerate(rows, 1):
                    if position is None or position >= len(row.expressions):
                        continue
                    value = row.expressions[position]
                    step = self.make_step(node, "derived", value, Scope(ctx, {}), ctx, "column_alias_list",
                                          [f"VALUES row {i} of {len(rows)}"] if len(rows) > 1 else [])
                    ctx.steps.append(step)
            else:
                parts = branches(local.query)
                names = self.local_names(local, ctx)
                label = set_op_label(local.query)
                for i, select in enumerate(parts, 1):
                    if not isinstance(select, exp.Select):
                        continue
                    scope = Scope(ctx, local_ctes(local, select), local.parent)
                    scope.add_select_sources(select)
                    items = self.projection(select, scope)
                    item = self.pick(items, names, name, i == 1)
                    if item is None:
                        if i == 1:
                            ctx.gap("wildcard_without_schema" if any(it.unknown for it in items) else "missing_definition",
                                    f"Output column {name} of {local.kind} {local.name} could not be located.", node, select)
                        continue
                    context = [f"{label} branch {i} of {len(parts)}"] if len(parts) > 1 else []
                    method = "column_alias_list" if local.column_aliases else "projection_alias"
                    ctx.steps.append(self.item_step(node, local.kind, item, scope, ctx, method, context, select))
            ctx.local_done[key] = node
        finally:
            ctx.local_active.discard(key)
        return node

    @staticmethod
    def pick(items: list[Item], names: list[str] | None, name: str, by_name: bool) -> Item | None:
        """Locate an output column: by position from the first branch's names."""
        if names is not None and name in names:
            position = names.index(name)
            if names.count(name) > 1:
                return None
            if position < len(items) and not any(it.unknown for it in items[:position + 1]):
                return items[position]
            return None
        if by_name:
            matches = [it for it in items if it.name == name and not it.unknown]
            prior_unknown = any(it.unknown for it in items)
            if len(matches) == 1 and not prior_unknown:
                return matches[0]
        return None

    # ================================================================= steps
    def item_step(self, target: Node, writer_kind: str, item: Item, scope: Scope, ctx: Ctx, method: str,
                  context: list[str], select: exp.Expression | None) -> Step:
        if item.source is not None and item.column is not None:
            column = exp.column(item.column, table=item.source.alias)
            step = self.make_step(target, writer_kind, column, scope, ctx, "wildcard_schema", context)
            step.notes.append(f"Expanded from {item.expr.sql(dialect='tsql')} using the ordered schema of {item.source.alias}.")
        else:
            step = self.make_step(target, writer_kind, item.expr, scope, ctx, method, context)
        if select is not None:
            step.row_selection.extend(self.row_selection(select, scope, ctx))
            step.status = worst(step.status, *[r.status for r in step.row_selection if r.status == AMBIGUOUS])
        return step

    def make_step(self, target: Node, writer_kind: str, value: exp.Expression, scope: Scope, ctx: Ctx,
                  method: str, context: list[str], procedure: str | None = None) -> Step:
        inputs: list[Input] = []
        extra_rows: list[RowSelection] = []
        gaps_before = {g.id for g in ctx.gaps}
        self.walk(value, VALUE, None, scope, ctx, inputs, extra_rows, True)
        inputs = dedupe_inputs(inputs)
        operation = classify(value, target.name)
        step = Step(target=target, writer_kind=writer_kind, expression=value.sql(dialect="tsql"),
                    operation=operation, inputs=inputs, row_selection=extra_rows,
                    branch_context=list(ctx.controls) + list(context), mapping_method=method,
                    procedure=procedure if procedure is not None else ctx.statement.procedure,
                    evidence=[ctx.evidence(value, "expression")], order=ctx.statement.start)
        step.gaps = sorted({g.id for g in ctx.gaps if g.id not in gaps_before} | {i.gap for i in inputs if i.gap})
        statuses = [i.status for i in inputs]
        step.status = worst(*statuses) if statuses else VERIFIED
        if any(i.status == UNRESOLVED for i in inputs) and any(i.status != UNRESOLVED for i in inputs):
            step.status = worst(PARTIAL, *[s for s in statuses if s != UNRESOLVED])
        if step.gaps and step.status in (VERIFIED, INFERRED):
            step.status = PARTIAL
        return step

    # ============================================================ expressions
    def walk(self, e, role, branch, scope: Scope, ctx: Ctx, out: list[Input], rows: list[RowSelection],
             const_ok: bool) -> None:
        if e is None or not isinstance(e, exp.Expression):
            return
        e = unparen(e)
        if isinstance(e, exp.Column):
            if isinstance(e.this, exp.Star):
                return
            for b in scope.bind(e):
                out.append(Input(b.node, role, e.sql(dialect="tsql"), branch, b.status, b.gap, b.note))
            return
        if isinstance(e, (exp.Literal, exp.Null, exp.Boolean)):
            if role == VALUE and const_ok:
                out.append(Input(Node(CONSTANT, e.sql(dialect="tsql")), VALUE, e.sql(dialect="tsql"), branch))
            return
        if isinstance(e, exp.DataType) or isinstance(e, exp.Var):
            return
        if isinstance(e, exp.Parameter):
            name = e.sql(dialect="tsql").lower()
            if name.startswith("@@"):
                if role == VALUE and const_ok:
                    out.append(Input(Node(CONSTANT, name), VALUE, name, branch, VERIFIED, None, "system function"))
                return
            kind = PARAMETER if ctx.catalog.is_parameter(ctx.scope_name, name) else VARIABLE
            out.append(Input(Node(kind, name, None, ctx.scope_name), role, name, branch))
            return
        if isinstance(e, exp.Case):
            if e.this is not None:
                self.walk(e.this, CONDITION, join_branch(branch, "CASE operand"), scope, ctx, out, rows, False)
            for case_if in e.args.get("ifs") or []:
                cond = case_if.this.sql(dialect="tsql")
                when = f"WHEN {e.this.sql(dialect='tsql')} = {cond}" if e.this is not None else f"WHEN {cond}"
                self.walk(case_if.this, CONDITION, join_branch(branch, when), scope, ctx, out, rows, False)
                self.walk(case_if.args.get("true"), role, join_branch(branch, when + " THEN"), scope, ctx, out, rows, True)
            default = e.args.get("default")
            if default is not None:
                self.walk(default, role, join_branch(branch, "ELSE"), scope, ctx, out, rows, True)
            elif role == VALUE:
                out.append(Input(Node(CONSTANT, "NULL"), VALUE, "NULL", join_branch(branch, "ELSE (implicit NULL)")))
            return
        if isinstance(e, exp.If):
            cond = e.this.sql(dialect="tsql")
            self.walk(e.this, CONDITION, join_branch(branch, f"IIF {cond}"), scope, ctx, out, rows, False)
            self.walk(e.args.get("true"), role, join_branch(branch, f"IIF {cond} THEN"), scope, ctx, out, rows, True)
            self.walk(e.args.get("false"), role, join_branch(branch, f"IIF {cond} ELSE"), scope, ctx, out, rows, True)
            return
        if isinstance(e, exp.Coalesce):
            label = "ISNULL" if e.args.get("is_null") else "COALESCE"
            args = [e.this] + list(e.expressions or [])
            for i, arg in enumerate(args, 1):
                self.walk(arg, role, join_branch(branch, f"{label} argument {i} of {len(args)}"), scope, ctx, out, rows, True)
            return
        if isinstance(e, exp.Nullif):
            self.walk(e.this, role, branch, scope, ctx, out, rows, const_ok)
            self.walk(e.expression, CONDITION, join_branch(branch, "NULLIF comparison"), scope, ctx, out, rows, False)
            return
        if isinstance(e, exp.Window):
            self.walk(e.this, role, branch, scope, ctx, out, rows, False)
            for part in e.args.get("partition_by") or []:
                self.walk(part, WINDOW, join_branch(branch, "PARTITION BY"), scope, ctx, out, rows, False)
            order = e.args.get("order")
            if order is not None:
                for part in order.expressions:
                    self.walk(part, WINDOW, join_branch(branch, "ORDER BY"), scope, ctx, out, rows, False)
            return
        if isinstance(e, exp.Subquery) or isinstance(e, exp.Select):
            self.scalar_subquery(e.this if isinstance(e, exp.Subquery) else e, role, branch, scope, ctx, out, rows)
            return
        if isinstance(e, (exp.Exists, exp.In)) or isinstance(e, exp.Any) or isinstance(e, exp.All):
            query = e.args.get("query") if isinstance(e, exp.In) else e.this
            if isinstance(e, exp.In):
                self.walk(e.this, role if role != VALUE else CONDITION, branch, scope, ctx, out, rows, False)
                for item in e.expressions or []:
                    self.walk(item, CONDITION if role == VALUE else role, branch, scope, ctx, out, rows, False)
            if query is not None:
                inner = query.this if isinstance(query, exp.Subquery) else query
                self.predicate_subquery(inner, role if role != VALUE else CONDITION, branch, scope, ctx, out)
            return
        if isinstance(e, exp.Dot) and isinstance(e.expression, (exp.Anonymous, exp.Func)):
            fname = f"{e.this.sql(dialect='tsql')}.{e.expression.name}"
            node = Node(UNKNOWN, fname + "(...)")
            ctx.gap("opaque_function", f"User function {fname} is applied; its body is not traced, so other "
                    "inputs it reads are not shown.", None, e)
            for arg in e.expression.expressions or []:
                self.walk(arg, role, branch, scope, ctx, out, rows, False)
            return
        is_arith = isinstance(e, (exp.Add, exp.Sub, exp.Mul, exp.Div, exp.Mod, exp.DPipe, exp.Neg, exp.Concat))
        for child in e.iter_expressions():
            self.walk(child, role, branch, scope, ctx, out, rows, const_ok and is_arith)

    def scalar_subquery(self, query, role, branch, scope: Scope, ctx: Ctx, out, rows) -> None:
        parts = branches(query)
        for select in parts:
            if not isinstance(select, exp.Select):
                continue
            inner = Scope(ctx, local_ctes_from(select, scope), scope)
            inner.add_select_sources(select)
            label = join_branch(branch, "scalar subquery")
            for item in self.projection(select, inner)[:1]:
                if item.expr is not None and not item.unknown:
                    self.walk(item.expr, role, label, inner, ctx, out, rows, True)
            for r in self.row_selection(select, inner, ctx):
                rows.append(RowSelection(r.node, r.role, "scalar subquery: " + r.expression, r.evidence, r.status, r.gap))

    def predicate_subquery(self, query, role, branch, scope: Scope, ctx: Ctx, out) -> None:
        for select in branches(query):
            if not isinstance(select, exp.Select):
                continue
            inner = Scope(ctx, local_ctes_from(select, scope), scope)
            inner.add_select_sources(select)
            sink_rows: list[RowSelection] = []
            for item in self.projection(select, inner):
                if item.expr is not None and not item.unknown:
                    self.walk(item.expr, role, join_branch(branch, "subquery"), inner, ctx, out, sink_rows, False)
            for clause in ("where", "having"):
                node = select.args.get(clause)
                if node is not None:
                    self.walk(node.this, role, join_branch(branch, "subquery " + clause.upper()), inner, ctx, out, sink_rows, False)
            for join in select.args.get("joins") or []:
                if join.args.get("on") is not None:
                    self.walk(join.args["on"], role, join_branch(branch, "subquery JOIN"), inner, ctx, out, sink_rows, False)

    # ========================================================= row selection
    def row_selection(self, select: exp.Expression, scope: Scope, ctx: Ctx) -> list[RowSelection]:
        out: list[RowSelection] = []

        def add(expr, role, prefix=""):
            for clause in conjuncts(expr):
                text = prefix + clause.sql(dialect="tsql")
                kind = role
                if role == JOIN_PREDICATE and isinstance(clause, exp.EQ) and \
                        isinstance(unparen(clause.this), exp.Column) and isinstance(unparen(clause.expression), exp.Column):
                    kind = JOIN_KEY
                inputs: list[Input] = []
                self.walk(clause, CONDITION, None, scope, ctx, inputs, [], False)
                evidence = ctx.evidence(clause, role)
                seen = set()
                for item in inputs:
                    if item.node.kind == CONSTANT or (item.node.id, kind) in seen:
                        continue
                    seen.add((item.node.id, kind))
                    out.append(RowSelection(item.node, kind, text, evidence, item.status, item.gap))

        where = select.args.get("where")
        if where is not None:
            add(where.this, FILTER)
        joins = list(select.args.get("joins") or [])
        from_ = select.args.get("from_")
        if from_ is not None:
            joins.extend(from_.args.get("joins") or [])
        for join in joins:
            side = " ".join(p for p in (join.args.get("side"), join.args.get("kind")) if p)
            prefix = f"{side} JOIN ".lstrip() if side else "JOIN "
            on = join.args.get("on")
            if on is not None:
                add(on, JOIN_PREDICATE, prefix + "ON ")
            for using in join.args.get("using") or []:
                name = norm(ident_text(using))
                for source in scope.sources:
                    if source.columns is None or name in (source.columns or []):
                        for b in self.source_column(source, name, scope, exp.column(name), False):
                            out.append(RowSelection(b.node, JOIN_KEY, f"{prefix}USING ({name})", ctx.evidence(join, JOIN_KEY), b.status, b.gap))
        group = select.args.get("group")
        if group is not None:
            for part in group.expressions:
                add(part, GROUP_BY, "GROUP BY ")
        having = select.args.get("having")
        if having is not None:
            add(having.this, HAVING, "HAVING ")
        return out

    # ===================================================== writer dispatch
    def writer_steps(self, writer: Writer, column: str, target: Node) -> tuple[list[Step], list[Gap], list[list[str]]]:
        """All steps by which ``writer`` sets ``column``; empty when it does not write it."""
        ctx = Ctx(self, writer.unit, writer.statement, writer.controls)
        tree = writer.statement.tree
        try:
            if writer.kind == "insert":
                self._insert(tree, column, target, ctx, writer)
            elif writer.kind == "select_into":
                self._select_into(tree, column, target, ctx)
            elif writer.kind == "view":
                self._view(tree, column, target, ctx)
            elif writer.kind == "update":
                self._update(tree, column, target, ctx, writer)
            elif writer.kind == "merge":
                self._merge(tree, column, target, ctx, writer)
        except RecursionError:
            ctx.gap("depth_limit", "Expression nesting is too deep to analyse.", target)
        return ctx.steps, ctx.gaps, ctx.cycles

    def _query_steps(self, query, names: list[str] | None, position: int | None, column: str, target: Node,
                     ctx: Ctx, writer_kind: str, method: str, ctes: dict, expected: int | None = None) -> None:
        parts = branches(query)
        label = set_op_label(query)
        for i, select in enumerate(parts, 1):
            context = [f"{label} branch {i} of {len(parts)}"] if len(parts) > 1 else []
            if isinstance(select, exp.Values):
                rows = [r for r in select.expressions if isinstance(r, exp.Tuple)]
                for j, row in enumerate(rows, 1):
                    if position is None or position >= len(row.expressions):
                        ctx.gap("column_count_mismatch", "VALUES row does not cover the target column.", target, row)
                        continue
                    row_context = context + ([f"VALUES row {j} of {len(rows)}"] if len(rows) > 1 else [])
                    ctx.steps.append(self.make_step(target, "insert_values", row.expressions[position], Scope(ctx, ctes),
                                                    ctx, method, row_context))
                continue
            if not isinstance(select, exp.Select):
                ctx.gap("unsupported_statement", f"Unsupported query form {type(select).__name__}.", target, select)
                continue
            scope = Scope(ctx, local_ctes_from(select, None, ctes))
            scope.add_select_sources(select)
            items = self.projection(select, scope)
            if position is not None:
                if any(it.unknown for it in items[:position + 1]):
                    ctx.gap("wildcard_without_schema", "A * in the projection cannot be expanded because a source "
                            "has no reliable ordered schema, so the target column's position is unknown.", target, select)
                    continue
                if expected is not None and not any(it.unknown for it in items) and len(items) != expected:
                    ctx.gap("column_count_mismatch", f"The query returns {len(items)} columns for {expected} target "
                            "columns.", target, select)
                    continue
                if position >= len(items):
                    ctx.gap("column_count_mismatch", "The query has fewer columns than the target list.", target, select)
                    continue
                item = items[position]
            else:
                item = self.pick(items, names, column, True) if i == 1 else (
                    items[names.index(column)] if names and column in names and names.index(column) < len(items)
                    and not any(it.unknown for it in items[:names.index(column) + 1]) else None)
                if item is None:
                    if any(it.unknown for it in items):
                        ctx.gap("wildcard_without_schema", "A * in the projection cannot be expanded, so it is unknown "
                                f"whether it supplies {column}.", target, select)
                    continue
            ctx.steps.append(self.item_step(target, writer_kind, item, scope, ctx, method, context, select))

    def _insert(self, tree, column, target, ctx, writer) -> None:
        ctes = statement_ctes(tree, ctx)
        query = tree.expression
        if isinstance(tree.this, exp.Schema) and tree.this.expressions:
            names = [norm(ident_text(c)) for c in tree.this.expressions]
            if column not in names:
                return
            self._query_steps(query, None, names.index(column), column, target, ctx,
                              "insert_values" if isinstance(query, exp.Values) else "insert_select",
                              "target_list", ctes, len(names))
            return
        columns, status, _ = self.catalog.schema_for(writer.ref)
        if status != "declared":
            message = ("INSERT without a column list, and the target has no declared ordered schema in the "
                       "scanned files, so its columns cannot be matched by position.") if status == "missing" else \
                ("INSERT without a column list, and the target has conflicting CREATE TABLE definitions.")
            ctx.gap("positional_without_schema", message, target, tree.this)
            return
        insertable = [c.name for c in columns if not (c.identity or c.computed or c.rowversion)]
        if column not in insertable:
            return
        self._query_steps(query, None, insertable.index(column), column, target, ctx,
                          "insert_values" if isinstance(query, exp.Values) else "insert_select",
                          "positional_schema", ctes, len(insertable))
        for step in ctx.steps:
            if step.target.id == target.id:
                step.status = worst(step.status, INFERRED)
                step.notes.append("Matched by position against the declared column order, skipping identity, "
                                  "computed and rowversion columns.")

    def _select_into(self, tree, column, target, ctx) -> None:
        ctes = statement_ctes(tree, ctx)
        first = branches(tree)[0]
        scope = Scope(ctx, local_ctes_from(first, None, ctes))
        scope.add_select_sources(first)
        names = [i.name for i in self.projection(first, scope)] if isinstance(first, exp.Select) else None
        if names is not None and column not in names and not any(n is None for n in names):
            return
        self._query_steps(tree, names, None, column, target, ctx, "select_into", "projection_alias", ctes)

    def _view(self, tree, column, target, ctx) -> None:
        query = tree.args.get("expression")
        ctes = statement_ctes(query, ctx)
        if isinstance(tree.this, exp.Schema) and tree.this.expressions:
            names = [norm(ident_text(c)) for c in tree.this.expressions]
            if column not in names:
                return
            self._query_steps(query, None, names.index(column), column, target, ctx, "view", "view_column_list", ctes,
                              len(names))
            return
        first = branches(query)[0]
        names = None
        if isinstance(first, exp.Select):
            scope = Scope(ctx, local_ctes_from(first, None, ctes))
            scope.add_select_sources(first)
            names = [i.name for i in self.projection(first, scope)]
            if column not in names and not any(n is None for n in names):
                return
        self._query_steps(query, names, None, column, target, ctx, "view", "projection_alias", ctes)

    def _update(self, tree: exp.Update, column, target, ctx, writer) -> None:
        ctes = statement_ctes(tree, ctx)
        scope = Scope(ctx, ctes)
        scope.add_select_sources(tree)
        target_table = update_target(tree)
        target_aliases = set()
        if isinstance(target_table, exp.Table):
            _, _, _, name = table_parts(target_table)
            target_aliases = {norm(name), norm(target_table.alias) if target_table.alias else None,
                              norm(tree.this.name) if isinstance(tree.this, exp.Table) else None} - {None}
            if not any(s.alias in target_aliases for s in scope.sources):
                scope.add_from(target_table)
        for assignment in tree.expressions:
            if not isinstance(assignment, exp.EQ) or not isinstance(assignment.this, exp.Column):
                continue
            left = assignment.this
            if norm(ident_text(left.this)) != column:
                continue
            if left.table and norm(left.table) not in target_aliases:
                continue
            step = self.make_step(target, "update", assignment.expression, scope, ctx, "set_clause", [])
            step.row_selection.extend(self.row_selection(tree, scope, ctx))
            step.notes.append("UPDATE changes only rows that satisfy its join and filter conditions.")
            ctx.steps.append(step)

    def _merge(self, tree: exp.Merge, column, target, ctx, writer) -> None:
        ctes = statement_ctes(tree, ctx)
        scope = Scope(ctx, ctes)
        target_source = scope.add_from(tree.this)
        scope.add_from(tree.args.get("using"))
        on = tree.args.get("on")
        base_rows = []
        if on is not None:
            for clause in conjuncts(on):
                inputs: list[Input] = []
                self.walk(clause, CONDITION, None, scope, ctx, inputs, [], False)
                role = JOIN_KEY if isinstance(clause, exp.EQ) else JOIN_PREDICATE
                for item in inputs:
                    if item.node.kind != CONSTANT:
                        base_rows.append(RowSelection(item.node, role, "MERGE ON " + clause.sql(dialect="tsql"),
                                                      ctx.evidence(clause, role), item.status, item.gap))
        whens = tree.args.get("whens")
        whens = whens.expressions if isinstance(whens, exp.Whens) else (whens or [])
        target_alias = target_source.alias if target_source else None
        for when in whens:
            matched = when.args.get("matched")
            by_source = when.args.get("source")
            label = "WHEN MATCHED" if matched else ("WHEN NOT MATCHED BY SOURCE" if by_source else "WHEN NOT MATCHED")
            condition = when.args.get("condition")
            if condition is not None:
                label += f" AND {condition.sql(dialect='tsql')}"
            then = when.args.get("then")
            rows = list(base_rows)
            if condition is not None:
                inputs: list[Input] = []
                self.walk(condition, CONDITION, None, scope, ctx, inputs, [], False)
                for item in inputs:
                    if item.node.kind != CONSTANT:
                        rows.append(RowSelection(item.node, MERGE_CONDITION, condition.sql(dialect="tsql"),
                                                 ctx.evidence(condition, MERGE_CONDITION), item.status, item.gap))
            if isinstance(then, exp.Update):
                for assignment in then.expressions:
                    if isinstance(assignment, exp.EQ) and isinstance(assignment.this, exp.Column) and \
                            norm(ident_text(assignment.this.this)) == column and \
                            (not assignment.this.table or norm(assignment.this.table) == target_alias):
                        step = self.make_step(target, "merge_update", assignment.expression, scope, ctx, "set_clause", [label])
                        step.row_selection.extend(rows)
                        ctx.steps.append(step)
            elif isinstance(then, exp.Insert):
                cols = then.this.expressions if isinstance(then.this, (exp.Tuple, exp.Schema)) else []
                names = [norm(ident_text(c.this if isinstance(c, exp.Column) else c)) for c in cols]
                values = then.expression.expressions if isinstance(then.expression, exp.Tuple) else []
                if not names:
                    ctx.gap("positional_without_schema", "MERGE INSERT without a column list is not mapped.", target, then)
                    continue
                if column in names:
                    position = names.index(column)
                    if position >= len(values):
                        ctx.gap("column_count_mismatch", "MERGE INSERT has fewer values than columns.", target, then)
                        continue
                    insert_scope = Scope(ctx, ctes)
                    for source in scope.sources:
                        if source is not target_source:
                            insert_scope.sources.append(source)
                    step = self.make_step(target, "merge_insert", values[position], insert_scope, ctx, "target_list", [label])
                    step.row_selection.extend(r for r in rows if r.node.object is None or
                                              not target_source or r.node.object not in [x for x, _ in target_source.refs])
                    ctx.steps.append(step)

    # ======================================================== variables etc.
    def assignment_steps(self, assignment, target: Node) -> tuple[list[Step], list[Gap]]:
        ctx = Ctx(self, assignment.unit, assignment.statement, assignment.unit.controls.get(assignment.statement.start, ()))
        if assignment.kind == "exec_output":
            gap = ctx.gap("unresolved_variable", f"{target.name} is set by a procedure OUTPUT parameter, which is not traced.",
                          target)
            return [], ctx.gaps
        if assignment.value is None:
            return [], ctx.gaps
        scope = Scope(ctx, {})
        if assignment.select is not None:
            scope = Scope(ctx, local_ctes_from(assignment.select, None, statement_ctes(assignment.statement.tree, ctx)))
            scope.add_select_sources(assignment.select)
        step = self.make_step(target, "variable_assignment", assignment.value, scope, ctx, "assignment", [])
        if assignment.select is not None:
            step.row_selection.extend(self.row_selection(assignment.select, scope, ctx))
            step.notes.append("SELECT @variable = ... keeps the value from the last row read when several rows qualify.")
        ctx.steps.append(step)
        return ctx.steps, ctx.gaps

    def argument_step(self, call, value: exp.Expression, target: Node) -> tuple[list[Step], list[Gap]]:
        ctx = Ctx(self, call.unit, call.statement, call.unit.controls.get(call.statement.start, ()))
        step = self.make_step(target, "parameter_binding", value, Scope(ctx, {}), ctx, "argument", [],
                              procedure=call.statement.procedure)
        ctx.steps.append(step)
        return ctx.steps, ctx.gaps


# ----------------------------------------------------------------- helpers
def join_branch(outer: str | None, inner: str) -> str:
    return f"{outer} / {inner}" if outer else inner


def scope_ctes(scope: Scope) -> dict:
    return scope.ctes


def statement_ctes(tree, ctx: Ctx, inherited: dict | None = None) -> dict:
    """CTEs declared on a statement (or its query), in declaration order."""
    ctes = dict(inherited or {})
    holders = [tree]
    if isinstance(tree, exp.Insert) and tree.expression is not None:
        holders.append(tree.expression)
    for holder in holders:
        with_ = holder.args.get("with_") if isinstance(holder, exp.Expression) else None
        if with_ is None:
            continue
        for cte in with_.expressions:
            name = norm(cte.alias_or_name)
            alias = cte.args.get("alias")
            columns = [norm(ident_text(c)) for c in alias.columns] if isinstance(alias, exp.TableAlias) and alias.columns else None
            visible = dict(ctes)
            local = Local("cte", name, cte.this, columns, visible, None,
                          ObjectRef(name, kind="cte", scope=ctx.stmt_id))
            ctes[name] = local
            # A recursive CTE may reference itself.
            visible[name] = local
    return ctes


def local_ctes(local: Local, select: exp.Expression) -> dict:
    return local_ctes_from(select, None, local.ctes)


def local_ctes_from(select: exp.Expression, scope: Scope | None, inherited: dict | None = None) -> dict:
    ctes = dict(inherited if inherited is not None else (scope.ctes if scope else {}))
    with_ = select.args.get("with_") if isinstance(select, exp.Expression) else None
    if with_ is not None and scope is not None:
        ctes.update(statement_ctes(select, scope.ctx, ctes))
    return ctes


def classify(value: exp.Expression, target_name: str) -> str:
    e = unparen(value)
    if isinstance(e, exp.Column):
        return "direct" if norm(ident_text(e.this)) == target_name else "rename"
    if isinstance(e, (exp.Cast, exp.TryCast, exp.Convert)):
        inner = unparen(e.this if not isinstance(e, exp.Convert) else e.expression)
        return "cast" if isinstance(inner, exp.Column) else "expression"
    if isinstance(e, (exp.Case, exp.If)):
        return "case"
    if isinstance(e, exp.Coalesce):
        return "coalesce"
    if isinstance(e, (exp.DPipe, exp.Concat)):
        return "string"
    if isinstance(e, (exp.Add, exp.Sub, exp.Mul, exp.Div, exp.Mod, exp.Neg)):
        if any(isinstance(n, exp.Literal) and n.is_string for n in e.walk()):
            return "string"
        return "arithmetic"
    if isinstance(e, exp.Window):
        return "window"
    if isinstance(e, exp.AggFunc):
        return "aggregate"
    if isinstance(e, (exp.Literal, exp.Null, exp.Boolean)):
        return "constant"
    if isinstance(e, exp.Parameter):
        return "variable"
    if isinstance(e, (exp.Subquery, exp.Select)):
        return "subquery"
    if isinstance(e, (exp.Func, exp.Dot)):
        return "function"
    return "expression"


def dedupe_inputs(inputs: list[Input]) -> list[Input]:
    seen: dict[tuple, Input] = {}
    for item in inputs:
        key = (item.node.id, item.role, item.branch)
        if key not in seen:
            seen[key] = item
    return list(seen.values())
