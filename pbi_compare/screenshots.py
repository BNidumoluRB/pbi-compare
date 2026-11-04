"""Screenshots of report pages and individual visuals.

Two engines, picked per workspace when mode = "auto":
  * export_api - Power BI Export-To-File (PNG). Needs Premium / Fabric / PPU.
  * browser    - headless Chromium (Playwright) rendering the embedded report
                 with powerbi-client. Works on shared (Pro) capacity.

Both engines capture the whole page once; each visual is then cropped from
the page image using its layout position, which keeps the number of exports
per page at one. ``per_visual_export`` asks the Export API for each visual
separately instead (slower, pixel-exact).
"""
from __future__ import annotations

import asyncio
import io
import json
import shutil
import subprocess
import sys
import threading
from dataclasses import dataclass, field
from typing import Dict, List, Optional, Sequence, Tuple

from PIL import Image, ImageDraw, ImageFont

from .pbi_api import PowerBIClient
from .report_parser import Page

POWERBI_CLIENT_JS = "https://cdn.jsdelivr.net/npm/powerbi-client@2.23.1/dist/powerbi.min.js"


@dataclass
class PageShot:
    page_name: str
    png: Optional[bytes] = None
    visuals: Dict[str, bytes] = field(default_factory=dict)
    layouts: Dict[str, Tuple[float, float, float, float]] = field(default_factory=dict)
    engine: str = ""
    error: str = ""
    bookmark: str = ""
    hidden: set = field(default_factory=set)   # visuals not visible in this view
    data: Dict[str, str] = field(default_factory=dict)       # visual -> exported CSV
    data_errors: Dict[str, str] = field(default_factory=dict)


# A view = one page, optionally with a bookmark applied (tabs built with buttons).
View = Tuple[Page, Optional[str]]


def view_key(page_name: str, bookmark: Optional[str]) -> str:
    return f"{page_name}::{bookmark or ''}"


# ================================================================ helpers
def crop_visuals(png: bytes, page: Page, layouts: Optional[Dict[str, Tuple]] = None
                 ) -> Dict[str, bytes]:
    """Cut each visual out of a page image (layout coordinates scaled to the image)."""
    img = Image.open(io.BytesIO(png)).convert("RGB")
    sx = img.width / (page.width or img.width)
    sy = img.height / (page.height or img.height)
    out = {}
    for v in page.visuals:
        x, y, w, h = (layouts or {}).get(v.name, (v.x, v.y, v.width, v.height))
        if w <= 2 or h <= 2:
            continue
        box = (max(0, int(x * sx)), max(0, int(y * sy)),
               min(img.width, int((x + w) * sx)), min(img.height, int((y + h) * sy)))
        if box[2] - box[0] < 4 or box[3] - box[1] < 4:
            continue
        buf = io.BytesIO()
        img.crop(box).save(buf, "PNG")
        out[v.name] = buf.getvalue()
    return out


def annotate_page(png: bytes, page: Page, status_by_visual: Dict[str, str],
                  colors: Dict[str, str]) -> bytes:
    """Outline each compared visual on the page image in its status colour."""
    img = Image.open(io.BytesIO(png)).convert("RGB")
    draw = ImageDraw.Draw(img)
    sx = img.width / (page.width or img.width)
    sy = img.height / (page.height or img.height)
    try:
        font = ImageFont.load_default(size=14)
    except TypeError:
        font = ImageFont.load_default()
    for v in page.visuals:
        st = status_by_visual.get(v.name)
        if not st:
            continue
        col = colors.get(st, "#5F6368")
        box = (int(v.x * sx), int(v.y * sy), int((v.x + v.width) * sx), int((v.y + v.height) * sy))
        draw.rectangle(box, outline=col, width=4)
        label = st.upper()
        tw = draw.textlength(label, font=font) + 10
        draw.rectangle((box[0], box[1], box[0] + tw, box[1] + 20), fill=col)
        draw.text((box[0] + 5, box[1] + 3), label, fill="white", font=font)
    buf = io.BytesIO()
    img.save(buf, "PNG")
    return buf.getvalue()


def placeholder_png(text: str, w: int = 640, h: int = 360) -> bytes:
    img = Image.new("RGB", (w, h), "#F1F3F4")
    d = ImageDraw.Draw(img)
    d.rectangle((0, 0, w - 1, h - 1), outline="#BDC1C6", width=2)
    try:
        font = ImageFont.load_default(size=16)
    except TypeError:
        font = ImageFont.load_default()
    y = 20
    for line in _wrap(text, 70):
        d.text((20, y), line, fill="#3C4043", font=font)
        y += 22
    buf = io.BytesIO()
    img.save(buf, "PNG")
    return buf.getvalue()


def wireframe_png(page: Page, label: str, hidden: Optional[set] = None) -> bytes:
    """Demo-mode stand-in for a real screenshot: the page layout as boxes."""
    w, h = int(page.width or 1280), int(page.height or 720)
    img = Image.new("RGB", (w, h), "#FFFFFF")
    d = ImageDraw.Draw(img)
    try:
        font = ImageFont.load_default(size=15)
    except TypeError:
        font = ImageFont.load_default()
    d.text((12, h - 24), f"{label} - {page.display_name} (demo wireframe)", fill="#9AA0A6", font=font)
    for v in page.visuals:
        if v.name in (hidden or set()) or v.is_group:
            continue
        box = (int(v.x), int(v.y), int(v.x + v.width), int(v.y + v.height))
        d.rectangle(box, fill="#F8F9FA", outline="#DADCE0", width=2)
        d.text((box[0] + 8, box[1] + 26), v.label(), fill="#202124", font=font)
        d.text((box[0] + 8, box[1] + 46), v.visual_type, fill="#5F6368", font=font)
    buf = io.BytesIO()
    img.save(buf, "PNG")
    return buf.getvalue()


def _wrap(text: str, n: int) -> List[str]:
    words, lines, cur = text.split(), [], ""
    for w in words:
        if len(cur) + len(w) + 1 > n:
            lines.append(cur)
            cur = w
        else:
            cur = f"{cur} {w}".strip()
    return lines + [cur]


# ============================================================ Export API
def shoot_with_export_api(client: PowerBIClient, ws: str, report_id: str, views: Sequence[View],
                          export_filters: Sequence[str], progress=None,
                          identity: Optional[dict] = None, dataset: str = ""
                          ) -> Dict[str, PageShot]:
    shots: Dict[str, PageShot] = {}
    for pg, bm in views:
        shot = PageShot(pg.name, engine="export_api", bookmark=bm or "")
        try:
            shot.png = client.export_png(ws, report_id, pg.name, None, list(export_filters),
                                         bookmark=bm, identity=identity, dataset=dataset)
            shot.visuals = crop_visuals(shot.png, pg)
        except Exception as exc:  # noqa: BLE001
            shot.error = str(exc)
        shots[view_key(pg.name, bm)] = shot
        if progress:
            progress(pg.display_name)
    return shots


# =============================================================== Browser
_install_lock = threading.Lock()
_installed = False


def _chromium_path() -> Optional[str]:
    for name in ("chromium", "chromium-browser", "google-chrome"):
        p = shutil.which(name)
        if p:
            return p
    return None


def ensure_browser() -> Optional[str]:
    """Return a system Chromium path, or install Playwright's Chromium once."""
    global _installed
    path = _chromium_path()
    if path:
        return path
    with _install_lock:
        if not _installed:
            subprocess.run([sys.executable, "-m", "playwright", "install", "chromium"],
                           check=False, capture_output=True, timeout=600)
            _installed = True
    return None


_EMBED_HTML = """<!doctype html><html><head><meta charset="utf-8">
<style>html,body{{margin:0;padding:0;background:#fff;overflow:hidden}}
#c{{position:absolute;left:0;top:0;width:{w}px;height:{h}px;border:0}}
#c iframe{{border:0}}</style>
<script src="{js}"></script></head><body><div id="c"></div><script>
window.__st = {{rendered: 0, loaded: 0, error: null}};
const models = window['powerbi-client'].models;
const cfg = {{
  type: 'report', id: {rid}, embedUrl: {url}, accessToken: {tok}, tokenType: {ttype},
  pageName: {page},
  settings: {{
    panes: {{filters: {{visible: false}}, pageNavigation: {{visible: false}}}},
    layoutType: models.LayoutType.Custom,
    customLayout: {{displayOption: models.DisplayOption.ActualSize}},
    background: models.BackgroundType.Default
  }}
}};
const report = powerbi.embed(document.getElementById('c'), cfg);
report.on('loaded', () => window.__st.loaded++);
report.on('rendered', () => window.__st.rendered++);
report.on('error', e => {{ window.__st.error = JSON.stringify(e.detail); }});
window.__report = report;
window.__models = models;
</script></body></html>"""

# Applies the user's filter choices to the live report:
#   ov[key] = [values]  -> replace that field's report/page filter or slicer selection
#   ov[key] = null      -> "All": remove the filter / clear the slicer
# key = table|column (or table|hierarchyLevel), lower-case.
_APPLY_CHOICES_JS = r"""async (arg) => {
  const ov = arg.ov, meta = arg.meta, extra = arg.extra;
  window.__targets = window.__targets || {};          // targets learnt on earlier pages
  const keyOf = t => ((t && t.table || '') + '|' + (t && (t.column || t.hierarchyLevel || t.level) || '')).toLowerCase();
  const mk = (t, vals) => ({$schema: "http://powerbi.com/product/schema#basic", target: t,
                            operator: "In", values: vals, filterType: 1});
  const done = new Set();
  const learn = t => { if (t) window.__targets[keyOf(t)] = t; };
  const fix = fs => { const out = []; for (const f of (fs || [])) {
      const k = keyOf(f.target); learn(f.target);
      if (k in ov) { done.add(k); if (ov[k] !== null) out.push(mk(f.target, ov[k])); }
      else out.push(f); } return out; };
  const rep = window.__report;
  try { await rep.setFilters(fix(await rep.getFilters()).concat(extra)); } catch (e) {}
  const page = await rep.getActivePage();
  const vs = await page.getVisuals();
  for (const v of vs) {
    if (!(v.type || '').toLowerCase().includes('slicer')) continue;
    try {
      const st = await v.getSlicerState();
      let nf = fix(st.filters);
      for (const t of (st.targets || [])) {
        learn(t);
        const k = keyOf(t);
        if (k in ov && !(st.filters || []).some(f => keyOf(f.target) === k)) {
          done.add(k); if (ov[k] !== null) nf.push(mk(t, ov[k]));
        }
      }
      await v.setSlicerState({filters: nf});
    } catch (e) {}
  }
  // fields this page has no filter/slicer for: add them as page filters
  const missing = [];
  try {
    const pf = fix(await page.getFilters());
    for (const k of Object.keys(ov)) {
      if (done.has(k) || ov[k] === null) continue;
      const t = window.__targets[k] || meta[k];
      if (t) { pf.push(mk(t, ov[k])); done.add(k); } else missing.push(k);
    }
    await page.setFilters(pf);
  } catch (e) {}
  return missing;
}"""


async def _wait_render(tab, n: int, timeout: int = 90_000) -> None:
    try:
        await tab.wait_for_function(f"window.__st.rendered > {n}", timeout=timeout)
    except Exception:  # noqa: BLE001 - nothing changed, so no new render event
        pass


_EXPORT_DATA_JS = """async (names) => {
  const p = await window.__report.getActivePage();
  const vs = await p.getVisuals();
  const out = {};
  for (const v of vs) {
    if (!names.includes(v.name)) continue;
    try { const r = await v.exportData(window.__models.ExportDataType.Summarized, 100000);
          out[v.name] = {ok: true, data: r.data}; }
    catch (e) { out[v.name] = {ok: false, error: JSON.stringify(e && (e.detail || e.message || e))}; }
  }
  return out;
}"""


async def _shoot_browser_async(embed_url: str, report_id: str, token: str, token_type: int,
                               views: Sequence[View], choices_js: dict, settle_ms: int,
                               export_data: Optional[Dict[str, List[str]]] = None
                               ) -> Dict[str, PageShot]:
    from playwright.async_api import async_playwright

    shots: Dict[str, PageShot] = {}
    exe = ensure_browser()
    async with async_playwright() as pw:
        browser = await pw.chromium.launch(
            executable_path=exe, headless=True,
            args=["--no-sandbox", "--disable-dev-shm-usage", "--disable-gpu"])
        try:
            first = views[0][0]
            ctx = await browser.new_context(viewport={"width": int(first.width),
                                                      "height": int(first.height)},
                                            device_scale_factor=1)
            tab = await ctx.new_page()
            html = _EMBED_HTML.format(w=int(first.width), h=int(first.height), js=POWERBI_CLIENT_JS,
                                      rid=json.dumps(report_id), url=json.dumps(embed_url),
                                      tok=json.dumps(token), ttype=token_type,
                                      page=json.dumps(first.name))
            await tab.set_content(html, wait_until="domcontentloaded")
            await tab.wait_for_function("window.__st.rendered > 0 || window.__st.error",
                                        timeout=180_000)
            err = await tab.evaluate("window.__st.error")
            if err:
                raise RuntimeError(f"Embed error: {err}")
            try:  # remember the saved state so default views can be restored after a bookmark
                await tab.evaluate("async () => { window.__init = "
                                   "await window.__report.bookmarksManager.capture(); }")
            except Exception:  # noqa: BLE001
                pass
            current, dirty = first.name, False
            for pg, bm in views:
                shot = PageShot(pg.name, engine="browser", bookmark=bm or "")
                try:
                    w, h = int(pg.width), int(pg.height)
                    await tab.set_viewport_size({"width": w, "height": h})
                    await tab.evaluate(f"(() => {{const c=document.getElementById('c');"
                                       f"c.style.width='{w}px';c.style.height='{h}px';}})()")
                    n = await tab.evaluate("window.__st.rendered")
                    if bm:
                        await tab.evaluate("b => window.__report.bookmarksManager.apply(b)", bm)
                        await _wait_render(tab, n)
                        dirty = True
                    elif dirty:
                        await tab.evaluate("async p => { if (window.__init) await window.__report"
                                           ".bookmarksManager.applyState(window.__init.state);"
                                           " await window.__report.setPage(p); }", pg.name)
                        await _wait_render(tab, n)
                        dirty = False
                    elif pg.name != current:
                        await tab.evaluate("p => window.__report.setPage(p)", pg.name)
                        await _wait_render(tab, n)
                    current = pg.name
                    n = await tab.evaluate("window.__st.rendered")
                    if choices_js.get("ov") or choices_js.get("extra"):
                        missing = await tab.evaluate(_APPLY_CHOICES_JS, choices_js)
                        if missing:
                            shot.error = ("Screenshot couldn't apply: " + ", ".join(missing)
                                          + " (data and SQL still use the choice).")
                        await _wait_render(tab, n, 60_000)
                    await tab.wait_for_timeout(settle_ms)
                    layouts = await tab.evaluate("""async () => {
                        const p = await window.__report.getActivePage();
                        const vs = await p.getVisuals();
                        return vs.map(v => [v.name, v.layout.x, v.layout.y, v.layout.width,
                                            v.layout.height,
                                            (v.layout.displayState || {}).mode || 0]);
                    }""")
                    shot.layouts = {r[0]: tuple(r[1:5]) for r in layouts}
                    shot.hidden = {r[0] for r in layouts if r[5] == 1}
                    shot.png = await tab.screenshot(clip={"x": 0, "y": 0, "width": w, "height": h})
                    shot.visuals = crop_visuals(shot.png, pg, shot.layouts)
                    for hn in shot.hidden:
                        shot.visuals.pop(hn, None)
                    want = (export_data or {}).get(view_key(pg.name, bm))
                    if want:  # read the numbers straight from the rendered visuals
                        res = await tab.evaluate(_EXPORT_DATA_JS, list(want))
                        for vn, r in (res or {}).items():
                            if r.get("ok"):
                                shot.data[vn] = r.get("data", "")
                            else:
                                shot.data_errors[vn] = r.get("error", "")
                except Exception as exc:  # noqa: BLE001
                    shot.error = str(exc)
                shots[view_key(pg.name, bm)] = shot
        finally:
            await browser.close()
    return shots


def shoot_with_browser(client: PowerBIClient, ws: str, report: dict, views: Sequence[View],
                       choices_js: dict, settle_ms: int = 2500, progress=None,
                       identity: Optional[dict] = None,
                       export_data: Optional[Dict[str, List[str]]] = None) -> Dict[str, PageShot]:
    if client.is_user and not identity:
        # signed-in person: embed with their own Azure AD token ("embed for your organisation")
        token, ttype = client.tokens(), 0
    else:
        # service principal, or a person viewing as another user: embed token with RLS identity
        try:
            token, ttype = client.generate_embed_token(ws, report["id"], report["datasetId"],
                                                       identity), 1
        except Exception:  # noqa: BLE001
            if not client.is_user:
                raise
            token, ttype = client.tokens(), 0   # screenshots then show the signed-in view
    result: Dict[str, PageShot] = {}
    error: List[BaseException] = []

    def run():
        try:
            result.update(asyncio.run(_shoot_browser_async(
                report["embedUrl"], report["id"], token, ttype, views, choices_js, settle_ms,
                export_data)))
        except BaseException as exc:  # noqa: BLE001
            error.append(exc)

    t = threading.Thread(target=run, daemon=True)
    t.start()
    t.join(timeout=60 * 45)
    if error:
        raise error[0]
    if progress:  # Streamlit widgets can only be touched from the script thread
        for pg, _ in views:
            progress(pg.display_name)
    return result
