#!/usr/bin/env python3
"""Generate a synthetic, deliberately messy T-SQL warehouse with known column lineage.

    python tools/generate_corpus.py --out DIR --objects N --seed S [--databases K]
                                    [--rate name=value ...] [--messiness X] [--chains C]

The corpus is a layered warehouse spread over K databases (one folder per database,
plus a root-level ``deploy`` folder whose scripts switch database with ``USE``):

    landing tables -> staging procs (#temp, CTE, CASE/COALESCE, joins, filters)
                   -> staging tables -> core loaders (MERGE / INSERT, IF/ELSE writers)
                   -> core tables -> views -> views on views
                   -> mart loaders (caller-created #temp filled by a child proc,
                      SELECT INTO #temp, MERGE, UPDATE ... FROM, INSERT ... EXEC,
                      dynamic SQL, cursors) -> mart tables -> mart summaries

Every column-level write the generator emits is also recorded in an in-memory
ground-truth graph, so ``DIR/manifest.json`` can list "known chains": a mart column,
the exact upstream source columns reachable through value/condition inputs, the
number of persistent-object layers and the messy features on the path.

After the warehouse and its chains, ``--extension-variants N`` (default 2) adds N
variations of each extension chain kind (``EXT_KINDS``: earlier-content reads, INSERT ...
EXEC, inline table-valued functions, * by name, positional INSERTs into SELECT ... INTO
tables, SQL Agent job ordering with scripts under ``DIR/jobs/``, multi-column UPDATE with
CROSS APPLY, ALTER TABLE ... ADD). Their manifest entries carry ``kind``, ``variant`` and
``expect`` (gap kinds and reasons, forbidden gap kinds, stop reasons, evidence roles, step
statuses, completeness, no cycles) so ``tools/perf.py`` scores a wrongly removed gap as a
miss. They use their own random stream and add objects on top of ``--objects``: the legacy
files and chains of a seed are byte-for-byte the same with or without them.

Output is byte-for-byte deterministic for the same arguments (no set iteration
order, clock or hash seed leaks into the files).
"""
from __future__ import annotations

import argparse
from collections import defaultdict
from dataclasses import dataclass, field
import json
from pathlib import Path
import random
import re
import shutil
import sys

GENERATOR_VERSION = "1.1"
MANIFEST_SCHEMA = "synthetic-corpus-manifest/1.1"

# Extension chain kinds (generator 1.1). Each is built in several variations after the layered warehouse
# and its 20 legacy chains, with its own random stream, so the legacy output of a seed is unchanged.
# The manifest entry of an extension chain carries ``kind``, ``variant`` and ``expect`` (what the column
# tool's trace must contain: expected gap kinds and reasons, forbidden gap kinds, stop reasons, evidence
# roles, step statuses, completeness and the absence of cycles) next to the usual expected leaves.
EXT_KINDS = (
    "self_update",            # UPDATE t SET a = a * 2 after an INSERT: insert source + prior_state stop, no cycle
    "preserve_restore",       # rows saved to a #temp, deleted, reloaded and restored from the #temp
    "aggregate_update",       # #working loaded, aggregated into #totals, updated from it, then copied out
    "variable_accumulate",    # SELECT @s = @s + col, then the variable is inserted
    "insert_exec_one",        # INSERT ... EXEC of a procedure with one result SELECT: mapped by position
    "insert_exec_two",        # ... with two result SELECTs: insert_exec gap
    "itvf_apply",             # inline table-valued function via CROSS / OUTER APPLY: its base table, inferred
    "star_by_name",           # TOP (0) * INTO #t from a table with no CREATE TABLE but an INSERT list; CTE *
    "positional_select_into", # INSERT without a column list into a SELECT ... INTO table: that order
    "positional_identity",    # ... whose source has an IDENTITY column: positional_without_schema gap
    "job_ordered",            # one SQL Agent job orders the loader and two UPDATE procedures: no order gap
    "job_split",              # the loader and the UPDATE run in two different jobs: writers_in_different_jobs
    "multi_update_apply",     # UPDATE SET a = ..., b = ... with CROSS APPLY reading both pre-update values
    "alter_add_column",       # ALTER TABLE ... WITH CHECK ADD col, then a loader selects that column: bound
)
EXT_RNG_SALT = 1_000_003

DB_NAMES = ["stage_db", "core_db", "mart_db", "mart2_db", "ref_db", "sandbox_db", "archive_db", "ops_db"]
NOUNS = ["order", "shipment", "invoice", "account", "asset", "claim", "ledger", "visit", "policy",
         "sensor", "booking", "payment", "contract", "ticket", "route", "parcel", "lease", "grant",
         "meter", "voucher"]

# Probabilities (or per-1000-object counts where named so). Override with --rate name=value.
DEFAULT_RATES: dict[str, float] = {
    # identifier and file-level mess
    "bracketed": 0.12,            # per identifier occurrence
    "mixed_case": 0.10,           # per identifier occurrence
    "three_part_same_db": 0.10,   # same-database reference written with its database name
    "one_part_name": 0.30,        # dbo objects in the same database referenced without a schema
    "bare_column": 0.15,          # single-source SELECT columns without an alias prefix
    "use_header": 0.15,           # per database-folder file: starts with USE [db]
    "crlf": 0.10,                 # per file
    "bom": 0.05,                  # per file
    "ssms_header": 0.25,          # per object: SET ANSI_NULLS / QUOTED_IDENTIFIER batches
    "drop_preamble": 0.15,        # per object: IF OBJECT_ID(...) DROP ...; GO
    "multi_object_file": 0.35,    # per object: placed in a GO-separated bundle file
    "module_file": 0.25,          # per table: placed in the same file as its loader
    "deploy_file": 0.03,          # per object: placed in a root-level USE-switching deploy script
    "proc_subfolder": 0.30,       # per procedure file: nested in a subject subfolder
    # procedure-level mess
    "sql_comment": 0.35,          # comments containing SQL-looking text
    "sql_string": 0.30,           # string literals containing SQL text
    "try_catch": 0.30,
    "no_begin_end": 0.08,         # procedure body without BEGIN/END
    "nolock": 0.10,               # WITH (NOLOCK) table hints
    "dynamic_noise": 0.08,        # off-path dynamic SQL (TRUNCATE / statistics)
    "unparseable_noise": 0.06,    # off-path statements sqlglot cannot parse
    # lineage-shaping features
    "if_else": 0.15,              # core loader has IF/ELSE alternative writers
    "while_loop": 0.12,           # staging insert runs in a WHILE batch loop
    "cte_star": 0.10,             # staging CTE uses r.* over a #temp
    "select_star_view": 0.15,     # extra SELECT * view layer
    "wildcard_insert": 0.08,      # INSERT ... SELECT * FROM #temp with a known schema
    "positional_insert": 0.15,    # INSERT without a column list
    "view_column_list": 0.10,     # CREATE VIEW v (cols) AS ...
    "computed_column": 0.10,      # core table with a computed column in the middle
    "identity_fact": 0.25,        # mart fact with a leading IDENTITY column
    "udf": 0.05,                  # per derived expression: scalar UDF call
    "scalar_subquery": 0.03,      # per derived expression: correlated scalar subquery
    "tsql_alias": 0.08,           # per projected column: `name = expr` alias style
    "nested_exec_temp": 0.70,     # mart loader fills a caller-created #temp in a child proc
    "insert_exec": 0.08,
    "dynamic_sql": 0.08,          # a mart column written only through dynamic SQL
    "cursor": 0.05,
    "unparseable_writer": 0.04,   # a mart column written only by UPDATE TOP (n) (sqlglot fails)
    "patch_writer": 0.10,         # second procedure writes core columns from another landing table
    "cross_subject_join": 0.30,   # wide view joins another subject's base view
    "ref_join": 0.50,             # core loader joins a reference table
    "summary_table": 0.35,
    "missing_landing_ddl": 0.04,  # landing table referenced but never defined
    "dead_proc": 0.03,            # fraction of the object budget spent on never-called procedures
    "dead_writer": 0.30,          # fraction of dead procedures that write a live core column
    "long_proc_per_1000": 1.0,    # 100+ statement maintenance procedures per 1000 objects
}
# Rates scaled by --messiness (structural ones such as nested_exec_temp are not).
MESSY_RATES = {
    "bracketed", "mixed_case", "three_part_same_db", "one_part_name", "use_header", "crlf", "bom",
    "deploy_file", "sql_comment", "sql_string", "no_begin_end", "nolock", "dynamic_noise",
    "unparseable_noise", "if_else", "while_loop", "cte_star", "select_star_view", "wildcard_insert",
    "positional_insert", "udf", "scalar_subquery", "tsql_alias", "insert_exec", "dynamic_sql", "cursor",
    "unparseable_writer", "patch_writer", "missing_landing_ddl", "dead_proc", "dead_writer",
}

# Features after which a purely static analyzer is not expected to prove the path.
OPAQUE_FEATURES = ("dynamic_sql", "parse_error", "insert_exec", "cursor", "udf_reads_table", "positional_identity")
LIKELY_GAP = {"dynamic_sql": "dynamic_sql", "parse_error": "parse_error", "insert_exec": "insert_exec",
              "cursor": "unsupported_statement", "udf_reads_table": "opaque_function",
              "positional_identity": "positional_without_schema"}

SQL_TYPES = {
    "int": ["int", "int", "bigint"],
    "num": ["decimal(18,4)", "decimal(18,2)", "decimal(19,4)", "money", "float"],
    "str": ["varchar(50)", "nvarchar(100)", "varchar(200)", "nvarchar(40)"],
    "date": ["date", "datetime2(3)", "datetime"],
    "flag": ["bit"],
}
KIND_DIR = {"table": "Tables", "view": "Views", "procedure": "StoredProcedures", "function": "Functions"}
CARRY = ("rec_id", "region_cd", "event_dt")
NO_PROPAGATE = {"load_ts", "load_dt", "is_deleted", "rn"}
ATTRS_A = [("attr_color", "str"), ("attr_size", "str"), ("attr_channel", "str"), ("attr_score", "num"),
           ("attr_weight", "num"), ("attr_rank", "int"), ("attr_note", "str"), ("attr_ratio", "num")]
ATTRS_B = [("b_score", "num"), ("b_flag_cd", "str"), ("b_units", "int"), ("b_fee", "num")]
SUFFIX = {
    "rename": ["_val", "_v", "_x", "_std", "_final", "_adj"], "case": ["_sel", "_pick"],
    "coalesce": ["_coal", "_nn"], "isnull": ["_nz"], "arith": ["_calc", "_net", "_ratio", "_ext"],
    "cast": ["_fmt", "_dec"], "udf": ["_clean", "_rnd"], "iif": ["_iif"], "concat": ["_key", "_lbl"],
    "const": ["_stamp"], "subquery": ["_wt"],
}


# ---------------------------------------------------------------------------
# model

@dataclass
class Column:
    name: str
    typ: str
    sqltype: str
    identity: bool = False
    computed: str | None = None
    computed_from: tuple = ()


@dataclass
class DbObject:
    db: str
    schema: str
    name: str
    kind: str                       # table | view | procedure | function
    columns: list[Column] = field(default_factory=list)
    layer: str = ""
    subject: str | None = None
    file: str | None = None
    defined: bool = True
    statements: int = 0

    @property
    def key(self) -> str:
        return f"{self.db}.{self.schema}.{self.name}".lower()

    def col(self, name: str) -> Column | None:
        return next((c for c in self.columns if c.name == name), None)


@dataclass
class Src:
    """A relation visible in a FROM clause."""
    alias: str
    sql: str
    cols: list[Column]
    prefix: str                     # ground-truth node prefix
    feats: frozenset = frozenset()
    kind: str = "table"
    hint: str = ""                  # table hint rendered after the alias

    def col(self, name: str) -> Column | None:
        return next((c for c in self.cols if c.name == name), None)


@dataclass
class Expr:
    sql: str
    inputs: list                    # [(node, role, feats)]
    feats: set
    typ: str
    kind: str
    base: str
    plain: str | None = None        # column name when the expression is a bare reference
    bare: bool = False              # contains a column reference without an alias prefix


@dataclass
class Ctx:
    db: str
    use: bool
    proc: str | None = None
    flow: list = field(default_factory=list)
    stmt_no: int = 0

    def flow_feats(self) -> set:
        return set(self.flow)


@dataclass
class FileBuf:
    path: str
    db: str | None
    use_header: bool
    capacity: int
    crlf: bool
    bom: bool
    chunks: list = field(default_factory=list)
    objects: int = 0
    current_use: str | None = None


class Body:
    def __init__(self, depth: int = 1) -> None:
        self.lines: list[str] = []
        self.depth = depth
        self.statements = 0

    def _add(self, sql: str) -> None:
        pad = "    " * self.depth
        self.lines.extend(pad + line if line else "" for line in sql.split("\n"))

    def stmt(self, sql: str) -> None:
        self._add(sql)
        self.statements += 1

    def raw(self, sql: str) -> None:
        self._add(sql)

    def push(self, header: str | None = None, begin: str = "BEGIN") -> None:
        if header:
            self.raw(header)
        self.raw(begin)
        self.depth += 1

    def pop(self, footer: str = "END") -> None:
        self.depth -= 1
        self.raw(footer)


class Truth:
    """Column-level ground truth recorded while SQL is emitted."""

    def __init__(self) -> None:
        self.edges: dict[str, list] = defaultdict(list)
        self.writers: dict[str, set] = defaultdict(set)
        self.node_feats: dict[str, set] = defaultdict(set)

    def write(self, target: str, inputs: list, feats: set, writer: str) -> None:
        self.writers[target].add(writer)
        for node, role, src_feats in inputs:
            self.edges[target].append((node, role, frozenset(set(feats) | set(src_feats))))

    @staticmethod
    def persistent(node: str) -> bool:
        return ":" not in node

    def is_source(self, node: str) -> bool:
        return self.persistent(node) and not self.writers.get(node)

    def finalize(self) -> None:
        for node in sorted(self.writers):
            if self.persistent(node) and len(self.writers[node]) > 1:
                self.node_feats[node].add("multiple_writers")

    def analyze(self, start: str) -> dict:
        value, cond, static, features, objects, static_objects = set(), set(), set(), set(), set(), set()
        seen = set()
        stack = [(start, False, False)]
        while stack:
            node, via_cond, opaque = stack.pop()
            if (node, via_cond, opaque) in seen:
                continue
            seen.add((node, via_cond, opaque))
            if self.persistent(node):
                objects.add(node.rsplit(".", 1)[0])
                if not opaque:
                    static_objects.add(node.rsplit(".", 1)[0])
            features |= self.node_feats.get(node, set())
            if node != start and self.is_source(node):
                (cond if via_cond else value).add(node)
                if not opaque:
                    static.add(node)
                continue
            for src, role, feats in self.edges.get(node, ()):
                if src == node:
                    continue
                features |= feats
                stack.append((src, via_cond or role != "value", opaque or bool(feats & set(OPAQUE_FEATURES))))
        layers = self._layers(start)
        return {"value_leaves": value, "condition_leaves": cond - value, "static_leaves": static,
                "features": features, "objects": objects, "static_objects": static_objects, "layers": layers}

    def _layers(self, start: str) -> tuple[int, int] | None:
        memo: dict[str, tuple[int, int] | None] = {}

        def visit(node: str, active: frozenset) -> tuple[int, int] | None:
            if node in memo:
                return memo[node]
            here = 1 if self.persistent(node) else 0
            if node != start and self.is_source(node):
                return (1, 1)
            best = None
            for src, _role, _feats in self.edges.get(node, ()):
                if src in active or src == node:
                    continue
                result = visit(src, active | {src})
                if result is None:
                    continue
                best = result if best is None else (min(best[0], result[0]), max(best[1], result[1]))
            out = None if best is None else (best[0] + here, best[1] + here)
            memo[node] = out
            return out

        result = visit(start, frozenset([start]))
        return None if result is None else (result[0] - 1, result[1] - 1)


class Picker:
    """Pick input columns, preferring ones not used yet so most columns propagate."""

    def __init__(self, pool: list, rng: random.Random) -> None:
        self.pool = pool
        self.unused = list(pool)
        self.rng = rng

    def pick(self, types: tuple[str, ...] | None = None, exclude=()) -> tuple | None:
        def ok(item):
            return (types is None or item[1].typ in types) and item not in exclude
        for item in self.unused:
            if ok(item):
                self.unused.remove(item)
                return item
        candidates = [item for item in self.pool if ok(item)]
        return self.rng.choice(candidates) if candidates else None


# ---------------------------------------------------------------------------
# generator

class Generator:
    def __init__(self, out: Path, objects: int, seed: int, databases: int, rates: dict[str, float],
                 n_chains: int = 20, extension_variants: int = 2) -> None:
        if databases < 1:
            raise ValueError("--databases must be at least 1")
        self.out = out
        self.target_objects = max(10, objects)
        self.seed = seed
        self.rates = rates
        self.n_chains = n_chains
        self.extension_variants = max(0, extension_variants)
        self.rng = random.Random(seed)
        self.ext_rng = random.Random(seed * EXT_RNG_SALT + 11)   # extension chains: separate stream
        self.ext_paths: set[str] = set()
        self.ext_jobs: list[dict] = []
        self.ext_objects = 0
        self.dbs = DB_NAMES[:databases] + [f"db{i:02d}" for i in range(len(DB_NAMES), databases)]
        self.folders = {db: self._folder_name(db) for db in self.dbs}
        self.stage_db = self.dbs[0]
        self.core_db = self.dbs[1] if len(self.dbs) > 1 else self.dbs[0]
        self.mart_dbs = self.dbs[2:] if len(self.dbs) > 2 else [self.dbs[-1]]
        self.objects: list[DbObject] = []
        self.files: dict[str, FileBuf] = {}
        self.open_bundles: dict[str, FileBuf] = {}
        self.bundle_counter: dict[str, int] = defaultdict(int)
        self.truth = Truth()
        self.subjects: list[dict] = []
        self.live_procs: dict[str, list[DbObject]] = defaultdict(list)
        self.table_targets: list[str] = []
        self.statement_count = 0

    # -- helpers -----------------------------------------------------------
    @staticmethod
    def _folder_name(db: str) -> str:
        base = db[:-3] if db.endswith("_db") else db
        return base[:1].upper() + base[1:]

    def chance(self, name: str) -> bool:
        return self.rng.random() < self.rates[name]

    def sqltype(self, typ: str) -> str:
        return self.rng.choice(SQL_TYPES[typ])

    def column(self, name: str, typ: str, **kw) -> Column:
        return Column(name, typ, self.sqltype(typ), **kw)

    def ident(self, name: str) -> tuple[str, bool]:
        styled = False
        text = name
        if not name.startswith("#") and self.chance("mixed_case"):
            text = self.rng.choice([name.upper(), "_".join(p[:1].upper() + p[1:] for p in name.split("_"))])
            styled = text != name
        if not name.startswith("#") and self.chance("bracketed"):
            text = f"[{text}]"
            styled = True
        return text, styled

    def cid(self, name: str) -> str:
        return self.ident(name)[0]

    def ref(self, obj: DbObject, ctx: Ctx, allow_one_part: bool = True) -> tuple[str, frozenset]:
        feats = set()
        if obj.db != ctx.db:
            parts = [obj.db, obj.schema, obj.name]
            feats.add("cross_db_three_part")
        elif allow_one_part and obj.schema == "dbo" and self.chance("one_part_name"):
            parts = [obj.name]
            feats.add("one_part_name")
        elif self.chance("three_part_same_db"):
            parts = [obj.db, obj.schema, obj.name]
            feats.add("three_part_name")
        else:
            parts = [obj.schema, obj.name]
            feats.add("use_context" if ctx.use else "folder_db_context")
        rendered = []
        for part in parts:
            text, styled = self.ident(part)
            rendered.append(text)
            if styled:
                feats.add("bracketed_or_mixed_case")
        return ".".join(rendered), frozenset(feats)

    def decl_name(self, obj: DbObject) -> str:
        return f"{self.cid(obj.schema)}.{self.cid(obj.name)}"

    def src(self, obj: DbObject, alias: str, ctx: Ctx, hint: bool = True) -> Src:
        sql, feats = self.ref(obj, ctx)
        table_hint = " WITH (NOLOCK)" if hint and obj.kind == "table" and self.chance("nolock") else ""
        if obj.kind == "view":
            feats = feats | {"via_view"}
        return Src(alias, sql, list(obj.columns), obj.key, feats, obj.kind, table_hint)

    def temp_src(self, scope: str, name: str, alias: str, cols: list[Column], feats=()) -> Src:
        return Src(alias, name, cols, f"temp:{scope}::{name.lower()}", frozenset(feats), "temp")

    # -- objects and files -------------------------------------------------
    def new_object(self, db: str, schema: str, name: str, kind: str, layer: str, subject=None,
                   columns=None, defined: bool = True) -> DbObject:
        obj = DbObject(db, schema, name, kind, list(columns or []), layer, subject, defined=defined)
        if defined:
            self.objects.append(obj)
        return obj

    def place(self, obj: DbObject, near: DbObject | None = None) -> Ctx:
        fb = None
        if near is not None and near.file and self.files[near.file].db in (obj.db, None):
            fb = self.files[near.file]
        if fb is None:
            roll = self.rng.random()
            if roll < self.rates["deploy_file"]:
                fb = self._bundle("deploy", None)
            elif roll < self.rates["deploy_file"] + self.rates["multi_object_file"]:
                fb = self._bundle(self.folders[obj.db], obj.db)
            else:
                sub = ""
                if obj.kind == "procedure" and obj.subject and self.chance("proc_subfolder"):
                    sub = obj.subject.split("_")[0].capitalize() + "/"
                ext = ".prc" if obj.kind == "procedure" and self.rng.random() < 0.04 else ".sql"
                path = f"{self.folders[obj.db]}/{KIND_DIR[obj.kind]}/{sub}{obj.schema}.{obj.name}{ext}"
                fb = self._new_file(path, obj.db, capacity=1)
        obj.file = fb.path
        fb.objects += 1
        if fb.objects >= fb.capacity:
            for key, open_fb in list(self.open_bundles.items()):
                if open_fb is fb:
                    del self.open_bundles[key]
        return Ctx(obj.db, use=fb.use_header or fb.db is None, proc=obj.key if obj.kind == "procedure" else None)

    def _new_file(self, path: str, db: str | None, capacity: int) -> FileBuf:
        use_header = db is not None and self.chance("use_header")
        fb = FileBuf(path, db, use_header, capacity, self.chance("crlf"), self.chance("bom"))
        if use_header:
            fb.chunks.append(f"USE {self.rng.choice([db, '[' + db + ']'])};")
        self.files[path] = fb
        return fb

    def _bundle(self, folder: str, db: str | None) -> FileBuf:
        fb = self.open_bundles.get(folder)
        if fb is None:
            self.bundle_counter[folder] += 1
            n = self.bundle_counter[folder]
            path = (f"deploy/release_{n:03d}.sql" if db is None
                    else f"{folder}/Scripts/deploy_batch_{n:03d}.sql")
            fb = self._new_file(path, db, capacity=self.rng.randint(4, 25))
            self.open_bundles[folder] = fb
        return fb

    def emit(self, obj: DbObject, text: str, near: DbObject | None = None) -> None:
        if obj.file is None:
            self.place(obj, near)
        fb = self.files[obj.file]
        if fb.db is None and fb.current_use != obj.db:
            fb.chunks.append(f"USE [{obj.db}];")
            fb.current_use = obj.db
        drop_kind = {"table": "TABLE", "view": "VIEW", "procedure": "PROCEDURE", "function": "FUNCTION"}[obj.kind]
        if self.chance("drop_preamble"):
            type_code = {"table": "U", "view": "V", "procedure": "P", "function": "FN"}[obj.kind]
            fb.chunks.append(f"IF OBJECT_ID(N'{obj.schema}.{obj.name}', N'{type_code}') IS NOT NULL\n"
                             f"    DROP {drop_kind} {obj.schema}.{obj.name};")
        if obj.kind != "table" and self.chance("ssms_header"):
            fb.chunks.append("SET ANSI_NULLS ON")
            fb.chunks.append("SET QUOTED_IDENTIFIER ON")
        fb.chunks.append(text.rstrip())

    def write_files(self) -> None:
        if self.out.exists():
            shutil.rmtree(self.out)
        self.out.mkdir(parents=True)
        # Legacy files first with the main stream, extension files after with their own: the bytes of the
        # legacy files do not depend on how many extension files exist.
        for path in sorted(p for p in self.files if p not in self.ext_paths):
            self._write_file(path, self.rng)
        for path in sorted(self.ext_paths):
            self._write_file(path, self.ext_rng)

    def _write_file(self, path: str, rng: random.Random) -> None:
        fb = self.files[path]
        parts = []
        for i, chunk in enumerate(fb.chunks):
            parts.append(chunk)
            last = i == len(fb.chunks) - 1
            if not last or rng.random() > 0.15:
                parts.append(rng.choice(["GO", "GO", "GO", "go", "GO "]))
        text = "\n".join(parts) + "\n"
        if fb.crlf:
            text = text.replace("\n", "\r\n")
        data = text.encode("utf-8")
        if fb.bom:
            data = b"\xef\xbb\xbf" + data
        target = self.out / path
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_bytes(data)

    # -- DDL ---------------------------------------------------------------
    def table_ddl(self, obj: DbObject) -> str:
        lines = []
        for col in obj.columns:
            name = self.cid(col.name)
            if col.computed:
                lines.append(f"{name} AS ({col.computed})")
            elif col.identity:
                lines.append(f"{name} {col.sqltype} IDENTITY(1,1) NOT NULL")
            elif col.name == "load_ts":
                lines.append(f"{name} datetime2(3) NOT NULL DEFAULT (SYSDATETIME())")
            else:
                null = " NOT NULL" if col.name == "rec_id" else " NULL"
                lines.append(f"{name} {col.sqltype}{null}")
        name = self.decl_name(obj)
        if self.rng.random() < 0.05:
            name = f"{self.cid(obj.db)}.{name}"
        body = ",\n    ".join(lines)
        key = next((c for c in obj.columns if c.identity), None) or obj.col("rec_id")
        constraint = ""
        if key is not None and self.rng.random() < 0.5:
            constraint = f",\n    CONSTRAINT pk_{obj.name} PRIMARY KEY CLUSTERED ({key.name})"
        return f"CREATE TABLE {name} (\n    {body}{constraint}\n);"

    def emit_table(self, obj: DbObject, near: DbObject | None = None) -> None:
        if not obj.defined:
            return
        if near is not None and not self.chance("module_file"):
            near = None
        self.place(obj, near)
        self.emit(obj, self.table_ddl(obj))
        for col in obj.columns:
            if col.computed:
                inputs = [(f"{obj.key}.{name}", "value", frozenset()) for name in col.computed_from]
                self.truth.write(f"{obj.key}.{col.name}", inputs, {"computed_column"}, f"ddl:{obj.key}")

    def proc_text(self, obj: DbObject, params: list[str], body: Body) -> str:
        verb = self.rng.choices(["CREATE PROCEDURE", "CREATE OR ALTER PROCEDURE", "CREATE PROC", "ALTER PROCEDURE"],
                                [55, 25, 12, 8])[0]
        head = f"{verb} {self.decl_name(obj)}"
        if params:
            head += "\n" + ",\n".join("    " + p for p in params)
        obj.statements = body.statements + 1
        self.statement_count += obj.statements
        if self.chance("no_begin_end"):
            lines = [line[4:] if line.startswith("    ") else line for line in body.lines]
            return f"{head}\nAS\nSET NOCOUNT ON;\n" + "\n".join(lines)
        return f"{head}\nAS\nBEGIN\n    SET NOCOUNT ON;\n" + "\n".join(body.lines) + "\nEND"

    # -- expressions -------------------------------------------------------
    def colref(self, src: Src, col: Column, single: bool) -> tuple[str, bool]:
        text, styled = self.ident(col.name)
        if single and self.chance("bare_column"):
            self._bare = True
            return text, styled
        return f"{src.alias}.{text}", styled

    def make_expr(self, ctx: Ctx, srcs: list[Src], picker: Picker) -> Expr:
        self._bare = False
        expr = self._make_expr(ctx, srcs, picker)
        expr.bare = self._bare
        return expr

    def _make_expr(self, ctx: Ctx, srcs: list[Src], picker: Picker) -> Expr:
        single = len(srcs) == 1
        inputs: list = []
        feats: set = set()

        def r(item, role="value") -> str:
            src, col = item
            text, styled = self.colref(src, col, single)
            extra = {"bracketed_or_mixed_case"} if styled else set()
            inputs.append((f"{src.prefix}.{col.name}", role, src.feats | extra))
            return text

        kinds = ["pass", "rename", "case", "coalesce", "isnull", "arith", "cast", "iif", "concat", "const",
                 "udf", "subquery"]
        weights = [30, 14, 10, 9, 6, 12, 5, 3, 3, 2, 100 * self.rates["udf"], 100 * self.rates["scalar_subquery"]]
        kind = self.rng.choices(kinds, weights)[0]
        num = ("num", "int")
        if kind in ("case", "coalesce", "arith", "iif"):
            a = picker.pick(num)
            b = picker.pick(num, exclude=(a,)) if a else None
            if not a or not b:
                kind = "pass"
        if kind == "pass" or kind == "rename":
            item = picker.pick(None)
            if item is None:
                kind = "const"
            else:
                return Expr(r(item), inputs, {"rename"} if kind == "rename" else set(), item[1].typ, kind,
                            item[1].name, plain=item[1].name if kind == "pass" else None)
        if kind == "case":
            k = picker.pick(None, exclude=(a, b)) or a
            cond = {"str": f"{r(k, 'condition')} IN ('A', 'X')", "num": f"{r(k, 'condition')} > 0",
                    "int": f"{r(k, 'condition')} >= 1", "date": f"{r(k, 'condition')} IS NULL",
                    "flag": f"{r(k, 'condition')} = 1"}[k[1].typ]
            first, second = r(a), r(b)
            tail = self.rng.choice([f" ELSE {second}", f" WHEN {second} IS NOT NULL THEN {second}"])
            sql = f"CASE WHEN {cond} THEN {first}{tail} END"
            feats.add("case")
            return Expr(sql, inputs, feats, "num", kind, a[1].name)
        if kind == "coalesce":
            sql = f"COALESCE({r(a)}, {r(b)}, 0)"
            return Expr(sql, inputs, {"coalesce"}, "num", kind, a[1].name)
        if kind == "iif":
            k = picker.pick(None, exclude=(a, b)) or a
            sql = f"IIF({r(k, 'condition')} IS NULL, {r(a)}, {r(b)})"
            return Expr(sql, inputs, {"iif"}, "num", kind, a[1].name)
        if kind == "arith":
            x, y = r(a), r(b)
            sql = self.rng.choice([f"{x} * {y}", f"{x} - {y}", f"{x} + {y}",
                                   f"ROUND({x} / NULLIF({y}, 0), 4)", f"({x} - {y}) * 1.05"])
            return Expr(sql, inputs, {"arithmetic"}, "num", kind, a[1].name)
        if kind == "isnull":
            item = picker.pick(num) or picker.pick(None)
            if item is None:
                kind = "const"
            else:
                default = "0" if item[1].typ in num else "''" if item[1].typ == "str" else "NULL"
                return Expr(f"ISNULL({r(item)}, {default})", inputs, {"isnull"}, item[1].typ, kind, item[1].name)
        if kind == "cast":
            item = picker.pick(None)
            if item is None:
                kind = "const"
            elif item[1].typ in num:
                return Expr(f"CAST({r(item)} AS decimal(18,2))", inputs, {"cast"}, "num", kind, item[1].name)
            else:
                return Expr(f"CONVERT(varchar(30), {r(item)})", inputs, {"cast"}, "str", kind, item[1].name)
        if kind == "concat":
            a = picker.pick(("str",))
            b = picker.pick(("str",), exclude=(a,)) if a else None
            if a and b:
                sql = self.rng.choice([f"CONCAT({r(a)}, '-', {r(b)})", f"{r(a)} + N'|' + {r(b)}"])
                return Expr(sql, inputs, {"concat"}, "str", kind, a[1].name)
            kind = "pass" if a else "const"
            if a:
                return Expr(r(a), inputs, set(), "str", "pass", a[1].name, plain=a[1].name)
        if kind == "udf":
            item = picker.pick(None)
            if item is None:
                kind = "const"
            else:
                fn = self.udfs["clean" if item[1].typ == "str" else "round"]
                region = next(((s_, c_) for s_ in srcs for c_ in s_.cols if c_.name == "region_cd"), None)
                if region is not None and self.rng.random() < 0.25:
                    fn, item = self.udfs["weight"], region
                fn_sql, ffeats = self.ref(fn, ctx, allow_one_part=False)
                feats = {"scalar_udf"} | ({"udf_reads_table"} if fn.name == "fn_region_weight" else set())
                sql = f"{fn_sql}({r(item)})"
                if fn.name == "fn_region_weight":
                    inputs.append((f"{self.region_weights.key}.weight_factor", "value", frozenset({"udf_reads_table"})))
                return Expr(sql, inputs, feats | set(ffeats), "num" if fn.name != "fn_clean_code" else "str", kind,
                            item[1].name)
        if kind == "subquery":
            region = next(((s, c) for s in srcs for c in s.cols if c.name == "region_cd"), None)
            if region is None:
                kind = "const"
            else:
                rm_sql, rfeats = self.ref(self.region_map, ctx)
                text, _ = self.colref(region[0], region[1], False)
                inputs.append((f"{self.region_map.key}.region_weight", "value", rfeats))
                sql = (f"(SELECT MAX(rmx.region_weight) FROM {rm_sql} AS rmx "
                       f"WHERE rmx.region_cd = {text})")
                return Expr(sql, inputs, {"scalar_subquery"}, "num", kind, "region")
        # const
        sql, typ = self.rng.choice([("SYSDATETIME()", "date"), ("CAST(NULL AS int)", "int"), ("N'N/A'", "str"),
                                    ("@run_id", "int")])
        return Expr(sql, inputs, set(), typ, "const", "batch")

    def out_name(self, expr: Expr, used: set) -> str:
        if expr.kind == "pass" and expr.base not in used:
            used.add(expr.base)
            return expr.base
        stem = "_".join(expr.base.split("_")[:2])
        suffix = self.rng.choice(SUFFIX.get(expr.kind, ["_v"]))
        name = stem + suffix
        n = 2
        while name in used:
            name = f"{stem}{suffix}{n}"
            n += 1
        used.add(name)
        if expr.plain and name != expr.plain:
            expr.plain = None
            expr.feats.add("rename")
        return name

    def project(self, ctx: Ctx, srcs: list[Src], n_extra: int, pool_srcs: list[Src] | None = None,
                carry=CARRY) -> list[tuple[str, Expr]]:
        used: set = set()
        out = []
        primary = srcs[0]
        single = len(srcs) == 1
        for name in carry:
            col = primary.col(name)
            if col is None:
                continue
            self._bare = False
            text, styled = self.colref(primary, col, single)
            out.append((name, Expr(text, [(f"{primary.prefix}.{name}", "value", primary.feats)], set(),
                                   col.typ, "pass", name, plain=name, bare=self._bare)))
            used.add(name)
        pool = [(s, c) for s in (pool_srcs or srcs) for c in s.cols
                if c.name not in carry and c.name not in NO_PROPAGATE and not c.identity]
        self.rng.shuffle(pool)
        picker = Picker(pool, self.rng)
        for _ in range(n_extra):
            expr = self.make_expr(ctx, srcs, picker)
            out.append((self.out_name(expr, used), expr))
        if sum(1 for _, e in out if e.typ in ("num", "int") and e.inputs) < 2:
            for s, c in pool:
                if c.typ == "num" and c.name not in used:
                    text, _ = self.colref(s, c, False)
                    out.append((c.name, Expr(text, [(f"{s.prefix}.{c.name}", "value", s.feats)], set(),
                                             "num", "pass", c.name, plain=c.name)))
                    used.add(c.name)
                    if sum(1 for _, e in out if e.typ in ("num", "int") and e.inputs) >= 2:
                        break
        return out

    def select_items(self, items: list[tuple[str, Expr]], pad: str = "       ") -> str:
        parts = []
        for name, expr in items:
            ident = self.cid(name)
            if expr.plain == name and self.rng.random() < 0.6:
                parts.append(expr.sql)
            elif self.chance("tsql_alias"):
                parts.append(f"{ident} = {expr.sql}")
            else:
                parts.append(f"{expr.sql} AS {ident}")
        return (",\n" + pad).join(parts)

    def record(self, target_prefix: str, items: list[tuple[str, Expr]], ctx: Ctx, feats: set, writer: str,
               targets: list[str] | None = None) -> None:
        for i, (name, expr) in enumerate(items):
            tname = (targets[i] if targets else name).lower()
            self.truth.write(f"{target_prefix}.{tname}", expr.inputs, set(feats) | expr.feats | ctx.flow_feats(),
                             writer)

    def passthrough(self, src: Src, names: list[str]) -> list[tuple[str, Expr]]:
        out = []
        for name in names:
            col = src.col(name)
            text, _ = self.colref(src, col, False)
            out.append((name, Expr(text, [(f"{src.prefix}.{name}", "value", src.feats)], set(), col.typ,
                                   "pass", name, plain=name)))
        return out

    def cols_from(self, items: list[tuple[str, Expr]]) -> list[Column]:
        return [self.column(name, expr.typ) for name, expr in items]

    # -- noise -------------------------------------------------------------
    def noise(self, body: Body, ctx: Ctx, obj: DbObject) -> None:
        if self.chance("sql_comment"):
            other = self.rng.choice(self.objects)
            body.raw(self.rng.choice([
                f"-- legacy: INSERT INTO {other.schema}.{other.name} SELECT * FROM {obj.schema}.{obj.name}_old;",
                f"/* Kept for reference (do not run):\n   UPDATE {other.schema}.{other.name} SET rec_id = rec_id WHERE 1 = 0;\n"
                f"   EXEC {obj.schema}.usp_removed_step @run_id = 1;\n   GO\n*/",
                f"-- TODO: MERGE {other.db}.{other.schema}.{other.name} instead of truncate/reload",
            ]))
        if self.chance("sql_string"):
            other = self.rng.choice(self.objects)
            msg = self.rng.choice([
                f"INSERT INTO {other.schema}.{other.name} SELECT * FROM landing.unknown_feed",
                f"MERGE {other.schema}.{other.name} USING #missing ON 1 = 0; EXEC dbo.usp_cleanup",
                f"SELECT * FROM {other.db}.{other.schema}.{other.name} WHERE 1 = 1 -- check",
            ]).replace("'", "''")
            log = self.ref(self.log_tables[ctx.db], ctx)[0]
            body.stmt(f"INSERT INTO {log} (proc_name, step_name, message_txt, logged_at)\n"
                      f"VALUES (N'{obj.schema}.{obj.name}', N'start', N'{msg}', SYSDATETIME());")
        if self.chance("dynamic_noise"):
            body.stmt("DECLARE @noise_sql nvarchar(max);")
            body.stmt(f"SET @noise_sql = N'UPDATE STATISTICS ' + QUOTENAME(N'{obj.schema}') + N'.' + QUOTENAME(N'{obj.name}');")
            body.stmt(self.rng.choice(["EXEC (@noise_sql);", "EXEC sp_executesql @noise_sql;",
                                       "EXEC (N'SELECT 1 AS ok');"]))
        if self.chance("unparseable_noise"):
            choice = self.rng.randrange(3)
            if choice == 0:
                body.stmt("WAITFOR DELAY '00:00:01';")
            elif choice == 1:
                body.stmt("DECLARE @retry int = 0;")
                body.stmt("SET @retry += 1;")
            else:
                body.stmt(f"UPDATE STATISTICS {obj.schema}.{obj.name} WITH SAMPLE 10 PERCENT;")

    def begin_try(self, body: Body, ctx: Ctx) -> bool:
        if not self.chance("try_catch"):
            return False
        body.raw("BEGIN TRY")
        body.depth += 1
        ctx.flow.append("try_catch")
        return True

    def end_try(self, body: Body, ctx: Ctx, used: bool, obj: DbObject) -> None:
        if not used:
            return
        ctx.flow.remove("try_catch")
        body.depth -= 1
        body.raw("END TRY")
        body.raw("BEGIN CATCH")
        body.depth += 1
        log = self.ref(self.log_tables[ctx.db], ctx)[0]
        body.stmt(f"INSERT INTO {log} (proc_name, step_name, message_txt, logged_at)\n"
                  f"VALUES (N'{obj.name}', N'error', ERROR_MESSAGE(), SYSDATETIME());")
        body.stmt("THROW;")
        body.depth -= 1
        body.raw("END CATCH")

    # -- global objects ----------------------------------------------------
    def build_globals(self) -> None:
        self.log_tables = {}
        for db in self.dbs:
            log = self.new_object(db, "dbo", "etl_run_log", "table", "ops", columns=[
                self.column("log_id", "int", identity=True), self.column("proc_name", "str"),
                self.column("step_name", "str"), self.column("message_txt", "str"), self.column("logged_at", "date")])
            self.log_tables[db] = log
            self.emit_table(log)
        core = self.core_db
        self.region_map = self.new_object(core, "ref", "region_map", "table", "reference", columns=[
            self.column("region_cd", "str"), self.column("region_name", "str"), self.column("region_group", "str"),
            self.column("region_weight", "num")])
        self.region_weights = self.new_object(core, "dbo", "region_weights", "table", "reference", columns=[
            self.column("region_cd", "str"), self.column("weight_factor", "num"), self.column("valid_from", "date")])
        self.calendar = self.new_object(core, "dbo", "calendar", "table", "reference", columns=[
            self.column("cal_date", "date"), self.column("fiscal_period", "str"), self.column("is_holiday", "flag")])
        for obj in (self.region_map, self.region_weights, self.calendar):
            self.emit_table(obj)
        self.udfs = {}
        specs = [
            ("clean", "fn_clean_code", "@v nvarchar(200)", "nvarchar(200)", "    RETURN UPPER(LTRIM(RTRIM(@v)));"),
            ("round", "fn_round_amt", "@a decimal(19,4)", "decimal(18,2)",
             "    RETURN CAST(ROUND(ISNULL(@a, 0), 2) AS decimal(18,2));"),
            ("weight", "fn_region_weight", "@region_cd nvarchar(40)", "decimal(18,4)", None),
        ]
        for key, name, params, returns, body in specs:
            fn = self.new_object(core, "dbo", name, "function", "function")
            ctx = self.place(fn)
            if body is None:
                rw = self.ref(self.region_weights, ctx)[0]
                body = (f"    DECLARE @w decimal(18,4);\n    SELECT @w = weight_factor FROM {rw} "
                        f"WHERE region_cd = @region_cd;\n    RETURN ISNULL(@w, 1.0);")
            text = f"CREATE FUNCTION {self.decl_name(fn)} ({params})\nRETURNS {returns}\nAS\nBEGIN\n{body}\nEND"
            self.emit(fn, text)
            self.udfs[key] = fn

    # -- one subject area ----------------------------------------------------
    def build_subject(self, index: int) -> None:
        rng = self.rng
        subj = f"{NOUNS[index % len(NOUNS)]}_{index:03d}"
        mart_db = self.mart_dbs[index % len(self.mart_dbs)]
        info: dict = {"subject": subj}

        # landing
        cols_a = [self.column("rec_id", "int"), self.column("src_code", "str"), self.column("status_cd", "str"),
                  self.column("amt_gross", "num"), self.column("amt_tax", "num"), self.column("qty", "int"),
                  self.column("unit_price", "num"), self.column("region_cd", "str"), self.column("event_dt", "date"),
                  self.column("load_dt", "date"), self.column("is_deleted", "flag")]
        cols_a += [self.column(n, t) for n, t in rng.sample(ATTRS_A, rng.randint(0, 5))]
        cols_b = [self.column("rec_id", "int"), self.column("amt_gross", "num"), self.column("qty", "int"),
                  self.column("alt_code", "str"), self.column("status_cd", "str"), self.column("adj_amt", "num"),
                  self.column("event_dt", "date")]
        cols_b += [self.column(n, t) for n, t in rng.sample(ATTRS_B, rng.randint(0, 3))]
        land_a = self.new_object(self.stage_db, "landing", f"{subj}_src_a", "table", "landing", subj, cols_a)
        land_b = self.new_object(self.stage_db, "landing", f"{subj}_src_b", "table", "landing", subj, cols_b,
                                 defined=not self.chance("missing_landing_ddl"))
        for obj in (land_a, land_b):
            self.emit_table(obj)
        if not land_b.defined:
            for col in land_b.columns:
                self.truth.node_feats[f"{land_b.key}.{col.name}"].add("missing_ddl")

        stg_table = self.build_staging(subj, land_a, land_b)
        core_table = self.build_core(subj, stg_table)
        base, wide = self.build_views(subj, core_table)
        info.update(base=base, wide=wide, core=core_table, landing=[land_a, land_b])
        fact = self.build_mart(subj, mart_db, base, wide)
        info["fact"] = fact
        self.table_targets.append(fact.key)
        if self.chance("summary_table"):
            info["summary"] = self.build_summary(subj, mart_db, fact)
        self.subjects.append(info)

    def build_staging(self, subj: str, land_a: DbObject, land_b: DbObject) -> DbObject:
        rng = self.rng
        proc = self.new_object(self.stage_db, "stg", f"usp_load_{subj}", "procedure", "staging", subj)
        ctx = self.place(proc)
        body = Body()
        self.noise(body, ctx, proc)
        used_try = self.begin_try(body, ctx)
        a, b = self.src(land_a, "a", ctx), self.src(land_b, "b", ctx)
        raw_name = f"#{subj}_raw"
        items = self.project(ctx, [a, b], rng.randint(5, 10))
        body.stmt(f"IF OBJECT_ID('tempdb..{raw_name}') IS NOT NULL DROP TABLE {raw_name};")
        join = rng.choice(["LEFT JOIN", "LEFT OUTER JOIN", "INNER JOIN"])
        body.stmt(f"SELECT {self.select_items(items)}\nINTO {raw_name}\nFROM {a.sql} AS a{a.hint}\n"
                  f"{join} {b.sql} AS b{b.hint} ON b.rec_id = a.rec_id\n"
                  f"WHERE a.is_deleted = 0\n  AND a.event_dt >= ISNULL(@from_dt, '19000101');")
        self.record(f"temp:{proc.key}::{raw_name}", items, ctx, {"select_into_temp", "join", "filter"}, proc.key)
        raw = self.temp_src(proc.key, raw_name, "r", self.cols_from(items))

        # CTE + insert
        cte_prefix = f"cte:{proc.key}:{len(body.lines)}::dedup"
        star = self.chance("cte_star")
        cte_items = self.passthrough(raw, [c.name for c in raw.cols])
        cte_sel = "r.*" if star else self.select_items(cte_items, pad="        ")
        cte_feats = {"cte"} | ({"select_star"} if star else set())
        self.record(cte_prefix, cte_items, ctx, cte_feats, proc.key)
        cte = Src("d", "dedup", raw.cols, cte_prefix, frozenset(), "cte")
        out = self.project(ctx, [cte], rng.randint(4, 9))
        stg_table = self.new_object(self.stage_db, "stg", subj, "table", "staging", subj,
                                    self.cols_from(out) + [self.column("load_ts", "date")])
        loop = self.chance("while_loop")
        target_sql = self.ref(stg_table, ctx)[0]
        names = ", ".join(self.cid(n) for n, _ in out)
        batch_filter = "\n  AND d.rec_id % 4 = @batch" if loop else ""
        stmt = (f";WITH dedup AS (\n    SELECT {cte_sel},\n"
                f"           ROW_NUMBER() OVER (PARTITION BY r.rec_id ORDER BY r.event_dt DESC) AS rn\n"
                f"    FROM {raw_name} AS r\n)\n"
                f"INSERT INTO {target_sql} ({names}, load_ts)\n"
                f"SELECT {self.select_items(out)},\n       SYSDATETIME()\nFROM dedup AS d\nWHERE d.rn = 1{batch_filter};")
        if loop:
            body.stmt("DECLARE @batch int = 0;")
            body.push("WHILE @batch < 4")
            ctx.flow.append("while_loop")
        body.stmt(stmt)
        self.record(stg_table.key, out, ctx, {"insert_select_target_list", "cte"}, proc.key)
        if loop:
            ctx.flow.remove("while_loop")
            body.stmt("SET @batch = @batch + 1;")
            body.pop()
        self.end_try(body, ctx, used_try, proc)
        self.emit(proc, self.proc_text(proc, ["@from_dt date = NULL", "@run_id int = NULL"], body))
        self.emit_table(stg_table, near=proc)
        self.live_procs[proc.db].append(proc)
        return stg_table

    def build_core(self, subj: str, stg_table: DbObject) -> DbObject:
        rng = self.rng
        proc = self.new_object(self.core_db, "core", f"usp_load_{subj}", "procedure", "core", subj)
        ctx = self.place(proc)
        body = Body()
        self.noise(body, ctx, proc)
        used_try = self.begin_try(body, ctx)
        s = self.src(stg_table, "s", ctx)
        srcs = [s]
        ref_join = ""
        if self.chance("ref_join"):
            ref_obj = rng.choice([self.region_map, self.region_weights])
            rm = self.src(ref_obj, "rm", ctx)
            srcs.append(rm)
            ref_join = f"\nLEFT JOIN {rm.sql} AS rm{rm.hint} ON rm.region_cd = s.region_cd"
        pool = [s] + srcs[1:]
        items = self.project(ctx, srcs, rng.randint(4, 9), pool_srcs=pool)
        cols = self.cols_from(items)
        core_cols = [self.column(f"{subj}_sk", "int", identity=True)] + cols
        if self.chance("computed_column"):
            nums = [c for c in cols if c.typ == "num"]
            if len(nums) >= 2:
                comp = Column(f"{nums[0].name}_x_{nums[1].name}"[:60], "num", "decimal(18,4)",
                              computed=f"{nums[0].name} * {nums[1].name}",
                              computed_from=(nums[0].name, nums[1].name))
                core_cols.insert(rng.randint(2, len(core_cols)), comp)
        core_cols.append(self.column("load_ts", "date"))
        core_table = self.new_object(self.core_db, "core", subj, "table", "core", subj, core_cols)
        target_sql = self.ref(core_table, ctx)[0]
        names = [n for n, _ in items]
        from_clause = f"FROM {s.sql} AS s{s.hint}{ref_join}\nWHERE s.rec_id IS NOT NULL"

        def emit_insert(items_, feats_extra=()):
            positional = self.chance("positional_insert")
            if positional:
                order = [c.name for c in core_cols if not c.identity and not c.computed and c.name != "load_ts"]
                by_name = dict(items_)
                ordered = [(n, by_name[n]) for n in order]
                body.stmt(f"INSERT INTO {target_sql}\nSELECT {self.select_items(ordered)},\n       SYSDATETIME()\n"
                          f"{from_clause};")
                self.record(core_table.key, ordered, ctx, {"insert_select_positional", "identity_skip"} | set(feats_extra),
                            proc.key)
            elif self.chance("wildcard_insert"):
                tmp = f"#{subj}_core_work"
                body.stmt(f"SELECT {self.select_items(items_)}\nINTO {tmp}\n{from_clause};")
                self.record(f"temp:{proc.key}::{tmp}", items_, ctx, {"select_into_temp"}, proc.key)
                work = self.temp_src(proc.key, tmp, "w", self.cols_from(items_))
                body.stmt(f"INSERT INTO {target_sql} ({', '.join(self.cid(n) for n in names)})\nSELECT * FROM {tmp};")
                self.record(core_table.key, self.passthrough(work, names), ctx,
                            {"wildcard_insert", "select_star"} | set(feats_extra), proc.key)
            else:
                body.stmt(f"INSERT INTO {target_sql} ({', '.join(self.cid(n) for n in names)})\n"
                          f"SELECT {self.select_items(items_)}\n{from_clause};")
                self.record(core_table.key, items_, ctx, {"insert_select_target_list"} | set(feats_extra), proc.key)

        def emit_merge(items_):
            derived = bool(ref_join) or any(e.bare for _, e in items_) or rng.random() < 0.5
            if derived:
                dprefix = f"derived:{proc.key}:{len(body.lines)}::src"
                using = f"(\n    SELECT {self.select_items(items_, pad='           ')}\n    {from_clause.replace(chr(10), chr(10) + '    ')}\n) AS src"
                self.record(dprefix, items_, ctx, {"derived_table"}, proc.key)
                dsrc = Src("src", "src", self.cols_from(items_), dprefix, frozenset(), "derived")
                mapped = self.passthrough(dsrc, names)
            else:
                using = f"{s.sql} AS src"
                mapped = [(n, Expr(re.sub(r"(?<![\w.\]])s\.", "src.", e.sql), e.inputs, e.feats, e.typ, e.kind,
                                   e.base, e.plain)) for n, e in items_]
            sets = ",\n        ".join(f"tgt.{self.cid(n)} = {e.sql}" for n, e in mapped if n != "rec_id")
            values = ", ".join(e.sql for _, e in mapped)
            body.stmt(f"MERGE {target_sql} AS tgt\nUSING {using}\n    ON tgt.rec_id = src.rec_id\n"
                      f"WHEN MATCHED THEN\n    UPDATE SET {sets},\n        tgt.load_ts = SYSDATETIME()\n"
                      f"WHEN NOT MATCHED BY TARGET THEN\n    INSERT ({', '.join(self.cid(n) for n in names)}, load_ts)\n"
                      f"    VALUES ({values}, SYSDATETIME());")
            self.record(core_table.key, [(n, e) for n, e in mapped if n != "rec_id"], ctx, {"merge_update"}, proc.key)
            self.record(core_table.key, mapped, ctx, {"merge_insert"}, proc.key)

        if self.chance("if_else"):
            alt = []
            pool_alt = Picker([(src_, c) for src_ in pool for c in src_.cols if c.name not in CARRY
                               and c.name not in NO_PROPAGATE and not c.identity], rng)
            for n, e in items:
                if n in CARRY or rng.random() < 0.5:
                    alt.append((n, e))
                else:
                    alt.append((n, self.make_expr(ctx, srcs, pool_alt)))
            body.push("IF @full_reload = 1")
            ctx.flow.append("if_else")
            body.stmt(f"TRUNCATE TABLE {target_sql};")
            emit_insert(items)
            body.pop()
            body.push("ELSE")
            emit_merge(alt)
            ctx.flow.remove("if_else")
            body.pop()
        elif rng.random() < 0.5:
            emit_merge(items)
        else:
            body.stmt(f"TRUNCATE TABLE {target_sql};")
            emit_insert(items)
        self.end_try(body, ctx, used_try, proc)
        self.emit(proc, self.proc_text(proc, ["@full_reload bit = 0", "@run_id int = NULL"], body))
        self.emit_table(core_table, near=proc)
        self.live_procs[proc.db].append(proc)

        if self.chance("patch_writer"):
            self.build_patch(subj, core_table)
        return core_table

    def build_patch(self, subj: str, core_table: DbObject) -> None:
        land_c = self.new_object(self.stage_db, "landing", f"{subj}_src_c", "table", "landing", subj, [
            self.column("rec_id", "int"), self.column("override_amt", "num"), self.column("override_reason", "str"),
            self.column("event_dt", "date")])
        self.emit_table(land_c)
        proc = self.new_object(self.core_db, "core", f"usp_patch_{subj}", "procedure", "core", subj)
        ctx = self.place(proc)
        body = Body()
        target = self.rng.choice([c for c in core_table.columns if c.typ == "num" and not c.computed] or
                                 [core_table.columns[-2]])
        p = self.src(land_c, "p", ctx)
        expr_sql = self.rng.choice(["p.override_amt", "COALESCE(p.override_amt, c.{0})".format(target.name)])
        inputs = [(f"{land_c.key}.override_amt", "value", p.feats)]
        if "COALESCE" in expr_sql:
            inputs.append((f"{core_table.key}.{target.name}", "value", frozenset()))
        body.stmt(f"UPDATE c\n   SET c.{self.cid(target.name)} = {expr_sql}\nFROM {self.ref(core_table, ctx)[0]} AS c\n"
                  f"INNER JOIN {p.sql} AS p{p.hint} ON p.rec_id = c.rec_id\nWHERE p.override_reason IS NOT NULL;")
        self.truth.write(f"{core_table.key}.{target.name}", inputs, {"update_from", "patch_writer"}, proc.key)
        self.emit(proc, self.proc_text(proc, ["@run_id int = NULL"], body))
        self.live_procs[proc.db].append(proc)

    def view_text(self, view: DbObject, items, from_sql: str, where: str, ctx: Ctx) -> str:
        verb = self.rng.choice(["CREATE VIEW", "CREATE VIEW", "CREATE OR ALTER VIEW", "ALTER VIEW"])
        if self.chance("view_column_list"):
            cols = ", ".join(self.cid(n) for n, _ in items)
            sel = ",\n       ".join(e.sql for _, e in items)
            text = f"{verb} {self.decl_name(view)} ({cols})\nAS\nSELECT {sel}\n{from_sql}{where};"
            return text, {"view_column_list"}
        return f"{verb} {self.decl_name(view)}\nAS\nSELECT {self.select_items(items)}\n{from_sql}{where};", set()

    def build_views(self, subj: str, core_table: DbObject) -> tuple[DbObject, DbObject]:
        rng = self.rng
        base_src_obj = core_table
        if self.chance("select_star_view"):
            v_all = self.new_object(self.core_db, "rpt", f"v_{subj}_all", "view", "view", subj, list(core_table.columns))
            ctx = self.place(v_all)
            src_sql = self.ref(core_table, ctx)[0]
            self.emit(v_all, f"CREATE VIEW {self.decl_name(v_all)}\nAS\nSELECT *\nFROM {src_sql};")
            self.statement_count += 1
            star_src = self.src(core_table, "c", ctx, hint=False)
            self.record(v_all.key, self.passthrough(star_src, [c.name for c in core_table.columns]), ctx,
                        {"view", "select_star"}, v_all.key)
            base_src_obj = v_all
        base = self.new_object(self.core_db, "rpt", f"v_{subj}_base", "view", "view", subj)
        ctx = self.place(base)
        c = self.src(base_src_obj, "c", ctx, hint=False)
        srcs = [c]
        from_sql = f"FROM {c.sql} AS c{c.hint}"
        if rng.random() < 0.4:
            cal = self.src(self.calendar, "cal", ctx, hint=False)
            srcs.append(cal)
            from_sql += f"\nLEFT JOIN {cal.sql} AS cal{cal.hint} ON cal.cal_date = CAST(c.event_dt AS date)"
        items = self.project(ctx, srcs, rng.randint(4, 9))
        base.columns = self.cols_from(items)
        text, extra = self.view_text(base, items, from_sql, "\nWHERE c.rec_id > 0", ctx)
        self.emit(base, text)
        self.statement_count += 1
        vfeats = {"view"} | extra | ({"view_on_view"} if base_src_obj.kind == "view" else set())
        self.record(base.key, items, ctx, vfeats, base.key)

        wide = self.new_object(self.core_db, "rpt", f"v_{subj}_wide", "view", "view", subj)
        ctx = self.place(wide)
        b = self.src(base, "b", ctx)
        srcs = [b]
        from_sql = f"FROM {b.sql} AS b{b.hint}"
        others = [s for s in self.subjects if s.get("base")]
        items = self.project(ctx, srcs, rng.randint(3, 8))
        if others and self.chance("cross_subject_join"):
            other = rng.choice(others)["base"]
            o = self.src(other, "o", ctx)
            from_sql += f"\nLEFT JOIN {o.sql} AS o{o.hint} ON o.rec_id = b.rec_id"
            used = {n for n, _ in items}
            for col in [c_ for c_ in o.cols if c_.name not in CARRY and c_.typ == "num"][:2]:
                name = f"{other.subject.split('_')[0]}_{col.name}"[:50]
                if name in used:
                    continue
                used.add(name)
                text_, _ = self.colref(o, col, False)
                items.append((name, Expr(text_, [(f"{o.prefix}.{col.name}", "value", o.feats)],
                                         {"rename", "cross_subject_join"}, col.typ,
                                         "rename", col.name)))
            srcs.append(o)
        wide.columns = self.cols_from(items)
        text, extra = self.view_text(wide, items, from_sql, "", ctx)
        self.emit(wide, text)
        self.statement_count += 1
        self.record(wide.key, items, ctx, {"view", "view_on_view"} | extra, wide.key)
        return base, wide

    def build_mart(self, subj: str, mart_db: str, base: DbObject, wide: DbObject) -> DbObject:
        rng = self.rng
        main = self.new_object(mart_db, "mart", f"usp_load_{subj}_fact", "procedure", "mart", subj)
        ctx = self.place(main)
        body = Body()
        self.noise(body, ctx, main)
        used_try = self.begin_try(body, ctx)
        stage_name = f"#{subj}_stage"
        nested = self.chance("nested_exec_temp")
        stage_prefix = f"temp:{main.key}::{stage_name}"
        fill = None
        if nested:
            fill = self.new_object(mart_db, "mart", f"usp_fill_{subj}_stage", "procedure", "mart", subj)
            fctx = self.place(fill)
            fbody = Body()
            w = self.src(wide, "w", fctx)
            items = self.project(fctx, [w], rng.randint(4, 9))
            names = ", ".join(self.cid(n) for n, _ in items)
            fbody.stmt(f"INSERT INTO {stage_name} ({names})\nSELECT {self.select_items(items)}\nFROM {w.sql} AS w{w.hint}\n"
                       f"WHERE w.event_dt >= @from_dt;")
            self.record(stage_prefix, items, fctx, {"caller_temp_nested_exec", "insert_select_target_list"}, fill.key)
            self.emit(fill, self.proc_text(fill, ["@from_dt date"], fbody))
            stage_cols = self.cols_from(items)
            coldefs = ",\n    ".join(f"{c.name} {c.sqltype}{' NOT NULL' if c.name == 'rec_id' else ' NULL'}"
                                     for c in stage_cols)
            body.stmt(f"CREATE TABLE {stage_name} (\n    {coldefs}\n);")
            body.stmt(f"EXEC {self.ref(fill, ctx, allow_one_part=False)[0]} @from_dt = @from_dt;")
        else:
            w = self.src(wide, "w", ctx)
            items = self.project(ctx, [w], rng.randint(4, 9))
            body.stmt(f"SELECT {self.select_items(items)}\nINTO {stage_name}\nFROM {w.sql} AS w{w.hint}\n"
                      f"WHERE w.event_dt >= @from_dt;")
            self.record(stage_prefix, items, ctx, {"select_into_temp"}, main.key)
            stage_cols = self.cols_from(items)
        stage = self.temp_src(main.key, stage_name, "s", stage_cols)

        calc_name = f"#{subj}_calc"
        calc_items = self.project(ctx, [stage], rng.randint(4, 8))
        body.stmt(f"SELECT {self.select_items(calc_items)}\nINTO {calc_name}\nFROM {stage_name} AS s;")
        self.record(f"temp:{main.key}::{calc_name}", calc_items, ctx, {"select_into_temp"}, main.key)
        calc = self.temp_src(main.key, calc_name, "c", self.cols_from(calc_items))
        calc_names = [n for n, _ in calc_items]

        extra_cols = [self.column("region_label", "str")]
        features = {
            "insert_exec": self.chance("insert_exec"),
            "dynamic_sql": self.chance("dynamic_sql"),
            "cursor": self.chance("cursor"),
            "parse_error": self.chance("unparseable_writer"),
        }
        special = {"insert_exec": "bonus_amt", "dynamic_sql": "dyn_metric", "cursor": "cursor_adj_amt",
                   "parse_error": "batch_fix_amt"}
        extra_cols += [self.column(special[k], "num") for k, on in features.items() if on]
        clash = {c.name for c in extra_cols} & set(calc_names)
        if clash:
            raise RuntimeError(f"generator bug: fact column clash {sorted(clash)}")
        fact_cols = calc.cols + extra_cols + [self.column("load_ts", "date")]
        if self.chance("identity_fact"):
            fact_cols.insert(0, self.column("fact_id", "int", identity=True))
        fact = self.new_object(mart_db, "mart", f"{subj}_fact", "table", "mart", subj, fact_cols)
        fact_sql = self.ref(fact, ctx)[0]

        if features["insert_exec"]:
            extra = self.new_object(mart_db, "mart", f"usp_get_{subj}_extra", "procedure", "mart", subj)
            ectx = self.place(extra)
            ebody = Body()
            bsrc = self.src(base, "b", ectx)
            num = next((c for c in base.columns if c.typ == "num"), None) or base.columns[0]
            ebody.stmt(f"SELECT b.rec_id,\n       b.{self.cid(num.name)} * 0.1 AS bonus_amt\nFROM {bsrc.sql} AS b{bsrc.hint}\n"
                       f"WHERE b.event_dt >= @from_dt;")
            self.emit(extra, self.proc_text(extra, ["@from_dt date"], ebody))
            extra_name = f"#{subj}_extra"
            body.stmt(f"CREATE TABLE {extra_name} (rec_id int NOT NULL, bonus_amt decimal(18,4) NULL);")
            body.stmt(f"INSERT INTO {extra_name} (rec_id, bonus_amt)\n"
                      f"EXEC {self.ref(extra, ctx, allow_one_part=False)[0]} @from_dt = @from_dt;")
            eprefix = f"temp:{main.key}::{extra_name.lower()}"
            self.truth.write(f"{eprefix}.rec_id", [(f"{base.key}.rec_id", "value", bsrc.feats)], {"insert_exec"},
                             main.key)
            self.truth.write(f"{eprefix}.bonus_amt", [(f"{base.key}.{num.name}", "value", bsrc.feats)],
                             {"insert_exec", "arithmetic"} | ctx.flow_feats(), main.key)
            self.live_procs[mart_db].append(extra)

        # main write: MERGE, positional INSERT or wildcard INSERT
        roll = rng.random()
        if roll < self.rates["positional_insert"]:
            fill_values = [("x", Expr("NULL", [], set(), "num", "const", "x")) for _ in extra_cols]
            items = self.passthrough(calc, calc_names)
            sel = self.select_items(items) + "".join(f",\n       {e.sql}" for _, e in fill_values) + ",\n       SYSDATETIME()"
            body.stmt(f"TRUNCATE TABLE {fact_sql};")
            body.stmt(f"INSERT INTO {fact_sql}\nSELECT {sel}\nFROM {calc_name} AS c;")
            self.record(fact.key, items, ctx, {"insert_select_positional"} |
                        ({"identity_skip"} if fact_cols[0].identity else set()), main.key)
        elif roll < self.rates["positional_insert"] + self.rates["wildcard_insert"]:
            body.stmt(f"DELETE FROM {fact_sql} WHERE event_dt >= @from_dt;")
            body.stmt(f"INSERT INTO {fact_sql} ({', '.join(self.cid(n) for n in calc_names)})\nSELECT *\nFROM {calc_name};")
            self.record(fact.key, self.passthrough(calc, calc_names), ctx, {"wildcard_insert", "select_star"}, main.key)
        else:
            mapped = self.passthrough(Src("src", calc_name, calc.cols, calc.prefix, frozenset(), "temp"), calc_names)
            sets = ",\n        ".join(f"tgt.{self.cid(n)} = {e.sql}" for n, e in mapped if n != "rec_id")
            body.stmt(f"MERGE {fact_sql} AS tgt\nUSING {calc_name} AS src\n    ON tgt.rec_id = src.rec_id\n"
                      f"WHEN MATCHED THEN\n    UPDATE SET {sets}\n"
                      f"WHEN NOT MATCHED BY TARGET THEN\n    INSERT ({', '.join(self.cid(n) for n in calc_names)})\n"
                      f"    VALUES ({', '.join(e.sql for _, e in mapped)})\n"
                      f"WHEN NOT MATCHED BY SOURCE AND tgt.event_dt >= @from_dt THEN\n    DELETE;")
            self.record(fact.key, [(n, e) for n, e in mapped if n != "rec_id"], ctx, {"merge_update"}, main.key)
            self.record(fact.key, mapped, ctx, {"merge_insert"}, main.key)

        # region label from a reference table (UPDATE ... FROM, cross database)
        rm = self.src(self.region_map, "rm", ctx)
        body.stmt(f"UPDATE f\n   SET f.region_label = COALESCE(rm.region_name, rm.region_group, N'UNKNOWN')\n"
                  f"FROM {fact_sql} AS f\nINNER JOIN {rm.sql} AS rm{rm.hint} ON rm.region_cd = f.region_cd;")
        self.truth.write(f"{fact.key}.region_label",
                         [(f"{self.region_map.key}.region_name", "value", rm.feats),
                          (f"{self.region_map.key}.region_group", "value", rm.feats)],
                         {"update_from", "coalesce"} | ctx.flow_feats(), main.key)
        num_calc = [c for c in calc.cols if c.typ in ("num", "int") and c.name != "rec_id"] or calc.cols
        if features["insert_exec"]:
            body.stmt(f"UPDATE f\n   SET f.bonus_amt = e.bonus_amt\nFROM {fact_sql} AS f\n"
                      f"INNER JOIN #{subj}_extra AS e ON e.rec_id = f.rec_id;")
            self.truth.write(f"{fact.key}.bonus_amt", [(f"temp:{main.key}::#{subj}_extra.bonus_amt", "value", frozenset())],
                             {"update_from"} | ctx.flow_feats(), main.key)
        if features["dynamic_sql"]:
            col = rng.choice(num_calc)
            body.stmt("DECLARE @sql nvarchar(max), @target_col sysname = N'dyn_metric', "
                      f"@source_col sysname = N'{col.name}';")
            body.stmt(f"SET @sql = N'UPDATE f SET f.' + QUOTENAME(@target_col) + N' = c.' + QUOTENAME(@source_col)\n"
                      f"         + N' * 1.0 FROM mart.{fact.name} AS f INNER JOIN {calc_name} AS c ON c.rec_id = f.rec_id;';")
            body.stmt(rng.choice(["EXEC sp_executesql @sql;", "EXEC (@sql);", "EXECUTE sp_executesql @stmt = @sql;"]))
            self.truth.write(f"{fact.key}.dyn_metric", [(f"{calc.prefix}.{col.name}", "value", frozenset())],
                             {"dynamic_sql", "update_from"} | ctx.flow_feats(), main.key)
        if features["cursor"]:
            num = next((c for c in base.columns if c.typ == "num"), None) or base.columns[0]
            bsql = self.ref(base, ctx)[0]
            body.stmt("DECLARE @cur_id int, @cur_val decimal(18,4);")
            body.stmt(f"DECLARE adj_cursor CURSOR LOCAL FAST_FORWARD FOR\n    SELECT b.rec_id, b.{self.cid(num.name)}\n"
                      f"    FROM {bsql} AS b\n    WHERE b.{self.cid(num.name)} IS NOT NULL;")
            body.stmt("OPEN adj_cursor;")
            body.stmt("FETCH NEXT FROM adj_cursor INTO @cur_id, @cur_val;")
            body.push("WHILE @@FETCH_STATUS = 0")
            body.stmt(f"UPDATE {fact_sql}\n   SET cursor_adj_amt = @cur_val\n WHERE rec_id = @cur_id;")
            body.stmt("FETCH NEXT FROM adj_cursor INTO @cur_id, @cur_val;")
            body.pop()
            body.stmt("CLOSE adj_cursor;")
            body.stmt("DEALLOCATE adj_cursor;")
            var = f"var:{main.key}::@cur_val"
            self.truth.write(var, [(f"{base.key}.{num.name}", "value", frozenset({"cross_db_three_part"}))],
                             {"cursor"}, main.key)
            self.truth.write(f"{fact.key}.cursor_adj_amt", [(var, "value", frozenset())], {"cursor", "while_loop"},
                             main.key)
        if features["parse_error"]:
            col = rng.choice(num_calc)
            body.push("WHILE 1 = 1")
            body.stmt(f"UPDATE TOP (5000) f\n   SET f.batch_fix_amt = c.{self.cid(col.name)}\nFROM {fact_sql} AS f\n"
                      f"INNER JOIN {calc_name} AS c ON c.rec_id = f.rec_id\nWHERE f.batch_fix_amt IS NULL;")
            body.stmt("IF @@ROWCOUNT = 0 BREAK;")
            body.pop()
            self.truth.write(f"{fact.key}.batch_fix_amt", [(f"{calc.prefix}.{col.name}", "value", frozenset())],
                             {"parse_error", "update_from", "while_loop"} | ctx.flow_feats(), main.key)
        self.end_try(body, ctx, used_try, main)
        self.emit(main, self.proc_text(main, ["@from_dt date = '19000101'", "@run_id int = NULL"], body))
        self.emit_table(fact, near=main)
        self.live_procs[mart_db].append(main)
        if fill is not None:
            self.live_procs[mart_db].append(fill)
        return fact

    def build_summary(self, subj: str, mart_db: str, fact: DbObject) -> DbObject:
        nums = [c for c in fact.columns if c.typ in ("num", "int") and not c.identity and c.name != "rec_id"]
        picks = self.rng.sample(nums, min(len(nums), self.rng.randint(2, 4)))
        cols = [self.column("region_cd", "str"), self.column("event_month", "date")]
        cols += [self.column(f"total_{c.name}"[:60], "num") for c in picks]
        cols += [self.column("row_cnt", "int"), self.column("load_ts", "date")]
        summary = self.new_object(mart_db, "mart", f"{subj}_summary", "table", "mart_summary", subj, cols)
        proc = self.new_object(mart_db, "mart", f"usp_load_{subj}_summary", "procedure", "mart_summary", subj)
        ctx = self.place(proc)
        body = Body()
        f = self.src(fact, "f", ctx)
        month = "DATEFROMPARTS(YEAR(f.event_dt), MONTH(f.event_dt), 1)"
        target = self.ref(summary, ctx)[0]
        positional = self.chance("positional_insert")
        collist = "" if positional else f" ({', '.join(self.cid(c.name) for c in cols)})"
        aggs = ",\n       ".join(f"SUM(f.{self.cid(c.name)})" for c in picks)
        body.stmt(f"TRUNCATE TABLE {target};")
        body.stmt(f"INSERT INTO {target}{collist}\nSELECT f.region_cd,\n       {month},\n       {aggs},\n"
                  f"       COUNT(*),\n       SYSDATETIME()\nFROM {f.sql} AS f{f.hint}\nGROUP BY f.region_cd, {month};")
        feats = {"aggregate", "insert_select_positional" if positional else "insert_select_target_list"}
        self.truth.write(f"{summary.key}.region_cd", [(f"{fact.key}.region_cd", "value", f.feats)], feats, proc.key)
        self.truth.write(f"{summary.key}.event_month", [(f"{fact.key}.event_dt", "value", f.feats)], feats, proc.key)
        for c, pick in zip(cols[2:], picks):
            self.truth.write(f"{summary.key}.{c.name}", [(f"{fact.key}.{pick.name}", "value", f.feats)], feats, proc.key)
        self.truth.write(f"{summary.key}.row_cnt", [], feats, proc.key)
        self.truth.write(f"{summary.key}.load_ts", [], feats, proc.key)
        self.emit(proc, self.proc_text(proc, ["@run_id int = NULL"], body))
        self.emit_table(summary, near=proc)
        self.live_procs[mart_db].append(proc)
        self.table_targets.append(summary.key)
        return summary

    # -- extras ---------------------------------------------------------------
    def build_dead(self, n: int) -> None:
        for k in range(n):
            info = self.rng.choice(self.subjects)
            subj = info["subject"]
            core = info["core"]
            land = info["landing"][0]
            proc = self.new_object(self.core_db, "zz_archive", f"usp_{subj}_legacy_v{k}", "procedure", "dead", subj)
            ctx = self.place(proc)
            ctx.flow.append("dead_proc")
            body = Body()
            self.noise(body, ctx, proc)
            a = self.src(land, "a", ctx)
            if self.chance("dead_writer"):
                targets = [c for c in core.columns if c.typ == "num" and not c.computed and not c.identity]
                if targets:
                    target = self.rng.choice(targets)
                    body.stmt(f"UPDATE c\n   SET c.{self.cid(target.name)} = a.amt_gross\n"
                              f"FROM {self.ref(core, ctx)[0]} AS c\nINNER JOIN {a.sql} AS a{a.hint} ON a.rec_id = c.rec_id;")
                    self.truth.write(f"{core.key}.{target.name}", [(f"{land.key}.amt_gross", "value", a.feats)],
                                     {"update_from", "dead_proc"}, proc.key)
            dead_table = self.new_object(self.core_db, "zz_archive", f"{subj}_snapshot_v{k}", "table", "dead", subj)
            items = self.project(ctx, [a], self.rng.randint(2, 5))
            dead_table.columns = self.cols_from(items)
            body.stmt(f"INSERT INTO {self.ref(dead_table, ctx)[0]} ({', '.join(self.cid(n_) for n_, _ in items)})\n"
                      f"SELECT {self.select_items(items)}\nFROM {a.sql} AS a{a.hint};")
            self.record(dead_table.key, items, ctx, {"insert_select_target_list"}, proc.key)
            self.emit(proc, self.proc_text(proc, [], body))
            self.emit_table(dead_table, near=proc)

    def build_long_procs(self, n: int) -> None:
        for k in range(n):
            db = self.mart_dbs[k % len(self.mart_dbs)]
            proc = self.new_object(db, "mart", f"usp_nightly_maintenance_{k:02d}", "procedure", "maintenance")
            ctx = self.place(proc)
            ctx.flow.append("long_proc")
            body = Body()
            used_try = self.begin_try(body, ctx)
            facts = [s["fact"] for s in self.subjects if s["fact"].db == db]
            self.rng.shuffle(facts)
            log = self.ref(self.log_tables[db], ctx)[0]
            goal = self.rng.randint(110, 160)
            i = 0
            while body.statements < goal and facts:
                fact = facts[i % len(facts)]
                i += 1
                fsql = self.ref(fact, ctx)[0]
                body.stmt(f"INSERT INTO {log} (proc_name, step_name, message_txt, logged_at)\n"
                          f"VALUES (N'{proc.name}', N'{fact.name}', N'UPDATE {fact.name} SET ... FROM ...', SYSDATETIME());")
                body.stmt(f"UPDATE {fsql}\n   SET region_label = ISNULL(region_label, N'UNKNOWN')\n WHERE region_label IS NULL;")
                self.truth.write(f"{fact.key}.region_label", [(f"{fact.key}.region_label", "value", frozenset())],
                                 {"long_proc", "isnull"} | ctx.flow_feats(), proc.key)
                nums = [c for c in fact.columns if c.typ == "num" and not c.identity]
                if nums:
                    col = self.rng.choice(nums)
                    if self.rng.random() < 0.3:
                        w = self.src(self.region_weights, "w", ctx)
                        body.stmt(f"UPDATE f\n   SET f.{self.cid(col.name)} = f.{self.cid(col.name)} * ISNULL(w.weight_factor, 1)\n"
                                  f"FROM {fsql} AS f\nLEFT JOIN {w.sql} AS w{w.hint} ON w.region_cd = f.region_cd\n"
                                  f"WHERE f.load_ts >= DATEADD(day, -1, SYSDATETIME());")
                        self.truth.write(f"{fact.key}.{col.name}",
                                         [(f"{fact.key}.{col.name}", "value", frozenset()),
                                          (f"{self.region_weights.key}.weight_factor", "value", w.feats)],
                                         {"long_proc", "update_from", "arithmetic"} | ctx.flow_feats(), proc.key)
                    else:
                        body.stmt(f"UPDATE {fsql}\n   SET {self.cid(col.name)} = ROUND({self.cid(col.name)}, 2)\n"
                                  f" WHERE {self.cid(col.name)} IS NOT NULL;")
                        self.truth.write(f"{fact.key}.{col.name}", [(f"{fact.key}.{col.name}", "value", frozenset())],
                                         {"long_proc"} | ctx.flow_feats(), proc.key)
                body.push("IF @@ROWCOUNT > 0")
                body.stmt(f"DELETE FROM {fsql} WHERE rec_id IS NULL;")
                body.pop()
            self.end_try(body, ctx, used_try, proc)
            self.emit(proc, self.proc_text(proc, ["@run_id int = NULL"], body))

    def build_orchestrators(self) -> None:
        previous = None
        for db in self.dbs:
            procs = self.live_procs.get(db, [])
            if not procs:
                continue
            proc = self.new_object(db, "dbo", "usp_run_nightly", "procedure", "orchestration")
            ctx = self.place(proc)
            body = Body()
            used_try = self.begin_try(body, ctx)
            if previous is not None:
                body.stmt(f"EXEC {self.ref(previous, ctx, allow_one_part=False)[0]} @run_id = @run_id;")
            for child in procs:
                if child.name.startswith(("usp_fill_", "usp_get_")):
                    continue  # called by their parent loader
                body.stmt(f"EXEC {self.ref(child, ctx, allow_one_part=False)[0]} @run_id = @run_id;")
            self.end_try(body, ctx, used_try, proc)
            self.emit(proc, self.proc_text(proc, ["@run_id int = NULL"], body))
            previous = proc

    # -- chains and manifest ---------------------------------------------------
    def select_chains(self) -> list[dict]:
        candidates = []
        for info in self.subjects:
            for table in [info["fact"]] + ([info["summary"]] if info.get("summary") else []):
                for col in table.columns:
                    node = f"{table.key}.{col.name}"
                    result = self.truth.analyze(node)
                    leaves = result["value_leaves"] | result["condition_leaves"]
                    if not leaves or result["layers"] is None or result["layers"][1] < 2:
                        continue
                    candidates.append((node, table, col, result))
        order = list(range(len(candidates)))
        self.rng.shuffle(order)
        chosen: list = []
        per_subject: dict = defaultdict(int)
        covered: set = set()

        def take(predicate, limit):
            for _ in range(limit):
                best, best_score = None, None
                for i in order:
                    node, table, col, result = candidates[i]
                    if any(node == c[0] for c in chosen) or per_subject[table.subject] >= 2 or not predicate(result):
                        continue
                    if table.layer == "mart_summary" and sum(1 for c in chosen if c[1].layer == "mart_summary") \
                            >= max(1, self.n_chains // 3):
                        continue
                    new = len(result["features"] - covered)
                    leaves = len(result["value_leaves"] | result["condition_leaves"])
                    score = (new, min(leaves, 4), result["layers"][1], min(leaves, 12))
                    if best_score is None or score > best_score:
                        best, best_score = i, score
                if best is None:
                    return
                node, table, col, result = candidates[best]
                chosen.append(candidates[best])
                per_subject[table.subject] += 1
                covered.update(result["features"])

        clean = max(1, self.n_chains // 4)
        take(lambda r: not (r["features"] & set(OPAQUE_FEATURES)) and "multiple_writers" not in r["features"], clean)
        for feature in OPAQUE_FEATURES + ("multiple_writers", "select_star", "caller_temp_nested_exec",
                                          "insert_select_positional", "if_else", "scalar_udf", "cross_subject_join",
                                          "computed_column", "wildcard_insert", "patch_writer", "dead_proc"):
            if len(chosen) >= self.n_chains:
                break
            if feature not in covered:
                take(lambda r, f=feature: f in r["features"], 1)
        take(lambda r: True, self.n_chains - len(chosen))

        chains = []
        for n, (node, table, col, result) in enumerate(sorted(chosen, key=lambda c: c[0]), start=1):
            chains.append(self.chain_entry(f"chain-{n:02d}", node, table, col, result))
        return chains

    def chain_entry(self, chain_id: str, node: str, table: DbObject, col: Column, result: dict, **extra) -> dict:
        leaves = sorted(result["value_leaves"] | result["condition_leaves"])
        features = sorted(result["features"])
        opaque = sorted(set(features) & set(OPAQUE_FEATURES))
        writers = sorted(self.truth.writers.get(node, ()))
        entry = {
            "id": chain_id,
            "column": node,
            "object": table.key,
            "column_name": col.name,
            "expected_leaves": leaves,
            "value_leaves": sorted(result["value_leaves"]),
            "condition_leaves": sorted(result["condition_leaves"]),
            "static_expected_leaves": sorted(result["static_leaves"]),
            "expected_layers": result["layers"][1],
            "min_layers": result["layers"][0],
            "path_objects": sorted(result["objects"]),
            "writers": writers,
            "features": features,
            "statically_traceable": not opaque,
            "opaque_features": opaque,
            "likely_gap_kinds": sorted({LIKELY_GAP[f] for f in opaque}),
        }
        entry.update(extra)
        return entry

    # -- extension chains (generator 1.1) ----------------------------------------
    # Built after the legacy corpus and its chains, with a separate random stream and without touching
    # any open bundle file, so the legacy files and chains of a seed stay byte-for-byte the same.

    def build_extension(self) -> list[dict]:
        variants = self.extension_variants
        if variants <= 0:
            return []
        saved_rng, saved_bundles, before = self.rng, self.open_bundles, set(self.files)
        objects_before = len(self.objects)
        self.rng, self.open_bundles = self.ext_rng, {}
        specs: list[dict] = []
        try:
            for n in range(variants):
                for kind in EXT_KINDS:
                    specs.extend(getattr(self, f"ext_{kind}")(n))
            self.write_job_scripts()
        finally:
            self.rng, self.open_bundles = saved_rng, saved_bundles
        self.truth.finalize()
        self.ext_paths = set(self.files) - before
        self.ext_objects = len(self.objects) - objects_before
        chains = []
        for i, spec in enumerate(specs, start=1):
            node, table = spec["node"], spec["table"]
            col = table.col(node.rsplit(".", 1)[1])
            result = self.truth.analyze(node)
            chains.append(self.chain_entry(f"ext-{i:02d}", node, table, col, result, kind=spec["kind"],
                                           variant=spec["variant"], expect=spec["expect"]))
        return chains

    @staticmethod
    def ext_spec(node: str, kind: str, n: int, table: DbObject, expect: dict) -> dict:
        return {"node": node, "kind": kind, "variant": n, "table": table, "expect": expect}

    def ext_pick(self) -> tuple[dict, DbObject, str, str]:
        """A subject (its always-defined landing table A is the source), and a target database/schema."""
        info = self.rng.choice(self.subjects)
        db = self.rng.choice(self.mart_dbs + [self.core_db])
        schema = "core" if db == self.core_db else "mart"
        return info, info["landing"][0], db, schema

    def ext_other_landing(self, subject: str) -> DbObject:
        others = [s for s in self.subjects if s["subject"] != subject] or self.subjects
        return self.rng.choice(others)["landing"][0]

    def ext_table(self, db: str, schema: str, name: str, subject: str, cols: list[Column], defined: bool = True,
                  near: DbObject | None = None) -> DbObject:
        obj = self.new_object(db, schema, name, "table", "extension", subject, cols, defined=defined)
        if defined:
            self.emit_table(obj, near=near)
        else:
            for col in obj.columns:
                self.truth.node_feats[f"{obj.key}.{col.name}"].add("missing_ddl")
        return obj

    def ext_proc(self, db: str, schema: str, name: str, subject: str, params: list[str], build) -> DbObject:
        proc = self.new_object(db, schema, name, "procedure", "extension", subject)
        ctx = self.place(proc)
        body = Body()
        self.noise(body, ctx, proc)
        build(body, ctx, proc)
        self.emit(proc, self.proc_text(proc, params, body))
        return proc

    def ext_write(self, obj: DbObject, column: str, inputs: list, feats: set, writer: str) -> None:
        self.truth.write(f"{obj.key}.{column}", inputs, feats, writer)

    @staticmethod
    def leaf(obj: DbObject | Src, column: str, feats=frozenset()) -> tuple:
        key = obj.prefix if isinstance(obj, Src) else obj.key
        src_feats = obj.feats if isinstance(obj, Src) else frozenset()
        return (f"{key}.{column}", "value", frozenset(src_feats) | frozenset(feats))

    def ext_target(self, db: str, schema: str, name: str, subject: str, cols: list[tuple[str, str]]) -> DbObject:
        columns = [self.column(n, t) for n, t in cols] + [self.column("load_ts", "date")]
        return self.new_object(db, schema, name, "table", "extension", subject, columns)

    NO_ORDER_GAPS = ["ambiguous_writer_order", "unexplained_end"]

    # 1a. UPDATE t SET a = a * 2 after an INSERT in the same procedure.
    def ext_self_update(self, n: int) -> list[dict]:
        info, land, db, schema = self.ext_pick()
        subj = info["subject"]
        t = self.ext_target(db, schema, f"{subj}_scaled_v{n}", subj, [("rec_id", "int"), ("amount", "num"), ("qty", "int")])
        style = n % 3

        def build(body: Body, ctx: Ctx, proc: DbObject) -> None:
            a = self.src(land, "a", ctx)
            tsql = self.ref(t, ctx)[0]
            body.stmt(f"TRUNCATE TABLE {tsql};")
            body.stmt(f"INSERT INTO {tsql} (rec_id, {self.cid('amount')}, qty, load_ts)\n"
                      f"SELECT a.rec_id, a.{self.cid('amt_gross')}, a.qty, SYSDATETIME()\nFROM {a.sql} AS a{a.hint}\n"
                      f"WHERE a.is_deleted = 0;")
            self.ext_write(t, "rec_id", [self.leaf(a, "rec_id")], {"insert_select_target_list"}, proc.key)
            self.ext_write(t, "amount", [self.leaf(a, "amt_gross")], {"insert_select_target_list"}, proc.key)
            self.ext_write(t, "qty", [self.leaf(a, "qty")], {"insert_select_target_list"}, proc.key)
            if style == 0:
                body.stmt(f"UPDATE {tsql}\n   SET amount = amount * 2;")
            elif style == 1:
                body.stmt(f"UPDATE t\n   SET t.amount = t.amount + 1\nFROM {tsql} AS t\nWHERE t.qty > 0;")
            else:
                body.stmt(f"UPDATE {tsql}\n   SET {self.cid('amount')} = ROUND(amount * @factor, 2)\n WHERE amount IS NOT NULL;")
            self.ext_write(t, "amount", [self.leaf(t, "amount")], {"self_update", "arithmetic"}, proc.key)

        proc = self.ext_proc(db, schema, f"usp_load_{subj}_scaled_v{n}", subj,
                             ["@factor decimal(9,4) = 2.0", "@run_id int = NULL"], build)
        self.emit_table(t, near=proc)
        return [self.ext_spec(f"{t.key}.amount", "self_update", n, t, {
            "no_cycles": True, "stop_reasons": ["prior_state"], "no_gap_kinds": self.NO_ORDER_GAPS, "complete": True})]

    # 1b. Preserve rows in a #temp, delete, reload, restore from the #temp.
    def ext_preserve_restore(self, n: int) -> list[dict]:
        info, land, db, schema = self.ext_pick()
        subj = info["subject"]
        t = self.ext_target(db, schema, f"{subj}_kept_v{n}", subj, [("rec_id", "int"), ("amount", "num"), ("region_cd", "str")])
        style = n % 2

        def build(body: Body, ctx: Ctx, proc: DbObject) -> None:
            a = self.src(land, "a", ctx)
            tsql = self.ref(t, ctx)[0]
            saved = f"#{subj}_keep" if style else "#saved"
            if style == 0:
                body.stmt(f"CREATE TABLE {saved} (rec_id int NOT NULL, amount decimal(18,4) NULL);")
                body.stmt(f"INSERT INTO {saved} (rec_id, amount)\nSELECT rec_id, amount\nFROM {tsql}\nWHERE region_cd = N'KEEP';")
            else:
                body.stmt(f"SELECT k.rec_id, k.{self.cid('amount')}\nINTO {saved}\nFROM {tsql} AS k\nWHERE k.region_cd = N'KEEP';")
            saved_src = self.temp_src(proc.key, saved, "s", [self.column("rec_id", "int"), self.column("amount", "num")])
            self.truth.write(f"{saved_src.prefix}.rec_id", [self.leaf(t, "rec_id")], {"preserve_restore"}, proc.key)
            self.truth.write(f"{saved_src.prefix}.amount", [self.leaf(t, "amount")], {"preserve_restore"}, proc.key)
            body.stmt(f"DELETE FROM {tsql};" if style == 0 else f"TRUNCATE TABLE {tsql};")
            body.stmt(f"INSERT INTO {tsql} (rec_id, amount, region_cd, load_ts)\n"
                      f"SELECT a.rec_id, a.amt_gross, a.{self.cid('region_cd')}, SYSDATETIME()\nFROM {a.sql} AS a{a.hint}\n"
                      f"WHERE a.region_cd <> N'KEEP';")
            self.ext_write(t, "rec_id", [self.leaf(a, "rec_id")], {"insert_select_target_list"}, proc.key)
            self.ext_write(t, "amount", [self.leaf(a, "amt_gross")], {"insert_select_target_list"}, proc.key)
            self.ext_write(t, "region_cd", [self.leaf(a, "region_cd")], {"insert_select_target_list"}, proc.key)
            body.stmt(f"INSERT INTO {tsql} (rec_id, amount, region_cd, load_ts)\n"
                      f"SELECT s.rec_id, s.amount, N'KEEP', SYSDATETIME()\nFROM {saved} AS s;")
            self.ext_write(t, "rec_id", [self.leaf(saved_src, "rec_id")], {"insert_select_target_list", "preserve_restore"}, proc.key)
            self.ext_write(t, "amount", [self.leaf(saved_src, "amount")], {"insert_select_target_list", "preserve_restore"}, proc.key)

        proc = self.ext_proc(db, schema, f"usp_reload_{subj}_kept_v{n}", subj, ["@run_id int = NULL"], build)
        self.emit_table(t, near=proc)
        return [self.ext_spec(f"{t.key}.amount", "preserve_restore", n, t, {
            "no_cycles": True, "stop_reasons": ["prior_state"], "no_gap_kinds": self.NO_ORDER_GAPS, "complete": True})]

    # 1c. Aggregate a #working table into #totals, update #working from it, then copy it out.
    def ext_aggregate_update(self, n: int) -> list[dict]:
        info, land, db, schema = self.ext_pick()
        subj = info["subject"]
        t = self.ext_target(db, schema, f"{subj}_region_totals_v{n}", subj, [("region_cd", "str"), ("amount", "num")])
        style = n % 2

        def build(body: Body, ctx: Ctx, proc: DbObject) -> None:
            a = self.src(land, "a", ctx)
            tsql = self.ref(t, ctx)[0]
            w_name, tot_name = f"#{subj}_working", f"#{subj}_totals"
            if style == 0:
                body.stmt(f"CREATE TABLE {w_name} (region_cd nvarchar(40) NULL, amount decimal(18,4) NULL);")
                body.stmt(f"INSERT INTO {w_name} (region_cd, amount)\nSELECT a.region_cd, a.amt_gross\nFROM {a.sql} AS a{a.hint};")
            else:
                body.stmt(f"SELECT a.{self.cid('region_cd')}, a.amt_gross AS amount\nINTO {w_name}\nFROM {a.sql} AS a{a.hint}\n"
                          f"WHERE a.is_deleted = 0;")
            w = self.temp_src(proc.key, w_name, "w", [self.column("region_cd", "str"), self.column("amount", "num")])
            self.truth.write(f"{w.prefix}.region_cd", [self.leaf(a, "region_cd")], {"insert_select_target_list" if style == 0 else "select_into_temp"}, proc.key)
            self.truth.write(f"{w.prefix}.amount", [self.leaf(a, "amt_gross")], {"insert_select_target_list" if style == 0 else "select_into_temp"}, proc.key)
            body.stmt(f"SELECT region_cd, SUM(amount) AS total_amount\nINTO {tot_name}\nFROM {w_name}\nGROUP BY region_cd;")
            tot = self.temp_src(proc.key, tot_name, "t", [self.column("region_cd", "str"), self.column("total_amount", "num")])
            self.truth.write(f"{tot.prefix}.region_cd", [self.leaf(w, "region_cd")], {"select_into_temp", "aggregate"}, proc.key)
            self.truth.write(f"{tot.prefix}.total_amount", [self.leaf(w, "amount")], {"select_into_temp", "aggregate"}, proc.key)
            set_col = "w.amount" if style else "amount"
            body.stmt(f"UPDATE w\n   SET {set_col} = t.total_amount\nFROM {w_name} AS w\nINNER JOIN {tot_name} AS t ON t.region_cd = w.region_cd;")
            self.truth.write(f"{w.prefix}.amount", [self.leaf(tot, "total_amount")], {"update_from", "aggregate_update"}, proc.key)
            body.stmt(f"INSERT INTO {tsql} (region_cd, amount, load_ts)\nSELECT region_cd, amount, SYSDATETIME()\nFROM {w_name};")
            self.ext_write(t, "region_cd", [self.leaf(w, "region_cd")], {"insert_select_target_list"}, proc.key)
            self.ext_write(t, "amount", [self.leaf(w, "amount")], {"insert_select_target_list"}, proc.key)

        proc = self.ext_proc(db, schema, f"usp_load_{subj}_region_totals_v{n}", subj, ["@run_id int = NULL"], build)
        self.emit_table(t, near=proc)
        return [self.ext_spec(f"{t.key}.amount", "aggregate_update", n, t, {
            "no_cycles": True, "stop_reasons": ["prior_state"], "no_gap_kinds": self.NO_ORDER_GAPS, "complete": True})]

    # 1d. A variable accumulating @s = @s + col over a table, then inserted.
    def ext_variable_accumulate(self, n: int) -> list[dict]:
        info, land, db, schema = self.ext_pick()
        subj = info["subject"]
        t = self.ext_target(db, schema, f"{subj}_labels_v{n}", subj, [("run_id", "int"), ("label", "str")])
        style = n % 2
        column = "region_cd" if style == 0 else "status_cd"

        def build(body: Body, ctx: Ctx, proc: DbObject) -> None:
            a = self.src(land, "a", ctx)
            tsql = self.ref(t, ctx)[0]
            if style == 0:
                body.stmt("DECLARE @s nvarchar(max) = N'';")
                body.stmt(f"SELECT @s = @s + a.{self.cid(column)} + N';'\nFROM {a.sql} AS a{a.hint}\nWHERE a.is_deleted = 0;")
            else:
                body.stmt("DECLARE @s nvarchar(4000);")
                body.stmt("SET @s = N'';")
                body.stmt(f"SELECT @s = @s + N',' + a.{column}\nFROM {a.sql} AS a{a.hint}\nORDER BY a.rec_id;")
            var = f"var:{proc.key}::@s"
            self.truth.write(var, [self.leaf(a, column), (var, "value", frozenset())], {"variable_accumulate"}, proc.key)
            if style == 0:
                body.stmt(f"INSERT INTO {tsql} (run_id, label, load_ts)\nVALUES (@run_id, @s, SYSDATETIME());")
            else:
                body.stmt(f"INSERT INTO {tsql} (run_id, {self.cid('label')}, load_ts)\nSELECT @run_id, @s, SYSDATETIME();")
            self.ext_write(t, "label", [(var, "value", frozenset())], {"insert_values" if style == 0 else "insert_select_target_list"}, proc.key)

        proc = self.ext_proc(db, schema, f"usp_build_{subj}_labels_v{n}", subj, ["@run_id int = NULL"], build)
        self.emit_table(t, near=proc)
        return [self.ext_spec(f"{t.key}.label", "variable_accumulate", n, t, {
            "no_cycles": True, "stop_reasons": ["prior_state"], "no_gap_kinds": self.NO_ORDER_GAPS, "complete": True})]

    # 2. INSERT ... EXEC of a named procedure: one result SELECT (mapped) or two (gap).
    def _ext_insert_exec(self, n: int, two: bool) -> list[dict]:
        info, land, db, schema = self.ext_pick()
        subj = info["subject"]
        kind = "insert_exec_two" if two else "insert_exec_one"
        stem = f"{subj}_{'split' if two else 'bonus'}_v{n}"
        t = self.ext_target(db, schema, stem, subj, [("rec_id", "int"), ("bonus_amt", "num")])
        callee = self.new_object(db, schema, f"usp_get_{stem}", "procedure", "extension", subj)
        cctx = self.place(callee)
        cbody = Body()
        b = self.src(land, "b", cctx)
        work = None
        if two:
            cbody.stmt(f"SELECT b.rec_id,\n       b.amt_gross * 0.1 AS bonus_amt\nFROM {b.sql} AS b{b.hint}\nWHERE b.event_dt >= @from_dt;")
            cbody.stmt(f"SELECT b.rec_id,\n       b.{self.cid('amt_tax')} AS bonus_amt\nFROM {b.sql} AS b{b.hint}\nWHERE b.amt_tax > 0;")
        elif n % 2 == 0:
            cbody.stmt(f"SELECT b.rec_id,\n       b.{self.cid('amt_gross')} * 0.1 AS bonus_amt\nFROM {b.sql} AS b{b.hint}\nWHERE b.event_dt >= @from_dt;")
        else:
            cbody.stmt("CREATE TABLE #work (rec_id int NOT NULL, bonus_amt decimal(18,4) NULL);")
            cbody.stmt(f"INSERT INTO #work (rec_id, bonus_amt)\nSELECT b.rec_id, b.amt_gross * 0.1\nFROM {b.sql} AS b{b.hint}\n"
                       f"WHERE b.event_dt >= @from_dt;")
            cbody.stmt("SELECT rec_id, bonus_amt FROM #work;")
            work = self.temp_src(callee.key, "#work", "w", [self.column("rec_id", "int"), self.column("bonus_amt", "num")])
            self.truth.write(f"{work.prefix}.rec_id", [self.leaf(b, "rec_id")], {"insert_select_target_list"}, callee.key)
            self.truth.write(f"{work.prefix}.bonus_amt", [self.leaf(b, "amt_gross")], {"insert_select_target_list", "arithmetic"}, callee.key)
        self.emit(callee, self.proc_text(callee, ["@from_dt date = '19000101'"], cbody))
        args = ["", " @from_dt = @from_dt", " @from_dt"][n % 3]
        feat = "insert_exec" if two else "insert_exec_mapped"
        # Even variants keep EXEC on the INSERT's line (as SSMS templates do), odd ones put it on its own line.
        sep = " " if n % 2 == 0 else "\n"

        def build(body: Body, ctx: Ctx, proc: DbObject) -> None:
            tsql = self.ref(t, ctx)[0]
            r_name = f"#{subj}_bonus"
            body.stmt(f"CREATE TABLE {r_name} (rec_id int NOT NULL, bonus_amt decimal(18,4) NULL);")
            body.stmt(f"INSERT INTO {r_name} (rec_id, bonus_amt){sep}EXEC {self.ref(callee, ctx, allow_one_part=False)[0]}{args};")
            r = self.temp_src(proc.key, r_name, "r", [self.column("rec_id", "int"), self.column("bonus_amt", "num")])
            if work is not None:
                self.truth.write(f"{r.prefix}.rec_id", [self.leaf(work, "rec_id")], {feat}, proc.key)
                self.truth.write(f"{r.prefix}.bonus_amt", [self.leaf(work, "bonus_amt")], {feat}, proc.key)
            else:
                self.truth.write(f"{r.prefix}.rec_id", [self.leaf(b, "rec_id")], {feat}, proc.key)
                self.truth.write(f"{r.prefix}.bonus_amt", [self.leaf(b, "amt_gross")], {feat, "arithmetic"}, proc.key)
                if two:
                    self.truth.write(f"{r.prefix}.bonus_amt", [self.leaf(b, "amt_tax")], {feat}, proc.key)
            body.stmt(f"INSERT INTO {tsql} (rec_id, bonus_amt, load_ts)\nSELECT rec_id, bonus_amt, SYSDATETIME()\nFROM {r_name};")
            self.ext_write(t, "rec_id", [self.leaf(r, "rec_id")], {"insert_select_target_list"}, proc.key)
            self.ext_write(t, "bonus_amt", [self.leaf(r, "bonus_amt")], {"insert_select_target_list"}, proc.key)

        proc = self.ext_proc(db, schema, f"usp_load_{stem}", subj, ["@from_dt date = '19000101'", "@run_id int = NULL"], build)
        self.emit_table(t, near=proc)
        if two:
            expect = {"gap_kinds": ["insert_exec"], "gap_reasons": {"insert_exec": "callee_has_several_result_statements"},
                      "complete": False}
        else:
            expect = {"no_gap_kinds": ["insert_exec"] + self.NO_ORDER_GAPS, "evidence_roles": ["insert_exec_call"],
                      "step_statuses": ["inferred"], "complete": True}
        return [self.ext_spec(f"{t.key}.bonus_amt", kind, n, t, expect)]

    def ext_insert_exec_one(self, n: int) -> list[dict]:
        return self._ext_insert_exec(n, two=False)

    def ext_insert_exec_two(self, n: int) -> list[dict]:
        return self._ext_insert_exec(n, two=True)

    # 3. Inline table-valued function read through CROSS / OUTER APPLY.
    def ext_itvf_apply(self, n: int) -> list[dict]:
        info, land, db, schema = self.ext_pick()
        subj = info["subject"]
        t = self.ext_target(db, schema, f"{subj}_applied_v{n}", subj, [("rec_id", "int"), ("amount", "num")])
        keys = self.ext_table(db, schema, f"{subj}_keys_v{n}", subj, [self.column("rec_id", "int"), self.column("region_cd", "str")])
        fn = self.new_object(db, schema, f"fn_{subj}_amounts_v{n}", "function", "extension", subj)
        fctx = self.place(fn)
        s = self.src(land, "s", fctx, hint=False)
        self.emit(fn, f"CREATE FUNCTION {self.decl_name(fn)} (@rec_id int)\nRETURNS TABLE\nAS\nRETURN (\n"
                      f"    SELECT s.{self.cid('amt_gross')} AS amount,\n           s.qty\n    FROM {s.sql} AS s\n"
                      f"    WHERE s.rec_id = @rec_id\n);")
        self.statement_count += 1
        style = n % 3

        def build(body: Body, ctx: Ctx, proc: DbObject) -> None:
            tsql = self.ref(t, ctx)[0]
            p = self.src(keys, "p", ctx)
            fsql = self.ref(fn, ctx, allow_one_part=False)[0]
            apply = {0: f"CROSS APPLY (SELECT * FROM {fsql}(p.rec_id)) AS x",
                     1: f"CROSS APPLY {fsql}(p.rec_id) AS x",
                     2: f"OUTER APPLY (SELECT f.amount FROM {fsql}(p.rec_id) AS f) AS x"}[style]
            body.stmt(f"INSERT INTO {tsql} (rec_id, amount, load_ts)\nSELECT p.rec_id, x.amount, SYSDATETIME()\n"
                      f"FROM {p.sql} AS p{p.hint}\n{apply};")
            self.ext_write(t, "rec_id", [self.leaf(p, "rec_id")], {"insert_select_target_list"}, proc.key)
            self.ext_write(t, "amount", [self.leaf(s, "amt_gross", {"itvf", "cross_apply"})], {"insert_select_target_list"}, proc.key)

        proc = self.ext_proc(db, schema, f"usp_load_{subj}_applied_v{n}", subj, ["@run_id int = NULL"], build)
        self.emit_table(t, near=proc)
        return [self.ext_spec(f"{t.key}.amount", "itvf_apply", n, t, {
            "no_gap_kinds": ["opaque_function", "missing_definition", "unsupported_statement"] + self.NO_ORDER_GAPS,
            "evidence_roles": ["function_definition"], "step_statuses": ["inferred"], "complete": True})]

    # 4. TOP (0) * INTO #t from a table with no CREATE TABLE (but an INSERT column list elsewhere), then CTE *.
    def ext_star_by_name(self, n: int) -> list[dict]:
        info, land, db, schema = self.ext_pick()
        subj = info["subject"]
        other = self.ext_other_landing(subj)
        t = self.ext_target(db, schema, f"{subj}_fed_v{n}", subj, [("rec_id", "int"), ("amount", "num")])
        feed = self.ext_table(self.stage_db, "landing", f"{subj}_feed_v{n}", subj,
                              [self.column("rec_id", "int"), self.column("amount", "num"), self.column("status_cd", "str")],
                              defined=False)

        def build_loader(body: Body, ctx: Ctx, proc: DbObject) -> None:
            a = self.src(land, "a", ctx)
            body.stmt(f"INSERT INTO {self.ref(feed, ctx)[0]} (rec_id, amount, status_cd)\n"
                      f"SELECT a.rec_id, a.amt_gross, a.status_cd\nFROM {a.sql} AS a{a.hint};")
            self.ext_write(feed, "rec_id", [self.leaf(a, "rec_id")], {"insert_select_target_list"}, proc.key)
            self.ext_write(feed, "amount", [self.leaf(a, "amt_gross")], {"insert_select_target_list"}, proc.key)
            self.ext_write(feed, "status_cd", [self.leaf(a, "status_cd")], {"insert_select_target_list"}, proc.key)

        self.ext_proc(self.stage_db, "stg", f"usp_load_{subj}_feed_v{n}", subj, ["@run_id int = NULL"], build_loader)
        style = n % 2

        def build(body: Body, ctx: Ctx, proc: DbObject) -> None:
            tsql = self.ref(t, ctx)[0]
            b = self.src(other, "b", ctx)
            tmp = f"#{subj}_fed"
            fsql = self.ref(feed, ctx)[0]
            body.stmt(f"SELECT TOP (0) *\nINTO {tmp}\nFROM {fsql};" if style == 0 else
                      f"SELECT *\nINTO {tmp}\nFROM {fsql}\nWHERE 1 = 2;")
            body.stmt(f"INSERT INTO {tmp} (rec_id, amount)\nSELECT b.rec_id, b.{self.cid('amt_gross')}\nFROM {b.sql} AS b{b.hint};")
            tmp_src = self.temp_src(proc.key, tmp, "x", [self.column("rec_id", "int"), self.column("amount", "num")])
            self.truth.write(f"{tmp_src.prefix}.rec_id", [self.leaf(b, "rec_id")], {"empty_layout_copy", "insert_select_target_list"}, proc.key)
            self.truth.write(f"{tmp_src.prefix}.amount", [self.leaf(b, "amt_gross")], {"empty_layout_copy", "insert_select_target_list"}, proc.key)
            cte_prefix = f"cte:{proc.key}:{len(body.lines)}::c"
            if style == 0:
                body.stmt(f";WITH c AS (\n    SELECT * FROM {tmp}\n)\nINSERT INTO {tsql} (rec_id, amount, load_ts)\n"
                          f"SELECT rec_id, amount, SYSDATETIME()\nFROM c;")
            else:
                body.stmt(f";WITH c AS (\n    SELECT *, ROW_NUMBER() OVER (PARTITION BY rec_id ORDER BY amount DESC) AS rn\n"
                          f"    FROM {tmp}\n)\nINSERT INTO {tsql} (rec_id, amount, load_ts)\n"
                          f"SELECT rec_id, amount, SYSDATETIME()\nFROM c\nWHERE rn = 1;")
            self.truth.write(f"{cte_prefix}.rec_id", [self.leaf(tmp_src, "rec_id")], {"cte", "select_star"}, proc.key)
            self.truth.write(f"{cte_prefix}.amount", [self.leaf(tmp_src, "amount")], {"cte", "select_star"}, proc.key)
            self.ext_write(t, "rec_id", [(f"{cte_prefix}.rec_id", "value", frozenset())], {"insert_select_target_list"}, proc.key)
            self.ext_write(t, "amount", [(f"{cte_prefix}.amount", "value", frozenset())], {"insert_select_target_list"}, proc.key)

        proc = self.ext_proc(db, schema, f"usp_load_{subj}_fed_v{n}", subj, ["@run_id int = NULL"], build)
        self.emit_table(t, near=proc)
        return [self.ext_spec(f"{t.key}.amount", "star_by_name", n, t, {
            "no_gap_kinds": ["wildcard_without_schema", "missing_definition"] + self.NO_ORDER_GAPS,
            "step_statuses": ["inferred"], "no_cycles": True, "complete": True})]

    # 5a. INSERT without a column list into a SELECT ... INTO table: positional by that order.
    def ext_positional_select_into(self, n: int) -> list[dict]:
        info, land, db, schema = self.ext_pick()
        subj = info["subject"]
        other = self.ext_other_landing(subj)
        t = self.ext_target(db, schema, f"{subj}_copied_v{n}", subj, [("rec_id", "int"), ("amount", "num")])
        style = n % 2
        shape = None
        if style == 1:
            shape = self.ext_table(db, schema, f"{subj}_shape_v{n}", subj,
                                   [self.column("rec_id", "int"), self.column("amt_gross", "num"), self.column("qty", "int")])

        def build(body: Body, ctx: Ctx, proc: DbObject) -> None:
            tsql = self.ref(t, ctx)[0]
            b = self.src(other, "b", ctx)
            tmp = f"#{subj}_copy"
            if style == 0:
                a = self.src(land, "a", ctx, hint=False)
                body.stmt(f"SELECT TOP (0) a.rec_id, a.amt_gross, a.qty\nINTO {tmp}\nFROM {a.sql} AS a;")
            else:
                sh = self.src(shape, "s", ctx, hint=False)
                body.stmt(f"SELECT TOP (0) s.*\nINTO {tmp}\nFROM {sh.sql} AS s;")
            body.stmt(f"INSERT INTO {tmp}\nSELECT b.rec_id, b.{self.cid('amt_gross')}, b.qty\nFROM {b.sql} AS b{b.hint}\nWHERE b.is_deleted = 0;")
            tmp_src = self.temp_src(proc.key, tmp, "c", [self.column("rec_id", "int"), self.column("amt_gross", "num"), self.column("qty", "int")])
            for col in ("rec_id", "amt_gross", "qty"):
                self.truth.write(f"{tmp_src.prefix}.{col}", [self.leaf(b, col)], {"insert_select_positional", "select_into_order"}, proc.key)
            body.stmt(f"INSERT INTO {tsql} (rec_id, amount, load_ts)\nSELECT rec_id, amt_gross, SYSDATETIME()\nFROM {tmp};")
            self.ext_write(t, "rec_id", [self.leaf(tmp_src, "rec_id")], {"insert_select_target_list"}, proc.key)
            self.ext_write(t, "amount", [self.leaf(tmp_src, "amt_gross")], {"insert_select_target_list"}, proc.key)

        proc = self.ext_proc(db, schema, f"usp_load_{subj}_copied_v{n}", subj, ["@run_id int = NULL"], build)
        self.emit_table(t, near=proc)
        return [self.ext_spec(f"{t.key}.amount", "positional_select_into", n, t, {
            "no_gap_kinds": ["positional_without_schema"] + self.NO_ORDER_GAPS, "evidence_roles": ["target_schema"],
            "step_statuses": ["inferred"], "complete": True})]

    # 5b. ... into a SELECT ... INTO copy of a table with an IDENTITY column: positional_without_schema gap.
    def ext_positional_identity(self, n: int) -> list[dict]:
        info, land, db, schema = self.ext_pick()
        subj = info["subject"]
        t = self.ext_target(db, schema, f"{subj}_numbered_v{n}", subj, [("rec_id", "int"), ("amount", "num")])
        ids = self.ext_table(db, schema, f"{subj}_ids_v{n}", subj,
                             [self.column("id", "int", identity=True), self.column("rec_id", "int"), self.column("amount", "num")])
        style = n % 2

        def build(body: Body, ctx: Ctx, proc: DbObject) -> None:
            tsql = self.ref(t, ctx)[0]
            a = self.src(land, "a", ctx)
            isql = self.ref(ids, ctx)[0]
            tmp = f"#{subj}_ident"
            body.stmt(f"SELECT TOP (0) *\nINTO {tmp}\nFROM {isql};" if style == 0 else
                      f"SELECT TOP (0) *\nINTO {tmp}\nFROM (SELECT d.id, d.rec_id, d.amount FROM {isql} AS d) AS d;")
            body.stmt(f"INSERT INTO {tmp}\nSELECT a.rec_id, a.amt_gross\nFROM {a.sql} AS a{a.hint};")
            tmp_src = self.temp_src(proc.key, tmp, "i", [self.column("rec_id", "int"), self.column("amount", "num")])
            self.truth.write(f"{tmp_src.prefix}.rec_id", [self.leaf(a, "rec_id")], {"insert_select_positional", "positional_identity"}, proc.key)
            self.truth.write(f"{tmp_src.prefix}.amount", [self.leaf(a, "amt_gross")], {"insert_select_positional", "positional_identity"}, proc.key)
            body.stmt(f"INSERT INTO {tsql} (rec_id, amount, load_ts)\nSELECT rec_id, amount, SYSDATETIME()\nFROM {tmp};")
            self.ext_write(t, "rec_id", [self.leaf(tmp_src, "rec_id")], {"insert_select_target_list"}, proc.key)
            self.ext_write(t, "amount", [self.leaf(tmp_src, "amount")], {"insert_select_target_list"}, proc.key)

        proc = self.ext_proc(db, schema, f"usp_load_{subj}_numbered_v{n}", subj, ["@run_id int = NULL"], build)
        self.emit_table(t, near=proc)
        return [self.ext_spec(f"{t.key}.amount", "positional_identity", n, t, {
            "gap_kinds": ["positional_without_schema"],
            "gap_reasons": {"positional_without_schema": "select_into_target_order_not_settled"}, "complete": False})]

    # 6. SQL Agent jobs: one job orders the writers (no gap); two jobs split them (gap).
    def _ext_job_writers(self, n: int, split: bool) -> tuple[DbObject, list[DbObject], str, DbObject]:
        info, land, db, schema = self.ext_pick()
        subj = info["subject"]
        stem = f"{subj}_{'split' if split else 'jobbed'}_v{n}"
        t = self.ext_target(db, schema, stem, subj, [("rec_id", "int"), ("amount", "num"), ("region_cd", "str")])
        fixes = [self.ext_table(db, schema, f"{subj}_corrections_v{n}_{'s' if split else 'j'}", subj,
                                [self.column("rec_id", "int"), self.column("amount", "num")])]
        if not split:
            fixes.append(self.ext_table(db, schema, f"{subj}_overrides_v{n}", subj,
                                        [self.column("rec_id", "int"), self.column("amount", "num"), self.column("reason", "str")]))

        def build_load(body: Body, ctx: Ctx, proc: DbObject) -> None:
            a = self.src(land, "a", ctx)
            tsql = self.ref(t, ctx)[0]
            body.stmt(f"TRUNCATE TABLE {tsql};")
            body.stmt(f"INSERT INTO {tsql} (rec_id, amount, region_cd, load_ts)\n"
                      f"SELECT a.rec_id, a.amt_gross, a.region_cd, SYSDATETIME()\nFROM {a.sql} AS a{a.hint};")
            self.ext_write(t, "rec_id", [self.leaf(a, "rec_id")], {"insert_select_target_list"}, proc.key)
            self.ext_write(t, "amount", [self.leaf(a, "amt_gross")], {"insert_select_target_list"}, proc.key)
            self.ext_write(t, "region_cd", [self.leaf(a, "region_cd")], {"insert_select_target_list"}, proc.key)

        load = self.ext_proc(db, schema, f"usp_load_{stem}", subj, ["@run_id int = NULL"], build_load)
        fix_procs = []
        for i, fix in enumerate(fixes):
            def build_fix(body: Body, ctx: Ctx, proc: DbObject, fix=fix, i=i) -> None:
                f = self.src(fix, "f", ctx)
                tsql = self.ref(t, ctx)[0]
                where = "\nWHERE f.reason IS NOT NULL" if fix.col("reason") else ""
                set_col = "t.amount" if i == 0 else "amount"
                body.stmt(f"UPDATE t\n   SET {set_col} = f.{self.cid('amount')}\nFROM {tsql} AS t\n"
                          f"INNER JOIN {f.sql} AS f{f.hint} ON f.rec_id = t.rec_id{where};")
                self.ext_write(t, "amount", [self.leaf(f, "amount")], {"update_from", "job_writer"}, proc.key)
            fix_procs.append(self.ext_proc(db, schema, f"usp_fix_{stem}_{i + 1}", subj, ["@run_id int = NULL"], build_fix))
        self.emit_table(t, near=load)
        return t, fix_procs, subj, load

    def ext_job_ordered(self, n: int) -> list[dict]:
        t, fixes, subj, load = self._ext_job_writers(n, split=False)
        if n % 2 == 0:
            steps = [("Load", load), ("Fix corrections", fixes[0]), ("Apply overrides", fixes[1])]
        else:
            def build_run(body: Body, ctx: Ctx, proc: DbObject) -> None:
                for fix in fixes:
                    body.stmt(f"EXEC {self.ref(fix, ctx, allow_one_part=False)[0]} @run_id = @run_id;")
            runner = self.ext_proc(t.db, t.schema, f"usp_run_{subj}_fixes_v{n}", subj, ["@run_id int = NULL"], build_run)
            steps = [("Load", load), ("Run fixes", runner)]
        self.ext_jobs.append({"name": f"Nightly {subj.replace('_', ' ')} v{n}", "steps": steps, "enabled": True,
                              "start_time": 20000 + n * 10000})
        return [self.ext_spec(f"{t.key}.amount", "job_ordered", n, t, {
            "no_gap_kinds": ["ambiguous_writer_order"], "evidence_roles": ["job_step"],
            "writer_kind_status": {"update": "inferred"}, "complete": True})]

    def ext_job_split(self, n: int) -> list[dict]:
        t, fixes, subj, load = self._ext_job_writers(n, split=True)
        label = subj.replace("_", " ")
        self.ext_jobs.append({"name": f"Load {label} v{n}", "steps": [("Load", load)], "enabled": True, "start_time": 10000})
        self.ext_jobs.append({"name": f"Fix {label} v{n}", "steps": [("Fix", fixes[0])], "enabled": True,
                              "start_time": 140000 + n * 10000})
        return [self.ext_spec(f"{t.key}.amount", "job_split", n, t, {
            "gap_kinds": ["ambiguous_writer_order"], "gap_reasons": {"ambiguous_writer_order": "writers_in_different_jobs"},
            "complete": False})]

    def write_job_scripts(self) -> None:
        """One SSMS-style 'Script Job as > CREATE' file per job under jobs/."""
        for job in self.ext_jobs:
            slug = re.sub(r"[^a-z0-9]+", "_", job["name"].lower()).strip("_")
            fb = self._new_file(f"jobs/{slug}.sql", None, capacity=1)
            fb.chunks.append("USE [msdb]")
            check = "IF (@@ERROR <> 0 OR @ReturnCode <> 0) GOTO QuitWithRollback"
            lines = ["BEGIN TRANSACTION", "DECLARE @ReturnCode INT", "SELECT @ReturnCode = 0",
                     "/****** Object:  Job [{0}]    Script Date: 2025-01-15 ******/".format(job["name"]),
                     "DECLARE @jobId BINARY(16)",
                     f"EXEC @ReturnCode =  msdb.dbo.sp_add_job @job_name=N'{job['name']}', \n\t\t@enabled={1 if job['enabled'] else 0}, "
                     f"\n\t\t@notify_level_eventlog=0, \n\t\t@notify_level_email=0, \n\t\t@notify_level_netsend=0, "
                     f"\n\t\t@notify_level_page=0, \n\t\t@delete_level=0, \n\t\t@description=N'Generated for the synthetic corpus.', "
                     f"\n\t\t@category_name=N'[Uncategorized (Local)]', \n\t\t@owner_login_name=N'sa', @job_id = @jobId OUTPUT",
                     check]
            for i, (step_name, proc) in enumerate(job["steps"], start=1):
                last = i == len(job["steps"])
                command = f"EXEC {proc.schema}.{proc.name}" + (";" if i % 2 else " @run_id = NULL;")
                lines += [f"EXEC @ReturnCode = msdb.dbo.sp_add_jobstep @job_id=@jobId, @step_name=N'{step_name}', "
                          f"\n\t\t@step_id={i}, \n\t\t@cmdexec_success_code=0, \n\t\t@on_success_action={1 if last else 3}, "
                          f"\n\t\t@on_success_step_id=0, \n\t\t@on_fail_action=2, \n\t\t@on_fail_step_id=0, "
                          f"\n\t\t@retry_attempts=0, \n\t\t@retry_interval=0, \n\t\t@os_run_priority=0, @subsystem=N'TSQL', "
                          f"\n\t\t@command=N'{command}', \n\t\t@database_name=N'{proc.db}', \n\t\t@flags=0", check]
            lines += ["EXEC @ReturnCode = msdb.dbo.sp_update_job @job_id = @jobId, @start_step_id = 1", check,
                      f"EXEC @ReturnCode = msdb.dbo.sp_add_jobschedule @job_id=@jobId, @name=N'Daily', \n\t\t@enabled=1, "
                      f"\n\t\t@freq_type=4, \n\t\t@freq_interval=1, \n\t\t@freq_subday_type=1, \n\t\t@freq_subday_interval=0, "
                      f"\n\t\t@freq_relative_interval=0, \n\t\t@freq_recurrence_factor=0, \n\t\t@active_start_date=20240101, "
                      f"\n\t\t@active_end_date=99991231, \n\t\t@active_start_time={job['start_time']}, \n\t\t@active_end_time=235959",
                      check,
                      "EXEC @ReturnCode = msdb.dbo.sp_add_jobserver @job_id = @jobId, @server_name = N'(local)'", check,
                      "COMMIT TRANSACTION", "GOTO EndSave", "QuitWithRollback:",
                      "    IF (@@TRANCOUNT > 0) ROLLBACK TRANSACTION", "EndSave:"]
            fb.chunks.append("\n".join(lines))

    # 7. Multi-column UPDATE with CROSS APPLY reading both columns' pre-update values.
    def ext_multi_update_apply(self, n: int) -> list[dict]:
        info, land, db, schema = self.ext_pick()
        subj = info["subject"]
        caps = self.ext_table(db, schema, f"{subj}_caps_v{n}", subj,
                              [self.column("rec_id", "int"), self.column("cap", "num"), self.column("amount_a", "num"),
                               self.column("amount_b", "num")])
        t = self.ext_target(db, schema, f"{subj}_capped_v{n}", subj, [("rec_id", "int"), ("col_a", "num"), ("col_b", "num")])
        style = n % 2
        out = None
        if style == 0:
            out = self.ext_target(db, schema, f"{subj}_capped_out_v{n}", subj, [("rec_id", "int"), ("col_a", "num"), ("col_b", "num")])

        def build(body: Body, ctx: Ctx, proc: DbObject) -> None:
            tsql = self.ref(t, ctx)[0]
            s = self.src(caps, "s", ctx)
            body.stmt(f"INSERT INTO {tsql} (rec_id, col_a, col_b, load_ts)\nSELECT s.rec_id, s.amount_a, s.amount_b, SYSDATETIME()\n"
                      f"FROM {s.sql} AS s{s.hint};")
            self.ext_write(t, "rec_id", [self.leaf(s, "rec_id")], {"insert_select_target_list"}, proc.key)
            self.ext_write(t, "col_a", [self.leaf(s, "amount_a")], {"insert_select_target_list"}, proc.key)
            self.ext_write(t, "col_b", [self.leaf(s, "amount_b")], {"insert_select_target_list"}, proc.key)
            ca, cb = (self.cid("col_a"), self.cid("col_b")) if style else ("col_a", "col_b")
            body.stmt(f"UPDATE t\n   SET {ca} = CASE WHEN x.total <= s.cap THEN t.{ca} ELSE s.cap END,\n"
                      f"       {cb} = CASE WHEN x.total <= s.cap THEN t.{cb} ELSE s.cap END\n"
                      f"FROM {tsql} AS t\nINNER JOIN {s.sql} AS s ON s.rec_id = t.rec_id\n"
                      f"CROSS APPLY (SELECT COALESCE(t.{ca}, 0) + COALESCE(t.{cb}, 0) AS total) AS x;")
            feats = {"update_from", "case", "cross_apply", "self_update"}
            for col, other in (("col_a", "col_b"), ("col_b", "col_a")):
                self.ext_write(t, col, [self.leaf(t, col), self.leaf(t, other), self.leaf(s, "cap"),
                                        (f"{caps.key}.cap", "condition", s.feats), (f"{t.key}.{col}", "condition", frozenset()),
                                        (f"{t.key}.{other}", "condition", frozenset())], feats, proc.key)
            if out is not None:
                osql = self.ref(out, ctx)[0]
                body.stmt(f"INSERT INTO {osql} (rec_id, col_a, col_b, load_ts)\nSELECT rec_id, col_a, col_b, SYSDATETIME()\nFROM {tsql};")
                for col in ("rec_id", "col_a", "col_b"):
                    self.ext_write(out, col, [self.leaf(t, col)], {"insert_select_target_list"}, proc.key)

        proc = self.ext_proc(db, schema, f"usp_cap_{subj}_v{n}", subj, ["@run_id int = NULL"], build)
        self.emit_table(t, near=proc)
        if out is not None:
            self.emit_table(out, near=proc)
        chain_table = out or t
        return [self.ext_spec(f"{chain_table.key}.col_a", "multi_update_apply", n, chain_table, {
            "no_cycles": True, "stop_reasons": ["prior_state"], "no_gap_kinds": self.NO_ORDER_GAPS, "complete": True})]

    # 8. ALTER TABLE ... WITH CHECK ADD col, then a loader selecting that column.
    def ext_alter_add_column(self, n: int) -> list[dict]:
        info, land, db, schema = self.ext_pick()
        subj = info["subject"]
        extra = self.ext_table(self.stage_db, "landing", f"{subj}_extra_v{n}", subj,
                               [self.column("rec_id", "int"), self.column("src_code", "str")])
        added = [self.column("late_amount", "num")] + ([self.column("late_flag", "flag")] if n % 2 else [])
        style = n % 2
        decl = f"{self.cid(extra.schema)}.{self.cid(extra.name)}"
        cols = ", ".join(f"{self.cid(c.name)} {c.sqltype} NULL" for c in added)
        alter = f"ALTER TABLE {decl} WITH CHECK ADD {cols};"
        if style == 0:
            self.files[extra.file].chunks.append(alter)
        else:
            fb = self._new_file(f"{self.folders[extra.db]}/Scripts/alter_{extra.name}.sql", extra.db, capacity=1)
            fb.chunks.append(alter)
        extra.columns.extend(added)
        t = self.ext_target(db, schema, f"{subj}_late_v{n}", subj, [("rec_id", "int"), ("late_amount", "num")])

        def build(body: Body, ctx: Ctx, proc: DbObject) -> None:
            tsql = self.ref(t, ctx)[0]
            e = self.src(extra, "e", ctx)
            if style == 0:
                body.stmt(f"INSERT INTO {tsql} (rec_id, late_amount, load_ts)\nSELECT rec_id, {self.cid('late_amount')}, SYSDATETIME()\n"
                          f"FROM {e.sql}{e.hint}\nWHERE late_amount IS NOT NULL;")
            else:
                a = self.src(land, "a", ctx)
                body.stmt(f"INSERT INTO {tsql} (rec_id, late_amount, load_ts)\nSELECT e.rec_id, e.late_amount, SYSDATETIME()\n"
                          f"FROM {e.sql} AS e{e.hint}\nINNER JOIN {a.sql} AS a{a.hint} ON a.rec_id = e.rec_id\nWHERE a.is_deleted = 0;")
            self.ext_write(t, "rec_id", [self.leaf(e, "rec_id")], {"insert_select_target_list"}, proc.key)
            self.ext_write(t, "late_amount", [self.leaf(e, "late_amount", {"alter_add_column"})], {"insert_select_target_list"}, proc.key)

        proc = self.ext_proc(db, schema, f"usp_load_{subj}_late_v{n}", subj, ["@run_id int = NULL"], build)
        self.emit_table(t, near=proc)
        return [self.ext_spec(f"{t.key}.late_amount", "alter_add_column", n, t, {
            "no_gap_kinds": ["missing_definition", "ambiguous_column", "wildcard_without_schema"] + self.NO_ORDER_GAPS,
            "complete": True})]

    def build(self) -> dict:
        self.build_globals()
        n = self.target_objects
        long_procs = int(round(n / 1000 * self.rates["long_proc_per_1000"])) if n >= 300 else 0
        dead = int(round(n * self.rates["dead_proc"]))
        reserve = 2 * dead + long_procs + len(self.dbs)
        index = 0
        while len(self.objects) < n - reserve or index < 2:
            self.build_subject(index)
            index += 1
        self.build_dead(dead)
        self.build_long_procs(long_procs)
        self.build_orchestrators()
        self.truth.finalize()
        chains = self.select_chains()
        chains += self.build_extension()
        self.write_files()
        return self.manifest(chains, long_procs)

    def table_target_truth(self) -> list[dict]:
        targets = self.table_targets[:: max(1, len(self.table_targets) // 10)][:10]
        out = []
        by_key = {o.key: o for o in self.objects}
        for key in targets:
            upstream: set = set()
            for col in by_key[key].columns:
                upstream |= self.truth.analyze(f"{key}.{col.name}")["static_objects"]
            upstream.discard(key)
            obj = by_key[key]
            out.append({"object": key, "table_tool_target": f"{obj.schema}.{obj.name}",
                        "upstream_objects": sorted(upstream),
                        "note": "objects reachable through static column lineage; a table-level tool should "
                                "find at least these (it may find more, e.g. join-only tables)"})
        return out

    def manifest(self, chains: list[dict], long_procs: int) -> dict:
        by_kind: dict = defaultdict(int)
        by_db: dict = defaultdict(int)
        by_layer: dict = defaultdict(int)
        for obj in self.objects:
            by_kind[obj.kind] += 1
            by_db[obj.db] += 1
            by_layer[obj.layer] += 1
        files_per_object = defaultdict(int)
        for obj in self.objects:
            files_per_object[obj.file] += 1
        manifest = {
            "schema_version": MANIFEST_SCHEMA,
            "generator_version": GENERATOR_VERSION,
            "seed": self.seed,
            "requested_objects": self.target_objects,
            "databases": self.dbs,
            "database_folders": {self.folders[db]: db for db in self.dbs},
            "database_map_args": [f"{self.folders[db]}={db}" for db in self.dbs],
            "unmapped_folders": {"deploy": "scripts switch database with USE statements"},
            "layer_databases": {"landing": self.stage_db, "staging": self.stage_db, "core": self.core_db,
                                "views": self.core_db, "mart": self.mart_dbs},
            "rates": self.rates,
            "opaque_features": list(OPAQUE_FEATURES),
            "stats": {
                "files": len(self.files),
                "objects": len(self.objects),
                "objects_by_kind": dict(sorted(by_kind.items())),
                "objects_by_database": dict(sorted(by_db.items())),
                "objects_by_layer": dict(sorted(by_layer.items())),
                "multi_object_files": sum(1 for v in files_per_object.values() if v > 1),
                "subjects": len(self.subjects),
                "long_procedures": long_procs,
                "max_procedure_statements": max((o.statements for o in self.objects if o.kind == "procedure"), default=0),
                "emitted_statements_estimate": self.statement_count,
                "truth_column_nodes": len(set(self.truth.writers) | set(self.truth.edges)),
                "extension_objects": self.ext_objects,
                "job_script_files": sum(1 for p in self.files if p.startswith("jobs/")),
            },
            "extension": {
                "variants": self.extension_variants,
                "kinds": list(EXT_KINDS),
                "chains": sum(1 for c in chains if "kind" in c),
                "jobs": len(self.ext_jobs),
                "note": "chains with a `kind` were added by generator 1.1 after the legacy chains; `expect` lists "
                        "what the column tool's trace must contain (gap kinds and reasons, forbidden gap kinds, "
                        "stop reasons, evidence roles, step statuses, completeness, no cycles)",
            },
            "job_scripts": "jobs" if self.ext_jobs else None,
            "table_targets": self.table_target_truth(),
            "chains": chains,
            "notes": [
                "expected_leaves: every persistent column with no writer reachable from the chain column through "
                "value or condition inputs (row-selection columns excluded). value_leaves/condition_leaves split it.",
                "static_expected_leaves: the subset reachable without crossing an opaque feature "
                "(dynamic SQL, unparseable writer, INSERT ... EXEC, cursor variable, UDF reading a table).",
                "expected_layers/min_layers: longest/shortest number of persistent-object hops "
                "(tables and views; #temp, CTE and derived steps do not count) from the chain column to a leaf.",
            ],
        }
        (self.out / "manifest.json").write_text(json.dumps(manifest, indent=2, sort_keys=False) + "\n", encoding="utf-8")
        return manifest


def parse_rates(values: list[str], messiness: float) -> dict[str, float]:
    rates = dict(DEFAULT_RATES)
    for name in MESSY_RATES:
        rates[name] = min(1.0, rates[name] * messiness)
    for item in values:
        name, sep, value = item.partition("=")
        if not sep or name not in rates:
            raise SystemExit(f"unknown rate {item!r}; known: {', '.join(sorted(rates))}")
        rates[name] = float(value)
    return rates


def generate(out: str | Path, objects: int, seed: int, databases: int = 3, rates: dict | None = None,
             chains: int = 20, extension_variants: int = 2) -> dict:
    """Generate a corpus into ``out`` (replacing it) and return the manifest."""
    return Generator(Path(out), objects, seed, databases, rates or dict(DEFAULT_RATES), chains,
                     extension_variants).build()


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    parser.add_argument("--out", required=True, help="output directory (replaced if it exists)")
    parser.add_argument("--objects", type=int, default=500, help="approximate number of tables + views + procedures")
    parser.add_argument("--seed", type=int, default=1)
    parser.add_argument("--databases", type=int, default=3)
    parser.add_argument("--chains", type=int, default=20, help="number of known chains in the manifest")
    parser.add_argument("--extension-variants", type=int, default=2,
                        help="variations of each extension chain kind (earlier content, INSERT EXEC, APPLY, jobs, ...) "
                             "added after the legacy chains; 0 disables them")
    parser.add_argument("--messiness", type=float, default=1.0, help="multiplier for the messy-feature rates")
    parser.add_argument("--rate", action="append", default=[], metavar="NAME=VALUE",
                        help="override one rate (see DEFAULT_RATES in this file)")
    args = parser.parse_args(argv)
    out = Path(args.out)
    if out.exists() and any(out.iterdir()) and not (out / "manifest.json").exists():
        parser.error(f"{out} exists and does not look like a generated corpus; refusing to replace it")
    manifest = generate(out, args.objects, args.seed, args.databases, parse_rates(args.rate, args.messiness), args.chains,
                        args.extension_variants)
    stats = manifest["stats"]
    ext = manifest["extension"]
    print(f"wrote {stats['files']} files, {stats['objects']} objects {stats['objects_by_kind']} "
          f"across {len(manifest['databases'])} databases, {len(manifest['chains'])} known chains "
          f"({ext['chains']} extension chains of {len(ext['kinds'])} kinds, {ext['jobs']} job scripts) -> {out}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
