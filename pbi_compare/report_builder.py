"""Comparison report outputs: self-contained HTML, Excel workbook and a ZIP bundle."""
from __future__ import annotations

import base64
import html
import io
import re
import zipfile
from typing import Optional

import pandas as pd
from openpyxl import Workbook
from openpyxl.styles import Alignment, Font, PatternFill
from openpyxl.utils import get_column_letter

from .compare import (ROW_COLORS, STATUS_COLORS, STATUS_DIFF, STATUS_ERROR, STATUS_MATCH,
                      STATUS_HIDDEN, STATUS_PROD_ONLY, STATUS_SKIPPED, STATUS_TEST_ONLY,
                      mismatched_cells)
from .runner import RunResult, VisualResult

MAX_HTML_ROWS = 500


def _b64(png: Optional[bytes]) -> str:
    return "data:image/png;base64," + base64.b64encode(png).decode() if png else ""


def _esc(s) -> str:
    return html.escape(str(s if s is not None else ""))


def _badge(status: str) -> str:
    return (f'<span class="badge" style="background:{STATUS_COLORS.get(status, "#5F6368")}">'
            f'{_esc(status)}</span>')


def _slug(s: str) -> str:
    return re.sub(r"[^A-Za-z0-9]+", "_", s).strip("_")[:60] or "item"


def _diff_table_html(vr: VisualResult, run: RunResult) -> str:
    d = vr.diff
    if d is None:
        return ""
    f = d.frame.head(MAX_HTML_ROWS)
    cells = mismatched_cells(d, run.settings).head(MAX_HTML_ROWS)
    cols = [c for c in f.columns]
    head = "".join(f"<th>{_esc('Status' if c == '__status' else c)}</th>" for c in cols)
    body = []
    for i, row in f.iterrows():
        bg = ROW_COLORS.get(row["__status"], "")
        tds = []
        for c in cols:
            v = row[c]
            if isinstance(v, float):
                txt = "(Blank)" if pd.isna(v) else f"{v:,.4f}".rstrip("0").rstrip(".")
            elif v is None or (not isinstance(v, str) and pd.isna(v)):
                txt = "(Blank)"
            else:
                txt = str(v)
            cls = ' class="bad"' if cells.at[i, c] else ""
            tds.append(f"<td{cls}>{_esc(txt)}</td>")
        body.append(f'<tr style="background:{bg}">' + "".join(tds) + "</tr>")
    more = (f'<p class="muted">Showing first {MAX_HTML_ROWS} of {len(d.frame)} rows - full '
            "detail is in the Excel workbook.</p>" if len(d.frame) > MAX_HTML_ROWS else "")
    counts = " · ".join(f"{k}: {v}" for k, v in d.counts.items())
    return (f'<p class="muted">{_esc(counts)}</p><div class="tablewrap"><table class="diff">'
            f"<thead><tr>{head}</tr></thead><tbody>{''.join(body)}</tbody></table></div>{more}")


def build_html(run: RunResult) -> str:
    c = run.counts()
    total = sum(c.values())
    kpis = "".join(
        f'<div class="kpi" style="border-top-color:{STATUS_COLORS[s]}"><div class="n">{c.get(s, 0)}'
        f'</div><div class="l">{s}</div></div>'
        for s in (STATUS_MATCH, STATUS_DIFF, STATUS_PROD_ONLY, STATUS_TEST_ONLY, STATUS_ERROR,
                  STATUS_SKIPPED, STATUS_HIDDEN) if s in c or s in (STATUS_MATCH, STATUS_DIFF))
    meta_rows = [
        ("Tenant", run.tenant),
        ("Run by", run.run_by or "n/a"),
        ("Row-level security", (f"role {run.role}" + (f" as {run.run_by}" if run.run_by else ""))
         if run.role else "not applied"),
        ("Production", f"{run.prod.workspace.get('name')} / {run.prod.report.get('name')}"),
        ("Test", f"{run.test.workspace.get('name')} / {run.test.report.get('name')}"),
        ("Prod model last refresh", (run.prod.last_refresh or {}).get("endTime", "n/a")),
        ("Test model last refresh", (run.test.last_refresh or {}).get("endTime", "n/a")),
        ("Run (UTC)", f"{run.started:%Y-%m-%d %H:%M:%S} → "
                      f"{run.finished:%H:%M:%S}" if run.finished else str(run.started)),
        ("Tolerance", f"abs {run.settings.abs_tolerance} · rel {run.settings.rel_tolerance}"),
        ("Prod model workspace", (run.prod.model_workspace or run.prod.workspace).get("name", "")),
        ("Test model workspace", (run.test.model_workspace or run.test.workspace).get("name", "")),
        ("Filter selections", "; ".join(run.filter_summary()) or "as saved in each report"),
        ("Screenshots", "; ".join(run.log) or run.settings.screenshot_mode),
    ]
    meta = "".join(f"<tr><th>{_esc(k)}</th><td>{_esc(v)}</td></tr>" for k, v in meta_rows)

    md = run.model_diff if run.model_diff is not None else pd.DataFrame()
    model_html = ('<p class="ok">Semantic models are identical (tables, columns, measures, '
                  "relationships, sources).</p>" if md.empty else
                  md.to_html(index=False, classes="diff", escape=True, border=0))

    toc, sections = [], []
    for pi, pr in enumerate(run.pages):
        pid = f"p{pi}"
        toc.append(f'<li><a href="#{pid}">{_esc(pr.title)}</a> {_badge(pr.status)}</li>')
        parts = [f'<section id="{pid}"><h2>{_esc(pr.title)} {_badge(pr.status)}</h2>']
        if pr.notes:
            parts.append("<ul class='notes'>" + "".join(f"<li>{_esc(n)}</li>" for n in pr.notes)
                         + "</ul>")
        if pr.prod_annotated or pr.test_annotated or pr.prod_shot or pr.test_shot:
            pimg = pr.prod_annotated or (pr.prod_shot.png if pr.prod_shot else None)
            timg = pr.test_annotated or (pr.test_shot.png if pr.test_shot else None)
            parts.append('<div class="pair">'
                         f'<figure><figcaption>Production</figcaption>'
                         f'{"<img src=" + chr(34) + _b64(pimg) + chr(34) + ">" if pimg else "<p class=muted>No screenshot</p>"}</figure>'
                         f'<figure><figcaption>Test</figcaption>'
                         f'{"<img src=" + chr(34) + _b64(timg) + chr(34) + ">" if timg else "<p class=muted>No screenshot</p>"}</figure>'
                         "</div>")
        for vi, vr in enumerate(pr.visuals):
            if vr.status == STATUS_HIDDEN:
                continue
            open_attr = " open" if vr.status in (STATUS_DIFF, STATUS_ERROR) else ""
            parts.append(f'<details class="visual"{open_attr}><summary>{_badge(vr.status)} '
                         f"<b>{_esc(vr.title)}</b> <span class='muted'>{_esc(vr.visual_type)}"
                         f"{' · matched by ' + _esc(vr.match_rule) if vr.match_rule else ''}"
                         "</span></summary>")
            parts.append('<div class="pair">')
            for lab, sv in (("Production", vr.prod), ("Test", vr.test)):
                img = f'<img src="{_b64(sv.screenshot)}">' if sv.screenshot else \
                    "<p class='muted'>No screenshot</p>"
                parts.append(f"<figure><figcaption>{lab}</figcaption>{img}</figure>")
            parts.append("</div>")
            if vr.notes:
                parts.append("<ul class='notes'>" + "".join(f"<li>{_esc(n)}</li>" for n in vr.notes)
                             + "</ul>")
            parts.append(_diff_table_html(vr, run))
            for lab, sv in (("Production", vr.prod), ("Test", vr.test)):
                if sv.dax and run.settings.include_dax_in_report:
                    parts.append(f"<details><summary>{lab} DAX query &amp; filters</summary>"
                                 f"<pre>{_esc(sv.dax.dax)}</pre>"
                                 + ("<p class='muted'>Filters: " +
                                    _esc(" | ".join(sv.dax.filters_applied)) + "</p>"
                                    if sv.dax.filters_applied else "") + "</details>")
                if sv.sql and run.settings.include_sql_in_report:
                    parts.append(f"<details><summary>{lab} Snowflake validation SQL"
                                 f"{'' if sv.sql.fully_translated else ' (needs review)'}</summary>"
                                 f"<pre>{_esc(sv.sql.sql)}</pre></details>")
            parts.append("</details>")
        parts.append("</section>")
        sections.append("".join(parts))

    return f"""<!doctype html><html lang="en"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>Power BI Prod vs Test comparison</title>
<style>
:root{{--fg:#202124;--muted:#5f6368;--line:#dadce0;--bg:#fff;--soft:#f8f9fa}}
body{{font-family:Segoe UI,Roboto,Helvetica,Arial,sans-serif;color:var(--fg);background:var(--bg);
margin:0;padding:24px 32px;line-height:1.45}}
h1{{font-size:24px;margin:0 0 4px}} h2{{font-size:19px;margin:32px 0 12px;padding-top:12px;
border-top:2px solid var(--line)}} h3{{font-size:16px}}
.muted{{color:var(--muted);font-size:13px}} .ok{{color:#1E8E3E;font-weight:600}}
.badge{{display:inline-block;color:#fff;font-size:11px;font-weight:600;padding:2px 8px;
border-radius:10px;vertical-align:middle}}
.kpis{{display:flex;gap:12px;flex-wrap:wrap;margin:16px 0}}
.kpi{{border:1px solid var(--line);border-top:4px solid;border-radius:6px;padding:10px 16px;
min-width:120px}} .kpi .n{{font-size:26px;font-weight:700}} .kpi .l{{font-size:12px;color:var(--muted)}}
table.meta th{{text-align:left;padding:3px 16px 3px 0;color:var(--muted);font-weight:500}}
.tablewrap{{overflow:auto;max-height:520px;border:1px solid var(--line);border-radius:6px}}
table.diff{{border-collapse:collapse;font-size:12.5px;width:100%}}
table.diff th{{position:sticky;top:0;background:#f1f3f4;text-align:left;padding:6px 8px;
border-bottom:1px solid var(--line);white-space:nowrap}}
table.diff td{{padding:4px 8px;border-bottom:1px solid #eee;white-space:nowrap}}
td.bad{{color:#B3261E;font-weight:700;outline:1px solid #D93025}}
.pair{{display:grid;grid-template-columns:1fr 1fr;gap:16px;margin:10px 0}}
figure{{margin:0;border:1px solid var(--line);border-radius:6px;padding:8px;background:var(--soft)}}
figure img{{max-width:100%;display:block;margin:auto}} figcaption{{font-size:12px;font-weight:600;
color:var(--muted);margin-bottom:6px}}
details.visual{{border:1px solid var(--line);border-radius:8px;padding:8px 12px;margin:10px 0}}
details summary{{cursor:pointer}} pre{{background:#f6f8fa;padding:10px;border-radius:6px;
overflow:auto;font-size:12px}} ul.notes{{font-size:13px;color:#8a4b00}}
@media (max-width:900px){{.pair{{grid-template-columns:1fr}}}}
@media print{{details{{display:block}} details>*{{display:block}}}}
</style></head><body>
<h1>Power BI report comparison - Production vs Test</h1>
<p class="muted">{total} visual pair(s) across {len(run.pages)} page(s)</p>
<div class="kpis">{kpis}</div>
<table class="meta">{meta}</table>
<h2>Semantic model differences</h2>{model_html}
<h2>Pages</h2><ul>{''.join(toc)}</ul>
{''.join(sections)}
</body></html>"""


# =================================================================== Excel
_FILL = {k: PatternFill("solid", fgColor=v.lstrip("#")) for k, v in ROW_COLORS.items() if v}
_BAD = Font(color="B3261E", bold=True)


def _write_frame(ws, df: pd.DataFrame, start_row: int = 1):
    for j, c in enumerate(df.columns, 1):
        cell = ws.cell(start_row, j, "Status" if c == "__status" else str(c))
        cell.font = Font(bold=True)
        cell.fill = PatternFill("solid", fgColor="F1F3F4")
    for i, row in enumerate(df.itertuples(index=False), start_row + 1):
        for j, v in enumerate(row, 1):
            if isinstance(v, float) and pd.isna(v):
                v = None
            ws.cell(i, j, v if isinstance(v, (int, float, str, bool, type(None))) else str(v))
    for j, c in enumerate(df.columns, 1):
        ws.column_dimensions[get_column_letter(j)].width = min(45, max(10, len(str(c)) + 4))
    ws.freeze_panes = ws.cell(start_row + 1, 1)


def build_excel(run: RunResult) -> bytes:
    wb = Workbook()
    ws = wb.active
    ws.title = "Summary"
    summ = run.summary_frame()
    _write_frame(ws, summ)
    sidx = list(summ.columns).index("Status") + 1 if not summ.empty else 0
    for r in range(2, len(summ) + 2):
        st = ws.cell(r, sidx).value
        if st in STATUS_COLORS:
            ws.cell(r, sidx).font = Font(color=STATUS_COLORS[st].lstrip("#"), bold=True)

    ws = wb.create_sheet("Model differences")
    md = run.model_diff if run.model_diff is not None else pd.DataFrame()
    _write_frame(ws, md if not md.empty else pd.DataFrame({"Result": ["Models identical"]}))
    for row in ws.iter_rows(min_row=2):
        for c in row:
            c.alignment = Alignment(wrap_text=True, vertical="top")

    used = set()
    for pr in run.pages:
        for vr in pr.visuals:
            if vr.diff is None or vr.status == STATUS_MATCH and len(vr.diff.frame) > 5000:
                continue
            name = _slug(f"{pr.title[:12]}_{vr.title[:15]}")[:28]
            base, k = name, 1
            while name in used:
                name, k = f"{base[:25]}_{k}", k + 1
            used.add(name)
            sh = wb.create_sheet(name)
            sh.cell(1, 1, f"{pr.title} - {vr.title}  [{vr.status}]").font = Font(bold=True, size=12)
            f = vr.diff.frame
            _write_frame(sh, f, start_row=3)
            cells = mismatched_cells(vr.diff, run.settings)
            cols = list(f.columns)
            for i in range(len(f)):
                st = f.iat[i, 0]
                fill = _FILL.get(st)
                for j, c in enumerate(cols):
                    cell = sh.cell(i + 4, j + 1)
                    if fill:
                        cell.fill = fill
                    if cells.iat[i, j]:
                        cell.font = _BAD

    if not run.settings.include_sql_in_report:
        buf = io.BytesIO()
        wb.save(buf)
        return buf.getvalue()
    ws = wb.create_sheet("SQL")
    _write_frame(ws, pd.DataFrame([{
        "Page": pr.title, "Visual": vr.title,
        "Prod SQL": vr.prod.sql.sql if vr.prod.sql else "",
        "Test SQL": vr.test.sql.sql if vr.test.sql else "",
        "Fully translated": vr.prod.sql.fully_translated if vr.prod.sql else None}
        for pr in run.pages for vr in pr.visuals if vr.prod.sql or vr.test.sql]
        or [{"Page": "", "Visual": "", "Prod SQL": "", "Test SQL": "", "Fully translated": None}]))
    for col in ("C", "D"):
        ws.column_dimensions[col].width = 90
    buf = io.BytesIO()
    wb.save(buf)
    return buf.getvalue()


def build_zip(run: RunResult) -> bytes:
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w", zipfile.ZIP_DEFLATED) as z:
        z.writestr("comparison_report.html", build_html(run))
        z.writestr("comparison_details.xlsx", build_excel(run))
        for pi, pr in enumerate(run.pages):
            pslug = f"{pi:02d}_{_slug(pr.title)}"
            for lab, img in (("prod", pr.prod_annotated), ("test", pr.test_annotated)):
                if img:
                    z.writestr(f"screenshots/{pslug}/_page_{lab}.png", img)
            for vi, vr in enumerate(pr.visuals):
                vslug = f"{vi:02d}_{_slug(vr.title)}"
                for lab, sv in (("prod", vr.prod), ("test", vr.test)):
                    if sv.screenshot:
                        z.writestr(f"screenshots/{pslug}/{vslug}_{lab}.png", sv.screenshot)
                    if sv.sql and run.settings.include_sql_in_report:
                        z.writestr(f"sql/{pslug}/{vslug}_{lab}.sql", sv.sql.sql)
                    if sv.dax and run.settings.include_dax_in_report:
                        z.writestr(f"dax/{pslug}/{vslug}_{lab}.dax", sv.dax.dax)
    return buf.getvalue()
