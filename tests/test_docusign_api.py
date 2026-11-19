import base64
import urllib.parse

import httpx
import jwt
import pytest

from esign_crm.docusign_api import (
    AmbiguousWrite,
    ConsentRequired,
    DocuSignError,
    JWTAuth,
    consent_url,
    make_assertion,
)

from .conftest import IKEY, USER


def test_assertion_claims(keypair):
    token = make_assertion(IKEY, USER, keypair[0], "account-d.docusign.com", now=1_000_000)
    claims = jwt.decode(token, keypair[1], algorithms=["RS256"], audience="account-d.docusign.com",
                        options={"verify_exp": False})
    assert claims["iss"] == IKEY and claims["sub"] == USER
    assert claims["aud"] == "account-d.docusign.com"   # no https://
    assert claims["scope"] == "signature impersonation"
    assert claims["exp"] - claims["iat"] == 3600


def test_assertion_header_rs256(keypair):
    header = jwt.get_unverified_header(make_assertion(IKEY, USER, keypair[0], "account-d.docusign.com"))
    assert header["alg"] == "RS256" and header["typ"] == "JWT"


def test_consent_url_shape():
    url = consent_url(IKEY, "https://example.com/cb")
    parsed = urllib.parse.urlparse(url)
    q = dict(urllib.parse.parse_qsl(parsed.query))
    assert parsed.netloc == "account-d.docusign.com" and parsed.path == "/oauth/auth"
    assert q == {"response_type": "code", "scope": "signature impersonation", "client_id": IKEY,
                 "redirect_uri": "https://example.com/cb"}
    assert "%20" in url   # space encoded as %20, as in DocuSign's docs


def test_token_obtained_and_cached(auth, fake_ds):
    t1 = auth.token()
    t2 = auth.token()
    assert t1 == t2 and auth.token_requests == 1


def test_token_renewed_near_expiry(fake_ds, keypair):
    now = [1000.0]
    fake_ds.clock = lambda: now[0]
    a = JWTAuth(IKEY, USER, keypair[0], "account-d.docusign.com", transport=fake_ds.transport(), clock=lambda: now[0])
    a.token()
    now[0] += 3600 - 60
    a.token()
    assert a.token_requests == 2


def test_consent_required_raises_clear_error(fake_ds, auth):
    fake_ds.consented = False
    with pytest.raises(ConsentRequired, match="consent-url"):
        auth.token()


def test_wrong_integration_key_rejected(fake_ds, keypair):
    a = JWTAuth("00000000-0000-0000-0000-000000000000", USER, keypair[0], "account-d.docusign.com",
                transport=fake_ds.transport())
    with pytest.raises(DocuSignError) as e:
        a.token()
    assert e.value.code == "invalid_grant"


def test_wrong_private_key_rejected(fake_ds):
    from cryptography.hazmat.primitives import serialization
    from cryptography.hazmat.primitives.asymmetric import rsa
    other = rsa.generate_private_key(public_exponent=65537, key_size=2048).private_bytes(
        serialization.Encoding.PEM, serialization.PrivateFormat.PKCS8, serialization.NoEncryption()).decode()
    a = JWTAuth(IKEY, USER, other, "account-d.docusign.com", transport=fake_ds.transport())
    with pytest.raises(DocuSignError):
        a.token()


def test_wrong_audience_rejected(fake_ds, keypair):
    a = JWTAuth(IKEY, USER, keypair[0], "https://account-d.docusign.com", transport=fake_ds.transport())
    a.oauth_host = "account-d.docusign.com"
    # Build an assertion with the https:// audience by hand to prove the fake (like DocuSign) refuses it.
    bad = make_assertion(IKEY, USER, keypair[0], "https://account-d.docusign.com")
    resp = httpx.Client(transport=fake_ds.transport()).post(
        "https://account-d.docusign.com/oauth/token",
        data={"grant_type": "urn:ietf:params:oauth:grant-type:jwt-bearer", "assertion": bad})
    assert resp.status_code == 400


def test_account_default_chosen(auth, fake_ds):
    acct = auth.account()
    assert acct.account_id == fake_ds.account_id and acct.base_uri == "https://demo.docusign.net"


def test_account_pinned_must_exist(auth):
    with pytest.raises(DocuSignError, match="not one of"):
        auth.account("not-my-account")


def _definition():
    pdf = b"%PDF-1.4 test"
    return {"emailSubject": "s", "documents": [{"documentBase64": base64.b64encode(pdf).decode(), "name": "C",
                                                "fileExtension": "pdf", "documentId": "1"}],
            "recipients": {"signers": [{"email": "a@example.com", "name": "A", "recipientId": "1",
                                        "routingOrder": "1", "clientUserId": "u1",
                                        "tabs": {"signHereTabs": [{"anchorString": "/sig_client/"}]}}]},
            "customFields": {"textCustomFields": [{"name": "crm_request_key", "value": "k1"}]},
            "status": "sent"}


def test_create_and_get_envelope(ds):
    env = ds.create_envelope(_definition())
    assert ds.get_envelope(env["envelopeId"])["status"] == "sent"


def test_api_path_uses_v21_and_account(ds, fake_ds):
    ds.create_envelope(_definition())
    assert fake_ds.requests[-1] == ("POST", f"/restapi/v2.1/accounts/{fake_ds.account_id}/envelopes")


def test_create_rejects_envelope_without_signature_tab(ds):
    d = _definition()
    d["recipients"]["signers"][0]["tabs"] = {}
    with pytest.raises(DocuSignError) as e:
        ds.create_envelope(d)
    assert e.value.code == "SIGNER_HAS_NO_SIGNATURE_TAB"


def test_find_envelopes_by_custom_field(ds):
    env = ds.create_envelope(_definition())
    found = ds.find_envelopes("crm_request_key", "k1", "2026-11-01")
    assert [e["envelopeId"] for e in found] == [env["envelopeId"]]
    assert ds.find_envelopes("crm_request_key", "other", "2026-11-01") == []


def test_read_retried_on_429_and_5xx(ds, fake_ds):
    env = ds.create_envelope(_definition())
    fake_ds.fail_next("GET", "/envelopes/", 429)
    fake_ds.fail_next("GET", "/envelopes/", 503)
    assert ds.get_envelope(env["envelopeId"])["envelopeId"] == env["envelopeId"]


def test_read_retried_on_timeout(ds, fake_ds):
    env = ds.create_envelope(_definition())
    fake_ds.fail_next("GET", "/envelopes/", "timeout")
    assert ds.get_envelope(env["envelopeId"])["status"] == "sent"


def test_read_gives_up_after_retries(ds, fake_ds):
    for _ in range(6):
        fake_ds.fail_next("GET", "/envelopes", 500)
    with pytest.raises(DocuSignError):
        ds.find_envelopes("crm_request_key", "x", "2026-11-01")


def test_create_retried_on_429(ds, fake_ds):
    fake_ds.fail_next("POST", "/envelopes", 429)
    ds.create_envelope(_definition())
    assert len(fake_ds.envelopes) == 1


def test_create_not_retried_on_5xx(ds, fake_ds):
    fake_ds.fail_next("POST", "/envelopes", 500)
    with pytest.raises(AmbiguousWrite):
        ds.create_envelope(_definition())
    assert len(fake_ds.envelopes) == 0


def test_create_timeout_is_ambiguous(ds, fake_ds):
    fake_ds.fail_next("POST", "/envelopes", "timeout")
    with pytest.raises(AmbiguousWrite):
        ds.create_envelope(_definition())


def test_401_refreshes_token_once(ds, fake_ds):
    ds.auth.token()
    fake_ds.tokens.clear()   # token revoked server-side
    env = ds.create_envelope(_definition())
    assert env["envelopeId"] and ds.auth.token_requests == 2


def test_recipient_view_embedded(ds, fake_ds):
    env = ds.create_envelope(_definition())
    url = ds.recipient_view(env["envelopeId"], email="a@example.com", user_name="A", client_user_id="u1",
                            return_url="https://x.example.com/back")
    assert url.startswith("https://demo.docusign.net/Signing/")
    assert fake_ds.views[-1]["authenticationMethod"] == "none"


def test_recipient_view_wrong_client_user_id(ds):
    env = ds.create_envelope(_definition())
    with pytest.raises(DocuSignError) as e:
        ds.recipient_view(env["envelopeId"], email="a@example.com", user_name="A", client_user_id="other",
                          return_url="https://x.example.com/back")
    assert e.value.code == "UNKNOWN_ENVELOPE_RECIPIENT"


def test_void_and_void_twice(ds):
    env = ds.create_envelope(_definition())
    ds.void_envelope(env["envelopeId"], "wrong price")
    assert ds.get_envelope(env["envelopeId"])["status"] == "voided"
    with pytest.raises(DocuSignError):
        ds.void_envelope(env["envelopeId"], "again")


def test_download_combined_and_certificate(ds, fake_ds):
    env = ds.create_envelope(_definition())
    fake_ds.set_status(env["envelopeId"], "completed")
    assert ds.download(env["envelopeId"], "combined").startswith(b"%PDF")
    assert b"certificate" in ds.download(env["envelopeId"], "certificate")


def test_download_rejects_unknown_kind(ds):
    with pytest.raises(ValueError):
        ds.download("x", "everything")


def test_error_message_has_code_not_token(ds, fake_ds):
    with pytest.raises(DocuSignError) as e:
        ds.get_envelope("missing")
    assert "ENVELOPE_DOES_NOT_EXIST" in str(e.value)
    assert ds.auth.token() not in str(e.value)


def test_calls_logged_without_bodies(ds):
    ds.create_envelope(_definition())
    assert ds.calls[-1] == ("POST", "/envelopes", 201)
