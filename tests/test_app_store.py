import json
import time

import pytest
from fastapi.testclient import TestClient

from esign_crm import signatures
from esign_crm.app import create_app
from esign_crm.fake_docusign import FakeDocuSign
from esign_crm.store import Store

from .conftest import HMAC_KEY

HS_SECRET = "hubspot-client-secret"
SHARED = "shared-webhook-secret-0123456789abcdef"
APPROVER = "approver-token-0123456789"
AUTH = {"Authorization": f"Bearer {APPROVER}"}


@pytest.fixture
def client(sender):
    app = create_app(sender, {"docusign_hmac_keys": [HMAC_KEY], "hubspot_client_secret": HS_SECRET,
                              "ghl_webhook_secret": SHARED, "zoho_webhook_secret": SHARED,
                              "approver_token": APPROVER})
    return TestClient(app, base_url="https://hooks.example.com")


def hubspot_post(client, events, secret=HS_SECRET, ts=None):
    body = json.dumps(events).encode()
    ts = ts or str(int(time.time() * 1000))
    uri = "https://hooks.example.com/webhooks/hubspot"
    sig = signatures.hubspot_sign("POST", uri, body, ts, secret)
    return client.post("/webhooks/hubspot", content=body, headers={
        "X-HubSpot-Signature-v3": sig, "X-HubSpot-Request-Timestamp": ts, "Content-Type": "application/json"})


STAGE_EVENT = [{"eventId": 77, "subscriptionType": "deal.propertyChange", "objectId": 101,
                "propertyName": "dealstage", "propertyValue": "contractsent", "occurredAt": 1}]


def _connect(client, fake: FakeDocuSign, envelope_id, event, key=HMAC_KEY, generated=None):
    body, headers = fake.signed_body(fake.connect_payload(envelope_id, event, generated), key)
    return client.post("/webhooks/docusign", content=body, headers=headers)


# --- app -------------------------------------------------------------------------------
def test_health(client):
    assert client.get("/health").json() == {"ok": True, "paused": False}


def test_hubspot_signed_event_creates_request(client, sender):
    r = hubspot_post(client, STAGE_EVENT)
    assert r.status_code == 200 and r.json()["results"] == ["pending_approval"]
    assert sender.store.list()[0].status == "pending_approval"


def test_hubspot_retry_is_duplicate_delivery(client):
    hubspot_post(client, STAGE_EVENT)
    assert hubspot_post(client, STAGE_EVENT).json()["results"] == ["duplicate_delivery"]


def test_hubspot_bad_signature(client, sender):
    assert hubspot_post(client, STAGE_EVENT, secret="wrong").status_code == 401
    assert sender.store.list() == []


def test_hubspot_old_timestamp_rejected(client):
    old = str(int((time.time() - 3600) * 1000))
    assert hubspot_post(client, STAGE_EVENT, ts=old).status_code == 401


def test_hubspot_crm_outage_lets_hubspot_retry(client, fake_hs, sender):
    for _ in range(6):
        fake_hs.fail_next("GET", "/deals/101", 503)
    assert hubspot_post(client, STAGE_EVENT).status_code == 502
    fake_hs.failures.clear()
    assert hubspot_post(client, STAGE_EVENT).json()["results"] == ["pending_approval"]


def test_paused_returns_503_and_allows_retry(client, sender):
    sender.s.paused = True
    assert hubspot_post(client, STAGE_EVENT).status_code == 503
    sender.s.paused = False
    assert hubspot_post(client, STAGE_EVENT).json()["results"] == ["pending_approval"]


def _shared_post(client, path, payload, form=False, sig=True, token=None):
    body = (("&".join(f"{k}={v}" for k, v in payload.items())).encode() if form else json.dumps(payload).encode())
    headers = {"Content-Type": "application/x-www-form-urlencoded" if form else "application/json"}
    if sig:
        headers[signatures.HMAC_HEADER] = signatures.generic_sign(body, SHARED)
    if token:
        headers[signatures.TOKEN_HEADER] = token
    return client.post(path, content=body, headers=headers)


def test_zoho_form_webhook_with_hmac(client):
    r = _shared_post(client, "/webhooks/zoho", {"deal_id": "9001", "stage": "Proposal/Price Quote"}, form=True)
    assert r.json()["results"] == ["pending_approval"]


def test_zoho_shared_token_accepted(client):
    r = _shared_post(client, "/webhooks/zoho", {"deal_id": "9001", "stage": "Proposal/Price Quote"},
                     sig=False, token=SHARED)
    assert r.status_code == 200


def test_zoho_without_signature_rejected(client):
    r = _shared_post(client, "/webhooks/zoho", {"deal_id": "9001", "stage": "x"}, sig=False)
    assert r.status_code == 401


def test_ghl_webhook(client):
    payload = {"type": "OpportunityStageUpdate", "id": "opp1", "pipelineStageId": "stage-contract", "webhookId": "w1"}
    assert _shared_post(client, "/webhooks/ghl", payload).json()["results"] == ["pending_approval"]


def test_unreadable_body_400(client):
    body = b"\xff\xfe not json"
    r = client.post("/webhooks/zoho", content=body, headers={signatures.HMAC_HEADER: signatures.generic_sign(body, SHARED)})
    assert r.status_code == 400


def test_body_too_large(client):
    body = b"x" * 1_000_001
    assert client.post("/webhooks/docusign", content=body).status_code == 413


def test_approval_api_needs_token(client):
    hubspot_post(client, STAGE_EVENT)
    assert client.get("/requests").status_code == 401
    assert client.post("/requests/1/approve", headers={"Authorization": "Bearer nope"}).status_code == 401


def test_approval_api_flow_and_connect(client, fake_ds, sender):
    hubspot_post(client, STAGE_EVENT)
    queue = client.get("/requests", headers=AUTH).json()
    assert queue[0]["status"] == "pending_approval" and queue[0]["deal_name"] == "Website rebuild"
    assert client.post("/requests/1/approve", headers=AUTH).json() == {"status": "sent"}
    eid = sender.store.get(1).envelope_id
    fake_ds.set_status(eid, "completed")
    r = _connect(client, fake_ds, eid, "envelope-completed")
    assert r.json() == {"status": "completed"}
    # DocuSign retries the same delivery: answered, not processed twice.
    assert _connect(client, fake_ds, eid, "envelope-completed").json() == {"status": "duplicate_delivery"}


def test_connect_bad_hmac(client, fake_ds, sender):
    hubspot_post(client, STAGE_EVENT)
    client.post("/requests/1/approve", headers=AUTH)
    eid = sender.store.get(1).envelope_id
    assert _connect(client, fake_ds, eid, "envelope-delivered", key="wrong-key").status_code == 401
    assert sender.store.get(1).status == "sent"


def test_connect_download_error_500_then_retry_succeeds(client, fake_ds, sender, sink):
    hubspot_post(client, STAGE_EVENT)
    client.post("/requests/1/approve", headers=AUTH)
    eid = sender.store.get(1).envelope_id
    fake_ds.set_status(eid, "completed")
    for _ in range(6):
        fake_ds.fail_next("GET", "/documents/combined", 503)
    assert _connect(client, fake_ds, eid, "envelope-completed").status_code == 500
    fake_ds.failures.clear()
    assert _connect(client, fake_ds, eid, "envelope-completed").json() == {"status": "completed"}
    assert any("Connect" in a[1] for a in sink.alerts)


def test_reject_and_void_over_api(client, fake_ds, sender):
    hubspot_post(client, STAGE_EVENT)
    assert client.post("/requests/1/reject", params={"reason": "price"}, headers=AUTH).json() == {"status": "rejected"}
    assert client.post("/requests/1/void", params={"reason": "x"}, headers=AUTH).status_code == 409


def test_large_amount_needs_confirm_over_api(client, sender):
    sender.s.max_amount = 100
    hubspot_post(client, STAGE_EVENT)
    assert client.post("/requests/1/approve", headers=AUTH).status_code == 409
    assert client.post("/requests/1/approve", params={"confirm_large": True}, headers=AUTH).json()["status"] == "sent"


def test_sign_redirect_embedded(client, sender):
    sender.s.signing_mode = "embedded"
    hubspot_post(client, STAGE_EVENT)
    client.post("/requests/1/approve", headers=AUTH)
    r = client.get("/requests/1/sign", headers=AUTH, follow_redirects=False)
    assert r.status_code == 302 and r.headers["location"].startswith("https://demo.docusign.net/")


def test_sign_redirect_refused_in_email_mode(client):
    hubspot_post(client, STAGE_EVENT)
    client.post("/requests/1/approve", headers=AUTH)
    assert client.get("/requests/1/sign", headers=AUTH, follow_redirects=False).status_code == 409


def test_signing_return_page_escapes_input(client):
    r = client.get("/signing-return", params={"event": "<script>alert(1)</script>"})
    assert "<script>" not in r.text and "Signing session ended" in r.text
    assert "is signed" in client.get("/signing-return", params={"event": "signing_complete"}).text


def test_missing_secret_disables_endpoint(sender):
    c = TestClient(create_app(sender, {}), base_url="https://hooks.example.com")
    assert hubspot_post(c, STAGE_EVENT).status_code == 401
    assert c.get("/requests", headers=AUTH).status_code == 401


# --- store -----------------------------------------------------------------------------
def test_store_first_delivery_and_forget():
    s = Store()
    assert s.first_delivery("k", "x") is True
    assert s.first_delivery("k", "x") is False
    s.forget_delivery("k")
    assert s.first_delivery("k", "x") is True


def test_store_request_key_unique():
    s = Store()
    assert s.create_request("key", "hubspot", "1", "st", {"deal": {}}) is not None
    assert s.create_request("key", "hubspot", "1", "st", {"deal": {}}) is None


def test_store_transition_is_compare_and_set():
    s = Store()
    r = s.create_request("key", "hubspot", "1", "st", {"deal": {}})
    assert s.transition(r.id, ("pending_approval",), "sending") is True
    assert s.transition(r.id, ("pending_approval",), "sending") is False


def test_store_rejects_unknown_fields():
    s = Store()
    r = s.create_request("key", "hubspot", "1", "st", {"deal": {}})
    with pytest.raises(ValueError):
        s.transition(r.id, ("pending_approval",), "x", status="hack")
    with pytest.raises(ValueError):
        s.update_fields(r.id, crm="other")


def test_store_audit_chain_detects_edit():
    s = Store()
    for i in range(3):
        s.audit("step", request_id=1, n=i)
    assert s.verify_audit() == (True, None)
    s.db.execute("UPDATE audit SET detail = '{\"n\": 99}' WHERE seq = 2")
    assert s.verify_audit() == (False, 2)


def test_store_audit_chain_detects_deletion():
    s = Store()
    for i in range(3):
        s.audit("step", n=i)
    s.db.execute("DELETE FROM audit WHERE seq = 2")
    assert s.verify_audit()[0] is False


def test_store_open_envelopes_and_sent_today():
    now = [1_000_000.0]
    s = Store(clock=lambda: now[0])
    r = s.create_request("key", "hubspot", "1", "st", {"deal": {}})
    s.transition(r.id, ("pending_approval",), "sent", envelope_id="e1")
    s.audit("envelope_sent", request_id=r.id)
    assert [x.envelope_id for x in s.open_envelopes()] == ["e1"]
    assert s.sent_today() == 1
    now[0] += 90_000
    assert s.sent_today() == 0


def test_store_on_disk(tmp_path):
    path = tmp_path / "sub" / "db.sqlite"
    s = Store(path)
    s.create_request("key", "zoho", "1", "st", {"deal": {}})
    assert Store(path).by_key("key").crm == "zoho"
