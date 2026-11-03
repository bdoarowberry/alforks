"""Turn one writer statement into column mapping steps.

For a requested target column the analyzer finds the expression that produces it
in each writer and branch, binds every column reference in that expression to an
identity, labels each input's role, and records the row-selection clauses.
CTEs and derived tables become statement-local nodes with their own steps.
"""
from __future__ import annotations

import dataclasses

from table_lineage.index import mask_noncode
import re

from dataclasses import dataclass, field

import sqlglot
from sqlglot import exp

from table_lineage.index import Statement

from .catalog import inline_functions, controls_of, restore_variable, Catalog, ColumnInfo, Unit, Writer, ident_text, norm, statement_id, table_parts, update_target, function_summary, split_name
from .model import (
    TOP_ORDER, AMBIGUOUS, COLUMN, CONDITION, CONSTANT, FILTER, GROUP_BY, HAVING, INFERRED, JOIN_KEY, JOIN_PREDICATE,
    LOCAL_COLUMN, MERGE_CONDITION, PARAMETER, PARTIAL, TEMP_COLUMN, UNKNOWN, UNRESOLVED, VALUE, VARIABLE,
    VERIFIED, WINDOW, Evidence, Gap, Input, Node, ObjectRef, RowSelection, Step, worst,
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
    certain: frozenset = frozenset()   # when columns are unknown: names every candidate definition has


@dataclass
class Item:
    name: str | None
    expr: exp.Expression | None
    source: Source | None = None   # set for columns expanded from a wildcard
    column: str | None = None
    unknown: bool = False          # a wildcard that could not be expanded
    certain: frozenset = frozenset()   # unknown wildcard: names it certainly supplies
    picked_by: str | None = None   # how a by-name lookup found it past an unexpanded wildcard


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
        self._positions: dict[int, int] | None = None
        # While analysing row-selection clauses, bind columns without expanding CTE/derived
        # sources: row-selection columns are recorded, not traced (unless --trace-row-selection).
        self.no_expand = False
        self.in_row_selection = False
        self.step_notes: list[str] = []     # notes the expression walk adds to the step being built
        self.step_inferred = False          # the walk relied on a summary (e.g. of a user function)
        self.star_reasons: set[str] = set()  # why a * seen in this statement could not be expanded

    def position(self, node) -> int:
        """Stable ordinal of an AST node within its statement, so reused aliases stay distinct."""
        if self._positions is None:
            tree = self.statement.tree
            self._positions = {id(n): i for i, n in enumerate(tree.walk())} if tree is not None else {}
        return self._positions.get(id(node), -1)

    def local_scope(self, node) -> str:
        return f"{self.stmt_id}@{self.position(node)}"

    def gap(self, kind: str, message: str, node: Node | None = None, expr: exp.Expression | None = None,
            detail: dict | None = None) -> str:
        detail = dict(detail or {})
        if self.in_row_selection:
            detail["raised_in_row_selection"] = True
        if kind == "wildcard_without_schema" and self.star_reasons:
            # Why a * in this statement could not be expanded (anonymous categories, for gap-summary.json).
            detail.setdefault("unexpanded_star", sorted(self.star_reasons))
        gap = Gap(kind, message, node, [self.catalog.evidence(self.unit, self.statement, expr, "gap")], detail)
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
        self.qualified_only = False   # private argument sources of an inlined function: reached by alias only

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
        for join in join_list(select):
            self.add_from(join.this)

    def could_supply(self, name: str) -> bool:
        scope = self
        while scope is not None:
            if not scope.qualified_only and any(s.columns is not None and name in s.columns for s in scope.sources):
                return True
            scope = scope.parent
        return False

    def find_alias(self, qualifier: str) -> list[Source]:
        q = qualifier.lower()
        exact = [s for s in self.sources if s.alias == q]
        if exact:
            return exact
        return [s for s in self.sources if q in s.names]

    # ---------------------------------------------------------------- binding
    def bind(self, column: exp.Column, expand_local: bool = True) -> list[Binding]:
        expand_local = expand_local and not self.ctx.no_expand
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
        outer_sources = False
        scope = self.parent
        while scope is not None:
            outer_sources = outer_sources or (bool(scope.sources) and not scope.qualified_only)
            scope = scope.parent
        if not qualifier and len(self.sources) == 1 and self.sources[0].kind == "object" and not outer_sources \
                and not ctx.in_row_selection:
            # One source and nothing outside the query could supply the name: working SQL binds it there.
            # Its scanned definition lacks the column (an out-of-date copy, a column added at run time or
            # by an ALTER form not recognised), so the binding is a stated rule, not proof.
            only = self.sources[0]
            out = self.ctx.analyzer.source_column(only, name, self, column, expand_local)
            what = only.refs[0][0].display if only.refs else only.alias
            return [Binding(b.node, worst(b.status, INFERRED), b.gap,
                            f"bound to the only source; the scanned definition of {what} does not list {name}")
                    for b in out]
        node = Node(UNKNOWN, column.sql(dialect="tsql", comments=False))
        if qualifier:
            gap = ctx.gap("unresolved_alias", f"Alias or object '{qualifier}' in {column.sql(dialect='tsql', comments=False)} is not "
                          "visible in this query.", node, column)
        else:
            gap = ctx.gap("ambiguous_column", f"Column {name} could not be bound to any source in this query.", node, column)
        return [Binding(node, UNRESOLVED, gap)]

    def _bind_here(self, name, qualifier, column, expand_local) -> list[Binding] | None:
        if self.qualified_only and not qualifier:
            return None
        if qualifier:
            candidates = self.find_alias(qualifier.split(".")[-1]) if "." not in qualifier else \
                [s for s in self.sources if qualifier in s.names or qualifier.split(".")[-1] == s.alias]
            if not candidates:
                return None
            if len(candidates) > 1:
                node = Node(UNKNOWN, column.sql(dialect="tsql", comments=False))
                gap = self.ctx.gap("ambiguous_column", f"Qualifier '{qualifier}' matches several sources.", node, column)
                return [Binding(node, AMBIGUOUS, gap)]
            return self.ctx.analyzer.source_column(candidates[0], name, self, column, expand_local)
        known = [s for s in self.sources if s.columns is not None and name in s.columns]
        unknown = [s for s in self.sources if s.columns is None]
        if len(known) == 1 and not unknown:
            return self.ctx.analyzer.source_column(known[0], name, self, column, expand_local)
        if len(self.sources) == 1:
            only = self.sources[0]
            if only.columns is not None and only.reliable and name not in only.columns:
                return None  # not supplied here: an outer (correlated) reference
            if only.columns is None and name in only.certain:
                # The scanned code proves the inner source has the column, and SQL binds the innermost one.
                out = self.ctx.analyzer.source_column(only, name, self, column, expand_local)
                return [Binding(b.node, worst(b.status, INFERRED), b.gap,
                                "bound to the inner source, which certainly has this column") for b in out]
            if only.columns is None and self.parent is not None and self.parent.could_supply(name):
                # SQL binds to the inner source if it has the column, else the outer one; with no
                # schema for the inner source we cannot tell which.
                inner = self.ctx.analyzer.source_column(only, name, self, column, expand_local)
                outer = self.parent.bind(column, expand_local)
                node = Node(UNKNOWN, column.sql(dialect="tsql", comments=False))
                gap = self.ctx.gap("ambiguous_column", f"Unqualified {name} in a subquery binds to {only.alias} if it "
                                   "has that column (its schema is unknown), otherwise to the outer query.", node, column)
                return [Binding(b.node, AMBIGUOUS, gap, "inner or outer binding") for b in inner + outer]
            return self.ctx.analyzer.source_column(only, name, self, column, expand_local)
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
        node = Node(UNKNOWN, column.sql(dialect="tsql", comments=False))
        candidates = sorted(s.alias for s in (known or unknown))
        gap = self.ctx.gap("ambiguous_column", f"Unqualified column {name} could come from any of: {', '.join(candidates)}.",
                           node, column, {"candidates": candidates})
        return [Binding(node, AMBIGUOUS, gap)]


class Analyzer:
    def __init__(self, catalog: Catalog):
        self.catalog = catalog
        self.expand_row_selection = False   # set by the tracer for --trace-row-selection
        self._object_columns: dict[str, tuple[list[str] | None, bool]] = {}
        self._result_sets: dict[int, tuple] = {}
        self._inlined: dict[int, object] = {}   # id(query) -> InlineFunction it was read from
        self._inline_why: str | None = None     # why the last inline_tvf call did not inline
        self._read_names: dict[str, set[str]] | None = None
        self._local_memo: dict[tuple, tuple] = {}   # (what, id(query)) -> (query, result); query kept alive
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
        if isinstance(node, exp.Table) and node.args.get("pivots"):
            pivot = node.args["pivots"][0]
            what = "UNPIVOT" if pivot.args.get("unpivot") else "PIVOT"
            n = Node(UNKNOWN, node.sql(dialect="tsql", comments=False)[:200])
            gap = ctx.gap("unsupported_statement", f"{what} over {table_parts(node)[3]}: its output columns are not "
                          "traced.", n, node, {"reason": "pivot_or_unpivot_not_traced"})
            return Source(alias or (pivot.alias or "pivot").lower(), "opaque", gap=gap,
                          names=tuple({norm(alias or ""), norm(pivot.alias or "")} - {""}))
        if isinstance(node, exp.Table):
            server, database, schema, name = table_parts(node)
            if isinstance(node.this, (exp.Func, exp.Anonymous)) or (
                    isinstance(node.this, exp.Dot) and isinstance(node.this.expression, (exp.Func, exp.Anonymous))):
                func = node.this if not isinstance(node.this, exp.Dot) else node.this.expression
                if isinstance(node.this, exp.Dot) and not schema:
                    # schema.fn(...) parsed as Dot(schema, fn): the qualifier is the schema (or database.schema)
                    qualifier = node.this.this
                    if isinstance(qualifier, exp.Identifier):
                        schema = qualifier.name
                    elif isinstance(qualifier, exp.Dot) and isinstance(qualifier.expression, exp.Identifier):
                        schema = qualifier.expression.name
                        if isinstance(qualifier.this, exp.Identifier) and not database:
                            database = qualifier.this.name
                fname = (func.name or func.sql(dialect="tsql", comments=False)).lower()
                kind = "external_object" if fname in EXTERNAL_FUNCTIONS else "table_valued_function"
                why = None
                if kind == "table_valued_function":
                    self._inline_why = None
                    inlined = self.inline_tvf(node, func, schema, scope, alias or fname, alias_columns)
                    if inlined is not None:
                        return inlined
                    why = self._inline_why
                n = Node(UNKNOWN, node.sql(dialect="tsql", comments=False))
                gap = ctx.gap(kind, f"Rows come from {fname.upper()}(...); its columns are not traced.", n, node)
                return Source(alias or fname, "opaque", gap=gap, names=(fname,), note=why)
            lname = norm(name) or ""
            if not database and not schema and lname in scope_ctes(scope):
                local = scope_ctes(scope)[lname]
                columns = self.local_names(local, ctx)
                return Source(alias or lname, "local", columns=columns, reliable=True, local=local, names=(lname,),
                              certain=self.local_certain(local, ctx) if columns is None else frozenset())
            refs = self.resolve_table(node, ctx)
            ref = refs[0][0]
            columns, reliable = self.object_columns(ref) if len(refs) == 1 else (None, False)
            certain = self.object_certain(ref) if len(refs) == 1 and (columns is None or not reliable) \
                else frozenset()
            names = {lname}
            if schema:
                names.add(f"{norm(schema)}.{lname}")
            if database and schema:
                names.add(f"{norm(database)}.{norm(schema)}.{lname}")
            return Source(alias or lname, "object", refs, columns, reliable, names=tuple(sorted(names)),
                          certain=certain)
        if isinstance(node, exp.Lateral) and (isinstance(node.this, (exp.Func, exp.Anonymous)) or (
                isinstance(node.this, exp.Dot) and isinstance(node.this.expression, (exp.Func, exp.Anonymous)))):
            # CROSS APPLY fn(args) AS x without a subquery: the same as FROM fn(args) AS x.
            table = exp.Table(this=node.this, alias=node.args.get("alias"))
            return self.source_for(table, scope)
        if isinstance(node, exp.Lateral) and isinstance(node.this, exp.Subquery) and \
                isinstance(node.this.this, exp.Values):
            alias_node = node.args.get("alias") or node.this.args.get("alias")
            name = norm(alias_node.name) if isinstance(alias_node, exp.TableAlias) and alias_node.name else \
                f"values{len(scope.sources) + 1}"
            cols = [norm(ident_text(c)) for c in alias_node.columns] if isinstance(alias_node, exp.TableAlias) \
                and alias_node.columns else None
            local = Local("values", name, node.this.this, cols, {}, scope,
                          ObjectRef(name, kind="derived", scope=ctx.local_scope(node)))
            return Source(name, "local", columns=cols, reliable=bool(cols), local=local, names=(name,))
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
                          ObjectRef(name, kind="derived", scope=ctx.local_scope(node)))
            columns = self.local_names(local, ctx)
            return Source(name, "local", columns=columns, reliable=True, local=local, names=(name,),
                          certain=self.local_certain(local, ctx) if columns is None else frozenset())
        if isinstance(node, exp.Values):
            name = alias or f"values{len(scope.sources) + 1}"
            local = Local("values", name, node, alias_columns, {}, None, ObjectRef(name, kind="derived", scope=ctx.local_scope(node)))
            return Source(name, "local", columns=alias_columns, reliable=bool(alias_columns), local=local, names=(name,))
        n = Node(UNKNOWN, node.sql(dialect="tsql", comments=False) if isinstance(node, exp.Expression) else str(node))
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
                added = [c.name for sch in self.catalog.schemas.get(key, []) if sch.source == "alter_add" for c in sch.columns]
                if definers and None not in name_sets and len(set(name_sets)) == 1:
                    result = (list(name_sets[0]) + [c for c in added if c not in name_sets[0]], True)
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
        memo = self._local_memo.get(("names", id(local.query)))
        if memo is not None and memo[0] is local.query:
            return memo[1]
        out = self._local_names(local, ctx)
        self._local_memo[("names", id(local.query))] = (local.query, out)
        return out

    def _local_names(self, local: Local, ctx: Ctx) -> list[str] | None:
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

    def into_order(self, ref: ObjectRef):
        """(ordered names, defining writer) of a table created only by SELECT ... INTO with one settled
        column order, when no identity, computed or rowversion column can have been copied into it."""
        definers = [w for w in self.catalog.writers_for(ref) if w.kind == "select_into"]
        if not definers or any(w.kind == "view" for w in self.catalog.writers_for(ref)):
            return None
        columns, reliable = self.object_columns(ref)
        if columns is None or not reliable:
            return None
        if any(c.identity or c.computed or c.rowversion for sch in self.catalog.schemas.get(ref.key, [])
               if sch.source == "alter_add" for c in sch.columns):
            return None   # ALTER TABLE ... ADD id int IDENTITY after the copy
        if not all(self._copies_plain_columns(w, set()) for w in definers):
            return None
        return list(columns), definers[0]

    def _copies_plain_columns(self, writer: Writer, seen: set) -> bool:
        """Whether a SELECT ... INTO certainly creates no identity, computed or rowversion column: every
        source it reads has a known schema without such columns (or is itself such a plain copy)."""
        tree = writer.statement.tree
        first = branches(tree)[0]
        if not isinstance(first, exp.Select):
            return False
        if any((f.name or "").upper() == "IDENTITY" for e in first.expressions
               for f in e.find_all(exp.Anonymous, exp.Func)):
            return False   # SELECT IDENTITY(int, 1, 1) AS id INTO ... creates an identity column
        ctx = Ctx(self, writer.unit, writer.statement)
        scope = Scope(ctx, statement_ctes(tree, ctx))
        scope.add_select_sources(first)
        for source in scope.sources:
            if source.kind != "object" or len(source.refs) != 1:
                # A CTE or derived table can pass an identity column through: not settled.
                return False
            ref = source.refs[0][0]
            columns, status, _ = self.catalog.schema_for(ref)
            if status == "declared":
                if any(c.identity or c.computed or c.rowversion for c in columns):
                    return False
                continue
            inner = [w for w in self.catalog.writers_for(ref) if w.kind == "select_into"]
            if not inner or ref.key in seen:
                return False
            seen.add(ref.key)
            if not all(self._copies_plain_columns(w, seen) for w in inner):
                return False
        return True

    def written_names(self, ref: ObjectRef) -> set[str]:
        """Column names that writers of ``ref`` name explicitly: INSERT column lists, UPDATE SET targets,
        MERGE insert lists and SET targets (not through views or CTEs)."""
        key = ("written", ref.key)
        if key in self._object_columns:
            return self._object_columns[key]
        names: set[str] = set()
        if ref.kind == "temp_table" and len(self.catalog.temp_resets.get((ref.scope, ref.name), [])) > 1:
            # Dropped and re-created in the procedure: a write or read proves a column of one
            # incarnation only, and which one a * reads is not tracked here.
            self._object_columns[key] = names
            return names
        for writer in self.catalog.writers_for(ref):
            tree = writer.statement.tree
            if writer.via or tree is None:
                continue
            nodes = []
            if writer.kind == "insert" and isinstance(tree, exp.Insert):
                nodes = [tree]
            elif writer.kind == "update" and isinstance(tree, exp.Update):
                nodes = [tree]
            elif writer.kind == "merge":
                nodes = [w.args.get("then") for w in tree.find_all(exp.When)]
            for node in nodes:
                if isinstance(node, exp.Insert) and isinstance(node.this, exp.Schema):
                    names |= {norm(ident_text(c)) for c in node.this.expressions}
                elif isinstance(node, exp.Insert) and isinstance(node.this, exp.Tuple):
                    names |= {norm(ident_text(c.this if isinstance(c, exp.Column) else c)) for c in node.this.expressions}
                elif isinstance(node, exp.Update):
                    target = node.this if isinstance(node.this, exp.Table) else (
                        tree.this if writer.kind == "merge" and isinstance(tree.this, exp.Table) else None)
                    allowed = {norm(target.alias_or_name), norm(target.name)} if target is not None else set()
                    for e in node.expressions:
                        if not (isinstance(e, exp.EQ) and isinstance(e.this, exp.Column)):
                            continue
                        qualifier = norm(e.this.table) if e.this.table else None
                        # SET loc.Lat = ... on a UDT column is a property, not a column named Lat.
                        if qualifier is None or qualifier in allowed:
                            names.add(norm(ident_text(e.this.this)))
        names |= self.read_names().get(ref.key, set())
        names |= self.catalog.proven_columns.get(ref.key, set())
        names -= self.catalog.dropped_columns.get(ref.key, set())   # ALTER TABLE ... DROP COLUMN wins
        names.discard(None)
        names.discard("")
        self._object_columns[key] = names
        return names

    def read_names(self) -> dict[str, set[str]]:
        """Object key -> column names that some parsed statement qualifies with an alias of that object
        (``s.col_a`` with ``FROM dbo.T AS s``): working SQL proves the column exists. Aliases a statement
        also uses for a CTE, derived table or another object are not used."""
        if self._read_names is not None:
            return self._read_names
        found: dict[str, set[str]] = {}
        for unit in self.catalog.units:
            for statement in unit.index.statements:
                tree = statement.tree
                if tree is None or isinstance(tree, exp.Command):
                    continue
                try:
                    ctes = {norm(c.alias) for c in tree.find_all(exp.CTE)}
                    other = {norm(a.name) for a in tree.find_all(exp.TableAlias) if not isinstance(a.parent, exp.Table)}
                    aliases: dict[str, set[str]] = {}
                    refs: dict[str, ObjectRef] = {}
                    for table in tree.find_all(exp.Table):
                        if not isinstance(table.this, exp.Identifier) or norm(table.name) in ctes \
                                or table.name.startswith("@"):
                            # A CTE, table variable, function or rowset: its alias proves nothing here,
                            # and must not prove anything for a real table that reuses it.
                            other.add(norm(table.alias_or_name))
                            continue
                        ref = self.catalog.ref_from_table(table, unit, statement)
                        if ref.kind == "temp_table" and len(self.catalog.temp_resets.get((ref.scope, ref.name), [])) > 1:
                            other.add(norm(table.alias_or_name))
                            continue
                        refs[ref.key] = ref
                        for name in {norm(table.alias_or_name), norm(table.name)}:
                            aliases.setdefault(name, set()).add(ref.key)
                    for column in tree.find_all(exp.Column):
                        qualifier = norm(column.table) if column.table else None
                        if not qualifier or column.args.get("db") or qualifier in other or qualifier in ctes:
                            continue
                        keys = aliases.get(qualifier, set())
                        if len(keys) == 1 and isinstance(column.this, exp.Identifier):
                            found.setdefault(next(iter(keys)), set()).add(norm(column.name))
                except Exception:  # noqa: BLE001 - a proof source only; never fail the run on it
                    continue
        self._read_names = found
        return found

    def star_reason(self, source: Source, depth: int = 0) -> str:
        """Why a * over ``source`` could not be expanded, as an anonymous category (one level deeper for
        a SELECT ... INTO copy or a view)."""
        if source.kind == "opaque":
            return "function_or_external_source" + (f"/{source.note}" if source.note else "")
        if source.kind == "local":
            return "cte_or_derived_with_unexpanded_star"
        if len(source.refs) != 1:
            return "object_resolves_to_several_databases"
        ref = source.refs[0][0]
        _, status, _ = self.catalog.schema_for(ref)
        if status == "ambiguous":
            return "differing_create_table_definitions"
        if status == "declared":
            return "declared_but_order_unsettled"
        definers = [w.kind for w in self.catalog.writers_for(ref) if w.kind in ("select_into", "view")]
        if definers:
            if len(definers) > 1:
                return "several_select_into_or_view_definitions_differ"
            label = "select_into_copy_of_an_unexpandable_source" if definers[0] == "select_into" else \
                "view_over_an_unexpandable_source"
            if depth < 3:
                writer = next(w for w in self.catalog.writers_for(ref) if w.kind in ("select_into", "view"))
                query = writer.statement.tree.args.get("expression") if writer.kind == "view" else writer.statement.tree
                first = branches(query)[0] if query is not None else None
                if isinstance(first, exp.Select):
                    inner_ctx = Ctx(self, writer.unit, writer.statement)
                    inner = Scope(inner_ctx, statement_ctes(writer.statement.tree, inner_ctx))
                    inner.add_select_sources(first)
                    unknown = [x for x in inner.sources if x.columns is None or not x.reliable]
                    if unknown:
                        label += "/" + self.star_reason(unknown[0], depth + 1)
            return label
        if ref.kind == "temp_table":
            return "temp_table_created_elsewhere_or_by_unparsed_statement"
        return "no_definition_in_selected_sources" if not self.catalog.is_known(ref) else \
            "written_but_no_definition_in_selected_sources"

    @staticmethod
    def _certain_of(items: list[Item]) -> frozenset:
        """Output names a projection certainly has: named items and what its * items certainly supply."""
        out = set()
        for item in items:
            if item.unknown:
                out |= item.certain
            elif item.name:
                out.add(item.name)
        return frozenset(out)

    def local_certain(self, local: Local, ctx: Ctx) -> frozenset:
        memo = self._local_memo.get(("certain", id(local.query)))
        if memo is not None and memo[0] is local.query:
            return memo[1]
        out = self._local_certain(local, ctx)
        self._local_memo[("certain", id(local.query))] = (local.query, out)
        return out

    def _local_certain(self, local: Local, ctx: Ctx) -> frozenset:
        if local.column_aliases or local.kind == "values":
            return frozenset(local.column_aliases or ())
        first = branches(local.query)[0]
        if not isinstance(first, exp.Select):
            return frozenset()
        scope = Scope(ctx, local_ctes(local, first), local.parent)
        scope.add_select_sources(first)
        return self._certain_of(self.projection(first, scope))

    def object_certain(self, ref: ObjectRef) -> frozenset:
        """Column names every candidate definition of an object has (when its ordered list is not settled)."""
        key = ("certain", ref.key)
        if key in self._object_columns:
            return self._object_columns[key]
        if key in self._object_columns_active:
            return frozenset()
        self._object_columns_active.add(key)
        try:
            sets = [frozenset(n) for n in self.catalog.definition_name_sets(ref)]
            for writer in self.catalog.writers_for(ref):
                if writer.kind not in ("view", "select_into"):
                    continue
                if writer.kind == "view" and isinstance(writer.statement.tree.this, exp.Schema) \
                        and writer.statement.tree.this.expressions:
                    sets.append(frozenset(norm(ident_text(c)) for c in writer.statement.tree.this.expressions))
                    continue
                query = writer.statement.tree.args.get("expression") if writer.kind == "view" else writer.statement.tree
                first = branches(query)[0] if query is not None else None
                if not isinstance(first, exp.Select):
                    sets.append(frozenset())
                    continue
                ctx = Ctx(self, writer.unit, writer.statement)
                scope = Scope(ctx, statement_ctes(writer.statement.tree, ctx))
                scope.add_select_sources(first)
                sets.append(self._certain_of(self.projection(first, scope)))
            added = {c.name for sch in self.catalog.schemas.get(ref.key, []) if sch.source == "alter_add"
                     for c in sch.columns}
            # A statement in the scanned files that names a column of the object in its INSERT column list,
            # UPDATE SET or MERGE proves the column exists (not where it sits).
            added |= self.written_names(ref)
            result = (frozenset.intersection(*sets) | added) if sets else frozenset(added)
            result -= self.catalog.dropped_columns.get(ref.key, set())
        finally:
            self._object_columns_active.discard(key)
        self._object_columns[key] = result
        return result

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
                        scope.ctx.star_reasons.add(self.star_reason(source))
                        items.append(Item(None, target, source, None, unknown=True, certain=source.certain))
                        continue
                    for column in source.columns:
                        items.append(Item(column, target, source, column))
                if not chosen:
                    scope.ctx.star_reasons.add("qualifier_not_found")
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
            node = Node(UNKNOWN, column.sql(dialect="tsql", comments=False))
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
            if ref.database_source in ("default", "unified") and ref.kind in ("table", "view") and status == INFERRED:
                note = (f"database {ref.database} assumed: the SQL names none, and it is the run's "
                        + ("default database" if ref.database_source == "default" else "only database known for this name"))
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
                    step = self.make_step(node, "derived", value, Scope(ctx, {}, local.parent), ctx, "column_alias_list",
                                          [f"VALUES row {i} of {len(rows)}"] if len(rows) > 1 else [])
                    ctx.steps.append(step)
            else:
                parts = branches(local.query)
                names = self.local_names(local, ctx)
                label = set_op_label(local.query)
                first_new = len(ctx.steps)
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
                        else:
                            # Later branches match by position, which a * of unknown width hides.
                            ctx.gap("wildcard_without_schema", f"Branch {i} of {local.kind} {local.name}: the position "
                                    f"of {name} is unknown because a * cannot be expanded.", node, select)
                        continue
                    context = [f"{label} branch {i} of {len(parts)}"] if len(parts) > 1 else []
                    method = "column_alias_list" if local.column_aliases else "projection_alias"
                    ctx.steps.append(self.item_step(node, local.kind, item, scope, ctx, method, context, select))
                definition = self._inlined.get(id(local.query))
                if definition is not None:
                    for step in ctx.steps[first_new:]:
                        if step.target.id != node.id:
                            continue
                        step.status = worst(step.status, INFERRED)
                        step.notes.append(f"Read from the inline table-valued function {definition.schema}."
                                          f"{definition.name} (its RETURN query, with the call's arguments in "
                                          "place of its parameters).")
                        step.evidence.append(Evidence(definition.unit.rel_path, definition.line, definition.line,
                                                      f"inline function {definition.schema}.{definition.name}",
                                                      "function_definition", None, definition.unit.root_label))
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
            stars = [it for it in items if it.unknown and name in it.certain]
            if len(matches) == 1 and not any(it.unknown for it in items):
                return matches[0]
            # A by-name lookup is only made where output names must be unique (CTE, derived table,
            # SELECT ... INTO, view), so an unexpanded * cannot also supply a name found elsewhere.
            if len(matches) + len(stars) == 1:
                if matches:
                    return Item(matches[0].name, matches[0].expr, matches[0].source, matches[0].column,
                                picked_by="unique_name")
                star = stars[0]
                return Item(name, star.expr, star.source, name, picked_by="certain_name")
        return None

    # ================================================================= steps
    def item_step(self, target: Node, writer_kind: str, item: Item, scope: Scope, ctx: Ctx, method: str,
                  context: list[str], select: exp.Expression | None) -> Step:
        if item.source is not None and item.column is not None:
            column = exp.column(item.column, table=item.source.alias)
            step = self.make_step(target, writer_kind, column, scope, ctx, "wildcard_schema", context)
            star = item.expr.sql(dialect="tsql", comments=False)
            # Cite the * as written; the expanded column is not in the source text.
            step.expression = f"{star} (expands to {item.source.alias}.{item.column})"
            step.evidence = [self.catalog.evidence(ctx.unit, ctx.statement, item.expr, "wildcard",
                                                   excerpt=f"{star}  [{item.source.alias}.{item.column} by position]")]
            step.evidence.extend(self.schema_evidence(item.source))
            step.status = worst(step.status, INFERRED)
            if item.picked_by == "certain_name":
                step.evidence[0] = self.catalog.evidence(ctx.unit, ctx.statement, item.expr, "wildcard",
                                                         excerpt=f"{star}  [{item.source.alias}.{item.column} by name]")
                step.notes.append(f"{star} could not be expanded in full (the definitions of {item.source.alias} "
                                  f"differ or are incomplete), but every candidate definition has {item.column}, and "
                                  "output names are unique here, so it supplies this column.")
            else:
                step.notes.append(f"{star} was expanded using the ordered column list of {item.source.alias} "
                                  "(see the source_schema evidence); the column is not named in this statement.")
        else:
            step = self.make_step(target, writer_kind, item.expr, scope, ctx, method, context)
            if item.picked_by == "unique_name":
                step.status = worst(step.status, INFERRED)
                step.notes.append("Found by name although a * in the same projection could not be expanded; "
                                  "output names are unique here, so the * cannot also supply it.")
        if select is not None:
            step.row_selection.extend(self.row_selection(select, scope, ctx))
        return step

    def schema_evidence(self, source: Source) -> list:
        """Where the ordered column list used to expand a * comes from."""
        out = []
        for ref, _ in source.refs[:1]:
            _, status, schema = self.catalog.schema_for(ref)
            if status == "declared" and schema is not None:
                out.append(self.catalog.evidence(schema.unit, schema.statement, role="source_schema",
                                                 excerpt=f"column list of {ref.display}"))
                continue
            for writer in self.catalog.writers_for(ref):
                if writer.kind in ("select_into", "view"):
                    out.append(self.catalog.evidence(writer.unit, writer.statement, role="source_schema",
                                                     excerpt=f"output columns of {ref.display} ({writer.kind})"))
                    break
        return out

    def make_step(self, target: Node, writer_kind: str, value: exp.Expression, scope: Scope, ctx: Ctx,
                  method: str, context: list[str], procedure: str | None = None) -> Step:
        inputs: list[Input] = []
        extra_rows: list[RowSelection] = []
        gaps_before = {g.id for g in ctx.gaps}
        notes_before, ctx.step_inferred = len(ctx.step_notes), False
        self.walk(value, VALUE, None, scope, ctx, inputs, extra_rows, True)
        notes, inferred = list(dict.fromkeys(ctx.step_notes[notes_before:])), ctx.step_inferred
        inputs = dedupe_inputs(inputs)
        if not inputs and not any(isinstance(n, (exp.Column, exp.Subquery, exp.Select)) for n in value.walk()):
            # CAST(NULL AS int), CONVERT(date, '19000101', 112), GETDATE(), ERROR_MESSAGE(): no column is read;
            # the expression itself is the end (a constant or a value the server supplies at run time).
            text = value.sql(dialect="tsql", comments=False)
            inputs = [Input(Node(CONSTANT, text), VALUE, text, None, VERIFIED, None, "no column is read")]
        operation = classify(value, target.name)
        step = Step(target=target, writer_kind=writer_kind, expression=value.sql(dialect="tsql", comments=False),
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
        if inferred:
            step.status = worst(step.status, INFERRED)
        step.notes.extend(notes)
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
                out.append(Input(b.node, role, e.sql(dialect="tsql", comments=False), branch, b.status, b.gap, b.note))
            return
        if isinstance(e, exp.Neg) and isinstance(unparen(e.this), exp.Literal):
            if role == VALUE and const_ok:
                out.append(Input(Node(CONSTANT, e.sql(dialect="tsql", comments=False)), VALUE, e.sql(dialect="tsql", comments=False), branch))
            return
        if isinstance(e, (exp.Literal, exp.Null, exp.Boolean)):
            if role == VALUE and const_ok:
                out.append(Input(Node(CONSTANT, e.sql(dialect="tsql", comments=False)), VALUE, e.sql(dialect="tsql", comments=False), branch))
            return
        if isinstance(e, exp.Var) and e.name.upper() == "DEFAULT" and role == VALUE and const_ok:
            out.append(Input(Node(CONSTANT, "DEFAULT"), VALUE, "DEFAULT", branch, VERIFIED, None,
                             "the column's default (see the column definition)"))
            return
        if isinstance(e, exp.DataType) or isinstance(e, exp.Var):
            return
        if isinstance(e, exp.NextValueFor):
            # A sequence: a system-generated value, not a column of any source.
            name = e.this.sql(dialect="tsql", comments=False)
            if role == VALUE:
                out.append(Input(Node(CONSTANT, f"NEXT VALUE FOR {name}"), VALUE, e.sql(dialect="tsql", comments=False),
                                 branch, VERIFIED, None, "generated by a sequence"))
            return
        if isinstance(e, exp.Anonymous) and str(e.this).upper() == "IDENTITY":
            # SELECT IDENTITY(type, seed, increment) AS c INTO ...: generated, not read from a source.
            if role == VALUE:
                out.append(Input(Node(CONSTANT, "IDENTITY"), VALUE, e.sql(dialect="tsql", comments=False), branch,
                                 VERIFIED, None, "generated identity value"))
            return
        if isinstance(e, exp.Anonymous) and str(e.this).upper() == "CHOOSE" and e.expressions:
            index, *choices = e.expressions
            label = index.sql(dialect="tsql", comments=False)
            self.walk(index, CONDITION, join_branch(branch, f"CHOOSE {label}"), scope, ctx, out, rows, False)
            for i, choice in enumerate(choices, 1):
                self.walk(choice, role, join_branch(branch, f"CHOOSE {label} = {i}"), scope, ctx, out, rows, True)
            if role == VALUE:
                out.append(Input(Node(CONSTANT, "NULL"), VALUE, "NULL", join_branch(branch, f"CHOOSE {label} out of range"),
                                 VERIFIED, None, "CHOOSE returns NULL when the index is out of range"))
            return
        if isinstance(e, (exp.Lag, exp.Lead)):
            self.walk(e.this, role, branch, scope, ctx, out, rows, const_ok)
            offset = e.args.get("offset")
            if offset is not None:
                self.walk(offset, WINDOW, join_branch(branch, "offset"), scope, ctx, out, rows, False)
            default = e.args.get("default")
            fallback = join_branch(branch, f"{type(e).__name__.upper()} default (no row at the offset)")
            if default is not None:
                self.walk(default, role, fallback, scope, ctx, out, rows, True)
            elif role == VALUE:
                out.append(Input(Node(CONSTANT, "NULL"), VALUE, "NULL", fallback, VERIFIED, None,
                                 "implicit default NULL: no row at the offset"))
            return
        if isinstance(e, exp.Parameter):
            name = restore_variable(e.sql(dialect="tsql", comments=False)).lower()
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
                cond = case_if.this.sql(dialect="tsql", comments=False)
                when = f"WHEN {e.this.sql(dialect='tsql', comments=False)} = {cond}" if e.this is not None else f"WHEN {cond}"
                self.walk(case_if.this, CONDITION, join_branch(branch, when), scope, ctx, out, rows, False)
                self.walk(case_if.args.get("true"), role, join_branch(branch, when + " THEN"), scope, ctx, out, rows, True)
            default = e.args.get("default")
            if default is not None:
                self.walk(default, role, join_branch(branch, "ELSE"), scope, ctx, out, rows, True)
            elif role == VALUE:
                out.append(Input(Node(CONSTANT, "NULL"), VALUE, "NULL", join_branch(branch, "ELSE"), VERIFIED, None,
                                 "implicit ELSE NULL: the CASE has no ELSE"))
            return
        if isinstance(e, exp.If):
            cond = e.this.sql(dialect="tsql", comments=False)
            self.walk(e.this, CONDITION, join_branch(branch, f"IIF {cond}"), scope, ctx, out, rows, False)
            self.walk(e.args.get("true"), role, join_branch(branch, f"IIF {cond} THEN"), scope, ctx, out, rows, True)
            self.walk(e.args.get("false"), role, join_branch(branch, f"IIF {cond} ELSE"), scope, ctx, out, rows, True)
            return
        if isinstance(e, exp.Coalesce):
            label = "ISNULL" if e.args.get("is_null") else "COALESCE"
            args = [e.this] + list(e.expressions or [])
            for i, arg in enumerate(args, 1):
                self.walk(arg, role, join_branch(branch, f"{label} argument {i}"), scope, ctx, out, rows, True)
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
            fname = f"{e.this.sql(dialect='tsql', comments=False)}.{e.expression.name}"
            args = list(e.expression.expressions or [])
            qualifier = split_name(e.this.sql(dialect="tsql", comments=False))
            schema = qualifier[-1]
            database = qualifier[-2] if len(qualifier) > 1 and qualifier[-2] else None
            summary = function_summary(self.catalog, schema, e.expression.name, database=database)
            if isinstance(summary, str):
                ctx.gap("opaque_function", f"User function {fname} is applied; its body is not traced ({summary}), "
                        "so other inputs it reads are not shown.", None, e)
                for arg in args:
                    self.walk(arg, role, branch, scope, ctx, out, rows, False)
                return
            # A scalar function that reads no tables: its result depends only on the arguments its body uses.
            used = [p for p, u in zip(summary.params, summary.used) if u]
            where = f"{summary.unit.root_label}/{summary.unit.rel_path}:{summary.line}"
            note = (f"{fname} is summarised from its definition at {where}: it reads no tables, and its result "
                    f"depends on {', '.join(used) or 'none of its parameters'}"
                    + (" and on the current date/time or a random value" if summary.time_dependent else "")
                    + "; the function body is not traced expression by expression.")
            ctx.step_notes.append(note)
            ctx.step_inferred = True
            for i, arg in enumerate(args):
                if isinstance(arg, (exp.Column, exp.Var)) and arg.name.upper() == "DEFAULT":
                    continue   # the parameter's default: a constant
                if i >= len(summary.params) or summary.used[i]:
                    only_condition = bool(summary.condition_only and i < len(summary.condition_only)
                                          and summary.condition_only[i])
                    self.walk(arg, CONDITION if only_condition and role == VALUE else role,
                              join_branch(branch, f"{fname} argument {i + 1}"), scope, ctx, out, rows, False)
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
            for join in join_list(select):
                if join.args.get("on") is not None:
                    self.walk(join.args["on"], role, join_branch(branch, "subquery JOIN"), inner, ctx, out, sink_rows, False)

    # ========================================================= row selection
    def row_selection(self, select: exp.Expression, scope: Scope, ctx: Ctx) -> list[RowSelection]:
        out: list[RowSelection] = []

        def add(expr, role, prefix=""):
            for clause in conjuncts(expr):
                text = prefix + clause.sql(dialect="tsql", comments=False)
                kind = role
                if role == JOIN_PREDICATE and isinstance(clause, exp.EQ) and \
                        isinstance(unparen(clause.this), exp.Column) and isinstance(unparen(clause.expression), exp.Column):
                    kind = JOIN_KEY
                inputs: list[Input] = []
                nested: list[RowSelection] = []
                with row_selection_mode(ctx):
                    self.walk(clause, CONDITION, None, scope, ctx, inputs, nested, False)
                out.extend(RowSelection(r.node, kind if role != JOIN_PREDICATE else JOIN_PREDICATE, text + " :: " + r.expression,
                                        r.evidence, r.status, r.gap) for r in nested)
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
        for join in join_list(select):
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
        limit = select.args.get("limit")
        order = select.args.get("order")
        if limit is not None and order is not None:
            # TOP n ... ORDER BY decides which rows survive.
            top = limit.expression.sql(dialect="tsql", comments=False) if isinstance(limit, exp.Limit) and limit.expression is not None else "n"
            for part in order.expressions:
                add(part.this if isinstance(part, exp.Ordered) else part, TOP_ORDER, f"TOP {top} ORDER BY ")
        return out

    # ===================================================== writer dispatch
    def writer_steps(self, writer: Writer, column: str, target: Node) -> tuple[list[Step], list[Gap], list[list[str]]]:
        """All steps by which ``writer`` sets ``column``; empty when it does not write it."""
        ctx = Ctx(self, writer.unit, writer.statement, writer.controls)
        tree = writer.statement.tree
        if writer.via:
            through = self.through_column(writer, column, ctx)
            if through is AMBIGUOUS_THROUGH:
                ctx.gap("ambiguous_object", f"This statement writes through view {writer.via[1]}, which has several "
                        "definitions in the scanned files; which base column it sets cannot be determined.", target)
                return [], ctx.gaps, ctx.cycles
            if through is None:
                return [], ctx.gaps, ctx.cycles
            column = through
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
            elif writer.kind == "insert_exec":
                return self._insert_exec(writer, column, target, ctx)
        except RecursionError:
            ctx.gap("depth_limit", "Expression nesting is too deep to analyse.", target)
        if (f"{writer.unit.root_label}/{writer.unit.rel_path}", writer.statement.start) in self.catalog.top_limited:
            for step in ctx.steps:
                step.notes.append("This statement has TOP (n): it writes only some of the qualifying rows, and "
                                  "which ones is not determined.")
        return ctx.steps, ctx.gaps, ctx.cycles

    # ------------------------------------------------------------ inline table-valued functions
    def inline_tvf(self, node, func, schema, scope: Scope, name: str, alias_columns):
        """A call of an inline table-valued function, read as a derived table: its RETURN query with the
        call's arguments in place of the parameters. Only for one definition in the caller's database,
        positional arguments, and arguments whose columns are qualified by a caller alias. The body is
        isolated from the caller: its own names never see the caller's sources, and an argument's
        columns are bound to the caller's sources under private aliases the body cannot capture."""
        ctx = scope.ctx
        fname = norm(func.name if not isinstance(func, exp.Anonymous) else str(func.this))
        _, database, _, _ = table_parts(node)
        current = norm(ctx.unit.database_at(ctx.statement.start, self.catalog.default_database)[0])
        if database and norm(database) != current:
            self._inline_why = "called_in_another_database"
            return None   # the body would be resolved in another database
        every = inline_functions(self.catalog).get((norm(schema) or self.catalog.default_schema, fname), [])
        defs = [d for d in every if norm(d.database) == current]
        if len(defs) != 1:
            self._inline_why = ("several_inline_definitions" if len(defs) > 1 else
                                "inline_definition_in_another_database" if every else
                                "no_inline_definition_multi_statement_or_not_scanned")
            return None
        definition = defs[0]
        args = [a.copy() for a in func.expressions]
        if len(args) != len(definition.params) or any(
                (isinstance(a, exp.Var) or (isinstance(a, exp.Column) and not a.table)) and a.name.upper() == "DEFAULT"
                for a in args):
            self._inline_why = "argument_count_or_default"
            return None
        arg_scope = Scope(ctx, {}, None)
        arg_scope.qualified_only = True
        private: dict[str, str] = {}
        for arg in args:
            for column in arg.find_all(exp.Column):
                qualifier = column.table
                if not qualifier:
                    self._inline_why = "unqualified_column_in_argument"
                    return None   # which caller source an unqualified argument column names is not settled
                found, level = [], scope
                while level is not None and not found:
                    found = level.find_alias(qualifier)
                    level = level.parent
                if len(found) != 1:
                    self._inline_why = "argument_alias_not_settled"
                    return None
                alias = private.setdefault(qualifier.lower(), f"__arg{len(private) + 1}_{qualifier.lower()}")
                if not any(src.alias == alias for src in arg_scope.sources):
                    arg_scope.sources.append(dataclasses.replace(found[0], alias=alias, names=(alias,)))
                column.set("table", exp.to_identifier(alias))
        mapping = dict(zip(definition.params, args))
        query = definition.query.copy()

        def substitute(n):
            if isinstance(n, (exp.Parameter, exp.Var)):
                text = restore_variable(n.sql(dialect="tsql", comments=False)).lower()
                if text in mapping:
                    return mapping[text].copy()
            return n

        query = query.transform(substitute)
        local = Local("derived", name, query, alias_columns, statement_ctes(query, ctx), arg_scope,
                      ObjectRef(name, kind="derived", scope=ctx.local_scope(node)))
        self._inlined[id(query)] = definition
        columns = self.local_names(local, ctx)
        return Source(name, "local", columns=columns, reliable=True, local=local, names=(name, fname),
                      certain=self.local_certain(local, ctx) if columns is None else frozenset())

    # ------------------------------------------------------------ INSERT ... EXEC
    def result_set(self, issue):
        """(callee ProcInfo, its one result SELECT statement, target column names) for an INSERT ... EXEC
        of a named procedure, or (None, reason). Only a settled result set is mapped: one callee
        definition, exactly one statement returning rows, and no nested EXEC or dynamic SQL there
        (their result sets would be captured too)."""
        key = id(issue)
        if key in self._result_sets:
            return self._result_sets[key]
        detail = issue.detail or {}
        callee = detail.get("callee")
        out = (None, "executes_a_string_not_a_named_procedure")
        if callee is not None:
            procs = self.catalog.procs_for(callee)
            if len(procs) != 1:
                out = (None, "callee_has_no_definition" if not procs else "callee_has_several_definitions")
            else:
                proc = procs[0]
                body = [st for st in proc.unit.index.statements if st.scope == proc.scope]
                nested = self.catalog.calls_by_caller.get(proc.scope) or self.catalog.dynamic_by_scope.get(proc.scope)
                unparsed = [i for i in self.catalog.issues if i.statement.scope == proc.scope
                            and i.kind in ("parse_error", "insert_exec", "unsupported_statement")]
                results = [st for st in body if self._returns_rows(st)]
                end = max((st.end for st in body), default=proc.start)
                hidden = self._may_return_rows(mask_noncode(proc.unit.text[proc.start:end]))
                if nested:
                    out = (None, "callee_runs_other_procedures_or_dynamic_sql")
                elif unparsed:
                    out = (None, "callee_has_unanalysed_statements")
                elif hidden:
                    out = (None, "callee_may_return_other_rows")
                elif len(results) != 1:
                    out = (None, "callee_has_no_result_statement" if not results
                           else "callee_has_several_result_statements")
                else:
                    names = detail.get("target_columns")
                    if names is None:
                        columns, status, _ = self.catalog.schema_for(detail["target"])
                        names = ([c.name for c in columns if not (c.identity or c.computed or c.rowversion)]
                                 if status == "declared" else None)
                        if names is None and status == "missing":
                            derived = self.into_order(detail["target"])   # a SELECT ... INTO table
                            names = derived[0] if derived else None
                    out = (proc, results[0], names) if names else (None, "no_column_list_and_no_target_order")
        self._result_sets[key] = out
        return out

    @staticmethod
    def _may_return_rows(body: str) -> bool:
        """Whether a procedure body (comments and strings masked) has a statement other than a plain SELECT
        that can send rows to its caller: OUTPUT without INTO, FETCH without INTO, DBCC, RESTORE, ...
        Checked on the text, so statements the indexer does not split out are not missed."""
        for match in re.finditer(r"\bOUTPUT\s+(?:inserted|deleted|\$action)\b[^;]*", body, re.I):
            if not re.search(r"\bINTO\b", match.group(0), re.I):
                return True
        for match in re.finditer(r"\bFETCH\b[^;\n]*", body, re.I):
            if not re.search(r"\bINTO\b", match.group(0), re.I):
                return True
        return re.search(r"(?:^|[;\s])(?:RESTORE|DBCC|READTEXT|RECEIVE)\b", body, re.I) is not None

    @staticmethod
    def _returns_rows(statement) -> bool:
        tree = statement.tree
        if not isinstance(tree, (exp.Select, exp.Union, exp.Except, exp.Intersect)):
            return False
        first = branches(tree)[0]
        if not isinstance(first, exp.Select) or first.args.get("into") is not None:
            return False
        # SELECT @v = ... assigns and returns nothing.
        return not any(isinstance(e, exp.EQ) and isinstance(e.this, exp.Parameter) for e in first.expressions)

    def _insert_exec(self, writer: Writer, column: str, target: Node, ctx: Ctx):
        issue = next((i for i in self.catalog.issues if i.kind == "insert_exec" and i.statement is writer.statement), None)
        found = self.result_set(issue) if issue is not None else (None, "no issue")
        if found[0] is None:
            return [], [], []   # the insert_exec gap on the object explains it
        proc, select, names = found
        if column not in names:
            return [], [], []
        inner = Ctx(self, proc.unit, select, controls_of(proc.unit, select))
        query = select.tree
        self._query_steps(query, None, names.index(column), column, target, inner, "insert_exec",
                          "result_set_position", statement_ctes(query, inner), len(names))
        call_ev = self.catalog.evidence(writer.unit, writer.statement, role="insert_exec_call")
        # A #temp table the callee reads but does not create is the calling procedure's (or one visible to
        # it), not the same-named #temp table of every other caller.
        def visible(node) -> bool:
            if node.kind != TEMP_COLUMN or node.object is None or node.object.kind != "temp_table" \
                    or node.object.scope == proc.scope:
                return True
            return node.object.scope in self.catalog.temp_scopes(node.object.name, writer.statement.scope)
        for step in inner.steps:
            step.inputs = [i for i in step.inputs if visible(i.node)]
            step.row_selection = [r for r in step.row_selection if visible(r.node)]
        for step in inner.steps:
            if step.target.id == target.id:
                step.status = worst(step.status, INFERRED)
                step.procedure = writer.statement.procedure or step.procedure
                step.evidence.insert(0, call_ev)
                step.notes.append(f"Rows come from the result set of {proc.ref.display} (its only statement that "
                                  f"returns rows), matched by position: column {names.index(column) + 1} of "
                                  f"{len(names)}.")
            step._origin = (proc.unit.path, select.start, select.scope)
        return inner.steps, inner.gaps, inner.cycles

    def through_column(self, writer: Writer, column: str, ctx: Ctx) -> str | None:
        """Output column of the CTE/view written by ``writer`` that is base column ``column``."""
        kind, name = writer.via
        if kind == "cte":
            local = statement_ctes(writer.statement.tree, ctx).get(name)
            if local is None:
                return None
            query, aliases, ctes, parent = local.query, local.column_aliases, local_ctes(local, branches(local.query)[0]), None
            inner_ctx = ctx
        else:
            views = [w for w in self.catalog.writers.get(name, []) if w.kind == "view"]
            if len(views) != 1:
                return AMBIGUOUS_THROUGH if views else None
            view = views[0]
            tree = view.statement.tree
            query = tree.args.get("expression")
            aliases = [norm(ident_text(c)) for c in tree.this.expressions] if isinstance(tree.this, exp.Schema) and tree.this.expressions else None
            inner_ctx = Ctx(self, view.unit, view.statement)
            ctes = statement_ctes(query, inner_ctx)
        first = branches(query)[0]
        if not isinstance(first, exp.Select):
            return None
        scope = Scope(inner_ctx, local_ctes_from(first, None, ctes))
        scope.add_select_sources(first)
        items = self.projection(first, scope)
        matches = []
        for i, item in enumerate(items):
            expr = item.expr
            if item.source is not None and item.column == column:
                refs = item.source.refs
            elif isinstance(expr, exp.Column) and norm(ident_text(expr.this)) == column:
                refs = [(b.node.object, b.status) for b in scope.bind(expr, expand_local=False) if b.node.object is not None]
            else:
                continue
            if any(r.key == writer.ref.key for r, _ in refs):
                out_name = aliases[i] if aliases and i < len(aliases) else item.name
                if out_name:
                    matches.append(out_name)
        return matches[0] if len(matches) == 1 else None

    @staticmethod
    def _constant_truth(e) -> bool | None:
        """True/False when a predicate's value does not depend on any row (literals only), else None."""
        if isinstance(e, exp.Paren):
            return Analyzer._constant_truth(e.this)
        if isinstance(e, exp.Not):
            inner = Analyzer._constant_truth(e.this)
            return None if inner is None else not inner
        if isinstance(e, exp.And):
            a, b = Analyzer._constant_truth(e.this), Analyzer._constant_truth(e.expression)
            return False if False in (a, b) else (True if a is True and b is True else None)
        if isinstance(e, exp.Or):
            a, b = Analyzer._constant_truth(e.this), Analyzer._constant_truth(e.expression)
            return True if True in (a, b) else (False if a is False and b is False else None)
        if isinstance(e, (exp.EQ, exp.NEQ, exp.GT, exp.GTE, exp.LT, exp.LTE)) and \
                isinstance(e.this, exp.Literal) and isinstance(e.expression, exp.Literal):
            a, b = e.this, e.expression
            if a.is_string != b.is_string:
                return None
            left, right = (a.this, b.this) if a.is_string else (float(a.this), float(b.this))
            return {exp.EQ: left == right, exp.NEQ: left != right, exp.GT: left > right, exp.GTE: left >= right,
                    exp.LT: left < right, exp.LTE: left <= right}[type(e)]
        if isinstance(e, exp.Boolean):
            return bool(e.this)
        return None

    @staticmethod
    def yields_no_rows(select) -> bool:
        """A SELECT that can return no row whatever the data: TOP (0), or a WHERE that is constant-false
        (`WHERE 1 = 2`, the usual way to copy a table's layout into an empty table)."""
        if not isinstance(select, exp.Select):
            return False
        limit = select.args.get("limit")
        count = limit.args.get("expression") if isinstance(limit, exp.Limit) else None
        if isinstance(count, exp.Paren):
            count = count.this
        if isinstance(count, exp.Literal) and not count.is_string and float(count.this) == 0:
            return True
        where = select.args.get("where")
        return where is not None and Analyzer._constant_truth(where.this) is False

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
                    value = row.expressions[position]
                    if isinstance(value, exp.Var) and value.name.upper() == "DEFAULT" and target.object is not None:
                        columns, status, _ = self.catalog.schema_for(target.object)
                        info = next((c for c in columns or [] if c.name == target.name), None)
                        if info is None:
                            ctx.gap("missing_definition", f"VALUES gives DEFAULT for {target.name}, but no definition "
                                    f"of {target.object.display} declares it.", target, row)
                            continue
                        default = info.default or self.catalog.column_defaults.get(target.object.key, {}).get(info.name)
                        value = parse_expression(default) if default else exp.Null()
                        step = self.make_step(target, "insert_values", value or exp.Null(), Scope(ctx, ctes), ctx,
                                              method, row_context)
                        step.notes.append(f"VALUES gives DEFAULT: the column's declared default "
                                          f"{default if default else '(none, so NULL)'}.")
                        ctx.steps.append(step)
                        continue
                    ctx.steps.append(self.make_step(target, "insert_values", value, Scope(ctx, ctes),
                                                    ctx, method, row_context))
                continue
            if not isinstance(select, exp.Select):
                ctx.gap("unsupported_statement", f"Unsupported query form {type(select).__name__}.", target, select)
                continue
            if self.yields_no_rows(select):
                continue   # copies the layout only (WHERE 1 = 2, TOP (0)): no row, so no value comes from here
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
                at = names.index(column) if names and column in names else None
                item = self.pick(items, names, column, True) if i == 1 else (
                    items[at] if at is not None and at < len(items) and None not in names[:at + 1]
                    and not any(it.unknown for it in items[:at + 1]) else None)
                if item is None:
                    if any(it.unknown for it in items):
                        ctx.gap("wildcard_without_schema", "A * in the projection cannot be expanded, so it is unknown "
                                f"whether it supplies {column}.", target, select)
                    elif i > 1:
                        ctx.gap("wildcard_without_schema", f"Branch {i}: the position of {column} is unknown because a "
                                "* in the first branch cannot be expanded.", target, select)
                    continue
            ctx.steps.append(self.item_step(target, writer_kind, item, scope, ctx, method, context, select))

    def _insert(self, tree, column, target, ctx, writer) -> None:
        ctes = statement_ctes(tree, ctx)
        query = tree.expression
        if isinstance(tree.this, exp.Schema) and tree.this.expressions:
            names = [norm(ident_text(c)) for c in tree.this.expressions]
            if column not in names:
                self._omitted(column, target, ctx, writer, tree)
                return
            self._query_steps(query, None, names.index(column), column, target, ctx,
                              "insert_values" if isinstance(query, exp.Values) else "insert_select",
                              "target_list", ctes, len(names))
            return
        columns, status, _ = self.catalog.schema_for(writer.ref)
        derived = self.into_order(writer.ref) if status == "missing" else None
        if derived is not None:
            names, definer = derived
            if column not in names:
                return
            self._query_steps(query, None, names.index(column), column, target, ctx,
                              "insert_values" if isinstance(query, exp.Values) else "insert_select",
                              "positional_schema", ctes, len(names))
            for step in ctx.steps:
                if step.target.id == target.id:
                    step.status = worst(step.status, INFERRED)
                    step.notes.append("Matched by position against the column order set by the SELECT ... INTO "
                                      "that creates the table (no identity, computed or rowversion column is copied).")
                    step.evidence.append(self.catalog.evidence(
                        definer.unit, definer.statement, role="target_schema",
                        excerpt=f"column order of {writer.ref.display}: position {names.index(column) + 1} of "
                                f"{len(names)}, from SELECT ... INTO"))
            return
        if status != "declared":
            message = ("INSERT without a column list, and the target has no declared ordered schema in the "
                       "scanned files, so its columns cannot be matched by position.") if status == "missing" else \
                ("INSERT without a column list, and the target has conflicting CREATE TABLE definitions.")
            if status == "ambiguous":
                reason = "target_has_differing_definitions"
            elif any(w.kind == "select_into" for w in self.catalog.writers_for(writer.ref)):
                reason = "select_into_target_order_not_settled"
            elif writer.ref.kind in ("temp_table", "table_variable"):
                reason = "temp_target_created_elsewhere_or_unparsed"
            else:
                reason = ("target_written_but_no_definition" if self.catalog.is_known(writer.ref)
                          else "target_has_no_definition")
            ctx.gap("positional_without_schema", message, target, tree.this, {"reason": reason})
            return
        insertable = [c.name for c in columns if not (c.identity or c.computed or c.rowversion)]
        if column not in insertable:
            return
        self._query_steps(query, None, insertable.index(column), column, target, ctx,
                          "insert_values" if isinstance(query, exp.Values) else "insert_select",
                          "positional_schema", ctes, len(insertable))
        _, _, schema = self.catalog.schema_for(writer.ref)
        for step in ctx.steps:
            if step.target.id == target.id:
                step.status = worst(step.status, INFERRED)
                step.notes.append("Matched by position against the declared column order, skipping identity, "
                                  "computed and rowversion columns.")
                if schema is not None:
                    # The column order the match relied on.
                    step.evidence.append(self.catalog.evidence(
                        schema.unit, schema.statement, role="target_schema",
                        excerpt=f"column order of {writer.ref.display}: position {insertable.index(column) + 1} "
                                f"of {len(insertable)}"))

    def _omitted(self, column, target, ctx, writer, tree) -> None:
        """An INSERT whose column list omits the target column adds rows holding its default or NULL."""
        if writer.via:
            return
        columns, status, schema = self.catalog.schema_for(writer.ref)
        info = next((c for c in columns or [] if c.name == column), None)
        if info is not None and (info.identity or info.computed or info.rowversion):
            return  # generated values are explained by the column definition, not by this INSERT
        if info is None and status != "declared" and column not in self.object_certain(writer.ref):
            # No definition lists the column and no statement names it: it may not exist (a phantom bound
            # from a keyword, a PIVOT output, ...). Never complete such a column from "default or NULL".
            ctx.gap("missing_definition", f"{writer.ref.display} has no known column {column}: no definition lists "
                    "it and no statement in the scan names it.", target, tree.this)
            return
        if info is not None and info.default:
            value = parse_expression(info.default) or exp.Null()
            what = f"its default {info.default}"
        else:
            value = exp.Null()
            what = "NULL (no default is declared)" if info is not None else "its default or NULL (schema unknown)"
        step = Step(target=target, writer_kind="insert_omitted", expression=value.sql(dialect="tsql", comments=False), operation="constant",
                    inputs=[Input(Node(CONSTANT, value.sql(dialect="tsql", comments=False)), VALUE, value.sql(dialect="tsql", comments=False), None)],
                    branch_context=list(ctx.controls), mapping_method="omitted_from_insert",
                    status=VERIFIED if info is not None else INFERRED, procedure=ctx.statement.procedure,
                    evidence=[ctx.evidence(tree.this, "target_column_list")], order=ctx.statement.start)
        step.notes.append(f"This INSERT does not list {column}, so the rows it adds hold {what}.")
        ctx.steps.append(step)

    def computed_step(self, ref: ObjectRef, info: ColumnInfo, schema, target: Node) -> tuple[list[Step], list[Gap]]:
        """A computed column's value comes from its definition over the same row."""
        ctx = Ctx(self, schema.unit, schema.statement)
        value = parse_expression(info.computed)
        if value is None:
            ctx.gap("parse_error", f"Computed column definition of {info.name} could not be parsed.", target)
            return [], ctx.gaps
        scope = Scope(ctx, {})
        columns = [c.name for c in self.catalog.schema_for(ref)[0] or []]
        scope.sources.append(Source(ref.name, "object", [(ref, VERIFIED)], columns, True, names=(ref.name,)))
        step = self.make_step(target, "computed_column", value, scope, ctx, "column_definition", [])
        step.notes.append("Computed column: evaluated from other columns of the same row.")
        ctx.steps.append(step)
        return ctx.steps, ctx.gaps

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
            if isinstance(assignment, exp.Dot) and isinstance(assignment.expression, exp.Anonymous) \
                    and str(assignment.expression.this).upper() == "WRITE" and norm(ident_text(assignment.this)) == column:
                # col.WRITE(expr, offset, length) replaces part of the existing value.
                value = exp.Anonymous(this="WRITE", expressions=[exp.column(column), *assignment.expression.expressions])
                step = self.make_step(target, "update", value, scope, ctx, "set_clause", [])
                step.operation = "partial_update"
                step.row_selection.extend(self.row_selection(tree, scope, ctx))
                step.notes.append(".WRITE replaces part of the existing value with the given expression.")
                ctx.steps.append(step)
                continue
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
                with row_selection_mode(ctx):
                    self.walk(clause, CONDITION, None, scope, ctx, inputs, [], False)
                role = JOIN_KEY if isinstance(clause, exp.EQ) else JOIN_PREDICATE
                for item in inputs:
                    if item.node.kind != CONSTANT:
                        base_rows.append(RowSelection(item.node, role, "MERGE ON " + clause.sql(dialect="tsql", comments=False),
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
                label += f" AND {condition.sql(dialect='tsql', comments=False)}"
            then = when.args.get("then")
            rows = list(base_rows)
            if condition is not None:
                inputs: list[Input] = []
                with row_selection_mode(ctx):
                    self.walk(condition, CONDITION, None, scope, ctx, inputs, [], False)
                for item in inputs:
                    if item.node.kind != CONSTANT:
                        rows.append(RowSelection(item.node, MERGE_CONDITION, condition.sql(dialect="tsql", comments=False),
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
                    # NOT MATCHED rows are those with no target match, so the ON keys on both sides select them.
                    step.row_selection.extend(rows)
                    ctx.steps.append(step)

    # ======================================================== variables etc.
    def assignment_steps(self, assignment, target: Node) -> tuple[list[Step], list[Gap]]:
        ctx = Ctx(self, assignment.unit, assignment.statement, controls_of(assignment.unit, assignment.statement))
        if assignment.kind == "exec_output":
            gap = ctx.gap("unresolved_variable", f"{target.name} is set by a procedure OUTPUT parameter or return code, which is not traced.",
                          target)
            return [], ctx.gaps
        if assignment.value is None:
            ctx.gap("unresolved_variable", f"{target.name} is set by {assignment.note or assignment.kind}; the value "
                    "could not be analysed.", target)
            return [], ctx.gaps
        scope = Scope(ctx, {})
        if assignment.select is not None:
            scope = Scope(ctx, local_ctes_from(assignment.select, None, statement_ctes(assignment.statement.tree, ctx)))
            scope.add_select_sources(assignment.select)
            if isinstance(assignment.select, exp.Update):
                target_table = update_target(assignment.select)
                if isinstance(target_table, exp.Table) and not any(
                        s.alias in {norm(target_table.alias_or_name), norm(table_parts(target_table)[3])} for s in scope.sources):
                    scope.add_from(target_table)
        step = self.make_step(target, "variable_assignment", assignment.value, scope, ctx, "assignment", [])
        if assignment.note:
            step.notes.append(assignment.note)
        if assignment.select is not None:
            step.row_selection.extend(self.row_selection(assignment.select, scope, ctx))
            if assignment.kind == "fetch":
                step.notes.append("Each FETCH reads the next cursor row; the variable takes values from many rows.")
            else:
                step.notes.append("Assigning a variable from a multi-row statement keeps the value from the last row "
                                  "processed.")
        ctx.steps.append(step)
        return ctx.steps, ctx.gaps

    def default_step(self, proc, value: exp.Expression, statement, target: Node, note: str):
        ctx = Ctx(self, proc.unit, statement)
        step = self.make_step(target, "parameter_default", value, Scope(ctx, {}), ctx, "parameter_default", [],
                              procedure=proc.ref.display)
        step.notes.append(f"Default value of {target.name}, {note}.")
        ctx.steps.append(step)
        return ctx.steps, ctx.gaps

    def argument_step(self, call, value: exp.Expression, target: Node) -> tuple[list[Step], list[Gap]]:
        ctx = Ctx(self, call.unit, call.statement, controls_of(call.unit, call.statement))
        step = self.make_step(target, "parameter_binding", value, Scope(ctx, {}), ctx, "argument", [],
                              procedure=call.statement.procedure)
        ctx.steps.append(step)
        return ctx.steps, ctx.gaps


# ----------------------------------------------------------------- helpers
AMBIGUOUS_THROUGH = object()   # through_column: the view has several definitions


class row_selection_mode:
    """Context manager: bind columns without expanding local sources (see Ctx.no_expand)."""

    def __init__(self, ctx: "Ctx"):
        self.ctx = ctx

    def __enter__(self):
        self.previous = (self.ctx.no_expand, self.ctx.in_row_selection)
        self.ctx.no_expand = not self.ctx.analyzer.expand_row_selection
        self.ctx.in_row_selection = True

    def __exit__(self, *exc):
        self.ctx.no_expand, self.ctx.in_row_selection = self.previous


def parse_expression(text: str | None):
    if not text:
        return None
    try:
        parsed = sqlglot.parse_one(f"SELECT {text}", read="tsql")
    except Exception:  # noqa: BLE001
        return None
    return parsed.expressions[0] if isinstance(parsed, exp.Select) and parsed.expressions else None


def join_list(select: exp.Expression) -> list[exp.Join]:
    """Joins of a query level; SQLGlot may attach them to the statement, the FROM, or the FROM's table."""
    joins = list(select.args.get("joins") or [])
    from_ = select.args.get("from_")
    if from_ is not None:
        joins.extend(from_.args.get("joins") or [])
        if isinstance(from_.this, exp.Expression):
            joins.extend(from_.this.args.get("joins") or [])
    return joins


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
                          ObjectRef(name, kind="cte", scope=ctx.local_scope(cte)))
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
