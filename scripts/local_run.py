"""The whole flow with no account: fake HubSpot + fake DocuSign, the real app and service.

    python scripts/local_run.py

A signed HubSpot stage webhook -> contract PDF waiting for approval -> approve over the API ->
envelope created -> the fake signer signs -> a signed DocuSign Connect event -> signed PDF and
certificate stored -> status written back to the (fake) deal. Prints one JSON line per step and
writes the files under examples/local-run/.
"""

from __future__ import annotations

import datetime as dt
import json
import shutil
import sys
import time
from pathlib import Path

from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric import rsa
from fastapi.testclient import TestClient

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from esign_crm import signatures  # noqa: E402
from esign_crm.app import create_app  # noqa: E402
from esign_crm.config import Settings  # noqa: E402
from esign_crm.crm import HubSpotAdapter  # noqa: E402
from esign_crm.docusign_api import Account, DocuSignClient, JWTAuth  # noqa: E402
from esign_crm.fake_crm import FakeHubSpot  # noqa: E402
from esign_crm.fake_docusign import FakeDocuSign  # noqa: E402
from esign_crm.service import Sender  # noqa: E402
from esign_crm.store import Store  # noqa: E402

OUT = ROOT / "examples" / "local-run"


def main() -> int:
    if OUT.exists():
        shutil.rmtree(OUT)
    OUT.mkdir(parents=True)
    key = rsa.generate_private_key(public_exponent=65537, key_size=2048)
    private = key.private_bytes(serialization.Encoding.PEM, serialization.PrivateFormat.PKCS8,
                                serialization.NoEncryption()).decode()
    public = key.public_key().public_bytes(serialization.Encoding.PEM,
                                           serialization.PublicFormat.SubjectPublicKeyInfo).decode()
    ds_fake = FakeDocuSign("demo-integration-key", "demo-user", public)
    auth = JWTAuth("demo-integration-key", "demo-user", private, "account-d.docusign.com", transport=ds_fake.transport())
    ds = DocuSignClient(auth, Account(ds_fake.account_id, "https://demo.docusign.net"), transport=ds_fake.transport())
    hs_fake = FakeHubSpot()
    hs_fake.properties.update({"docusign_status", "docusign_envelope_id", "docusign_completed_at"})
    hs_fake.add_deal("101")
    hubspot = HubSpotAdapter("hs-token", transport=hs_fake.transport())
    settings = Settings(storage_dir=str(OUT / "storage"), db_path=":memory:", public_base_url="https://sign.example.com")
    sender = Sender(settings, Store(), {"hubspot": hubspot}, lambda: ds, today=lambda: dt.date(2026, 11, 19),
                    template_root=ROOT)
    secrets = {"hubspot_client_secret": "hs-secret", "docusign_hmac_keys": ["connect-key"], "approver_token": "tok"}
    client = TestClient(create_app(sender, secrets), base_url="https://sign.example.com")
    log = []

    def step(name, **kw):
        line = {"step": name, **kw}
        log.append(line)
        print(json.dumps(line))

    body = json.dumps([{"eventId": 1, "subscriptionType": "deal.propertyChange", "objectId": 101,
                        "propertyName": "dealstage", "propertyValue": "contractsent"}]).encode()
    ts = str(int(time.time() * 1000))
    sig = signatures.hubspot_sign("POST", "https://sign.example.com/webhooks/hubspot", body, ts, "hs-secret")
    r = client.post("/webhooks/hubspot", content=body,
                    headers={"X-HubSpot-Signature-v3": sig, "X-HubSpot-Request-Timestamp": ts})
    step("hubspot_webhook", http=r.status_code, result=r.json())
    r = client.post("/requests/1/approve", headers={"Authorization": "Bearer tok"})
    step("approve", http=r.status_code, result=r.json())
    envelope_id = sender.store.get(1).envelope_id
    ds_fake.set_status(envelope_id, "completed")
    body, headers = ds_fake.signed_body(ds_fake.connect_payload(envelope_id, "envelope-completed"), "connect-key")
    r = client.post("/webhooks/docusign", content=body, headers=headers)
    step("connect_completed", http=r.status_code, result=r.json())
    req = sender.store.get(1)
    step("stored", status=req.status, signed_sha256=req.signed_sha256, certificate_sha256=req.certificate_sha256)
    step("crm", deal=hs_fake.deals["101"].get("docusign_status"), notes=len(hs_fake.notes))
    step("audit", intact=sender.store.verify_audit()[0], rows=len(sender.store.audit_rows()))
    (OUT / "run.json").write_text(json.dumps(log, indent=2))
    return 0 if req.status == "completed" else 1


if __name__ == "__main__":
    sys.exit(main())
