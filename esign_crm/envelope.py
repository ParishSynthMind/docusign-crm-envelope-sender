"""Builds the envelope definition sent to DocuSign (a pure function, easy to test).

- One PDF document, generated from the template.
- The client signs first (routing order 1); an optional countersigner from our side signs
  after (routing order 2).
- Tabs (signature, date signed, full name) sit on the invisible anchor strings in the PDF.
- `clientUserId` on the client makes it an EMBEDDED signer (we hand out the signing link,
  DocuSign sends no email to them); without it DocuSign emails the signing link.
- Hidden text custom fields carry the request key, CRM and deal id: that is how a retry finds
  an envelope that was already created, and how a Connect event finds its deal.
- When a public URL is configured, an envelope-level Connect subscription (SIM mode, JSON,
  HMAC-signed) reports sent, delivered, completed, declined and voided back to us.
"""

from __future__ import annotations

import base64

from .contracts import ANCHORS, Contract

CONNECT_EVENTS = ["envelope-sent", "envelope-delivered", "envelope-completed", "envelope-declined",
                  "envelope-voided"]
FIELD_REQUEST_KEY = "crm_request_key"
FIELD_CRM = "crm"
FIELD_DEAL_ID = "crm_deal_id"


def _tab(anchor: str, y_offset: str = "-4") -> dict:
    return {"anchorString": anchor, "anchorUnits": "pixels", "anchorXOffset": "0", "anchorYOffset": y_offset,
            "anchorIgnoreIfNotPresent": "false", "anchorMatchWholeWord": "true"}


def _signer(role: str, name: str, email: str, recipient_id: str, routing_order: str,
            client_user_id: str | None, anchors: list[str]) -> dict:
    tabs: dict = {"signHereTabs": [_tab(ANCHORS[("signature", role)], "-12")]}
    if ANCHORS[("date", role)] in anchors:
        tabs["dateSignedTabs"] = [_tab(ANCHORS[("date", role)])]
    if ANCHORS[("name", role)] in anchors:
        tabs["fullNameTabs"] = [_tab(ANCHORS[("name", role)])]
    signer = {"email": email, "name": name, "recipientId": recipient_id, "routingOrder": routing_order,
              "tabs": tabs}
    if client_user_id:
        signer["clientUserId"] = client_user_id
    return signer


def build_definition(contract: Contract, *, request_key: str, crm: str, deal_id: str,
                     client_name: str, client_email: str, email_subject: str, email_blurb: str,
                     embedded: bool, countersigner: tuple[str, str] | None = None,
                     connect_url: str | None = None, status: str = "sent") -> dict:
    if status not in ("sent", "created"):
        raise ValueError("status must be sent or created")
    signers = [_signer("client", client_name, client_email, "1", "1",
                       f"crm-{crm}-{deal_id}"[:100] if embedded else None, contract.anchors)]
    if countersigner:
        signers.append(_signer("countersigner", countersigner[0], countersigner[1], "2", "2", None,
                               contract.anchors))
    definition = {
        "emailSubject": email_subject[:100],
        "emailBlurb": email_blurb[:10000],
        "documents": [{
            "documentBase64": base64.b64encode(contract.pdf).decode("ascii"),
            "name": contract.title[:100],
            "fileExtension": "pdf",
            "documentId": "1",
        }],
        "recipients": {"signers": signers},
        "customFields": {"textCustomFields": [
            {"name": FIELD_REQUEST_KEY, "value": request_key, "show": "false", "required": "false"},
            {"name": FIELD_CRM, "value": crm, "show": "false", "required": "false"},
            {"name": FIELD_DEAL_ID, "value": str(deal_id), "show": "false", "required": "false"},
        ]},
        "status": status,
    }
    if connect_url:
        definition["eventNotification"] = {
            "url": connect_url,
            "requireAcknowledgment": "true",
            "includeHMAC": "true",
            "loggingEnabled": "true",
            "deliveryMode": "SIM",
            "events": CONNECT_EVENTS,
            "eventData": {"version": "restv2.1", "format": "json", "includeData": ["custom_fields", "recipients"]},
        }
    return definition


def custom_field(envelope_or_summary: dict, name: str) -> str | None:
    fields = (envelope_or_summary.get("customFields") or {}).get("textCustomFields") or []
    for f in fields:
        if f.get("name") == name:
            return f.get("value")
    return None
