"""HubSpot: deal stage webhooks (v3 signed) in, deal properties and a note out.

Webhook: a HubSpot app subscribed to `deal.propertyChange` for `dealstage` posts a JSON
list of events; each has an `eventId`, `objectId` (the deal) and `propertyValue` (the new
stage id, e.g. `contractsent`). Newer generic subscriptions (`object.propertyChange`
with objectTypeId 0-3) are read the same way.
Write-back: PATCH the deal's custom properties (docusign_status, docusign_envelope_id,
docusign_completed_at) and add a note associated with the deal (association type 214).
"""

from __future__ import annotations

import datetime as dt

from ..config import HUBSPOT_CRM_VERSION
from .base import CRMError, Deal, HTTPAdapter, StageEvent

NOTE_TO_DEAL = 214


class HubSpotAdapter(HTTPAdapter):
    name = "hubspot"

    def __init__(self, token: str, base_url: str = "https://api.hubapi.com", *,
                 status_field: str = "docusign_status", envelope_field: str = "docusign_envelope_id",
                 completed_field: str = "docusign_completed_at", **kw):
        super().__init__(base_url, **kw)
        self.token = token
        self.fields = {"status": status_field, "envelope": envelope_field, "completed": completed_field}
        self.v = HUBSPOT_CRM_VERSION

    def headers(self) -> dict:
        return {"Authorization": f"Bearer {self.token}", "Content-Type": "application/json"}

    def parse_events(self, payload) -> list[StageEvent]:
        if not isinstance(payload, list):
            raise CRMError("HubSpot webhook body must be a JSON list")
        events = []
        for e in payload:
            if not isinstance(e, dict):
                continue
            kind = e.get("subscriptionType")
            is_deal = kind == "deal.propertyChange" or (kind == "object.propertyChange" and e.get("objectTypeId") == "0-3")
            if not is_deal or e.get("propertyName") != "dealstage":
                continue
            if e.get("objectId") is None or e.get("propertyValue") in (None, ""):
                continue
            # Retries of the same event keep its eventId, so it is the delivery key.
            key = f"hubspot:{e.get('eventId')}" if e.get("eventId") is not None else \
                f"hubspot:{e['objectId']}:{e['propertyValue']}:{e.get('occurredAt')}"
            events.append(StageEvent("hubspot", str(e["objectId"]), str(e["propertyValue"]), key))
        return events

    def get_deal(self, deal_id: str) -> Deal:
        resp = self.request("GET", f"/crm/{self.v}/objects/deals/{deal_id}", params={
            "properties": "dealname,amount,deal_currency_code,dealstage,description",
            "associations": "contacts,companies"})
        data = resp.json()
        props = data.get("properties") or {}
        assoc = data.get("associations") or {}
        contacts = [r["id"] for r in (assoc.get("contacts") or {}).get("results", [])]
        if not contacts:
            raise CRMError(f"HubSpot deal {deal_id} has no associated contact to sign")
        contact = self.request("GET", f"/crm/{self.v}/objects/contacts/{contacts[0]}", params={
            "properties": "firstname,lastname,email,company"}).json().get("properties") or {}
        company = contact.get("company") or ""
        companies = [r["id"] for r in (assoc.get("companies") or {}).get("results", [])]
        if companies:
            cp = self.request("GET", f"/crm/{self.v}/objects/companies/{companies[0]}",
                              params={"properties": "name"}).json().get("properties") or {}
            company = cp.get("name") or company
        name = " ".join(x for x in (contact.get("firstname"), contact.get("lastname")) if x).strip()
        return Deal(crm="hubspot", deal_id=str(deal_id), deal_name=props.get("dealname") or "",
                    amount=props.get("amount"), currency=props.get("deal_currency_code") or "USD",
                    stage=props.get("dealstage") or "", client_name=name, client_email=contact.get("email") or "",
                    client_company=company, contact_id=str(contacts[0]), description=props.get("description") or "")

    def write_status(self, deal, status, envelope_id=None, completed_at=None) -> None:
        deal_id = deal.deal_id if isinstance(deal, Deal) else str(deal)
        props = {self.fields["status"]: status}
        if envelope_id:
            props[self.fields["envelope"]] = envelope_id
        if completed_at:
            props[self.fields["completed"]] = completed_at
        self.request("PATCH", f"/crm/{self.v}/objects/deals/{deal_id}", json_body={"properties": props})

    def add_note(self, deal, text) -> None:
        now = dt.datetime.now(dt.timezone.utc).strftime("%Y-%m-%dT%H:%M:%S.000Z")
        self.request("POST", f"/crm/{self.v}/objects/notes", json_body={
            "properties": {"hs_timestamp": now, "hs_note_body": text[:65000]},
            "associations": [{"to": {"id": deal.deal_id}, "types": [
                {"associationCategory": "HUBSPOT_DEFINED", "associationTypeId": NOTE_TO_DEAL}]}]})

    def setup_fields(self) -> list[str]:
        made = []
        for key, label in (("status", "DocuSign status"), ("envelope", "DocuSign envelope ID"),
                           ("completed", "DocuSign completed at")):
            try:
                self.request("POST", f"/crm/{self.v}/properties/deals", json_body={
                    "name": self.fields[key], "label": label, "type": "string", "fieldType": "text",
                    "groupName": "dealinformation"})
                made.append(self.fields[key])
            except CRMError as e:
                if e.status != 409:   # 409 = already exists, fine
                    raise
        return made
