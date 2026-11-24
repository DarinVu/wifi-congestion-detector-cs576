"""Glue between SQLite, windowing and rules. Shared by the CLI and dashboard."""

import json
import time

from .rules import Config, compute_baseline, detect
from .windows import load_windows

AUTO_BASELINE_WINDOWS = 4   # 2 minutes, used when no stored baseline exists


def save_baseline(conn, run_id, name=None):
    _, windows = load_windows(conn, run_id)
    stats = compute_baseline(windows)
    cur = conn.execute(
        "INSERT INTO baselines(name, run_id, created_at, stats) VALUES (?, ?, ?, ?)",
        (name, run_id, time.time(), json.dumps(stats)))
    conn.commit()
    return cur.lastrowid, stats


def get_baseline(conn, baseline_id=None):
    """Return (baseline_id, stats) for the given or most recent baseline, or (None, None)."""
    if baseline_id is None:
        row = conn.execute("SELECT * FROM baselines ORDER BY baseline_id DESC LIMIT 1").fetchone()
    else:
        row = conn.execute("SELECT * FROM baselines WHERE baseline_id = ?", (baseline_id,)).fetchone()
    return (row["baseline_id"], json.loads(row["stats"])) if row else (None, None)


def analyze_run(conn, run_id, baseline_id=None, cfg=Config(), live=False, save=True,
                modes=("multi_signal", "latency_only")):
    """Window a run, apply the detectors, and (optionally) store its alerts.

    Returns dict with run, baseline info, per-window results per mode, alerts per mode.
    """
    run, windows = load_windows(conn, run_id, live=live)
    bid, base = get_baseline(conn, baseline_id)
    baseline_source = f"baseline #{bid}" if bid else None
    if base is None:
        # Fall back to the run's own first 2 minutes.
        head = windows[:AUTO_BASELINE_WINDOWS]
        if len([w for w in head if w["sufficient"]]) < 2:
            return {"run": run, "baseline": None, "baseline_source": None,
                    "windows": {}, "alerts": {}, "error": "waiting for baseline data"}
        base = compute_baseline(head)
        baseline_source = "first 2 minutes of this run (no stored baseline)"

    result = {"run": run, "baseline": base, "baseline_source": baseline_source,
              "windows": {}, "alerts": {}, "error": None}
    for mode in modes:
        per_window, alerts = detect(windows, base, cfg, mode)
        result["windows"][mode] = per_window
        result["alerts"][mode] = alerts
        if save:
            conn.execute("DELETE FROM alerts WHERE run_id = ? AND mode = ?", (run_id, mode))
            conn.executemany(
                "INSERT INTO alerts(run_id, mode, kind, start_ts, alert_ts, end_ts, explanation) "
                "VALUES (?, ?, ?, ?, ?, ?, ?)",
                [(run_id, a["mode"], a["kind"], a["start_ts"], a["alert_ts"], a["end_ts"],
                  a["explanation"]) for a in alerts])
    if save:
        conn.commit()
    return result
