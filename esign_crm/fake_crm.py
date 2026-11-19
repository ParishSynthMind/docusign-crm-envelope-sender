"""In-memory fakes of the three CRM APIs (only the calls this project makes).

They check auth headers (HubSpot bearer, GHL bearer + Version, Zoho-oauthtoken), keep deals,
contacts, notes and written fields, and can inject 429, 5xx and timeouts. All data is made up
(example.com addresses).
"""

from __future__ import annotations

import json
import urllib.parse

import httpx


class _Base:
    def __init__(self):
        self.requests: list[tuple[str, str]] = []
        self.failures: list[tuple[str, str, object]] = []
        self.notes: list[dict] = []

    def fail_next(self, method: str, path_part: str, status) -> None:
        self.failures.append((method, path_part, status))

    def transport(self) -> httpx.MockTransport:
        return httpx.MockTransport(self.handle)

    def handle(self, request: httpx.Request) -> httpx.Response:
        path = request.url.path
        self.requests.append((request.method, path))
        for i, (m, part, status) in enumerate(self.failures):
            if m == request.method and part in path:
                del self.failures[i]
                if status == "timeout":
                    raise httpx.ConnectTimeout("fake timeout", request=request)
                return httpx.Response(status, headers={"Retry-After": "0"}, json={"message": "injected"})
        if not self.authed(request):
            return httpx.Response(401, json={"message": "unauthorized"})
        return self.route(request, path)

    def authed(self, request) -> bool:
        raise NotImplementedError

    def route(self, request, path) -> httpx.Response:
        raise NotImplementedError


class FakeHubSpot(_Base):
    def __init__(self, token: str = "hs-token"):
        super().__init__()
        self.token = token
        self.deals: dict[str, dict] = {}
        self.contacts: dict[str, dict] = {}
        self.companies: dict[str, dict] = {}
        self.links: dict[str, dict] = {}
        self.properties: set[str] = set()

    def add_deal(self, deal_id, *, name="Website rebuild", amount="4800", stage="contractsent", currency="USD",
                 contact=("Dana", "Lee", "dana.lee@example.com"), company="Northwind Test Co"):
        cid, coid = f"c{deal_id}", f"co{deal_id}"
        self.deals[str(deal_id)] = {"dealname": name, "amount": amount, "dealstage": stage,
                                    "deal_currency_code": currency, "description": "Five-page site and CMS."}
        self.links[str(deal_id)] = {"contacts": [cid] if contact else [], "companies": [coid] if company else []}
        if contact:
            self.contacts[cid] = {"firstname": contact[0], "lastname": contact[1], "email": contact[2]}
        if company:
            self.companies[coid] = {"name": company}

    def authed(self, request) -> bool:
        return request.headers.get("Authorization") == f"Bearer {self.token}"

    def route(self, request, path):
        parts = [p for p in path.split("/") if p]
        if parts[:3] == ["crm", "v3", "objects"] and len(parts) == 5 and request.method == "GET":
            kind, oid = parts[3], parts[4]
            store = {"deals": self.deals, "contacts": self.contacts, "companies": self.companies}.get(kind)
            if store is None or oid not in store:
                return httpx.Response(404, json={"message": "not found"})
            body = {"id": oid, "properties": dict(store[oid])}
            if kind == "deals" and "associations" in request.url.params:
                body["associations"] = {k: {"results": [{"id": i, "type": f"deal_to_{k[:-1]}"} for i in v]}
                                        for k, v in self.links[oid].items() if v}
            return httpx.Response(200, json=body)
        if parts[:4] == ["crm", "v3", "objects", "deals"] and len(parts) == 5 and request.method == "PATCH":
            if parts[4] not in self.deals:
                return httpx.Response(404, json={"message": "not found"})
            props = json.loads(request.content)["properties"]
            unknown = [p for p in props if p.startswith("docusign_") and p not in self.properties]
            if unknown:
                return httpx.Response(400, json={"message": f"Property {unknown[0]} does not exist"})
            self.deals[parts[4]].update(props)
            return httpx.Response(200, json={"id": parts[4], "properties": self.deals[parts[4]]})
        if parts == ["crm", "v3", "objects", "notes"] and request.method == "POST":
            body = json.loads(request.content)
            self.notes.append(body)
            return httpx.Response(201, json={"id": str(len(self.notes))})
        if parts == ["crm", "v3", "properties", "deals"] and request.method == "POST":
            name = json.loads(request.content)["name"]
            if name in self.properties:
                return httpx.Response(409, json={"message": "exists"})
            self.properties.add(name)
            return httpx.Response(201, json={"name": name})
        return httpx.Response(404, json={"message": "no route"})


class FakeGHL(_Base):
    def __init__(self, token: str = "ghl-token"):
        super().__init__()
        self.token = token
        self.opps: dict[str, dict] = {}
        self.contacts: dict[str, dict] = {}

    def add_opp(self, opp_id, *, name="SEO retainer", value=1500, stage="stage-contract",
                contact=("Sam", "Patel", "sam.patel@example.com", "Bluebird Test LLC")):
        cid = f"ct-{opp_id}"
        self.opps[str(opp_id)] = {"id": str(opp_id), "name": name, "monetaryValue": value,
                                  "pipelineStageId": stage, "contactId": cid, "customFields": []}
        self.contacts[cid] = {"id": cid, "firstName": contact[0], "lastName": contact[1], "email": contact[2],
                              "companyName": contact[3]}

    def authed(self, request) -> bool:
        return (request.headers.get("Authorization") == f"Bearer {self.token}"
                and request.headers.get("Version") == "2021-07-28")

    def route(self, request, path):
        parts = [p for p in path.split("/") if p]
        if parts[0] == "opportunities" and len(parts) == 2:
            opp = self.opps.get(parts[1])
            if not opp:
                return httpx.Response(404, json={"message": "not found"})
            if request.method == "GET":
                return httpx.Response(200, json={"opportunity": opp})
            if request.method == "PUT":
                for f in json.loads(request.content).get("customFields", []):
                    opp["customFields"] = [x for x in opp["customFields"] if x["id"] != f["id"]] + [f]
                return httpx.Response(200, json={"opportunity": opp})
        if parts[0] == "contacts" and len(parts) == 2 and request.method == "GET":
            c = self.contacts.get(parts[1])
            return httpx.Response(200, json={"contact": c}) if c else httpx.Response(404, json={})
        if parts[0] == "contacts" and len(parts) == 3 and parts[2] == "notes" and request.method == "POST":
            self.notes.append({"contact": parts[1], **json.loads(request.content)})
            return httpx.Response(201, json={"note": {"id": str(len(self.notes))}})
        return httpx.Response(404, json={"message": "no route"})


class FakeZoho(_Base):
    def __init__(self, refresh_token: str = "zoho-refresh"):
        super().__init__()
        self.refresh_token = refresh_token
        self.access: set[str] = set()
        self.deals: dict[str, dict] = {}
        self.contacts: dict[str, dict] = {}
        self.fields = {"Deal_Name", "Amount", "Stage", "Contact_Name", "Account_Name", "Description"}
        self.token_calls = 0

    def add_deal(self, deal_id, *, name="Bookkeeping setup", amount=2200.0, stage="Proposal/Price Quote",
                 contact=("Lee", "Morgan", "lee.morgan@example.com"), account="Harbour Test Ltd"):
        cid = f"z{deal_id}"
        self.deals[str(deal_id)] = {"id": str(deal_id), "Deal_Name": name, "Amount": amount, "Stage": stage,
                                    "Contact_Name": {"id": cid, "name": f"{contact[0]} {contact[1]}"},
                                    "Account_Name": {"id": "a1", "name": account}, "Description": None}
        self.contacts[cid] = {"id": cid, "First_Name": contact[0], "Last_Name": contact[1], "Email": contact[2]}

    def handle(self, request: httpx.Request) -> httpx.Response:
        if request.url.path == "/oauth/v2/token":
            self.token_calls += 1
            params = dict(request.url.params)
            if params.get("refresh_token") != self.refresh_token or params.get("grant_type") != "refresh_token":
                return httpx.Response(200, json={"error": "invalid_code"})   # Zoho answers 200 with an error
            token = f"zat-{self.token_calls}"
            self.access.add(token)
            return httpx.Response(200, json={"access_token": token, "expires_in": 3600, "token_type": "Bearer"})
        return super().handle(request)

    def expire_tokens(self) -> None:
        self.access.clear()

    def authed(self, request) -> bool:
        auth = request.headers.get("Authorization", "")
        return auth.startswith("Zoho-oauthtoken ") and auth.split(" ", 1)[1] in self.access

    def route(self, request, path):
        parts = [p for p in path.split("/") if p]   # crm, v8, ...
        if parts[:2] != ["crm", "v8"]:
            return httpx.Response(404, json={})
        rest = parts[2:]
        if rest == ["settings", "fields"]:
            if request.method == "GET":
                return httpx.Response(200, json={"fields": [{"api_name": f} for f in sorted(self.fields)]})
            label = json.loads(request.content)["fields"][0]["field_label"]
            api = label.replace(" ", "_")
            self.fields.add(api)
            return httpx.Response(201, json={"fields": [{"code": "SUCCESS", "details": {"api_name": api}}]})
        if len(rest) == 2 and rest[0] in ("Deals", "Contacts") and request.method == "GET":
            store = self.deals if rest[0] == "Deals" else self.contacts
            rec = store.get(rest[1])
            return httpx.Response(200, json={"data": [rec]}) if rec else httpx.Response(204)
        if len(rest) == 2 and rest[0] == "Deals" and request.method == "PUT":
            rec = self.deals.get(rest[1])
            if not rec:
                return httpx.Response(200, json={"data": [{"code": "INVALID_DATA", "message": "the id given seems to be invalid"}]})
            row = json.loads(request.content)["data"][0]
            bad = [k for k in row if k not in self.fields]
            if bad:
                return httpx.Response(200, json={"data": [{"code": "INVALID_DATA", "message": "invalid field",
                                                           "details": {"api_name": bad[0]}}]})
            rec.update(row)
            return httpx.Response(200, json={"data": [{"code": "SUCCESS", "details": {"id": rest[1]}}]})
        if rest == ["Notes"] and request.method == "POST":
            row = json.loads(request.content)["data"][0]
            self.notes.append(row)
            return httpx.Response(201, json={"data": [{"code": "SUCCESS", "details": {"id": str(len(self.notes))}}]})
        return httpx.Response(404, json={"message": urllib.parse.quote(path)})
