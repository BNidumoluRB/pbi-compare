"""Turn a parsed visual (fields + effective filters) into an executable DAX query."""
from __future__ import annotations

import re
from dataclasses import dataclass, field
from datetime import datetime
from typing import List, Optional, Sequence, Tuple

from .report_parser import Field, FilterSpec, Visual, parse_field, spec_from_json


def tq(table: str) -> str:
    return "'" + table.replace("'", "''") + "'"


def cq(col: str) -> str:
    return "[" + col.replace("]", "]]") + "]"


def field_dax(f: Field) -> str:
    if f.kind == "measure":
        return cq(f.name)
    col = f"{tq(f.table)}{cq(f.name)}"
    if f.variation and f.hierarchy:
        col += f".{cq(f.hierarchy)}"
    if f.kind == "aggregation":
        if f.agg == "COUNT":
            return f"COUNTA({col})"
        return f"{f.agg}({col})"
    return col


def dax_string(s: str) -> str:
    return '"' + s.replace('"', '""') + '"'


def literal_dax(lit: str) -> str:
    """Power BI query literal -> DAX literal."""
    v = str(lit)
    if v == "null":
        return "BLANK()"
    if v in ("true", "false"):
        return v.upper() + "()"
    if v.startswith("'") and v.endswith("'"):
        return dax_string(v[1:-1].replace("''", "'"))
    m = re.match(r"^datetime'(.+)'$", v)
    if m:
        d = datetime.fromisoformat(m.group(1).replace("Z", "")[:19])
        base = f"DATE({d.year},{d.month},{d.day})"
        return base if (d.hour, d.minute, d.second) == (0, 0, 0) else \
            f"({base}+TIME({d.hour},{d.minute},{d.second}))"
    m = re.match(r"^(-?\d+(?:\.\d+)?)[LDM]?$", v)
    if m:
        return m.group(1)
    return dax_string(v)


def literal_python(lit: str):
    v = str(lit)
    if v == "null":
        return None
    if v in ("true", "false"):
        return v == "true"
    if v.startswith("'") and v.endswith("'"):
        return v[1:-1].replace("''", "'")
    m = re.match(r"^datetime'(.+)'$", v)
    if m:
        return m.group(1)
    m = re.match(r"^(-?\d+(?:\.\d+)?)[LDM]?$", v)
    if m:
        return float(m.group(1)) if "." in m.group(1) else int(m.group(1))
    return v


class UnsupportedFilter(Exception):
    pass


@dataclass
class _Cond:
    dax: str
    columns: List[Tuple[str, str]] = field(default_factory=list)  # (table, column)
    measures: List[str] = field(default_factory=list)              # measure-level DAX
    simple_in: Optional[Tuple[str, List[str]]] = None               # (col dax, literals)


CMP = {0: "=", 1: ">", 2: ">=", 3: "<", 4: "<="}


class FilterTranslator:
    def __init__(self, from_list: Sequence[dict]):
        self.aliases = {f["Name"]: f.get("Entity", "") for f in from_list if "Entity" in f}
        self.subqueries = {f["Name"]: f["Expression"]["Subquery"]["Query"] for f in from_list
                           if "Expression" in f and "Subquery" in f.get("Expression", {})}

    def expr(self, e: dict) -> Tuple[str, List[Tuple[str, str]], List[str]]:
        if "Literal" in e:
            return literal_dax(e["Literal"]["Value"]), [], []
        f = parse_field(e, self.aliases)
        if f.kind == "unsupported":
            raise UnsupportedFilter(f"expression {list(e)[0]}")
        d = field_dax(f)
        if f.is_measure_like:
            return d, [], [d]
        return d, [(f.table, f.name)], []

    def cond(self, c: dict) -> _Cond:
        if "And" in c or "Or" in c:
            op = "And" if "And" in c else "Or"
            l, r = self.cond(c[op]["Left"]), self.cond(c[op]["Right"])
            return _Cond(f"({l.dax} {'&&' if op == 'And' else '||'} {r.dax})",
                         l.columns + r.columns, l.measures + r.measures)
        if "Not" in c:
            inner = self.cond(c["Not"]["Expression"])
            return _Cond(f"NOT({inner.dax})", inner.columns, inner.measures)
        if "In" in c:
            i = c["In"]
            exprs = [self.expr(e) for e in i["Expressions"]]
            cols = [x for e in exprs for x in e[1]]
            meas = [x for e in exprs for x in e[2]]
            if "Table" in i:  # TopN filter: col IN TOPN(n, VALUES(col), order)
                sq = self.subqueries.get(i["Table"]["SourceRef"].get("Source", ""))
                if not sq:
                    raise UnsupportedFilter("In-table filter without subquery")
                return _Cond(f"{exprs[0][0]} IN {self._topn(sq, exprs[0][0])}", cols, meas)
            if len(exprs) == 1:
                vals = [literal_dax(v[0]["Literal"]["Value"]) for v in i["Values"]]
                has_null = "BLANK()" in vals
                d = f"{exprs[0][0]} IN {{{', '.join(vals)}}}"
                if has_null:
                    d = f"({d} || ISBLANK({exprs[0][0]}))"
                return _Cond(d, cols, meas, None if has_null else (exprs[0][0], vals))
            tuples = ["(" + ", ".join(literal_dax(x["Literal"]["Value"]) for x in v) + ")"
                      for v in i["Values"]]
            return _Cond(f"({', '.join(e[0] for e in exprs)}) IN {{{', '.join(tuples)}}}",
                         cols, meas)
        if "Comparison" in c:
            cp = c["Comparison"]
            if "DateSpan" in cp["Right"] or "DateAdd" in cp["Right"] or "Now" in cp["Right"]:
                raise UnsupportedFilter("relative date filter")
            l, lc, lm = self.expr(cp["Left"])
            r, rc, rm = self.expr(cp["Right"])
            op = CMP.get(cp.get("ComparisonKind", 0), "=")
            if r == "BLANK()" and op == "=":
                return _Cond(f"ISBLANK({l})", lc, lm)
            return _Cond(f"{l} {op} {r}", lc + rc, lm + rm)
        if "Between" in c:
            b = c["Between"]
            e, ec, em = self.expr(b["Expression"])
            lo, hi = self.expr(b["LowerBound"])[0], self.expr(b["UpperBound"])[0]
            return _Cond(f"({e} >= {lo} && {e} <= {hi})", ec, em)
        if "Contains" in c or "StartsWith" in c or "EndsWith" in c:
            k = next(x for x in ("Contains", "StartsWith", "EndsWith") if x in c)
            l, lc, lm = self.expr(c[k]["Left"])
            r = self.expr(c[k]["Right"])[0]
            d = {"Contains": f"CONTAINSSTRING({l}, {r})",
                 "StartsWith": f"LEFT({l}, LEN({r})) = {r}",
                 "EndsWith": f"RIGHT({l}, LEN({r})) = {r}"}[k]
            return _Cond(d, lc, lm)
        raise UnsupportedFilter(f"condition {list(c)[0]}")

    def _topn(self, q: dict, col_dax: str) -> str:
        sub = FilterTranslator(q.get("From", []))
        top = q.get("Top", 10)
        order = q.get("OrderBy", [{}])[0]
        oexpr = sub.expr(order.get("Expression", {}))[0]
        direction = "DESC" if order.get("Direction", 2) == 2 else "ASC"
        return f"TOPN({top}, VALUES({col_dax}), {oexpr}, {direction})"


@dataclass
class DaxQuery:
    dax: str
    columns: List[Tuple[str, str]]     # (result key guess, display label)
    group_labels: List[str]
    value_labels: List[str]
    measure_filter_cols: List[str]
    warnings: List[str]
    filters_applied: List[str]


def filter_tables(filters: Sequence[FilterSpec]) -> Tuple[List[str], List[str], List[str], List[str]]:
    """-> (filter-table DAX, measure-filter conditions, warnings, descriptions)."""
    tables, mconds, warns, desc = [], [], [], []
    for fs in filters:
        try:
            tr = FilterTranslator(fs.filter_json.get("From", []))
            for w in fs.filter_json.get("Where", []):
                c = tr.cond(w["Condition"])
                desc.append(f"[{fs.level}] {c.dax}")
                if c.measures:
                    mconds.append(c.dax)
                    continue
                if c.simple_in:
                    tables.append(f"TREATAS({{{', '.join(c.simple_in[1])}}}, {c.simple_in[0]})")
                    continue
                cols = list(dict.fromkeys(c.columns))
                alls = ", ".join(f"{tq(t)}{cq(col)}" for t, col in cols)
                if len({t for t, _ in cols}) <= 1:
                    tables.append(f"FILTER(ALL({alls}), {c.dax})")
                else:
                    cj = ", ".join(f"ALL({tq(t)}{cq(col)})" for t, col in cols)
                    tables.append(f"FILTER(CROSSJOIN({cj}), {c.dax})")
        except UnsupportedFilter as exc:
            warns.append(f"{fs.level} filter '{fs.description or fs.name}' skipped ({exc}).")
        except (KeyError, IndexError, TypeError) as exc:
            warns.append(f"{fs.level} filter '{fs.description or fs.name}' could not be parsed "
                         f"({exc}).")
    return tables, mconds, warns, desc


def build_visual_query(visual: Visual, filters: Sequence[FilterSpec],
                       max_rows: int = 50000) -> DaxQuery:
    groups = [f for f in visual.group_fields]
    values = [f for f in visual.value_fields]
    warns = list(visual.warnings)
    ftables, mconds, fw, fdesc = filter_tables(filters)
    warns += fw

    # de-duplicate fields that appear in two roles
    seen, g_unique = set(), []
    for f in groups:
        d = field_dax(f)
        if d not in seen:
            seen.add(d)
            g_unique.append(f)
    labels, v_unique = set(), []
    for f in values:
        lab = f.label
        while lab in labels:
            lab += " "
        labels.add(lab)
        v_unique.append((f, lab))

    mf_cols = []
    args = [field_dax(f) for f in g_unique] + ftables
    for f, lab in v_unique:
        args.append(f"{dax_string(lab)}, {field_dax(f)}")
    for i, cond in enumerate(mconds):
        # visual-level measure filters are evaluated per row (group) of the visual
        name = f"__mf{i}"
        mf_cols.append(name)
        args.append(f"{dax_string(name)}, IF({cond}, 1)")
    body = "SUMMARIZECOLUMNS(\n    " + ",\n    ".join(args) + "\n)" if args else "{BLANK()}"
    if mf_cols:
        body = f"FILTER(\n{body},\n    " + " && ".join(f"NOT ISBLANK([{c}])" for c in mf_cols) + ")"
    order = ", ".join(field_dax(f) for f in g_unique)
    dax = "EVALUATE\n" + body
    if order:
        dax += f"\nORDER BY {order}"
    cols = [(f"{f.table}[{f.name}]", f.label) for f in g_unique] + \
           [(f"[{lab}]", lab.rstrip()) for _, lab in v_unique]
    return DaxQuery(dax, cols, [f.label for f in g_unique], [lab.rstrip() for _, lab in v_unique],
                    mf_cols, warns, fdesc)


def rows_to_frame(rows: List[dict], q: DaxQuery):
    import pandas as pd
    df = pd.DataFrame(rows)
    if df.empty:
        return pd.DataFrame(columns=[lab for _, lab in q.columns])
    df = df[[c for c in df.columns if not any(c.endswith(f"[{m}]") for m in q.measure_filter_cols)]]
    rename = {}
    remaining = list(df.columns)
    for key, lab in q.columns:
        hit = next((c for c in remaining if c == key or c.endswith(key) or
                    c.replace("'", "") == key), None)
        if hit:
            rename[hit] = lab
            remaining.remove(hit)
    if len(rename) < len(q.columns) and len(df.columns) == len(q.columns):
        rename = {c: lab for c, (_, lab) in zip(df.columns, q.columns)}  # positional fallback
    return df.rename(columns=rename)


# ============================================================ user filters
@dataclass
class UserFilter:
    """A filter the owner adds in the UI that is applied to BOTH reports."""
    table: str
    column: str
    values: List[str]
    data_type: str = "text"   # text | number | date

    def to_spec(self) -> FilterSpec:
        def lit(v):
            if self.data_type == "number":
                return f"{v}D"
            if self.data_type == "date":
                return f"datetime'{v}T00:00:00'"
            return "'" + str(v).replace("'", "''") + "'"
        fj = {"Version": 2, "From": [{"Name": "u", "Entity": self.table, "Type": 0}],
              "Where": [{"Condition": {"In": {
                  "Expressions": [{"Column": {"Expression": {"SourceRef": {"Source": "u"}},
                                              "Property": self.column}}],
                  "Values": [[{"Literal": {"Value": lit(v)}}] for v in self.values]}}}]}
        return spec_from_json("user", fj, f"{self.table}.{self.column}", "Categorical",
                              f"{self.table}[{self.column}] in {self.values}")

    def to_export_filter(self) -> str:
        t = self.table.replace(" ", "_x0020_")
        c = self.column.replace(" ", "_x0020_")
        if self.data_type == "number":
            vals = ",".join(str(v) for v in self.values)
        else:
            vals = ",".join("'" + str(v).replace("'", "''") + "'" for v in self.values)
        return f"{t}/{c} in ({vals})"

    def to_js_filter(self) -> dict:
        vals = [float(v) if self.data_type == "number" else v for v in self.values]
        return {"$schema": "http://powerbi.com/product/schema#basic",
                "target": {"table": self.table, "column": self.column},
                "operator": "In", "values": vals, "filterType": 1}


# ===================================================== page filter choices
MONTHS = ["january", "february", "march", "april", "may", "june", "july", "august",
          "september", "october", "november", "december"]


def field_to_expr(f: Field) -> dict:
    """Canonical (entity-based) query expression for a column / hierarchy level."""
    src = {"SourceRef": {"Entity": f.table}}
    if f.variation:
        return {"HierarchyLevel": {"Expression": {"Hierarchy": {
            "Expression": {"PropertyVariationSource": {"Expression": src, "Name": "Variation",
                                                       "Property": f.name}},
            "Hierarchy": f.hierarchy_name or "Date Hierarchy"}}, "Level": f.hierarchy}}
    if f.kind == "hierarchy":
        return {"HierarchyLevel": {"Expression": {"Hierarchy": {"Expression": src,
                                                                "Hierarchy": f.hierarchy}},
                                   "Level": f.name}}
    return {"Column": {"Expression": src, "Property": f.name}}


_ISO = re.compile(r"^\d{4}-\d{2}-\d{2}(T\d{2}:\d{2}(:\d{2}(\.\d+)?)?)?Z?$")


def py_literal(v) -> str:
    """Python value (as returned by executeQueries) -> Power BI query literal."""
    if v is None:
        return "null"
    if isinstance(v, bool):
        return "true" if v else "false"
    if isinstance(v, int):
        return f"{v}L"
    if isinstance(v, float):
        return f"{int(v)}L" if v.is_integer() else f"{v}D"
    s = str(v)
    if _ISO.match(s):
        d = s.rstrip("Z")
        if "T" not in d:
            d += "T00:00:00"
        return f"datetime'{d[:19]}'"
    return "'" + s.replace("'", "''") + "'"


def choice_spec(f: Field, values: Sequence) -> FilterSpec:
    fj = {"Version": 2, "From": [], "Where": [{"Condition": {"In": {
        "Expressions": [field_to_expr(f)],
        "Values": [[{"Literal": {"Value": py_literal(v)}}] for v in values]}}}]}
    shown = ", ".join(str(v) for v in list(values)[:6]) + (" …" if len(values) > 6 else "")
    return spec_from_json("choice", fj, f.key, "Categorical", f"{f.friendly} in [{shown}]")


def values_query(f: Field) -> str:
    d = field_dax(f)
    return f"EVALUATE\nSUMMARIZECOLUMNS(\n    {d}\n)\nORDER BY {d}"


def sort_values(values: List) -> List:
    low = [str(v).strip().lower() for v in values]
    if values and all(x in MONTHS or x[:3] in [m[:3] for m in MONTHS] for x in low if x):
        return sorted(values, key=lambda v: [m[:3] for m in MONTHS].index(str(v).strip().lower()[:3])
                      if v else -1)
    try:
        return sorted(values, key=lambda v: (v is None, v))
    except TypeError:
        return sorted(values, key=lambda v: (v is None, str(v)))


def fetch_values(run_dax, f: Field, limit: int = 2000) -> List:
    rows = run_dax(values_query(f))
    vals = [next(iter(r.values())) if r else None for r in rows[:limit]]
    return sort_values(vals)
