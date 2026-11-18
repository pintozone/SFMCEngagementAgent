"""Thin REST wrapper with auth, retry/backoff and helpful errors."""
from __future__ import annotations

import time
from typing import Any

import requests

from ..auth import TokenManager


class SFMCError(RuntimeError):
    def __init__(self, msg: str, status: int | None = None, body: Any = None):
        super().__init__(msg)
        self.status = status
        self.body = body


class RestClient:
    def __init__(self, tm: TokenManager, session: requests.Session | None = None, verbose: bool = False):
        self.tm = tm
        self.session = session or tm.session
        self.verbose = verbose

    def request(self, method: str, path: str, *, json: Any = None, params: dict | None = None,
                ok: tuple[int, ...] = (200, 201, 202, 204), allow_404: bool = False) -> Any:
        url = path if path.startswith("http") else f"{self.tm.rest_instance_url}{path}"
        delay = 1.0
        for attempt in range(5):
            headers = {"Authorization": f"Bearer {self.tm.token()}", "Content-Type": "application/json"}
            r = self.session.request(method, url, json=json, params=params, headers=headers, timeout=60)
            if self.verbose:
                print(f"  [rest] {method} {path} -> {r.status_code}")
            if r.status_code == 401 and attempt == 0:
                self.tm.token(force=True)
                continue
            if r.status_code in (429, 500, 502, 503, 504) and attempt < 4:
                time.sleep(delay)
                delay *= 2
                continue
            if allow_404 and r.status_code == 404:
                return None
            if r.status_code not in ok:
                try:
                    body = r.json()
                except ValueError:
                    body = r.text
                raise SFMCError(f"{method} {path} failed: HTTP {r.status_code}: {str(body)[:1500]}", r.status_code, body)
            if not r.content:
                return None
            try:
                return r.json()
            except ValueError:
                return r.text
        raise SFMCError(f"{method} {path} failed after retries")

    def get(self, path: str, **kw: Any) -> Any:
        return self.request("GET", path, **kw)

    def post(self, path: str, json: Any = None, **kw: Any) -> Any:
        return self.request("POST", path, json=json, **kw)

    def patch(self, path: str, json: Any = None, **kw: Any) -> Any:
        return self.request("PATCH", path, json=json, **kw)

    def put(self, path: str, json: Any = None, **kw: Any) -> Any:
        return self.request("PUT", path, json=json, **kw)

    def delete(self, path: str, **kw: Any) -> Any:
        return self.request("DELETE", path, **kw)
