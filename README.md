# Wi-Fi Congestion Detector (CS576 Group 7)

An explainable prototype that watches a Wi-Fi link, learns what "normal" looks
like, and flags **possible congestion**, **weak signal**, or a **WAN / external
issue**, with the evidence behind each alert. It runs on a single Mac: no
Raspberry Pi, wired server, or router access required (they're optional extras).

```
 Mac on Wi-Fi ──ping──▶ router (local path)
              ──ping──▶ 1.1.1.1 (external path)
              ──read──▶ Wi-Fi radio stats (CoreWLAN)
        │
        ▼
 collector ──▶ SQLite (data/wifi.db) ──▶ detector (30 s windows + rules) ──▶ dashboard
```

## Setup (once)

Requires macOS and Python 3.10+.

```bash
python3 -m venv .venv
.venv/bin/pip install -r requirements.txt
.venv/bin/python hardware_check.py          # what this Mac can measure
```

Optional: `brew install iperf3` for throughput tests (also needs a machine running
`iperf3 -s`, ideally plugged into the router by Ethernet).

## Quick start (live demo)

Run each in its own terminal:

```bash
.venv/bin/python -m collector run --label live      # sample once per second
.venv/bin/python -m detector watch                  # live window-by-window verdicts
.venv/bin/streamlit run dashboard/app.py            # dashboard at http://localhost:8501
```

With no saved baseline, the detector uses the run's own first 2 minutes. For
real tests, save one first (see below).

## Controlled tests (slide 11)

6 conditions × 3 runs × 5 minutes, randomized order, one run per condition held
out for evaluation: `quiet`, `load_1`, `load_2`, `load_4`, `weak_signal`, `wan_only`.

```bash
.venv/bin/python -m experiment plan --seed 1           # writes data/schedule.csv
.venv/bin/python -m experiment baseline --name desk    # 2.5 min quiet baseline for this setup
.venv/bin/python -m experiment next                    # repeat 18 times
.venv/bin/python -m experiment status
```

`next` prints the setup steps for the next condition, waits for Enter, records 5
minutes, prints the teardown steps, and marks the run done. Ctrl+C cancels a run
and leaves it pending.

- **Load conditions:** other devices on the same Wi-Fi generate heavy traffic
  (iperf3 to a LAN server, 4K video, or large downloads) for the whole run.
- **Weak signal:** move the laptop until RSSI is about −70 dBm or worse, with no extra load.
- **WAN only:** `sudo experiment/wan_shaper.sh on` adds 150 ms delay and a 2 Mbit/s
  cap to traffic to 1.1.1.1 only (macOS pf/dummynet). Turn it off afterwards with
  `sudo experiment/wan_shaper.sh off`. This *emulates* an upstream bottleneck;
  a router bandwidth limit is more realistic if your router supports one.

Record the same baseline setup (location, channel) for all runs, and re-run
`experiment baseline` if the laptop position, channel, or hardware changes.

## Evaluation (slide 12)

```bash
.venv/bin/python -m evaluation           # default thresholds
.venv/bin/python -m evaluation --tune    # tune on dev runs, score on held-out test runs
```

Writes `results/summary.md` (tables + figures), `results/metrics.json`, and PNGs:
detector comparison (precision / recall / F1), per-condition status heatmaps for
both detectors, router RTT by condition, and one example run per condition with
alerts shaded.

- **Labels:** `load_*` runs are positive (congestion); `quiet`, `weak_signal`,
  `wan_only` are negative. These are operational labels from the test condition.
- **Precision / recall:** over 30-second windows; a window is predicted positive
  while a congestion alert is active.
- **False alerts per hour:** congestion alerts during negative runs.
- **Detection delay:** run start (load onset) to the first congestion alert.
- **Data quality:** missing-sample rate, insufficient windows, probe overhead.
- **Tuning:** `--tune` grid-searches `rtt_factor`, `rtt_margin_ms` and
  `persist_windows` on dev runs only (each detector tuned separately), then reports
  the held-out test runs.

Each run is scored against the latest baseline saved before it started.

## Commands

| Command | What it does |
|---|---|
| `python hardware_check.py` | Report available metrics; saves `data/hardware_check.json` |
| `python -m collector run --label L [--duration S] [--iperf-server IP] [--lan-server IP]` | Record samples each second |
| `python -m collector mark "load start"` | Timestamped event in the running run |
| `python -m collector list` / `export RUN_ID` | List runs / export a run to CSV |
| `python -m detector baseline RUN_ID --name N` | Save a baseline from a quiet run |
| `python -m detector analyze RUN_ID --mode both [-v]` | Windows + alerts for both detectors |
| `python -m detector watch` | Live verdicts on the running run |
| `python -m experiment plan / baseline / next / status` | Guided controlled tests |
| `python -m evaluation [--tune]` | Scores, figures and `results/summary.md` |
| `streamlit run dashboard/app.py [-- --db PATH]` | Dashboard |

(Prefix Python commands with `.venv/bin/`.)

## How detection works

Samples are grouped into non-overlapping 30-second windows (iperf3 test seconds
are excluded so the monitor's own load doesn't trigger alerts).

A window is **degraded** when router p95 RTT > max(2 × baseline, baseline + 20 ms)
or router loss ≥ max(5 %, baseline + 3 %). Then:

| Router path | Signal | Verdict |
|---|---|---|
| degraded | normal | **Possible congestion** (contention likely) |
| degraded | ≤ −70 dBm or 12 dB below baseline | **Weak signal** |
| degraded | normal, but 2+ error clues | **Errors / interference** |
| degraded | unavailable | **Degraded, cause uncertain** |
| normal | — (external degraded) | **WAN / external issue** |

**Errors vs. congestion.** Congestion builds queues, so delay rises first and loss
follows. Errors (interference, corrupted frames) cause loss and make Wi-Fi fall
back to slower link rates without much queueing. With a normal signal, the
detector counts error clues: noise floor up ≥ 6 dB or SNR down ≥ 10 dB, link rate
below half of baseline, and loss above its limit while p95 RTT is not. Two or more
clues → **Errors / interference**; otherwise **Possible congestion** (a single
clue is listed in the evidence).

An alert fires after **3 consecutive** degraded windows (60–90 s delay) and
clears after 3 healthy windows. A **latency-only** detector (RTT rule alone)
runs alongside for comparison. Thresholds live in `detector/rules.py` (`Config`)
and are initial settings for calibration.

## macOS limitations

macOS does not expose wireless retries or channel busy time, and a client can't
count other devices on the network. So "possible congestion" rests on latency/loss
inflation with a normal signal rather than direct channel evidence. Missing
values are stored as NULL, never 0. A Linux client could add a `radio_linux.py`
reading `iw` station and survey counters without changing anything else.

## Layout

```
hardware_check.py       Step 0: metric availability report
collector/              probes (ping, CoreWLAN, netstat, iperf3), SQLite storage, CLI
detector/               windows.py, rules.py (baseline + rules + alerts), engine.py, CLI
dashboard/app.py        Streamlit dashboard
experiment/             controlled-test runner + WAN shaper
evaluation/             metrics.py (labels, scoring, tuning), plots.py, CLI
results/                evaluation report and figures
data/                   wifi.db, schedule.csv, exports (created on first run)
```
