"""SQLite persistence: claims, documents, extractions, findings, decisions, users and an append-only audit log."""
from __future__ import annotations
import datetime as dt
import hashlib
import json
import secrets
import sqlite3
from contextlib import contextmanager
from pathlib import Path

SCHEMA = """
CREATE TABLE IF NOT EXISTS claims(
  claim_id TEXT PRIMARY KEY, created_at TEXT, status TEXT, claimant_ref TEXT, lane TEXT, score REAL,
  vehicle_reg TEXT, incident_date TEXT, claimed_amount REAL, processing_ms REAL, summary_json TEXT);
CREATE TABLE IF NOT EXISTS documents(
  id INTEGER PRIMARY KEY AUTOINCREMENT, claim_id TEXT, doc_type TEXT, filename TEXT, path TEXT, sha256 TEXT, uploaded_at TEXT);
CREATE INDEX IF NOT EXISTS idx_doc_hash ON documents(sha256);
CREATE TABLE IF NOT EXISTS extractions(claim_id TEXT, doc_type TEXT, json TEXT, PRIMARY KEY(claim_id, doc_type));
CREATE TABLE IF NOT EXISTS findings(id INTEGER PRIMARY KEY AUTOINCREMENT, claim_id TEXT, check_id TEXT, json TEXT);
CREATE TABLE IF NOT EXISTS decisions(
  id INTEGER PRIMARY KEY AUTOINCREMENT, claim_id TEXT, decision TEXT, reviewer TEXT, reason TEXT,
  customer_message TEXT, decided_at TEXT);
CREATE TABLE IF NOT EXISTS users(username TEXT PRIMARY KEY, salt TEXT, pw_hash TEXT, role TEXT);
CREATE TABLE IF NOT EXISTS audit_log(
  id INTEGER PRIMARY KEY AUTOINCREMENT, ts TEXT, claim_id TEXT, actor TEXT, role TEXT, event TEXT, details TEXT);
CREATE TRIGGER IF NOT EXISTS audit_no_update BEFORE UPDATE ON audit_log
  BEGIN SELECT RAISE(ABORT, 'audit_log is append-only'); END;
CREATE TRIGGER IF NOT EXISTS audit_no_delete BEFORE DELETE ON audit_log
  BEGIN SELECT RAISE(ABORT, 'audit_log is append-only'); END;
"""


def _now():
    return dt.datetime.now(dt.timezone.utc).isoformat(timespec="seconds")


def sha256_bytes(b: bytes) -> str:
    return hashlib.sha256(b).hexdigest()


def _hash_pw(password: str, salt: str) -> str:
    return hashlib.pbkdf2_hmac("sha256", password.encode(), salt.encode(), 120_000).hex()


class Database:
    def __init__(self, path):
        self.path = str(path)
        Path(self.path).parent.mkdir(parents=True, exist_ok=True)
        with self.conn() as c:
            c.executescript(SCHEMA)

    @contextmanager
    def conn(self):
        c = sqlite3.connect(self.path, timeout=30)
        c.row_factory = sqlite3.Row
        try:
            yield c
            c.commit()
        except Exception:
            c.rollback()
            raise
        finally:
            c.close()

    # ---- claims
    def new_claim(self, claimant_ref="") -> str:
        today = dt.date.today().strftime("%Y%m%d")
        with self.conn() as c:
            n = c.execute("SELECT COUNT(*) FROM claims WHERE claim_id LIKE ?", (f"CW-{today}-%",)).fetchone()[0] + 1
            cid = f"CW-{today}-{n:04d}"
            c.execute("INSERT INTO claims(claim_id, created_at, status, claimant_ref) VALUES (?,?,?,?)",
                      (cid, _now(), "SUBMITTED", claimant_ref))
        return cid

    def update_claim(self, claim_id, **fields):
        if not fields:
            return
        cols = ", ".join(f"{k}=?" for k in fields)
        with self.conn() as c:
            c.execute(f"UPDATE claims SET {cols} WHERE claim_id=?", (*fields.values(), claim_id))

    def get_claim(self, claim_id):
        with self.conn() as c:
            r = c.execute("SELECT * FROM claims WHERE claim_id=?", (claim_id,)).fetchone()
        return dict(r) if r else None

    def list_claims(self):
        with self.conn() as c:
            return [dict(r) for r in c.execute("SELECT * FROM claims ORDER BY created_at DESC, claim_id DESC")]

    # ---- documents
    def add_document(self, claim_id, doc_type, filename, path, sha):
        with self.conn() as c:
            c.execute("INSERT INTO documents(claim_id, doc_type, filename, path, sha256, uploaded_at) VALUES (?,?,?,?,?,?)",
                      (claim_id, doc_type, filename, str(path), sha, _now()))

    def get_documents(self, claim_id) -> dict:
        """Latest version of each document type."""
        docs = {}
        with self.conn() as c:
            for r in c.execute("SELECT * FROM documents WHERE claim_id=? ORDER BY id", (claim_id,)):
                docs[r["doc_type"]] = {"path": r["path"], "sha256": r["sha256"], "filename": r["filename"]}
        return docs

    def find_documents_by_hash(self, sha, exclude_claim):
        with self.conn() as c:
            return [dict(r) for r in c.execute(
                "SELECT DISTINCT claim_id, doc_type FROM documents WHERE sha256=? AND claim_id<>?", (sha, exclude_claim))]

    def find_similar_claims(self, reg, window, exclude_claim):
        lo, hi = window[0].isoformat(), window[1].isoformat()
        with self.conn() as c:
            return [dict(r) for r in c.execute(
                "SELECT claim_id, incident_date FROM claims WHERE vehicle_reg=? AND claim_id<>? "
                "AND incident_date BETWEEN ? AND ?", (reg, exclude_claim, lo, hi))]

    # ---- extractions / findings
    def save_extractions(self, claim_id, extractions: dict):
        with self.conn() as c:
            c.execute("DELETE FROM extractions WHERE claim_id=?", (claim_id,))
            for dtp, ex in extractions.items():
                c.execute("INSERT INTO extractions VALUES (?,?,?)", (claim_id, dtp, json.dumps(ex.to_dict())))

    def get_extractions(self, claim_id) -> dict:
        from .schemas import Extraction
        with self.conn() as c:
            return {r["doc_type"]: Extraction.from_dict(json.loads(r["json"]))
                    for r in c.execute("SELECT doc_type, json FROM extractions WHERE claim_id=?", (claim_id,))}

    def save_findings(self, claim_id, findings):
        with self.conn() as c:
            c.execute("DELETE FROM findings WHERE claim_id=?", (claim_id,))
            for f in findings:
                c.execute("INSERT INTO findings(claim_id, check_id, json) VALUES (?,?,?)",
                          (claim_id, f.check_id, json.dumps(f.to_dict())))

    def get_findings(self, claim_id):
        from .schemas import Finding
        with self.conn() as c:
            return [Finding.from_dict(json.loads(r["json"]))
                    for r in c.execute("SELECT json FROM findings WHERE claim_id=? ORDER BY id", (claim_id,))]

    # ---- decisions
    def add_decision(self, claim_id, decision, reviewer, reason, customer_message):
        with self.conn() as c:
            c.execute("INSERT INTO decisions(claim_id, decision, reviewer, reason, customer_message, decided_at) VALUES (?,?,?,?,?,?)",
                      (claim_id, decision, reviewer, reason, customer_message, _now()))

    def get_decisions(self, claim_id):
        with self.conn() as c:
            return [dict(r) for r in c.execute("SELECT * FROM decisions WHERE claim_id=? ORDER BY id", (claim_id,))]

    # ---- audit
    def audit(self, claim_id, actor, role, event, details=None):
        with self.conn() as c:
            c.execute("INSERT INTO audit_log(ts, claim_id, actor, role, event, details) VALUES (?,?,?,?,?,?)",
                      (_now(), claim_id, actor, role, event, json.dumps(details or {})))

    def audit_rows(self, claim_id=None):
        q, args = "SELECT * FROM audit_log", ()
        if claim_id:
            q, args = q + " WHERE claim_id=?", (claim_id,)
        with self.conn() as c:
            return [dict(r) for r in c.execute(q + " ORDER BY id", args)]

    # ---- users (demo-grade role-based access)
    def add_user(self, username, password, role="employee"):
        salt = secrets.token_hex(8)
        with self.conn() as c:
            c.execute("INSERT OR REPLACE INTO users VALUES (?,?,?,?)", (username, salt, _hash_pw(password, salt), role))

    def check_login(self, username, password):
        with self.conn() as c:
            r = c.execute("SELECT * FROM users WHERE username=?", (username,)).fetchone()
        if r and secrets.compare_digest(r["pw_hash"], _hash_pw(password, r["salt"])):
            return {"username": r["username"], "role": r["role"]}
        return None
