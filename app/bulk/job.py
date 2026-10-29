"""Background runner for insolvency-sheet fills. One job at a time; resumes after restarts."""
from __future__ import annotations

import json
import logging
import secrets
import threading
import time
import traceback
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

from .. import db
from . import fetch
from .writer import company_numbers, fill

log = logging.getLogger("bulk")
ROOT = db.DATA / "bulk"
SCHEMA = """
CREATE TABLE IF NOT EXISTS bulk_jobs (
  id TEXT PRIMARY KEY, user_id INTEGER NOT NULL, created TEXT NOT NULL, filename TEXT,
  status TEXT NOT NULL, total INTEGER DEFAULT 0, done INTEGER DEFAULT 0, rate REAL,
  error TEXT, stats TEXT, finished TEXT
);
"""
_lock = threading.Lock()
_wake = threading.Event()
_stop: set[str] = set()
_runner: threading.Thread | None = None


def init():
    ROOT.mkdir(parents=True, exist_ok=True)
    with db.conn() as c:
        c.executescript(SCHEMA)
        c.execute("UPDATE bulk_jobs SET status='queued' WHERE status='running'")  # resume after restart
    start_runner()


def folder(jid: str) -> Path:
    return ROOT / jid


def create(uid: int, filename: str, workbook: bytes, progress: bytes | None) -> str:
    jid = secrets.token_hex(6)
    d = folder(jid)
    d.mkdir(parents=True)
    (d / "input.xlsx").write_bytes(workbook)
    numbers = company_numbers(d / "input.xlsx")
    if not numbers:
        raise ValueError("No company numbers found. The sheet needs a 'Company Number' column.")
    (d / "numbers.txt").write_text("\n".join(numbers) + "\n")
    seeded = 0
    if progress:
        wanted = set(numbers)
        with open(d / "cache.jsonl", "w") as fh:
            for line in progress.decode("utf-8", "ignore").splitlines():
                try:
                    rec = json.loads(line)
                except ValueError:
                    continue
                if isinstance(rec, dict) and rec.get("n") in wanted and rec.get("ins_status") != "error":
                    fh.write(json.dumps(rec) + "\n")
                    seeded += 1
    with db.conn() as c:
        c.execute("INSERT INTO bulk_jobs(id,user_id,created,filename,status,total,done) VALUES(?,?,?,?,?,?,?)",
                  (jid, uid, db.now(), filename, "queued", len(numbers), seeded))
    _wake.set()
    return jid


def get(jid):
    with db.conn() as c:
        return c.execute("SELECT bulk_jobs.*, users.name AS user_name FROM bulk_jobs "
                         "LEFT JOIN users ON users.id=bulk_jobs.user_id WHERE bulk_jobs.id=?", (jid,)).fetchone()


def all_jobs():
    with db.conn() as c:
        return c.execute("SELECT bulk_jobs.*, users.name AS user_name FROM bulk_jobs "
                         "LEFT JOIN users ON users.id=bulk_jobs.user_id ORDER BY created DESC LIMIT 20").fetchall()


def update(jid, **kw):
    sets = ",".join(f"{k}=?" for k in kw)
    with db.conn() as c:
        c.execute(f"UPDATE bulk_jobs SET {sets} WHERE id=?", (*kw.values(), jid))


def stop(jid):
    _stop.add(jid)
    with db.conn() as c:
        c.execute("UPDATE bulk_jobs SET status='stopped' WHERE id=? AND status='queued'", (jid,))


def resume(jid):
    _stop.discard(jid)
    update(jid, status="queued", error=None)
    _wake.set()


def build_output(jid, partial=False) -> tuple[Path, Path, dict]:
    d = folder(jid)
    cache = d / "cache.jsonl"
    if not cache.exists():
        cache.write_text("")
    name = "partial" if partial else "filled"
    out, log_csv = d / f"{name}.xlsx", d / f"{name}-sources.csv"
    with _lock:  # one workbook build at a time; they are memory hungry
        stats = fill(d / "input.xlsx", cache, out, log_csv)
    return out, log_csv, stats


def _done_set(cache: Path) -> set[str]:
    done = set()
    if cache.exists():
        with open(cache) as fh:
            for line in fh:
                try:
                    rec = json.loads(line)
                except ValueError:
                    continue
                if rec.get("ins_status") != "error" and rec.get("chg_status") != "error":
                    done.add(rec["n"])
    return done


def _run(jid):
    d = folder(jid)
    numbers = [l.strip() for l in open(d / "numbers.txt") if l.strip()]
    cache = d / "cache.jsonl"
    done = _done_set(cache)
    todo = [n for n in numbers if n not in done]
    update(jid, status="running", done=len(numbers) - len(todo), error=None)
    t0, count, last = time.time(), 0, 0.0
    with open(cache, "a") as fh, ThreadPoolExecutor(6) as ex:
        # submit in small batches so a stop request takes effect quickly
        i = 0
        while i < len(todo):
            if jid in _stop:
                update(jid, status="stopped")
                return
            batch = todo[i:i + 24]
            i += len(batch)
            for rec in ex.map(fetch.fetch_one, batch):
                fh.write(json.dumps(rec) + "\n")
                count += 1
            fh.flush()
            if time.time() - last > 5:
                last = time.time()
                update(jid, done=len(numbers) - len(todo) + count, rate=round(count / (time.time() - t0), 3))
    update(jid, done=len(numbers), rate=round(count / max(time.time() - t0, 1), 3))
    _, _, stats = build_output(jid)
    update(jid, status="done", stats=json.dumps(stats), finished=db.now())


def _loop():
    while True:
        with db.conn() as c:
            row = c.execute("SELECT id FROM bulk_jobs WHERE status='queued' ORDER BY created LIMIT 1").fetchone()
        if not row:
            _wake.wait(30)
            _wake.clear()
            continue
        jid = row["id"]
        try:
            _run(jid)
        except Exception as e:  # keep the runner alive; the job can be resumed
            log.error("bulk job %s failed: %s", jid, traceback.format_exc())
            update(jid, status="failed", error=f"{type(e).__name__}: {e}")
        finally:
            _stop.discard(jid)


def start_runner():
    global _runner
    if _runner is None or not _runner.is_alive():
        _runner = threading.Thread(target=_loop, name="bulk-runner", daemon=True)
        _runner.start()
