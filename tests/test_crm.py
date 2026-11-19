import pytest

from esign_crm.crm import CRMError, GHLAdapter, HubSpotAdapter, ZohoAdapter
from esign_crm.fake_crm import FakeHubSpot, FakeZoho

from .conftest import NO_SLEEP


# --- HubSpot -------------------------------------------------------------------
def test_hubspot_parse_dealstage_event():
    evs = HubSpotAdapter("t").parse_events([{"eventId": 7, "subscriptionType": "deal.propertyChange",
                                             "propertyName": "dealstage", "objectId": 101,
                                             "propertyValue": "contractsent"}])
    assert [(e.deal_id, e.stage, e.delivery_key) for e in evs] == [("101", "contractsent", "hubspot:7")]


def test_hubspot_parse_generic_object_event():
    evs = HubSpotAdapter("t").parse_events([{"eventId": 8, "subscriptionType": "object.propertyChange",
                                             "objectTypeId": "0-3", "propertyName": "dealstage", "objectId": 5,
                                             "propertyValue": "x"}])
    assert evs[0].deal_id == "5"


def test_hubspot_parse_ignores_other_properties_and_objects():
    a = HubSpotAdapter("t")
    assert a.parse_events([{"subscriptionType": "deal.propertyChange", "propertyName": "amount", "objectId": 1,
                            "propertyValue": "5"},
                           {"subscriptionType": "contact.propertyChange", "propertyName": "dealstage",
                            "objectId": 1, "propertyValue": "x"},
                           {"subscriptionType": "object.propertyChange", "objectTypeId": "0-1",
                            "propertyName": "dealstage", "objectId": 1, "propertyValue": "x"},
                           "junk"]) == []


def test_hubspot_parse_without_event_id_uses_fallback_key():
    ev = HubSpotAdapter("t").parse_events([{"subscriptionType": "deal.propertyChange", "propertyName": "dealstage",
                                            "objectId": 1, "propertyValue": "s", "occurredAt": 123}])[0]
    assert ev.delivery_key == "hubspot:1:s:123"


def test_hubspot_parse_requires_list():
    with pytest.raises(CRMError):
        HubSpotAdapter("t").parse_events({"objectId": 1})


def test_hubspot_get_deal(hubspot):
    d = hubspot.get_deal("101")
    assert (d.deal_name, d.amount, d.client_name, d.client_email, d.client_company) == (
        "Website rebuild", "4800", "Dana Lee", "dana.lee@example.com", "Northwind Test Co")


def test_hubspot_deal_without_contact(fake_hs, hubspot):
    fake_hs.add_deal("102", contact=None)
    with pytest.raises(CRMError, match="no associated contact"):
        hubspot.get_deal("102")


def test_hubspot_write_status_and_note(fake_hs, hubspot):
    d = hubspot.get_deal("101")
    hubspot.write_status(d, "sent", envelope_id="env-1")
    hubspot.add_note(d, "Sent")
    assert fake_hs.deals["101"]["docusign_status"] == "sent"
    assert fake_hs.deals["101"]["docusign_envelope_id"] == "env-1"
    note = fake_hs.notes[-1]
    assert note["associations"][0]["types"][0]["associationTypeId"] == 214
    assert note["properties"]["hs_note_body"] == "Sent"


def test_hubspot_write_status_fails_when_property_missing():
    f = FakeHubSpot()
    f.add_deal("1")
    a = HubSpotAdapter("hs-token", transport=f.transport(), sleep=NO_SLEEP)
    with pytest.raises(CRMError, match="does not exist"):
        a.write_status("1", "sent")


def test_hubspot_setup_fields_idempotent():
    f = FakeHubSpot()
    a = HubSpotAdapter("hs-token", transport=f.transport(), sleep=NO_SLEEP)
    assert len(a.setup_fields()) == 3
    assert a.setup_fields() == []


def test_hubspot_bad_token():
    f = FakeHubSpot()
    f.add_deal("1")
    with pytest.raises(CRMError) as e:
        HubSpotAdapter("wrong", transport=f.transport(), sleep=NO_SLEEP).get_deal("1")
    assert e.value.status == 401


def test_hubspot_retries_reads_on_429(fake_hs, hubspot):
    fake_hs.fail_next("GET", "/deals/101", 429)
    assert hubspot.get_deal("101").deal_id == "101"


def test_hubspot_note_post_not_retried_on_5xx(fake_hs, hubspot):
    d = hubspot.get_deal("101")
    fake_hs.fail_next("POST", "/notes", 502)
    with pytest.raises(CRMError):
        hubspot.add_note(d, "x")
    assert fake_hs.notes == []


def test_hubspot_patch_retried_on_5xx(fake_hs, hubspot):
    fake_hs.fail_next("PATCH", "/deals/101", 503)
    hubspot.write_status("101", "sent")
    assert fake_hs.deals["101"]["docusign_status"] == "sent"


def test_hubspot_timeout_on_read_retried(fake_hs, hubspot):
    fake_hs.fail_next("GET", "/deals/101", "timeout")
    assert hubspot.get_deal("101")


# --- GoHighLevel -------------------------------------------------------------------
def test_ghl_parse_stage_update():
    ev = GHLAdapter("t").parse_events({"type": "OpportunityStageUpdate", "id": "o1", "pipelineStageId": "s1",
                                       "webhookId": "w9"})[0]
    assert (ev.deal_id, ev.stage, ev.delivery_key) == ("o1", "s1", "ghl:o1:s1:w9")


def test_ghl_parse_ignores_other_types():
    assert GHLAdapter("t").parse_events({"type": "ContactCreate", "id": "c1"}) == []


def test_ghl_parse_missing_fields():
    assert GHLAdapter("t").parse_events({"type": "OpportunityStageUpdate", "id": "o1"}) == []


def test_ghl_parse_same_body_same_key():
    body = {"type": "OpportunityStageUpdate", "id": "o1", "pipelineStageId": "s1"}
    a = GHLAdapter("t")
    assert a.parse_events(body)[0].delivery_key == a.parse_events(dict(body))[0].delivery_key


def test_ghl_parse_requires_object():
    with pytest.raises(CRMError):
        GHLAdapter("t").parse_events([1])


def test_ghl_get_deal(ghl):
    d = ghl.get_deal("opp1")
    assert (d.amount, d.client_name, d.client_email, d.client_company, d.contact_id) == (
        "1500", "Sam Patel", "sam.patel@example.com", "Bluebird Test LLC", "ct-opp1")


def test_ghl_requires_version_header(fake_ghl):
    class NoVersion(GHLAdapter):
        def headers(self):
            h = super().headers()
            h.pop("Version")
            return h
    with pytest.raises(CRMError) as e:
        NoVersion("ghl-token", transport=fake_ghl.transport(), sleep=NO_SLEEP).get_deal("opp1")
    assert e.value.status == 401


def test_ghl_write_status_by_field_id(fake_ghl, ghl):
    ghl.write_status("opp1", "completed", envelope_id="e1", completed_at="2026-11-17T17:01:02Z")
    fields = {f["id"]: f["field_value"] for f in fake_ghl.opps["opp1"]["customFields"]}
    assert fields == {"f-status": "completed", "f-env": "e1", "f-done": "2026-11-17T17:01:02Z"}


def test_ghl_write_status_needs_field_ids(fake_ghl):
    with pytest.raises(CRMError, match="field ids"):
        GHLAdapter("ghl-token", transport=fake_ghl.transport(), sleep=NO_SLEEP).write_status("opp1", "sent")


def test_ghl_note_on_contact(fake_ghl, ghl):
    ghl.add_note(ghl.get_deal("opp1"), "hello")
    assert fake_ghl.notes[-1] == {"contact": "ct-opp1", "body": "hello"}


def test_ghl_missing_opportunity(ghl):
    with pytest.raises(CRMError):
        ghl.get_deal("nope")


# --- Zoho ------------------------------------------------------------------------------
def test_zoho_parse_json_and_form_shapes():
    a = ZohoAdapter("i", "s", "r")
    assert a.parse_events({"deal_id": "1", "stage": "Proposal/Price Quote"})[0].deal_id == "1"
    assert a.parse_events({"id": "2", "Stage": "X", "modified_time": "t"})[0].delivery_key == "zoho:2:X:t"


def test_zoho_parse_missing():
    assert ZohoAdapter("i", "s", "r").parse_events({"deal_id": "1"}) == []


def test_zoho_get_deal_refreshes_token_once(fake_zoho, zoho):
    d = zoho.get_deal("9001")
    zoho.get_deal("9001")
    assert (d.deal_name, d.amount, d.client_name, d.client_email, d.client_company) == (
        "Bookkeeping setup", "2200.0", "Lee Morgan", "lee.morgan@example.com", "Harbour Test Ltd")
    assert fake_zoho.token_calls == 1


def test_zoho_expired_token_renewed_on_401(fake_zoho, zoho):
    zoho.get_deal("9001")
    fake_zoho.expire_tokens()
    zoho.get_deal("9001")
    assert fake_zoho.token_calls == 2


def test_zoho_bad_refresh_token():
    f = FakeZoho()
    f.add_deal("1")
    with pytest.raises(CRMError, match="token refresh failed"):
        ZohoAdapter("i", "s", "wrong", transport=f.transport(), sleep=NO_SLEEP).get_deal("1")


def test_zoho_missing_deal(zoho):
    with pytest.raises(CRMError, match="not found"):
        zoho.get_deal("404")


def test_zoho_write_status(fake_zoho, zoho):
    zoho.write_status("9001", "sent", envelope_id="e1")
    assert fake_zoho.deals["9001"]["DocuSign_Status"] == "sent"
    assert fake_zoho.deals["9001"]["DocuSign_Envelope_ID"] == "e1"


def test_zoho_write_status_rejected_field_raises(fake_zoho):
    fake_zoho.fields.discard("DocuSign_Status")
    a = ZohoAdapter("cid", "csecret", "zoho-refresh", transport=fake_zoho.transport(), sleep=NO_SLEEP)
    with pytest.raises(CRMError, match="INVALID_DATA"):
        a.write_status("9001", "sent")


def test_zoho_note_parent_is_deal(fake_zoho, zoho):
    zoho.add_note(zoho.get_deal("9001"), "Signed")
    n = fake_zoho.notes[-1]
    assert n["Parent_Id"] == {"module": {"api_name": "Deals"}, "id": "9001"} and n["Note_Content"] == "Signed"


def test_zoho_setup_fields_idempotent():
    f = FakeZoho()
    a = ZohoAdapter("cid", "csecret", "zoho-refresh", transport=f.transport(), sleep=NO_SLEEP)
    assert sorted(a.setup_fields()) == ["DocuSign_Completed_At", "DocuSign_Envelope_ID", "DocuSign_Status"]
    assert a.setup_fields() == []


def test_zoho_deal_without_contact(fake_zoho, zoho):
    fake_zoho.deals["9001"]["Contact_Name"] = None
    with pytest.raises(CRMError, match="no contact"):
        zoho.get_deal("9001")
