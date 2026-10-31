"""Read an SSIS package (.dtsx) into a plain inventory.

Supports the SSIS 2012+ format (identities are ``refId`` paths, properties are
attributes) and the SSIS 2008 format (integer pipeline ids, ``DTS:Property`` child
elements, DTSID references). Namespaces are ignored: every element and attribute is
known by its local name, so ``DTS:ObjectName`` and ``ObjectName`` read the same.

Nothing here decides lineage; ``ssis.py`` does. Line numbers are those of the
element's start tag in the package file.
"""
from __future__ import annotations

from dataclasses import dataclass, field
import re
import xml.parsers.expat


class PackageError(ValueError):
    pass


# ----------------------------------------------------------------------------- XML

@dataclass
class El:
    tag: str
    attrs: dict
    line: int
    children: list = field(default_factory=list)
    text: str = ""

    def get(self, name: str, default=None):
        return self.attrs.get(name, default)

    def find(self, tag: str) -> "El | None":
        return next((c for c in self.children if c.tag == tag), None)

    def findall(self, tag: str) -> list["El"]:
        return [c for c in self.children if c.tag == tag]

    def path(self, *tags: str) -> list["El"]:
        """Children reached through a chain of tags (each level may repeat)."""
        current = [self]
        for tag in tags:
            current = [c for el in current for c in el.children if c.tag == tag]
        return current

    def iter(self, tag: str | None = None):
        stack = [self]
        while stack:
            el = stack.pop()
            if tag is None or el.tag == tag:
                yield el
            stack.extend(reversed(el.children))


def parse_xml(data: bytes) -> El:
    parser = xml.parsers.expat.ParserCreate(namespace_separator="}")
    parser.buffer_text = True
    stack: list[El] = []
    roots: list[El] = []

    def local(name: str) -> str:
        return name.rsplit("}", 1)[-1]

    def start(name, attrs):
        el = El(local(name), {local(k): v for k, v in attrs.items()}, parser.CurrentLineNumber)
        if stack:
            stack[-1].children.append(el)
        else:
            roots.append(el)
        stack.append(el)

    def end(name):
        stack.pop()

    def chars(data):
        if stack:
            stack[-1].text += data

    def refuse(*_args):
        raise PackageError("package files with a DOCTYPE or entity declarations are not read")

    parser.StartElementHandler = start
    parser.EndElementHandler = end
    parser.CharacterDataHandler = chars
    parser.StartDoctypeDeclHandler = refuse
    parser.EntityDeclHandler = refuse
    try:
        parser.Parse(data, True)
    except xml.parsers.expat.ExpatError as exc:
        raise PackageError(f"XML error: {exc}") from exc
    if not roots:
        raise PackageError("empty document")
    return roots[0]


def dts(el: El, name: str) -> str | None:
    """A DTS property: an attribute (2012+) or a ``Property Name=...`` child element (2008)."""
    if name in el.attrs:
        return el.attrs[name]
    for child in el.children:
        if child.tag == "Property" and child.get("Name") == name:
            return child.text.strip()
    return None


def truthy(value: str | None) -> bool:
    return (value or "").strip().lower() in ("true", "-1", "1")


def normalize_ref(raw: str | None) -> str | None:
    """A lineage/column reference as written in a property: ``#{refId}``, ``#12`` or ``12``."""
    if raw is None:
        return None
    raw = raw.strip()
    if raw.startswith("#{") and raw.endswith("}"):
        raw = raw[2:-1]
    elif raw.startswith("#"):
        raw = raw[1:]
    return raw or None


def bracket_name(ref: str | None) -> str | None:
    """``Package.ConnectionManagers[name]`` -> ``name``."""
    m = re.search(r"\[([^\]]*)\]\s*$", ref or "")
    return m.group(1) if m else None


# ------------------------------------------------------------------------- inventory

@dataclass
class Connection:
    name: str
    ref_id: str | None
    dtsid: str | None
    creation: str
    connection_string: str
    server: str | None
    database: str | None
    file_path: str | None
    expressions: dict          # property -> expression (set at run time)
    columns: list              # flat-file column names, in order
    line: int


@dataclass
class Variable:
    name: str                  # Namespace::Name
    value: str | None
    expression: str | None     # when EvaluateAsExpression
    dtsid: str | None
    line: int


@dataclass
class Prop:
    value: str
    line: int


@dataclass
class Col:
    id: str
    name: str
    lineage: str | None
    external_id: str | None
    usage: str                 # readonly, readwrite, '' for output columns
    props: dict                # name -> Prop
    line: int
    data_type: str | None = None
    length: str | None = None
    special: str | None = None
    sort_key: str | None = None
    port: "Port | None" = None


@dataclass
class Port:
    id: str
    name: str
    kind: str                  # input, output
    columns: list
    external: dict             # external column id -> (name, line)
    sync_input: str | None
    is_error: bool
    props: dict
    line: int
    component: "Component | None" = None

    def external_name(self, col: Col) -> str | None:
        found = self.external.get(col.external_id or "")
        return found[0] if found else None


@dataclass
class Component:
    id: str
    name: str
    cls: str                   # normalized class, or 'unsupported'
    cls_raw: str
    props: dict
    connections: list          # [(connection name in component, connection manager reference)]
    inputs: list
    outputs: list
    line: int


@dataclass
class Task:
    key: str                   # refId (2012+) or path (2008)
    dtsid: str | None
    name: str
    path: str                  # Package\Container\Task
    kind: str                  # data_flow, execute_sql, sequence, for_loop, foreach_loop, package, other
    creation: str
    line: int
    disabled: bool
    parent: "Task | None"
    el: El
    props: dict = field(default_factory=dict)     # property expressions on the task: name -> expression
    branches: list = field(default_factory=list)


@dataclass
class Constraint:
    source: str                # key or DTSID of the preceding executable
    target: str
    value: str                 # 0 success, 1 failure, 2 completion
    eval_op: str | None
    expression: str | None
    logical_and: bool
    line: int


@dataclass
class Flow:
    task: Task
    components: list
    ports: dict                # port id -> Port
    upstream: dict             # input port id -> output Port
    lineage: dict              # lineage id -> output Col
    input_cols: dict           # input column id -> Col
    line: int


@dataclass
class SqlTask:
    task: Task
    connection: str | None
    source_type: str           # DirectInput, FileConnection, Variable
    sql: str
    line: int
    parameters: list           # [(parameter name, variable, direction)]
    results: list              # variables set from the result set


@dataclass
class PackageFile:
    format: str                # 2012+, 2008
    name: str
    connections: list
    variables: dict            # name -> Variable
    parameters: set
    tasks: list
    constraints: list
    flows: list
    sql_tasks: list
    runtime_setters: dict      # variable -> [description]
    configurations: bool
    issues: list               # (kind, message, line)


COMPONENT_CLASSES = {
    "microsoft.flatfilesource": "flat_file_source",
    "microsoft.oledbsource": "oledb_source",
    "microsoft.oledbdestination": "oledb_destination",
    "microsoft.sqlserverdestination": "sqlserver_destination",
    "microsoft.flatfiledestination": "flat_file_destination",
    "microsoft.dataconvert": "data_conversion",
    "microsoft.derivedcolumn": "derived_column",
    "microsoft.conditionalsplit": "conditional_split",
    "microsoft.lookup": "lookup",
    "microsoft.unionall": "union_all",
    "microsoft.merge": "merge",
    "microsoft.mergejoin": "merge_join",
    "microsoft.multicast": "multicast",
    "microsoft.sort": "sort",
    "microsoft.rowcount": "row_count",
    "microsoft.copymap": "copy_column",
    "microsoft.copycolumn": "copy_column",
    "microsoft.aggregate": "aggregate",
}
# SSIS 2008 components carry a GUID class id; their contactInfo (set by the component, not
# the package author) starts with the component type.
CONTACT_CLASSES = {
    "flat file source": "flat_file_source", "ole db source": "oledb_source",
    "ole db destination": "oledb_destination", "sql server destination": "sqlserver_destination",
    "flat file destination": "flat_file_destination", "data conversion": "data_conversion",
    "derived column": "derived_column", "conditional split": "conditional_split", "lookup": "lookup",
    "union all": "union_all", "merge": "merge", "merge join": "merge_join", "multicast": "multicast",
    "sort": "sort", "row count": "row_count", "copy column": "copy_column", "aggregate": "aggregate",
    "ado net source": "adonet_source", "ado.net source": "adonet_source", "datareader source": "adonet_source",
    "ado net destination": "adonet_destination", "ado.net destination": "adonet_destination",
}
MANAGED_CLASSES = {"datareadersourceadapter": "adonet_source", "adonetdestination": "adonet_destination"}


def classify(el: El, props: dict) -> tuple[str, str]:
    raw = el.get("componentClassID") or ""
    low = raw.strip().lower()
    if low in COMPONENT_CLASSES:
        return COMPONENT_CLASSES[low], raw
    user = (props.get("UserComponentTypeName").value if props.get("UserComponentTypeName") else "") or ""
    for key, cls in MANAGED_CLASSES.items():
        if key in user.lower():
            return cls, user or raw
    if user:
        return "unsupported", user
    contact = (el.get("contactInfo") or "").split(";", 1)[0].strip().lower()
    if contact in CONTACT_CLASSES:
        return CONTACT_CLASSES[contact], raw or contact
    return "unsupported", raw or contact or "unknown component"


def parse_connection_string(text: str) -> dict:
    out = {}
    for part in re.findall(r'(?:[^;"\']|"[^"]*"|\'[^\']*\')+', text or ""):
        if "=" in part:
            key, value = part.split("=", 1)
            out[key.strip().lower()] = value.strip().strip('"\'')
    return out


def _props(el: El) -> dict:
    holder = el.find("properties")
    if holder is None:
        return {}
    return {p.get("name"): Prop((p.text or "").strip(), p.line) for p in holder.findall("property") if p.get("name")}


def _property_expressions(el: El) -> dict:
    out = {}
    for child in el.children:
        if child.tag == "PropertyExpression" and child.get("Name"):
            out[child.get("Name")] = child.text.strip()
    return out


def _executable_type(el: El) -> str:
    return el.get("ExecutableType") or dts(el, "ExecutableType") or el.get("CreationName") or dts(el, "CreationName") or ""


def _task_kind(el: El, etype: str, parent: Task | None) -> str:
    low = etype.lower()
    data = el.find("ObjectData")
    if parent is None:
        return "package"
    if "pipeline" in low or (data is not None and data.find("pipeline") is not None):
        return "data_flow"
    if "executesqltask" in low or (data is not None and data.find("SqlTaskData") is not None):
        return "execute_sql"
    if "forloop" in low:
        return "for_loop"
    if "foreachloop" in low:
        return "foreach_loop"
    if "sequence" in low:
        return "sequence"
    return "other"


def read_package(data: bytes) -> PackageFile:
    root = parse_xml(data)
    if root.tag != "Executable":
        raise PackageError("not an SSIS package (root element is not DTS:Executable)")
    fmt = "2012+" if root.get("refId") is not None or dts(root, "PackageFormatVersion") not in (None, "2", "3") \
        else "2008"
    if root.get("refId") is None and dts(root, "PackageFormatVersion") is None:
        fmt = "2008"
    pkg = PackageFile(fmt, dts(root, "ObjectName") or "Package", [], {}, set(), [], [], [], [], {}, False, [])
    pkg.configurations = any(True for _ in root.iter("Configuration"))

    for holder in root.findall("ConnectionManagers"):
        for cm in holder.findall("ConnectionManager"):
            pkg.connections.append(_connection(cm))
    for holder in root.findall("PackageParameters"):
        for param in holder.findall("PackageParameter"):
            name = dts(param, "ObjectName")
            if name:
                pkg.parameters.add(f"$Package::{name}")
    _executables(root, None, pkg)
    _setters(root, pkg)
    return pkg


def _connection(cm: El) -> Connection:
    name = dts(cm, "ObjectName") or bracket_name(cm.get("refId")) or "?"
    inner = next((c for c in cm.path("ObjectData", "ConnectionManager")), None)
    text = (dts(inner, "ConnectionString") if inner is not None else None) or dts(cm, "ConnectionString") or ""
    parts = parse_connection_string(text)
    creation = dts(cm, "CreationName") or ""
    columns = []
    if inner is not None:
        for col in inner.path("FlatFileColumns", "FlatFileColumn"):
            if dts(col, "ObjectName"):
                columns.append(dts(col, "ObjectName"))
    is_file = creation.upper() in ("FLATFILE", "FILE", "MULTIFLATFILE") or bool(columns)
    return Connection(
        name=name, ref_id=cm.get("refId"), dtsid=dts(cm, "DTSID"), creation=creation, connection_string=text,
        server=parts.get("data source") or parts.get("server"),
        database=None if is_file else (parts.get("initial catalog") or parts.get("database")),
        file_path=text if is_file else None, expressions=_property_expressions(cm), columns=columns, line=cm.line)


def _variables(el: El, pkg: PackageFile) -> None:
    for holder in el.findall("Variables"):
        for var in holder.findall("Variable"):
            name = f"{dts(var, 'Namespace') or 'User'}::{dts(var, 'ObjectName') or '?'}"
            value_el = var.find("VariableValue")
            expression = dts(var, "Expression") if truthy(dts(var, "EvaluateAsExpression")) else None
            pkg.variables.setdefault(name, Variable(name, value_el.text if value_el is not None else None,
                                                    expression, dts(var, "DTSID"), var.line))


def _executables(el: El, parent: Task | None, pkg: PackageFile) -> None:
    if parent is None:
        task = Task(el.get("refId") or "Package", dts(el, "DTSID"), dts(el, "ObjectName") or "Package", "Package",
                    "package", _executable_type(el), el.line, False, None, el)
        pkg.tasks.append(task)
        parent_task = task
    else:
        parent_task = parent
    _variables(el, pkg)
    for holder in el.findall("Executables"):
        for ex in holder.findall("Executable"):
            name = dts(ex, "ObjectName") or "?"
            path = f"{parent_task.path}\\{name}"
            etype = _executable_type(ex)
            task = Task(ex.get("refId") or path, dts(ex, "DTSID"), name, path, _task_kind(ex, etype, parent_task),
                        etype, ex.line, truthy(dts(ex, "Disabled")), parent_task, ex, _property_expressions(ex))
            pkg.tasks.append(task)
            if task.kind == "data_flow":
                try:
                    pkg.flows.append(_flow(task))
                except PackageError as exc:
                    pkg.issues.append(("unsupported_package_step", f"Data flow {path} could not be read: {exc}", ex.line))
            elif task.kind == "execute_sql":
                sql = _sql_task(task)
                if sql is not None:
                    pkg.sql_tasks.append(sql)
            _executables(ex, task, pkg)
    for holder in el.findall("PrecedenceConstraints"):
        for pc in holder.findall("PrecedenceConstraint"):
            source, target = pc.get("From"), pc.get("To")
            if source is None or target is None:
                for ref in pc.findall("Executable"):
                    if truthy(ref.get("IsFrom")):
                        source = ref.get("IDREF")
                    else:
                        target = ref.get("IDREF")
            if source and target:
                pkg.constraints.append(Constraint(source, target, dts(pc, "Value") or "0", dts(pc, "EvalOp"),
                                                  dts(pc, "Expression"), dts(pc, "LogicalAnd") is None
                                                  or truthy(dts(pc, "LogicalAnd")), pc.line))


def _sql_task(task: Task) -> SqlTask | None:
    data = next(iter(task.el.path("ObjectData", "SqlTaskData")), None)
    if data is None:
        return None
    params = [(p.get("ParameterName"), p.get("DtsVariableName"), p.get("ParameterDirection") or "Input")
              for p in data.findall("ParameterBinding")]
    results = [r.get("DtsVariableName") for r in data.findall("ResultBinding") if r.get("DtsVariableName")]
    return SqlTask(task, data.get("Connection"), data.get("SqlStmtSourceType") or "DirectInput",
                   data.get("SqlStatementSource") or "", data.line, params, results)


def _setters(root: El, pkg: PackageFile) -> None:
    """Variables something in the package sets at run time (result sets, output parameters,
    loop mappings, script tasks with write access, expression tasks)."""
    def add(name, what):
        if name:
            for part in re.split(r"[,;]", name):
                part = part.strip()
                if part:
                    if "::" not in part:
                        part = f"User::{part}"
                    pkg.runtime_setters.setdefault(part, []).append(what)
    for sql in pkg.sql_tasks:
        for name in sql.results:
            add(name, f"result set of Execute SQL task {sql.task.path}")
        for _, name, direction in sql.parameters:
            if (direction or "").lower() in ("output", "returnvalue"):
                add(name, f"{direction} parameter of Execute SQL task {sql.task.path}")
    for el in root.iter():
        if el.tag == "ForEachVariableMapping":
            add(dts(el, "VariableName"), "a Foreach loop variable mapping")
        for key in ("ReadWriteVariables",):
            if el.get(key):
                add(el.get(key), "a script with write access")
        if el.tag == "Property" and el.get("Name") == "ReadWriteVariables" and el.text.strip():
            add(el.text.strip(), "a script with write access")
        if el.tag == "ExpressionTask" and el.get("Expression"):
            m = re.match(r"\s*@\[([^\]]+)\]\s*=", el.get("Expression"))
            if m:
                add(m.group(1), "an Expression task")
    for flow in pkg.flows:
        for comp in flow.components:
            if comp.cls == "row_count" and comp.props.get("VariableName"):
                add(comp.props["VariableName"].value, f"Row Count component {comp.name}")


def _columns(el: El, kind: str) -> list[Col]:
    tag = "inputColumn" if kind == "input" else "outputColumn"
    holder = el.find("inputColumns" if kind == "input" else "outputColumns")
    out = []
    for c in holder.findall(tag) if holder is not None else []:
        cid = c.get("refId") or c.get("id") or ""
        lineage = c.get("lineageId") or (cid if kind == "output" else None)
        external = c.get("externalMetadataColumnId")
        out.append(Col(cid, c.get("cachedName") or c.get("name") or "", lineage,
                       external if external not in (None, "", "0") else None,
                       (c.get("usageType") or "").lower(), _props(c), c.line, c.get("dataType"),
                       c.get("length"), c.get("specialFlags"), c.get("sortKeyPosition")))
    return out


def _external(el: El) -> dict:
    holder = el.find("externalMetadataColumns")
    out = {}
    for c in holder.findall("externalMetadataColumn") if holder is not None else []:
        cid = c.get("refId") or c.get("id")
        if cid:
            out[cid] = (c.get("name") or "", c.line)
    return out


def _flow(task: Task) -> Flow:
    pipeline = next(iter(task.el.path("ObjectData", "pipeline")), None)
    if pipeline is None:
        raise PackageError("no pipeline element")
    flow = Flow(task, [], {}, {}, {}, {}, pipeline.line)
    for c in pipeline.path("components", "component"):
        props = _props(c)
        cls, raw = classify(c, props)
        comp = Component(c.get("refId") or c.get("id") or "", c.get("name") or "?", cls, raw, props,
                         [(x.get("name"), x.get("connectionManagerRefId") or x.get("connectionManagerID"))
                          for x in c.path("connections", "connection")], [], [], c.line)
        for kind, holder_tag, tag in (("input", "inputs", "input"), ("output", "outputs", "output")):
            for p in c.path(holder_tag, tag):
                sync = p.get("synchronousInputId")
                port = Port(p.get("refId") or p.get("id") or "", p.get("name") or "", kind, _columns(p, kind),
                            _external(p), sync if sync not in (None, "", "0") else None,
                            truthy(p.get("isErrorOut")), _props(p), p.line, comp)
                for col in port.columns:
                    col.port = port
                    if kind == "output" and col.lineage:
                        flow.lineage[col.lineage] = col
                    if kind == "input":
                        flow.input_cols[col.id] = col
                (comp.inputs if kind == "input" else comp.outputs).append(port)
                flow.ports[port.id] = port
        flow.components.append(comp)
    for p in pipeline.path("paths", "path"):
        start, end = p.get("startId"), p.get("endId")
        if start in flow.ports and end in flow.ports:
            flow.upstream[end] = flow.ports[start]
    return flow
