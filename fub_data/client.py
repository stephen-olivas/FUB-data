"""Minimal Follow Up Boss API client.

Auth: HTTP Basic with the API key as the username and a blank password.
Optional X-System / X-System-Key headers raise the rate limits (250 req / 10s
global instead of 125). The client honours 429 Retry-After and pages with the
`next` token that FUB returns in `_metadata`.
"""

from __future__ import annotations

import os
import sys
import time
from typing import Any, Iterator

import requests

DEFAULT_BASE_URL = "https://api.followupboss.com/v1"


class FUBError(RuntimeError):
    pass


class FUBClient:
    def __init__(
        self,
        api_key: str | None = None,
        base_url: str | None = None,
        system: str | None = None,
        system_key: str | None = None,
        max_retries: int = 6,
        verbose: bool = False,
    ) -> None:
        api_key = api_key or os.environ.get("FUB_API_KEY")
        if not api_key:
            raise FUBError("Set FUB_API_KEY (Admin > API in Follow Up Boss).")
        self.base_url = (base_url or os.environ.get("FUB_BASE_URL") or DEFAULT_BASE_URL).rstrip("/")
        self.session = requests.Session()
        self.session.auth = (api_key, "")
        self.session.headers["Accept"] = "application/json"
        system = system or os.environ.get("FUB_SYSTEM")
        system_key = system_key or os.environ.get("FUB_SYSTEM_KEY")
        if system:
            self.session.headers["X-System"] = system
        if system_key:
            self.session.headers["X-System-Key"] = system_key
        self.max_retries = max_retries
        self.verbose = verbose
        self.request_count = 0

    # -- low level -----------------------------------------------------------
    def _url(self, path_or_url: str) -> str:
        if path_or_url.startswith("http"):
            return path_or_url
        return f"{self.base_url}/{path_or_url.lstrip('/')}"

    def _request(self, method: str, path: str, params: dict | None = None, json: dict | None = None) -> dict:
        url = self._url(path)
        backoff = 2.0
        for attempt in range(self.max_retries + 1):
            self.request_count += 1
            resp = self.session.request(method, url, params=params, json=json, timeout=60)
            if resp.status_code == 429:
                wait = float(resp.headers.get("Retry-After") or backoff)
                if self.verbose:
                    ctx = resp.headers.get("X-RateLimit-Context", "?")
                    print(f"  rate limited ({ctx}); waiting {wait:.0f}s", file=sys.stderr)
                time.sleep(wait)
                backoff = min(backoff * 2, 60)
                continue
            if resp.status_code >= 500 and attempt < self.max_retries:
                time.sleep(backoff)
                backoff = min(backoff * 2, 60)
                continue
            if resp.status_code >= 400:
                raise FUBError(f"{method} {url} -> {resp.status_code}: {resp.text[:300]}")
            return resp.json() if resp.content else {}
        raise FUBError(f"{method} {url} failed after {self.max_retries} retries")

    def get(self, path: str, params: dict | None = None) -> dict:
        return self._request("GET", path, params=params)

    def put(self, path: str, body: dict, params: dict | None = None) -> dict:
        return self._request("PUT", path, params=params, json=body)

    def paginate(
        self,
        path: str,
        params: dict | None = None,
        collection: str | None = None,
        limit: int = 100,
    ) -> Iterator[dict]:
        """Yield every record from a list endpoint, following `next` tokens."""
        params = dict(params or {})
        params.setdefault("limit", limit)
        url: str = path
        while True:
            data = self.get(url, params)
            meta = data.get("_metadata", {}) or {}
            key = meta.get("collection") if meta.get("collection") in data else None
            if key is None:
                want = (collection or path.strip("/").split("/")[-1]).lower()
                key = next((k for k, v in data.items() if k.lower() == want and isinstance(v, list)), None)
            for item in (data.get(key) if key else None) or []:
                yield item
            next_link = meta.get("nextLink")
            next_token = meta.get("next")
            if next_link:
                url, params = next_link, None
            elif next_token:
                params = {**(params or {}), "next": next_token}
                params.pop("offset", None)
            else:
                return

    # -- convenience ---------------------------------------------------------
    def list_all(self, path: str, collection: str, params: dict | None = None) -> list[dict]:
        return list(self.paginate(path, params, collection))

    def try_list(self, path: str, collection: str, params: dict | None = None) -> tuple[list[dict], str | None]:
        """Like list_all but returns (records, error) instead of raising (e.g. 403 on an endpoint)."""
        try:
            return self.list_all(path, collection, params), None
        except FUBError as exc:
            return [], str(exc)
