"""Generate synthetic trade documents + ground-truth JSON.

Real BLs/invoices are confidential, so we generate realistic ones and plant known
errors. Because we generate them, every document has an exact ground truth ->
a real offline eval (accuracy, calibration, false auto-approve), not eyeballing.

  python scripts/generate_samples.py                 # data/samples (demo + eval set)
  python scripts/generate_samples.py --seed-set 30   # data/seed (history for NL queries)
"""
import _path  # noqa: F401
import argparse
import json
import random
from pathlib import Path

import pypdfium2 as pdfium
from PIL import Image, ImageDraw, ImageFilter, ImageFont
from reportlab.lib.pagesizes import A4
from reportlab.pdfgen import canvas

from nova.config import ROOT

BASE = {
    "consignee_name": "Acme Electronics Pvt Ltd",
    "hs_code": "8471.30.10",
    "port_of_loading": "Nhava Sheva, India",
    "port_of_discharge": "Rotterdam, Netherlands",
    "incoterms": "FOB Nhava Sheva",
    "description_of_goods": "Laptop computers, 14-inch, model VX-14 (1,200 units)",
    "gross_weight": "12,450.50 KG",
    "invoice_number": "INV-2026-0142",
}
SHIPPER = ["Vega Components Pvt Ltd", "Plot 44, MIDC Bhosari", "Pune 411026, India"]
CONS_ADDR = ["Europoort Logistics Park, Unit 12", "3198 LK Rotterdam, Netherlands"]


def _kv(c, x, y, label, value, bold_value=False):
    c.setFont("Helvetica-Bold", 9)
    c.drawString(x, y, f"{label}:")
    c.setFont("Helvetica-Bold" if bold_value else "Helvetica", 9)
    c.drawString(x + 118, y, value)


def make_invoice(path: Path, f: dict):
    c = canvas.Canvas(str(path), pagesize=A4)
    W, H = A4
    c.setFont("Helvetica-Bold", 16)
    c.drawString(40, H - 50, "COMMERCIAL INVOICE")
    c.setFont("Helvetica", 8)
    c.drawString(40, H - 64, "Exporter: " + ", ".join(SHIPPER))
    c.line(40, H - 72, W - 40, H - 72)
    y = H - 92
    rows = [("Invoice No.", f.get("invoice_number")), ("Invoice Date", "14-Sep-2026"),
            ("Consignee", f.get("consignee_name")), ("Consignee Address", ", ".join(CONS_ADDR)),
            ("Port of Loading", f.get("port_of_loading")), ("Port of Discharge", f.get("port_of_discharge")),
            ("Incoterms 2020", f.get("incoterms")), ("Payment Terms", "30 days from B/L date"),
            ("HS Code", f.get("hs_code")), ("Description of Goods", f.get("description_of_goods")),
            ("Gross Weight", f.get("gross_weight")), ("Net Weight", "11,980.00 KG"),
            ("Packages", "40 pallets")]
    for label, val in rows:
        if val is None:
            continue
        _kv(c, 40, y, label, val)
        y -= 17
    y -= 8
    c.line(40, y, W - 40, y)
    y -= 16
    c.setFont("Helvetica-Bold", 9)
    for x, h in [(40, "Item"), (90, "Qty"), (140, "Unit price (USD)"), (260, "Amount (USD)")]:
        c.drawString(x, y, h)
    c.setFont("Helvetica", 9)
    y -= 15
    for x, v in [(40, "VX-14"), (90, "1,200"), (140, "412.00"), (260, "494,400.00")]:
        c.drawString(x, y, v)
    y -= 24
    c.setFont("Helvetica-Bold", 10)
    c.drawString(40, y, "Total: USD 494,400.00")
    c.setFont("Helvetica", 8)
    c.drawString(40, 60, "We certify that this invoice is true and correct.   Authorised signatory, Vega Components Pvt Ltd")
    c.save()


def make_bol(path: Path, f: dict):
    c = canvas.Canvas(str(path), pagesize=A4)
    W, H = A4
    c.setFont("Helvetica-Bold", 15)
    c.drawString(40, H - 50, "BILL OF LADING")
    c.setFont("Helvetica", 8)
    c.drawString(40, H - 62, "For combined transport or port to port shipment      B/L No: NVLS2609881")
    c.rect(36, H - 470, W - 72, 395)
    y = H - 92
    rows = [("Shipper", ", ".join(SHIPPER)), ("Consignee", f.get("consignee_name")),
            ("Consignee Address", ", ".join(CONS_ADDR)), ("Notify Party", "Same as consignee"),
            ("Vessel / Voyage", "MSC AURORA / 614W"), ("Port of Loading", f.get("port_of_loading")),
            ("Port of Discharge", f.get("port_of_discharge")), ("Container No.", "MSCU 482913-7 (40HC)"),
            ("Marks & Numbers", "ACME/RTM/1-40"), ("Description of Goods", f.get("description_of_goods")),
            ("HS Code", f.get("hs_code")), ("Gross Weight", f.get("gross_weight")),
            ("Measurement", "58.20 CBM"), ("Freight", "Collect")]
    if f.get("incoterms"):
        rows.insert(7, ("Delivery Terms", f["incoterms"]))
    for label, val in rows:
        if val is None:
            continue
        _kv(c, 44, y, label, val)
        y -= 18
    c.setFont("Helvetica", 7)
    c.drawString(40, 80, "SHIPPED on board in apparent good order and condition. Place and date of issue: Nhava Sheva, 16-Sep-2026")
    c.save()


def degrade(pdf_path: Path, out_path: Path, rng: random.Random):
    """Simulate a phone photo / fax of a printout: low DPI, skew, blur, noise, stamp, JPEG."""
    pdf = pdfium.PdfDocument(str(pdf_path))
    img = pdf[0].render(scale=100 / 72).to_pil().convert("RGB")
    pdf.close()
    img = img.rotate(rng.uniform(1.2, 2.4), expand=True, fillcolor=(236, 234, 228))
    noise = Image.effect_noise(img.size, 28).convert("RGB")
    img = Image.blend(img, noise, 0.10)
    img = img.filter(ImageFilter.GaussianBlur(0.9))
    d = ImageDraw.Draw(img, "RGBA")
    w, h = img.size
    cx, cy = int(w * 0.52), int(h * 0.305)          # stamp over the lower field block
    d.ellipse([cx - 95, cy - 55, cx + 95, cy + 55], outline=(40, 70, 160, 170), width=5)
    try:
        font = ImageFont.load_default(size=22)
    except TypeError:
        font = ImageFont.load_default()
    d.text((cx - 62, cy - 12), "RECEIVED", fill=(40, 70, 160, 170), font=font)
    d.rectangle([0, int(h * 0.93), w, h], fill=(210, 205, 195, 255))   # cut-off footer
    img = img.resize((int(w * 0.85), int(h * 0.85)))
    img.save(out_path, "JPEG", quality=35)


def truth(path: Path, doc_type: str, f: dict, expected: str, acceptable: list[str], planted: list[str], notes=""):
    fields = {k: f.get(k) for k in BASE}
    if doc_type == "bill_of_lading":
        fields["invoice_number"] = None
        fields["incoterms"] = f.get("incoterms")
    data = {"file": path.name, "doc_type": doc_type, "customer_id": "acme_electronics", "fields": fields,
            "expected_decision": expected, "acceptable_decisions": acceptable,
            "planted_errors": planted, "notes": notes}
    path.with_suffix(".truth.json").write_text(json.dumps(data, indent=2))


def demo_set(out: Path):
    out.mkdir(parents=True, exist_ok=True)
    rng = random.Random(7)
    specs = [
        ("invoice_clean.pdf", "commercial_invoice", {}, "auto_approve", ["auto_approve"], [], "clean baseline"),
        ("invoice_errors.pdf", "commercial_invoice",
         {"incoterms": "CIF Rotterdam", "hs_code": "8528.72.00"}, "amendment", ["amendment"],
         ["incoterms", "hs_code"], "wrong Incoterm + HS code outside allowed headings"),
        ("invoice_weight_lbs.pdf", "commercial_invoice", {"gross_weight": "27,448 LBS"}, "amendment",
         ["amendment"], ["gross_weight"], "weight in LBS; customer requires KG"),
        ("invoice_consignee_alias.pdf", "commercial_invoice",
         {"consignee_name": "ACME ELECTRONICS PRIVATE LIMITED"}, "auto_approve", ["auto_approve"], [],
         "approved alias written differently - must NOT be flagged"),
        ("invoice_consignee_typo.pdf", "commercial_invoice", {"consignee_name": "Acme Electronix Pvt Ltd"},
         "human_review", ["human_review", "amendment"], ["consignee_name"], "near-match: typo or different entity?"),
        ("invoice_missing_incoterm.pdf", "commercial_invoice", {"incoterms": None}, "amendment",
         ["amendment", "human_review"], ["incoterms"], "required field absent - must not be invented"),
        ("bol_clean.pdf", "bill_of_lading", {}, "auto_approve", ["auto_approve"], [],
         "BL: invoice number legitimately absent"),
        ("bol_wrong_port.pdf", "bill_of_lading", {"port_of_discharge": "Antwerp, Belgium"}, "amendment",
         ["amendment"], ["port_of_discharge"], "discharge port not allowed"),
    ]
    for name, dt, overrides, exp, acc, planted, notes in specs:
        f = {**BASE, **overrides}
        p = out / name
        (make_bol if dt == "bill_of_lading" else make_invoice)(p, f)
        truth(p, dt, f, exp, acc, planted, notes)
    for src, dst, exp, acc, planted in [
        ("invoice_clean.pdf", "invoice_clean_scan.jpg", "auto_approve", ["auto_approve", "human_review"], []),
        ("invoice_errors.pdf", "invoice_errors_scan.jpg", "amendment", ["amendment", "human_review"],
         ["incoterms", "hs_code"]),
    ]:
        degrade(out / src, out / dst, rng)
        t = json.loads((out / src).with_suffix(".truth.json").read_text())
        t.update({"file": dst, "expected_decision": exp, "acceptable_decisions": acc,
                  "notes": "messy scan (skew, blur, noise, stamp, JPEG q35) of " + src})
        (out / dst).with_suffix(".truth.json").write_text(json.dumps(t, indent=2))
    print(f"Wrote {len(list(out.glob('*.truth.json')))} documents + ground truth to {out}")


ERRORS = {
    "incoterms": ["CIF Rotterdam", "EXW Pune", "CFR Hamburg"],
    "hs_code": ["8528.72.00", "8517.62.90", "9403.20.00"],
    "port_of_discharge": ["Antwerp, Belgium", "Felixstowe, UK"],
    "gross_weight": ["27,448 LBS", "31,200.00 KG"],
    "consignee_name": ["Acme Electrical Traders LLP", "Acme Electronix Pvt Ltd"],
}


def seed_set(out: Path, n: int):
    out.mkdir(parents=True, exist_ok=True)
    rng = random.Random(42)
    for i in range(n):
        f = dict(BASE)
        f["invoice_number"] = f"INV-2026-{300 + i:04d}"
        f["gross_weight"] = f"{rng.uniform(4000, 22000):,.2f} KG"
        planted = []
        if rng.random() < 0.45:
            for k in rng.sample(list(ERRORS), rng.choice([1, 1, 2])):
                f[k] = rng.choice(ERRORS[k])
                planted.append(k)
        p = out / f"seed_{i:03d}.pdf"
        make_invoice(p, f)
        truth(p, "commercial_invoice", f, "amendment" if planted else "auto_approve", [], planted)
    print(f"Wrote {n} seed invoices to {out}")


if __name__ == "__main__":
    ap = argparse.ArgumentParser()
    ap.add_argument("--seed-set", type=int, default=0, help="also generate N random invoices in data/seed")
    a = ap.parse_args()
    demo_set(ROOT / "data" / "samples")
    if a.seed_set:
        seed_set(ROOT / "data" / "seed", a.seed_set)
