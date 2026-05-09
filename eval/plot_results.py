#!/usr/bin/env python3
"""
eval/plot_results.py
====================
Generate comparison plots from benchmark CSV files.

Usage:
    python3 eval/plot_results.py --results eval/results/
    # Saves PNG plots to eval/results/plots/
"""

import argparse
import glob
import os

import matplotlib
matplotlib.use("Agg")
import matplotlib.pyplot as plt
import matplotlib.patches as mpatches
import pandas as pd

PLOT_DIR = "eval/results/plots"
os.makedirs(PLOT_DIR, exist_ok=True)

COLORS = {
    "baseline":  "#888888",
    "telemetry": "#1D9E75",
}

STYLE = {
    "figure.figsize": (9, 5),
    "axes.spines.top":   False,
    "axes.spines.right": False,
    "font.size": 11,
}
plt.rcParams.update(STYLE)


def plot_load(df: pd.DataFrame, out_dir: str):
    """Throughput and loss vs offered load."""
    fig, (ax1, ax2) = plt.subplots(1, 2, figsize=(12, 5))

    for mode in ["baseline", "telemetry"]:
        sub = df[df["mode"] == mode].sort_values("offered_mbps")
        ax1.plot(sub["offered_mbps"], sub["actual_mbps"],
                 marker="o", color=COLORS[mode], label=mode)
        ax2.plot(sub["offered_mbps"], sub["loss_pct"],
                 marker="o", color=COLORS[mode], label=mode)

    # Ideal line
    x = df["offered_mbps"].unique()
    ax1.plot(sorted(x), sorted(x), "k--", linewidth=0.8, alpha=0.4, label="ideal")

    ax1.set_xlabel("Offered load (Mbps)")
    ax1.set_ylabel("Actual throughput (Mbps)")
    ax1.set_title("Throughput vs Offered Load")
    ax1.legend()

    ax2.set_xlabel("Offered load (Mbps)")
    ax2.set_ylabel("Packet loss (%)")
    ax2.set_title("Loss vs Offered Load")
    ax2.legend()

    fig.tight_layout()
    path = os.path.join(out_dir, "exp1_load.png")
    fig.savefig(path, dpi=150)
    plt.close(fig)
    print(f"  → {path}")


def plot_queue(df: pd.DataFrame, out_dir: str):
    """Average queue depth vs offered load."""
    fig, ax = plt.subplots()

    for mode in ["baseline", "telemetry"]:
        sub = df[df["mode"] == mode].sort_values("offered_mbps")
        ax.bar(
            sub["offered_mbps"].astype(str) + f"\n({mode[:4]})",
            sub["avg_qdepth"],
            color=COLORS[mode], alpha=0.8, width=0.35
        )

    ax.axhline(800, color="red", linestyle="--", linewidth=0.8, label="threshold (800)")
    ax.set_xlabel("Offered load (Mbps) / mode")
    ax.set_ylabel("Avg queue depth (cells)")
    ax.set_title("Queue Depth vs Load")
    ax.legend()

    fig.tight_layout()
    path = os.path.join(out_dir, "exp1_qdepth.png")
    fig.savefig(path, dpi=150)
    plt.close(fig)
    print(f"  → {path}")


def plot_burst(df: pd.DataFrame, out_dir: str):
    """Queue depth over burst cycles."""
    fig, ax = plt.subplots()

    for mode in ["baseline", "telemetry"]:
        sub = df[df["mode"] == mode].reset_index(drop=True)
        x   = range(len(sub))
        colors = ["#E24B4A" if r == "burst" else "#1D9E75"
                  for r in sub["phase"]]
        ax.scatter(x, sub["avg_qdepth"],
                   c=colors, alpha=0.7, s=60, label=mode)
        ax.plot(x, sub["avg_qdepth"], color=COLORS[mode], linewidth=0.8)

    ax.axhline(800, color="red", linestyle="--", linewidth=0.8, label="threshold")
    ax.set_xlabel("Measurement index")
    ax.set_ylabel("Avg queue depth (cells)")
    ax.set_title("Queue Depth: Bursty Traffic")
    burst_patch = mpatches.Patch(color="#E24B4A", label="burst phase")
    idle_patch  = mpatches.Patch(color="#1D9E75", label="idle phase")
    ax.legend(handles=[burst_patch, idle_patch])

    fig.tight_layout()
    path = os.path.join(out_dir, "exp2_burst.png")
    fig.savefig(path, dpi=150)
    plt.close(fig)
    print(f"  → {path}")


def plot_degradation(df: pd.DataFrame, out_dir: str):
    """Throughput and loss under different netem conditions."""
    fig, (ax1, ax2) = plt.subplots(1, 2, figsize=(14, 5))

    scenarios = df["scenario"].unique()
    x = range(len(scenarios))

    for mode in ["baseline", "telemetry"]:
        sub = df[df["mode"] == mode]
        bps  = [sub[sub["scenario"] == s]["bps"].mean()      for s in scenarios]
        loss = [sub[sub["scenario"] == s]["loss_pct"].mean()  for s in scenarios]

        ax1.plot(x, bps,  marker="o", color=COLORS[mode], label=mode)
        ax2.plot(x, loss, marker="o", color=COLORS[mode], label=mode)

    ax1.set_xticks(x)
    ax1.set_xticklabels(scenarios, rotation=30, ha="right")
    ax1.set_ylabel("Throughput (Mbps)")
    ax1.set_title("Throughput under Link Degradation")
    ax1.legend()

    ax2.set_xticks(x)
    ax2.set_xticklabels(scenarios, rotation=30, ha="right")
    ax2.set_ylabel("Packet loss (%)")
    ax2.set_title("Loss under Link Degradation")
    ax2.legend()

    fig.tight_layout()
    path = os.path.join(out_dir, "exp3_degrade.png")
    fig.savefig(path, dpi=150)
    plt.close(fig)
    print(f"  → {path}")


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--results", default="eval/results")
    args = parser.parse_args()

    plot_dir = os.path.join(args.results, "plots")
    os.makedirs(plot_dir, exist_ok=True)

    print(f"Generating plots from {args.results}/ ...\n")

    # Experiment 1: load
    load_files = glob.glob(os.path.join(args.results, "exp1_load_*.csv"))
    if load_files:
        df = pd.concat([pd.read_csv(f) for f in load_files])
        plot_load(df, plot_dir)
        plot_queue(df, plot_dir)

    # Experiment 2: burst
    burst_files = glob.glob(os.path.join(args.results, "exp2_burst_*.csv"))
    if burst_files:
        df = pd.concat([pd.read_csv(f) for f in burst_files])
        plot_burst(df, plot_dir)

    # Experiment 3: degradation
    deg_files = glob.glob(os.path.join(args.results, "exp3_degrade_*.csv"))
    if deg_files:
        df = pd.concat([pd.read_csv(f) for f in deg_files])
        plot_degradation(df, plot_dir)

    print(f"\nPlots saved to {plot_dir}/")


if __name__ == "__main__":
    main()
