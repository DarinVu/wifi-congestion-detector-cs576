#!/usr/bin/env python3
"""Step 0: macOS hardware check for the Wi-Fi Congestion Detector.

Reports which metrics this Mac can actually collect, how fast it can read
them, and which ones are unavailable. Missing metrics are reported as
unavailable, never as zero.

Usage:
    .venv/bin/python hardware_check.py [--external 1.1.1.1] [--samples 10]

Writes a JSON summary to data/hardware_check.json.
"""

import argparse
import json
import platform
import re
import shutil
import statistics
import subprocess
import time
from datetime import datetime, timezone
from pathlib import Path

OK, WARN, MISSING = "available", "partial", "unavailable"


def run(cmd, timeout=10):
    try:
        p = subprocess.run(cmd, capture_output=True, text=True, timeout=timeout)
        return p.returncode, p.stdout, p.stderr
    except (subprocess.TimeoutExpired, FileNotFoundError) as e:
        return -1, "", str(e)


# ---------------------------------------------------------------- network basics

def wifi_interface():
    """Return the device name of the Wi-Fi hardware port (usually en0)."""
    _, out, _ = run(["networksetup", "-listallhardwareports"])
    m = re.search(r"Hardware Port: Wi-Fi\s*\nDevice: (\S+)", out)
    return m.group(1) if m else None


def default_route():
    _, out, _ = run(["route", "-n", "get", "default"])
    gw = re.search(r"gateway: (\S+)", out)
    iface = re.search(r"interface: (\S+)", out)
    return (gw.group(1) if gw else None, iface.group(1) if iface else None)


def ping_once(host, timeout_ms=1000):
    """One ICMP echo. Returns RTT in ms, or None if no reply."""
    code, out, _ = run(["ping", "-c", "1", "-W", str(timeout_ms), "-n", host],
                       timeout=timeout_ms / 1000 + 2)
    m = re.search(r"time=([\d.]+) ms", out)
    return float(m.group(1)) if code == 0 and m else None


def ping_test(host, count):
    rtts = []
    for _ in range(count):
        rtts.append(ping_once(host))
        time.sleep(0.2)
    got = [r for r in rtts if r is not None]
    return {
        "host": host,
        "sent": count,
        "received": len(got),
        "loss_pct": round(100 * (count - len(got)) / count, 1),
        "rtt_min_ms": round(min(got), 2) if got else None,
        "rtt_median_ms": round(statistics.median(got), 2) if got else None,
        "rtt_max_ms": round(max(got), 2) if got else None,
        "status": OK if got else MISSING,
    }


# ---------------------------------------------------------------- radio (CoreWLAN)

def corewlan_check(samples):
    try:
        import CoreWLAN  # from pyobjc-framework-CoreWLAN
    except ImportError:
        return {"status": MISSING,
                "error": "pyobjc-framework-CoreWLAN not installed "
                         "(run: .venv/bin/pip install -r requirements.txt)"}

    iface = CoreWLAN.CWWiFiClient.sharedWiFiClient().interface()
    if iface is None:
        return {"status": MISSING, "error": "No Wi-Fi interface found"}
    if not iface.powerOn():
        return {"status": MISSING, "error": "Wi-Fi is turned off"}

    def read():
        ch = iface.wlanChannel()
        return {
            "rssi_dbm": iface.rssiValue(),
            "noise_dbm": iface.noiseMeasurement(),
            "tx_rate_mbps": iface.transmitRate(),
            "tx_power_mw": iface.transmitPower(),
            "channel": ch.channelNumber() if ch else None,
            "channel_band": {1: "2.4GHz", 2: "5GHz", 3: "6GHz"}.get(ch.channelBand()) if ch else None,
            "channel_width_mhz": {1: 20, 2: 40, 3: 80, 4: 160}.get(ch.channelWidth()) if ch else None,
            "phy_mode": {1: "802.11a", 2: "802.11b", 3: "802.11g", 4: "802.11n",
                         5: "802.11ac", 6: "802.11ax", 7: "802.11be"}.get(iface.activePHYMode()),
        }

    reads, times = [], []
    for _ in range(samples):
        t = time.perf_counter()
        reads.append(read())
        times.append((time.perf_counter() - t) * 1000)
        time.sleep(0.2)

    # 0 RSSI / noise means "not associated" on macOS, so treat it as missing.
    rssi = [r["rssi_dbm"] for r in reads if r["rssi_dbm"]]
    noise = [r["noise_dbm"] for r in reads if r["noise_dbm"]]
    latest = reads[-1]

    fields = {
        "signal_rssi": OK if rssi else MISSING,
        "noise": OK if noise else MISSING,
        "tx_rate": OK if latest["tx_rate_mbps"] else MISSING,
        "channel": OK if latest["channel"] else MISSING,
        # SSID/BSSID need Location Services permission; not needed for the project.
        "ssid_bssid": OK if iface.ssid() else MISSING,
    }
    return {
        "status": OK if rssi else MISSING,
        "interface": iface.interfaceName(),
        "fields": fields,
        "latest": latest,
        "rssi_range_dbm": [min(rssi), max(rssi)] if rssi else None,
        "snr_db": (latest["rssi_dbm"] - latest["noise_dbm"])
                  if latest["rssi_dbm"] and latest["noise_dbm"] else None,
        "read_time_ms_median": round(statistics.median(times), 2),
    }


# ---------------------------------------------------------------- interface counters

def interface_counters(iface):
    """netstat link-layer counters: packets and errors (not Wi-Fi retries)."""
    _, out, _ = run(["netstat", "-I", iface, "-b", "-n"])
    for line in out.splitlines():
        parts = line.split()
        if len(parts) >= 11 and parts[0] == iface and parts[2].startswith("<Link"):
            # Name Mtu Network Address Ipkts Ierrs Ibytes Opkts Oerrs Obytes Coll
            return {"status": OK, "ipkts": int(parts[4]), "ierrs": int(parts[5]),
                    "opkts": int(parts[7]), "oerrs": int(parts[8])}
    return {"status": MISSING}


# ---------------------------------------------------------------- report

def mark(status):
    return {OK: "[ OK ]", WARN: "[WARN]", MISSING: "[ -- ]"}[status]


def main():
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("--external", default="1.1.1.1", help="external ping target")
    ap.add_argument("--lan-server", default=None,
                    help="optional wired LAN server IP (iperf3 / ping target)")
    ap.add_argument("--samples", type=int, default=10)
    args = ap.parse_args()

    print("Wi-Fi Congestion Detector: macOS hardware check\n")

    wifi_if = wifi_interface()
    gateway, route_if = default_route()
    radio = corewlan_check(args.samples)
    local = ping_test(gateway, args.samples) if gateway else {"status": MISSING}
    external = ping_test(args.external, args.samples)
    lan = ping_test(args.lan_server, args.samples) if args.lan_server else None
    counters = interface_counters(wifi_if) if wifi_if else {"status": MISSING}
    iperf_path = shutil.which("iperf3")

    print(f"macOS {platform.mac_ver()[0]} ({platform.machine()}), Python {platform.python_version()}")
    print(f"Wi-Fi interface : {wifi_if or 'not found'}")
    print(f"Default route   : {gateway} via {route_if}"
          + ("" if route_if == wifi_if else "   <-- WARNING: default route is NOT over Wi-Fi"))
    print()

    print("Core metrics (needed for the prototype)")
    print(f"  {mark(local['status'])} Local RTT/loss (router {gateway}): "
          f"median {local.get('rtt_median_ms')} ms, loss {local.get('loss_pct')}%")
    print(f"  {mark(external['status'])} External RTT/loss ({args.external}): "
          f"median {external.get('rtt_median_ms')} ms, loss {external.get('loss_pct')}%")
    if lan:
        print(f"  {mark(lan['status'])} LAN server RTT ({args.lan_server}): "
              f"median {lan.get('rtt_median_ms')} ms, loss {lan.get('loss_pct')}%")
    print(f"  {mark(radio['status'])} Signal strength (RSSI): "
          + (f"{radio['latest']['rssi_dbm']} dBm, read in {radio['read_time_ms_median']} ms"
             if radio["status"] == OK else radio.get("error", "")))
    print(f"  {mark(OK if iperf_path else WARN)} Throughput (iperf3): "
          + (iperf_path if iperf_path else "not installed, run: brew install iperf3"))
    print()

    print("Additional context")
    if radio["status"] == OK:
        L = radio["latest"]
        print(f"  {mark(radio['fields']['noise'])} Noise: {L['noise_dbm']} dBm (SNR {radio['snr_db']} dB)")
        print(f"  {mark(radio['fields']['tx_rate'])} Transmit (PHY) rate: {L['tx_rate_mbps']} Mbps "
              "(link rate, not achieved throughput)")
        print(f"  {mark(radio['fields']['channel'])} Channel: {L['channel']} "
              f"({L['channel_band']}, {L['channel_width_mhz']} MHz, {L['phy_mode']})")
    print(f"  {mark(counters['status'])} Interface packet/error counters (netstat)"
          + (f": {counters['oerrs']} out errors, {counters['ierrs']} in errors" if counters["status"] == OK else ""))
    print(f"  {mark(MISSING)} Wireless retries: not exposed by macOS")
    print(f"  {mark(MISSING)} Channel utilization (busy time): not exposed by macOS")
    print(f"  {mark(MISSING)} Associated device count: needs router admin page / AP telemetry")
    if radio["status"] == OK and radio["fields"]["ssid_bssid"] == MISSING:
        print("  (SSID/BSSID hidden: macOS needs Location Services permission. Not needed.)")
    print()

    print("Detector implications")
    print("  - Congestion evidence must rely on RTT/loss inflation with a normal signal;")
    print("    without retries or channel use, report 'degraded, cause uncertain' when unsure.")
    print("  - Local path = router ping" + (" + LAN server." if lan else "; add --lan-server once a wired machine exists."))

    result = {
        "checked_at": datetime.now(timezone.utc).isoformat(),
        "platform": {"os": "macOS", "version": platform.mac_ver()[0],
                     "machine": platform.machine(), "python": platform.python_version()},
        "wifi_interface": wifi_if,
        "default_gateway": gateway,
        "default_route_interface": route_if,
        "ping_local": local,
        "ping_external": external,
        "ping_lan_server": lan,
        "radio": radio,
        "interface_counters": counters,
        "iperf3": iperf_path,
        "unavailable": ["wireless_retries", "channel_utilization", "associated_devices"],
    }
    out = Path(__file__).parent / "data" / "hardware_check.json"
    out.parent.mkdir(exist_ok=True)
    out.write_text(json.dumps(result, indent=2))
    print(f"\nSaved: {out}")


if __name__ == "__main__":
    main()
