import base64
import datetime as dt

import pytest

from esign_crm import contracts
from esign_crm.contracts import ANCHORS, ContractError, build_contract, fill, money
from esign_crm.envelope import CONNECT_EVENTS, build_definition, custom_field

from .conftest import ROOT

TEMPLATE = ROOT / "templates" / "service_agreement.md"
COUNTER = ROOT / "templates" / "service_agreement_countersigned.md"
FIELDS = {"reference": "HUBSPOT-101", "date_prepared": "2026-11-17", "provider_name": "Acme Consulting Ltd",
          "client_name": "Dana Lee", "client_email": "dana.lee@example.com", "client_company": "Northwind Test Co",
          "deal_name": "Website rebuild", "amount_text": "USD 4,800.00"}
WHEN = dt.datetime(2026, 11, 17, tzinfo=dt.timezone.utc)


def test_fill_replaces_placeholders():
    assert fill("Hi {{client_name}}, {{ deal_name }}", FIELDS) == "Hi Dana Lee, Website rebuild"


def test_fill_missing_field_stops():
    with pytest.raises(ContractError, match="client_name"):
        fill("Hi {{client_name}}", {})


def test_fill_blank_field_counts_as_missing():
    with pytest.raises(ContractError):
        fill("Total {{amount_text}}", {"amount_text": "   "})


def test_fill_default_used_when_missing():
    assert fill("Start {{start_date|on signature}}", {}) == "Start on signature"


def test_fill_lists_every_missing_field_once():
    with pytest.raises(ContractError) as e:
        fill("{{a}} {{b}} {{a}}", {})
    assert str(e.value) == "missing deal fields: a, b"


def test_crm_values_cannot_inject_placeholders_or_markers():
    out = fill("Name: {{client_name}}", {"client_name": "{{amount_text}} [[signature:client]]"})
    assert "{{" not in out and "[[" not in out


def test_crm_value_newlines_flattened():
    assert fill("{{x}}", {"x": "a\n# Heading\r\nb"}) == "a # Heading  b"


def test_overlong_field_rejected():
    with pytest.raises(ContractError):
        fill("{{x}}", {"x": "y" * 501})


@pytest.mark.parametrize("amount,currency,expected", [("4800", "usd", "USD 4,800.00"), (1234.5, "GBP", "GBP 1,234.50"),
                                                      ("0", "AED", "AED 0.00")])
def test_money(amount, currency, expected):
    assert money(amount, currency) == expected


@pytest.mark.parametrize("bad", ["abc", "-5", None])
def test_money_rejects_bad(bad):
    with pytest.raises(ContractError):
        money(bad)


def test_build_contract_produces_pdf_with_client_anchors():
    c = build_contract(TEMPLATE, FIELDS, "2026-11-17", creation_date=WHEN)
    assert c.pdf.startswith(b"%PDF")
    assert c.title == "Service Agreement"
    assert ANCHORS[("signature", "client")] in c.anchors
    assert ANCHORS[("date", "client")] in c.anchors and ANCHORS[("name", "client")] in c.anchors
    assert len(c.sha256) == 64


def test_build_contract_is_deterministic_for_same_input():
    a = build_contract(TEMPLATE, FIELDS, "v1", creation_date=WHEN)
    b = build_contract(TEMPLATE, FIELDS, "v1", creation_date=WHEN)
    assert a.sha256 == b.sha256


def test_build_contract_text_has_values_and_sample_warning():
    c = build_contract(TEMPLATE, FIELDS, "v1", creation_date=WHEN)
    assert "Dana Lee" in c.text and "USD 4,800.00" in c.text and "Not legal advice" in c.text


def test_build_contract_missing_template():
    with pytest.raises(ContractError, match="not found"):
        build_contract(ROOT / "templates" / "nope.md", FIELDS, "v1")


def test_template_without_signature_line_rejected(tmp_path):
    t = tmp_path / "t.md"
    t.write_text("# T\nHello {{client_name}}\n")
    with pytest.raises(ContractError, match="signature:client"):
        build_contract(t, FIELDS, "v1")


def test_countersigner_needs_template_line():
    with pytest.raises(ContractError, match="countersigner"):
        build_contract(TEMPLATE, FIELDS, "v1", needs_countersigner=True)


def test_countersigned_template_has_both_anchors():
    c = build_contract(COUNTER, FIELDS, "v1", needs_countersigner=True)
    assert ANCHORS[("signature", "countersigner")] in c.anchors


def test_marker_must_be_on_its_own_line(tmp_path):
    t = tmp_path / "t.md"
    t.write_text("# T\nSign here [[signature:client]] please\n")
    with pytest.raises(ContractError, match="own line"):
        build_contract(t, FIELDS, "v1")


def test_long_template_spans_pages(tmp_path):
    t = tmp_path / "t.md"
    t.write_text("# T\n" + "\n".join(f"Clause {i}: {'words ' * 30}" for i in range(120)) + "\n[[signature:client]]\n")
    c = build_contract(t, FIELDS, "v1")
    assert c.pdf.count(b"/Type /Page") - c.pdf.count(b"/Type /Pages") >= 2


def test_unicode_quotes_do_not_crash():
    c = build_contract(TEMPLATE, {**FIELDS, "client_name": "Zoë “Z” O’Neil — 中"}, "v1")
    assert c.pdf.startswith(b"%PDF")


def _definition(**kw):
    c = build_contract(TEMPLATE, FIELDS, "v1", creation_date=WHEN)
    args = dict(request_key="hubspot:101:contractsent:v1", crm="hubspot", deal_id="101", client_name="Dana Lee",
                client_email="dana.lee@example.com", email_subject="Please sign", email_blurb="Hello",
                embedded=False)
    args.update(kw)
    return c, build_definition(c, **args)


def test_definition_document_is_the_pdf():
    c, d = _definition()
    assert base64.b64decode(d["documents"][0]["documentBase64"]) == c.pdf
    assert d["documents"][0]["fileExtension"] == "pdf" and d["documents"][0]["documentId"] == "1"


def test_definition_email_signer_has_no_client_user_id():
    _, d = _definition()
    assert "clientUserId" not in d["recipients"]["signers"][0]


def test_definition_embedded_signer_has_client_user_id():
    _, d = _definition(embedded=True)
    assert d["recipients"]["signers"][0]["clientUserId"] == "crm-hubspot-101"


def test_definition_tabs_on_anchors():
    _, d = _definition()
    tabs = d["recipients"]["signers"][0]["tabs"]
    assert tabs["signHereTabs"][0]["anchorString"] == ANCHORS[("signature", "client")]
    assert tabs["dateSignedTabs"][0]["anchorString"] == ANCHORS[("date", "client")]
    assert tabs["fullNameTabs"][0]["anchorString"] == ANCHORS[("name", "client")]
    assert tabs["signHereTabs"][0]["anchorIgnoreIfNotPresent"] == "false"


def test_definition_custom_fields_hidden_and_findable():
    _, d = _definition()
    assert custom_field(d, "crm_request_key") == "hubspot:101:contractsent:v1"
    assert custom_field(d, "crm_deal_id") == "101"
    assert all(f["show"] == "false" for f in d["customFields"]["textCustomFields"])


def test_definition_countersigner_routes_second():
    c = build_contract(COUNTER, FIELDS, "v1", needs_countersigner=True)
    d = build_definition(c, request_key="k", crm="zoho", deal_id="1", client_name="A", client_email="a@example.com",
                         email_subject="s", email_blurb="b", embedded=False, countersigner=("Pat", "pat@example.com"))
    signers = d["recipients"]["signers"]
    assert [s["routingOrder"] for s in signers] == ["1", "2"]
    assert signers[1]["tabs"]["signHereTabs"][0]["anchorString"] == ANCHORS[("signature", "countersigner")]


def test_definition_connect_block_only_with_url():
    _, d = _definition()
    assert "eventNotification" not in d
    _, d = _definition(connect_url="https://x.example.com/webhooks/docusign")
    en = d["eventNotification"]
    assert en["includeHMAC"] == "true" and en["deliveryMode"] == "SIM" and en["requireAcknowledgment"] == "true"
    assert en["eventData"]["version"] == "restv2.1" and set(en["events"]) == set(CONNECT_EVENTS)


def test_definition_status_only_sent_or_created():
    with pytest.raises(ValueError):
        _definition(status="completed")


def test_definition_subject_capped_at_100():
    _, d = _definition(email_subject="x" * 300)
    assert len(d["emailSubject"]) == 100


def test_custom_field_missing_returns_none():
    assert custom_field({}, "x") is None


def test_anchor_strings_unique():
    assert len(set(contracts.ANCHORS.values())) == len(contracts.ANCHORS)


def test_signature_line_has_room_for_the_signature_box():
    # Live run 19 Nov 2026: DocuSign's signature box (about 46 px) covered the Name line above it
    # when rows were 10 mm apart. The signature row now has extra space above it.
    import re

    from esign_crm.contracts import render_pdf
    text = "# T\n\n[[name:client]]\n[[signature:client]]\n[[date:client]]\n"
    pdf, _ = render_pdf(text, "footer")
    import zlib
    streams = re.findall(rb"stream\r?\n(.*?)\r?\nendstream", pdf, re.S)
    content = b"".join(zlib.decompress(s) if s[:1] == b"x" else s for s in streams)
    ys = [float(m) for m in re.findall(rb"BT [\d.]+ ([\d.]+) Td \((?:Name|Signature|Date):\) Tj", content)]
    assert len(ys) == 3
    name_y, sig_y, date_y = ys
    assert (name_y - sig_y) > (sig_y - date_y) + 20   # at least ~9 mm (25 pt) more room above the signature
