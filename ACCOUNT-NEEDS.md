# What this build needs from an account (and what only Parish can do)

| Step | Who | Status (2026-11-19) |
|---|---|---|
| Free DocuSign developer (demo) account, sign-up with email code and phone step | **Parish only** (creating accounts is his) | Done 18 Nov (first login errored on DocuSign's side; worked 19 Nov) |
| Log in to the demo account in the automation Chrome | **Parish only** | Done 19 Nov 14:3x |
| Apps and Keys: app "docusign-crm-envelope-sender (portfolio demo)", Private custom integration, our own RSA public key uploaded, consent redirect URI | Neo | Done 19 Nov; ids in Keychain (`docusign-integration-key`, `docusign-user-id`, `docusign-account-id`), private key only in `docusign-private-key` |
| One-time JWT consent (Allow Access) for our own app on the demo account | Neo (Parish's go-ahead for the live test) | Done 19 Nov |
| Connect HMAC key | Neo | Done 19 Nov, Keychain `docusign-connect-hmac-key` only |
| Zoho CRM test org (existing, from the n8n lead pipeline build) | already there | 3 DocuSign fields added to Deals; 2 test deals + contacts (example.com) left in the org |

No card or payment is needed: the developer account is free and never bills. Demo envelopes carry a "demonstration document" watermark and are not legally binding. Going to production would need DocuSign's go-live review and a paid plan (a client's own account, never ours: playbook rule B5/H13).
