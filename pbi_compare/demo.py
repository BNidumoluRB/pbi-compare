"""Offline demo: lets anyone try the full UI flow without a tenant.

Uses the bundled sample report/model definitions and a fake DAX engine that
produces deterministic numbers, with a few deliberate Test-side differences so
the highlighting, screenshots and report output can be seen end to end.
"""
from __future__ import annotations

import hashlib
import re
from typing import Dict, List

from .model import model_from_tmdl
from .report_parser import parse_report_definition
from .runner import Side

DEMO_VALUES = {
    "Region": ["East", "North", "South", "West"],
    "Customer Name": ["Adventure Co", "Contoso", "Fabrikam", "Northwind", "Test Customer"],
    "Year": [2023, 2024, 2025],
    "Month": ["January", "February", "March", "April", "May", "June", "July", "August",
              "September", "October", "November", "December"],
}


def _num(*parts) -> float:
    h = int(hashlib.md5("|".join(map(str, parts)).encode()).hexdigest()[:8], 16)
    return round(1000 + h % 90000 / 3.7, 2)


def fake_dax_runner(env: str):
    def run(dax: str) -> List[dict]:
        sc = dax.split("SUMMARIZECOLUMNS(", 1)[-1]
        groups = re.findall(r"^\s+'([^']+)'\[([^\]]+)\](?:\.\[([^\]]+)\])?,?\s*$", sc, re.M)
        measures = re.findall(r'^\s+"([^"]+)",\s*(.+?),?\s*$', sc, re.M)
        measures = [(n, e) for n, e in measures if not n.startswith("__mf")]
        excluded = set(re.findall(r'NOT\([^)]*IN \{"([^"]+)"', dax))
        included = re.findall(r'TREATAS\(\{([^}]*)\}', dax)
        keep = None
        if included:
            keep = set(re.findall(r'"([^"]+)"', included[0]))
        combos = [{}]
        for t, c, lvl in groups:
            vals = DEMO_VALUES.get(lvl or c, [f"{c} {i}" for i in range(1, 4)])
            if c == "Region" and keep:
                vals = [v for v in vals if v in keep]
            vals = [v for v in vals if v not in excluded]
            key = f"{t}[{c}]" + (f".[{lvl}]" if lvl else "")
            combos = [dict(cmb, **{key: v}) for cmb in combos for v in vals]
        # filters change the numbers (identically in both environments)
        fsig = "|".join(sorted(l.strip() for l in sc.splitlines()
                               if l.strip().startswith(("TREATAS", "FILTER"))))
        rows = []
        for cmb in combos:
            row = dict(cmb)
            for name, _expr in measures:
                v = _num(name, fsig, *cmb.values())
                if env == "test":
                    if cmb.get("Customer[Region]") == "East" and name == "Total Sales":
                        v = round(v * 1.031, 2)
                    if name == "Avg Order Value":
                        v = round(v + 0.5, 2)
                row[f"[{name}]"] = v
            if env == "test" and measures and cmb.get("Customer[Region]") == "West" and not groups[1:]:
                continue  # a row that disappears in Test
            rows.append(row)
        return rows
    return run


def demo_sides() -> Dict[str, Side]:
    from .samples import pbir_parts, tmdl_parts  # bundled sample definitions

    sides = {}
    for env, label in (("prod", "Production"), ("test", "Test")):
        rd = parse_report_definition(pbir_parts())
        model = model_from_tmdl(tmdl_parts(env))
        sides[env] = Side(
            label, None,
            {"id": f"demo-ws-{env}", "name": f"Sales Analytics [{label}] (demo)",
             "isOnDedicatedCapacity": env == "prod"},
            {"id": f"demo-report-{env}", "name": "Sales Overview", "datasetId": f"demo-ds-{env}"},
            rd, model, [], dax_runner=fake_dax_runner(env),
            last_refresh={"endTime": "2026-11-04T06:00:00Z" if env == "prod" else
                          "2026-11-03T23:30:00Z", "status": "Completed"})
    return sides
