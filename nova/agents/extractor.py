"""EXTRACTOR AGENT (executor / perception).

Responsibility : read ONE document, return the 8 fields with value, evidence, page
                 and a CALIBRATED confidence. It does not know customer rules.
Input          : doc dict from docio.load_document (page image paths + text layer)
Output         : ExtractionResult

Two implementations behind one interface:
  - llm_extract       : vision LLM (default). Structured JSON output.
  - text_layer_extract: deterministic label parser for digital PDFs. Used in
                        offline mode, and as the fallback when the LLM is down or
                        over budget. On scans it returns nothing -> everything
                        'uncertain' -> human review (fails loud, never guesses).
Plus `refine`: a second, independent read of ONLY the low-confidence fields by a
stronger model at higher DPI; agreement raises confidence, disagreement lowers it.
"""
from __future__ import annotations

import json
import re
from typing import Optional

from ..grounding import calibrate, has_reference_text
from ..llm import LLMClient, image_part
from ..rules import canonical
from ..schemas import FIELD_LABELS, FIELD_NAMES, ExtractedField, ExtractionResult, RawExtraction, RawField

SYSTEM_PROMPT = """You are the Extractor agent in a trade-document validation pipeline.
You read ONE trade document (page images, plus its machine text layer when available)
and return specific fields as JSON. Your output is checked against the source by code.

Rules:
1. Copy each value exactly as printed. Do not correct, translate, reformat or infer.
2. For every non-null value, "evidence" must be a short verbatim snippet (5-25 words)
   copied from the document that contains the value.
3. If a field is not printed on the document, return "value": null. Never infer a field
   from other fields (do not guess the port from an address, the Incoterm from freight
   terms, or an HS code from the goods description).
4. "confidence" (0-1) = how sure you are the value is exactly right as printed.
   Lower it for blurred, stamped, handwritten, cut-off or partially hidden text.
5. "page" = 1-based page number where the value appears.
6. "doc_type" is one of: commercial_invoice, bill_of_lading, packing_list,
   certificate_of_origin, unknown.
Return ONLY a JSON object with this shape:
{"doc_type": "...", "fields": {"<field>": {"value": str|null, "evidence": str|null, "page": int|null, "confidence": float}}}
"""

FIELD_GUIDE = {
    "consignee_name": "the consignee / buyer company name (name only, not address)",
    "hs_code": "Harmonized System (HS / HSN) tariff code of the goods",
    "port_of_loading": "port where goods are loaded (origin port)",
    "port_of_discharge": "port where goods are discharged (destination port)",
    "incoterms": "Incoterms / terms of delivery, e.g. 'FOB Nhava Sheva'",
    "description_of_goods": "description of the goods",
    "gross_weight": "total gross weight INCLUDING the unit exactly as printed",
    "invoice_number": "commercial invoice number",
}


def _user_content(doc: dict, pages: list[str], fields: list[str], note: str = "") -> list[dict]:
    text = (doc.get("text") or "")[:12000]
    guide = "\n".join(f'- "{f}": {FIELD_GUIDE[f]}' for f in fields)
    parts: list[dict] = [{"type": "text", "text":
        f"Fields to extract:\n{guide}\n\n{note}\n"
        f"Machine text layer (may be empty, incomplete or out of order):\n<<<\n{text or '[none - scanned image]'}\n>>>"}]
    parts += [image_part(p) for p in pages]
    return parts


def _to_result(doc_id: str, raw: RawExtraction, doc: dict, extractor: str, warnings: list[str]) -> ExtractionResult:
    raw = raw.complete()
    fields = [calibrate(n, raw.fields[n], doc.get("text"), doc.get("text_source")) for n in FIELD_NAMES]
    return ExtractionResult(doc_id=doc_id, doc_type=raw.doc_type, fields=fields, extractor=extractor,
                            text_source=doc.get("text_source"), warnings=list(doc.get("warnings", [])) + warnings)


def llm_extract(doc_id: str, doc: dict, client: LLMClient, model: str) -> ExtractionResult:
    data = client.complete_json(
        model=model, node="extract",
        messages=[{"role": "system", "content": SYSTEM_PROMPT},
                  {"role": "user", "content": _user_content(doc, doc["pages"], FIELD_NAMES)}])
    raw = RawExtraction.model_validate(data)   # schema boundary: bad shape -> exception
    return _to_result(doc_id, raw, doc, model, [])


# ---------------- deterministic text-layer extractor ------------------------
_LABELS: dict[str, list[str]] = {
    "consignee_name": [r"consignee(?:\s*name)?", r"sold\s*to", r"buyer"],
    "hs_code": [r"hs\s*code", r"hsn(?:\s*code)?", r"tariff\s*code"],
    "port_of_loading": [r"port\s*of\s*loading", r"loading\s*port", r"pol"],
    "port_of_discharge": [r"port\s*of\s*discharge", r"discharge\s*port", r"pod", r"destination\s*port"],
    "incoterms": [r"incoterms?(?:\s*2020)?", r"terms\s*of\s*delivery", r"delivery\s*terms"],
    "description_of_goods": [r"description\s*of\s*goods", r"goods\s*description", r"description"],
    "gross_weight": [r"(?:total\s*)?gross\s*weight", r"gross\s*wt\.?"],
    "invoice_number": [r"invoice\s*(?:no\.?|number|#)", r"inv\.?\s*no\.?"],
}


def _doc_type_from_text(text: str) -> str:
    t = text.lower()
    if "bill of lading" in t:
        return "bill_of_lading"
    if "packing list" in t:
        return "packing_list"
    if "certificate of origin" in t:
        return "certificate_of_origin"
    if "invoice" in t:
        return "commercial_invoice"
    return "unknown"


def text_layer_extract(doc_id: str, doc: dict, extra_warning: Optional[str] = None) -> ExtractionResult:
    text = doc.get("text") or ""
    lines = [ln.strip() for ln in text.splitlines() if ln.strip()]
    raw = RawExtraction(doc_type=_doc_type_from_text(text))
    usable = has_reference_text(text)
    # A digital text layer is exact. OCR of a scan is not: garbled values ("Nnave Sheva")
    # and dropped lines are normal, so parser output on OCR text starts BELOW threshold.
    digital = doc.get("text_source") == "pdf_text_layer"
    found_conf, absent_conf = (0.92, 0.90) if digital else (0.60, 0.30)
    for name, labels in _LABELS.items():
        found: Optional[RawField] = None
        for label in labels:
            rx = re.compile(rf"^\s*{label}\s*[:\-]\s*(.+)$", re.I)
            for ln in lines:
                m = rx.match(ln)
                if m and m.group(1).strip():
                    found = RawField(value=m.group(1).strip(), evidence=ln, page=1, confidence=found_conf)
                    break
            if found:
                break
        # absence on a readable text layer is fairly reliable; on a scan it means nothing
        raw.fields[name] = found or RawField(value=None, confidence=absent_conf if usable else 0.0)
    warns = [extra_warning] if extra_warning else []
    return _to_result(doc_id, raw, doc, "text-layer-parser", warns)


# ---------------- refinement: second independent read ----------------------
def low_confidence_fields(ex: ExtractionResult, threshold: float) -> list[str]:
    return [f.name for f in ex.fields if f.confidence < threshold]


def refine(doc_id: str, ex: ExtractionResult, doc_hi: dict, client: LLMClient, model: str,
           fields: list[str], independent: bool = True) -> ExtractionResult:
    """Re-read only `fields` with a stronger model on higher-DPI pages, then merge:
       - both reads agree            -> keep, confidence raised (bounded, still capped if ungrounded)
       - reads disagree              -> keep first value, confidence <= 0.40, alt value recorded
       - first null, second found    -> take second, calibrated on its own
    """
    note = ("This is an independent second reading. Only these fields were unclear in a first pass; "
            "read them carefully. If still unreadable, return null with low confidence.")
    data = client.complete_json(
        model=model, node="refine",
        messages=[{"role": "system", "content": SYSTEM_PROMPT},
                  {"role": "user", "content": _user_content(doc_hi, doc_hi["pages"], fields, note)}])
    second = RawExtraction.model_validate(data).complete()

    merged: list[ExtractedField] = []
    for f in ex.fields:
        if f.name not in fields:
            merged.append(f)
            continue
        r2 = second.fields[f.name]
        f2 = calibrate(f.name, r2, doc_hi.get("text"), doc_hi.get("text_source"))
        sig = dict(f.signals)
        sig["second_read"] = {"value": r2.value, "model_confidence": r2.confidence, "model": model}
        if f.value is None and f2.value is not None:
            f2.signals.update({"second_read_only": True, "first_read": None})
            merged.append(f2)
            continue
        same = canonical(f.name, f.value) == canonical(f.name, r2.value)
        sig["agreement"] = same
        if same and f.value is not None:
            ungrounded = f.signals.get("grounded") is False
            max_boost = 0.90 if independent else 0.80
            boosted = min(max_boost, (f.signals.get("model_confidence", f.confidence) + r2.confidence) / 2)
            sig["independent_second_read"] = independent
            conf = f.confidence if ungrounded or not f.signals.get("format_ok", True) else max(f.confidence, boosted)
            merged.append(f.model_copy(update={"confidence": round(conf, 3), "signals": sig}))
        elif same:  # both say absent
            merged.append(f.model_copy(update={"signals": sig}))
        else:
            merged.append(f.model_copy(update={"confidence": round(min(f.confidence, 0.40), 3), "signals": sig}))
    return ex.model_copy(update={"fields": merged, "passes": ex.passes + 1,
                                 "extractor": f"{ex.extractor} + {model} (refine)"})


def summarize(ex: ExtractionResult) -> str:
    return json.dumps({f.name: [f.value, f.confidence] for f in ex.fields}, ensure_ascii=False)


LABELS = FIELD_LABELS
