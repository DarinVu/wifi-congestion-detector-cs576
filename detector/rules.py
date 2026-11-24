"""Baseline learning and explainable detection rules.

Starting rule (slide 10), adapted to macOS where retries and channel busy time
are not exposed:

    local window degraded  :=  p95 RTT > max(2 x baseline, baseline + 20 ms)
                               OR loss >= max(5%, baseline loss + 3%)
    signal evidence        :=  RSSI vs. baseline / absolute floor

    local degraded + weak signal     -> weak signal
    local degraded + 2+ error clues  -> errors / interference
    local degraded + normal signal   -> possible congestion (contention likely)
    local degraded + no radio data   -> degraded, cause uncertain
    only external path degraded      -> WAN / external issue

Error clues (signal strength is fine, but frames are being corrupted):
    noise up >= 6 dB or SNR down >= 10 dB vs. baseline   (interference)
    link rate below half of baseline                       (rate adaptation backing off)
    loss above its limit while p95 RTT is not              (loss without queueing delay)
Congestion builds queues (delay first, loss later); errors cause loss and rate
fallback without much queueing. Two clues are required so one noisy reading
doesn't turn congestion into interference.

An alert fires after 3 consecutive degraded windows and clears after 3 healthy
windows. All thresholds are initial settings for calibration.
"""

import statistics
from collections import Counter
from dataclasses import asdict, dataclass

LABELS = {
    "healthy": "Healthy",
    "insufficient": "Insufficient data",
    "congestion": "Possible congestion",
    "weak_signal": "Weak signal",
    "interference": "Errors / interference",
    "wan_issue": "WAN / external issue",
    "degraded_uncertain": "Degraded, cause uncertain",
    "latency": "Latency alert",           # latency-only comparison detector
}
DEGRADED_STATES = {"congestion", "weak_signal", "interference", "wan_issue",
                   "degraded_uncertain", "latency"}


@dataclass
class Config:
    rtt_factor: float = 2.0          # p95 RTT > factor x baseline ...
    rtt_margin_ms: float = 20.0      # ... and > baseline + margin
    loss_floor_pct: float = 5.0      # loss >= max(floor, baseline + margin)
    loss_margin_pct: float = 3.0
    weak_rssi_dbm: float = -70.0     # RSSI at or below this is weak ...
    rssi_drop_db: float = 12.0       # ... or this far below baseline
    rate_drop_frac: float = 0.5      # link rate below half of baseline -> error clue
    noise_rise_db: float = 6.0       # noise floor this far above baseline -> error clue
    snr_drop_db: float = 10.0        # SNR this far below baseline -> error clue
    error_clues_needed: int = 2      # clues needed to call it errors / interference
    persist_windows: int = 3         # degraded windows needed to alert
    clear_windows: int = 3           # healthy windows needed to clear


# ------------------------------------------------------------------ baseline

def compute_baseline(windows):
    """Baseline statistics from a quiet run's windows (medians across windows)."""
    good = [w for w in windows if w["sufficient"]]
    if len(good) < 2:
        raise ValueError(f"Need at least 2 complete 30 s windows for a baseline, got {len(good)}")

    def med(key):
        vals = [w[key] for w in good if w[key] is not None]
        return round(statistics.median(vals), 3) if vals else None

    def mean(key):
        vals = [w[key] for w in good if w[key] is not None]
        return round(statistics.fmean(vals), 3) if vals else None

    return {
        "n_windows": len(good),
        "local_p95_ms": med("local_p95_ms"),
        "local_loss_pct": mean("local_loss_pct"),
        "external_p95_ms": med("external_p95_ms"),
        "external_loss_pct": mean("external_loss_pct"),
        "lan_p95_ms": med("lan_p95_ms"),
        "lan_loss_pct": mean("lan_loss_pct"),
        "rssi_dbm": med("rssi_dbm_median"),
        "noise_dbm": med("noise_dbm_median"),
        "snr_db": med("snr_db_median"),
        "tx_rate_mbps": med("tx_rate_mbps_median"),
    }


# ------------------------------------------------------------------ per-window rules

def _rtt_limit(base_p95, cfg):
    return max(cfg.rtt_factor * base_p95, base_p95 + cfg.rtt_margin_ms)


def _path_check(w, base, path, cfg, use_loss=True):
    """Return (degraded?, [reasons]) for one ping path."""
    p95, loss = w[f"{path}_p95_ms"], w[f"{path}_loss_pct"]
    b95, bloss = base.get(f"{path}_p95_ms"), base.get(f"{path}_loss_pct") or 0.0
    if loss is None or b95 is None:
        return False, []
    reasons = []
    name = {"local": "router", "external": "external", "lan": "LAN server"}[path]
    if p95 is not None and p95 > _rtt_limit(b95, cfg):
        reasons.append(f"{name} p95 RTT {p95:.0f} ms vs {b95:.0f} ms baseline "
                       f"(limit {_rtt_limit(b95, cfg):.0f} ms)")
    loss_limit = max(cfg.loss_floor_pct, bloss + cfg.loss_margin_pct)
    if use_loss and loss >= loss_limit:
        reasons.append(f"{name} loss {loss:.0f}% (limit {loss_limit:.0f}%)")
    return bool(reasons), reasons


def error_clues(w, base, cfg=Config()):
    """Evidence that frames are being corrupted (errors) rather than queued (congestion)."""
    clues = []
    noise, bnoise = w["noise_dbm_median"], base.get("noise_dbm")
    if noise is not None and bnoise is not None and noise - bnoise >= cfg.noise_rise_db:
        clues.append(f"noise floor up {noise - bnoise:.0f} dB ({noise:.0f} vs {bnoise:.0f} dBm)")
    else:
        snr, bsnr = w["snr_db_median"], base.get("snr_db")
        if bsnr is None and base.get("rssi_dbm") is not None and bnoise is not None:
            bsnr = base["rssi_dbm"] - bnoise          # baselines saved before snr_db existed
        if snr is not None and bsnr is not None and bsnr - snr >= cfg.snr_drop_db:
            clues.append(f"SNR down {bsnr - snr:.0f} dB ({snr:.0f} vs {bsnr:.0f} dB)")
    rate, brate = w["tx_rate_mbps_median"], base.get("tx_rate_mbps")
    if rate is not None and brate and rate < cfg.rate_drop_frac * brate:
        clues.append(f"link rate fell to {rate:.0f} Mbps from {brate:.0f} Mbps")
    for path, name in (("local", "router"), ("lan", "LAN server")):
        loss, p95v = w[f"{path}_loss_pct"], w[f"{path}_p95_ms"]
        b95, bloss = base.get(f"{path}_p95_ms"), base.get(f"{path}_loss_pct") or 0.0
        if loss is None or b95 is None:
            continue
        lossy = loss >= max(cfg.loss_floor_pct, bloss + cfg.loss_margin_pct)
        if lossy and (p95v is None or p95v <= _rtt_limit(b95, cfg)):
            clues.append(f"{name} loss {loss:.0f}% without queueing delay")
            break
    return clues


def classify(w, base, cfg=Config(), mode="multi_signal"):
    """Classify one window. Returns (state, reasons)."""
    if not w["sufficient"]:
        return "insufficient", [f"only {w['n']} samples in window"]

    if mode == "latency_only":
        deg, reasons = _path_check(w, base, "local", cfg, use_loss=False)
        return ("latency" if deg else "healthy"), reasons

    local_deg, reasons = _path_check(w, base, "local", cfg)
    lan_deg, lan_reasons = _path_check(w, base, "lan", cfg)
    if lan_deg:
        local_deg, reasons = True, reasons + lan_reasons
    ext_deg, ext_reasons = _path_check(w, base, "external", cfg)

    rssi, brssi = w["rssi_dbm_median"], base.get("rssi_dbm")

    if local_deg:
        if rssi is None:
            return "degraded_uncertain", reasons + ["no signal data available"]
        weak = rssi <= cfg.weak_rssi_dbm or (brssi is not None and brssi - rssi >= cfg.rssi_drop_db)
        if weak:
            return "weak_signal", reasons + [
                f"signal {rssi:.0f} dBm" + (f" vs {brssi:.0f} dBm baseline" if brssi is not None else "")]
        signal = (f"signal normal ({rssi:.0f} dBm"
                  + (f" vs {brssi:.0f} dBm baseline)" if brssi is not None else ")"))
        clues = error_clues(w, base, cfg)
        if len(clues) >= cfg.error_clues_needed:
            return "interference", reasons + [signal] + clues
        evidence = [signal, "channel use / retries not available on macOS"]
        if clues:
            evidence.append(f"only {len(clues)} error clue ({clues[0]}), not enough for interference")
        if ext_deg:
            evidence.append("external path also degraded")
        return "congestion", reasons + evidence

    if ext_deg:
        return "wan_issue", ext_reasons + ["router path normal, so the slowdown is beyond the local Wi-Fi"]
    return "healthy", []


# ------------------------------------------------------------------ alert state machine

def detect(windows, base, cfg=Config(), mode="multi_signal"):
    """Run the persistence/clearing state machine over windows.

    Returns (per_window, alerts). per_window rows carry state, reasons and the
    displayed status; alerts are dicts ready for the alerts table.
    """
    per_window, alerts = [], []
    active = None           # current alert dict
    recent = []             # last degraded states in the current streak
    healthy_streak = 0

    for w in windows:
        state, reasons = classify(w, base, cfg, mode)
        if state == "insufficient":
            pass  # pause: neither extends nor breaks a streak
        elif state in DEGRADED_STATES:
            healthy_streak = 0
            recent.append(state)
            last = recent[-cfg.persist_windows:]
            if active is None and len(recent) >= cfg.persist_windows:
                kind = Counter(last).most_common(1)[0][0] if len(set(last)) > 1 else last[-1]
                active = _open(w, kind, reasons, cfg, mode)
                alerts.append(active)
            elif active is not None and len(last) == cfg.persist_windows \
                    and len(set(last)) == 1 and last[0] != active["kind"]:
                # Cause changed persistently: close and reopen with the new kind.
                active["end_ts"] = w["end_ts"]
                active = _open(w, last[0], reasons, cfg, mode)
                alerts.append(active)
        else:  # healthy
            healthy_streak += 1
            if active is None:
                recent = []
            elif healthy_streak >= cfg.clear_windows:
                active["end_ts"] = w["end_ts"]
                active, recent = None, []

        if active is not None:
            status = active["kind"]
        elif recent:
            status = f"watching ({len(recent)}/{cfg.persist_windows})"
        else:
            status = "healthy"
        per_window.append({**w, "state": state, "reasons": reasons, "status": status})

    return per_window, alerts


def _open(w, kind, reasons, cfg, mode):
    return {
        "mode": mode,
        "kind": kind,
        "start_ts": w["end_ts"] - cfg.persist_windows * (w["end_ts"] - w["start_ts"]),
        "alert_ts": w["end_ts"],
        "end_ts": None,
        "explanation": f"{LABELS[kind]}: " + "; ".join(reasons),
    }


def config_dict(cfg):
    return asdict(cfg)
