import pytest

from esign_crm.heartbeat import Heartbeat


@pytest.fixture
def sent():
    return []


@pytest.fixture
def hb(tmp_path, sent):
    return Heartbeat("DocuSign CRM sender", ping_url="", alert=lambda *a: sent.append(a) or True,
                     state_dir=tmp_path, min_items=0)


def test_reconcile_failure_alerts_and_reraises(hb, sent):
    def boom():
        raise RuntimeError("DocuSign token request failed: 400 consent_required")
    with pytest.raises(RuntimeError):
        hb.run(boom)
    assert sent[0][1] == "failure" and "consent_required" in sent[0][2]


def test_failure_alert_once_per_cooldown(hb, sent):
    for _ in range(3):
        with pytest.raises(ValueError):
            hb.run(lambda: (_ for _ in ()).throw(ValueError("x")))
    assert len(sent) == 1


def test_zero_open_envelopes_is_a_success(hb, sent):
    assert hb.run(lambda: 0) == 0
    assert sent == [] and hb.last_success() is not None


def test_missed_heartbeat(hb, sent):
    assert hb.check_missed(3600) is True          # never ran
    assert sent[-1][1] == "missed"
    hb.run(lambda: 0)
    assert hb.check_missed(3600) is False


def test_alert_channel_error_never_raises(tmp_path):
    def broken(*a):
        raise OSError("smtp down")
    h = Heartbeat("x", ping_url="", alert=broken, state_dir=tmp_path)
    assert h.alert("failure", "r") is False
