"""Drive one live run against a DocuSign developer (demo) account and a Zoho CRM test org.

Steps (each prints a one-line JSON result, no secrets):
  make-deal NAME AMOUNT        create a test contact (example.com) and a deal at the trigger stage
  trigger DEAL_ID              POST a signed Zoho-style stage webhook to the running server
  approve REQUEST_ID           approve through the server's approval API
  sign-url REQUEST_ID          print a fresh embedded signing link
  void REQUEST_ID REASON       void through the approval API
  zoho DEAL_ID                 read the deal's DocuSign fields back from Zoho

Needs the server running (`esign-crm serve`) and reachable at ESIGN_PUBLIC_URL. Secrets come
from the environment or the Keychain like everything else (config.secret).
"""

from __future__ import annotations

import json
import os
import sys
import time

import httpx

from esign_crm import config, signatures
from esign_crm.cli import build_crms

BASE = os.environ.get("ESIGN_PUBLIC_URL", "http://127.0.0.1:8000").rstrip("/")


def out(**kw) -> None:
    print(json.dumps(kw, default=str))


def zoho():
    s = config.load_settings()
    return build_crms(s)["zoho"]


def approver() -> dict:
    return {"Authorization": f"Bearer {config.secret('approver_token')}"}


def main(argv: list[str]) -> int:
    cmd, args = argv[0], argv[1:]
    if cmd == "make-deal":
        name, amount = args[0], float(args[1])
        z = zoho()
        stamp = time.strftime("%H%M%S")
        c = z._check(z.request("POST", "/Contacts", json_body={"data": [{
            "First_Name": "Dana", "Last_Name": f"Lee (test {stamp})",
            "Email": f"dana.lee.{stamp}@example.com"}]}), "create contact")
        contact_id = c["details"]["id"]
        d = z._check(z.request("POST", "/Deals", json_body={"data": [{
            "Deal_Name": name, "Amount": amount, "Stage": "Proposal/Price Quote",
            "Contact_Name": {"id": contact_id}, "Closing_Date": time.strftime("%Y-%m-%d"),
            "Description": "Portfolio live test. Made-up client, example.com address."}]}), "create deal")
        out(step="make-deal", deal_id=d["details"]["id"], contact_id=contact_id)
    elif cmd == "trigger":
        body = json.dumps({"deal_id": args[0], "stage": "Proposal/Price Quote",
                           "modified_time": time.strftime("%Y-%m-%dT%H:%M:%S")}).encode()
        sig = signatures.generic_sign(body, config.secret("zoho_webhook_secret"))
        r = httpx.post(f"{BASE}/webhooks/zoho", content=body, timeout=60,
                       headers={"Content-Type": "application/json", signatures.HMAC_HEADER: sig})
        out(step="trigger", http=r.status_code, body=r.json())
        bad = httpx.post(f"{BASE}/webhooks/zoho", content=body, timeout=30,
                         headers={"Content-Type": "application/json", signatures.HMAC_HEADER: "sha256=" + "0" * 64})
        out(step="trigger-with-bad-signature", http=bad.status_code)
    elif cmd == "approve":
        r = httpx.post(f"{BASE}/requests/{args[0]}/approve", headers=approver(), timeout=120)
        out(step="approve", http=r.status_code, body=r.json())
    elif cmd == "sign-url":
        r = httpx.get(f"{BASE}/requests/{args[0]}/sign", headers=approver(), timeout=60, follow_redirects=False)
        out(step="sign-url", http=r.status_code, host=httpx.URL(r.headers.get("location", "")).host)
        print(r.headers.get("location", ""), file=sys.stderr)
    elif cmd == "void":
        r = httpx.post(f"{BASE}/requests/{args[0]}/void", params={"reason": args[1]}, headers=approver(), timeout=60)
        out(step="void", http=r.status_code, body=r.json())
    elif cmd == "zoho":
        d = zoho().request("GET", f"/Deals/{args[0]}").json()["data"][0]
        out(step="zoho-readback", deal_id=args[0], **{k: d.get(k) for k in
                                                     ("Deal_Name", "Stage", "DocuSign_Status", "DocuSign_Envelope_ID",
                                                      "DocuSign_Completed_At")})
    else:
        print(__doc__)
        return 2
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
