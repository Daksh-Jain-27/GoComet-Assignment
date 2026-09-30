"""Typed contracts between agents. Every handoff is validated against these
models, so a malformed output fails at the boundary instead of three steps later."""
from __future__ import annotations

from typing import Any, Literal, Optional

from pydantic import BaseModel, Field, field_validator

FIELD_NAMES = [
    "consignee_name", "hs_code", "port_of_loading", "port_of_discharge",
    "incoterms", "description_of_goods", "gross_weight", "invoice_number",
]
FIELD_LABELS = {
    "consignee_name": "Consignee name",
    "hs_code": "HS code",
    "port_of_loading": "Port of loading",
    "port_of_discharge": "Port of discharge",
    "incoterms": "Incoterms",
    "description_of_goods": "Description of goods",
    "gross_weight": "Gross weight",
    "invoice_number": "Invoice number",
}
DOC_TYPES = ["commercial_invoice", "bill_of_lading", "packing_list", "certificate_of_origin", "unknown"]
DOC_TYPE_LABELS = {
    "commercial_invoice": "Commercial invoice", "bill_of_lading": "Bill of lading",
    "packing_list": "Packing list", "certificate_of_origin": "Certificate of origin",
    "unknown": "Document",
}


# ---------- Extractor: raw model output (untrusted) -------------------------
class RawField(BaseModel):
    value: Optional[str] = None
    evidence: Optional[str] = None
    page: Optional[int] = None
    confidence: float = Field(0.0, ge=0.0, le=1.0)

    @field_validator("value", "evidence", mode="before")
    @classmethod
    def _to_str(cls, v: Any):
        if v is None:
            return None
        s = str(v).strip()
        return None if s.lower() in ("", "null", "none", "n/a") else s

    @field_validator("confidence", mode="before")
    @classmethod
    def _clip(cls, v: Any):
        try:
            return min(1.0, max(0.0, float(v)))
        except (TypeError, ValueError):
            return 0.0

    @field_validator("page", mode="before")
    @classmethod
    def _page(cls, v: Any):
        try:
            return int(v) if v is not None else None
        except (TypeError, ValueError):
            return None


class RawExtraction(BaseModel):
    doc_type: str = "unknown"
    fields: dict[str, RawField] = Field(default_factory=dict)

    @field_validator("doc_type", mode="before")
    @classmethod
    def _dt(cls, v: Any):
        v = str(v or "unknown").lower().strip().replace(" ", "_")
        return v if v in DOC_TYPES else "unknown"

    def complete(self) -> "RawExtraction":
        """Missing keys become explicit nulls with zero confidence (never dropped silently)."""
        for n in FIELD_NAMES:
            self.fields.setdefault(n, RawField())
        return self


# ---------- Extractor: calibrated output (what downstream trusts) -----------
class ExtractedField(BaseModel):
    name: str
    value: Optional[str]
    confidence: float
    evidence: Optional[str] = None
    page: Optional[int] = None
    signals: dict[str, Any] = Field(default_factory=dict)  # why the confidence is what it is


class ExtractionResult(BaseModel):
    doc_id: str
    doc_type: str
    fields: list[ExtractedField]
    extractor: str
    passes: int = 1
    text_source: Optional[str] = None
    warnings: list[str] = Field(default_factory=list)

    def get(self, name: str) -> ExtractedField:
        return next(f for f in self.fields if f.name == name)


# ---------- Validator ------------------------------------------------------
FieldStatus = Literal["match", "mismatch", "uncertain"]


class FieldCheck(BaseModel):
    name: str
    status: FieldStatus
    found: Optional[str]
    expected: Optional[str]
    rule: str
    reason: str
    confidence: float
    evidence: Optional[str] = None
    page: Optional[int] = None


class ValidationResult(BaseModel):
    doc_id: str
    customer_id: str
    doc_type: str
    threshold: float
    checks: list[FieldCheck]

    def by_status(self, status: str) -> list[FieldCheck]:
        return [c for c in self.checks if c.status == status]


# ---------- Router ---------------------------------------------------------
Action = Literal["auto_approve", "human_review", "amendment"]


class Decision(BaseModel):
    doc_id: str
    action: Action
    reasoning: str
    policy_trace: list[str]
    cg_checklist: list[str] = Field(default_factory=list)   # what CG must verify by hand
    amendment_subject: Optional[str] = None
    amendment_draft: Optional[str] = None
    drafted_by: Optional[str] = None                        # "llm" | "template"
