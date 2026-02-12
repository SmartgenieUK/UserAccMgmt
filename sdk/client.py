from __future__ import annotations

import httpx


class AuthClient:
    def __init__(self, base_url: str, timeout: float = 10.0):
        self.base_url = base_url.rstrip("/")
        self._client = httpx.Client(timeout=timeout)
        self.access_token: str | None = None
        self.refresh_token: str | None = None

    def _headers(self) -> dict:
        headers = {}
        if self.access_token:
            headers["Authorization"] = f"Bearer {self.access_token}"
        return headers

    def register(self, email: str, password: str, display_name: str | None = None) -> dict:
        payload = {"email": email, "password": password, "display_name": display_name}
        resp = self._client.post(f"{self.base_url}/api/v1/register", json=payload)
        resp.raise_for_status()
        return resp.json()

    def login(self, email: str, password: str, org_id: str | None = None) -> dict:
        payload = {"email": email, "password": password, "org_id": org_id}
        resp = self._client.post(f"{self.base_url}/api/v1/login", json=payload)
        resp.raise_for_status()
        data = resp.json()
        self.access_token = data["access_token"]
        self.refresh_token = data["refresh_token"]
        return data

    def refresh(self) -> dict:
        resp = self._client.post(f"{self.base_url}/api/v1/refresh", json={"refresh_token": self.refresh_token})
        resp.raise_for_status()
        data = resp.json()
        self.access_token = data["access_token"]
        self.refresh_token = data["refresh_token"]
        return data

    def get_me(self) -> dict:
        resp = self._client.get(f"{self.base_url}/api/v1/me", headers=self._headers())
        resp.raise_for_status()
        return resp.json()

    def create_org(self, name: str, slug: str | None = None) -> dict:
        resp = self._client.post(
            f"{self.base_url}/api/v1/orgs",
            json={"name": name, "slug": slug},
            headers=self._headers(),
        )
        resp.raise_for_status()
        return resp.json()
