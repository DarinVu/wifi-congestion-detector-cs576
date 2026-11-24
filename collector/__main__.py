"""Wi-Fi metric collector (macOS).

Samples once per second: ping to the router (local path), an external host,
and an optional wired LAN server, plus Wi-Fi radio stats and interface
counters. Optionally runs scheduled iperf3 tests. Everything goes to SQLite.

Examples:
    .venv/bin/python -m collector run --label quiet --duration 300
    .venv/bin/python -m collector run --label load_2 --iperf-server 192.168.86.50
    .venv/bin/python -m collector mark "load start"
    .venv/bin/python -m collector list
    .venv/bin/python -m collector export 3
"""

import argparse
import csv
import re
import subprocess
import sys
import threading
import time
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime
from pathlib import Path

from . import storage
from .probes import MacRadio, interface_counters, ping_once, run_iperf


def default_gateway():
    out = subprocess.run(["route", "-n", "get", "default"],
                         capture_output=True, text=True).stdout
    m = re.search(r"gateway: (\S+)", out)
    return m.group(1) if m else None


def fmt(v, unit="", nd=1):
    return "--" if v is None else f"{v:.{nd}f}{unit}"


# ------------------------------------------------------------------ run

def cmd_run(args):
    conn = storage.connect(args.db)
    db_lock = threading.Lock()

    radio = MacRadio()
    targets = {"local": args.local or default_gateway(), "external": args.external,
               "lan": args.lan_server}
    if not targets["local"]:
        sys.exit("Could not find the default gateway. Pass --local <router IP>.")

    config = {"interface": radio.name, "targets": targets, "interval_s": args.interval,
              "iperf_server": args.iperf_server, "iperf_interval_s": args.iperf_interval}
    run_id = storage.start_run(conn, args.label, args.notes, config)
    print(f"Run {run_id} [{args.label}] on {radio.name}: "
          + ", ".join(f"{k}={v}" for k, v in targets.items() if v))
    print("Ctrl+C to stop.\n")

    stop = threading.Event()
    iperf_active = threading.Event()

    def iperf_loop():
        # First test after one interval so the run starts with passive data.
        while not stop.wait(args.iperf_interval):
            iperf_active.set()
            row = run_iperf(args.iperf_server, args.iperf_duration, download=args.iperf_download)
            iperf_active.clear()
            row["run_id"] = run_id
            with db_lock:
                storage.insert_iperf(conn, row)
            print(f"  iperf3 {row['direction']}: "
                  + (f"{row['mbps']} Mbps, {row['retransmits']} retransmits"
                     if row["error"] is None else f"error: {row['error']}"))

    if args.iperf_server:
        threading.Thread(target=iperf_loop, daemon=True).start()

    pool = ThreadPoolExecutor(max_workers=3)
    timeout_ms = int(min(900, args.interval * 900))
    end_at = time.time() + args.duration if args.duration else None
    next_tick = time.time()
    n = 0
    try:
        while not end_at or time.time() < end_at:
            ts = time.time()
            futures = {k: pool.submit(ping_once, host, timeout_ms)
                       for k, host in targets.items() if host}
            sample = {"ts": ts, "run_id": run_id,
                      "iperf_active": 1 if iperf_active.is_set() else 0}
            sample.update(radio.read())
            sample.update(interface_counters(radio.name))
            for k, f in futures.items():
                rtt = f.result()
                sample[f"{k}_sent"] = 1
                sample[f"{k}_recv"] = 0 if rtt is None else 1
                sample[f"{k}_rtt_ms"] = rtt
            with db_lock:
                storage.insert_sample(conn, sample)
            n += 1

            if not args.quiet:
                print(f"{datetime.fromtimestamp(ts):%H:%M:%S}  "
                      f"local {fmt(sample.get('local_rtt_ms'), 'ms'):>8}  "
                      f"ext {fmt(sample.get('external_rtt_ms'), 'ms'):>8}  "
                      + (f"lan {fmt(sample.get('lan_rtt_ms'), 'ms'):>8}  " if targets["lan"] else "")
                      + f"rssi {fmt(sample.get('rssi_dbm'), 'dBm', 0):>7}  "
                      f"rate {fmt(sample.get('tx_rate_mbps'), 'M', 0):>5}"
                      + ("  [iperf]" if sample["iperf_active"] else ""))

            next_tick += args.interval
            delay = next_tick - time.time()
            if delay > 0:
                time.sleep(delay)
            else:
                next_tick = time.time()  # fell behind; don't try to catch up
    except KeyboardInterrupt:
        pass
    finally:
        stop.set()
        pool.shutdown(wait=False)
        with db_lock:
            storage.end_run(conn, run_id)
        print(f"\nRun {run_id} ended: {n} samples saved to {args.db}")


# ------------------------------------------------------------------ mark / list / export

def cmd_mark(args):
    conn = storage.connect(args.db)
    run = storage.latest_open_run(conn)
    if run is None:
        sys.exit("No run in progress.")
    storage.insert_event(conn, run["run_id"], args.text)
    print(f"Marked '{args.text}' in run {run['run_id']} at {datetime.now():%H:%M:%S}")


def cmd_list(args):
    conn = storage.connect(args.db)
    rows = conn.execute("""
        SELECT r.run_id, r.label, r.started_at, r.ended_at,
               COUNT(s.ts) AS n,
               SUM(s.local_sent) - SUM(s.local_recv) AS local_lost
        FROM runs r LEFT JOIN samples s ON s.run_id = r.run_id
        GROUP BY r.run_id ORDER BY r.run_id""").fetchall()
    if not rows:
        print("No runs yet.")
        return
    print(f"{'id':>4}  {'label':<14} {'started':<19}  {'dur':>6}  {'samples':>7}  local lost")
    for r in rows:
        dur = f"{(r['ended_at'] or time.time()) - r['started_at']:.0f}s"
        print(f"{r['run_id']:>4}  {r['label']:<14} "
              f"{datetime.fromtimestamp(r['started_at']):%Y-%m-%d %H:%M:%S}  {dur:>6}  "
              f"{r['n']:>7}  {r['local_lost'] or 0}"
              + ("" if r["ended_at"] else "  (running)"))


def cmd_export(args):
    conn = storage.connect(args.db)
    out_dir = Path(args.db).parent
    for table in ("samples", "iperf_tests", "events"):
        rows = conn.execute(f"SELECT * FROM {table} WHERE run_id = ? ORDER BY ts",
                            (args.run_id,)).fetchall()
        if not rows:
            continue
        path = out_dir / f"run{args.run_id}_{table}.csv"
        with open(path, "w", newline="") as f:
            w = csv.writer(f)
            w.writerow(rows[0].keys())
            w.writerows(rows)
        print(f"Wrote {len(rows)} rows to {path}")


def main():
    ap = argparse.ArgumentParser(prog="collector", description="Wi-Fi metric collector")
    ap.add_argument("--db", default=str(storage.DEFAULT_DB), help="SQLite path")
    sub = ap.add_subparsers(dest="cmd", required=True)

    r = sub.add_parser("run", help="collect samples")
    r.add_argument("--label", default="live",
                   help="run/condition label, e.g. quiet, load_1, load_2, load_4, weak_signal, wan_only")
    r.add_argument("--notes", help="free-text notes (placement, channel, etc.)")
    r.add_argument("--duration", type=float, help="seconds to run (default: until Ctrl+C)")
    r.add_argument("--interval", type=float, default=1.0, help="seconds between samples")
    r.add_argument("--local", help="local ping target (default: router / default gateway)")
    r.add_argument("--external", default="1.1.1.1", help="external ping target")
    r.add_argument("--lan-server", help="optional wired LAN server to ping")
    r.add_argument("--iperf-server", help="run scheduled iperf3 tests against this server")
    r.add_argument("--iperf-interval", type=float, default=120, help="seconds between iperf3 tests")
    r.add_argument("--iperf-duration", type=int, default=5, help="seconds per iperf3 test")
    r.add_argument("--iperf-download", action="store_true", help="test download (-R) instead of upload")
    r.add_argument("--quiet", action="store_true", help="don't print each sample")
    r.set_defaults(func=cmd_run)

    m = sub.add_parser("mark", help="add a timestamped event to the running run")
    m.add_argument("text")
    m.set_defaults(func=cmd_mark)

    sub.add_parser("list", help="list runs").set_defaults(func=cmd_list)

    e = sub.add_parser("export", help="export one run to CSV files in data/")
    e.add_argument("run_id", type=int)
    e.set_defaults(func=cmd_export)

    args = ap.parse_args()
    args.func(args)


if __name__ == "__main__":
    main()
