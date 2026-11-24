"""Live dashboard for the Wi-Fi Congestion Detector.

Run:
    .venv/bin/streamlit run dashboard/app.py
    .venv/bin/streamlit run dashboard/app.py -- --db path/to/other.db
"""

import argparse
import sys
import time
from datetime import datetime
from pathlib import Path

import altair as alt
import pandas as pd
import streamlit as st

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
from collector import storage  # noqa: E402
from detector.engine import analyze_run  # noqa: E402
from detector.rules import LABELS, Config  # noqa: E402

# Validated categorical slots 1-2 (dataviz reference palette) + status colors.
ROUTER_COLOR, EXTERNAL_COLOR, LAN_COLOR = "#2a78d6", "#eb6834", "#1baf7a"
ALERT_FILL = "#ec835a"   # status "serious"
WATCH_FILL = "#fab219"   # status "warning"

ALERT_ICON = {"congestion": "🔴", "weak_signal": "📶", "interference": "⚡", "wan_issue": "🌐",
              "degraded_uncertain": "⚠️", "latency": "🔴"}

st.set_page_config(page_title="Wi-Fi Congestion Detector", page_icon="📡", layout="wide")


def db_path():
    ap = argparse.ArgumentParser()
    ap.add_argument("--db", default=str(storage.DEFAULT_DB))
    args, _ = ap.parse_known_args(sys.argv[1:])
    return args.db


@st.cache_resource
def get_conn(path):
    return storage.connect(path)


conn = get_conn(db_path())


def hms(ts):
    return "" if ts is None or pd.isna(ts) else datetime.fromtimestamp(ts).strftime("%H:%M:%S")


def dur(seconds):
    m, s = divmod(int(seconds), 60)
    return f"{m}m {s:02d}s" if m else f"{s}s"


def fmt(v, unit, nd=0):
    return "—" if v is None or pd.isna(v) else f"{v:.{nd}f} {unit}"


# ------------------------------------------------------------------ sidebar

runs = conn.execute("SELECT * FROM runs ORDER BY run_id DESC").fetchall()
st.sidebar.title("📡 Wi-Fi Congestion Detector")
if not runs:
    st.title("Wi-Fi Congestion Detector")
    st.info("No runs yet. Start the collector in a terminal:\n\n"
            "`.venv/bin/python -m collector run --label live`\n\nThis page updates on its own.")
    time.sleep(5)
    st.rerun()

run_labels = {r["run_id"]: f"#{r['run_id']} {r['label']}"
              + (" (running)" if r["ended_at"] is None else
                 f" · {datetime.fromtimestamp(r['started_at']):%b %d %H:%M}")
              for r in runs}
run_id = st.sidebar.selectbox("Run", list(run_labels), format_func=run_labels.get)

baselines = conn.execute("SELECT baseline_id, name, run_id FROM baselines "
                         "ORDER BY baseline_id DESC").fetchall()
base_opts = {None: "Latest stored (or run's first 2 min)"}
base_opts.update({b["baseline_id"]: f"#{b['baseline_id']} {b['name'] or ''} (run {b['run_id']})"
                  for b in baselines})
baseline_id = st.sidebar.selectbox("Baseline", list(base_opts), format_func=base_opts.get)

mode = st.sidebar.radio("Detector", ["multi_signal", "latency_only"],
                        format_func={"multi_signal": "Multi-signal (proposed)",
                                     "latency_only": "Latency-only (comparison)"}.get)
span_label = st.sidebar.radio("Time range", ["Last 10 min", "Last 30 min", "Whole run"])
refresh = st.sidebar.select_slider("Refresh every", [2, 5, 10, 30], value=5,
                                   format_func=lambda s: f"{s}s")
st.sidebar.caption(f"Database: `{db_path()}`")


# ------------------------------------------------------------------ live section

@st.fragment(run_every=refresh)
def live_view():
    run = conn.execute("SELECT * FROM runs WHERE run_id = ?", (run_id,)).fetchone()
    is_live = run["ended_at"] is None
    res = analyze_run(conn, run_id, baseline_id, live=is_live, save=False,
                      modes=("multi_signal", "latency_only"))

    samples = pd.read_sql_query("SELECT * FROM samples WHERE run_id = ? ORDER BY ts",
                                conn, params=(run_id,))
    events = pd.read_sql_query("SELECT * FROM events WHERE run_id = ? ORDER BY ts",
                               conn, params=(run_id,))
    iperf = pd.read_sql_query("SELECT * FROM iperf_tests WHERE run_id = ? ORDER BY ts",
                              conn, params=(run_id,))

    # ---- header + status
    started = datetime.fromtimestamp(run["started_at"])
    elapsed = (run["ended_at"] or time.time()) - run["started_at"]
    st.title(f"Run #{run_id} · {run['label']}")
    st.caption(f"{'🟢 Live' if is_live else '⏹ Ended'} · started {started:%b %d %H:%M:%S} · "
               f"{dur(elapsed)} · {len(samples)} samples"
               + (f" · {run['notes']}" if run["notes"] else ""))

    if res["error"]:
        st.info(f"⏳ Learning a baseline: {res['error']}. "
                "The first status appears after about 1 minute of data.")
        return

    windows = res["windows"][mode]
    alerts = res["alerts"][mode]
    base = res["baseline"]
    cfg = Config()
    last = windows[-1] if windows else None
    status = last["status"] if last else "healthy"
    active = next((a for a in reversed(alerts) if a["end_ts"] is None), None)

    if active:
        st.error(f"### {ALERT_ICON.get(active['kind'], '⚠️')} {LABELS[active['kind']]}\n"
                 f"Since {hms(active['alert_ts'])}. "
                 + active["explanation"].split(": ", 1)[-1], icon=None)
    elif status.startswith("watching"):
        st.warning(f"### ⚠️ Watching: degraded window {status.split('(')[1].rstrip(')')}\n"
                   + "; ".join(last["reasons"]))
    elif last is None:
        st.info("### ⏳ Collecting the first 30-second window")
    elif last["state"] == "insufficient":
        st.warning("### ⏳ Insufficient data in the latest window")
    else:
        st.success("### ✅ Healthy\nLatest 30-second window is within baseline limits.")

    # ---- metric tiles (latest complete window vs baseline)
    def delta(v, b, unit):
        if v is None or b is None or abs(v - b) < 0.5:
            return None
        return f"{v - b:+.0f} {unit} vs baseline"

    c = st.columns(5)
    if last:
        c[0].metric("Router p95 RTT", fmt(last["local_p95_ms"], "ms"),
                    delta(last["local_p95_ms"], base["local_p95_ms"], "ms"), delta_color="inverse")
        c[1].metric("External p95 RTT", fmt(last["external_p95_ms"], "ms"),
                    delta(last["external_p95_ms"], base["external_p95_ms"], "ms"), delta_color="inverse")
        c[2].metric("Router loss", fmt(last["local_loss_pct"], "%"),
                    delta(last["local_loss_pct"], base["local_loss_pct"], "pts"), delta_color="inverse")
        c[3].metric("Signal (RSSI)", fmt(last["rssi_dbm_median"], "dBm"),
                    delta(last["rssi_dbm_median"], base["rssi_dbm"], "dB"))
        c[4].metric("Link rate", fmt(last["tx_rate_mbps_median"], "Mbps"),
                    delta(last["tx_rate_mbps_median"], base["tx_rate_mbps"], "Mbps"))
        st.caption(f"Latest complete window {hms(last['start_ts'])}–{hms(last['end_ts'])}. "
                   f"Baseline: {res['baseline_source']}.")

    # ---- time range
    if samples.empty:
        return
    t_end = samples["ts"].max()
    span = {"Last 10 min": 600, "Last 30 min": 1800, "Whole run": None}[span_label]
    t0 = max(samples["ts"].min(), t_end - span) if span else samples["ts"].min()
    s = samples[samples["ts"] >= t0].copy()
    s["time"] = pd.to_datetime(s["ts"], unit="s", utc=True).dt.tz_convert(
        datetime.now().astimezone().tzinfo).dt.tz_localize(None)

    def to_dt(ts):
        return pd.to_datetime(ts, unit="s", utc=True).tz_convert(
            datetime.now().astimezone().tzinfo).tz_localize(None)

    # Shaded spans: active alerts (serious) and "watching" windows (warning).
    spans = []
    for a in alerts:
        a0, a1 = a["start_ts"], a["end_ts"] or t_end
        if a1 >= t0:
            spans.append({"start": to_dt(max(a0, t0)), "end": to_dt(a1),
                          "what": LABELS[a["kind"]], "fill": "alert"})
    span_df = pd.DataFrame(spans)

    def span_layer():
        if span_df.empty:
            return None
        return alt.Chart(span_df).mark_rect(opacity=0.15, color=ALERT_FILL).encode(
            x="start:T", x2="end:T", tooltip=[alt.Tooltip("what:N", title="Alert")])

    def event_layer():
        ev = events[events["ts"] >= t0].copy()
        if ev.empty:
            return None
        ev["time"] = ev["ts"].map(to_dt)
        return alt.Chart(ev).mark_rule(strokeDash=[2, 3], strokeWidth=1.5, color="gray").encode(
            x="time:T", tooltip=[alt.Tooltip("text:N", title="Event"),
                                 alt.Tooltip("time:T", format="%H:%M:%S", title="At")])

    def compose(*layers):
        layers = [l for l in layers if l is not None]
        # No wheel-zoom: it would hijack page scrolling. Hover tooltips stay.
        return alt.layer(*layers).properties(height=260)

    # ---- RTT chart (all series in ms: one axis)
    st.subheader("Round-trip time")
    paths = [("local_rtt_ms", "Router", ROUTER_COLOR), ("external_rtt_ms", "External", EXTERNAL_COLOR)]
    if s["lan_sent"].notna().any():
        paths.append(("lan_rtt_ms", "LAN server", LAN_COLOR))
    rtt = s.melt(id_vars=["time", "iperf_active"], value_vars=[p[0] for p in paths],
                 var_name="path", value_name="rtt_ms")
    rtt["path"] = rtt["path"].map({p[0]: p[1] for p in paths})
    domain, colors = [p[1] for p in paths], [p[2] for p in paths]

    hover = alt.selection_point(fields=["time"], nearest=True, on="pointerover",
                                clear="pointerout", empty=False)
    base_line = alt.Chart(rtt).encode(
        x=alt.X("time:T", title=None, axis=alt.Axis(format="%H:%M:%S", grid=False)),
        y=alt.Y("rtt_ms:Q", title="RTT (ms)"),
        color=alt.Color("path:N", scale=alt.Scale(domain=domain, range=colors),
                        legend=alt.Legend(orient="top", title=None)))
    lines = base_line.mark_line(strokeWidth=2, interpolate="monotone")
    points = base_line.mark_point(size=60, filled=True, opacity=1).encode(
        opacity=alt.condition(hover, alt.value(1), alt.value(0)),
        tooltip=[alt.Tooltip("time:T", format="%H:%M:%S", title="Time"),
                 alt.Tooltip("path:N", title="Path"),
                 alt.Tooltip("rtt_ms:Q", format=".1f", title="RTT (ms)")]).add_params(hover)
    limit = _rtt_limit = max(cfg.rtt_factor * base["local_p95_ms"],
                             base["local_p95_ms"] + cfg.rtt_margin_ms)
    limit_rule = alt.Chart(pd.DataFrame({"y": [limit], "label": [f"router limit {limit:.0f} ms"]})) \
        .mark_rule(strokeDash=[6, 4], strokeWidth=1, color=ROUTER_COLOR, opacity=0.7) \
        .encode(y="y:Q", tooltip=[alt.Tooltip("label:N", title="Threshold")])
    lost = s[s["local_sent"].eq(1) & s["local_recv"].eq(0)]
    loss_layer = None
    if not lost.empty:
        loss_layer = alt.Chart(lost.assign(y=0)).mark_tick(
            color="#d03b3b", thickness=2, size=14).encode(
            x="time:T", y="y:Q",
            tooltip=[alt.Tooltip("time:T", format="%H:%M:%S", title="Lost router ping")])
    st.altair_chart(compose(span_layer(), lines, points, limit_rule, loss_layer, event_layer()),
                    use_container_width=True)
    st.caption("Shaded: alert periods. Dashed line: router p95 alert limit. "
               "Red ticks at 0: lost router pings. Dotted rules: marked events. Hover for values.")

    # ---- signal chart (dBm: its own axis/chart)
    left, right = st.columns(2)
    with left:
        st.subheader("Signal strength")
        sig = s.dropna(subset=["rssi_dbm"])
        sig_chart = alt.Chart(sig).mark_line(strokeWidth=2, color=ROUTER_COLOR).encode(
            x=alt.X("time:T", title=None, axis=alt.Axis(format="%H:%M:%S", grid=False)),
            y=alt.Y("rssi_dbm:Q", title="RSSI (dBm)", scale=alt.Scale(zero=False)),
            tooltip=[alt.Tooltip("time:T", format="%H:%M:%S", title="Time"),
                     alt.Tooltip("rssi_dbm:Q", title="RSSI (dBm)"),
                     alt.Tooltip("tx_rate_mbps:Q", title="Link rate (Mbps)")])
        weak = alt.Chart(pd.DataFrame({"y": [cfg.weak_rssi_dbm]})).mark_rule(
            strokeDash=[6, 4], strokeWidth=1, color="gray").encode(
            y="y:Q", tooltip=[alt.Tooltip("y:Q", title="Weak-signal floor (dBm)")])
        st.altair_chart(compose(span_layer(), sig_chart, weak, event_layer()),
                        use_container_width=True)
        st.caption(f"Dashed line: weak-signal floor ({cfg.weak_rssi_dbm:.0f} dBm).")

    with right:
        st.subheader("Throughput (iperf3)")
        ok = iperf.dropna(subset=["mbps"])
        if ok.empty:
            st.info("No throughput tests in this run. Start the collector with "
                    "`--iperf-server <IP>` to add scheduled iperf3 tests.")
        else:
            ok = ok.assign(time=ok["ts"].map(to_dt))
            tp = alt.Chart(ok).mark_line(strokeWidth=2, color=ROUTER_COLOR,
                                         point=alt.OverlayMarkDef(size=64, filled=True)).encode(
                x=alt.X("time:T", title=None, axis=alt.Axis(format="%H:%M:%S", grid=False)),
                y=alt.Y("mbps:Q", title="Throughput (Mbps)"),
                tooltip=[alt.Tooltip("time:T", format="%H:%M:%S", title="Time"),
                         alt.Tooltip("direction:N", title="Direction"),
                         alt.Tooltip("mbps:Q", format=".1f", title="Mbps"),
                         alt.Tooltip("retransmits:Q", title="Retransmits")])
            st.altair_chart(tp.properties(height=260), use_container_width=True)
        errs = iperf[iperf["error"].notna()]
        if not errs.empty:
            st.caption(f"{len(errs)} iperf3 test(s) failed; latest error: {errs['error'].iloc[-1]}")

    # ---- alert history
    st.subheader("Alert history")
    if not alerts:
        st.write("No alerts in this run.")
    else:
        st.dataframe(pd.DataFrame([{
            "": ALERT_ICON.get(a["kind"], "⚠️"),
            "Alert": LABELS[a["kind"]],
            "Degradation began": hms(a["start_ts"]),
            "Fired": hms(a["alert_ts"]),
            "Cleared": hms(a["end_ts"]) if a["end_ts"] else "active",
            "Duration": dur((a["end_ts"] or t_end) - a["alert_ts"]),
            "Evidence": a["explanation"].split(": ", 1)[-1],
        } for a in reversed(alerts)]), hide_index=True, use_container_width=True,
            column_config={"": st.column_config.TextColumn(width=40),
                           "Evidence": st.column_config.TextColumn(width="large")})

    other = "latency_only" if mode == "multi_signal" else "multi_signal"
    st.caption(f"The {other.replace('_', '-')} detector raised "
               f"{len(res['alerts'][other])} alert(s) on this run.")

    # ---- windows table
    with st.expander("30-second windows (table view)"):
        st.dataframe(pd.DataFrame([{
            "Start": hms(w["start_ts"]),
            "Samples": w["n"],
            "Router p95 (ms)": w["local_p95_ms"],
            "Router loss (%)": w["local_loss_pct"],
            "External p95 (ms)": w["external_p95_ms"],
            "External loss (%)": w["external_loss_pct"],
            "RSSI (dBm)": w["rssi_dbm_median"],
            "Link rate (Mbps)": w["tx_rate_mbps_median"],
            "Window result": LABELS.get(w["state"], w["state"]),
            "Status": LABELS.get(w["status"], w["status"]),
            "Reasons": "; ".join(w["reasons"]),
        } for w in reversed(windows)]), hide_index=True, use_container_width=True,
            column_config={c: st.column_config.NumberColumn(format="%.1f") for c in
                           ["Router p95 (ms)", "Router loss (%)", "External p95 (ms)",
                            "External loss (%)", "RSSI (dBm)", "Link rate (Mbps)"]})


live_view()
