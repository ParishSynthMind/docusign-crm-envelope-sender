"""What every CRM adapter does, and the shared HTTP retry logic."""

from __future__ import annotations

import time
from dataclasses import dataclass, field

import httpx


class CRMError(RuntimeError):
    def __init__(self, message: str, status: int | None = None):
        super().__init__(message)
        self.status = status


@dataclass
class Deal:
    crm: str
    deal_id: str
    deal_name: str
    amount: str | None
    currency: str
    stage: str
    client_name: str
    client_email: str
    client_company: str = ""
    contact_id: str = ""
    description: str = ""
    extra: dict = field(default_factory=dict)

    def contract_fields(self) -> dict:
        return {"deal_name": self.deal_name, "amount": self.amount, "currency": self.currency,
                "client_name": self.client_name, "client_email": self.client_email,
                "client_company": self.client_company, "description": self.description, **self.extra}


@dataclass
class StageEvent:
    crm: str
    deal_id: str
    stage: str
    delivery_key: str


class HTTPAdapter:
    """Shared request loop: reads and idempotent writes (PUT/PATCH) are retried on 429/5xx and
    timeouts; non-idempotent writes (POST of a note) are never retried after a 5xx or timeout."""

    name = "crm"

    def __init__(self, base_url: str, transport: httpx.BaseTransport | None = None, timeout: float = 30.0,
                 max_retries: int = 4, sleep=time.sleep):
        self.base_url = base_url.rstrip("/")
        self.http = httpx.Client(transport=transport, timeout=timeout)
        self.max_retries = max_retries
        self.sleep = sleep
        self.calls: list[tuple[str, str, int]] = []

    def headers(self) -> dict:
        raise NotImplementedError

    def on_unauthorized(self) -> bool:
        """Return True if credentials were refreshed and the call should be repeated once."""
        return False

    def request(self, method: str, path: str, *, json_body=None, params=None) -> httpx.Response:
        url = self.base_url + path
        retry_safe = method in ("GET", "PUT", "PATCH", "DELETE")
        attempt = 0
        refreshed = False
        while True:
            attempt += 1
            try:
                resp = self.http.request(method, url, json=json_body, params=params, headers=self.headers())
            except httpx.TransportError as e:
                self.calls.append((method, path, 0))
                if not retry_safe or attempt > self.max_retries:
                    raise CRMError(f"{self.name} {method} {path}: {type(e).__name__}") from e
                self.sleep(min(2 ** attempt, 30))
                continue
            self.calls.append((method, path, resp.status_code))
            if resp.status_code == 401 and not refreshed and self.on_unauthorized():
                refreshed = True
                continue
            if resp.status_code == 429 and attempt <= self.max_retries:
                retry_after = resp.headers.get("Retry-After", "")
                self.sleep(int(retry_after) if retry_after.isdigit() else min(2 ** attempt, 30))
                continue
            if resp.status_code >= 500 and retry_safe and attempt <= self.max_retries:
                self.sleep(min(2 ** attempt, 30))
                continue
            if resp.status_code >= 400:
                raise CRMError(f"{self.name} {method} {path} failed: {resp.status_code} {resp.text[:300]}",
                               resp.status_code)
            return resp

    # --- the interface ----------------------------------------------------------
    def parse_events(self, payload) -> list[StageEvent]:
        raise NotImplementedError

    def get_deal(self, deal_id: str) -> Deal:
        raise NotImplementedError

    def write_status(self, deal: Deal | str, status: str, envelope_id: str | None = None,
                     completed_at: str | None = None) -> None:
        raise NotImplementedError

    def add_note(self, deal: Deal, text: str) -> None:
        raise NotImplementedError

    def setup_fields(self) -> list[str]:
        """Create the custom fields the status is written to. Safe to re-run."""
        return []
