import datetime as dt
from pathlib import Path

import pytest
from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric import rsa

from esign_crm.config import Settings
from esign_crm.crm import GHLAdapter, HubSpotAdapter, ZohoAdapter
from esign_crm.docusign_api import Account, DocuSignClient, JWTAuth
from esign_crm.fake_crm import FakeGHL, FakeHubSpot, FakeZoho
from esign_crm.fake_docusign import FakeDocuSign
from esign_crm.service import Sender
from esign_crm.store import Store

ROOT = Path(__file__).resolve().parent.parent
IKEY = "11111111-2222-3333-4444-555555555555"
USER = "aaaaaaaa-bbbb-cccc-dddd-eeeeeeeeeeee"
HMAC_KEY = "test-connect-hmac-key-0123456789"
NO_SLEEP = lambda s: None  # noqa: E731


@pytest.fixture(scope="session")
def keypair():
    key = rsa.generate_private_key(public_exponent=65537, key_size=2048)
    private = key.private_bytes(serialization.Encoding.PEM, serialization.PrivateFormat.PKCS8,
                                serialization.NoEncryption()).decode()
    public = key.public_key().public_bytes(serialization.Encoding.PEM,
                                           serialization.PublicFormat.SubjectPublicKeyInfo).decode()
    return private, public


class AlertSink:
    def __init__(self):
        self.alerts = []

    def __call__(self, kind, reason, detail=""):
        self.alerts.append((kind, reason, detail))
        return True


@pytest.fixture
def sink():
    return AlertSink()


@pytest.fixture
def fake_ds(keypair):
    return FakeDocuSign(IKEY, USER, keypair[1])


@pytest.fixture
def auth(fake_ds, keypair):
    return JWTAuth(IKEY, USER, keypair[0], "account-d.docusign.com", transport=fake_ds.transport())


@pytest.fixture
def ds(fake_ds, auth):
    return DocuSignClient(auth, Account(fake_ds.account_id, "https://demo.docusign.net"),
                          transport=fake_ds.transport(), sleep=NO_SLEEP)


@pytest.fixture
def fake_hs():
    f = FakeHubSpot()
    f.properties.update({"docusign_status", "docusign_envelope_id", "docusign_completed_at"})
    f.add_deal("101")
    return f


@pytest.fixture
def hubspot(fake_hs):
    return HubSpotAdapter("hs-token", transport=fake_hs.transport(), sleep=NO_SLEEP)


@pytest.fixture
def fake_ghl():
    f = FakeGHL()
    f.add_opp("opp1")
    return f


@pytest.fixture
def ghl(fake_ghl):
    return GHLAdapter("ghl-token", field_ids={"status": "f-status", "envelope": "f-env", "completed": "f-done"},
                      transport=fake_ghl.transport(), sleep=NO_SLEEP)


@pytest.fixture
def fake_zoho():
    f = FakeZoho()
    f.fields.update({"DocuSign_Status", "DocuSign_Envelope_ID", "DocuSign_Completed_At"})
    f.add_deal("9001")
    return f


@pytest.fixture
def zoho(fake_zoho):
    return ZohoAdapter("cid", "csecret", "zoho-refresh", transport=fake_zoho.transport(), sleep=NO_SLEEP)


@pytest.fixture
def settings(tmp_path):
    return Settings(storage_dir=str(tmp_path / "storage"), db_path=":memory:",
                    trigger_stages={"hubspot": ["contractsent"], "ghl": ["stage-contract"],
                                    "zoho": ["Proposal/Price Quote"]})


@pytest.fixture
def store():
    return Store(":memory:")


@pytest.fixture
def make_sender(settings, store, hubspot, ghl, zoho, ds, sink):
    def make(**overrides):
        for k, v in overrides.items():
            setattr(settings, k, v)
        return Sender(settings, store, {"hubspot": hubspot, "ghl": ghl, "zoho": zoho}, lambda: ds, alert=sink,
                      today=lambda: dt.date(2026, 11, 17), template_root=ROOT)
    return make


@pytest.fixture
def sender(make_sender):
    return make_sender()
