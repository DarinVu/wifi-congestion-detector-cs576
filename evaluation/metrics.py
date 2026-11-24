"""Label runs, replay both detectors, and score them (slide 12).

Operational labels (not universal proof of congestion):
  positive runs : load_1, load_2, load_4   (controlled contention)
  negative runs : quiet, weak_signal, wan_only

A window counts as a predicted positive when a congestion alert is active
(multi-signal: kind "congestion"; latency-only: kind "latency"). Weak-signal,
interference and WAN alerts from the multi-signal detector are non-congestion verdicts.
"""

import csv
import itertools
import json
from dataclasses import replace
from pathlib import Path

from detector.rules import Config, detect
from detector.windows import load_windows

POSITIVE = {"load_1", "load_2", "load_4"}
NEGATIVE = {"quiet", "weak_signal", "wan_only"}
CONDITION_ORDER = ["quiet", "load_1", "load_2", "load_4", "weak_signal", "wan_only"]
POSITIVE_KIND = {"multi_signal": "congestion", "latency_only": "latency"}
MODES = ("multi_signal", "latency_only")


def load_runs(conn, db_path):
    """Completed runs with a test condition label, plus their dev/test split.

    Uses data/schedule.csv for the split when present; otherwise every run is "dev".
    """
    split = {}
    sched = Path(db_path).parent / "schedule.csv"
    if sched.exists():
        with open(sched, newline="") as f:
            for r in csv.DictReader(f):
                if r["run_id"]:
                    split[int(r["run_id"])] = r["split"]
    rows = conn.execute(
        "SELECT * FROM runs WHERE ended_at IS NOT NULL AND label IN (%s) ORDER BY run_id"
        % ",".join("?" * len(CONDITION_ORDER)), CONDITION_ORDER).fetchall()
    return [{"run_id": r["run_id"], "condition": r["label"], "split": split.get(r["run_id"], "dev"),
             "started_at": r["started_at"], "ended_at": r["ended_at"]} for r in rows]


def baseline_for(conn, started_at):
    """The latest baseline saved before this run started (the setup's baseline)."""
    row = conn.execute("SELECT * FROM baselines WHERE created_at <= ? "
                       "ORDER BY created_at DESC LIMIT 1", (started_at,)).fetchone()
    if row is None:  # fall back to any baseline
        row = conn.execute("SELECT * FROM baselines ORDER BY baseline_id LIMIT 1").fetchone()
    return (row["baseline_id"], json.loads(row["stats"])) if row else (None, None)


def prepare(conn, runs):
    """Load windows and baselines once; replaying the detector is then cheap."""
    prepared = []
    for r in runs:
        _, windows = load_windows(conn, r["run_id"])
        bid, base = baseline_for(conn, r["started_at"])
        if base is None:
            raise SystemExit("No baseline saved. Run: python -m experiment baseline --name <setup>")
        n_samples = conn.execute("SELECT COUNT(*) FROM samples WHERE run_id = ?",
                                 (r["run_id"],)).fetchone()[0]
        prepared.append({**r, "windows": windows, "baseline": base, "baseline_id": bid,
                         "n_samples": n_samples})
    return prepared


def replay(prepared, cfg, mode):
    """Run one detector over every prepared run."""
    out = []
    for r in prepared:
        per_window, alerts = detect(r["windows"], r["baseline"], cfg, mode)
        out.append({**r, "per_window": per_window, "alerts": alerts})
    return out


def score(results, mode, split=None):
    """Window-level precision/recall plus run-level delay and false alerts per hour."""
    pos_kind = POSITIVE_KIND[mode]
    tp = fp = fn = tn = 0
    delays, missed, false_alerts, neg_hours = [], 0, 0, 0.0
    for r in results:
        if split and r["split"] != split:
            continue
        positive = r["condition"] in POSITIVE
        for w in r["per_window"]:
            if w["state"] == "insufficient":
                continue
            pred = w["status"] == pos_kind
            if positive and pred:
                tp += 1
            elif positive:
                fn += 1
            elif pred:
                fp += 1
            else:
                tn += 1
        fired = [a for a in r["alerts"] if a["kind"] == pos_kind]
        if positive:
            if fired:
                delays.append(fired[0]["alert_ts"] - r["started_at"])
            else:
                missed += 1
        else:
            false_alerts += len(fired)
            neg_hours += (r["ended_at"] - r["started_at"]) / 3600

    precision = tp / (tp + fp) if tp + fp else None
    recall = tp / (tp + fn) if tp + fn else None
    f1 = (2 * precision * recall / (precision + recall)
          if precision and recall else 0.0 if precision is not None and recall is not None else None)
    return {
        "mode": mode, "split": split or "all",
        "tp": tp, "fp": fp, "fn": fn, "tn": tn,
        "precision": precision, "recall": recall, "f1": f1,
        "false_alerts": false_alerts,
        "false_alerts_per_hour": false_alerts / neg_hours if neg_hours else None,
        "delay_s_median": sorted(delays)[len(delays) // 2] if delays else None,
        "delay_s_max": max(delays) if delays else None,
        "positive_runs_missed": missed,
    }


def verdict_matrix(results, mode, split=None):
    """Condition x displayed status, counted in windows (for the confusion plot)."""
    statuses = (["healthy", "watching", "congestion", "weak_signal", "interference",
                 "wan_issue", "degraded_uncertain"] if mode == "multi_signal"
                else ["healthy", "watching", "latency"])
    m = {c: {s: 0 for s in statuses} for c in CONDITION_ORDER}
    for r in results:
        if split and r["split"] != split:
            continue
        for w in r["per_window"]:
            if w["state"] == "insufficient":
                continue
            s = "watching" if w["status"].startswith("watching") else w["status"]
            m[r["condition"]][s] = m[r["condition"]].get(s, 0) + 1
    return statuses, m


def data_quality(prepared):
    """Missing-data rate and probe overhead (slide 12, last row)."""
    expected = sum((r["ended_at"] - r["started_at"]) for r in prepared)
    got = sum(r["n_samples"] for r in prepared)
    # Each probe: one 84-byte ICMP echo out and one back, per path, per second.
    paths = 2
    return {
        "missing_sample_rate": max(0.0, 1 - got / expected) if expected else None,
        "probe_overhead_bps": paths * 2 * 84 * 8,
        "windows_insufficient": sum(1 for r in prepared for w in r["windows"] if not w["sufficient"]),
        "windows_total": sum(len(r["windows"]) for r in prepared),
    }


GRID = {
    "rtt_factor": [1.5, 2.0, 3.0],
    "rtt_margin_ms": [10.0, 20.0, 30.0],
    "persist_windows": [2, 3, 4],
}


def tune(prepared, mode="multi_signal"):
    """Grid-search a few thresholds on DEV runs only (maximize F1, then fewer false alerts)."""
    dev = [r for r in prepared if r["split"] == "dev"]
    best = None
    for vals in itertools.product(*GRID.values()):
        cfg = replace(Config(), **dict(zip(GRID, vals)))
        s = score(replay(dev, cfg, mode), mode)
        key = (s["f1"] or 0.0, -(s["false_alerts_per_hour"] or 0.0), -(s["delay_s_median"] or 1e9))
        if best is None or key > best[0]:
            best = (key, cfg, s)
    return best[1], best[2]
