import pytest

from esign_crm import cli, config


@pytest.fixture
def env(tmp_path, monkeypatch):
    cfg = tmp_path / "sender.yaml"
    cfg.write_text(f"crm: zoho\nstorage_dir: {tmp_path / 'st'}\ndb_path: {tmp_path / 'db.sqlite'}\n")
    monkeypatch.setenv("ESIGN_CONFIG", str(cfg))
    for name in ("DOCUSIGN_INTEGRATION_KEY", "ZOHO_CLIENT_ID", "ZOHO_CLIENT_SECRET", "ZOHO_REFRESH_TOKEN"):
        monkeypatch.setenv(name, "x-" + name.lower())
    monkeypatch.setattr(config, "keychain", lambda item: None)
    return tmp_path


def test_consent_url(env, capsys):
    assert cli.main(["consent-url"]) == 0
    out = capsys.readouterr().out
    assert out.startswith("https://account-d.docusign.com/oauth/auth?") and "signature%20impersonation" in out


def test_list_empty_and_audit_verify(env, capsys):
    assert cli.main(["list"]) == 0
    assert capsys.readouterr().out.strip() == "[]"
    assert cli.main(["audit-verify"]) == 0
    assert "intact" in capsys.readouterr().out


def test_show_missing(env, capsys):
    assert cli.main(["show", "5"]) == 1


def test_approve_missing_request_is_not_done(env, capsys):
    assert cli.main(["approve", "5"]) == 1
    assert "not done" in capsys.readouterr().err


def test_build_crms_needs_secrets(env, monkeypatch):
    monkeypatch.delenv("ZOHO_REFRESH_TOKEN")
    s = config.load_settings()
    with pytest.raises(RuntimeError) as e:
        cli.build_crms(s, secret=lambda n, required=True: config.secret(n, required, keychain_fn=lambda i: None))
    assert "zoho_refresh_token" in str(e.value) and "x-" not in str(e.value)
