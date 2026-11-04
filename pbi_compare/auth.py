"""Service principal (client credentials) authentication with MSAL."""
from __future__ import annotations

import threading
import time
from typing import Dict, Tuple

import msal

from .config import TenantProfile

PBI_SCOPE = ["https://analysis.windows.net/powerbi/api/.default"]
FABRIC_SCOPE = ["https://api.fabric.microsoft.com/.default"]

_apps: Dict[Tuple[str, str], msal.ConfidentialClientApplication] = {}
_lock = threading.Lock()


class AuthError(RuntimeError):
    pass


def _app(profile: TenantProfile) -> msal.ConfidentialClientApplication:
    key = (profile.tenant_id, profile.client_id)
    with _lock:
        app = _apps.get(key)
        if app is None:
            app = msal.ConfidentialClientApplication(
                client_id=profile.client_id,
                client_credential=profile.client_secret,
                authority=profile.authority,
            )
            _apps[key] = app
        return app


def get_token(profile: TenantProfile, scopes=PBI_SCOPE) -> str:
    if not profile.is_complete():
        raise AuthError("Tenant ID, Client ID and Client secret are all required.")
    result = _app(profile).acquire_token_for_client(scopes=scopes)
    if "access_token" not in result:
        raise AuthError(
            f"{result.get('error')}: {result.get('error_description', 'token request failed')}"
        )
    return result["access_token"]


class TokenProvider:
    """Small cache so every API call doesn't round-trip to MSAL."""

    def __init__(self, profile: TenantProfile):
        self.profile = profile
        self._cache: Dict[str, Tuple[str, float]] = {}

    def __call__(self, scopes=PBI_SCOPE) -> str:
        k = scopes[0]
        tok, exp = self._cache.get(k, ("", 0.0))
        if tok and exp - time.time() > 300:
            return tok
        tok = get_token(self.profile, scopes)
        self._cache[k] = (tok, time.time() + 3000)
        return tok


class UserTokenProvider:
    """Delegated sign-in as a person (device code flow) - one per browser session.

    Used when the semantic model has row-level security: Power BI refuses DAX
    queries from service principals on RLS models, but a signed-in workspace
    member can query, export and embed normally. Needs, on the app registration:
    Authentication → "Allow public client flows" = Yes, and delegated Power BI
    Service permissions (Dataset.Read.All, Report.Read.All, Workspace.Read.All).
    """

    is_user = True

    def __init__(self, profile: TenantProfile):
        self.profile = profile
        self.cache = msal.SerializableTokenCache()
        self.app = msal.PublicClientApplication(profile.client_id, authority=profile.authority,
                                                token_cache=self.cache)
        self.flow: Dict = {}
        self.username = ""

    # -------------------------------------------------------- sign-in
    def start(self) -> Dict:
        self.flow = self.app.initiate_device_flow(scopes=PBI_SCOPE)
        if "user_code" not in self.flow:
            raise AuthError(self.flow.get("error_description", "Couldn't start sign-in. Check "
                                          "'Allow public client flows' on the app registration."))
        return self.flow

    def finish(self, wait_seconds: int = 15) -> bool:
        if not self.flow:
            return False
        deadline = time.time() + wait_seconds
        result = self.app.acquire_token_by_device_flow(
            self.flow, exit_condition=lambda flow: time.time() > deadline)
        if "access_token" in result:
            self.username = (result.get("id_token_claims") or {}).get("preferred_username", "")
            if not self.username:
                accts = self.app.get_accounts()
                self.username = accts[0].get("username", "") if accts else "signed in"
            self.flow = {}
            return True
        if result.get("error") not in (None, "authorization_pending"):
            raise AuthError(f"{result.get('error')}: {result.get('error_description', '')}")
        return False

    @property
    def signed_in(self) -> bool:
        return bool(self.app.get_accounts())

    def sign_out(self) -> None:
        for a in self.app.get_accounts():
            self.app.remove_account(a)
        self.username = ""

    # -------------------------------------------------------- tokens
    def __call__(self, scopes=PBI_SCOPE) -> str:
        accts = self.app.get_accounts()
        if not accts:
            raise AuthError("Not signed in - use 'Sign in' in the sidebar.")
        result = self.app.acquire_token_silent(list(scopes), account=accts[0])
        if not result or "access_token" not in result:
            raise AuthError("Sign-in expired or consent missing for "
                            f"{scopes[0]} - sign in again.")
        return result["access_token"]


# Sign-in flows waiting for Microsoft to redirect back. Kept per process (not per
# browser session) because the redirect opens a fresh Streamlit session.
_PENDING: Dict[str, Tuple[Dict, float]] = {}
_PENDING_LOCK = threading.Lock()


class SsoTokenProvider:
    """Microsoft single sign-on (authorization-code flow) - one per browser session.

    The user clicks "Sign in with Microsoft", signs in (usually silently, with the
    session they already have), and is redirected back to the app. Their own
    permissions then apply, so Viewers see data as Power BI shows it to them.
    App registration needs a *Web* redirect URI equal to the app's URL and the
    delegated Power BI permissions listed in the README.
    """

    is_user = True

    def __init__(self, profile: TenantProfile, redirect_uri: str):
        self.profile = profile
        self.redirect_uri = redirect_uri
        self.cache = msal.SerializableTokenCache()
        self.app = msal.ConfidentialClientApplication(
            profile.client_id, client_credential=profile.client_secret,
            authority=profile.authority, token_cache=self.cache)
        self.username = ""

    def login_url(self, tenant_key: str = "") -> str:
        flow = self.app.initiate_auth_code_flow(scopes=PBI_SCOPE, redirect_uri=self.redirect_uri,
                                                prompt="select_account")
        flow["_tenant_key"] = tenant_key
        now = time.time()
        with _PENDING_LOCK:
            for k in [k for k, (_, t) in _PENDING.items() if now - t > 900]:
                _PENDING.pop(k, None)
            _PENDING[flow["state"]] = (flow, now)
        return flow["auth_uri"]

    @staticmethod
    def pending_tenant(state: str) -> str:
        with _PENDING_LOCK:
            hit = _PENDING.get(state)
        return (hit[0].get("_tenant_key", "") if hit else "")

    @staticmethod
    def is_callback(params: Dict) -> bool:
        return bool(params.get("state")) and ("code" in params or "error" in params)

    def complete(self, params: Dict) -> bool:
        if params.get("error"):
            raise AuthError(f"{params.get('error')}: {params.get('error_description', '')}")
        with _PENDING_LOCK:
            pending = _PENDING.pop(params.get("state", ""), None)
        if not pending:
            raise AuthError("This sign-in link has expired or was already used - sign in again.")
        flow = {k: v for k, v in pending[0].items() if k != "_tenant_key"}
        result = self.app.acquire_token_by_auth_code_flow(flow, dict(params))
        if "access_token" not in result:
            raise AuthError(f"{result.get('error')}: {result.get('error_description', '')}")
        claims = result.get("id_token_claims") or {}
        self.username = claims.get("preferred_username") or claims.get("name") or "signed in"
        return True

    @property
    def signed_in(self) -> bool:
        return bool(self.app.get_accounts())

    def sign_out(self) -> None:
        for a in self.app.get_accounts():
            self.app.remove_account(a)
        self.username = ""

    def __call__(self, scopes=PBI_SCOPE) -> str:
        accts = self.app.get_accounts()
        if not accts:
            raise AuthError("Not signed in.")
        result = self.app.acquire_token_silent(list(scopes), account=accts[0])
        if not result or "access_token" not in result:
            raise AuthError("Your sign-in expired - sign in again.")
        return result["access_token"]
