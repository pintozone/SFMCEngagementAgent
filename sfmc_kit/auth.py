"""OAuth2 client_credentials auth for an SFMC Installed Package (server-to-server)."""
from __future__ import annotations

import threading
import time

import requests

from .config import SFMCConfig


class AuthError(RuntimeError):
    pass


class TokenManager:
    """Fetches and caches an access token in memory (never written to disk)."""

    def __init__(self, cfg: SFMCConfig, session: requests.Session | None = None):
        self.cfg = cfg
        self.session = session or requests.Session()
        self._token: str | None = None
        self._expires_at = 0.0
        self.rest_instance_url: str = cfg.rest_base
        self.soap_instance_url: str = cfg.soap_url
        self.scope: str = ""
        self._lock = threading.Lock()

    def token(self, force: bool = False) -> str:
        with self._lock:
            if force or not self._token or time.time() > self._expires_at - 60:
                self._fetch()
            return self._token  # type: ignore[return-value]

    def _fetch(self) -> None:
        body = {
            "grant_type": "client_credentials",
            "client_id": self.cfg.client_id,
            "client_secret": self.cfg.client_secret,
        }
        if self.cfg.account_id:
            body["account_id"] = self.cfg.account_id
        r = self.session.post(f"{self.cfg.auth_base}/v2/token", json=body, timeout=30)
        if r.status_code != 200:
            # Never echo the request body (it contains the secret).
            raise AuthError(f"Token request failed: HTTP {r.status_code}: {r.text[:500]}")
        data = r.json()
        self._token = data["access_token"]
        self._expires_at = time.time() + int(data.get("expires_in", 1080))
        self.rest_instance_url = data.get("rest_instance_url", self.cfg.rest_base).rstrip("/")
        soap = data.get("soap_instance_url", "").rstrip("/")
        self.soap_instance_url = f"{soap}/Service.asmx" if soap else self.cfg.soap_url
        self.scope = data.get("scope", "")
