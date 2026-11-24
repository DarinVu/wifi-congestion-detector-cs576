"""Evaluate the detectors on the controlled test runs (slide 12).

    .venv/bin/python -m evaluation                # default thresholds
    .venv/bin/python -m evaluation --tune         # tune on dev runs, report on held-out test runs

Writes results/summary.md, results/metrics.json and PNG figures to results/.
"""

import argparse
import json
import sys
from dataclasses import asdict
from datetime import datetime
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
from collector import storage  # noqa: E402
from detector.rules import LABELS, Config  # noqa: E402

from .metrics import (CONDITION_ORDER, MODES, POSITIVE, data_quality, load_runs,  # noqa: E402
                      prepare, replay, score, tune, verdict_matrix)
from . import plots  # noqa: E402

MODE_NAME = {"multi_signal": "Multi-signal (proposed)", "latency_only": "Latency-only (baseline)"}


def pct(v):
    return "—" if v is None else f"{100 * v:.0f}%"


def num(v, nd=1):
    return "—" if v is None else f"{v:.{nd}f}"


def score_table(scores):
    lines = ["| Detector | Split | Precision | Recall | F1 | False alerts/h (negative runs) "
             "| Median delay | Max delay | Positive runs missed |",
             "|---|---|---|---|---|---|---|---|---|"]
    for s in scores:
        lines.append(f"| {MODE_NAME[s['mode']]} | {s['split']} | {pct(s['precision'])} | "
                     f"{pct(s['recall'])} | {num(s['f1'], 2)} | {num(s['false_alerts_per_hour'])} | "
                     f"{num(s['delay_s_median'], 0)} s | {num(s['delay_s_max'], 0)} s | "
                     f"{s['positive_runs_missed']} |")
    return "\n".join(lines)


def matrix_table(results, mode, split):
    statuses, m = verdict_matrix(results, mode, split)
    head = "| Condition | " + " | ".join(LABELS.get(s, s.title()) for s in statuses) + " |"
    lines = [head, "|---|" + "---|" * len(statuses)]
    for c in CONDITION_ORDER:
        if sum(m[c].values()):
            lines.append(f"| {c} | " + " | ".join(str(m[c][s]) for s in statuses) + " |")
    return "\n".join(lines)


def main():
    ap = argparse.ArgumentParser(prog="evaluation", description=__doc__.splitlines()[0])
    ap.add_argument("--db", default=str(storage.DEFAULT_DB))
    ap.add_argument("--out", default=str(ROOT / "results"))
    ap.add_argument("--tune", action="store_true",
                    help="grid-search thresholds on dev runs before scoring")
    args = ap.parse_args()

    conn = storage.connect(args.db)
    runs = load_runs(conn, args.db)
    if not runs:
        sys.exit("No completed test runs (labels: " + ", ".join(CONDITION_ORDER) + ").")
    prepared = prepare(conn, runs)
    out = Path(args.out)
    out.mkdir(parents=True, exist_ok=True)

    has_test = any(r["split"] == "test" for r in prepared)
    report_split = "test" if has_test else None

    cfgs = {m: Config() for m in MODES}
    tuning = {}
    if args.tune:
        if not any(r["split"] == "dev" for r in prepared):
            sys.exit("--tune needs dev runs.")
        for m in MODES:
            cfgs[m], dev_score = tune(prepared, m)
            tuning[m] = {"config": asdict(cfgs[m]), "dev_score": dev_score}

    results = {m: replay(prepared, cfgs[m], m) for m in MODES}
    scores = []
    for m in MODES:
        for split in (["dev", "test"] if has_test else [None]):
            scores.append(score(results[m], m, split))
    quality = data_quality(prepared)

    # ---- figures
    plots.comparison(scores, report_split or "all", out / "comparison.png")
    plots.rtt_by_condition(prepared, out / "rtt_by_condition.png")
    plots.confusion(*verdict_matrix(results["multi_signal"], "multi_signal", report_split),
                    "Multi-signal detector", out / "confusion_multi_signal.png")
    plots.confusion(*verdict_matrix(results["latency_only"], "latency_only", report_split),
                    "Latency-only detector", out / "confusion_latency_only.png")
    plots.timeseries(conn, results["multi_signal"], out / "timeseries.png")

    # ---- report
    n_by = {c: sum(r["condition"] == c for r in prepared) for c in CONDITION_ORDER}
    split_note = ("Scores on **held-out test runs** (one whole run per condition); dev scores shown for reference."
                  if has_test else "No dev/test split found (no schedule.csv): all runs scored together.")
    md = [
        "# Wi-Fi Congestion Detector: evaluation",
        f"_Generated {datetime.now():%Y-%m-%d %H:%M} from `{args.db}`_",
        "",
        f"Runs: {len(prepared)} ("
        + ", ".join(f"{c} {n}" for c, n in n_by.items() if n) + "). "
        f"Positive (congestion) runs: {', '.join(sorted(POSITIVE))}. "
        "Labels are operational (the controlled test condition), not proof of congestion.",
        "",
        split_note,
        "",
        "## Detector comparison",
        score_table(scores),
        "",
        "Precision and recall are counted over 30-second windows. Recall is capped below 100% "
        "because an alert needs 3 degraded windows before it fires; detection delay is measured "
        "from the start of the loaded run to the first congestion alert.",
        "",
        "![comparison](comparison.png)",
        "",
        "## What each detector reported, by condition (windows)",
        "### Multi-signal",
        matrix_table(results["multi_signal"], "multi_signal", report_split),
        "",
        "![confusion multi](confusion_multi_signal.png)",
        "",
        "### Latency-only",
        matrix_table(results["latency_only"], "latency_only", report_split),
        "",
        "![confusion latency](confusion_latency_only.png)",
        "",
        "## RTT by condition",
        "![rtt by condition](rtt_by_condition.png)",
        "",
        "## Example runs",
        "![timeseries](timeseries.png)",
        "",
        "## Data quality and overhead",
        f"- Missing-sample rate: {pct(quality['missing_sample_rate'])}",
        f"- Windows with insufficient data: {quality['windows_insufficient']} of {quality['windows_total']}",
        f"- Probe overhead: about {quality['probe_overhead_bps'] / 1000:.1f} kbit/s "
        "(two ICMP pings per second; iperf3 tests excluded)",
        "",
        "## Thresholds used",
    ]
    for m in MODES:
        c = cfgs[m]
        md.append(f"- **{MODE_NAME[m]}**: RTT > max({c.rtt_factor} × baseline, baseline + "
                  f"{c.rtt_margin_ms:.0f} ms), alert after {c.persist_windows} windows, clear after "
                  f"{c.clear_windows}" + (" _(tuned on dev runs)_" if args.tune else " _(defaults)_"))
    (out / "summary.md").write_text("\n".join(md) + "\n")
    (out / "metrics.json").write_text(json.dumps(
        {"scores": scores, "data_quality": quality, "tuning": tuning,
         "configs": {m: asdict(cfgs[m]) for m in MODES},
         "runs": [{k: r[k] for k in ("run_id", "condition", "split", "baseline_id")} for r in prepared]},
        indent=2))

    # ---- console
    print(f"Evaluated {len(prepared)} runs. " + split_note.replace("**", ""))
    for s in scores:
        print(f"  {MODE_NAME[s['mode']]:<26} {s['split']:<4}  precision {pct(s['precision']):>4}  "
              f"recall {pct(s['recall']):>4}  F1 {num(s['f1'], 2):>4}  "
              f"false alerts/h {num(s['false_alerts_per_hour']):>4}  "
              f"median delay {num(s['delay_s_median'], 0):>3} s")
    if args.tune:
        for m in MODES:
            c = cfgs[m]
            print(f"  tuned {m}: rtt_factor={c.rtt_factor}, rtt_margin_ms={c.rtt_margin_ms}, "
                  f"persist_windows={c.persist_windows}")
    print(f"Report: {out / 'summary.md'}")


if __name__ == "__main__":
    main()
