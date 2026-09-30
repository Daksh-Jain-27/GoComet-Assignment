# Failure log

Log every surprise here as you test: what happened, how you found it, the fix, and how it's
tested now. The write-up asks for real failures from your own testing, so reproduce these on
your machine and add your own, especially from LLM mode.

## F1: Absence claimed on a bad scan turned into confident "missing field" amendments
- Found: `invoice_errors_scan.jpg` in offline mode, with tesseract installed.
- What happened: OCR garbled or dropped lines. The parser said 5 fields were "not on document"
  with 0.90 confidence, so the Router drafted an amendment asking SU to add fields that were
  actually printed on the page. That would have sent SU a wrong request.
- Fix: absence is trusted only on a digital text layer. Parser confidence on OCR text starts
  below threshold (0.60 found, 0.30 absent). `grounding.calibrate` caps absence claims on
  scans and OCR at 0.50.
- Now: the scan goes to human_review with a per-field checklist.

## F2: Correct LLM values flagged as hallucinations because the OCR was noisy
- Found: faked-LLM test on `invoice_clean_scan.jpg`. OCR read "Acme Electronics Pvt Ltd" as
  "Acme Biecotics Pet Lid", so grounding said "not in document" and capped 7 correct values at 0.25.
- Why it matters: it was safe (human review), but it told CG "probable hallucination" about
  correct values. That destroys trust in the flags.
- Fix: only a digital text layer can prove a negative. Against OCR text, a miss means
  "unverifiable" (cap 0.70), and a second independent read can lift it. Customer policy
  `auto_approve_requires_grounding` still keeps scans in front of CG during the pilot.
- Test: `test_scan_refine_agreement_raises_confidence_but_policy_keeps_cg_in_loop`.

## F3: Provider outage (Gemini 503 "high demand") during extraction
- Found: first LLM-mode run of invoice_errors.pdf. Extraction failed 3 times (20 s lost);
  the pipeline fell back to the text-layer parser and logged it in `errors`.
- Why it mattered: the result was correct only because the doc was a digital PDF. On a scan
  the parser reads nothing, so the doc would go to human review. Safe, but no automation.
  Also, the retry waits (1 s, 2 s) were too short for an overloaded provider.
- Fix: longer back-off on 503/429 (10 s, 20 s); model routing: primary Flash -> Pro fallback
  -> parser, with each downgrade logged. Also moved drafting to Flash-Lite (route took 27 s).
- Production version: LiteLLM router with provider-level fallbacks (e.g. a second provider),
  and an alert if the fallback rate goes above a threshold.
