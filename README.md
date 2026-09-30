# Nova DAW: multi-agent trade document checker (Part 1)

Checks a trade document (Bill of Lading, commercial invoice, as PDF or image) against one
customer's rules and decides what should happen next: **approve**, **send to a human for
review**, or **draft an amendment request** to the shipper. Results are stored, and you can ask
questions about them in plain English.

Three agents do the work:

1. **Extractor:** reads the document with a vision LLM and returns 8 fields, each with a
   confidence score and the text snippet it came from.
2. **Validator:** checks each field against the customer's rules (no LLM; plain code).
3. **Router:** decides approve / review / amend and explains why. It can draft the email to the
   shipper, but **it never sends anything**. A person always does.

Architecture, design decisions and failure analysis are in the technical write-up and PRD
(see [Documents](#documents)).

---

## 1. Requirements

- **Python 3.11 or newer** (check with `python --version`)
- **Git**
- **For LLM mode only:** a Gemini API key (free). Offline mode needs no key.

Works on Windows, macOS and Linux. Commands below show both where they differ.

## 2. Install

```bash
git clone https://github.com/Daksh-Jain-27/GoComet-Assignment.git
cd gocomet-nova
```

Create and activate a virtual environment:

```bash
# macOS / Linux
python3 -m venv .venv
source .venv/bin/activate

# Windows (PowerShell)
python -m venv .venv
.venv\Scripts\Activate.ps1
```

Install dependencies:

```bash
pip install -r requirements.txt
```

Create your settings file from the template:

```bash
# macOS / Linux
cp .env.example .env

# Windows (PowerShell)
Copy-Item .env.example .env
```


## 3. Choose a mode

| Mode | Needs a key? | What reads the document | Good for |
|---|---|---|---|
| `offline` (default) | No | A simple parser that reads the text built into digital PDFs | Trying the app, running tests |
| `llm` | Yes | A vision LLM that reads the page image | The real system, including scanned documents |

Offline mode cannot read scans or photos, so on those every field comes back "uncertain" and the
document goes to human review. That is deliberate: it never guesses.

### Setting up LLM mode

1. Get a free API key at [Google AI Studio](https://aistudio.google.com): **Get API key → Create API key**.
2. Edit `.env`:

```
NOVA_MODE=llm
EXTRACTOR_MODEL=gemini/gemini-3.5-flash-lite
FALLBACK_MODEL=gemini/gemini-3.8-flash
TEXT_MODEL=gemini/gemini-3.5-flash-lite
GEMINI_API_KEY=your-key-here
LLM_MAX_RETRIES=1
```

| Setting | What it does |
|---|---|
| `EXTRACTOR_MODEL` | Reads every document. A fast, cheap vision model. |
| `FALLBACK_MODEL` | A **different** model, used only to re-read unclear fields on hard documents, and as a backup if the first model is down. |
| `TEXT_MODEL` | Writes amendment drafts and turns your questions into database queries. |
| `BUDGET_USD_PER_DOC` | Spending cap per document (default $0.05). |

3. Check that your key and models work (reads the HS code off a sample page):

```bash
python scripts/check_model.py gemini/gemini-3.5-flash-lite
```

You should see `OK` and `8528.72.00`. Model names change over time. If you get "model not found",
list the models your key can use in AI Studio and update `.env`.

**Free-tier limits:** Google limits free requests per model per day (for example, some models
allow only 20 requests a day, and Pro models may have none). If you hit a limit, the app switches
to the fallback model and tells you. See [Troubleshooting](#8-troubleshooting).

## 4. Create sample data

```bash
python scripts/generate_samples.py --seed-set 30
python scripts/seed_history.py
```

- The first command creates **10 test documents** in `data/samples/` (clean invoices, invoices
  with planted errors, bills of lading, and two deliberately messy scans), each with a
  `.truth.json` file holding the correct answers. It also creates 30 extra invoices for history.
- The second runs the pipeline on those 30 and backdates them over 4 weeks, with **simulated**
  reviewer actions, so questions like "how many were flagged this week?" have data to answer.
  It uses offline mode, so it costs nothing.

## 5. Use it

### The operator screen

```bash
streamlit run app/ui.py
```

Open http://localhost:8501, then:

1. In the sidebar, pick the customer and the mode (`offline` or `llm`).
2. Pick a sample document (or upload your own PDF/image) and click **Run check**.
   Tick **Re-run** to process a document again instead of loading the stored result.
3. Read the result from the top:
   - **Banner:** the decision, and what to do next.
   - **Table:** every field with what was found, whether it passes the rule, what was expected,
     and the confidence. Problems are listed first.
   - **Needs your attention:** found vs expected for each flagged field, with the source snippet.
   - **Draft request to the shipper:** editable. Click **Mark as sent** when done (nothing is
     actually emailed in this version).
   - **Agree / Override decision:** records your review; this feeds the Metrics tab.
4. Other tabs:
   - **History:** every processed document.
   - **Ask the data:** plain-English questions; every answer shows the SQL it ran and the rows.
   - **Metrics:** the north-star metric and supporting metrics.

**Good documents to try:** `invoice_errors.pdf` (two rule violations), `invoice_consignee_typo.pdf`
(a near-match name the agent won't decide on its own), and `invoice_errors_scan.jpg` (a messy scan,
LLM mode).

### From the command line

Run one document and print the full result:

```bash
python scripts/run_pipeline.py data/samples/invoice_errors.pdf
python scripts/run_pipeline.py data/samples/invoice_errors_scan.jpg --mode llm --force
```

`--mode` overrides `.env` for one run; `--force` re-runs a document that was already processed;
`--json` prints the full internal state.

Ask a question:

```bash
python scripts/ask.py "how many shipments were flagged this week?"
```

Example questions (offline mode understands these; LLM mode accepts any phrasing):

- how many shipments were flagged this week?
- show me everything pending review for Acme
- what are the most common mismatches this month?
- how many were auto-approved in the last 7 days?
- what is the average cost per document?

## 6. Test and evaluate

**Tests** (the LLM is simulated, so they're free, fast and need no key):

```bash
python -m pytest -q tests
```

Each test checks one safety guarantee: invented values are caught, low-confidence fields are never
auto-approved, outages fall back loudly, bad drafts are rejected, the budget stops extra calls,
and database writes from questions are refused.

**Offline evaluation** against the correct answers in `data/samples`:

```bash
python evals/run_eval.py --mode offline
python evals/run_eval.py --mode llm        # uses your API key, ~10-20 requests
```

It reports field accuracy, hallucination rate, calibration, decision accuracy, cost, latency and,
most importantly, **false auto-approvals, which must be 0** (the script exits with an error
otherwise). Reports are written to `evals/results/`. The committed reports from my runs are
listed under [Documents](#documents).

Eval runs use a separate database (`data/eval.db`), so they don't appear in History or Metrics.

## 7. Crash recovery demo

The pipeline saves its state after every step, so if it dies midway, running the same document
again resumes where it stopped instead of starting over.

```bash
# macOS / Linux
NOVA_CRASH_AT=validate python scripts/run_pipeline.py data/samples/invoice_clean.pdf --force
python scripts/run_pipeline.py data/samples/invoice_clean.pdf
```

```powershell
# Windows (PowerShell)
$env:NOVA_CRASH_AT="validate"; python scripts/run_pipeline.py data/samples/invoice_clean.pdf
Remove-Item Env:NOVA_CRASH_AT
python scripts/run_pipeline.py data/samples/invoice_clean.pdf
```

The first command crashes at the validation step on purpose. The second prints
`resuming from checkpoint at ('validate',)` and finishes without repeating the extraction.
Use a document you haven't processed yet, or the second run returns the stored result.

## 8. Troubleshooting

| Symptom | Cause | Fix |
|---|---|---|
| `extractor: text-layer-parser` in LLM mode | Every LLM call failed, so the offline parser took over | Read the `ERRORS` section, or run `python scripts/show_errors.py` |
| `429` / `quota` / `limit: 0` | Free-tier limit reached, or the model isn't in the free tier | Wait for the reset, or use a different model in `.env` |
| `503` / `high demand` | The provider is overloaded (temporary) | The app falls back automatically; re-run later for the primary model |
| `model not found` / `404` | Wrong or retired model name | Check available names in AI Studio; test with `scripts/check_model.py` |
| `API key not valid` | Key missing or mistyped in `.env` | Re-copy it, with no quotes or spaces |
| A document returns "already completed" | It was processed before; results are cached | Add `--force`, or tick **Re-run** in the UI |
| Metrics show "no reviews yet" | Nobody has clicked Agree / Override / Mark as sent | Review a few documents, or run `scripts/seed_history.py` |
| Commands with quotes fail in PowerShell | PowerShell quoting differs from bash | Use the helper scripts in `scripts/` instead of `python -c "..."` |

`python scripts/show_errors.py 10` prints the last 10 failed LLM calls with the full error text.

**Start fresh** (deletes all results and history):

```bash
# macOS / Linux
rm -f data/nova.db data/checkpoints.db && rm -rf data/runs

# Windows (PowerShell)
Remove-Item data/nova.db, data/checkpoints.db -ErrorAction SilentlyContinue
Remove-Item data/runs -Recurse -ErrorAction SilentlyContinue
```

Always delete both database files together.

## Documents

| Document | What it covers |
|---|---|
| `notes.md` | Failure log from testing |
| `evals/results/latest_offline.md` | Baseline: offline parser |
| `evals/results/eval_llm_flash_lite.md` | LLM mode results |
| `evals/results/eval_llm_flash_quota_limited.md` | Flash attempt, limited by free-tier quota |

## Project layout

```
app/ui.py                 operator screen (Streamlit)
nova/
  agents/extractor.py     Extractor: vision extraction, second read, offline parser
  agents/validator.py     Validator: rule checks (no LLM)
  agents/router.py        Router: decision policy, explanation, amendment draft
  graph.py                runs the agents in order; saves state after each step
  schemas.py              the data passed between agents
  grounding.py            how confidence scores are calculated
  llm.py                  all LLM calls: budget, retries, fallback, logging
  rules.py, reference/    rule loading, Incoterms, port codes
  docio.py                PDF/image loading
  storage.py              SQLite database
  query.py                plain-English questions -> safe SQL
  metrics.py              metrics shown in the UI
rules/                    one YAML rule file per customer
scripts/                  generate_samples, seed_history, run_pipeline, ask,
                          check_model, show_errors
evals/                    evaluation script and reports
tests/                    safety tests
data/samples/             test documents with correct answers
```