"""GoHighLevel (API v2): opportunity stage webhooks in, opportunity custom fields and a contact note out.

Webhook: the `OpportunityStageUpdate` event (from a Marketplace app, or a workflow's
"Opportunity stage changed" trigger with a Custom Webhook action) carries the opportunity
`id` and its new `pipelineStageId`. Workflow webhooks cannot sign the body, so they carry a
long random shared token in an `X-Webhook-Token` header (see signatures.py).
Write-back: PUT /opportunities/{id} with the custom field ids from settings.ghl_field_ids
(`status`, `envelope`, `completed`), and a note on the contact. Every call sends the pinned
`Version` header.
"""

from __future__ import annotations

import hashlib
import json

from ..config import GHL_API_VERSION
from .base import CRMError, Deal, HTTPAdapter, StageEvent


class GHLAdapter(HTTPAdapter):
    name = "ghl"

    def __init__(self, token: str, base_url: str = "https://services.leadconnectorhq.com", *,
                 field_ids: dict | None = None, **kw):
        super().__init__(base_url, **kw)
        self.token = token
        self.field_ids = field_ids or {}

    def headers(self) -> dict:
        return {"Authorization": f"Bearer {self.token}", "Version": GHL_API_VERSION,
                "Accept": "application/json", "Content-Type": "application/json"}

    def parse_events(self, payload) -> list[StageEvent]:
        if not isinstance(payload, dict):
            raise CRMError("GoHighLevel webhook body must be a JSON object")
        kind = payload.get("type")
        if kind not in ("OpportunityStageUpdate", "OpportunityCreate", "OpportunityUpdate"):
            return []
        opp_id = payload.get("id") or payload.get("opportunityId")
        stage = payload.get("pipelineStageId") or payload.get("stageId")
        if not opp_id or not stage:
            return []
        marker = payload.get("webhookId") or hashlib.sha256(
            json.dumps(payload, sort_keys=True).encode()).hexdigest()[:16]
        return [StageEvent("ghl", str(opp_id), str(stage), f"ghl:{opp_id}:{stage}:{marker}")]

    def get_deal(self, deal_id: str) -> Deal:
        opp = self.request("GET", f"/opportunities/{deal_id}").json().get("opportunity") or {}
        if not opp:
            raise CRMError(f"GoHighLevel opportunity {deal_id} not found")
        contact_id = opp.get("contactId") or (opp.get("contact") or {}).get("id")
        if not contact_id:
            raise CRMError(f"GoHighLevel opportunity {deal_id} has no contact to sign")
        c = self.request("GET", f"/contacts/{contact_id}").json().get("contact") or {}
        name = " ".join(x for x in (c.get("firstName"), c.get("lastName")) if x).strip() or c.get("name") or ""
        value = opp.get("monetaryValue")
        return Deal(crm="ghl", deal_id=str(deal_id), deal_name=opp.get("name") or "",
                    amount=None if value in (None, "") else str(value), currency=opp.get("currency") or "USD",
                    stage=opp.get("pipelineStageId") or "", client_name=name, client_email=c.get("email") or "",
                    client_company=c.get("companyName") or "", contact_id=str(contact_id))

    def write_status(self, deal, status, envelope_id=None, completed_at=None) -> None:
        deal_id = deal.deal_id if isinstance(deal, Deal) else str(deal)
        values = {"status": status, "envelope": envelope_id, "completed": completed_at}
        fields = [{"id": self.field_ids[k], "field_value": v} for k, v in values.items()
                  if v and self.field_ids.get(k)]
        if not fields:
            raise CRMError("GoHighLevel custom field ids are not configured (settings.ghl_field_ids)")
        self.request("PUT", f"/opportunities/{deal_id}", json_body={"customFields": fields})

    def add_note(self, deal, text) -> None:
        self.request("POST", f"/contacts/{deal.contact_id}/notes", json_body={"body": text[:5000]})
