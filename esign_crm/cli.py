"""Command line: `esign-crm <command>`.

  serve [--port 8000]            run the webhook + approval server
  list [--status S]              the queue
  show ID                        one request, with its audit trail
  approve ID [--confirm-large]   send it to DocuSign (a person's decision)
  reject ID [--reason TEXT]
  void ID --reason TEXT
  sign-url ID                    embedded mode: a fresh 5-minute signing link
  reconcile                      poll DocuSign for open envelopes (run on a schedule)
  resync ID                      write the status to the CRM again
  retry ID                       re-run a failed request after fixing the deal
  audit-verify                   check the audit log's hash chain
  consent-url                    the one-time DocuSign consent link
  check-auth                     JWT grant + userinfo, prints the account (no secrets)
  setup-fields                   create the CRM status fields (safe to re-run)
  check-heartbeat --max-age-hours H
"""

from __future__ import annotations

import argparse
import getpass
import json
import logging
import sys

from . import config
from .crm import GHLAdapter, HubSpotAdapter, ZohoAdapter
from .docusign_api import DocuSignClient, JWTAuth, consent_url
from .heartbeat import Heartbeat
from .service import NotAllowed, Paused, Sender
from .store import Store

JOB = "DocuSign CRM sender"


def build_crms(s: config.Settings, secret=config.secret) -> dict:
    crms = {}
    if s.crm == "hubspot":
        crms["hubspot"] = HubSpotAdapter(secret("hubspot_access_token"), s.hubspot_base_url,
                                         status_field=s.crm_status_field, envelope_field=s.crm_envelope_field,
                                         completed_field=s.crm_completed_field, timeout=s.http_timeout,
                                         max_retries=s.max_retries)
    elif s.crm == "ghl":
        crms["ghl"] = GHLAdapter(secret("ghl_access_token"), s.ghl_base_url, field_ids=s.ghl_field_ids,
                                 timeout=s.http_timeout, max_retries=s.max_retries)
    elif s.crm == "zoho":
        crms["zoho"] = ZohoAdapter(secret("zoho_client_id"), secret("zoho_client_secret"),
                                   secret("zoho_refresh_token"), api_base=s.zoho_api_base,
                                   accounts_url=s.zoho_accounts_url, timeout=s.http_timeout,
                                   max_retries=s.max_retries)
    return crms


def docusign_auth(s: config.Settings, secret=config.secret) -> JWTAuth:
    key = config.decode_private_key(secret("docusign_private_key"))
    return JWTAuth(secret("docusign_integration_key"), secret("docusign_user_id"), key, s.oauth_host,
                   timeout=s.http_timeout)


def docusign_factory(s: config.Settings, secret=config.secret):
    def make() -> DocuSignClient:
        auth = docusign_auth(s, secret)
        account = auth.account(secret("docusign_account_id", required=False))
        return DocuSignClient(auth, account, timeout=s.http_timeout, max_retries=s.max_retries)
    return make


def build_sender(s: config.Settings, heartbeat: Heartbeat | None = None) -> Sender:
    hb = heartbeat or Heartbeat(JOB)
    return Sender(s, Store(s.db_path), build_crms(s), docusign_factory(s),
                  alert=lambda kind, reason, detail="": hb.alert(kind, reason, detail))


def _print(obj) -> None:
    print(json.dumps(obj, indent=2, default=str))


def main(argv=None) -> int:
    logging.basicConfig(level=logging.INFO, format="%(message)s")
    p = argparse.ArgumentParser(prog="esign-crm", description="DocuSign CRM envelope sender")
    p.add_argument("--config", default=None)
    sub = p.add_subparsers(dest="cmd", required=True)
    sv = sub.add_parser("serve")
    sv.add_argument("--host", default="127.0.0.1")
    sv.add_argument("--port", type=int, default=8000)
    ls = sub.add_parser("list")
    ls.add_argument("--status")
    for name in ("show", "sign-url", "resync", "retry"):
        sub.add_parser(name).add_argument("id", type=int)
    ap = sub.add_parser("approve")
    ap.add_argument("id", type=int)
    ap.add_argument("--confirm-large", action="store_true")
    rj = sub.add_parser("reject")
    rj.add_argument("id", type=int)
    rj.add_argument("--reason", default="")
    vd = sub.add_parser("void")
    vd.add_argument("id", type=int)
    vd.add_argument("--reason", required=True)
    sub.add_parser("reconcile")
    sub.add_parser("audit-verify")
    cu = sub.add_parser("consent-url")
    cu.add_argument("--redirect-uri", default="https://developers.docusign.com/platform/auth/consent")
    sub.add_parser("check-auth")
    sub.add_parser("setup-fields")
    hb = sub.add_parser("check-heartbeat")
    hb.add_argument("--max-age-hours", type=float, default=2.0)
    a = p.parse_args(argv)

    s = config.load_settings(a.config)
    actor = f"cli:{getpass.getuser()}"

    if a.cmd == "consent-url":
        print(consent_url(config.secret("docusign_integration_key"), a.redirect_uri, s.oauth_host))
        return 0
    if a.cmd == "check-auth":
        auth = docusign_auth(s)
        acct = auth.account(config.secret("docusign_account_id", required=False))
        _print({"ok": True, "account_name": acct.account_name, "base_uri": acct.base_uri,
                "account_id_last4": acct.account_id[-4:]})
        return 0
    if a.cmd == "check-heartbeat":
        return 1 if Heartbeat(JOB).check_missed(a.max_age_hours * 3600) else 0
    if a.cmd == "setup-fields":
        for name, adapter in build_crms(s).items():
            _print({"crm": name, "created": adapter.setup_fields()})
        return 0

    if a.cmd == "serve":
        import uvicorn

        from .app import create_app
        sender = build_sender(s)
        secrets = {
            "docusign_hmac_keys": [k for k in [config.secret("docusign_connect_hmac_key", required=False)] if k],
            "hubspot_client_secret": config.secret("hubspot_client_secret", required=False),
            "ghl_webhook_secret": config.secret("ghl_webhook_secret", required=False),
            "zoho_webhook_secret": config.secret("zoho_webhook_secret", required=False),
            "approver_token": config.secret("approver_token", required=False),
        }
        uvicorn.run(create_app(sender, secrets), host=a.host, port=a.port, log_level="warning")
        return 0

    heartbeat = Heartbeat(JOB, min_items=0)
    sender = build_sender(s, heartbeat)
    try:
        if a.cmd == "list":
            _print([{"id": r.id, "crm": r.crm, "deal": r.deal_id, "status": r.status, "envelope": r.envelope_id,
                     "deal_name": r.deal["deal"]["deal_name"], "amount": r.deal["deal"]["amount"],
                     "pdf": r.pdf_path} for r in sender.store.list(a.status)])
        elif a.cmd == "show":
            r = sender.store.get(a.id)
            if not r:
                print("no such request", file=sys.stderr)
                return 1
            _print({**r.as_dict(), "audit": sender.store.audit_rows(a.id)})
        elif a.cmd == "approve":
            print(sender.approve(a.id, actor, confirm_large=a.confirm_large))
        elif a.cmd == "reject":
            print(sender.reject(a.id, actor, a.reason))
        elif a.cmd == "void":
            print(sender.void(a.id, actor, a.reason))
        elif a.cmd == "sign-url":
            print(sender.signing_url(a.id))
        elif a.cmd == "resync":
            print("ok" if sender.resync(a.id) else "failed")
        elif a.cmd == "retry":
            print(sender.retry(a.id, actor))
        elif a.cmd == "reconcile":
            changed = heartbeat.run(sender.reconcile)
            print(f"updated {changed}")
            print(f"HEARTBEAT_ITEMS={changed}")
        elif a.cmd == "audit-verify":
            ok, bad = sender.store.verify_audit()
            print("audit log intact" if ok else f"audit log BROKEN at row {bad}")
            return 0 if ok else 2
    except (NotAllowed, Paused) as e:
        print(f"not done: {e}", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
