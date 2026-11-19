"""Webhook signature checks. Every check runs on the RAW body, before any JSON parsing,
with a constant-time comparison, and anything unsigned or malformed is rejected.

- DocuSign Connect: `X-DocuSign-Signature-1..N`, each a base64 HMAC-SHA256 of the whole
  body with one of the account's Connect HMAC keys (up to 100 keys; one match is enough).
- HubSpot (v3): `X-HubSpot-Signature-v3` = base64 HMAC-SHA256 of
  method + full URI + body + `X-HubSpot-Request-Timestamp`, keyed with the app's client
  secret; requests older than 5 minutes are refused (replay protection).
- GoHighLevel and Zoho workflow webhooks: neither can compute an HMAC itself, but both let
  you add a custom header, so they carry a long random shared token in `X-Webhook-Token`.
  If something in the middle (n8n, Make) can sign, `X-Webhook-Signature: sha256=<hex>` of
  the body is accepted too.
"""

from __future__ import annotations

import base64
import hashlib
import hmac
import time

DOCUSIGN_HEADER_PREFIX = "x-docusign-signature-"
HUBSPOT_SIGNATURE = "X-HubSpot-Signature-v3"
HUBSPOT_TIMESTAMP = "X-HubSpot-Request-Timestamp"
TOKEN_HEADER = "X-Webhook-Token"
HMAC_HEADER = "X-Webhook-Signature"


def _b64_hmac_sha256(key: str, message: bytes) -> str:
    return base64.b64encode(hmac.new(key.encode("utf-8"), message, hashlib.sha256).digest()).decode("ascii")


# --- DocuSign Connect -------------------------------------------------------
def docusign_sign(body: bytes, key: str) -> str:
    return _b64_hmac_sha256(key, body)


def docusign_signatures(headers) -> list[str]:
    """All X-DocuSign-Signature-N values from a header mapping (any case)."""
    found = []
    for name, value in headers.items():
        if name.lower().startswith(DOCUSIGN_HEADER_PREFIX) and value:
            found.append(value.strip())
    return found


def verify_docusign(body: bytes, headers, keys: list[str]) -> bool:
    keys = [k for k in keys if k]
    given = docusign_signatures(headers)
    if not keys or not given:
        return False
    for key in keys:
        expected = docusign_sign(body, key)
        for sig in given:
            if hmac.compare_digest(expected.encode("ascii"), sig.encode("ascii", "replace")):
                return True
    return False


# --- HubSpot v3 -------------------------------------------------------------
def hubspot_sign(method: str, uri: str, body: bytes, timestamp: str, client_secret: str) -> str:
    message = method.upper().encode("utf-8") + uri.encode("utf-8") + body + timestamp.encode("utf-8")
    return _b64_hmac_sha256(client_secret, message)


def verify_hubspot(method: str, uri: str, body: bytes, signature: str | None, timestamp: str | None,
                   client_secret: str, max_age_seconds: int = 300, now=time.time) -> bool:
    if not client_secret or not signature or not timestamp:
        return False
    try:
        ts_ms = int(timestamp)
    except ValueError:
        return False
    if abs(now() * 1000 - ts_ms) > max_age_seconds * 1000:
        return False
    expected = hubspot_sign(method, uri, body, timestamp, client_secret)
    return hmac.compare_digest(expected.encode("ascii"), signature.strip().encode("ascii", "replace"))


# --- Shared token / HMAC for GHL and Zoho workflow webhooks -----------------
def generic_sign(body: bytes, secret: str) -> str:
    return "sha256=" + hmac.new(secret.encode("utf-8"), body, hashlib.sha256).hexdigest()


def verify_shared(body: bytes, token: str | None, signature: str | None, secret: str) -> bool:
    if not secret:
        return False
    if len(secret) < 24:
        # A short shared token is guessable; refuse to run with one.
        return False
    if signature:
        sig = signature.strip()
        return hmac.compare_digest(generic_sign(body, secret).encode("ascii"), sig.encode("ascii", "replace"))
    if token:
        return hmac.compare_digest(secret.encode("utf-8"), token.strip().encode("utf-8"))
    return False
