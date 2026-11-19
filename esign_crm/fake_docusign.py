"""An in-memory fake of the DocuSign OAuth service and eSignature REST API v2.1.

Used by the tests and `scripts/local_run.py`, so the whole flow runs with no account. It
checks what the real service checks that matters to us: the JWT assertion (RS256 signature
with the app's public key, iss, sub, aud without https://, scope, lifetime), consent, the
bearer token on every call, the envelope definition (documents, signers, anchor tabs, custom
fields), clientUserId for embedded signing, and it can inject 429, 5xx and timeouts.
It also builds signed Connect (SIM JSON) payloads like DocuSign's.
"""

from __future__ import annotations

import base64
import json
import secrets as pysecrets
import time
import urllib.parse
import uuid

import httpx
import jwt

from .signatures import docusign_sign

OAUTH = "account-d.docusign.com"
BASE = "https://demo.docusign.net"


class FakeDocuSign:
    def __init__(self, integration_key: str, user_id: str, public_key_pem: str, account_id: str | None = None,
                 consented: bool = True, clock=time.time):
        self.integration_key = integration_key
        self.user_id = user_id
        self.public_key_pem = public_key_pem
        self.account_id = account_id or str(uuid.uuid4())
        self.consented = consented
        self.clock = clock
        self.tokens: dict[str, float] = {}
        self.envelopes: dict[str, dict] = {}
        self.requests: list[tuple[str, str]] = []
        self.failures: list[tuple[str, str, object]] = []   # (method, path contains, status or "timeout")
        self.views: list[dict] = []

    # --- test controls ----------------------------------------------------------
    def fail_next(self, method: str, path_part: str, status) -> None:
        self.failures.append((method, path_part, status))

    def transport(self) -> httpx.MockTransport:
        return httpx.MockTransport(self.handle)

    # --- helpers -------------------------------------------------------------------
    @staticmethod
    def _json(status: int, body) -> httpx.Response:
        return httpx.Response(status, json=body)

    def _error(self, status: int, code: str, message: str = "") -> httpx.Response:
        return self._json(status, {"errorCode": code, "message": message or code})

    def handle(self, request: httpx.Request) -> httpx.Response:
        path = request.url.path
        self.requests.append((request.method, path))
        for i, (m, part, status) in enumerate(self.failures):
            if m == request.method and part in path:
                del self.failures[i]
                if status == "timeout":
                    raise httpx.ReadTimeout("fake timeout", request=request)
                if status == "applied_then_500":
                    # The write lands, then the answer is lost: the classic ambiguous write.
                    self._route(request, path)
                    return self._error(503, "SERVICE_UNAVAILABLE")
                headers = {"X-RateLimit-Reset": "0"} if status == 429 else {}
                return httpx.Response(status, headers=headers, json={"errorCode": "INJECTED", "message": "injected"})
        return self._route(request, path)

    def _route(self, request: httpx.Request, path: str) -> httpx.Response:
        host = request.url.host
        if host == OAUTH:
            if path == "/oauth/token" and request.method == "POST":
                return self._token(request)
            if path == "/oauth/userinfo":
                if not self._authed(request):
                    return self._json(401, {"error": "invalid_token"})
                return self._json(200, {"sub": self.user_id, "name": "Test Sender", "email": "sender@example.com",
                                        "accounts": [{"account_id": self.account_id, "is_default": True,
                                                      "account_name": "Demo Account", "base_uri": BASE}]})
            return self._json(404, {"error": "not_found"})
        if not self._authed(request):
            return self._error(401, "USER_AUTHENTICATION_FAILED")
        prefix = f"/restapi/v2.1/accounts/{self.account_id}"
        if not path.startswith(prefix):
            return self._error(400, "INVALID_ACCOUNT_ID")
        rest = path[len(prefix):]
        parts = [p for p in rest.split("/") if p]
        if parts == ["envelopes"] and request.method == "POST":
            return self._create(json.loads(request.content))
        if parts == ["envelopes"] and request.method == "GET":
            return self._list(request)
        if len(parts) >= 2 and parts[0] == "envelopes":
            env = self.envelopes.get(parts[1])
            if not env:
                return self._error(404, "ENVELOPE_DOES_NOT_EXIST")
            if len(parts) == 2 and request.method == "GET":
                return self._json(200, self._public(env))
            if len(parts) == 2 and request.method == "PUT":
                body = json.loads(request.content)
                if body.get("status") == "voided":
                    if env["status"] in ("completed", "voided", "declined"):
                        return self._error(400, "ENVELOPE_CANNOT_VOID_INVALID_STATE")
                    if not body.get("voidedReason"):
                        return self._error(400, "INVALID_REQUEST_PARAMETER", "voidedReason required")
                    env["status"] = "voided"
                    env["voidedReason"] = body["voidedReason"]
                    return self._json(200, {"envelopeId": env["envelopeId"]})
                return self._error(400, "INVALID_REQUEST_PARAMETER")
            if parts[2:] == ["views", "recipient"] and request.method == "POST":
                return self._view(env, json.loads(request.content))
            if len(parts) == 4 and parts[2] == "documents" and request.method == "GET":
                if parts[3] == "combined":
                    if env["status"] != "completed":
                        return self._json(200, {"note": "unsigned"})
                    return httpx.Response(200, content=b"%PDF-1.4 signed " + env["envelopeId"].encode(),
                                          headers={"Content-Type": "application/pdf"})
                if parts[3] == "certificate":
                    return httpx.Response(200, content=b"%PDF-1.4 certificate " + env["envelopeId"].encode(),
                                          headers={"Content-Type": "application/pdf"})
        return self._error(404, "NOT_FOUND")

    def _authed(self, request: httpx.Request) -> bool:
        auth = request.headers.get("Authorization", "")
        token = auth.removeprefix("Bearer ").strip()
        return token in self.tokens and self.tokens[token] > self.clock()

    def _token(self, request: httpx.Request) -> httpx.Response:
        form = dict(urllib.parse.parse_qsl(request.content.decode()))
        if form.get("grant_type") != "urn:ietf:params:oauth:grant-type:jwt-bearer":
            return self._json(400, {"error": "unsupported_grant_type"})
        try:
            claims = jwt.decode(form.get("assertion", ""), self.public_key_pem, algorithms=["RS256"],
                                audience=OAUTH, options={"require": ["iss", "sub", "aud", "iat", "exp"],
                                                         "verify_exp": False, "verify_iat": False})
        except jwt.InvalidAudienceError:
            return self._json(400, {"error": "invalid_grant", "error_description": "audience"})
        except jwt.PyJWTError:
            return self._json(400, {"error": "invalid_grant"})
        # Expiry is checked against the fake's own clock, so tests can move time.
        if claims["exp"] <= self.clock() or claims["iat"] > self.clock() + 300:
            return self._json(400, {"error": "invalid_grant", "error_description": "expired"})
        if claims["iss"] != self.integration_key:
            return self._json(400, {"error": "invalid_grant", "error_description": "issuer_not_found"})
        if claims["sub"] != self.user_id:
            return self._json(400, {"error": "invalid_grant", "error_description": "user_not_found"})
        if "signature" not in claims.get("scope", "").split() or "impersonation" not in claims.get("scope", "").split():
            return self._json(400, {"error": "invalid_scope"})
        if not self.consented:
            return self._json(400, {"error": "consent_required"})
        token = pysecrets.token_urlsafe(24)
        self.tokens[token] = self.clock() + 3600
        return self._json(200, {"access_token": token, "token_type": "Bearer", "expires_in": 3600})

    def _create(self, d: dict) -> httpx.Response:
        docs = d.get("documents") or []
        if not docs:
            return self._error(400, "ENVELOPE_IS_INCOMPLETE")
        for doc in docs:
            raw = base64.b64decode(doc.get("documentBase64", ""))
            if not raw.startswith(b"%PDF") or doc.get("fileExtension") != "pdf" or not doc.get("documentId"):
                return self._error(400, "INVALID_DOCUMENT")
        signers = (d.get("recipients") or {}).get("signers") or []
        if not signers:
            return self._error(400, "ENVELOPE_IS_INCOMPLETE", "no recipients")
        for s in signers:
            if "@" not in s.get("email", "") or not s.get("name") or not s.get("recipientId"):
                return self._error(400, "INVALID_RECIPIENT")
            sign_tabs = (s.get("tabs") or {}).get("signHereTabs") or []
            if not sign_tabs or not all(t.get("anchorString") for t in sign_tabs):
                return self._error(400, "SIGNER_HAS_NO_SIGNATURE_TAB")
        if d.get("status") not in ("sent", "created"):
            return self._error(400, "INVALID_STATUS")
        envelope_id = str(uuid.uuid4())
        env = {"envelopeId": envelope_id, "status": d["status"], "emailSubject": d.get("emailSubject"),
               "customFields": d.get("customFields") or {}, "recipients": {"signers": signers},
               "eventNotification": d.get("eventNotification"), "documents": docs,
               "createdDateTime": time.strftime("%Y-%m-%dT%H:%M:%S.0000000Z", time.gmtime())}
        self.envelopes[envelope_id] = env
        return self._json(201, {"envelopeId": envelope_id, "status": d["status"],
                                "statusDateTime": env["createdDateTime"], "uri": f"/envelopes/{envelope_id}"})

    def _list(self, request: httpx.Request) -> httpx.Response:
        params = dict(request.url.params)
        if not params.get("from_date"):
            return self._error(400, "INVALID_REQUEST_PARAMETER", "from_date required")
        out = list(self.envelopes.values())
        if params.get("custom_field"):
            name, _, value = params["custom_field"].partition("=")
            out = [e for e in out if any(f.get("name") == name and f.get("value") == value
                                         for f in e["customFields"].get("textCustomFields", []))]
        return self._json(200, {"envelopes": [self._public(e) for e in out], "resultSetSize": str(len(out))})

    def _public(self, env: dict) -> dict:
        return {k: v for k, v in env.items() if k not in ("documents", "eventNotification")}

    def _view(self, env: dict, body: dict) -> httpx.Response:
        if env["status"] not in ("sent", "delivered"):
            return self._error(400, "ENVELOPE_INVALID_STATUS")
        signer = next((s for s in env["recipients"]["signers"]
                       if s.get("clientUserId") and s["clientUserId"] == body.get("clientUserId")), None)
        if not signer or signer["email"] != body.get("email") or signer["name"] != body.get("userName"):
            return self._error(400, "UNKNOWN_ENVELOPE_RECIPIENT")
        if not body.get("returnUrl") or not body.get("authenticationMethod"):
            return self._error(400, "INVALID_REQUEST_PARAMETER")
        self.views.append(body)
        return self._json(201, {"url": f"https://demo.docusign.net/Signing/MTRedeem/v1/{uuid.uuid4()}?slt=fake"})

    # --- simulate the signer and build Connect payloads ---------------------------------
    def set_status(self, envelope_id: str, status: str, reason: str = "") -> None:
        env = self.envelopes[envelope_id]
        env["status"] = status
        if status == "completed":
            env["completedDateTime"] = "2026-11-17T17:01:02.5370000Z"
        if status == "declined":
            env["recipients"]["signers"][0]["declinedReason"] = reason
        if status == "voided":
            env["voidedReason"] = reason

    def connect_payload(self, envelope_id: str, event: str, generated: str | None = None) -> dict:
        env = self.envelopes[envelope_id]
        return {"event": event, "apiVersion": "v2.1",
                "uri": f"/restapi/v2.1/accounts/{self.account_id}/envelopes/{envelope_id}",
                "retryCount": 0, "configurationId": 10000001,
                "generatedDateTime": generated or "2026-11-17T17:01:03.1234567Z",
                "data": {"accountId": self.account_id, "userId": self.user_id, "envelopeId": envelope_id,
                         "envelopeSummary": self._public(env)}}

    @staticmethod
    def signed_body(payload: dict, hmac_key: str) -> tuple[bytes, dict]:
        body = json.dumps(payload).encode()
        return body, {"X-DocuSign-Signature-1": docusign_sign(body, hmac_key), "Content-Type": "application/json"}
