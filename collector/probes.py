"""Measurement probes: ICMP ping, macOS radio stats, interface counters, iperf3."""

import json
import re
import subprocess
import time

_RTT_RE = re.compile(r"time=([\d.]+) ms")


def ping_once(host, timeout_ms=900):
    """Send one ICMP echo. Returns RTT in ms, or None if no reply (lost).

    macOS ping lingers ~1.9 s on a lost reply regardless of -W, so the process
    is killed at the deadline to keep 1-second sampling on schedule.
    """
    try:
        p = subprocess.run(
            ["ping", "-c", "1", "-W", str(timeout_ms), "-n", host],
            capture_output=True, text=True, timeout=timeout_ms / 1000,
        )
    except subprocess.TimeoutExpired:
        return None
    m = _RTT_RE.search(p.stdout)
    return float(m.group(1)) if p.returncode == 0 and m else None


class MacRadio:
    """Reads Wi-Fi link stats through CoreWLAN (pyobjc-framework-CoreWLAN)."""

    _WIDTH = {1: 20, 2: 40, 3: 80, 4: 160}

    def __init__(self):
        import CoreWLAN
        self.iface = CoreWLAN.CWWiFiClient.sharedWiFiClient().interface()
        if self.iface is None:
            raise RuntimeError("No Wi-Fi interface found")

    @property
    def name(self):
        return self.iface.interfaceName()

    def read(self):
        if not self.iface.powerOn():
            return {}
        rssi = self.iface.rssiValue()
        noise = self.iface.noiseMeasurement()
        rate = self.iface.transmitRate()
        ch = self.iface.wlanChannel()
        # macOS reports 0 when not associated: store as missing, not zero.
        return {
            "rssi_dbm": rssi or None,
            "noise_dbm": noise or None,
            "tx_rate_mbps": rate or None,
            "channel": ch.channelNumber() if ch else None,
            "channel_width": self._WIDTH.get(ch.channelWidth()) if ch else None,
        }


def interface_counters(iface):
    """Cumulative link-layer packet/error counters from netstat."""
    try:
        out = subprocess.run(["netstat", "-I", iface, "-b", "-n"],
                             capture_output=True, text=True, timeout=2).stdout
    except subprocess.TimeoutExpired:
        return {}
    for line in out.splitlines():
        parts = line.split()
        if len(parts) >= 11 and parts[0] == iface and parts[2].startswith("<Link"):
            return {"ipkts": int(parts[4]), "ierrs": int(parts[5]),
                    "opkts": int(parts[7]), "oerrs": int(parts[8])}
    return {}


def run_iperf(server, duration_s=5, download=False, port=5201):
    """Run one TCP iperf3 test. Returns a dict with mbps/retransmits or an error."""
    cmd = ["iperf3", "-c", server, "-p", str(port), "-t", str(duration_s), "-J"]
    if download:
        cmd.append("-R")
    row = {"ts": time.time(), "server": server, "duration_s": duration_s,
           "direction": "download" if download else "upload",
           "mbps": None, "retransmits": None, "error": None}
    try:
        p = subprocess.run(cmd, capture_output=True, text=True, timeout=duration_s + 15)
        data = json.loads(p.stdout or "{}")
    except FileNotFoundError:
        row["error"] = "iperf3 not installed"
        return row
    except (subprocess.TimeoutExpired, json.JSONDecodeError) as e:
        row["error"] = type(e).__name__
        return row
    if "error" in data:
        row["error"] = data["error"]
        return row
    end = data.get("end", {})
    recv = end.get("sum_received", {})
    sent = end.get("sum_sent", {})
    bps = recv.get("bits_per_second") or sent.get("bits_per_second")
    row["mbps"] = round(bps / 1e6, 2) if bps else None
    row["retransmits"] = sent.get("retransmits")
    return row
