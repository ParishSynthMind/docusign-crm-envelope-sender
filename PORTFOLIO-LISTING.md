# Portfolio listing: DocuSign CRM Envelope Sender

Status: DRAFT 2026-11-19, not posted. Live test PASSED 19 Nov 2026 (DocuSign demo account + Zoho CRM test org). Pre-approved by Parish (17 Nov) to post on all four platforms once the live test passed.

## Upwork (portfolio project)
**Title:** DocuSign e-signature from a CRM stage: HubSpot, GoHighLevel, Zoho

**Role:** Automation and API integration developer

**Description (under 600 characters):**
When a CRM deal reaches the contract stage, this service fills the contract template, waits for a person's approval, then sends it with DocuSign (JWT grant, email or embedded signing). DocuSign Connect webhooks (HMAC-checked) report delivered, completed, declined or voided; on completion the signed PDF and certificate are stored and the status goes back to the deal. One envelope per deal, audit trail, kill switch. Tested live on a DocuSign developer account with Zoho CRM; 239 automated tests.

**Skills (5):** DocuSign, API Integration, CRM Automation, Python, Webhooks

**Images:** `media/thumbnail-4x3.png` (cover, 1200x900), `media/live-signed-contract-1.png`, `media/live-embedded-signing.png`, `media/live-adopt-signature.png` (all from the real run).
**Web link block:** https://github.com/ParishSynthMind/docusign-crm-envelope-sender

## PeoplePerHour (200 characters max, no links)
DocuSign from your CRM: deal hits a stage, contract is generated, you approve, DocuSign sends it, the signed PDF and status come back to the deal. Tested live. Code available on request.

## LinkedIn
**Featured card title:** DocuSign CRM Envelope Sender
**Blurb:** A CRM deal stage triggers a contract, a person approves it, DocuSign sends it, and the signed PDF, certificate and status come back to the deal. Tested live.
**Link:** https://github.com/ParishSynthMind/docusign-crm-envelope-sender
**Skills to add:** DocuSign (or "Electronic Signatures" if LinkedIn has no DocuSign skill). Nothing posted to the feed.

## GitHub
Description: "CRM deal stage -> contract PDF -> human approval -> DocuSign envelope (JWT) -> Connect HMAC webhooks -> signed PDF, certificate and status back to HubSpot, GoHighLevel or Zoho."
Topics: docusign, esignature, e-signature, crm, hubspot, gohighlevel, zoho-crm, webhooks, fastapi, python

## Honesty checks before posting
- No "clients", "customers served" or "production" claims (grep the text).
- Live claims only for what `examples/live-run/` shows: DocuSign demo + Zoho. HubSpot and GoHighLevel are tested against fakes only; never say they were tested live.
- Demo envelopes are not legally binding; the sample contract is not legal advice.
