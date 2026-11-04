"""Pair pages/visuals between the two reports and diff their data row by row."""
from __future__ import annotations

from dataclasses import dataclass, field
from typing import Dict, List, Sequence, Tuple

import numpy as np
import pandas as pd

from .config import CompareSettings
from .report_parser import Page, ReportDef, Visual

STATUS_MATCH = "Match"
STATUS_DIFF = "Mismatch"
STATUS_PROD_ONLY = "Only in Production"
STATUS_TEST_ONLY = "Only in Test"
STATUS_ERROR = "Error"
STATUS_SKIPPED = "Not compared"
STATUS_HIDDEN = "Hidden in view"

ROW_COLORS = {STATUS_DIFF: "#FDECEA", STATUS_PROD_ONLY: "#FFF4E5", STATUS_TEST_ONLY: "#E8F1FD",
              STATUS_MATCH: ""}
STATUS_COLORS = {STATUS_MATCH: "#1E8E3E", STATUS_DIFF: "#D93025", STATUS_PROD_ONLY: "#E37400",
                 STATUS_TEST_ONLY: "#1A73E8", STATUS_ERROR: "#A50E0E", STATUS_SKIPPED: "#5F6368",
                 STATUS_HIDDEN: "#9AA0A6"}


# ============================================================ pairing
def pair_pages(prod: ReportDef, test: ReportDef, prod_sel: Sequence[str],
               test_sel: Sequence[str]) -> Tuple[List[Tuple[Page, Page]], List[Page], List[Page]]:
    """prod_sel / test_sel are page names; empty means all pages."""
    p_pages = [p for p in prod.pages if not prod_sel or p.name in prod_sel]
    t_pages = [p for p in test.pages if not test_sel or p.name in test_sel]
    if len(p_pages) == 1 and len(t_pages) == 1:
        return [(p_pages[0], t_pages[0])], [], []
    pairs, used = [], set()
    for p in p_pages:
        t = next((x for x in t_pages if x.name == p.name and x.name not in used), None) or \
            next((x for x in t_pages if x.display_name.strip().lower() ==
                  p.display_name.strip().lower() and x.name not in used), None)
        if t:
            used.add(t.name)
            pairs.append((p, t))
    paired_p = {p.name for p, _ in pairs}
    return (pairs, [p for p in p_pages if p.name not in paired_p],
            [t for t in t_pages if t.name not in used])


def pair_visuals(p: Page, t: Page) -> Tuple[List[Tuple[Visual, Visual, str]], List[Visual], List[Visual]]:
    pv = list(p.visuals)
    tv = list(t.visuals)
    pairs: List[Tuple[Visual, Visual, str]] = []

    def take(rule: str, key):
        for a in list(pv):
            ka = key(a)
            if ka is None:
                continue
            b = next((x for x in tv if key(x) == ka), None)
            if b is not None:
                pairs.append((a, b, rule))
                pv.remove(a)
                tv.remove(b)

    take("visual id", lambda v: v.name or None)
    take("type + title", lambda v: (v.visual_type, v.title.lower()) if v.title else None)
    take("type + fields", lambda v: (v.visual_type, v.signature()) if v.fields else None)
    # last resort: same type, closest position
    for a in list(pv):
        cands = [x for x in tv if x.visual_type == a.visual_type]
        if cands:
            b = min(cands, key=lambda x: abs(x.x - a.x) + abs(x.y - a.y))
            pairs.append((a, b, "type + position"))
            pv.remove(a)
            tv.remove(b)
    return pairs, pv, tv


# =============================================================== diff
@dataclass
class DiffResult:
    frame: pd.DataFrame                       # merged frame with __status
    keys: List[str]
    values: List[str]
    counts: Dict[str, int]
    schema_notes: List[str] = field(default_factory=list)

    @property
    def status(self) -> str:
        if self.schema_notes or any(self.counts.get(s, 0) for s in
                                    (STATUS_DIFF, STATUS_PROD_ONLY, STATUS_TEST_ONLY)):
            return STATUS_DIFF
        return STATUS_MATCH


def _norm_key(s: pd.Series, settings: CompareSettings) -> pd.Series:
    def f(v):
        if v is None or (isinstance(v, float) and np.isnan(v)):
            return "(Blank)"
        if isinstance(v, float) and v.is_integer():
            v = int(v)
        v = str(v)
        if settings.trim_text:
            v = v.strip()
        if not settings.case_sensitive_text:
            v = v.lower()
        return v
    return s.map(f)


def _is_num(s: pd.Series) -> bool:
    s = s.dropna()
    if s.empty:
        return True
    if pd.api.types.is_bool_dtype(s):
        return False
    return bool(pd.to_numeric(s, errors="coerce").notna().all())


def diff_frames(prod: pd.DataFrame, test: pd.DataFrame, keys: Sequence[str],
                values: Sequence[str], settings: CompareSettings) -> DiffResult:
    keys = [k for k in keys]
    notes = []
    for side, df in (("Production", prod), ("Test", test)):
        missing = [c for c in list(keys) + list(values) if c not in df.columns]
        if missing:
            notes.append(f"{side} result is missing column(s): {', '.join(missing)}")
    keys = [k for k in keys if k in prod.columns and k in test.columns]
    vals = [v for v in values if v in prod.columns or v in test.columns]

    p, t = prod.copy(), test.copy()
    for df in (p, t):
        for k in keys:
            df[f"__k_{k}"] = _norm_key(df[k], settings)
        kcols = [f"__k_{k}" for k in keys]
        df["__occ"] = df.groupby(kcols).cumcount() if kcols else range(len(df))
    kcols = [f"__k_{k}" for k in keys] + ["__occ"]
    m = p.merge(t, on=kcols, how="outer", suffixes=(" (Prod)", " (Test)"), indicator=True)

    out = pd.DataFrame(index=m.index)
    for k in keys:
        out[k] = m[f"{k} (Prod)"].where(m["_merge"] != "right_only", m[f"{k} (Test)"])
    status = pd.Series(STATUS_MATCH, index=m.index)
    status[m["_merge"] == "left_only"] = STATUS_PROD_ONLY
    status[m["_merge"] == "right_only"] = STATUS_TEST_ONLY
    diff_cells = pd.DataFrame(False, index=m.index, columns=vals)
    nan = pd.Series(np.nan, index=m.index)

    def side(v, df, suffix):
        if v not in df.columns:
            return nan
        return m[f"{v}{suffix}"] if f"{v}{suffix}" in m.columns else m[v]

    for v in vals:
        pc, tc = side(v, p, " (Prod)"), side(v, t, " (Test)")
        out[f"{v} (Prod)"] = pc
        out[f"{v} (Test)"] = tc
        both = m["_merge"] == "both"
        if _is_num(pc) and _is_num(tc):
            a, b = pd.to_numeric(pc, errors="coerce"), pd.to_numeric(tc, errors="coerce")
            delta = b - a
            tol = np.maximum(settings.abs_tolerance, settings.rel_tolerance * a.abs().fillna(0))
            neq = (delta.abs() > tol) | (a.isna() != b.isna())
            out[f"{v} Δ"] = delta
        else:
            neq = _norm_key(pc, settings) != _norm_key(tc, settings)
        diff_cells[v] = neq & both
    status[(diff_cells.any(axis=1)) & (status == STATUS_MATCH)] = STATUS_DIFF
    out.insert(0, "__status", status)
    out = out.sort_values(by=["__status"], key=lambda s: s.map(
        {STATUS_DIFF: 0, STATUS_PROD_ONLY: 1, STATUS_TEST_ONLY: 2, STATUS_MATCH: 3}),
        kind="stable").reset_index(drop=True)
    counts = out["__status"].value_counts().to_dict()
    counts["Production rows"] = len(prod)
    counts["Test rows"] = len(test)
    return DiffResult(out, keys, vals, counts, notes)


def mismatched_cells(d: DiffResult, settings: CompareSettings) -> pd.DataFrame:
    """Boolean frame (same index as d.frame) marking value cells that differ."""
    f = d.frame
    mask = pd.DataFrame(False, index=f.index, columns=f.columns)
    for v in d.values:
        pc, tc = f"{v} (Prod)", f"{v} (Test)"
        both = f["__status"] == STATUS_DIFF
        if f"{v} Δ" in f:
            a = pd.to_numeric(f[pc], errors="coerce")
            b = pd.to_numeric(f[tc], errors="coerce")
            tol = np.maximum(settings.abs_tolerance, settings.rel_tolerance * a.abs().fillna(0))
            neq = ((b - a).abs() > tol) | (a.isna() != b.isna())
            mask[f"{v} Δ"] = neq & both
        else:
            neq = _norm_key(f[pc], settings) != _norm_key(f[tc], settings)
        mask[pc] = neq & both
        mask[tc] = neq & both
    return mask


def style_diff(d: DiffResult, settings: CompareSettings, max_rows: int = 2000):
    f = d.frame.head(max_rows)
    cells = mismatched_cells(d, settings).head(max_rows)

    def row_style(row):
        bg = ROW_COLORS.get(row["__status"], "")
        return [f"background-color: {bg}" if bg else "" for _ in row]

    styler = f.rename(columns={"__status": "Status"}).style
    styler = styler.apply(lambda r: row_style(r.rename({"Status": "__status"})), axis=1)
    css = "color: #B3261E; font-weight: 700; outline: 1px solid #D93025"
    cell_css = pd.DataFrame(np.where(cells.to_numpy(dtype=bool), css, ""), index=cells.index,
                            columns=["Status" if c == "__status" else c for c in cells.columns])
    styler = styler.apply(lambda _: cell_css, axis=None)
    styler = styler.format(precision=4, na_rep="(Blank)")
    return styler
