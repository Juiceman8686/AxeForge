import sqlite3
import json
import time
import os
from typing import Optional, List, Dict
from contextlib import contextmanager

DB_PATH = os.environ.get("DB_PATH", "/app/data/axeforge.db")


class Database:
    def __init__(self):
        os.makedirs(os.path.dirname(DB_PATH), exist_ok=True)
        self._init_db()

    @contextmanager
    def _conn(self):
        conn = sqlite3.connect(DB_PATH, check_same_thread=False)
        conn.row_factory = sqlite3.Row
        conn.execute("PRAGMA journal_mode=WAL")
        conn.execute("PRAGMA synchronous=NORMAL")
        try:
            yield conn
            conn.commit()
        except Exception:
            conn.rollback()
            raise
        finally:
            conn.close()

    def _init_db(self):
        with self._conn() as conn:
            conn.executescript("""
                CREATE TABLE IF NOT EXISTS miners (
                    id        INTEGER PRIMARY KEY AUTOINCREMENT,
                    ip        TEXT    UNIQUE NOT NULL,
                    nickname  TEXT,
                    created_at INTEGER DEFAULT (strftime('%s','now'))
                );

                CREATE TABLE IF NOT EXISTS metrics (
                    id               INTEGER PRIMARY KEY AUTOINCREMENT,
                    miner_id         INTEGER NOT NULL,
                    timestamp        INTEGER NOT NULL,
                    hashrate         REAL,
                    temp             REAL,
                    voltage          INTEGER,
                    frequency        INTEGER,
                    fan_speed        INTEGER,
                    fan_rpm          INTEGER,
                    shares_accepted  INTEGER,
                    shares_rejected  INTEGER,
                    power            REAL,
                    vrm_temp         REAL,
                    FOREIGN KEY (miner_id) REFERENCES miners(id)
                );
                CREATE INDEX IF NOT EXISTS idx_metrics_miner_time
                    ON metrics(miner_id, timestamp DESC);

                CREATE TABLE IF NOT EXISTS tuning_sessions (
                    id               INTEGER PRIMARY KEY AUTOINCREMENT,
                    miner_id         INTEGER NOT NULL,
                    name             TEXT    NOT NULL,
                    started_at       INTEGER NOT NULL,
                    ended_at         INTEGER,
                    priority         TEXT    NOT NULL,
                    step_mode        TEXT    DEFAULT 'slow',
                    baseline_freq    INTEGER,
                    baseline_voltage INTEGER,
                    best_freq        INTEGER,
                    best_voltage     INTEGER,
                    best_hashrate    REAL,
                    best_temp        REAL,
                    best_error_rate  REAL,
                    best_score       REAL,
                    summary          TEXT,
                    status           TEXT    DEFAULT 'running',
                    FOREIGN KEY (miner_id) REFERENCES miners(id)
                );

                CREATE TABLE IF NOT EXISTS session_datapoints (
                    id         INTEGER PRIMARY KEY AUTOINCREMENT,
                    session_id INTEGER NOT NULL,
                    timestamp  INTEGER NOT NULL,
                    hashrate   REAL,
                    temp       REAL,
                    error_rate REAL,
                    frequency  INTEGER,
                    voltage    INTEGER,
                    score      REAL,
                    FOREIGN KEY (session_id) REFERENCES tuning_sessions(id)
                );

                CREATE TABLE IF NOT EXISTS settings (
                    key   TEXT PRIMARY KEY,
                    value TEXT NOT NULL
                );

                INSERT OR IGNORE INTO settings VALUES ('max_temp',              '65');
                INSERT OR IGNORE INTO settings VALUES ('max_voltage',           '1250');
                INSERT OR IGNORE INTO settings VALUES ('error_rate_threshold',  '1.0');
                INSERT OR IGNORE INTO settings VALUES ('theme',                 'dark');
            """)

    # ── Miners ────────────────────────────────────────────────────────────────

    def add_miner(self, ip: str, nickname: str = None) -> int:
        with self._conn() as conn:
            cur = conn.execute(
                "INSERT OR IGNORE INTO miners (ip, nickname) VALUES (?,?)",
                (ip, nickname or ip),
            )
            if cur.lastrowid:
                return cur.lastrowid
            row = conn.execute("SELECT id FROM miners WHERE ip=?", (ip,)).fetchone()
            return row["id"]

    def get_miners(self) -> List[Dict]:
        with self._conn() as conn:
            return [dict(r) for r in conn.execute(
                "SELECT * FROM miners ORDER BY created_at"
            ).fetchall()]

    def get_miner_by_id(self, miner_id: int) -> Optional[Dict]:
        with self._conn() as conn:
            row = conn.execute(
                "SELECT * FROM miners WHERE id=?", (miner_id,)
            ).fetchone()
            return dict(row) if row else None

    def update_miner_nickname(self, miner_id: int, nickname: str):
        with self._conn() as conn:
            conn.execute(
                "UPDATE miners SET nickname=? WHERE id=?", (nickname, miner_id)
            )

    def delete_miner(self, miner_id: int):
        with self._conn() as conn:
            conn.execute("DELETE FROM miners  WHERE id=?",       (miner_id,))
            conn.execute("DELETE FROM metrics WHERE miner_id=?", (miner_id,))

    # ── Metrics ───────────────────────────────────────────────────────────────

    def save_metrics(self, miner_id: int, s: Dict):
        """Persist a downsampled snapshot every ~10 s (caller decides frequency)."""
        with self._conn() as conn:
            conn.execute(
                """INSERT INTO metrics
                   (miner_id,timestamp,hashrate,temp,voltage,frequency,
                    fan_speed,fan_rpm,shares_accepted,shares_rejected,power,vrm_temp)
                   VALUES (?,?,?,?,?,?,?,?,?,?,?,?)""",
                (
                    miner_id, int(time.time()),
                    s.get("hashRate"),      s.get("temp"),
                    s.get("coreVoltageActual", s.get("coreVoltage")),
                    s.get("frequency"),
                    s.get("fanspeed"),      s.get("fanrpm"),
                    s.get("sharesAccepted"),s.get("sharesRejected"),
                    s.get("power"),
                    s.get("vrTemp", s.get("boardtemp1")),
                ),
            )
            # Keep only last 24 h of raw metric rows
            cutoff = int(time.time()) - 86400
            conn.execute(
                "DELETE FROM metrics WHERE miner_id=? AND timestamp<?",
                (miner_id, cutoff),
            )

    def get_latest_metrics(self, miner_id: int) -> Optional[Dict]:
        with self._conn() as conn:
            row = conn.execute(
                "SELECT * FROM metrics WHERE miner_id=? ORDER BY timestamp DESC LIMIT 1",
                (miner_id,),
            ).fetchone()
            return dict(row) if row else None

    # ── Tuning sessions ───────────────────────────────────────────────────────

    def create_session(
        self, miner_id: int, name: str, priority: str,
        step_mode: str, baseline_freq: int, baseline_voltage: int,
    ) -> int:
        with self._conn() as conn:
            cur = conn.execute(
                """INSERT INTO tuning_sessions
                   (miner_id,name,started_at,priority,step_mode,
                    baseline_freq,baseline_voltage,status)
                   VALUES (?,?,?,?,?,?,?,'running')""",
                (miner_id, name, int(time.time()), priority,
                 step_mode, baseline_freq, baseline_voltage),
            )
            return cur.lastrowid

    def update_session(self, session_id: int, **kwargs):
        if not kwargs:
            return
        cols = ", ".join(f"{k}=?" for k in kwargs)
        vals = list(kwargs.values()) + [session_id]
        with self._conn() as conn:
            conn.execute(
                f"UPDATE tuning_sessions SET {cols} WHERE id=?", vals
            )

    def add_session_datapoint(
        self, session_id: int, hashrate: float, temp: float,
        error_rate: float, frequency: int, voltage: int, score: float,
    ):
        with self._conn() as conn:
            conn.execute(
                """INSERT INTO session_datapoints
                   (session_id,timestamp,hashrate,temp,error_rate,frequency,voltage,score)
                   VALUES (?,?,?,?,?,?,?,?)""",
                (session_id, int(time.time()), hashrate, temp,
                 error_rate, frequency, voltage, score),
            )

    def get_sessions(self, miner_id: int = None) -> List[Dict]:
        with self._conn() as conn:
            if miner_id:
                rows = conn.execute(
                    """SELECT ts.*, m.nickname, m.ip
                       FROM tuning_sessions ts JOIN miners m ON ts.miner_id=m.id
                       WHERE ts.miner_id=? ORDER BY ts.started_at DESC""",
                    (miner_id,),
                ).fetchall()
            else:
                rows = conn.execute(
                    """SELECT ts.*, m.nickname, m.ip
                       FROM tuning_sessions ts JOIN miners m ON ts.miner_id=m.id
                       ORDER BY ts.started_at DESC"""
                ).fetchall()
            return [dict(r) for r in rows]

    def get_session_datapoints(self, session_id: int) -> List[Dict]:
        with self._conn() as conn:
            return [dict(r) for r in conn.execute(
                "SELECT * FROM session_datapoints WHERE session_id=? ORDER BY timestamp",
                (session_id,),
            ).fetchall()]

    def delete_session(self, session_id: int):
        with self._conn() as conn:
            conn.execute(
                "DELETE FROM session_datapoints WHERE session_id=?", (session_id,)
            )
            conn.execute(
                "DELETE FROM tuning_sessions WHERE id=?", (session_id,)
            )

    # ── Settings ──────────────────────────────────────────────────────────────

    def get_setting(self, key: str) -> Optional[str]:
        with self._conn() as conn:
            row = conn.execute(
                "SELECT value FROM settings WHERE key=?", (key,)
            ).fetchone()
            return row["value"] if row else None

    def get_all_settings(self) -> Dict:
        with self._conn() as conn:
            return {r["key"]: r["value"] for r in conn.execute(
                "SELECT key, value FROM settings"
            ).fetchall()}

    def set_setting(self, key: str, value: str):
        with self._conn() as conn:
            conn.execute(
                "INSERT OR REPLACE INTO settings (key,value) VALUES (?,?)",
                (key, str(value)),
            )
