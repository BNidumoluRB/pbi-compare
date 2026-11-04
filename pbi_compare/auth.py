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
