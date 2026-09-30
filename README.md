# Nova DAW - Multi-agent trade document pipeline (Part 1 POC)

Extractor -> Validator -> Router over trade documents (PDF or image), with storage,
natural-language queries over results, and a CG operator screen.

```
            +-----------+   ExtractionResult   +-----------+  ValidationResult  +-----------+
 PDF/image  | Extractor | -------------------> | Validator | -----------------> |  Router   | --> Decision
 --------> | (vision   |   value, evidence,   | (rules in |  match/mismatch/   | (policy + |    + draft (never sent)
  ingest    |  LLM)     |   page, calibrated   |  code)    |  uncertain, found  |  drafting)|        |
            +-----+-----+   confidence         +-----------+  vs expected       +-----------+        v
                  | low-confidence fields only                                              SQLite store
                  v (max once, budget-checked)                                            (documents, fields,
            +-----------+                                                                  llm_calls, feedback)
            |  Refine   |  stronger model, higher DPI, agreement check                        |
            +-----------+                                                                       v
   LangGraph StateGraph; SqliteSaver checkpoint after every node (crash -> resume)        NL query (guarded SQL)
```

## Quick start (no API key needed)

```bash
python -m venv .venv && source .venv/bin/activate      # Windows: .venv\Scripts\activate
pip install -r requirements.txt
cp .env.example .env                                   # NOVA_MODE=offline by default

python scripts/generate_samples.py --seed-set 30       # 10 demo docs + ground truth, 30 seed docs
python scripts/seed_history.py                         # 4 weeks of history for NL queries
python scripts/run_pipeline.py data/samples/invoice_errors.pdf
streamlit run app/ui.py                                # operator screen at http://localhost:8501
```

Offline mode uses a deterministic text-layer parser instead of the vision LLM. It works on
digital PDFs; on scans it cannot read reliably, so everything becomes `uncertain` and goes
to human review. That is intentional: it fails loud instead of guessing.

## LLM mode (the real extractor)

Set in `.env`:
```
NOVA_MODE=llm
EXTRACTOR_MODEL=gemini/gemini-2.5-flash     # any LiteLLM vision model string
FALLBACK_MODEL=gemini/gemini-2.5-pro        # stronger model, used only for low-confidence fields
TEXT_MODEL=gemini/gemini-2.5-flash          # drafting + NL->SQL
GEMINI_API_KEY=...
```
Model names change; check your provider's current list. Any provider LiteLLM supports works
(set the matching key). Optional: `pip install pytesseract` + the tesseract binary gives scans an
OCR text layer; optional Langfuse keys turn on LLM tracing.

## Commands

| What | Command |
|---|---|
| Run one document | `python scripts/run_pipeline.py <file> [--mode llm] [--force] [--json]` |
| Crash-recovery demo | `NOVA_CRASH_AT=validate python scripts/run_pipeline.py <file>` then run again without the variable: it resumes at `validate` |
| Ask a question | `python scripts/ask.py "how many shipments were flagged this week?"` |
| Offline eval | `python evals/run_eval.py [--mode llm]` -> `evals/results/latest_<mode>.md` |
| Tests (LLM faked, free) | `python -m pytest -q tests` |
| UI | `streamlit run app/ui.py` |

## Sample queries (offline templates; free-form in LLM mode)

- how many shipments were flagged this week?
- show me everything pending review for Acme
- what are the most common mismatches this month?
- how many were auto-approved in the last 7 days?
- what is the average cost per document?

Every answer shows the SQL it ran and the rows it returned.

## Sample documents (`data/samples`, each with `.truth.json`)

| File | What it tests | Expected |
|---|---|---|
| invoice_clean.pdf | baseline | auto_approve |
| invoice_errors.pdf | wrong Incoterm + HS outside allowed headings | amendment |
| invoice_weight_lbs.pdf | weight in LBS, customer requires KG | amendment |
| invoice_consignee_alias.pdf | approved alias in different wording | auto_approve (must not flag) |
| invoice_consignee_typo.pdf | near-match name | human_review |
| invoice_missing_incoterm.pdf | required field absent (must not be invented) | amendment |
| bol_clean.pdf | BL without invoice no. (not required on a BL) | auto_approve |
| bol_wrong_port.pdf | discharge port not allowed | amendment |
| invoice_clean_scan.jpg | messy scan: skew, blur, noise, stamp, JPEG | human_review or auto_approve |
| invoice_errors_scan.jpg | messy scan of the error invoice | amendment or human_review |

## Project structure

```
nova/
  config.py          settings from .env
  schemas.py         Pydantic contracts for every agent handoff
  rules.py           customer rule loader, Incoterms, UN/LOCODE ports, normalisers
  grounding.py       confidence calibration: grounding + format caps
  docio.py           PDF/image -> page PNGs + text layer (+ optional OCR)
  llm.py             LiteLLM gateway: budget, retries, timeout, JSON repair, call log
  agents/extractor.py  vision extraction, text-layer fallback, refine (2nd read)
  agents/validator.py  deterministic rule checks
  agents/router.py     decision policy + verified amendment draft
  graph.py           LangGraph wiring + SQLite checkpointer + run_document()
  storage.py         SQLite schema, persistence, feedback
  query.py           NL -> guarded SQL -> grounded answer
  metrics.py         north star + supporting metrics from the store
  reference/ports.csv
rules/acme_electronics.yaml   customer rule set (configuration, not code)
scripts/            generate_samples, run_pipeline, seed_history, ask
evals/run_eval.py   offline eval against ground truth
tests/              behavioural tests of the trust guarantees (LLM faked)
app/ui.py           Streamlit CG screen
docs/DESIGN_NOTES.md  PRD sections 4-7 notes, mapped to code
notes.md            failure log (feeds the write-up)
```
