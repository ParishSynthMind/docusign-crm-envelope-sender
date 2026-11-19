"""The flow: CRM stage event -> contract -> human approval -> DocuSign envelope -> status back.

1. A deal reaches a trigger stage. We fetch the deal, check it (signer email, name, a real
   amount), fill the template, save the PDF and its SHA-256, and park it as
   `pending_approval`. The CRM shows "pending_approval" and a note. Nothing is sent.
2. A person approves (CLI or the approval API). Compare-and-set moves it to `sending`, so two
   approvals cannot send twice. Before creating an envelope we look for one already carrying
   this request key (a crash or timeout after DocuSign accepted it), and adopt it instead.
   The PDF is re-hashed: if the file changed since it was generated, the send stops.
3. DocuSign Connect (or `reconcile`, for missed webhooks) reports delivered, completed,
   declined or voided. Statuses only move forward. On completed we download the signed PDF
   and the certificate of completion, store them with their hashes, and write the status back.
Every step goes into the hash-chained audit log; failures alert a named person.
"""

from __future__ import annotations

import datetime as dt
import hashlib
import json
import logging
import re
from decimal import Decimal, InvalidOperation
from pathlib import Path
from typing import Callable

from . import contracts
from .config import Settings
from .contracts import Contract, ContractError
from .crm.base import CRMError, Deal, HTTPAdapter, StageEvent
from .docusign_api import AmbiguousWrite, DocuSignClient, DocuSignError
from .envelope import FIELD_REQUEST_KEY, build_definition, custom_field

log = logging.getLogger("esign_crm")

EMAIL = re.compile(r"^[^@\s]+@[^@\s]+\.[^@\s]+$")
EVENT_STATUS = {"envelope-sent": "sent", "envelope-delivered": "delivered", "envelope-completed": "completed",
                "envelope-declined": "declined", "envelope-voided": "voided"}
ENVELOPE_STATUS = {"sent": "sent", "delivered": "delivered", "completed": "completed", "declined": "declined",
                   "voided": "voided"}
RANK = {"pending_approval": 0, "sending": 1, "sent": 2, "delivered": 3, "completed": 4, "declined": 4, "voided": 4,
        "rejected": 4, "failed": 4}
FINAL = {"completed", "declined", "voided", "rejected"}


class Paused(RuntimeError):
    """The kill switch is on."""


class NotAllowed(RuntimeError):
    """The action is not allowed in the request's current state."""


def mask(text: str) -> str:
    """Mask emails and long digit runs before anything reaches a log line."""
    text = re.sub(r"([A-Za-z0-9._%+-])[A-Za-z0-9._%+-]*@([A-Za-z0-9.-]+)", r"\1***@\2", str(text))
    return re.sub(r"\d{7,}", lambda m: m.group(0)[:2] + "***", text)


def _log(event: str, **fields) -> None:
    log.info(json.dumps({"event": event, **{k: mask(v) if isinstance(v, str) else v for k, v in fields.items()}},
                        default=str))


def _now_iso() -> str:
    return dt.datetime.now(dt.timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


class Sender:
    def __init__(self, settings: Settings, store, crms: dict[str, HTTPAdapter],
                 docusign: Callable[[], DocuSignClient], alert: Callable | None = None,
                 today: Callable[[], dt.date] = dt.date.today, template_root: str | Path = "."):
        self.s = settings
        self.store = store
        self.crms = crms
        self._docusign_factory = docusign
        self._docusign: DocuSignClient | None = None
        self.alert_fn = alert or (lambda kind, reason, detail="": False)
        self.today = today
        self.template_root = Path(template_root)

    # --- helpers ------------------------------------------------------------------
    @property
    def docusign(self) -> DocuSignClient:
        if self._docusign is None:
            self._docusign = self._docusign_factory()
        return self._docusign

    def alert(self, kind: str, reason: str, detail: str = "") -> None:
        _log("alert", kind=kind, reason=reason)
        try:
            self.alert_fn(kind, mask(reason), mask(detail))
        except Exception as e:   # an alert channel outage must never break the flow
            log.warning("alert failed: %s", e)

    def _crm(self, name: str) -> HTTPAdapter:
        try:
            return self.crms[name]
        except KeyError:
            raise NotAllowed(f"CRM {name!r} is not configured") from None

    def _crm_update(self, req_id: int, crm: str, deal: Deal | str, status: str, note: str | None = None,
                    **kw) -> bool:
        """Write the status (and a note) back. A CRM outage is logged and alerted, never fatal:
        DocuSign is the source of truth and `resync` repeats the write later."""
        adapter = self._crm(crm)
        try:
            adapter.write_status(deal, status, **kw)
            if note and isinstance(deal, Deal):
                adapter.add_note(deal, note)
            self.store.audit("crm_updated", request_id=req_id, crm=crm, status=status)
            return True
        except CRMError as e:
            self.store.audit("crm_update_failed", request_id=req_id, crm=crm, status=status, error=str(e)[:300])
            self.alert("failure", f"could not write status '{status}' back to {crm}", str(e))
            return False

    def _storage(self, req_id: int) -> Path:
        path = Path(self.s.storage_dir) / f"request-{req_id:06d}"
        path.mkdir(parents=True, exist_ok=True)
        return path

    def _deal_from_snapshot(self, crm: str, snap: dict) -> Deal:
        d = dict(snap["deal"])
        return Deal(**d)

    # --- 1. stage event -> contract waiting for approval ----------------------------------
    def check_deal(self, deal: Deal) -> list[str]:
        problems = []
        if not deal.client_name.strip():
            problems.append("the contact has no name")
        if not EMAIL.match(deal.client_email or ""):
            problems.append("the contact has no valid email")
        if not deal.deal_name.strip():
            problems.append("the deal has no name")
        try:
            if deal.amount is None or str(deal.amount).strip() == "" or Decimal(str(deal.amount)) <= 0:
                problems.append("the deal has no amount")
        except (InvalidOperation, ValueError):
            problems.append("the deal amount is not a number")
        return problems

    def handle_stage_event(self, ev: StageEvent) -> str:
        if self.s.paused:
            raise Paused("kill switch on")
        if ev.stage not in self.s.stages_for(ev.crm):
            return "ignored_stage"
        key = f"{ev.crm}:{ev.deal_id}:{ev.stage}:{self.s.template_version}"
        if self.store.by_key(key):
            return "duplicate"
        adapter = self._crm(ev.crm)
        deal = adapter.get_deal(ev.deal_id)   # a CRM outage raises: the webhook is retried
        problems = self.check_deal(deal)
        fields = {**deal.contract_fields(), "reference": f"{ev.crm.upper()}-{ev.deal_id}",
                  "date_prepared": self.today().isoformat(), "provider_name": self.s.business_name}
        contract: Contract | None = None
        if not problems:
            try:
                fields["amount_text"] = contracts.money(deal.amount, deal.currency)
                contract = contracts.build_contract(self.template_root / self.s.template, fields,
                                                    self.s.template_version,
                                                    needs_countersigner=bool(self.s.countersigner_email))
            except ContractError as e:
                problems.append(str(e))
        snapshot = {"deal": deal.__dict__, "contract": None}
        req = self.store.create_request(key, ev.crm, ev.deal_id, ev.stage, snapshot,
                                        status="failed" if problems else "pending_approval")
        if req is None:
            return "duplicate"
        if problems:
            reason = "; ".join(problems)
            self.store.transition(req.id, ("failed",), "failed", reason=reason[:500])
            self.store.audit("contract_not_generated", request_id=req.id, reason=reason)
            self._crm_update(req.id, ev.crm, deal, "failed",
                             f"Contract NOT generated: {reason}. Fix the deal, then run: esign-crm retry {req.id}")
            self.alert("failure", f"contract not generated for {ev.crm} deal {ev.deal_id}", reason)
            return "failed"
        folder = self._storage(req.id)
        pdf_path = folder / "contract.pdf"
        pdf_path.write_bytes(contract.pdf)
        snapshot["contract"] = {"title": contract.title, "anchors": contract.anchors,
                                "template_version": contract.template_version}
        self.store.db.execute("UPDATE requests SET deal = ? WHERE id = ?", (json.dumps(snapshot, sort_keys=True), req.id))
        self.store.update_fields(req.id, pdf_path=str(pdf_path), pdf_sha256=contract.sha256)
        large = Decimal(str(deal.amount)) > Decimal(str(self.s.max_amount))
        self.store.audit("contract_generated", request_id=req.id, pdf_sha256=contract.sha256,
                         template_version=contract.template_version, large_amount=large)
        note = (f"Contract generated ({contract.title}, template {contract.template_version}) and waiting for "
                f"approval. Request {req.id}, PDF SHA-256 {contract.sha256[:16]}.")
        if large:
            note += f" Amount is above the {self.s.max_amount:,.0f} limit: approval needs --confirm-large."
        self._crm_update(req.id, ev.crm, deal, "pending_approval", note)
        _log("contract_generated", request=req.id, crm=ev.crm, deal=ev.deal_id)
        return "pending_approval"

    def retry(self, request_id: int, actor: str) -> str:
        """Re-run a failed request (after the deal was fixed in the CRM)."""
        req = self.store.get(request_id)
        if not req or req.status != "failed" or req.envelope_id:
            raise NotAllowed("only a failed request with no envelope can be retried")
        self.store.db.execute("DELETE FROM requests WHERE id = ? AND status = 'failed'", (request_id,))
        self.store.audit("retry", actor=actor, request_id=request_id)
        return self.handle_stage_event(StageEvent(req.crm, req.deal_id, req.stage, f"retry:{request_id}"))

    # --- 2. approval -> envelope ---------------------------------------------------------
    def reject(self, request_id: int, actor: str, reason: str = "") -> str:
        req = self.store.get(request_id)
        if not req:
            raise NotAllowed("no such request")
        if not self.store.transition(request_id, ("pending_approval",), "rejected", decided_by=actor,
                                     reason=reason[:500] or None):
            return f"already_{req.status}"
        self.store.audit("rejected", actor=actor, request_id=request_id, reason=reason)
        deal = self._deal_from_snapshot(req.crm, req.deal)
        self._crm_update(request_id, req.crm, deal, "rejected", f"Contract rejected by {actor}. {reason}".strip())
        return "rejected"

    def approve(self, request_id: int, actor: str, confirm_large: bool = False) -> str:
        if self.s.paused:
            raise Paused("kill switch on")
        req = self.store.get(request_id)
        if not req:
            raise NotAllowed("no such request")
        if req.status != "pending_approval":
            return f"already_{req.status}"
        deal = self._deal_from_snapshot(req.crm, req.deal)
        if Decimal(str(deal.amount)) > Decimal(str(self.s.max_amount)) and not confirm_large:
            raise NotAllowed(f"amount above {self.s.max_amount:,.0f}: approve again with confirm_large")
        if self.store.sent_today() >= self.s.max_envelopes_per_day:
            self.alert("failure", "daily envelope cap reached", f"cap {self.s.max_envelopes_per_day}")
            raise NotAllowed(f"daily cap of {self.s.max_envelopes_per_day} envelopes reached")
        if not self.store.transition(request_id, ("pending_approval",), "sending", decided_by=actor):
            return f"already_{self.store.get(request_id).status}"
        self.store.audit("approved", actor=actor, request_id=request_id, confirm_large=confirm_large)
        return self._send(self.store.get(request_id), deal)

    def _fail_send(self, req, deal: Deal, reason: str) -> str:
        self.store.transition(req.id, ("sending",), "failed", reason=reason[:500])
        self.store.audit("send_failed", request_id=req.id, reason=reason[:500])
        self._crm_update(req.id, req.crm, deal, "failed", f"DocuSign send FAILED: {reason[:300]}")
        self.alert("failure", f"DocuSign send failed for request {req.id}", reason)
        return "failed"

    def _send(self, req, deal: Deal) -> str:
        pdf = Path(req.pdf_path).read_bytes()
        if hashlib.sha256(pdf).hexdigest() != req.pdf_sha256:
            return self._fail_send(req, deal, "the contract PDF changed after it was generated; not sent")
        meta = req.deal["contract"]
        contract = Contract(title=meta["title"], text="", pdf=pdf, sha256=req.pdf_sha256, anchors=meta["anchors"],
                            template_version=meta["template_version"])
        from_date = (dt.datetime.fromtimestamp(req.created_at, dt.timezone.utc) - dt.timedelta(days=1)).strftime("%Y-%m-%d")
        ds = self.docusign
        try:
            existing = [e for e in ds.find_envelopes(FIELD_REQUEST_KEY, req.request_key, from_date)
                        if e.get("status") != "voided"]
        except DocuSignError as e:
            return self._fail_send(req, deal, f"could not check for an existing envelope: {e}")
        if existing:
            envelope_id = existing[0]["envelopeId"]
            self.store.audit("adopted_existing_envelope", request_id=req.id, envelope_id=envelope_id)
        else:
            connect_url = f"{self.s.public_base_url.rstrip('/')}/webhooks/docusign" if self.s.public_base_url else None
            fmt = {"deal_name": deal.deal_name, "client_name": deal.client_name}
            definition = build_definition(
                contract, request_key=req.request_key, crm=req.crm, deal_id=req.deal_id,
                client_name=deal.client_name, client_email=deal.client_email,
                email_subject=self.s.email_subject.format(**fmt), email_blurb=self.s.email_blurb.format(**fmt),
                embedded=self.s.signing_mode == "embedded",
                countersigner=(self.s.countersigner_name, self.s.countersigner_email) if self.s.countersigner_email else None,
                connect_url=connect_url)
            try:
                envelope_id = ds.create_envelope(definition)["envelopeId"]
            except AmbiguousWrite as e:
                # It may exist. Look once more before deciding.
                try:
                    found = ds.find_envelopes(FIELD_REQUEST_KEY, req.request_key, from_date)
                except DocuSignError:
                    found = []
                if not found:
                    return self._fail_send(req, deal, f"{e}; no envelope found afterwards. Check DocuSign before "
                                                      f"retrying")
                envelope_id = found[0]["envelopeId"]
                self.store.audit("adopted_after_ambiguous_write", request_id=req.id, envelope_id=envelope_id)
            except DocuSignError as e:
                return self._fail_send(req, deal, str(e))
        self.store.transition(req.id, ("sending",), "sent", envelope_id=envelope_id)
        self.store.audit("envelope_sent", request_id=req.id, envelope_id=envelope_id, mode=self.s.signing_mode)
        how = "signing link handed out by us (embedded)" if self.s.signing_mode == "embedded" else "emailed by DocuSign"
        self._crm_update(req.id, req.crm, deal, "sent", f"Sent for signature via DocuSign ({how}). Envelope {envelope_id}.",
                         envelope_id=envelope_id)
        _log("envelope_sent", request=req.id, envelope=envelope_id)
        return "sent"

    def signing_url(self, request_id: int) -> str:
        if self.s.signing_mode != "embedded":
            raise NotAllowed("signing links are only for embedded signing; DocuSign emails the signer")
        req = self.store.get(request_id)
        if not req or req.status not in ("sent", "delivered") or not req.envelope_id:
            raise NotAllowed("the envelope is not waiting for a signature")
        deal = self._deal_from_snapshot(req.crm, req.deal)
        sep = "&" if "?" in self.s.return_url else "?"
        url = self.docusign.recipient_view(
            req.envelope_id, email=deal.client_email, user_name=deal.client_name,
            client_user_id=f"crm-{req.crm}-{req.deal_id}"[:100], return_url=f"{self.s.return_url}{sep}request={req.id}")
        self.store.audit("signing_link_issued", request_id=req.id)
        return url

    def void(self, request_id: int, actor: str, reason: str) -> str:
        if not reason.strip():
            raise NotAllowed("a void needs a reason (DocuSign shows it to the signer)")
        req = self.store.get(request_id)
        if not req or req.status not in ("sent", "delivered"):
            raise NotAllowed("only a sent or delivered envelope can be voided")
        self.docusign.void_envelope(req.envelope_id, reason)
        self.store.audit("void_requested", actor=actor, request_id=req.id, reason=reason)
        return self._apply_status(req, "voided", {"voidedReason": reason})

    # --- 3. status back ------------------------------------------------------------------
    def handle_connect(self, payload: dict) -> str:
        event = payload.get("event", "")
        data = payload.get("data") or {}
        envelope_id = data.get("envelopeId")
        if not envelope_id:
            return "ignored_no_envelope"
        status = EVENT_STATUS.get(event)
        if not status:
            return "ignored_event"
        req = self.store.by_envelope(envelope_id)
        summary = data.get("envelopeSummary") or {}
        if not req:
            key = custom_field(summary, FIELD_REQUEST_KEY)
            req = self.store.by_key(key) if key else None
            if req and req.status == "sending":
                # Crash between DocuSign accepting the envelope and us saving its id.
                self.store.transition(req.id, ("sending",), "sent", envelope_id=envelope_id)
                req = self.store.get(req.id)
        if not req:
            self.store.audit("connect_unknown_envelope", envelope_id=envelope_id, event_name=event)
            return "unknown_envelope"
        self.store.audit("connect_event", request_id=req.id, event_name=event, envelope_id=envelope_id)
        return self._apply_status(req, status, summary)

    def _decline_reason(self, summary: dict) -> str:
        for signer in (summary.get("recipients") or {}).get("signers") or []:
            if signer.get("declinedReason"):
                return signer["declinedReason"]
        return ""

    def _apply_status(self, req, status: str, summary: dict) -> str:
        if req.status in FINAL:
            return "stale"
        if RANK[status] < RANK.get(req.status, 0) or (RANK[status] == RANK[req.status] and status == req.status):
            return "stale"
        deal = self._deal_from_snapshot(req.crm, req.deal)
        if status == "completed":
            ds = self.docusign
            signed = ds.download(req.envelope_id, "combined")
            cert = ds.download(req.envelope_id, "certificate")
            folder = self._storage(req.id)
            (folder / "signed.pdf").write_bytes(signed)
            (folder / "certificate.pdf").write_bytes(cert)
            fields = dict(signed_path=str(folder / "signed.pdf"), signed_sha256=hashlib.sha256(signed).hexdigest(),
                          certificate_path=str(folder / "certificate.pdf"),
                          certificate_sha256=hashlib.sha256(cert).hexdigest())
            if not self.store.transition(req.id, (req.status,), "completed", **fields):
                return "stale"
            completed_at = summary.get("completedDateTime") or _now_iso()
            self.store.audit("completed", request_id=req.id, envelope_id=req.envelope_id,
                             signed_sha256=fields["signed_sha256"], certificate_sha256=fields["certificate_sha256"])
            self._crm_update(req.id, req.crm, deal, "completed",
                             f"Signed. Signed PDF and certificate of completion stored (SHA-256 "
                             f"{fields['signed_sha256'][:16]} / {fields['certificate_sha256'][:16]}).",
                             envelope_id=req.envelope_id, completed_at=completed_at[:19] + "Z" if "T" in completed_at else completed_at)
            return "completed"
        reason = ""
        if status == "declined":
            reason = self._decline_reason(summary)
        elif status == "voided":
            reason = summary.get("voidedReason") or ""
        if not self.store.transition(req.id, (req.status,), status, reason=reason[:500] or req.reason):
            return "stale"
        self.store.audit(status, request_id=req.id, envelope_id=req.envelope_id, reason=reason)
        note = None
        if status == "declined":
            note = f"The client DECLINED to sign. Reason: {reason or 'none given'}"
        elif status == "voided":
            note = f"Envelope voided. Reason: {reason or 'none given'}"
        self._crm_update(req.id, req.crm, deal, status, note, envelope_id=req.envelope_id)
        return status

    def reconcile(self) -> int:
        """Poll DocuSign for every open envelope (catches missed Connect deliveries)."""
        changed = 0
        for req in self.store.open_envelopes():
            env = self.docusign.get_envelope(req.envelope_id)
            status = ENVELOPE_STATUS.get(env.get("status", ""))
            if not status or status == req.status:
                continue
            outcome = self._apply_status(req, status, env)
            if outcome not in ("stale",):
                changed += 1
                self.store.audit("reconciled", request_id=req.id, status=outcome)
        return changed

    def resync(self, request_id: int) -> bool:
        """Write the current status to the CRM again (after a CRM outage)."""
        req = self.store.get(request_id)
        if not req:
            raise NotAllowed("no such request")
        return self._crm_update(req.id, req.crm, req.deal_id, req.status, envelope_id=req.envelope_id)
