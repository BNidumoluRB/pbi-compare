"""'View data as' for models with row-level security (RLS).

The owner configures the table that lists users (default RAM_PS_RB_BU_SBU_RUS)
and its user column (default OPRID). The app lists those users; picking one
applies RLS as that user, picking "Admin" skips RLS:

* Service principal - Power BI needs an effective identity for RLS models, so
  Admin = an unrestricted role (one with no row filters); a user = that user's
  identity with the model's dynamic roles.
* Signed-in person (SSO) - workspace Members/Contributors aren't restricted by
  RLS, so Admin = no identity at all; a user = DAX impersonation
  (impersonatedUserName) plus an RLS embed identity for the screenshots.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from typing import Callable, Dict, List, Optional, Tuple

from .dax_builder import cq, sort_values, tq
from .model import SemanticModel

ADMIN = "Admin (no RLS)"


@dataclass
class UserDirectory:
    users: List[str] = field(default_factory=list)       # OPRID values
    identity: Dict[str, str] = field(default_factory=dict)  # OPRID -> value RLS compares with
    source: str = ""                                      # dax | snowflake | typed
    error: str = ""


@dataclass
class SideAccess:
    identity: Optional[dict]          # embed / export identity {"username", "roles"}
    impersonate: Optional[str]        # executeQueries impersonatedUserName
    note: str = ""


def _ident_col(model: SemanticModel, table: str, column: str) -> Optional[str]:
    hit = model.identity_column()
    if hit and hit[0].lower() == table.lower() and hit[1].lower() != column.lower():
        return hit[1]
    return None


def list_users(model: SemanticModel, table: str, column: str,
               run_dax: Optional[Callable[[str], List[dict]]] = None,
               run_sql: Optional[Callable[[str], "object"]] = None,
               sql_object: str = "") -> UserDirectory:
    """OPRIDs from the model (DAX) or, when DAX is refused, from Snowflake."""
    other = _ident_col(model, table, column)
    errors = []
    if run_dax is not None:
        cols = [f"{tq(table)}{cq(column)}"] + ([f"{tq(table)}{cq(other)}"] if other else [])
        dax = "EVALUATE\nSUMMARIZECOLUMNS(\n    " + ",\n    ".join(cols) + "\n)\nORDER BY " + cols[0]
        try:
            rows = run_dax(dax)
            d = UserDirectory(source="dax")
            for r in rows:
                vals = list(r.values())
                if vals and vals[0] not in (None, ""):
                    d.users.append(str(vals[0]))
                    if other and len(vals) > 1 and vals[1]:
                        d.identity[str(vals[0])] = str(vals[1])
            d.users = sort_values(list(dict.fromkeys(d.users)))
            return d
        except Exception as exc:  # noqa: BLE001
            errors.append(f"DAX: {str(exc)[:200]}")
    if run_sql is not None and sql_object:
        sel = column.upper() + (f", {other.upper()}" if other else "")
        try:
            df = run_sql(f"SELECT DISTINCT {sel} FROM {sql_object} ORDER BY 1")
            d = UserDirectory(source="snowflake")
            for row in df.itertuples(index=False):
                if row[0] not in (None, ""):
                    d.users.append(str(row[0]))
                    if other and len(row) > 1 and row[1]:
                        d.identity[str(row[0])] = str(row[1])
            d.users = list(dict.fromkeys(d.users))
            return d
        except Exception as exc:  # noqa: BLE001
            errors.append(f"Snowflake: {str(exc)[:200]}")
    return UserDirectory(source="typed", error=" | ".join(errors))


def access_for(model: SemanticModel, choice: str, directory: UserDirectory,
               signed_in_user: bool, advanced: Optional[dict] = None) -> SideAccess:
    """Identity / impersonation for one environment given the 'View data as' choice."""
    if not model.has_rls:
        return SideAccess(None, None)
    adv = advanced or {}
    if choice == ADMIN:
        if signed_in_user:
            return SideAccess(None, None, "Admin: your own access (workspace Members and "
                                          "Contributors aren't restricted by RLS; Viewers are).")
        full = adv.get("roles") or model.full_access_roles()
        if full:
            return SideAccess({"username": adv.get("username") or "pbi-compare", "roles": full},
                              None, f"Admin: unrestricted role {', '.join(full)}.")
        return SideAccess({"username": adv.get("username") or "pbi-compare",
                           "roles": model.roles}, None,
                          "Admin: this model has no role without row filters, so RLS can't be "
                          "skipped for the app's service principal - all roles are applied. Sign "
                          "in with a Member account, or add an unrestricted 'Admin' role.")
    username = adv.get("username") or directory.identity.get(choice, choice)
    roles = adv.get("roles") or model.dynamic_roles() or model.roles
    return SideAccess({"username": username, "roles": roles},
                      username if signed_in_user else None,
                      f"Viewing as {choice}" + (f" ({username})" if username != choice else "")
                      + f" with role(s) {', '.join(roles)}.")


def describe_identity(model: SemanticModel, table: str, column: str) -> Tuple[str, str]:
    """Human-readable summary of how the model's RLS identifies users."""
    hit = model.identity_column()
    if not hit:
        return ("", f"Couldn't find USERPRINCIPALNAME()/USERNAME() in the role filters; the "
                    f"selected {column} is used as the RLS username.")
    t, c, f = hit
    if t.lower() == table.lower() and c.lower() != column.lower():
        return (c, f"RLS matches {f}() against {t}[{c}]; each {column} is translated to its {c}.")
    return (c, f"RLS matches {f}() against {t}[{c}].")
