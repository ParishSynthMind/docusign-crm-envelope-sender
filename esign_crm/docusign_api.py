"""DocuSign eSignature REST API v2.1 client, with the JWT grant for authentication.

JWT grant (server-to-server, no browser after the one-time consent):
  a JWT signed RS256 with the app's RSA private key (iss = integration key, sub = the
  user id GUID, aud = account-d.docusign.com for demo, scope "signature impersonation")
  is swapped at https://<oauth host>/oauth/token for a 1-hour access token. No refresh
  token exists; we simply make a new JWT when the token is about to expire.
  /oauth/userinfo gives the account id and its base_uri (never hard-coded).

Retries: reads are retried on 429, 5xx and timeouts with backoff. Creating an envelope is
only retried on 429 (the request was refused, so nothing was created). After a 5xx or a
timeout the envelope MAY exist, so the caller looks it up by its custom field instead of
blindly creating a second one.
"""

from __future__ import annotations

import base64
import time
import urllib.parse
from dataclasses import dataclass

import httpx
import jwt

from .config import DOCUSIGN_API_VERSION

SCOPES = "signature impersonation"


class DocuSignError(RuntimeError):
    def __init__(self, message: str, status: int | None = None, code: str | None = None):
        super().__init__(message)
        self.status = status
        self.code = code


class ConsentRequired(DocuSignError):
    """The user has not granted (or has revoked) consent for this integration key."""


class AmbiguousWrite(DocuSignError):
    """A write failed in a way where it may or may not have been applied (5xx, timeout)."""


def consent_url(integration_key: str, redirect_uri: str, oauth_host: str = "account-d.docusign.com") -> str:
    query = urllib.parse.urlencode({
        "response_type": "code", "scope": SCOPES, "client_id": integration_key, "redirect_uri": redirect_uri,
    }, quote_via=urllib.parse.quote)
    return f"https://{oauth_host}/oauth/auth?{query}"


def make_assertion(integration_key: str, user_id: str, private_key_pem: str, oauth_host: str,
                   now: float | None = None, lifetime: int = 3600) -> str:
    iat = int(now if now is not None else time.time())
    claims = {"iss": integration_key, "sub": user_id, "aud": oauth_host, "iat": iat, "exp": iat + lifetime,
              "scope": SCOPES}
    return jwt.encode(claims, private_key_pem, algorithm="RS256", headers={"typ": "JWT"})


@dataclass
class Account:
    account_id: str
    base_uri: str
    account_name: str = ""


class JWTAuth:
    """Gets and caches access tokens with the JWT grant."""

    def __init__(self, integration_key: str, user_id: str, private_key_pem: str, oauth_host: str,
                 transport: httpx.BaseTransport | None = None, clock=time.time, timeout: float = 30.0):
        self.integration_key = integration_key
        self.user_id = user_id
        self.private_key_pem = private_key_pem
        self.oauth_host = oauth_host
        self.clock = clock
        self.http = httpx.Client(transport=transport, timeout=timeout)
        self._token: str | None = None
        self._expires_at = 0.0
        self.token_requests = 0

    def token(self) -> str:
        if self._token and self.clock() < self._expires_at - 120:
            return self._token
        assertion = make_assertion(self.integration_key, self.user_id, self.private_key_pem, self.oauth_host,
                                   now=self.clock())
        resp = self.http.post(f"https://{self.oauth_host}/oauth/token", data={
            "grant_type": "urn:ietf:params:oauth:grant-type:jwt-bearer", "assertion": assertion})
        self.token_requests += 1
        if resp.status_code != 200:
            try:
                body = resp.json()
            except ValueError:
                body = {}
            error = body.get("error", "")
            if error == "consent_required":
                raise ConsentRequired(
                    "consent_required: open the consent URL once (esign-crm consent-url) and click Allow",
                    resp.status_code, error)
            raise DocuSignError(f"token request failed: {resp.status_code} {error or resp.text[:200]}",
                                resp.status_code, error)
        body = resp.json()
        self._token = body["access_token"]
        self._expires_at = self.clock() + int(body.get("expires_in", 3600))
        return self._token

    def invalidate(self) -> None:
        self._token = None
        self._expires_at = 0.0

    def userinfo(self) -> dict:
        resp = self.http.get(f"https://{self.oauth_host}/oauth/userinfo",
                             headers={"Authorization": f"Bearer {self.token()}"})
        if resp.status_code != 200:
            raise DocuSignError(f"userinfo failed: {resp.status_code}", resp.status_code)
        return resp.json()

    def account(self, account_id: str | None = None) -> Account:
        info = self.userinfo()
        accounts = info.get("accounts") or []
        if not accounts:
            raise DocuSignError("this user has no DocuSign accounts")
        if account_id:
            match = [a for a in accounts if a.get("account_id") == account_id]
            if not match:
                raise DocuSignError("the configured account id is not one of this user's accounts")
            chosen = match[0]
        else:
            chosen = next((a for a in accounts if a.get("is_default")), accounts[0])
        return Account(chosen["account_id"], chosen["base_uri"].rstrip("/"), chosen.get("account_name", ""))


class DocuSignClient:
    def __init__(self, auth: JWTAuth, account: Account, transport: httpx.BaseTransport | None = None,
                 timeout: float = 30.0, max_retries: int = 4, sleep=time.sleep):
        self.auth = auth
        self.account = account
        self.http = httpx.Client(transport=transport, timeout=timeout)
        self.max_retries = max_retries
        self.sleep = sleep
        self.calls: list[tuple[str, str, int]] = []   # (method, path, status) for the run log; no bodies

    @property
    def base(self) -> str:
        return f"{self.account.base_uri}/restapi/{DOCUSIGN_API_VERSION}/accounts/{self.account.account_id}"

    def _request(self, method: str, path: str, *, json_body=None, params=None, write: bool = False,
                 accept: str = "application/json") -> httpx.Response:
        url = self.base + path
        attempt = 0
        refreshed = False
        while True:
            attempt += 1
            headers = {"Authorization": f"Bearer {self.auth.token()}", "Accept": accept}
            try:
                resp = self.http.request(method, url, json=json_body, params=params, headers=headers)
            except httpx.TimeoutException as e:
                self.calls.append((method, path, 0))
                if write:
                    raise AmbiguousWrite(f"{method} {path} timed out; it may have been applied") from e
                if attempt > self.max_retries:
                    raise DocuSignError(f"{method} {path} timed out {attempt} times") from e
                self.sleep(min(2 ** attempt, 30))
                continue
            except httpx.TransportError as e:
                self.calls.append((method, path, 0))
                if write or attempt > self.max_retries:
                    raise (AmbiguousWrite if write else DocuSignError)(f"{method} {path}: {e}") from e
                self.sleep(min(2 ** attempt, 30))
                continue
            self.calls.append((method, path, resp.status_code))
            if resp.status_code == 401 and not refreshed:
                refreshed = True
                self.auth.invalidate()
                continue
            if resp.status_code == 429 and attempt <= self.max_retries:
                reset = resp.headers.get("X-RateLimit-Reset")
                wait = 2 ** attempt
                if reset and reset.isdigit():
                    wait = max(1, min(int(reset) - int(time.time()), 60))
                self.sleep(wait)
                continue
            if resp.status_code >= 500:
                if write:
                    raise AmbiguousWrite(f"{method} {path} returned {resp.status_code}; it may have been applied",
                                         resp.status_code)
                if attempt <= self.max_retries:
                    self.sleep(min(2 ** attempt, 30))
                    continue
            if resp.status_code >= 400:
                code = ""
                try:
                    body = resp.json()
                    code = body.get("errorCode", "")
                    msg = body.get("message", "")
                except ValueError:
                    msg = resp.text[:200]
                raise DocuSignError(f"{method} {path} failed: {resp.status_code} {code} {msg}".strip(),
                                    resp.status_code, code)
            return resp

    # --- envelopes ------------------------------------------------------------
    def create_envelope(self, definition: dict) -> dict:
        return self._request("POST", "/envelopes", json_body=definition, write=True).json()

    def get_envelope(self, envelope_id: str) -> dict:
        return self._request("GET", f"/envelopes/{envelope_id}").json()

    def find_envelopes(self, field_name: str, value: str, from_date: str) -> list[dict]:
        """Envelopes whose text custom field `field_name` equals `value` (the idempotency lookup)."""
        resp = self._request("GET", "/envelopes", params={
            "from_date": from_date, "custom_field": f"{field_name}={value}", "include": "custom_fields"})
        return resp.json().get("envelopes") or []

    def recipient_view(self, envelope_id: str, *, email: str, user_name: str, client_user_id: str,
                       return_url: str) -> str:
        body = {"returnUrl": return_url, "authenticationMethod": "none", "email": email, "userName": user_name,
                "clientUserId": client_user_id}
        return self._request("POST", f"/envelopes/{envelope_id}/views/recipient", json_body=body).json()["url"]

    def void_envelope(self, envelope_id: str, reason: str) -> dict:
        return self._request("PUT", f"/envelopes/{envelope_id}",
                             json_body={"status": "voided", "voidedReason": reason[:200]}, write=True).json()

    def download(self, envelope_id: str, document: str = "combined") -> bytes:
        """`combined` = every document as one PDF; `certificate` = the certificate of completion."""
        if document not in ("combined", "certificate", "archive"):
            raise ValueError("document must be combined, certificate or archive")
        resp = self._request("GET", f"/envelopes/{envelope_id}/documents/{document}", accept="application/pdf")
        data = resp.content
        if resp.headers.get("Content-Transfer-Encoding", "").lower() == "base64":
            data = base64.b64decode(data)
        return data
