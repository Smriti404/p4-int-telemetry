#!/usr/bin/env python3
"""
eval/benchmark.py
=================
Automated evaluation script for comparing baseline vs telemetry-driven routing.

Experiments:
  1. Increasing load (2/5/8/9 Mbps) — steady-state
  2. Bursty traffic  — 8 Mbps bursts every 2s
  3. Link degradation — netem delay + loss on primary path

Metrics collected:
  - Throughput (Mbps)
  - Packet loss (%)
  - Average per-hop queue depth (cells)
  - Flow completion time (s)
  - Reroute trigger count

Usage:
    cd /path/to/project
    sudo python3 eval/benchmark.py --mode all
    # Results written to eval/results/
"""

import argparse
import csv
import json
import os
import subprocess
import sys
import time
from datetime import datetime
from typing import List, Dict

import requests

RESULTS_DIR   = "eval/results"
COLLECTOR_URL = "http://localhost:5000"
H1_IP         = "10.0.1.1"
H2_IP         = "10.0.2.1"
IPERF_PORT    = 5201
DURATION_SEC  = 10      # per iperf3 run

os.makedirs(RESULTS_DIR, exist_ok=True)


# ─────────────────────────────────────────────────────────────
# Helpers
# ─────────────────────────────────────────────────────────────

def mn_cmd(host: str, cmd: str, background: bool = False) -> str:
    """Run a command on a Mininet host via 'mn --cmd'."""
    flag = "&" if background else ""
    full = f"m {host} {cmd} {flag}"
    result = subprocess.run(["mnexec", "-a", host, "bash", "-c", cmd],
                            capture_output=True, text=True)
    return result.stdout + result.stderr


def run_iperf(bandwidth_mbps: float, duration: int = DURATION_SEC,
              udp: bool = True) -> dict:
    """
    Run iperf3 from h1 to h2 and return parsed results.
    Starts server on h2, runs client on h1.
    """
    proto = "-u" if udp else ""
    bw    = f"{bandwidth_mbps}M"

    # Kill any lingering iperf3
    mn_cmd("h2", "pkill iperf3 2>/dev/null; true")
    time.sleep(0.2)

    # Start server
    mn_cmd("h2", f"iperf3 -s -p {IPERF_PORT} -D")
    time.sleep(0.5)

    # Run client, get JSON output
    client_out = mn_cmd("h1",
        f"iperf3 -c {H2_IP} -p {IPERF_PORT} {proto} "
        f"-b {bw} -t {duration} -J"
    )

    try:
        data = json.loads(client_out)
        if udp:
            end = data["end"]["sum"]
            return {
                "bps":          end.get("bits_per_second", 0) / 1e6,
                "loss_pct":     end.get("lost_percent", 0),
                "jitter_ms":    end.get("jitter_ms", 0),
                "pkts_sent":    end.get("packets", 0),
                "pkts_lost":    end.get("lost_packets", 0),
            }
        else:
            end = data["end"]["sum_received"]
            return {
                "bps":          end.get("bits_per_second", 0) / 1e6,
                "loss_pct":     0,
                "jitter_ms":    0,
                "pkts_sent":    0,
                "pkts_lost":    0,
            }
    except (json.JSONDecodeError, KeyError) as e:
        print(f"  [!] iperf3 parse error: {e}")
        return {"bps": 0, "loss_pct": 100, "jitter_ms": 0,
                "pkts_sent": 0, "pkts_lost": 0}


def get_queue_stats() -> dict:
    """Fetch current telemetry stats from collector."""
    try:
        r = requests.get(f"{COLLECTOR_URL}/stats", timeout=2)
        return r.json()
    except Exception:
        return {}


def get_reroute_count() -> int:
    """Count currently rerouted flows."""
    try:
        r = requests.get(f"{COLLECTOR_URL}/congestion", timeout=2)
        return len(r.json().get("congested", []))
    except Exception:
        return 0


def avg_queue_depth() -> float:
    stats = get_queue_stats()
    if not stats:
        return 0.0
    depths = [v["avg_enq_qdepth"] for v in stats.values()]
    return sum(depths) / len(depths) if depths else 0.0


def apply_link_netem(delay_ms: int = 0, loss_pct: float = 0.0):
    """Apply tc netem on s1-eth2 (primary path first hop) inside Mininet."""
    if delay_ms == 0 and loss_pct == 0:
        mn_cmd("s1", "tc qdisc del dev s1-eth2 root 2>/dev/null; true")
    else:
        mn_cmd("s1",
            f"tc qdisc replace dev s1-eth2 root netem "
            f"delay {delay_ms}ms loss {loss_pct}%"
        )
    time.sleep(0.3)


def save_csv(filename: str, rows: List[Dict]):
    path = os.path.join(RESULTS_DIR, filename)
    if not rows:
        return
    with open(path, "w", newline="") as f:
        writer = csv.DictWriter(f, fieldnames=rows[0].keys())
        writer.writeheader()
        writer.writerows(rows)
    print(f"  → Saved {path}")


# ─────────────────────────────────────────────────────────────
# Experiment 1: Increasing Load
# ─────────────────────────────────────────────────────────────

def exp_increasing_load(mode: str) -> List[Dict]:
    """
    mode: "baseline" (policy off) or "telemetry" (policy on)
    """
    print(f"\n{'='*60}")
    print(f"  Experiment 1: Increasing Load  [{mode}]")
    print(f"{'='*60}")

    loads = [2, 4, 6, 8, 9]   # Mbps
    rows  = []

    for bw in loads:
        print(f"  → {bw} Mbps ... ", end="", flush=True)
        t_start   = time.time()
        result    = run_iperf(bw, duration=DURATION_SEC)
        t_elapsed = time.time() - t_start
        q_depth   = avg_queue_depth()

        row = {
            "mode":          mode,
            "offered_mbps":  bw,
            "actual_mbps":   round(result["bps"], 2),
            "loss_pct":      round(result["loss_pct"], 2),
            "jitter_ms":     round(result["jitter_ms"], 2),
            "avg_qdepth":    round(q_depth, 1),
            "rerouted_flows": get_reroute_count(),
            "elapsed_s":     round(t_elapsed, 1),
        }
        rows.append(row)
        print(f"{result['bps']:.2f} Mbps  loss={result['loss_pct']:.1f}%  "
              f"q={q_depth:.0f} cells")

    return rows


# ─────────────────────────────────────────────────────────────
# Experiment 2: Bursty Traffic
# ─────────────────────────────────────────────────────────────

def exp_bursty_traffic(mode: str) -> List[Dict]:
    print(f"\n{'='*60}")
    print(f"  Experiment 2: Bursty Traffic  [{mode}]")
    print(f"{'='*60}")

    BURST_BW     = 9       # Mbps during burst
    IDLE_BW      = 1       # Mbps between bursts
    BURST_DUR    = 2       # seconds
    IDLE_DUR     = 2       # seconds
    N_BURSTS     = 5

    rows = []
    for i in range(N_BURSTS):
        # Burst phase
        print(f"  Burst {i+1}/{N_BURSTS}: {BURST_BW} Mbps ... ", end="", flush=True)
        result  = run_iperf(BURST_BW, duration=BURST_DUR)
        q_depth = avg_queue_depth()
        print(f"{result['bps']:.1f} Mbps  loss={result['loss_pct']:.1f}%  "
              f"q={q_depth:.0f}")

        rows.append({
            "mode":     mode,
            "phase":    "burst",
            "burst_n":  i + 1,
            "bps":      round(result["bps"], 2),
            "loss_pct": round(result["loss_pct"], 2),
            "avg_qdepth": round(q_depth, 1),
            "rerouted": get_reroute_count(),
        })

        # Idle phase
        print(f"  Idle:  {IDLE_BW} Mbps ...", end="", flush=True)
        result  = run_iperf(IDLE_BW, duration=IDLE_DUR)
        q_depth = avg_queue_depth()
        print(f" {result['bps']:.1f} Mbps  q={q_depth:.0f}")

        rows.append({
            "mode":     mode,
            "phase":    "idle",
            "burst_n":  i + 1,
            "bps":      round(result["bps"], 2),
            "loss_pct": round(result["loss_pct"], 2),
            "avg_qdepth": round(q_depth, 1),
            "rerouted": get_reroute_count(),
        })

    return rows


# ─────────────────────────────────────────────────────────────
# Experiment 3: Link Degradation
# ─────────────────────────────────────────────────────────────

def exp_link_degradation(mode: str) -> List[Dict]:
    print(f"\n{'='*60}")
    print(f"  Experiment 3: Link Degradation  [{mode}]")
    print(f"{'='*60}")

    scenarios = [
        {"label": "baseline_clean",  "delay_ms": 0,  "loss_pct": 0},
        {"label": "delay_10ms",      "delay_ms": 10, "loss_pct": 0},
        {"label": "delay_50ms",      "delay_ms": 50, "loss_pct": 0},
        {"label": "loss_1pct",       "delay_ms": 0,  "loss_pct": 1},
        {"label": "loss_5pct",       "delay_ms": 0,  "loss_pct": 5},
        {"label": "delay50_loss5",   "delay_ms": 50, "loss_pct": 5},
    ]

    rows = []
    for sc in scenarios:
        apply_link_netem(sc["delay_ms"], sc["loss_pct"])
        print(f"  → {sc['label']} ... ", end="", flush=True)
        time.sleep(0.5)  # let netem settle

        result  = run_iperf(8, duration=DURATION_SEC)  # fixed 8 Mbps load
        q_depth = avg_queue_depth()

        print(f"{result['bps']:.1f} Mbps  loss={result['loss_pct']:.1f}%  "
              f"q={q_depth:.0f}  rerouted={get_reroute_count()}")

        rows.append({
            "mode":         mode,
            "scenario":     sc["label"],
            "netem_delay":  sc["delay_ms"],
            "netem_loss":   sc["loss_pct"],
            "bps":          round(result["bps"], 2),
            "loss_pct":     round(result["loss_pct"], 2),
            "avg_qdepth":   round(q_depth, 1),
            "rerouted":     get_reroute_count(),
        })

    # Clean up netem
    apply_link_netem(0, 0)
    return rows


# ─────────────────────────────────────────────────────────────
# Report generator
# ─────────────────────────────────────────────────────────────

def print_summary(label: str, rows_base: List[Dict], rows_tele: List[Dict]):
    print(f"\n{'='*70}")
    print(f"  Summary: {label}")
    print(f"{'='*70}")
    print(f"  {'Offered':>10}  {'Base Mbps':>10}  {'Tele Mbps':>10}  "
          f"{'Base Loss%':>11}  {'Tele Loss%':>11}  {'Base Q':>8}  {'Tele Q':>8}")
    print(f"  {'-'*80}")

    for b, t in zip(rows_base, rows_tele):
        offered = b.get("offered_mbps", b.get("phase", "?"))
        print(f"  {str(offered):>10}  {b.get('bps',b.get('actual_mbps',0)):>10.2f}  "
              f"{t.get('bps',t.get('actual_mbps',0)):>10.2f}  "
              f"{b.get('loss_pct',0):>11.2f}  {t.get('loss_pct',0):>11.2f}  "
              f"{b.get('avg_qdepth',0):>8.0f}  {t.get('avg_qdepth',0):>8.0f}")


# ─────────────────────────────────────────────────────────────
# Main
# ─────────────────────────────────────────────────────────────

def main():
    parser = argparse.ArgumentParser(description="P4 Telemetry Benchmark")
    parser.add_argument("--mode", choices=["load", "burst", "degrade", "all"],
                        default="all")
    args = parser.parse_args()

    ts = datetime.now().strftime("%Y%m%d_%H%M%S")

    # ── Check collector is up ─────────────────────────────────
    try:
        requests.get(f"{COLLECTOR_URL}/health", timeout=2)
    except Exception:
        print(f"[!] Collector not reachable at {COLLECTOR_URL}")
        print("    Start it first: python3 controller/collector.py")
        sys.exit(1)

    print(f"\n[*] P4 Telemetry Evaluation — {ts}")
    print(f"[*] Results will be saved to {RESULTS_DIR}/")

    # ────────────────────────────────────────────────────────
    # Each experiment runs TWICE:
    #   1. baseline mode: policy loop stopped, only LPM forwarding
    #   2. telemetry mode: policy loop running, congestion-aware rerouting
    #
    # In this automated script we assume the controller is running.
    # For baseline: manually stop the policy (Ctrl+C the controller
    #               and restart with --no-policy), then rerun with --mode X.
    # ────────────────────────────────────────────────────────

    if args.mode in ("load", "all"):
        rows_base  = exp_increasing_load("baseline")
        # Ideally restart controller in --no-policy mode between runs
        rows_tele  = exp_increasing_load("telemetry")
        all_rows   = rows_base + rows_tele
        save_csv(f"exp1_load_{ts}.csv", all_rows)
        print_summary("Increasing Load", rows_base, rows_tele)

    if args.mode in ("burst", "all"):
        rows_base  = exp_bursty_traffic("baseline")
        rows_tele  = exp_bursty_traffic("telemetry")
        all_rows   = rows_base + rows_tele
        save_csv(f"exp2_burst_{ts}.csv", all_rows)

    if args.mode in ("degrade", "all"):
        rows_base  = exp_link_degradation("baseline")
        rows_tele  = exp_link_degradation("telemetry")
        all_rows   = rows_base + rows_tele
        save_csv(f"exp3_degrade_{ts}.csv", all_rows)
        print_summary("Link Degradation", rows_base, rows_tele)

    print(f"\n[✓] Evaluation complete. Results in {RESULTS_DIR}/\n")


if __name__ == "__main__":
    main()
