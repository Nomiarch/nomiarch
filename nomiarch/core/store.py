"""The core alone owns SQLite; services communicate over authenticated APIs."""
import hashlib
import json
from pathlib import Path
import secrets
import sqlite3
import time
import uuid

from nomiarch.common import canonical, private_dir


class Store:
    def __init__(self, root):
        self.path = private_dir(root) / "core.db"
        with self.connect() as db:
            db.executescript("""
                PRAGMA journal_mode=WAL;
                CREATE TABLE IF NOT EXISTS events (
                    seq INTEGER PRIMARY KEY AUTOINCREMENT, payload TEXT NOT NULL,
                    previous TEXT NOT NULL, hash TEXT NOT NULL);
                CREATE TABLE IF NOT EXISTS tasks (
                    id TEXT PRIMARY KEY, created REAL NOT NULL, config TEXT NOT NULL,
                    status TEXT NOT NULL, result TEXT, lease TEXT, until REAL,
                    attempts INTEGER NOT NULL DEFAULT 0, idem TEXT UNIQUE, input_hash TEXT);
            """)
        self.path.chmod(0o600)

    def connect(self):
        db = sqlite3.connect(self.path, timeout=10)
        db.row_factory = sqlite3.Row
        db.execute("PRAGMA synchronous=FULL")
        return db

    @staticmethod
    def event(db, kind, subject, details):
        prev = db.execute("SELECT hash FROM events ORDER BY seq DESC LIMIT 1").fetchone()
        prev = prev[0] if prev else "0" * 64
        payload = canonical({"time": time.time(), "kind": kind, "subject": subject,
                             "details": details}).decode()
        h = hashlib.sha256(prev.encode() + payload.encode()).hexdigest()
        db.execute("INSERT INTO events(payload, previous, hash) VALUES(?,?,?)", (payload, prev, h))

    def submit(self, config, idem=None):
        encoded = canonical(config).decode()
        h = hashlib.sha256(encoded.encode()).hexdigest()
        with self.connect() as db:
            db.execute("BEGIN IMMEDIATE")
            if idem:
                row = db.execute("SELECT * FROM tasks WHERE idem=?", (idem,)).fetchone()
                if row:
                    if row["input_hash"] != h:
                        raise ValueError("Idempotency key already used for different input")
                    return row["id"]
            if db.execute("SELECT count(*) FROM tasks WHERE status IN ('queued','running')").fetchone()[0] >= 100:
                raise ValueError("Task queue is full")
            task = str(uuid.uuid4())
            db.execute("INSERT INTO tasks(id,created,config,status,idem,input_hash) VALUES(?,?,?,?,?,?)",
                       (task, time.time(), encoded, "queued", idem, h))
            self.event(db, "task.submitted", task, {"input_sha256": h})
            return task

    def claim(self):
        with self.connect() as db:
            db.execute("BEGIN IMMEDIATE")
            for row in db.execute("SELECT id FROM tasks WHERE status='running' AND until<? AND attempts>=3",
                                  (time.time(),)).fetchall():
                db.execute("UPDATE tasks SET status='failed', lease=NULL WHERE id=?", (row["id"],))
                self.event(db, "task.failed", row["id"], {"reason": "lease retry limit"})
            row = db.execute("""SELECT * FROM tasks WHERE status='queued' OR
                (status='running' AND until<? AND attempts<3) ORDER BY created LIMIT 1""", (time.time(),)).fetchone()
            if not row:
                return None
            lease = secrets.token_urlsafe(32)
            db.execute("UPDATE tasks SET status='running', lease=?, until=?, attempts=attempts+1 WHERE id=?",
                       (lease, time.time() + 180, row["id"]))
            self.event(db, "task.claimed", row["id"], {"attempt": row["attempts"] + 1})
            return {"id": row["id"], "config": json.loads(row["config"]), "lease": lease}

    def complete(self, task, lease, result, ok):
        with self.connect() as db:
            db.execute("BEGIN IMMEDIATE")
            row = db.execute("SELECT * FROM tasks WHERE id=?", (task,)).fetchone()
            if not row or row["status"] != "running" or not secrets.compare_digest(row["lease"] or "", lease):
                raise ValueError("Stale or invalid task lease")
            if row["until"] < time.time():
                raise ValueError("Task lease expired")
            status = "completed" if ok else "failed"
            db.execute("UPDATE tasks SET status=?,result=?,lease=NULL,until=NULL WHERE id=?",
                       (status, canonical(result).decode(), task))
            self.event(db, "task." + status, task, {"result_sha256": hashlib.sha256(canonical(result)).hexdigest()})

    def audit(self, kind, task, details):
        with self.connect() as db:
            db.execute("BEGIN IMMEDIATE")
            self.event(db, kind, task, details)

    def task(self, task):
        with self.connect() as db:
            row = db.execute("SELECT id,created,status,result,attempts FROM tasks WHERE id=?", (task,)).fetchone()
            if not row:
                return None
            result = dict(row)
            result["result"] = json.loads(result["result"]) if result["result"] else None
            return result

    def evidence(self):
        with self.connect() as db:
            rows = db.execute("SELECT * FROM events ORDER BY seq DESC LIMIT 1000").fetchall()
        events = [dict(row) for row in reversed(rows)]
        return {"events": events, "checkpoint": events[-1]["hash"] if events else "0" * 64,
                "scope": "most recent 1000 events; retain checkpoints outside this host"}
