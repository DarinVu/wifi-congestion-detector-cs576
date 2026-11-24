"""Report figures (matplotlib, light theme for the written report)."""

import matplotlib

matplotlib.use("Agg")
import matplotlib.pyplot as plt  # noqa: E402
from matplotlib.colors import LinearSegmentedColormap  # noqa: E402

from detector.rules import LABELS  # noqa: E402

from .metrics import CONDITION_ORDER  # noqa: E402

# Validated reference palette (dataviz skill): categorical slots 1-2, text, sequential blue.
BLUE, ORANGE = "#2a78d6", "#eb6834"
TEXT, TEXT2, GRID = "#0b0b0b", "#52514e", "#e4e3df"
ALERT_FILL = "#ec835a"
SEQ = LinearSegmentedColormap.from_list(
    "seq_blue", ["#f7f9fc", "#cde2fb", "#9ec5f4", "#6da7ec", "#3987e5", "#256abf", "#184f95", "#0d366b"])
MODE_COLOR = {"multi_signal": BLUE, "latency_only": ORANGE}
MODE_NAME = {"multi_signal": "Multi-signal", "latency_only": "Latency-only"}

plt.rcParams.update({
    "font.size": 10, "axes.edgecolor": GRID, "axes.labelcolor": TEXT2, "axes.titlecolor": TEXT,
    "xtick.color": TEXT2, "ytick.color": TEXT2, "axes.spines.top": False,
    "axes.spines.right": False, "axes.grid": True, "grid.color": GRID, "grid.linewidth": 0.8,
    "axes.axisbelow": True, "figure.dpi": 150, "savefig.bbox": "tight",
})


def comparison(scores, split, path):
    """Grouped bars: precision / recall / F1 for both detectors on one split."""
    rows = [s for s in scores if s["split"] == split]
    metrics = ["precision", "recall", "f1"]
    fig, ax = plt.subplots(figsize=(6.5, 3.4))
    width = 0.36
    for i, s in enumerate(rows):
        xs = [k + (i - 0.5) * (width + 0.03) for k in range(len(metrics))]
        vals = [s[m] or 0 for m in metrics]
        bars = ax.bar(xs, vals, width, color=MODE_COLOR[s["mode"]], label=MODE_NAME[s["mode"]])
        for b, v in zip(bars, vals):
            ax.text(b.get_x() + b.get_width() / 2, v + 0.02, f"{v:.2f}", ha="center",
                    va="bottom", color=TEXT, fontsize=9)
    ax.set_xticks(range(len(metrics)), ["Precision", "Recall", "F1"])
    ax.set_ylim(0, 1.12)
    ax.set_ylabel("Score")
    ax.grid(axis="x", visible=False)
    ax.set_title(f"Congestion detection, {split} runs (30 s windows)", loc="left")
    ax.legend(frameon=False, loc="upper left", bbox_to_anchor=(1, 1))
    fig.savefig(path)
    plt.close(fig)
    return path


def rtt_by_condition(prepared, path):
    """Box plot of window router p95 RTT for each condition."""
    data, labels = [], []
    for c in CONDITION_ORDER:
        vals = [w["local_p95_ms"] for r in prepared if r["condition"] == c
                for w in r["windows"] if w["sufficient"] and w["local_p95_ms"] is not None]
        if vals:
            data.append(vals)
            labels.append(c)
    fig, ax = plt.subplots(figsize=(6.5, 3.4))
    if data:
        ax.boxplot(data, tick_labels=labels, widths=0.5, patch_artist=True,
                   boxprops={"facecolor": "#cde2fb", "edgecolor": BLUE},
                   medianprops={"color": BLUE, "linewidth": 2},
                   whiskerprops={"color": BLUE}, capprops={"color": BLUE},
                   flierprops={"marker": "o", "markersize": 4, "markerfacecolor": BLUE,
                               "markeredgecolor": "white"})
    ax.set_ylabel("Router p95 RTT per window (ms)")
    ax.grid(axis="x", visible=False)
    ax.set_title("Local-path latency by test condition", loc="left")
    fig.savefig(path)
    plt.close(fig)
    return path


def confusion(statuses, matrix, title, path):
    """Heatmap: condition (rows) x displayed status (columns), counts in windows."""
    conds = [c for c in CONDITION_ORDER if sum(matrix[c].values())]
    grid = [[matrix[c][s] for s in statuses] for c in conds]
    fig, ax = plt.subplots(figsize=(1.3 * len(statuses) + 1.6, 0.5 * len(conds) + 1.3))
    if conds:
        vmax = max(max(row) for row in grid) or 1
        ax.imshow(grid, cmap=SEQ, vmin=0, vmax=vmax, aspect="auto")
        for i, row in enumerate(grid):
            for j, v in enumerate(row):
                ax.text(j, i, str(v), ha="center", va="center", fontsize=9,
                        color="white" if v > 0.55 * vmax else TEXT)
    ax.set_xticks(range(len(statuses)),
                  [LABELS.get(s, s.title()).replace(", ", ",\n").replace(" / ", " /\n") for s in statuses],
                  fontsize=8)
    ax.set_yticks(range(len(conds)), conds)
    ax.grid(False)
    for s in ax.spines.values():
        s.set_visible(False)
    ax.set_title(f"{title}: status per window", loc="left")
    fig.savefig(path)
    plt.close(fig)
    return path


def timeseries(conn, results, path):
    """One example run per condition (held-out run when available)."""
    picks = []
    for c in CONDITION_ORDER:
        rs = [r for r in results if r["condition"] == c]
        rs.sort(key=lambda r: r["split"] != "test")
        if rs:
            picks.append(rs[0])
    if not picks:
        return None
    cols = 2
    rows = (len(picks) + 1) // cols
    fig, axes = plt.subplots(rows, cols, figsize=(11, 2.4 * rows), sharey=True, squeeze=False)
    all_rtts = []
    for ax, r in zip(axes.flat, picks):
        t0 = r["started_at"]
        s = conn.execute("SELECT ts, local_rtt_ms, external_rtt_ms FROM samples "
                         "WHERE run_id = ? ORDER BY ts", (r["run_id"],)).fetchall()
        x = [(row["ts"] - t0) / 60 for row in s]
        for a in r["alerts"]:
            a0, a1 = (a["start_ts"] - t0) / 60, ((a["end_ts"] or r["ended_at"]) - t0) / 60
            ax.axvspan(a0, a1, color=ALERT_FILL, alpha=0.15, lw=0)
            ax.text(a0 + 0.05, 0.95, LABELS[a["kind"]], transform=ax.get_xaxis_transform(),
                    va="top", fontsize=7.5, color=TEXT2)
        ax.plot(x, [row["local_rtt_ms"] for row in s], color=BLUE, lw=1.2, label="Router")
        ax.plot(x, [row["external_rtt_ms"] for row in s], color=ORANGE, lw=1.2, label="External")
        all_rtts += [v for row in s for v in (row["local_rtt_ms"], row["external_rtt_ms"]) if v is not None]
        ax.set_title(f"{r['condition']} (run {r['run_id']}, {r['split']})", loc="left", fontsize=10)
        ax.set_xlabel("Minutes into run")
    for ax in axes.flat[len(picks):]:
        ax.set_visible(False)
    if all_rtts:
        # Shared scale, capped at the 99th percentile so rare spikes don't flatten every panel.
        all_rtts.sort()
        axes.flat[0].set_ylim(0, all_rtts[int(0.99 * (len(all_rtts) - 1))] * 1.15)
        fig.text(0.99, 0.005, "Y axis capped at the 99th percentile; rarer spikes are clipped.",
                 ha="right", fontsize=7.5, color=TEXT2)
    for ax in axes[:, 0]:
        ax.set_ylabel("RTT (ms)")
    axes.flat[0].legend(frameon=False, loc="upper right", fontsize=8)
    fig.suptitle("Example runs with multi-signal alerts shaded", x=0.01, ha="left", color=TEXT)
    fig.tight_layout()
    fig.savefig(path)
    plt.close(fig)
    return path
