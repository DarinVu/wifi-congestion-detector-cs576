"""SQLite storage for runs, per-second samples, iperf3 tests and event markers.

Missing values are stored as NULL, never as 0.
"""

import json
import sqlite3
import time
from pathlib import Path

DEFAULT_DB = Path(__file__).resolve().parent.parent / "data" / "wifi.db"

SCHEMA = """
CREATE TABLE IF NOT EXISTS runs (
    run_id      INTEGER PRIMARY KEY AUTOINCREMENT,
    label       TEXT NOT NULL,          -- e.g. "quiet", "load_2", "weak_signal", "wan_only", "live"
    started_at  REAL NOT NULL,          -- unix seconds
    ended_at    REAL,
    notes       TEXT,
    config      TEXT                    -- JSON: targets, interface, etc.
);

CREATE TABLE IF NOT EXISTS samples (
    ts              REAL NOT NULL,      -- unix seconds
    run_id          INTEGER NOT NULL REFERENCES runs(run_id),
    -- ping paths: *_sent is NULL when the target isn't configured
    local_sent      INTEGER, local_recv    INTEGER, local_rtt_ms    REAL,
    external_sent   INTEGER, external_recv INTEGER, external_rtt_ms REAL,
    lan_sent        INTEGER, lan_recv      INTEGER, lan_rtt_ms      REAL,
    -- radio (CoreWLAN)
    rssi_dbm        REAL,
    noise_dbm       REAL,
    tx_rate_mbps    REAL,
    channel         INTEGER,
    channel_width   INTEGER,
    -- interface counters (cumulative, from netstat)
    ipkts INTEGER, ierrs INTEGER, opkts INTEGER, oerrs INTEGER,
    -- 1 while a scheduled iperf3 test is running (exclude from passive stats)
    iperf_active    INTEGER NOT NULL DEFAULT 0
);
CREATE INDEX IF NOT EXISTS idx_samples_run_ts ON samples(run_id, ts);

CREATE TABLE IF NOT EXISTS iperf_tests (
    ts          REAL NOT NULL,          -- test start
    run_id      INTEGER NOT NULL REFERENCES runs(run_id),
    server      TEXT,
    direction   TEXT,                   -- "upload" or "download"
    duration_s  REAL,
    mbps        REAL,
    retransmits INTEGER,
    error       TEXT
);

CREATE TABLE IF NOT EXISTS events (
    ts      REAL NOT NULL,
    run_id  INTEGER REFERENCES runs(run_id),
    text    TEXT NOT NULL               -- e.g. "load start", "load stop"
);

CREATE TABLE IF NOT EXISTS baselines (
    baseline_id INTEGER PRIMARY KEY AUTOINCREMENT,
    name        TEXT,                   -- test setup, e.g. "desk_ch149"
    run_id      INTEGER REFERENCES runs(run_id),
    created_at  REAL NOT NULL,
    stats       TEXT NOT NULL           -- JSON baseline statistics
);

CREATE TABLE IF NOT EXISTS alerts (
    alert_id    INTEGER PRIMARY KEY AUTOINCREMENT,
    run_id      INTEGER NOT NULL REFERENCES runs(run_id),
    mode        TEXT NOT NULL,          -- "multi_signal" or "latency_only"
    kind        TEXT NOT NULL,          -- congestion / weak_signal / wan_issue / degraded_uncertain / latency
    start_ts    REAL NOT NULL,          -- start of the first degraded window
    alert_ts    REAL NOT NULL,          -- when the alert fired (end of 3rd degraded window)
    end_ts      REAL,                   -- when it cleared; NULL if still active
    explanation TEXT
);
"""

SAMPLE_COLUMNS = [
    "ts", "run_id",
    "local_sent", "local_recv", "local_rtt_ms",
    "external_sent", "external_recv", "external_rtt_ms",
    "lan_sent", "lan_recv", "lan_rtt_ms",
    "rssi_dbm", "noise_dbm", "tx_rate_mbps", "channel", "channel_width",
    "ipkts", "ierrs", "opkts", "oerrs",
    "iperf_active",
]


def connect(db_path=DEFAULT_DB):
    db_path = Path(db_path)
    db_path.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(db_path, check_same_thread=False, timeout=10)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA journal_mode=WAL")  # lets the dashboard read while we write
    conn.executescript(SCHEMA)
    return conn


def start_run(conn, label, notes=None, config=None):
    cur = conn.execute(
        "INSERT INTO runs(label, started_at, notes, config) VALUES (?, ?, ?, ?)",
        (label, time.time(), notes, json.dumps(config or {})),
    )
    conn.commit()
    return cur.lastrowid


def end_run(conn, run_id):
    conn.execute("UPDATE runs SET ended_at = ? WHERE run_id = ?", (time.time(), run_id))
    conn.commit()


def insert_sample(conn, sample):
    cols = ", ".join(SAMPLE_COLUMNS)
    marks = ", ".join("?" for _ in SAMPLE_COLUMNS)
    conn.execute(f"INSERT INTO samples({cols}) VALUES ({marks})",
                 [sample.get(c) for c in SAMPLE_COLUMNS])
    conn.commit()


def insert_iperf(conn, row):
    conn.execute(
        "INSERT INTO iperf_tests(ts, run_id, server, direction, duration_s, mbps, retransmits, error) "
        "VALUES (:ts, :run_id, :server, :direction, :duration_s, :mbps, :retransmits, :error)",
        row,
    )
    conn.commit()


def insert_event(conn, run_id, text, ts=None):
    conn.execute("INSERT INTO events(ts, run_id, text) VALUES (?, ?, ?)",
                 (ts or time.time(), run_id, text))
    conn.commit()


def latest_open_run(conn):
    return conn.execute(
        "SELECT * FROM runs WHERE ended_at IS NULL ORDER BY run_id DESC LIMIT 1"
    ).fetchone()
