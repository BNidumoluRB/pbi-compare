"""Generate Snowflake SQL that should reproduce a visual's numbers.

Pipeline per visual:
  fields + filters + model  ->  base (fact) table + join path over relationships
  -> SELECT group columns, translated measures FROM ... JOIN ... WHERE ... GROUP BY

DAX measures are translated by a small DAX-subset compiler (``DaxToSql``):
SUM/AVERAGE/MIN/MAX/COUNT/COUNTA/COUNTROWS/DISTINCTCOUNT, the X iterators,
DIVIDE, IF, SWITCH(TRUE()), arithmetic, VAR/RETURN, nested measures and
CALCULATE with simple column filters (pushed into CASE WHEN). Anything else
is emitted as NULL with the original DAX in a TODO comment so a human can
finish it - the SQL is never silently wrong.
"""
from __future__ import annotations

import re
from collections import deque
from dataclasses import dataclass
from typing import Callable, Dict, List, Optional, Sequence, Set, Tuple

from .model import SemanticModel
from .report_parser import Field, FilterSpec, Visual, parse_field


class NotTranslatable(Exception):
    pass


# ================================================================ tokenizer
_TOKEN = re.compile(r"""
    (?P<ws>\s+|//[^\n]*|--[^\n]*|/\*.*?\*/)
  | (?P<str>"(?:[^"]|"")*")
  | (?P<num>\d+(?:\.\d+)?(?:[eE][+-]?\d+)?)
  | (?P<tbl>'(?:[^']|'')+')
  | (?P<col>\[(?:[^\]]|\]\])+\])
  | (?P<op>&&|\|\||<=|>=|<>|==|=|<|>|\+|-|\*|/|&|\^)
  | (?P<punc>[(),{}])
  | (?P<id>[A-Za-z_][\w.]*)
""", re.X | re.S)


def tokenize(expr: str) -> List[Tuple[str, str]]:
    out, pos = [], 0
    while pos < len(expr):
        m = _TOKEN.match(expr, pos)
        if not m:
            raise NotTranslatable(f"unexpected character {expr[pos]!r}")
        pos = m.end()
        kind = m.lastgroup
        if kind != "ws":
            out.append((kind, m.group()))
    return out


# ============================================================== translator
ColResolver = Callable[[str, str], str]          # (table, column) -> SQL
MeasureResolver = Callable[[str], Optional[str]]  # name -> DAX expression (or None)

AGG = {"SUM": "SUM", "AVERAGE": "AVG", "MIN": "MIN", "MAX": "MAX", "COUNT": "COUNT",
       "COUNTA": "COUNT", "DISTINCTCOUNT": "COUNT(DISTINCT", "MEDIAN": "MEDIAN",
       "DISTINCTCOUNTNOBLANK": "COUNT(DISTINCT"}
ITER = {"SUMX": "SUM", "AVERAGEX": "AVG", "MINX": "MIN", "MAXX": "MAX", "COUNTX": "COUNT"}
SCALAR = {"ABS": "ABS", "ROUND": "ROUND", "INT": "FLOOR", "SQRT": "SQRT", "UPPER": "UPPER",
          "LOWER": "LOWER", "LEN": "LENGTH", "YEAR": "YEAR", "MONTH": "MONTH", "DAY": "DAY",
          "COALESCE": "COALESCE", "ROUNDUP": "CEIL", "ROUNDDOWN": "FLOOR", "TRIM": "TRIM"}
OPS = {"&&": "AND", "||": "OR", "=": "=", "==": "=", "<>": "<>", "<": "<", ">": ">",
       "<=": "<=", ">=": ">=", "+": "+", "-": "-", "*": "*", "/": "/", "&": "||", "^": "^"}
PREC = {"||": 1, "&&": 2, "=": 3, "==": 3, "<>": 3, "<": 3, ">": 3, "<=": 3, ">=": 3,
        "&": 4, "+": 5, "-": 5, "*": 6, "/": 6, "^": 7}


def _unq_table(tok: str) -> str:
    return tok[1:-1].replace("''", "'") if tok.startswith("'") else tok


def _unq_col(tok: str) -> str:
    return tok[1:-1].replace("]]", "]")


class DaxToSql:
    def __init__(self, col: ColResolver, measure: MeasureResolver, home_table: str = "",
                 depth: int = 0):
        self.col = col
        self.measure = measure
        self.home = home_table
        self.depth = depth

    # ------------------------------------------------------------- public
    def translate(self, expr: str, conds: Sequence[str] = ()) -> str:
        self.toks = tokenize(expr)
        self.i = 0
        self.vars: Dict[str, str] = {}
        self.conds = list(conds)
        self.iter_table: Optional[str] = None
        out = self._statement()
        if self.i != len(self.toks):
            raise NotTranslatable(f"unexpected token {self.toks[self.i][1]!r}")
        return out

    @staticmethod
    def is_simple(expr: str) -> bool:
        try:
            DaxToSql(lambda t, c: "x", lambda m: "1").translate(expr)
            return True
        except NotTranslatable:
            return False
        except Exception:  # noqa: BLE001
            return False

    # ------------------------------------------------------------- parser
    def _peek(self, k=0):
        j = self.i + k
        return self.toks[j] if j < len(self.toks) else ("eof", "")

    def _take(self, val: Optional[str] = None):
        t = self._peek()
        if val is not None and t[1].upper() != val.upper():
            raise NotTranslatable(f"expected {val!r}, got {t[1]!r}")
        self.i += 1
        return t

    def _statement(self) -> str:
        while self._peek()[1].upper() == "VAR":
            self._take()
            name = self._take()[1]
            self._take("=")
            self.vars[name.upper()] = f"({self._expr()})"
        if self._peek()[1].upper() == "RETURN":
            self._take()
        return self._expr()

    def _expr(self, min_prec: int = 1) -> str:
        left = self._unary()
        while True:
            kind, val = self._peek()
            if kind == "id" and val.upper() == "IN" and min_prec <= 3:
                self._take()
                left = f"{left} IN ({self._set_literal()})"
                continue
            if kind != "op" or PREC[val] < min_prec:
                break
            prec = PREC[val]
            self._take()
            right = self._expr(prec + 1)
            if val == "/":
                left = f"{left} / NULLIF({right}, 0)"
            else:
                left = f"{left} {OPS[val]} {right}"
        return left

    def _set_literal(self) -> str:
        self._take("{")
        vals = [self._expr()]
        while self._peek()[1] == ",":
            self._take()
            vals.append(self._expr())
        self._take("}")
        return ", ".join(vals)

    def _unary(self) -> str:
        kind, val = self._peek()
        if val == "-":
            self._take()
            return f"-{self._unary()}"
        if val == "(":
            self._take()
            e = self._expr()
            self._take(")")
            return f"({e})"
        if kind == "num":
            self._take()
            return val
        if kind == "str":
            self._take()
            return "'" + val[1:-1].replace('""', '"').replace("'", "''") + "'"
        if kind == "tbl" or (kind == "id" and self._peek(1)[0] == "col"):
            self._take()
            col = _unq_col(self._take()[1])
            return self.col(_unq_table(val), col)
        if kind == "col":
            self._take()
            name = _unq_col(val)
            m = self.measure(name)
            if m is not None:
                if self.depth > 12:
                    raise NotTranslatable("measure recursion too deep")
                sub = DaxToSql(self.col, self.measure, self.home, self.depth + 1)
                return f"({sub.translate(m, self.conds)})"
            return self.col(self.iter_table or self.home, name)
        if kind == "id":
            up = val.upper()
            if up in self.vars:
                self._take()
                return self.vars[up]
            if self._peek(1)[1] == "(":
                return self._func(up)
            if up in ("TRUE", "FALSE"):
                self._take()
                return up
        raise NotTranslatable(f"unsupported token {val!r}")

    def _args_raw(self) -> List[List[Tuple[str, str]]]:
        """Split the tokens of a call into raw argument token lists."""
        self._take("(")
        args, cur, depth = [], [], 0
        while True:
            t = self._take()
            if t[0] == "eof":
                raise NotTranslatable("unbalanced parentheses")
            if t[1] in ("(", "{"):
                depth += 1
            elif t[1] in (")", "}"):
                if depth == 0 and t[1] == ")":
                    if cur:
                        args.append(cur)
                    return args
                depth -= 1
            if t[1] == "," and depth == 0:
                args.append(cur)
                cur = []
                continue
            cur.append(t)

    def _sub(self, toks, conds=None, iter_table=None) -> str:
        s = DaxToSql(self.col, self.measure, self.home, self.depth)
        s.toks, s.i, s.vars = toks, 0, self.vars
        s.conds = self.conds if conds is None else conds
        s.iter_table = iter_table if iter_table is not None else self.iter_table
        out = s._expr()
        if s.i != len(toks):
            raise NotTranslatable(f"unexpected token {toks[s.i][1]!r}")
        return out

    def _wrap(self, inner: str) -> str:
        if not self.conds:
            return inner
        return f"CASE WHEN {' AND '.join(self.conds)} THEN {inner} END"

    def _table_name(self, toks) -> str:
        if len(toks) != 1 or toks[0][0] not in ("tbl", "id"):
            raise NotTranslatable("table expression too complex (FILTER/VALUES/ALL)")
        return _unq_table(toks[0][1])

    def _func(self, name: str) -> str:
        self._take()
        args = self._args_raw()
        if name in AGG:
            col = self._sub(args[0])
            if AGG[name].startswith("COUNT(DISTINCT"):
                return f"COUNT(DISTINCT {self._wrap(col)})"
            return f"{AGG[name]}({self._wrap(col)})"
        if name == "COUNTROWS":
            self._table_name(args[0])
            return f"COUNT({self._wrap('1')})" if self.conds else "COUNT(*)"
        if name in ITER:
            tbl = self._table_name(args[0])
            inner = self._sub(args[1], iter_table=tbl)
            return f"{ITER[name]}({self._wrap(inner)})"
        if name == "DIVIDE":
            a, b = self._sub(args[0]), self._sub(args[1])
            alt = self._sub(args[2]) if len(args) > 2 else None
            q = f"{a} / NULLIF({b}, 0)"
            return f"COALESCE({q}, {alt})" if alt else f"({q})"
        if name == "IF":
            c = self._sub(args[0])
            a = self._sub(args[1])
            b = self._sub(args[2]) if len(args) > 2 else "NULL"
            return f"CASE WHEN {c} THEN {a} ELSE {b} END"
        if name == "SWITCH":
            if self._sub(args[0]) != "TRUE":
                subj = self._sub(args[0])
                pairs = [(f"{subj} = {self._sub(args[i])}", self._sub(args[i + 1]))
                         for i in range(1, len(args) - 1, 2)]
            else:
                pairs = [(self._sub(args[i]), self._sub(args[i + 1]))
                         for i in range(1, len(args) - 1, 2)]
            default = self._sub(args[-1]) if len(args) % 2 == 0 else "NULL"
            return "CASE " + " ".join(f"WHEN {c} THEN {v}" for c, v in pairs) + \
                f" ELSE {default} END"
        if name == "CALCULATE":
            conds = list(self.conds)
            for a in args[1:]:
                if a and a[0][1].upper() in ("ALL", "REMOVEFILTERS", "ALLSELECTED",
                                             "ALLEXCEPT", "USERELATIONSHIP", "CROSSFILTER"):
                    raise NotTranslatable(f"CALCULATE modifier {a[0][1]}")
                if a and a[0][1].upper() == "KEEPFILTERS":
                    a = a[2:-1]
                if a and a[0][1].upper() == "FILTER":
                    raise NotTranslatable("CALCULATE with FILTER()")
                conds.append(f"({self._sub(a, conds=[])})")
            return f"({self._sub(args[0], conds=conds)})"
        if name == "BLANK":
            return "NULL"
        if name in ("TRUE", "FALSE"):
            return name
        if name == "ISBLANK":
            return f"({self._sub(args[0])} IS NULL)"
        if name == "NOT":
            return f"NOT ({self._sub(args[0])})"
        if name in ("AND", "OR"):
            return f"({self._sub(args[0])} {name} {self._sub(args[1])})"
        if name in SCALAR:
            return f"{SCALAR[name]}({', '.join(self._sub(a) for a in args)})"
        if name == "RELATED":
            return self._sub(args[0])
        raise NotTranslatable(f"function {name}()")


# ========================================================= query assembly
@dataclass
class SqlResult:
    sql: str
    fully_translated: bool
    notes: List[str]


AUTO_DATE = ("LocalDateTable_", "DateTableTemplate_")


def is_auto_date(table: str) -> bool:
    return table.startswith(AUTO_DATE)


class SqlGenerator:
    """Builds Snowflake SQL. Every table is written as DATABASE.SCHEMA.TABLE:
    parts the model doesn't reveal come from ``default_database`` / ``default_schema``
    (set per environment in the app), and ``table_overrides`` win over everything."""

    def __init__(self, model: SemanticModel, table_overrides: Optional[Dict[str, str]] = None,
                 default_database: str = "", default_schema: str = "",
                 uppercase_identifiers: bool = True):
        self.model = model
        self.overrides = {k: v for k, v in (table_overrides or {}).items() if v}
        self.default_db = (default_database or "").strip()
        self.default_schema = (default_schema or "").strip()
        self.upper = uppercase_identifiers

    # ------------------------------------------------------------ helpers
    def ident(self, name: str) -> str:
        """Snowflake identifier: simple names unquoted and upper-case (how Snowflake stores
        unquoted names); anything else quoted exactly."""
        name = name.strip()
        if re.match(r"^[A-Za-z_][A-Za-z0-9_$]*$", name):
            return name.upper() if self.upper else (name if name.isupper() else f'"{name}"')
        return '"' + name.replace('"', '""') + '"'

    def qualify(self, database: str, schema: str, obj: str) -> Tuple[str, List[str]]:
        notes = []
        db = database or self.default_db
        sch = schema or self.default_schema
        if not db:
            notes.append(f"No database known for {obj} - set the default database for this "
                         "environment (Model insights → Snowflake mapping).")
        if not sch:
            notes.append(f"No schema known for {obj} - set the default schema for this "
                         "environment (Model insights → Snowflake mapping).")
        parts = [db or "<DATABASE>", sch or "<SCHEMA>", obj]
        return ".".join(p if p.startswith("<") else self.ident(p) for p in parts), notes

    def resolved_object(self, table: str) -> Tuple[str, List[str]]:
        """The fully qualified Snowflake object (or subquery) behind a model table."""
        if table in self.overrides:
            ov = self.overrides[table].strip()
            if ov.startswith("("):
                return ov, []
            parts = [p.strip() for p in ov.split(".")]
            if len(parts) == 3:
                return ".".join(self.ident(p) for p in parts), []
            return self.qualify(parts[0] if len(parts) == 2 else "",
                                parts[-2] if len(parts) == 2 else "", parts[-1])
        t = self.model.table(table)
        if not t:
            return self.qualify("", "", table.upper().replace(" ", "_"))[0], \
                [f"Table {table} not in model."]
        notes = list(t.source.notes)
        if t.is_calculated:
            notes.append(f"{table} is a DAX calculated table - map it to a Snowflake object "
                         "in 'Snowflake mapping'.")
        if t.source.native_query:
            notes.append(f"{table} is loaded with a native SQL query - it is used as a subquery; "
                         "check it runs in Snowflake.")
            return f"(\n{t.source.native_query.strip()}\n)", notes
        obj = t.source.object_name
        if not obj:
            notes.append(f"No source object found for {table} in Power Query; using the table "
                         "name. Set it in 'Snowflake mapping'.")
            obj = re.sub(r"\W+", "_", table).upper()
        fq, n2 = self.qualify(t.source.database, t.source.schema, obj)
        return fq, notes + n2

    def _from_sql(self, table: str) -> Tuple[str, List[str]]:
        return self.resolved_object(table)

    def _join_path(self, base: str, needed: Set[str]) -> Tuple[List[Tuple[str, str, str, str, str]], List[str]]:
        """BFS from base across active relationships. Returns join steps."""
        adj: Dict[str, List[Tuple[str, str, str, str]]] = {}
        for r in self.model.relationships:
            if not r.is_active:
                continue
            adj.setdefault(r.from_table, []).append((r.to_table, r.from_column, r.to_column, "down"))
            adj.setdefault(r.to_table, []).append((r.from_table, r.to_column, r.from_column, "up"))
        prev: Dict[str, Tuple[str, str, str, str]] = {base: ("", "", "", "")}
        q = deque([base])
        while q:
            cur = q.popleft()
            for nxt, c1, c2, d in adj.get(cur, []):
                if nxt not in prev:
                    prev[nxt] = (cur, c1, c2, d)
                    q.append(nxt)
        steps, notes, done = [], [], {base}
        for tgt in needed - {base}:
            if tgt not in prev:
                notes.append(f"No active relationship path from {base} to {tgt} - "
                             "it is cross-joined; add the join manually.")
                steps.append((base, "", tgt, "", "cross"))
                continue
            chain, n = [], tgt
            while n != base:
                p, c1, c2, d = prev[n]
                chain.append((p, c1, n, c2, d))
                n = p
            for step in reversed(chain):
                if step[2] not in done:
                    done.add(step[2])
                    if step[4] == "up":
                        notes.append(f"Join {step[0]} -> {step[2]} goes from the one-side to the "
                                     "many-side; aggregates may be inflated.")
                    steps.append(step)
        return steps, notes

    def generate(self, visual: Visual, filters: Sequence[FilterSpec],
                 env_label: str = "") -> SqlResult:
        notes: List[str] = []
        ok = True
        groups = visual.group_fields
        values = visual.value_fields

        # tables touched by measures decide the base (fact) table
        mtables: Dict[str, int] = {}
        for f in values:
            if f.kind == "aggregation":
                mtables[f.table] = mtables.get(f.table, 0) + 1
            else:
                m = self.model.find_measure(f.name, f.table)
                if m:
                    _, cols = self.model.measure_dependencies(m)
                    for t, _c in cols:
                        mtables[t] = mtables.get(t, 0) + 1
        roles = self.model.table_roles()
        for t in [t for t in mtables if is_auto_date(t)]:
            src = self.model.date_source(t)
            mtables.pop(t)
            if src:
                mtables[src[0]] = mtables.get(src[0], 0) + 1
        mtables.pop("", None)
        if mtables:
            base = sorted(mtables, key=lambda t: (roles.get(t) != "fact", -mtables[t]))[0]
        elif groups:
            base = next((self.model.date_source(g.table)[0] if is_auto_date(g.table) and
                         self.model.date_source(g.table) else g.table for g in groups), "")
        else:
            base = visual.fields[0].table if visual.fields else ""

        self.alias = {base: "t0"}
        fsql: List[Tuple[str, bool]] = []  # (condition, is_having)
        for fs in filters:
            try:
                for cond, having, _tables in _filter_to_sql(fs, self):
                    fsql.append((cond, having))
            except NotTranslatable as exc:
                notes.append(f"{fs.level} filter '{fs.description or fs.name}' not translated: {exc}")
                ok = False

        select, group_by = [], []
        for f in groups:
            try:
                expr = self.col_sql(f.table, f.name, f.hierarchy if f.variation else "")
            except NotTranslatable as exc:
                ok = False
                notes.append(str(exc))
                expr = f"NULL /* TODO {f.table}[{f.name}] */"
            select.append(f"{expr} AS {_q(f.label)}")
            group_by.append(expr)
        for f in values:
            try:
                expr = self.measure_sql(f)
            except NotTranslatable as exc:
                ok = False
                dax = self._field_dax_text(f)
                notes.append(f"Measure [{f.label}] not translated ({exc}).")
                expr = f"NULL /* TODO translate DAX manually:\n{_indent(dax, 6)}\n   */"
            select.append(f"{expr} AS {_q(f.label)}")

        # every table referenced above must be joined to the base table
        needed = {t for t in self.alias if t and not is_auto_date(t)} | set(mtables)
        steps, jnotes = self._join_path(base, needed) if base else ([], [])
        notes += jnotes
        for st in steps:
            self.alias.setdefault(st[2], f"t{len(self.alias)}")

        src, n2 = self._from_sql(base)
        notes += n2
        lines = [f"-- Visual: {visual.label()}  [{visual.visual_type}]  page={visual.page_name}"]
        if env_label:
            lines.append(f"-- Environment: {env_label}")
        lines.append("SELECT\n    " + ",\n    ".join(select or ["COUNT(*) AS ROW_COUNT"]))
        lines.append(f"FROM {src} t0")
        for p, c1, n, c2, d in steps:
            nsrc, n3 = self._from_sql(n)
            notes += n3
            if d == "cross":
                lines.append(f"CROSS JOIN {nsrc} {self.alias[n]}")
                continue
            pa, na = self.alias[p], self.alias[n]
            lines.append(f"LEFT JOIN {nsrc} {na}\n       ON {pa}.{self._src_col(p, c1)} = "
                         f"{na}.{self._src_col(n, c2)}")
        where = [c for c, h in fsql if not h]
        having = [c for c, h in fsql if h]
        if where:
            lines.append("WHERE " + "\n  AND ".join(where))
        if group_by:
            lines.append("GROUP BY " + ", ".join(group_by))
        if having:
            lines.append("HAVING " + "\n   AND ".join(having))
        elif values and group_by:
            # Power BI hides rows where every measure is blank
            lines.append("HAVING " + " OR ".join(f"{_strip_alias(s)} IS NOT NULL"
                                                  for s in select[len(groups):]))
        if group_by:
            lines.append("ORDER BY " + ", ".join(str(i + 1) for i in range(len(group_by))))
        sql = "\n".join(lines) + ";"
        if notes:
            sql = "\n".join(f"-- NOTE: {n}" for n in dict.fromkeys(notes)) + "\n" + sql
        return SqlResult(sql, ok, list(dict.fromkeys(notes)))

    # --------------------------------------------------------- resolvers
    def _src_col(self, table: str, column: str) -> str:
        return self.ident(self.model.source_column_name(table, column) or column)

    def col_sql(self, table: str, column: str, variation_level: str = "") -> str:
        if is_auto_date(table):
            # Auto date/time table: compute the level from the real date column instead
            src = self.model.date_source(table)
            if not src:
                raise NotTranslatable(f"auto date table {table} has no source date column")
            lvl = {"date": "", "year": "year", "monthno": "monthno", "month": "month",
                   "quarterno": "quarterno", "quarter": "quarter", "day": "day"
                   }.get(column.lower(), "")
            return self.col_sql(src[0], src[1], lvl)
        alias = self.alias.get(table)
        if alias is None:
            alias = self.alias.setdefault(table, f"t{len(self.alias)}")
        c = self.model.find_column(table, column)
        if c is not None and c.is_calculated:
            try:
                expr = DaxToSql(self.col_sql, self._measure_expr, table).translate(c.expression)
                base = f"({expr})"
            except NotTranslatable as exc:
                raise NotTranslatable(f"calculated column {table}[{column}] ({exc})")
        else:
            base = f"{alias}.{self._src_col(table, column)}"
        lvl = (variation_level or "").lower()
        if lvl == "year":
            return f"YEAR({base})"
        if lvl == "quarter":
            return f"'Qtr ' || QUARTER({base})"
        if lvl == "quarterno":
            return f"QUARTER({base})"
        if lvl == "month":
            return f"TO_CHAR({base}, 'MMMM')"
        if lvl == "monthno":
            return f"MONTH({base})"
        if lvl == "day":
            return f"DAY({base})"
        return base

    def _measure_expr(self, name: str) -> Optional[str]:
        m = self.model.find_measure(name)
        return m.expression if m else None

    def _field_dax_text(self, f: Field) -> str:
        if f.kind == "measure":
            m = self.model.find_measure(f.name, f.table)
            return f"[{f.name}] = {m.expression if m else '?'}"
        return f"{f.agg}('{f.table}'[{f.name}])"

    def measure_sql(self, f: Field) -> str:
        tr = DaxToSql(self.col_sql, self._measure_expr, f.table)
        if f.kind == "aggregation":
            col = self.col_sql(f.table, f.name, f.hierarchy if f.variation else "")
            if f.agg == "DISTINCTCOUNT":
                return f"COUNT(DISTINCT {col})"
            return {"SUM": "SUM", "AVERAGE": "AVG", "MIN": "MIN", "MAX": "MAX", "COUNT": "COUNT",
                    "MEDIAN": "MEDIAN", "STDEV.P": "STDDEV_POP", "VAR.P": "VAR_POP"
                    }.get(f.agg, f.agg) + f"({col})"
        m = self.model.find_measure(f.name, f.table)
        if not m:
            raise NotTranslatable(f"measure {f.name} not found in model")
        return tr.translate(m.expression)


# ========================================================== filters -> SQL
_SQL_CMP = {0: "=", 1: ">", 2: ">=", 3: "<", 4: "<="}


def _sql_lit(v: str) -> str:
    from .dax_builder import literal_python
    p = literal_python(v)
    if p is None:
        return "NULL"
    if isinstance(p, bool):
        return "TRUE" if p else "FALSE"
    if isinstance(p, (int, float)):
        return str(p)
    if str(v).startswith("datetime'"):
        return f"'{p[:19].replace('T', ' ')}'::TIMESTAMP"
    return "'" + str(p).replace("'", "''") + "'"


def _filter_to_sql(fs: FilterSpec, gen: SqlGenerator):
    aliases = {f["Name"]: f.get("Entity", "") for f in fs.filter_json.get("From", [])
               if "Entity" in f}

    def ex(e) -> Tuple[str, bool, Set[str]]:
        if "Literal" in e:
            return _sql_lit(e["Literal"]["Value"]), False, set()
        f = parse_field(e, aliases)
        if f.kind == "unsupported":
            raise NotTranslatable("unsupported expression")
        if f.is_measure_like:
            return gen.measure_sql(f), True, {f.table}
        return gen.col_sql(f.table, f.name, f.hierarchy if f.variation else ""), False, {f.table}

    def cond(c) -> Tuple[str, bool, Set[str]]:
        for op, sop in (("And", "AND"), ("Or", "OR")):
            if op in c:
                l, r = cond(c[op]["Left"]), cond(c[op]["Right"])
                return f"({l[0]} {sop} {r[0]})", l[1] or r[1], l[2] | r[2]
        if "Not" in c:
            x = cond(c["Not"]["Expression"])
            return f"NOT ({x[0]})", x[1], x[2]
        if "In" in c:
            i = c["In"]
            if "Table" in i:
                raise NotTranslatable("Top N filter (rank in SQL with QUALIFY)")
            es = [ex(e) for e in i["Expressions"]]
            if len(es) == 1:
                vals = [_sql_lit(v[0]["Literal"]["Value"]) for v in i["Values"]]
                nn = [v for v in vals if v != "NULL"]
                s = f"{es[0][0]} IN ({', '.join(nn)})" if nn else "FALSE"
                if len(nn) != len(vals):
                    s = f"({s} OR {es[0][0]} IS NULL)"
                return s, es[0][1], es[0][2]
            tup = ", ".join("(" + ", ".join(_sql_lit(x["Literal"]["Value"]) for x in v) + ")"
                            for v in i["Values"])
            return (f"({', '.join(e[0] for e in es)}) IN ({tup})", any(e[1] for e in es),
                    set().union(*(e[2] for e in es)))
        if "Comparison" in c:
            cp = c["Comparison"]
            if any(k in cp["Right"] for k in ("DateSpan", "DateAdd", "Now")):
                raise NotTranslatable("relative date filter")
            l, r = ex(cp["Left"]), ex(cp["Right"])
            op = _SQL_CMP.get(cp.get("ComparisonKind", 0), "=")
            if r[0] == "NULL":
                return f"{l[0]} IS NULL", l[1], l[2]
            return f"{l[0]} {op} {r[0]}", l[1] or r[1], l[2] | r[2]
        if "Between" in c:
            b = c["Between"]
            e = ex(b["Expression"])
            return (f"{e[0]} BETWEEN {ex(b['LowerBound'])[0]} AND {ex(b['UpperBound'])[0]}",
                    e[1], e[2])
        for k, tpl in (("Contains", "CONTAINS({l}, {r})"), ("StartsWith", "STARTSWITH({l}, {r})"),
                       ("EndsWith", "ENDSWITH({l}, {r})")):
            if k in c:
                l, r = ex(c[k]["Left"]), ex(c[k]["Right"])
                return tpl.format(l=l[0], r=r[0]), l[1], l[2]
        raise NotTranslatable(f"condition {list(c)[0]}")

    out = []
    for w in fs.filter_json.get("Where", []):
        s, having, tables = cond(w["Condition"])
        out.append((s, having, tables))
    return out


def _q(label: str) -> str:
    return '"' + label.replace('"', '""') + '"'


def _indent(s: str, n: int) -> str:
    return "\n".join(" " * n + l.replace("*/", "* /") for l in s.splitlines())


def _strip_alias(select_item: str) -> str:
    return select_item.rsplit(" AS ", 1)[0]
