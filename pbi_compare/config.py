"""Tenant profiles.

Profiles come from Streamlit secrets (``[tenants.<key>]`` blocks). The app is
locked to those tenants. When ``[admin].owner_passcode`` is set, the owner can
unlock the sidebar editor and point the app at another tenant / client for the
current session only (nothing is written back to disk).
"""
from __future__ import annotations

from dataclasses import dataclass, field, asdict
from typing import Dict, Mapping, Optional


@dataclass
class TenantProfile:
    key: str
    display_name: str
    tenant_id: str
    client_id: str
    client_secret: str = field(repr=False, default="")
    # Optional: restrict the workspace pickers to these workspace IDs / names.
    allowed_workspaces: tuple = ()
    # Optional Snowflake connection for executing the generated validation SQL.
    snowflake: Optional[dict] = field(default=None, repr=False)

    @property
    def authority(self) -> str:
        return f"https://login.microsoftonline.com/{self.tenant_id}"

    def is_complete(self) -> bool:
        return bool(self.tenant_id and self.client_id and self.client_secret)

    def public_dict(self) -> dict:
        d = asdict(self)
        d["client_secret"] = "***" if self.client_secret else ""
        if d.get("snowflake"):
            d["snowflake"] = {k: ("***" if "pass" in k or "key" in k else v)
                              for k, v in d["snowflake"].items()}
        return d


def _to_dict(obj) -> dict:
    """Streamlit secrets sections are Mapping-like; normalise to plain dicts."""
    if obj is None:
        return {}
    if isinstance(obj, Mapping):
        return {k: _to_dict(v) if isinstance(v, Mapping) else v for k, v in obj.items()}
    return dict(obj)


def load_profiles(secrets: Mapping) -> Dict[str, TenantProfile]:
    profiles: Dict[str, TenantProfile] = {}
    tenants = _to_dict(secrets.get("tenants", {})) if secrets else {}
    for key, cfg in tenants.items():
        if not isinstance(cfg, dict):
            continue
        profiles[key] = TenantProfile(
            key=key,
            display_name=cfg.get("display_name", key),
            tenant_id=str(cfg.get("tenant_id", "")).strip(),
            client_id=str(cfg.get("client_id", "")).strip(),
            client_secret=str(cfg.get("client_secret", "")).strip(),
            allowed_workspaces=tuple(cfg.get("allowed_workspaces", []) or []),
            snowflake=cfg.get("snowflake") or None,
        )
    return profiles


def owner_passcode(secrets: Mapping) -> str:
    try:
        return str(_to_dict(secrets.get("admin", {})).get("owner_passcode", ""))
    except Exception:  # noqa: BLE001
        return ""


@dataclass
class CompareSettings:
    abs_tolerance: float = 0.0001
    rel_tolerance: float = 0.0
    max_rows: int = 50000
    screenshot_mode: str = "auto"  # auto | export_api | browser | off
    case_sensitive_text: bool = False
    trim_text: bool = True
    generate_sql: bool = True         # build Snowflake validation SQL per visual
    include_sql_in_report: bool = True
    include_dax_in_report: bool = True
