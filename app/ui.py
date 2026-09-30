import json
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

import pandas as pd  # noqa: E402
import streamlit as st  # noqa: E402

from nova import metrics, storage  # noqa: E402
from nova.config import SETTINGS  # noqa: E402
from nova.graph import run_document  # noqa: E402
from nova.query import ask  # noqa: E402
from nova.rules import list_customers, load_rules  # noqa: E402
from nova.schemas import DOC_TYPE_LABELS, FIELD_LABELS  # noqa: E402

st.set_page_config(page_title="Nova document check", layout="wide")
st.markdown("""
<style>
.decision {padding: 18px 22px; border-radius: 6px; color: #fff; margin: 6px 0 14px 0;}
.decision h2 {margin: 0 0 4px 0; font-size: 1.55rem; color: #fff;}
.decision p {margin: 0; font-size: 0.98rem; opacity: 0.95;}
.d-auto_approve {background: #2f6f4e;} .d-human_review {background: #9a6414;} .d-amendment {background: #a23b37;}
.snippet {background: #f6f3e8; border-left: 3px solid #9a6414; padding: 8px 10px; font-family: Georgia, serif;
          color: #222;}
</style>""", unsafe_allow_html=True)

STAGE = {"ingest": "Read the document", "extract": "Extracted fields", "refine": "Re-read unclear fields",
         "validate": "Checked against customer rules", "route": "Decided next step", "persist": "Saved result"}
TITLE = {"auto_approve": "Approved", "human_review": "Needs your review", "amendment": "Amendment needed"}
STATUS_ORDER = {"mismatch": 0, "uncertain": 1, "match": 2}

# ---------------- sidebar ------------------------------------------------------
with st.sidebar:
    st.subheader("Settings")
    customer = st.selectbox("Customer", list_customers(),
                            format_func=lambda c: load_rules(c).get("customer_name", c))
    mode = st.radio("Mode", ["offline", "llm"], index=0 if SETTINGS.mode != "llm" else 1,
                    help="offline: no API key, text-layer parser. llm: vision model extraction.")
    if mode == "llm":
        st.caption(f"Extractor: `{SETTINGS.extractor_model}`  \nFallback: `{SETTINGS.fallback_model or 'off'}`  \n"
                   f"Drafting/query: `{SETTINGS.text_model}`  \nBudget: ${SETTINGS.budget_usd_per_doc}/document")
    rules = load_rules(customer)
    with st.expander("Customer rules in force"):
        st.caption(f"Version {rules.get('version')}; confidence threshold {rules['confidence_threshold']}")
        st.json({k: v for k, v in rules.items() if k not in ("customer_id", "customer_name", "version")}, expanded=False)

tab_check, tab_hist, tab_ask, tab_metrics = st.tabs(["Check a document", "History", "Ask the data", "Metrics"])

# ---------------- check a document --------------------------------------------
with tab_check:
    samples = sorted(p for p in SETTINGS.samples_dir.glob("*") if p.suffix.lower() in (".pdf", ".jpg", ".jpeg", ".png"))
    c1, c2, c3 = st.columns([3, 3, 1])
    choice = c1.selectbox("Sample document", [p.name for p in samples]) if samples else None
    upload = c2.file_uploader("…or upload a PDF / image", type=["pdf", "png", "jpg", "jpeg"])
    force = c3.checkbox("Re-run", help="Ignore the stored result and run the pipeline again")
    if st.button("Run check", type="primary"):
        if upload is not None:
            up_dir = ROOT / "data" / "uploads"
            up_dir.mkdir(parents=True, exist_ok=True)
            path = up_dir / upload.name
            path.write_bytes(upload.getbuffer())
        else:
            path = SETTINGS.samples_dir / choice
        with st.status("Running the pipeline…", expanded=True) as status:
            def show(node, update):
                extra = ""
                if node == "extract" and "extraction" in update:
                    extra = f" with `{update['extraction']['extractor']}`"
                st.write(f"{STAGE.get(node, node)}{extra}")
            try:
                st.session_state["result"] = run_document(str(path), customer, mode=mode, force=force, on_update=show)
                r = st.session_state["result"]
                status.update(label=("Loaded stored result (tick Re-run to run again)" if r.get("cached")
                                     else "Resumed after a crash and finished" if r.get("resumed") else "Done"),
                              state="complete", expanded=False)
            except Exception as e:  # fail loud
                status.update(label="Pipeline failed", state="error")
                st.error(f"{type(e).__name__}: {e}. Nothing was approved. Re-run to resume from the last completed step.")

    r = st.session_state.get("result")
    if r and "decision" in r:
        d, v, ex, doc = r["decision"], r["validation"], r["extraction"], r["doc"]
        mism, unc = [c for c in v["checks"] if c["status"] == "mismatch"], [c for c in v["checks"] if c["status"] == "uncertain"]
        summary = {"auto_approve": f"All {len(v['checks'])} fields match the rules for {rules['customer_name']}.",
                   "human_review": f"{len(unc)} field(s) need a human look before this can be approved.",
                   "amendment": f"{len(mism)} field(s) break the rules for {rules['customer_name']}. A draft request to the shipper is ready below."}[d["action"]]
        likely = [c for c in unc if c.get("rule_verdict") is False]
        if d["action"] == "human_review" and likely:
            names = " and ".join(FIELD_LABELS[c["name"]] for c in likely)
            n = "this field" if len(likely) == 1 else f"these {len(likely)} fields"
            summary = (f"Likely amendment: {names} probably break the rules for {rules['customer_name']}, "
                       f"but the values couldn't be fully verified. Check {n} on the document, "
                       f"then send the prepared draft below.")
        st.markdown(f"<div class='decision d-{d['action']}'><h2>{TITLE[d['action']]}</h2><p>{summary}</p></div>",
                    unsafe_allow_html=True)
        notes = ex.get("warnings", []) + r.get("errors", [])
        # Real failures stay loud. Routine explanations (fallback model used, confidence caps)
        # go into a collapsed section so the operator sees the decision first.
        loud = [w for w in notes if any(k in w for k in ("fell back to text-layer", "failed on all", "Budget"))]
        for w in loud:
            st.warning(w)
        if ex.get("text_source") != "pdf_text_layer" and not loud:
            st.info("This is a scanned image, so the values couldn't be cross-checked against the "
                    "document's text. Please confirm the flagged fields on the page image.")
        if notes:
            with st.expander(f"System notes ({len(notes)})"):
                for w in notes:
                    st.caption(w)

        left, right = st.columns([3, 2])
        with left:
            st.markdown(f"**{DOC_TYPE_LABELS.get(ex['doc_type'], 'Document')}** · `{doc['filename']}`")
            def rank(c):
                if c["status"] == "mismatch":
                    return 0
                if c["status"] == "uncertain":
                    return {False: 1, None: 2, True: 3}[c.get("rule_verdict")]
                return 4

            def rule_check(c):
                if c["status"] == "match":
                    return "passes"
                if c["status"] == "mismatch":
                    return "FAILS"
                return {False: "likely fails", None: "can't tell", True: "likely passes"}[c.get("rule_verdict")]

            rows = sorted(v["checks"], key=lambda c: (rank(c), c["name"]))
            df = pd.DataFrame([{"Field": FIELD_LABELS[c["name"]], "Rule check": rule_check(c),
                                "Status": c["status"], "Confidence": c["confidence"],
                                "Found": c["found"] if c["found"] is not None else "(not on document)",
                                "Expected": c["expected"]} for c in rows])
            st.dataframe(df, hide_index=True, width="stretch",
                         column_config={"Confidence": st.column_config.ProgressColumn(
                             "Confidence", min_value=0.0, max_value=1.0, format="%.2f")})
            st.caption(f"Fields below {v['threshold']} confidence are always marked uncertain, even if the value looks right.")
        with right:
            if doc.get("pages"):
                st.image(doc["pages"][0], caption="Page 1 as the agent saw it", width="stretch")

        flagged = mism + unc
        if flagged:
            st.subheader("Needs your attention")
            fields_by_name = {f["name"]: f for f in ex["fields"]}
            for c in flagged:
                f = fields_by_name[c["name"]]
                with st.expander(f"{FIELD_LABELS[c['name']]}: {c['status']}", expanded=c["status"] == "mismatch"):
                    a, b = st.columns(2)
                    a.markdown(f"**Found on document**  \n{c['found'] if c['found'] is not None else '_not found_'}")
                    b.markdown(f"**Expected**  \n{c['expected']}")
                    st.markdown(f"**Why:** {c['reason']}")
                    if f.get("evidence"):
                        st.markdown(f"**Source snippet** (page {f.get('page') or '?'}):")
                        st.markdown(f"<div class='snippet'>{f['evidence']}</div>", unsafe_allow_html=True)
                    st.caption("Confidence signals: " + json.dumps(f.get("signals", {})))
                    st.caption(f"Rule: {c['rule']}")

        with st.expander("Why the agent decided this", expanded=True):
            st.write(d["reasoning"])
            for t in d["policy_trace"]:
                st.markdown(f"- {t}")
            for t in d["cg_checklist"]:
                st.markdown(f"- To check: {t}")

        if d.get("amendment_draft"):
            st.subheader("Draft request to the shipper")
            if "provisional" in (d.get("drafted_by") or ""):
                st.warning("Provisional draft: the fields in it were read with low confidence. "
                           "Check them on the document first, then edit or send.")
            st.caption(f"Written by: {d['drafted_by']}. Edit before sending. The agent never sends on its own.")
            subj = st.text_input("Subject", d["amendment_subject"], key=f"subj-{r['run_id']}")
            body = st.text_area("Message", d["amendment_draft"], height=260, key=f"body-{r['run_id']}")
            if st.button("Mark as sent"):
                edited = body.strip() != (d["amendment_draft"] or "").strip() or subj != d["amendment_subject"]
                storage.record_feedback(r["run_id"], "send_amendment", detail=f"Subject: {subj}\n\n{body}")
                # Sending the agent's draft IS accepting its decision; whether CG had to edit it
                # is the draft-quality signal (Part 2: "sendable with one edit?").
                storage.record_feedback(r["run_id"], "accept_decision",
                                        detail="draft sent edited" if edited else "draft sent unedited")
                st.success("Recorded as sent by CG. (Email plumbing is out of scope for Part 1; nothing left this machine.)")

        st.divider()
        fb1, fb2, _ = st.columns([1, 1, 3])
        if fb1.button("Agree with decision"):
            storage.record_feedback(r["run_id"], "accept_decision")
            st.toast("Saved: decision accepted")
        if fb2.button("Override decision"):
            storage.record_feedback(r["run_id"], "override_decision")
            st.toast("Saved: decision overridden")
        with st.expander("Run details"):
            st.write({"run_id": r["run_id"], "extractor": ex["extractor"], "passes": ex["passes"],
                      "text_source": ex.get("text_source"), "cost_usd": round(r.get("cost_usd", 0.0), 5),
                      "timings_ms": r.get("timings_ms"), "rule_version": r.get("rule_version"),
                      "resumed_from_checkpoint": r.get("resumed")})

# ---------------- history -------------------------------------------------------
with tab_hist:
    try:
        hist = storage.fetch_df("SELECT created_at, filename, customer_id, doc_type, decision, status, n_mismatch, "
                                "n_uncertain, invoice_number, cost_usd, latency_ms, reviewer_action "
                                "FROM documents ORDER BY created_at DESC LIMIT 500")
        st.dataframe(hist, hide_index=True, width="stretch")
    except Exception as e:
        st.info(f"No history yet. Run a document or `python scripts/seed_history.py`. ({e})")

# ---------------- ask ----------------------------------------------------------------
with tab_ask:
    st.caption("Answers are computed from the stored results. The query and the rows it returned are shown under each answer.")
    examples = ["How many shipments were flagged this week?", "Show me everything pending review for Acme",
                "What are the most common mismatches this month?", "How many were auto-approved in the last 7 days?"]
    ex_cols = st.columns(len(examples))
    for i, q in enumerate(examples):
        if ex_cols[i].button(q, key=f"ex{i}"):
            st.session_state["q"] = q
    q = st.text_input("Your question", st.session_state.get("q", ""))
    if q:
        res = ask(q, mode=mode)
        st.markdown(f"### {res['answer']}")
        if res.get("note"):
            st.caption(res["note"])
        if res["sql"]:
            st.code(res["sql"], language="sql")
        if res["rows"]:
            st.dataframe(pd.DataFrame(res["rows"], columns=res["columns"]), hide_index=True, width="stretch")

# ---------------- metrics --------------------------------------------------------
with tab_metrics:
    m = metrics.compute()
    ns = m["north_star_first_pass_resolution_pct"]
    st.metric("First-pass resolution (north star)", f"{ns}%" if ns is not None else "no reviews yet",
              help="Share of CG-reviewed documents where CG accepted the agent's decision without overriding it.")
    st.caption(f"Based on {m['reviewed_documents']} reviewed of {m['documents']} documents. "
               f"Guardrail - auto-approved then overridden: {m['guardrail_auto_approved_then_overridden']} (target 0).")
    st.dataframe(pd.DataFrame([{"Metric": k, "Value": v} for k, v in m.items()]), hide_index=True, width="stretch")
    latest = ROOT / "evals" / "results" / f"latest_{mode}.md"
    if latest.exists():
        with st.expander("Latest offline eval"):
            st.markdown(latest.read_text())
