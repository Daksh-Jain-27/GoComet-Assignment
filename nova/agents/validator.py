"""VALIDATOR AGENT (verifier).

Responsibility : compare extracted fields to ONE customer's rule set, field by field.
Input          : ExtractionResult + customer rules (YAML)
Output         : ValidationResult (match / mismatch / uncertain + found vs expected + reason)

Deliberately DETERMINISTIC - no LLM. Rule checks are exact logic; an LLM here would
add cost, latency and non-determinism to the one step that must be auditable and
reproducible ("why was this rejected?" must have the same answer tomorrow).
Fuzzy matching (company names, port names) uses rapidfuzz with explicit thresholds.

Safety invariant: a field below the confidence threshold is ALWAYS 'uncertain',
even if its value happens to satisfy the rule. Nothing is silently approved.
"""
from __future__ import annotations

import re
from typing import Callable, Optional

from rapidfuzz import fuzz

from ..rules import (find_incoterm, hs_digits, norm_company, norm_text, parse_weight, port_name,
                     resolve_port, weight_in_kg)
from ..schemas import FIELD_NAMES, ExtractionResult, FieldCheck, ValidationResult

# (ok: True/False/None, expected: str, reason: str). None = rule cannot decide -> uncertain
CheckOut = tuple[Optional[bool], str, str]


def _consignee(v: str, r: dict) -> CheckOut:
    cfg = r["consignee_name"]
    names = [cfg["expected"]] + cfg.get("aliases", [])
    got = norm_company(v)
    best = max(fuzz.token_sort_ratio(got, norm_company(n)) for n in names)
    exp = cfg["expected"]
    if got in {norm_company(n) for n in names}:
        return True, exp, "matches expected consignee (after normalising Pvt/Private, Ltd/Limited)"
    if best >= 97:
        return True, exp, f"matches expected consignee (similarity {best:.0f})"
    if best >= 80:
        return None, exp, f"near-match (similarity {best:.0f}): could be a typo or a different entity - human check"
    return False, exp, f"different consignee (similarity {best:.0f})"


def _hs(v: str, r: dict) -> CheckOut:
    d, pref = hs_digits(v), r["hs_code"]["allowed_prefixes"]
    exp = "starts with " + " or ".join(pref)
    if not 6 <= len(d) <= 10:
        return False, exp, f"'{v}' is not a valid HS code ({len(d)} digits; must be 6-10)"
    if any(d.startswith(p) for p in pref):
        return True, exp, f"HS {d} is within allowed headings"
    return False, exp, f"HS {d} (chapter {d[:2]}, heading {d[:4]}) is outside allowed headings"


def _incoterm(v: str, r: dict) -> CheckOut:
    allowed = r["incoterms"]["allowed"]
    t = find_incoterm(v)
    exp = " / ".join(allowed)
    if t is None:
        return False, exp, f"'{v}' is not a valid Incoterms 2020 term"
    return (t in allowed, exp, f"Incoterm {t} {'is' if t in allowed else 'is not'} contractually agreed")


def _port(key: str) -> Callable[[str, dict], CheckOut]:
    def check(v: str, r: dict) -> CheckOut:
        allowed = r[key]["allowed"]
        exp = " or ".join(f"{port_name(c)} ({c})" for c in allowed)
        code, score = resolve_port(v)
        if code is None:
            return None, exp, f"could not map '{v}' to a known UN/LOCODE (best score {score:.0f}) - human check"
        return (code in allowed, exp,
                f"resolved to {port_name(code)} ({code}); {'allowed' if code in allowed else 'not an allowed port'}")
    return check


def _weight(v: str, r: dict) -> CheckOut:
    cfg = r["gross_weight"]
    unit, mx = cfg["unit"].upper(), cfg.get("max")
    exp = f"in {unit}" + (f", <= {mx:,} {unit}" if mx else "")
    w = parse_weight(v)
    if not w:
        return False, exp, "no numeric weight found"
    num, u = w
    if u == "UNKNOWN":
        return None, exp, "weight has no unit - human check"
    if u != unit:
        kg = weight_in_kg(num, u)
        conv = f" (= {kg:,.1f} KG)" if kg is not None and unit == "KG" else ""
        return False, exp, f"stated in {u}{conv}; customer requires {unit}"
    if mx and num > mx:
        return False, exp, f"{num:,.1f} {unit} exceeds max {mx:,} {unit}"
    return True, exp, f"{num:,.1f} {unit} within limits"


def _invoice(v: str, r: dict) -> CheckOut:
    pat = r["invoice_number"]["pattern"]
    ok = re.match(pat, v.strip()) is not None
    return ok, f"format {pat}", "format ok" if ok else "does not match the customer's invoice number format"


def _description(v: str, r: dict) -> CheckOut:
    kws = r["description_of_goods"]["keywords"]
    t = norm_text(v)
    hit = [k for k in kws if k in t]
    if hit:
        return True, "goods consistent with contract (" + ", ".join(kws[:4]) + "...)", f"mentions: {', '.join(hit)}"
    # semantic mismatch can't be decided by keywords -> human, not auto-reject
    return None, "goods consistent with contract", "no contract keyword found; needs a human look"


CHECKS: dict[str, tuple[str, Callable[[str, dict], CheckOut]]] = {
    "consignee_name": ("consignee must equal expected name or an approved alias", _consignee),
    "hs_code": ("HS code must start with an allowed heading", _hs),
    "port_of_loading": ("port of loading must be an allowed UN/LOCODE", _port("port_of_loading")),
    "port_of_discharge": ("port of discharge must be an allowed UN/LOCODE", _port("port_of_discharge")),
    "incoterms": ("Incoterm must be one of the agreed terms", _incoterm),
    "description_of_goods": ("goods must match contract categories", _description),
    "gross_weight": ("gross weight must be in required unit and under max", _weight),
    "invoice_number": ("invoice number must match customer format", _invoice),
}


def validate(ex: ExtractionResult, rules: dict) -> ValidationResult:
    thr = float(rules["confidence_threshold"])
    required = set(rules["required_fields"].get(ex.doc_type, rules["required_fields"]["unknown"]))
    checks: list[FieldCheck] = []
    for name in FIELD_NAMES:
        f = ex.get(name)
        rule_text, fn = CHECKS[name]
        base = dict(name=name, found=f.value, rule=rule_text, confidence=f.confidence,
                    evidence=f.evidence, page=f.page)

        if f.value is None:
            if name not in required:
                checks.append(FieldCheck(**base, status="match", expected="not required on this document type",
                                         reason=f"not present; not required for {ex.doc_type}"))
            elif f.confidence >= thr:
                checks.append(FieldCheck(**base, status="mismatch", expected="present on document",
                                         reason="required field is missing from the document"))
            else:
                checks.append(FieldCheck(**base, status="uncertain", expected="present on document",
                                         reason=f"not found, but extraction confidence {f.confidence:.2f} < {thr} "
                                                f"- may be present but unreadable"))
            continue

        ok, expected, why = fn(f.value, rules)
        if f.confidence < thr:
            # SAFETY INVARIANT: low confidence always surfaces, whatever the rule says.
            verdict = {True: "would match", False: "would NOT match", None: "undetermined"}[ok]
            checks.append(FieldCheck(**base, status="uncertain", expected=expected,
                                     reason=f"low extraction confidence {f.confidence:.2f} < {thr}; rule {verdict}: {why}"))
        elif ok is None:
            checks.append(FieldCheck(**base, status="uncertain", expected=expected, reason=why))
        else:
            checks.append(FieldCheck(**base, status="match" if ok else "mismatch", expected=expected, reason=why))
    return ValidationResult(doc_id=ex.doc_id, customer_id=rules["customer_id"], doc_type=ex.doc_type,
                            threshold=thr, checks=checks)
