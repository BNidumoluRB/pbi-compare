"""Thin Power BI + Fabric REST client used by the app."""
from __future__ import annotations

import base64
import time
from typing import Any, Dict, List, Optional

import requests

from .auth import FABRIC_SCOPE, PBI_SCOPE, TokenProvider

PBI = "https://api.powerbi.com/v1.0/myorg"
FABRIC = "https://api.fabric.microsoft.com/v1"


class PbiApiError(RuntimeError):
    def __init__(self, message: str, status: int = 0, body: str = ""):
        super().__init__(message)
        self.status = status
        self.body = body


class PowerBIClient:
    def __init__(self, tokens: TokenProvider, timeout: int = 120):
        self.tokens = tokens
        self.timeout = timeout
        self.session = requests.Session()

    # ------------------------------------------------------------------ core
    def _request(self, method: str, url: str, scopes=PBI_SCOPE, retries: int = 5,
                 raw: bool = False, **kw) -> Any:
        for attempt in range(retries):
            headers = kw.pop("headers", {}) or {}
            headers["Authorization"] = f"Bearer {self.tokens(scopes)}"
            resp = self.session.request(method, url, headers=headers, timeout=self.timeout, **kw)
            if resp.status_code == 429 or resp.status_code >= 500:
                wait = int(resp.headers.get("Retry-After", 2 ** attempt * 3))
                time.sleep(min(wait, 60))
                kw["headers"] = {}
                continue
            if resp.status_code >= 400:
                raise PbiApiError(_explain(resp), resp.status_code, resp.text[:2000])
            if raw:
                return resp
            if not resp.content:
                return {}
            try:
                return resp.json()
            except ValueError:
                return resp
        raise PbiApiError(f"Gave up after {retries} retries: {method} {url}", 429)

    def _get(self, path: str, **kw):
        return self._request("GET", f"{PBI}{path}", **kw)

    def _post(self, path: str, **kw):
        return self._request("POST", f"{PBI}{path}", **kw)

    # -------------------------------------------------------------- browse
    def workspaces(self) -> List[dict]:
        return self._get("/groups?$top=5000").get("value", [])

    def reports(self, ws: str) -> List[dict]:
        return self._get(f"/groups/{ws}/reports").get("value", [])

    def pages(self, ws: str, report: str) -> List[dict]:
        pages = self._get(f"/groups/{ws}/reports/{report}/pages").get("value", [])
        return sorted(pages, key=lambda p: p.get("order", 0))

    def datasets(self, ws: str) -> List[dict]:
        return self._get(f"/groups/{ws}/datasets").get("value", [])

    def dataset_workspace(self, report: dict, report_ws: str,
                          candidates: Optional[List[dict]] = None) -> Optional[str]:
        """Workspace that holds the report's semantic model.

        Thin reports often live in a different workspace from their model. The
        reports API returns ``datasetWorkspaceId``; when it doesn't, look for the
        dataset in the report's own workspace and then in every workspace the
        service principal can see.
        """
        if report.get("datasetWorkspaceId"):
            return report["datasetWorkspaceId"]
        ds = report.get("datasetId")
        if not ds:
            return None
        order = [report_ws] + [w["id"] for w in (candidates or []) if w["id"] != report_ws]
        for ws in order:
            try:
                if any(d["id"] == ds for d in self.datasets(ws)):
                    return ws
            except PbiApiError:
                continue
        return None

    def dataset(self, ws: str, dataset: str) -> dict:
        return self._get(f"/groups/{ws}/datasets/{dataset}")

    def last_refresh(self, ws: str, dataset: str) -> Optional[dict]:
        try:
            v = self._get(f"/groups/{ws}/datasets/{dataset}/refreshes?$top=1").get("value", [])
            return v[0] if v else None
        except PbiApiError:
            return None  # DirectQuery / live models have no refresh history

    # ---------------------------------------------------------------- query
    def execute_dax(self, ws: str, dataset: str, dax: str) -> List[dict]:
        body = {"queries": [{"query": dax}], "serializerSettings": {"includeNulls": True}}
        res = self._post(f"/groups/{ws}/datasets/{dataset}/executeQueries", json=body)
        result = res.get("results", [{}])[0]
        if "error" in result:
            raise PbiApiError(f"DAX error: {result['error']}")
        tables = result.get("tables", [])
        if tables and "error" in tables[0]:
            raise PbiApiError(f"DAX error: {tables[0]['error']}")
        return tables[0].get("rows", []) if tables else []

    # --------------------------------------------------------- definitions
    def _fabric_lro(self, url: str) -> dict:
        """POST a Fabric getDefinition call and follow the long-running op."""
        resp = self._request("POST", url, scopes=FABRIC_SCOPE, raw=True)
        if resp.status_code == 200:
            return resp.json()
        loc = resp.headers.get("Location")
        if not loc:
            raise PbiApiError("Fabric getDefinition returned no Location header")
        for _ in range(120):
            time.sleep(int(resp.headers.get("Retry-After", 2)))
            resp = self._request("GET", loc, scopes=FABRIC_SCOPE, raw=True)
            status = (resp.json() or {}).get("status", "")
            if status == "Succeeded":
                return self._request("GET", f"{loc}/result", scopes=FABRIC_SCOPE)
            if status in ("Failed", "Cancelled"):
                raise PbiApiError(f"Fabric operation {status}: {resp.text[:500]}")
        raise PbiApiError("Fabric getDefinition timed out")

    @staticmethod
    def _decode_parts(defn: dict) -> Dict[str, str]:
        parts = {}
        for p in defn.get("definition", {}).get("parts", []):
            if p.get("payloadType", "InlineBase64") == "InlineBase64":
                parts[p["path"]] = base64.b64decode(p["payload"]).decode("utf-8", "replace")
        return parts

    def report_definition(self, ws: str, report: str) -> Dict[str, str]:
        """Returns {path: text}. PBIR (definition/...) or legacy (report.json)."""
        return self._decode_parts(
            self._fabric_lro(f"{FABRIC}/workspaces/{ws}/reports/{report}/getDefinition"))

    def model_definition(self, ws: str, dataset: str) -> Dict[str, str]:
        """Returns TMDL parts (definition/tables/*.tmdl ...) or model.bim."""
        try:
            return self._decode_parts(self._fabric_lro(
                f"{FABRIC}/workspaces/{ws}/semanticModels/{dataset}/getDefinition?format=TMDL"))
        except PbiApiError:
            return self._decode_parts(self._fabric_lro(
                f"{FABRIC}/workspaces/{ws}/semanticModels/{dataset}/getDefinition"))

    def export_pbix(self, ws: str, report: str) -> bytes:
        resp = self._request("GET", f"{PBI}/groups/{ws}/reports/{report}/Export?downloadType=LiveConnect",
                             raw=True)
        return resp.content

    # ------------------------------------------------------- screenshots
    def generate_embed_token(self, ws: str, report: str, dataset: str) -> str:
        body = {"reports": [{"id": report}], "datasets": [{"id": dataset}],
                "targetWorkspaces": []}
        try:
            return self._post("/GenerateToken", json=body)["token"]
        except PbiApiError:
            return self._post(f"/groups/{ws}/reports/{report}/GenerateToken",
                              json={"accessLevel": "View"})["token"]

    def export_png(self, ws: str, report: str, page: str, visual: Optional[str] = None,
                   report_filters: Optional[List[str]] = None, timeout_s: int = 600,
                   bookmark: Optional[str] = None) -> bytes:
        """Export-To-File API (needs Premium / Fabric / PPU capacity)."""
        page_cfg: Dict[str, Any] = {"pageName": page}
        if visual:
            page_cfg["visualName"] = visual
        if bookmark:
            page_cfg["bookmark"] = {"name": bookmark}
        cfg: Dict[str, Any] = {"pages": [page_cfg]}
        if report_filters:
            cfg["reportLevelFilters"] = [{"filter": f} for f in report_filters]
        job = self._post(f"/groups/{ws}/reports/{report}/ExportTo",
                         json={"format": "PNG", "powerBIReportConfiguration": cfg})
        export_id = job["id"]
        start = time.time()
        while time.time() - start < timeout_s:
            st = self._get(f"/groups/{ws}/reports/{report}/exports/{export_id}")
            if st.get("status") == "Succeeded":
                return self._request("GET", f"{PBI}/groups/{ws}/reports/{report}/exports/"
                                     f"{export_id}/file", raw=True).content
            if st.get("status") == "Failed":
                raise PbiApiError(f"Export failed: {st.get('error')}")
            time.sleep(3)
        raise PbiApiError("Export timed out")


def _explain(resp: requests.Response) -> str:
    hints = {
        401: "Unauthorized - check the client secret and that the tenant setting "
             "'Service principals can use Fabric APIs' includes this app.",
        403: "Forbidden - add the service principal to the workspace (Member/Contributor), "
             "and for DAX queries enable 'Dataset Execute Queries REST API'.",
        404: "Not found - wrong ID or the service principal can't see this item.",
    }
    return f"HTTP {resp.status_code}. {hints.get(resp.status_code, '')} {resp.text[:400]}"
