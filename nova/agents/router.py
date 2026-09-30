"""ROUTER / DECISION AGENT (planner of the next action).

Responsibility : choose ONE next action and explain it; draft the amendment email.
Input          : ValidationResult (+ ExtractionResult for context)
Output         : Decision

Split of labour:
  - The DECISION is a deterministic policy in code (auditable, testable, cannot be
    talked into approving). The LLM never decides.
  - The LANGUAGE (amendment email) is drafted by an LLM, then VERIFIED by code:
    every discrepancy must be mentioned. If not, or if the LLM fails / budget is
    exhausted, a template draft is used. The agent never sends anything.

Policy (first match wins):
  1. any MISMATCH (confident)    -> amendment      (SU must fix; uncertain fields go on CG's checklist)
  2. any UNCERTAIN               -> human_review   (CG decides; never silently approved)
  3. all MATCH, all conf >= thr  -> auto_approve
     3a. ...unless some values are unverifiable (scan, no reliable text) and the customer's
         policy `auto_approve_requires_grounding` is on (default) -> human_review
"""
from __future__ import annotations

from typing import Optional

from ..llm import BudgetExceeded, LLMClient, LLMError
from ..rules import norm_text
from ..schemas import DOC_TYPE_LABELS, FIELD_LABELS, Decision, ExtractionResult, FieldCheck, ValidationResult

DRAFT_PROMPT = """You draft amendment-request emails for a Cargo/Control Group (CG) operator.
Write to the shipper's documentation team (SU). Be brief, polite and specific.
List EVERY discrepancy given, each with: field name, what the document says, what is required.
Do not add discrepancies, values, deadlines or promises that are not in the input.
Do not mention AI, agents, confidence scores or internal systems.
Return ONLY JSON: {"subject": "...", "body": "..."}"""


def _fmt(c: FieldCheck) -> str:
    return f'{FIELD_LABELS[c.name]}: found "{c.found if c.found is not None else "missing"}" - expected {c.expected}'


def _template_draft(ex: ExtractionResult, mism: list[FieldCheck], customer: str) -> tuple[str, str]:
    inv = ex.get("invoice_number").value or "(no invoice no.)"
    dt = DOC_TYPE_LABELS.get(ex.doc_type, "Document")
    lines = [f"{i}. {FIELD_LABELS[c.name]}: the document shows "
             f'"{c.found if c.found is not None else "nothing (field missing)"}". Required: {c.expected}.'
             for i, c in enumerate(mism, 1)]
    subject = f"Amendment required: {dt} {inv}"
    body = ("Dear Documentation Team,\n\n"
            f"We have reviewed {dt.lower()} {inv} against the requirements of {customer}. "
            f"Please correct the following {len(mism)} item(s) and resend the document:\n\n"
            + "\n".join(lines) +
            "\n\nPlease reply to this email with the corrected document so we can complete validation.\n\n"
            "Regards,\nCG Team")
    return subject, body


def _verify_draft(body: str, mism: list[FieldCheck]) -> list[str]:
    """Code-level verifier: each discrepancy's field name and found value must appear."""
    b = norm_text(body)
    missing = []
    for c in mism:
        if norm_text(FIELD_LABELS[c.name]) not in b:
            missing.append(FIELD_LABELS[c.name])
        elif c.found and norm_text(c.found)[:20] not in b:
            missing.append(f"{FIELD_LABELS[c.name]} value")
    return missing


def _llm_draft(client: LLMClient, model: str, ex: ExtractionResult, mism: list[FieldCheck],
               customer: str) -> tuple[str, str]:
    payload = {"customer": customer, "document_type": DOC_TYPE_LABELS.get(ex.doc_type),
               "invoice_number": ex.get("invoice_number").value,
               "discrepancies": [{"field": FIELD_LABELS[c.name], "found": c.found or "missing",
                                  "required": c.expected, "why": c.reason} for c in mism]}
    import json
    data = client.complete_json(model=model, node="route_draft", max_tokens=700,
                                messages=[{"role": "system", "content": DRAFT_PROMPT},
                                          {"role": "user", "content": json.dumps(payload)}])
    return str(data["subject"]), str(data["body"])


def decide(ex: ExtractionResult, val: ValidationResult, rules: dict,
           client: Optional[LLMClient] = None, model: Optional[str] = None) -> tuple[Decision, list[str]]:
    errors: list[str] = []
    mism, unc = val.by_status("mismatch"), val.by_status("uncertain")
    customer = rules.get("customer_name", rules["customer_id"])
    trace = [f"{len(val.checks) - len(mism) - len(unc)} match, {len(mism)} mismatch, {len(unc)} uncertain "
             f"(threshold {val.threshold})"]
    checklist = [f"Verify {FIELD_LABELS[c.name]}: {c.reason}" for c in unc]

    if mism:
        action = "amendment"
        trace.append("Rule 1: at least one confident mismatch -> SU must amend")
        reasoning = (f"{len(mism)} field(s) confidently violate the rules for {customer}: "
                     + "; ".join(_fmt(c) for c in mism) + ".")
        if unc:
            reasoning += (f" {len(unc)} further field(s) are uncertain and are NOT in the draft; "
                          f"CG should verify them before sending: " + ", ".join(FIELD_LABELS[c.name] for c in unc) + ".")
    elif unc:
        action = "human_review"
        trace.append("Rule 2: no confident mismatch but uncertain fields -> human review (no silent approval)")
        reasoning = (f"No rule violations found, but {len(unc)} field(s) could not be confirmed: "
                     + "; ".join(f"{FIELD_LABELS[c.name]} ({c.reason})" for c in unc)
                     + ". A CG operator must check these against the document before approval.")
    else:
        unverified = [f.name for f in ex.fields if f.value is not None and f.signals.get("grounded") is not True]
        if unverified and rules.get("auto_approve_requires_grounding", True):
            action = "human_review"
            trace.append("Rule 3a: all fields match, but some values could not be verified against a text layer "
                         "(scan) and this customer's policy requires grounding for auto-approval -> human review")
            reasoning = (f"All fields match the rules for {customer}, but {len(unverified)} value(s) come from a scan and "
                         f"could not be verified against the document text: "
                         + ", ".join(FIELD_LABELS[n] for n in unverified)
                         + ". Two independent reads agreed where shown; CG confirms before approval.")
            checklist += [f"Confirm {FIELD_LABELS[n]} on the scan" for n in unverified]
        else:
            action = "auto_approve"
            trace.append("Rule 3: all fields match with confidence >= threshold -> auto-approve")
            reasoning = (f"All {len(val.checks)} fields match the rules for {customer} with confidence >= {val.threshold}. "
                         f"Lowest field confidence: {min(c.confidence for c in val.checks):.2f}.")

    # Hard invariant, enforced in code rather than trusted to prompts or thresholds upstream.
    if action == "auto_approve":
        assert all(c.status == "match" for c in val.checks), "auto_approve with non-matching field"
        assert all(c.confidence >= val.threshold or c.found is None for c in val.checks), \
            "auto_approve with low-confidence field"

    subject = draft = drafted_by = None
    if action == "amendment":
        if client is not None and model:
            try:
                subject, draft = _llm_draft(client, model, ex, mism, customer)
                missing = _verify_draft(draft, mism)
                if missing:
                    errors.append(f"LLM draft omitted {missing}; replaced with template draft")
                    subject = draft = None
                else:
                    drafted_by = "llm"
            except (LLMError, BudgetExceeded, KeyError, TypeError) as e:
                errors.append(f"LLM drafting failed ({type(e).__name__}); used template draft")
        if draft is None:
            subject, draft = _template_draft(ex, mism, customer)
            drafted_by = "template"
        trace.append(f"Draft by {drafted_by}; verified to mention all {len(mism)} discrepancies; NOT sent")

    return Decision(doc_id=val.doc_id, action=action, reasoning=reasoning, policy_trace=trace,
                    cg_checklist=checklist, amendment_subject=subject, amendment_draft=draft,
                    drafted_by=drafted_by), errors
