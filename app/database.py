import sqlite3
from pathlib import Path


class Database:
    def __init__(self, path: str):
        self.path = path
        Path(path).parent.mkdir(parents=True, exist_ok=True)
        with self.connect() as db:
            db.executescript("""
                CREATE TABLE IF NOT EXISTS runs (
                    id TEXT PRIMARY KEY,
                    status TEXT NOT NULL,
                    site_url TEXT NOT NULL,
                    min_amount TEXT,
                    max_amount TEXT,
                    max_clicks INTEGER,
                    delay_ms INTEGER NOT NULL,
                    clicks_per_minute INTEGER,
                    login_status TEXT NOT NULL DEFAULT 'pending',
                    started_at TEXT NOT NULL,
                    ended_at TEXT,
                    error_text TEXT
                );
                CREATE TABLE IF NOT EXISTS clicks (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    run_id TEXT NOT NULL REFERENCES runs(id),
                    row_id TEXT NOT NULL,
                    amount TEXT,
                    timestamp TEXT NOT NULL,
                    popup_shown INTEGER NOT NULL,
                    outcome TEXT NOT NULL,
                    latency_ms INTEGER NOT NULL,
                    error_text TEXT
                );
                CREATE INDEX IF NOT EXISTS clicks_run_id ON clicks(run_id);
            """)
            columns = {row[1] for row in db.execute("PRAGMA table_info(runs)")}
            if "clicks_per_minute" not in columns:
                db.execute("ALTER TABLE runs ADD COLUMN clicks_per_minute INTEGER")
            if "login_status" not in columns:
                db.execute("ALTER TABLE runs ADD COLUMN login_status TEXT NOT NULL DEFAULT 'pending'")

    def connect(self):
        db = sqlite3.connect(self.path, timeout=10)
        db.row_factory = sqlite3.Row
        db.execute("PRAGMA foreign_keys=ON")
        return db

    def create_run(self, run: dict):
        with self.connect() as db:
            db.execute("""INSERT INTO runs
                (id,status,site_url,min_amount,max_amount,max_clicks,delay_ms,clicks_per_minute,login_status,started_at)
                VALUES (:id,:status,:site_url,:min_amount,:max_amount,:max_clicks,:delay_ms,:clicks_per_minute,:login_status,:started_at)""", run)

    def update_login_status(self, run_id: str, status: str):
        with self.connect() as db:
            db.execute("UPDATE runs SET login_status=? WHERE id=?", (status, run_id))

    def update_run(self, run_id: str, status: str, ended_at: str | None = None, error_text: str | None = None):
        with self.connect() as db:
            db.execute("UPDATE runs SET status=?, ended_at=?, error_text=? WHERE id=?",
                       (status, ended_at, error_text, run_id))

    def add_click(self, click: dict):
        with self.connect() as db:
            db.execute("""INSERT INTO clicks
                (run_id,row_id,amount,timestamp,popup_shown,outcome,latency_ms,error_text)
                VALUES (:run_id,:row_id,:amount,:timestamp,:popup_shown,:outcome,:latency_ms,:error_text)""", click)

    def has_active_run(self) -> bool:
        with self.connect() as db:
            row = db.execute("SELECT 1 FROM runs WHERE status='running' LIMIT 1").fetchone()
            return row is not None

    def get_run(self, run_id: str):
        with self.connect() as db:
            row = db.execute("SELECT * FROM runs WHERE id=?", (run_id,)).fetchone()
            return dict(row) if row else None

    def list_runs(self, limit: int = 50):
        with self.connect() as db:
            return [dict(row) for row in db.execute(
                "SELECT * FROM runs ORDER BY started_at DESC LIMIT ?", (limit,)).fetchall()]

    def list_clicks(self, run_id: str):
        with self.connect() as db:
            return [dict(row) for row in db.execute(
                "SELECT * FROM clicks WHERE run_id=? ORDER BY id", (run_id,)).fetchall()]
