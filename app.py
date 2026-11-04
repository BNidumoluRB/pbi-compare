"""Power BI Prod vs Test - visual-by-visual comparison (Streamlit).

Run locally:   streamlit run app.py
Deploy:        Streamlit Community Cloud -> this repo -> app.py (see README.md)
"""
from __future__ import annotations

import dataclasses
from typing import Callable, Dict, List, Optional

import pandas as pd
import streamlit as st
import streamlit.components.v1 as components

from pbi_compare import __version__
from pbi_compare.auth import AuthError, TokenProvider
from pbi_compare.compare import (STATUS_COLORS, STATUS_DIFF, STATUS_ERROR, STATUS_HIDDEN,
                                 STATUS_MATCH, diff_frames, style_diff)
from pbi_compare.config import CompareSettings, TenantProfile, load_profiles, owner_passcode
from pbi_compare.dax_builder import UserFilter, fetch_values, filter_tables
from pbi_compare.model import SemanticModel, diff_models, model_from_definition, model_from_info
from pbi_compare.pbi_api import PbiApiError, PowerBIClient
from pbi_compare.report_builder import build_excel, build_html, build_zip
from pbi_compare.report_parser import FilterField, ReportDef, parse_pbix, parse_report_definition
from pbi_compare.runner import FilterChoice, RunResult, Side, run_comparison

st.set_page_config(page_title="Power BI Prod vs Test Compare", page_icon="📊", layout="wide")

st.markdown("""
<style>
.block-container {padding-top: 1.6rem;}
.pill {display:inline-block;padding:2px 10px;border-radius:12px;color:#fff;font-size:12px;font-weight:600}
.kpi {border:1px solid rgba(128,128,128,.3);border-top:4px solid;border-radius:8px;padding:8px 14px}
.kpi .n {font-size:26px;font-weight:700} .kpi .l {font-size:12px;opacity:.75}
.side-h {font-weight:700;font-size:1.05rem;margin-bottom:.2rem}
.fsrc {font-size:12px;opacity:.7}
</style>""", unsafe_allow_html=True)

DEMO = "__demo__"
SS = st.session_state
for k, v in {"overrides": {}, "user_filters": [], "loaded": {}, "run": None,
             "table_map": {"prod": {}, "test": {}}, "owner_unlocked": False,
             "value_cache": {}}.items():
    SS.setdefault(k, v)


def pill(status: str) -> str:
    return f'<span class="pill" style="background:{STATUS_COLORS.get(status, "#5F6368")}">{status}</span>'


def _secrets() -> dict:
    try:
        return st.secrets.to_dict()
    except Exception:  # noqa: BLE001 - no secrets.toml locally
        return {}


# ============================================== optional viewer passcode
# When the app is shared through a public github.io link, set [admin].viewer_passcode
# so only people you give the passcode to can use the tenant's service principal.
_viewer_code = str((_secrets().get("admin", {}) or {}).get("viewer_passcode", ""))
if _viewer_code and not SS.get("viewer_ok"):
    st.title("Power BI report comparison")
    code = st.text_input("Access code", type="password",
                         help="Ask the app owner for the access code.")
    if st.button("Enter", type="primary"):
        if code == _viewer_code:
            SS.viewer_ok = True
            st.rerun()
        st.error("That code isn't right.")
    st.stop()


# ======================================================== sidebar: tenant
with st.sidebar:
    st.markdown(f"### 📊 PBI Prod ↔ Test\n<small>v{__version__}</small>", unsafe_allow_html=True)
    profiles: Dict[str, TenantProfile] = load_profiles(_secrets())
    profiles.update(SS.overrides)
    options = list(profiles) + [DEMO]
    labels = {k: profiles[k].display_name for k in profiles}
    labels[DEMO] = "Demo (sample report, no tenant)"
    default = 0 if profiles else len(options) - 1
    tenant_key = st.selectbox("Tenant", options, index=default, format_func=lambda k: labels[k],
                              help="Tenants come from the app's secrets. The app only talks to "
                                   "the tenant selected here.")
    demo = tenant_key == DEMO
    profile: Optional[TenantProfile] = None if demo else profiles[tenant_key]

    passcode = owner_passcode(_secrets())
    with st.expander("🔐 Owner settings", expanded=not profiles):
        if passcode and not SS.owner_unlocked:
            code = st.text_input("Owner passcode", type="password")
            if st.button("Unlock") and code == passcode:
                SS.owner_unlocked = True
                st.rerun()
            st.caption("Owners can change tenant / client details for this session.")
        else:
            if not passcode:
                st.caption("No owner passcode set in secrets - anyone using the app can edit "
                           "these. Set `[admin] owner_passcode` before sharing the app.")
            base = profile or TenantProfile("custom", "Custom tenant", "", "", "")
            with st.form("owner_form"):
                name = st.text_input("Display name", base.display_name)
                tid = st.text_input("Tenant ID", base.tenant_id)
                cid = st.text_input("Client (app) ID", base.client_id)
                sec = st.text_input("Client secret", type="password",
                                    placeholder="leave blank to keep current" if base.client_secret else "")
                ws_allow = st.text_area("Allowed workspaces (optional, one ID or name per line)",
                                        "\n".join(base.allowed_workspaces))
                if st.form_submit_button("Apply for this session"):
                    key = base.key if not demo else "custom"
                    SS.overrides[key] = TenantProfile(
                        key, name, tid.strip(), cid.strip(), sec.strip() or base.client_secret,
                        tuple(x.strip() for x in ws_allow.splitlines() if x.strip()),
                        base.snowflake)
                    for k in ("client", "client_key", "ws_cache", "rep_cache"):
                        SS.pop(k, None)
                    SS.loaded, SS.value_cache = {}, {}
                    st.success("Applied. Select the tenant above if it isn't already.")
                    st.rerun()

    st.divider()
    st.markdown("**Comparison settings**")
    settings = CompareSettings(
        abs_tolerance=st.number_input("Absolute tolerance", 0.0, value=0.0001, format="%.6f"),
        rel_tolerance=st.number_input("Relative tolerance (0.001 = 0.1%)", 0.0, 1.0, 0.0,
                                      format="%.5f"),
        case_sensitive_text=st.toggle("Case-sensitive text keys", False),
        trim_text=st.toggle("Trim spaces in text keys", True),
        screenshot_mode=st.selectbox(
            "Screenshot engine", ["auto", "export_api", "browser"],
            format_func={"auto": "Auto (best per workspace)",
                         "export_api": "Export API (Premium / Fabric / PPU)",
                         "browser": "Headless browser (any capacity)"}.get,
            help="Screenshots are always captured. Auto uses the Export API on capacity "
                 "workspaces and the browser otherwise - and always the browser when you change "
                 "slicer/filter selections, because only it can replace them."),
        max_rows=st.number_input("Max rows per visual", 1000, 100000, 50000, step=1000),
        generate_sql=st.toggle("Generate Snowflake validation SQL", True),
    )


# ======================================================= client helpers
def get_client() -> Optional[PowerBIClient]:
    if demo:
        return None
    key = (profile.tenant_id, profile.client_id)
    if SS.get("client_key") != key:
        SS.client = PowerBIClient(TokenProvider(profile))
        SS.client_key = key
        SS.ws_cache, SS.rep_cache = None, {}
    return SS.client


def workspaces() -> List[dict]:
    if demo:
        if "demo_sides" not in SS:
            from pbi_compare.demo import demo_sides
            SS.demo_sides = demo_sides()
        return [SS.demo_sides["prod"].workspace, SS.demo_sides["test"].workspace]
    if SS.get("ws_cache") is None:
        ws = get_client().workspaces()
        allow = set(profile.allowed_workspaces)
        if allow:
            ws = [w for w in ws if w["id"] in allow or w["name"] in allow]
        SS.ws_cache = sorted(ws, key=lambda w: w["name"].lower())
    return SS.ws_cache


def _demo_env(ws_id: str) -> str:
    return "prod" if ws_id.endswith("prod") else "test"


def reports(ws_id: str) -> List[dict]:
    if demo:
        return [SS.demo_sides[_demo_env(ws_id)].report]
    if ws_id not in SS.rep_cache:
        SS.rep_cache[ws_id] = sorted(get_client().reports(ws_id), key=lambda r: r["name"].lower())
    return SS.rep_cache[ws_id]


def pages(ws_id: str, report_id: str) -> List[dict]:
    if demo:
        return [{"name": p.name, "displayName": p.display_name}
                for p in SS.demo_sides[_demo_env(ws_id)].report_def.pages]
    key = ("pages", ws_id, report_id)
    if key not in SS:
        SS[key] = get_client().pages(ws_id, report_id)
    return SS[key]


def model_workspace(ws: dict, rep: dict) -> Optional[str]:
    """Workspace holding the report's semantic model (may differ from the report's)."""
    if demo:
        return ws["id"]
    key = ("model_ws", rep["id"])
    if key not in SS:
        SS[key] = get_client().dataset_workspace(rep, ws["id"], workspaces())
    return SS[key]


def dax_runner(env: str) -> Callable[[str], List[dict]]:
    if demo:
        return SS.demo_sides[env].dax_runner
    s = sel[env]
    return lambda dax: get_client().execute_dax(s["model_ws"]["id"], s["rep"]["datasetId"], dax)


def load_definitions(env: str, ws: dict, rep: dict, model_ws: dict) -> Dict:
    """Report layout + semantic model for one side (cached per report)."""
    key = (env, ws["id"], rep["id"])
    if key in SS.loaded:
        return SS.loaded[key]
    if demo:
        side = SS.demo_sides[env]
        out = {"report_def": side.report_def, "model": side.model, "report_src": "sample PBIR",
               "refresh": side.last_refresh}
        SS.loaded[key] = out
        return out
    c = get_client()
    try:
        rd = parse_report_definition(c.report_definition(ws["id"], rep["id"]))
        src = f"Fabric getDefinition ({'PBIR' if rd.format == 'pbir' else 'legacy layout'})"
    except Exception as exc:  # noqa: BLE001 - fall back to downloading the .pbix
        rd = parse_pbix(c.export_pbix(ws["id"], rep["id"]))
        src = f".pbix export (getDefinition unavailable: {str(exc)[:120]})"
    mws, ds = model_ws["id"], rep["datasetId"]
    try:
        model = model_from_definition(c.model_definition(mws, ds))
    except Exception as exc:  # noqa: BLE001 - fall back to DAX INFO functions
        model = model_from_info(lambda dax: c.execute_dax(mws, ds, dax))
        model.warnings.insert(0, f"Model definition API unavailable ({str(exc)[:160]}); "
                                 "metadata read with DAX INFO functions.")
    out = {"report_def": rd, "model": model, "report_src": src,
           "refresh": c.last_refresh(mws, ds)}
    SS.loaded[key] = out
    return out


def loaded(env: str) -> Optional[Dict]:
    s = sel.get(env)
    return SS.loaded.get((env, s["ws"]["id"], s["rep"]["id"])) if s else None


# ================================================================ header
st.title("Power BI report comparison · Production vs Test")
st.caption("Pick a workspace, report and pages for each environment, choose any filter or slicer "
           "values (Year, Month, …) and tab views, then compare every visual - with screenshots, "
           "highlighted row differences and optional Snowflake validation SQL.")
if demo:
    st.info("Demo mode: a bundled sample report and model with a few deliberate differences. "
            "Pick a configured tenant in the sidebar to work with real reports.", icon="🧪")
elif not profile.is_complete():
    st.warning("This tenant profile is missing the tenant ID, client ID or secret. "
               "Fill them in under **Owner settings** or in the app secrets.")
    st.stop()

try:
    ws_list = workspaces()
except (AuthError, PbiApiError) as exc:
    st.error(f"Could not list workspaces for this tenant: {exc}")
    st.stop()
ws_by_id = {w["id"]: w for w in ws_list}

tab_sel, tab_model, tab_run, tab_report = st.tabs(
    ["① Select, views & filters", "② Model insights", "③ Compare", "④ Report"])

# ====================================================== ① select + filters
sel: Dict[str, Dict] = {}
with tab_sel:
    cols = st.columns(2, gap="large")
    for col, env, label in ((cols[0], "prod", "Production"), (cols[1], "test", "Test")):
        with col:
            st.markdown(f'<div class="side-h">{"🟢" if env == "prod" else "🟡"} {label}</div>',
                        unsafe_allow_html=True)
            ws_idx = 0 if env == "prod" or len(ws_list) < 2 else 1
            ws = st.selectbox(f"{label} report workspace", ws_list,
                              index=min(ws_idx, len(ws_list) - 1),
                              format_func=lambda w: w["name"], key=f"ws_{env}")
            reps = reports(ws["id"]) if ws else []
            if not reps:
                st.warning("No reports in this workspace that the service principal can see.")
                continue
            rep = st.selectbox(f"{label} report", reps, format_func=lambda r: r["name"],
                               key=f"rep_{env}_{ws['id']}")
            # ---- semantic model may live in another workspace (thin reports)
            detected = model_workspace(ws, rep)
            mws_opts = ws_list if detected in ws_by_id or detected is None else \
                ws_list + [{"id": detected, "name": f"{detected} (no access listed)"}]
            idx = next((i for i, w in enumerate(mws_opts) if w["id"] == (detected or ws["id"])), 0)
            with st.expander("Semantic model location", expanded=detected is None):
                if detected is None:
                    st.warning("Couldn't find this report's model in any workspace the service "
                               "principal can see. Add the app to the model's workspace, or pick "
                               "it below.")
                elif detected != ws["id"]:
                    st.info(f"Model lives in a different workspace: **{ws_by_id.get(detected, {}).get('name', detected)}**")
                model_ws = st.selectbox("Model workspace", mws_opts, index=idx,
                                        format_func=lambda w: w["name"], key=f"mws_{env}_{rep['id']}")
            pgs = pages(ws["id"], rep["id"])
            all_pages = st.toggle("All pages", False, key=f"all_{env}_{rep['id']}")
            chosen = []
            if not all_pages:
                chosen = st.multiselect(f"{label} pages", [p["name"] for p in pgs],
                                        default=[pgs[0]["name"]] if pgs else [],
                                        format_func=lambda n, m={p["name"]: p["displayName"]
                                                                 for p in pgs}: m.get(n, n),
                                        key=f"pg_{env}_{rep['id']}")
            cap = "Premium / Fabric capacity" if ws.get("isOnDedicatedCapacity") else "Shared (Pro)"
            st.caption(f"{len(pgs)} page(s) · capacity: {cap}")
            sel[env] = {"ws": ws, "rep": rep, "pages": chosen, "all": all_pages,
                        "page_list": pgs, "model_ws": model_ws}

    ready_sel = len(sel) == 2 and all(s["all"] or s["pages"] for s in sel.values())
    if len(sel) == 2 and not ready_sel:
        st.warning("Select at least one page on each side, or switch on 'All pages'.")

    # ---- understanding both reports & models is mandatory: done automatically
    if ready_sel and not (loaded("prod") and loaded("test")):
        with st.spinner("Reading both reports and understanding their semantic models…"):
            try:
                for e in ("prod", "test"):
                    load_definitions(e, sel[e]["ws"], sel[e]["rep"], sel[e]["model_ws"])
            except Exception as exc:  # noqa: BLE001
                st.error(f"Couldn't read the report or model: {exc}")

    ready_model = ready_sel and bool(loaded("prod") and loaded("test"))
    views: Dict[str, List[Optional[str]]] = {}
    choices: List[FilterChoice] = []

    if ready_model:
        prd: ReportDef = loaded("prod")["report_def"]
        tsd: ReportDef = loaded("test")["report_def"]
        p_pages = [p for p in prd.pages if sel["prod"]["all"] or p.name in sel["prod"]["pages"]]
        st.divider()
        st.subheader("Views & filters")
        st.caption("Every filter and slicer on the selected pages is listed. Leave a filter "
                   "**As saved** to use the report's own selection, pick **All** to clear it, or "
                   "**Choose** specific values. Choices apply to both reports - to the data, the "
                   "screenshots and the SQL - on **every page being compared** (the pages you "
                   "selected, or all pages), including pages without that slicer.")

        shown_keys: Dict[str, str] = {}
        for page in p_pages:
            fields: List[FilterField] = prd.filter_fields(page)
            bms = prd.page_bookmarks(page)
            with st.container(border=True):
                st.markdown(f"**📄 {page.display_name}**")
                if bms:
                    opts = [""] + [b.name for b in bms]
                    view_labels = {"": "Page as saved"} | {
                        b.name: f"🔖 {b.label}" + (f"  ← button: {', '.join(btns)}"
                                                   if (btns := prd.bookmark_buttons(b)) else "")
                        for b in bms}
                    picked_views = st.multiselect(
                        "Views to compare (tabs / buttons on this page use bookmarks)", opts,
                        default=opts, format_func=lambda o, m=view_labels: m.get(o, o),
                        key=f"views_{page.name}")
                    views[page.name] = [v or None for v in picked_views] or [None]
                    missing = [b.display_name for b in bms if not tsd.find_bookmark(b)]
                    if missing:
                        st.caption(f"Not in the Test report: {', '.join(missing)}")
                else:
                    views[page.name] = [None]
                in_scope = {p.name for p in p_pages}
                nav = [(v.label(), prd.page(v.action_target)) for v in prd.buttons(page)
                       if v.action in ("PageNavigation", "Drillthrough")]
                outside = [f"'{b}' → {t.display_name}" for b, t in nav if t and t.name not in in_scope]
                if outside:
                    st.caption("Buttons here open other pages that aren't selected: "
                               + "; ".join(outside) + ". Add those pages to compare them too.")
                if not fields:
                    st.caption("No filters or slicers on this page.")
                for ff in fields:
                    k = ff.key
                    if k in shown_keys:
                        st.caption(f"↳ {ff.field.friendly}: set above on '{shown_keys[k]}'")
                        continue
                    shown_keys[k] = page.display_name
                    c1, c2, c3 = st.columns([3, 2, 5])
                    group_note = " · same slicer as other levels" if ff.group else ""
                    c1.markdown(f"**{ff.field.label}**  \n<span class='fsrc'>{ff.field.friendly} · "
                                f"{', '.join(dict.fromkeys(ff.sources))}{group_note}</span>",
                                unsafe_allow_html=True)
                    mode = c2.radio("Mode", ["As saved", "All", "Choose"], horizontal=True,
                                    key=f"mode_{k}", label_visibility="collapsed")
                    saved_txt = "; ".join(d for d in filter_tables(ff.saved)[3]) if ff.saved else "nothing selected"
                    if mode == "As saved":
                        c3.caption(f"Saved: {saved_txt}")
                    elif mode == "All":
                        c3.caption("All values (saved selection cleared)")
                        choices.append(FilterChoice(ff.field, None))
                    else:
                        vk = (sel["prod"]["rep"]["id"], k)
                        if vk not in SS.value_cache:
                            try:
                                vals = fetch_values(dax_runner("prod"), ff.field)
                                if k in {x.key for p2 in tsd.pages for x in tsd.filter_fields(p2)}:
                                    extra = [v for v in fetch_values(dax_runner("test"), ff.field)
                                             if v not in vals]
                                    vals = vals + extra
                                SS.value_cache[vk] = vals
                            except Exception as exc:  # noqa: BLE001
                                SS.value_cache[vk] = []
                                c3.error(f"Couldn't read values: {str(exc)[:200]}")
                        picked = c3.multiselect("Values", SS.value_cache[vk], key=f"vals_{k}",
                                                format_func=lambda v: "(Blank)" if v is None else str(v),
                                                label_visibility="collapsed",
                                                placeholder=f"Choose {ff.field.label} values")
                        if picked:
                            choices.append(FilterChoice(ff.field, list(picked)))
                if ff_groups := {ff.group for ff in fields if ff.group}:
                    st.caption("Fields that share one slicer (e.g. Year + Month) are stored as one "
                               "selection: changing one of them replaces the slicer's saved "
                               "selection, so set each level you want to keep.")

        with st.expander("➕ Extra filter on any model column (applied to all pages)"):
            pm: SemanticModel = loaded("prod")["model"]
            tm: SemanticModel = loaded("test")["model"]
            common = sorted(set(pm.tables) & set(tm.tables))
            c1, c2, c3, c4 = st.columns([2, 2, 3, 1])
            tbl = c1.selectbox("Table", common, key="uf_tbl")
            colnames = sorted(set(pm.tables[tbl].columns) & set(tm.tables[tbl].columns)) if tbl else []
            colname = c2.selectbox("Column", colnames, key="uf_col")
            vals = c3.text_input("Values (comma separated)", placeholder="West, East", key="uf_vals")
            dtype = c4.selectbox("Type", ["text", "number", "date"], key="uf_type")
            if st.button("Add filter") and tbl and colname and vals.strip():
                SS.user_filters.append(UserFilter(tbl, colname, [v.strip() for v in vals.split(",")
                                                                 if v.strip()], dtype))
            for i, uf in enumerate(list(SS.user_filters)):
                d1, d2 = st.columns([6, 1])
                d1.code(uf.to_spec().description)
                if d2.button("Remove", key=f"rmf{i}"):
                    SS.user_filters.pop(i)
                    st.rerun()

        n_views = sum(len(v) for v in views.values())
        st.success(f"Ready: {len(p_pages)} page(s), {n_views} view(s), {len(choices)} filter "
                   f"choice(s). Open **③ Compare** to run.")

# ======================================================= ② model insights
with tab_model:
    if not ready_model:
        st.info("Pick both reports and pages first - the reports and models are read "
                "automatically.")
    else:
        if st.button("↻ Re-read reports and models"):
            for e in ("prod", "test"):
                SS.loaded.pop((e, sel[e]["ws"]["id"], sel[e]["rep"]["id"]), None)
            SS.value_cache = {}
            st.rerun()
        defs = {e: loaded(e) for e in ("prod", "test")}
        md = diff_models(defs["prod"]["model"], defs["test"]["model"])
        c1, c2, c3 = st.columns(3)
        c1.metric("Model differences", len(md))
        c2.metric("Prod visuals", sum(len(p.visuals) for p in defs["prod"]["report_def"].pages))
        c3.metric("Test visuals", sum(len(p.visuals) for p in defs["test"]["report_def"].pages))

        env_tab = st.radio("Show", ["Production", "Test", "Prod ↔ Test model diff"],
                           horizontal=True, label_visibility="collapsed")
        if env_tab.startswith("Prod ↔"):
            if md.empty:
                st.success("Models are identical: tables, columns, measures, relationships and sources.")
            else:
                st.dataframe(md, width="stretch", hide_index=True)
                st.caption("Source differences such as PROD_DB vs TEST_DB are expected; "
                           "measure / relationship differences usually explain data mismatches.")
        else:
            env = "prod" if env_tab == "Production" else "test"
            d = defs[env]
            model: SemanticModel = d["model"]
            rd: ReportDef = d["report_def"]
            for w in model.warnings:
                st.warning(w)
            mws_name = sel[env]["model_ws"]["name"]
            st.caption(f"Report read from {d['report_src']} · model in workspace '{mws_name}' read "
                       f"via {model.origin} · last refresh {(d['refresh'] or {}).get('endTime', 'n/a')}")
            fr = model.summary_frames()
            s1, s2, s3, s4 = st.columns(4)
            s1.metric("Tables", len(model.tables))
            s2.metric("Measures", len(fr["measures"]))
            s3.metric("Relationships", len(model.relationships))
            trans = fr["measures"]["SQL translatable"].mean() * 100 if len(fr["measures"]) else 0
            s4.metric("Measures auto-translatable to SQL", f"{trans:.0f}%")
            mt = st.tabs(["Tables & roles", "Relationship diagram", "Measures & lineage",
                          "Columns", "Snowflake mapping", "Report layout", "Bookmarks / tabs",
                          "Visual lineage"])
            with mt[0]:
                st.dataframe(fr["tables"], width="stretch", hide_index=True)
            with mt[1]:
                st.graphviz_chart(model.to_dot(), width="stretch")
                st.caption("Yellow = fact, blue = dimension, green = bridge. Dashed = inactive. "
                           "Arrows show filter direction.")
                st.dataframe(fr["relationships"], width="stretch", hide_index=True)
            with mt[2]:
                st.dataframe(fr["measures"], width="stretch", hide_index=True,
                             column_config={"DAX": st.column_config.TextColumn(width="large")})
            with mt[3]:
                st.dataframe(fr["columns"], width="stretch", hide_index=True)
            with mt[4]:
                st.markdown("Snowflake object behind each table, resolved from Power Query "
                            "(including M parameters). Override any row - the SQL generator "
                            "uses your value.")
                rows = []
                for n, t in model.tables.items():
                    if n.startswith(("LocalDateTable_", "DateTableTemplate_")):
                        continue
                    rows.append({"Table": n, "Detected source": t.source.fqn or
                                 ("native SQL" if t.source.native_query else ""),
                                 "Connector": t.source.connector, "Server": t.source.server,
                                 "Warehouse": t.source.warehouse,
                                 "Override (Snowflake object or (subquery))":
                                     SS.table_map[env].get(n, ""),
                                 "Notes": " ".join(t.source.notes)})
                edited = st.data_editor(pd.DataFrame(rows), hide_index=True,
                                        width="stretch", key=f"map_{env}",
                                        disabled=["Table", "Detected source", "Connector",
                                                  "Server", "Warehouse", "Notes"])
                SS.table_map[env] = {r["Table"]: r["Override (Snowflake object or (subquery))"]
                                     for _, r in edited.iterrows()
                                     if r["Override (Snowflake object or (subquery))"]}
                if model.parameters:
                    with st.expander("Power Query parameters"):
                        st.json(model.parameters)
            with mt[5]:
                lay = []
                for p in rd.pages:
                    for v in p.visuals:
                        lay.append({"Page": p.display_name, "Visual": v.label(),
                                    "Type": v.visual_type, "Data visual": v.is_data_visual,
                                    "Hidden by default": rd.is_hidden(p, v, None),
                                    "Fields": ", ".join(f"{f.role}: {f.friendly}"
                                                        + (f" ({f.agg})" if f.agg else "")
                                                        for f in v.fields),
                                    "Visual filters": len([f for f in v.filters if f.level == "visual"]),
                                    "Slicer selection": any(f.level == "slicer" for f in v.filters),
                                    "Warnings": " ".join(v.warnings)})
                st.dataframe(pd.DataFrame(lay), width="stretch", hide_index=True)
                st.caption(f"Report-level filters: {len(rd.filters)} · format: {rd.format}")
            with mt[6]:
                btn_rows = []
                for p in rd.pages:
                    for v in rd.buttons(p):
                        tgt = (rd.bookmark(v.action_target).label if v.action == "Bookmark"
                               and rd.bookmark(v.action_target) else
                               (rd.page(v.action_target).display_name if rd.page(v.action_target)
                                else v.action_target))
                        btn_rows.append({"Page": p.display_name, "Button": v.label(),
                                         "Action": v.action, "Opens": tgt})
                if btn_rows:
                    st.markdown("**Buttons**")
                    st.dataframe(pd.DataFrame(btn_rows), width="stretch", hide_index=True)
                    st.caption("Bookmark buttons become views you can compare; page-navigation "
                               "buttons are covered by selecting the target pages.")
                if not rd.bookmarks:
                    st.caption("No bookmarks in this report.")
                for b in rd.bookmarks:
                    pg = rd.page(b.page)
                    st.markdown(f"**🔖 {b.label}** → page *{pg.display_name if pg else b.page}*"
                                f"{' · display only (no data state)' if b.suppress_data else ''}")
                    if pg:
                        vis_now = [v.label() for v in pg.visuals if not v.is_group
                                   and not rd.is_hidden(pg, v, b)]
                        st.caption("Visible: " + ", ".join(vis_now))
            with mt[7]:
                vis = [(p, v) for p in rd.pages for v in p.visuals if v.is_data_visual]
                if vis:
                    pv = st.selectbox("Visual", vis, format_func=lambda x: f"{x[0].display_name} › {x[1].label()}")
                    page, v = pv
                    touched = set()
                    lines = []
                    for f in v.fields:
                        touched.add(f.table)
                        if f.kind == "measure":
                            m = model.find_measure(f.name, f.table)
                            if m:
                                dm, dc = model.measure_dependencies(m)
                                touched |= {t for t, _ in dc}
                                lines.append(f"**[{f.name}]** = `{m.expression.strip()[:300]}`  \n"
                                             f"→ columns: {', '.join(sorted(f'{t}[{c}]' for t, c in dc)) or '—'}")
                        else:
                            src = model.source_column_name(f.table, f.name)
                            tb = model.table(f.table)
                            lines.append(f"**{f.friendly}** {f.agg} → "
                                         f"`{(tb.source.fqn if tb else '?')}.{src or '?'}`")
                    st.markdown("\n\n".join(lines))
                    filters = rd.effective_filters(page, v)
                    if filters:
                        st.markdown("**Filters that reach this visual (as saved):** " +
                                    ", ".join(f"{f.level}: {f.description or f.name}" for f in filters))
                    st.graphviz_chart(model.to_dot(highlight=touched), width="stretch")

# ============================================================ ③ compare
with tab_run:
    if not ready_model:
        st.warning("Select both reports and pages first (step ①). Understanding both models is "
                   "required and happens automatically there.")
    else:
        if choices or SS.user_filters:
            st.markdown("**Filter selections:** " + " · ".join(
                [c.description for c in choices] + [u.to_spec().description for u in SS.user_filters]))
        else:
            st.caption("Filters: as saved in each report.")
    run_clicked = st.button("▶ Run comparison", type="primary", disabled=not ready_model)
    if run_clicked:
        sides = {}
        for env, label in (("prod", "Production"), ("test", "Test")):
            d = loaded(env)
            base = SS.demo_sides[env] if demo else None
            mws = sel[env]["model_ws"]
            sides[env] = Side(label, get_client(), sel[env]["ws"], sel[env]["rep"], d["report_def"],
                              d["model"], [] if sel[env]["all"] else sel[env]["pages"],
                              dict(SS.table_map[env]), d["refresh"],
                              model_workspace=mws if mws["id"] != sel[env]["ws"]["id"] else None,
                              dax_runner=base.dax_runner if base else None)
        bar = st.progress(0.0, "Starting…")
        try:
            SS.run = run_comparison(profile.display_name if profile else "Demo", sides["prod"],
                                    sides["test"], settings, SS.user_filters,
                                    lambda f, m: bar.progress(min(1.0, f), m),
                                    views=views, choices=choices)
            for k in ("report_html", "report_key"):
                SS.pop(k, None)
        except Exception as exc:  # noqa: BLE001
            st.error(f"Comparison failed: {exc}")
        bar.empty()

    run: Optional[RunResult] = SS.run
    if run:
        counts = run.counts()
        kcols = st.columns(7)
        for col, s in zip(kcols, [STATUS_MATCH, STATUS_DIFF, "Only in Production", "Only in Test",
                                  STATUS_ERROR, "Not compared", STATUS_HIDDEN]):
            col.markdown(f'<div class="kpi" style="border-top-color:{STATUS_COLORS[s]}">'
                         f'<div class="n">{counts.get(s, 0)}</div><div class="l">{s}</div></div>',
                         unsafe_allow_html=True)
        for line in run.log:
            st.caption(f"📸 {line}")
        only_issues = st.toggle("Show only visuals with differences or errors", False)
        st.dataframe(run.summary_frame(), width="stretch", hide_index=True)
        for pi, pr in enumerate(run.pages):
            st.markdown(f"### {pr.title} &nbsp; {pill(pr.status)}", unsafe_allow_html=True)
            for n in pr.notes:
                st.caption(f"• {n}")
            if pr.prod_annotated or pr.test_annotated:
                c1, c2 = st.columns(2)
                if pr.prod_annotated:
                    c1.image(pr.prod_annotated, caption="Production page", width="stretch")
                if pr.test_annotated:
                    c2.image(pr.test_annotated, caption="Test page", width="stretch")
            for vi, vr in enumerate(pr.visuals):
                if vr.status == STATUS_HIDDEN:
                    continue
                if only_issues and vr.status in (STATUS_MATCH, "Not compared"):
                    continue
                icon = {STATUS_MATCH: "✅", STATUS_DIFF: "❌", STATUS_ERROR: "⚠️"}.get(vr.status, "➖")
                with st.expander(f"{icon} {vr.title} · {vr.visual_type} · {vr.status}",
                                 expanded=vr.status in (STATUS_DIFF, STATUS_ERROR)):
                    c1, c2 = st.columns(2)
                    for c, lab, sv in ((c1, "Production", vr.prod), (c2, "Test", vr.test)):
                        if sv.screenshot:
                            c.image(sv.screenshot, caption=lab, width="stretch")
                        else:
                            c.caption(f"{lab}: no visual")
                    for n in vr.notes:
                        st.caption(f"• {n}")
                    if vr.diff is not None:
                        st.markdown("Rows: " + " · ".join(f"**{k}** {v}" for k, v in vr.diff.counts.items()))
                        st.dataframe(style_diff(vr.diff, run.settings), width="stretch",
                                     hide_index=True)
                    t1, t2, t3 = st.tabs(["DAX", "Snowflake SQL", "Validate in Snowflake"])
                    with t1:
                        for lab, sv in (("Production", vr.prod), ("Test", vr.test)):
                            if sv.dax:
                                st.markdown(f"**{lab}**")
                                st.code(sv.dax.dax, language="sql")
                    with t2:
                        if not run.settings.generate_sql:
                            st.caption("SQL generation was switched off for this run.")
                        for lab, sv in (("Production", vr.prod), ("Test", vr.test)):
                            if sv.sql:
                                st.markdown(f"**{lab}**" + ("" if sv.sql.fully_translated
                                                           else " · ⚠️ needs review"))
                                st.code(sv.sql.sql, language="sql")
                    with t3:
                        sf = profile.snowflake if profile else None
                        if not sf:
                            st.caption("Add a `[tenants.<key>.snowflake]` block to the secrets to run "
                                       "the SQL from here and compare it with the Power BI numbers.")
                        elif vr.prod.sql and vr.prod.frame is not None and \
                                st.button("Run Production SQL in Snowflake", key=f"sf{pi}_{vi}"):
                            from pbi_compare.snowflake_runner import run_sql
                            try:
                                sdf = run_sql(sf, vr.prod.sql.sql)
                                d = diff_frames(vr.prod.frame, sdf, vr.prod.dax.group_labels,
                                                vr.prod.dax.value_labels, run.settings)
                                st.markdown(f"Power BI (Prod) vs Snowflake: {pill(d.status)}",
                                            unsafe_allow_html=True)
                                st.dataframe(style_diff(d, run.settings), width="stretch",
                                             hide_index=True)
                            except Exception as exc:  # noqa: BLE001
                                st.error(str(exc))

# ============================================================= ④ report
with tab_report:
    run = SS.run
    if not run:
        st.info("Run a comparison first.")
    else:
        c1, c2 = st.columns(2)
        inc_sql = c1.toggle("Include SQL queries in the report", run.settings.generate_sql,
                            disabled=not run.settings.generate_sql)
        inc_dax = c2.toggle("Include DAX queries in the report", True)
        rkey = (id(run), inc_sql, inc_dax)
        if SS.get("report_key") != rkey:
            with st.spinner("Building report…"):
                view = dataclasses.replace(run, settings=dataclasses.replace(
                    run.settings, include_sql_in_report=inc_sql, include_dax_in_report=inc_dax))
                SS.report_html = build_html(view)
                SS.report_xlsx = build_excel(view)
                SS.report_zip = build_zip(view)
                SS.report_key = rkey
        stamp = run.started.strftime("%Y%m%d_%H%M")
        c1, c2, c3 = st.columns(3)
        c1.download_button("⬇ HTML report (with screenshots)", SS.report_html,
                           f"pbi_compare_{stamp}.html", "text/html", width="stretch")
        c2.download_button("⬇ Excel workbook (highlighted rows)", SS.report_xlsx,
                           f"pbi_compare_{stamp}.xlsx",
                           "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
                           width="stretch")
        c3.download_button("⬇ Full bundle (.zip: report, screenshots"
                           + (", SQL" if inc_sql else "") + ")", SS.report_zip,
                           f"pbi_compare_{stamp}.zip", "application/zip", width="stretch")
        if st.toggle("Preview the HTML report here", True):
            if hasattr(st, "iframe"):
                st.iframe(SS.report_html, height=900)
            else:  # older Streamlit
                components.html(SS.report_html, height=900, scrolling=True)

st.caption("Data stays inside your tenant: the app calls the Power BI / Fabric REST APIs with the "
           "tenant's service principal and keeps results in this browser session only.")
