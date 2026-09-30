# Offline eval - mode `offline` - 2026-09-30T20:08:11

| Metric | Value |
|---|---|
| documents | 10 |
| field_accuracy | 0.8 |
| hallucination_rate | 0.0 |
| miss_rate | 0.208 |
| decision_accuracy | 1.0 |
| false_auto_approvals | 0 |
| avg_cost_usd | 0.0 |
| avg_latency_ms | 69.0 |

## Calibration (accuracy per confidence bucket)

| Confidence | n | Accuracy |
|---|---|---|
| 0.00-0.50 | 16 | 0.0 |
| 0.50-0.85 | 0 | None |
| 0.85-0.95 | 64 | 1.0 |
| 0.95-1.00 | 0 | None |

## Per field

| Field | Accuracy |
|---|---|
| consignee_name | 0.8 |
| hs_code | 0.8 |
| port_of_loading | 0.8 |
| port_of_discharge | 0.8 |
| incoterms | 0.8 |
| description_of_goods | 0.8 |
| gross_weight | 0.8 |
| invoice_number | 0.8 |

## Per document

| File | Decision | Acceptable | OK | False auto-approve | Field acc | Notes |
|---|---|---|---|---|---|---|
| bol_clean.pdf | auto_approve | auto_approve | yes | no | 1.0 | BL: invoice number legitimately absent |
| bol_wrong_port.pdf | amendment | amendment | yes | no | 1.0 | discharge port not allowed |
| invoice_clean.pdf | auto_approve | auto_approve | yes | no | 1.0 | clean baseline |
| invoice_clean_scan.jpg | human_review | auto_approve, human_review | yes | no | 0.0 | messy scan (skew, blur, noise, stamp, JPEG q35) of invoice_clean.pdf |
| invoice_consignee_alias.pdf | auto_approve | auto_approve | yes | no | 1.0 | approved alias written differently - must NOT be flagged |
| invoice_consignee_typo.pdf | human_review | human_review, amendment | yes | no | 1.0 | near-match: typo or different entity? |
| invoice_errors.pdf | amendment | amendment | yes | no | 1.0 | wrong Incoterm + HS code outside allowed headings |
| invoice_errors_scan.jpg | human_review | amendment, human_review | yes | no | 0.0 | messy scan (skew, blur, noise, stamp, JPEG q35) of invoice_errors.pdf |
| invoice_missing_incoterm.pdf | amendment | amendment, human_review | yes | no | 1.0 | required field absent - must not be invented |
| invoice_weight_lbs.pdf | amendment | amendment | yes | no | 1.0 | weight in LBS; customer requires KG |