"""Summarize per-second samples into non-overlapping 30-second windows."""

import math
import statistics

WINDOW_S = 30
MIN_COVERAGE = 0.5   # a window needs >= 50% of its expected samples


def p95(values):
    """Nearest-rank 95th percentile."""
    if not values:
        return None
    s = sorted(values)
    return s[max(0, math.ceil(0.95 * len(s)) - 1)]


def _median(values):
    return statistics.median(values) if values else None


def _path_stats(rows, path):
    sent = [r for r in rows if r[f"{path}_sent"]]
    if not sent:
        return {f"{path}_p95_ms": None, f"{path}_median_ms": None, f"{path}_loss_pct": None}
    rtts = [r[f"{path}_rtt_ms"] for r in sent if r[f"{path}_rtt_ms"] is not None]
    lost = sum(1 for r in sent if not r[f"{path}_recv"])
    return {
        f"{path}_p95_ms": p95(rtts),
        f"{path}_median_ms": _median(rtts),
        f"{path}_loss_pct": 100.0 * lost / len(sent),
    }


def summarize(rows, start_ts, interval_s=1.0, window_s=WINDOW_S, end_ts=None,
              include_iperf=False):
    """Group sample rows into windows [start + k*window_s, start + (k+1)*window_s).

    Only complete windows (ending at or before end_ts) are returned. Samples taken
    while an iperf3 test was running are excluded unless include_iperf is True,
    because that load comes from the monitor itself.
    """
    if end_ts is None:
        end_ts = rows[-1]["ts"] + interval_s if rows else start_ts
    n_windows = int((end_ts - start_ts) // window_s)
    buckets = [[] for _ in range(n_windows)]
    iperf_counts = [0] * n_windows
    for r in rows:
        k = int((r["ts"] - start_ts) // window_s)
        if 0 <= k < n_windows:
            if r["iperf_active"] and not include_iperf:
                iperf_counts[k] += 1
                continue
            buckets[k].append(r)

    expected = window_s / interval_s
    windows = []
    for k, b in enumerate(buckets):
        w = {
            "index": k,
            "start_ts": start_ts + k * window_s,
            "end_ts": start_ts + (k + 1) * window_s,
            "n": len(b),
            "coverage": len(b) / max(1.0, expected - iperf_counts[k]),
            "iperf_excluded": iperf_counts[k],
        }
        for path in ("local", "external", "lan"):
            w.update(_path_stats(b, path))
        for col in ("rssi_dbm", "noise_dbm", "tx_rate_mbps"):
            w[f"{col}_median"] = _median([r[col] for r in b if r[col] is not None])
        snr = [r["rssi_dbm"] - r["noise_dbm"] for r in b
               if r["rssi_dbm"] is not None and r["noise_dbm"] is not None]
        w["snr_db_median"] = _median(snr)
        # Interface error deltas within the window (cumulative counters).
        oerrs = [r["oerrs"] for r in b if r["oerrs"] is not None]
        w["oerrs_delta"] = (oerrs[-1] - oerrs[0]) if len(oerrs) >= 2 and oerrs[-1] >= oerrs[0] else None
        # Enough samples, and the local path was probed (a fully lost window still counts).
        w["sufficient"] = w["coverage"] >= MIN_COVERAGE and w["local_loss_pct"] is not None
        windows.append(w)
    return windows


def load_windows(conn, run_id, include_iperf=False, live=False):
    """Load a run's samples from SQLite and summarize them into windows."""
    import json, time
    run = conn.execute("SELECT * FROM runs WHERE run_id = ?", (run_id,)).fetchone()
    if run is None:
        raise ValueError(f"No run {run_id}")
    interval = json.loads(run["config"] or "{}").get("interval_s", 1.0)
    rows = conn.execute("SELECT * FROM samples WHERE run_id = ? ORDER BY ts",
                        (run_id,)).fetchall()
    if run["ended_at"]:
        end_ts = run["ended_at"]
    elif live:
        end_ts = time.time()
    else:
        end_ts = None
    return run, summarize(rows, run["started_at"], interval, end_ts=end_ts,
                          include_iperf=include_iperf)
