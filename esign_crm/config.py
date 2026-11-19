"""Settings and secrets.

Settings come from a YAML file (config/sender.yaml) with a few environment
variables on top. Secrets (the DocuSign RSA private key, the Connect HMAC key,
the CRM tokens and webhook secrets, the approver token) come from the
environment or the macOS Keychain, never from a file in the repo.
"""

from __future__ import annotations

import base64
import binascii
import os
import subprocess
from dataclasses import dataclass, field
from pathlib import Path

import yaml

# Pinned API versions (rule B17): one value each, changed on purpose only.
DOCUSIGN_API_VERSION = "v2.1"          # eSignature REST API path segment
HUBSPOT_CRM_VERSION = "v3"             # /crm/v3/objects/...
GHL_API_VERSION = "2021-07-28"         # GoHighLevel API v2 `Version` header
ZOHO_CRM_VERSION = "v8"                # /crm/v8/...

OAUTH_HOSTS = {
    "demo": "account-d.docusign.com",
    "production": "account.docusign.com",
}

CRMS = ("hubspot", "ghl", "zoho")
SIGNING_MODES = ("email", "embedded")

# The statuses we write back to the CRM, in the order a deal moves through them.
STATUSES = ("pending_approval", "sent", "delivered", "completed", "declined", "voided", "failed", "rejected")


@dataclass
class Settings:
    business_name: str = "Acme Consulting Ltd"
    environment: str = "demo"                 # demo or production
    crm: str = "hubspot"
    # The stage that triggers a contract, per CRM (stage id as the CRM sends it).
    trigger_stages: dict = field(default_factory=lambda: {
        "hubspot": ["contractsent"], "ghl": [], "zoho": ["Proposal/Price Quote"]})
    template: str = "templates/service_agreement.md"
    template_version: str = "2026-11-17"
    signing_mode: str = "email"               # email: DocuSign emails the signer; embedded: we hand out a signing link
    countersigner_name: str = ""              # optional second signer (our side), signs after the client
    countersigner_email: str = ""
    email_subject: str = "Please sign: {deal_name}"
    email_blurb: str = "Hello {client_name}, please review and sign the agreement for {deal_name}."
    return_url: str = "http://localhost:8000/signing-return"
    storage_dir: str = "storage"
    db_path: str = "data/sender.db"
    # CRM fields the status is written to (created once by `esign-crm setup-fields`).
    crm_status_field: str = "docusign_status"
    crm_envelope_field: str = "docusign_envelope_id"
    crm_completed_field: str = "docusign_completed_at"
    hubspot_base_url: str = "https://api.hubapi.com"
    ghl_base_url: str = "https://services.leadconnectorhq.com"
    ghl_field_ids: dict = field(default_factory=dict)   # GHL custom fields are written by id
    zoho_api_base: str = "https://www.zohoapis.com"
    zoho_accounts_url: str = "https://accounts.zoho.com"
    public_base_url: str = ""                 # e.g. https://sign.example.com, used for the Connect URL per envelope
    # Kill switch: true stops every DocuSign send, every CRM write and every approval.
    paused: bool = False
    # Safety caps.
    max_envelopes_per_day: int = 50
    max_amount: float = 250000.0              # deals above this need a second look before approval
    http_timeout: float = 30.0
    max_retries: int = 4
    webhook_max_age_seconds: int = 300        # HubSpot v3 timestamp window

    @property
    def oauth_host(self) -> str:
        try:
            return OAUTH_HOSTS[self.environment]
        except KeyError:
            raise ValueError(f"unknown environment {self.environment!r}, use demo or production") from None

    def validate(self) -> None:
        _ = self.oauth_host
        if self.crm not in CRMS:
            raise ValueError(f"unknown crm {self.crm!r}, use one of {', '.join(CRMS)}")
        if self.signing_mode not in SIGNING_MODES:
            raise ValueError(f"unknown signing_mode {self.signing_mode!r}, use email or embedded")
        if bool(self.countersigner_name) != bool(self.countersigner_email):
            raise ValueError("countersigner_name and countersigner_email go together")
        if self.max_envelopes_per_day < 1:
            raise ValueError("max_envelopes_per_day must be at least 1")

    def stages_for(self, crm: str) -> list[str]:
        return [str(s) for s in (self.trigger_stages or {}).get(crm, [])]


def _bool(value: str) -> bool:
    return value.strip().lower() in {"1", "true", "yes", "on"}


def load_settings(path: str | os.PathLike | None = None) -> Settings:
    path = Path(path or os.environ.get("ESIGN_CONFIG", "config/sender.yaml"))
    data = {}
    if path.exists():
        data = yaml.safe_load(path.read_text(encoding="utf-8")) or {}
    known = set(Settings.__dataclass_fields__)
    unknown = sorted(set(data) - known)
    if unknown:
        raise ValueError(f"unknown settings in {path}: {', '.join(unknown)}")
    s = Settings(**data)
    env = os.environ
    if env.get("ESIGN_DB"):
        s.db_path = env["ESIGN_DB"]
    if env.get("ESIGN_STORAGE"):
        s.storage_dir = env["ESIGN_STORAGE"]
    if env.get("ESIGN_PAUSED"):
        s.paused = _bool(env["ESIGN_PAUSED"])
    if env.get("ESIGN_PUBLIC_URL"):
        s.public_base_url = env["ESIGN_PUBLIC_URL"]
    s.validate()
    return s


def keychain(item: str) -> str | None:
    """Read a generic password from the macOS Keychain. None if missing or not on a Mac."""
    try:
        out = subprocess.run(["security", "find-generic-password", "-s", item, "-w"],
                             capture_output=True, text=True, timeout=10)
    except (FileNotFoundError, subprocess.TimeoutExpired):
        return None
    value = out.stdout.strip()
    return value if out.returncode == 0 and value else None


# name -> (environment variable, Keychain item)
SECRETS = {
    "docusign_integration_key": ("DOCUSIGN_INTEGRATION_KEY", "docusign-integration-key"),
    "docusign_user_id": ("DOCUSIGN_USER_ID", "docusign-user-id"),
    "docusign_private_key": ("DOCUSIGN_PRIVATE_KEY", "docusign-private-key"),
    "docusign_account_id": ("DOCUSIGN_ACCOUNT_ID", "docusign-account-id"),
    "docusign_connect_hmac_key": ("DOCUSIGN_CONNECT_HMAC_KEY", "docusign-connect-hmac-key"),
    "hubspot_access_token": ("HUBSPOT_ACCESS_TOKEN", "hubspot-esign-token"),
    "hubspot_client_secret": ("HUBSPOT_CLIENT_SECRET", "hubspot-esign-client-secret"),
    "ghl_access_token": ("GHL_ACCESS_TOKEN", "ghl-esign-token"),
    "ghl_webhook_secret": ("GHL_WEBHOOK_SECRET", "ghl-esign-webhook-secret"),
    "zoho_client_id": ("ZOHO_CLIENT_ID", "zoho-client-id"),
    "zoho_client_secret": ("ZOHO_CLIENT_SECRET", "zoho-client-secret"),
    "zoho_refresh_token": ("ZOHO_REFRESH_TOKEN", "zoho-refresh-token"),
    "zoho_webhook_secret": ("ZOHO_WEBHOOK_SECRET", "zoho-esign-webhook-secret"),
    "approver_token": ("APPROVER_TOKEN", "esign-approver-token"),
}


def secret(name: str, required: bool = True, keychain_fn=keychain) -> str | None:
    """Environment variable first, then the Keychain. Raises a clear error (never the value) if missing."""
    env_name, item = SECRETS[name]
    value = os.environ.get(env_name) or keychain_fn(item)
    if not value and required:
        raise RuntimeError(f"missing secret {name}: set {env_name} or the Keychain item '{item}'")
    return value or None


def decode_private_key(value: str) -> str:
    """The RSA key may be stored as PEM, as base64 of the PEM (one Keychain line), or hex (how
    `security -w` prints values containing newlines). Returns PEM text."""
    value = value.strip()
    if value.startswith("-----BEGIN"):
        return value.replace("\\n", "\n")
    for decode in (lambda v: base64.b64decode(v, validate=True), bytes.fromhex):
        try:
            text = decode(value).decode("utf-8")
        except (binascii.Error, ValueError, UnicodeDecodeError):
            continue
        if text.strip().startswith("-----BEGIN"):
            return text.strip()
    raise ValueError("DocuSign private key is not PEM, base64 PEM or hex PEM")
