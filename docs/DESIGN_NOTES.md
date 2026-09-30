# Design notes: PRD sections 4-7, mapped to the code

These are working notes, not PRD prose. Write the PRD in your own words, and replace
every "(measure)" with a number from your own runs (`evals/results`, `llm_calls` table).

---

## 4. Agent architecture

### Why three agents, not one prompt, not five

The boundaries sit where the *nature of the work* changes:

| Agent | Kind of work | Determinism | Cost | Framing |
|---|---|---|---|---|
| Extractor | perception: read pixels | probabilistic | expensive (vision) | executor |
| Validator | rule evaluation | deterministic code | ~free | verifier |
| Router | policy + communication | policy in code, wording by LLM | cheap text model | planner of the next action |

- **Why not one prompt:** one prompt mixes perception, rule-checking and decision-making.
  - You couldn't evaluate extraction accuracy separately from rule logic.
  - You couldn't retry only the failed step.
  - You couldn't use a cheap model for one part and a strong one for another.
  - You couldn't audit *why* something was rejected: an LLM's "HS code wrong" is not reproducible.
  - Customer rules would live in a prompt instead of versioned config.
- **Why not five:** candidates for extra agents are a doc-type classifier, a separate confidence scorer, and a separate email writer.
  - Each would add a handoff without adding an independently testable boundary.
  - Classification is one field of the Extractor's output.
  - Confidence calibration is code (`grounding.py`), not an agent.
  - Drafting is the Router's output format.
  - Part 2's cross-document check becomes a fourth *step*, not a new agent type.
- **"Agent" is used loosely on purpose:** the Validator has no LLM. It is a verifier by responsibility, not by implementation.

### Responsibility / input / output

| Agent | Input | Output | File |
|---|---|---|---|
| Extractor | page images + text layer | `ExtractionResult`: 8 fields with value, evidence, page, calibrated confidence, signals | `agents/extractor.py` |
| Refine (optional step) | low-confidence field names + higher-DPI pages | merged `ExtractionResult` | `extractor.refine` |
| Validator | `ExtractionResult` + customer YAML | `ValidationResult`: match / mismatch / uncertain, found, expected, rule, reason | `agents/validator.py` |
| Router | `ValidationResult` (+ extraction) | `Decision`: action, reasoning, policy trace, CG checklist, amendment draft | `agents/router.py` |

### How agents talk

- **Structured handoff through one typed LangGraph state** (`graph.PipelineState`).
- Each node reads the keys it needs and writes its own key.
- Every value is validated against a Pydantic model at the boundary (`schemas.py`), so a bad handoff fails immediately.
- No free-text message passing between agents, and no shared scratchpad.

### Crash recovery

- `SqliteSaver` checkpoints state after every node, keyed by `thread_id = run_id`.
- `run_id = sha256(file)[:16] + customer + mode`, so the same file maps to the same run (idempotent).
- A crash in `validate` resumes at `validate`:
  - the expensive extraction is not repeated;
  - `cost_usd` is in state, so the budget survives the crash.
- Persistence is `INSERT OR REPLACE` keyed by `run_id`, so re-running never duplicates rows.
- Demo: `NOVA_CRASH_AT=validate python scripts/run_pipeline.py <file>`, then run again.
- Production: the same pattern with a Postgres checkpointer, or Temporal (which is in their stack).

---

## 5. LLM & tooling choices

| Where | Choice | Why | Trade-off |
|---|---|---|---|
| Extraction | cheap/fast vision model (Flash-class) | runs on every doc; clean digital docs don't need a frontier model | weaker on bad scans -> refine |
| Fallback | stronger vision model (Pro-class), **low-confidence fields only**, at 220 vs 150 DPI | spend where it matters | 2nd call latency on scans only |
| Validation | **no LLM** | must be deterministic, auditable, free | fuzzy matching via rapidfuzz thresholds, not semantics |
| Routing decision | **no LLM** | the LLM must not be able to "decide" to approve | none |
| Amendment wording | cheap text model | natural, editable draft | verified by code; template fallback |
| NL query | cheap text model -> SQL | open questions | guarded SQL; answer only from rows |
| Gateway | LiteLLM | provider-agnostic, cost tracking, Langfuse hook (all in their stack) | extra dependency |
| Orchestration | LangGraph | checkpointing, conditional edges, streaming; it's Nova's stack | heavier than a plain function chain |
| Storage | SQLite | laptop-runnable; schema maps to ClickHouse | not for concurrency at scale |

- **Fallback when the doc is bad quality:**
  1. Stronger model re-reads the low-confidence fields at higher DPI.
  2. Agreement between the two reads raises confidence (capped at 0.90); disagreement lowers it (to 0.40 or below).
  3. Anything still low goes to a human.
  4. If the LLM is down or over budget, the deterministic text-layer parser takes over, and the failure is recorded in `errors`.
- **Structured output is used for:** extraction JSON, draft JSON, SQL JSON. All are parsed and Pydantic-validated, with one repair retry.
- **Structured output is avoided for:** the decision and the rule checks. Code does those, so they can't be prompt-injected by document text (e.g. a doc saying "APPROVED").
- **Tool use:** none. Tools would let the model choose actions; here the actions are fixed and the model only reads and writes.

---

## 6. Trust, failure handling & evals

### Hallucination
- The prompt requires a verbatim evidence snippet and allows an explicit `null`.
- Code checks the value against the document's text layer (`grounding.is_grounded`).
- On a **digital** text layer, a value that isn't found is capped at 0.25 and becomes `uncertain`.
- On **noisy OCR**, a value that isn't found is only "unverifiable" (cap 0.70). See failure F2 in `notes.md`.
- The Router can't approve a field below threshold (code assertions).

### Low confidence
- Below the threshold (0.85, set per customer), a field is **always** `uncertain`, even if the rule would pass.
- The Router sends uncertain fields to `human_review`, with a CG checklist item for each.
- Customer policy knob `auto_approve_requires_grounding` (default true): scans never auto-approve in a pilot.

### Loops, cost, retries
- The graph is a DAG, `refine` runs at most once (`state.refined`), and `recursion_limit=12` is a backstop.
- Per-document USD budget is checked before every call.
- Refine only runs if at least 25% of the budget is left.
- Retries are bounded (`LLM_MAX_RETRIES`) with exponential backoff and a timeout.
- JSON repair is attempted once.
- NL->SQL gets one retry.
- Page cap: `MAX_PAGES`.

### Evals
- **Offline:** `evals/run_eval.py` on the labelled set. It reports:
  - field accuracy after canonicalisation;
  - hallucination and miss rates;
  - calibration (accuracy per confidence bucket);
  - decision accuracy;
  - **false auto-approvals (must be 0; exits non-zero otherwise, so it can gate CI)**;
  - cost and latency.
- **Behavioural tests:** `tests/`, with the LLM faked. They prove the guarantees:
  - a hallucinated value is caught;
  - low confidence never auto-approves;
  - an outage falls back loudly;
  - a draft that omits a discrepancy is rejected;
  - the budget stops refine;
  - SQL writes are refused.
- **Online:**
  - CG override rate on decisions (`feedback` table);
  - auto-approved documents later overridden (guardrail);
  - field correction rate.

---

## 7. Metrics & success criteria

**North star:** First-pass resolution rate = % of documents where CG accepted the agent's
decision without overriding it. (`metrics.compute()`, UI Metrics tab.)
- Alternative worth considering: median time from document arrival to a CG-sendable decision. It is closer to the business outcome but needs email timestamps (Part 2).
- Always pair the north star with the guardrail below. Otherwise you can "win" by sending everything to review.

**Supporting metrics:**

| Type | Metric |
|---|---|
| Agent quality | Field accuracy, by digital vs scan (offline eval) |
| Agent quality | Calibration gap: accuracy within the >= 0.85 bucket |
| Agent quality / safety | False auto-approve count. Guardrail: 0 |
| Operations | Uncertain-field rate. Too high means CG still reads everything |
| System health | p95 latency per document; LLM error rate; pipeline error count |
| Cost | Cost per document (avg, p95) |
| Business | Amendment cycles per shipment. Baseline 2-4; needs Part 2 email threading |
| Business | CG handling minutes per document vs a week-0 baseline |

**Go / No-Go for a 2-week pilot, one customer.** These are proposals; tune them.
- **Go only if all of these hold:**
  - 0 false auto-approvals. In week 1 CG checks 100% of auto-approves; in week 2, 20%.
  - Field accuracy >= 97% on digital docs and >= 90% on scans (sampled CG audit).
  - First-pass resolution >= 80%.
  - CG handling time down >= 40% vs baseline.
  - p95 latency < 60 s.
  - Cost per document within budget.
- **No-Go if any of these happen:**
  - a wrong document reaches the customer because of an auto-approve;
  - override rate > 25%;
  - CG bypasses the tool on > 30% of documents.

### Cost, back of envelope (fill with measured numbers)
- Formula: cost/doc ~ pages x (image tokens + text-layer tokens + prompt) x input price + output tokens x output price.
- Add the refine call (scans only) and one small drafting call (amendments only).
- Measure it: `SELECT node, AVG(input_tokens), AVG(output_tokens), AVG(cost_usd) FROM llm_calls GROUP BY node`.
- Where it blows up:
  - long multi-page docs (capped by MAX_PAGES);
  - bad scans triggering refine with the strong model;
  - retry storms (bounded).

### Latency
- The slowest hop is the vision extraction call, then refine on scans.
- Measure it: `timings_ms` per run, or the `llm_calls` table.
- Fixes:
  - render at lower DPI for the first pass;
  - send only pages that contain fields;
  - run per-page extraction in parallel;
  - skip the vision call entirely for digital PDFs whose text-layer parse is complete and grounded.
