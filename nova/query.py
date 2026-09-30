"""Natural-language questions over stored results.

LLM mode : question -> SQL (LLM, JSON output) -> guarded execution -> answer written
           ONLY from the returned rows. SQL + rows are always shown, so every answer
           is checkable (grounded), not a model's opinion.
Offline  : a small set of template questions (so the chain works without a key).

SQL safety (defence in depth):
  1. single statement, must start with SELECT/WITH, blocklist of write/admin keywords
  2. read-only SQLite connection (mode=ro)
  3. sqlite authorizer that denies anything except reads
  4. LIMIT wrapper + one bounded retry on SQL error
"""
from __future__ import annotations

import re
import sqlite3
from datetime import datetime, timezone
from typing import Optional

from .config import SETTINGS
from .llm import BudgetExceeded, LLMClient, LLMError
from .rules import list_customers, load_rules

SCHEMA_DOC = """SQLite database. Tables:
documents(run_id TEXT PK, doc_id TEXT, filename TEXT, customer_id TEXT, doc_type TEXT,
  decision TEXT  -- 'auto_approve' | 'human_review' | 'amendment'
  status TEXT    -- 'approved' | 'pending_review' | 'amendment_drafted' | 'amendment_sent' | 'error'
  reasoning TEXT, amendment_subject TEXT, amendment_draft TEXT, invoice_number TEXT, consignee TEXT,
  n_mismatch INT, n_uncertain INT, extractor TEXT, text_source TEXT, cost_usd REAL, latency_ms REAL,
  rule_version TEXT, created_at TEXT -- UTC 'YYYY-MM-DD HH:MM:SS', reviewed_at TEXT,
  reviewer_action TEXT -- 'accepted' | 'overridden' | NULL)
fields(run_id TEXT, name TEXT -- consignee_name|hs_code|port_of_loading|port_of_discharge|incoterms|
  description_of_goods|gross_weight|invoice_number, value TEXT, confidence REAL,
  status TEXT -- 'match'|'mismatch'|'uncertain', expected TEXT, rule TEXT, reason TEXT, evidence TEXT, page INT)
llm_calls(run_id, node, model, latency_ms, input_tokens, output_tokens, cost_usd, ok, error, created_at)
feedback(run_id, field, action, detail, created_at)

Business definitions:
- "flagged" = decision IN ('human_review','amendment')
- "pending" / "pending review" = status IN ('pending_review','amendment_drafted')
- "this week" = created_at >= date('now','-6 days','weekday 1')   (week starts Monday)
- "today" = date(created_at) = date('now'); "last N days" = created_at >= datetime('now','-N days')
- one row in documents = one processed document ("shipment" in user questions)
"""

SQL_PROMPT = f"""You translate a CG operator's question into ONE SQLite SELECT query.
{SCHEMA_DOC}
Rules: read-only SELECT (or WITH ... SELECT) only. Use only the tables/columns above.
Prefer COUNT/GROUP BY for "how many". Include identifying columns (filename, invoice_number,
customer_id, created_at) when listing documents.
Return ONLY JSON: {{"sql": "..."}}"""

ANSWER_PROMPT = """Answer the question in 1-3 plain sentences using ONLY the SQL result rows given.
Quote the numbers exactly. If rows are empty, say no matching records were found.
Do not speculate beyond the rows. Return ONLY JSON: {"answer": "..."}"""

_BLOCK = re.compile(r"\b(insert|update|delete|drop|alter|create|replace|attach|detach|pragma|vacuum|reindex|trigger)\b", re.I)
_ALLOWED_ACTIONS = {sqlite3.SQLITE_SELECT, sqlite3.SQLITE_READ, sqlite3.SQLITE_FUNCTION}
if hasattr(sqlite3, "SQLITE_RECURSIVE"):
    _ALLOWED_ACTIONS.add(sqlite3.SQLITE_RECURSIVE)


class UnsafeSQL(ValueError):
    pass


def guard_sql(sql: str) -> str:
    s = sql.strip().rstrip(";").strip()
    if ";" in s:
        raise UnsafeSQL("multiple statements are not allowed")
    if not re.match(r"^(select|with)\b", s, re.I):
        raise UnsafeSQL("only SELECT queries are allowed")
    if _BLOCK.search(s):
        raise UnsafeSQL("write/admin keyword detected")
    return f"SELECT * FROM ({s}) LIMIT 200"


def _authorizer(action, *_):
    return sqlite3.SQLITE_OK if action in _ALLOWED_ACTIONS else sqlite3.SQLITE_DENY


def run_sql(sql: str) -> tuple[list[str], list[tuple]]:
    safe = guard_sql(sql)
    conn = sqlite3.connect(f"file:{SETTINGS.db_path}?mode=ro", uri=True)
    try:
        conn.set_authorizer(_authorizer)
        cur = conn.execute(safe)
        cols = [d[0] for d in cur.description]
        return cols, cur.fetchall()
    finally:
        conn.close()


# ---------------- offline templates -----------------------------------------
def _time_filter(q: str) -> tuple[str, str]:
    q = q.lower()
    if "today" in q:
        return "date(created_at) = date('now')", "today"
    if "this week" in q:
        return "created_at >= date('now','-6 days','weekday 1')", "this week"
    if "last week" in q:
        return ("created_at >= date('now','-6 days','weekday 1','-7 days') AND "
                "created_at < date('now','-6 days','weekday 1')"), "last week"
    m = re.search(r"last (\d+) days", q)
    if m:
        return f"created_at >= datetime('now','-{int(m.group(1))} days')", f"in the last {m.group(1)} days"
    if "this month" in q:
        return "created_at >= date('now','start of month')", "this month"
    return "1=1", "in total"


def _customer_filter(q: str) -> tuple[str, str, tuple]:
    ql = q.lower()
    for cid in list_customers():
        name = str(load_rules(cid).get("customer_name", cid)).lower()
        if cid.replace("_", " ") in ql or name in ql or name.split()[0] in ql:
            return "customer_id = ?", f" for {load_rules(cid).get('customer_name', cid)}", (cid,)
    return "1=1", "", ()


def _offline(q: str) -> Optional[dict]:
    tf, tlabel = _time_filter(q)
    cf, clabel, cparams = _customer_filter(q)
    ql = q.lower()
    where = f"WHERE {tf} AND {cf}"
    if re.search(r"(common|top|most|frequent).*(mismatch|error|issue|discrepanc|problem)", ql):
        sql = (f"SELECT f.name AS field, COUNT(*) AS mismatches FROM fields f JOIN documents d USING(run_id) "
               f"{where.replace('created_at', 'd.created_at').replace('customer_id', 'd.customer_id')} "
               f"AND f.status='mismatch' GROUP BY f.name ORDER BY mismatches DESC")
        fmt = lambda c, r: ("Most common mismatches " + tlabel + clabel + ": " +
                            ", ".join(f"{a} ({b})" for a, b in r[:5])) if r else "No mismatches found."
    elif "pending" in ql:
        sql = (f"SELECT filename, invoice_number, customer_id, decision, status, created_at FROM documents "
               f"{where} AND status IN ('pending_review','amendment_drafted') ORDER BY created_at DESC")
        fmt = lambda c, r: f"{len(r)} document(s) pending CG action{clabel} ({tlabel})."
    elif "flag" in ql:
        sql = (f"SELECT COUNT(*) AS flagged FROM documents {where} "
               f"AND decision IN ('human_review','amendment')")
        fmt = lambda c, r: f"{r[0][0]} document(s) were flagged (human review or amendment) {tlabel}{clabel}."
    elif "amend" in ql:
        sql = f"SELECT COUNT(*) AS amendments FROM documents {where} AND decision='amendment'"
        fmt = lambda c, r: f"{r[0][0]} amendment request(s) were drafted {tlabel}{clabel}."
    elif re.search(r"auto.?approv|approved", ql):
        sql = (f"SELECT SUM(decision='auto_approve') AS auto_approved, COUNT(*) AS total, "
               f"ROUND(100.0*SUM(decision='auto_approve')/MAX(COUNT(*),1),1) AS pct FROM documents {where}")
        fmt = lambda c, r: f"{r[0][0] or 0} of {r[0][1]} document(s) were auto-approved ({r[0][2] or 0}%) {tlabel}{clabel}."
    elif "cost" in ql:
        sql = (f"SELECT ROUND(AVG(cost_usd),5) AS avg_cost_usd, ROUND(SUM(cost_usd),4) AS total_cost_usd, "
               f"COUNT(*) AS docs FROM documents {where}")
        fmt = lambda c, r: f"Average cost ${r[0][0] or 0} per document, ${r[0][1] or 0} total over {r[0][2]} documents {tlabel}."
    elif re.search(r"how many|count|number of", ql):
        sql = f"SELECT COUNT(*) AS documents FROM documents {where}"
        fmt = lambda c, r: f"{r[0][0]} document(s) processed {tlabel}{clabel}."
    else:
        return None
    return {"sql": sql, "params": cparams, "fmt": fmt}


def ask(question: str, mode: Optional[str] = None) -> dict:
    mode = (mode or SETTINGS.mode).lower()
    if mode == "llm":
        try:
            return _ask_llm(question)
        except (LLMError, BudgetExceeded) as e:
            fallback = _ask_offline(question)
            fallback["note"] = f"LLM unavailable ({type(e).__name__}); answered with offline template."
            return fallback
    return _ask_offline(question)


def _ask_offline(question: str) -> dict:
    t = _offline(question)
    if not t:
        return {"question": question, "method": "template", "sql": None, "columns": [], "rows": [],
                "answer": "Offline mode understands questions about: flagged, pending, amendments, "
                          "auto-approved, cost, most common mismatches, and counts - optionally with "
                          "'today', 'this week', 'last week', 'last N days', 'this month' and a customer name. "
                          "Set NOVA_MODE=llm for free-form questions."}
    conn = sqlite3.connect(f"file:{SETTINGS.db_path}?mode=ro", uri=True)
    try:
        conn.set_authorizer(_authorizer)
        cur = conn.execute(t["sql"], t["params"])
        cols, rows = [d[0] for d in cur.description], cur.fetchall()
    finally:
        conn.close()
    sql_shown = t["sql"]
    for p in t["params"]:
        sql_shown = sql_shown.replace("?", f"'{p}'", 1)
    return {"question": question, "method": "template", "sql": sql_shown, "columns": cols,
            "rows": [list(r) for r in rows], "answer": t["fmt"](cols, rows)}


def _ask_llm(question: str) -> dict:
    client = LLMClient(run_id=f"query-{datetime.now(timezone.utc):%Y%m%d%H%M%S}", budget_usd=0.02)
    msgs = [{"role": "system", "content": SQL_PROMPT},
            {"role": "user", "content": f"Current UTC time: {datetime.now(timezone.utc):%Y-%m-%d %H:%M}. Question: {question}"}]
    sql, cols, rows, err = None, [], [], None
    for _ in range(2):                                  # bounded: one retry with the error
        sql = str(client.complete_json(model=SETTINGS.text_model, messages=msgs, node="nl2sql",
                                       max_tokens=400).get("sql", ""))
        try:
            cols, rows = run_sql(sql)
            err = None
            break
        except (sqlite3.Error, UnsafeSQL) as e:
            err = str(e)
            msgs += [{"role": "assistant", "content": f'{{"sql": "{sql}"}}'},
                     {"role": "user", "content": f"That query failed: {err}. Fix it. Return only JSON."}]
    if err:
        return {"question": question, "method": "llm", "sql": sql, "columns": [], "rows": [],
                "answer": f"Could not produce a valid query ({err}). Try rephrasing."}
    preview = [dict(zip(cols, r)) for r in rows[:50]]
    ans = client.complete_json(model=SETTINGS.text_model, node="nl_answer", max_tokens=300,
                               messages=[{"role": "system", "content": ANSWER_PROMPT},
                                         {"role": "user", "content": f"Question: {question}\nSQL: {sql}\n"
                                                                     f"Rows ({len(rows)} total): {preview}"}])
    return {"question": question, "method": "llm", "sql": sql, "columns": cols,
            "rows": [list(r) for r in rows], "answer": str(ans.get("answer", ""))}
