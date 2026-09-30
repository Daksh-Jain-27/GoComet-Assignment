"""Turns the model's self-reported confidence into a calibrated one.

LLM self-confidence is poorly calibrated, so it is only ONE input. The final
confidence is the model's number, capped by independent checks:

  1. Grounding   - the value (or its evidence snippet) must actually appear in the
                   document's digital text layer. Not found -> probable hallucination -> cap 0.25.
                   (Against noisy OCR text a miss proves nothing -> treated as unverifiable, cap 0.70.)
  2. Format      - the value must look like what the field is (HS = 6-10 digits,
                   Incoterm in the 11 Incoterms 2020 terms, weight has a number...). Fail -> cap 0.40.
  3. Unverifiable- scan with no text layer / OCR: we cannot ground -> cap 0.70
                   (below threshold, so it surfaces as 'uncertain' unless a second
                   independent read agrees - see agents/extractor.refine).

Caps are deliberately simple and explainable; every field carries `signals`
so the UI/audit trail can show *why* a field is low-confidence.
"""
from __future__ import annotations

import re
from typing import Optional

from rapidfuzz import fuzz

from .rules import INCOTERMS_2020, find_incoterm, hs_digits, norm_text, parse_weight
from .schemas import ExtractedField, RawField

CAP_UNGROUNDED = 0.25
CAP_BAD_FORMAT = 0.40
CAP_UNVERIFIABLE = 0.70
CAP_ABSENT_NO_TEXT = 0.50
MIN_REFERENCE_CHARS = 60


def has_reference_text(doc_text: Optional[str]) -> bool:
    return bool(doc_text) and len(doc_text.strip()) >= MIN_REFERENCE_CHARS


def is_grounded(value: str, evidence: Optional[str], doc_text: str) -> tuple[bool, str]:
    doc = norm_text(doc_text)
    val = norm_text(value)
    if not val:
        return False, "empty value"
    # value itself appears in the document (fuzzy, to tolerate OCR noise / spacing)
    if len(val) >= 3 and fuzz.partial_ratio(val, doc) >= 92:
        return True, "value found in document text"
    if val.replace(" ", "") and val.replace(" ", "") in doc.replace(" ", ""):
        return True, "value found in document text (spacing-insensitive)"
    if evidence:
        ev = norm_text(evidence)
        if fuzz.partial_ratio(ev, doc) >= 90 and fuzz.partial_ratio(val, ev) >= 90:
            return True, "evidence snippet found in document and contains value"
        return False, "evidence snippet or value not found in document text"
    return False, "value not found in document text and no evidence given"


def format_check(field: str, value: str) -> tuple[bool, str]:
    if field == "hs_code":
        d = hs_digits(value)
        return (6 <= len(d) <= 10, f"{len(d)} digits (HS codes have 6-10)")
    if field == "incoterms":
        t = find_incoterm(value)
        return (t is not None, f"Incoterm {t}" if t else f"not one of {', '.join(INCOTERMS_2020)}")
    if field == "gross_weight":
        w = parse_weight(value)
        return (w is not None and w[0] > 0, "number + unit parsed" if w else "no number found")
    if field == "invoice_number":
        return (bool(re.search(r"\d", value)) and len(value) >= 3, "has digits")
    if field in ("port_of_loading", "port_of_discharge", "consignee_name", "description_of_goods"):
        return (len(value.strip()) >= 3, "non-trivial text")
    return True, "no format rule"


def calibrate(name: str, raw: RawField, doc_text: Optional[str],
              text_source: Optional[str] = None) -> ExtractedField:
    signals: dict = {"model_confidence": round(raw.confidence, 3)}
    conf = raw.confidence
    text_ok = has_reference_text(doc_text)

    if raw.value is None:
        # A claim of absence. Only a clean digital text layer can back it up: OCR of a
        # bad scan silently drops lines, so "not in the OCR text" != "not on the page".
        signals["absent"] = True
        if not text_ok or text_source != "pdf_text_layer":
            conf = min(conf, CAP_ABSENT_NO_TEXT)
            signals["cap"] = "absence claimed on a document we cannot fully read (scan/OCR)"
        return ExtractedField(name=name, value=None, confidence=round(conf, 3),
                              evidence=None, page=raw.page, signals=signals)

    fmt_ok, fmt_detail = format_check(name, raw.value)
    signals["format_ok"], signals["format_detail"] = fmt_ok, fmt_detail
    if not fmt_ok:
        conf = min(conf, CAP_BAD_FORMAT)

    if text_ok:
        grounded, detail = is_grounded(raw.value, raw.evidence, doc_text)  # type: ignore[arg-type]
        if grounded:
            signals["grounded"], signals["grounding_detail"] = True, detail
        elif text_source == "pdf_text_layer":
            # exact digital text and the value is not in it -> probable hallucination
            signals["grounded"], signals["grounding_detail"] = False, detail
            conf = min(conf, CAP_UNGROUNDED)
        else:
            # OCR of a bad scan is too noisy to prove a negative ('Acme Biecotics Pet Lid').
            # Treat as unverifiable, not as hallucination.
            signals["grounded"] = None
            signals["grounding_detail"] = "not found in OCR text, but OCR is noisy: unverifiable"
            conf = min(conf, CAP_UNVERIFIABLE)
    else:
        signals["grounded"] = None
        signals["grounding_detail"] = "no text layer/OCR: cannot verify against source"
        conf = min(conf, CAP_UNVERIFIABLE)

    return ExtractedField(name=name, value=raw.value, confidence=round(conf, 3),
                          evidence=raw.evidence, page=raw.page, signals=signals)
