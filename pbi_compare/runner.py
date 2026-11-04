"""Orchestrates one Prod-vs-Test comparison run."""
from __future__ import annotations

import time
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Callable, Dict, List, Optional, Sequence, Tuple

import pandas as pd

from .compare import (STATUS_COLORS, STATUS_DIFF, STATUS_ERROR, STATUS_HIDDEN, STATUS_MATCH,
                      STATUS_PROD_ONLY, STATUS_SKIPPED, STATUS_TEST_ONLY, DiffResult, diff_frames,
                      pair_pages, pair_visuals)
from .config import CompareSettings
from .dax_builder import DaxQuery, UserFilter, build_visual_query, choice_spec, rows_to_frame
from .model import SemanticModel, diff_models
from .pbi_api import PowerBIClient
from .report_parser import Bookmark, Field, FilterSpec, Page, ReportDef, Visual
from .screenshots import (PageShot, annotate_page, crop_visuals, placeholder_png,
                          shoot_with_browser, shoot_with_export_api, view_key, wireframe_png)
from .sql_builder import SqlGenerator, SqlResult


@dataclass
class FilterChoice:
    """The user's selection for one filter / slicer field (applied to both reports)."""
    field: Field
    values: Optional[List] = None      # None = All (clear the saved selection)

    def spec(self) -> Optional[List[FilterSpec]]:
        return None if self.values is None else [choice_spec(self.field, self.values)]

    @property
    def description(self) -> str:
        if self.values is None:
            return f"{self.field.friendly} = All"
        return choice_spec(self.field, self.values).description


@dataclass
class Side:
    label: str                      # "Production" / "Test"
    client: Optional[PowerBIClient]
    workspace: dict                 # {id, name, isOnDedicatedCapacity}
    report: dict                    # {id, name, datasetId, embedUrl, webUrl}
    report_def: ReportDef
    model: SemanticModel
    page_names: List[str]           # empty = all pages
    table_overrides: Dict[str, str] = field(default_factory=dict)
    last_refresh: Optional[dict] = None
    model_workspace: Optional[dict] = None   # where the semantic model lives, if elsewhere
    # Demo/offline mode: callable(dax) -> rows, instead of the REST API
    dax_runner: Optional[Callable[[str], List[dict]]] = None

    @property
    def ws_id(self) -> str:
        return self.workspace.get("id", "")

    @property
    def model_ws_id(self) -> str:
        return (self.model_workspace or {}).get("id") or self.ws_id

    @property
    def dataset_id(self) -> str:
        return self.report.get("datasetId", "")

    def run_dax(self, dax: str) -> List[dict]:
        if self.dax_runner:
            return self.dax_runner(dax)
        return self.client.execute_dax(self.model_ws_id, self.dataset_id, dax)


@dataclass
class SideVisual:
    visual: Optional[Visual]
    dax: Optional[DaxQuery] = None
    frame: Optional[pd.DataFrame] = None
    sql: Optional[SqlResult] = None
    screenshot: Optional[bytes] = None
    error: str = ""
    seconds: float = 0.0


@dataclass
class VisualResult:
    page_key: str
    prod: SideVisual
    test: SideVisual
    match_rule: str = ""
    status: str = STATUS_SKIPPED
    diff: Optional[DiffResult] = None
    notes: List[str] = field(default_factory=list)

    @property
    def title(self) -> str:
        v = self.prod.visual or self.test.visual
        return v.label() if v else "?"

    @property
    def visual_type(self) -> str:
        v = self.prod.visual or self.test.visual
        return v.visual_type if v else ""


@dataclass
class PageResult:
    prod_page: Optional[Page]
    test_page: Optional[Page]
    prod_bookmark: Optional[Bookmark] = None
    test_bookmark: Optional[Bookmark] = None
    visuals: List[VisualResult] = field(default_factory=list)
    prod_shot: Optional[PageShot] = None
    test_shot: Optional[PageShot] = None
    prod_annotated: Optional[bytes] = None
    test_annotated: Optional[bytes] = None
    notes: List[str] = field(default_factory=list)

    @property
    def key(self) -> str:
        return view_key((self.prod_page or self.test_page).name,
                        (self.prod_bookmark or self.test_bookmark or Bookmark("", "")).name)

    @property
    def view_label(self) -> str:
        b = self.prod_bookmark or self.test_bookmark
        return b.label if b else ""

    @property
    def title(self) -> str:
        p, t = self.prod_page, self.test_page
        if p and t and p.display_name != t.display_name:
            base = f"{p.display_name}  ↔  {t.display_name}"
        else:
            base = (p or t).display_name
        return f"{base} › {self.view_label}" if self.view_label else base

    @property
    def status(self) -> str:
        if not self.prod_page:
            return STATUS_TEST_ONLY
        if not self.test_page:
            return STATUS_PROD_ONLY
        sts = {v.status for v in self.visuals}
        if STATUS_ERROR in sts:
            return STATUS_ERROR
        if sts & {STATUS_DIFF, STATUS_PROD_ONLY, STATUS_TEST_ONLY}:
            return STATUS_DIFF
        return STATUS_MATCH


@dataclass
class RunResult:
    tenant: str
    prod: Side
    test: Side
    settings: CompareSettings
    user_filters: List[UserFilter]
    started: datetime
    choices: List[FilterChoice] = field(default_factory=list)
    finished: Optional[datetime] = None
    pages: List[PageResult] = field(default_factory=list)
    model_diff: Optional[pd.DataFrame] = None
    log: List[str] = field(default_factory=list)

    def all_visuals(self) -> List[VisualResult]:
        return [v for p in self.pages for v in p.visuals]

    def counts(self) -> Dict[str, int]:
        out: Dict[str, int] = {}
        for v in self.all_visuals():
            out[v.status] = out.get(v.status, 0) + 1
        return out

    def filter_summary(self) -> List[str]:
        return [c.description for c in self.choices] + \
            [u.to_spec().description + " (custom)" for u in self.user_filters]

    def summary_frame(self) -> pd.DataFrame:
        rows = []
        for p in self.pages:
            for v in p.visuals:
                rows.append({
                    "Page / view": p.title, "Visual": v.title, "Type": v.visual_type,
                    "Status": v.status, "Matched by": v.match_rule,
                    "Prod rows": None if v.prod.frame is None else len(v.prod.frame),
                    "Test rows": None if v.test.frame is None else len(v.test.frame),
                    "Mismatched rows": (v.diff.counts.get(STATUS_DIFF, 0) if v.diff else None),
                    "Only in Prod": (v.diff.counts.get(STATUS_PROD_ONLY, 0) if v.diff else None),
                    "Only in Test": (v.diff.counts.get(STATUS_TEST_ONLY, 0) if v.diff else None),
                    "SQL fully translated": (v.prod.sql.fully_translated if v.prod.sql else None),
                    "Notes": " | ".join(v.notes)[:500],
                })
        return pd.DataFrame(rows)


# ===================================================================== run
def run_comparison(tenant: str, prod: Side, test: Side, settings: CompareSettings,
                   user_filters: Sequence[UserFilter],
                   progress: Callable[[float, str], None] = lambda f, m: None,
                   views: Optional[Dict[str, List[Optional[str]]]] = None,
                   choices: Sequence[FilterChoice] = ()) -> RunResult:
    """``views`` maps a Production page name to the views to compare on it:
    ``None`` = the page as saved, a bookmark name = the page with that bookmark
    applied (button-driven tabs). Missing pages default to ``[None]``."""
    res = RunResult(tenant, prod, test, settings, list(user_filters),
                    datetime.now(timezone.utc), list(choices))
    ufilters = [u.to_spec() for u in user_filters]
    choice_specs = {c.field.key: c.spec() for c in choices}
    res.model_diff = diff_models(prod.model, test.model)

    pairs, p_only, t_only = pair_pages(prod.report_def, test.report_def,
                                       prod.page_names, test.page_names)
    for p, t in pairs:
        for bm_name in (views or {}).get(p.name, [None]) or [None]:
            pbm = prod.report_def.bookmark(bm_name) if bm_name else None
            tbm = test.report_def.find_bookmark(pbm) if pbm else None
            pr = PageResult(p, t, pbm, tbm)
            if pbm and not tbm:
                pr.notes.append(f"Bookmark '{pbm.display_name}' not found in the Test report - "
                                "Test page compared as saved.")
            vpairs, pv, tv = pair_visuals(p, t)
            for a, b, rule in vpairs:
                pr.visuals.append(VisualResult(p.name, SideVisual(a), SideVisual(b), rule))
            for a in pv:
                pr.visuals.append(VisualResult(p.name, SideVisual(a), SideVisual(None),
                                               status=STATUS_PROD_ONLY))
            for b in tv:
                pr.visuals.append(VisualResult(p.name, SideVisual(None), SideVisual(b),
                                               status=STATUS_TEST_ONLY))
            res.pages.append(pr)
    for p in p_only:
        res.pages.append(PageResult(p, None))
    for t in t_only:
        res.pages.append(PageResult(None, t))

    # ------------------------------------------------- visibility per view
    for pr in res.pages:
        for vr in pr.visuals:
            ph = (vr.prod.visual is None or pr.prod_page is None or
                  prod.report_def.is_hidden(pr.prod_page, vr.prod.visual, pr.prod_bookmark))
            th = (vr.test.visual is None or pr.test_page is None or
                  test.report_def.is_hidden(pr.test_page, vr.test.visual, pr.test_bookmark))
            if vr.prod.visual is not None and vr.test.visual is not None and ph and th:
                vr.status = STATUS_HIDDEN
            elif vr.prod.visual is not None and vr.test.visual is not None and ph != th:
                vr.notes.append(f"Visible only in {'Test' if ph else 'Production'} in this view.")

    # ------------------------------------------------------------- data
    jobs: List[Tuple[VisualResult, Side, SideVisual, Page, Optional[Bookmark]]] = []
    for pr in res.pages:
        for vr in pr.visuals:
            if vr.status == STATUS_HIDDEN:
                continue
            for side, sv, pg, bm in ((prod, vr.prod, pr.prod_page, pr.prod_bookmark),
                                     (test, vr.test, pr.test_page, pr.test_bookmark)):
                if sv.visual is not None and pg is not None and sv.visual.is_data_visual:
                    jobs.append((vr, side, sv, pg, bm))
    total = max(1, len(jobs))
    done = [0]
    cache: Dict[Tuple[str, str], Tuple] = {}   # same visual + same filters -> reuse result

    def run_job(job):
        vr, side, sv, pg, bm = job
        t0 = time.time()
        filters = side.report_def.effective_filters(pg, sv.visual, bm, choice_specs) + ufilters
        try:
            sv.dax = build_visual_query(sv.visual, filters, settings.max_rows)
            ck = (side.label, sv.dax.dax)
            if ck in cache:
                sv.frame = cache[ck]
            else:
                rows = side.run_dax(sv.dax.dax)
                sv.frame = cache[ck] = rows_to_frame(rows, sv.dax)
            if len(sv.frame) >= min(settings.max_rows, 100000):
                vr.notes.append(f"{side.label}: result capped at {len(sv.frame)} rows.")
        except Exception as exc:  # noqa: BLE001
            sv.error = str(exc)[:1500]
        if settings.generate_sql:
            try:
                sv.sql = SqlGenerator(side.model, side.table_overrides).generate(
                    sv.visual, filters, f"{side.label} - {side.report.get('name', '')}")
            except Exception as exc:  # noqa: BLE001
                vr.notes.append(f"{side.label} SQL generation failed: {exc}")
        sv.seconds = time.time() - t0
        return job

    with ThreadPoolExecutor(max_workers=4) as pool:
        for _ in pool.map(run_job, jobs):
            done[0] += 1
            progress(0.05 + 0.6 * done[0] / total, f"Queried {done[0]} of {total} visual datasets")

    for pr in res.pages:
        for vr in pr.visuals:
            _finalise(vr, settings)

    # ------------------------------------------------------ screenshots
    if settings.screenshot_mode != "off":
        for k, side in enumerate((prod, test)):
            views_side = []
            for pr in res.pages:
                pg = pr.prod_page if side is prod else pr.test_page
                bm = pr.prod_bookmark if side is prod else pr.test_bookmark
                if pg is not None:
                    views_side.append((pg, bm.name if bm else None))
            views_side = list({view_key(p.name, b): (p, b) for p, b in views_side}.values())
            if not views_side:
                continue
            progress(0.7 + 0.12 * k, f"Capturing {side.label} screenshots ({len(views_side)} views)")
            shots = _capture(side, views_side, user_filters, list(choices), settings, res.log)
            for pr in res.pages:
                pg = pr.prod_page if side is prod else pr.test_page
                bm = pr.prod_bookmark if side is prod else pr.test_bookmark
                if pg is None:
                    continue
                shot = shots.get(view_key(pg.name, bm.name if bm else None)) or \
                    PageShot(pg.name, error="not captured")
                if side is prod:
                    pr.prod_shot = shot
                else:
                    pr.test_shot = shot
                if shot.png and shot.error:
                    pr.notes.append(f"{side.label} screenshot: {shot.error}")
                for vr in pr.visuals:
                    sv = vr.prod if side is prod else vr.test
                    if sv.visual is None or vr.status == STATUS_HIDDEN or sv.visual.is_group:
                        continue
                    sv.screenshot = shot.visuals.get(sv.visual.name) or placeholder_png(
                        f"{side.label}: no screenshot for '{sv.visual.label()}'. "
                        f"{shot.error or 'Visual is hidden in this view or outside the canvas.'}",
                        480, 200)

    for pr in res.pages:
        for side, pg, shot in ((prod, pr.prod_page, pr.prod_shot), (test, pr.test_page, pr.test_shot)):
            if pg is None or shot is None or not shot.png:
                continue
            status = {}
            for vr in pr.visuals:
                sv = vr.prod if side is prod else vr.test
                if sv.visual is not None and vr.status not in (STATUS_SKIPPED, STATUS_HIDDEN) \
                        and not sv.visual.is_group:
                    status[sv.visual.name] = vr.status
            ann = annotate_page(shot.png, pg, status, STATUS_COLORS)
            if side is prod:
                pr.prod_annotated = ann
            else:
                pr.test_annotated = ann
    progress(1.0, "Done")
    res.finished = datetime.now(timezone.utc)
    return res


def _finalise(vr: VisualResult, settings: CompareSettings) -> None:
    p, t = vr.prod, vr.test
    if p.visual is None or t.visual is None or vr.status == STATUS_HIDDEN:
        return
    if not p.visual.is_data_visual and not t.visual.is_data_visual:
        vr.status = STATUS_SKIPPED
        vr.notes.append("No data (button, text, image or group) - compared by screenshot only.")
        return
    if p.dax is None or t.dax is None:
        vr.status = STATUS_ERROR
        vr.notes.append("Visual holds data on one side only.")
        return
    if p.error or t.error:
        vr.status = STATUS_ERROR
        if p.error:
            vr.notes.append(f"Production query failed: {p.error}")
        if t.error:
            vr.notes.append(f"Test query failed: {t.error}")
        return
    if p.visual.visual_type != t.visual.visual_type:
        vr.notes.append(f"Visual type differs: {p.visual.visual_type} vs {t.visual.visual_type}")
    keys, vals = p.dax.group_labels, p.dax.value_labels
    tf = t.frame
    t_cols = t.dax.group_labels + t.dax.value_labels
    p_cols = keys + vals
    if t_cols != p_cols and len(t_cols) == len(p_cols):
        tf = tf.rename(columns=dict(zip(t_cols, p_cols)))
        vr.notes.append("Field labels differ between reports; columns aligned by position.")
    vr.diff = diff_frames(p.frame, tf, keys, vals, settings)
    vr.notes += vr.diff.schema_notes
    for w in (p.dax.warnings + t.dax.warnings):
        if w not in vr.notes:
            vr.notes.append(w)
    vr.status = vr.diff.status


def _choices_js(side: Side, views, choices: Sequence[FilterChoice],
                user_filters: Sequence[UserFilter]) -> dict:
    ov, meta = {}, {}
    for c in choices:
        f = c.field
        ov[f.js_key] = c.values
        if f.variation:   # auto date/time level, e.g. Sales[OrderDate] › Year
            meta[f.js_key] = {"table": f.table, "hierarchy": f"{f.name} Hierarchy",
                              "hierarchyLevel": f.hierarchy}
        elif f.kind == "hierarchy":
            meta[f.js_key] = {"table": f.table, "hierarchy": f.hierarchy, "hierarchyLevel": f.name}
        else:
            meta[f.js_key] = {"table": f.table, "column": f.name}
    return {"ov": ov, "meta": meta, "extra": [u.to_js_filter() for u in user_filters]}


def _capture(side: Side, views, user_filters: Sequence[UserFilter],
             choices: Sequence[FilterChoice], settings: CompareSettings,
             log: List[str]) -> Dict[str, PageShot]:
    if side.client is None:  # demo mode: draw a wireframe of each view instead
        out = {}
        for p, bm in views:
            bmo = side.report_def.bookmark(bm) if bm else None
            hidden = {v.name for v in p.visuals if side.report_def.is_hidden(p, v, bmo)}
            png = wireframe_png(p, side.label + (f" › {bmo.display_name}" if bmo else ""), hidden)
            out[view_key(p.name, bm)] = PageShot(p.name, png, crop_visuals(png, p),
                                                 engine="wireframe", bookmark=bm or "",
                                                 hidden=hidden)
        return out
    mode = settings.screenshot_mode
    if mode == "auto":
        # Export API can add filters but can't clear or replace slicer selections,
        # so filter choices need the browser engine to be faithful.
        mode = "export_api" if side.workspace.get("isOnDedicatedCapacity") and not choices \
            else "browser"
    log.append(f"{side.label}: screenshots via {mode}")
    cjs = _choices_js(side, views, choices, user_filters)
    try:
        if mode == "export_api":
            if choices:
                log.append(f"{side.label}: Export API can't replace slicer selections - "
                           "screenshots show saved slicer state; data uses your choices.")
            shots = shoot_with_export_api(side.client, side.ws_id, side.report["id"], views,
                                          [u.to_export_filter() for u in user_filters])
            failed = [(p, b) for p, b in views if not shots[view_key(p.name, b)].png]
            if failed:
                k0 = view_key(failed[0][0].name, failed[0][1])
                log.append(f"{side.label}: Export API failed for {len(failed)} view(s) "
                           f"({shots[k0].error[:200]}); retrying in browser")
                shots.update(shoot_with_browser(side.client, side.ws_id, side.report, failed, cjs))
            return shots
        return shoot_with_browser(side.client, side.ws_id, side.report, views, cjs)
    except Exception as exc:  # noqa: BLE001
        log.append(f"{side.label}: screenshots failed - {exc}")
        return {view_key(p.name, b): PageShot(p.name, error=str(exc)[:300]) for p, b in views}
