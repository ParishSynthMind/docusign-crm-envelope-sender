"""Contract generation: a Markdown-ish template filled from the deal, rendered to PDF.

Template syntax (kept tiny on purpose so a non-developer can edit the wording):
- `# Heading` and `## Subheading`, blank lines between paragraphs, `- ` bullet lines.
- `{{field}}` placeholders, filled from the deal. A placeholder with no value STOPS the
  contract (it is never sent with a blank price or name). `{{field|default text}}` allows a
  default for optional fields.
- `[[signature:client]]`, `[[date:client]]`, `[[name:client]]` (and `:countersigner`) mark where
  DocuSign puts the signing fields. They become invisible anchor strings in the PDF, and the
  envelope places its tabs on those anchors, so moving a paragraph never breaks the tabs.

Values from the CRM are treated as text only: they cannot add placeholders, markers or
headings (a deal called "{{amount}}" or "[[signature:client]]" is printed literally).
"""

from __future__ import annotations

import hashlib
import re
from dataclasses import dataclass
from decimal import Decimal, InvalidOperation
from pathlib import Path

from fpdf import FPDF

PLACEHOLDER = re.compile(r"\{\{\s*([a-z_][a-z0-9_]*)\s*(?:\|([^}]*))?\}\}")
MARKER = re.compile(r"\[\[(signature|date|name):(client|countersigner)\]\]")

# The anchor strings DocuSign searches for. Printed in white, 1pt, so they are invisible.
ANCHORS = {
    ("signature", "client"): "/sig_client/",
    ("date", "client"): "/date_client/",
    ("name", "client"): "/name_client/",
    ("signature", "countersigner"): "/sig_counter/",
    ("date", "countersigner"): "/date_counter/",
    ("name", "countersigner"): "/name_counter/",
}

MAX_FIELD_CHARS = 500


class ContractError(ValueError):
    """The contract cannot be generated (missing field, bad template). Nothing is sent."""


@dataclass
class Contract:
    title: str
    text: str            # the filled template, as rendered
    pdf: bytes
    sha256: str
    anchors: list[str]   # anchor strings present in the document
    template_version: str


def money(amount, currency: str = "USD") -> str:
    try:
        value = Decimal(str(amount))
    except (InvalidOperation, ValueError):
        raise ContractError(f"amount {amount!r} is not a number") from None
    if value < 0:
        raise ContractError("amount is negative")
    return f"{currency.upper()} {value:,.2f}"


def _clean_value(value) -> str:
    text = str(value).replace("\r", " ").replace("\n", " ").strip()
    if len(text) > MAX_FIELD_CHARS:
        raise ContractError(f"a deal field is longer than {MAX_FIELD_CHARS} characters")
    # Neutralise anything that looks like template syntax so CRM data stays data.
    text = text.replace("{{", "{ {").replace("}}", "} }").replace("[[", "[ [").replace("]]", "] ]")
    return text


def fill(template: str, fields: dict) -> str:
    missing = []

    def sub(m: re.Match) -> str:
        name, default = m.group(1), m.group(2)
        value = fields.get(name)
        if value is None or str(value).strip() == "":
            if default is not None:
                return _clean_value(default)
            missing.append(name)
            return ""
        return _clean_value(value)

    out = PLACEHOLDER.sub(sub, template)
    if missing:
        raise ContractError("missing deal fields: " + ", ".join(sorted(set(missing))))
    return out


def _latin1(text: str) -> str:
    # The built-in PDF fonts are Latin-1; replace what they cannot draw instead of crashing.
    replacements = {"‘": "'", "’": "'", "“": '"', "”": '"', "–": "-", "—": "-",
                    "…": "...", " ": " ", "•": "-"}
    for a, b in replacements.items():
        text = text.replace(a, b)
    return text.encode("latin-1", "replace").decode("latin-1")


class _PDF(FPDF):
    def __init__(self, footer_text: str):
        super().__init__(format="A4")
        self.footer_text = footer_text

    def footer(self):
        self.set_y(-14)
        self.set_font("Helvetica", size=8)
        self.set_text_color(110, 110, 110)
        self.cell(0, 6, _latin1(f"{self.footer_text}   Page {self.page_no()} of {{nb}}"), align="C")


def render_pdf(text: str, footer_text: str, creation_date=None) -> tuple[bytes, list[str]]:
    pdf = _PDF(footer_text)
    pdf.alias_nb_pages()
    pdf.set_auto_page_break(auto=True, margin=20)
    pdf.set_margins(20, 18, 20)
    if creation_date is not None:
        pdf.set_creation_date(creation_date)
    pdf.add_page()
    anchors: list[str] = []
    for raw in text.split("\n"):
        line = raw.rstrip()
        marker = MARKER.fullmatch(line.strip())
        if marker:
            anchor = ANCHORS[(marker.group(1), marker.group(2))]
            anchors.append(anchor)
            label = {"signature": "Signature:", "date": "Date:", "name": "Name:"}[marker.group(1)]
            if marker.group(1) == "signature":
                # A DocuSign signature box is taller than a text row: leave room above the line
                # so it never covers the line before (seen on the first live envelope).
                pdf.ln(9)
            pdf.set_font("Helvetica", size=10)
            pdf.set_text_color(0, 0, 0)
            pdf.cell(28, 10, label)
            x, y = pdf.get_x(), pdf.get_y()
            pdf.set_text_color(255, 255, 255)
            pdf.set_font("Helvetica", size=1)
            pdf.cell(1, 10, anchor)
            pdf.set_xy(x, y)
            pdf.set_text_color(0, 0, 0)
            pdf.set_font("Helvetica", size=10)
            pdf.cell(0, 10, "_" * 40, new_x="LMARGIN", new_y="NEXT")
            continue
        if MARKER.search(line):
            raise ContractError("a signing marker must be on its own line")
        if line.startswith("# "):
            pdf.set_font("Helvetica", "B", 16)
            pdf.multi_cell(0, 9, _latin1(line[2:]), new_x="LMARGIN", new_y="NEXT")
            pdf.ln(2)
        elif line.startswith("## "):
            pdf.ln(2)
            pdf.set_font("Helvetica", "B", 12)
            pdf.multi_cell(0, 7, _latin1(line[3:]), new_x="LMARGIN", new_y="NEXT")
        elif line.startswith("- "):
            pdf.set_font("Helvetica", size=10)
            pdf.multi_cell(0, 5.5, _latin1("  -  " + line[2:]), new_x="LMARGIN", new_y="NEXT")
        elif not line.strip():
            pdf.ln(3)
        else:
            pdf.set_font("Helvetica", size=10)
            pdf.multi_cell(0, 5.5, _latin1(line), new_x="LMARGIN", new_y="NEXT")
    return bytes(pdf.output()), anchors


def build_contract(template_path: str | Path, fields: dict, template_version: str,
                   needs_countersigner: bool = False, creation_date=None) -> Contract:
    path = Path(template_path)
    if not path.exists():
        raise ContractError(f"template not found: {path}")
    template = path.read_text(encoding="utf-8")
    text = fill(template, fields)
    title = next((ln[2:].strip() for ln in text.splitlines() if ln.startswith("# ")), path.stem)
    pdf, anchors = render_pdf(text, f"{title} (template {template_version})", creation_date=creation_date)
    if ANCHORS[("signature", "client")] not in anchors:
        raise ContractError("template has no [[signature:client]] line")
    if needs_countersigner and ANCHORS[("signature", "countersigner")] not in anchors:
        raise ContractError("a countersigner is configured but the template has no [[signature:countersigner]] line")
    return Contract(title=title, text=text, pdf=pdf, sha256=hashlib.sha256(pdf).hexdigest(),
                    anchors=anchors, template_version=template_version)
