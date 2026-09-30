"""SQLite store for verified outputs, per-field results, LLM call log and CG feedback.
SQLite = zero-setup on a laptop. In production this maps 1:1 to ClickHouse tables."""
from __future__ import annotations

import json
import sqlite3
from contextlib import contextmanager
from datetime import datetime, timezone
from typing import Optional

from .config import SETTINGS

SCHEMA = """
CREATE TABLE IF NOT EXISTS documents (
    run_id TEXT PRIMARY KEY,
    doc_id TEXT NOT NULL,
    filename TEXT,
    customer_id TEXT,
    doc_type TEXT,
    decision TEXT,            -- auto_approve | human_review | amendment
    status TEXT,              -- approved | pending_review | amendment_drafted | amendment_sent | rejected | error
    reasoning TEXT,
    amendment_subject TEXT,
    amendment_draft TEXT,
    invoice_number TEXT,
    consignee TEXT,
    n_mismatch INTEGER,
    n_uncertain INTEGER,
    extractor TEXT,
    text_source TEXT,
    cost_usd REAL,
    latency_ms REAL,
    rule_version TEXT,
    errors TEXT,
    created_at TEXT,          -- 'YYYY-MM-DD HH:MM:SS' UTC
    reviewed_at TEXT,
    reviewer_action TEXT      -- accepted | overridden
);
CREATE TABLE IF NOT EXISTS fields (
    run_id TEXT, name TEXT, value TEXT, confidence REAL, status TEXT,
    expected TEXT, rule TEXT, reason TEXT, evidence TEXT, page INTEGER, signals TEXT,
    PRIMARY KEY (run_id, name)
);
CREATE TABLE IF NOT EXISTS llm_calls (
    id INTEGER PRIMARY KEY AUTOINCREMENT, run_id TEXT, node TEXT, model TEXT,
    latency_ms REAL, input_tokens INTEGER, output_tokens INTEGER, cost_usd REAL,
    ok INTEGER, error TEXT, created_at TEXT
);
CREATE TABLE IF NOT EXISTS feedback (
    id INTEGER PRIMARY KEY AUTOINCREMENT, run_id TEXT, field TEXT,
    action TEXT,              -- accept_decision | override_decision | correct_field | send_amendment
    detail TEXT, created_at TEXT
);
CREATE INDEX IF NOT EXISTS idx_docs_created ON documents(created_at);
CREATE INDEX IF NOT EXISTS idx_docs_customer ON documents(customer_id, status);
"""


def now_utc() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M:%S")


@contextmanager
def connect(readonly: bool = False):
    if readonly:
        conn = sqlite3.connect(f"file:{SETTINGS.db_path}?mode=ro", uri=True, check_same_thread=False)
    else:
        conn = sqlite3.connect(SETTINGS.db_path, check_same_thread=False)
    conn.row_factory = sqlite3.Row
    try:
        yield conn
        if not readonly:
            conn.commit()
    finally:
        conn.close()


def init_db() -> None:
    with connect() as c:
        c.executescript(SCHEMA)


init_db()

_STATUS_FOR = {"auto_approve": "approved", "human_review": "pending_review", "amendment": "amendment_drafted"}


def save_run(state: dict) -> str:
    """Idempotent: re-running persist for the same run_id overwrites, never duplicates."""
    ex, val, dec, doc = state["extraction"], state["validation"], state["decision"], state["doc"]
    fields = {f["name"]: f for f in ex["fields"]}
    status = _STATUS_FOR[dec["action"]]
    latency = sum(state.get("timings_ms", {}).values())
    created = state.get("created_at") or now_utc()
    with connect() as c:
        c.execute("""INSERT OR REPLACE INTO documents
            (run_id, doc_id, filename, customer_id, doc_type, decision, status, reasoning,
             amendment_subject, amendment_draft, invoice_number, consignee, n_mismatch, n_uncertain,
             extractor, text_source, cost_usd, latency_ms, rule_version, errors, created_at)
            VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)""", (
            state["run_id"], state["doc_id"], doc["filename"], state["customer_id"], ex["doc_type"],
            dec["action"], status, dec["reasoning"], dec.get("amendment_subject"), dec.get("amendment_draft"),
            fields["invoice_number"]["value"], fields["consignee_name"]["value"],
            sum(ch["status"] == "mismatch" for ch in val["checks"]),
            sum(ch["status"] == "uncertain" for ch in val["checks"]),
            ex["extractor"], ex.get("text_source"), round(state.get("cost_usd", 0.0), 6), round(latency, 1),
            state.get("rule_version"), json.dumps(state.get("errors", [])), created))
        c.execute("DELETE FROM fields WHERE run_id = ?", (state["run_id"],))
        for ch in val["checks"]:
            f = fields[ch["name"]]
            c.execute("""INSERT INTO fields (run_id, name, value, confidence, status, expected, rule,
                         reason, evidence, page, signals) VALUES (?,?,?,?,?,?,?,?,?,?,?)""",
                      (state["run_id"], ch["name"], f["value"], f["confidence"], ch["status"], ch["expected"],
                       ch["rule"], ch["reason"], f.get("evidence"), f.get("page"), json.dumps(f.get("signals", {}))))
    return status


def log_llm_call(run_id: str, node: str, model: str, latency_ms: float, in_tok: Optional[int],
                 out_tok: Optional[int], cost: float, ok: bool, error: Optional[str]) -> None:
    with connect() as c:
        c.execute("""INSERT INTO llm_calls (run_id, node, model, latency_ms, input_tokens, output_tokens,
                     cost_usd, ok, error, created_at) VALUES (?,?,?,?,?,?,?,?,?,?)""",
                  (run_id, node, model, round(latency_ms, 1), in_tok, out_tok, cost, int(ok), error, now_utc()))


def record_feedback(run_id: str, action: str, field: Optional[str] = None, detail: Optional[str] = None) -> None:
    """CG actions. This is the online-eval signal (override rate, north star)."""
    with connect() as c:
        c.execute("INSERT INTO feedback (run_id, field, action, detail, created_at) VALUES (?,?,?,?,?)",
                  (run_id, field, action, detail, now_utc()))
        if action == "accept_decision":
            c.execute("UPDATE documents SET reviewed_at=?, reviewer_action='accepted' WHERE run_id=?", (now_utc(), run_id))
        elif action == "override_decision":
            c.execute("UPDATE documents SET reviewed_at=?, reviewer_action='overridden' WHERE run_id=?", (now_utc(), run_id))
        elif action == "send_amendment":
            c.execute("UPDATE documents SET status='amendment_sent', amendment_draft=? WHERE run_id=?", (detail, run_id))


def latest_completed(doc_id: str, customer_id: str) -> Optional[dict]:
    with connect() as c:
        row = c.execute("SELECT run_id FROM documents WHERE doc_id=? AND customer_id=? "
                        "ORDER BY created_at DESC LIMIT 1", (doc_id, customer_id)).fetchone()
    return dict(row) if row else None


def fetch_df(sql: str, params: tuple = ()):
    import pandas as pd
    with connect(readonly=True) as c:
        return pd.read_sql_query(sql, c, params=params)
