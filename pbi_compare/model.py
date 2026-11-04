"""Semantic model understanding.

Builds one ``SemanticModel`` from any of three sources, best first:
  1. Fabric getDefinition TMDL parts          (full fidelity, incl. M sources)
  2. Fabric getDefinition model.bim (TMSL)     (full fidelity)
  3. DAX INFO.* / INFO.VIEW.* via executeQueries (works on any capacity)

Then derives what the comparison and SQL generator need: column -> source
column, table -> Snowflake object (resolving M parameters), relationships,
measure dependency lineage, fact/dimension roles and a model-to-model diff.
"""
from __future__ import annotations

import json
import re
from dataclasses import dataclass, field
from typing import Callable, Dict, List, Optional, Set, Tuple

import pandas as pd


# ----------------------------------------------------------------- objects
@dataclass
class Column:
    table: str
    name: str
    data_type: str = ""
    source_column: str = ""
    expression: str = ""          # calculated column DAX
    is_hidden: bool = False

    @property
    def is_calculated(self) -> bool:
        return bool(self.expression)


@dataclass
class Measure:
    table: str
    name: str
    expression: str
    format_string: str = ""
    is_hidden: bool = False


@dataclass
class Relationship:
    from_table: str
    from_column: str
    to_table: str
    to_column: str
    is_active: bool = True
    cross_filter: str = "oneDirection"
    from_cardinality: str = "many"
    to_cardinality: str = "one"

    def key(self) -> str:
        return f"{self.from_table}[{self.from_column}] -> {self.to_table}[{self.to_column}]"


@dataclass
class SourceInfo:
    connector: str = ""            # Snowflake.Databases, Sql.Database, ...
    server: str = ""
    warehouse: str = ""
    database: str = ""
    schema: str = ""
    object_name: str = ""
    native_query: str = ""
    renames: Dict[str, str] = field(default_factory=dict)   # model name -> source name
    mode: str = ""                 # import / directQuery / dual
    m_expression: str = ""
    notes: List[str] = field(default_factory=list)

    @property
    def fqn(self) -> str:
        parts = [p for p in (self.database, self.schema, self.object_name) if p]
        return ".".join(_sf_ident(p) for p in parts)

    def sql_from(self) -> str:
        if self.native_query:
            return f"(\n{self.native_query.strip()}\n)"
        return self.fqn


@dataclass
class Table:
    name: str
    columns: Dict[str, Column] = field(default_factory=dict)
    measures: Dict[str, Measure] = field(default_factory=dict)
    source: SourceInfo = field(default_factory=SourceInfo)
    is_hidden: bool = False
    is_calculated: bool = False
    calc_expression: str = ""


@dataclass
class SemanticModel:
    name: str = ""
    tables: Dict[str, Table] = field(default_factory=dict)
    relationships: List[Relationship] = field(default_factory=list)
    parameters: Dict[str, str] = field(default_factory=dict)  # shared M expressions
    origin: str = ""                                           # tmdl | bim | info
    warnings: List[str] = field(default_factory=list)
    roles: List[str] = field(default_factory=list)             # RLS roles
    role_filters: Dict[str, Dict[str, str]] = field(default_factory=dict)  # role -> table -> DAX
    role_members: Dict[str, List[str]] = field(default_factory=dict)       # role -> emails/groups

    def roles_for(self, email: str) -> List[str]:
        """Roles the email is a direct member of (when the model records members)."""
        e = (email or "").strip().lower()
        return [r for r in self.roles if e and e in [m.lower() for m in self.role_members.get(r, [])]]

    @property
    def has_rls(self) -> bool:
        return bool(self.roles)

    def full_access_roles(self) -> List[str]:
        """Roles with no row filters - applying one of them shows all data."""
        return [r for r in self.roles if not any((self.role_filters.get(r) or {}).values())]

    def dynamic_roles(self) -> List[str]:
        """Roles whose filters depend on who is viewing (USERPRINCIPALNAME() etc.)."""
        return [r for r in self.roles if any(_IDENT_FUNC.search(f or "")
                                             for f in (self.role_filters.get(r) or {}).values())]

    def identity_column(self) -> Optional[Tuple[str, str, str]]:
        """(table, column, function) that dynamic RLS compares with the user's identity,
        e.g. ('RAM_PS_RB_BU_SBU_RUS', 'EMAIL', 'USERPRINCIPALNAME')."""
        for r in self.roles:
            for tbl, f in (self.role_filters.get(r) or {}).items():
                hit = rls_identity_ref(f or "", tbl)
                if hit:
                    return hit
        return None

    def date_source(self, table: str) -> Optional[Tuple[str, str]]:
        """Auto date/time tables (LocalDateTable_*) -> the real date column they hang off."""
        for r in self.relationships:
            if r.to_table == table and not r.from_table.startswith(("LocalDateTable_",
                                                                     "DateTableTemplate_")):
                return r.from_table, r.from_column
        return None

    # ---------------------------------------------------------- lookups
    def table(self, name: str) -> Optional[Table]:
        if name in self.tables:
            return self.tables[name]
        low = name.lower()
        return next((t for n, t in self.tables.items() if n.lower() == low), None)

    def find_measure(self, name: str, table_hint: str = "") -> Optional[Measure]:
        if table_hint and (t := self.table(table_hint)) and name in t.measures:
            return t.measures[name]
        for t in self.tables.values():
            if name in t.measures:
                return t.measures[name]
        low = name.lower()
        for t in self.tables.values():
            for m in t.measures.values():
                if m.name.lower() == low:
                    return m
        return None

    def find_column(self, table: str, name: str) -> Optional[Column]:
        t = self.table(table)
        if not t:
            return None
        if name in t.columns:
            return t.columns[name]
        low = name.lower()
        return next((c for c in t.columns.values() if c.name.lower() == low), None)

    def all_measures(self) -> List[Measure]:
        return [m for t in self.tables.values() for m in t.measures.values()]

    # -------------------------------------------------- understanding
    def table_roles(self) -> Dict[str, str]:
        many = {r.from_table for r in self.relationships if r.from_cardinality == "many"}
        one = {r.to_table for r in self.relationships}
        roles = {}
        for n, t in self.tables.items():
            if n.startswith(("DateTableTemplate_", "LocalDateTable_")):
                roles[n] = "auto date"
            elif n in many and n not in one:
                roles[n] = "fact"
            elif n in one and n not in many:
                roles[n] = "dimension"
            elif n in one and n in many:
                roles[n] = "bridge / snowflaked dim"
            elif t.measures and not t.columns:
                roles[n] = "measure table"
            elif t.is_calculated:
                roles[n] = "calculated table"
            else:
                roles[n] = "disconnected"
        return roles

    def measure_dependencies(self, measure: Measure, _seen: Optional[Set[str]] = None
                             ) -> Tuple[Set[str], Set[Tuple[str, str]]]:
        """Return (measures used, (table, column) used) - transitively."""
        seen = _seen if _seen is not None else set()
        measures: Set[str] = set()
        columns: Set[Tuple[str, str]] = set()
        if measure.name in seen:
            return measures, columns
        seen.add(measure.name)
        for tbl, col in dax_column_refs(measure.expression):
            if tbl:
                columns.add((tbl, col))
            else:
                m = self.find_measure(col)
                if m:
                    measures.add(m.name)
                    sub_m, sub_c = self.measure_dependencies(m, seen)
                    measures |= sub_m
                    columns |= sub_c
                else:  # [Col] inside an iterator over the measure's home table
                    columns.add((measure.table, col))
        for tbl in dax_table_refs(measure.expression):
            columns.add((tbl, "*"))
        return measures, columns

    def summary_frames(self) -> Dict[str, pd.DataFrame]:
        roles = self.table_roles()
        tables = pd.DataFrame([{
            "Table": n, "Role": roles.get(n, ""), "Columns": len(t.columns),
            "Measures": len(t.measures), "Mode": t.source.mode,
            "Source object": t.source.fqn or ("native SQL" if t.source.native_query else
                                              ("calculated" if t.is_calculated else "")),
            "Hidden": t.is_hidden,
        } for n, t in self.tables.items()])
        cols = pd.DataFrame([{
            "Table": c.table, "Column": c.name, "Type": c.data_type,
            "Source column": self.source_column_name(c.table, c.name) or "",
            "Calculated DAX": c.expression, "Hidden": c.is_hidden,
        } for t in self.tables.values() for c in t.columns.values()])
        meas = []
        for m in self.all_measures():
            dm, dc = self.measure_dependencies(m)
            meas.append({"Table": m.table, "Measure": m.name, "DAX": m.expression,
                         "Format": m.format_string,
                         "Depends on measures": ", ".join(sorted(dm - {m.name})),
                         "Depends on columns": ", ".join(sorted(f"{t}[{c}]" for t, c in dc)),
                         "SQL translatable": translatable(m.expression)})
        rels = pd.DataFrame([{
            "From": f"{r.from_table}[{r.from_column}]", "To": f"{r.to_table}[{r.to_column}]",
            "Cardinality": f"{r.from_cardinality}:{r.to_cardinality}",
            "Cross filter": r.cross_filter, "Active": r.is_active} for r in self.relationships])
        return {"tables": tables, "columns": cols, "measures": pd.DataFrame(meas),
                "relationships": rels}

    def source_column_name(self, table: str, column: str) -> str:
        t = self.table(table)
        c = self.find_column(table, column)
        if not t or not c or c.is_calculated:
            return ""
        name = c.source_column or c.name
        return t.source.renames.get(name, name)

    def to_dot(self, highlight: Optional[Set[str]] = None) -> str:
        roles = self.table_roles()
        colors = {"fact": "#F2C94C", "dimension": "#8ECAE6", "bridge / snowflaked dim": "#B5E48C",
                  "measure table": "#E0E0E0"}
        lines = ["digraph model {", 'rankdir=LR; node [shape=box, style="rounded,filled", '
                 'fontname="Helvetica", fontsize=10]; edge [fontsize=8, fontname="Helvetica"];']
        for n in self.tables:
            if roles.get(n) == "auto date":
                continue
            pen = ', penwidth=3, color="#D62828"' if highlight and n in highlight else ""
            lines.append(f'"{n}" [fillcolor="{colors.get(roles.get(n), "#FFFFFF")}"{pen}];')
        for r in self.relationships:
            if r.from_table.startswith(("LocalDateTable_", "DateTableTemplate_")) or \
               r.to_table.startswith(("LocalDateTable_", "DateTableTemplate_")):
                continue
            style = "solid" if r.is_active else "dashed"
            dirn = "both" if r.cross_filter.lower().startswith("both") else "forward"
            lines.append(f'"{r.to_table}" -> "{r.from_table}" [label="{r.to_column}", '
                         f'style={style}, dir={dirn}];')
        lines.append("}")
        return "\n".join(lines)


# ======================================================== RLS helpers
_IDENT_FUNC = re.compile(r"\b(USERPRINCIPALNAME|USERNAME|CUSTOMDATA|USEROBJECTID)\s*\(\s*\)", re.I)
_REF = r"(?:'((?:[^']|'')+)'|([A-Za-z_]\w*))?\[([^\]]+)\]"


def rls_identity_ref(filter_dax: str, home_table: str) -> Optional[Tuple[str, str, str]]:
    """Find the column a role filter compares with the viewer's identity.

    Handles  T[Col] = USERPRINCIPALNAME(),  USERNAME() = [Col],  T[Col] IN {USERNAME()}
    and LOOKUPVALUE(..., T[Col], USERPRINCIPALNAME()).
    """
    pats = [_REF + r"\s*(?:=|==|IN\s*\{)\s*" + r"(USERPRINCIPALNAME|USERNAME|CUSTOMDATA|USEROBJECTID)\s*\(\s*\)",
            r"(USERPRINCIPALNAME|USERNAME|CUSTOMDATA|USEROBJECTID)\s*\(\s*\)\s*(?:=|==)\s*" + _REF,
            _REF + r"\s*,\s*(USERPRINCIPALNAME|USERNAME|CUSTOMDATA|USEROBJECTID)\s*\(\s*\)"]
    for i, pat in enumerate(pats):
        m = re.search(pat, filter_dax or "", re.I)
        if not m:
            continue
        if i == 1:
            func, q, b, col = m.group(1), m.group(2), m.group(3), m.group(4)
        else:
            q, b, col, func = m.group(1), m.group(2), m.group(3), m.group(4)
        tbl = (q.replace("''", "'") if q else b) or home_table
        return tbl, col, func.upper()
    return None


# ======================================================== DAX ref helpers
_COLREF = re.compile(r"(?:'((?:[^']|'')+)'|([A-Za-z_][\w]*))?\[([^\]]+)\]")
_TABLE_FUNCS = re.compile(r"\b(?:COUNTROWS|ALL|ALLSELECTED|VALUES|FILTER|RELATEDTABLE)\s*\(\s*"
                          r"(?:'((?:[^']|'')+)'|([A-Za-z_]\w*))\s*[,)]", re.I)


def dax_column_refs(expr: str) -> List[Tuple[str, str]]:
    expr = _strip_dax_comments_strings(expr)
    out = []
    for q, bare, col in _COLREF.findall(expr):
        tbl = (q.replace("''", "'") if q else bare)
        if tbl and tbl.upper() in _DAX_KEYWORDS:
            tbl = ""
        out.append((tbl, col))
    return out


def dax_table_refs(expr: str) -> Set[str]:
    expr = _strip_dax_comments_strings(expr)
    return {(q.replace("''", "'") if q else b) for q, b in _TABLE_FUNCS.findall(expr)
            if not (b and b.upper() in _DAX_KEYWORDS)}


_DAX_KEYWORDS = {"VAR", "RETURN", "IN", "AND", "OR", "NOT", "TRUE", "FALSE"}


def _strip_dax_comments_strings(expr: str) -> str:
    expr = re.sub(r"//[^\n]*|--[^\n]*", "", expr or "")
    expr = re.sub(r"/\*.*?\*/", "", expr, flags=re.S)
    return re.sub(r'"(?:[^"]|"")*"', '""', expr)


def translatable(expr: str) -> bool:
    from .sql_builder import DaxToSql  # local import to avoid a cycle
    return DaxToSql.is_simple(expr)


# ============================================================ TMDL parser
_OBJ_KW = {"model", "table", "column", "measure", "partition", "relationship", "hierarchy",
           "level", "calculationGroup", "calculationItem", "expression", "annotation",
           "extendedProperty", "role", "member", "perspective", "culture", "database", "dataSource",
           "ref", "tablePermission", "queryGroup", "variation", "linguisticMetadata",
           "calendar", "formatStringDefinition", "detailRowsDefinition"}


@dataclass
class _Node:
    kind: str
    name: str
    level: int
    expr: str = ""
    props: Dict[str, str] = field(default_factory=dict)
    children: List["_Node"] = field(default_factory=list)


def _level(line: str) -> int:
    lead = len(line) - len(line.lstrip("\t "))
    raw = line[:lead]
    return raw.count("\t") + raw.count(" ") // 4


def _split_name(rest: str) -> Tuple[str, Optional[str]]:
    rest = rest.strip()
    if rest.startswith("'"):
        i, name = 1, ""
        while i < len(rest):
            if rest[i] == "'" and i + 1 < len(rest) and rest[i + 1] == "'":
                name += "'"
                i += 2
                continue
            if rest[i] == "'":
                i += 1
                break
            name += rest[i]
            i += 1
        tail = rest[i:].strip()
    else:
        m = re.match(r"([^=\s]+)(.*)$", rest)
        name, tail = (m.group(1), m.group(2).strip()) if m else (rest, "")
    if tail.startswith("="):
        return name, tail[1:].strip()
    return name, None


def parse_tmdl(text: str) -> List[_Node]:
    lines = text.replace("\r\n", "\n").split("\n")
    roots: List[_Node] = []
    stack: List[_Node] = []
    i = 0
    while i < len(lines):
        raw = lines[i]
        s = raw.strip()
        if not s or s.startswith("///"):
            i += 1
            continue
        lvl = _level(raw)
        while stack and stack[-1].level >= lvl:
            stack.pop()
        word = s.split(None, 1)[0]
        prop = re.match(r"^([A-Za-z_]\w*)\s*:\s*(.*)$", s)
        if word in _OBJ_KW and not prop:
            name, expr = _split_name(s[len(word):])
            node = _Node(word, name, lvl)
            i, node.expr = _collect_expr(lines, i, lvl, expr)
            (stack[-1].children if stack else roots).append(node)
            stack.append(node)
            continue
        if prop and stack:
            stack[-1].props[prop.group(1)] = prop.group(2).strip()
            i += 1
            continue
        if re.fullmatch(r"[A-Za-z_]\w*", s) and stack:   # bare flag, e.g. isHidden
            stack[-1].props[s] = "true"
            i += 1
            continue
        peq = re.match(r"^([A-Za-z_]\w*)\s*=\s*(.*)$", s)
        if peq and stack:
            i, val = _collect_expr(lines, i, lvl, peq.group(2))
            stack[-1].props[peq.group(1)] = val
            continue
        i += 1
    return roots


def _collect_expr(lines: List[str], i: int, lvl: int, first: Optional[str]) -> Tuple[int, str]:
    """Collect an expression starting on line i (owner at level lvl)."""
    if first is None:
        return i + 1, ""
    first = first.strip()
    if first.startswith("```"):
        body, i = [], i + 1
        while i < len(lines) and not lines[i].strip().startswith("```"):
            body.append(lines[i])
            i += 1
        return i + 1, _dedent(body)
    body = [first] if first else []
    i += 1
    while i < len(lines):
        s = lines[i].strip()
        if s and _level(lines[i]) <= lvl + 1:
            break
        body.append(lines[i])
        i += 1
    while body and not body[-1].strip():
        body.pop()
    return i, _dedent(body) if not first else (first + ("\n" + _dedent(body[1:]) if len(body) > 1 else ""))


def _dedent(lines: List[str]) -> str:
    nonblank = [l for l in lines if l.strip()]
    if not nonblank:
        return ""
    cut = min(len(l) - len(l.lstrip("\t ")) for l in nonblank)
    return "\n".join(l[cut:] for l in lines).strip()


def _strip_q(v: str) -> str:
    v = v.strip()
    if len(v) >= 2 and v[0] == v[-1] == '"':
        return v[1:-1]
    return v


def _split_colref(ref: str) -> Tuple[str, str]:
    """'Sales Order'.'Order Key'  |  Sales.CustomerKey  -> (table, column)."""
    ref = ref.strip()
    m = re.match(r"^(?:'((?:[^']|'')+)'|([^.']+))\.(?:'((?:[^']|'')+)'|(.+))$", ref)
    if not m:
        return ref, ""
    t = (m.group(1) or m.group(2)).replace("''", "'")
    c = (m.group(3) or m.group(4)).replace("''", "'")
    return t, c


def model_from_tmdl(parts: Dict[str, str]) -> SemanticModel:
    model = SemanticModel(origin="tmdl")
    nodes: List[_Node] = []
    for path, text in parts.items():
        if path.endswith(".tmdl"):
            nodes.extend(parse_tmdl(text))
    for n in nodes:
        if n.kind == "model":
            model.name = n.name
        if n.kind == "expression":
            model.parameters[n.name] = n.expr
        if n.kind == "role":
            model.roles.append(n.name)
            model.role_filters[n.name] = {c.name: c.expr for c in n.children
                                          if c.kind == "tablePermission"}
            model.role_members[n.name] = [c.name for c in n.children if c.kind == "member"]
        if n.kind == "table":
            t = Table(n.name, is_hidden="isHidden" in n.props)
            for ch in n.children:
                hidden = "isHidden" in ch.props
                if ch.kind == "column":
                    t.columns[ch.name] = Column(n.name, ch.name, ch.props.get("dataType", ""),
                                                _strip_q(ch.props.get("sourceColumn", "")),
                                                ch.expr or "", hidden)
                elif ch.kind == "measure":
                    t.measures[ch.name] = Measure(n.name, ch.name, ch.expr,
                                                  _strip_q(ch.props.get("formatString", "")), hidden)
                elif ch.kind == "partition":
                    src = ch.props.get("source", "")
                    kind = (ch.expr or "").strip()
                    if kind == "calculated":
                        t.is_calculated, t.calc_expression = True, src
                    t.source = SourceInfo(mode=ch.props.get("mode", "import"), m_expression=src)
            nodes_rel = [x for x in n.children if x.kind == "relationship"]
            model.tables[n.name] = t
            nodes.extend(nodes_rel)
        if n.kind == "relationship":
            ft, fc = _split_colref(n.props.get("fromColumn", ""))
            tt, tc = _split_colref(n.props.get("toColumn", ""))
            if ft and tt:
                model.relationships.append(Relationship(
                    ft, fc, tt, tc, n.props.get("isActive", "true") != "false",
                    n.props.get("crossFilteringBehavior", "oneDirection"),
                    n.props.get("fromCardinality", "many"), n.props.get("toCardinality", "one")))
    _resolve_sources(model)
    return model


# ============================================================= BIM parser
def _j(v) -> str:
    return "\n".join(v) if isinstance(v, list) else (v or "")


def model_from_bim(text: str) -> SemanticModel:
    doc = json.loads(text)
    m = doc.get("model", doc)
    model = SemanticModel(name=doc.get("name", ""), origin="bim")
    for e in m.get("expressions", []):
        model.parameters[e["name"]] = _j(e.get("expression"))
    for tj in m.get("tables", []):
        t = Table(tj["name"], is_hidden=tj.get("isHidden", False))
        for c in tj.get("columns", []):
            if c.get("type") == "rowNumber":
                continue
            t.columns[c["name"]] = Column(t.name, c["name"], c.get("dataType", ""),
                                          c.get("sourceColumn", ""), _j(c.get("expression")),
                                          c.get("isHidden", False))
        for ms in tj.get("measures", []):
            t.measures[ms["name"]] = Measure(t.name, ms["name"], _j(ms.get("expression")),
                                             ms.get("formatString", ""), ms.get("isHidden", False))
        for p in tj.get("partitions", [])[:1]:
            src = p.get("source", {})
            if src.get("type") == "calculated":
                t.is_calculated, t.calc_expression = True, _j(src.get("expression"))
            t.source = SourceInfo(mode=p.get("mode", "import"), m_expression=_j(src.get("expression")))
        model.tables[t.name] = t
    model.roles = [r["name"] for r in m.get("roles", []) if r.get("name")]
    model.role_filters = {r["name"]: {tp.get("name"): _j(tp.get("filterExpression"))
                                      for tp in r.get("tablePermissions", []) or []}
                          for r in m.get("roles", []) if r.get("name")}
    model.role_members = {r["name"]: [mb.get("memberName", "") for mb in r.get("members", []) or []]
                          for r in m.get("roles", []) if r.get("name")}
    for r in m.get("relationships", []):
        model.relationships.append(Relationship(
            r["fromTable"], r["fromColumn"], r["toTable"], r["toColumn"], r.get("isActive", True),
            r.get("crossFilteringBehavior", "oneDirection"),
            r.get("fromCardinality", "many"), r.get("toCardinality", "one")))
    _resolve_sources(model)
    return model


def model_from_definition(parts: Dict[str, str]) -> SemanticModel:
    if any(p.endswith(".tmdl") for p in parts):
        return model_from_tmdl(parts)
    bim = next((v for k, v in parts.items() if k.endswith("model.bim")), None)
    if bim:
        return model_from_bim(bim)
    raise ValueError("Definition contains neither TMDL nor model.bim")


# ============================================================ INFO parser
def _g(row: dict, *names, default=""):
    for n in names:
        for k, v in row.items():
            if k.endswith(f"[{n}]") or k == n:
                return v if v is not None else default
    return default


def model_from_info(run_dax: Callable[[str], List[dict]]) -> SemanticModel:
    """Fallback: read metadata with DAX INFO functions through executeQueries."""
    model = SemanticModel(origin="info")
    try:
        tables = {r_id: name for r_id, name in
                  ((_g(r, "ID"), _g(r, "Name")) for r in run_dax("EVALUATE INFO.TABLES()"))}
        for tid, name in tables.items():
            model.tables[name] = Table(name)
        colname: Dict = {}
        for r in run_dax("EVALUATE INFO.COLUMNS()"):
            if _g(r, "Type") == 3:
                continue
            tname = tables.get(_g(r, "TableID"))
            if not tname:
                continue
            name = _g(r, "ExplicitName") or _g(r, "InferredName")
            colname[_g(r, "ID")] = (tname, name)
            model.tables[tname].columns[name] = Column(
                tname, name, str(_g(r, "ExplicitDataType")), _g(r, "SourceColumn"),
                _g(r, "Expression"), bool(_g(r, "IsHidden", default=False)))
        for r in run_dax("EVALUATE INFO.MEASURES()"):
            tname = tables.get(_g(r, "TableID"))
            if tname:
                model.tables[tname].measures[_g(r, "Name")] = Measure(
                    tname, _g(r, "Name"), _g(r, "Expression"), _g(r, "FormatString"))
        for r in run_dax("EVALUATE INFO.RELATIONSHIPS()"):
            f, t = colname.get(_g(r, "FromColumnID")), colname.get(_g(r, "ToColumnID"))
            if f and t:
                model.relationships.append(Relationship(
                    f[0], f[1], t[0], t[1], bool(_g(r, "IsActive", default=True)),
                    "bothDirections" if _g(r, "CrossFilteringBehavior") == 2 else "oneDirection"))
        for r in run_dax("EVALUATE INFO.PARTITIONS()"):
            tname = tables.get(_g(r, "TableID"))
            if tname:
                model.tables[tname].source = SourceInfo(m_expression=_g(r, "QueryDefinition"))
        for r in run_dax("EVALUATE INFO.EXPRESSIONS()"):
            model.parameters[_g(r, "Name")] = _g(r, "Expression")
        try:
            role_ids = {_g(r, "ID"): _g(r, "Name") for r in run_dax("EVALUATE INFO.ROLES()")}
            model.roles = list(role_ids.values())
            model.role_filters = {n: {} for n in model.roles}
            for r in run_dax("EVALUATE INFO.TABLEPERMISSIONS()"):
                rn, tn = role_ids.get(_g(r, "RoleID")), tables.get(_g(r, "TableID"))
                if rn and tn:
                    model.role_filters[rn][tn] = _g(r, "FilterExpression")
        except Exception:  # noqa: BLE001
            pass
    except Exception as exc:  # noqa: BLE001 - fall back to the INFO.VIEW family
        model = _model_from_info_view(run_dax)
        model.warnings.append(f"INFO.* needs admin on the model ({exc}); used INFO.VIEW.* "
                              "instead - source (M) mapping unavailable, map tables manually.")
    _resolve_sources(model)
    return model


def _model_from_info_view(run_dax) -> SemanticModel:
    model = SemanticModel(origin="info.view")
    for r in run_dax("EVALUATE INFO.VIEW.TABLES()"):
        model.tables[_g(r, "Name")] = Table(_g(r, "Name"))
    for r in run_dax("EVALUATE INFO.VIEW.COLUMNS()"):
        t = model.tables.setdefault(_g(r, "Table"), Table(_g(r, "Table")))
        t.columns[_g(r, "Name")] = Column(t.name, _g(r, "Name"), str(_g(r, "DataType")),
                                          _g(r, "SourceColumn"), _g(r, "Expression"))
    for r in run_dax("EVALUATE INFO.VIEW.MEASURES()"):
        t = model.tables.setdefault(_g(r, "Table"), Table(_g(r, "Table")))
        t.measures[_g(r, "Name")] = Measure(t.name, _g(r, "Name"), _g(r, "Expression"))
    for r in run_dax("EVALUATE INFO.VIEW.RELATIONSHIPS()"):
        rel = _g(r, "Relationship")
        m = re.match(r"\s*'?(.+?)'?\[(.+?)\]\s*\S+\s*'?(.+?)'?\[(.+?)\]", rel or "")
        if m:
            model.relationships.append(Relationship(m.group(1), m.group(2), m.group(3), m.group(4),
                                                    bool(_g(r, "IsActive", default=True))))
    return model


# ====================================================== M source resolver
_STR = r'"((?:[^"]|"")*)"'


def _m_str_or_param(token: str, params: Dict[str, str]) -> str:
    token = token.strip()
    m = re.match(_STR, token)
    if m:
        return m.group(1).replace('""', '"')
    p = params.get(token.strip("#\"").strip())
    if p:
        pm = re.match(r"\s*" + _STR, p)
        if pm:
            return pm.group(1).replace('""', '"')
    return token


_ARG = r'("(?:[^"]|"")*"|#"(?:[^"]|"")*"|[A-Za-z_][\w.]*)'


def _rec_value(rec: str, key: str, params: Dict[str, str]) -> str:
    m = re.search(r"\b" + key + r"\s*=\s*" + _ARG, rec)
    return _m_str_or_param(m.group(1), params) if m else ""


def _simple_from(sql: str) -> Optional[str]:
    """'SELECT * FROM db.schema.tbl' (optionally WHERE ...) -> 'db.schema.tbl'."""
    m = re.match(r"\s*select\s+\*\s+from\s+([\w.\"\[\]$]+)\s*(?:;|\s*$)", sql, re.I)
    return m.group(1) if m else None


def parse_m_source(m_expr: str, params: Dict[str, str],
                   shared: Optional[Dict[str, str]] = None) -> SourceInfo:
    """Work out the database object a Power Query (M) partition reads.

    Handles Snowflake.Databases, Sql.Database (incl. [Query="..."]), ODBC,
    Value.NativeQuery, navigation records in any key order, and partitions that
    start from a shared query (expressions.tmdl) - those are inlined first.
    """
    si = SourceInfo(m_expression=m_expr)
    if not m_expr:
        return si
    text = _inline_shared(m_expr, shared or {})
    arg = _ARG
    sf = re.search(r"Snowflake\.Databases\s*\(\s*" + arg + r"\s*,\s*" + arg, text)
    sq = re.search(r"\b(Sql\.Databases?|AzureDataExplorer\.Contents|AmazonRedshift\.Database|"
                   r"GoogleBigQuery\.Database|Databricks\.Catalogs|Odbc\.DataSource|Odbc\.Query|"
                   r"Oracle\.Database|PostgreSQL\.Database)\s*\(\s*" + arg + r"(?:\s*,\s*" + arg + r")?",
                   text)
    if sf:
        si.connector = "Snowflake"
        si.server = _m_str_or_param(sf.group(1), params)
        si.warehouse = _m_str_or_param(sf.group(2), params)
    elif sq:
        si.connector = sq.group(1)
        si.server = _m_str_or_param(sq.group(2), params)
        if sq.group(1) == "Sql.Database" and sq.group(3):
            si.database = _m_str_or_param(sq.group(3), params)
        if sq.group(1) == "Odbc.Query" and sq.group(3):
            si.native_query = _m_str_or_param(sq.group(3), params)
        si.notes.append(f"Model reads through {sq.group(1)} (not the Snowflake connector); "
                        "the SQL targets the same database/schema/table names in Snowflake.")
    # navigation records: {[Name="X", Kind="Database"]}, {[Schema="dbo", Item="T"]}, in any order
    for rec in re.findall(r"\{\s*\[([^\[\]]*)\]\s*\}", text):
        kind = _rec_value(rec, "Kind", params)
        name = _rec_value(rec, "Name", params)
        if kind and name:
            k = kind.lower()
            if k == "database" and not si.database:
                si.database = name
            elif k == "schema":
                si.schema = name
            elif k in ("table", "view", "function"):
                si.object_name = name
            continue
        sch, item = _rec_value(rec, "Schema", params), _rec_value(rec, "Item", params)
        if item:
            si.schema, si.object_name = sch or si.schema, item
    # native SQL: Value.NativeQuery(src, "sql") or Sql.Database(..., [Query="sql"])
    nq = re.search(r"Value\.NativeQuery\s*\([^,]+,\s*" + _STR, text, re.S) or \
        re.search(r"\bQuery\s*=\s*" + _STR, text, re.S)
    if nq and not si.native_query:
        si.native_query = nq.group(1)
    if si.native_query:
        si.native_query = si.native_query.replace('""', '"').replace("#(lf)", "\n") \
            .replace("#(cr)", "").replace("#(tab)", "\t")
        simple = _simple_from(si.native_query)
        if simple:  # SELECT * FROM a.b.c -> just use the object
            parts = [p.strip('"[]') for p in simple.split(".")]
            si.object_name = parts[-1]
            if len(parts) >= 2:
                si.schema = parts[-2]
            if len(parts) >= 3:
                si.database = parts[-3]
            si.native_query = ""
    for rn in re.finditer(r"Table\.RenameColumns\s*\([^{]*\{(.*?)\}\s*\)", m_expr, re.S):
        for old, new in re.findall(r'\{\s*"((?:[^"]|"")*)"\s*,\s*"((?:[^"]|"")*)"\s*\}', rn.group(1)):
            si.renames[new] = old
    if "Table.SelectRows" in m_expr:
        si.notes.append("Power Query filters rows (Table.SelectRows) - add the same WHERE clause "
                        "to SQL if totals differ.")
    if re.search(r"Table\.(Group|Join|NestedJoin|Combine|AddColumn|Pivot|Unpivot)", m_expr):
        si.notes.append("Power Query reshapes data (group/join/add column) - generated SQL reads "
                        "the base object; verify.")
    return si


def _inline_shared(m_expr: str, shared: Dict[str, str], depth: int = 0) -> str:
    """Append the text of shared queries the partition refers to (recursively)."""
    if depth > 4 or not shared:
        return m_expr
    out = m_expr
    for name, body in shared.items():
        if re.match(r'\s*("(?:[^"]|"")*"|\d+(\.\d+)?|true|false)\s*(meta\b|$)', body or ""):
            continue  # plain parameter - handled by _m_str_or_param
        pat = r'#"' + re.escape(name) + r'"' if not re.match(r"^[A-Za-z_]\w*$", name) else \
            r'(?<![\w"])' + re.escape(name) + r'(?![\w"])'
        if re.search(pat, m_expr):
            out += "\n" + _inline_shared(body, {k: v for k, v in shared.items() if k != name},
                                         depth + 1)
    return out


def _resolve_sources(model: SemanticModel) -> None:
    for t in model.tables.values():
        if t.is_calculated:
            continue
        mode = t.source.mode
        if t.source.m_expression:
            t.source = parse_m_source(t.source.m_expression, model.parameters, model.parameters)
            t.source.mode = mode


# =============================================================== diff
def _norm(expr: str) -> str:
    return re.sub(r"\s+", " ", expr or "").strip()


def diff_models(prod: SemanticModel, test: SemanticModel) -> pd.DataFrame:
    rows = []

    def add(kind, obj, status, p="", t=""):
        rows.append({"Object type": kind, "Object": obj, "Status": status,
                     "Production": p, "Test": t})

    for name in sorted(set(prod.tables) | set(test.tables)):
        pt, tt = prod.tables.get(name), test.tables.get(name)
        if not pt or not tt:
            add("Table", name, "Only in Production" if pt else "Only in Test")
            continue
        if pt.source.fqn != tt.source.fqn or _norm(pt.source.native_query) != _norm(tt.source.native_query):
            add("Table source", name, "Different", pt.source.fqn or pt.source.native_query[:200],
                tt.source.fqn or tt.source.native_query[:200])
        for cn in sorted(set(pt.columns) | set(tt.columns)):
            pc, tc = pt.columns.get(cn), tt.columns.get(cn)
            if not pc or not tc:
                add("Column", f"{name}[{cn}]", "Only in Production" if pc else "Only in Test")
            elif pc.data_type != tc.data_type:
                add("Column type", f"{name}[{cn}]", "Different", pc.data_type, tc.data_type)
            elif _norm(pc.expression) != _norm(tc.expression):
                add("Calculated column", f"{name}[{cn}]", "Different", pc.expression, tc.expression)
            elif pc.source_column != tc.source_column:
                add("Column source", f"{name}[{cn}]", "Different", pc.source_column, tc.source_column)
        for mn in sorted(set(pt.measures) | set(tt.measures)):
            pm, tm = pt.measures.get(mn), tt.measures.get(mn)
            if not pm or not tm:
                add("Measure", f"[{mn}]", "Only in Production" if pm else "Only in Test",
                    pm.expression if pm else "", tm.expression if tm else "")
            elif _norm(pm.expression) != _norm(tm.expression):
                add("Measure DAX", f"[{mn}]", "Different", pm.expression, tm.expression)
            elif pm.format_string != tm.format_string:
                add("Measure format", f"[{mn}]", "Different", pm.format_string, tm.format_string)
    pr = {r.key(): r for r in prod.relationships}
    tr = {r.key(): r for r in test.relationships}
    for k in sorted(set(pr) | set(tr)):
        a, b = pr.get(k), tr.get(k)
        if not a or not b:
            add("Relationship", k, "Only in Production" if a else "Only in Test")
        elif (a.is_active, a.cross_filter) != (b.is_active, b.cross_filter):
            add("Relationship", k, "Different", f"active={a.is_active}, {a.cross_filter}",
                f"active={b.is_active}, {b.cross_filter}")
    for p in sorted(set(prod.parameters) | set(test.parameters)):
        if _norm(prod.parameters.get(p, "")) != _norm(test.parameters.get(p, "")):
            add("M parameter", p, "Different", prod.parameters.get(p, "")[:200],
                test.parameters.get(p, "")[:200])
    return pd.DataFrame(rows, columns=["Object type", "Object", "Status", "Production", "Test"])


def _sf_ident(name: str) -> str:
    if re.match(r"^[A-Z_][A-Z0-9_$]*$", name):
        return name
    return '"' + name.replace('"', '""') + '"'
