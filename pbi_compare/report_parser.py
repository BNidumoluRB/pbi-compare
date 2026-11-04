"""Parse a Power BI report definition into pages, visuals, fields, filters and bookmarks.

Handles both formats returned by the Fabric getDefinition API:
  * PBIR      -> definition/report.json, definition/pages/<p>/page.json,
                 definition/pages/<p>/visuals/<v>/visual.json,
                 definition/bookmarks/<b>.bookmark.json
  * Legacy    -> report.json (sections / visualContainers, JSON-in-strings)
and the Layout file inside an exported .pbix (same shape as legacy).

Bookmarks matter because "tabs inside one page" are usually built with buttons
that apply bookmarks: each bookmark hides/shows visuals and may carry its own
filter and slicer state. Each bookmark can be compared as its own *view*.
"""
from __future__ import annotations

import io
import json
import re
import zipfile
from dataclasses import dataclass, field
from typing import Dict, Iterable, List, Optional, Sequence, Set

NON_DATA_VISUALS = {"textbox", "image", "shape", "basicShape", "actionButton", "pageNavigator",
                    "bookmarkNavigator", "group", "iframe"}
AGG_FUNCS = {0: "SUM", 1: "AVERAGE", 2: "DISTINCTCOUNT", 3: "MIN", 4: "MAX", 5: "COUNT",
             6: "MEDIAN", 7: "STDEV.P", 8: "VAR.P"}
TABULAR_VISUALS = {"tableEx", "pivotTable", "table", "matrix"}
OVERRIDABLE_LEVELS = {"report", "page", "slicer"}


@dataclass
class Field:
    role: str
    kind: str                 # column | measure | aggregation | hierarchy | unsupported
    table: str
    name: str                 # column / measure / level name (variation: the date column)
    agg: str = ""             # SUM, COUNT ... for aggregation
    query_ref: str = ""
    display_name: str = ""
    hierarchy: str = ""       # variation: level (Year); user hierarchy: hierarchy name
    variation: bool = False   # auto date/time variation (Sales[Date].[Year])
    hierarchy_name: str = ""  # variation: "Date Hierarchy"
    raw: dict = field(default_factory=dict, repr=False)

    @property
    def is_measure_like(self) -> bool:
        return self.kind in ("measure", "aggregation")

    @property
    def level(self) -> str:
        """Level shown to users for hierarchies (Year, Month...)."""
        return self.hierarchy if self.variation else (self.name if self.kind == "hierarchy" else "")

    @property
    def label(self) -> str:
        if self.display_name:
            return self.display_name
        if self.kind == "aggregation":
            return f"{self.agg.title()} of {self.name}"
        if self.variation:
            return f"{self.name} {self.hierarchy}"
        return self.name

    @property
    def key(self) -> str:
        """Stable identity of the filtered column (used for user overrides)."""
        if self.variation:
            return f"{self.table}|{self.name}|{self.hierarchy}".lower()
        return f"{self.table}|{self.name}".lower()

    @property
    def js_key(self) -> str:
        """How powerbi-client names the same target: table|column or table|hierarchyLevel."""
        return f"{self.table}|{self.hierarchy if self.variation else self.name}".lower()

    @property
    def friendly(self) -> str:
        if self.variation:
            return f"{self.table}[{self.name}] › {self.hierarchy}"
        if self.kind == "hierarchy":
            return f"{self.table} › {self.hierarchy} › {self.name}"
        return f"{self.table}[{self.name}]"

    def signature(self) -> str:
        return f"{self.kind}:{self.table}.{self.name}:{self.agg}".lower()


@dataclass
class FilterSpec:
    level: str                # report | page | visual | slicer | choice | user
    name: str
    filter_type: str
    filter_json: dict         # {"Version", "From", "Where"}
    source_visual: str = ""   # slicer that produced it
    description: str = ""
    fields: List[Field] = field(default_factory=list)

    @property
    def keys(self) -> Set[str]:
        return {f.key for f in self.fields}


@dataclass
class Visual:
    page_name: str
    name: str
    visual_type: str
    title: str = ""
    x: float = 0
    y: float = 0
    width: float = 0
    height: float = 0
    fields: List[Field] = field(default_factory=list)
    filters: List[FilterSpec] = field(default_factory=list)
    warnings: List[str] = field(default_factory=list)
    hidden: bool = False
    parent_group: str = ""
    action: str = ""          # button action: Bookmark | PageNavigation | Drillthrough | Back | ...
    action_target: str = ""   # bookmark name or page name
    raw: dict = field(default_factory=dict, repr=False)

    @property
    def is_data_visual(self) -> bool:
        return self.visual_type not in NON_DATA_VISUALS and bool(self.fields)

    @property
    def is_tabular(self) -> bool:
        return self.visual_type in TABULAR_VISUALS

    @property
    def is_slicer(self) -> bool:
        # slicer, advancedSlicerVisual (button slicer), listSlicer, textSlicer, ...
        return "slicer" in self.visual_type.lower()

    @property
    def is_group(self) -> bool:
        return self.visual_type == "group"

    @property
    def group_fields(self) -> List[Field]:
        return [f for f in self.fields if not f.is_measure_like and f.kind != "unsupported"]

    @property
    def value_fields(self) -> List[Field]:
        return [f for f in self.fields if f.is_measure_like]

    def label(self) -> str:
        return self.title or f"{self.visual_type} ({self.name[:8]})"

    def signature(self) -> str:
        return "|".join(sorted(f.signature() for f in self.fields))


@dataclass
class Page:
    name: str
    display_name: str
    ordinal: int = 0
    width: float = 1280
    height: float = 720
    filters: List[FilterSpec] = field(default_factory=list)
    visuals: List[Visual] = field(default_factory=list)
    interactions: Dict[str, Dict[str, str]] = field(default_factory=dict)  # src -> tgt -> type
    hidden: bool = False

    def visual(self, name: str) -> Optional[Visual]:
        return next((v for v in self.visuals if v.name == name), None)


@dataclass
class Bookmark:
    name: str
    display_name: str
    page: str = ""                                   # activeSection
    group: str = ""
    report_filters: Optional[List[FilterSpec]] = None
    page_filters: Dict[str, List[FilterSpec]] = field(default_factory=dict)
    slicer_filters: Dict[str, List[FilterSpec]] = field(default_factory=dict)
    hidden: Set[str] = field(default_factory=set)    # visuals / groups hidden by the bookmark
    shown: Set[str] = field(default_factory=set)     # visuals / groups the bookmark shows
    suppress_data: bool = False                      # "Data" unticked: display state only
    target_visuals: List[str] = field(default_factory=list)

    @property
    def label(self) -> str:
        return f"{self.group} › {self.display_name}" if self.group else self.display_name


@dataclass
class ReportDef:
    format: str
    pages: List[Page] = field(default_factory=list)
    filters: List[FilterSpec] = field(default_factory=list)
    bookmarks: List[Bookmark] = field(default_factory=list)

    def page(self, name: str) -> Optional[Page]:
        return next((p for p in self.pages if p.name == name or p.display_name == name), None)

    def page_bookmarks(self, page: Page) -> List[Bookmark]:
        return [b for b in self.bookmarks if b.page == page.name]

    def buttons(self, page: Page) -> List[Visual]:
        return [v for v in page.visuals if v.action]

    def bookmark_buttons(self, bookmark: Bookmark) -> List[str]:
        """Labels of buttons (on any page) that apply this bookmark."""
        return [v.label() for p in self.pages for v in p.visuals
                if v.action == "Bookmark" and v.action_target == bookmark.name]

    def bookmark(self, name: str) -> Optional[Bookmark]:
        return next((b for b in self.bookmarks if b.name == name), None)

    def find_bookmark(self, other: Bookmark) -> Optional[Bookmark]:
        """Bookmark in this report that corresponds to one from the other report."""
        return next((b for b in self.bookmarks if b.name == other.name), None) or next(
            (b for b in self.bookmarks
             if b.display_name.strip().lower() == other.display_name.strip().lower()), None)

    # ------------------------------------------------------- visibility
    def is_hidden(self, page: Page, visual: Visual, bookmark: Optional[Bookmark]) -> bool:
        chain = [visual.name]
        g = visual.parent_group
        while g:
            chain.append(g)
            gv = page.visual(g)
            g = gv.parent_group if gv else ""
        for n in chain:
            if bookmark is not None:
                if n in bookmark.hidden:
                    return True
                if n in bookmark.shown:
                    continue
            v = page.visual(n)
            if v is not None and v.hidden:
                return True
        return False

    # ----------------------------------------------------------- filters
    def filter_fields(self, page: Page) -> List["FilterField"]:
        """Every field a user can filter on for this page: report & page filters + slicers."""
        out: Dict[str, FilterField] = {}

        def add(f: Field, source: str, spec: Optional[FilterSpec], group: str = ""):
            if f.kind in ("measure", "aggregation", "unsupported"):
                return
            ff = out.setdefault(f.key, FilterField(f, []))
            ff.sources.append(source)
            if spec is not None:
                ff.saved.append(spec)
            if group:
                ff.group = group

        for spec in self.filters:
            for f in spec.fields:
                add(f, "Report filter", spec)
        for spec in page.filters:
            for f in spec.fields:
                add(f, "Page filter", spec)
        for v in page.visuals:
            if v.is_slicer:
                slicer_specs = [s for s in v.filters if s.level == "slicer"]
                grp = v.name if len(v.group_fields) > 1 else ""
                for f in v.group_fields:
                    spec = next((s for s in slicer_specs if f.key in s.keys), None)
                    add(f, f"Slicer: {v.label()}", spec, grp)
        return list(out.values())

    def effective_filters(self, page: Page, visual: Visual, bookmark: Optional[Bookmark] = None,
                          choices: Optional[Dict[str, Optional[List[FilterSpec]]]] = None
                          ) -> List[FilterSpec]:
        """Report + page + slicer selections (respecting edit interactions) + visual filters.

        ``bookmark`` replaces the saved report/page/slicer state with the bookmark's
        captured state (unless the bookmark only stores display state).
        ``choices`` maps a field key to the user's selection: ``None`` = All (drop
        every saved filter on that field), a list of specs = use these instead.
        Choices apply to every page being compared - the caller only passes the
        pages in scope (the selected pages, or all pages) - including pages that have
        no filter or slicer on that field.
        """
        use_bm = bookmark is not None and not bookmark.suppress_data
        rep = (bookmark.report_filters if use_bm and bookmark.report_filters is not None
               else self.filters)
        pg = (bookmark.page_filters.get(page.name) if use_bm and page.name in bookmark.page_filters
              else page.filters)
        out = list(rep) + list(pg)
        if not visual.is_slicer:
            for s in page.visuals:
                if s.is_slicer and s.name != visual.name:
                    mode = page.interactions.get(s.name, {}).get(visual.name, "")
                    if mode.lower() in ("nofilter", "none"):
                        continue
                    if use_bm and s.name in bookmark.slicer_filters:
                        out.extend(bookmark.slicer_filters[s.name])
                    else:
                        out.extend(f for f in s.filters if f.level == "slicer")
        if choices:
            active = dict(choices)
            if active:
                out = [f for f in out if not (f.level in OVERRIDABLE_LEVELS and f.keys & set(active))]
                for k, specs in active.items():
                    if specs and not (visual.is_slicer and k in {f.key for f in visual.fields}):
                        out.extend(specs)
        out.extend(f for f in visual.filters if f.level == "visual")
        return out


@dataclass
class FilterField:
    field: Field
    sources: List[str]
    saved: List[FilterSpec] = field(default_factory=list)
    group: str = ""          # slicer with several fields (e.g. Year + Month hierarchy)

    @property
    def key(self) -> str:
        return self.field.key


# =================================================================== fields
def _entity(expr: dict, aliases: Dict[str, str]) -> str:
    sr = expr.get("SourceRef", {})
    if "Entity" in sr:
        return sr["Entity"]
    return aliases.get(sr.get("Source", ""), sr.get("Source", ""))


def parse_field(fexpr: dict, aliases: Dict[str, str], role: str = "", query_ref: str = "",
                display_name: str = "") -> Field:
    if "Column" in fexpr:
        c = fexpr["Column"]
        return Field(role, "column", _entity(c["Expression"], aliases), c["Property"],
                     query_ref=query_ref, display_name=display_name, raw=fexpr)
    if "Measure" in fexpr:
        m = fexpr["Measure"]
        return Field(role, "measure", _entity(m["Expression"], aliases), m["Property"],
                     query_ref=query_ref, display_name=display_name, raw=fexpr)
    if "Aggregation" in fexpr:
        a = fexpr["Aggregation"]
        inner = parse_field(a["Expression"], aliases)
        return Field(role, "aggregation", inner.table, inner.name,
                     AGG_FUNCS.get(a.get("Function", 0), "SUM"), query_ref, display_name,
                     inner.hierarchy, inner.variation, inner.hierarchy_name, raw=fexpr)
    if "HierarchyLevel" in fexpr:
        h = fexpr["HierarchyLevel"]
        hexpr = h["Expression"]["Hierarchy"]
        inner = hexpr["Expression"]
        if "PropertyVariationSource" in inner:
            pvs = inner["PropertyVariationSource"]
            return Field(role, "hierarchy", _entity(pvs["Expression"], aliases), pvs["Property"],
                         query_ref=query_ref, display_name=display_name or h["Level"],
                         hierarchy=h["Level"], variation=True,
                         hierarchy_name=hexpr.get("Hierarchy", "Date Hierarchy"), raw=fexpr)
        return Field(role, "hierarchy", _entity(inner, aliases), h["Level"], query_ref=query_ref,
                     display_name=display_name, hierarchy=hexpr.get("Hierarchy", ""), raw=fexpr)
    return Field(role, "unsupported", "", query_ref or "expression", query_ref=query_ref,
                 display_name=display_name, raw=fexpr)


def _condition_fields(cond: dict, aliases: Dict[str, str]) -> List[Field]:
    out: List[Field] = []

    def walk(node):
        if isinstance(node, dict):
            if any(k in node for k in ("Column", "Measure", "Aggregation", "HierarchyLevel")):
                f = parse_field(node, aliases)
                if f.kind != "unsupported":
                    out.append(f)
                return
            for k, v in node.items():
                if k in ("Values", "Literal", "Table"):
                    continue
                walk(v)
        elif isinstance(node, list):
            for x in node:
                walk(x)
    walk(cond)
    seen, uniq = set(), []
    for f in out:
        if f.key not in seen:
            seen.add(f.key)
            uniq.append(f)
    return uniq


def spec_from_json(level: str, fj: dict, name: str = "", ftype: str = "", desc: str = "",
                   source_visual: str = "") -> FilterSpec:
    aliases = {f["Name"]: f.get("Entity", "") for f in fj.get("From", []) if "Entity" in f}
    fields = _condition_fields([w.get("Condition", {}) for w in fj.get("Where", [])], aliases)
    return FilterSpec(level, name, ftype, fj, source_visual, desc, fields)


def _literal(objs: dict, prop: str, key: str = "visualLink") -> str:
    try:
        v = objs[key][0]["properties"][prop]["expr"]["Literal"]["Value"]
        return v.strip("'").replace("''", "'")
    except (KeyError, IndexError, TypeError):
        return ""


def _button_action(vis: "Visual", objs: dict) -> None:
    if not objs or "visualLink" not in objs:
        return
    if _literal(objs, "show") == "false":
        return
    vis.action = _literal(objs, "type")
    vis.action_target = (_literal(objs, "bookmark") or _literal(objs, "navigationSection")
                         or _literal(objs, "drillthroughSection"))


def _literal_title(objs: dict) -> str:
    try:
        v = objs["title"][0]["properties"]["text"]["expr"]["Literal"]["Value"]
        return v.strip("'").replace("''", "'")
    except (KeyError, IndexError, TypeError):
        return ""


def _parse_filters(raw, level: str) -> List[FilterSpec]:
    if isinstance(raw, str):
        try:
            raw = json.loads(raw) if raw.strip() else []
        except ValueError:
            raw = []
    if isinstance(raw, dict):
        raw = raw.get("filters", raw.get("byExpr", []))
    out = []
    for f in raw or []:
        fj = f.get("filter")
        if not fj or not fj.get("Where"):
            continue  # filter card present but nothing selected
        out.append(spec_from_json(level, fj, f.get("name", ""), f.get("type", ""),
                                  f.get("displayName", "")))
    return out


def _slicer_filter(objects: dict, vname: str) -> List[FilterSpec]:
    for key in ("general", "data"):
        try:
            fj = objects[key][0]["properties"]["filter"]["filter"]
        except (KeyError, IndexError, TypeError):
            continue
        if fj.get("Where"):
            return [spec_from_json("slicer", fj, f"slicer:{vname}", "Categorical",
                                   source_visual=vname)]
    return []


# ================================================================ bookmarks
def _parse_bookmark(bj: dict, group: str = "") -> Bookmark:
    es = bj.get("explorationState", {}) or {}
    opts = bj.get("options", {}) or {}
    bm = Bookmark(bj.get("name", ""), bj.get("displayName", bj.get("name", "")),
                  es.get("activeSection", ""), group,
                  suppress_data=bool(opts.get("suppressData")),
                  target_visuals=list(opts.get("targetVisualNames", []) or []))
    if "filters" in es:
        bm.report_filters = _parse_filters(es["filters"], "report")
    for sec_name, sec in (es.get("sections", {}) or {}).items():
        if "filters" in sec:
            bm.page_filters[sec_name] = _parse_filters(sec["filters"], "page")
        for vname, vc in (sec.get("visualContainers", {}) or {}).items():
            sv = vc.get("singleVisual", {}) or {}
            mode = (sv.get("display", {}) or {}).get("mode")
            if mode == "hidden":
                bm.hidden.add(vname)
            elif sv.get("display") is not None or mode == "visible":
                bm.shown.add(vname)
            merge = ((sv.get("objects", {}) or {}).get("merge", {}) or {})
            sl = _slicer_filter(merge, vname)
            if sl or "general" in merge:
                bm.slicer_filters[vname] = sl
        for gname, g in (sec.get("visualContainerGroups", {}) or {}).items():
            if g.get("isHidden"):
                bm.hidden.add(gname)
            else:
                bm.shown.add(gname)
    return bm


def _walk_bookmarks(items: Iterable[dict], group: str = "") -> List[Bookmark]:
    out = []
    for b in items or []:
        if b.get("children"):
            out += _walk_bookmarks(b["children"], b.get("displayName", group))
        elif "explorationState" in b:
            out.append(_parse_bookmark(b, group))
    return out


# ===================================================================== PBIR
def parse_pbir(parts: Dict[str, str]) -> ReportDef:
    rep = ReportDef("pbir")
    rj = json.loads(parts.get("definition/report.json", "{}") or "{}")
    rep.filters = _parse_filters(rj.get("filterConfig", {}), "report")
    order = json.loads(parts.get("definition/pages/pages.json", "{}") or "{}").get("pageOrder", [])
    page_files = {k.split("/")[2]: v for k, v in parts.items()
                  if re.match(r"definition/pages/[^/]+/page\.json$", k)}
    for pid, text in page_files.items():
        pj = json.loads(text)
        page = Page(pj.get("name", pid), pj.get("displayName", pid),
                    order.index(pid) if pid in order else 999,
                    pj.get("width", 1280), pj.get("height", 720),
                    _parse_filters(pj.get("filterConfig", {}), "page"),
                    hidden=pj.get("visibility") == "HiddenInViewMode")
        for vi in pj.get("visualInteractions", []) or []:
            page.interactions.setdefault(vi.get("source"), {})[vi.get("target")] = vi.get("type", "")
        prefix = f"definition/pages/{pid}/visuals/"
        for path, vtext in parts.items():
            if path.startswith(prefix) and path.endswith("/visual.json"):
                page.visuals.append(_pbir_visual(page.name, json.loads(vtext)))
        rep.pages.append(page)
    rep.pages.sort(key=lambda p: p.ordinal)

    meta = json.loads(parts.get("definition/bookmarks/bookmarks.json", "{}") or "{}")
    groups = {}
    for item in meta.get("items", []) or []:
        for child in item.get("children", []) or []:
            groups[child] = item.get("displayName", "")
    for path, text in parts.items():
        if path.startswith("definition/bookmarks/") and path.endswith(".bookmark.json"):
            bj = json.loads(text)
            rep.bookmarks.append(_parse_bookmark(bj, groups.get(bj.get("name"), "")))
    rank = {c: i for i, c in enumerate(
        [c for it in meta.get("items", []) or [] for c in (it.get("children") or [it.get("name")])])}
    rep.bookmarks.sort(key=lambda b: rank.get(b.name, 999))
    return rep


def _pbir_visual(page_name: str, vj: dict) -> Visual:
    pos = vj.get("position", {})
    v = vj.get("visual", {})
    vis = Visual(page_name, vj.get("name", ""), v.get("visualType", "group" if "visualGroup" in vj
                                                                   else "unknown"),
                 _literal_title(v.get("visualContainerObjects", {})) or
                 (vj.get("visualGroup", {}) or {}).get("displayName", ""),
                 pos.get("x", 0), pos.get("y", 0), pos.get("width", 0), pos.get("height", 0),
                 hidden=bool(vj.get("isHidden")), parent_group=vj.get("parentGroupName", ""),
                 raw=vj)
    qs = v.get("query", {}).get("queryState", {})
    for role, proj in qs.items():
        for p in proj.get("projections", []):
            f = parse_field(p.get("field", {}), {}, role, p.get("queryRef", ""),
                            p.get("displayName", ""))
            if f.kind == "unsupported":
                vis.warnings.append(f"Field {p.get('queryRef')} uses an unsupported expression "
                                    "(visual calculation?) - excluded from the DAX query.")
            vis.fields.append(f)
    vis.filters = _parse_filters(vj.get("filterConfig", {}), "visual")
    if vis.is_slicer:
        vis.filters += _slicer_filter(v.get("objects", {}), vis.name)
    _button_action(vis, v.get("visualContainerObjects", {}) or v.get("objects", {}))
    if not vis.title and vis.visual_type == "actionButton":
        vis.title = _literal(v.get("objects", {}), "text", "text")
    return vis


# =================================================================== legacy
def parse_legacy(layout: dict) -> ReportDef:
    rep = ReportDef("legacy")
    rep.filters = _parse_filters(layout.get("filters", "[]"), "report")
    for s in layout.get("sections", []):
        page = Page(s.get("name", ""), s.get("displayName", ""), s.get("ordinal", 0),
                    s.get("width", 1280), s.get("height", 720),
                    _parse_filters(s.get("filters", "[]"), "page"))
        try:
            scfg = json.loads(s.get("config", "{}") or "{}")
            page.hidden = scfg.get("visibility") == 1
            for rel in scfg.get("relationships", []) or []:
                page.interactions.setdefault(rel.get("source"), {})[rel.get("target")] = \
                    {1: "Filter", 2: "Highlight", 3: "NoFilter"}.get(rel.get("type"), "")
        except ValueError:
            pass
        for vc in s.get("visualContainers", []):
            page.visuals.append(_legacy_visual(page.name, vc))
        rep.pages.append(page)
    rep.pages.sort(key=lambda p: p.ordinal)
    try:
        cfg = json.loads(layout.get("config", "{}") or "{}")
        rep.bookmarks = _walk_bookmarks(cfg.get("bookmarks", []))
    except ValueError:
        pass
    return rep


def _legacy_visual(page_name: str, vc: dict) -> Visual:
    cfg = json.loads(vc.get("config", "{}") or "{}")
    sv = cfg.get("singleVisual", {})
    is_group = "singleVisualGroup" in cfg
    vis = Visual(page_name, cfg.get("name", ""), "group" if is_group else sv.get("visualType", "unknown"),
                 _literal_title(sv.get("vcObjects", {})) or
                 (cfg.get("singleVisualGroup", {}) or {}).get("displayName", ""),
                 vc.get("x", 0), vc.get("y", 0), vc.get("width", 0), vc.get("height", 0),
                 hidden=bool((sv.get("display", {}) or {}).get("mode") == "hidden" or
                             (cfg.get("singleVisualGroup", {}) or {}).get("isHidden")),
                 parent_group=cfg.get("parentGroupName", ""), raw=cfg)
    pq = sv.get("prototypeQuery", {}) or {}
    aliases = {f["Name"]: f.get("Entity", "") for f in pq.get("From", [])}
    selects = {s.get("Name"): s for s in pq.get("Select", [])}
    names = {s.get("Name"): (sv.get("columnProperties", {}).get(s.get("Name"), {})
                             .get("displayName", ""))
             for s in pq.get("Select", [])}
    seen = set()
    for role, projs in (sv.get("projections", {}) or {}).items():
        for p in projs:
            qr = p.get("queryRef")
            if qr in selects and (role, qr) not in seen:
                seen.add((role, qr))
                f = parse_field(selects[qr], aliases, role, qr, names.get(qr, ""))
                if f.kind == "unsupported":
                    vis.warnings.append(f"Field {qr} uses an unsupported expression.")
                vis.fields.append(f)
    vis.filters = _parse_filters(vc.get("filters", "[]"), "visual")
    if vis.is_slicer:
        vis.filters += _slicer_filter(sv.get("objects", {}), vis.name)
    _button_action(vis, sv.get("vcObjects", {}) or sv.get("objects", {}))
    return vis


# ================================================================== entry
def parse_report_definition(parts: Dict[str, str]) -> ReportDef:
    if any(k.startswith("definition/pages/") for k in parts):
        return parse_pbir(parts)
    if "report.json" in parts:
        return parse_legacy(json.loads(parts["report.json"]))
    raise ValueError("Report definition has neither PBIR nor report.json")


def parse_pbix(data: bytes) -> ReportDef:
    with zipfile.ZipFile(io.BytesIO(data)) as z:
        names = z.namelist()
        if "Report/Layout" in names:
            raw = z.read("Report/Layout")
            text = raw.decode("utf-16-le") if b"\x00" in raw[:8] else raw.decode("utf-8")
            text = text.lstrip("﻿")
            return parse_legacy(json.loads(text))
        parts = {n: z.read(n).decode("utf-8", "replace") for n in names
                 if n.startswith("Report/definition/")}
        return parse_pbir({k[len("Report/"):]: v for k, v in parts.items()})


def unique(seq: Sequence) -> list:
    return list(dict.fromkeys(seq))
