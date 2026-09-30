# Sample queries against stored output
Run 2026-09-30 20:17, mode `llm`. Each answer is computed from the rows shown.

## How many shipments were flagged this week?
**Answer:** There were 11 shipments flagged this week.
*Method: llm*

```sql
SELECT COUNT(*) FROM documents WHERE decision IN ('human_review', 'amendment') AND created_at >= date('now', '-6 days', 'weekday 1')
```
| COUNT(*) |
|---|
| 11 |

## Show me everything pending review for Acme
**Answer:** No matching records were found.
*Method: llm*

```sql
SELECT run_id, doc_id, filename, customer_id, doc_type, decision, status, created_at, invoice_number, consignee FROM documents WHERE customer_id = 'Acme' AND status IN ('pending_review', 'amendment_drafted')
```

## What are the most common mismatches this month?
**Answer:** The most common mismatches this month are 'incoterms' and 'hs_code' with 7 mismatches each, followed by 'port_of_discharge' and 'gross_weight' with 2 mismatches each, and 'consignee_name' with 1 mismatch.
*Method: llm*

```sql
SELECT name, COUNT(*) as mismatch_count FROM fields WHERE status = 'mismatch' AND run_id IN (SELECT run_id FROM documents WHERE created_at >= strftime('%Y-%m-01 00:00:00', '2026-09-30 14:47')) GROUP BY name ORDER BY mismatch_count DESC
```
| name | mismatch_count |
|---|---|
| incoterms | 7 |
| hs_code | 7 |
| port_of_discharge | 2 |
| gross_weight | 2 |
| consignee_name | 1 |

## How many were auto-approved in the last 7 days?
**Answer:** There were 1 auto-approved documents in the last 7 days.
*Method: llm*

```sql
SELECT COUNT(*) FROM documents WHERE decision = 'auto_approve' AND created_at >= datetime('now', '-7 days');
```
| COUNT(*) |
|---|
| 1 |

## What is the average cost per document?
**Answer:** The average cost per document is 0.000631875.
*Method: llm*

```sql
SELECT AVG(cost_usd) AS average_cost_per_document FROM documents
```
| average_cost_per_document |
|---|
| 0.000631875 |

## Which documents had an HS code problem?
**Answer:** The documents with an HS code problem include invoice_errors.pdf (doc_id 619367e9c030d557), invoice_errors_scan.jpg (doc_id eea0444812250c2a), seed_000.pdf (doc_id d95513c401231118), seed_016.pdf (doc_id 3c8ce02ff1c29e65), and seed_020.pdf (doc_id 7283ffb45f6ce0f3).
*Method: llm*

```sql
SELECT DISTINCT d.doc_id, d.filename, d.customer_id, d.invoice_number, d.created_at FROM documents d JOIN fields f ON d.run_id = f.run_id WHERE f.name = 'hs_code' AND f.status IN ('mismatch', 'uncertain')
```
| doc_id | filename | customer_id | invoice_number | created_at |
|---|---|---|---|---|
| 619367e9c030d557 | invoice_errors.pdf | acme_electronics | INV-2026-0142 | 2026-09-30 10:43:46 |
| 619367e9c030d557 | invoice_errors.pdf | acme_electronics | INV-2026-0142 | 2026-09-30 11:02:54 |
| 619367e9c030d557 | invoice_errors.pdf | acme_electronics | INV-2026-0142 | 2026-09-30 11:17:00 |
| eea0444812250c2a | invoice_errors_scan.jpg | acme_electronics | INV-2026-0142 | 2026-09-30 11:21:35 |
| eea0444812250c2a | invoice_errors_scan.jpg | acme_electronics | INV-2026-0142 | 2026-09-30 11:31:04 |
| eea0444812250c2a | invoice_errors_scan.jpg | acme_electronics | INV-2026-0142 | 2026-09-30 11:44:03 |
| eea0444812250c2a | invoice_errors_scan.jpg | acme_electronics | INV-2026-0142 | 2026-09-30 11:57:13 |
| eea0444812250c2a | invoice_errors_scan.jpg | acme_electronics | INV-2026-0142 | 2026-09-30 12:11:51 |
| eea0444812250c2a | invoice_errors_scan.jpg | acme_electronics | INV-2026-0142 | 2026-09-30 12:19:45 |
| d95513c401231118 | seed_000.pdf | acme_electronics | INV-2026-0300 | 2026-09-26 11:19:12 |
*... 3 more rows*

## How many documents needed an amendment last week?
**Answer:** Last week, 1 document needed an amendment.
*Method: llm*

```sql
SELECT COUNT(*) FROM documents WHERE decision = 'amendment' AND created_at >= datetime('now', '-14 days') AND created_at < datetime('now', '-7 days')
```
| COUNT(*) |
|---|
| 1 |
