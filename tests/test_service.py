import json

import pytest

from esign_crm.crm.base import StageEvent
from esign_crm.docusign_api import DocuSignError
from esign_crm.envelope import FIELD_REQUEST_KEY
from esign_crm.service import NotAllowed, Paused, mask

EV = StageEvent("hubspot", "101", "contractsent", "hubspot:1")


def _pending(sender):
    assert sender.handle_stage_event(EV) == "pending_approval"
    return sender.store.list()[0]


def _sent(sender):
    req = _pending(sender)
    assert sender.approve(req.id, "tester") == "sent"
    return sender.store.get(req.id)


# --- 1. stage event -> contract waiting for approval ------------------------------
def test_stage_event_generates_contract_and_waits(sender, fake_hs, fake_ds):
    req = _pending(sender)
    assert req.status == "pending_approval"
    assert open(req.pdf_path, "rb").read().startswith(b"%PDF")
    assert len(req.pdf_sha256) == 64
    assert fake_hs.deals["101"]["docusign_status"] == "pending_approval"
    assert "waiting for approval" in json.dumps(fake_hs.notes)
    assert fake_ds.envelopes == {}            # nothing sent before a person approves


def test_other_stage_ignored(sender, fake_ds):
    assert sender.handle_stage_event(StageEvent("hubspot", "101", "appointmentscheduled", "k")) == "ignored_stage"
    assert sender.store.list() == []


def test_same_deal_same_stage_is_one_contract(sender):
    _pending(sender)
    assert sender.handle_stage_event(StageEvent("hubspot", "101", "contractsent", "hubspot:2")) == "duplicate"
    assert len(sender.store.list()) == 1


def test_new_template_version_makes_new_request(make_sender):
    s = make_sender()
    _pending(s)
    s.s.template_version = "2026-12-01"
    assert s.handle_stage_event(EV) == "pending_approval"
    assert len(s.store.list()) == 2


def test_paused_refuses_stage_event(make_sender):
    with pytest.raises(Paused):
        make_sender(paused=True).handle_stage_event(EV)


def test_deal_without_email_fails_with_note_and_alert(sender, fake_hs, sink):
    fake_hs.contacts["c101"]["email"] = "not-an-email"
    assert sender.handle_stage_event(EV) == "failed"
    req = sender.store.list()[0]
    assert req.status == "failed" and "valid email" in req.reason
    assert fake_hs.deals["101"]["docusign_status"] == "failed"
    assert "NOT generated" in json.dumps(fake_hs.notes)
    assert sink.alerts and sink.alerts[0][0] == "failure"


def test_deal_without_amount_fails(sender, fake_hs):
    fake_hs.deals["101"]["amount"] = ""
    assert sender.handle_stage_event(EV) == "failed"
    assert "no amount" in sender.store.list()[0].reason


def test_deal_with_bad_amount_fails(sender, fake_hs):
    fake_hs.deals["101"]["amount"] = "lots"
    assert sender.handle_stage_event(EV) == "failed"


def test_check_deal_lists_every_problem(sender, hubspot):
    deal = hubspot.get_deal("101")
    deal.client_name, deal.client_email, deal.deal_name, deal.amount = "", "", "", "0"
    assert len(sender.check_deal(deal)) == 4


def test_retry_after_fixing_deal(sender, fake_hs):
    fake_hs.contacts["c101"]["email"] = "bad"
    sender.handle_stage_event(EV)
    req = sender.store.list()[0]
    fake_hs.contacts["c101"]["email"] = "dana.lee@example.com"
    assert sender.retry(req.id, "tester") == "pending_approval"
    assert [r.status for r in sender.store.list()] == ["pending_approval"]


def test_retry_only_for_failed(sender):
    req = _pending(sender)
    with pytest.raises(NotAllowed):
        sender.retry(req.id, "tester")


def test_crm_write_failure_does_not_stop_contract(sender, fake_hs, sink):
    fake_hs.properties.clear()                # status property missing -> 400 on PATCH
    assert sender.handle_stage_event(EV) == "pending_approval"
    assert any("could not write status" in a[1] for a in sink.alerts)
    actions = [r["action"] for r in sender.store.audit_rows()]
    assert "crm_update_failed" in actions


def test_large_amount_needs_confirmation(make_sender, fake_hs):
    s = make_sender(max_amount=1000)
    req = _pending(s)
    assert "confirm-large" in json.dumps(fake_hs.notes)
    with pytest.raises(NotAllowed):
        s.approve(req.id, "tester")
    assert s.approve(req.id, "tester", confirm_large=True) == "sent"


# --- 2. approval -> envelope -------------------------------------------------------
def test_approve_creates_envelope_with_request_key(sender, fake_ds, fake_hs):
    req = _sent(sender)
    assert req.status == "sent" and req.envelope_id in fake_ds.envelopes
    env = fake_ds.envelopes[req.envelope_id]
    fields = {f["name"]: f["value"] for f in env["customFields"]["textCustomFields"]}
    assert fields[FIELD_REQUEST_KEY] == req.request_key
    assert env["recipients"]["signers"][0]["email"] == "dana.lee@example.com"
    assert fake_hs.deals["101"]["docusign_status"] == "sent"
    assert fake_hs.deals["101"]["docusign_envelope_id"] == req.envelope_id


def test_approve_twice_sends_once(sender, fake_ds):
    req = _sent(sender)
    assert sender.approve(req.id, "tester") == "already_sent"
    assert len(fake_ds.envelopes) == 1


def test_approve_unknown_request(sender):
    with pytest.raises(NotAllowed):
        sender.approve(999, "tester")


def test_approve_paused(sender):
    req = _pending(sender)
    sender.s.paused = True
    with pytest.raises(Paused):
        sender.approve(req.id, "tester")


def test_reject_then_approve_does_nothing(sender, fake_ds, fake_hs):
    req = _pending(sender)
    assert sender.reject(req.id, "boss", "wrong price") == "rejected"
    assert sender.approve(req.id, "tester") == "already_rejected"
    assert fake_ds.envelopes == {}
    assert fake_hs.deals["101"]["docusign_status"] == "rejected"


def test_reject_twice(sender):
    req = _pending(sender)
    sender.reject(req.id, "boss")
    assert sender.reject(req.id, "boss") == "already_rejected"


def test_pdf_changed_after_generation_is_not_sent(sender, fake_ds, sink):
    req = _pending(sender)
    with open(req.pdf_path, "ab") as f:
        f.write(b"tampered")
    assert sender.approve(req.id, "tester") == "failed"
    assert fake_ds.envelopes == {}
    assert "changed" in sender.store.get(req.id).reason


def test_existing_envelope_is_adopted_not_duplicated(sender, fake_ds):
    req = _pending(sender)
    # A crash after DocuSign accepted it: the envelope exists, our row still says pending.
    fake_ds.envelopes["pre"] = {"envelopeId": "pre", "status": "sent", "recipients": {"signers": []},
                                "customFields": {"textCustomFields": [{"name": FIELD_REQUEST_KEY,
                                                                       "value": req.request_key}]}}
    assert sender.approve(req.id, "tester") == "sent"
    assert sender.store.get(req.id).envelope_id == "pre"
    assert len(fake_ds.envelopes) == 1


def test_ambiguous_write_then_found(sender, fake_ds):
    req = _pending(sender)
    fake_ds.fail_next("POST", "/envelopes", "applied_then_500")
    assert sender.approve(req.id, "tester") == "sent"
    assert len(fake_ds.envelopes) == 1
    assert "adopted_after_ambiguous_write" in [r["action"] for r in sender.store.audit_rows()]


def test_ambiguous_write_not_found_fails_safely(sender, fake_ds, sink):
    req = _pending(sender)
    fake_ds.fail_next("POST", "/envelopes", 503)
    assert sender.approve(req.id, "tester") == "failed"
    assert fake_ds.envelopes == {}
    assert "Check DocuSign" in sender.store.get(req.id).reason


def test_timeout_on_create_is_ambiguous(sender, fake_ds):
    req = _pending(sender)
    fake_ds.fail_next("POST", "/envelopes", "timeout")
    assert sender.approve(req.id, "tester") == "failed"


def test_docusign_rejects_envelope(sender, fake_ds, fake_hs):
    req = _pending(sender)
    fake_ds.fail_next("POST", "/envelopes", 400)
    assert sender.approve(req.id, "tester") == "failed"
    assert fake_hs.deals["101"]["docusign_status"] == "failed"


def test_lookup_failure_stops_send(sender, fake_ds):
    req = _pending(sender)
    fake_ds.fail_next("GET", "/envelopes", 400)
    assert sender.approve(req.id, "tester") == "failed"
    assert fake_ds.envelopes == {}


def test_daily_cap(make_sender, sink):
    s = make_sender(max_envelopes_per_day=1)
    _sent(s)
    s.handle_stage_event(StageEvent("hubspot", "101", "contractsent-2", "k2"))  # ignored stage
    s.s.template_version = "v2"
    s.handle_stage_event(EV)
    second = s.store.list("pending_approval")[0]
    with pytest.raises(NotAllowed):
        s.approve(second.id, "tester")
    assert any("cap" in a[1] for a in sink.alerts)


def test_countersigner_is_second_signer(make_sender, fake_ds):
    s = make_sender(countersigner_name="Sam Owner", countersigner_email="sam@example.com",
                    template="templates/service_agreement_countersigned.md")
    req = _sent(s)
    signers = fake_ds.envelopes[req.envelope_id]["recipients"]["signers"]
    assert [x["email"] for x in signers] == ["dana.lee@example.com", "sam@example.com"]
    assert signers[0]["routingOrder"] < signers[1]["routingOrder"]


def test_countersigner_without_template_line_fails(make_sender):
    s = make_sender(countersigner_name="Sam Owner", countersigner_email="sam@example.com")
    assert s.handle_stage_event(EV) == "failed"
    assert "countersigner" in s.store.list()[0].reason


def test_connect_url_set_per_envelope(make_sender, fake_ds):
    s = make_sender(public_base_url="https://sign.example.com/")
    req = _sent(s)
    note = fake_ds.envelopes[req.envelope_id]["eventNotification"]
    assert note["url"] == "https://sign.example.com/webhooks/docusign"


def test_email_mode_has_no_client_user_id(sender, fake_ds):
    req = _sent(sender)
    assert "clientUserId" not in fake_ds.envelopes[req.envelope_id]["recipients"]["signers"][0]


# --- embedded signing ------------------------------------------------------------------
def test_embedded_signing_url(make_sender, fake_ds):
    s = make_sender(signing_mode="embedded")
    req = _sent(s)
    url = s.signing_url(req.id)
    assert url.startswith("https://demo.docusign.net/Signing/")
    assert fake_ds.views[0]["returnUrl"].endswith(f"?request={req.id}")


def test_signing_url_refused_in_email_mode(sender):
    req = _sent(sender)
    with pytest.raises(NotAllowed):
        sender.signing_url(req.id)


def test_signing_url_refused_when_not_sent(make_sender):
    s = make_sender(signing_mode="embedded")
    req = _pending(s)
    with pytest.raises(NotAllowed):
        s.signing_url(req.id)


# --- void ------------------------------------------------------------------------------
def test_void_needs_reason(sender):
    req = _sent(sender)
    with pytest.raises(NotAllowed):
        sender.void(req.id, "boss", "  ")


def test_void_sent_envelope(sender, fake_ds, fake_hs):
    req = _sent(sender)
    assert sender.void(req.id, "boss", "price changed") == "voided"
    assert fake_ds.envelopes[req.envelope_id]["status"] == "voided"
    assert fake_hs.deals["101"]["docusign_status"] == "voided"
    assert sender.store.get(req.id).reason == "price changed"


def test_void_pending_not_allowed(sender):
    req = _pending(sender)
    with pytest.raises(NotAllowed):
        sender.void(req.id, "boss", "x")


# --- 3. Connect events ---------------------------------------------------------------
def test_completed_stores_signed_pdf_and_certificate(sender, fake_ds, fake_hs):
    req = _sent(sender)
    fake_ds.set_status(req.envelope_id, "completed")
    assert sender.handle_connect(fake_ds.connect_payload(req.envelope_id, "envelope-completed")) == "completed"
    done = sender.store.get(req.id)
    assert open(done.signed_path, "rb").read().startswith(b"%PDF")
    assert b"certificate" in open(done.certificate_path, "rb").read()
    assert len(done.signed_sha256) == 64 and len(done.certificate_sha256) == 64
    assert fake_hs.deals["101"]["docusign_status"] == "completed"
    assert fake_hs.deals["101"]["docusign_completed_at"]


def test_delivered_then_completed(sender, fake_ds):
    req = _sent(sender)
    fake_ds.set_status(req.envelope_id, "delivered")
    assert sender.handle_connect(fake_ds.connect_payload(req.envelope_id, "envelope-delivered")) == "delivered"
    fake_ds.set_status(req.envelope_id, "completed")
    assert sender.handle_connect(fake_ds.connect_payload(req.envelope_id, "envelope-completed")) == "completed"


def test_out_of_order_event_is_stale(sender, fake_ds):
    req = _sent(sender)
    fake_ds.set_status(req.envelope_id, "completed")
    sender.handle_connect(fake_ds.connect_payload(req.envelope_id, "envelope-completed"))
    assert sender.handle_connect(fake_ds.connect_payload(req.envelope_id, "envelope-delivered")) == "stale"
    assert sender.store.get(req.id).status == "completed"


def test_repeat_completed_is_stale(sender, fake_ds):
    req = _sent(sender)
    fake_ds.set_status(req.envelope_id, "completed")
    payload = fake_ds.connect_payload(req.envelope_id, "envelope-completed")
    sender.handle_connect(payload)
    assert sender.handle_connect(payload) == "stale"


def test_declined_records_reason(sender, fake_ds, fake_hs):
    req = _sent(sender)
    fake_ds.set_status(req.envelope_id, "declined", "Terms not agreed")
    assert sender.handle_connect(fake_ds.connect_payload(req.envelope_id, "envelope-declined")) == "declined"
    assert sender.store.get(req.id).reason == "Terms not agreed"
    assert "DECLINED" in json.dumps(fake_hs.notes)
    assert fake_hs.deals["101"]["docusign_status"] == "declined"


def test_voided_by_connect(sender, fake_ds):
    req = _sent(sender)
    fake_ds.set_status(req.envelope_id, "voided", "Sender voided")
    assert sender.handle_connect(fake_ds.connect_payload(req.envelope_id, "envelope-voided")) == "voided"


def test_connect_unknown_envelope(sender):
    payload = {"event": "envelope-completed", "data": {"envelopeId": "nope", "envelopeSummary": {}}}
    assert sender.handle_connect(payload) == "unknown_envelope"


def test_connect_without_envelope_or_unknown_event(sender):
    assert sender.handle_connect({"event": "envelope-completed", "data": {}}) == "ignored_no_envelope"
    assert sender.handle_connect({"event": "recipient-sent", "data": {"envelopeId": "x"}}) == "ignored_event"


def test_connect_rescues_crash_between_create_and_save(sender, fake_ds):
    req = _pending(sender)
    sender.store.transition(req.id, ("pending_approval",), "sending")
    eid = "crashed-envelope"
    fake_ds.envelopes[eid] = {"envelopeId": eid, "status": "sent", "recipients": {"signers": []},
                              "customFields": {"textCustomFields": [{"name": FIELD_REQUEST_KEY,
                                                                     "value": req.request_key}]}}
    assert sender.handle_connect(fake_ds.connect_payload(eid, "envelope-delivered")) == "delivered"
    assert sender.store.get(req.id).envelope_id == eid


def test_completed_download_failure_raises_for_retry(sender, fake_ds):
    req = _sent(sender)
    fake_ds.set_status(req.envelope_id, "completed")
    fake_ds.fail_next("GET", "/documents/combined", 404)
    with pytest.raises(DocuSignError):
        sender.handle_connect(fake_ds.connect_payload(req.envelope_id, "envelope-completed"))
    assert sender.store.get(req.id).status == "sent"     # DocuSign retries; nothing half-written


# --- reconcile and resync ------------------------------------------------------------
def test_reconcile_catches_missed_webhook(sender, fake_ds):
    req = _sent(sender)
    fake_ds.set_status(req.envelope_id, "completed")
    assert sender.reconcile() == 1
    assert sender.store.get(req.id).status == "completed"
    assert sender.reconcile() == 0


def test_reconcile_nothing_open(sender):
    assert sender.reconcile() == 0


def test_resync_rewrites_status(sender, fake_hs):
    req = _sent(sender)
    fake_hs.deals["101"]["docusign_status"] = "garbage"
    assert sender.resync(req.id) is True
    assert fake_hs.deals["101"]["docusign_status"] == "sent"


def test_resync_unknown(sender):
    with pytest.raises(NotAllowed):
        sender.resync(42)


# --- other CRMs end to end -------------------------------------------------------------
def test_ghl_flow_end_to_end(sender, fake_ghl, fake_ds):
    assert sender.handle_stage_event(StageEvent("ghl", "opp1", "stage-contract", "ghl:1")) == "pending_approval"
    req = sender.store.list()[0]
    assert sender.approve(req.id, "tester") == "sent"
    eid = sender.store.get(req.id).envelope_id
    fake_ds.set_status(eid, "completed")
    assert sender.handle_connect(fake_ds.connect_payload(eid, "envelope-completed")) == "completed"


def test_zoho_flow_end_to_end(sender, fake_zoho, fake_ds):
    ev = StageEvent("zoho", "9001", "Proposal/Price Quote", "zoho:1")
    assert sender.handle_stage_event(ev) == "pending_approval"
    req = sender.store.list()[0]
    assert sender.approve(req.id, "tester") == "sent"


def test_unconfigured_crm(sender):
    sender.s.trigger_stages["pipedrive"] = ["won"]
    with pytest.raises(NotAllowed):
        sender.handle_stage_event(StageEvent("pipedrive", "1", "won", "k"))


# --- privacy and audit -----------------------------------------------------------------
def test_mask_hides_emails_and_long_numbers():
    out = mask("dana.lee@example.com paid 1234567890")
    assert "dana.lee" not in out and "1234567890" not in out and "example.com" in out


def test_alerts_are_masked(sender, fake_hs, sink):
    fake_hs.contacts["c101"]["email"] = "broken"
    fake_hs.deals["101"]["amount"] = ""
    sender.handle_stage_event(EV)
    assert all("dana" not in json.dumps(a) for a in sink.alerts)


def test_alert_channel_failure_does_not_break_flow(make_sender, fake_hs):
    s = make_sender()

    def boom(*a):
        raise RuntimeError("smtp down")
    s.alert_fn = boom
    fake_hs.contacts["c101"]["email"] = "bad"
    assert s.handle_stage_event(EV) == "failed"


def test_full_flow_audit_chain_intact(sender, fake_ds):
    req = _sent(sender)
    fake_ds.set_status(req.envelope_id, "completed")
    sender.handle_connect(fake_ds.connect_payload(req.envelope_id, "envelope-completed"))
    actions = [r["action"] for r in sender.store.audit_rows(req.id)]
    for a in ("contract_generated", "approved", "envelope_sent", "completed"):
        assert a in actions
    assert sender.store.verify_audit() == (True, None)
    approved = [r for r in sender.store.audit_rows(req.id) if r["action"] == "approved"][0]
    assert approved["actor"] == "tester"
