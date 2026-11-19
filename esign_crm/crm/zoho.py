"""Zoho CRM (API v8): deal stage webhooks in, deal fields and a note out.

Webhook: a Zoho workflow rule on Deals ("when Stage is updated to ...") with a Webhook
action. The body is ours to define in Zoho; we expect `deal_id` and `stage` (JSON or form
fields), plus the shared token header `X-Webhook-Token` (Zoho lets you add custom headers).
Auth: a refresh token (OAuth, offline access) is swapped for a 1-hour access token, renewed
on expiry or on a 401. Header `Authorization: Zoho-oauthtoken <token>`.
Write-back: PUT /crm/v8/Deals/{id} with the custom fields (API names from settings), and a
note under the deal.
"""

from __future__ import annotations

import time

import httpx

from ..config import ZOHO_CRM_VERSION
from .base import CRMError, Deal, HTTPAdapter, StageEvent

DEFAULT_FIELDS = {"status": "DocuSign_Status", "envelope": "DocuSign_Envelope_ID",
                  "completed": "DocuSign_Completed_At"}
FIELD_LABELS = {"status": "DocuSign Status", "envelope": "DocuSign Envelope ID",
                "completed": "DocuSign Completed At"}


class ZohoAdapter(HTTPAdapter):
    name = "zoho"

    def __init__(self, client_id: str, client_secret: str, refresh_token: str, *,
                 api_base: str = "https://www.zohoapis.com", accounts_url: str = "https://accounts.zoho.com",
                 field_names: dict | None = None, clock=time.time, **kw):
        super().__init__(api_base.rstrip("/") + f"/crm/{ZOHO_CRM_VERSION}", **kw)
        self.client_id = client_id
        self.client_secret = client_secret
        self.refresh_token = refresh_token
        self.accounts_url = accounts_url.rstrip("/")
        self.field_names = {**DEFAULT_FIELDS, **(field_names or {})}
        self.clock = clock
        self._token: str | None = None
        self._expires = 0.0
        self.token_requests = 0

    def _refresh(self) -> None:
        resp = self.http.post(f"{self.accounts_url}/oauth/v2/token", params={
            "refresh_token": self.refresh_token, "client_id": self.client_id,
            "client_secret": self.client_secret, "grant_type": "refresh_token"})
        self.token_requests += 1
        body = {}
        try:
            body = resp.json()
        except ValueError:
            pass
        if resp.status_code != 200 or "access_token" not in body:
            raise CRMError(f"Zoho token refresh failed: {resp.status_code} {body.get('error', '')}", resp.status_code)
        self._token = body["access_token"]
        self._expires = self.clock() + int(body.get("expires_in", 3600))

    def headers(self) -> dict:
        if not self._token or self.clock() > self._expires - 120:
            self._refresh()
        return {"Authorization": f"Zoho-oauthtoken {self._token}", "Content-Type": "application/json"}

    def on_unauthorized(self) -> bool:
        self._token = None
        return True

    def parse_events(self, payload) -> list[StageEvent]:
        if not isinstance(payload, dict):
            raise CRMError("Zoho webhook body must be an object (JSON or form fields)")
        deal_id = payload.get("deal_id") or payload.get("id")
        stage = payload.get("stage") or payload.get("Stage")
        if not deal_id or not stage:
            return []
        marker = payload.get("modified_time") or payload.get("event_id") or ""
        return [StageEvent("zoho", str(deal_id), str(stage), f"zoho:{deal_id}:{stage}:{marker}")]

    @staticmethod
    def _check(resp: httpx.Response, what: str, key: str = "data") -> dict:
        body = resp.json() if resp.content else {}
        rows = body.get(key) or []
        if not rows:
            raise CRMError(f"Zoho {what}: empty answer")
        row = rows[0]
        if "code" in row and row.get("code") != "SUCCESS":
            raise CRMError(f"Zoho {what} rejected: {row.get('code')} {row.get('message', '')} {row.get('details', '')}")
        return row

    def get_deal(self, deal_id: str) -> Deal:
        resp = self.request("GET", f"/Deals/{deal_id}")
        if resp.status_code == 204:
            raise CRMError(f"Zoho deal {deal_id} not found")
        d = self._check(resp, "get deal")
        contact = d.get("Contact_Name") or {}
        if not contact.get("id"):
            raise CRMError(f"Zoho deal {deal_id} has no contact to sign")
        cresp = self.request("GET", f"/Contacts/{contact['id']}")
        c = self._check(cresp, "get contact")
        name = " ".join(x for x in (c.get("First_Name"), c.get("Last_Name")) if x).strip() or contact.get("name", "")
        account = d.get("Account_Name") or {}
        amount = d.get("Amount")
        return Deal(crm="zoho", deal_id=str(deal_id), deal_name=d.get("Deal_Name") or "",
                    amount=None if amount in (None, "") else str(amount), currency=d.get("Currency") or "USD",
                    stage=d.get("Stage") or "", client_name=name, client_email=c.get("Email") or "",
                    client_company=(account.get("name") if isinstance(account, dict) else str(account)) or "",
                    contact_id=str(contact["id"]), description=d.get("Description") or "")

    def write_status(self, deal, status, envelope_id=None, completed_at=None) -> None:
        deal_id = deal.deal_id if isinstance(deal, Deal) else str(deal)
        row = {self.field_names["status"]: status}
        if envelope_id:
            row[self.field_names["envelope"]] = envelope_id
        if completed_at:
            row[self.field_names["completed"]] = completed_at
        self._check(self.request("PUT", f"/Deals/{deal_id}", json_body={"data": [row]}), "update deal")

    def add_note(self, deal, text) -> None:
        self._check(self.request("POST", "/Notes", json_body={"data": [{
            "Note_Title": "DocuSign", "Note_Content": text[:30000],
            "Parent_Id": {"module": {"api_name": "Deals"}, "id": deal.deal_id}}]}), "add note")

    def setup_fields(self) -> list[str]:
        existing = {f.get("api_name") for f in
                    self.request("GET", "/settings/fields", params={"module": "Deals"}).json().get("fields", [])}
        made = []
        for key, api_name in self.field_names.items():
            if api_name in existing:
                continue
            self._check(self.request("POST", "/settings/fields", params={"module": "Deals"}, json_body={
                "fields": [{"field_label": FIELD_LABELS[key], "data_type": "text", "length": 255}]}),
                "create field", key="fields")
            made.append(api_name)
        return made
