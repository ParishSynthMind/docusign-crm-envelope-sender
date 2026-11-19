"""The webhook and approval server (FastAPI).

POST /webhooks/hubspot     HubSpot deal stage events (v3 signature)
POST /webhooks/ghl         GoHighLevel opportunity stage events (shared token or HMAC)
POST /webhooks/zoho        Zoho CRM workflow webhook (shared token or HMAC; JSON or form)
POST /webhooks/docusign    DocuSign Connect (HMAC-SHA256, SIM JSON)
GET  /requests             the queue (approver token)
POST /requests/{id}/approve | /reject | /void   (approver token)
GET  /requests/{id}/sign   302 to a fresh embedded signing session (approver token)
GET  /signing-return       where DocuSign sends the signer back
GET  /health

Signatures are checked on the raw bytes before anything is parsed. CRM events are handled in
the request (a contract needs the deal, and the CRM retries on a non-2xx), Connect events too:
DocuSign retries until we answer 200, so a failed download is retried by DocuSign itself.
"""

from __future__ import annotations

import hmac
import json
import logging
import urllib.parse

from fastapi import FastAPI, Header, HTTPException, Request
from fastapi.responses import HTMLResponse, JSONResponse, RedirectResponse

from . import signatures
from .crm.base import CRMError
from .docusign_api import DocuSignError
from .service import NotAllowed, Paused, Sender

log = logging.getLogger("esign_crm")
MAX_BODY = 1_000_000


def create_app(sender: Sender, secrets: dict) -> FastAPI:
    """`secrets`: docusign_hmac_keys (list), hubspot_client_secret, ghl_webhook_secret,
    zoho_webhook_secret, approver_token. A missing secret disables that endpoint (401)."""
    app = FastAPI(title="DocuSign CRM envelope sender", docs_url=None, redoc_url=None)
    app.state.sender = sender

    async def raw(request: Request) -> bytes:
        body = await request.body()
        if len(body) > MAX_BODY:
            raise HTTPException(413, "body too large")
        return body

    def require_approver(authorization: str | None) -> str:
        token = secrets.get("approver_token") or ""
        given = (authorization or "").removeprefix("Bearer ").strip()
        if not token or not given or not hmac.compare_digest(token.encode(), given.encode()):
            raise HTTPException(401, "approver token required")
        return "api-approver"

    def run_events(crm: str, payload) -> JSONResponse:
        adapter = sender.crms.get(crm)
        if adapter is None:
            raise HTTPException(404, f"{crm} is not configured")
        try:
            events = adapter.parse_events(payload)
        except CRMError as e:
            raise HTTPException(400, str(e)) from None
        results = []
        for ev in events:
            if not sender.store.first_delivery(ev.delivery_key, crm):
                results.append("duplicate_delivery")
                continue
            try:
                results.append(sender.handle_stage_event(ev))
            except Paused:
                sender.store.forget_delivery(ev.delivery_key)
                return JSONResponse({"status": "paused"}, status_code=503)
            except (CRMError, DocuSignError) as e:
                # Let the CRM retry the delivery later.
                sender.store.forget_delivery(ev.delivery_key)
                sender.alert("failure", f"{crm} stage event could not be processed", str(e))
                return JSONResponse({"status": "error"}, status_code=502)
        return JSONResponse({"status": "ok", "results": results})

    @app.get("/health")
    def health():
        return {"ok": True, "paused": sender.s.paused}

    @app.post("/webhooks/hubspot")
    async def hubspot(request: Request,
                      x_hubspot_signature_v3: str | None = Header(default=None),
                      x_hubspot_request_timestamp: str | None = Header(default=None)):
        body = await raw(request)
        uri = str(request.url)
        if request.headers.get("x-forwarded-proto") == "https" and uri.startswith("http://"):
            uri = "https://" + uri[len("http://"):]
        uri = urllib.parse.unquote(uri)
        if not signatures.verify_hubspot("POST", uri, body, x_hubspot_signature_v3, x_hubspot_request_timestamp,
                                         secrets.get("hubspot_client_secret") or "",
                                         sender.s.webhook_max_age_seconds):
            raise HTTPException(401, "bad signature")
        try:
            payload = json.loads(body)
        except ValueError:
            raise HTTPException(400, "not JSON") from None
        return run_events("hubspot", payload)

    async def shared(request: Request, crm: str, secret_name: str):
        body = await raw(request)
        if not signatures.verify_shared(body, request.headers.get(signatures.TOKEN_HEADER),
                                        request.headers.get(signatures.HMAC_HEADER), secrets.get(secret_name) or ""):
            raise HTTPException(401, "bad token or signature")
        ctype = request.headers.get("content-type", "")
        try:
            if "application/x-www-form-urlencoded" in ctype:
                payload = dict(urllib.parse.parse_qsl(body.decode("utf-8")))
            else:
                payload = json.loads(body)
        except (ValueError, UnicodeDecodeError):
            raise HTTPException(400, "unreadable body") from None
        return run_events(crm, payload)

    @app.post("/webhooks/ghl")
    async def ghl(request: Request):
        return await shared(request, "ghl", "ghl_webhook_secret")

    @app.post("/webhooks/zoho")
    async def zoho(request: Request):
        return await shared(request, "zoho", "zoho_webhook_secret")

    @app.post("/webhooks/docusign")
    async def docusign(request: Request):
        body = await raw(request)
        if not signatures.verify_docusign(body, request.headers, secrets.get("docusign_hmac_keys") or []):
            raise HTTPException(401, "bad signature")
        try:
            payload = json.loads(body)
        except ValueError:
            raise HTTPException(400, "not JSON") from None
        data = payload.get("data") or {}
        key = f"docusign:{data.get('envelopeId')}:{payload.get('event')}:{payload.get('generatedDateTime')}"
        if not sender.store.first_delivery(key, "docusign"):
            return {"status": "duplicate_delivery"}
        try:
            outcome = sender.handle_connect(payload)
        except (DocuSignError, CRMError, OSError) as e:
            sender.store.forget_delivery(key)
            sender.alert("failure", "DocuSign Connect event could not be processed", str(e))
            return JSONResponse({"status": "error"}, status_code=500)   # DocuSign retries
        return {"status": outcome}

    # --- approval API --------------------------------------------------------------
    @app.get("/requests")
    def list_requests(status: str | None = None, authorization: str | None = Header(default=None)):
        require_approver(authorization)
        return [{"id": r.id, "crm": r.crm, "deal_id": r.deal_id, "status": r.status, "envelope_id": r.envelope_id,
                 "deal_name": r.deal["deal"]["deal_name"], "amount": r.deal["deal"]["amount"],
                 "pdf_sha256": r.pdf_sha256} for r in sender.store.list(status)]

    def act(fn, *args, **kw):
        try:
            return {"status": fn(*args, **kw)}
        except Paused:
            raise HTTPException(503, "paused (kill switch on)") from None
        except NotAllowed as e:
            raise HTTPException(409, str(e)) from None
        except DocuSignError as e:
            raise HTTPException(502, str(e)) from None

    @app.post("/requests/{request_id}/approve")
    def approve(request_id: int, confirm_large: bool = False, authorization: str | None = Header(default=None)):
        who = require_approver(authorization)
        return act(sender.approve, request_id, who, confirm_large=confirm_large)

    @app.post("/requests/{request_id}/reject")
    def reject(request_id: int, reason: str = "", authorization: str | None = Header(default=None)):
        who = require_approver(authorization)
        return act(sender.reject, request_id, who, reason)

    @app.post("/requests/{request_id}/void")
    def void(request_id: int, reason: str = "", authorization: str | None = Header(default=None)):
        who = require_approver(authorization)
        return act(sender.void, request_id, who, reason)

    @app.get("/requests/{request_id}/sign")
    def sign(request_id: int, authorization: str | None = Header(default=None)):
        require_approver(authorization)
        try:
            return RedirectResponse(sender.signing_url(request_id), status_code=302)
        except NotAllowed as e:
            raise HTTPException(409, str(e)) from None

    @app.get("/signing-return", response_class=HTMLResponse)
    def signing_return(event: str = "", request: str = ""):
        # Never trust this page for the status (DocuSign says so too): Connect and reconcile decide.
        safe = "".join(c for c in event if c.isalnum() or c == "_")[:40]
        text = {"signing_complete": "Thank you, the document is signed. A copy will reach you by email.",
                "decline": "You declined to sign. The sender has been told.",
                "cancel": "Signing paused. You can come back to it later."}.get(safe, "Signing session ended.")
        return f"<!doctype html><html><body style='font-family:sans-serif;max-width:40em;margin:4em auto'>" \
               f"<h1>{text}</h1></body></html>"

    return app
