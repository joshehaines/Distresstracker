"""SQLite storage: users, invites, runs, filing history."""
from __future__ import annotations

import hashlib
import hmac
import json
import os
import secrets
import sqlite3
import threading
from contextlib import contextmanager
from datetime import datetime, timedelta, timezone
from pathlib import Path

DATA = Path(os.environ.get("DATA_DIR", "/data"))
DB_PATH = DATA / "casealerts.db"
_lock = threading.Lock()

SCHEMA = """
CREATE TABLE IF NOT EXISTS users (
  id INTEGER PRIMARY KEY, email TEXT UNIQUE NOT NULL, name TEXT NOT NULL,
  pw_hash TEXT, is_admin INTEGER DEFAULT 0, active INTEGER DEFAULT 1,
  created TEXT NOT NULL, last_login TEXT
);
CREATE TABLE IF NOT EXISTS invites (
  token_hash TEXT PRIMARY KEY, email TEXT NOT NULL, kind TEXT NOT NULL,
  created_by INTEGER, created TEXT NOT NULL, expires TEXT NOT NULL, used TEXT
);
CREATE TABLE IF NOT EXISTS runs (
  id TEXT PRIMARY KEY, user_id INTEGER NOT NULL, created TEXT NOT NULL,
  pdf_name TEXT, alert_date TEXT, status TEXT NOT NULL, error TEXT, summary TEXT
);
CREATE TABLE IF NOT EXISTS filings (
  company_number TEXT NOT NULL, case_ref TEXT NOT NULL, alert_date TEXT, run_id TEXT,
  PRIMARY KEY (company_number, case_ref)
);
"""


def now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


@contextmanager
def conn():
    with _lock:
        c = sqlite3.connect(DB_PATH)
        c.row_factory = sqlite3.Row
        try:
            yield c
            c.commit()
        finally:
            c.close()


def init():
    DATA.mkdir(parents=True, exist_ok=True)
    (DATA / "runs").mkdir(exist_ok=True)
    with conn() as c:
        c.executescript(SCHEMA)


# ---- passwords (stdlib scrypt) ------------------------------------------------
def hash_pw(pw: str) -> str:
    salt = secrets.token_bytes(16)
    h = hashlib.scrypt(pw.encode(), salt=salt, n=2**14, r=8, p=1)
    return f"scrypt${salt.hex()}${h.hex()}"


def check_pw(pw: str, stored: str | None) -> bool:
    if not stored:
        return False
    try:
        _, salt, h = stored.split("$")
        got = hashlib.scrypt(pw.encode(), salt=bytes.fromhex(salt), n=2**14, r=8, p=1)
        return hmac.compare_digest(got.hex(), h)
    except Exception:
        return False


# ---- users ----------------------------------------------------------------------
def user_by_email(email: str):
    with conn() as c:
        return c.execute("SELECT * FROM users WHERE lower(email)=lower(?)", (email.strip(),)).fetchone()


def user_by_id(uid: int):
    with conn() as c:
        return c.execute("SELECT * FROM users WHERE id=?", (uid,)).fetchone()


def all_users():
    with conn() as c:
        return c.execute("SELECT * FROM users ORDER BY created").fetchall()


def create_user(email, name, pw, is_admin=False):
    with conn() as c:
        c.execute("INSERT INTO users(email,name,pw_hash,is_admin,created) VALUES(?,?,?,?,?)",
                  (email.strip().lower(), name.strip(), hash_pw(pw), int(is_admin), now()))


def set_password(uid, pw):
    with conn() as c:
        c.execute("UPDATE users SET pw_hash=? WHERE id=?", (hash_pw(pw), uid))


def set_flag(uid, field, value):
    assert field in ("active", "is_admin")
    with conn() as c:
        c.execute(f"UPDATE users SET {field}=? WHERE id=?", (int(value), uid))


def touch_login(uid):
    with conn() as c:
        c.execute("UPDATE users SET last_login=? WHERE id=?", (now(), uid))


def ensure_admin(email, password, name="Administrator"):
    if not email or not password:
        return
    if not user_by_email(email):
        create_user(email, name, password, is_admin=True)


# ---- invites / reset links -------------------------------------------------------
def _th(token: str) -> str:
    return hashlib.sha256(token.encode()).hexdigest()


def make_invite(email: str, created_by: int, kind: str = "invite", days: int = 7) -> str:
    token = secrets.token_urlsafe(32)
    exp = (datetime.now(timezone.utc) + timedelta(days=days)).isoformat(timespec="seconds")
    with conn() as c:
        c.execute("INSERT INTO invites VALUES(?,?,?,?,?,?,NULL)",
                  (_th(token), email.strip().lower(), kind, created_by, now(), exp))
    return token


def get_invite(token: str):
    with conn() as c:
        row = c.execute("SELECT * FROM invites WHERE token_hash=?", (_th(token),)).fetchone()
    if not row or row["used"] or row["expires"] < now():
        return None
    return row


def use_invite(token: str):
    with conn() as c:
        c.execute("UPDATE invites SET used=? WHERE token_hash=?", (now(), _th(token)))


def open_invites():
    with conn() as c:
        return c.execute("SELECT rowid, * FROM invites WHERE used IS NULL AND expires>? ORDER BY created DESC",
                         (now(),)).fetchall()


def revoke_invite(rowid: int):
    with conn() as c:
        c.execute("UPDATE invites SET used='revoked' WHERE rowid=?", (rowid,))


# ---- runs ---------------------------------------------------------------------------
def new_run(uid: int, pdf_name: str) -> str:
    rid = secrets.token_hex(8)
    with conn() as c:
        c.execute("INSERT INTO runs(id,user_id,created,pdf_name,status) VALUES(?,?,?,?,?)",
                  (rid, uid, now(), pdf_name, "queued"))
    return rid


def update_run(rid, **kw):
    if "summary" in kw and not isinstance(kw["summary"], str):
        kw["summary"] = json.dumps(kw["summary"])
    sets = ",".join(f"{k}=?" for k in kw)
    with conn() as c:
        c.execute(f"UPDATE runs SET {sets} WHERE id=?", (*kw.values(), rid))


def get_run(rid):
    with conn() as c:
        return c.execute("SELECT runs.*, users.name AS user_name FROM runs JOIN users ON users.id=runs.user_id "
                         "WHERE runs.id=?", (rid,)).fetchone()


def list_runs(uid=None, limit=50):
    q = "SELECT runs.*, users.name AS user_name FROM runs JOIN users ON users.id=runs.user_id"
    args = ()
    if uid is not None:
        q += " WHERE user_id=?"
        args = (uid,)
    with conn() as c:
        return c.execute(q + " ORDER BY created DESC LIMIT ?", (*args, limit)).fetchall()


# ---- filing history (feeds 'Multiple Caseboard Filings') ----------------------------
def history_for(numbers) -> dict[str, list[str]]:
    numbers = list({n for n in numbers if n})
    if not numbers:
        return {}
    out: dict[str, list[str]] = {}
    with conn() as c:
        q = "SELECT company_number, case_ref FROM filings WHERE company_number IN (%s) ORDER BY alert_date" % \
            ",".join("?" * len(numbers))
        for r in c.execute(q, numbers):
            out.setdefault(r["company_number"], []).append(r["case_ref"])
    return out


def record_filings(rid, alert_date, mapping: dict[str, str]):
    with conn() as c:
        c.executemany("INSERT OR IGNORE INTO filings VALUES(?,?,?,?)",
                      [(num, ref, alert_date, rid) for ref, num in mapping.items()])
