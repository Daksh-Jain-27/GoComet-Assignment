"""Customer rules + reference data (Incoterms, UN/LOCODE ports) + normalisers.
Normalisers are shared by the Validator and the offline eval, so 'correct' means
the same thing in both places."""
from __future__ import annotations

import csv
import re
from functools import lru_cache
from pathlib import Path
from typing import Optional

import yaml
from rapidfuzz import fuzz

from .config import SETTINGS

INCOTERMS_2020 = ["EXW", "FCA", "CPT", "CIP", "DAP", "DPU", "DDP", "FAS", "FOB", "CFR", "CIF"]
_LBS_TO_KG = 0.45359237


# ---------- customer rules -------------------------------------------------
def list_customers() -> list[str]:
    return sorted(p.stem for p in SETTINGS.rules_dir.glob("*.yaml"))


@lru_cache(maxsize=32)
def load_rules(customer_id: str) -> dict:
    path = SETTINGS.rules_dir / f"{customer_id}.yaml"
    if not path.exists():
        raise FileNotFoundError(f"No rule set for customer '{customer_id}' at {path}")
    rules = yaml.safe_load(path.read_text())
    for key in ("customer_id", "confidence_threshold", "required_fields"):
        if key not in rules:
            raise ValueError(f"Rule set {path.name} is missing '{key}'")
    return rules


# ---------- text normalisation ----------------------------------------------
def norm_text(s: Optional[str]) -> str:
    if not s:
        return ""
    s = s.lower()
    s = re.sub(r"[^a-z0-9]+", " ", s)
    return re.sub(r"\s+", " ", s).strip()


_COMPANY_SUFFIXES = [
    (r"\bprivate\b", "pvt"), (r"\blimited\b", "ltd"), (r"\bincorporated\b", "inc"),
    (r"\bcorporation\b", "corp"), (r"\bcompany\b", "co"),
]


def norm_company(s: Optional[str]) -> str:
    s = norm_text(s)
    for pat, rep in _COMPANY_SUFFIXES:
        s = re.sub(pat, rep, s)
    return s


def hs_digits(s: Optional[str]) -> str:
    return re.sub(r"\D", "", s or "")


def find_incoterm(s: Optional[str]) -> Optional[str]:
    if not s:
        return None
    for tok in re.findall(r"[A-Za-z]{3}", s.upper()):
        if tok in INCOTERMS_2020:
            return tok
    return None


def parse_weight(s: Optional[str]) -> Optional[tuple[float, str]]:
    """'12,450.50 KG' -> (12450.5, 'KG'); '27,447 LBS' -> (27447.0, 'LBS')."""
    if not s:
        return None
    m = re.search(r"([\d][\d,]*\.?\d*)\s*(kgs?|kilograms?|lbs?|pounds?|mt|tonnes?)?", s, re.I)
    if not m:
        return None
    num = float(m.group(1).replace(",", ""))
    unit = (m.group(2) or "").lower()
    if unit.startswith(("kg", "kilo")):
        unit = "KG"
    elif unit.startswith(("lb", "pound")):
        unit = "LBS"
    elif unit in ("mt", "tonne", "tonnes"):
        unit = "MT"
    else:
        unit = "UNKNOWN"
    return num, unit


def weight_in_kg(num: float, unit: str) -> Optional[float]:
    return {"KG": num, "LBS": num * _LBS_TO_KG, "MT": num * 1000}.get(unit)


# ---------- ports (UN/LOCODE subset) ---------------------------------------
@lru_cache(maxsize=1)
def _ports() -> list[dict]:
    path = Path(__file__).parent / "reference" / "ports.csv"
    with path.open() as f:
        rows = list(csv.DictReader(f))
    for r in rows:
        r["names"] = [r["name"]] + [a for a in (r.get("aliases") or "").split("|") if a]
    return rows


def port_name(code: str) -> str:
    return next((p["name"] for p in _ports() if p["code"] == code), code)


def resolve_port(text: Optional[str]) -> tuple[Optional[str], float]:
    """Map free text ('Nhava Sheva, India', 'INNSA', 'JNPT') to a UN/LOCODE.
    Returns (code, score 0-100). None if nothing is close enough."""
    if not text:
        return None, 0.0
    upper = text.upper()
    for p in _ports():
        if re.search(rf"\b{p['code']}\b", upper):
            return p["code"], 100.0
    t = norm_text(text)
    best, best_score = None, 0.0
    for p in _ports():
        for name in p["names"]:
            score = fuzz.token_set_ratio(t, norm_text(name))
            if score > best_score:
                best, best_score = p["code"], score
    return (best, best_score) if best_score >= 88 else (None, best_score)


# ---------- canonical form (used by evals to compare to ground truth) ---------
def canonical(field: str, value: Optional[str]) -> Optional[str]:
    if value is None:
        return None
    if field == "hs_code":
        return hs_digits(value)
    if field in ("port_of_loading", "port_of_discharge"):
        code, _ = resolve_port(value)
        return code or norm_text(value)
    if field == "incoterms":
        return find_incoterm(value) or norm_text(value)
    if field == "gross_weight":
        w = parse_weight(value)
        return f"{w[0]:.1f} {w[1]}" if w else norm_text(value)
    if field == "consignee_name":
        return norm_company(value)
    if field == "invoice_number":
        return value.strip().upper()
    return norm_text(value)
