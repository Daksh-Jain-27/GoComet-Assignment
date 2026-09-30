# Offline eval - mode `llm` - 2026-09-30T17:24:26

| Metric | Value |
|---|---|
| documents | 10 |
| field_accuracy | 1.0 |
| hallucination_rate | 0.0 |
| miss_rate | 0.0 |
| decision_accuracy | 1.0 |
| false_auto_approvals | 0 |
| avg_cost_usd | 0.00231 |
| avg_latency_ms | 4650.3 |

## Calibration (accuracy per confidence bucket)

| Confidence | n | Accuracy |
|---|---|---|
| 0.00-0.50 | 0 | None |
| 0.50-0.85 | 16 | 1.0 |
| 0.85-0.95 | 0 | None |
| 0.95-1.00 | 64 | 1.0 |

## Per field

| Field | Accuracy |
|---|---|
| consignee_name | 1.0 |
| hs_code | 1.0 |
| port_of_loading | 1.0 |
| port_of_discharge | 1.0 |
| incoterms | 1.0 |
| description_of_goods | 1.0 |
| gross_weight | 1.0 |
| invoice_number | 1.0 |

## Per document

| File | Decision | Acceptable | OK | False auto-approve | Field acc | Notes |
|---|---|---|---|---|---|---|
| bol_clean.pdf | auto_approve | auto_approve | yes | no | 1.0 | BL: invoice number legitimately absent |
| bol_wrong_port.pdf | amendment | amendment | yes | no | 1.0 | discharge port not allowed |
| invoice_clean.pdf | auto_approve | auto_approve | yes | no | 1.0 | clean baseline |
| invoice_clean_scan.jpg | human_review | auto_approve, human_review | yes | no | 1.0 | messy scan (skew, blur, noise, stamp, JPEG q35) of invoice_clean.pdf |
| invoice_consignee_alias.pdf | auto_approve | auto_approve | yes | no | 1.0 | approved alias written differently - must NOT be flagged |
| invoice_consignee_typo.pdf | human_review | human_review, amendment | yes | no | 1.0 | near-match: typo or different entity? |
| invoice_errors.pdf | amendment | amendment | yes | no | 1.0 | wrong Incoterm + HS code outside allowed headings |
| invoice_errors_scan.jpg | human_review | amendment, human_review | yes | no | 1.0 | messy scan (skew, blur, noise, stamp, JPEG q35) of invoice_errors.pdf |
| invoice_missing_incoterm.pdf | amendment | amendment, human_review | yes | no | 1.0 | required field absent - must not be invented |
| invoice_weight_lbs.pdf | amendment | amendment | yes | no | 1.0 | weight in LBS; customer requires KG |