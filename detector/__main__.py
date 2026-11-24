"""Wi-Fi congestion detector CLI.

Examples:
    .venv/bin/python -m detector baseline 1 --name desk        # learn from a quiet run
    .venv/bin/python -m detector analyze 2                     # windows + alerts for a run
    .venv/bin/python -m detector analyze 2 --mode latency_only
    .venv/bin/python -m detector watch                         # live status for the running run
"""

import argparse
import sys
import time
from datetime import datetime
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
from collector import storage  # noqa: E402

from .engine import analyze_run, save_baseline  # noqa: E402
from .rules import LABELS  # noqa: E402


def hms(ts):
    return "--:--:--" if ts is None else f"{datetime.fromtimestamp(ts):%H:%M:%S}"


def f(v, nd=0):
    return "--" if v is None else f"{v:.{nd}f}"


def print_baseline(stats, source):
    print(f"Baseline ({source}): router p95 {f(stats['local_p95_ms'], 1)} ms, "
          f"external p95 {f(stats['external_p95_ms'], 1)} ms, "
          f"loss {f(stats['local_loss_pct'], 1)}%, signal {f(stats['rssi_dbm'])} dBm, "
          f"rate {f(stats['tx_rate_mbps'])} Mbps")


def window_line(w):
    status = LABELS.get(w["status"], w["status"])
    return (f"{hms(w['start_ts'])}  n={w['n']:>2}  "
            f"router p95 {f(w['local_p95_ms']):>4} ms loss {f(w['local_loss_pct']):>3}%  "
            f"ext p95 {f(w['external_p95_ms']):>4} ms  "
            f"rssi {f(w['rssi_dbm_median']):>4}  "
            f"-> {LABELS.get(w['state'], w['state']):<26} [{status}]")


def print_alerts(alerts, mode):
    print(f"\nAlerts ({mode}): {len(alerts)}")
    for a in alerts:
        print(f"  {hms(a['alert_ts'])} -> {hms(a['end_ts']) if a['end_ts'] else 'active'}  "
              f"{a['explanation']}")


def cmd_baseline(args):
    conn = storage.connect(args.db)
    bid, stats = save_baseline(conn, args.run_id, args.name)
    print_baseline(stats, f"#{bid} from run {args.run_id}, {stats['n_windows']} windows")


def cmd_analyze(args):
    conn = storage.connect(args.db)
    modes = ("multi_signal", "latency_only") if args.mode == "both" else (args.mode,)
    res = analyze_run(conn, args.run_id, args.baseline, modes=modes, save=not args.no_save)
    if res["error"]:
        sys.exit(res["error"])
    print(f"Run {args.run_id} [{res['run']['label']}]")
    print_baseline(res["baseline"], res["baseline_source"])
    for mode in modes:
        print(f"\nWindows ({mode}):")
        for w in res["windows"][mode]:
            print("  " + window_line(w))
            if args.verbose and w["reasons"]:
                print("      " + "; ".join(w["reasons"]))
        print_alerts(res["alerts"][mode], mode)


def cmd_watch(args):
    conn = storage.connect(args.db)
    run_id = args.run
    if run_id is None:
        run = storage.latest_open_run(conn)
        if run is None:
            sys.exit("No run in progress. Start one with: python -m collector run")
        run_id = run["run_id"]
    print(f"Watching run {run_id}. New 30-second windows appear as they complete. Ctrl+C to stop.")
    shown, last_err, baseline_shown = 0, None, False
    try:
        while True:
            res = analyze_run(conn, run_id, args.baseline, live=True)
            if res["error"]:
                if res["error"] != last_err:
                    print(f"  ({res['error']})")
                    last_err = res["error"]
            else:
                if not baseline_shown:
                    print_baseline(res["baseline"], res["baseline_source"])
                    baseline_shown = True
                ws = res["windows"]["multi_signal"]
                for w in ws[shown:]:
                    print(window_line(w))
                    if w["reasons"] and w["state"] != "healthy":
                        print("    " + "; ".join(w["reasons"]))
                shown = len(ws)
            run = conn.execute("SELECT ended_at FROM runs WHERE run_id = ?", (run_id,)).fetchone()
            if run["ended_at"]:
                print("Run ended.")
                break
            time.sleep(args.poll)
    except KeyboardInterrupt:
        pass


def main():
    ap = argparse.ArgumentParser(prog="detector", description="Wi-Fi congestion detector")
    ap.add_argument("--db", default=str(storage.DEFAULT_DB))
    sub = ap.add_subparsers(dest="cmd", required=True)

    b = sub.add_parser("baseline", help="learn a baseline from a quiet run")
    b.add_argument("run_id", type=int)
    b.add_argument("--name", help="test setup name, e.g. desk_ch149")
    b.set_defaults(func=cmd_baseline)

    a = sub.add_parser("analyze", help="windows and alerts for a recorded run")
    a.add_argument("run_id", type=int)
    a.add_argument("--baseline", type=int, help="baseline id (default: latest)")
    a.add_argument("--mode", choices=["both", "multi_signal", "latency_only"], default="multi_signal")
    a.add_argument("--no-save", action="store_true", help="don't write alerts to the database")
    a.add_argument("-v", "--verbose", action="store_true", help="show reasons for every window")
    a.set_defaults(func=cmd_analyze)

    w = sub.add_parser("watch", help="live detection on the running run")
    w.add_argument("--run", type=int, help="run id (default: latest running run)")
    w.add_argument("--baseline", type=int, help="baseline id (default: latest)")
    w.add_argument("--poll", type=float, default=5.0, help="seconds between checks")
    w.set_defaults(func=cmd_watch)

    args = ap.parse_args()
    args.func(args)


if __name__ == "__main__":
    main()
