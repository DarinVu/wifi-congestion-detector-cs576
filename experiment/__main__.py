"""Controlled test runner (slide 11 test design).

Six conditions x 3 runs x 5 minutes, in randomized order, with one run per
condition held out for final evaluation.

    .venv/bin/python -m experiment plan                 # make the randomized schedule
    .venv/bin/python -m experiment baseline --name desk # 2.5 min quiet baseline for this setup
    .venv/bin/python -m experiment next                 # guided: set up, record, tear down
    .venv/bin/python -m experiment status               # progress
"""

import argparse
import csv
import random
import subprocess
import sys
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
from collector import storage  # noqa: E402
from detector.engine import save_baseline  # noqa: E402

CONDITIONS = {
    "quiet": {
        "purpose": "Measure normal variation.",
        "setup": [
            "Laptop at its usual spot near the router.",
            "Stop streaming/downloads on other devices you control.",
        ],
        "teardown": [],
    },
    "load_1": {
        "purpose": "Shared wireless load: 1 active traffic client.",
        "setup": [
            "1 other device on the SAME Wi-Fi starts heavy traffic and keeps it up for the whole run:",
            "  - with a LAN iperf3 server: iperf3 -c <server> -t 330 -P 4      (or add -R for download)",
            "  - otherwise: play a 4K video, or start a large download (e.g. an OS update / big file).",
            "Laptop stays at its usual spot.",
        ],
        "teardown": ["Stop the traffic on the load device."],
    },
    "load_2": {
        "purpose": "Shared wireless load: 2 active traffic clients.",
        "setup": ["2 other devices on the same Wi-Fi start heavy traffic (as in load_1)."],
        "teardown": ["Stop the traffic on both load devices."],
    },
    "load_4": {
        "purpose": "Shared wireless load: 4 active traffic clients.",
        "setup": ["4 other devices on the same Wi-Fi start heavy traffic (as in load_1)."],
        "teardown": ["Stop the traffic on all load devices."],
    },
    "weak_signal": {
        "purpose": "Check for false congestion alerts when the signal is weak.",
        "setup": [
            "No extra load on the network.",
            "Move the laptop far from the router (other room / behind walls) until",
            "  signal is about -70 dBm or worse. Check with: .venv/bin/python hardware_check.py",
            "Write the location in --notes so it is repeatable.",
        ],
        "teardown": ["Move the laptop back to its usual spot."],
    },
    "wan_only": {
        "purpose": "Check local vs. external separation (slowdown beyond the Wi-Fi).",
        "setup": [
            "No extra load. Laptop at its usual spot.",
            "Turn on the WAN shaper (adds delay + bandwidth limit to the external target only):",
            "  sudo experiment/wan_shaper.sh on",
        ],
        "teardown": ["Turn the shaper off:  sudo experiment/wan_shaper.sh off"],
    },
}
FIELDS = ["order", "condition", "repeat", "split", "status", "run_id", "notes"]


def schedule_path(db):
    return Path(db).parent / "schedule.csv"


def read_schedule(db):
    p = schedule_path(db)
    if not p.exists():
        sys.exit("No schedule yet. Run: .venv/bin/python -m experiment plan")
    with open(p, newline="") as f:
        return list(csv.DictReader(f))


def write_schedule(db, rows):
    with open(schedule_path(db), "w", newline="") as f:
        w = csv.DictWriter(f, fieldnames=FIELDS)
        w.writeheader()
        w.writerows(rows)


def run_collector(args, label, seconds, notes):
    """Run the collector as a subprocess (shows its live output). Returns run_id or None."""
    cmd = [sys.executable, "-m", "collector", "--db", args.db, "run",
           "--label", label, "--duration", str(seconds)]
    if notes:
        cmd += ["--notes", notes]
    if args.lan_server:
        cmd += ["--lan-server", args.lan_server]
    if args.iperf_server:
        cmd += ["--iperf-server", args.iperf_server]
    if args.quiet:
        cmd.append("--quiet")
    conn = storage.connect(args.db)
    before = conn.execute("SELECT COALESCE(MAX(run_id), 0) FROM runs").fetchone()[0]
    proc = subprocess.Popen(cmd, cwd=ROOT)
    try:
        proc.wait()
    except KeyboardInterrupt:
        proc.wait()  # collector closes its run on Ctrl+C
        return None
    row = conn.execute("SELECT run_id FROM runs WHERE run_id > ? ORDER BY run_id DESC LIMIT 1",
                       (before,)).fetchone()
    return row["run_id"] if proc.returncode == 0 and row else None


def pause(msg, assume_yes):
    print(msg)
    if not assume_yes:
        input("Press Enter when ready (Ctrl+C to cancel)... ")


# ------------------------------------------------------------------ commands

def cmd_plan(args):
    p = schedule_path(args.db)
    if p.exists() and not args.force:
        sys.exit(f"{p} already exists. Use --force to replace it (completed runs stay in the database).")
    rng = random.Random(args.seed)
    rows = []
    for cond in CONDITIONS:
        held_out = rng.randrange(args.runs)     # one whole run per condition held out
        for r in range(args.runs):
            rows.append({"condition": cond, "repeat": r + 1,
                         "split": "test" if r == held_out else "dev",
                         "status": "pending", "run_id": "", "notes": ""})
    rng.shuffle(rows)
    for i, r in enumerate(rows, 1):
        r["order"] = i
    write_schedule(args.db, rows)
    print(f"Wrote {len(rows)} runs ({len(CONDITIONS)} conditions x {args.runs}) to {p}")
    print(f"Total recording time: about {len(rows) * args.minutes} minutes plus setup.")
    cmd_status(args)


def cmd_status(args):
    rows = read_schedule(args.db)
    done = sum(r["status"] == "done" for r in rows)
    print(f"\n{done}/{len(rows)} runs done\n")
    print(f"{'#':>3}  {'condition':<12} {'rep':>3}  {'split':<5} {'status':<8} run_id")
    for r in rows:
        print(f"{r['order']:>3}  {r['condition']:<12} {r['repeat']:>3}  {r['split']:<5} "
              f"{r['status']:<8} {r['run_id']}")


def cmd_baseline(args):
    pause(f"\nBASELINE for setup '{args.name}': quiet network, laptop at its usual spot.\n"
          f"Recording {args.seconds} s.", args.yes)
    run_id = run_collector(args, "baseline", args.seconds, f"baseline for {args.name}")
    if run_id is None:
        sys.exit("Baseline recording cancelled.")
    conn = storage.connect(args.db)
    bid, stats = save_baseline(conn, run_id, args.name)
    print(f"\nSaved baseline #{bid} from run {run_id}: router p95 {stats['local_p95_ms']} ms, "
          f"external p95 {stats['external_p95_ms']} ms, signal {stats['rssi_dbm']} dBm")


def cmd_next(args):
    conn = storage.connect(args.db)
    if conn.execute("SELECT COUNT(*) FROM baselines").fetchone()[0] == 0:
        print("Note: no baseline saved yet. Run `python -m experiment baseline --name <setup>` first.")
    rows = read_schedule(args.db)
    todo = [r for r in rows if r["status"] != "done"]
    if not todo:
        print("All scheduled runs are done.")
        return
    row = todo[0]
    cond = CONDITIONS[row["condition"]]
    seconds = args.seconds or args.minutes * 60

    print(f"\n=== Run {row['order']}/{len(rows)}: {row['condition']} "
          f"(repeat {row['repeat']}, {row['split']}) ===")
    print(f"Purpose: {cond['purpose']}\nSetup:")
    for line in cond["setup"]:
        print(f"  {line}")
    pause(f"Will record {seconds} s once the condition is in place.", args.yes)

    notes = f"schedule #{row['order']} {row['condition']} rep {row['repeat']} split={row['split']}"
    if args.notes:
        notes += f"; {args.notes}"
    run_id = run_collector(args, row["condition"], seconds, notes)

    if cond["teardown"]:
        print("\nTeardown:")
        for line in cond["teardown"]:
            print(f"  {line}")

    if run_id is None:
        print("\nRun cancelled or failed; it stays pending in the schedule.")
        return
    run = conn.execute("SELECT * FROM runs WHERE run_id = ?", (run_id,)).fetchone()
    storage.insert_event(conn, run_id, f"condition start: {row['condition']}", run["started_at"])
    storage.insert_event(conn, run_id, f"condition stop: {row['condition']}", run["ended_at"])
    row["status"], row["run_id"], row["notes"] = "done", run_id, args.notes or ""
    write_schedule(args.db, rows)
    left = len(todo) - 1
    print(f"\nSaved as run {run_id}. {left} run(s) left."
          + (" Next: .venv/bin/python -m experiment next" if left else " All done!"))


def main():
    ap = argparse.ArgumentParser(prog="experiment", description="Controlled test runner")
    ap.add_argument("--db", default=str(storage.DEFAULT_DB))
    sub = ap.add_subparsers(dest="cmd", required=True)

    def collector_opts(p):
        p.add_argument("--lan-server", help="wired LAN server to ping")
        p.add_argument("--iperf-server", help="run scheduled iperf3 tests against this server")
        p.add_argument("--quiet", action="store_true", help="hide per-second output")
        p.add_argument("-y", "--yes", action="store_true", help="don't wait for Enter")

    p = sub.add_parser("plan", help="create the randomized schedule")
    p.add_argument("--runs", type=int, default=3, help="runs per condition")
    p.add_argument("--minutes", type=int, default=5, help="minutes per run (for the time estimate)")
    p.add_argument("--seed", type=int, help="random seed (for a reproducible order)")
    p.add_argument("--force", action="store_true", help="replace an existing schedule")
    p.set_defaults(func=cmd_plan)

    sub.add_parser("status", help="show schedule progress").set_defaults(func=cmd_status)

    b = sub.add_parser("baseline", help="record a quiet baseline and save it")
    b.add_argument("--name", required=True, help="test setup name, e.g. desk_ch149")
    b.add_argument("--seconds", type=int, default=150)
    collector_opts(b)
    b.set_defaults(func=cmd_baseline)

    n = sub.add_parser("next", help="run the next scheduled condition")
    n.add_argument("--minutes", type=int, default=5)
    n.add_argument("--seconds", type=int, help="override run length (for quick tests)")
    n.add_argument("--notes", help="extra notes, e.g. laptop location")
    collector_opts(n)
    n.set_defaults(func=cmd_next)

    args = ap.parse_args()
    args.func(args)


if __name__ == "__main__":
    main()
