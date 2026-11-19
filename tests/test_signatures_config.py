import base64
import hashlib
import hmac

import pytest

from esign_crm import config, signatures
from esign_crm.config import Settings, decode_private_key, load_settings

KEY = "connect-key-abcdefghijklmnopqrstuvwxyz"
SHARED = "a-very-long-shared-token-0123456789"


# --- DocuSign Connect ---------------------------------------------------------
def test_docusign_signature_is_base64_hmac_sha256_of_raw_body():
    body = b'{"event":"envelope-completed"}\r\n'
    expected = base64.b64encode(hmac.new(KEY.encode(), body, hashlib.sha256).digest()).decode()
    assert signatures.docusign_sign(body, KEY) == expected


def test_docusign_verify_accepts_matching_signature():
    body = b"{}"
    assert signatures.verify_docusign(body, {"X-DocuSign-Signature-1": signatures.docusign_sign(body, KEY)}, [KEY])


def test_docusign_verify_header_case_insensitive():
    body = b"{}"
    assert signatures.verify_docusign(body, {"x-docusign-signature-1": signatures.docusign_sign(body, KEY)}, [KEY])


def test_docusign_verify_rejects_tampered_body():
    sig = signatures.docusign_sign(b'{"a":1}', KEY)
    assert not signatures.verify_docusign(b'{"a":2}', {"X-DocuSign-Signature-1": sig}, [KEY])


def test_docusign_verify_rejects_missing_header():
    assert not signatures.verify_docusign(b"{}", {}, [KEY])


def test_docusign_verify_rejects_when_no_keys_configured():
    body = b"{}"
    assert not signatures.verify_docusign(body, {"X-DocuSign-Signature-1": signatures.docusign_sign(body, KEY)}, [])


def test_docusign_verify_any_of_several_keys_and_headers():
    body = b"{}"
    other = "rotated-key-zyxwvutsrqponmlkjihgfedcba"
    headers = {"X-DocuSign-Signature-1": signatures.docusign_sign(body, "unknown-key-xxxxxxxxxxxxxxxx"),
               "X-DocuSign-Signature-2": signatures.docusign_sign(body, other)}
    assert signatures.verify_docusign(body, headers, [KEY, other])


def test_docusign_verify_rejects_garbage_signature():
    assert not signatures.verify_docusign(b"{}", {"X-DocuSign-Signature-1": "not base64 \u00e9"}, [KEY])


# --- HubSpot v3 -----------------------------------------------------------------
def _hs(body=b'[{"a":1}]', ts=None, uri="https://hooks.example.com/webhooks/hubspot", secret="hs-secret"):
    ts = ts or "1763380000000"
    return signatures.hubspot_sign("POST", uri, body, ts, secret), ts


def test_hubspot_v3_signature_matches_documented_recipe():
    body, uri, ts = b"[]", "https://x.example.com/w", "1763380000000"
    expected = base64.b64encode(hmac.new(b"s", b"POST" + uri.encode() + body + ts.encode(), hashlib.sha256).digest()).decode()
    assert signatures.hubspot_sign("post", uri, body, ts, "s") == expected


def test_hubspot_verify_ok_within_window():
    sig, ts = _hs()
    assert signatures.verify_hubspot("POST", "https://hooks.example.com/webhooks/hubspot", b'[{"a":1}]', sig, ts,
                                     "hs-secret", now=lambda: 1763380000.0 + 60)


def test_hubspot_verify_rejects_old_timestamp():
    sig, ts = _hs()
    assert not signatures.verify_hubspot("POST", "https://hooks.example.com/webhooks/hubspot", b'[{"a":1}]', sig, ts,
                                         "hs-secret", now=lambda: 1763380000.0 + 301)


def test_hubspot_verify_rejects_future_timestamp():
    sig, ts = _hs()
    assert not signatures.verify_hubspot("POST", "https://hooks.example.com/webhooks/hubspot", b'[{"a":1}]', sig, ts,
                                         "hs-secret", now=lambda: 1763380000.0 - 400)


def test_hubspot_verify_rejects_other_uri():
    sig, ts = _hs()
    assert not signatures.verify_hubspot("POST", "https://evil.example.com/webhooks/hubspot", b'[{"a":1}]', sig, ts,
                                         "hs-secret", now=lambda: 1763380000.0)


def test_hubspot_verify_rejects_non_numeric_timestamp():
    sig, _ = _hs()
    assert not signatures.verify_hubspot("POST", "https://hooks.example.com/webhooks/hubspot", b'[{"a":1}]', sig,
                                         "yesterday", "hs-secret")


def test_hubspot_verify_rejects_missing_secret():
    sig, ts = _hs()
    assert not signatures.verify_hubspot("POST", "https://hooks.example.com/webhooks/hubspot", b'[{"a":1}]', sig, ts,
                                         "", now=lambda: 1763380000.0)


# --- shared token / HMAC ------------------------------------------------------------
def test_shared_token_ok():
    assert signatures.verify_shared(b"{}", SHARED, None, SHARED)


def test_shared_token_wrong():
    assert not signatures.verify_shared(b"{}", SHARED + "x", None, SHARED)


def test_shared_hmac_ok_and_tamper():
    sig = signatures.generic_sign(b'{"a":1}', SHARED)
    assert signatures.verify_shared(b'{"a":1}', None, sig, SHARED)
    assert not signatures.verify_shared(b'{"a":2}', None, sig, SHARED)


def test_shared_refuses_short_secret():
    assert not signatures.verify_shared(b"{}", "short", None, "short")


def test_shared_refuses_no_credentials():
    assert not signatures.verify_shared(b"{}", None, None, SHARED)


def test_shared_hmac_wins_over_token_when_both_sent():
    assert not signatures.verify_shared(b"{}", SHARED, "sha256=deadbeef", SHARED)


# --- settings ---------------------------------------------------------------------------
def test_default_settings_valid():
    Settings().validate()


def test_oauth_host_demo_and_production():
    assert Settings().oauth_host == "account-d.docusign.com"
    assert Settings(environment="production").oauth_host == "account.docusign.com"


@pytest.mark.parametrize("field,value", [("environment", "staging"), ("crm", "salesforce"),
                                         ("signing_mode", "fax"), ("max_envelopes_per_day", 0)])
def test_bad_settings_rejected(field, value):
    with pytest.raises(ValueError):
        Settings(**{field: value}).validate()


def test_countersigner_needs_both_fields():
    with pytest.raises(ValueError):
        Settings(countersigner_name="Pat").validate()


def test_load_settings_rejects_unknown_keys(tmp_path):
    p = tmp_path / "s.yaml"
    p.write_text("crm: hubspot\nsend_without_approval: true\n")
    with pytest.raises(ValueError, match="unknown settings"):
        load_settings(p)


def test_load_settings_env_overrides(tmp_path, monkeypatch):
    p = tmp_path / "s.yaml"
    p.write_text("crm: zoho\n")
    monkeypatch.setenv("ESIGN_PAUSED", "true")
    monkeypatch.setenv("ESIGN_DB", str(tmp_path / "x.db"))
    s = load_settings(p)
    assert s.paused and s.crm == "zoho" and s.db_path.endswith("x.db")


def test_repo_config_file_loads():
    s = load_settings(config.Path(__file__).resolve().parent.parent / "config" / "sender.yaml")
    assert s.environment == "demo" and s.paused is False


def test_stages_for():
    s = Settings(trigger_stages={"hubspot": ["a", 2]})
    assert s.stages_for("hubspot") == ["a", "2"] and s.stages_for("ghl") == []


def test_secret_from_env(monkeypatch):
    monkeypatch.setenv("APPROVER_TOKEN", "tok")
    assert config.secret("approver_token", keychain_fn=lambda item: None) == "tok"


def test_secret_from_keychain(monkeypatch):
    monkeypatch.delenv("APPROVER_TOKEN", raising=False)
    assert config.secret("approver_token", keychain_fn=lambda item: "kc-" + item) == "kc-esign-approver-token"


def test_missing_secret_error_names_place_not_value(monkeypatch):
    monkeypatch.delenv("DOCUSIGN_USER_ID", raising=False)
    with pytest.raises(RuntimeError, match="docusign-user-id"):
        config.secret("docusign_user_id", keychain_fn=lambda item: None)


def test_optional_secret_none(monkeypatch):
    monkeypatch.delenv("DOCUSIGN_ACCOUNT_ID", raising=False)
    assert config.secret("docusign_account_id", required=False, keychain_fn=lambda item: None) is None


def test_decode_private_key_forms(keypair):
    pem = keypair[0]
    assert decode_private_key(pem).startswith("-----BEGIN")
    assert decode_private_key(base64.b64encode(pem.encode()).decode()) == pem.strip()
    assert decode_private_key(pem.encode().hex()) == pem.strip()
    assert decode_private_key(pem.replace("\n", "\\n")).count("\n") > 5


def test_decode_private_key_rejects_junk():
    with pytest.raises(ValueError):
        decode_private_key("hello")
